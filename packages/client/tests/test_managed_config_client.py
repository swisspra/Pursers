"""Client forwarding contract for managed board configuration."""

from __future__ import annotations

from typing import Any

import pytest

from pursers_client import BoardClient


@pytest.mark.anyio
async def test_managed_board_methods_forward_exact_typed_arguments(monkeypatch) -> None:
    board = BoardClient(
        "https://central.example/mcp",
        "TOKEN_PLACEHOLDER",
        "board-a",
        agent_name="dashboard-a",
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        return {"ok": True}

    monkeypatch.setattr(board, "_call", call)
    principal_id = "PR-" + "a" * 64

    await board.board_members()
    await board.board_member_add(principal_id, "reviewer")
    await board.board_member_set_role(principal_id, "member")
    await board.board_member_remove(principal_id)

    assert calls == [
        ("board_members", {}),
        (
            "board_member_add",
            {
                "agent_name": "dashboard-a",
                "principal_id": principal_id,
                "role": "reviewer",
            },
        ),
        (
            "board_member_set_role",
            {
                "agent_name": "dashboard-a",
                "principal_id": principal_id,
                "role": "member",
            },
        ),
        (
            "board_member_remove",
            {"agent_name": "dashboard-a", "principal_id": principal_id},
        ),
    ]
