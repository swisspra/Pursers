from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]

import central  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


class TicketProgressTests(unittest.IsolatedAsyncioTestCase):
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
        self.worker = central.Principal(
            "PR-worker", "worker", frozenset({"board:read", "board:write"})
        )
        self.other = central.Principal(
            "PR-other", "worker", frozenset({"board:read", "board:write"})
        )
        self.reviewer = central.Principal(
            "PR-reviewer",
            "reviewer",
            frozenset({"board:read", "board:review"}),
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call("board_join", agent_name="admin-agent")
        for principal, role in (
            (self.worker, "member"),
            (self.other, "member"),
            (self.reviewer, "reviewer"),
        ):
            await self.call(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principal.principal_id,
                role=role,
            )
        self.principal = self.worker
        await self.call("board_join", agent_name="worker-agent")
        self.principal = self.other
        await self.call("board_join", agent_name="other-agent")
        self.principal = self.reviewer
        await self.call(
            "board_join", agent_name="reviewer-agent", role="reviewer"
        )

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name, {"board_id": "pursers", **arguments}
        )

    def configure(self, *, enabled: bool, history_limit: int = 50) -> None:
        def mutate(document: dict) -> None:
            document["config"]["ticket_progress_v1"] = enabled
            document["config"]["inline_history_limit"] = history_limit

        self.service.mutate("pursers", mutate)

    async def create_and_claim(self) -> str:
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="progress target",
            description="exercise explicit worker progress",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["observations"],
        )
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.principal = self.worker
        await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        return ticket_id

    async def progress(
        self,
        ticket_id: str,
        *,
        low: int = 35,
        high: int = 55,
        confidence: str = "medium",
        evidence: str = "Parser is complete; integration tests remain.",
        revision: int = 0,
        agent_name: str = "worker-agent",
    ):
        return await self.call(
            "ticket_progress_update",
            agent_name=agent_name,
            ticket_id=ticket_id,
            low_percent=low,
            high_percent=high,
            confidence=confidence,
            evidence=evidence,
            expected_revision=revision,
        )

    async def test_feature_flag_defaults_off_and_preserves_stored_records(self) -> None:
        ticket_id = await self.create_and_claim()
        with self.assertRaisesRegex(ToolError, "disabled"):
            await self.progress(ticket_id)

        self.configure(enabled=True)
        await self.progress(ticket_id)
        self.configure(enabled=False)
        fetched = await self.call("ticket_get", ticket_id=ticket_id, view="full")
        self.assertEqual(fetched.structured_content["ticket"]["progress"]["revision"], 1)
        with self.assertRaisesRegex(ToolError, "disabled"):
            await self.progress(ticket_id, revision=1, low=50, high=60)

    async def test_schema_auth_revision_history_and_journal_boundary(self) -> None:
        self.configure(enabled=True, history_limit=2)
        ticket_id = await self.create_and_claim()
        raw = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(raw["work_attempt"], 1)
        workflow_updated_at = raw["updated_at"]

        receipt = await self.progress(
            ticket_id, low=0, high=0, confidence="high", evidence="Setup complete."
        )
        self.assertEqual(receipt.structured_content["attempt"], 1)
        self.assertEqual(receipt.structured_content["revision"], 1)
        raw = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(raw["updated_at"], workflow_updated_at)
        self.assertEqual(raw["progress"]["low_percent"], 0)

        events = self.service.journal.read_after("pursers", 0, 1000)["events"]
        update_event = next(
            event for event in events if event["kind"] == "ticket_progress_updated"
        )
        self.assertNotIn("evidence", update_event)
        self.assertEqual(update_event["progress_ref"], f"board://pursers/ticket/{ticket_id}#progress-1-1")

        conflict = await self.progress(
            ticket_id, low=1, high=1, evidence="One unit complete.", revision=0
        )
        self.assertFalse(conflict.structured_content["ok"])
        self.assertEqual(conflict.structured_content["current_revision"], 1)
        with self.assertRaisesRegex(ToolError, "semantic duplicate"):
            await self.progress(
                ticket_id,
                low=0,
                high=0,
                confidence="high",
                evidence="Setup complete.",
                revision=1,
            )

        await self.progress(
            ticket_id, low=98, high=99, evidence="Focused tests remain.", revision=1
        )
        await self.progress(
            ticket_id, low=50, high=50, evidence="Regression suite remains.", revision=2
        )
        await self.progress(
            ticket_id, low=99, high=99, evidence="Submission remains.", revision=3
        )
        raw = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(raw["progress"]["revision"], 4)
        self.assertEqual(len(raw["progress_history"]), 2)
        self.assertEqual(raw["progress_history_omitted_count"], 1)
        self.assertTrue(
            all(item["end_reason"] == "superseded" for item in raw["progress_history"])
        )

        self.principal = self.other
        with self.assertRaisesRegex(ToolError, "exact current work holder"):
            await self.progress(ticket_id, agent_name="other-agent", revision=4)

        self.principal = self.worker
        invalid = (
            {"low": True},
            {"low": 1.5},
            {"low": 100},
            {"low": 60, "high": 59},
            {"confidence": "certain"},
            {"evidence": "   "},
            {"evidence": "x" * 281},
            {"revision": True},
        )
        for values in invalid:
            arguments = {"revision": 4, **values}
            with self.assertRaises(ToolError):
                await self.progress(ticket_id, **arguments)

    async def test_lease_operations_do_not_mutate_progress_and_freshness_is_derived(self) -> None:
        self.configure(enabled=True)
        ticket_id = await self.create_and_claim()
        claimed = self.service.load("pursers")["tickets"][ticket_id]
        claimed_at = datetime.fromisoformat(claimed["claimed_at"]).timestamp()
        with patch.object(central.time, "time", return_value=claimed_at + 10):
            await self.progress(ticket_id, low=25, high=50)
        before = self.service.load("pursers")["tickets"][ticket_id]["progress"]
        fresh_until = datetime.fromisoformat(before["fresh_until"]).timestamp()

        self.principal = self.admin
        with patch.object(central.time, "time", return_value=claimed_at + 100):
            await self.call(
                "board_claim_ttl_set",
                agent_name="admin-agent",
                claim_ttl_s=3600,
            )
        self.principal = self.worker
        with patch.object(central.time, "time", return_value=claimed_at + 101):
            await self.call(
                "lease_renew", agent_name="worker-agent", ticket_id=ticket_id
            )
        after = self.service.load("pursers")["tickets"][ticket_id]["progress"]
        self.assertEqual(after, before)

        with patch.object(central.time, "time", return_value=fresh_until - 0.01):
            fetched = await self.call("ticket_get", ticket_id=ticket_id)
        self.assertEqual(fetched.structured_content["ticket"]["progress_freshness"], "fresh")
        with patch.object(central.time, "time", return_value=fresh_until):
            fetched = await self.call("ticket_get", ticket_id=ticket_id)
        self.assertEqual(fetched.structured_content["ticket"]["progress_freshness"], "stale")

    async def test_pre_submission_states_and_exact_holder_boundary(self) -> None:
        self.configure(enabled=True)
        ticket_id = await self.create_and_claim()

        await self.progress(ticket_id, low=10, high=20, revision=0)
        for revision, status in enumerate(
            ("in_progress", "creating_report"), start=1
        ):
            self.service.mutate(
                "pursers",
                lambda document, state=status: document["tickets"][ticket_id].__setitem__(
                    "status", state
                ),
            )
            await self.progress(
                ticket_id,
                low=10 + revision,
                high=20 + revision,
                revision=revision,
            )

        def corrupt_holder(document: dict) -> None:
            document["tickets"][ticket_id]["claimed_by_principal_id"] = "PR-other"

        self.service.mutate("pursers", corrupt_holder)
        with self.assertRaisesRegex(ToolError, "exact current work holder"):
            await self.progress(ticket_id, revision=3)

        def restore_holder(document: dict) -> None:
            ticket = document["tickets"][ticket_id]
            ticket["claimed_by_principal_id"] = "PR-worker"
            ticket["status"] = "claimed"

        self.service.mutate("pursers", restore_holder)
        for status in ("open", "submitted", "reviewing", "closed", "canceled"):
            self.service.mutate(
                "pursers",
                lambda document, state=status: document["tickets"][ticket_id].__setitem__(
                    "status", state
                ),
            )
            with self.assertRaisesRegex(ToolError, f"ticket is {status}"):
                await self.progress(ticket_id, revision=3)

        self.service.mutate("pursers", restore_holder)
        with self.assertRaisesRegex(ToolError, "bearer_token"):
            await self.progress(
                ticket_id,
                evidence="Authorization: Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                revision=3,
            )

    async def test_unclaim_submit_reject_expiry_and_cancel_reset_attempts(self) -> None:
        self.configure(enabled=True)
        ticket_id = await self.create_and_claim()
        await self.progress(ticket_id)
        await self.call(
            "ticket_unclaim", agent_name="worker-agent", ticket_id=ticket_id
        )
        raw = self.service.load("pursers")["tickets"][ticket_id]
        self.assertNotIn("progress", raw)
        self.assertEqual(raw["progress_history"][-1]["end_reason"], "unclaimed")

        await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        self.assertEqual(
            self.service.load("pursers")["tickets"][ticket_id]["work_attempt"], 2
        )
        await self.progress(ticket_id, low=80, high=90)
        await self.call(
            "ticket_submit",
            agent_name="worker-agent",
            ticket_id=ticket_id,
            summary="Progress lifecycle exercised.",
            files_changed=[],
            notes="observations: lifecycle test",
        )
        raw = self.service.load("pursers")["tickets"][ticket_id]
        self.assertNotIn("progress", raw)
        self.assertEqual(raw["progress_history"][-1]["end_reason"], "submitted")

        self.principal = self.reviewer
        await self.call(
            "ticket_review",
            agent_name="reviewer-agent",
            ticket_id=ticket_id,
            verdict="reject",
            review_notes="Retry the bounded fixture.",
        )
        self.principal = self.worker
        await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        self.assertEqual(
            self.service.load("pursers")["tickets"][ticket_id]["work_attempt"], 3
        )
        await self.progress(ticket_id, low=10, high=20)
        await self.call(
            "ticket_cancel", agent_name="worker-agent", ticket_id=ticket_id
        )
        raw = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(raw["progress_history"][-1]["end_reason"], "canceled")

        reset_reasons = [
            event.get("reset_reason")
            for event in self.service.journal.read_after("pursers", 0, 1000)["events"]
            if event["kind"] == "ticket_progress_reset"
        ]
        self.assertEqual(
            reset_reasons,
            ["unclaimed", "submitted", "review_rejected", "canceled"],
        )

    async def test_expired_lease_archives_progress_once(self) -> None:
        self.configure(enabled=True)
        self.principal = self.admin
        await self.call(
            "board_claim_ttl_set", agent_name="admin-agent", claim_ttl_s=10
        )
        ticket_id = await self.create_and_claim()
        await self.progress(ticket_id)
        raw = self.service.load("pursers")["tickets"][ticket_id]
        expires = float(raw["lease_expires_at_epoch"])
        with patch.object(central.time, "time", return_value=expires):
            await self.call("board_reap")
        raw = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(raw["status"], "open")
        self.assertNotIn("progress", raw)
        self.assertEqual(raw["progress_history"][-1]["end_reason"], "lease_expired")


if __name__ == "__main__":
    unittest.main()
