from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from pursers_client import BoardClientError, JoinedIdentity  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402


class BulkClient:
    def __init__(
        self,
        count: int = 500,
        *,
        page_size: int = 500,
        ticket_order: str = "oldest",
    ) -> None:
        self.agent_name = "perf-seat"
        self.identity = JoinedIdentity(
            wait_server.BOARD_ID,
            wait_server._derived_agent_id("PR-perf", self.agent_name),
            "PR-perf",
            self.agent_name,
            "worker",
        )
        self.events = [
            {
                "id": f"EV-{seq}",
                "seq": seq,
                "kind": "ticket_created",
                "ticket_id": f"TK-{seq:04d}",
                "status_to": "open",
            }
            for seq in range(1, count + 1)
        ]
        self.tickets = [
            {
                "ticket_id": f"TK-{seq:04d}",
                "status": "open",
                "target_url": "pursers/work",
            }
            for seq in range(1, count + 1)
        ]
        self.page_size = page_size
        self.ticket_order = ticket_order
        self.persisted_cursor = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def board_catchup(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("board_catchup", dict(arguments)))
        cursor = arguments.get("cursor")
        start = self.persisted_cursor if cursor is None else int(cursor)
        page_size = min(self.page_size, int(arguments.get("limit", 100)))
        page = [event for event in self.events if event["seq"] > start][:page_size]
        next_cursor = page[-1]["seq"] if page else len(self.events)
        if arguments.get("ack"):
            self.persisted_cursor = next_cursor
        return {
            "events": page,
            "next_cursor": next_cursor,
            "latest_cursor": len(self.events),
            "acknowledged_cursor": start,
            "has_more": next_cursor < len(self.events),
            "resync_required": False,
        }

    async def ticket_list(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("ticket_list", dict(arguments)))
        tickets = list(self.tickets)
        if self.ticket_order == "newest":
            tickets.reverse()
        ticket_ids = arguments.get("ticket_ids")
        if ticket_ids is not None:
            selected = set(ticket_ids)
            tickets = [ticket for ticket in tickets if ticket["ticket_id"] in selected]
        status = arguments.get("status")
        if status is not None:
            tickets = [ticket for ticket in tickets if ticket["status"] == status]
        total = len(tickets)
        tickets = tickets[: int(arguments.get("limit", 100))]
        return {
            "tickets": tickets,
            "count": len(tickets),
            "total_matching": total,
        }

    async def ticket_get(self, _ticket_id: str) -> dict[str, Any]:
        raise AssertionError("batched catch-up must not call ticket_get")

    async def lease_renew(self, _ticket_id: str) -> dict[str, Any]:
        return {"lease_expires_at": "later"}


class CatchupPerformanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        wait_server._BACKLOG_SEEN.clear()

    async def test_500_event_replay_is_batched_bounded_and_compacted(self) -> None:
        client = BulkClient()
        started = time.monotonic()

        result = await wait_server._wait_for_work(
            client, since_seq=0, timeout_s=1, only_mine=False
        )

        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5)
        self.assertLess(len(client.calls), 20)
        self.assertEqual(
            [name for name, _arguments in client.calls],
            ["board_catchup", "ticket_list"],
        )
        self.assertTrue(result["compacted"])
        self.assertEqual(result["dropped"], 300)
        self.assertEqual(len(result["events"]), wait_server.REPLAY_EVENT_LIMIT)
        self.assertEqual(result["event_counts"], {"ticket_created": 500})
        self.assertEqual(result["new_seq"], 500)

    async def test_omitted_cursor_acks_and_does_not_replay(self) -> None:
        client = BulkClient()
        first = await wait_server._wait_for_work(
            client, since_seq=None, timeout_s=1, only_mine=False
        )
        self.assertTrue(first["compacted"])
        self.assertEqual(client.persisted_cursor, 500)
        self.assertNotIn(
            "cursor",
            next(args for name, args in client.calls if name == "board_catchup"),
        )

        with (
            patch.object(wait_server, "WAIT_MODE", "poll"),
            patch.object(wait_server, "clamp_timeout", return_value=0.02),
            patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.01),
        ):
            second = await wait_server._wait_for_work(
                client, since_seq=None, timeout_s=1, only_mine=False
            )
        self.assertEqual(second["events"], [])
        self.assertTrue(second["timed_out"])
        self.assertEqual(client.persisted_cursor, 500)

    async def test_slow_multi_page_catchup_returns_partial_cursor(self) -> None:
        client = BulkClient(page_size=100)
        original = client.board_catchup

        async def slow_catchup(**arguments: Any) -> dict[str, Any]:
            await asyncio.sleep(0.02)
            return await original(**arguments)

        client.board_catchup = slow_catchup  # type: ignore[method-assign]
        with patch.object(wait_server, "clamp_timeout", return_value=0.03):
            started = time.monotonic()
            result = await wait_server._wait_for_work(
                client, since_seq=0, timeout_s=1, only_mine=False
            )
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(result["partial"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["new_seq"], 0)

    async def test_single_slow_catchup_is_bounded_by_wait_deadline(self) -> None:
        client = BulkClient()
        original = client.board_catchup

        async def slow_catchup(**arguments: Any) -> dict[str, Any]:
            await asyncio.sleep(0.2)
            return await original(**arguments)

        client.board_catchup = slow_catchup  # type: ignore[method-assign]
        with patch.object(wait_server, "clamp_timeout", return_value=0.03):
            started = time.monotonic()
            result = await wait_server._wait_for_work(
                client, since_seq=17, timeout_s=1, only_mine=False
            )
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(result["partial"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["new_seq"], 17)

    async def test_slow_ticket_projection_is_bounded_by_wait_deadline(self) -> None:
        client = BulkClient(count=100)

        async def slow_ticket_list(**arguments: Any) -> dict[str, Any]:
            client.calls.append(("ticket_list", dict(arguments)))
            await asyncio.sleep(0.2)
            return {"tickets": [], "count": 0, "total_matching": 0}

        client.ticket_list = slow_ticket_list  # type: ignore[method-assign]
        with patch.object(wait_server, "clamp_timeout", return_value=0.03):
            started = time.monotonic()
            result = await wait_server._wait_for_work(
                client, since_seq=0, timeout_s=1, only_mine=False
            )
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(result["partial"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["new_seq"], 0)
        self.assertEqual(
            [name for name, _arguments in client.calls],
            ["board_catchup", "ticket_list"],
        )

    async def test_failed_ticket_projection_never_falls_back_per_event(self) -> None:
        client = BulkClient(count=100)

        async def fail_ticket_list(**arguments: Any) -> dict[str, Any]:
            client.calls.append(("ticket_list", dict(arguments)))
            raise BoardClientError("synthetic projection failure")

        client.ticket_list = fail_ticket_list  # type: ignore[method-assign]
        result = await wait_server._wait_for_work(
            client, since_seq=0, timeout_s=1, only_mine=False
        )
        self.assertTrue(result["partial"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["new_seq"], 0)
        self.assertEqual(
            [name for name, _arguments in client.calls],
            ["board_catchup", "ticket_list"],
        )

    async def test_rearm_after_partial_reaches_tickets_beyond_projection_limit(self) -> None:
        for ticket_order in ("oldest", "newest"):
            with self.subTest(ticket_order=ticket_order):
                client = BulkClient(count=600, ticket_order=ticket_order)
                original = client.ticket_list
                projection_calls = 0

                async def truncate_second_projection(**arguments: Any) -> dict[str, Any]:
                    nonlocal projection_calls
                    projection_calls += 1
                    result = await original(**arguments)
                    if projection_calls == 2:
                        result["total_matching"] = len(result["tickets"]) + 1
                    return result

                client.ticket_list = truncate_second_projection  # type: ignore[method-assign]
                with patch.object(wait_server, "clamp_timeout", return_value=0.1):
                    started = time.monotonic()
                    first = await wait_server._wait_for_work(
                        client, since_seq=None, timeout_s=1, only_mine=False
                    )
                self.assertLess(time.monotonic() - started, 0.1)
                self.assertTrue(first["partial"])
                self.assertEqual(first["new_seq"], 500)
                self.assertEqual(client.persisted_cursor, 500)

                calls_after_first = len(client.calls)
                with patch.object(wait_server, "clamp_timeout", return_value=0.1):
                    second = await wait_server._wait_for_work(
                        client, since_seq=None, timeout_s=1, only_mine=False
                    )
                self.assertFalse(second.get("partial", False))
                self.assertEqual(second["new_seq"], 600)
                self.assertEqual(client.persisted_cursor, 600)
                self.assertEqual(
                    {event["ticket_id"] for event in second["events"]},
                    {f"TK-{seq:04d}" for seq in range(501, 601)},
                )
                self.assertLess(calls_after_first, 20)
                self.assertLess(len(client.calls) - calls_after_first, 20)
                keyed = [
                    arguments
                    for name, arguments in client.calls
                    if name == "ticket_list" and "ticket_ids" in arguments
                ]
                self.assertTrue(keyed)
                self.assertTrue(all(len(arguments["ticket_ids"]) <= 500 for arguments in keyed))

    async def test_submitted_ticket_claimed_by_other_reviewer_does_not_wake(self) -> None:
        client = BulkClient(count=0)
        client.identity = JoinedIdentity(
            wait_server.BOARD_ID,
            wait_server._derived_agent_id("PR-perf", client.agent_name),
            "PR-perf",
            client.agent_name,
            "reviewer",
        )
        client.events = [
            {
                "id": "EV-1",
                "seq": 1,
                "kind": "ticket_submitted",
                "ticket_id": "TK-held-review",
                "status_to": "submitted",
            }
        ]
        client.tickets = [
            {
                "ticket_id": "TK-held-review",
                "status": "submitted",
                "target_url": "pursers/review",
                "dispatch_state": {"state": "broadcast"},
                "review_lease": {"reviewer_agent_id": "AI-other"},
            }
        ]

        with (
            patch.object(wait_server, "WAIT_MODE", "poll"),
            patch.object(wait_server, "clamp_timeout", return_value=0.02),
            patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.01),
        ):
            result = await wait_server._wait_for_work(
                client,
                since_seq=0,
                timeout_s=1,
                only_mine=False,
                wait_for="submitted",
            )
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["events"], [])
        list_calls = [args for name, args in client.calls if name == "ticket_list"]
        self.assertTrue(list_calls)
        self.assertTrue(all("review_unclaimed_only" not in args for args in list_calls))

        missing = await wait_server._filter_relevant(
            client,
            client.events,
            client.identity.agent_id,
            only_mine=False,
            project=None,
            wait_for="submitted",
            tickets=[],
        )
        self.assertEqual(missing, [])

    async def test_cursor_ahead_is_clamped_with_warning(self) -> None:
        client = BulkClient(count=10)
        original = client.board_catchup

        async def reject_ahead(**arguments: Any) -> dict[str, Any]:
            cursor = arguments.get("cursor")
            if cursor is not None and int(cursor) > len(client.events):
                raise BoardClientError("cursor is ahead of journal")
            return await original(**arguments)

        client.board_catchup = reject_ahead  # type: ignore[method-assign]
        client.tickets = []
        with (
            patch.object(wait_server, "WAIT_MODE", "poll"),
            patch.object(wait_server, "clamp_timeout", return_value=0.02),
            patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.01),
        ):
            result = await wait_server._wait_for_work(
                client, since_seq=999, timeout_s=1, only_mine=False
            )
        self.assertEqual(result["new_seq"], 10)
        self.assertEqual(
            result["warnings"],
            [
                {
                    "code": "cursor_ahead_clamped",
                    "requested_cursor": 999,
                    "clamped_cursor": 10,
                }
            ],
        )

    async def test_central_error_message_is_preserved_by_a2a_wait(self) -> None:
        client = BulkClient(count=0)

        async def fail(**_arguments: Any) -> dict[str, Any]:
            raise BoardClientError("synthetic Central detail")

        client.board_catchup = fail  # type: ignore[method-assign]
        context = SimpleNamespace(
            request_context=SimpleNamespace(lifespan_context={"client": client})
        )
        with self.assertRaisesRegex(
            ToolError, "a2a_wait Central error: synthetic Central detail"
        ):
            await wait_server.a2a_wait(
                context, since_seq=0, timeout_s=1, only_mine=False
            )


if __name__ == "__main__":
    unittest.main()
