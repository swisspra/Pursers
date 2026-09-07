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
from .events import (
    HELD_TICKET_KINDS,
    OFFER_EXPIRED,
    OFFER_REVOKED,
    REVIEW_LEASE_KINDS,
    REVIEW_OFFERED,
    TICKET_OFFERED,
)


PROJECT_REGISTRY_KEY = "project_registry"
PROJECT_REGISTRY_SCHEMA_VERSION = 1
PROJECT_STATUSES = frozenset({"active", "paused"})
WORK_DIR_OWNERS = frozenset({"operator", "fleet"})
CATCHUP_PAGE_LIMIT = 100
MAX_CATCHUP_PAGES_PER_BOARD = 8
MAX_EVENTS_PER_BOARD = 1


def _held_ticket_update(
    ticket: dict[str, Any],
    event: dict[str, Any],
    agent_id: str,
    *,
    submitted: bool,
) -> bool:
    """Return whether an authoritative holder-targeted event belongs to a seat."""
    kind = event.get("kind")
    if kind not in HELD_TICKET_KINDS and not (
        submitted and kind in REVIEW_LEASE_KINDS
    ):
        return False
    review_lease = ticket.get("review_lease")
    human_request = ticket.get("human_request")
    if submitted:
        return bool(
            event.get("reviewer_agent_id") == agent_id
            or (
                isinstance(review_lease, dict)
                and review_lease.get("reviewer_agent_id") == agent_id
            )
        )
    return bool(
        ticket.get("claimed_by_agent_id") == agent_id
        or event.get("submitted_by_agent_id") == agent_id
        or event.get("last_abandoned_by") == agent_id
        or (
            ticket.get("claimed_by_agent_id") is None
            and ticket.get("last_claimed_by_agent_id") == agent_id
        )
        or (
            isinstance(human_request, dict)
            and human_request.get("asked_by", {}).get("agent_id") == agent_id
        )
    )


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
    normalized: dict[str, dict[str, Any]] = {}
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
        owner = project.get("work_dir_owner", "operator")
        if owner not in WORK_DIR_OWNERS:
            raise ValueError(
                f"project_registry project {name!r} work_dir_owner must be "
                "operator or fleet"
            )
        fleet_clone_dir = project.get("fleet_clone_dir")
        if fleet_clone_dir is not None and (
            not isinstance(fleet_clone_dir, str)
            or not os.path.isabs(fleet_clone_dir)
        ):
            raise ValueError(
                f"project_registry project {name!r} fleet_clone_dir must be absolute"
            )
        if "fleet" in project and type(project["fleet"]) is not bool:
            raise ValueError(
                f"project_registry project {name!r} fleet must be boolean"
            )
        normalized[name] = {
            "board_id": board_id,
            "work_dir": work_dir,
            "status": status,
        }
        if "work_dir_owner" in project:
            normalized[name]["work_dir_owner"] = owner
        if fleet_clone_dir is not None:
            normalized[name]["fleet_clone_dir"] = fleet_clone_dir
        if "fleet" in project:
            normalized[name]["fleet"] = project["fleet"]
    return {"schema_version": PROJECT_REGISTRY_SCHEMA_VERSION, "projects": normalized}


def active_registry_boards(registry: dict[str, Any], home_board: str) -> list[str]:
    selected = {home_board}
    selected.update(
        project["board_id"]
        for project in registry["projects"].values()
        if project["status"] == "active" and project.get("fleet", True)
    )
    return sorted(selected)


def registry_work_dirs(registry: dict[str, Any]) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for project in registry["projects"].values():
        if project["status"] == "active" and project.get("fleet", True):
            candidates.setdefault(project["board_id"], set()).add(
                project.get("fleet_clone_dir") or project["work_dir"]
            )
    return {
        board_id: next(iter(paths))
        for board_id, paths in candidates.items()
        if len(paths) == 1
    }


