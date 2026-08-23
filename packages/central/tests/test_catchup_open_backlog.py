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


class CatchupOpenBacklogTests(unittest.IsolatedAsyncioTestCase):
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
        self.late_member = central.Principal(
            "PR-late-member",
            "late-member-canonical",
            frozenset({"board:read", "board:write"}),
        )
        self.reviewer = central.Principal(
            "PR-reviewer",
            "reviewer-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call("board_join", agent_name="admin-agent")
        await self.call(
            "board_member_add",
            agent_name="admin-agent",
            principal_id=self.late_member.principal_id,
            role="member",
        )

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name,
            {"board_id": "pursers", **arguments},
        )

    async def create_ticket(self, title: str) -> str:
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title=title,
            description="catchup backlog test",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        self.assertFalse(created.is_error)
        return created.structured_content["ticket"]["ticket_id"]

    async def join_late_member_and_catch_up(self):
        self.principal = self.late_member
        joined = await self.call("board_join", agent_name="late-agent")
        self.assertFalse(joined.is_error)
        catchup = await self.call(
            "board_catchup",
            agent_name="late-agent",
            cursor=0,
            limit=100,
            ack=False,
        )
        self.assertFalse(catchup.is_error)
        return catchup.structured_content

    async def test_new_agent_sees_preexisting_open_ticket_from_cursor_zero(self) -> None:
        ticket_id = await self.create_ticket("open before late join")

        catchup = await self.join_late_member_and_catch_up()

        matching = [
            event
            for event in catchup["events"]
            if event.get("ticket_id") == ticket_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["kind"], "ticket_created")
        self.assertEqual(matching[0]["status_to"], "open")

    async def test_closed_ticket_is_not_resurfaced_for_new_agent(self) -> None:
        ticket_id = await self.create_ticket("closed before late join")
        claimed = await self.call(
            "ticket_claim", agent_name="admin-agent", ticket_id=ticket_id
        )
        self.assertFalse(claimed.is_error)
        submitted = await self.call(
            "ticket_submit",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            summary="done",
        )
        self.assertFalse(submitted.is_error)
        await self.call(
            "board_member_add",
            agent_name="admin-agent",
            principal_id=self.reviewer.principal_id,
            role="reviewer",
        )
        self.principal = self.reviewer
        await self.call("board_join", agent_name="reviewer-agent")
        reviewed = await self.call(
            "ticket_review",
            agent_name="reviewer-agent",
            ticket_id=ticket_id,
            verdict="approve",
            review_notes="approved",
        )
        self.assertFalse(reviewed.is_error)

        catchup = await self.join_late_member_and_catch_up()

        self.assertFalse(
            any(
                event.get("ticket_id") == ticket_id
                for event in catchup["events"]
            )
        )


if __name__ == "__main__":
    unittest.main()
