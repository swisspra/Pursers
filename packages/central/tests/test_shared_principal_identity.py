from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = PACKAGE_ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


class SharedPrincipalIdentityTests(unittest.IsolatedAsyncioTestCase):
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
        review_scopes = frozenset({"board:read", "board:write", "board:review"})
        work_scopes = frozenset({"board:read", "board:write"})
        self.admin = central.Principal("PR-admin", "admin", review_scopes)
        self.shared_worker = central.Principal(
            "PR-shared-worker", "shared-worker", work_scopes
        )
        self.shared_reviewer = central.Principal(
            "PR-shared-reviewer", "shared-reviewer", review_scopes
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        admin = await self.call("board_join", agent_name="admin-agent")
        self.admin_agent_id = admin.structured_content["agent_id"]
        for principal, membership_role in (
            (self.shared_worker, "member"),
            (self.shared_reviewer, "reviewer"),
        ):
            self.principal = self.admin
            await self.call(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principal.principal_id,
                role=membership_role,
            )
        for principal, name, role, capabilities in (
            (
                self.shared_worker,
                "worker-a",
                "worker",
                {"can_work": True, "can_review": False},
            ),
            (
                self.shared_worker,
                "worker-b",
                "worker",
                {"can_work": True, "can_review": False},
            ),
            (
                self.shared_reviewer,
                "review-worker",
                "worker",
                {"can_work": True, "can_review": False},
            ),
            (
                self.shared_reviewer,
                "reviewer-b",
                "reviewer",
                {"can_work": False, "can_review": True},
            ),
        ):
            self.principal = principal
            await self.call(
                "board_join",
                agent_name=name,
                role=role,
                capabilities=capabilities,
            )

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name, {"board_id": "pursers", **arguments}
        )

    async def create_for(self, ticket_id: str, agent_name: str = "worker-a") -> None:
        self.principal = self.admin
        await self.call(
            "ticket_create",
            ticket_id=ticket_id,
            agent_name="admin-agent",
            title="shared principal identity check",
            assigned_to=agent_name,
        )

    async def test_executor_cancel_isolated_by_agent_id_admin_can_cancel(self) -> None:
        await self.create_for("TK-shared-cancel")
        self.principal = self.shared_worker
        await self.call(
            "ticket_claim", agent_name="worker-a", ticket_id="TK-shared-cancel"
        )
        with self.assertRaisesRegex(
            ToolError, "cancel denied: requires creator, current executor, or board:review"
        ):
            await self.call(
                "ticket_cancel", agent_name="worker-b", ticket_id="TK-shared-cancel"
            )
        self.principal = self.admin
        canceled = await self.call(
            "ticket_cancel", agent_name="admin-agent", ticket_id="TK-shared-cancel"
        )
        self.assertEqual(canceled.structured_content["ticket"]["status"], "canceled")
        self.assertEqual(canceled.structured_content["permission"], "creator principal")

    async def test_reviewers_share_principal_but_not_submitter_identity(self) -> None:
        await self.create_for("TK-shared-review", "review-worker")
        self.principal = self.shared_reviewer
        await self.call(
            "ticket_claim", agent_name="review-worker", ticket_id="TK-shared-review"
        )
        submitted = await self.call(
            "ticket_submit", agent_name="review-worker", ticket_id="TK-shared-review"
        )
        self.assertEqual(
            submitted.structured_content["ticket"]["review_offer"]["agent_name"],
            "reviewer-b",
        )
        with self.assertRaisesRegex(ToolError, "self-review denied"):
            await self.call(
                "ticket_review_claim",
                agent_name="review-worker",
                ticket_id="TK-shared-review",
            )
        await self.call(
            "ticket_review_claim",
            agent_name="reviewer-b",
            ticket_id="TK-shared-review",
        )
        reviewed = await self.call(
            "ticket_review",
            agent_name="reviewer-b",
            ticket_id="TK-shared-review",
            verdict="approve",
        )
        self.assertEqual(reviewed.structured_content["ticket"]["status"], "closed")

    async def test_private_memories_are_isolated_by_agent_id_with_legacy_fallback(self) -> None:
        self.principal = self.shared_worker
        written = await self.call(
            "memory_write",
            agent_name="worker-a",
            title="worker-a-private",
            content="only seat a",
            scope="private",
        )
        memory_id = written.structured_content["memory"]["memory_id"]
        self.assertEqual(
            written.structured_content["event"]["recipient_identities"],
            [central.agent_id("pursers", self.shared_worker.principal_id, "worker-a")],
        )
        seat_a = await self.call("memory_read", agent_name="worker-a")
        seat_b = await self.call("memory_read", agent_name="worker-b")
        self.assertIn(memory_id, {item["memory_id"] for item in seat_a.structured_content["memories"]})
        self.assertNotIn(memory_id, {item["memory_id"] for item in seat_b.structured_content["memories"]})
        hidden = await self.call(
            "memory_search", agent_name="worker-b", query="worker-a-private"
        )
        self.assertEqual(hidden.structured_content["results"], [])

        def add_legacy(document: dict[str, object]) -> None:
            memories = document["memories"]
            assert isinstance(memories, list)
            memories.append(
                {
                    "memory_id": "MEM-legacy-private",
                    "title": "legacy-private",
                    "content": "legacy principal visibility",
                    "scope": "private",
                    "author_principal_id": self.shared_worker.principal_id,
                    "memory_type": "context",
                    "tags": [],
                    "related_files": [],
                    "related_tickets": [],
                    "priority": 0,
                    "pinned": False,
                    "created_at_epoch": 1.0,
                }
            )

        self.service.mutate("pursers", add_legacy, require_generation=False)
        legacy = await self.call("memory_read", agent_name="worker-b")
        self.assertIn(
            "MEM-legacy-private",
            {item["memory_id"] for item in legacy.structured_content["memories"]},
        )

    async def test_active_collision_journaled_and_takeover_stale_retired_allowed(self) -> None:
        self.principal = self.shared_worker
        reason = (
            "seat name already active under this principal; choose another name "
            "or pass allow_takeover=true"
        )
        with self.assertRaisesRegex(ToolError, reason.replace("+", r"\+")):
            await self.call("board_join", agent_name="worker-a")
        event = self.service.journal.read_after("pursers", 0, 1_000)["events"][-1]
        self.assertEqual(event["kind"], "seat_name_collision")
        self.assertEqual(event["refusal_reason"], reason)
        self.assertEqual(event["recipient_identities"], [self.admin_agent_id])

        takeover = await self.call(
            "board_join", agent_name="worker-a", allow_takeover=True
        )
        self.assertTrue(takeover.structured_content["rejoined"])
        with self.assertRaisesRegex(ToolError, "seat name already active"):
            await self.call("board_onboard", agent_name="worker-a")

        seat_a_id = central.agent_id(
            "pursers", self.shared_worker.principal_id, "worker-a"
        )

        def make_stale(document: dict[str, object]) -> None:
            members = document["members"]
            assert isinstance(members, dict)
            members[seat_a_id]["last_activity_at"] = central.iso_at(
                time.time() - 4 * 86_400
            )

        self.service.mutate("pursers", make_stale, require_generation=False)
        stale = await self.call("board_join", agent_name="worker-a")
        self.assertTrue(stale.structured_content["rejoined"])

        def retire(document: dict[str, object]) -> None:
            members = document["members"]
            assert isinstance(members, dict)
            members[seat_a_id]["lifecycle_status"] = "retired"

        self.service.mutate("pursers", retire, require_generation=False)
        retired = await self.call("board_join", agent_name="worker-a")
        self.assertTrue(retired.structured_content["rejoined"])
        self.assertEqual(retired.structured_content["lifecycle_status"], "active")


if __name__ == "__main__":
    unittest.main()
