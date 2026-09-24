from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


MODULE_PATH = Path(__file__).resolve().parents[1] / "board_butler.py"
REPOSITORY_ROOT = MODULE_PATH.parents[2]
CENTRAL_SOURCE = REPOSITORY_ROOT / "packages" / "central" / "src"
CLIENT_SOURCE = REPOSITORY_ROOT / "packages" / "client" / "src"
for source in (CENTRAL_SOURCE, CLIENT_SOURCE):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from pursers_central import central  # noqa: E402


SPEC = importlib.util.spec_from_file_location("board_butler_real_join", MODULE_PATH)
assert SPEC and SPEC.loader
butler = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = butler
SPEC.loader.exec_module(butler)


def _free_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def _credential(root: Path, issuer: str, audience: str) -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_key.update({"kid": "board-butler-real-join", "alg": "RS256", "use": "sig"})
    (root / "jwks.json").write_text(
        json.dumps({"keys": [public_key]}), encoding="utf-8"
    )
    subject = "board-butler-real-join"
    client_id = "board-butler-real-join"
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": issuer,
            "sub": subject,
            "aud": audience,
            "resource": audience,
            "scope": "board:read board:coordinate",
            "client_id": client_id,
            "iat": now,
            "nbf": now - timedelta(seconds=5),
            "exp": now + timedelta(minutes=10),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "board-butler-real-join"},
    )
    canonical = json.dumps([client_id, issuer, subject], separators=(",", ":"))
    principal_id = "PR-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return token, principal_id


