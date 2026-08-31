from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import simulate  # noqa: E402


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(seconds: int) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat()


def worker(name: str, agent_id: str) -> dict[str, str]:
    return {
        "principal_id": f"PR-{name}",
        "agent_name": name,
        "agent_id": agent_id,
        "joined_at": at(0),
        "dispatch_role": "worker",
    }


def event(
    seq: int,
    ticket_id: str,
    seconds: int,
    actor: str,
    status_to: str,
    **fields: object,
) -> dict[str, object]:
    return {
        "seq": seq,
        "board_id": "alpha",
        "ticket_id": ticket_id,
        "kind": "ticket_created" if status_to == "open" and seq == 1 else "ticket_status_changed",
        "occurred_at": at(seconds),
        "actor": actor,
        "status_to": status_to,
        **fields,
    }


def history(events: list[dict[str, object]]) -> dict[str, object]:
    ids = sorted({str(item["ticket_id"]) for item in events})
    return {
        "boards": [
            {
                "board_id": "alpha",
                "agents": [worker("worker-a", "AI-a"), worker("worker-b", "AI-b")],
                "tickets": [
                    {
                        "ticket_id": ticket_id,
                        "priority": "medium",
                    }
                    for ticket_id in ids
                ],
                "events": events,
                "coverage": {
                    "pages": 1,
                    "scanned_events": len(events),
                    "visible_events": len(events),
                    "latest_cursor": len(events),
                },
                "warnings": [],
            }
        ],
        "warnings": [],
        "worker_seats": [],
    }


