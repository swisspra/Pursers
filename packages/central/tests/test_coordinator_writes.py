from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


class CoordinatorWriteTests(unittest.IsolatedAsyncioTestCase):
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
            "PR-admin",
            "admin-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.worker = central.Principal(
            "PR-worker",
            "worker-canonical",
            frozenset({"board:read", "board:write"}),
        )
        self.other_worker = central.Principal(
            "PR-other-worker",
            "other-worker-canonical",
            frozenset({"board:read", "board:write"}),
        )
        self.coordinator = central.Principal(
            "PR-coordinator",
            "coordinator-canonical",
            frozenset({"board:read", "board:coordinate"}),
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call("board_join", agent_name="admin-agent")
        for principal in (self.worker, self.other_worker, self.coordinator):
            await self.call(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principal.principal_id,
                role="member",
            )
        self.principal = self.worker
        joined = await self.call("board_join", agent_name="worker-agent")
        self.worker_id = joined.structured_content["agent_id"]
        self.principal = self.coordinator
        joined = await self.call("board_join", agent_name="coordinator-1")
        self.coordinator_id = joined.structured_content["agent_id"]

    async def join_other_worker(self) -> str:
        self.principal = self.other_worker
        joined = await self.call("board_join", agent_name="other-worker-agent")
        self.assertFalse(joined.is_error)
        return joined.structured_content["agent_id"]

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name, {"board_id": "pursers", **arguments}
        )

    async def create_ticket(self, title: str = "coordinator target") -> str:
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title=title,
            description="exercise phase two coordination writes",
            target_url="pursers/tools/coordinator",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        self.assertFalse(created.is_error)
        return created.structured_content["ticket"]["ticket_id"]

    async def test_assignment_is_atomic_targeted_and_idempotent(self) -> None:
        ticket_id = await self.create_ticket()
        await self.join_other_worker()
        before_seq = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]
        self.principal = self.coordinator
        payload = {
            "agent_name": "coordinator-1",
            "ticket_id": ticket_id,
            "assigned_to_agent_id": self.worker_id,
            "expected_status": "open",
            "expected_assigned_to_agent_id": None,
            "coordinator_op_key": "coord-op-assignment-1",
            "reason": "oldest starved ticket reached twice its threshold",
        }
        first = await self.call("ticket_assign", **payload)
        second = await self.call("ticket_assign", **payload)

        self.assertFalse(first.is_error)
        self.assertFalse(second.is_error)
        self.assertTrue(first.structured_content["event_created"])
        self.assertFalse(second.structured_content["event_created"])
        self.assertTrue(second.structured_content["idempotent_replay"])
        ticket = second.structured_content["ticket"]
        self.assertEqual(ticket["status"], "open")
        self.assertEqual(ticket["assigned_to_agent_id"], self.worker_id)
        self.assertNotIn("claimed_by_agent_id", ticket)
        event = first.structured_content["event"]
        self.assertEqual(event["kind"], "coordinator_assignment")
        self.assertEqual(event["recipient_identities"], [self.worker_id])
        self.assertEqual(event["status_from"], "open")
        self.assertEqual(event["status_to"], "open")

        self.principal = self.worker
        target_catchup = await self.call(
            "board_catchup",
            agent_name="worker-agent",
            cursor=before_seq,
            ack=False,
        )
        self.assertEqual(
            [item["kind"] for item in target_catchup.structured_content["events"]],
            ["coordinator_assignment"],
        )
        self.principal = self.other_worker
        non_target_catchup = await self.call(
            "board_catchup",
            agent_name="other-worker-agent",
            cursor=before_seq,
            ack=False,
        )
        self.assertEqual(non_target_catchup.structured_content["events"], [])

    async def test_assignment_loses_claim_race_without_overwriting(self) -> None:
        ticket_id = await self.create_ticket("race target")
        self.principal = self.worker
        claimed = await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        self.assertFalse(claimed.is_error)

        self.principal = self.coordinator
        with self.assertRaisesRegex(ToolError, "state precondition failed"):
            await self.call(
                "ticket_assign",
                agent_name="coordinator-1",
                ticket_id=ticket_id,
                assigned_to_agent_id=self.worker_id,
                expected_status="open",
                coordinator_op_key="coord-op-race",
                reason="simulated stale decision",
            )
        ticket = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(ticket["status"], "claimed")
        self.assertEqual(ticket["claimed_by_agent_id"], self.worker_id)

    async def test_nudge_is_targeted_and_deduplicated(self) -> None:
        ticket_id = await self.create_ticket("nudge target")
        await self.join_other_worker()
        before_seq = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]
        self.principal = self.coordinator
        payload = {
            "agent_name": "coordinator-1",
            "ticket_id": ticket_id,
            "target_agent_id": self.worker_id,
            "coordinator_op_key": "coord-op-nudge-1",
            "reason": "starvation threshold reached",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
        }
        first = await self.call("agent_nudge", **payload)
        second = await self.call("agent_nudge", **payload)

        self.assertTrue(first.structured_content["event_created"])
        self.assertFalse(second.structured_content["event_created"])
        self.assertEqual(
            first.structured_content["event"]["recipient_identities"],
            [self.worker_id],
        )
        self.assertEqual(
            first.structured_content["event"]["kind"], "coordinator_nudge"
        )
        self.principal = self.worker
        catchup = await self.call(
            "board_catchup",
            agent_name="worker-agent",
            cursor=before_seq,
            ack=False,
        )
        events = catchup.structured_content["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["coordinator_op_key"], "coord-op-nudge-1")
        self.principal = self.other_worker
        non_target = await self.call(
            "board_catchup",
            agent_name="other-worker-agent",
            cursor=before_seq,
            ack=False,
        )
        self.assertEqual(non_target.structured_content["events"], [])

    async def test_open_ticket_backlog_remains_broadcast_to_late_worker(self) -> None:
        ticket_id = await self.create_ticket("ordinary open backlog")
        await self.join_other_worker()

        self.principal = self.other_worker
        catchup = await self.call(
            "board_catchup",
            agent_name="other-worker-agent",
            cursor=0,
            ack=False,
        )

        matching = [
            event
            for event in catchup.structured_content["events"]
            if event.get("ticket_id") == ticket_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["kind"], "ticket_created")
        self.assertEqual(matching[0]["status_to"], "open")

    async def test_coordinate_scope_is_narrow(self) -> None:
        ticket_id = await self.create_ticket("authority target")
        self.principal = self.coordinator
        forbidden_calls = (
            (
                "ticket_create",
                {
                    "agent_name": "coordinator-1",
                    "title": "forbidden",
                    "description": "must not create",
                    "target_url": "pursers",
                    "scope": "interactive-no-send",
                    "required_fields": ["test_output"],
                },
            ),
            (
                "ticket_review",
                {
                    "agent_name": "coordinator-1",
                    "ticket_id": ticket_id,
                    "verdict": "approve",
                },
            ),
            (
                "ticket_claim",
                {
                    "agent_name": "coordinator-1",
                    "ticket_id": ticket_id,
                },
            ),
            (
                "ticket_cancel",
                {
                    "agent_name": "coordinator-1",
                    "ticket_id": ticket_id,
                },
            ),
            (
                "board_member_add",
                {
                    "agent_name": "coordinator-1",
                    "principal_id": "PR-forbidden",
                    "role": "member",
                },
            ),
        )
        for name, arguments in forbidden_calls:
            with self.subTest(name=name), self.assertRaises(ToolError):
                await self.call(name, **arguments)

        with self.assertRaisesRegex(ToolError, "only coordinator_findings"):
            await self.call(
                "board_state_update",
                agent_name="coordinator-1",
                key="project_registry",
                value="{}",
            )
        with self.assertRaisesRegex(ToolError, "only coordinator digest"):
            await self.call(
                "memory_write",
                agent_name="coordinator-1",
                title="arbitrary memory",
                content="forbidden",
                scope="project",
            )

        report = await self.call(
            "board_state_update",
            agent_name="coordinator-1",
            key="coordinator_findings",
            value='{"schema_version":2}',
        )
        digest = await self.call(
            "memory_write",
            agent_name="coordinator-1",
            title="Coordinator daily digest 2030-01-08",
            content="bounded coordinator digest",
            scope="project",
            memory_type="checkpoint",
            tags=["coordinator", "digest", "daily"],
        )
        self.assertFalse(report.is_error)
        self.assertFalse(digest.is_error)


if __name__ == "__main__":
    unittest.main()
