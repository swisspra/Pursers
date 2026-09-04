from __future__ import annotations

import asyncio
import copy
import json
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
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from pursers_client import BoardClient, JoinedIdentity  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402


HOME = wait_server.BOARD_ID
ALPHA = "alpha"
OMEGA = "omega"
WORKER = "pool-worker"
SECOND_WORKER = "pool-worker-2"


def project_registry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "projects": {
            "pursers": {
                "board_id": HOME,
                "work_dir": "/synthetic/work/pursers",
                "status": "active",
            },
            "alpha": {
                "board_id": ALPHA,
                "work_dir": "/synthetic/work/alpha",
                "status": "active",
            },
            "omega": {
                "board_id": OMEGA,
                "work_dir": "/synthetic/work/omega",
                "status": "paused",
            },
        },
    }


class FakeResult:
    def __init__(self, value: dict[str, Any]) -> None:
        self.is_error = False
        self.structured_content = {"result": value}
        self.content: list[object] = []


class FakeSubscription:
    def __init__(self, uris: list[str], queue: asyncio.Queue[object]) -> None:
        self.honored = SimpleNamespace(resource_subscriptions=uris)
        self.queue = queue

    def __aiter__(self) -> FakeSubscription:
        return self

    async def __anext__(self) -> object:
        return await self.queue.get()


class FakeListenContext(AbstractAsyncContextManager[FakeSubscription]):
    def __init__(
        self, central: FakePoolCentral, board_id: str, uris: list[str]
    ) -> None:
        self.central = central
        self.board_id = board_id
        self.uris = uris

    async def __aenter__(self) -> FakeSubscription:
        self.central.subscription_ready[self.board_id].set()
        return FakeSubscription(self.uris, self.central.cues[self.board_id])

    async def __aexit__(self, *_arguments: Any) -> None:
        return None