class SimulationTests(unittest.TestCase):
    def test_durable_canceled_projection_closes_phantom_open_ticket(self) -> None:
        board = {
            "board_id": "alpha",
            "events": [],
            "tickets": [
                {
                    "ticket_id": "TK-canceled",
                    "status": "canceled",
                    "created_at": at(0),
                    "created_by_agent_id": "AI-maker",
                    "updated_at": at(10),
                    "canceled_by": "AI-admin",
                }
            ],
        }

        events = simulate._normalized_events(board)

        self.assertEqual(
            [item["status_to"] for item in events], ["open", "canceled"]
        )

    def test_legacy_closed_projection_closes_phantom_open_ticket(self) -> None:
        board = {
            "board_id": "alpha",
            "events": [],
            "tickets": [
                {
                    "ticket_id": "TK-closed",
                    "status": "closed",
                    "created_at": at(0),
                    "created_by": "maker",
                    "claimed_at": at(2),
                    "claimed_by": "worker-a",
                    "reviewed_at": at(10),
                    "reviewed_by": "reviewer",
                    "submitted_by": "worker-a",
                }
            ],
        }

        events = simulate._normalized_events(board)

        self.assertEqual(
            [item["status_to"] for item in events],
            ["open", "claimed", "closed"],
        )

    def test_agreement(self) -> None:
        result = simulate.replay(
            history(
                [
                    event(1, "TK-one", 0, "AI-maker", "open"),
                    event(2, "TK-one", 10, "AI-a", "claimed"),
                ]
            )
        )
        self.assertEqual(result["agreement_count"], 1)
        self.assertEqual(result["agreement_rate"], 1.0)
        self.assertIn("worker-a", result["decisions"][0]["proposed"])

    def test_disagreement_is_listed(self) -> None:
        result = simulate.replay(
            history(
                [
                    event(1, "TK-one", 0, "AI-maker", "open"),
                    event(2, "TK-one", 10, "AI-b", "claimed"),
                ]
            )
        )
        self.assertEqual(result["agreement_count"], 0)
        self.assertEqual(len(result["mismatches"]), 1)
        self.assertFalse(result["decisions"][0]["agreement"])

    def test_newer_claim_is_listed_as_queue_order_mismatch(self) -> None:
        result = simulate.replay(
            history(
                [
                    event(1, "TK-old", 0, "AI-maker", "open"),
                    event(2, "TK-new", 1, "AI-maker", "open"),
                    event(3, "TK-new", 10, "AI-a", "claimed"),
                ]
            )
        )

        decision = result["decisions"][0]
        self.assertTrue(decision["worker_agreement"])
        self.assertFalse(decision["queue_order_agreement"])
        self.assertTrue(decision["agreement"])
        self.assertEqual(result["mismatches"], [decision])

    def test_critical_ticket_precedes_older_noncritical(self) -> None:
        value = history(
            [
                event(1, "TK-old", 0, "AI-maker", "open"),
                event(2, "TK-critical", 1, "AI-maker", "open"),
                event(3, "TK-critical", 10, "AI-a", "claimed"),
            ]
        )
        critical = next(
            item
            for item in value["boards"][0]["tickets"]
            if item["ticket_id"] == "TK-critical"
        )
        critical["priority"] = "critical"

        result = simulate.replay(value)

        self.assertTrue(result["decisions"][0]["queue_order_agreement"])
        self.assertTrue(result["decisions"][0]["agreement"])

    def test_starvation_is_caught_with_an_idle_worker(self) -> None:
        result = simulate.replay(
            history(
                [
                    event(1, "TK-one", 0, "AI-maker", "open"),
                    event(2, "TK-one", 120, "AI-a", "claimed"),
                ]
            ),
            starvation_seconds=60,
        )
        self.assertEqual(len(result["starvation_events"]), 1)
        self.assertEqual(result["starvation_events"][0]["ticket_id"], "TK-one")
        self.assertEqual(result["starvation_events"][0]["lead_seconds"], 60)

    def test_repeat_abandoner_is_deprioritized(self) -> None:
        result = simulate.replay(
            history(
                [
                    event(1, "TK-old", 0, "AI-maker", "open"),
                    event(2, "TK-old", 1, "AI-a", "claimed"),
                    event(
                        3,
                        "TK-old",
                        2,
                        "AI-reaper",
                        "open",
                        last_abandoned_by="AI-a",
                        abandoned_count=1,
                    ),
                    event(4, "TK-new", 3, "AI-maker", "open"),
                    event(5, "TK-new", 4, "AI-b", "claimed"),
                ]
            )
        )
        decision = next(
            item for item in result["decisions"] if item["ticket_id"] == "TK-new"
        )
        self.assertIn("worker-b", decision["proposed"])
        self.assertTrue(decision["worker_agreement"])
        self.assertIn("abandonment penalty", decision["reason"])

    def test_first_claim_causally_discovers_worker_for_later_claims(self) -> None:
        value = history(
            [
                event(1, "TK-one", 0, "AI-maker", "open"),
                event(2, "TK-one", 10, "AI-a", "claimed"),
                event(3, "TK-one", 11, "AI-a", "submitted"),
                event(
                    4,
                    "TK-one",
                    12,
                    "AI-reviewer",
                    "closed",
                    submitted_by_agent_id="AI-a",
                ),
                event(5, "TK-two", 13, "AI-maker", "open"),
                event(6, "TK-two", 20, "AI-a", "claimed"),
            ]
        )
        for agent in value["boards"][0]["agents"]:
            agent.pop("dispatch_role")

        result = simulate.replay(value)

        self.assertIsNone(result["decisions"][0]["agreement"])
        self.assertTrue(result["decisions"][1]["agreement"])
        self.assertIn("worker-a", result["decisions"][1]["proposed"])

    def test_review_releases_submitter_not_reviewer(self) -> None:
        value = history(
            [
                event(1, "TK-busy", 0, "AI-maker", "open"),
                event(2, "TK-busy", 1, "AI-b", "claimed"),
                event(3, "TK-one", 2, "AI-maker", "open"),
                event(4, "TK-one", 3, "AI-a", "claimed"),
                event(5, "TK-one", 4, "AI-a", "submitted"),
                event(
                    6,
                    "TK-one",
                    5,
                    "AI-b",
                    "closed",
                    submitted_by_agent_id="AI-a",
                ),
                event(7, "TK-two", 6, "AI-maker", "open"),
                event(8, "TK-two", 7, "AI-a", "claimed"),
            ]
        )

        result = simulate.replay(value)

        third = result["decisions"][2]
        self.assertIn("worker-a", third["proposed"])
        self.assertTrue(third["agreement"])

    def test_markdown_is_deterministic(self) -> None:
        value = simulate.replay(
            history(
                [
                    event(1, "TK-one", 0, "AI-maker", "open"),
                    event(2, "TK-one", 10, "AI-a", "claimed"),
                ]
            )
        )
        first = simulate.render_markdown(value, starvation_seconds=300)
        second = simulate.render_markdown(value, starvation_seconds=300)
        self.assertEqual(first, second)
        self.assertIn("Agreement rate: **100.0%**", first)


if __name__ == "__main__":
    unittest.main()
