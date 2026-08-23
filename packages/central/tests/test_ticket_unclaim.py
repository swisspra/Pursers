from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "onboard_central"))

import central  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


class TicketUnclaimTests(unittest.IsolatedAsyncioTestCase):
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
        self.member = central.Principal(
            "PR-member",
            "member-canonical",
            frozenset({"board:read", "board:write"}),
        )
        self.other_member = central.Principal(
            "PR-other-member",
            "other-member-canonical",
            frozenset({"board:read", "board:write"}),
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call("board_join", agent_name="admin-agent")
        for principal in (self.member, self.other_member):
            await self.call(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principal.principal_id,
                role="member",
            )
        self.principal = self.member
        await self.call("board_join", agent_name="member-agent")
        self.principal = self.other_member
        await self.call("board_join", agent_name="other-agent")

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        result = await self.mcp.call_tool(
            name,
            {"board_id": "pursers", **arguments},
        )
        return result

    async def create_and_claim(self) -> str:
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="unclaim test ticket",
            description="exercise explicit claim release",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        self.assertFalse(created.is_error)
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.principal = self.member
        claimed = await self.call(
            "ticket_claim", agent_name="member-agent", ticket_id=ticket_id
        )
        self.assertFalse(claimed.is_error)
        return ticket_id

    async def test_claimer_unclaims_to_open_and_emits_journal_event(self) -> None:
        ticket_id = await self.create_and_claim()
        before_seq = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]

        result = await self.call(
            "ticket_unclaim", agent_name="member-agent", ticket_id=ticket_id
        )

        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertEqual(payload["ticket"]["status"], "open")
        self.assertEqual(payload["permission"], "current claiming agent")
        for key in (
            "claimed_by_agent_id",
            "claimed_by_principal_id",
            "claimed_by",
            "claimed_at",
            "lease_expires_at_epoch",
            "lease_expires_at",
            "lease_renewed_at",
            "ttl_s",
        ):
            self.assertNotIn(key, payload["ticket"])
        self.assertEqual(payload["event"]["kind"], "ticket_status_changed")
        self.assertEqual(payload["event"]["status_from"], "claimed")
        self.assertEqual(payload["event"]["status_to"], "open")
        journal = self.service.journal.read_after("pursers", before_seq, 10)
        self.assertEqual(len(journal["events"]), 1)
        self.assertEqual(journal["events"][0]["ticket_id"], ticket_id)
        self.assertEqual(journal["events"][0]["status_to"], "open")

    async def test_non_claimer_non_admin_is_rejected(self) -> None:
        ticket_id = await self.create_and_claim()
        self.principal = self.other_member

        with self.assertRaisesRegex(
            ToolError, "current claiming agent or board admin"
        ):
            await self.call(
                "ticket_unclaim", agent_name="other-agent", ticket_id=ticket_id
            )
        ticket = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(ticket["status"], "claimed")
        self.assertEqual(ticket["claimed_by"], "member-agent")

    async def test_admin_can_unclaim_another_agents_ticket(self) -> None:
        ticket_id = await self.create_and_claim()
        self.principal = self.admin

        result = await self.call(
            "ticket_unclaim", agent_name="admin-agent", ticket_id=ticket_id
        )

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["permission"], "board admin")
        self.assertEqual(result.structured_content["ticket"]["status"], "open")

    async def test_submitted_and_closed_tickets_are_rejected(self) -> None:
        ticket_id = await self.create_and_claim()
        submitted = await self.call(
            "ticket_submit",
            agent_name="member-agent",
            ticket_id=ticket_id,
            summary="done",
        )
        self.assertFalse(submitted.is_error)

        with self.assertRaisesRegex(ToolError, "ticket is submitted"):
            await self.call(
                "ticket_unclaim", agent_name="member-agent", ticket_id=ticket_id
            )

        self.principal = self.admin
        reviewed = await self.call(
            "ticket_review",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            verdict="approve",
            review_notes="approved",
        )
        self.assertFalse(reviewed.is_error)
        with self.assertRaisesRegex(ToolError, "ticket is closed"):
            await self.call(
                "ticket_unclaim", agent_name="admin-agent", ticket_id=ticket_id
            )


if __name__ == "__main__":
    unittest.main()
