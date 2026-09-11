from __future__ import annotations

import hashlib
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
            frozenset(
                {"board:read", "board:write", "board:review", "board:coordinate"}
            ),
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
        self.reviewer_coordinator = central.Principal(
            "PR-reviewer-coordinator",
            "reviewer-coordinator-canonical",
            frozenset({"board:read", "board:coordinate"}),
        )
        self.outsider_coordinator = central.Principal(
            "PR-outsider-coordinator",
            "outsider-coordinator-canonical",
            frozenset({"board:read", "board:coordinate"}),
        )
        self.intake_joiner = central.Principal(
            "PR-intake",
            "intake-canonical",
            frozenset({"board:coordinate", "board:intake"}),
        )
        self.intake = central.Principal(
            "PR-intake",
            "intake-canonical",
            frozenset({"board:intake"}),
        )
        self.intake_runtime = central.Principal(
            "PR-intake",
            "intake-canonical",
            frozenset({"board:read", "board:coordinate", "board:intake"}),
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call("board_join", agent_name="admin-agent")
        for principal in (
            self.worker,
            self.other_worker,
            self.intake_joiner,
        ):
            await self.call(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principal.principal_id,
                role="member",
            )
        await self.call(
            "board_member_add",
            agent_name="admin-agent",
            principal_id=self.coordinator.principal_id,
            role="admin",
        )
        await self.call(
            "board_member_add",
            agent_name="admin-agent",
            principal_id=self.reviewer_coordinator.principal_id,
            role="reviewer",
        )
        self.principal = self.worker
        joined = await self.call("board_join", agent_name="worker-agent")
        self.worker_id = joined.structured_content["agent_id"]
        self.principal = self.coordinator
        joined = await self.call(
            "board_join", agent_name="coordinator-1", role="coordinator"
        )
        self.coordinator_id = joined.structured_content["agent_id"]
        self.principal = self.intake_joiner
        joined = await self.call(
            "board_join", agent_name="intake-coordinator", role="coordinator"
        )
        self.intake_id = joined.structured_content["agent_id"]

    async def join_other_worker(self) -> str:
        self.principal = self.other_worker
        joined = await self.call("board_join", agent_name="other-worker-agent")
        self.assertFalse(joined.is_error)
        return joined.structured_content["agent_id"]

    async def enable_work_dispatch(
        self,
        principal: central.Principal,
        agent_name: str,
        *,
        tier_max: int = 2,
    ) -> None:
        self.principal = principal
        result = await self.call(
            "agent_capabilities_set",
            agent_name=agent_name,
            capabilities={
                "tier_max": tier_max,
                "can_work": True,
                "can_review": False,
            },
        )
        self.assertFalse(result.is_error)

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

    async def test_coordinate_only_join_accepts_every_admitted_membership(self) -> None:
        admitted = (
            (self.admin, "admin"),
            (self.coordinator, "admin"),
            (self.reviewer_coordinator, "reviewer"),
        )
        for principal, membership_role in admitted:
            for tool_name in ("board_join", "board_onboard"):
                for seat_role in ("orchestrator", "coordinator"):
                    with self.subTest(
                        membership_role=membership_role,
                        tool_name=tool_name,
                        seat_role=seat_role,
                    ):
                        self.principal = principal
                        result = await self.call(
                            tool_name,
                            agent_name=(
                                f"{membership_role}-{tool_name}-{seat_role}"
                            ),
                            role=seat_role,
                        )
                        self.assertFalse(result.is_error)
                        self.assertEqual(
                            result.structured_content["role"], seat_role
                        )
                        self.assertEqual(
                            result.structured_content["membership_role"],
                            membership_role,
                        )

    async def test_coordinate_only_join_denies_nonmember_and_invite_token(self) -> None:
        self.principal = self.outsider_coordinator
        with self.assertRaisesRegex(ToolError, "board access denied"):
            await self.call(
                "board_join",
                agent_name="outsider-orchestrator",
                role="orchestrator",
            )

        self.principal = self.admin
        with self.assertRaisesRegex(
            ToolError, "coordinator authorization cannot change board membership"
        ):
            await self.call(
                "board_join",
                agent_name="admin-invite-orchestrator",
                role="orchestrator",
                invite_token="invite-cannot-be-consumed-by-coordinate-only-join",
            )

    async def test_admin_membership_can_run_narrow_coordinator_operations(self) -> None:
        ticket_id = await self.create_ticket("admin coordination target")
        self.principal = central.Principal(
            self.admin.principal_id,
            self.admin.canonical,
            frozenset({"board:read", "board:coordinate"}),
        )

        updated = await self.call(
            "ticket_update",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            tier=2,
        )
        policy = await self.call(
            "board_dispatch_policy_set",
            agent_name="admin-agent",
            offer_ttl_s=120,
        )

        assigned = await self.call(
            "ticket_assign",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            assigned_to_agent_id=self.worker_id,
            expected_status="open",
            expected_assigned_to_agent_id=None,
            coordinator_op_key="admin-assignment",
            reason="admin coordinate scope is authorized",
        )
        finding = await self.call(
            "board_state_update",
            agent_name="admin-agent",
            key="coordinator_findings",
            value='{"admin":"authorized"}',
        )
        digest = await self.call(
            "memory_write",
            agent_name="admin-agent",
            title="Coordinator daily digest 2030-01-09",
            content="admin coordinate scope is authorized",
            scope="project",
            memory_type="checkpoint",
            tags=["coordinator", "digest", "daily"],
        )

        self.principal = self.intake_joiner
        with self.assertRaisesRegex(ToolError, "board role not authorized"):
            await self.call(
                "board_dispatch_policy_set",
                agent_name="intake-coordinator",
                offer_ttl_s=180,
            )

        self.principal = central.Principal(
            self.admin.principal_id,
            self.admin.canonical,
            frozenset({"board:read", "board:intake"}),
        )
        intake_ticket = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            ticket_id="TK-admin-intake",
            title="Admin intake membership",
            description="Admin membership may use a scoped intake credential.",
            target_url="pursers/tests",
            scope="interactive-no-send",
            required_fields=["test_output"],
            unassigned=True,
            coordinator_op_key="admin-intake-create",
        )

        for result in (
            updated,
            policy,
            assigned,
            finding,
            digest,
            intake_ticket,
        ):
            self.assertFalse(result.is_error)

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
        self.assertEqual(second.structured_content["dispatch_events"], [])
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

    async def test_assignment_accepts_null_to_clear_existing_pin(self) -> None:
        ticket_id = await self.create_ticket("clear assignment pin")
        self.principal = self.coordinator
        await self.call(
            "ticket_assign",
            agent_name="coordinator-1",
            ticket_id=ticket_id,
            assigned_to_agent_id=self.worker_id,
            expected_status="open",
            expected_assigned_to_agent_id=None,
            coordinator_op_key="coord-op-assignment-set",
            reason="set a temporary pin",
        )

        cleared = await self.call(
            "ticket_assign",
            agent_name="coordinator-1",
            ticket_id=ticket_id,
            assigned_to_agent_id=None,
            expected_status="open",
            expected_assigned_to_agent_id=self.worker_id,
            coordinator_op_key="coord-op-assignment-clear",
            reason="clear the temporary pin",
        )

        self.assertFalse(cleared.is_error)
        ticket = cleared.structured_content["ticket"]
        self.assertNotIn("assigned_to", ticket)
        self.assertNotIn("assigned_to_agent_id", ticket)
        self.assertNotIn("assigned_to_kind", ticket)
        self.assertIsNone(
            ticket["coordinator_assignment"]["assigned_to_agent_id"]
        )
        self.assertEqual(
            ticket["coordinator_assignment"]["previous_assigned_to_agent_id"],
            self.worker_id,
        )
        event = cleared.structured_content["event"]
        self.assertEqual(event["recipient_identities"], [self.worker_id])
        self.assertIsNone(event["assigned_to_agent_id"])
        self.assertEqual(event["previous_assigned_to_agent_id"], self.worker_id)

    async def test_assignment_revokes_old_offer_and_redispatches_to_target(
        self,
    ) -> None:
        other_worker_id = await self.join_other_worker()
        await self.enable_work_dispatch(self.worker, "worker-agent")
        await self.enable_work_dispatch(
            self.other_worker, "other-worker-agent"
        )
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="atomic reassignment",
            description="revoke the old offer and wake the new target",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
            prefer_agents=[self.worker_id],
        )
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.assertEqual(
            created.structured_content["ticket"]["work_offer"]["agent_id"],
            self.worker_id,
        )

        def add_stale_review_offer(document) -> None:
            ticket = document["tickets"][ticket_id]
            review_offer = dict(ticket["work_offer"])
            review_offer["kind"] = "review"
            ticket["review_offer"] = review_offer

        self.service.mutate(
            "pursers", add_stale_review_offer, require_generation=False
        )
        before_seq = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]

        self.principal = self.coordinator
        assigned = await self.call(
            "ticket_assign",
            agent_name="coordinator-1",
            ticket_id=ticket_id,
            assigned_to_agent_id=other_worker_id,
            expected_status="open",
            expected_assigned_to_agent_id=None,
            coordinator_op_key="atomic-reassignment",
            reason="move the unclaimed ticket to the eligible target",
        )

        self.assertFalse(assigned.is_error)
        ticket = assigned.structured_content["ticket"]
        self.assertEqual(ticket["assigned_to_agent_id"], other_worker_id)
        self.assertEqual(ticket["work_offer"]["agent_id"], other_worker_id)
        self.assertNotIn("review_offer", ticket)
        self.assertEqual(
            [
                event["kind"]
                for event in assigned.structured_content["dispatch_events"]
            ],
            [
                central.OFFER_REVOKED,
                central.OFFER_REVOKED,
                central.TICKET_OFFERED,
            ],
        )
        persisted = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(persisted["work_offer"]["agent_id"], other_worker_id)
        self.assertEqual(
            persisted["dispatch_state"]["agent_id"], other_worker_id
        )
        restarted_mcp, restarted_service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.mcp = restarted_mcp
        self.service = restarted_service
        reloaded = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(reloaded["work_offer"]["agent_id"], other_worker_id)
        self.assertEqual(
            reloaded["dispatch_state"]["agent_id"], other_worker_id
        )

        self.principal = self.worker
        old_catchup = await self.call(
            "board_catchup",
            agent_name="worker-agent",
            cursor=before_seq,
            ack=False,
        )
        self.assertIn(
            central.OFFER_REVOKED,
            [event["kind"] for event in old_catchup.structured_content["events"]],
        )
        with self.assertRaisesRegex(
            ToolError, "ticket is not offered to this seat"
        ):
            await self.call(
                "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
            )

        self.principal = self.other_worker
        target_catchup = await self.call(
            "board_catchup",
            agent_name="other-worker-agent",
            cursor=before_seq,
            ack=False,
        )
        target_kinds = [
            event["kind"] for event in target_catchup.structured_content["events"]
        ]
        self.assertIn(central.TICKET_OFFERED, target_kinds)
        self.assertIn("coordinator_assignment", target_kinds)
        claimed = await self.call(
            "ticket_claim", agent_name="other-worker-agent", ticket_id=ticket_id
        )
        self.assertFalse(claimed.is_error)

    async def test_park_assign_unpark_rebuilds_consistent_offer(self) -> None:
        other_worker_id = await self.join_other_worker()
        await self.enable_work_dispatch(self.worker, "worker-agent")
        await self.enable_work_dispatch(
            self.other_worker, "other-worker-agent"
        )
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="released recovery sequence",
            description="withdraw, assign while parked, and reoffer once",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
            prefer_agents=[self.worker_id],
        )
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.assertEqual(
            created.structured_content["ticket"]["work_offer"]["agent_id"],
            self.worker_id,
        )

        parked = await self.call(
            "ticket_update",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            parked=True,
        )
        self.assertTrue(parked.structured_content["ticket"]["parked"])
        self.assertNotIn("work_offer", parked.structured_content["ticket"])
        self.assertEqual(
            parked.structured_content["dispatch_events"][0]["kind"],
            central.OFFER_REVOKED,
        )

        self.principal = self.coordinator
        assigned = await self.call(
            "ticket_assign",
            agent_name="coordinator-1",
            ticket_id=ticket_id,
            assigned_to_agent_id=other_worker_id,
            expected_status="open",
            expected_assigned_to_agent_id=None,
            coordinator_op_key="released-park-assign-unpark",
            reason="rebuild one offer for the exact eligible target",
        )
        self.assertEqual(
            assigned.structured_content["ticket"]["assigned_to_agent_id"],
            other_worker_id,
        )
        self.assertNotIn("work_offer", assigned.structured_content["ticket"])

        unparked = await self.call(
            "ticket_update",
            agent_name="coordinator-1",
            ticket_id=ticket_id,
            parked=False,
        )
        recovered = unparked.structured_content["ticket"]
        self.assertFalse(recovered["parked"])
        self.assertEqual(recovered["work_offer"]["agent_id"], other_worker_id)
        self.assertEqual(recovered["dispatch_state"]["agent_id"], other_worker_id)
        self.assertEqual(
            unparked.structured_content["dispatch_events"][0]["kind"],
            central.TICKET_OFFERED,
        )

        self.principal = self.worker
        with self.assertRaisesRegex(
            ToolError, "ticket is not offered to this seat"
        ):
            await self.call(
                "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
            )
        self.principal = self.other_worker
        claimed = await self.call(
            "ticket_claim",
            agent_name="other-worker-agent",
            ticket_id=ticket_id,
        )
        self.assertEqual(
            claimed.structured_content["ticket"]["claimed_by_agent_id"],
            other_worker_id,
        )

    async def test_assignment_noop_preserves_offer_and_clear_redispatches(
        self,
    ) -> None:
        other_worker_id = await self.join_other_worker()
        await self.enable_work_dispatch(self.worker, "worker-agent")
        await self.enable_work_dispatch(
            self.other_worker, "other-worker-agent"
        )
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="safe repeated assignment",
            description="preserve an active offer on assignment no-op",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
            prefer_agents=[self.worker_id],
        )
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.principal = self.coordinator
        assigned = await self.call(
            "ticket_assign",
            agent_name="coordinator-1",
            ticket_id=ticket_id,
            assigned_to_agent_id=other_worker_id,
            expected_status="open",
            expected_assigned_to_agent_id=None,
            coordinator_op_key="assignment-set-target",
            reason="set the target",
        )
        offer = assigned.structured_content["ticket"]["work_offer"]

        repeated = await self.call(
            "ticket_assign",
            agent_name="coordinator-1",
            ticket_id=ticket_id,
            assigned_to_agent_id=other_worker_id,
            expected_status="open",
            expected_assigned_to_agent_id=other_worker_id,
            coordinator_op_key="assignment-repeat-target",
            reason="repeat the same target safely",
        )
        self.assertEqual(repeated.structured_content["dispatch_events"], [])
        self.assertEqual(
            repeated.structured_content["ticket"]["work_offer"], offer
        )

        cleared = await self.call(
            "ticket_assign",
            agent_name="coordinator-1",
            ticket_id=ticket_id,
            assigned_to_agent_id=None,
            expected_status="open",
            expected_assigned_to_agent_id=other_worker_id,
            coordinator_op_key="assignment-clear-target",
            reason="return the ticket to soft dispatch",
        )
        cleared_ticket = cleared.structured_content["ticket"]
        self.assertNotIn("assigned_to_agent_id", cleared_ticket)
        self.assertEqual(
            cleared_ticket["work_offer"]["agent_id"], self.worker_id
        )
        self.assertEqual(
            [
                event["kind"]
                for event in cleared.structured_content["dispatch_events"]
            ],
            [central.OFFER_REVOKED, central.TICKET_OFFERED],
        )

    async def test_assignment_to_ineligible_target_reports_unassignable(
        self,
    ) -> None:
        other_worker_id = await self.join_other_worker()
        await self.enable_work_dispatch(self.worker, "worker-agent")
        await self.enable_work_dispatch(
            self.other_worker, "other-worker-agent", tier_max=1
        )
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="ineligible reassignment",
            description="do not fabricate a target offer",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
            prefer_agents=[self.worker_id],
            tier=2,
        )
        ticket_id = created.structured_content["ticket"]["ticket_id"]

        self.principal = self.coordinator
        assigned = await self.call(
            "ticket_assign",
            agent_name="coordinator-1",
            ticket_id=ticket_id,
            assigned_to_agent_id=other_worker_id,
            expected_status="open",
            expected_assigned_to_agent_id=None,
            coordinator_op_key="assignment-ineligible-target",
            reason="exercise target eligibility",
        )

        ticket = assigned.structured_content["ticket"]
        self.assertNotIn("work_offer", ticket)
        self.assertEqual(ticket["dispatch_state"]["state"], "unassignable")
        self.assertEqual(
            ticket["dispatch_state"]["reason"], "pinned_seat_unavailable"
        )
        self.assertEqual(
            [
                event["kind"]
                for event in assigned.structured_content["dispatch_events"]
            ],
            [central.OFFER_REVOKED, "dispatch_unassignable"],
        )
        persisted = self.service.load("pursers")["tickets"][ticket_id]
        self.assertNotIn("work_offer", persisted)

    async def test_assignment_requires_admin_membership(self) -> None:
        ticket_id = await self.create_ticket("admin-only assignment")
        for principal, agent_name in (
            (self.intake_joiner, "intake-coordinator"),
            (self.reviewer_coordinator, "reviewer-coordinator"),
        ):
            with self.subTest(principal=principal.principal_id):
                self.principal = principal
                with self.assertRaisesRegex(ToolError, "board role not authorized"):
                    await self.call(
                        "ticket_assign",
                        agent_name=agent_name,
                        ticket_id=ticket_id,
                        assigned_to_agent_id=self.worker_id,
                        expected_status="open",
                        coordinator_op_key=f"denied-{agent_name}",
                        reason="admin membership is required",
                    )
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

        claimed = await self.call(
            "ticket_claim",
            agent_name="coordinator-1",
            ticket_id=ticket_id,
        )
        self.assertTrue(claimed.structured_content["ok"])
        self.assertEqual(
            claimed.structured_content["ticket"]["claimed_by"],
            "coordinator-1",
        )

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

    async def test_intake_scope_creates_origin_journaled_unassigned_ticket(self) -> None:
        self.principal = self.intake
        created = await self.call(
            "ticket_create",
            agent_name="intake-coordinator",
            ticket_id="TK-intake-allowed",
            title="Update the operator guide",
            description="Structured coordinator intake.",
            target_url="pursers/docs",
            scope="interactive-no-send",
            required_fields=["commit_hash", "test_output"],
            tags=["coordinator-intake"],
            unassigned=True,
            coordinator_op_key="coord-intake-allowed",
        )

        self.assertFalse(created.is_error)
        ticket = created.structured_content["ticket"]
        self.assertEqual(ticket["origin"], "coordinator-intake")
        self.assertEqual(ticket["coordinator_op_key"], "coord-intake-allowed")
        self.assertIsNone(ticket["assigned_to_agent_id"])
        event = created.structured_content["event"]
        self.assertEqual(event["origin"], "coordinator-intake")
        self.assertEqual(event["coordinator_op_key"], "coord-intake-allowed")

    async def test_intake_scope_state_keys_and_compare_and_set(self) -> None:
        self.principal = self.admin
        seeded = await self.call(
            "board_state_update",
            agent_name="admin-agent",
            key="coordinator_intake",
            value='[{"id":"ask-1"}]',
        )
        self.assertFalse(seeded.is_error)

        self.principal = self.intake
        read = await self.call("board_state_get", key="coordinator_intake")
        self.assertEqual(read.structured_content["state"]["value"], '[{"id":"ask-1"}]')
        expected_sha256 = hashlib.sha256(b'[{"id":"ask-1"}]').hexdigest()
        updated = await self.call(
            "board_state_update",
            agent_name="intake-coordinator",
            key="coordinator_intake",
            value="[]",
            expected_sha256=expected_sha256,
        )
        self.assertFalse(updated.is_error)
        finding = await self.call(
            "board_state_update",
            agent_name="intake-coordinator",
            key="coordinator_findings",
            value='{"findings":[]}',
        )
        self.assertFalse(finding.is_error)
        with self.assertRaisesRegex(ToolError, "state precondition failed"):
            await self.call(
                "board_state_update",
                agent_name="intake-coordinator",
                key="coordinator_intake",
                value='[{"id":"lost"}]',
                expected_sha256=expected_sha256,
            )
        with self.assertRaisesRegex(ToolError, "reads only"):
            await self.call("board_state_get", key="project_registry")
        with self.assertRaisesRegex(ToolError, "reads only"):
            await self.call("board_state_get")

    async def test_intake_scope_server_rate_limit(self) -> None:
        self.service.mutate(
            "pursers",
            lambda document: document["config"].update(
                {"intake_rate_limit_per_hour": 2}
            ),
        )
        self.principal = self.intake
        for index in range(2):
            created = await self.call(
                "ticket_create",
                agent_name="intake-coordinator",
                ticket_id=f"TK-intake-rate-{index}",
                title=f"Intake {index}",
                description="Rate limit probe.",
                target_url="pursers/tests",
                scope="interactive-no-send",
                required_fields=["test_output"],
                unassigned=True,
                coordinator_op_key=f"coord-intake-rate-{index}",
            )
            self.assertFalse(created.is_error)
        with self.assertRaisesRegex(ToolError, "ticket already exists"):
            await self.call(
                "ticket_create",
                agent_name="intake-coordinator",
                ticket_id="TK-intake-rate-0",
                title="Intake 0",
                description="Rate limit probe.",
                target_url="pursers/tests",
                scope="interactive-no-send",
                required_fields=["test_output"],
                unassigned=True,
                coordinator_op_key="coord-intake-rate-0",
            )
        with self.assertRaisesRegex(ToolError, "hourly ticket creation limit"):
            await self.call(
                "ticket_create",
                agent_name="intake-coordinator",
                ticket_id="TK-intake-rate-denied",
                title="Intake denied",
                description="Rate limit probe.",
                target_url="pursers/tests",
                scope="interactive-no-send",
                required_fields=["test_output"],
                unassigned=True,
                coordinator_op_key="coord-intake-rate-denied",
            )

    async def test_intake_scope_denied_call_matrix(self) -> None:
        ticket_id = await self.create_ticket("intake denial target")
        self.principal = self.intake
        forbidden_calls = (
            ("ticket_claim", {"agent_name": "intake-coordinator", "ticket_id": ticket_id}),
            ("ticket_submit", {"agent_name": "intake-coordinator", "ticket_id": ticket_id}),
            (
                "ticket_review",
                {
                    "agent_name": "intake-coordinator",
                    "ticket_id": ticket_id,
                    "verdict": "approve",
                },
            ),
            ("ticket_cancel", {"agent_name": "intake-coordinator", "ticket_id": ticket_id}),
            (
                "ticket_assign",
                {
                    "agent_name": "intake-coordinator",
                    "ticket_id": ticket_id,
                    "assigned_to_agent_id": self.worker_id,
                    "expected_status": "open",
                    "coordinator_op_key": "coord-intake-no-assign",
                    "reason": "must be denied",
                },
            ),
            (
                "board_member_add",
                {
                    "agent_name": "intake-coordinator",
                    "principal_id": "PR-forbidden",
                    "role": "member",
                },
            ),
            (
                "memory_write",
                {
                    "agent_name": "intake-coordinator",
                    "title": "forbidden",
                    "content": "forbidden",
                    "scope": "project",
                },
            ),
        )
        for name, arguments in forbidden_calls:
            with self.subTest(name=name), self.assertRaises(ToolError):
                await self.call(name, **arguments)

        with self.assertRaisesRegex(ToolError, "requires coordinator_op_key"):
            await self.call(
                "ticket_create",
                agent_name="intake-coordinator",
                ticket_id="TK-intake-no-key",
                title="forbidden",
                description="missing op key",
                target_url="pursers",
                scope="interactive-no-send",
                required_fields=["test_output"],
                unassigned=True,
            )
        with self.assertRaisesRegex(ToolError, "only unassigned"):
            await self.call(
                "ticket_create",
                agent_name="intake-coordinator",
                ticket_id="TK-intake-assigned",
                title="forbidden",
                description="assignment escalation",
                target_url="pursers",
                scope="interactive-no-send",
                required_fields=["test_output"],
                coordinator_op_key="coord-intake-assigned",
            )
        with self.assertRaisesRegex(ToolError, "permits only"):
            await self.call(
                "board_state_update",
                agent_name="intake-coordinator",
                key="project_registry",
                value="{}",
            )

    async def test_runtime_scope_create_publish_and_cas_drain(self) -> None:
        queue = '[{"id":"ask-live","text":"Update docs"}]'
        self.principal = self.admin
        await self.call(
            "board_state_update",
            agent_name="admin-agent",
            key="coordinator_intake",
            value=queue,
        )

        self.principal = self.intake_runtime
        created = await self.call(
            "ticket_create",
            agent_name="intake-coordinator",
            ticket_id="TK-intake-live",
            title="Update docs",
            description="Structured coordinator intake.",
            target_url="pursers/docs",
            scope="interactive-no-send",
            required_fields=["commit_hash", "test_output"],
            unassigned=True,
            coordinator_op_key="coord-intake-live",
        )
        published = await self.call(
            "board_state_update",
            agent_name="intake-coordinator",
            key="coordinator_findings",
            value='{"findings":[{"ask_id":"ask-live","kind":"intake-created"}]}',
        )
        observed = await self.call("board_state_get", key="coordinator_intake")
        drained = await self.call(
            "board_state_update",
            agent_name="intake-coordinator",
            key="coordinator_intake",
            value="[]",
            expected_sha256=hashlib.sha256(queue.encode()).hexdigest(),
        )

        self.assertFalse(created.is_error)
        self.assertFalse(published.is_error)
        self.assertEqual(observed.structured_content["state"]["value"], queue)
        self.assertFalse(drained.is_error)
        document = self.service.load("pursers")
        self.assertEqual(document["state"]["coordinator_intake"]["value"], "[]")
        self.assertEqual(
            document["tickets"]["TK-intake-live"]["origin"],
            "coordinator-intake",
        )

    async def test_ticket_annotations_enforce_roles_and_preserve_workflow(self) -> None:
        ticket_id = await self.create_ticket("annotated coordinator target")
        self.principal = self.worker
        for kind in ("note", "evidence", "authorization", "decision"):
            with self.subTest(worker_kind=kind), self.assertRaisesRegex(
                ToolError, "annotation requires"
            ):
                await self.call(
                    "ticket_annotate", agent_name="worker-agent",
                    ticket_id=ticket_id, text="worker text", kind=kind,
                )

        reviewer = central.Principal(
            "PR-annotation-reviewer", "annotation-reviewer-canonical",
            frozenset({"board:read", "board:review"}),
        )
        self.principal = self.admin
        await self.call(
            "board_member_add", agent_name="admin-agent",
            principal_id=reviewer.principal_id, role="reviewer",
        )
        self.principal = reviewer
        joined = await self.call("board_join", agent_name="annotation-reviewer")
        reviewer_id = joined.structured_content["agent_id"]
        note = await self.call(
            "ticket_annotate", agent_name="annotation-reviewer",
            ticket_id=ticket_id, text="reviewer note",
        )
        self.assertFalse(note.is_error)
        for kind in ("evidence", "authorization", "decision"):
            with self.subTest(reviewer_kind=kind), self.assertRaisesRegex(
                ToolError, "only kind=note"
            ):
                await self.call(
                    "ticket_annotate", agent_name="annotation-reviewer",
                    ticket_id=ticket_id, text="reviewer escalation", kind=kind,
                )

        self.principal = self.worker
        claimed = await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        self.assertFalse(claimed.is_error)
        before = self.service.load("pursers")["tickets"][ticket_id]
        workflow = {
            key: before.get(key)
            for key in ("status", "notes", "claim_lease", "review_lease", "submission_generation")
        }
        self.principal = self.admin
        results = []
        for kind in ("note", "evidence", "authorization", "decision"):
            results.append(await self.call(
                "ticket_annotate", agent_name="admin-agent",
                ticket_id=ticket_id, text=f"admin {kind}", kind=kind,
            ))
        self.principal = self.intake_joiner
        coordinated = await self.call(
            "ticket_annotate", agent_name="intake-coordinator",
            ticket_id=ticket_id, text="coordinator evidence", kind="evidence",
        )
        for result in [*results, coordinated]:
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["event"]["kind"], "ticket_annotated")
            annotation = result.structured_content["annotation"]
            self.assertRegex(annotation["annotation_id"], r"^AN-\d{12}$")
            self.assertTrue(annotation["by"]["principal_id"])
            self.assertTrue(annotation["by"]["agent_id"])
            self.assertTrue(annotation["by"]["agent_name"])
        recipients = results[-1].structured_content["event"]["recipient_identities"]
        self.assertIn(self.worker_id, recipients)
        self.assertIn(reviewer_id, recipients)
        after = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(workflow, {key: after.get(key) for key in workflow})

    async def test_ticket_annotations_are_scrubbed_capped_and_projected(self) -> None:
        ticket_id = await self.create_ticket("bounded annotations")
        self.service.mutate(
            "pursers", lambda document: document["config"].pop("scrub_profile", None)
        )
        self.principal = self.admin
        with self.assertRaisesRegex(ToolError, "email"):
            await self.call(
                "ticket_annotate", agent_name="admin-agent", ticket_id=ticket_id,
                text="operator@example.invalid", kind="evidence",
            )
        self.assertNotIn(
            "annotations", self.service.load("pursers")["tickets"][ticket_id]
        )
        for index in range(51):
            result = await self.call(
                "ticket_annotate", agent_name="admin-agent", ticket_id=ticket_id,
                text=f"bounded annotation {index}",
            )
            self.assertFalse(result.is_error)
        fetched = await self.call("ticket_get", ticket_id=ticket_id)
        ticket = fetched.structured_content["ticket"]
        self.assertEqual(ticket["annotation_count"], 51)
        self.assertEqual(ticket["annotations_omitted_count"], 1)
        self.assertEqual(len(ticket["annotations"]), 50)
        self.assertEqual(ticket["annotations"][0]["text"], "bounded annotation 1")
        listed = await self.call("ticket_list")
        summary = next(
            item for item in listed.structured_content["tickets"]
            if item["ticket_id"] == ticket_id
        )
        self.assertEqual(summary["annotation_count"], 51)
        self.assertNotIn("annotations", summary)


if __name__ == "__main__":
    unittest.main()
