"""Product-level tests for durable Butler commands and config control."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError
import jsonschema

import pursers_central.central as central


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]


def validate_schema(name: str, value: dict) -> None:
    schema = json.loads(
        (REPO_ROOT / "docs" / "design" / "schemas" / name).read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(value)


def set_envelope_fingerprint(config: dict) -> None:
    fingerprint_input = dict(config["envelope"])
    fingerprint_input.pop("fingerprint_sha256")
    encoded = json.dumps(
        fingerprint_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    config["envelope"]["fingerprint_sha256"] = fingerprint
    config["authorization"]["envelope_fingerprint_sha256"] = fingerprint


def config_v1(board_id: str = "pursers") -> dict:
    now = datetime.now(timezone.utc)
    future = (now + timedelta(days=1)).isoformat()
    config = {
        "schema": "autonomous_butler_config_v1",
        "schema_version": 1,
        "board_id": board_id,
        "revision": 1,
        "enabled": True,
        "host_runtime": {
            "host_ref": "host-a",
            "revision": 1,
            "agent_process_ceiling": 12,
            "control_plane_processes": 2,
            "total_process_ceiling": 14,
            "configured_by": "human-admin",
            "configured_at": now.isoformat(),
        },
        "desired": {
            "mode": "autonomous",
            "runner": "direct_api",
            "capacity": {
                role: {"min": 0, "target": 1, "max": 2}
                for role in ("worker", "reviewer", "acp_worker")
            },
            "host_concurrency": 4,
            "board_concurrency": 3,
            "cooldowns": {
                "scale_up_s": 30,
                "scale_down_s": 60,
                "failure_backoff_s": 10,
            },
            "budget": {
                "period": "day",
                "max_tokens": 10_000,
                "max_cost_microunits": 1_000_000,
                "max_external_calls": 100,
            },
            "connectors": [],
        },
        "envelope": {
            "fingerprint_sha256": "0" * 64,
            "approved_template_ids": ["worker-template"],
            "approved_connector_ids": [],
            "max_capacity": {"worker": 4, "reviewer": 4, "acp_worker": 4},
            "max_host_concurrency": 8,
            "max_board_concurrency": 8,
            "max_budget": {
                "period": "day",
                "max_tokens": 100_000,
                "max_cost_microunits": 10_000_000,
                "max_external_calls": 1_000,
            },
            "created_by": "human-admin",
            "created_at": now.isoformat(),
        },
        "authorization": {
            "authorization_id": "auth-1",
            "config_revision": 1,
            "envelope_fingerprint_sha256": "0" * 64,
            "expires_at": future,
        },
    }
    set_envelope_fingerprint(config)
    return config


class ButlerCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=PACKAGE_ROOT)
        self.root = Path(self.temp_dir.name)
        jwks_path = self.root / "jwks.json"
        jwks_path.write_text('{"keys": []}', encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": "https://issuer.example",
                "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
                "CENTRAL_JWKS_PATH": str(jwks_path),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
            },
        )
        self.environment.start()
        self.mcp, self.service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.admin = central.Principal(
            "PR-admin", "admin", frozenset({"board:read", "board:write"})
        )
        self.butler = central.Principal(
            "PR-butler",
            "butler",
            frozenset({"board:read", "board:write", "board:coordinate"}),
        )
        self.worker = central.Principal(
            "PR-worker", "worker", frozenset({"board:read", "board:write"})
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call("board_join", agent_name="human-admin", role="worker")
        await self.call(
            "board_member_add",
            agent_name="human-admin",
            principal_id=self.butler.principal_id,
            role="admin",
        )
        await self.call(
            "board_member_add",
            agent_name="human-admin",
            principal_id=self.worker.principal_id,
            role="member",
        )
        self.principal = self.butler
        await self.call(
            "board_join",
            agent_name="board-butler-1",
            role="coordinator",
            capabilities={"can_work": False, "can_review": False},
        )
        self.principal = self.worker
        await self.call("board_join", agent_name="worker-1", role="worker")
        self.principal = self.admin
        created = await self.call(
            "butler_config_set",
            agent_name="human-admin",
            mutation_id="config-init",
            sender_channel="human",
            config=config_v1(),
            expected_revision=0,
        )
        self.assertFalse(created.is_error)

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments):
        return await self.mcp.call_tool(
            name, {"board_id": "pursers", **arguments}
        )

    def expiry(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()

    async def test_config_cas_idempotency_and_a2a_field_authority(self) -> None:
        current = await self.call("butler_config_get")
        self.assertEqual(current.structured_content["effective_mode"], "autonomous")

        self.principal = self.butler
        next_config = copy.deepcopy(current.structured_content["config"])
        next_config["revision"] = 2
        next_config["desired"]["capacity"]["worker"]["target"] = 2
        changed = await self.call(
            "butler_config_set",
            agent_name="board-butler-1",
            mutation_id="scale-worker",
            sender_channel="a2a",
            config=next_config,
            expected_revision=1,
        )
        self.assertFalse(changed.is_error)
        self.assertEqual(
            changed.structured_content["rollback_evidence"]["changed_paths"],
            ["desired.capacity.worker.target"],
        )
        self.assertEqual(
            changed.structured_content["rollback_evidence"]["prior_revision"], 1
        )
        replay = await self.call(
            "butler_config_set",
            agent_name="board-butler-1",
            mutation_id="scale-worker",
            sender_channel="a2a",
            config=next_config,
            expected_revision=1,
        )
        self.assertTrue(replay.structured_content["idempotent_replay"])
        self.assertFalse(replay.structured_content["event_created"])
        read = await self.call("butler_config_get")
        self.assertEqual(read.structured_content["effective_mode"], "shadow")

        spoofed = copy.deepcopy(next_config)
        spoofed["revision"] = 3
        spoofed["desired"]["capacity"]["worker"]["target"] = 1
        with self.assertRaisesRegex(ToolError, "cannot assert human authority"):
            await self.call(
                "butler_config_set",
                agent_name="board-butler-1",
                mutation_id="spoof-human",
                sender_channel="human",
                config=spoofed,
                expected_revision=2,
            )
        with self.assertRaisesRegex(ToolError, "cannot assert human authority"):
            await self.call(
                "butler_command_submit",
                agent_name="board-butler-1",
                request_id="spoof-human-command",
                project_id="pursers",
                sender_channel="human",
                intent="kill",
                parameters={"reason_code": "operator_stop"},
                expected_config_revision=2,
                expires_at=self.expiry(),
                priority="emergency",
            )

        forbidden = copy.deepcopy(next_config)
        forbidden["revision"] = 3
        forbidden["envelope"]["max_capacity"]["worker"] = 5
        set_envelope_fingerprint(forbidden)
        with self.assertRaisesRegex(ToolError, "A2A authority cannot mutate"):
            await self.call(
                "butler_config_set",
                agent_name="board-butler-1",
                mutation_id="raise-envelope",
                sender_channel="a2a",
                config=forbidden,
                expected_revision=2,
            )

    async def test_command_replay_priority_lifecycle_and_product_result(self) -> None:
        self.principal = self.worker
        worker_command = await self.call(
            "butler_command_submit",
            agent_name="worker-1",
            request_id="cmd-worker",
            project_id="pursers",
            sender_channel="a2a",
            intent="reconcile_now",
            parameters={},
            expected_config_revision=1,
            expires_at=self.expiry(),
            priority="high",
        )
        self.assertFalse(worker_command.is_error)
        replay = await self.call(
            "butler_command_submit",
            agent_name="worker-1",
            request_id="cmd-worker",
            project_id="pursers",
            sender_channel="a2a",
            intent="reconcile_now",
            parameters={},
            expected_config_revision=1,
            expires_at=worker_command.structured_content["command"]["expires_at"],
            priority="high",
        )
        self.assertTrue(replay.structured_content["idempotent_replay"])
        with self.assertRaisesRegex(ToolError, "different canonical bytes"):
            await self.call(
                "butler_command_submit",
                agent_name="worker-1",
                request_id="cmd-worker",
                project_id="pursers",
                sender_channel="a2a",
                intent="kill",
                parameters={"reason_code": "policy_breach"},
                expected_config_revision=1,
                expires_at=worker_command.structured_content["command"]["expires_at"],
                priority="high",
            )

        self.principal = self.admin
        human = await self.call(
            "butler_command_submit",
            agent_name="human-admin",
            request_id="cmd-human",
            project_id="pursers",
            sender_channel="human",
            intent="kill",
            parameters={"reason_code": "operator_stop"},
            expected_config_revision=1,
            expires_at=self.expiry(),
            priority="emergency",
        )
        queue = await self.call(
            "butler_command_inspect", agent_name="human-admin", status="accepted"
        )
        self.assertEqual(
            [item["command_id"] for item in queue.structured_content["commands"]],
            ["cmd-human", "cmd-worker"],
        )

        self.principal = self.worker
        with self.assertRaisesRegex(ToolError, "cannot cancel a human command"):
            await self.call(
                "butler_command_cancel",
                agent_name="worker-1",
                command_id="cmd-human",
                expected_revision=1,
                sender_channel="a2a",
                reason_code="not_authorized",
            )

        self.principal = self.butler
        revision = 1
        for state in ("validating", "pending", "applying"):
            acknowledged = await self.call(
                "butler_command_acknowledge",
                agent_name="board-butler-1",
                command_id="cmd-worker",
                expected_revision=revision,
                target_status=state,
                reason_code=f"entered_{state}",
            )
            revision = acknowledged.structured_content["command"]["revision"]
        completed = await self.call(
            "butler_command_result",
            agent_name="board-butler-1",
            command_id="cmd-worker",
            expected_revision=revision,
            result={
                "outcome": "succeeded",
                "reason_code": "observed_effect",
                "commit_state": "reached",
                "effect_observation_ref": "state-observation-1",
                "config_revision": 1,
            },
        )
        command = completed.structured_content["command"]
        self.assertEqual(command["status"], "succeeded")
        self.assertEqual(command["revision"], 5)
        self.assertEqual(len(command["transition_history"]), 5)
        validate_schema("autonomous-butler-command-v2.schema.json", command)
        document = self.service.load("pursers")
        for audit in document["butler_audit"]:
            validate_schema("autonomous-butler-control-audit-v2.schema.json", audit)

    async def test_wait_wakes_on_revision_and_config_rejects_unknown_fields(self) -> None:
        self.principal = self.worker
        created = await self.call(
            "butler_command_submit",
            agent_name="worker-1",
            request_id="cmd-wait",
            project_id="pursers",
            sender_channel="a2a",
            intent="reconcile_now",
            parameters={},
            expected_config_revision=1,
            expires_at=self.expiry(),
            priority="normal",
        )
        self.assertFalse(created.is_error)
        self.principal = self.butler
        waiter = asyncio.create_task(
            self.call(
                "butler_command_wait",
                agent_name="board-butler-1",
                command_id="cmd-wait",
                after_revision=1,
                timeout_s=2,
            )
        )
        await asyncio.sleep(0.05)
        await self.call(
            "butler_command_acknowledge",
            agent_name="board-butler-1",
            command_id="cmd-wait",
            expected_revision=1,
            target_status="validating",
            reason_code="validation_started",
        )
        waited = await waiter
        self.assertFalse(waited.structured_content["timed_out"])
        self.assertEqual(waited.structured_content["command"]["revision"], 2)

        self.principal = self.admin
        invalid = config_v1()
        invalid["revision"] = 2
        invalid["credential"] = "forbidden"
        with self.assertRaisesRegex(ToolError, "unsupported field credential"):
            await self.call(
                "butler_config_set",
                agent_name="human-admin",
                mutation_id="unknown-config-field",
                sender_channel="human",
                config=invalid,
                expected_revision=1,
            )

        tampered = config_v1()
        tampered["revision"] = 2
        tampered["envelope"]["max_capacity"]["worker"] = 5
        with self.assertRaisesRegex(ToolError, "does not match the envelope"):
            await self.call(
                "butler_config_set",
                agent_name="human-admin",
                mutation_id="bad-envelope-fingerprint",
                sender_channel="human",
                config=tampered,
                expected_revision=1,
            )


if __name__ == "__main__":
    unittest.main()
