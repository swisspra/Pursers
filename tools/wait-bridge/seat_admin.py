#!/usr/bin/env python3
"""Provision and inspect registry-backed Pursers worker/reviewer seats."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import sys
from collections.abc import Callable, Sequence
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
        if project["status"] == "active" and board_id not in boards:
            boards.append(board_id)
    return boards


def _parse_boards(raw: str, registry: dict[str, Any], home_board: str) -> list[str]:
    if raw == "registry":
        return _active_boards(registry, home_board)
    boards = [_identifier(item, "board id") for item in raw.split(",")]
    if len(set(boards)) != len(boards):
        raise RegistryError("board list contains duplicates")
    return boards


async def _seat_rows(backend: SeatBackend, boards: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for board_id in boards:
        members = await backend.members(board_id)
        snapshots = await backend.snapshot(board_id)
        omitted_agents = int(snapshots.get("omitted_counts", {}).get("agents", 0))
        if omitted_agents:
            raise RegistryError(
                f"agent scan on {board_id!r} omitted {omitted_agents} rows; "
                "refusing an incomplete duplicate-name check"
            )
        statuses = {
            (agent.get("principal_id"), agent.get("agent_name")): agent.get(
                "status", "unknown"
            )
            for agent in snapshots.get("agents", [])
        }
        for member in members.get("members", []):
            for name in member.get("agent_names", []):
                rows.append(
                    {
                        "board_id": board_id,
                        "principal_id": member["principal_id"],
                        "name": name,
                        "role": member["role"],
                        "status": str(
                            statuses.get((member["principal_id"], name), "inactive")
                        ),
                    }
                )
    return rows


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
    elif args.command == "check":
        _identifier(args.name, "agent name")
    else:
        _identifier(args.board, "board id")

    registry = await backend.registry()
    active_boards = _active_boards(registry, backend.home_board)
    seat_registry = _validate_seat_registry(await backend.seat_registry())
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
    except Exception as exc:
        token = os.environ.get("ONBOARD_CENTRAL_TOKEN", "")
        print(f"ERROR: {_safe_error(exc, token)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
