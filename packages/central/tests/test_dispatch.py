from __future__ import annotations

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
    DISPATCH_KINDS,
    OFFER_EXPIRED,
    OFFER_REVOKED,
    REVIEW_OFFERED,
    TICKET_OFFERED,
)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
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
        self.worker_a = central.Principal("PR-worker-a", "worker-a", work_scopes)
        self.worker_b = central.Principal("PR-worker-b", "worker-b", work_scopes)
        self.reviewer_a = central.Principal("PR-review-a", "review-a", review_scopes)
        self.reviewer_b = central.Principal("PR-review-b", "review-b", review_scopes)
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call(
            "board_join", agent_name="admin-agent",
            capabilities={"can_work": False, "can_review": False},
        )

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(name, {"board_id": "pursers", **arguments})

    async def add_seat(
        self,
        principal: central.Principal,
        name: str,
        capabilities: dict[str, object],
        *,
        role: str = "member",
    ) -> str:
        self.principal = self.admin
        await self.call(
            "board_member_add", agent_name="admin-agent",
            principal_id=principal.principal_id, role=role,
        )
        self.principal = principal
        joined = await self.call(
            "board_join", agent_name=name,
            role="reviewer" if role == "reviewer" else "worker",
            capabilities=capabilities,
        )
        return joined.structured_content["agent_id"]

    async def test_declared_role_is_independent_from_token_scopes(self) -> None:
        self.principal = self.admin
        worker = await self.call("board_join", agent_name="admin-worker-default")
        self.assertEqual(worker.structured_content["role"], "worker")
        self.assertTrue(worker.structured_content["capabilities"]["can_work"])
        self.assertFalse(worker.structured_content["capabilities"]["can_review"])

        reviewer = await self.call(
            "board_onboard", agent_name="admin-reviewer", role="reviewer"
        )
        reviewer_id = reviewer.structured_content["agent_id"]
        self.assertEqual(reviewer.structured_content["role"], "reviewer")
        self.assertFalse(reviewer.structured_content["capabilities"]["can_work"])
        self.assertTrue(reviewer.structured_content["capabilities"]["can_review"])

        inferred = await self.call(
            "board_join", agent_name="role-migration", role="reviewer"
        )
        self.assertTrue(inferred.structured_content["capabilities"]["can_review"])
        migrated = await self.call("board_join", agent_name="role-migration")
        self.assertEqual(migrated.structured_content["role"], "worker")
        self.assertFalse(migrated.structured_content["capabilities"]["can_review"])

        self.principal = self.admin
        await self.call(
            "board_member_add", agent_name="admin-agent",
            principal_id=self.worker_a.principal_id, role="member",
        )
        self.principal = self.worker_a
        with self.assertRaisesRegex(ToolError, "board:review"):
            await self.call(
                "board_join", agent_name="worker-a-reviewer", role="reviewer"
            )

        coordinator = central.Principal(
            "PR-coordinate", "coordinate",
            frozenset({"board:read", "board:coordinate"}),
        )
        self.principal = self.admin
        await self.call(
            "board_member_add", agent_name="admin-agent",
            principal_id=coordinator.principal_id, role="member",
        )
        self.principal = coordinator
        with self.assertRaisesRegex(ToolError, "board:write"):
            await self.call("board_join", agent_name="coordinate-worker")
        coordination_ids = []
        for role in ("coordinator", "orchestrator"):
            joined = await self.call(
                "board_join", agent_name=f"{role}-agent", role=role
            )
            coordination_ids.append(joined.structured_content["agent_id"])
            self.assertEqual(joined.structured_content["role"], role)
            self.assertFalse(joined.structured_content["capabilities"]["can_work"])
            self.assertFalse(joined.structured_content["capabilities"]["can_review"])

        self.principal = self.admin
        with self.assertRaisesRegex(ToolError, "can_review must be false"):
            await self.call(
                "board_join", agent_name="hybrid-worker",
                capabilities={"can_review": True},
            )
        with self.assertRaisesRegex(ToolError, "can_work must be false"):
            await self.call(
                "board_join", agent_name="hybrid-reviewer", role="reviewer",
                capabilities={"can_work": True},
            )

        self.principal = self.admin
        status = await self.call("board_status")
        agents = status.structured_content["agents"]
        admin = next(row for row in agents if row["agent_name"] == "admin-agent")
        self.assertEqual(admin["role"], "worker")
        self.assertIn("board:review", admin["scopes"])

        await self.call(
            "agent_capabilities_set", agent_name="admin-worker-default",
            capabilities={"tier_max": 2},
        )
        await self.call(
            "board_dispatch_policy_set", agent_name="admin-agent", offer_ttl_s=60
        )
        non_workers = [reviewer_id, *coordination_ids]
        created = await self.create(prefer_agents=non_workers)
        offered_to = created.structured_content["ticket"]["work_offer"]["agent_id"]
        self.assertNotIn(offered_to, non_workers)

    async def create(self, **extra: object):
        self.principal = self.admin
        return await self.call(
            "ticket_create", agent_name="admin-agent", title="dispatch target",
            description="dispatch by capability", target_url="pursers/packages/central",
            scope="interactive-no-send", required_fields=["test_output"],
            **extra,
        )

    async def test_work_offer_uses_lowest_sufficient_tier_and_targeted_event(self) -> None:
        tier_three = await self.add_seat(
            self.worker_a, "worker-a", {"tier_max": 3, "skills": ["python"]}
        )
        tier_two = await self.add_seat(
            self.worker_b, "worker-b", {"tier_max": 2, "skills": ["python"]}
        )
        created = await self.create(tier=2, skills_required=["python"])
        ticket = created.structured_content["ticket"]
        event = created.structured_content["dispatch_event"]
        self.assertEqual(ticket["work_offer"]["agent_id"], tier_two)
        self.assertNotEqual(tier_two, tier_three)
        self.assertEqual(event["kind"], TICKET_OFFERED)
        self.assertEqual(event["recipient_identities"], [tier_two])
        self.principal = self.admin
        status = await self.call("board_status")
        selected = next(
            item for item in status.structured_content["agents"]
            if item["agent_id"] == tier_two
        )
        self.assertEqual(selected["status"], "busy")
        self.assertEqual(selected["current_offer"]["ticket_id"], ticket["ticket_id"])
        self.assertEqual(selected["capabilities"]["tier_max"], 2)

    async def test_ticket_update_revokes_offer_and_redispatches(self) -> None:
        worker_a = await self.add_seat(self.worker_a, "worker-a", {"tier_max": 2})
        worker_b = await self.add_seat(self.worker_b, "worker-b", {"tier_max": 2})
        created = await self.create()
        ticket = created.structured_content["ticket"]
        first = ticket["work_offer"]["agent_id"]
        self.principal = self.admin
        updated = await self.call(
            "ticket_update", agent_name="admin-agent", ticket_id=ticket["ticket_id"],
            exclude_agents=[first],
        )
        self.assertNotEqual(updated.structured_content["ticket"]["work_offer"]["agent_id"], first)
        self.assertEqual(
            [item["kind"] for item in updated.structured_content["dispatch_events"]],
            [OFFER_REVOKED, TICKET_OFFERED],
        )
        self.assertIn(first, {worker_a, worker_b})

    async def test_preference_wins_and_non_offered_claim_is_structured(self) -> None:
        worker_a = await self.add_seat(self.worker_a, "worker-a", {"tier_max": 2})
        worker_b = await self.add_seat(self.worker_b, "worker-b", {"tier_max": 2})
        created = await self.create(prefer_agents=[worker_b])
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.assertEqual(created.structured_content["ticket"]["work_offer"]["agent_id"], worker_b)
        self.principal = self.worker_a
        denied = await self.call(
            "ticket_claim", agent_name="worker-a", ticket_id=ticket_id
        )
        self.assertEqual(
            denied.structured_content["error"]["code"], "claim_not_offered"
        )
        self.principal = self.worker_b
        claimed = await self.call(
            "ticket_claim", agent_name="worker-b", ticket_id=ticket_id
        )
        self.assertTrue(claimed.structured_content["ok"])
        self.assertNotIn("work_offer", claimed.structured_content["ticket"])
        self.assertEqual(
            claimed.structured_content["ticket"]["dispatch_state"]["state"],
            "claimed",
        )
        self.assertNotEqual(worker_a, worker_b)

    async def test_max_parallel_one_distributes_two_offers(self) -> None:
        worker_a = await self.add_seat(self.worker_a, "worker-a", {"tier_max": 2})
        worker_b = await self.add_seat(self.worker_b, "worker-b", {"tier_max": 2})
        first = await self.create()
        second = await self.create()
        offered = {
            first.structured_content["ticket"]["work_offer"]["agent_id"],
            second.structured_content["ticket"]["work_offer"]["agent_id"],
        }
        self.assertEqual(offered, {worker_a, worker_b})

    async def test_unassignable_work_rejects_every_ineligible_claim(self) -> None:
        busy_principal = central.Principal(
            "PR-busy", "busy", frozenset({"board:read", "board:write"})
        )
        busy = await self.add_seat(
            busy_principal, "worker-busy",
            {"tier_max": 3, "skills": ["rust"]},
        )
        first = await self.create(tier=3, skills_required=["rust"])
        self.principal = busy_principal
        await self.call(
            "ticket_claim", agent_name="worker-busy",
            ticket_id=first.structured_content["ticket"]["ticket_id"],
        )
        low = await self.add_seat(
            self.worker_a, "worker-low", {"tier_max": 1, "skills": ["rust"]}
        )
        missing = await self.add_seat(
            self.worker_b, "worker-missing", {"tier_max": 3, "skills": []}
        )
        disabled_principal = central.Principal(
            "PR-disabled", "disabled", frozenset({"board:read", "board:write"})
        )
        disabled = await self.add_seat(
            disabled_principal, "worker-disabled",
            {"tier_max": 3, "skills": ["rust"], "can_work": False},
        )
        excluded_principal = central.Principal(
            "PR-excluded", "excluded", frozenset({"board:read", "board:write"})
        )
        excluded = await self.add_seat(
            excluded_principal, "worker-excluded",
            {"tier_max": 3, "skills": ["rust"]},
        )
        blocked = await self.create(
            tier=3, skills_required=["rust"], exclude_agents=[excluded]
        )
        ticket_id = blocked.structured_content["ticket"]["ticket_id"]
        self.assertEqual(
            blocked.structured_content["ticket"]["dispatch_state"],
            {"state": "unassignable", "kind": "work", "reason": "no_eligible_worker"},
        )
        attempts = (
            (self.worker_a, "worker-low", low),
            (self.worker_b, "worker-missing", missing),
            (disabled_principal, "worker-disabled", disabled),
            (excluded_principal, "worker-excluded", excluded),
            (busy_principal, "worker-busy", busy),
        )
        for principal, name, _ in attempts:
            self.principal = principal
            denied = await self.call(
                "ticket_claim", agent_name=name, ticket_id=ticket_id
            )
            self.assertFalse(denied.structured_content["ok"])
            self.assertEqual(denied.structured_content["error"]["code"], "claim_not_offered")
            self.assertEqual(denied.structured_content["error"]["reason"], "no_eligible_worker")
        self.principal = self.admin
        fetched = await self.call("ticket_get", ticket_id=ticket_id)
        self.assertEqual(fetched.structured_content["ticket"]["status"], "open")
        self.assertNotIn("claimed_by_agent_id", fetched.structured_content["ticket"])

    async def test_unassignable_redispatches_when_matching_seat_joins(self) -> None:
        created = await self.create(tier=3, skills_required=["rust"])
        ticket = created.structured_content["ticket"]
        self.assertEqual(ticket["dispatch_state"]["state"], "unassignable")
        worker = await self.add_seat(
            self.worker_a, "worker-a", {"tier_max": 3, "skills": ["rust"]}
        )
        fetched = await self.call("ticket_get", ticket_id=ticket["ticket_id"])
        self.assertEqual(fetched.structured_content["ticket"]["work_offer"]["agent_id"], worker)

    async def test_offer_expiry_emits_event_and_selects_next_worker(self) -> None:
        worker_a = await self.add_seat(self.worker_a, "worker-a", {"tier_max": 2})
        worker_b = await self.add_seat(self.worker_b, "worker-b", {"tier_max": 2})
        self.principal = self.admin
        await self.call(
            "board_dispatch_policy_set", agent_name="admin-agent", offer_ttl_s=1
        )
        with patch.object(central.time, "time", return_value=1000.0):
            created = await self.create()
        first = created.structured_content["ticket"]["work_offer"]["agent_id"]
        with patch.object(central.time, "time", return_value=1002.0):
            reaped = await self.call("board_reap")
        kinds = [item["kind"] for item in reaped.structured_content["release_events"]]
        self.assertIn(OFFER_EXPIRED, kinds)
        self.principal = self.admin
        fetched = await self.call(
            "ticket_get", ticket_id=created.structured_content["ticket"]["ticket_id"]
        )
        self.assertIn(first, {worker_a, worker_b})
        self.assertNotEqual(fetched.structured_content["ticket"]["work_offer"]["agent_id"], first)

    async def test_offer_limit_falls_back_to_broadcast(self) -> None:
        await self.add_seat(self.worker_a, "worker-a", {"tier_max": 2})
        self.principal = self.admin
        await self.call(
            "board_dispatch_policy_set", agent_name="admin-agent", offer_ttl_s=1
        )
        with patch.object(central.time, "time", return_value=1000.0):
            created = await self.create()
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        for now in (1002.0, 1004.0, 1006.0):
            with patch.object(central.time, "time", return_value=now):
                await self.call("board_reap")
        fetched = await self.call("ticket_get", ticket_id=ticket_id)
        ticket = fetched.structured_content["ticket"]
        self.assertEqual(ticket["dispatch_state"]["state"], "broadcast")
        self.assertNotIn("work_offer", ticket)
        self.principal = self.worker_a
        claimed = await self.call(
            "ticket_claim", agent_name="worker-a", ticket_id=ticket_id
        )
        self.assertTrue(claimed.structured_content["ok"])

    async def test_capability_update_redispatches_unassignable_ticket(self) -> None:
        worker = await self.add_seat(
            self.worker_a, "worker-a", {"tier_max": 2, "skills": ["python"]}
        )
        created = await self.create(tier=3, skills_required=["python"])
        self.assertEqual(
            created.structured_content["ticket"]["dispatch_state"]["state"],
            "unassignable",
        )
        self.principal = self.worker_a
        updated = await self.call(
            "agent_capabilities_set", agent_name="worker-a",
            capabilities={"tier_max": 3, "skills": ["python"]},
        )
        self.assertEqual(updated.structured_content["capabilities"]["tier_max"], 3)
        fetched = await self.call(
            "ticket_get", ticket_id=created.structured_content["ticket"]["ticket_id"]
        )
        self.assertEqual(fetched.structured_content["ticket"]["work_offer"]["agent_id"], worker)

    async def test_review_offer_is_gated_and_second_opinion_rotates(self) -> None:
        await self.add_seat(self.worker_a, "worker-a", {"tier_max": 2})
        reviewer_a = await self.add_seat(
            self.reviewer_a, "reviewer-a", {"tier_max": 2, "can_review": True, "can_work": False}, role="reviewer"
        )
        reviewer_b = await self.add_seat(
            self.reviewer_b, "reviewer-b", {"tier_max": 2, "can_review": True, "can_work": False}, role="reviewer"
        )
        created = await self.create()
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.principal = self.worker_a
        await self.call("ticket_claim", agent_name="worker-a", ticket_id=ticket_id)
        submitted = await self.call(
            "ticket_submit", agent_name="worker-a", ticket_id=ticket_id, summary="ready"
        )
        first = submitted.structured_content["ticket"]["review_offer"]["agent_id"]
        self.assertEqual(submitted.structured_content["dispatch_event"]["kind"], REVIEW_OFFERED)
        other_principal, other_name = (
            (self.reviewer_b, "reviewer-b") if first == reviewer_a
            else (self.reviewer_a, "reviewer-a")
        )
        self.principal = other_principal
        denied = await self.call(
            "ticket_review_claim", agent_name=other_name, ticket_id=ticket_id
        )
        self.assertEqual(denied.structured_content["error"]["code"], "review_not_offered")
        self.assertIn(first, {reviewer_a, reviewer_b})
        first_principal, first_name = (
            (self.reviewer_a, "reviewer-a") if first == reviewer_a
            else (self.reviewer_b, "reviewer-b")
        )
        self.principal = first_principal
        review_claimed = await self.call(
            "ticket_review_claim", agent_name=first_name, ticket_id=ticket_id
        )
        self.assertNotIn("review_offer", review_claimed.structured_content["ticket"])
        self.assertEqual(
            review_claimed.structured_content["ticket"]["dispatch_state"]["state"],
            "review_claimed",
        )
        rejected = await self.call(
            "ticket_review", agent_name=first_name, ticket_id=ticket_id,
            verdict="reject", review_notes="needs another pass",
            fix_instructions="adjust contract",
        )
        self.assertEqual(rejected.structured_content["ticket"]["status"], "open")
        self.principal = self.worker_a
        await self.call("ticket_claim", agent_name="worker-a", ticket_id=ticket_id)
        resubmitted = await self.call(
            "ticket_submit", agent_name="worker-a", ticket_id=ticket_id, summary="fixed"
        )
        self.assertNotEqual(
            resubmitted.structured_content["ticket"]["review_offer"]["agent_id"], first
        )

    async def test_unassignable_review_denies_claim_and_direct_verdict(self) -> None:
        submitter = await self.add_seat(
            self.reviewer_a, "submitter",
            {"tier_max": 3, "skills": ["rust"], "can_work": True},
        )
        low = await self.add_seat(
            self.reviewer_b, "reviewer-low",
            {"tier_max": 1, "skills": ["rust"], "can_work": False, "can_review": True},
            role="reviewer",
        )
        missing_principal = central.Principal(
            "PR-review-missing", "review-missing",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        await self.add_seat(
            missing_principal, "reviewer-missing",
            {"tier_max": 3, "skills": [], "can_work": False, "can_review": True},
            role="reviewer",
        )
        disabled_principal = central.Principal(
            "PR-review-disabled", "review-disabled",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        await self.add_seat(
            disabled_principal, "reviewer-disabled",
            {"tier_max": 3, "skills": ["rust"], "can_work": False, "can_review": False},
            role="reviewer",
        )
        excluded_principal = central.Principal(
            "PR-review-excluded", "review-excluded",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        excluded = await self.add_seat(
            excluded_principal, "reviewer-excluded",
            {"tier_max": 3, "skills": ["rust"], "can_work": False, "can_review": True},
            role="reviewer",
        )
        created = await self.create(
            tier=3, skills_required=["rust"], exclude_agents=[excluded],
            prefer_agents=[submitter],
        )
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.principal = self.reviewer_a
        await self.call("ticket_claim", agent_name="submitter", ticket_id=ticket_id)
        submitted = await self.call(
            "ticket_submit", agent_name="submitter", ticket_id=ticket_id, summary="ready"
        )
        self.assertEqual(
            submitted.structured_content["ticket"]["dispatch_state"],
            {"state": "unassignable", "kind": "review", "reason": "no_eligible_reviewer"},
        )
        review_attempts = (
            (self.reviewer_b, "reviewer-low"),
            (missing_principal, "reviewer-missing"),
            (disabled_principal, "reviewer-disabled"),
            (excluded_principal, "reviewer-excluded"),
        )
        denied_claim = None
        for review_principal, review_name in review_attempts:
            self.principal = review_principal
            denied_claim = await self.call(
                "ticket_review_claim", agent_name=review_name, ticket_id=ticket_id
            )
            self.assertEqual(
                denied_claim.structured_content["error"]["code"],
                "review_not_offered",
            )
        assert denied_claim is not None
        self.principal = self.reviewer_b
        denied_direct = await self.call(
            "ticket_review", agent_name="reviewer-low", ticket_id=ticket_id,
            verdict="approve", review_notes="must not apply",
        )
        self.assertFalse(denied_direct.structured_content["ok"])
        self.assertEqual(denied_direct.structured_content["error"]["code"], "review_not_offered")
        self.principal = self.reviewer_a
        with self.assertRaises(ToolError):
            await self.call(
                "ticket_review_claim", agent_name="submitter", ticket_id=ticket_id
            )
        self.principal = self.admin
        fetched = await self.call("ticket_get", ticket_id=ticket_id)
        ticket = fetched.structured_content["ticket"]
        self.assertEqual(ticket["status"], "submitted")
        self.assertNotIn("review_lease", ticket)
        self.assertNotIn("review_verdict", ticket)
        self.assertEqual(
            denied_claim.structured_content["error"]["reason"],
            "no_eligible_reviewer",
        )

    async def test_dispatch_projection_is_cross_seat_but_catchup_stays_scoped(
        self,
    ) -> None:
        dashboard_principal = central.Principal(
            "PR-dashboard", "dashboard", frozenset({"board:read", "board:write"})
        )
        await self.add_seat(
            dashboard_principal,
            "dashboard-seat",
            {"can_work": False, "can_review": False},
        )
        worker_a = await self.add_seat(
            self.worker_a, "worker-a", {"tier_max": 2, "can_work": True}
        )
        worker_b = await self.add_seat(
            self.worker_b, "worker-b", {"tier_max": 2, "can_work": True}
        )
        reviewer = await self.add_seat(
            self.reviewer_a,
            "reviewer-a",
            {"tier_max": 2, "can_work": False, "can_review": True},
            role="reviewer",
        )
        self.principal = self.admin
        await self.call(
            "board_dispatch_policy_set", agent_name="admin-agent", offer_ttl_s=1
        )
        with patch.object(central.time, "time", return_value=1000.0):
            created = await self.create(prefer_agents=[worker_a])
            ticket_id = created.structured_content["ticket"]["ticket_id"]
            updated = await self.call(
                "ticket_update",
                agent_name="admin-agent",
                ticket_id=ticket_id,
                exclude_agents=[worker_a],
            )
        self.assertEqual(
            updated.structured_content["ticket"]["work_offer"]["agent_id"], worker_b
        )
        with patch.object(central.time, "time", return_value=1002.0):
            await self.call("board_reap")

        with patch.object(central.time, "time", return_value=2000.0):
            review_target = await self.create(prefer_agents=[worker_a])
        review_ticket_id = review_target.structured_content["ticket"]["ticket_id"]
        self.principal = self.worker_a
        with patch.object(central.time, "time", return_value=2000.5):
            await self.call(
                "ticket_claim", agent_name="worker-a", ticket_id=review_ticket_id
            )
            submitted = await self.call(
                "ticket_submit",
                agent_name="worker-a",
                ticket_id=review_ticket_id,
                summary="ready",
            )
        self.assertEqual(
            submitted.structured_content["ticket"]["review_offer"]["agent_id"],
            reviewer,
        )

        self.principal = dashboard_principal
        projection = await self.call("board_dispatch_events", limit=100)
        events = projection.structured_content["events"]
        kinds = {event["kind"] for event in events}
        self.assertTrue(
            {TICKET_OFFERED, REVIEW_OFFERED, OFFER_REVOKED, OFFER_EXPIRED}
            <= kinds
        )
        offered_names = {
            event.get("offered_agent_name")
            for event in events
            if event.get("kind") in {TICKET_OFFERED, REVIEW_OFFERED}
        }
        self.assertNotIn("dashboard-seat", offered_names)
        self.assertTrue(
            {"worker-a", "worker-b", "reviewer-a"} <= offered_names
        )
        for event in events:
            self.assertNotIn("recipient_identities", event)
            self.assertNotIn("offered_agent_id", event)
            self.assertNotIn("payload_ref", event)
            self.assertNotIn("actor", event)

        scoped = await self.call(
            "board_catchup",
            agent_name="dashboard-seat",
            cursor=0,
            limit=1000,
            ack=False,
            touch=False,
            max_events=1000,
        )
        scoped_kinds = {
            event["kind"] for event in scoped.structured_content["events"]
        }
        self.assertFalse(scoped_kinds & central.DISPATCH_EVENT_KINDS)

        self.principal = central.Principal(
            "PR-outsider", "outsider", frozenset({"board:read"})
        )
        with self.assertRaises(ToolError):
            await self.call("board_dispatch_events")

    async def test_legacy_fleet_keeps_broadcast_claim_behavior(self) -> None:
        root = self.root / "legacy-data"
        mcp, _ = central.build_server("localhost", 8766, root)
        principal = central.Principal(
            "PR-legacy", "legacy", frozenset({"board:read", "board:write"})
        )
        central.current_principal = lambda: principal
        joined = await mcp.call_tool(
            "board_join", {"board_id": "legacy", "agent_name": "legacy-worker"}
        )
        created = await mcp.call_tool(
            "ticket_create",
            {"board_id": "legacy", "agent_name": "legacy-worker", "ticket_id": "TK-legacy", "title": "legacy"},
        )
        ticket = created.structured_content["ticket"]
        self.assertNotIn("work_offer", ticket)
        claimed = await mcp.call_tool(
            "ticket_claim",
            {"board_id": "legacy", "agent_name": "legacy-worker", "ticket_id": "TK-legacy"},
        )
        self.assertTrue(joined.structured_content["ok"])
        self.assertTrue(claimed.structured_content["ok"])

    def test_shared_dispatch_constants(self) -> None:
        self.assertEqual(
            DISPATCH_KINDS,
            frozenset({TICKET_OFFERED, OFFER_EXPIRED, OFFER_REVOKED, REVIEW_OFFERED}),
        )

    async def test_dashboard_viewer_and_probe_identities_are_never_offered(self) -> None:
        self.principal = self.admin
        await self.call("board_dispatch_policy_set", agent_name="admin-agent", offer_ttl_s=60)
        viewer = await self.add_seat(
            self.worker_a, "fleet-dashboard-viewer",
            {"can_work": False, "can_review": False},
        )
        self.principal = self.admin
        await self.call(
            "board_member_add", agent_name="admin-agent",
            principal_id=self.worker_b.principal_id, role="member",
        )
        self.principal = self.worker_b
        probe_res = await self.call("board_join", agent_name="probe-agent")
        probe = probe_res.structured_content["agent_id"]

        real_principal = central.Principal("PR-real-worker", "real-worker", frozenset({"board:read", "board:write"}))
        real = await self.add_seat(real_principal, "real-worker", {"tier_max": 2})

        created = await self.create()
        ticket = created.structured_content["ticket"]
        self.assertEqual(ticket["work_offer"]["agent_id"], real)
        self.assertNotIn(ticket["work_offer"]["agent_id"], {viewer, probe})

        self.principal = self.admin
        updated = await self.call(
            "ticket_update", agent_name="admin-agent", ticket_id=ticket["ticket_id"],
            exclude_agents=[real],
        )
        up_ticket = updated.structured_content["ticket"]
        self.assertEqual(up_ticket["dispatch_state"]["state"], "unassignable")
        self.assertNotIn("work_offer", up_ticket)

    async def test_startup_bridge_identity_on_codex_is_never_offered(self) -> None:
        self.principal = self.admin
        await self.call("board_dispatch_policy_set", agent_name="admin-agent", offer_ttl_s=60)
        startup = await self.add_seat(
            self.worker_a, "purser-codex-1",
            {"can_work": False, "can_review": False},
        )
        self.principal = self.worker_a
        session_res = await self.call(
            "board_join", agent_name="pursers-codex-1",
            capabilities={"tier_max": 2},
        )
        session_worker = session_res.structured_content["agent_id"]
        created = await self.create()
        ticket = created.structured_content["ticket"]
        self.assertEqual(ticket["work_offer"]["agent_id"], session_worker)
        self.assertNotEqual(ticket["work_offer"]["agent_id"], startup)

    async def test_rotation_and_per_ticket_skip(self) -> None:
        worker_1 = await self.add_seat(self.worker_a, "worker-1", {"tier_max": 2})
        worker_2 = await self.add_seat(self.worker_b, "worker-2", {"tier_max": 2})
        princ_c = central.Principal("PR-worker-c", "worker-c", frozenset({"board:read", "board:write"}))
        worker_3 = await self.add_seat(princ_c, "worker-3", {"tier_max": 2})
        self.principal = self.admin
        await self.call(
            "board_dispatch_policy_set", agent_name="admin-agent", offer_ttl_s=10
        )
        t1 = (await self.create()).structured_content["ticket"]
        t2 = (await self.create()).structured_content["ticket"]
        t3 = (await self.create()).structured_content["ticket"]
        assigned = {
            t1["work_offer"]["agent_id"],
            t2["work_offer"]["agent_id"],
            t3["work_offer"]["agent_id"],
        }
        self.assertEqual(assigned, {worker_1, worker_2, worker_3})

        first_holder = t1["work_offer"]["agent_id"]
        base_time = central.time.time()
        with patch.object(central.time, "time", return_value=base_time + 20):
            await self.call("board_reap")
        t1_fetched = (await self.call("ticket_get", ticket_id=t1["ticket_id"])).structured_content["ticket"]
        self.assertNotEqual(t1_fetched["work_offer"]["agent_id"], first_holder)
        second_holder = t1_fetched["work_offer"]["agent_id"]

        with patch.object(central.time, "time", return_value=base_time + 40):
            await self.call("board_reap")
        t1_fetched2 = (await self.call("ticket_get", ticket_id=t1["ticket_id"])).structured_content["ticket"]
        third_holder = t1_fetched2["work_offer"]["agent_id"]
        self.assertNotIn(third_holder, {first_holder, second_holder})

        with patch.object(central.time, "time", return_value=base_time + 60):
            await self.call("board_reap")
        t1_fetched3 = (await self.call("ticket_get", ticket_id=t1["ticket_id"])).structured_content["ticket"]
        self.assertEqual(t1_fetched3["dispatch_state"]["state"], "broadcast")
        self.assertIn(t1_fetched3["dispatch_state"]["reason"], {"no_candidates_remaining", "offer_limit_reached"})
        self.assertNotIn("work_offer", t1_fetched3)

    async def test_prompt_expiry_without_unrelated_mutations(self) -> None:
        await self.add_seat(self.worker_a, "worker-a", {"tier_max": 2})
        self.principal = self.admin
        await self.call(
            "board_dispatch_policy_set", agent_name="admin-agent", offer_ttl_s=1
        )
        with patch.object(central.time, "time", return_value=1000.0):
            created = await self.create()
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.assertIn("work_offer", created.structured_content["ticket"])

        self.principal = self.worker_a
        with patch.object(central.time, "time", return_value=1002.0):
            page = await self.call(
                "board_catchup", agent_name="worker-a", cursor=0, touch=False
            )
        event_kinds = [ev["kind"] for ev in page.structured_content.get("events", [])]
        self.assertIn(OFFER_EXPIRED, event_kinds)

        fetched = await self.call("ticket_get", ticket_id=ticket_id)
        self.assertEqual(fetched.structured_content["ticket"]["dispatch_state"]["state"], "broadcast")
        self.assertEqual(fetched.structured_content["ticket"]["dispatch_state"]["reason"], "no_candidates_remaining")

    async def test_immediate_broadcast_fallback_when_no_candidates_remain(self) -> None:
        await self.add_seat(self.worker_a, "only-worker", {"tier_max": 2})
        self.principal = self.admin
        await self.call("board_dispatch_policy_set", agent_name="admin-agent", offer_ttl_s=10)
        t = (await self.create()).structured_content["ticket"]
        self.assertIn("work_offer", t)
        base_time = central.time.time()
        with patch.object(central.time, "time", return_value=base_time + 20):
            await self.call("board_reap")
        fetched = (await self.call("ticket_get", ticket_id=t["ticket_id"])).structured_content["ticket"]
        self.assertEqual(fetched["dispatch_state"]["state"], "broadcast")
        self.assertEqual(fetched["dispatch_state"]["reason"], "no_candidates_remaining")
        self.assertEqual(fetched["work_offer_expirations"], 1)
        self.assertNotIn("work_offer", fetched)


if __name__ == "__main__":
    unittest.main()
