"""Client contract for the least-privilege Butler control surface."""

from __future__ import annotations

from typing import Any

import pytest

from pursers_client import BoardClient
from pursers_client.mcp_proxy import DEFAULT_TOOLS


@pytest.mark.anyio
async def test_board_client_forwards_every_butler_operation(monkeypatch) -> None:
    board = BoardClient(
        "https://central.example/mcp",
        "TOKEN_PLACEHOLDER",
        "board-a",
        agent_name="butler-a",
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        return {"ok": True}

    monkeypatch.setattr(board, "_call", call)
    config = {"schema": "autonomous_butler_config_v1"}
    result = {
        "outcome": "failed",
        "reason_code": "executor_failed",
        "commit_state": "not_reached",
    }

    await board.butler_config_get(revision=2)
    await board.butler_config_set("mut-1", "a2a", config, 1)
    await board.butler_command_submit(
        "cmd-1", "project-a", "a2a", "reconcile_now", {}, 2,
        "2026-09-24T00:00:00+00:00", priority="high",
    )
    await board.butler_command_inspect(command_id="cmd-1", status="accepted", limit=4)
    await board.butler_command_wait("cmd-1", 1, timeout_s=5)
    await board.butler_command_acknowledge("cmd-1", 1, "validating", "started")
    await board.butler_command_cancel("cmd-1", 2, "a2a", "cancelled", commit_state="not_reached")
    await board.butler_command_result("cmd-1", 3, result)

    assert [name for name, _ in calls] == [
        "butler_config_get",
        "butler_config_set",
        "butler_command_submit",
        "butler_command_inspect",
        "butler_command_wait",
        "butler_command_acknowledge",
        "butler_command_cancel",
        "butler_command_result",
    ]
    assert calls[2][1]["priority"] == "high"
    assert calls[4][1]["timeout_s"] == 5
    assert calls[6][1]["commit_state"] == "not_reached"
    assert calls[7][1]["result"] == result


def test_proxy_default_profile_exposes_complete_butler_surface() -> None:
    assert {
        "butler_config_get",
        "butler_config_set",
        "butler_command_submit",
        "butler_command_inspect",
        "butler_command_wait",
        "butler_command_acknowledge",
        "butler_command_cancel",
        "butler_command_result",
    } <= DEFAULT_TOOLS
