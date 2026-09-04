"""Per-call BoardClient identity must not mutate shared client state."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from pursers_client import BoardClient, JoinedIdentity


def joined(name: str) -> dict[str, Any]:
    return {
        "board_id": "board-multi-name",
        "agent_id": f"AI-{name}",
        "principal_id": "PR-shared",
        "agent_name": name,
        "role": "worker",
        "generation_token": "generation-from-server",
    }


def client() -> BoardClient:
    return BoardClient(
        "https://central.example/mcp",
        "TOKEN_PLACEHOLDER",
        "board-multi-name",
        agent_name="env-default",
    )


@pytest.mark.anyio
async def test_default_board_join_preserves_existing_behavior(monkeypatch) -> None:
    board = client()
    captured: dict[str, Any] = {}

    async def call_refresh(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        captured.update({"tool": name, "arguments": arguments})
        return joined(arguments["agent_name"])

    monkeypatch.setattr(board, "_call_refresh", call_refresh)

    result = await board.board_join()

    assert captured == {
        "tool": "board_join",
        "arguments": {"agent_name": "env-default"},
    }
    assert result["agent_name"] == "env-default"
    assert board.identity == JoinedIdentity(
        "board-multi-name", "AI-env-default", "PR-shared", "env-default", "worker"
    )
    assert "identity" not in result


@pytest.mark.anyio
async def test_explicit_board_join_returns_identity_without_mutation(monkeypatch) -> None:
    board = client()
    original_identity = JoinedIdentity(
        "board-multi-name", "AI-env-default", "PR-shared", "env-default", "worker"
    )
    board.identity = original_identity
    board.generation_token = "generation-before-call"
    captured: dict[str, Any] = {}

    async def call_uncached(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        captured.update({"tool": name, "arguments": arguments})
        return joined(arguments["agent_name"])

    monkeypatch.setattr(board, "_call_refresh_uncached", call_uncached)

    result = await board.board_join(agent_name="session-x")

    assert captured == {
        "tool": "board_join",
        "arguments": {"agent_name": "session-x"},
    }
    assert result["identity"] == JoinedIdentity(
        "board-multi-name", "AI-session-x", "PR-shared", "session-x", "worker"
    )
    assert board.agent_name == "env-default"
    assert board.identity is original_identity
    assert board.generation_token == "generation-before-call"


@pytest.mark.anyio
async def test_board_catchup_uses_explicit_or_default_name(monkeypatch) -> None:
    board = client()
    calls: list[dict[str, Any]] = []

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "board_catchup"
        calls.append(arguments)
        return {"events": [], "next_cursor": arguments["cursor"]}

    monkeypatch.setattr(board, "_call", call)

    await board.board_catchup(cursor=1, agent_name="session-x")
    await board.board_catchup(cursor=2)

    assert [item["agent_name"] for item in calls] == ["session-x", "env-default"]
    assert board.agent_name == "env-default"


@pytest.mark.anyio
async def test_bounded_read_parameters_are_forwarded(monkeypatch) -> None:
    board = client()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        return {"tickets": [], "events": []}

    monkeypatch.setattr(board, "_call", call)

    await board.board_snapshot(limit=50, max_bytes=200_000)
    await board.board_catchup(
        cursor=4,
        limit=20,
        ack=False,
        max_events=20,
        max_bytes=100_000,
        touch=False,
    )

    assert calls == [
        ("board_snapshot", {"limit": 50, "max_bytes": 200_000}),
        (
            "board_catchup",
            {
                "agent_name": "env-default",
                "cursor": 4,
                "limit": 20,
                "ack": False,
                "max_events": 20,
                "max_bytes": 100_000,
                "touch": False,
            },
        ),
    ]


@pytest.mark.anyio
async def test_interleaved_per_call_joins_do_not_clobber_state(monkeypatch) -> None:
    board = client()
    original_identity = JoinedIdentity(
        "board-multi-name", "AI-env-default", "PR-shared", "env-default", "worker"
    )
    board.identity = original_identity
    board.generation_token = "generation-before-call"
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    sent_names: list[str] = []

    async def call_uncached(_tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        name = arguments["agent_name"]
        sent_names.append(name)
        if name == "session-a":
            first_entered.set()
            await second_entered.wait()
        else:
            await first_entered.wait()
            second_entered.set()
        return joined(name)

    monkeypatch.setattr(board, "_call_refresh_uncached", call_uncached)

    first, second = await asyncio.gather(
        board.board_join(agent_name="session-a"),
        board.board_join(agent_name="session-b"),
    )

    assert set(sent_names) == {"session-a", "session-b"}
    assert first["identity"].agent_name == "session-a"
    assert second["identity"].agent_name == "session-b"
    assert board.agent_name == "env-default"
    assert board.identity is original_identity
    assert board.generation_token == "generation-before-call"


@pytest.mark.anyio
async def test_events_reports_each_honored_subscription_handshake(
    monkeypatch,
) -> None:
    import pursers_client.client as client_module

    board = client()
    journal_uri = "board://board-multi-name/journal"
    ready = asyncio.Event()
    hold = asyncio.Event()

    @asynccontextmanager
    async def context(value):
        yield value

    class Subscription:
        honored = SimpleNamespace(resource_subscriptions=[journal_uri])

        def __aiter__(self):
            return self

        async def __anext__(self):
            await hold.wait()
            raise StopAsyncIteration

    class Session:
        def listen(self, **_arguments):
            return context(Subscription())

    async def drain(*_args, **_kwargs):
        if False:
            yield {}

    monkeypatch.setattr(board, "_http", lambda: context(object()))
    monkeypatch.setattr(board, "_drain", drain)
    monkeypatch.setattr(
        client_module, "streamable_http_client", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        client_module,
        "Client",
        lambda *_args, **_kwargs: context(Session()),
    )

    events = board.events(
        from_cursor=4,
        only_mine=False,
        resource_subscriptions=(journal_uri,),
        acknowledge=False,
        touch=False,
        subscription_callback=ready.set,
    )
    pending = asyncio.create_task(anext(events))
    await asyncio.wait_for(ready.wait(), timeout=1)
    assert not pending.done()
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    await events.aclose()


@pytest.mark.anyio
async def test_events_early_close_exits_listen_scopes_in_producer_task(
    monkeypatch,
) -> None:
    import pursers_client.client as client_module

    board = client()
    journal_uri = "board://board-multi-name/journal"
    scope_tasks: list[tuple[asyncio.Task[Any] | None, asyncio.Task[Any] | None]] = []

    @asynccontextmanager
    async def task_bound_context(value):
        entered = asyncio.current_task()
        try:
            yield value
        finally:
            exited = asyncio.current_task()
            scope_tasks.append((entered, exited))
            if exited is not entered:
                raise RuntimeError(
                    "Attempted to exit cancel scope in a different task than it was entered in"
                )

    class Subscription:
        honored = SimpleNamespace(resource_subscriptions=[journal_uri])

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()
            raise StopAsyncIteration

    class Session:
        def listen(self, **_arguments):
            return task_bound_context(Subscription())

    async def drain(*_args, **_kwargs):
        yield {"id": "EV-5", "seq": 5, "kind": "ticket_created"}

    monkeypatch.setattr(board, "_http", lambda: task_bound_context(object()))
    monkeypatch.setattr(board, "_drain", drain)
    monkeypatch.setattr(
        client_module, "streamable_http_client", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        client_module,
        "Client",
        lambda *_args, **_kwargs: task_bound_context(Session()),
    )

    events = board.events(
        from_cursor=4,
        only_mine=False,
        resource_subscriptions=(journal_uri,),
        acknowledge=False,
        touch=False,
    )
    assert (await anext(events))["id"] == "EV-5"
    await asyncio.create_task(events.aclose())

    assert len(scope_tasks) == 3
    assert all(entered is exited for entered, exited in scope_tasks)
