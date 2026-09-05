from __future__ import annotations

import sys
import unittest
from pathlib import Path


BRIDGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_ROOT))

from backlog import backlog_events, ticket_is_relevant  # noqa: E402


class BacklogTests(unittest.TestCase):
    def ticket(self, **overrides: object) -> dict:
        value = {
            "ticket_id": "TK-open",
            "status": "open",
            "target_url": "pursers/packages/central",
            "created_by_agent_id": "AI-creator",
            "claimed_by_agent_id": None,
            "assigned_to_agent_id": None,
            "payload_ref": "board://pursers/ticket/TK-open",
        }
        value.update(overrides)
        return value

    def test_open_unassigned_ticket_becomes_sequence_free_backlog_cue(self) -> None:
        events = backlog_events(
            [self.ticket()], "AI-me", only_mine=True, project="pursers"
        )
        self.assertEqual(
            events,
            [
                {
                    "kind": "ticket_backlog",
                    "source": "backlog_scan",
                    "ticket_id": "TK-open",
                    "status": "open",
                    "payload_ref": "board://pursers/ticket/TK-open",
                }
            ],
        )
        self.assertNotIn("seq", events[0])

    def test_only_mine_excludes_ticket_assigned_to_another_agent(self) -> None:
        ticket = self.ticket(assigned_to_agent_id="AI-other")
        self.assertFalse(
            ticket_is_relevant(ticket, "AI-me", True, "pursers")
        )
        self.assertTrue(
            ticket_is_relevant(ticket, "AI-me", False, "pursers")
        )

    def test_only_mine_includes_ticket_assigned_to_me(self) -> None:
        ticket = self.ticket(assigned_to_agent_id="AI-me")
        self.assertTrue(
            ticket_is_relevant(ticket, "AI-me", True, "pursers")
        )

    def test_project_filter_and_open_status_are_enforced(self) -> None:
        wrong_project = self.ticket(target_url="other/packages/central")
        closed = self.ticket(status="closed")
        events = backlog_events(
            [wrong_project, closed], "AI-me", True, "pursers"
        )
        self.assertEqual(events, [])

    def test_submitted_backlog_requires_available_review_state(self) -> None:
        available = self.ticket(
            ticket_id="TK-review", status="submitted", review_state="unclaimed"
        )
        expired = self.ticket(
            ticket_id="TK-expired", status="submitted", review_state="expired"
        )
        unavailable = [
            self.ticket(
                ticket_id="TK-busy",
                status="submitted",
                review_state=state,
            )
            for state in ("claimed_by_me", "claimed_by_other")
        ]
        unavailable.append(
            self.ticket(
                ticket_id="TK-lease",
                status="submitted",
                review_lease={"reviewer_agent_id": "AI-other"},
            )
        )

        events = backlog_events(
            [available, expired, *unavailable],
            "AI-reviewer",
            only_mine=False,
            project="pursers",
            wait_for="submitted",
        )

        self.assertEqual(
            [event["ticket_id"] for event in events],
            ["TK-review", "TK-expired"],
        )

    def test_dispatch_worker_sees_only_its_offer_with_offer_payload(self) -> None:
        mine = self.ticket(
            ticket_id="TK-mine",
            dispatch_state={"state": "offered"},
            work_offer={"agent_id": "AI-me", "expires_at": "later"},
            tier=1,
            skills_required=["python"],
        )
        other = self.ticket(
            ticket_id="TK-tier-3",
            dispatch_state={"state": "offered"},
            work_offer={"agent_id": "AI-tier-3", "expires_at": "later"},
            tier=3,
        )

        events = backlog_events(
            [mine, other], "AI-me", only_mine=False, project="pursers",
            board_id="pursers",
        )

        self.assertEqual([event["ticket_id"] for event in events], ["TK-mine"])
        self.assertEqual(events[0]["kind"], "ticket_offered")
        self.assertEqual(
            events[0]["offer"],
            {
                "ticket_id": "TK-mine",
                "board_id": "pursers",
                "expires_at": "later",
                "tier": 1,
                "skills_required": ["python"],
            },
        )

    def test_dispatch_reviewer_sees_only_its_review_offer(self) -> None:
        mine = self.ticket(
            ticket_id="TK-review-mine",
            status="submitted",
            dispatch_state={"state": "review_offered"},
            review_offer={"agent_id": "AI-reviewer", "expires_at": "later"},
        )
        other = self.ticket(
            ticket_id="TK-review-other",
            status="submitted",
            dispatch_state={"state": "review_offered"},
            review_offer={"agent_id": "AI-other", "expires_at": "later"},
        )

        events = backlog_events(
            [mine, other], "AI-reviewer", only_mine=False, project="pursers",
            wait_for="submitted", board_id="pursers",
        )

        self.assertEqual([event["ticket_id"] for event in events], ["TK-review-mine"])
        self.assertEqual(events[0]["kind"], "review_offered")

    def test_backlog_cue_includes_successor_continuation(self) -> None:
        ticket = self.ticket(
            last_claimed_by="prior-worker",
            last_claimed_by_agent_id="AI-prior",
            last_release_reason="lease expired",
            abandoned_count=2,
            submission_history=[
                {
                    "notes": (
                        "test_output: pass\n"
                        "branch_and_commit: codex/TK-open @ " + "a" * 40
                    )
                }
            ],
        )

        event = backlog_events(
            [ticket], "AI-me", only_mine=True, project="pursers"
        )[0]

        self.assertEqual(
            event["continuation"]["branch_and_commit"],
            "codex/TK-open @ " + "a" * 40,
        )
        self.assertEqual(
            event["continuation"]["prior_holder"]["agent_name"],
            "prior-worker",
        )
        self.assertEqual(event["continuation"]["abandoned_count"], 2)


if __name__ == "__main__":
    unittest.main()
