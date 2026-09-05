#!/usr/bin/env python3
"""Safely inspect and edit the shared Pursers project registry."""

from __future__ import annotations

import argparse
import asyncio
import copy
import difflib
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from pursers_client import BoardClient


CENTRAL_URL_DEFAULT = "https://127.0.0.1:8766/mcp"
HOME_BOARD_ID = "pursers"
REGISTRY_KEY = "project_registry"
SCHEMA_VERSION = 1
VALID_STATUSES = frozenset({"active", "paused"})
VALID_WORK_DIR_OWNERS = frozenset({"operator", "fleet"})


class RegistryError(RuntimeError):
    """An expected, safe-to-display registry administration failure."""


class RegistryClient(Protocol):
    async def board_state_get(self, key: str | None = None) -> dict[str, Any]: ...

    async def board_state_update(self, key: str, value: str) -> dict[str, Any]: ...


def _require_clean_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RegistryError(f"{label} must be a non-empty, trimmed string")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise RegistryError(f"{label} must not contain control characters")
    return value


def validate_registry(document: Any) -> dict[str, Any]:
    """Return a deep copy of a registry that exactly matches schema v1."""
    if not isinstance(document, dict):
        raise RegistryError("project_registry must be a JSON object")
    if set(document) != {"schema_version", "projects"}:
        raise RegistryError(
            "project_registry must contain exactly schema_version and projects"
        )
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise RegistryError("project_registry schema_version must be 1")
    projects = document["projects"]
    if not isinstance(projects, dict):
        raise RegistryError("project_registry projects must be an object")

    for name, entry in projects.items():
        _require_clean_string(name, "project name")
        required = {"board_id", "work_dir", "status"}
        optional = {"work_dir_owner", "fleet_clone_dir"}
        if (
            not isinstance(entry, dict)
            or not required <= set(entry)
            or not set(entry) <= required | optional
        ):
            raise RegistryError(
                f"project {name!r} must contain exactly board_id, work_dir, and "
                "status plus optional work_dir_owner and fleet_clone_dir; "
                "only work_dir_owner and fleet_clone_dir are optional"
            )
        _require_clean_string(entry["board_id"], f"project {name!r} board_id")
        work_dir = _require_clean_string(entry["work_dir"], f"project {name!r} work_dir")
        status = entry["status"]
        if not os.path.isabs(work_dir):
            raise RegistryError(f"project {name!r} work_dir must be an absolute path")
        if not isinstance(status, str) or status not in VALID_STATUSES:
            raise RegistryError(
                f"project {name!r} status must be active or paused"
            )
        owner = entry.get("work_dir_owner", "operator")
        if owner not in VALID_WORK_DIR_OWNERS:
            raise RegistryError(
                f"project {name!r} work_dir_owner must be operator or fleet"
            )
        fleet_clone_dir = entry.get("fleet_clone_dir")
        if fleet_clone_dir is not None:
            fleet_clone_dir = _require_clean_string(
                fleet_clone_dir, f"project {name!r} fleet_clone_dir"
            )
            if not os.path.isabs(fleet_clone_dir):
                raise RegistryError(
                    f"project {name!r} fleet_clone_dir must be an absolute path"
                )

    return copy.deepcopy(document)


def _registry_from_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RegistryError("board_state_get returned a non-object response")
    state = result.get("state")
    if not isinstance(state, dict):
        raise RegistryError("project_registry state entry is missing")
    raw_value = state.get("value")
    if not isinstance(raw_value, str):
        raise RegistryError("project_registry state value must be a JSON string")
    try:
        document = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RegistryError("project_registry state value is not valid JSON") from exc
    return validate_registry(document)


async def read_registry(client: RegistryClient) -> dict[str, Any]:
    return _registry_from_result(await client.board_state_get(REGISTRY_KEY))


def _render(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)


