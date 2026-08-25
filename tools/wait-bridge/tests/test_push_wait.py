from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import TracebackType
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
from pursers_client import BoardClient, JoinedIdentity  # noqa: E402
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


class PushWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
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

    async def test_push_wakes_on_new_ticket_and_refetches_event(self) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw:
            client = await self._joined_client(raw)
            ready = asyncio.Event()
            signaling = SignalingListenClient(raw, ready)
            client._client = signaling

            with (
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
                    )
                )
                await asyncio.wait_for(ready.wait(), timeout=1)
                created = await client.create_ticket("push wake fixture")
                result = await asyncio.wait_for(waiting, timeout=1)

            self.assertEqual(signaling.listen_calls, 1)
            self.assertFalse(result["timed_out"])
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(
                any(
                    event.get("ticket_id")
                    == created["ticket"]["ticket_id"]
                    for event in result["events"]
                )
            )

    async def test_push_timeout_is_honored(self) -> None:
        async with Client(self.mcp, mode="2026-07-28", cache=None) as raw:
            client = await self._joined_client(raw)
            ready = asyncio.Event()
            signaling = SignalingListenClient(raw, ready)
            client._client = signaling

            with patch.object(wait_server, "WAIT_MODE", "push"):
                started = time.monotonic()
                waiting = asyncio.create_task(
                    wait_server._wait_for_work(
                        client,
                        since_seq=0,
                        timeout_s=1,
                        only_mine=False,
                        project="pursers",
                    )
                )
                await asyncio.wait_for(ready.wait(), timeout=1)
                result = await asyncio.wait_for(waiting, timeout=1.5)
                elapsed = time.monotonic() - started

            self.assertEqual(signaling.listen_calls, 1)
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


if __name__ == "__main__":
    unittest.main()
