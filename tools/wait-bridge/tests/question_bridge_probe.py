"""Executable private probe for the stdio coordinator-question bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from mcp import Client
from mcp.client.stdio import StdioServerParameters


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
CLIENT_SRC = REPOSITORY / "packages" / "client" / "src"
CENTRAL_SRC = REPOSITORY / "packages" / "central" / "src" / "pursers_central"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(CENTRAL_SRC))
sys.path.insert(0, str(ROOT))

import central  # noqa: E402
from pursers_client import BoardClient  # noqa: E402
from runtime_health import create_streamable_http_app  # noqa: E402


ISSUER = "https://issuer.example"
BOARD = "pursers"


def principal_id(name: str) -> str:
    canonical = json.dumps([name, ISSUER, name], separators=(",", ":"))
    return "PR-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def jwt_fixture(root: Path, audience: str):
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    public = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public.update({"kid": "question-bridge", "alg": "RS256", "use": "sig"})
    jwks = root / "jwks.json"
    jwks.write_text(json.dumps({"keys": [public]}), encoding="utf-8")

    def issue(name: str, scopes: str, nonce: str = "one") -> str:
        now = datetime.now(timezone.utc)
        return jwt.encode(
            {
                "iss": ISSUER,
                "sub": name,
                "aud": audience,
                "resource": audience,
                "scope": scopes,
                "client_id": name,
                "jti": f"{name}-{nonce}",
                "iat": now,
                "nbf": now - timedelta(seconds=5),
                "exp": now + timedelta(minutes=10),
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "question-bridge"},
        )

    return jwks, issue


def bridge_params(
    root: Path, url: str, token: str, name: str, role: str
) -> StdioServerParameters:
    env = os.environ.copy()
    env.update(
        {
            "ONBOARD_CENTRAL_URL": url,
            "ONBOARD_CENTRAL_TOKEN": token,
            "ONBOARD_BOARD_ID": BOARD,
            "ONBOARD_AGENT_NAME": name,
            "PURSERS_ROLE": role,
            "PURSERS_HOST": "headless",
            "PURSERS_CAN_WORK": "true" if role == "worker" else "false",
            "PURSERS_CAN_REVIEW": "true" if role == "reviewer" else "false",
            "PURSERS_BRIDGE_STATS": str(root / f"stats-{name}.json"),
            "PURSERS_BRIDGE_STATE": str(root / f"state-{name}.json"),
            "PYTHONPATH": os.pathsep.join((str(CLIENT_SRC), str(ROOT))),
        }
    )
    return StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "pursers_wait_server.py")],
        env=env,
    )


def decode(result) -> dict:
    return BoardClient._decode(result)


async def expect_error(client: Client, name: str, arguments: dict) -> None:
    result = await client.call_tool(name, arguments)
    if not result.is_error:
        raise AssertionError(f"{name} unexpectedly accepted {sorted(arguments)}")


async def run_probe() -> None:
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
        root = Path(temporary)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = int(listener.getsockname()[1])
        url = f"http://127.0.0.1:{port}/mcp"
        jwks, issue = jwt_fixture(root, url)
        admin_token = issue(
            "probe-admin", "board:read board:write board:review board:coordinate"
        )
        worker_token = issue("probe-worker", "board:read board:write")
        reviewer_token = issue(
            "probe-reviewer", "board:read board:write board:review"
        )
        coordinator_token = issue(
            "probe-coordinator", "board:read board:write board:coordinate"
        )
        rotated_coordinator_token = issue(
            "probe-coordinator",
            "board:read board:write board:coordinate",
            "rotated",
        )
        environment = {
            "CENTRAL_AUTH_MODE": "jwt",
            "CENTRAL_JWT_ISSUER": ISSUER,
            "CENTRAL_JWT_AUDIENCE": url,
            "CENTRAL_JWKS_PATH": str(jwks),
            "CENTRAL_ADMISSION": "invite",
            "STORE_BACKEND": "sqlite",
        }
        previous = {key: os.environ.get(key) for key in environment}
        os.environ.update(environment)
        server: uvicorn.Server | None = None
        thread: threading.Thread | None = None
        try:
            mcp, service = central.build_server("127.0.0.1", port, root / "data")
            app = create_streamable_http_app(mcp, service, host="127.0.0.1")
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    log_level="error",
                    access_log=False,
                )
            )
            thread = threading.Thread(
                target=server.run, kwargs={"sockets": [listener]}, daemon=True
            )
            thread.start()
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            if not server.started:
                raise RuntimeError("disposable Central did not start")

            worker_name = "probe-worker-seat"
            reviewer_name = "probe-reviewer-seat"
            coordinator_name = "probe-coordinator-seat"
            coordinator_agent_id = central.agent_id(
                BOARD, principal_id("probe-coordinator"), coordinator_name
            )
            async with BoardClient(
                url,
                admin_token,
                BOARD,
                agent_name="probe-admin-seat",
                allow_takeover=True,
            ) as admin:
                for name, role in (
                    ("probe-worker", "member"),
                    ("probe-reviewer", "reviewer"),
                    ("probe-coordinator", "member"),
                ):
                    await admin._call(
                        "board_member_add",
                        {
                            "agent_name": admin.agent_name,
                            "principal_id": principal_id(name),
                            "role": role,
                        },
                    )
                await admin.board_state_update(
                    central.PROJECT_COORDINATORS_STATE_KEY,
                    json.dumps({BOARD: [coordinator_agent_id]}),
                )
                async with BoardClient(
                    url,
                    worker_token,
                    BOARD,
                    agent_name=worker_name,
                    role="worker",
                    capabilities={"can_work": True, "can_review": False},
                    allow_takeover=True,
                ) as setup_worker:
                    await admin.ticket_create(
                        "TK-probe-reviewer",
                        "Reviewer coordinator question",
                        assigned_to=setup_worker.identity.agent_id,
                    )
                    await setup_worker.ticket_claim("TK-probe-reviewer")
                    await setup_worker.ticket_submit(
                        "TK-probe-reviewer", summary="ready for review"
                    )
                    await admin.ticket_create(
                        "TK-probe-worker",
                        "Worker coordinator question",
                        assigned_to=setup_worker.identity.agent_id,
                    )
                    await setup_worker.ticket_claim("TK-probe-worker")
                async with BoardClient(
                    url,
                    reviewer_token,
                    BOARD,
                    agent_name=reviewer_name,
                    role="reviewer",
                    capabilities={"can_work": False, "can_review": True},
                    allow_takeover=True,
                ) as setup_reviewer:
                    await setup_reviewer.ticket_review_claim("TK-probe-reviewer")

                cursor = int((await admin.board_snapshot())["latest_seq"])
                worker_params = bridge_params(
                    root, url, worker_token, worker_name, "worker"
                )
                coordinator_params = bridge_params(
                    root, url, coordinator_token, coordinator_name, "coordinator"
                )
                async with (
                    Client(worker_params, mode="2026-07-28", read_timeout_seconds=10) as worker,
                    Client(
                        coordinator_params,
                        mode="2026-07-28",
                        read_timeout_seconds=10,
                    ) as coordinator,
                ):
                    listed = await worker.list_tools()
                    schemas = {
                        tool.name: tool.input_schema
                        for tool in listed.tools
                        if "question" in tool.name
                    }
                    serialized = json.dumps(schemas, sort_keys=True).casefold()
                    if any(secret in serialized for secret in ("host_binding", "bearer", "token")):
                        raise AssertionError("question tool schema exposes private auth input")
                    await expect_error(
                        worker,
                        "ticket_question_ask",
                        {
                            "ticket_id": "TK-probe-worker",
                            "message": "secret-input-negative",
                            "kind": "decision",
                            "host_binding": "caller-controlled",
                        },
                    )
                    await expect_error(worker, "board_question_inbox", {})
                    await expect_error(
                        coordinator,
                        "ticket_question_ask",
                        {
                            "ticket_id": "TK-probe-worker",
                            "message": "wrong role",
                            "kind": "decision",
                        },
                    )
                    await expect_error(
                        worker,
                        "ticket_question_ask",
                        {
                            "ticket_id": "TK-probe-worker",
                            "message": "wrong board override",
                            "kind": "decision",
                            "board_id": "other-project",
                        },
                    )
                    await expect_error(
                        coordinator,
                        "board_question_wait",
                        {
                            "since_seq": cursor,
                            "agent_name": "caller-selected-identity",
                        },
                    )
                    arrival = asyncio.create_task(
                        coordinator.call_tool(
                            "board_question_wait",
                            {"since_seq": cursor, "timeout_s": 5},
                        )
                    )
                    await asyncio.sleep(0)
                    asked = decode(
                        await worker.call_tool(
                            "ticket_question_ask",
                            {
                                "ticket_id": "TK-probe-worker",
                                "message": "Canary or global rollout?",
                                "kind": "decision",
                                "message_id": "probe-worker-question",
                            },
                        )
                    )
                    duplicate = decode(
                        await worker.call_tool(
                            "ticket_question_ask",
                            {
                                "ticket_id": "TK-probe-worker",
                                "message": "Canary or global rollout?",
                                "kind": "decision",
                                "message_id": "probe-worker-question",
                            },
                        )
                    )
                    if not duplicate.get("duplicate"):
                        raise AssertionError("idempotent ask retry created a duplicate")
                    arrived = decode(await arrival)
                    if arrived["question"]["question_id"] != asked["question_id"]:
                        raise AssertionError("coordinator wait returned wrong question")
                    accepted = decode(
                        await coordinator.call_tool(
                            "ticket_question_answer",
                            {
                                "ticket_id": "TK-probe-worker",
                                "question_id": asked["question_id"],
                                "action": "accept",
                            },
                        )
                    )
                    if accepted["question"]["state"] != "accepted":
                        raise AssertionError("coordinator accept did not persist")

                rotated_params = bridge_params(
                    root,
                    url,
                    rotated_coordinator_token,
                    coordinator_name,
                    "coordinator",
                )
                async with (
                    Client(worker_params, mode="2026-07-28", read_timeout_seconds=10) as worker,
                    Client(
                        rotated_params,
                        mode="2026-07-28",
                        read_timeout_seconds=10,
                    ) as coordinator,
                ):
                    answer_wait = asyncio.create_task(
                        worker.call_tool(
                            "ticket_question_wait",
                            {
                                "ticket_id": "TK-probe-worker",
                                "question_id": asked["question_id"],
                                "since_seq": asked["event"]["seq"],
                                "timeout_s": 5,
                            },
                        )
                    )
                    await asyncio.sleep(0)
                    answered = decode(
                        await coordinator.call_tool(
                            "ticket_question_answer",
                            {
                                "ticket_id": "TK-probe-worker",
                                "question_id": asked["question_id"],
                                "action": "answer",
                                "message": "Canary first",
                            },
                        )
                    )
                    delivered = decode(await answer_wait)
                    if delivered["question"]["answer"] != "Canary first":
                        raise AssertionError("asker wait returned wrong answer")
                    if "binding" in json.dumps((answered, delivered)).casefold():
                        raise AssertionError("private binding escaped a tool result")

                    reviewer_params = bridge_params(
                        root, url, reviewer_token, reviewer_name, "reviewer"
                    )
                    async with Client(
                        reviewer_params,
                        mode="2026-07-28",
                        read_timeout_seconds=10,
                    ) as reviewer:
                        review_ask = decode(
                            await reviewer.call_tool(
                                "ticket_question_ask",
                                {
                                    "ticket_id": "TK-probe-reviewer",
                                    "message": "Is the evidence scope sufficient?",
                                    "kind": "information",
                                },
                            )
                        )
                    inbox = decode(
                        await coordinator.call_tool(
                            "board_question_inbox",
                            {"ticket_id": "TK-probe-reviewer", "state": "open"},
                        )
                    )
                    if review_ask["question_id"] not in {
                        item["question_id"] for item in inbox["questions"]
                    }:
                        raise AssertionError("reviewer question was not routed to coordinator")

                snapshot = await admin.board_snapshot()
                members = {item["agent_name"]: item for item in snapshot["agents"]}
                expected = {
                    worker_name: ("worker", True, False),
                    reviewer_name: ("reviewer", False, True),
                    coordinator_name: ("coordinator", False, False),
                }
                for name, (role, can_work, can_review) in expected.items():
                    member = members[name]
                    capabilities = member["capabilities"]
                    actual = (
                        member["role"],
                        capabilities["can_work"],
                        capabilities["can_review"],
                    )
                    if actual != (role, can_work, can_review):
                        raise AssertionError(f"role/capability drift for {name}: {actual}")

            print(
                "question-bridge-probe: stdio=ok separate_credentials=4 "
                "worker_ask=ok reviewer_ask=ok coordinator_accept=ok "
                "rotated_reconnect_reply=ok correlated_wait=ok "
                "secret_surface=clean role_preservation=ok model_continuation=host-managed"
            )
        finally:
            if server is not None:
                server.should_exit = True
            if thread is not None:
                await asyncio.to_thread(thread.join, 5)
            listener.close()
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    asyncio.run(run_probe())
