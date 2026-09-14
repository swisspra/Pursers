from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
sys.path.insert(0, str(REPOSITORY / "packages" / "client" / "src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from seat_lifecycle import SeatLifecycleService

BOARD = "sandbox-home"


def agent(**changes):
    value = {
        "board": BOARD,
        "agent_id": "AI-worker-1",
        "principal_id": "PR-worker",
        "agent_name": "worker-1",
        "role": "worker",
        "lifecycle_status": "active",
        "status": "idle",
        "lease_expires_at": None,
        "capabilities": {"can_work": True, "can_review": False, "tier_max": 2},
        "capabilities_explicit": True,
        "agent_platform": "aionui-home",
        "task_focus": "standalone-seat",
    }
    value.update(changes)
    return value


def status(row=None):
    return {"ok": True, "board_id": BOARD, "agents": [row or agent()]}


def test_join_is_board_pinned_and_uses_explicit_safe_capabilities() -> None:
    calls = []

    async def call(entry, tool, arguments):
        calls.append((entry, tool, arguments))
        if tool == "board_join":
            return {"ok": True, **agent(), "rejoined": False}
        return status()

    service = SeatLifecycleService("/state", BOARD, call)
    decoded = {"u": "http://127.0.0.1:8766/mcp", "b": BOARD, "r": "worker", "t": "secret"}
    with (
        patch("seat_lifecycle.door_state.decode_door", return_value=decoded),
        patch("seat_lifecycle.door_state.state_path", return_value=Path("/state/doors.json")),
        patch("seat_lifecycle.door_state.store", return_value=decoded),
        patch("seat_lifecycle.door_state.reserve_name"),
    ):
        result = asyncio.run(service.dispatch("join", {
            "board": BOARD, "door": "redacted-door", "agent_name": "worker-1",
            "role": "worker", "tier_max": 2, "folder": "standalone-seat",
        }))
    assert result["ok"] is True
    assert calls[0][1] == "board_join"
    assert calls[0][2]["capabilities"] == {
        "host": "aionui-home", "max_parallel": 1, "tier_max": 2,
        "can_work": True, "can_review": False,
    }
    assert "secret" not in str(result)


def test_bound_rejoin_preflights_exact_identity_before_join() -> None:
    calls = []

    async def call(_entry, tool, arguments):
        calls.append((tool, arguments))
        return status(agent(principal_id="PR-replacement"))

    service = SeatLifecycleService("/state", BOARD, call)
    decoded = {"u": "https://central.example/mcp", "b": BOARD, "r": "worker", "t": "secret"}
    with (
        patch("seat_lifecycle.door_state.decode_door", return_value=decoded),
        patch("seat_lifecycle.door_state.state_path", return_value=Path("/state/doors.json")),
        patch("seat_lifecycle.door_state.store", return_value=decoded),
        patch("seat_lifecycle.door_state.reserve_name"),
        patch("seat_lifecycle._entry", return_value=decoded),
    ):
        result = asyncio.run(service.dispatch("join", {
            "board": BOARD, "door": "redacted-door", "agent_name": "worker-1", "role": "worker",
            "expected_identity": {k: agent()[k] for k in ("board", "agent_id", "principal_id", "agent_name", "role")},
        }))
    assert result["code"] == "identity_mismatch"
    assert [tool for tool, _arguments in calls] == ["board_status"]


def test_retire_preflights_and_verifies_the_same_identity() -> None:
    calls = []

    async def call(_entry, tool, arguments):
        calls.append((tool, arguments))
        if tool == "agent_retire":
            return {"ok": True, "agent": agent(lifecycle_status="retired")}
        return status()

    service = SeatLifecycleService("/state", BOARD, call)
    expected = {k: agent()[k] for k in ("board", "agent_id", "principal_id", "agent_name", "role")}
    with patch("seat_lifecycle._entry", return_value={"b": BOARD}):
        result = asyncio.run(service.dispatch("retire", {
            "board": BOARD, "agent_name": "worker-1", "expected_identity": expected,
        }))
    assert result["ok"] is True
    assert result["agent"]["lifecycle_status"] == "retired"
    assert [tool for tool, _arguments in calls] == ["board_status", "agent_retire"]


def test_join_rejects_invalid_tier_before_local_or_central_mutation() -> None:
    calls = []

    async def call(_entry, tool, arguments):
        calls.append((tool, arguments))
        return {}

    result = asyncio.run(SeatLifecycleService("/state", BOARD, call).dispatch("join", {
        "board": BOARD, "door": "redacted-door", "agent_name": "worker-1",
        "role": "worker", "tier_max": 4,
    }))
    assert result["code"] == "invalid_input"
    assert calls == []