async def write_and_verify(
    client: RegistryClient, expected: dict[str, Any]
) -> dict[str, Any]:
    validated = validate_registry(expected)
    await client.board_state_update(
        REGISTRY_KEY,
        json.dumps(validated, separators=(",", ":"), sort_keys=True),
    )
    try:
        actual = await read_registry(client)
    except RegistryError as exc:
        raise RegistryError(f"project_registry read-back failed: {exc}") from exc
    if actual != validated:
        difference = "\n".join(
            difflib.unified_diff(
                _render(validated).splitlines(),
                _render(actual).splitlines(),
                fromfile="expected",
                tofile="read-back",
                lineterm="",
            )
        )
        raise RegistryError(
            "project_registry read-back mismatch; stored document differs from write"
            + (f"\n{difference}" if difference else "")
        )
    return actual


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely inspect and edit the Pursers project registry."
    )
    parser.add_argument(
        "--central-url",
        default=os.environ.get("ONBOARD_CENTRAL_URL", CENTRAL_URL_DEFAULT),
        help="Central MCP URL (default: ONBOARD_CENTRAL_URL or localhost)",
    )
    parser.add_argument(
        "--agent-name",
        default=os.environ.get("ONBOARD_AGENT_NAME", "project-registry-admin"),
        help="board identity used for the operation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("show", help="validate and print the current registry")

    add = subparsers.add_parser("add", help="add or replace a project")
    add.add_argument("name")
    add.add_argument("--board-id", required=True)
    add.add_argument("--work-dir", required=True)
    add.add_argument(
        "--work-dir-owner",
        choices=sorted(VALID_WORK_DIR_OWNERS),
    )
    add.add_argument("--fleet-clone-dir")
    add.add_argument("--status", choices=sorted(VALID_STATUSES), default="active")
    add.add_argument(
        "--force",
        action="store_true",
        help="replace an existing project with the same name",
    )

    for command in ("pause", "activate", "remove"):
        action = subparsers.add_parser(command)
        action.add_argument("name")
    return parser


async def execute(args: argparse.Namespace, client: RegistryClient) -> None:
    current = await read_registry(client)
    if args.command == "show":
        print(_render(current))
        return

    name = _require_clean_string(args.name, "project name")
    projects = current["projects"]
    if args.command == "add":
        board_id = _require_clean_string(args.board_id, "board_id")
        work_dir = _require_clean_string(args.work_dir, "work_dir")
        if not os.path.isabs(work_dir):
            raise RegistryError("work_dir must be an absolute path")
        if name in projects and not args.force:
            raise RegistryError(
                f"project {name!r} already exists; pass --force to replace it"
            )
        projects[name] = {
            "board_id": board_id,
            "work_dir": work_dir,
            "status": args.status,
        }
        if args.work_dir_owner is not None:
            projects[name]["work_dir_owner"] = args.work_dir_owner
        if args.fleet_clone_dir is not None:
            clone_dir = _require_clean_string(args.fleet_clone_dir, "fleet_clone_dir")
            if not os.path.isabs(clone_dir):
                raise RegistryError("fleet_clone_dir must be an absolute path")
            projects[name]["fleet_clone_dir"] = clone_dir
    else:
        if name not in projects:
            raise RegistryError(f"unknown project {name!r}")
        if args.command == "pause":
            projects[name]["status"] = "paused"
        elif args.command == "activate":
            projects[name]["status"] = "active"
        elif args.command == "remove":
            removed = projects.pop(name)
            verified = await write_and_verify(client, current)
            print("Removed entry (save this JSON to restore it by hand):")
            print(_render({name: removed}))
            print("Verified registry:")
            print(_render(verified))
            return
        else:  # pragma: no cover - argparse prevents this path
            raise RegistryError(f"unsupported command {args.command!r}")

    verified = await write_and_verify(client, current)
    print(_render(verified))


ClientFactory = Callable[..., Any]


async def run(args: argparse.Namespace, client_factory: ClientFactory = BoardClient) -> None:
    token = os.environ.get("ONBOARD_CENTRAL_TOKEN", "")
    if not token:
        raise RegistryError("ONBOARD_CENTRAL_TOKEN is not set")
    async with client_factory(
        args.central_url,
        token,
        HOME_BOARD_ID,
        agent_name=args.agent_name,
    ) as client:
        await execute(args, client)


def _safe_error(exc: BaseException, token: str) -> str:
    message = str(exc)
    return message.replace(token, "[REDACTED]") if token else message


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
