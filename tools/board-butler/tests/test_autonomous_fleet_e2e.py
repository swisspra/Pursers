from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp.server.mcpserver.exceptions import ToolError


ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "autonomous_fleet_e2e_v1.json"
CENTRAL_SRC = ROOT / "packages" / "central" / "src"
CLIENT_SRC = ROOT / "packages" / "client" / "src"
for source in (CENTRAL_SRC, CLIENT_SRC):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from pursers_central import central  # noqa: E402


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


butler = _load(ROOT / "tools" / "board-butler" / "board_butler.py", "fleet_e2e_butler")
executor = _load(ROOT / "tools" / "seat-kit" / "fleet_executor.py", "fleet_e2e_executor")
dashboard = _load(ROOT / "tools" / "fleet-dashboard" / "butler_settings.py", "fleet_e2e_dashboard")


def fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class CentralScenarioBackend:
    """Use the real in-process Central tools as the Butler evidence/write seam."""

    def __init__(
        self,
        mcp: Any,
        principal_slot: list[Any],
        principal: Any,
        identity: Mapping[str, Any],
        board_id: str,
        config: Mapping[str, Any],
    ) -> None:
        self.mcp = mcp
        self.principal_slot = principal_slot
        self.principal = principal
        self.identity = SimpleNamespace(**identity)
        self.board_id = board_id
        self.project_name = board_id
        self.config = dict(config)

    async def _call(self, name: str, **arguments: Any) -> Mapping[str, Any]:
        self.principal_slot[0] = self.principal
        result = await self.mcp.call_tool(name, {"board_id": self.board_id, **arguments})
        return result.structured_content

    def _assert_board(self, board_id: str) -> None:
        if board_id != self.board_id:
            raise AssertionError("proof backend received a cross-board operation")

    async def ticket_get(
        self, ticket_id: str, *, board_id: str
    ) -> Mapping[str, Any]:
        self._assert_board(board_id)
        return await self._call("ticket_get", ticket_id=ticket_id)

    async def board_status(self) -> Mapping[str, Any]:
        return await self._call("board_snapshot", limit=100, max_bytes=250_000)

    async def answered_questions(self) -> list[dict[str, Any]]:
        result = await self._call(
            "board_question_inbox",
            agent_name=self.identity.agent_name,
            state="answered",
            limit=100,
        )
        return list(result.get("questions", []))

    async def findings(self) -> Mapping[str, Any]:
        try:
            return await self._call("board_state_get", key=butler.STATE_KEY)
        except ToolError as exc:
            if "state key not found" in str(exc).lower():
                return {}
            raise

    async def evaluation(self, question_id: str) -> Mapping[str, Any]:
        try:
            return await self._call(
                "board_state_get", key=butler.evaluation_state_key(question_id)
            )
        except ToolError as exc:
            if "state key not found" in str(exc).lower():
                return {}
            raise

    async def _write_state(
        self, key: str, value: str, expected_value: str | None
    ) -> Mapping[str, Any]:
        expected = (
            hashlib.sha256(expected_value.encode("utf-8")).hexdigest()
            if expected_value is not None
            else None
        )
        return await self._call(
            "board_state_update",
            agent_name=self.identity.agent_name,
            key=key,
            value=value,
            expected_sha256=expected,
        )

    async def write_findings(
        self, value: str, expected_value: str | None
    ) -> Mapping[str, Any]:
        return await self._write_state(butler.STATE_KEY, value, expected_value)

    async def write_evaluation(
        self, question_id: str, value: str, expected_value: str | None
    ) -> Mapping[str, Any]:
        return await self._write_state(
            butler.evaluation_state_key(question_id), value, expected_value
        )

    async def coordinator_config(self) -> Mapping[str, Any]:
        return self.config

    async def question(
        self,
        ticket_id: str,
        question_id: str,
        *,
        board_id: str,
    ) -> Mapping[str, Any] | None:
        self._assert_board(board_id)
        result = await self._call(
            "board_question_inbox",
            agent_name=self.identity.agent_name,
            ticket_id=ticket_id,
            limit=100,
        )
        return next(
            (
                {**dict(row), "board_id": self.board_id}
                for row in result.get("questions", [])
                if row.get("question_id") == question_id
            ),
            None,
        )

    async def answer_question(
        self,
        ticket_id: str,
        question_id: str,
        message: str,
        *,
        board_id: str,
    ) -> Mapping[str, Any]:
        self._assert_board(board_id)
        return await self._call(
            "ticket_question_answer",
            ticket_id=ticket_id,
            agent_name=self.identity.agent_name,
            question_id=question_id,
            action="answer",
            message=message,
            host_binding=central.current_host_binding(self.identity.agent_id),
        )

    async def release_question(
        self,
        ticket_id: str,
        question_id: str,
        *,
        board_id: str,
    ) -> Mapping[str, Any]:
        self._assert_board(board_id)
        return await self._call(
            "ticket_question_answer",
            ticket_id=ticket_id,
            agent_name=self.identity.agent_name,
            question_id=question_id,
            action="release",
            host_binding=central.current_host_binding(self.identity.agent_id),
        )


