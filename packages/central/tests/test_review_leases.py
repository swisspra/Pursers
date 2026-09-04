from __future__ import annotations

import asyncio
import contextvars
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
    REVIEW_LEASE_EXPIRED,
    REVIEW_LEASE_KINDS,
    REVIEW_LEASE_RELEASED,
    TICKET_REVIEW_CLAIMED,
)


class ReviewLeaseTests(unittest.IsolatedAsyncioTestCase):
    def test_central_review_event_contract_matches_client_constant(self) -> None:
        self.assertEqual(
            central.REVIEW_EVENT_KINDS,
            frozenset({"board_review_policy_changed"}) | REVIEW_LEASE_KINDS,
        )

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
        scopes = frozenset({"board:read", "board:write", "board:review"})
        self.admin = central.Principal("PR-admin", "admin", scopes)
        self.worker = central.Principal(
            "PR-worker", "worker", frozenset({"board:read", "board:write"})
        )
        self.reviewer_a = central.Principal("PR-review-a", "review-a", scopes)
        self.reviewer_b = central.Principal("PR-review-b", "review-b", scopes)
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call("board_join", agent_name="admin-agent")
        for principal, role in (
            (self.worker, "member"),
            (self.reviewer_a, "reviewer"),
            (self.reviewer_b, "reviewer"),
        ):
            await self.call(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principal.principal_id,
                role=role,
            )
        for principal, name in (
            (self.worker, "worker-agent"),
            (self.reviewer_a, "reviewer-a"),
            (self.reviewer_b, "reviewer-b"),
        ):
            self.principal = principal
            await self.call("board_join", agent_name=name)

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name, {"board_id": "pursers", **arguments}
        )

    async def submitted_ticket(self) -> str:
        return await self.submitted_ticket_on("pursers")

    async def submitted_ticket_on(self, board_id: str) -> str:
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            board_id=board_id,
            agent_name="admin-agent",
            title="review lease target",
            description="verify exclusive review",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.principal = self.worker
        await self.call(
            "ticket_claim", board_id=board_id,
            agent_name="worker-agent", ticket_id=ticket_id,
        )
        submitted = await self.call(
            "ticket_submit",
            board_id=board_id,
            agent_name="worker-agent",
            ticket_id=ticket_id,
            summary="ready",
        )
        self.assertFalse(submitted.is_error)
        return ticket_id

    async def test_only_one_reviewer_wins_the_claim(self) -> None:
        ticket_id = await self.submitted_ticket()
        task_principal = contextvars.ContextVar(
            "review_race_principal", default=self.admin
        )
        central.current_principal = task_principal.get
        ready: set[str] = set()
        start = asyncio.Event()

        async def claim_as(
            principal: central.Principal, agent_name: str
        ):
            token = task_principal.set(principal)
            try:
                ready.add(agent_name)
                if len(ready) == 2:
                    start.set()
                await start.wait()
                return await self.call(
                    "ticket_review_claim",
                    agent_name=agent_name,
                    ticket_id=ticket_id,
                )
            finally:
                task_principal.reset(token)

        try:
            first, second = await asyncio.gather(
                claim_as(self.reviewer_a, "reviewer-a"),
                claim_as(self.reviewer_b, "reviewer-b"),
            )
        finally:
            central.current_principal = lambda: self.principal
        results = [("reviewer-a", first), ("reviewer-b", second)]
        winners = [item for item in results if item[1].structured_content["ok"]]
        losers = [item for item in results if not item[1].structured_content["ok"]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        holder_name, winner = winners[0]
        _, loser = losers[0]

        self.principal = self.admin
        snapshot = await self.call("board_snapshot")

        self.assertEqual(
            winner.structured_content["event"]["kind"], TICKET_REVIEW_CLAIMED
        )
        self.assertEqual(
            loser.structured_content["error"],
            {
                "code": "review_already_claimed",
                "holder_name": holder_name,
                "expires_at": winner.structured_content["review_lease"]["expires_at"],
            },
        )
        reviewer = next(
            row for row in snapshot.structured_content["agents"]
            if row["agent_name"] == holder_name
        )
        self.assertEqual(reviewer["status"], "working")
        self.assertEqual(
            reviewer["lease_expires_at"],
            winner.structured_content["review_lease"]["expires_at"],
        )

    async def test_review_claim_and_renew_use_board_claim_ttl(self) -> None:
        board_id = "short-review-ttl"
        self.principal = self.admin
        joined = await self.call(
            "board_join", board_id=board_id,
            agent_name="admin-agent", claim_ttl_s=120,
        )
        self.assertEqual(joined.structured_content["claim_ttl_s"], 120)
        for principal, role, name in (
            (self.worker, "member", "worker-agent"),
            (self.reviewer_a, "reviewer", "reviewer-a"),
        ):
            self.principal = self.admin
            await self.call(
                "board_member_add", board_id=board_id,
                agent_name="admin-agent", principal_id=principal.principal_id,
                role=role,
            )
            self.principal = principal
            await self.call("board_join", board_id=board_id, agent_name=name)

        ticket_id = await self.submitted_ticket_on(board_id)
        self.principal = self.reviewer_a
        with patch.object(central.time, "time", return_value=1_000.0):
            claimed = await self.call(
                "ticket_review_claim", board_id=board_id,
                agent_name="reviewer-a", ticket_id=ticket_id,
            )
        lease = claimed.structured_content["review_lease"]
        self.assertEqual(lease["ttl_s"], 120)
        self.assertEqual(lease["expires_at_epoch"], 1_120.0)

        with patch.object(central.time, "time", return_value=1_050.0):
            renewed = await self.call(
                "lease_renew", board_id=board_id,
                agent_name="reviewer-a", ticket_id=ticket_id,
            )
        self.assertEqual(renewed.structured_content["lease_kind"], "review")
        self.assertEqual(renewed.structured_content["ttl_s"], 120)
        self.assertEqual(
            renewed.structured_content["lease_expires_at"], central.iso_at(1_170.0)
        )

    async def test_submitter_principal_cannot_claim_review(self) -> None:
        self.principal = self.reviewer_a
        created = await self.call(
            "ticket_create", agent_name="reviewer-a", ticket_id="TK-self-review",
            title="self review target",
        )
        self.assertFalse(created.is_error)
        await self.call(
            "ticket_claim", agent_name="reviewer-a", ticket_id="TK-self-review"
        )
        await self.call(
            "ticket_submit", agent_name="reviewer-a", ticket_id="TK-self-review"
        )
        with self.assertRaisesRegex(ToolError, "self-review denied"):
            await self.call(
                "ticket_review_claim",
                agent_name="reviewer-a",
                ticket_id="TK-self-review",
            )

    async def test_expired_review_lease_is_claimable_and_emits_event(self) -> None:
        ticket_id = await self.submitted_ticket()
        self.principal = self.reviewer_a
        with patch.object(central.time, "time", return_value=1_000.0):
            await self.call(
                "ticket_review_claim", agent_name="reviewer-a", ticket_id=ticket_id
            )
        self.principal = self.reviewer_b
        with patch.object(central.time, "time", return_value=1_901.0):
            claimed = await self.call(
                "ticket_review_claim", agent_name="reviewer-b", ticket_id=ticket_id
            )
        self.assertTrue(claimed.structured_content["ok"])
        self.assertEqual(
            [event["kind"] for event in claimed.structured_content["release_events"]],
            [REVIEW_LEASE_EXPIRED],
        )

    async def test_verdict_auto_claims_when_free_and_rejects_other_holder(self) -> None:
        first_id = await self.submitted_ticket()
        self.principal = self.reviewer_a
        approved = await self.call(
            "ticket_review",
            agent_name="reviewer-a",
            ticket_id=first_id,
            verdict="approve",
            review_notes="verified",
        )
        self.assertFalse(approved.is_error)
        self.assertEqual(approved.structured_content["ticket"]["status"], "closed")
        self.assertNotIn("review_lease", approved.structured_content["ticket"])
        self.assertEqual(
            approved.structured_content["review_claim_event"]["kind"],
            TICKET_REVIEW_CLAIMED,
        )
        self.assertEqual(
            approved.structured_content["review_release_event"]["kind"],
            REVIEW_LEASE_RELEASED,
        )

        second_id = await self.submitted_ticket()
        self.principal = self.reviewer_a
        await self.call(
            "ticket_review_claim", agent_name="reviewer-a", ticket_id=second_id
        )
        self.principal = self.reviewer_b
        with self.assertRaisesRegex(ToolError, "review lease is held by reviewer-a"):
            await self.call(
                "ticket_review",
                agent_name="reviewer-b",
                ticket_id=second_id,
                verdict="approve",
                review_notes="duplicate",
            )

    async def test_review_renew_release_and_list_states(self) -> None:
        ticket_id = await self.submitted_ticket()
        self.principal = self.reviewer_a
        with patch.object(central.time, "time", return_value=2_000.0):
            claimed = await self.call(
                "ticket_review_claim", agent_name="reviewer-a", ticket_id=ticket_id
            )
        with patch.object(central.time, "time", return_value=2_100.0):
            renewed = await self.call(
                "lease_renew", agent_name="reviewer-a", ticket_id=ticket_id
            )
            listed = await self.call(
                "ticket_list", agent_name="reviewer-a", status="submitted"
            )
            unclaimed = await self.call(
                "ticket_list",
                agent_name="reviewer-a",
                status="submitted",
                review_unclaimed_only=True,
            )
            self.principal = self.reviewer_b
            listed_by_other = await self.call(
                "ticket_list", agent_name="reviewer-b", status="submitted"
            )
        self.assertEqual(renewed.structured_content["lease_kind"], "review")
        self.assertGreater(
            renewed.structured_content["lease_expires_at"],
            claimed.structured_content["review_lease"]["expires_at"],
        )
        self.assertEqual(listed.structured_content["tickets"][0]["review_state"], "claimed_by_me")
        self.assertEqual(unclaimed.structured_content["tickets"], [])
        other_row = listed_by_other.structured_content["tickets"][0]
        self.assertEqual(other_row["review_state"], "claimed_by_other")
        self.assertEqual(other_row["review_claimed_by"], "reviewer-a")

        self.principal = self.reviewer_a
        with patch.object(central.time, "time", return_value=2_200.0):
            released = await self.call(
                "ticket_review_release",
                agent_name="reviewer-a",
                ticket_id=ticket_id,
                reason="handoff",
            )
        self.assertEqual(
            released.structured_content["event"]["kind"], REVIEW_LEASE_RELEASED
        )
        self.assertIn(released.structured_content["event"]["kind"], REVIEW_LEASE_KINDS)
        self.assertNotIn("review_lease", released.structured_content["ticket"])


if __name__ == "__main__":
    unittest.main()
