"""Project-registry parsing and subscription wait helpers."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .client import GENERATION_META_KEY, BoardClient, BoardClientError


PROJECT_REGISTRY_KEY = "project_registry"
PROJECT_REGISTRY_SCHEMA_VERSION = 1
PROJECT_STATUSES = frozenset({"active", "paused"})


def parse_project_registry(result: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize Central's string-valued registry state entry."""
    state = result.get("state")
    if not isinstance(state, dict):
        raise ValueError("project_registry state entry is missing")
    raw_value = state.get("value")
    if not isinstance(raw_value, str):
        raise ValueError("project_registry state value must be a JSON string")
    try:
        registry = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("project_registry state value is not valid JSON") from exc
    if not isinstance(registry, dict):
        raise ValueError("project_registry must be a JSON object")
    schema_version = registry.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version != PROJECT_REGISTRY_SCHEMA_VERSION
    ):
        raise ValueError(
            f"project_registry schema_version must be {PROJECT_REGISTRY_SCHEMA_VERSION}"
        )
    projects = registry.get("projects")
    if not isinstance(projects, dict):
        raise ValueError("project_registry projects must be an object")
    normalized: dict[str, dict[str, str]] = {}
    for name, project in projects.items():
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError("project_registry project names must be non-empty strings")
        if not isinstance(project, dict):
            raise ValueError(f"project_registry project {name!r} must be an object")
        board_id = project.get("board_id")
        work_dir = project.get("work_dir")
        status = project.get("status")
        if not isinstance(board_id, str) or not board_id or board_id != board_id.strip():
            raise ValueError(f"project_registry project {name!r} has an invalid board_id")
        if not isinstance(work_dir, str) or not os.path.isabs(work_dir):
            raise ValueError(
                f"project_registry project {name!r} work_dir must be absolute"
            )
        if status not in PROJECT_STATUSES:
            raise ValueError(
                f"project_registry project {name!r} status must be active or paused"
            )
        normalized[name] = {
            "board_id": board_id,
            "work_dir": work_dir,
            "status": status,
        }
    return {"schema_version": PROJECT_REGISTRY_SCHEMA_VERSION, "projects": normalized}


def active_registry_boards(registry: dict[str, Any], home_board: str) -> list[str]:
    selected = {home_board}
    selected.update(
        project["board_id"]
        for project in registry["projects"].values()
        if project["status"] == "active"
    )
    return sorted(selected)


def registry_work_dirs(registry: dict[str, Any]) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for project in registry["projects"].values():
        if project["status"] == "active":
            candidates.setdefault(project["board_id"], set()).add(project["work_dir"])
    return {
        board_id: next(iter(paths))
        for board_id, paths in candidates.items()
        if len(paths) == 1
    }


def registry_project_work_dirs(registry: dict[str, Any]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for name, project in registry["projects"].items():
        if project["status"] != "active":
            continue
        selected[name.casefold()] = project["work_dir"]
        selected[Path(project["work_dir"]).name.casefold()] = project["work_dir"]
    return selected


def _cursors(boards: list[str], since: int | dict[str, int], home: str) -> dict[str, int]:
    if isinstance(since, dict):
        return {board: max(0, int(since.get(board, 0))) for board in boards}
    value = max(0, int(since))
    return {board: value if board == home else 0 for board in boards}


async def wait_for_boards(
    client: BoardClient,
    boards: Iterable[str],
    since: int | dict[str, int],
    timeout_s: int,
    *,
    kinds: Iterable[str],
    submitted: bool,
    work_dirs: dict[str, str] | None = None,
    project_work_dirs: dict[str, str] | None = None,
    poll_fallback: bool = False,
) -> dict[str, Any]:
    """Wait on all authorized board journals in one listen subscription."""
    board_ids = sorted({str(board).strip() for board in boards if str(board).strip()})
    cursors = _cursors(board_ids, since, client.board_id)
    skipped: dict[str, str] = {}
    identities: dict[str, str] = {}
    generations: dict[str, str | None] = {}
    if client._client is None:  # package helper; BoardClient must be entered
        raise RuntimeError("BoardClient is not entered")
    for board_id in board_ids:
        try:
            joined = BoardClient._decode(
                await client._client.call_tool(
                    "board_join",
                    {"board_id": board_id, "agent_name": client.agent_name},
                )
            )
            identities[board_id] = joined["agent_id"]
            generations[board_id] = joined.get("generation_token")
        except BoardClientError as exc:
            skipped[board_id] = str(exc)
    active = [board for board in board_ids if board in identities]
    selected_kinds = frozenset(kinds)
    work_dirs = work_dirs or {}
    project_work_dirs = project_work_dirs or {}
    started = time.monotonic()

    async def drain(raw: Client, board_id: str) -> list[dict[str, Any]]:
        found = []
        while True:
            arguments = {
                "board_id": board_id,
                "agent_name": client.agent_name,
                "cursor": cursors[board_id],
                "limit": 100,
                "ack": False,
                "touch": False,
            }
            generation = generations.get(board_id)
            result = BoardClient._decode(
                await raw.call_tool(
                    "board_catchup",
                    arguments,
                    **({"meta": {GENERATION_META_KEY: generation}} if generation else {}),
                )
            )
            cursors[board_id] = int(result.get("next_cursor", cursors[board_id]))
            for event in result.get("events", []):
                if event.get("kind") not in selected_kinds:
                    continue
                if submitted:
                    if event.get("status_to") != "submitted":
                        continue
                elif identities[board_id] not in event.get("recipient_identities", []):
                    continue
                work_dir = work_dirs.get(board_id)
                ticket_id = event.get("ticket_id")
                if ticket_id:
                    try:
                        ticket_result = BoardClient._decode(
                            await raw.call_tool(
                                "ticket_get",
                                {"board_id": board_id, "ticket_id": ticket_id},
                            )
                        )
                        target = str(ticket_result.get("ticket", {}).get("target_url", ""))
                        project = target.split("/", 1)[0].casefold()
                        work_dir = project_work_dirs.get(project, work_dir)
                    except BoardClientError:
                        pass
                found.append({**event, "board_id": board_id, "work_dir": work_dir})
            if not result.get("has_more"):
                return found
        return found

    async def response(events: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "new_seq": dict(cursors),
            "events": events,
            "timed_out": not events,
            "waited_s": round(time.monotonic() - started, 2),
            "boards": active,
            "skipped_boards": skipped,
        }

    if not active:
        return await response([])
    if poll_fallback:
        deadline = started + timeout_s
        while time.monotonic() < deadline:
            events: list[dict[str, Any]] = []
            for board_id in active:
                events.extend(await drain(client._client, board_id))
            if events:
                return await response(events)
            await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
        return await response([])

    resources = [f"board://{board}/journal" for board in active]
    async with client._http() as http:
        transport = streamable_http_client(client.url, http_client=http)
        async with Client(transport, mode="2026-07-28", cache=None) as raw:
            async with raw.listen(resource_subscriptions=resources) as subscription:
                events = []
                for board_id in active:
                    events.extend(await drain(raw, board_id))
                if events:
                    return await response(events)
                try:
                    async with asyncio.timeout(timeout_s):
                        async for _cue in subscription:
                            for board_id in active:
                                events.extend(await drain(raw, board_id))
                            if events:
                                return await response(events)
                except TimeoutError:
                    pass
    return await response([])
