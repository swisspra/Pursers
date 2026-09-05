from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
CENTRAL_SRC = ROOT.parents[1] / "packages" / "central" / "src" / "pursers_central"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(CENTRAL_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from pursers_client import BoardClientError, JoinedIdentity  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402
import central  # noqa: E402


class BulkClient:
    def __init__(
        self,
        count: int = 500,
        *,
        page_size: int = 500,
        ticket_order: str = "oldest",
        supports_ticket_filter: bool = True,
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
        self.supports_ticket_filter = supports_ticket_filter
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
        if ticket_ids is not None and self.supports_ticket_filter:
            selected = set(ticket_ids)
            tickets = [ticket for ticket in tickets if ticket["ticket_id"] in selected]
        status = arguments.get("status")
        if status is not None:
            tickets = [ticket for ticket in tickets if ticket["status"] == status]
        total = len(tickets)
        tickets = tickets[: int(arguments.get("limit", 100))]
        result = {
            "tickets": tickets,
            "count": len(tickets),
            "total_matching": total,
        }
        if ticket_ids is not None and self.supports_ticket_filter:
            result["filters"] = {"ticket_ids": sorted(ticket_ids)}
        return result

    async def ticket_get(self, _ticket_id: str) -> dict[str, Any]:
        raise AssertionError("batched catch-up must not call ticket_get")

    async def lease_renew(self, _ticket_id: str) -> dict[str, Any]:
        return {"lease_expires_at": "later"}


class InProcessCentralClient:
    def __init__(self, mcp: Any, identity: JoinedIdentity) -> None:
        self.mcp = mcp
        self.identity = identity
        self.agent_name = identity.agent_name
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {"board_id": wait_server.BOARD_ID, **arguments}
        self.calls.append((name, dict(payload)))
        result = await self.mcp.call_tool(name, payload)
        if result.is_error:
            raise BoardClientError(str(result.content))
        return dict(result.structured_content or {})

    async def board_join(self, **arguments: Any) -> dict[str, Any]:
        return await self._call("board_join", arguments)

    async def board_catchup(self, **arguments: Any) -> dict[str, Any]:
        arguments.setdefault("agent_name", self.agent_name)
        return await self._call("board_catchup", arguments)

    async def ticket_list(self, **arguments: Any) -> dict[str, Any]:
        arguments.setdefault("agent_name", self.agent_name)
        return await self._call("ticket_list", arguments)

    async def ticket_get(self, ticket_id: str) -> dict[str, Any]:
        return await self._call("ticket_get", {"ticket_id": ticket_id})

    async def lease_renew(self, ticket_id: str) -> dict[str, Any]:
        return await self._call(
            "lease_renew",
            {"ticket_id": ticket_id, "agent_name": self.agent_name},
        )


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
        self.assertEqual(len(result["events"]), 100)
        self.assertTrue(
            all(event["projection_state"] == "unprojected" for event in result["events"])
        )
        self.assertEqual(result["new_seq"], 100)

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
        self.assertEqual(len(result["events"]), 100)
        self.assertTrue(
            all(event["projection_state"] == "unprojected" for event in result["events"])
        )
        self.assertEqual(result["new_seq"], 100)
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
        self.assertEqual(len(result["events"]), 100)
        self.assertTrue(
            all(event["projection_state"] == "unprojected" for event in result["events"])
        )
        self.assertEqual(result["new_seq"], 100)
        self.assertEqual(
            [name for name, _arguments in client.calls],
            ["board_catchup", "ticket_list"],
        )

    async def test_ignored_ticket_filter_advances_once_and_does_not_replay(self) -> None:
        for ticket_order in ("oldest", "newest"):
            with self.subTest(ticket_order=ticket_order):
                client = BulkClient(
                    count=600,
                    ticket_order=ticket_order,
                    supports_ticket_filter=False,
                )
                with patch.object(wait_server, "clamp_timeout", return_value=0.1):
                    started = time.monotonic()
                    first = await wait_server._wait_for_work(
                        client, since_seq=None, timeout_s=1, only_mine=False
                    )
                self.assertLess(time.monotonic() - started, 0.1)
                self.assertTrue(first["partial"])
                self.assertEqual(first["new_seq"], 600)
                self.assertEqual(client.persisted_cursor, 600)
                self.assertIn(
                    "ticket_projection_unprojected",
                    {warning["code"] for warning in first["warnings"]},
                )

                missing_number = 600 if ticket_order == "oldest" else 1
                client.events.append(
                    {
                        "id": "EV-601",
                        "seq": 601,
                        "kind": "ticket_created",
                        "ticket_id": f"TK-{missing_number:04d}",
                        "status_to": "open",
                    }
                )
                calls_after_first = len(client.calls)
                with patch.object(wait_server, "clamp_timeout", return_value=0.1):
                    second = await wait_server._wait_for_work(
                        client, since_seq=None, timeout_s=1, only_mine=False
                    )
                self.assertTrue(second["partial"])
                self.assertEqual(second["new_seq"], 601)
                self.assertEqual(client.persisted_cursor, 601)
                self.assertEqual(len(second["events"]), 1)
                self.assertEqual(second["events"][0]["seq"], 601)
                self.assertEqual(
                    second["events"][0]["projection_state"], "unprojected"
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


class RealCentralCatchupPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=ROOT)
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
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        joined = await self.call("board_join", agent_name="admin-agent")
        self.admin_agent_id = joined["agent_id"]
        await self.call(
            "board_member_add",
            agent_name="admin-agent",
            principal_id=self.worker.principal_id,
            role="member",
        )
        self.principal = self.worker
        worker_joined = await self.call("board_join", agent_name="perf-worker")
        identity = JoinedIdentity(
            wait_server.BOARD_ID,
            worker_joined["agent_id"],
            self.worker.principal_id,
            "perf-worker",
            "worker",
        )
        self.client = InProcessCentralClient(self.mcp, identity)

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object) -> dict[str, Any]:
        result = await self.mcp.call_tool(
            name, {"board_id": wait_server.BOARD_ID, **arguments}
        )
        self.assertFalse(result.is_error)
        return dict(result.structured_content or {})

    async def test_1500_event_real_central_reaches_head_in_two_waits(self) -> None:
        self.principal = self.admin
        ticket_ids: list[str] = []
        for number in range(210):
            created = await self.call(
                "ticket_create",
                agent_name="admin-agent",
                title=f"bulk ticket {number:03d}",
                description="real Central catch-up fixture",
                target_url="pursers/tools/wait-bridge",
                scope="interactive-no-send",
                required_fields=["test_output"],
            )
            ticket_ids.append(created["ticket"]["ticket_id"])

        latest = self.service.journal.read_after(
            wait_server.BOARD_ID, 0, 1
        )["latest_cursor"]
        for number in range(max(0, 1500 - int(latest))):
            ticket_id = ticket_ids[number % len(ticket_ids)]
            self.service.journal.append(
                wait_server.BOARD_ID,
                {
                    "kind": "ticket_created",
                    "actor": self.admin_agent_id,
                    "payload_ref": f"board://pursers/ticket/{ticket_id}",
                    "recipient_identities": [],
                    "ticket_id": ticket_id,
                    "status_to": "open",
                },
            )

        self.principal = self.worker
        first_head = int(
            self.service.journal.read_after(wait_server.BOARD_ID, 0, 1)[
                "latest_cursor"
            ]
        )
        first = await wait_server._wait_for_work(
            self.client,
            since_seq=0,
            timeout_s=12,
            only_mine=False,
            agent_name="perf-worker",
        )
        self.assertEqual(first["new_seq"], first_head)
        self.assertFalse(first.get("partial", False))
        first_calls = list(self.client.calls)
        self.assertLessEqual(
            sum(name == "board_catchup" for name, _ in first_calls), 8
        )
        self.assertLessEqual(
            sum(name == "ticket_list" for name, _ in first_calls), 2
        )

        self.service.journal.append(
            wait_server.BOARD_ID,
            {
                "kind": "ticket_created",
                "actor": self.admin_agent_id,
                "payload_ref": f"board://pursers/ticket/{ticket_ids[0]}",
                "recipient_identities": [],
                "ticket_id": ticket_ids[0],
                "status_to": "open",
            },
        )
        second_head = int(
            self.service.journal.read_after(wait_server.BOARD_ID, 0, 1)[
                "latest_cursor"
            ]
        )
        calls_before_second = len(self.client.calls)
        second = await wait_server._wait_for_work(
            self.client,
            since_seq=first["new_seq"],
            timeout_s=12,
            only_mine=False,
            agent_name="perf-worker",
        )
        self.assertEqual(second["new_seq"], second_head)
        self.assertFalse(second.get("partial", False))
        second_calls = self.client.calls[calls_before_second:]
        self.assertLessEqual(
            sum(name == "board_catchup" for name, _ in second_calls), 1
        )
        self.assertLessEqual(
            sum(name == "ticket_list" for name, _ in second_calls), 1
        )


if __name__ == "__main__":
    unittest.main()
