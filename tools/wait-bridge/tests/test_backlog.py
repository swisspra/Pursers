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


if __name__ == "__main__":
    unittest.main()