class FakePoolCentral:
    """One offline Central fake shared by registry, wait, and claim calls."""

    def __init__(self) -> None:
        self.principal_id = "PR-cross-project-e2e"
        self.registry = project_registry()
        self.events: dict[str, list[dict[str, Any]]] = {
            board_id: [] for board_id in (HOME, ALPHA, OMEGA)
        }
        self.tickets: dict[str, dict[str, dict[str, Any]]] = {
            board_id: {} for board_id in (HOME, ALPHA, OMEGA)
        }
        self.latest = {board_id: 0 for board_id in (HOME, ALPHA, OMEGA)}
        self.join_calls: list[tuple[str, str]] = []
        self.joined_ids: dict[tuple[str, str], str] = {}
        self.catchup_calls: list[tuple[str, str, int]] = []
        self.renewed: list[tuple[str, str]] = []
        self.registry_reads = 0
        self.initial_scan_boards: set[str] = set()
        self.initial_scan_done = asyncio.Event()
        self.subscription_ready = {
            board_id: asyncio.Event() for board_id in (HOME, ALPHA, OMEGA)
        }
        self.cues = {
            board_id: asyncio.Queue() for board_id in (HOME, ALPHA, OMEGA)
        }

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        **_options: Any,
    ) -> FakeResult:
        board_id = arguments["board_id"]
        payload = {key: value for key, value in arguments.items() if key != "board_id"}

        if name == "board_join":
            agent_name = payload["agent_name"]
            agent_id = wait_server._derived_agent_id(
                self.principal_id, agent_name, board_id
            )
            self.join_calls.append((board_id, agent_name))
            self.joined_ids[(board_id, agent_name)] = agent_id
            return FakeResult(
                {
                    "board_id": board_id,
                    "agent_id": agent_id,
                    "principal_id": self.principal_id,
                    "agent_name": agent_name,
                    "role": "worker",
                }
            )

        if name == "board_catchup":
            cursor = int(payload.get("cursor", 0))
            agent_name = payload["agent_name"]
            self.catchup_calls.append((board_id, agent_name, cursor))
            return FakeResult(
                {
                    "events": [
                        event
                        for event in self.events[board_id]
                        if int(event["seq"]) > cursor
                    ],
                    "next_cursor": self.latest[board_id],
                    "has_more": False,
                    "resync_required": False,
                }
            )

        if name == "ticket_get":
            return FakeResult(
                {"ticket": copy.deepcopy(self.tickets[board_id][payload["ticket_id"]])}
            )

        if name == "ticket_list":
            tickets = list(self.tickets[board_id].values())
            status = payload.get("status")
            if status is not None:
                tickets = [ticket for ticket in tickets if ticket["status"] == status]
            assigned_to = payload.get("assigned_to")
            if assigned_to is not None:
                tickets = [
                    ticket
                    for ticket in tickets
                    if ticket.get("claimed_by") == assigned_to
                ]
            if status in {None, "open"} and assigned_to is None:
                self.initial_scan_boards.add(board_id)
                if {HOME, ALPHA}.issubset(self.initial_scan_boards):
                    self.initial_scan_done.set()
            return FakeResult({"tickets": copy.deepcopy(tickets)})

        if name == "ticket_claim":
            ticket = self.tickets[board_id][payload["ticket_id"]]
            agent_name = payload["agent_name"]
            agent_id = wait_server._derived_agent_id(
                self.principal_id, agent_name, board_id
            )
            ticket.update(
                status="claimed",
                claimed_by=agent_name,
                claimed_by_agent_id=agent_id,
            )
            self._append_event(
                board_id,
                ticket["ticket_id"],
                kind="ticket_status_changed",
                status_from="open",
                status_to="claimed",
            )
            return FakeResult({"ok": True, "ticket": copy.deepcopy(ticket)})

        if name == "lease_renew":
            ticket_id = payload["ticket_id"]
            if self.tickets[board_id][ticket_id]["status"] != "claimed":
                raise AssertionError("only a live claim can be renewed")
            self.renewed.append((board_id, ticket_id))
            return FakeResult({"lease_expires_at": "synthetic-later"})

        raise AssertionError(f"unexpected tool: {name}")

    def listen(self, *, resource_subscriptions: list[str]) -> FakeListenContext:
        uri = resource_subscriptions[0]
        board_id = uri.removeprefix("board://").removesuffix("/journal")
        return FakeListenContext(self, board_id, resource_subscriptions)

    def _append_event(
        self,
        board_id: str,
        ticket_id: str,
        *,
        kind: str,
        status_to: str,
        status_from: str | None = None,
    ) -> None:
        self.latest[board_id] += 1
        self.events[board_id].append(
            {
                "seq": self.latest[board_id],
                "kind": kind,
                "ticket_id": ticket_id,
                "status_from": status_from,
                "status_to": status_to,
            }
        )

    def add_open_ticket(self, board_id: str, ticket_id: str) -> None:
        self.tickets[board_id][ticket_id] = {
            "ticket_id": ticket_id,
            "status": "open",
            "target_url": f"{board_id}/work",
        }
        self._append_event(
            board_id,
            ticket_id,
            kind="ticket_created",
            status_to="open",
        )

    async def claim(
        self, board_id: str, ticket_id: str, agent_name: str
    ) -> dict[str, Any]:
        result = await self.call_tool(
            "ticket_claim",
            {
                "board_id": board_id,
                "ticket_id": ticket_id,
                "agent_name": agent_name,
            },
        )
        return BoardClient._decode(result)

    async def send_cue(self, board_id: str) -> None:
        await self.cues[board_id].put(object())

    def pause_alpha(self) -> None:
        self.registry["projects"]["alpha"]["status"] = "paused"