def registry_project_work_dirs(registry: dict[str, Any]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for name, project in registry["projects"].items():
        if project["status"] != "active" or not project.get("fleet", True):
            continue
        work_dir = project.get("fleet_clone_dir") or project["work_dir"]
        selected[name.casefold()] = work_dir
        selected[Path(project["work_dir"]).name.casefold()] = work_dir
    return selected


def registry_operator_work_dirs(registry: dict[str, Any]) -> dict[str, str]:
    """Return unambiguous active operator-owned checkouts by board ID."""
    candidates: dict[str, set[str]] = {}
    for project in registry["projects"].values():
        if (
            project["status"] == "active"
            and project.get("fleet", True)
            and project.get("work_dir_owner", "operator") == "operator"
        ):
            candidates.setdefault(project["board_id"], set()).add(project["work_dir"])
    return {
        board_id: next(iter(paths))
        for board_id, paths in candidates.items()
        if len(paths) == 1
    }


def registry_project_operator_work_dirs(
    registry: dict[str, Any],
) -> dict[str, str]:
    """Return active operator-owned checkout paths by project routing key."""
    selected: dict[str, str] = {}
    for name, project in registry["projects"].items():
        if (
            project["status"] != "active"
            or not project.get("fleet", True)
            or project.get("work_dir_owner", "operator") != "operator"
        ):
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
    capabilities: dict[str, Any] | None = None,
    allow_takeover: bool = False,
) -> dict[str, Any]:
    """Wait on all authorized board journals in one listen subscription."""
    board_ids = sorted({str(board).strip() for board in boards if str(board).strip()})
    cursors = _cursors(board_ids, since, client.board_id)
    skipped: dict[str, str] = {}
    identities: dict[str, str] = {}
    generations: dict[str, str | None] = {}
    if client._client is None:  # package helper; BoardClient must be entered
        raise RuntimeError("BoardClient is not entered")
    sessions = getattr(client, "_registry_wait_sessions", None)
    if not isinstance(sessions, dict):
        sessions = {}
        setattr(client, "_registry_wait_sessions", sessions)
    for board_id in board_ids:
        if board_id == client.board_id and client.identity is not None:
            identities[board_id] = client.identity.agent_id
            generations[board_id] = getattr(client, "generation_token", None)
            continue
        cached = sessions.get(board_id)
        if (
            isinstance(cached, dict)
            and cached.get("agent_name") == client.agent_name
            and cached.get("capabilities") == capabilities
        ):
            identities[board_id] = cached["agent_id"]
            generations[board_id] = cached.get("generation_token")
            continue
        try:
            join_arguments: dict[str, Any] = {
                "board_id": board_id,
                "agent_name": client.agent_name,
            }
            if capabilities is not None:
                join_arguments["capabilities"] = capabilities
            if allow_takeover:
                join_arguments["allow_takeover"] = True
            joined = BoardClient._decode(
                await client._client.call_tool(
                    "board_join",
                    join_arguments,
                )
            )
            identities[board_id] = joined["agent_id"]
            generations[board_id] = joined.get("generation_token")
            sessions[board_id] = {
                "agent_name": client.agent_name,
                "capabilities": (
                    dict(capabilities) if capabilities is not None else None
                ),
                "agent_id": joined["agent_id"],
                "generation_token": joined.get("generation_token"),
            }
        except BoardClientError as exc:
            sessions.pop(board_id, None)
            skipped[board_id] = str(exc)
    active = [board for board in board_ids if board in identities]
    if not active:
        details = "\n".join(
            f"{board_id}: {skipped.get(board_id, 'join failed')}"
            for board_id in board_ids
        )
        raise BoardClientError(f"all selected boards were skipped:\n{details}")
    selected_kinds = frozenset(kinds)
    work_dirs = work_dirs or {}
    project_work_dirs = project_work_dirs or {}
    started = time.monotonic()

    async def drain(raw: Client, board_id: str) -> tuple[list[dict[str, Any]], bool]:
        """Read bounded pages and return at most one relevant event for this board."""
        for _page_number in range(MAX_CATCHUP_PAGES_PER_BOARD):
            initial_cursor = cursors[board_id]
            arguments = {
                "board_id": board_id,
                "agent_name": client.agent_name,
                "cursor": initial_cursor,
                "limit": CATCHUP_PAGE_LIMIT,
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
            if result.get("resync_required"):
                reset_cursor = result.get("reset_cursor")
                if type(reset_cursor) is not int:
                    raise RuntimeError(
                        "board_catchup resync is missing an integer reset_cursor"
                    )
                cursors[board_id] = max(initial_cursor, reset_cursor)
                return [], False
            page = result.get("events", [])
            found: list[dict[str, Any]] = []
            for index, event in enumerate(page):
                event_seq = event.get("seq")
                if type(event_seq) is not int:
                    raise RuntimeError("board_catchup event is missing an integer seq")
                work_dir = work_dirs.get(board_id)
                enriched = {**event, "board_id": board_id, "work_dir": work_dir}
                ticket_id = event.get("ticket_id")
                ticket: dict[str, Any] = {}
                if ticket_id:
                    try:
                        ticket_result = BoardClient._decode(
                            await raw.call_tool(
                                "ticket_get",
                                {"board_id": board_id, "ticket_id": ticket_id},
                            )
                        )
                        target = str(
                            ticket_result.get("ticket", {}).get("target_url", "")
                        )
                        ticket = ticket_result.get("ticket", {})
                        project = target.split("/", 1)[0].casefold()
                        enriched["work_dir"] = project_work_dirs.get(project, work_dir)
                    except BoardClientError:
                        pass
                kind = event.get("kind")
                relevant = kind in selected_kinds
                if submitted and kind == TICKET_OFFERED:
                    relevant = False
                elif not submitted and kind == REVIEW_OFFERED:
                    relevant = False
                elif kind in {OFFER_EXPIRED, OFFER_REVOKED}:
                    relevant = event.get("offer_kind") == (
                        "review" if submitted else "work"
                    )
                dispatch_state = ticket.get("dispatch_state")
                held_update = _held_ticket_update(
                    ticket,
                    event,
                    identities[board_id],
                    submitted=submitted,
                )
                if relevant and isinstance(dispatch_state, dict):
                    state = dispatch_state.get("state")
                    offer_kind = "review" if submitted else "work"
                    offer = ticket.get(f"{offer_kind}_offer")
                    lifecycle = event.get("kind") in {OFFER_EXPIRED, OFFER_REVOKED}
                    if lifecycle:
                        relevant = (
                            event.get("offer_kind") == offer_kind
                            and event.get("offered_agent_id") == identities[board_id]
                        )
                    elif submitted:
                        lease = ticket.get("review_lease")
                        relevant = bool(
                            (
                                event.get("kind") == REVIEW_OFFERED
                                and isinstance(offer, dict)
                                and offer.get("agent_id") == identities[board_id]
                            )
                            or held_update
                            or (
                                event.get("kind") in REVIEW_LEASE_KINDS
                                and (
                                    event.get("reviewer_agent_id")
                                    == identities[board_id]
                                    or (
                                        isinstance(lease, dict)
                                        and lease.get("reviewer_agent_id")
                                        == identities[board_id]
                                    )
                                )
                            )
                            or (
                                state == "broadcast"
                                and event.get("kind")
                                in {"ticket_status_changed", "ticket_submitted", "ticket_resubmitted"}
                                and event.get("status_to") == "submitted"
                            )
                        )
                    else:
                        relevant = bool(
                            (
                                event.get("kind") == TICKET_OFFERED
                                and isinstance(offer, dict)
                                and offer.get("agent_id") == identities[board_id]
                            )
                            or held_update
                            or (state == "broadcast" and ticket.get("status") == "open")
                        )
                    expected_offer_kind = (
                        REVIEW_OFFERED if submitted else TICKET_OFFERED
                    )
                    if (
                        relevant
                        and event.get("kind") == expected_offer_kind
                        and isinstance(offer, dict)
                    ):
                        enriched["offer"] = {
                            "ticket_id": ticket_id,
                            "board_id": board_id,
                            "expires_at": offer.get("expires_at"),
                            "tier": ticket.get("tier", 2),
                            "skills_required": list(ticket.get("skills_required") or []),
                        }
                    if relevant:
                        enriched["reason"] = (
                            "offer"
                            if event.get("kind") == expected_offer_kind or lifecycle
                            else "held_ticket_update"
                            if held_update
                            else "broadcast"
                        )
                elif relevant:
                    if held_update:
                        enriched["reason"] = "held_ticket_update"
                    elif submitted:
                        relevant = event.get("status_to") == "submitted"
                        if relevant:
                            enriched["reason"] = "broadcast"
                    else:
                        relevant = identities[board_id] in event.get(
                            "recipient_identities", []
                        )
                        if relevant:
                            enriched["reason"] = "broadcast"
                if not relevant:
                    cursors[board_id] = max(cursors[board_id], event_seq)
                    continue
                cursors[board_id] = max(cursors[board_id], event_seq)
                found.append(enriched)
                pending = index + 1 < len(page) or bool(result.get("has_more"))
                return found[:MAX_EVENTS_PER_BOARD], pending

            cursors[board_id] = max(
                cursors[board_id], int(result.get("next_cursor", cursors[board_id]))
            )
            has_more = bool(result.get("has_more"))
            if not has_more:
                return found, False
            if cursors[board_id] <= initial_cursor:
                return found, True
        return [], True

    def response(events: list[dict[str, Any]]) -> dict[str, Any]:
        reason = events[0].get("reason", "held_ticket_update") if events else "timeout"
        return {
            "new_seq": dict(cursors),
            "events": events,
            "timed_out": not events,
            "waited_s": round(time.monotonic() - started, 2),
            "boards": active,
            "skipped_boards": skipped,
            "reason": reason,
        }

    if poll_fallback:
        deadline = started + timeout_s
        while time.monotonic() < deadline:
            events: list[dict[str, Any]] = []
            pending = False
            for board_id in active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return response(events)
                try:
                    async with asyncio.timeout(remaining):
                        board_events, board_pending = await drain(client._client, board_id)
                except TimeoutError:
                    return response(events)
                events.extend(board_events)
                pending = pending or board_pending
            if events or pending:
                return response(events)
            await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
        return response([])

    resources = [
        uri
        for board in active
        for uri in (
            f"board://{board}/journal",
            f"board://{board}/agent/{identities[board]}",
        )
    ]
    events: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(timeout_s):
            async with client._http() as http:
                transport = streamable_http_client(client.url, http_client=http)
                async with Client(transport, mode="2026-07-28", cache=None) as raw:
                    async with raw.listen(resource_subscriptions=resources) as subscription:
                        pending = False
                        for board_id in active:
                            board_events, board_pending = await drain(raw, board_id)
                            events.extend(board_events)
                            pending = pending or board_pending
                        if events or pending:
                            return response(events)
                        async for _cue in subscription:
                            pending = False
                            for board_id in active:
                                board_events, board_pending = await drain(raw, board_id)
                                events.extend(board_events)
                                pending = pending or board_pending
                            if events or pending:
                                return response(events)
    except TimeoutError:
        pass
    return response(events)
