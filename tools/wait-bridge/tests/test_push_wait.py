from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    redirect_stderr,
)
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
CLIENT_SRC = REPOSITORY / "packages" / "client" / "src"
CENTRAL_SRC = REPOSITORY / "packages" / "central" / "src" / "pursers_central"
sys.path.insert(0, str(CENTRAL_SRC))
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from mcp import Client  # noqa: E402
from pursers_client import BoardClient, BoardClientError, JoinedIdentity  # noqa: E402
import central  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402


class InProcessBoardClient:
    """Minimal BoardClient-compatible adapter over a real in-process Central."""

    def __init__(self, raw_client: Client) -> None:
        self._raw_client = raw_client
        self._client: Any = raw_client
        self.agent_name = "push-listener"
        self.identity: JoinedIdentity | None = None

    async def _call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = await self._raw_client.call_tool(
            name, {"board_id": wait_server.BOARD_ID, **arguments}
        )
        return BoardClient._decode(result)

    async def board_join(self, *, agent_name: str | None = None) -> dict[str, Any]:
        selected = self.agent_name if agent_name is None else agent_name
        joined = await self._call("board_join", agent_name=selected)
        identity = JoinedIdentity(
            joined["board_id"],
            joined["agent_id"],
            joined["principal_id"],
            joined["agent_name"],
            joined["role"],
        )
        if agent_name is None:
            self.identity = identity
        return joined

    async def board_catchup(self, **arguments: Any) -> dict[str, Any]:
        arguments.setdefault("agent_name", self.agent_name)
        return await self._call("board_catchup", **arguments)

    async def ticket_get(self, ticket_id: str) -> dict[str, Any]:
        return await self._call("ticket_get", ticket_id=ticket_id)

    async def ticket_list(self, **arguments: Any) -> dict[str, Any]:
        return await self._call("ticket_list", **arguments)

    async def lease_renew(self, ticket_id: str) -> dict[str, Any]:
        return await self._call("lease_renew", ticket_id=ticket_id)

    async def create_ticket(self, title: str) -> dict[str, Any]:
        return await self._call(
            "ticket_create",
            agent_name="push-actor",
            title=title,
            description="synthetic wait-bridge push fixture",
            target_url="pursers/tools/wait-bridge",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )

    async def claim_ticket(self, ticket_id: str) -> dict[str, Any]:
        return await self._call(
            "ticket_claim", agent_name="push-actor", ticket_id=ticket_id
        )

    async def submit_ticket(self, ticket_id: str) -> dict[str, Any]:
        return await self._call(
            "ticket_submit",
            agent_name="push-actor",
            ticket_id=ticket_id,
            summary="reviewer wait fixture",
            notes="test_output: PASS",
            files_changed=[],
        )

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
        async with self._client.listen(
            resource_subscriptions=resources
        ) as subscription:
            while True:
                page = await self.board_catchup(
                    cursor=cursor,
                    ack=False,
                    **({"touch": False} if pure_catchup else {}),
                )
                cursor = int(page["next_cursor"])
                cursor_callback(cursor)
                for event in page["events"]:
                    yield event
                if not page.get("has_more"):
                    break
            async for _cue in subscription:
                page = await self.board_catchup(
                    cursor=cursor,
                    ack=False,
                    **({"touch": False} if pure_catchup else {}),
                )
                cursor = int(page["next_cursor"])
                cursor_callback(cursor)
                for event in page["events"]:
                    yield event


class SignalingListenContext(AbstractAsyncContextManager[Any]):
    def __init__(
        self, inner: AbstractAsyncContextManager[Any], ready: asyncio.Event
    ) -> None:
        self.inner = inner
        self.ready = ready

    async def __aenter__(self) -> Any:
        subscription = await self.inner.__aenter__()
        self.ready.set()
        return subscription

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return await self.inner.__aexit__(exc_type, exc, traceback)