class FakeRootClient:
    def __init__(self, central: FakePoolCentral) -> None:
        self._client = central
        self.agent_name = "env-default"
        self.identity = JoinedIdentity(
            HOME,
            wait_server._derived_agent_id(
                central.principal_id, self.agent_name, HOME
            ),
            central.principal_id,
            self.agent_name,
            "worker",
        )
        self.central = central

    async def board_state_get(self, key: str | None = None) -> dict[str, Any]:
        if key != wait_server.PROJECT_REGISTRY_KEY:
            raise AssertionError(f"unexpected board-state key: {key}")
        self.central.registry_reads += 1
        return {
            "ok": True,
            "state": {
                "scope": "project",
                "value": json.dumps(self.central.registry),
            },
        }

    async def events_for_board(
        self,
        board_id: str,
        from_cursor: int,
        identity: JoinedIdentity,
        cursor_callback: Any,
        *,
        generation_token: str | None,
        pure_catchup: bool,
    ) -> Any:
        resources = [
            f"board://{board_id}/journal",
            f"board://{board_id}/agent/{identity.agent_id}",
        ]
        cursor = from_cursor

        async def drain() -> list[dict[str, Any]]:
            nonlocal cursor
            result = await self.central.call_tool(
                "board_catchup",
                {
                    "board_id": board_id,
                    "agent_name": identity.agent_name,
                    "cursor": cursor,
                    "limit": 100,
                    "ack": False,
                    **({"touch": False} if pure_catchup else {}),
                },
            )
            page = BoardClient._decode(result)
            cursor = int(page["next_cursor"])
            cursor_callback(cursor)
            return list(page["events"])

        async with self.central.listen(
            resource_subscriptions=resources
        ) as subscription:
            for event in await drain():
                yield event
            async for _cue in subscription:
                for event in await drain():
                    yield event


def context_for(client: FakeRootClient) -> SimpleNamespace:
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={"client": client})
    )


