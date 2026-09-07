"""Per-call BoardClient identity must not mutate shared client state."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from pursers_client import BoardClient, BoardClientError, JoinedIdentity


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
        "arguments": {"agent_name": "env-default", "role": "worker"},
    }
    assert result["agent_name"] == "env-default"
    assert board.identity == JoinedIdentity(
        "board-multi-name", "AI-env-default", "PR-shared", "env-default", "worker"
    )
    assert "identity" not in result


@pytest.mark.anyio
async def test_board_join_can_defer_role_to_central(monkeypatch) -> None:
    board = BoardClient(
        "https://central.example/mcp",
        "TOKEN_PLACEHOLDER",
        "board-multi-name",
        agent_name="membership-defaulted",
        role=None,
    )
    captured: dict[str, Any] = {}

    async def call_refresh(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        captured.update({"tool": name, "arguments": arguments})
        return {**joined(arguments["agent_name"]), "role": "reviewer"}

    monkeypatch.setattr(board, "_call_refresh", call_refresh)

    result = await board.board_join()

    assert captured == {
        "tool": "board_join",
        "arguments": {"agent_name": "membership-defaulted"},
    }
    assert result["role"] == "reviewer"
    assert board.identity is not None
    assert board.identity.role == "reviewer"


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
        "arguments": {"agent_name": "session-x", "role": "worker"},
    }
    assert result["identity"] == JoinedIdentity(
        "board-multi-name", "AI-session-x", "PR-shared", "session-x", "worker"
    )
    assert board.agent_name == "env-default"
    assert board.identity is original_identity
    assert board.generation_token == "generation-before-call"


@pytest.mark.anyio
async def test_declared_role_is_forwarded_for_join_and_onboard(monkeypatch) -> None:
    board = BoardClient(
        "https://central.example/mcp",
        "TOKEN_PLACEHOLDER",
        "board-multi-name",
        agent_name="review-seat",
        role="reviewer",
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call_refresh(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        return {**joined(arguments["agent_name"]), "role": arguments["role"]}

    monkeypatch.setattr(board, "_call_refresh", call_refresh)
    await board.board_join()
    await board.board_onboard(role="worker")

    assert calls[0][1]["role"] == "reviewer"
    assert calls[1][1]["role"] == "worker"


@pytest.mark.anyio
async def test_takeover_and_memory_identity_are_forwarded(monkeypatch) -> None:
    board = client()
    refresh_calls: list[tuple[str, dict[str, Any]]] = []
    read_calls: list[tuple[str, dict[str, Any]]] = []

    async def call_refresh(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        refresh_calls.append((name, arguments))
        return joined(arguments["agent_name"])

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        read_calls.append((name, arguments))
        return {"results": [], "nodes": [], "edges": []}

    monkeypatch.setattr(board, "_call_refresh", call_refresh)
    monkeypatch.setattr(board, "_call", call)

    await board.board_join(allow_takeover=True)
    await board.board_onboard(allow_takeover=True)
    await board.memory_search("private")
    await board.memory_links()

    assert refresh_calls[0][1]["allow_takeover"] is True
    assert refresh_calls[1][1]["allow_takeover"] is True
    assert read_calls[0][1]["agent_name"] == "env-default"
    assert read_calls[1][1]["agent_name"] == "env-default"


@pytest.mark.anyio
@pytest.mark.parametrize("allow_takeover", [False, True])
async def test_context_startup_forwards_explicit_takeover_policy(
    monkeypatch, allow_takeover: bool
) -> None:
    import pursers_client.client as client_module

    @asynccontextmanager
    async def context(value):
        yield value

    board = BoardClient(
        "https://central.example/mcp",
        "TOKEN_PLACEHOLDER",
        "board-multi-name",
        agent_name="stable-seat",
        allow_takeover=allow_takeover,
    )
    captured: dict[str, Any] = {}

    async def board_join(
        _claim_ttl_s: int | None = None,
        *,
        capabilities: dict[str, Any] | None = None,
        allow_takeover: bool = False,
    ) -> dict[str, Any]:
        captured.update(
            capabilities=capabilities,
            allow_takeover=allow_takeover,
        )
        return joined("stable-seat")

    monkeypatch.setattr(board, "_http", lambda: context(object()))
    monkeypatch.setattr(board, "board_join", board_join)
    monkeypatch.setattr(
        client_module, "streamable_http_client", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        client_module, "Client", lambda *_args, **_kwargs: context(object())
    )

    async with board:
        pass

    assert captured == {
        "capabilities": None,
        "allow_takeover": allow_takeover,
    }


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
    await board.board_dispatch_events(limit=25)
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
        ("board_dispatch_events", {"limit": 25}),
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
async def test_ticket_annotate_forwards_identity_kind_and_remembers_event(
    monkeypatch,
) -> None:
    board = client()
    calls: list[tuple[str, dict[str, Any]]] = []
    remembered: list[dict[str, Any]] = []

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        return {"ok": True, "event": {"seq": 17}}

    monkeypatch.setattr(board, "_call", call)
    monkeypatch.setattr(board, "_remember_event", remembered.append)

    result = await board.ticket_annotate(
        "TK-annotation", "operator result", kind="evidence"
    )

    assert result["ok"] is True
    assert calls == [
        (
            "ticket_annotate",
            {
                "agent_name": "env-default",
                "ticket_id": "TK-annotation",
                "text": "operator result",
                "kind": "evidence",
            },
        )
    ]
    assert remembered == [result]


@pytest.mark.anyio
async def test_ticket_update_forwards_parked(monkeypatch) -> None:
    board = client()
    captured: dict[str, Any] = {}

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        captured.update({"tool": name, "arguments": arguments})
        return {"ok": True}

    monkeypatch.setattr(board, "_call", call)
    await board.ticket_update("TK-parked", parked=True)

    assert captured == {
        "tool": "ticket_update",
        "arguments": {
            "agent_name": "env-default",
            "ticket_id": "TK-parked",
            "parked": True,
        },
    }


def test_tool_error_text_is_preserved_verbatim() -> None:
    message = "ticket is not offered to this seat; wait for your offer"
    result = SimpleNamespace(
        is_error=True,
        content=[SimpleNamespace(text=f"Error executing tool ticket_claim: {message}")],
    )

    with pytest.raises(BoardClientError, match=message):
        BoardClient._decode(result)


@pytest.mark.anyio
async def test_ticket_submit_truncates_notes_at_line_boundary(monkeypatch) -> None:
    board = client()
    captured: dict[str, Any] = {}

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        captured.update({"tool": name, "arguments": arguments})
        return {"ok": True}

    monkeypatch.setattr(board, "_call", call)
    notes = "\n".join(f"line-{index:03d}-" + "x" * 90 for index in range(60))

    with pytest.warns(RuntimeWarning, match="exceeded 5000 characters"):
        result = await board.ticket_submit("TK-long-notes", notes=notes)

    submitted = captured["arguments"]["notes"]
    metadata = result["input_truncation"]["notes"]
    assert captured["tool"] == "ticket_submit"
    assert len(submitted) <= 5_000
    assert submitted.endswith(metadata["marker"])
    assert submitted[: -len(metadata["marker"])].endswith("\n")
    assert metadata == {
        "field": "notes",
        "limit": 5_000,
        "original_chars": len(notes),
        "submitted_chars": len(submitted),
        "truncated_chars": metadata["truncated_chars"],
        "marker": f"…[truncated {metadata['truncated_chars']} chars]",
    }


@pytest.mark.anyio
async def test_review_lease_calls_carry_the_seat_identity(monkeypatch) -> None:
    board = client()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        return {"ok": True}

    monkeypatch.setattr(board, "_call", call)

    await board.ticket_review_claim("TK-review")
    await board.lease_renew("TK-review")
    await board.ticket_review_release("TK-review", reason="handoff")
    await board.ticket_list(status="submitted", review_unclaimed_only=True)

    assert calls == [
        (
            "ticket_review_claim",
            {"agent_name": "env-default", "ticket_id": "TK-review"},
        ),
        (
            "lease_renew",
            {"agent_name": "env-default", "ticket_id": "TK-review"},
        ),
        (
            "ticket_review_release",
            {
                "agent_name": "env-default",
                "ticket_id": "TK-review",
                "reason": "handoff",
            },
        ),
        (
            "ticket_list",
            {
                "agent_name": "env-default",
                "include_closed": False,
                "limit": 100,
                "review_unclaimed_only": True,
                "status": "submitted",
            },
        ),
    ]


@pytest.mark.anyio
async def test_claim_ttl_set_carries_admin_seat_identity(monkeypatch) -> None:
    board = client()
    captured: dict[str, Any] = {}

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        captured.update({"tool": name, "arguments": arguments})
        return {"ok": True, "claim_ttl_s": 120}

    monkeypatch.setattr(board, "_call", call)
    result = await board.board_claim_ttl_set(120)

    assert result["claim_ttl_s"] == 120
    assert captured == {
        "tool": "board_claim_ttl_set",
        "arguments": {"agent_name": "env-default", "claim_ttl_s": 120},
    }


@pytest.mark.anyio
async def test_agent_lifecycle_arguments_are_forwarded(monkeypatch) -> None:
    board = client()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        return {"ok": True}

    monkeypatch.setattr(board, "_call", call)
    await board.board_snapshot(include_retired=True)
    await board.board_status(include_retired=True)
    await board.agent_retire("AI-target")
    await board.agent_retire_inert()
    await board.board_stale_after_set(7)

    assert calls == [
        ("board_snapshot", {"include_retired": True}),
        ("board_status", {"include_retired": True}),
        (
            "agent_retire",
            {"agent_name": "env-default", "target_agent_id": "AI-target"},
        ),
        ("agent_retire_inert", {"agent_name": "env-default"}),
        (
            "board_stale_after_set",
            {"agent_name": "env-default", "stale_after_days": 7},
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
async def test_events_redeclares_after_subscription_reconnect(monkeypatch) -> None:
    import pursers_client.client as client_module

    board = client()
    board.reconnect_delay_s = 0.01
    journal_uri = "board://board-multi-name/journal"
    second_handshake = asyncio.Event()
    callback_count = 0
    session_count = 0

    @asynccontextmanager
    async def context(value):
        yield value

    class Subscription:
        honored = SimpleNamespace(resource_subscriptions=[journal_uri])

        def __init__(self, index: int) -> None:
            self.index = index

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.index == 1:
                raise StopAsyncIteration
            await asyncio.Event().wait()
            raise StopAsyncIteration

    class Session:
        def __init__(self, index: int) -> None:
            self.index = index

        def listen(self, **_arguments):
            return context(Subscription(self.index))

    def session_context(*_args, **_kwargs):
        nonlocal session_count
        session_count += 1
        return context(Session(session_count))

    async def drain(*_args, **_kwargs):
        if False:
            yield {}

    def on_handshake() -> None:
        nonlocal callback_count
        callback_count += 1
        if callback_count == 2:
            second_handshake.set()

    monkeypatch.setattr(board, "_http", lambda: context(object()))
    monkeypatch.setattr(board, "_drain", drain)
    monkeypatch.setattr(
        client_module, "streamable_http_client", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(client_module, "Client", session_context)

    events = board.events(
        from_cursor=4,
        only_mine=False,
        resource_subscriptions=(journal_uri,),
        acknowledge=False,
        touch=False,
        subscription_callback=on_handshake,
    )
    pending = asyncio.create_task(anext(events))
    await asyncio.wait_for(second_handshake.wait(), timeout=1)
    assert callback_count == 2
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    await events.aclose()


@pytest.mark.anyio
async def test_fifty_reconnects_reuse_one_http_pool_and_close_each_transport(
    monkeypatch,
) -> None:
    import pursers_client.client as client_module

    board = client()
    board.reconnect_delay_s = 0
    journal_uri = "board://board-multi-name/journal"
    shared_http = object()
    board._http_client = shared_http
    active = 0
    peak = 0
    opened = 0
    closed = 0
    http_context_entries = 0
    ready = asyncio.Event()
    hold = asyncio.Event()
    seen_http_clients: list[object] = []

    class SyntheticDisconnect(RuntimeError):
        pass

    SyntheticDisconnect.__module__ = "httpx2"

    @asynccontextmanager
    async def unexpected_http_context():
        nonlocal http_context_entries
        http_context_entries += 1
        yield object()

    @asynccontextmanager
    async def session_context(*_args, **_kwargs):
        nonlocal active, peak, opened, closed
        opened += 1
        index = opened
        active += 1
        peak = max(peak, active)
        try:
            yield Session(index)
        finally:
            active -= 1
            closed += 1

    @asynccontextmanager
    async def subscription_context(subscription):
        yield subscription

    class Subscription:
        honored = SimpleNamespace(resource_subscriptions=[journal_uri])

        def __init__(self, index: int) -> None:
            self.index = index

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.index <= 50:
                raise SyntheticDisconnect("synthetic forced disconnect")
            ready.set()
            await hold.wait()
            raise StopAsyncIteration

    class Session:
        def __init__(self, index: int) -> None:
            self.index = index

        def listen(self, **_arguments):
            return subscription_context(Subscription(self.index))

    async def drain(*_args, **_kwargs):
        if False:
            yield {}

    def transport(_url: str, *, http_client: object):
        seen_http_clients.append(http_client)
        return object()

    monkeypatch.setattr(board, "_http", unexpected_http_context)
    monkeypatch.setattr(board, "_drain", drain)
    monkeypatch.setattr(client_module, "streamable_http_client", transport)
    monkeypatch.setattr(client_module, "Client", session_context)

    events = board.events(
        from_cursor=0,
        only_mine=False,
        resource_subscriptions=(journal_uri,),
        acknowledge=False,
        touch=False,
    )
    pending = asyncio.create_task(anext(events))
    await asyncio.wait_for(ready.wait(), timeout=2)
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    await events.aclose()

    assert opened == closed == 51
    assert active == 0
    assert peak == 1
    assert http_context_entries == 0
    assert len(seen_http_clients) == 51
    assert all(http is shared_http for http in seen_http_clients)


@pytest.mark.anyio
async def test_events_drops_unknown_kinds_and_keeps_subscription(monkeypatch) -> None:
    import pursers_client.client as client_module

    board = client()
    journal_uri = "board://board-multi-name/journal"
    captured: list[frozenset[str]] = []

    @asynccontextmanager
    async def context(value):
        yield value

    class Subscription:
        honored = SimpleNamespace(resource_subscriptions=[journal_uri])

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()
            raise StopAsyncIteration

    class Session:
        def listen(self, **_arguments):
            return context(Subscription())

    async def drain(*_args, kinds, **_kwargs):
        captured.append(kinds)
        yield {"id": "EV-5", "seq": 5, "kind": "ticket_created"}

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
        kinds=("ticket_created", "future_server_kind"),
        only_mine=False,
        resource_subscriptions=(journal_uri,),
        acknowledge=False,
        touch=False,
    )
    with pytest.warns(
        RuntimeWarning,
        match=r"dropping unknown event kinds: \['future_server_kind'\]",
    ):
        assert (await anext(events))["id"] == "EV-5"
    await events.aclose()
    assert captured == [frozenset({"ticket_created"})]


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