class SignalingListenClient:
    def __init__(self, raw_client: Client, ready: asyncio.Event) -> None:
        self.raw_client = raw_client
        self.ready = ready
        self.listen_calls = 0

    def listen(self, **arguments: Any) -> SignalingListenContext:
        self.listen_calls += 1
        return SignalingListenContext(
            self.raw_client.listen(**arguments), self.ready
        )


class UnavailableListenContext(AbstractAsyncContextManager[Any]):
    def __init__(self, attempted: asyncio.Event) -> None:
        self.attempted = attempted

    async def __aenter__(self) -> Any:
        self.attempted.set()
        raise RuntimeError("synthetic subscriptions/listen unavailable")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class UnavailableListenClient:
    def __init__(self, attempted: asyncio.Event) -> None:
        self.attempted = attempted
        self.listen_calls = 0

    def listen(self, **_arguments: Any) -> UnavailableListenContext:
        self.listen_calls += 1
        return UnavailableListenContext(self.attempted)


class ForbiddenListenClient:
    def __init__(self) -> None:
        self.listen_calls = 0

    def listen(self, **_arguments: Any) -> AbstractAsyncContextManager[Any]:
        self.listen_calls += 1
        raise AssertionError("poll mode must not call listen")


class StubSubscription:
    """Offline subscription stream whose values are wake cues, not events."""

    def __init__(self, cues: list[object], honored_uri: str) -> None:
        self._cues = iter(cues)
        self.yielded = 0
        self.honored = SimpleNamespace(resource_subscriptions=[honored_uri])

    def __aiter__(self) -> StubSubscription:
        return self

    async def __anext__(self) -> object:
        try:
            cue = next(self._cues)
        except StopIteration:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        self.yielded += 1
        return cue


class StubListenContext(AbstractAsyncContextManager[StubSubscription]):
    def __init__(self, subscription: StubSubscription) -> None:
        self.subscription = subscription

    async def __aenter__(self) -> StubSubscription:
        return self.subscription

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class StubListenClient:
    def __init__(self, subscription: StubSubscription) -> None:
        self.subscription = subscription
        self.calls: list[dict[str, object]] = []

    def listen(self, **arguments: object) -> StubListenContext:
        self.calls.append(arguments)
        return StubListenContext(self.subscription)


class ScriptedBoardClient:
    """Small BoardClient stub for deterministic wait-path assertions."""

    def __init__(
        self,
        catchups: list[tuple[list[dict[str, object]], int]],
        *,
        tickets: list[dict[str, object]] | None = None,
        transport: object | None = None,
    ) -> None:
        self.identity = SimpleNamespace(principal_id="PR-scripted")
        self._catchups = list(catchups)
        self._last_cursor = 0
        self._tickets = list(tickets or [])
        self._client = transport
        self.catchup_calls = 0
        self.ticket_list_calls = 0

    async def board_catchup(self, **arguments: object) -> dict[str, object]:
        self.catchup_calls += 1
        if self._catchups:
            events, self._last_cursor = self._catchups.pop(0)
        else:
            events = []
            self._last_cursor = int(arguments.get("cursor", self._last_cursor))
        return {
            "events": events,
            "next_cursor": self._last_cursor,
            "has_more": False,
            "resync_required": False,
        }

    async def ticket_list(self, **_arguments: object) -> dict[str, object]:
        self.ticket_list_calls += 1
        return {"tickets": self._tickets}

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
        async with self._client.listen(
            resource_subscriptions=resources
        ) as subscription:
            page = await self.board_catchup(
                cursor=from_cursor,
                ack=False,
                **({"touch": False} if pure_catchup else {}),
            )
            cursor_callback(int(page["next_cursor"]))
            for event in page["events"]:
                yield event
            async for _cue in subscription:
                page = await self.board_catchup(
                    cursor=int(page["next_cursor"]),
                    ack=False,
                    **({"touch": False} if pure_catchup else {}),
                )
                cursor_callback(int(page["next_cursor"]))
                for event in page["events"]:
                    yield event


class ManualClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


class PushWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        wait_server._BACKLOG_SEEN.clear()
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
        self.mcp, _service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.principal = central.Principal(
            "PR-push-test",
            "push-test-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal

    def tearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def _joined_client(
        self, raw_client: Client
    ) -> InProcessBoardClient:
        client = InProcessBoardClient(raw_client)
        await client.board_join()
        await client.board_join(agent_name="push-actor")
        return client

    async def test_push_task_stops_after_first_real_cue_without_cleanup_error(
        self,
    ) -> None:
        loop = asyncio.get_running_loop()
        cleanup_errors: list[dict[str, Any]] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: cleanup_errors.append(context)
        )
        try:
            async with Client(self.mcp, mode="2026-07-28", cache=None) as raw_client:
                client = await self._joined_client(raw_client)
                created = await client.create_ticket("early exit cleanup")
                ticket_id = created["ticket"]["ticket_id"]
                assert client.identity is not None
                view = SimpleNamespace(
                    _parent=client,
                    identity=client.identity,
                    generation_token=None,
                    _pursers_pure_catchup=True,
                )
                queue: asyncio.Queue[
                    tuple[str, str, dict[str, Any] | str | None]
                ] = asyncio.Queue()
                running = asyncio.create_task(
                    wait_server._push_cues(
                        wait_server.BOARD_ID, view, 0, queue
                    )
                )
                event: dict[str, Any] | None = None
                while event is None:
                    _board_id, kind, detail = await asyncio.wait_for(
                        queue.get(), timeout=2
                    )
                    if kind == "event" and isinstance(detail, dict):
                        event = detail
                self.assertEqual(event["ticket_id"], ticket_id)

                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        self.assertEqual(cleanup_errors, [])

    async def test_real_board_client_early_close_uses_same_task_for_listen_exit(
        self,
    ) -> None:
        import pursers_client.client as client_module

        @asynccontextmanager
        async def http_context():
            yield object()

        board = BoardClient(
            "http://central.invalid/mcp",
            "test-token",
            wait_server.BOARD_ID,
            agent_name="push-listener",
        )
        board._http = http_context  # type: ignore[method-assign]
        with patch.object(
            client_module, "streamable_http_client", return_value=self.mcp
        ):
            async with board:
                await board.board_join()
                async with Client(
                    self.mcp, mode="2026-07-28", cache=None
                ) as actor:
                    await actor.call_tool(
                        "board_join",
                        {
                            "board_id": wait_server.BOARD_ID,
                            "agent_name": "push-actor",
                        },
                    )
                    created = BoardClient._decode(
                        await actor.call_tool(
                            "ticket_create",
                            {
                                "board_id": wait_server.BOARD_ID,
                                "agent_name": "push-actor",
                                "title": "real listen early exit",
                                "description": "exercise production BoardClient.events",
                                "target_url": "pursers/tools/wait-bridge",
                                "scope": "interactive-no-send",
                                "required_fields": ["test_output"],
                            },
                        )
                    )

                events = board.events(
                    from_cursor=0,
                    only_mine=False,
                    resource_subscriptions=(
                        f"board://{wait_server.BOARD_ID}/journal",
                    ),
                    acknowledge=False,
                    touch=False,
                )
                event = await asyncio.wait_for(anext(events), timeout=2)
                self.assertEqual(
                    event["ticket_id"], created["ticket"]["ticket_id"]
                )
                close_result = await asyncio.gather(
                    asyncio.create_task(events.aclose()),
                    return_exceptions=True,
                )
                self.assertEqual(close_result, [None])

    def test_host_profiles_apply_timeout_minus_margin(self) -> None:
        cases = {
            "codex": 560,
            "codex-cli": 560,
            "goose": 270,
            "claude-code": 21_540,
            "claude-desktop": 200,
            "headless": 21_540,
        }
        for host, expected in cases.items():
            with self.subTest(host=host), patch.dict(
                os.environ,
                {"PURSERS_HOST": host, "PURSERS_HOST_TIMEOUT_S": ""},
            ):
                self.assertEqual(wait_server.clamp_timeout(999_999), expected)
        with patch.dict(
            os.environ,
            {"PURSERS_HOST": "goose", "PURSERS_HOST_TIMEOUT_S": "3600"},
        ):
            self.assertEqual(wait_server.clamp_timeout(999_999), 3540)

    def test_claude_code_enables_five_minute_progress_cadence(self) -> None:
        with patch.dict(os.environ, {"PURSERS_HOST": "claude-code"}):
            self.assertEqual(
                wait_server._progress_cadence_s(),
                wait_server.PROGRESS_INTERVAL_S,
            )
        with patch.dict(os.environ, {"PURSERS_HOST": "codex"}):
            self.assertIsNone(wait_server._progress_cadence_s())

    async def test_push_wakes_on_new_ticket_and_refetches_event(self) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw:
            client = await self._joined_client(raw)
            ready = asyncio.Event()
            signaling = SignalingListenClient(raw, ready)
            client._client = signaling

            stderr = io.StringIO()
            with (
                redirect_stderr(stderr),
                patch.object(wait_server, "WAIT_MODE", "push"),
                patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 10.0),
            ):
                started = time.monotonic()
                waiting = asyncio.create_task(
                    wait_server._wait_for_work(
                        client,
                        since_seq=0,
                        timeout_s=2,
                        only_mine=False,
                        project="pursers",
                        wait_for="claimable",
                    )
                )
                await asyncio.wait_for(ready.wait(), timeout=1)
                created = await client.create_ticket("push wake fixture")
                result = await asyncio.wait_for(waiting, timeout=1)

            self.assertEqual(signaling.listen_calls, 1)
            self.assertNotIn("falling back to poll", stderr.getvalue())
            self.assertFalse(result["timed_out"])
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(
                any(
                    event.get("ticket_id")
                    == created["ticket"]["ticket_id"]
                    for event in result["events"]
                )
            )

    async def test_reviewer_real_listen_ignores_open_and_wakes_on_submit(self) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw:
            client = await self._joined_client(raw)
            open_ticket = await client.create_ticket("open reviewer fixture")
            ready = asyncio.Event()
            signaling = SignalingListenClient(raw, ready)
            client._client = signaling

            with patch.object(wait_server, "WAIT_MODE", "push"):
                waiting = asyncio.create_task(
                    wait_server._wait_for_work(
                        client,
                        since_seq=0,
                        timeout_s=2,
                        only_mine=False,
                        project="pursers",
                    )
                )
                await asyncio.wait_for(ready.wait(), timeout=1)
                submitted = await client.create_ticket("submitted reviewer fixture")
                ticket_id = submitted["ticket"]["ticket_id"]
                await client.claim_ticket(ticket_id)
                await client.submit_ticket(ticket_id)
                result = await asyncio.wait_for(waiting, timeout=1)

            self.assertEqual(client.identity.role, "reviewer")
            self.assertEqual(result["reason"], "journal")
            self.assertFalse(result["timed_out"])
            self.assertEqual(
                [event.get("ticket_id") for event in result["events"]],
                [ticket_id],
            )
            self.assertNotEqual(ticket_id, open_ticket["ticket"]["ticket_id"])

    async def test_push_timeout_is_honored(self) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw:
            client = await self._joined_client(raw)
            ready = asyncio.Event()
            signaling = SignalingListenClient(raw, ready)
            client._client = signaling

            stderr = io.StringIO()
            with (
                redirect_stderr(stderr),
                patch.object(wait_server, "WAIT_MODE", "push"),
            ):
                started = time.monotonic()
                waiting = asyncio.create_task(
                    wait_server._wait_for_work(
                        client,
                        since_seq=0,
                        timeout_s=1,
                        only_mine=False,
                        project="pursers",
                        wait_for="claimable",
                    )
                )
                await asyncio.wait_for(ready.wait(), timeout=1)
                result = await asyncio.wait_for(waiting, timeout=1.5)
                elapsed = time.monotonic() - started

            self.assertEqual(signaling.listen_calls, 1)
            self.assertNotIn("falling back to poll", stderr.getvalue())
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["events"], [])
            self.assertGreaterEqual(elapsed, 0.9)
            self.assertLess(elapsed, 1.5)

    async def test_unavailable_listen_falls_back_to_poll(self) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw:
            client = await self._joined_client(raw)
            attempted = asyncio.Event()
            unavailable = UnavailableListenClient(attempted)
            client._client = unavailable

            with (
                patch.object(wait_server, "WAIT_MODE", "push"),
                patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.02),
            ):
                waiting = asyncio.create_task(
                    wait_server._wait_for_work(
                        client,
                        since_seq=0,
                        timeout_s=2,
                        only_mine=False,
                        project="pursers",
                        wait_for="claimable",
                    )
                )
                await asyncio.wait_for(attempted.wait(), timeout=1)
                created = await client.create_ticket("poll fallback fixture")
                result = await asyncio.wait_for(waiting, timeout=1)

            self.assertEqual(unavailable.listen_calls, 1)
            self.assertFalse(result["timed_out"])
            self.assertTrue(
                any(
                    event.get("ticket_id")
                    == created["ticket"]["ticket_id"]
                    for event in result["events"]
                )
            )

    async def test_poll_mode_never_opens_subscription(self) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw:
            client = await self._joined_client(raw)
            forbidden = ForbiddenListenClient()
            client._client = forbidden

            with (
                patch.object(wait_server, "WAIT_MODE", "poll"),
                patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.02),
            ):
                waiting = asyncio.create_task(
                    wait_server._wait_for_work(
                        client,
                        since_seq=0,
                        timeout_s=2,
                        only_mine=False,
                        project="pursers",
                        wait_for="claimable",
                    )
                )
                await asyncio.sleep(0.05)
                created = await client.create_ticket("poll mode fixture")
                result = await asyncio.wait_for(waiting, timeout=1)

            self.assertEqual(forbidden.listen_calls, 0)
            self.assertFalse(result["timed_out"])
            self.assertTrue(
                any(
                    event.get("ticket_id")
                    == created["ticket"]["ticket_id"]
                    for event in result["events"]
                )
            )

    async def test_push_subscribes_to_journal_and_per_seat_uri(self) -> None:
        journal_uri = f"board://{wait_server.BOARD_ID}/journal"
        subscription = StubSubscription(
            [
                {"uri": "board://pursers/ticket/TK-cue-only"},
                {"uri": "board://pursers/ticket/TK-cue-only"},
                {"uri": "board://pursers/ticket/TK-cue-only"},
            ],
            journal_uri,
        )
        transport = StubListenClient(subscription)
        authoritative = [
            {"seq": 51, "kind": "ticket_created", "ticket_id": "TK-one"},
            {"seq": 52, "kind": "ticket_created", "ticket_id": "TK-two"},
        ]
        client = ScriptedBoardClient(
            [([], 50), ([], 50), (authoritative, 52)], transport=transport
        )

        with patch.object(wait_server, "WAIT_MODE", "push"):
            result = await wait_server._wait_for_work(
                client,
                since_seq=50,
                timeout_s=2,
                only_mine=False,
                project=None,
            )

        agent_id = wait_server._derived_agent_id("PR-scripted", wait_server.AGENT_NAME)
        self.assertEqual(transport.calls, [{"resource_subscriptions": [
            journal_uri,
            f"board://pursers/agent/{agent_id}",
        ]}])
        self.assertNotIn("board://pursers/ticket/TK-cue-only", str(transport.calls))
        self.assertEqual(result["events"], authoritative)
        self.assertEqual(result["new_seq"], 52)
        self.assertEqual(
            [event["ticket_id"] for event in result["events"]],
            ["TK-one", "TK-two"],
        )
        self.assertGreaterEqual(subscription.yielded, 1)
        self.assertGreaterEqual(client.catchup_calls, 3)

    async def test_seat_denial_retries_journal_only_before_poll(self) -> None:
        journal_uri = f"board://{wait_server.BOARD_ID}/journal"
        seat_uri = "board://pursers/agent/AI-listener"
        calls: list[list[str]] = []
        event = {"seq": 61, "kind": "ticket_created", "ticket_id": "TK-journal"}

        class RetryEventClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.identity: JoinedIdentity | None = None
                self.generation_token: str | None = None

            async def events(self, **arguments: Any) -> Any:
                resources = list(arguments["resource_subscriptions"])
                calls.append(resources)
                if len(resources) > 1:
                    raise BoardClientError(
                        "subscription denied: principal is not a board member"
                    )
                yield event

        parent = SimpleNamespace(
            url="http://central.invalid/mcp",
            token="test-token",
            reconnect_delay_s=0.01,
        )
        identity = JoinedIdentity(
            wait_server.BOARD_ID,
            "AI-listener",
            "PR-listener",
            "push-listener",
            "worker",
        )
        stderr = io.StringIO()
        with (
            redirect_stderr(stderr),
            patch.object(wait_server, "BoardClient", RetryEventClient),
        ):
            found = [
                item
                async for item in wait_server._event_stream(
                    parent,
                    wait_server.BOARD_ID,
                    identity,
                    None,
                    60,
                    lambda _cursor: None,
                    pure_catchup=True,
                )
            ]

        self.assertEqual(calls, [[journal_uri, seat_uri], [journal_uri]])
        self.assertEqual(found, [event])
        self.assertIn("retrying journal-only", stderr.getvalue())
        self.assertNotIn("falling back to poll", stderr.getvalue())

    async def test_push_backlog_scan_precedes_subscription(self) -> None:
        ticket = {
            "ticket_id": "TK-before-cursor",
            "status": "open",
            "target_url": "pursers/tools/wait-bridge",
            "created_by_agent_id": "AI-other",
            "claimed_by_agent_id": None,
            "assigned_to_agent_id": None,
        }
        forbidden = ForbiddenListenClient()
        client = ScriptedBoardClient(
            [([], 80)], tickets=[ticket], transport=forbidden
        )

        with patch.object(wait_server, "WAIT_MODE", "push"):
            result = await wait_server._wait_for_work(
                client,
                since_seq=80,
                timeout_s=2,
                only_mine=True,
                project="pursers",
            )

        self.assertEqual(client.ticket_list_calls, 1)
        self.assertEqual(forbidden.listen_calls, 0)
        self.assertEqual(
            result["events"],
            [
                {
                    "kind": "ticket_backlog",
                    "source": "backlog_scan",
                    "ticket_id": "TK-before-cursor",
                    "status": "open",
                }
            ],
        )
        self.assertEqual(result["new_seq"], 80)
        self.assertFalse(result["timed_out"])

    async def test_subscription_failure_is_byte_identical_to_poll_mode(self) -> None:
        event = {"seq": 91, "kind": "ticket_created", "ticket_id": "TK-fallback"}

        async def run(mode: str) -> tuple[dict[str, object], int]:
            clock = ManualClock()
            attempted = asyncio.Event()
            transport: object
            if mode == "push":
                transport = UnavailableListenClient(attempted)
            else:
                transport = ForbiddenListenClient()
            client = ScriptedBoardClient(
                [([], 90), ([event], 91)], transport=transport
            )
            with (
                patch.object(wait_server, "WAIT_MODE", mode),
                patch.object(wait_server, "DEFAULT_POLL_INTERVAL_S", 0.25),
                patch.object(wait_server.time, "monotonic", clock.monotonic),
                patch.object(wait_server.asyncio, "sleep", clock.sleep),
            ):
                result = await wait_server._wait_for_work(
                    client,
                    since_seq=90,
                    timeout_s=2,
                    only_mine=False,
                    project=None,
                )
            return result, getattr(transport, "listen_calls")

        push_result, push_listens = await run("push")
        poll_result, poll_listens = await run("poll")

        self.assertEqual(push_result, poll_result)
        self.assertEqual(
            push_result,
            {
                "new_seq": 91,
                "events": [event],
                "waited_s": 0.25,
                "timed_out": False,
                "reason": "journal",
                "resynced": False,
            },
        )
        self.assertEqual(push_listens, 1)
        self.assertEqual(poll_listens, 0)


if __name__ == "__main__":
    unittest.main()
