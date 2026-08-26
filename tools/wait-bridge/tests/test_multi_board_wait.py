from __future__ import annotations

import asyncio
import os
import sys
import unittest
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from pursers_client import JoinedIdentity  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402


class FakeResult:
    def __init__(self, value: dict[str, Any] | None = None, error: str | None = None):
        self.is_error = error is not None
        self.structured_content = None if error else {"result": value or {}}
        self.content = [SimpleNamespace(text=error or "")]


class FakeSubscription:
    def __init__(self, uri: str, queue: asyncio.Queue[object]) -> None:
        self.honored = SimpleNamespace(resource_subscriptions=[uri])
        self.queue = queue

    def __aiter__(self) -> FakeSubscription:
        return self

    async def __anext__(self) -> object:
        return await self.queue.get()


class FakeListenContext(AbstractAsyncContextManager[FakeSubscription]):
    def __init__(self, transport: FakeTransport, board_id: str) -> None:
        self.transport = transport
        self.board_id = board_id

    async def __aenter__(self) -> FakeSubscription:
        if self.board_id in self.transport.listen_failures:
            raise RuntimeError("synthetic per-board listen failure")
        self.transport.ready[self.board_id].set()
        uri = f"board://{self.board_id}/journal"
        return FakeSubscription(uri, self.transport.cues[self.board_id])

    async def __aexit__(self, *_arguments: Any) -> None:
        return None


class FakeTransport:
    def __init__(self, boards: list[str]) -> None:
        self.principal_id = "PR-multi"
        self.events = {board_id: [] for board_id in boards}
        self.latest = {board_id: 0 for board_id in boards}
        self.tickets: dict[str, dict[str, dict[str, Any]]] = {
            board_id: {} for board_id in boards
        }
        self.denied: set[str] = set()
        self.held: dict[str, list[dict[str, Any]]] = {
            board_id: [] for board_id in boards
        }
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.renewed: list[tuple[str, str]] = []
        self.ready = {board_id: asyncio.Event() for board_id in boards}
        self.cues = {
            board_id: asyncio.Queue() for board_id in boards
        }
        self.listen_failures: set[str] = set()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        **_options: Any,
    ) -> FakeResult:
        board_id = arguments["board_id"]
        payload = {key: value for key, value in arguments.items() if key != "board_id"}
        self.calls.append((name, board_id, payload))
        if name == "board_join":
            if board_id in self.denied:
                return FakeResult(error=f"board {board_id!r} requires an invite")
            agent_name = payload["agent_name"]
            return FakeResult(
                {
                    "board_id": board_id,
                    "agent_id": wait_server._derived_agent_id(
                        self.principal_id, agent_name, board_id
                    ),
                    "principal_id": self.principal_id,
                    "agent_name": agent_name,
                    "role": "worker",
                }
            )
        if name == "board_catchup":
            cursor = int(payload.get("cursor") or 0)
            events = [
                event for event in self.events[board_id]
                if int(event.get("seq", 0)) > cursor
            ]
            return FakeResult(
                {
                    "events": events,
                    "next_cursor": self.latest[board_id],
                    "has_more": False,
                    "resync_required": False,
                }
            )
        if name == "ticket_get":
            return FakeResult(
                {"ticket": self.tickets[board_id][payload["ticket_id"]]}
            )
        if name == "ticket_list":
            tickets = (
                self.held[board_id]
                if payload.get("assigned_to") is not None
                else list(self.tickets[board_id].values())
            )
            if payload.get("status") is not None:
                tickets = [
                    ticket for ticket in tickets
                    if ticket.get("status") == payload["status"]
                ]
            return FakeResult({"tickets": tickets})
        if name == "lease_renew":
            self.renewed.append((board_id, payload["ticket_id"]))
            return FakeResult({"lease_expires_at": "later"})
        raise AssertionError(f"unexpected tool: {name}")

    def listen(self, *, resource_subscriptions: list[str]) -> FakeListenContext:
        uri = resource_subscriptions[0]
        board_id = uri.removeprefix("board://").removesuffix("/journal")
        return FakeListenContext(self, board_id)

    def add_event(self, board_id: str, ticket_id: str, seq: int) -> None:
        self.latest[board_id] = seq
        self.events[board_id].append(
            {
                "seq": seq,
                "kind": "ticket_created",
                "ticket_id": ticket_id,
                "status_to": "open",
            }
        )
        self.tickets[board_id][ticket_id] = {
            "ticket_id": ticket_id,
            "status": "open",
            "target_url": f"{board_id}/work",
        }


class FakeRootClient:
    def __init__(self, transport: FakeTransport) -> None:
        self._client = transport
        self.agent_name = "env-default"
        self.identity = JoinedIdentity(
            wait_server.BOARD_ID,
            wait_server._derived_agent_id(
                transport.principal_id, "env-default"
            ),
            transport.principal_id,
            "env-default",
            "worker",
        )


class MultiBoardWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_board_function_keeps_original_response_shape(self) -> None:
        from test_per_call_wait import FakeClient

        client = FakeClient()
        context = SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context={"client": client}
            )
        )
        result = await wait_server.a2a_wait(
            context, since_seq=0, only_mine=True
        )

        self.assertEqual(client.join_calls, [])
        self.assertEqual(
            list(result),
            ["new_seq", "events", "waited_s", "timed_out", "resynced"],
        )
        self.assertIsInstance(result["new_seq"], int)
        self.assertNotIn("board_id", result["events"][0])

    async def test_two_boards_keep_cursors_isolated_and_tag_events(self) -> None:
        transport = FakeTransport(["alpha", "beta"])
        transport.latest.update(alpha=4, beta=9)
        transport.add_event("alpha", "TK-alpha", 4)
        transport.add_event("beta", "TK-beta", 9)

        result = await wait_server._wait_for_work_many(
            FakeRootClient(transport),
            boards=["alpha", "beta"],
            since_seq={"alpha": 3, "beta": 7},
            only_mine=False,
        )

        self.assertEqual(result["new_seq"], {"alpha": 4, "beta": 9})
        self.assertEqual(
            [(event["board_id"], event["ticket_id"]) for event in result["events"]],
            [("alpha", "TK-alpha"), ("beta", "TK-beta")],
        )
        catchups = [call for call in transport.calls if call[0] == "board_catchup"]
        self.assertEqual(
            [(board_id, args["cursor"]) for _, board_id, args in catchups],
            [("alpha", 3), ("beta", 7)],
        )
        lists = [call[1] for call in transport.calls if call[0] == "ticket_list"]
        self.assertEqual(lists, ["alpha", "beta"])

    async def test_push_cue_refetches_only_the_cued_board(self) -> None:
        transport = FakeTransport(["alpha", "beta"])
        with patch.object(wait_server, "WAIT_MODE", "push"):
            waiting = asyncio.create_task(
                wait_server._wait_for_work_many(
                    FakeRootClient(transport),
                    boards=["alpha", "beta"],
                    timeout_s=2,
                    only_mine=False,
                )
            )
            await asyncio.wait_for(
                asyncio.gather(
                    transport.ready["alpha"].wait(),
                    transport.ready["beta"].wait(),
                ),
                timeout=1,
            )
            for _ in range(100):
                catchups = [
                    call for call in transport.calls if call[0] == "board_catchup"
                ]
                if len(catchups) >= 4:
                    break
                await asyncio.sleep(0)
            before = {
                board_id: sum(
                    1
                    for name, called_board, _ in transport.calls
                    if name == "board_catchup" and called_board == board_id
                )
                for board_id in ("alpha", "beta")
            }
            transport.add_event("beta", "TK-beta-cue", 1)
            await transport.cues["beta"].put(object())
            result = await asyncio.wait_for(waiting, timeout=1)

        after = {
            board_id: sum(
                1
                for name, called_board, _ in transport.calls
                if name == "board_catchup" and called_board == board_id
            )
            for board_id in ("alpha", "beta")
        }
        self.assertEqual(after["alpha"], before["alpha"])
        self.assertEqual(after["beta"], before["beta"] + 1)
        self.assertEqual(result["events"][0]["board_id"], "beta")

    async def test_push_failure_degrades_only_that_board_to_polling(self) -> None:
        transport = FakeTransport(["alpha", "beta"])
        transport.listen_failures.add("beta")
        with (
            patch.object(wait_server, "WAIT_MODE", "push"),
            patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.02),
        ):
            waiting = asyncio.create_task(
                wait_server._wait_for_work_many(
                    FakeRootClient(transport),
                    boards=["alpha", "beta"],
                    timeout_s=2,
                    only_mine=False,
                )
            )
            await asyncio.wait_for(transport.ready["alpha"].wait(), timeout=1)
            for _ in range(100):
                catchups = [
                    call for call in transport.calls if call[0] == "board_catchup"
                ]
                if len(catchups) >= 4:
                    break
                await asyncio.sleep(0)
            before_alpha = sum(
                1
                for name, board_id, _ in transport.calls
                if name == "board_catchup" and board_id == "alpha"
            )
            transport.add_event("beta", "TK-beta-fallback", 1)
            result = await asyncio.wait_for(waiting, timeout=1)

        after_alpha = sum(
            1
            for name, board_id, _ in transport.calls
            if name == "board_catchup" and board_id == "alpha"
        )
        self.assertEqual(after_alpha, before_alpha)
        self.assertEqual(result["events"][0]["board_id"], "beta")

    async def test_denied_board_is_reported_and_does_not_abort(self) -> None:
        transport = FakeTransport(["alpha", "denied"])
        transport.denied.add("denied")
        transport.add_event("alpha", "TK-alpha", 1)

        result = await wait_server._wait_for_work_many(
            FakeRootClient(transport),
            boards=["alpha", "denied"],
            only_mine=False,
        )

        self.assertIn("denied", result["skipped_boards"])
        self.assertEqual(result["new_seq"], {"alpha": 1, "denied": 0})
        self.assertEqual(result["events"][0]["board_id"], "alpha")

    async def test_heartbeat_renews_only_on_board_holding_claim(self) -> None:
        transport = FakeTransport(["alpha", "beta"])
        name = "pool-worker"
        beta_id = wait_server._derived_agent_id(
            transport.principal_id, name, "beta"
        )
        transport.held["beta"] = [
            {
                "ticket_id": "TK-held-beta",
                "status": "claimed",
                "claimed_by_agent_id": beta_id,
            }
        ]

        with (
            patch.object(wait_server, "WAIT_MODE", "poll"),
            patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.05),
            patch.object(wait_server, "HEARTBEAT_INTERVAL_S", 0.2),
        ):
            await wait_server._wait_for_work_many(
                FakeRootClient(transport),
                boards=["alpha", "beta"],
                timeout_s=1,
                only_mine=False,
                agent_name=name,
            )

        self.assertTrue(transport.renewed)
        self.assertEqual(set(transport.renewed), {("beta", "TK-held-beta")})


if __name__ == "__main__":
    unittest.main()
