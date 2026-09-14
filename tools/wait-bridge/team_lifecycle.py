"""Persistent board-state backend for AionUi Home standalone seat groups."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from typing import Any, Callable

import door_state
from pursers_client import BoardClient, BoardClientError

STATE_KEY = "home_seat_groups_v1"
SCHEMA_VERSION = 1
MAX_GROUPS = 25
MAX_MEMBERS = 50
MAX_STATE_CHARS = 5_000
GROUP_ID = re.compile(r"^group-[0-9a-f]{12}$")
CAPABILITIES = {"can_work": False, "can_review": False}


def _failure(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}


def _empty() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "revision": 0, "groups": []}


def _encode(document: dict[str, Any]) -> str:
    value = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(value) > MAX_STATE_CHARS:
        raise ValueError("group state exceeds 5000 characters")
    return value


def _validate_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be text")
    selected = value.strip()
    if not 1 <= len(selected) <= 80:
        raise ValueError("name must contain 1-80 characters")
    return selected


def _validate_members(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MEMBERS:
        raise ValueError("member_agent_ids must contain 1-50 agent IDs")
    if not all(isinstance(item, str) and item.strip() == item and item for item in value):
        raise ValueError("member_agent_ids must contain non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError("member_agent_ids must be unique")
    return list(value)


def _validate_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "revision", "groups"}:
        raise ValueError("group state has invalid fields")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("group state has an unsupported schema")
    if type(value.get("revision")) is not int or value["revision"] < 0:
        raise ValueError("group state revision is invalid")
    groups = value.get("groups")
    if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
        raise ValueError("group state groups are invalid")
    clean: list[dict[str, Any]] = []
    ids: set[str] = set()
    names: set[str] = set()
    for raw in groups:
        required = {"group_id", "name", "member_agent_ids", "created_at", "updated_at"}
        if not isinstance(raw, dict) or set(raw) != required:
            raise ValueError("group state contains an invalid group")
        group_id = raw.get("group_id")
        if not isinstance(group_id, str) or not GROUP_ID.fullmatch(group_id) or group_id in ids:
            raise ValueError("group state contains an invalid group ID")
        name = _validate_name(raw.get("name"))
        folded = name.casefold()
        if folded in names:
            raise ValueError("group names must be unique")
        members = _validate_members(raw.get("member_agent_ids"))
        if not all(isinstance(raw.get(field), str) and raw[field] for field in ("created_at", "updated_at")):
            raise ValueError("group timestamps are invalid")
        ids.add(group_id)
        names.add(folded)
        clean.append({**raw, "name": name, "member_agent_ids": members})
    return {"schema_version": SCHEMA_VERSION, "revision": value["revision"], "groups": clean}


def _classify(exc: BaseException) -> dict[str, Any]:
    message = str(exc).casefold()
    if "permission" in message or "denied" in message or "not a member" in message:
        return _failure("permission_denied", "Central denied this action for the connected principal.")
    if "precondition" in message or "generation" in message or "conflict" in message:
        return _failure("conflict", "Groups changed. Refresh before retrying.")
    if isinstance(exc, (BoardClientError, OSError, RuntimeError)):
        return _failure("backend_unavailable", "The board connection is unavailable. Reconnect the helper, then retry.", retryable=True)
    return _failure("invalid_input", str(exc) or "Central rejected the request.")


class TeamLifecycleService:
    def __init__(self, client: Any, board: str) -> None:
        self.client = client
        self.board = board
        self._lock = asyncio.Lock()

    async def _read(self) -> tuple[dict[str, Any], str | None]:
        try:
            result = await self.client.board_state_get(STATE_KEY)
        except BoardClientError as exc:
            if "state key not found" in str(exc).casefold():
                return _empty(), None
            raise
        state = result.get("state")
        raw = state.get("value") if isinstance(state, dict) else None
        if not isinstance(raw, str):
            raise ValueError("group state is malformed")
        try:
            document = _validate_document(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError("group state is not valid JSON") from exc
        return document, hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def initialize(self) -> None:
        _document, digest = await self._read()
        if digest is None:
            # Central has no create-if-absent precondition. Concurrent initializers
            # write the same canonical empty value, after which every user edit is CAS.
            await self.client.board_state_update(STATE_KEY, _encode(_empty()))

    async def _agents(self) -> list[dict[str, Any]]:
        snapshot = await self.client.board_snapshot(limit=1_000, max_bytes=300_000, include_retired=True)
        rows = snapshot.get("agents")
        if not isinstance(rows, list):
            raise ValueError("board snapshot has no agent roster")
        projected = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("agent_id"), str):
                continue
            capabilities = row.get("capabilities")
            if (
                row.get("role") not in {"worker", "reviewer"}
                or not isinstance(capabilities, dict)
                or not (
                    capabilities.get("can_work") is True
                    or capabilities.get("can_review") is True
                )
            ):
                continue
            projected.append({
                "agent_id": row["agent_id"],
                "agent_name": str(row.get("agent_name") or row["agent_id"]),
                "role": str(row.get("role") or "member"),
                "lifecycle_status": str(row.get("lifecycle_status") or "active"),
            })
        return projected

    @staticmethod
    def _project(document: dict[str, Any], agents: list[dict[str, Any]]) -> dict[str, Any]:
        by_id = {row["agent_id"]: row for row in agents}
        groups = []
        for group in document["groups"]:
            members = []
            for agent_id in group["member_agent_ids"]:
                row = by_id.get(agent_id)
                members.append(
                    {**row, "present": True} if row else {
                        "agent_id": agent_id,
                        "agent_name": agent_id,
                        "role": "unknown",
                        "lifecycle_status": "missing",
                        "present": False,
                    }
                )
            groups.append({**group, "members": members})
        return {"revision": document["revision"], "groups": groups, "agents": agents}

    async def _mutate(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            await self.initialize()
            document, digest = await self._read()
            if digest is None:
                raise RuntimeError("group state initialization did not persist")
            expected = payload.get("expected_revision")
            if type(expected) is not int or expected != document["revision"]:
                return _failure("conflict", "Groups changed. Refresh before retrying.")
            agents = await self._agents()
            agent_ids = {row["agent_id"] for row in agents}
            groups = list(document["groups"])
            now = datetime.now(timezone.utc).isoformat()

            if operation == "create":
                if len(groups) >= MAX_GROUPS:
                    raise ValueError("at most 25 groups are supported")
                name = _validate_name(payload.get("name"))
                members = _validate_members(payload.get("member_agent_ids"))
                if name.casefold() in {group["name"].casefold() for group in groups}:
                    raise ValueError("group name already exists")
                missing = next((item for item in members if item not in agent_ids), None)
                if missing:
                    return _failure("member_not_found", f"Agent {missing} does not belong to board {self.board}.")
                groups.append({
                    "group_id": f"group-{secrets.token_hex(6)}", "name": name,
                    "member_agent_ids": members, "created_at": now, "updated_at": now,
                })
            else:
                group_id = payload.get("group_id")
                index = next((i for i, group in enumerate(groups) if group["group_id"] == group_id), None)
                if index is None:
                    return _failure("group_not_found", "The group no longer exists. Refresh the list.")
                if operation == "update":
                    name = _validate_name(payload.get("name"))
                    members = _validate_members(payload.get("member_agent_ids"))
                    if name.casefold() in {group["name"].casefold() for i, group in enumerate(groups) if i != index}:
                        raise ValueError("group name already exists")
                    missing = next((item for item in members if item not in agent_ids), None)
                    if missing:
                        return _failure("member_not_found", f"Agent {missing} does not belong to board {self.board}.")
                    groups[index] = {**groups[index], "name": name, "member_agent_ids": members, "updated_at": now}
                elif operation == "remove":
                    groups.pop(index)
                else:
                    return _failure("invalid_input", "This group action is not supported.")

            saved = {"schema_version": SCHEMA_VERSION, "revision": document["revision"] + 1, "groups": groups}
            raw = _encode(_validate_document(saved))
            await self.client.board_state_update(STATE_KEY, raw, expected_sha256=digest)
            projected = self._project(saved, agents)
            return {"ok": True, "board": self.board, **projected}

    async def dispatch(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("board") != self.board:
            return _failure("board_mismatch", f"Use the configured board {self.board}.")
        try:
            if operation == "status":
                await self.initialize()
                return {"ok": True, "board": self.board, "connected": True, "actor": self.client.agent_name}
            if operation == "list":
                await self.initialize()
                document, _digest = await self._read()
                return {"ok": True, "board": self.board, **self._project(document, await self._agents())}
            if operation in {"create", "update", "remove"}:
                return await self._mutate(operation, payload)
            return _failure("invalid_input", "This group action is not supported.")
        except (BoardClientError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return _classify(exc)


def _entry(state_dir: str, board: str) -> dict[str, Any]:
    document = door_state.load(door_state.state_path(state_dir))
    candidates = [entry for entry in document["doors"] if entry["b"] == board]
    if not candidates:
        raise ValueError(f"no stored door for board {board}")
    return sorted(candidates, key=lambda entry: entry["r"] != "worker")[0]


def create_sidecar_client(entry: dict[str, Any], board: str, client_factory: Callable[..., Any] = BoardClient) -> Any:
    return client_factory(
        entry["u"], entry["t"], board,
        agent_name=f"pursers-home-team-lifecycle-{entry['r']}",
        role=entry["r"], capabilities=CAPABILITIES, allow_takeover=True,
    )


async def _serve(args: argparse.Namespace, client_factory: Callable[..., Any] = BoardClient) -> None:
    entry = _entry(args.state_dir, args.board)
    client = create_sidecar_client(entry, args.board, client_factory)
    async with client:
        service = TeamLifecycleService(client, args.board)
        await service.initialize()
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
    parser = argparse.ArgumentParser(prog="pursers-wait-bridge team-lifecycle")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--board", required=True)
    args = parser.parse_args(argv)
    try:
        asyncio.run(_serve(args))
    except (BoardClientError, OSError, RuntimeError, ValueError) as exc:
        print(f"pursers-wait-bridge: team lifecycle unavailable: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