async def _seed_board(data_root: Path, port: int, principal_id: str) -> None:
    mcp, service = central.build_server("127.0.0.1", port, data_root)
    admin = central.Principal(
        "PR-real-join-admin",
        "real-join-admin",
        frozenset({"board:read", "board:write", "board:coordinate"}),
    )
    butler_principal = central.Principal(
        principal_id,
        "board-butler-real-join",
        frozenset({"board:read", "board:coordinate"}),
    )
    current = [admin]
    original_current_principal = central.current_principal
    central.current_principal = lambda: current[0]
    try:
        joined = await mcp.call_tool(
            "board_join",
            {"board_id": "butler-real-join", "agent_name": "bootstrap-admin"},
        )
        assert not joined.is_error
        admitted = await mcp.call_tool(
            "board_member_add",
            {
                "board_id": "butler-real-join",
                "agent_name": "bootstrap-admin",
                "principal_id": principal_id,
                "role": "admin",
            },
        )
        assert not admitted.is_error
        current[0] = butler_principal
        butler_join = await mcp.call_tool(
            "board_join",
            {
                "board_id": "butler-real-join",
                "agent_name": "board-butler-real-join",
                "role": "coordinator",
                "capabilities": dict(butler.BOARD_BUTLER_CAPABILITIES),
            },
        )
        assert not butler_join.is_error
        butler_agent_id = butler_join.structured_content["agent_id"]
        current[0] = admin
        for key, value in (
            (
                "project_registry",
                {
                    "schema_version": 1,
                    "projects": {
                        "butler-real-join": {
                            "board_id": "butler-real-join",
                            "work_dir": "/PATH/TO/Pursers",
                            "status": "active",
                        }
                    },
                },
            ),
            (
                central.PROJECT_COORDINATORS_STATE_KEY,
                {"butler-real-join": [butler_agent_id]},
            ),
            (
                butler.CONFIG_KEY,
                {
                    "board_butler": {
                        "schema_version": 1,
                        "boards": {
                            "butler-real-join": {
                                "mode": "active",
                                "answering_mode": "autonomous",
                                "kill_switch": False,
                                "answer_scope": {"ticket_status": "auto"},
                                "required_evidence_kinds": ["ticket_status"],
                                "hold_before_post_s": 60,
                                "active_windows": [
                                    {
                                        "days": [
                                            "mon", "tue", "wed", "thu", "fri", "sat", "sun"
                                        ],
                                        "start": "00:00",
                                        "end": "23:59",
                                        "timezone": "UTC",
                                    }
                                ],
                            }
                        },
                    }
                },
            ),
        ):
            updated = await mcp.call_tool(
                "board_state_update",
                {
                    "board_id": "butler-real-join",
                    "agent_name": "bootstrap-admin",
                    "key": key,
                    "value": json.dumps(value),
                },
            )
            assert not updated.is_error

        asked_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        decided_at = asked_at + timedelta(minutes=5)

        def seed_observation(document: dict[str, object]) -> dict[str, object]:
            document["tickets"]["TK-observed"] = {
                "ticket_id": "TK-observed",
                "title": "Real Central observer fixture",
                "description": "Independently seeded board input",
                "status": "open",
                "project": "butler-real-join",
                "priority": "medium",
                "created_at": asked_at.isoformat(),
                "updated_at": decided_at.isoformat(),
                "related_files": ["tools/board-butler/"],
                "annotations": [
                    {
                        "annotation_id": "AN-observed",
                        "kind": "decision",
                        "text": "CQ-observed — retain the existing boundary.",
                        "by": {
                            "principal_id": admin.principal_id,
                            "agent_id": "AI-bootstrap",
                            "agent_name": "bootstrap-admin",
                        },
                        "at": decided_at.isoformat(),
                    }
                ],
                "coordinator_questions": [
                    {
                        "question_id": "CQ-observed",
                        "project": "butler-real-join",
                        "asker_role": "worker",
                        "message_id": None,
                        "in_reply_to": None,
                        "message": "Which boundary applies?",
                        "kind": "information",
                        "state": "open",
                        "asked_by": {
                            "agent_id": "AI-worker",
                            "agent_name": "worker",
                            "principal_id": "PR-worker",
                        },
                        "asked_at": asked_at.isoformat(),
                        "accepted_by": None,
                        "accepted_at": None,
                        "binding": None,
                        "rebound_at": None,
                        "answer": None,
                        "answered_at": None,
                    },
                    {
                        "question_id": "CQ-answer",
                        "project": "butler-real-join",
                        "asker_role": "worker",
                        "message_id": "MSG-answer",
                        "in_reply_to": None,
                        "message": "What is the status of TK-observed?",
                        "kind": "information",
                        "state": "open",
                        "asked_by": {
                            "agent_id": "AI-worker",
                            "agent_name": "worker",
                            "principal_id": "PR-worker",
                        },
                        "asked_at": asked_at.isoformat(),
                        "accepted_by": None,
                        "accepted_at": None,
                        "binding": None,
                        "rebound_at": None,
                        "answer": None,
                        "answered_at": None,
                    },
                    *[
                        {
                            "question_id": question_id,
                            "project": "butler-real-join",
                            "asker_role": "worker",
                            "message_id": f"MSG-{question_id}",
                            "in_reply_to": None,
                            "message": "What is the status of TK-observed?",
                            "kind": "information",
                            "state": "open",
                            "asked_by": {
                                "agent_id": "AI-worker",
                                "agent_name": "worker",
                                "principal_id": "PR-worker",
                            },
                            "asked_at": asked_at.isoformat(),
                            "accepted_by": None,
                            "accepted_at": None,
                            "binding": None,
                            "rebound_at": None,
                            "answer": None,
                            "answered_at": None,
                        }
                        for question_id in (
                            "CQ-veto",
                            "CQ-failure-0",
                            "CQ-failure-1",
                            "CQ-failure-2",
                        )
                    ],
                ],
                "dispatch_history": [],
            }
            return {}

        service.mutate("butler-real-join", seed_observation)
    finally:
        central.current_principal = original_current_principal
        task = getattr(service, "recurring_reaper_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


@contextmanager
def _central_process(
    data_root: Path, port: int, environment: dict[str, str]
) -> Iterator[str]:
    command = [
        sys.executable,
        "-m",
        "pursers_central.pursers_central_runtime",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--data-dir",
        str(data_root),
        "--log-level",
        "error",
    ]
    process = subprocess.Popen(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    health_url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    f"Central exited before healthz: stdout={stdout!r} stderr={stderr!r}"
                )
            try:
                with urllib.request.urlopen(health_url, timeout=0.2) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.02)
        else:
            raise AssertionError("Central did not become healthy within 10 seconds")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_butler_reaches_first_working_state_against_real_central(
    tmp_path: Path,
) -> None:
    port = _free_port()
    issuer = f"http://127.0.0.1:{port}"
    audience = f"{issuer}/mcp"
    token, principal_id = _credential(tmp_path, issuer, audience)
    data_root = tmp_path / "central-data"
    environment = os.environ.copy()
    environment.update(
        {
            "CENTRAL_AUTH_MODE": "jwt",
            "CENTRAL_JWT_ISSUER": issuer,
            "CENTRAL_JWT_AUDIENCE": audience,
            "CENTRAL_JWKS_PATH": str(tmp_path / "jwks.json"),
            "CENTRAL_ADMISSION": "invite",
            "STORE_BACKEND": "sqlite",
        }
    )
    previous = os.environ.copy()
    os.environ.update(environment)
    try:
        asyncio.run(_seed_board(data_root, port, principal_id))
    finally:
        os.environ.clear()
        os.environ.update(previous)

    async def connect(url: str) -> None:
        options = SimpleNamespace(
            url=url,
            home_board="butler-real-join",
            agent_name="board-butler-real-join",
            dry_run=True,
            kill_switch=False,
            veto_question=None,
            repo=REPOSITORY_ROOT,
        )
        async with butler.CentralBackend(options, token) as backend:
            assert backend.client is not None
            assert backend.identity.agent_name == "board-butler-real-join"
            assert backend.identity.principal_id == principal_id
            assert backend.identity.role == "coordinator"
            snapshot = await backend.client.board_snapshot(limit=10, max_bytes=4_096)
            own = next(
                row
                for row in snapshot["agents"]
                if row["agent_id"] == backend.identity.agent_id
            )
            assert own["capabilities"] == {
                "model": None,
                "provider": None,
                "tier_max": 1,
                "skills": [],
                "can_review": False,
                "can_work": False,
                "host": None,
                "max_parallel": 1,
            }
            context = await backend._observation_context_for_board(
                "butler-real-join", snapshot, butler.utc_now()
            )
            observations = butler.derive_board_observations(context)
            stale = [
                row
                for row in observations
                if row.get("observer") == "stale_open_question"
            ]
            assert context.questions_complete is True
            observed = next(row for row in stale if row["question_id"] == "CQ-observed")
            assert observed["annotation_id"] == "AN-observed"
            await backend._write_observation_findings(
                "butler-real-join", observations, context.now
            )
            stored = await backend.client.board_state_get(butler.STATE_KEY)
            state = json.loads(stored["state"]["value"])
            written = [
                row
                for row in state["findings"]
                if row.get("kind") == butler.OBSERVATION_FINDING_KIND
            ]
            assert any(
                row["observation_key"] == observed["observation_key"] for row in written
            )

            options.dry_run = False
            options.runtime_mode = "active"
            options.act_on_board = ["butler-real-join"]
            options.project = "butler-real-join"
            options.integration_ref = "origin/main"
            options.drafts_per_hour = 5
            options.drafts_per_ticket = 10
            options.drafts_per_board = 20
            options.provider_secrets_dir = None
            drafted_at = butler.utc_now()
            pending = await backend.pending_questions()
            answerable = next(row for row in pending if row["question_id"] == "CQ-answer")
            result = await butler.process_question(
                backend, answerable, options, drafted_at
            )
            immediate_evaluation = await backend.evaluation("CQ-answer")
            immediate_audit = json.loads(immediate_evaluation["state"]["value"])[
                "evaluation"
            ]["answer_audit"]
            assert result["auto_eligible"] is True, result
            assert immediate_audit["status"] == "pending", immediate_audit
            assert result.get("answer_status") is None, immediate_audit
            still_open = await backend.question("TK-observed", "CQ-answer")
            assert still_open is not None
            assert still_open["state"] == "open"
            assert still_open["accepted_by"] is None

            result = await butler.process_question(
                backend,
                answerable,
                options,
                drafted_at + timedelta(seconds=61),
            )
            assert result.get("answer_status") == "answered", result
            answered = await backend.client.board_question_inbox(
                state="answered", ticket_id="TK-observed", limit=10
            )
            row = next(
                item for item in answered["questions"] if item["question_id"] == "CQ-answer"
            )
            assert row["answer"] == "TK-observed is open."
            assert row["answered_by"]["agent_id"] == backend.identity.agent_id
            assert row["accepted_by"] is None
            await butler.process_question(backend, row, options, butler.utc_now())
            answered_again = await backend.client.board_question_inbox(
                state="answered", ticket_id="TK-observed", limit=10
            )
            assert sum(
                item["question_id"] == "CQ-answer"
                for item in answered_again["questions"]
            ) == 1
            evaluation = await backend.evaluation("CQ-answer")
            audit = json.loads(evaluation["state"]["value"])["evaluation"][
                "answer_audit"
            ]
            assert audit["status"] == "answered"
            assert audit["event_id"].startswith("EV-")

            pending = await backend.pending_questions()
            held_ids = {
                "CQ-veto",
                "CQ-failure-0",
                "CQ-failure-1",
                "CQ-failure-2",
            }
            held = {row["question_id"]: row for row in pending if row["question_id"] in held_ids}
            assert set(held) == held_ids
            for question_id in (
                "CQ-failure-0",
                "CQ-failure-1",
                "CQ-failure-2",
                "CQ-veto",
            ):
                row = held[question_id]
                await butler.process_question(backend, row, options, drafted_at)
                current = await backend.question("TK-observed", row["question_id"])
                assert current is not None
                assert current["state"] == "open"
                assert current["accepted_by"] is None

            raw_findings = await backend.findings()
            finding_state, previous_findings = butler._decode_state(raw_findings)
            vetoed = butler.veto_question(
                finding_state, "CQ-veto", "real Central veto", drafted_at
            )
            await backend.write_findings(
                json.dumps(vetoed, sort_keys=True, separators=(",", ":")),
                previous_findings,
            )

        class FailingDeliveryBackend(butler.CentralBackend):
            answer_attempts = 0

            async def answer_question(
                self, ticket_id: str, question_id: str, message: str
            ) -> dict[str, object]:
                self.answer_attempts += 1
                raise RuntimeError("private injected delivery detail")

        async with FailingDeliveryBackend(options, token) as restarted:
            pending = await restarted.pending_questions()
            by_id = {row["question_id"]: row for row in pending}
            release_time = drafted_at + timedelta(seconds=61)
            await butler.process_question(
                restarted, by_id["CQ-veto"], options, release_time
            )
            veto_question = await restarted.question("TK-observed", "CQ-veto")
            assert veto_question is not None
            assert veto_question["state"] == "open"
            assert veto_question["accepted_by"] is None
            veto_evaluation = await restarted.evaluation("CQ-veto")
            veto_audit = json.loads(veto_evaluation["state"]["value"])["evaluation"][
                "answer_audit"
            ]
            assert veto_audit["status"] == "escalated"
            assert veto_audit["reason_code"] == "vetoed"

            for question_id in ("CQ-failure-0", "CQ-failure-1", "CQ-failure-2"):
                await butler.process_question(
                    restarted, by_id[question_id], options, release_time
                )
                evaluation = await restarted.evaluation(question_id)
                failure_audit = json.loads(evaluation["state"]["value"])[
                    "evaluation"
                ]["answer_audit"]
                if failure_audit["status"] == "pending":
                    await butler.process_question(
                        restarted,
                        by_id[question_id],
                        options,
                        release_time + timedelta(seconds=61),
                    )
                current = await restarted.question("TK-observed", question_id)
                assert current is not None
                assert current["state"] == "open"
                assert current["accepted_by"] is None
                evaluation = await restarted.evaluation(question_id)
                failure_audit = json.loads(evaluation["state"]["value"])[
                    "evaluation"
                ]["answer_audit"]
                assert failure_audit["status"] == "failed"
                assert failure_audit["reason_code"] == "central_answer_failed"

            assert restarted.answer_attempts == 3
            raw_findings = await restarted.findings()
            finding_state, _ = butler._decode_state(raw_findings)
            effective = butler.resolve_config(
                await restarted.coordinator_config(),
                options,
                finding_state,
                release_time + timedelta(seconds=61),
                project_name=restarted.project_name,
            )
            assert effective.future_active_state == "auto_demoted"
            assert effective.effective_answering_mode == "assist"
            assert "private injected delivery detail" not in json.dumps(finding_state)

    with _central_process(data_root, port, environment) as url:
        asyncio.run(connect(url))
