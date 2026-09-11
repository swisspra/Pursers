from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = PACKAGE_ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from pursers_client import (  # noqa: E402
    COORDINATOR_QUESTION_ACCEPTED,
    COORDINATOR_QUESTION_ANSWERED,
    COORDINATOR_QUESTION_ASKED,
    KNOWN_EVENT_KINDS,
)


class CoordinatorQuestionTests(unittest.IsolatedAsyncioTestCase):
    """Non-pausing coordinator question/reply sibling (AN405)."""

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
            "PR-admin", "admin",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.coordinator = central.Principal(
            "PR-coord", "coord",
            frozenset({"board:read", "board:write", "board:coordinate"}),
        )
        self.coordinator_two = central.Principal(
            "PR-coord2", "coord2",
            frozenset({"board:read", "board:write", "board:coordinate"}),
        )
        self.worker = central.Principal(
            "PR-worker", "worker", frozenset({"board:read", "board:write"})
        )
        self.other = central.Principal(
            "PR-other", "other", frozenset({"board:read", "board:write"})
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call("board_join", agent_name="admin-agent")
        self.agent_ids: dict[str, str] = {}
        for principal in (
            self.coordinator, self.coordinator_two, self.worker, self.other
        ):
            await self.call(
                "board_member_add", agent_name="admin-agent",
                principal_id=principal.principal_id, role="member",
            )
            self.principal = principal
            joined = await self.call("board_join", agent_name=principal.canonical)
            self.agent_ids[principal.canonical] = (
                joined.structured_content["agent_id"]
            )
            self.principal = self.admin

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(name, {"board_id": "pursers", **arguments})

    async def register_coordinators(self, *canonicals: str) -> None:
        self.principal = self.admin
        await self.call(
            "board_state_update", agent_name="admin-agent",
            key=central.PROJECT_COORDINATORS_STATE_KEY,
            value=json.dumps(
                {"pursers": [self.agent_ids[name] for name in canonicals]}
            ),
        )

    async def claimed_ticket(self, ticket_id: str = "TK-comm") -> None:
        self.principal = self.admin
        await self.call(
            "ticket_create", ticket_id=ticket_id, agent_name="admin-agent",
            title="Coordinator communication", description="Ask without pausing",
            scope="interactive-no-send", required_fields=["test_output"],
            unassigned=True,
        )
        self.principal = self.worker
        await self.call("ticket_claim", ticket_id=ticket_id, agent_name="worker")

    async def ask(self, **extra: object):
        self.principal = self.worker
        return await self.call(
            "ticket_question_ask", ticket_id="TK-comm", agent_name="worker",
            message="Which rollout order do you want", kind="decision", **extra,
        )

    async def test_ask_does_not_pause_work_or_touch_the_lease(self) -> None:
        await self.register_coordinators("coord")
        await self.claimed_ticket()
        before = self.service.load("pursers")["tickets"]["TK-comm"]
        lease_before = before.get("lease_expires_at")
        result = (await self.ask()).structured_content
        self.assertTrue(result["ok"])
        self.assertEqual(result["event"]["kind"], COORDINATOR_QUESTION_ASKED)
        self.assertIn(COORDINATOR_QUESTION_ASKED, KNOWN_EVENT_KINDS)
        after = self.service.load("pursers")["tickets"]["TK-comm"]
        self.assertEqual(after["status"], "claimed")
        self.assertEqual(
            after["claimed_by_agent_id"], before["claimed_by_agent_id"]
        )
        # The lease is retained, not released. prepare_board_call renews it
        # implicitly on every board call, so it may move forward, never away.
        self.assertGreaterEqual(after["lease_expires_at"], lease_before)
        self.assertIn("TK-comm", result["implicitly_renewed"] or ["TK-comm"])
        self.assertNotIn("human_request", after)
        self.assertIsNone(after.get("dispatch_state", {}).get("request_id"))
        self.assertEqual(after["coordinator_questions"][0]["state"], "open")
        self.assertEqual(after["coordinator_questions"][0]["project"], "pursers")

    async def test_ask_routes_only_to_the_registered_project_coordinator(self) -> None:
        await self.register_coordinators("coord")
        await self.claimed_ticket()
        result = (await self.ask()).structured_content
        self.assertEqual(
            result["event"]["recipient_identities"], [self.agent_ids["coord"]]
        )
        self.assertNotIn(self.agent_ids["coord2"], result["event"]["recipient_identities"])

    async def test_ask_without_a_registered_coordinator_returns_a_useful_error(
        self,
    ) -> None:
        await self.claimed_ticket()
        with self.assertRaisesRegex(ToolError, "no project coordinator is registered"):
            await self.ask()

    async def test_unauthorized_participant_cannot_ask(self) -> None:
        await self.register_coordinators("coord")
        await self.claimed_ticket()
        self.principal = self.other
        with self.assertRaisesRegex(ToolError, "coordinator question requires"):
            await self.call(
                "ticket_question_ask", ticket_id="TK-comm", agent_name="other",
                message="Let me in", kind="decision",
            )

    async def test_review_lease_holder_may_ask_without_new_write_rights(self) -> None:
        await self.register_coordinators("coord")
        await self.claimed_ticket()

        def grant_review_lease(document: dict[str, object]) -> dict[str, object]:
            ticket = document["tickets"]["TK-comm"]
            ticket["review_lease"] = {
                "reviewer_agent_id": self.agent_ids["other"],
                "reviewer_agent_name": "other",
                "reviewer_principal_id": self.other.principal_id,
            }
            return {}

        self.service.mutate("pursers", grant_review_lease)
        self.principal = self.other
        result = (
            await self.call(
                "ticket_question_ask", ticket_id="TK-comm", agent_name="other",
                message="Is the ACL matrix in scope for this review",
                kind="information",
            )
        ).structured_content
        self.assertEqual(result["question"]["asker_role"], "reviewer")
        with self.assertRaisesRegex(ToolError, "registered project coordinator"):
            await self.call(
                "ticket_question_answer", ticket_id="TK-comm", agent_name="other",
                question_id=result["question_id"], action="answer", message="no",
            )

    async def test_worker_cannot_read_the_inbox_or_answer(self) -> None:
        await self.register_coordinators("coord")
        await self.claimed_ticket()
        asked = (await self.ask()).structured_content
        self.principal = self.worker
        with self.assertRaisesRegex(ToolError, "coordinator inbox requires"):
            await self.call("board_question_inbox", agent_name="worker")
        with self.assertRaisesRegex(ToolError, "registered project coordinator"):
            await self.call(
                "ticket_question_answer", ticket_id="TK-comm", agent_name="worker",
                question_id=asked["question_id"], action="answer", message="mine",
            )

    async def test_accept_then_answer_reaches_the_original_asker(self) -> None:
        await self.register_coordinators("coord")
        await self.claimed_ticket()
        asked = (await self.ask()).structured_content
        self.principal = self.coordinator
        inbox = (
            await self.call(
                "board_question_inbox", agent_name="coord", state="open"
            )
        ).structured_content
        self.assertEqual(inbox["total"], 1)
        self.assertNotIn("binding", inbox["questions"][0])
        accepted = (
            await self.call(
                "ticket_question_answer", ticket_id="TK-comm", agent_name="coord",
                question_id=asked["question_id"], action="accept",
                host_binding="codex-profile-a/session-1",
            )
        ).structured_content
        self.assertEqual(accepted["question"]["state"], "accepted")
        self.assertEqual(accepted["event"]["kind"], COORDINATOR_QUESTION_ACCEPTED)
        answered = (
            await self.call(
                "ticket_question_answer", ticket_id="TK-comm", agent_name="coord",
                question_id=asked["question_id"], action="answer",
                message="Ship the ACL negatives first",
                host_binding="codex-profile-a/session-1",
            )
        ).structured_content
        self.assertEqual(answered["question"]["state"], "answered")
        self.assertEqual(answered["event"]["kind"], COORDINATOR_QUESTION_ANSWERED)
        self.assertEqual(
            answered["event"]["recipient_identities"], [self.agent_ids["worker"]]
        )

    async def test_second_coordinator_cannot_consume_an_accepted_question(self) -> None:
        await self.register_coordinators("coord", "coord2")
        await self.claimed_ticket()
        asked = (await self.ask()).structured_content
        self.principal = self.coordinator
        await self.call(
            "ticket_question_answer", ticket_id="TK-comm", agent_name="coord",
            question_id=asked["question_id"], action="accept",
            host_binding="codex-profile-a/session-1",
        )
        self.principal = self.coordinator_two
        with self.assertRaisesRegex(ToolError, "already accepted by another"):
            await self.call(
                "ticket_question_answer", ticket_id="TK-comm", agent_name="coord2",
                question_id=asked["question_id"], action="answer",
                message="wrong profile", host_binding="codex-profile-b/session-9",
            )

    async def test_same_coordinator_rebinds_after_reconnect(self) -> None:
        await self.register_coordinators("coord")
        await self.claimed_ticket()
        asked = (await self.ask()).structured_content
        self.principal = self.coordinator
        await self.call(
            "ticket_question_answer", ticket_id="TK-comm", agent_name="coord",
            question_id=asked["question_id"], action="accept",
            host_binding="codex-profile-a/session-1",
        )
        rebound = (
            await self.call(
                "ticket_question_answer", ticket_id="TK-comm", agent_name="coord",
                question_id=asked["question_id"], action="answer",
                message="Answered after reconnect",
                host_binding="codex-profile-a/session-2",
            )
        ).structured_content
        self.assertEqual(rebound["question"]["state"], "answered")
        self.assertIsNotNone(rebound["question"]["rebound_at"])
        stored = self.service.load("pursers")["tickets"]["TK-comm"]
        entry = stored["coordinator_questions"][0]
        self.assertNotIn("codex-profile-a", json.dumps(entry))

    async def test_retry_is_idempotent_in_both_directions(self) -> None:
        await self.register_coordinators("coord")
        await self.claimed_ticket()
        first = (await self.ask(message_id="MSG-1")).structured_content
        second = (await self.ask(message_id="MSG-1")).structured_content
        self.assertTrue(second["duplicate"])
        self.assertIsNone(second["event"])
        self.assertEqual(second["question_id"], first["question_id"])
        stored = self.service.load("pursers")["tickets"]["TK-comm"]
        self.assertEqual(len(stored["coordinator_questions"]), 1)
        self.principal = self.coordinator
        await self.call(
            "ticket_question_answer", ticket_id="TK-comm", agent_name="coord",
            question_id=first["question_id"], action="answer", message="once",
        )
        retry = (
            await self.call(
                "ticket_question_answer", ticket_id="TK-comm", agent_name="coord",
                question_id=first["question_id"], action="answer", message="twice",
            )
        ).structured_content
        self.assertTrue(retry["duplicate"])
        self.assertIsNone(retry["event"])
        self.assertEqual(retry["question"]["answer"], "once")


if __name__ == "__main__":
    unittest.main()
