from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
BOARDS = ("pursers", "alpha", "beta")
WORKER_NAME = "registry-cap-worker"


def _principal_id(name: str) -> str:
    canonical = json.dumps([name, ISSUER, name], separators=(",", ":"))
    return "PR-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _jwt_fixture(root: Path, audience: str):
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    public = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public.update({"kid": "registry-cap", "alg": "RS256", "use": "sig"})
    jwks = root / "jwks.json"
    jwks.write_text(json.dumps({"keys": [public]}), encoding="utf-8")

    def issue(name: str, scopes: str) -> str:
        now = datetime.now(timezone.utc)
        return jwt.encode(
            {
                "iss": ISSUER,
                "sub": name,
                "aud": audience,
                "resource": audience,
                "scope": scopes,
                "client_id": name,
                "iat": now,
                "nbf": now - timedelta(seconds=5),
                "exp": now + timedelta(minutes=10),
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "registry-cap"},
        )

    return jwks, issue


def _decode(result) -> dict:
    return BoardClient._decode(result)


class RegistryConnectionCapTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_cap_keeps_three_registry_boards_push_only(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = int(listener.getsockname()[1])
            url = f"http://127.0.0.1:{port}/mcp"
            jwks, issue = _jwt_fixture(root, url)
            admin_token = issue(
                "registry-cap-admin",
                "board:read board:write board:review board:coordinate",
            )
            worker_token = issue(
                "registry-cap-worker", "board:read board:write"
            )
            worker_principal = _principal_id("registry-cap-worker")
            environment = {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": ISSUER,
                "CENTRAL_JWT_AUDIENCE": url,
                "CENTRAL_JWKS_PATH": str(jwks),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
            }
            server: uvicorn.Server | None = None
            thread: threading.Thread | None = None
            with patch.dict(os.environ, environment, clear=False):
                try:
                    mcp, service = central.build_server(
                        "127.0.0.1", port, root / "data"
                    )
                    app = create_streamable_http_app(
                        mcp, service, host="127.0.0.1"
                    )
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
                        target=server.run,
                        kwargs={"sockets": [listener]},
                        daemon=True,
                    )
                    thread.start()
                    deadline = time.monotonic() + 5
                    while not server.started and time.monotonic() < deadline:
                        await asyncio.sleep(0.01)
                    self.assertTrue(server.started)

                    registry = {
                        "schema_version": 1,
                        "projects": {
                            board: {
                                "board_id": board,
                                "work_dir": f"/PATH/TO/{board}",
                                "work_dir_owner": "fleet",
                                "status": "active",
                            }
                            for board in BOARDS
                        },
                    }
                    cursors: dict[str, int] = {}
                    for board in BOARDS:
                        async with BoardClient(
                            url,
                            admin_token,
                            board,
                            agent_name=f"registry-cap-admin-{board}",
                            allow_takeover=True,
                        ) as admin:
                            await admin._call(
                                "board_member_add",
                                {
                                    "agent_name": admin.agent_name,
                                    "principal_id": worker_principal,
                                    "role": "member",
                                },
                            )
                            if board == BOARDS[0]:
                                await admin.board_state_update(
                                    "project_registry",
                                    json.dumps(registry, sort_keys=True),
                                )
                            cursors[board] = int(
                                (await admin.board_snapshot())["latest_seq"]
                            )

                    bridge_env = os.environ.copy()
                    bridge_env.update(
                        {
                            "ONBOARD_CENTRAL_URL": url,
                            "ONBOARD_CENTRAL_TOKEN": worker_token,
                            "ONBOARD_BOARD_ID": BOARDS[0],
                            "ONBOARD_AGENT_NAME": WORKER_NAME,
                            "PURSERS_ROLE": "worker",
                            "PURSERS_HOST": "headless",
                            "PURSERS_CAN_WORK": "true",
                            "PURSERS_CAN_REVIEW": "false",
                            "PURSERS_BOARDS": "registry",
                            "PURSERS_HOME_BOARD": "",
                            "PURSERS_BRIDGE_STATS": str(root / "stats.json"),
                            "PURSERS_BRIDGE_STATE_DIR": str(root / "state"),
                            "PYTHONPATH": os.pathsep.join(
                                (str(CLIENT_SRC), str(ROOT))
                            ),
                        }
                    )
                    bridge_env.pop("PURSERS_CENTRAL_CONNECTION_CAP", None)
                    params = StdioServerParameters(
                        command=sys.executable,
                        args=[str(ROOT / "pursers_wait_server.py")],
                        env=bridge_env,
                    )
                    async with Client(
                        params,
                        mode="2026-07-28",
                        read_timeout_seconds=20,
                    ) as bridge:
                        first = _decode(
                            await bridge.call_tool(
                                "a2a_wait",
                                {
                                    "agent_name": WORKER_NAME,
                                    "boards": "registry",
                                    "only_mine": True,
                                    "since_seq": cursors,
                                    "timeout_s": 1,
                                    "wait_for": "claimable",
                                },
                            )
                        )
                        self.assertTrue(first["timed_out"], first)
                        self.assertEqual(first["mode"], "push", first)
                        self.assertEqual(
                            first["mode_by_board"],
                            {board: "push" for board in BOARDS},
                        )

                        target_board = BOARDS[-1]
                        worker_id = central.agent_id(
                            target_board, worker_principal, WORKER_NAME
                        )
                        async with BoardClient(
                            url,
                            admin_token,
                            target_board,
                            agent_name=f"registry-cap-admin-{target_board}",
                            allow_takeover=True,
                        ) as admin:
                            created = await admin.ticket_create(
                                None,
                                "Registry cap claimability regression",
                                description="Prove the post-timeout offer stays claimable.",
                                target_url=f"{target_board}/work",
                                scope="interactive-no-send",
                                required_fields=["test_output"],
                                assigned_to=worker_id,
                            )
                        ticket_id = created["ticket"]["ticket_id"]
                        offered = _decode(
                            await bridge.call_tool(
                                "a2a_wait",
                                {
                                    "agent_name": WORKER_NAME,
                                    "boards": "registry",
                                    "only_mine": True,
                                    "since_seq": first["new_seq"],
                                    "timeout_s": 5,
                                    "wait_for": "claimable",
                                },
                            )
                        )
                        self.assertIn(
                            ticket_id,
                            {event.get("ticket_id") for event in offered["events"]},
                        )
                        async with BoardClient(
                            url,
                            worker_token,
                            target_board,
                            agent_name=WORKER_NAME,
                            role="worker",
                            capabilities={"can_work": True, "can_review": False},
                            allow_takeover=True,
                        ) as worker:
                            claimed = await worker.ticket_claim(ticket_id)
                            fetched = await worker.ticket_get(ticket_id)
                        self.assertTrue(claimed["ok"], claimed)
                        self.assertEqual(fetched["ticket"]["status"], "claimed")
                finally:
                    if server is not None:
                        server.should_exit = True
                    if thread is not None:
                        await asyncio.to_thread(thread.join, 5)
                    listener.close()


if __name__ == "__main__":
    unittest.main()
