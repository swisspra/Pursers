"""Board-pinned backend for the AionUi Home standalone seat lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import door_state
from pursers_client import BoardClient, BoardClientError

SAFE_ROLES = {"worker", "reviewer"}
IDENTITY_FIELDS = ("board", "agent_id", "principal_id", "agent_name", "role")


def _failure(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message, "retryable": retryable}


def _classify(exc: BaseException) -> dict[str, Any]:
    message = str(exc).casefold()
    if "no stored door" in message:
        return _failure("not_connected", "No door is stored for this board and role.", retryable=True)
    if "already exists" in message or "collision" in message or "takeover" in message:
        return _failure("identity_conflict", "Central refused a different or active seat identity.")
    if "active work" in message or "active review" in message or "live lease" in message:
        return _failure("active_lease", "The seat has an active work or review lease.", retryable=True)
    if "permission" in message or "denied" in message or "scope" in message:
        return _failure("permission_denied", "Central denied this seat lifecycle action.")
    if isinstance(exc, (BoardClientError, OSError, RuntimeError)):
        return _failure("board_unavailable", "Live board state is unavailable.", retryable=True)
    return _failure("invalid_input", "Central rejected the seat lifecycle request.")


def _entry(state_dir: str, board: str, role: str | None = None) -> dict[str, Any]:
    document = door_state.load(door_state.state_path(state_dir))
    candidates = [
        item for item in document["doors"]
        if item["b"] == board and (role is None or item["r"] == role)
    ]
    if not candidates:
        raise ValueError(f"no stored door for board {board}")
    return sorted(candidates, key=lambda item: item["r"] != "worker")[0]


def _identity(row: Any, board: str) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    value = {
        "board": row.get("board") or row.get("board_id") or board,
        "agent_id": row.get("agent_id"),
        "principal_id": row.get("principal_id"),
        "agent_name": row.get("agent_name"),
        "role": row.get("role"),
        "lifecycle_status": row.get("lifecycle_status"),
    }
    if value["board"] != board or value["role"] not in SAFE_ROLES:
        return None
    if not all(isinstance(value[field], str) and value[field] for field in IDENTITY_FIELDS[1:]):
        return None
    if value["lifecycle_status"] not in {"active", "handed_off", "retired", "stale", "unknown"}:
        return None
    return value


def _same_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(field) == right.get(field) for field in IDENTITY_FIELDS)


async def _call(entry: dict[str, Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {entry['t']}"},
        timeout=httpx2.Timeout(10.0, read=None),
        trust_env=False,
    ) as http:
        transport = streamable_http_client(entry["u"], http_client=http)
        async with Client(transport, mode="2026-07-28", cache=None) as client:
            result = await client.call_tool(tool, {"board_id": entry["b"], **arguments})
            return BoardClient._decode(result)


def _project_agents(value: Any, board: str) -> list[dict[str, Any]] | None:
    if not isinstance(value, dict) or value.get("board_id") != board or not isinstance(value.get("agents"), list):
        return None
    projected = []
    for row in value["agents"]:
        identity = _identity(row, board)
        if identity is None:
            return None
        projected.append({
            **identity,
            "status": row.get("status") or "unknown",
            "lease_expires_at": row.get("lease_expires_at"),
            "current_offer": row.get("current_offer"),
            "capabilities": row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {},
            "capabilities_explicit": row.get("capabilities_explicit") is True,
            "agent_platform": row.get("agent_platform"),
            "task_focus": row.get("task_focus"),
        })
    return projected


class SeatLifecycleService:
    def __init__(
        self,
        state_dir: str,
        board: str,
        call: Callable[[dict[str, Any], str, dict[str, Any]], Awaitable[dict[str, Any]]] = _call,
    ) -> None:
        self.state_dir = state_dir
        self.board = board
        self.call = call

    async def read(self) -> dict[str, Any]:
        entry = _entry(self.state_dir, self.board)
        value = await self.call(entry, "board_status", {"include_retired": True})
        agents = _project_agents(value, self.board)
        if agents is None:
            return _failure("invalid_board_response", "The board returned inconsistent seat data.", retryable=True)
        return {"ok": True, "board_id": self.board, "agents": agents}

    async def join(self, payload: dict[str, Any]) -> dict[str, Any]:
        door = payload.get("door")
        name = payload.get("agent_name")
        role = payload.get("role")
        tier_max = payload.get("tier_max", 2)
        if not isinstance(door, str) or not door or not isinstance(name, str) or role not in SAFE_ROLES:
            return _failure("invalid_input", "Door, seat name, and role are required.")
        if type(tier_max) is not int or tier_max not in {1, 2, 3}:
            return _failure("invalid_input", "Tier ceiling must be 1, 2, or 3.")
        decoded = door_state.decode_door(door)
        parsed = urlsplit(decoded["u"])
        if decoded["b"] != self.board or decoded["r"] != role:
            return _failure("wrong_board", "The door does not match the configured board and role.")
        if not door_state.is_loopback_url(decoded["u"]) and parsed.scheme != "https":
            return _failure("invalid_input", "Remote Central doors require HTTPS.")
        state_path = door_state.state_path(self.state_dir)
        entry = door_state.store(state_path, door, allow_remote=True)
        door_state.reserve_name(state_path, self.board, role, requested=name)

        expected = payload.get("expected_identity")
        existing = None
        if expected is not None:
            snapshot = await self.read()
            if snapshot.get("ok") is not True:
                return snapshot
            matches = [row for row in snapshot["agents"] if row["agent_id"] == expected.get("agent_id")]
            if len(matches) != 1 or not _same_identity(matches[0], expected):
                return _failure("identity_mismatch", "The preserved identity does not match live Central state.")
            existing = matches[0]

        capabilities = (
            existing["capabilities"] if existing is not None else {
                "host": "aionui-home",
                "max_parallel": 1,
                "tier_max": tier_max,
                "can_work": role == "worker",
                "can_review": role == "reviewer",
            }
        )
        arguments: dict[str, Any] = {
            "agent_name": name,
            "role": role,
            "capabilities": capabilities,
            "agent_platform": existing.get("agent_platform") if existing else "aionui-home",
            "task_focus": existing.get("task_focus") if existing else (payload.get("folder") or "standalone-seat"),
        }
        if existing is not None:
            if not existing["capabilities_explicit"] or not arguments["agent_platform"] or not arguments["task_focus"]:
                return _failure("identity_mismatch", "The preserved seat lacks matching takeover markers.")
            arguments["allow_matching_takeover"] = True
        joined = await self.call(entry, "board_join", arguments)
        identity = _identity(joined, self.board)
        if identity is None or identity["agent_name"] != name or identity["role"] != role:
            return _failure("invalid_join_response", "Central returned inconsistent seat identity.")
        return {"ok": True, "identity": identity, "rejoined": bool(joined.get("rejoined"))}

    async def retire(self, payload: dict[str, Any]) -> dict[str, Any]:
        expected = payload.get("expected_identity")
        if not isinstance(expected, dict) or expected.get("board") != self.board:
            return _failure("identity_mismatch", "The preserved seat identity is required.")
        role = expected.get("role")
        entry = _entry(self.state_dir, self.board, role)
        snapshot = await self.read()
        if snapshot.get("ok") is not True:
            return snapshot
        matches = [row for row in snapshot["agents"] if row["agent_id"] == expected.get("agent_id")]
        if len(matches) != 1 or not _same_identity(matches[0], expected):
            return _failure("identity_mismatch", "The preserved identity does not match live Central state.")
        retired = await self.call(entry, "agent_retire", {"agent_name": expected["agent_name"]})
        identity = _identity(retired.get("agent"), self.board)
        if retired.get("ok") is not True or identity is None or not _same_identity(identity, expected):
            return _failure("invalid_retirement_response", "Central returned inconsistent retirement identity.")
        return {"ok": True, "board_id": self.board, "agent": identity}

    def forget(self, payload: dict[str, Any]) -> dict[str, Any]:
        role = payload.get("role")
        if role not in SAFE_ROLES:
            return _failure("invalid_input", "Seat role must be worker or reviewer.")
        removed = door_state.forget(door_state.state_path(self.state_dir), self.board, role)
        return {"ok": removed, "board": self.board, "role": role}

    async def dispatch(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("board") != self.board:
            return _failure("wrong_board", f"Use the configured board {self.board}.")
        try:
            if operation == "join":
                return await self.join(payload)
            if operation == "read":
                return await self.read()
            if operation == "retire":
                return await self.retire(payload)
            if operation == "forget":
                return self.forget(payload)
            return _failure("invalid_input", "This seat lifecycle action is not supported.")
        except (BoardClientError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _classify(exc)


async def _serve(args: argparse.Namespace) -> None:
    service = SeatLifecycleService(args.state_dir, args.board)
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            return
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("payload"), dict):
                raise ValueError
            result = await service.dispatch(str(request.get("operation") or ""), request["payload"])
            response = {"id": request.get("id"), "result": result}
        except (TypeError, ValueError, json.JSONDecodeError):
            response = {"id": None, "result": _failure("invalid_input", "The sidecar request is invalid.")}
        print(json.dumps(response, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pursers-wait-bridge seat-lifecycle")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--board", required=True)
    args = parser.parse_args(argv)
    try:
        asyncio.run(_serve(args))
    except (BoardClientError, OSError, RuntimeError, ValueError) as exc:
        print(f"pursers-wait-bridge: seat lifecycle unavailable: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