class CrossProjectPoolE2ETests(unittest.IsolatedAsyncioTestCase):
    async def _exercise_phase_one_loop(self, mode: str) -> None:
        central = FakePoolCentral()
        client = FakeRootClient(central)
        context = context_for(client)

        with (
            patch.object(wait_server, "WAIT_MODE", mode),
            patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.01),
        ):
            waiting = asyncio.create_task(
                wait_server.a2a_wait(
                    context,
                    boards="registry",
                    since_seq={HOME: 0, ALPHA: 0, OMEGA: 0},
                    timeout_s=1,
                    only_mine=True,
                    agent_name=WORKER,
                )
            )
            if mode == "push":
                await asyncio.wait_for(
                    asyncio.gather(
                        central.subscription_ready[HOME].wait(),
                        central.subscription_ready[ALPHA].wait(),
                    ),
                    timeout=0.5,
                )
                # Wait through the post-subscription splice. The ticket must
                # arrive after that refetch so only its push cue can wake us.
                for _ in range(100):
                    if len(central.catchup_calls) >= 4:
                        break
                    await asyncio.sleep(0)
                else:
                    self.fail("push subscriptions never completed their splice")
            else:
                await asyncio.wait_for(central.initial_scan_done.wait(), timeout=0.5)

            catchups_before_wake = {
                board_id: sum(
                    1
                    for called_board, called_name, _ in central.catchup_calls
                    if called_board == board_id and called_name == WORKER
                )
                for board_id in (HOME, ALPHA)
            }
            central.add_open_ticket(ALPHA, "TK-alpha")
            if mode == "push":
                await central.send_cue(ALPHA)
            first = await asyncio.wait_for(waiting, timeout=0.5)

            # Registry selection excludes paused omega; per-board cursors do not mix.
            self.assertEqual(central.join_calls, [(HOME, WORKER), (ALPHA, WORKER)])
            self.assertEqual(first["new_seq"], {HOME: 0, ALPHA: 1})
            self.assertEqual(
                [(event["board_id"], event["ticket_id"]) for event in first["events"]],
                [(ALPHA, "TK-alpha")],
            )
            self.assertNotIn(OMEGA, {board for board, _, _ in central.catchup_calls})
            if mode == "push":
                catchups_after_wake = {
                    board_id: sum(
                        1
                        for called_board, called_name, _ in central.catchup_calls
                        if called_board == board_id and called_name == WORKER
                    )
                    for board_id in (HOME, ALPHA)
                }
                self.assertEqual(
                    catchups_after_wake,
                    {
                        HOME: catchups_before_wake[HOME],
                        ALPHA: catchups_before_wake[ALPHA] + 1,
                    },
                )

            # The worker resolves execution location from the same registry source.
            resolved = await wait_server.project_registry_get(context)
            self.assertEqual(
                resolved["projects"]["alpha"]["work_dir"],
                "/synthetic/work/alpha",
            )

            claimed = await central.claim(ALPHA, "TK-alpha", WORKER)
            claimed_agent_id = central.joined_ids[(ALPHA, WORKER)]
            self.assertEqual(
                claimed["ticket"]["claimed_by_agent_id"], claimed_agent_id
            )

            # Entry snapshots exact-filter held claims; maintenance then renews
            # only the board on which this identity owns a lease.
            home_view = wait_server._BoardView(client, HOME)
            home_join = await home_view.board_join(agent_name=WORKER)
            alpha_view = wait_server._BoardView(client, ALPHA)
            alpha_join = await alpha_view.board_join(agent_name=WORKER)
            for view, agent_id in (
                (home_view, home_join["agent_id"]),
                (alpha_view, alpha_join["agent_id"]),
            ):
                held: dict[str, float] = {}
                await wait_server._scan_open_backlog(
                    view, agent_id, True, None, held
                )
                await wait_server._renew_due_leases(
                    view,
                    held,
                    {ticket_id: 0.0 for ticket_id in held},
                    1.0,
                )
            self.assertEqual(central.renewed, [(ALPHA, "TK-alpha")])

            # A second identity still sees independent home-board work. The
            # first worker's alpha claim event is filtered as somebody else's.
            central.add_open_ticket(HOME, "TK-pursers")
            second = await wait_server.a2a_wait(
                context,
                boards="registry",
                since_seq=first["new_seq"],
                timeout_s=1,
                only_mine=True,
                agent_name=SECOND_WORKER,
            )
            self.assertEqual(
                [(event["board_id"], event["ticket_id"]) for event in second["events"]],
                [(HOME, "TK-pursers")],
            )
            self.assertNotEqual(
                central.joined_ids[(HOME, WORKER)],
                central.joined_ids[(ALPHA, WORKER)],
            )
            self.assertNotEqual(
                central.joined_ids[(HOME, WORKER)],
                central.joined_ids[(HOME, SECOND_WORKER)],
            )
            self.assertNotEqual(
                central.joined_ids[(ALPHA, WORKER)],
                central.joined_ids[(ALPHA, SECOND_WORKER)],
            )

            # Registry membership is fetched once per invocation. Pausing alpha
            # removes it from the next wait without disturbing the live claim.
            central.pause_alpha()
            join_marker = len(central.join_calls)
            third = await wait_server.a2a_wait(
                context,
                boards="registry",
                since_seq=second["new_seq"],
                timeout_s=1,
                only_mine=True,
                agent_name=WORKER,
            )
            self.assertEqual(central.join_calls[join_marker:], [(HOME, WORKER)])
            self.assertEqual(set(third["new_seq"]), {HOME})
            self.assertEqual(central.registry_reads, 4)
            self.assertEqual(central.tickets[ALPHA]["TK-alpha"]["status"], "claimed")
            self.assertEqual(
                central.tickets[ALPHA]["TK-alpha"]["claimed_by_agent_id"],
                claimed_agent_id,
            )

    async def test_registry_pool_loop_wakes_by_poll(self) -> None:
        await self._exercise_phase_one_loop("poll")

    async def test_registry_pool_loop_wakes_by_push_cue(self) -> None:
        await self._exercise_phase_one_loop("push")


if __name__ == "__main__":
    unittest.main()
