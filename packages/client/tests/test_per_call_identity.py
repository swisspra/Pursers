"""Per-call BoardClient identity must not mutate shared client state."""

from __future__ import annotations

import asyncio
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