class RecordingFleetClient:
    def __init__(self) -> None:
        self.operations: list[Any] = []

    def execute(self, operation: Any) -> Mapping[str, Any]:
        self.operations.append(operation)
        return {
            "operation_id": operation.operation_id,
            "outcome": "succeeded",
            "committed": True,
        }


def _butler_args(board_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        home_board=board_id,
        repo=ROOT,
        integration_ref="origin/main",
        drafts_per_hour=20,
        drafts_per_ticket=10,
        drafts_per_board=50,
        project=board_id,
        dry_run=False,
        runtime_mode="active",
        act_on_board=[board_id],
        provider_secrets_dir=None,
    )


def test_real_board_arrival_questions_and_independent_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = fixture()
    board_id = scenario["board_id"]
    monkeypatch.setenv("CENTRAL_AUTH_MODE", "jwt")
    monkeypatch.setenv("CENTRAL_JWT_ISSUER", "https://issuer.example.invalid")
    monkeypatch.setenv("CENTRAL_JWT_AUDIENCE", "http://127.0.0.1:8765/mcp")
    jwks = tmp_path / "jwks.json"
    jwks.write_text('{"keys": []}', encoding="utf-8")
    monkeypatch.setenv("CENTRAL_JWKS_PATH", str(jwks))
    monkeypatch.setenv("CENTRAL_ADMISSION", "invite")
    monkeypatch.setenv("STORE_BACKEND", "sqlite")
    mcp, service = central.build_server("127.0.0.1", 8765, tmp_path / "central")
    principals = {
        "admin": central.Principal(
            "PR-fleet-admin",
            "fleet-admin",
            frozenset({"board:read", "board:write", "board:review", "board:coordinate"}),
        ),
        "worker": central.Principal(
            "PR-fleet-worker", "fleet-worker", frozenset({"board:read", "board:write"})
        ),
        "acp": central.Principal(
            "PR-fleet-acp", "fleet-acp-worker", frozenset({"board:read", "board:write"})
        ),
        "reviewer": central.Principal(
            "PR-fleet-reviewer",
            "fleet-reviewer",
            frozenset({"board:read", "board:review"}),
        ),
        "butler": central.Principal(
            "PR-fleet-butler",
            "fleet-butler",
            frozenset({"board:read", "board:coordinate"}),
        ),
    }
    current = [principals["admin"]]
    original_current = central.current_principal
    original_host_binding = central.current_host_binding
    central.current_principal = lambda: current[0]
    central.current_host_binding = lambda agent_id: hashlib.sha256(
        json.dumps(
            ["synthetic-private-capability", agent_id], separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()

    async def call(name: str, **arguments: Any) -> Mapping[str, Any]:
        result = await mcp.call_tool(name, {"board_id": board_id, **arguments})
        return result.structured_content

    async def run() -> None:
        now = datetime.fromisoformat(scenario["observed_at"])
        admin = await call(
            "board_join",
            agent_name="fleet-admin",
            capabilities={"can_work": False, "can_review": False},
        )
        for key, role in (("worker", "member"), ("acp", "member"), ("reviewer", "reviewer"), ("butler", "admin")):
            current[0] = principals["admin"]
            await call(
                "board_member_add",
                agent_name="fleet-admin",
                principal_id=principals[key].principal_id,
                role=role,
            )

        identities: dict[str, Mapping[str, Any]] = {"admin": admin}
        for key, name, role, capabilities in (
            (
                "worker",
                "fleet-worker",
                "worker",
                {"can_work": True, "can_review": False, "tier_max": 2, "max_parallel": 1, "provider": "direct"},
            ),
            (
                "acp",
                "fleet-acp-worker",
                "worker",
                {"can_work": True, "can_review": False, "tier_max": 3, "max_parallel": 1, "provider": "acp"},
            ),
            (
                "reviewer",
                "fleet-reviewer",
                "reviewer",
                {"can_work": False, "can_review": True, "tier_max": 3, "max_parallel": 1, "provider": "direct"},
            ),
            (
                "butler",
                "fleet-butler",
                "coordinator",
                dict(butler.BOARD_BUTLER_CAPABILITIES),
            ),
        ):
            current[0] = principals[key]
            identities[key] = await call(
                "board_join", agent_name=name, role=role, capabilities=capabilities
            )

        current[0] = principals["admin"]
        registry = {
            "schema_version": 1,
            "projects": {
                board_id: {
                    "board_id": board_id,
                    "work_dir": "/PATH/TO/fleet-lab",
                    "status": "active",
                }
            },
        }
        await call(
            "board_state_update",
            agent_name="fleet-admin",
            key="project_registry",
            value=json.dumps(registry),
        )
        await call(
            "board_state_update",
            agent_name="fleet-admin",
            key=central.PROJECT_COORDINATORS_STATE_KEY,
            value=json.dumps({board_id: [identities["butler"]["agent_id"]]}),
        )
        config = {
            "board_butler": {
                "schema_version": 1,
                "boards": {
                    board_id: {
                        "mode": "active",
                        "answering_mode": "autonomous",
                        "kill_switch": False,
                        "answer_scope": {"ticket_status": "auto"},
                        "required_evidence_kinds": ["ticket_status"],
                        "hold_before_post_s": 0,
                        "active_windows": [
                            {
                                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                                "start": "00:00",
                                "end": "23:59",
                                "timezone": "UTC",
                            }
                        ],
                    }
                },
            }
        }
        await call(
            "board_state_update",
            agent_name="fleet-admin",
            key=butler.CONFIG_KEY,
            value=json.dumps(config),
        )

        tickets: list[str] = []
        for row in scenario["tickets"]:
            created = await call(
                "ticket_create",
                agent_name="fleet-admin",
                title=row["title"],
                description="Synthetic autonomous fleet proof input.",
                target_url=f"{board_id}/work",
                scope="interactive-no-send",
                required_fields=["test_output"],
                priority="high" if row["tier"] == 1 else "medium",
                tier=row["tier"],
                tags=["acp-worker"] if row["tier"] == 3 else [],
                assigned_to=row["assignee"],
                model_usage={
                    "schema_version": 1,
                    "turns": 0,
                    "reported_turns": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            )
            tickets.append(created["ticket"]["ticket_id"])

        current[0] = principals["butler"]
        board_snapshot = await call("board_snapshot", limit=100, max_bytes=250_000)
        templates = {
            row["seat_id"]: _seat_template(
                tmp_path,
                row,
                principal_id=(
                    principals["acp"].principal_id
                    if row["role"] == "acp_worker"
                    else None
                ),
            )
            for row in scenario["seats"]
        }
        executor_seats = [
            {
                "seat_id": row["seat_id"],
                "board_id": board_id,
                "role": row["role"],
                "provider": row["provider"],
                "template_id": templates[row["seat_id"]].template_id,
                "template_digest_sha256": templates[row["seat_id"]].digest_sha256,
                "generation": 1,
                "lifecycle": "stopped",
                "transition_at": (now - timedelta(minutes=5)).isoformat(),
                "managed": True,
            }
            for row in scenario["seats"]
        ]
        snapshot = butler.fleet_snapshot_from_products(
            {board_id: board_snapshot},
            executor_seats,
            {
                board_id: {
                    "direct": {"status": "healthy", "latency_ms": 12}
                }
            },
            {
                "load_ratio": 0.1,
                "capacity_available": True,
                "executor_status": "healthy",
            },
            now,
        )
        demand = snapshot.demands[board_id]
        assert demand.open_by_tier == {1: 1, 2: 1, 3: 0}
        assert demand.acp_backlog == 1
        roles = {
            "worker": butler.FleetRolePolicy(0, 2, 2, 1),
            "reviewer": butler.FleetRolePolicy(0, 1, 1, 1),
            "acp_worker": butler.FleetRolePolicy(0, 1, 1, 1),
        }
        board_policy = butler.FleetBoardPolicy(
            board_id=board_id,
            roles=roles,
            board_maximum=4,
            provider_maximums={"direct": 4},
            approved_template_ids=frozenset(
                template.template_id for template in templates.values()
            ),
            idle_grace_s=60,
            scale_up_cooldown_s=0,
            scale_down_cooldown_s=0,
            failure_backoff_s=1,
            provider_latency_limit_ms=1000,
        )
        engine = butler.FleetReconciler(
            butler.FleetHostPolicy(4, 2, 6),
            {board_id: board_policy},
            config_revision=3,
            authorization_fingerprint_sha256="a" * 64,
        )
        fleet_store = butler.MemoryFleetStateStore()
        fleet_client = RecordingFleetClient()
        scaled = engine.reconcile(snapshot, fleet_store, fleet_client)
        assert scaled["desired"][board_id] == {
            "worker": 2,
            "reviewer": 0,
            "acp_worker": 1,
        }
        acp_operation = next(
            operation
            for operation in fleet_client.operations
            if operation.seat_id == "fleet-acp-a"
        )
        acp_template = templates[acp_operation.seat_id]
        assert acp_template.template_id in board_policy.approved_template_ids
        assert acp_template.principal_id == principals["acp"].principal_id
        assert acp_template.capabilities["tier_max"] == 3

        current[0] = principals["worker"]
        await call("ticket_claim", agent_name="fleet-worker", ticket_id=tickets[0])
        asked = await call(
            "ticket_question_ask",
            agent_name="fleet-worker",
            ticket_id=tickets[0],
            message=f"What is the status of {tickets[0]}?",
            kind="information",
        )
        forbidden = await call(
            "ticket_question_ask",
            agent_name="fleet-worker",
            ticket_id=tickets[0],
            message=scenario["forbidden_question"],
            kind="decision",
        )

        backend = CentralScenarioBackend(
            mcp,
            current,
            principals["butler"],
            identities["butler"],
            board_id,
            config,
        )
        info_question = {
            **asked["question"],
            "board_id": board_id,
            "ticket_id": tickets[0],
        }
        answered = await butler.process_question(
            backend, info_question, _butler_args(board_id), now
        )
        assert answered.get("answer_status") == "answered"
        stored = await backend.question(
            tickets[0], info_question["question_id"], board_id=board_id
        )
        assert stored is not None and stored["state"] == "answered"
        assert stored["answer"] == f"{tickets[0]} is claimed."

        forbidden_question = {
            **forbidden["question"],
            "board_id": board_id,
            "ticket_id": tickets[0],
        }
        escalated = await butler.process_question(
            backend, forbidden_question, _butler_args(board_id), now
        )
        assert escalated["auto_eligible"] is False
        assert escalated["verdict"] in {"ESCALATE", "UNKNOWN"}
        still_open = await backend.question(
            tickets[0], forbidden_question["question_id"], board_id=board_id
        )
        assert still_open is not None and still_open["state"] == "open"

        current[0] = principals["worker"]
        await call(
            "ticket_submit",
            agent_name="fleet-worker",
            ticket_id=tickets[0],
            summary="Synthetic work complete.",
            model_usage={"schema_version": 1, "turns": 1, "reported_turns": 1, "input_tokens": 120, "output_tokens": 30},
        )
        await call("ticket_claim", agent_name="fleet-worker", ticket_id=tickets[1])
        await call(
            "ticket_submit",
            agent_name="fleet-worker",
            ticket_id=tickets[1],
            summary="Synthetic documentation complete.",
            model_usage={"schema_version": 1, "turns": 1, "reported_turns": 1, "input_tokens": 80, "output_tokens": 20},
        )
        current[0] = principals["acp"]
        await call("ticket_claim", agent_name="fleet-acp-worker", ticket_id=tickets[2])
        await call(
            "ticket_submit",
            agent_name="fleet-acp-worker",
            ticket_id=tickets[2],
            summary="Synthetic MCP inspection complete.",
            model_usage={"schema_version": 1, "turns": 1, "reported_turns": 1, "input_tokens": 60, "output_tokens": 10},
        )

        current[0] = principals["butler"]
        submitted_snapshot = await call(
            "board_snapshot", limit=100, max_bytes=250_000
        )
        projected_submitted = butler.fleet_snapshot_from_products(
            {board_id: submitted_snapshot},
            executor_seats,
            {
                board_id: {
                    "direct": {"status": "healthy", "latency_ms": 12}
                }
            },
            {
                "load_ratio": 0.1,
                "capacity_available": True,
                "executor_status": "healthy",
            },
            now + timedelta(seconds=1),
        )
        assert projected_submitted.demands[board_id].review_backlog == 3
        reviewer_client = RecordingFleetClient()
        reviewer_scaled = engine.reconcile(
            projected_submitted,
            butler.MemoryFleetStateStore(),
            reviewer_client,
        )
        assert reviewer_scaled["desired"][board_id]["reviewer"] == 1
        assert any(
            operation.seat_id == "fleet-reviewer-a"
            for operation in reviewer_client.operations
        )

        assert sum(
            row["status"] == "submitted"
            for row in service.load(board_id)["tickets"].values()
        ) == 3
        current[0] = principals["worker"]
        with pytest.raises(ToolError, match="board:review"):
            await call(
                "ticket_review",
                agent_name="fleet-worker",
                ticket_id=tickets[0],
                verdict="approve",
                review_notes="self review must fail",
            )

        current[0] = principals["reviewer"]
        for ticket_id in tickets:
            await call(
                "ticket_review",
                agent_name="fleet-reviewer",
                ticket_id=ticket_id,
                verdict="approve",
                review_notes="Independent synthetic verification passed.",
                model_usage={"schema_version": 1, "turns": 1, "reported_turns": 1, "input_tokens": 25, "output_tokens": 5},
            )
        closed = [service.load(board_id)["tickets"][ticket_id] for ticket_id in tickets]
        assert all(row["status"] == "closed" for row in closed)
        assert all(
            row["submitted_by_principal_id"] != row["reviewed_by_principal_id"]
            for row in closed
        )
        full = [
            await call("ticket_get", ticket_id=ticket_id, view="full")
            for ticket_id in tickets
        ]
        assert sum(row["ticket"]["model_usage"]["total_tokens"] for row in full) == 410

    try:
        asyncio.run(run())
    finally:
        central.current_principal = original_current
        central.current_host_binding = original_host_binding
        task = getattr(service, "recurring_reaper_task", None)
        if task is not None:
            task.cancel()


class ScenarioServiceAdapter:
    def __init__(self) -> None:
        self.observations: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []

    def inspect(self, seat_id: str, _template: Any) -> Any:
        self.calls.append(("inspect", seat_id))
        return self.observations.get(
            seat_id, executor.ServiceObservation(False, False, False, True)
        )

    def instantiate(self, seat_id: str, _template: Any) -> None:
        self.calls.append(("instantiate", seat_id))
        self.observations[seat_id] = executor.ServiceObservation(True, False, False, True)

    def start(self, seat_id: str, _template: Any) -> None:
        self.calls.append(("start", seat_id))
        self.observations[seat_id] = executor.ServiceObservation(
            True, True, True, True, f"synthetic:{seat_id}:1"
        )

    def drain(self, seat_id: str, _template: Any) -> None:
        self.calls.append(("drain", seat_id))

    def stop(self, seat_id: str, _template: Any) -> None:
        self.calls.append(("stop", seat_id))
        self.observations[seat_id] = executor.ServiceObservation(True, False, False, True)


class ScenarioLeases:
    def __init__(self) -> None:
        self.live: set[str] = set()

    def observe(self, _board_id: str, seat_id: str) -> Any:
        return executor.LeaseObservation(True, live_work=seat_id in self.live)


class ScenarioReadiness:
    def observe(self, board_id: str, _seat_id: str, _template: Any) -> Any:
        return executor.RegistryReadinessObservation(True, True, (board_id,))


class ScenarioPublisher:
    def __init__(self) -> None:
        self.receipts: list[dict[str, Any]] = []

    def publish(self, receipt: Mapping[str, Any]) -> None:
        if not any(
            row["operation_id"] == receipt["operation_id"]
            and row["request_digest_sha256"] == receipt["request_digest_sha256"]
            for row in self.receipts
        ):
            self.receipts.append(dict(receipt))


class DirectSignedExecutorClient:
    def __init__(
        self,
        service: Any,
        private_key: Ed25519PrivateKey,
        now: datetime,
        *,
        disconnect_once: bool = False,
    ) -> None:
        self.service = service
        self.private_key = private_key
        self.now = now
        self.disconnect_once = disconnect_once
        self.disconnected_operation: str | None = None
        self.receipts: list[Mapping[str, Any]] = []

    def execute(self, operation: Any) -> Mapping[str, Any]:
        signed_at = self.now.isoformat()
        request: dict[str, Any] = {
            "schema": executor.SCHEMA,
            "schema_version": 1,
            "message_type": "request",
            "operation_id": operation.operation_id,
            "board_id": operation.board_id,
            "action": operation.action,
            "seat_id": operation.seat_id,
            "template_id": operation.template_id,
            "template_digest_sha256": operation.template_digest_sha256,
            "expected_seat_generation": operation.expected_seat_generation,
            "authorization_fingerprint_sha256": operation.authorization_fingerprint_sha256,
            "deadline": (
                self.now.replace(second=0, microsecond=0) + timedelta(minutes=1)
            ).isoformat(),
            "caller_auth": {},
        }
        digest = executor.request_digest(request)
        nonce = "nonce:" + hashlib.sha256(operation.operation_id.encode()).hexdigest()[:32]
        message = b"\0".join(
            (
                executor.SIGNING_CONTEXT,
                digest.encode("ascii"),
                b"butler-local",
                nonce.encode(),
                signed_at.encode(),
            )
        )
        request["caller_auth"] = {
            "scheme": "local_ed25519_v1",
            "key_id": "butler-local",
            "nonce": nonce,
            "signed_at": signed_at,
            "request_digest_sha256": digest,
            "signature_base64": base64.b64encode(self.private_key.sign(message)).decode(),
        }
        receipt = self.service.handle(request)
        self.receipts.append(receipt)
        if self.disconnect_once and self.disconnected_operation is None:
            self.disconnected_operation = operation.operation_id
            raise OSError("synthetic disconnect after commit")
        return receipt


def _seat_template(
    tmp_path: Path,
    row: Mapping[str, Any],
    *,
    principal_id: str | None = None,
) -> Any:
    repository = tmp_path / "repositories" / "fleet-lab"
    seat_root = tmp_path / "seats" / row["seat_id"]
    repository.mkdir(parents=True, exist_ok=True)
    seat_root.mkdir(parents=True, exist_ok=True)
    role = row["role"]
    return executor.SeatTemplate.from_record(
        f"template:{row['role']}:{row['provider']}:{row['seat_id']}",
        {
            "role": role,
            "principal_id": principal_id or f"PR-{row['seat_id']}",
            "credential_ref": f"credential.{row['seat_id']}",
            "repository_root": str(repository),
            "seat_root": str(seat_root),
            "command": [sys.executable, "-c", "raise SystemExit(0)"],
            "boards": "registry",
            "capabilities": {
                "can_work": role in {"worker", "acp_worker"},
                "can_review": role == "reviewer",
                "tier_max": 3 if role == "acp_worker" else 2,
                "max_parallel": 1,
            },
        },
    )


def _fleet_seat(row: Mapping[str, Any], template: Any, now: datetime, lifecycle: str) -> Any:
    return butler.FleetSeat(
        seat_id=row["seat_id"],
        board_id="fleet-lab",
        role=row["role"],
        provider=row["provider"],
        template_id=template.template_id,
        template_digest_sha256=template.digest_sha256,
        generation=1,
        lifecycle=lifecycle,
        ready=lifecycle in {"ready", "busy"},
        busy=lifecycle == "busy",
        live_lease=lifecycle == "busy",
        transition_at=now - timedelta(minutes=5),
    )


def test_real_executor_restart_caps_live_lease_and_dashboard(tmp_path: Path) -> None:
    scenario = fixture()
    now = datetime.fromisoformat(scenario["observed_at"])
    templates = {
        row["seat_id"]: _seat_template(tmp_path, row) for row in scenario["seats"]
    }
    credentials: dict[str, Path] = {}
    for row in scenario["seats"]:
        path = tmp_path / f"{row['seat_id']}.env"
        path.write_text("", encoding="utf-8")
        credentials[f"credential.{row['seat_id']}"] = path
    private = Ed25519PrivateKey.generate()
    fingerprint = "a" * 64
    policy = executor.ExecutorPolicy(
        authorization_fingerprint_sha256=fingerprint,
        templates={template.template_id: template for template in templates.values()},
        caller_keys={"butler-local": private.public_key()},
        credential_paths=credentials,
        repository_roots=((tmp_path / "repositories").resolve(),),
        seat_roots=((tmp_path / "seats").resolve(),),
        board_caps={scenario["board_id"]: scenario["host"]["agent_process_ceiling"]},
        host_cap=scenario["host"]["agent_process_ceiling"],
        signature_skew_s=90,
        mutation_cooldown_s=0,
        failure_backoff_s=0,
    )
    adapter = ScenarioServiceAdapter()
    leases = ScenarioLeases()
    publisher = ScenarioPublisher()
    store_path = tmp_path / "executor" / "executor.sqlite3"
    service = executor.FleetExecutor(
        policy,
        executor.ExecutorStore(store_path),
        adapter,
        leases,
        ScenarioReadiness(),
        publisher,
        clock=lambda: now.timestamp(),
    )
    roles = {
        "worker": butler.FleetRolePolicy(0, 2, 2, 1),
        "reviewer": butler.FleetRolePolicy(0, 1, 1, 1),
        "acp_worker": butler.FleetRolePolicy(0, 1, 1, 1),
    }
    board_policy = butler.FleetBoardPolicy(
        board_id=scenario["board_id"],
        roles=roles,
        board_maximum=4,
        provider_maximums={"direct": 4, "failed-provider": 2},
        approved_template_ids=frozenset(template.template_id for template in templates.values()),
        idle_grace_s=60,
        scale_up_cooldown_s=0,
        scale_down_cooldown_s=0,
        failure_backoff_s=1,
        provider_latency_limit_ms=1000,
    )
    engine = butler.FleetReconciler(
        butler.FleetHostPolicy(4, 2, 6),
        {scenario["board_id"]: board_policy},
        config_revision=3,
        authorization_fingerprint_sha256=fingerprint,
    )
    demand = butler.FleetDemand(
        board_id=scenario["board_id"],
        open_by_tier={1: 1, 2: 1, 3: 1},
        review_backlog=2,
        acp_backlog=1,
        oldest_ticket_age_s=600,
        expiring_offers=1,
        provider_health={"direct": "healthy", "failed-provider": "unavailable"},
        provider_latency_ms={"direct": 12, "failed-provider": 1},
    )
    stopped = [
        _fleet_seat(row, templates[row["seat_id"]], now, "stopped")
        for row in scenario["seats"]
    ]
    snapshot = butler.FleetSnapshot(
        observed_at=now,
        demands={scenario["board_id"]: demand},
        seats=tuple(stopped),
        host_load_ratio=0.1,
        host_capacity_available=True,
        executor_healthy=True,
    )
    fleet_store = butler.FileFleetStateStore(tmp_path / "butler" / "fleet.json")
    initial_plan = engine.plan(snapshot, {})
    first_client = DirectSignedExecutorClient(service, private, now, disconnect_once=True)
    first = engine.reconcile(snapshot, fleet_store, first_client)
    assert sum(first["desired"][scenario["board_id"]].values()) == 4
    assert len(first["operations"]) == 4
    assert [row["outcome"] for row in first["receipts"]].count("unknown") == 1
    assert len(publisher.receipts) == 4

    disconnected = first_client.disconnected_operation
    assert disconnected is not None
    disconnected_seat = next(
        operation.seat_id
        for operation in initial_plan.operations
        if operation.operation_id == disconnected
    )
    restarted_service = executor.FleetExecutor(
        policy,
        executor.ExecutorStore(store_path),
        adapter,
        leases,
        ScenarioReadiness(),
        publisher,
        clock=lambda: (now + timedelta(seconds=6)).timestamp(),
    )
    retry_seats = []
    for row in scenario["seats"]:
        template = templates[row["seat_id"]]
        lifecycle = "stopped" if row["seat_id"] == disconnected_seat else "ready"
        retry_seats.append(_fleet_seat(row, template, now, lifecycle))
    retry_snapshot = butler.FleetSnapshot(
        observed_at=now + timedelta(seconds=6),
        demands={scenario["board_id"]: demand},
        seats=tuple(retry_seats),
        host_load_ratio=0.1,
        host_capacity_available=True,
        executor_healthy=True,
    )
    second_client = DirectSignedExecutorClient(
        restarted_service, private, now + timedelta(seconds=6)
    )
    engine.reconcile(retry_snapshot, fleet_store, second_client)
    replay = next(row for row in second_client.receipts if row["operation_id"] == disconnected)
    assert replay["replayed"] is True
    assert len(publisher.receipts) == 4
    assert adapter.calls.count(("start", disconnected_seat)) == 1

    live_seat = scenario["seats"][0]
    leases.live.add(live_seat["seat_id"])
    malicious_stop = butler.FleetOperation(
        operation_id="operation:forbidden-live-stop",
        board_id=scenario["board_id"],
        action="stop",
        seat_id=live_seat["seat_id"],
        template_id=templates[live_seat["seat_id"]].template_id,
        template_digest_sha256=templates[live_seat["seat_id"]].digest_sha256,
        expected_seat_generation=1,
        authorization_fingerprint_sha256=fingerprint,
    )
    live_receipt = DirectSignedExecutorClient(
        restarted_service, private, now + timedelta(seconds=7)
    ).execute(malicious_stop)
    assert live_receipt["outcome"] == "rejected"
    assert live_receipt["reason_code"] == "live_lease"
    assert ("stop", live_seat["seat_id"]) not in adapter.calls

    ready_snapshot = butler.FleetSnapshot(
        observed_at=now + timedelta(seconds=8),
        demands={scenario["board_id"]: demand},
        seats=tuple(
            _fleet_seat(row, templates[row["seat_id"]], now, "ready")
            for row in scenario["seats"]
        ),
        host_load_ratio=0.1,
        host_capacity_available=True,
        executor_healthy=True,
    )
    _revision, durable = fleet_store.load()
    plan = engine.plan(ready_snapshot, durable)
    state = engine.desired_state_document(scenario["board_id"], ready_snapshot, plan)
    view = dashboard.autonomous_butler_view(
        {
            "board_id": scenario["board_id"],
            "revision": 3,
            "config_digest_sha256": fingerprint,
            "effective_mode": "autonomous",
            "config": None,
        },
        {"commands": []},
        state,
        now=ready_snapshot.observed_at,
    )
    assert view["actual_state_available"] is True
    assert view["effective_state"] == "autonomous"
    assert view["actual_state"]["host_processes"] == {
        "role_agents": 4,
        "control_plane": 2,
        "agent_process_ceiling": 4,
        "total_process_ceiling": 6,
        "observed_at": ready_snapshot.observed_at.isoformat(),
    }

    idle_demand = butler.FleetDemand(
        board_id=scenario["board_id"],
        open_by_tier={1: 0, 2: 0, 3: 0},
        review_backlog=0,
        acp_backlog=0,
        oldest_ticket_age_s=0,
        expiring_offers=0,
        provider_health=demand.provider_health,
        provider_latency_ms=demand.provider_latency_ms,
    )
    grace_snapshot = butler.FleetSnapshot(
        observed_at=now + timedelta(seconds=10),
        demands={scenario["board_id"]: idle_demand},
        seats=ready_snapshot.seats,
        host_load_ratio=0.1,
        host_capacity_available=True,
        executor_healthy=True,
    )
    grace_client = DirectSignedExecutorClient(
        restarted_service, private, grace_snapshot.observed_at
    )
    grace = engine.reconcile(grace_snapshot, fleet_store, grace_client)
    assert grace["operations"] == []

    idle_seats = tuple(
        _fleet_seat(
            row,
            templates[row["seat_id"]],
            now,
            "busy" if row["seat_id"] == live_seat["seat_id"] else "ready",
        )
        for row in scenario["seats"]
    )
    idle_snapshot = butler.FleetSnapshot(
        observed_at=now + timedelta(seconds=80),
        demands={scenario["board_id"]: idle_demand},
        seats=idle_seats,
        host_load_ratio=0.1,
        host_capacity_available=True,
        executor_healthy=True,
    )
    _revision, durable = fleet_store.load()
    idle_plan = engine.plan(idle_snapshot, durable)
    assert sum(idle_plan.desired[scenario["board_id"]].values()) == 1
    assert {operation.action for operation in idle_plan.operations} == {"drain"}
    assert live_seat["seat_id"] not in {
        operation.seat_id for operation in idle_plan.operations
    }
    idle_service = executor.FleetExecutor(
        policy,
        executor.ExecutorStore(store_path),
        adapter,
        leases,
        ScenarioReadiness(),
        publisher,
        clock=lambda: idle_snapshot.observed_at.timestamp(),
    )
    idle_client = DirectSignedExecutorClient(
        idle_service, private, idle_snapshot.observed_at
    )
    idle = engine.reconcile(idle_snapshot, fleet_store, idle_client)
    assert all(row["outcome"] == "succeeded" for row in idle["receipts"])
    assert ("stop", live_seat["seat_id"]) not in adapter.calls


class ScenarioModelBackend:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def run(
        self, _request: Mapping[str, Any], *, timeout_s: float
    ) -> Any:
        assert timeout_s > 0
        if self.fail:
            raise RuntimeError("synthetic-provider-private-detail")
        return butler.ModelBackendResponse(
            proposal_json='{"answer":"bounded"}',
            citations=("evidence:fleet",),
            usage={
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "cost_microunits": 37,
                "measured": True,
            },
            provider_request_ref="provider:fleet",
        )


def test_model_usage_cost_and_provider_failure_are_isolated() -> None:
    scenario = fixture()
    now = datetime.fromisoformat(scenario["observed_at"])
    schemas = butler.TaskSchemaRegistry()
    schema_digest = schemas.register(
        "fleet-answer:v1",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string", "minLength": 1}},
        },
    )

    def request(request_id: str) -> dict[str, Any]:
        task_input = {
            "instruction": "Return a bounded synthetic answer.",
            "observation_refs": ["observation:fleet"],
            "constraints": ["Use only cited evidence."],
        }
        return {
            "schema": "autonomous_butler_model_v1",
            "schema_version": 1,
            "message_type": "request",
            "request_id": request_id,
            "board_id": scenario["board_id"],
            "subject_id": "ticket:synthetic",
            "task_kind": "answer_question",
            "policy_digest_sha256": "b" * 64,
            "task_input": task_input,
            "task_input_digest_sha256": butler._sha256_json(task_input),
            "task_schema": {
                "schema_id": "fleet-answer:v1",
                "schema_sha256": schema_digest,
            },
            "evidence_refs": ["evidence:fleet"],
            "max_output_bytes": 4096,
            "usage_limit": {
                "max_input_tokens": 1500,
                "max_output_tokens": 500,
                "max_cost_microunits": scenario["budget"]["max_cost_microunits"],
            },
            "deadline": (now + timedelta(minutes=1)).isoformat(),
            "cancellation_token": f"cancel:{request_id}",
        }

    def runner(backend: Any) -> Any:
        return butler.AutonomousModelRunner(
            backend,
            task_schemas=schemas,
            policy_digest=lambda _board: "b" * 64,
            now=lambda: now,
        )

    failed = asyncio.run(
        runner(ScenarioModelBackend(fail=True)).run(request("request:failed"))
    )
    succeeded = asyncio.run(
        runner(ScenarioModelBackend()).run(request("request:succeeded"))
    )
    assert failed["reason_code"] == "provider_crash"
    assert "synthetic-provider-private-detail" not in json.dumps(failed)
    assert succeeded["outcome"] == "succeeded"
    assert succeeded["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cost_microunits": 37,
        "measured": True,
    }


def test_real_mcp_v2_reconnect_replays_once_and_redacts(tmp_path: Path) -> None:
    scenario = fixture()
    attempts = tmp_path / "attempts.jsonl"
    marker = tmp_path / "first-attempt"
    server = tmp_path / "connector.py"
    server.write_text(
        textwrap.dedent(
            f"""
            import json
            import os
            from pathlib import Path
            from mcp.server import MCPServer

            attempts = Path({str(attempts)!r})
            marker = Path({str(marker)!r})
            mcp = MCPServer("fleet-case-connector")

            @mcp.tool()
            def lookup(query: str, call_id: str):
                with attempts.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({{"query": query, "call_id": call_id}}) + "\\n")
                if not marker.exists():
                    marker.write_text("committed", encoding="utf-8")
                    os._exit(17)
                return {{"query": query, "call_id": call_id, "token": "synthetic-secret"}}

            if __name__ == "__main__":
                mcp.run()
            """
        ),
        encoding="utf-8",
    )
    declaration = butler.ConnectorDeclaration.from_mapping(
        {
            "connector_id": scenario["mcp"]["connector_id"],
            "enabled": True,
            "transport": "stdio",
            "protocol_revision": "2026-07-28",
            "endpoint_ref": "endpoint:inventory",
            "secret_ref": "secret:inventory",
            "tools": [
                {
                    "name": scenario["mcp"]["tool"],
                    "effect": "read_only",
                    "replay": "safe_with_stable_call_id",
                    "stable_call_id_field": "call_id",
                }
            ],
            "resources": [],
            "risky_tools": [],
            "limits": {
                "timeout_ms": 30000,
                "max_input_bytes": 4096,
                "max_output_bytes": 32000,
                "max_concurrency": 1,
                "calls_per_minute": 4,
            },
        }
    )
    persistence = butler.InMemoryConnectorPersistence()
    connector = butler.ConnectorRuntime(
        board_id=scenario["board_id"],
        project_id=scenario["project_id"],
        actor_id="fleet-butler",
        policy_digest_sha256="c" * 64,
        declaration=declaration,
        approved_connector_ids=[scenario["mcp"]["connector_id"]],
        endpoint_resolver=lambda _ref: butler.StdioConnectorEndpoint(
            sys.executable, (str(server),)
        ),
        secret_resolver=lambda _ref: "synthetic-secret",
        persistence=persistence,
    )

    async def run() -> Any:
        await connector.discover("operation:discover")
        return await connector.call_tool(
            "operation:mcp-task",
            scenario["mcp"]["tool"],
            {"query": scenario["mcp"]["query"]},
        )

    result = asyncio.run(run())
    rows = [json.loads(line) for line in attempts.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["call_id"] == rows[1]["call_id"] == result.call_id
    assert "synthetic-secret" not in json.dumps(result.payload)
    assert len(persistence.reservations) == 1
    assert persistence.audit_records[-1]["outcome"] == "succeeded"
