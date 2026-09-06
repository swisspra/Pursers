#!/usr/bin/env python3
"""Provision and inspect registry-backed Pursers worker/reviewer seats."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shlex
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from pursers_client import BoardClient, BoardClientError
from registry_admin import (
    CENTRAL_URL_DEFAULT,
    HOME_BOARD_ID,
    RegistryError,
    read_registry,
)

VALID_ROLES = frozenset({"worker", "reviewer"})
CENTRAL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
SEAT_REGISTRY_KEY = "seat_registry"
SEAT_REGISTRY_SCHEMA_VERSION = 1
DEFAULT_STALE_SECONDS = 300
ACTIVE_CLAIM_STATES = frozenset({"claimed", "in_progress", "creating_report"})
ACTIVE_REVIEW_STATES = frozenset({"submitted", "reviewing", "in_review"})


class SeatBackend(Protocol):
    home_board: str

    async def registry(self) -> dict[str, Any]: ...

    async def seat_registry(self) -> dict[str, Any]: ...

    async def write_seat_registry(self, document: dict[str, Any]) -> None: ...

    async def members(self, board_id: str) -> dict[str, Any]: ...

    async def snapshot(self, board_id: str) -> dict[str, Any]: ...

    async def member_add(self, board_id: str, principal_id: str) -> None: ...

    async def member_set_role(
        self, board_id: str, principal_id: str, role: str
    ) -> None: ...

    async def member_remove(self, board_id: str, principal_id: str) -> None: ...


ClientFactory = Callable[..., Any]


class SeatBoardClient(BoardClient):
    """Packaged admin calls layered on the released BoardClient transport."""

    async def board_members(self) -> dict[str, Any]:
        return await self._call("board_members", {})

    async def board_member_add(
        self, principal_id: str, *, role: str = "member"
    ) -> dict[str, Any]:
        return await self._call(
            "board_member_add",
            {
                "agent_name": self.agent_name,
                "principal_id": principal_id,
                "role": role,
            },
        )

    async def board_member_set_role(
        self, principal_id: str, role: str
    ) -> dict[str, Any]:
        return await self._call(
            "board_member_set_role",
            {
                "agent_name": self.agent_name,
                "principal_id": principal_id,
                "role": role,
            },
        )

    async def board_member_remove(self, principal_id: str) -> dict[str, Any]:
        return await self._call(
            "board_member_remove",
            {
                "agent_name": self.agent_name,
                "principal_id": principal_id,
            },
        )


class LiveBackend:
    def __init__(
        self,
        central_url: str,
        token: str,
        home_board: str,
        admin_name: str,
        client_factory: ClientFactory = SeatBoardClient,
    ) -> None:
        self.central_url = central_url
        self.token = token
        self.home_board = home_board
        self.admin_name = admin_name
        self.client_factory = client_factory

    def _client(self, board_id: str) -> Any:
        return self.client_factory(
            self.central_url,
            self.token,
            board_id,
            agent_name=self.admin_name,
        )

    async def registry(self) -> dict[str, Any]:
        async with self._client(self.home_board) as client:
            return await read_registry(client)

    async def seat_registry(self) -> dict[str, Any]:
        async with self._client(self.home_board) as client:
            try:
                result = await client.board_state_get(SEAT_REGISTRY_KEY)
            except BoardClientError as exc:
                if "state key not found" not in str(exc):
                    raise
                return _empty_seat_registry()
        state = result.get("state")
        if not isinstance(state, dict) or not isinstance(state.get("value"), str):
            raise RegistryError("seat_registry state entry is malformed")
        try:
            return _validate_seat_registry(json.loads(state["value"]))
        except json.JSONDecodeError as exc:
            raise RegistryError("seat_registry state value is not valid JSON") from exc

    async def write_seat_registry(self, document: dict[str, Any]) -> None:
        validated = _validate_seat_registry(document)
        value = json.dumps(validated, separators=(",", ":"), sort_keys=True)
        async with self._client(self.home_board) as client:
            await client.board_state_update(SEAT_REGISTRY_KEY, value)
        actual = await self.seat_registry()
        if actual != validated:
            raise RegistryError("seat_registry read-back mismatch")

    async def members(self, board_id: str) -> dict[str, Any]:
        async with self._client(board_id) as client:
            return await client.board_members()

    async def snapshot(self, board_id: str) -> dict[str, Any]:
        async with self._client(board_id) as client:
            return await client.board_snapshot(limit=1_000, max_bytes=300_000)

    async def member_add(self, board_id: str, principal_id: str) -> None:
        async with self._client(board_id) as client:
            await client.board_member_add(principal_id, role="member")

    async def member_set_role(
        self, board_id: str, principal_id: str, role: str
    ) -> None:
        async with self._client(board_id) as client:
            await client.board_member_set_role(principal_id, role)

    async def member_remove(self, board_id: str, principal_id: str) -> None:
        async with self._client(board_id) as client:
            await client.board_member_remove(principal_id)


def _clean(value: str, label: str) -> str:
    if not value or value != value.strip():
        raise RegistryError(f"{label} must be a non-empty, trimmed string")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise RegistryError(f"{label} must not contain control characters")
    return value


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not CENTRAL_ID_RE.fullmatch(value):
        raise RegistryError(f"{label} must match {CENTRAL_ID_RE.pattern}")
    return value


def _empty_seat_registry() -> dict[str, Any]:
    return {"schema_version": SEAT_REGISTRY_SCHEMA_VERSION, "seats": {}}


def _validate_seat_registry(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {"schema_version", "seats"}:
        raise RegistryError(
            "seat_registry must contain exactly schema_version and seats"
        )
    if document["schema_version"] != SEAT_REGISTRY_SCHEMA_VERSION:
        raise RegistryError("seat_registry schema_version must be 1")
    seats = document["seats"]
    if not isinstance(seats, dict):
        raise RegistryError("seat_registry seats must be an object")
    normalized = _empty_seat_registry()
    for name, definition in seats.items():
        _identifier(name, "seat name")
        if not isinstance(definition, dict) or set(definition) != {
            "principal_id",
            "role",
            "board_mode",
        }:
            raise RegistryError(
                f"seat {name!r} must contain principal_id, role, and board_mode"
            )
        principal_id = _identifier(definition["principal_id"], "principal id")
        role = definition["role"]
        if role not in VALID_ROLES:
            raise RegistryError(f"seat {name!r} role must be worker or reviewer")
        mode = definition["board_mode"]
        if mode == "registry":
            normalized_mode: str | list[str] = "registry"
        elif isinstance(mode, list) and mode:
            normalized_mode = [_identifier(item, "board id") for item in mode]
            if len(set(normalized_mode)) != len(normalized_mode):
                raise RegistryError(f"seat {name!r} board_mode contains duplicates")
        else:
            raise RegistryError(
                f"seat {name!r} board_mode must be registry or a non-empty list"
            )
        normalized["seats"][name] = {
            "principal_id": principal_id,
            "role": role,
            "board_mode": normalized_mode,
        }
    return normalized


def _active_boards(registry: dict[str, Any], home_board: str) -> list[str]:
    boards = [_identifier(home_board, "home board id")]
    for project in registry["projects"].values():
        board_id = _identifier(project["board_id"], "registry board id")
        if (
            project["status"] == "active"
            and project.get("fleet", True)
            and board_id not in boards
        ):
            boards.append(board_id)
    return boards


def _parse_boards(raw: str, registry: dict[str, Any], home_board: str) -> list[str]:
    if raw == "registry":
        return _active_boards(registry, home_board)
    boards = [_identifier(item, "board id") for item in raw.split(",")]
    if len(set(boards)) != len(boards):
        raise RegistryError("board list contains duplicates")
    return boards


async def _inventory(
    backend: SeatBackend, boards: list[str]
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    rows: list[dict[str, Any]] = []
    memberships: dict[str, dict[str, Any]] = {}
    snapshots: dict[str, dict[str, Any]] = {}
    for board_id in boards:
        members = await backend.members(board_id)
        snapshot = await backend.snapshot(board_id)
        memberships[board_id] = members
        snapshots[board_id] = snapshot
        omitted_agents = int(snapshot.get("omitted_counts", {}).get("agents", 0))
        if omitted_agents:
            raise RegistryError(
                f"agent scan on {board_id!r} omitted {omitted_agents} rows; "
                "refusing an incomplete duplicate-name check"
            )
        agents = {
            (agent.get("principal_id"), agent.get("agent_name")): agent
            for agent in snapshot.get("agents", [])
            if isinstance(agent, dict)
        }
        for member in members.get("members", []):
            for name in member.get("agent_names", []):
                agent = agents.get((member["principal_id"], name), {})
                rows.append(
                    {
                        "board_id": board_id,
                        "principal_id": member["principal_id"],
                        "name": name,
                        "role": member["role"],
                        "agent_id": agent.get("agent_id"),
                        "status": str(agent.get("status", "inactive")),
                        "lifecycle_status": agent.get("lifecycle_status"),
                        "last_seen": agent.get("last_activity_at")
                        or agent.get("last_seen"),
                    }
                )
    return rows, memberships, snapshots


async def _seat_rows(backend: SeatBackend, boards: list[str]) -> list[dict[str, Any]]:
    rows, _memberships, _snapshots = await _inventory(backend, boards)
    return rows


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _snapshot_omitted(snapshot: dict[str, Any], collection: str) -> int:
    value = snapshot.get("omitted_counts", {}).get(collection, 0)
    return value if type(value) is int and value >= 0 else 0


def _active_claims(
    snapshots: dict[str, dict[str, Any]], principals: set[str]
) -> list[dict[str, str]]:
    claims: list[dict[str, str]] = []
    now_epoch = datetime.now(timezone.utc).timestamp()
    for board_id, snapshot in snapshots.items():
        omitted = _snapshot_omitted(snapshot, "tickets")
        if omitted:
            raise RegistryError(
                f"ticket scan on {board_id!r} omitted {omitted} rows; "
                "refusing an incomplete active-claim check"
            )
        agent_principals: dict[str, set[str]] = {}
        for agent in snapshot.get("agents", []):
            if not isinstance(agent, dict):
                continue
            agent_id = agent.get("agent_id")
            principal_id = agent.get("principal_id")
            if not isinstance(agent_id, str) or not isinstance(principal_id, str):
                continue
            agent_principals.setdefault(agent_id, set()).add(principal_id)
        target_agent_ids = {
            agent_id
            for agent_id, owners in agent_principals.items()
            if owners & principals
        }
        for ticket in snapshot.get("tickets", []):
            if not isinstance(ticket, dict):
                continue
            ticket_id = str(ticket.get("ticket_id", "unknown"))
            claimed_agent_id = ticket.get("claimed_by_agent_id")
            if (
                ticket.get("status") in ACTIVE_CLAIM_STATES
                and claimed_agent_id in target_agent_ids
            ):
                owners = agent_principals[str(claimed_agent_id)]
                if len(owners) != 1:
                    raise RegistryError(
                        f"ambiguous active work claim holder on "
                        f"{board_id}/{ticket_id}; refusing active-claim check"
                    )
                owner = next(iter(owners))
                claims.append(
                    {
                        "board_id": board_id,
                        "ticket_id": ticket_id,
                        "claim_kind": "work",
                        "principal_id": owner,
                        "claimed_by": str(ticket.get("claimed_by", "unknown")),
                    }
                )
            lease = ticket.get("review_lease")
            if lease is None or ticket.get("status") not in ACTIVE_REVIEW_STATES:
                continue
            if not isinstance(lease, dict):
                raise RegistryError(
                    f"malformed review lease on {board_id}/{ticket_id}; "
                    "refusing active-claim check"
                )
            expires_at = lease.get("expires_at_epoch")
            if isinstance(expires_at, bool):
                raise RegistryError(
                    f"malformed review lease expiry on {board_id}/{ticket_id}; "
                    "refusing active-claim check"
                )
            try:
                expiry = float(expires_at)
            except (TypeError, ValueError):
                raise RegistryError(
                    f"malformed review lease expiry on {board_id}/{ticket_id}; "
                    "refusing active-claim check"
                ) from None
            if not math.isfinite(expiry):
                raise RegistryError(
                    f"malformed review lease expiry on {board_id}/{ticket_id}; "
                    "refusing active-claim check"
                )
            if expiry <= now_epoch:
                continue
            reviewer_agent_id = lease.get("reviewer_agent_id")
            if not isinstance(reviewer_agent_id, str) or not reviewer_agent_id:
                raise RegistryError(
                    f"malformed live review lease on {board_id}/{ticket_id}; "
                    "refusing active-claim check"
                )
            owners = agent_principals.get(reviewer_agent_id)
            if not owners:
                raise RegistryError(
                    f"cannot resolve live review lease holder on "
                    f"{board_id}/{ticket_id}; refusing active-claim check"
                )
            if len(owners) != 1:
                raise RegistryError(
                    f"ambiguous live review lease holder on "
                    f"{board_id}/{ticket_id}; refusing active-claim check"
                )
            owner = next(iter(owners))
            lease_principal = lease.get("reviewer_principal_id")
            if lease_principal is not None and lease_principal != owner:
                raise RegistryError(
                    f"ambiguous live review lease principal on "
                    f"{board_id}/{ticket_id}; refusing active-claim check"
                )
            if owner in principals:
                claims.append(
                    {
                        "board_id": board_id,
                        "ticket_id": ticket_id,
                        "claim_kind": "review",
                        "principal_id": owner,
                        "claimed_by": str(
                            lease.get("reviewer_agent_name", "unknown")
                        ),
                    }
                )
    return claims


def _format_active_claims(claims: list[dict[str, str]]) -> str:
    return ", ".join(
        f"{item['board_id']}/{item['ticket_id']} ({item['claim_kind']})"
        for item in claims
    )


def _member_role(
    memberships: dict[str, dict[str, Any]], board_id: str, principal_id: str
) -> str | None:
    match = next(
        (
            item
            for item in memberships[board_id].get("members", [])
            if item.get("principal_id") == principal_id
        ),
        None,
    )
    return None if match is None else str(match.get("role"))


async def _remove_and_verify(
    backend: SeatBackend, board_id: str, principal_id: str
) -> dict[str, Any]:
    await backend.member_remove(board_id, principal_id)
    members = await backend.members(board_id)
    if any(
        item.get("principal_id") == principal_id
        for item in members.get("members", [])
    ):
        raise RegistryError(
            f"read-back failed on {board_id!r}: principal membership remains"
        )
    snapshot = await backend.snapshot(board_id)
    omitted = _snapshot_omitted(snapshot, "agents")
    if omitted:
        raise RegistryError(
            f"read-back agent scan on {board_id!r} omitted {omitted} rows"
        )
    remaining = [
        agent
        for agent in snapshot.get("agents", [])
        if isinstance(agent, dict) and agent.get("principal_id") == principal_id
    ]
    if remaining:
        raise RegistryError(
            f"read-back failed on {board_id!r}: seat remains in agents projection"
        )
    return {
        "board_id": board_id,
        "principal_id": principal_id,
        "membership_present": False,
        "agents_present": 0,
        "verified": True,
    }


def _print_mutation_failure(
    *,
    action: str,
    verified: list[dict[str, Any]],
    failed_board: str | None,
    failed_principal_id: str | None,
    pending_boards: list[str],
    error: Exception,
) -> None:
    print(
        json.dumps(
            {
                "action": action,
                "status": "partial-failure" if verified else "failed",
                "verified_read_back": verified,
                "failed_board": failed_board,
                "failed_principal_id": failed_principal_id,
                "failed_board_state": "unverified",
                "pending_boards": pending_boards,
                "seat_registry_preserved": True,
                "error_type": type(error).__name__,
            },
            indent=2,
            sort_keys=True,
        )
    )


async def _remove_many_and_verify(
    backend: SeatBackend,
    operations: list[tuple[str, str]],
    *,
    action: str,
) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    for index, (board_id, principal_id) in enumerate(operations):
        try:
            verified.append(
                await _remove_and_verify(backend, board_id, principal_id)
            )
        except Exception as exc:
            _print_mutation_failure(
                action=action,
                verified=verified,
                failed_board=board_id,
                failed_principal_id=principal_id,
                pending_boards=[
                    pending_board
                    for pending_board, _pending_principal in operations[index + 1 :]
                ],
                error=exc,
            )
            raise RegistryError(
                f"{action} mutation failed on {board_id!r}; "
                "see structured audit output"
            ) from exc
    return verified


async def _remaining_memberships(
    backend: SeatBackend, boards: list[str], principals: set[str]
) -> list[dict[str, str]]:
    remaining: list[dict[str, str]] = []
    for board_id in boards:
        members = await backend.members(board_id)
        for member in members.get("members", []):
            principal_id = member.get("principal_id")
            if principal_id in principals:
                remaining.append(
                    {
                        "board_id": board_id,
                        "principal_id": str(principal_id),
                        "role": str(member.get("role", "unknown")),
                    }
                )
    return remaining


def _protected_names(values: list[str]) -> set[str]:
    protected: set[str] = set()
    for value in values:
        for item in value.split(","):
            protected.add(_identifier(item, "protected agent name"))
    return protected


async def _verified_role(
    backend: SeatBackend, board_id: str, principal_id: str, expected: str
) -> None:
    members = await backend.members(board_id)
    match = next(
        (
            item
            for item in members.get("members", [])
            if item.get("principal_id") == principal_id
        ),
        None,
    )
    if match is None or match.get("role") != expected:
        actual = None if match is None else match.get("role")
        raise RegistryError(
            f"read-back failed on {board_id!r}: expected {principal_id} "
            f"role {expected!r}, got {actual!r}"
        )


async def _current_role(
    backend: SeatBackend, board_id: str, principal_id: str
) -> str | None:
    members = await backend.members(board_id)
    match = next(
        (
            item
            for item in members.get("members", [])
            if item.get("principal_id") == principal_id
        ),
        None,
    )
    return None if match is None else str(match.get("role"))


async def _provision(
    backend: SeatBackend, board_id: str, principal_id: str, role: str
) -> None:
    current = await _current_role(backend, board_id, principal_id)
    if current == "admin":
        return
    if current == "reviewer":
        if role == "worker":
            raise RegistryError(
                f"principal {principal_id!r} is reviewer on {board_id!r}; "
                "refusing incompatible worker role"
            )
        return
    if current == "member":
        if role == "worker":
            return
        await backend.member_set_role(board_id, principal_id, "reviewer")
        await _verified_role(backend, board_id, principal_id, "reviewer")
        return
    if current is not None:
        raise RegistryError(
            f"principal {principal_id!r} has unsupported role {current!r} "
            f"on {board_id!r}"
        )
    await backend.member_add(board_id, principal_id)
    await _verified_role(backend, board_id, principal_id, "member")
    if role == "reviewer":
        await backend.member_set_role(board_id, principal_id, "reviewer")
        await _verified_role(backend, board_id, principal_id, "reviewer")


async def _preflight_provision(
    backend: SeatBackend, operations: list[tuple[str, str, str]]
) -> None:
    for board_id, principal_id, requested_role in operations:
        _identifier(board_id, "board id")
        _identifier(principal_id, "principal id")
        if requested_role not in VALID_ROLES:
            raise RegistryError(f"unsupported seat role {requested_role!r}")
        current = await _current_role(backend, board_id, principal_id)
        if current == "reviewer" and requested_role == "worker":
            raise RegistryError(
                f"principal {principal_id!r} is reviewer on {board_id!r}; "
                "refusing incompatible worker role"
            )
        if current not in {None, "member", "reviewer", "admin"}:
            raise RegistryError(
                f"principal {principal_id!r} has unsupported role {current!r} "
                f"on {board_id!r}"
            )


async def _provision_many(
    backend: SeatBackend, operations: list[tuple[str, str, str]]
) -> None:
    await _preflight_provision(backend, operations)
    for board_id, principal_id, role in operations:
        await _provision(backend, board_id, principal_id, role)


def _config(
    *,
    central_url: str,
    home_board: str,
    boards: str,
    name: str,
    token_path: str,
) -> str:
    values = {
        "central_url": central_url,
        "wait_call_boards": boards,
        "agent_name": name,
        "wait_bridge_env_lines": [
            f"ONBOARD_CENTRAL_URL={central_url}",
            f"ONBOARD_BOARD_ID={home_board}",
            f"ONBOARD_AGENT_NAME={name}",
            f'ONBOARD_CENTRAL_TOKEN="$(< {shlex.quote(token_path)})"',
        ],
        "token_file": token_path,
    }
    return json.dumps(values, indent=2, sort_keys=True)


async def _retire(
    args: argparse.Namespace,
    backend: SeatBackend,
    registry: dict[str, Any],
    seat_registry: dict[str, Any],
) -> None:
    name = _identifier(args.name, "agent name")
    if args.stale_seconds < 1:
        raise RegistryError("--stale-seconds must be positive")
    boards = _parse_boards(args.boards, registry, backend.home_board)
    active_boards = _active_boards(registry, backend.home_board)
    scan_boards = list(dict.fromkeys([*active_boards, *boards]))
    rows, memberships, snapshots = await _inventory(backend, scan_boards)
    definition = seat_registry["seats"].get(name)
    candidates = {
        row["principal_id"] for row in rows if row["name"] == name
    }
    if definition is not None:
        candidates.add(definition["principal_id"])
    if args.principal is not None:
        principal = _identifier(args.principal, "principal id")
        if not candidates:
            raise RegistryError(f"agent name {name!r} was not found")
        if principal not in candidates:
            raise RegistryError(
                f"agent name {name!r} is not associated with {principal!r}"
            )
    elif len(candidates) > 1:
        raise RegistryError(
            f"agent name {name!r} exists across principals "
            f"{', '.join(sorted(candidates))}; --principal is required"
        )
    elif candidates:
        principal = next(iter(candidates))
    else:
        raise RegistryError(f"agent name {name!r} was not found")

    principal_definitions = {
        stored_name: stored
        for stored_name, stored in seat_registry["seats"].items()
        if stored["principal_id"] == principal
    }
    if args.boards != "registry" and any(
        stored["board_mode"] == "registry"
        for stored in principal_definitions.values()
    ):
        raise RegistryError(
            "a principal with a registry-mode seat must be retired with "
            "--boards registry"
        )

    target_boards = [
        board_id
        for board_id in boards
        if _member_role(memberships, board_id, principal) is not None
    ]
    definition_targeted = args.boards == "registry" and bool(principal_definitions)
    if args.boards != "registry":
        definition_targeted = any(
            bool(set(stored["board_mode"]) & set(boards))
            for stored in principal_definitions.values()
        )
    if not target_boards and not definition_targeted:
        raise RegistryError(
            f"principal {principal!r} has no selected membership or seat definition"
        )
    admin_boards = [
        board_id
        for board_id in target_boards
        if _member_role(memberships, board_id, principal) == "admin"
    ]
    if admin_boards:
        raise RegistryError(
            "refusing to retire an admin principal from "
            f"{', '.join(admin_boards)}"
        )
    target_snapshots = {
        board_id: snapshots[board_id] for board_id in target_boards
    }
    claims = _active_claims(target_snapshots, {principal})
    if claims:
        listed = _format_active_claims(claims)
        raise RegistryError(f"active claims prevent retirement: {listed}")

    relevant_rows = [
        row
        for row in rows
        if row["board_id"] in target_boards
        and row["principal_id"] == principal
    ]
    now = datetime.now(timezone.utc)
    recent = []
    for row in relevant_rows:
        seen = _parse_time(row.get("last_seen"))
        if seen is None or now - seen <= timedelta(seconds=args.stale_seconds):
            recent.append(f"{row['board_id']}/{row['name']}")
    if recent and not args.force:
        raise RegistryError(
            "seat may still be alive on "
            f"{', '.join(sorted(recent))}; pass --force"
        )

    verified = await _remove_many_and_verify(
        backend,
        [(board_id, principal) for board_id in target_boards],
        action="retire",
    )
    updated_registry = json.loads(json.dumps(seat_registry))
    removed_definitions: list[str] = []
    for stored_name in sorted(principal_definitions):
        stored = updated_registry["seats"][stored_name]
        if args.boards == "registry":
            del updated_registry["seats"][stored_name]
            removed_definitions.append(stored_name)
        else:
            remaining = [
                board_id
                for board_id in stored["board_mode"]
                if board_id not in boards
            ]
            if remaining:
                stored["board_mode"] = remaining
            else:
                del updated_registry["seats"][stored_name]
                removed_definitions.append(stored_name)
    registry_updated = updated_registry != seat_registry
    if registry_updated:
        await backend.write_seat_registry(updated_registry)
    affected_names = sorted(
        {str(row["name"]) for row in relevant_rows}
        | set(principal_definitions)
    )
    print(
        json.dumps(
            {
                "action": "retire",
                "name": name,
                "principal_id": principal,
                "affected_names": affected_names,
                "boards_requested": boards,
                "boards_removed": [item["board_id"] for item in verified],
                "seat_registry_definitions_removed": removed_definitions,
                "seat_registry_read_back_verified": registry_updated,
                "verified_read_back": verified,
            },
            indent=2,
            sort_keys=True,
        )
    )


async def _dedupe(
    args: argparse.Namespace,
    backend: SeatBackend,
    registry: dict[str, Any],
    seat_registry: dict[str, Any],
) -> None:
    name = _identifier(args.name, "agent name")
    keep = _identifier(args.keep_principal, "principal id")
    boards = _active_boards(registry, backend.home_board)
    rows, memberships, snapshots = await _inventory(backend, boards)
    candidates = {
        str(row["principal_id"])
        for row in rows
        if row["name"] == name
    }
    definition = seat_registry["seats"].get(name)
    if definition is not None:
        candidates.add(str(definition["principal_id"]))
    if keep not in candidates:
        raise RegistryError(
            f"agent name {name!r} is not associated with keep principal {keep!r}"
        )
    targets = sorted(candidates - {keep})
    if not targets:
        raise RegistryError(f"agent name {name!r} has no duplicate principals")

    operations: list[tuple[str, str]] = []
    plan: list[dict[str, Any]] = []
    for principal in targets:
        target_boards = [
            board_id for board_id in boards
            if _member_role(memberships, board_id, principal) is not None
        ]
        admin_boards = [
            board_id for board_id in target_boards
            if _member_role(memberships, board_id, principal) == "admin"
        ]
        if admin_boards:
            raise RegistryError(
                "refusing to dedupe an admin principal from "
                + ", ".join(admin_boards)
            )
        operations.extend((board_id, principal) for board_id in target_boards)
        identity_rows = [
            row for row in rows
            if row["name"] == name and row["principal_id"] == principal
        ]
        plan.append(
            {
                "principal_id": principal,
                "boards": target_boards,
                "last_seen": sorted(
                    {str(row.get("last_seen") or "unknown") for row in identity_rows}
                ),
            }
        )

    claims = _active_claims(snapshots, set(targets))
    claims_by_principal: dict[str, list[dict[str, str]]] = {}
    for claim in claims:
        claims_by_principal.setdefault(claim["principal_id"], []).append(claim)
    for item in plan:
        item["active_claims"] = claims_by_principal.get(
            item["principal_id"], []
        )
    result: dict[str, Any] = {
        "action": "dedupe",
        "mode": "commit" if args.commit else "dry-run",
        "name": name,
        "keep_principal": keep,
        "retire": plan,
        "active_claims": claims,
        "writes": len(operations) if args.commit else 0,
    }
    if not args.commit:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if claims:
        raise RegistryError(
            "active claims prevent dedupe: " + _format_active_claims(claims)
        )

    verified = await _remove_many_and_verify(
        backend, operations, action="dedupe"
    )
    updated_registry = json.loads(json.dumps(seat_registry))
    removed_definitions = [
        stored_name
        for stored_name, stored in updated_registry["seats"].items()
        if stored["principal_id"] in targets
    ]
    for stored_name in removed_definitions:
        del updated_registry["seats"][stored_name]
    if updated_registry != seat_registry:
        await backend.write_seat_registry(updated_registry)
    result.update(
        {
            "verified_read_back": verified,
            "seat_registry_definitions_removed": sorted(removed_definitions),
        }
    )
    print(json.dumps(result, indent=2, sort_keys=True))


async def _prune_stale(
    args: argparse.Namespace,
    backend: SeatBackend,
    registry: dict[str, Any],
    seat_registry: dict[str, Any],
) -> None:
    if args.older_than_days < 1:
        raise RegistryError("--older-than-days must be positive")
    protected = _protected_names(args.protected)
    boards = _active_boards(registry, backend.home_board)
    rows, memberships, snapshots = await _inventory(backend, boards)
    rows_by_principal: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_principal.setdefault(row["principal_id"], []).append(row)
    memberships_by_principal: dict[str, list[dict[str, Any]]] = {}
    for board_id, result in memberships.items():
        for member in result.get("members", []):
            principal_id = member.get("principal_id")
            if not isinstance(principal_id, str):
                continue
            memberships_by_principal.setdefault(principal_id, []).append(
                {
                    "board_id": board_id,
                    "role": str(member.get("role", "unknown")),
                    "agent_names": [
                        name
                        for name in member.get("agent_names", [])
                        if isinstance(name, str) and name
                    ],
                }
            )
    durable_by_principal: dict[str, list[tuple[str, str]]] = {}
    for name, definition in seat_registry["seats"].items():
        durable_by_principal.setdefault(definition["principal_id"], []).append(
            (name, definition["role"])
        )

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)
    plan: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for principal, principal_memberships in sorted(
        memberships_by_principal.items()
    ):
        principal_rows = rows_by_principal.get(principal, [])
        names = sorted(
            {
                name
                for membership in principal_memberships
                for name in membership["agent_names"]
            }
        )
        durable = durable_by_principal.get(principal, [])
        all_names = sorted(set(names) | {name for name, _role in durable})
        roles = {membership["role"] for membership in principal_memberships} | {
            role for _name, role in durable
        }
        reasons = []
        if roles & {"reviewer", "admin"}:
            reasons.append("protected-role")
        if protected & set(all_names):
            reasons.append("protected-name")
        missing_activity_boards = sorted(
            membership["board_id"]
            for membership in principal_memberships
            if not membership["agent_names"]
            or any(
                not any(
                    row["board_id"] == membership["board_id"]
                    and row["name"] == name
                    for row in principal_rows
                )
                for name in membership["agent_names"]
            )
        )
        if missing_activity_boards:
            reasons.append("missing-activity-evidence")
        observed = [_parse_time(row.get("last_seen")) for row in principal_rows]
        if any(value is None for value in observed):
            reasons.append("unknown-last-seen")
        latest = max((value for value in observed if value is not None), default=None)
        if latest is not None and latest >= cutoff:
            reasons.append("not-stale")
        if reasons:
            excluded.append(
                {
                    "principal_id": principal,
                    "names": all_names,
                    "reasons": sorted(set(reasons)),
                    "missing_activity_boards": missing_activity_boards,
                }
            )
            continue
        target_boards = sorted(
            {membership["board_id"] for membership in principal_memberships}
        )
        plan.append(
            {
                "principal_id": principal,
                "names": all_names,
                "boards": target_boards,
                "last_seen": latest.isoformat() if latest is not None else None,
            }
        )

    principals = {item["principal_id"] for item in plan}
    claims = _active_claims(snapshots, principals)
    claims_by_principal: dict[str, list[dict[str, str]]] = {}
    for claim in claims:
        claims_by_principal.setdefault(claim["principal_id"], []).append(claim)
    for item in plan:
        item["active_claims"] = claims_by_principal.get(item["principal_id"], [])

    if not args.commit:
        print(
            json.dumps(
                {
                    "action": "prune-stale",
                    "mode": "dry-run",
                    "older_than_days": args.older_than_days,
                    "protected": sorted(protected),
                    "plan": plan,
                    "excluded": excluded,
                    "writes": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if claims:
        listed = _format_active_claims(claims)
        raise RegistryError(f"active claims prevent prune commit: {listed}")

    operations = [
        (board_id, item["principal_id"])
        for item in plan
        for board_id in item["boards"]
    ]
    verified = await _remove_many_and_verify(
        backend, operations, action="prune-stale"
    )
    try:
        remaining_memberships = await _remaining_memberships(
            backend, boards, principals
        )
    except Exception as exc:
        _print_mutation_failure(
            action="prune-stale",
            verified=verified,
            failed_board=None,
            failed_principal_id=None,
            pending_boards=[],
            error=exc,
        )
        raise RegistryError(
            "prune-stale final membership read-back failed; "
            "see structured audit output"
        ) from exc
    if remaining_memberships:
        listed = ", ".join(
            f"{item['board_id']}/{item['principal_id']}"
            for item in remaining_memberships
        )
        error = RegistryError(
            "membership read-back remains after prune; preserving seat_registry: "
            f"{listed}"
        )
        _print_mutation_failure(
            action="prune-stale",
            verified=verified,
            failed_board=remaining_memberships[0]["board_id"],
            failed_principal_id=remaining_memberships[0]["principal_id"],
            pending_boards=[],
            error=error,
        )
        raise error
    updated_registry = json.loads(json.dumps(seat_registry))
    removed_definitions = sorted(
        name
        for name, definition in seat_registry["seats"].items()
        if definition["principal_id"] in principals
    )
    for name in removed_definitions:
        del updated_registry["seats"][name]
    registry_updated = updated_registry != seat_registry
    if registry_updated:
        await backend.write_seat_registry(updated_registry)
    print(
        json.dumps(
            {
                "action": "prune-stale",
                "mode": "commit",
                "older_than_days": args.older_than_days,
                "plan": plan,
                "excluded": excluded,
                "seat_registry_definitions_removed": removed_definitions,
                "seat_registry_read_back_verified": registry_updated,
                "verified_read_back": verified,
            },
            indent=2,
            sort_keys=True,
        )
    )


async def execute(args: argparse.Namespace, backend: SeatBackend) -> None:
    _identifier(backend.home_board, "home board id")
    if args.command == "add":
        _identifier(args.name, "agent name")
        if not args.principal:
            raise RegistryError(
                "--principal is required after the operator mints the token; "
                "token minting remains operator-run"
            )
        _identifier(args.principal, "principal id")
        if not args.token_path:
            raise RegistryError(
                "--token-path or ONBOARD_TOKEN_FILE is required; "
                "the target token itself must not be passed"
            )
        token_path = _clean(args.token_path, "token path")
        if not os.path.isabs(token_path):
            raise RegistryError("token path must be absolute")
        if args.boards != "registry":
            for item in args.boards.split(","):
                _identifier(item, "board id")
    elif args.command in {"check", "retire", "dedupe"}:
        _identifier(args.name, "agent name")
        if args.command == "retire" and args.principal is not None:
            _identifier(args.principal, "principal id")
        if args.command == "dedupe":
            _identifier(args.keep_principal, "principal id")
    elif args.command == "prune-stale":
        _protected_names(args.protected)
    else:  # new-board
        _identifier(args.board, "board id")

    registry = await backend.registry()
    seat_registry = _validate_seat_registry(await backend.seat_registry())
    if args.command == "retire":
        await _retire(args, backend, registry, seat_registry)
        return
    if args.command == "dedupe":
        await _dedupe(args, backend, registry, seat_registry)
        return
    if args.command == "prune-stale":
        await _prune_stale(args, backend, registry, seat_registry)
        return

    active_boards = _active_boards(registry, backend.home_board)
    rows = await _seat_rows(backend, active_boards)

    if args.command == "check":
        name = _identifier(args.name, "agent name")
        definition = seat_registry["seats"].get(name)
        actual_rows = [row for row in rows if row["name"] == name]
        board_rows: list[dict[str, str | None]] = list(actual_rows)
        if definition is not None:
            targets = (
                active_boards
                if definition["board_mode"] == "registry"
                else definition["board_mode"]
            )
            for board_id in targets:
                if any(row["board_id"] == board_id for row in actual_rows):
                    continue
                board_rows.append(
                    {
                        "board_id": board_id,
                        "principal_id": definition["principal_id"],
                        "name": name,
                        "role": await _current_role(
                            backend, board_id, definition["principal_id"]
                        ),
                        "status": "pending",
                    }
                )
        print(
            json.dumps(
                {
                    "name": name,
                    "definition": definition,
                    "boards": board_rows,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    if args.command == "add":
        name = _identifier(args.name, "agent name")
        duplicates = [row for row in rows if row["name"] == name]
        existing_definition = seat_registry["seats"].get(name)
        if (duplicates or existing_definition is not None) and not args.force:
            locations = sorted({row["board_id"] for row in duplicates})
            where = ", ".join(locations) if locations else "seat_registry"
            raise RegistryError(
                f"agent name {name!r} is already in use on {where}; pass --force"
            )
        if not args.principal:
            raise RegistryError(
                "--principal is required after the operator mints the token; "
                "token minting remains operator-run"
            )
        principal = _identifier(args.principal, "principal id")
        if not args.token_path:
            raise RegistryError(
                "--token-path or ONBOARD_TOKEN_FILE is required; "
                "the target token itself must not be passed"
            )
        token_path = _clean(args.token_path, "token path")
        if not os.path.isabs(token_path):
            raise RegistryError("token path must be absolute")
        boards = _parse_boards(args.boards, registry, backend.home_board)
        for other_name, definition in seat_registry["seats"].items():
            if (
                other_name != name
                and definition["principal_id"] == principal
                and definition["role"] != args.role
            ):
                raise RegistryError(
                    f"principal {principal!r} already backs {other_name!r} as "
                    f"{definition['role']}; refusing role conflict"
                )
        operations = [(board_id, principal, args.role) for board_id in boards]
        await _provision_many(backend, operations)
        updated_registry = json.loads(json.dumps(seat_registry))
        updated_registry["seats"][name] = {
            "principal_id": principal,
            "role": args.role,
            "board_mode": "registry" if args.boards == "registry" else boards,
        }
        if updated_registry != seat_registry:
            await backend.write_seat_registry(updated_registry)
        print(
            _config(
                central_url=args.central_url,
                home_board=backend.home_board,
                boards=args.boards,
                name=name,
                token_path=token_path,
            )
        )
        return

    board_id = _identifier(args.board, "board id")
    principals: dict[str, str] = {}
    for definition in seat_registry["seats"].values():
        principal_id = definition["principal_id"]
        role = definition["role"]
        current = principals.get(principal_id)
        if current is not None and current != role:
            raise RegistryError(
                f"principal {principal_id!r} has conflicting durable seat roles"
            )
        principals[principal_id] = role
    for row in rows:
        if row["role"] == "admin" and row["principal_id"] in principals:
            continue
        observed_role = "reviewer" if row["role"] == "reviewer" else "worker"
        current = principals.get(row["principal_id"])
        if current is not None and current != observed_role:
            raise RegistryError(
                f"principal {row['principal_id']!r} has conflicting observed role"
            )
        principals[row["principal_id"]] = observed_role
    operations = [
        (board_id, principal_id, role)
        for principal_id, role in sorted(principals.items())
    ]
    await _provision_many(backend, operations)
    print(
        json.dumps(
            {"board_id": board_id, "principals_provisioned": len(principals)},
            indent=2,
            sort_keys=True,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--central-url",
        default=os.environ.get("ONBOARD_CENTRAL_URL", CENTRAL_URL_DEFAULT),
    )
    parser.add_argument(
        "--home-board",
        default=os.environ.get("ONBOARD_BOARD_ID", HOME_BOARD_ID),
    )
    parser.add_argument(
        "--admin-name",
        default=os.environ.get("ONBOARD_AGENT_NAME", "seat-admin"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add")
    add.add_argument("--name", required=True)
    add.add_argument("--role", choices=sorted(VALID_ROLES), required=True)
    add.add_argument("--boards", default="registry")
    add.add_argument("--principal")
    add.add_argument(
        "--token-path",
        default=os.environ.get("ONBOARD_TOKEN_FILE"),
    )
    add.add_argument("--force", action="store_true")

    check = subparsers.add_parser("check")
    check.add_argument("--name", required=True)

    retire = subparsers.add_parser("retire")
    retire.add_argument("--name", required=True)
    retire.add_argument("--boards", default="registry")
    retire.add_argument("--principal")
    retire.add_argument(
        "--stale-seconds",
        type=int,
        default=int(os.environ.get("PURSERS_STALE_SECONDS", DEFAULT_STALE_SECONDS)),
    )
    retire.add_argument("--force", action="store_true")

    dedupe = subparsers.add_parser("dedupe")
    dedupe.add_argument("--name", required=True)
    dedupe.add_argument("--keep-principal", required=True)
    dedupe.add_argument("--commit", action="store_true")

    prune = subparsers.add_parser("prune-stale")
    prune.add_argument("--older-than-days", type=int, required=True)
    prune.add_argument(
        "--protect",
        "--protected",
        dest="protected",
        action="append",
        default=[],
        metavar="NAME[,NAME...]",
    )
    mode = prune.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="commit", action="store_false")
    mode.add_argument("--commit", action="store_true")
    prune.set_defaults(commit=False)

    new_board = subparsers.add_parser("new-board")
    new_board.add_argument("--board", required=True)
    return parser


async def run(args: argparse.Namespace) -> None:
    token = os.environ.get("ONBOARD_CENTRAL_TOKEN", "")
    if not token:
        raise RegistryError("ONBOARD_CENTRAL_TOKEN is not set")
    _identifier(args.home_board, "home board id")
    _identifier(args.admin_name, "admin agent name")
    backend = LiveBackend(
        args.central_url,
        token,
        args.home_board,
        args.admin_name,
    )
    await execute(args, backend)


def _safe_error(exc: BaseException, token: str) -> str:
    return str(exc).replace(token, "[REDACTED]") if token else str(exc)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 - CLI boundary renders a safe error.
        token = os.environ.get("ONBOARD_CENTRAL_TOKEN", "")
        print(f"ERROR: {_safe_error(exc, token)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
