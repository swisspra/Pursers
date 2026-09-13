from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


class JournalCompactTests(unittest.IsolatedAsyncioTestCase):
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
        self.principal = central.Principal(
            "PR-admin",
            "admin-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        joined = await self.call("board_join", agent_name="admin-agent")
        self.assertFalse(joined.is_error)
        ticket = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="durable ticket",
            description="must survive compaction",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        self.assertFalse(ticket.is_error)
        memory = await self.call(
            "memory_write",
            agent_name="admin-agent",
            title="durable memory",
            content="must survive compaction",
            scope="project",
        )
        self.assertFalse(memory.is_error)

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name, {"board_id": "pursers", **arguments}
        )

    def seed_journal(self, total: int = 510) -> None:
        current = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]
        for index in range(current, total):
            self.service.journal.append(
                "pursers",
                {
                    "kind": "memory_written",
                    "actor": "AI-synthetic",
                    "payload_ref": f"board://pursers/memory/MEM-{index}",
                    "memory_id": f"MEM-{index}",
                    "fixture_provenance": "synthetic journal compaction test",
                },
            )

    async def test_compacts_events_only_and_stale_cursor_requires_resync(
        self,
    ) -> None:
        self.seed_journal()
        self.service.cursors.ack(
            self.principal.principal_id, "admin-agent", "pursers", 1
        )
        board_before = copy.deepcopy(self.service.load("pursers"))
        cursor_before = self.service.cursors.get(
            self.principal.principal_id, "admin-agent", "pursers"
        )

        result = await self.call("journal_compact", retain_last=500)

        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertEqual(payload["removed"], 10)
        self.assertEqual(payload["retained"], 500)
        self.assertEqual(payload["compacted_through"], 10)
        self.assertEqual(payload["latest_cursor"], 510)
        self.assertTrue(payload["durable_records_untouched"])
        retained = self.service.journal.read_after("pursers", 10, 1000)
        self.assertEqual(len(retained["events"]), 500)
        self.assertEqual(retained["events"][0]["seq"], 11)
        self.assertEqual(retained["events"][-1]["seq"], 510)
        self.assertEqual(self.service.load("pursers"), board_before)
        self.assertEqual(
            self.service.cursors.get(
                self.principal.principal_id, "admin-agent", "pursers"
            ),
            cursor_before,
        )

        catchup = await self.call(
            "board_catchup",
            agent_name="admin-agent",
            cursor=1,
            ack=True,
        )
        self.assertFalse(catchup.is_error)
        self.assertTrue(catchup.structured_content["resync_required"])
        self.assertEqual(catchup.structured_content["events"], [])
        self.assertEqual(catchup.structured_content["reset_cursor"], 510)
        self.assertEqual(catchup.structured_content["acknowledged_cursor"], 1)

    async def test_retain_last_floor_is_enforced_without_changes(self) -> None:
        self.seed_journal()
        before = self.service.journal.read_after("pursers", 0, 1000)

        with self.assertRaisesRegex(ToolError, "at least 500"):
            await self.call("journal_compact", retain_last=499)

        after = self.service.journal.read_after("pursers", 0, 1000)
        self.assertEqual(after, before)

    async def test_admin_sets_retention_and_hard_row_cap(self) -> None:
        self.seed_journal(620)

        result = await self.call(
            "board_journal_retention_set",
            agent_name="admin-agent",
            journal_retention_days=0,
            journal_row_cap=501,
        )

        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertEqual(payload["previous_journal_retention_days"], 7)
        self.assertEqual(payload["previous_journal_row_cap"], 50_000)
        self.assertEqual(payload["journal_retention_days"], 0)
        self.assertEqual(payload["journal_row_cap"], 501)
        self.assertTrue(payload["changed"])
        self.assertGreater(payload["journal_compaction"]["removed"], 0)
        journal = self.service.store.load(
            self.service.journal._path("pursers"), dict
        )
        self.assertEqual(len(journal["rows"]), 501)
        self.assertEqual(
            journal["rows"][-1]["archived_reason"], "journal_compaction"
        )

        status = await self.call("board_status", agent_name="admin-agent")
        self.assertEqual(status.structured_content["journal_retention_days"], 0)
        self.assertEqual(status.structured_content["journal_row_cap"], 501)

    async def test_coordinator_can_set_retention_but_member_cannot(self) -> None:
        coordinator = central.Principal(
            "PR-coordinator",
            "coordinator",
            frozenset({"board:read", "board:coordinate"}),
        )
        member = central.Principal(
            "PR-member",
            "member",
            frozenset({"board:read", "board:write"}),
        )
        for principal in (coordinator, member):
            self.principal = central.Principal(
                "PR-admin",
                "admin-canonical",
                frozenset({"board:read", "board:write", "board:review"}),
            )
            await self.call(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principal.principal_id,
                role="member",
            )
        self.principal = coordinator
        await self.call(
            "board_join",
            agent_name="coordinator-agent",
            role="coordinator",
        )
        configured = await self.call(
            "board_journal_retention_set",
            agent_name="coordinator-agent",
            journal_retention_days=3,
            journal_row_cap=2_000,
        )
        self.assertFalse(configured.is_error)
        self.assertEqual(configured.structured_content["journal_row_cap"], 2_000)

        self.principal = member
        await self.call("board_join", agent_name="member-agent")
        with self.assertRaisesRegex(
            ToolError, "requires board admin or coordinator"
        ):
            await self.call(
                "board_journal_retention_set",
                agent_name="member-agent",
                journal_retention_days=4,
                journal_row_cap=3_000,
            )

    async def test_journal_retention_bounds_are_validated(self) -> None:
        for arguments, message in (
            (
                {"journal_retention_days": 7, "journal_row_cap": 500},
                "journal_row_cap",
            ),
            (
                {"journal_retention_days": 7, "journal_row_cap": True},
                "journal_row_cap",
            ),
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ToolError, message):
                    await self.call(
                        "board_journal_retention_set",
                        agent_name="admin-agent",
                        **arguments,
                    )


if __name__ == "__main__":
    unittest.main()
