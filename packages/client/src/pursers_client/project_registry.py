"""Project-registry parsing and subscription wait helpers."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

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
WAIT_RECONCILE_LIMIT = 100
try:
    WAIT_RECONCILE_INTERVAL_S = max(
        1.0, float(os.environ.get("PURSERS_WAIT_RECONCILE_INTERVAL_S", "30"))
    )
except ValueError:
    WAIT_RECONCILE_INTERVAL_S = 30.0


class RegistryRoutingError(ValueError):
    """A safe, actionable failure to resolve a ticket target."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _repository_url(value: Any, project_name: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(
            f"project_registry project {project_name!r} repository_url must be "
            "a non-empty, trimmed HTTPS URL"
        )
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path in {"", "/"}
    ):
        raise ValueError(
            f"project_registry project {project_name!r} repository_url must be "
            "a credential-free HTTPS repository URL without query or fragment"
        )
    return value


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
        repository_url = _repository_url(project.get("repository_url"), name)
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
        if repository_url is not None:
            normalized[name]["repository_url"] = repository_url
    return {"schema_version": PROJECT_REGISTRY_SCHEMA_VERSION, "projects": normalized}


def resolve_registry_target(
    registry: dict[str, Any], board_id: str, target_url: str
) -> dict[str, str | None]:
    """Resolve one ticket target within its board, without cross-board fallback."""
    target = str(target_url)
    try:
        parsed = urlsplit(target)
    except ValueError as exc:
        raise RegistryRoutingError(
            "target_url_malformed",
            "target_url must be a valid legacy project/path or credential-free "
            "HTTPS repository URL",
        ) from exc
    repository_target = bool(parsed.scheme or parsed.netloc)
    if repository_target and (
        target != target.strip()
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path in {"", "/"}
    ):
        raise RegistryRoutingError(
            "target_url_malformed",
            "target_url repository URL must be an exact credential-free HTTPS "
            "repository URL without query or fragment",
        )
    active = [
        (name, project)
        for name, project in registry["projects"].items()
        if project["status"] == "active"
        and project.get("fleet", True)
        and project["board_id"] == board_id
    ]
    if repository_target:
        matches = [
            (name, project)
            for name, project in active
            if project.get("repository_url") == target
        ]
        if not matches:
            other_board = any(
                project["status"] == "active"
                and project.get("fleet", True)
                and project.get("repository_url") == target
                for project in registry["projects"].values()
            )
            code = (
                "repository_url_board_mismatch"
                if other_board
                else "repository_url_not_registered"
            )
            raise RegistryRoutingError(
                code,
                f"target_url repository URL is not registered for board {board_id!r}; "
                "configure the project's exact project_registry repository_url",
            )
    else:
        project_key = target.split("/", 1)[0].casefold()
        matches = [
            (name, project)
            for name, project in active
            if project_key
            in {name.casefold(), Path(project["work_dir"]).name.casefold()}
        ]
        if not matches:
            raise RegistryRoutingError(
                "project_route_not_registered",
                f"target_url must begin with a project registered for board {board_id!r}",
            )
    if len(matches) != 1:
        raise RegistryRoutingError(
            (
                "repository_url_ambiguous"
                if repository_target
                else "project_route_ambiguous"
            ),
            f"target_url matches multiple active projects on board {board_id!r}",
        )
    name, project = matches[0]
    return {
        "project": name,
        "board_id": board_id,
        "work_dir": project.get("fleet_clone_dir") or project["work_dir"],
        "operator_work_dir": (
            project["work_dir"]
            if project.get("work_dir_owner", "operator") == "operator"
            else None
        ),
    }


def permanent_registry_claim_refusal(
    registry: dict[str, Any], board_id: str, target_url: str
) -> dict[str, str] | None:
    """Return a permanent worker-claim refusal for an unchanged route.

    Repository availability is deliberately not inspected here.  A registered
    fleet clone that has not appeared yet is transient and must remain visible
    so a later wait can observe it after provisioning completes.
    """
    try:
        route = resolve_registry_target(registry, board_id, target_url)
    except RegistryRoutingError as exc:
        return {"code": exc.code, "message": str(exc)}
    work_dir = route.get("work_dir")
    operator_dir = route.get("operator_work_dir")
    if (
        isinstance(work_dir, str)
        and isinstance(operator_dir, str)
        and Path(work_dir).resolve() == Path(operator_dir).resolve()
    ):
        return {
            "code": "operator_checkout_read_only",
            "message": "operator checkout is read-only for seats",
        }
    return None


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


BOARD_JOIN_DENIAL_RETRY_S = 900.0
"""Seconds a permanently denied board join is remembered before one retry."""

_PERMANENT_JOIN_DENIAL_MARKERS = (
    "invite required",
    "lacks board:",
    "board role not authorized",
    "access denied",
    "not authorized",
    "forbidden",
)


def join_denial_is_permanent(message: str) -> bool:
    """Return whether a board_join refusal is an authorization decision.

    Invite-required boards, missing role scopes, and role refusals do not
    change between two wait calls of the same credential. Treating them as
    transient made every wait cycle re-issue ``board_join`` against every
    denied registry board (thousands of refused joins per seat per day).
    """
    text = str(message).casefold()
    return any(marker in text for marker in _PERMANENT_JOIN_DENIAL_MARKERS)


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
    registry: dict[str, Any] | None = None,
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
        if isinstance(cached, dict) and "denied" in cached:
            if time.monotonic() < float(cached.get("denied_until", 0.0)):
                skipped[board_id] = str(cached["denied"])
                continue
            sessions.pop(board_id, None)
            cached = None
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
            message = str(exc)
            if join_denial_is_permanent(message):
                sessions[board_id] = {
                    "denied": message,
                    "denied_until": time.monotonic() + BOARD_JOIN_DENIAL_RETRY_S,
                }
            else:
                sessions.pop(board_id, None)
            skipped[board_id] = message
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
                        if registry is not None:
                            try:
                                route = resolve_registry_target(
                                    registry, board_id, target
                                )
                                enriched["work_dir"] = route["work_dir"]
                            except RegistryRoutingError as exc:
                                enriched["work_dir"] = None
                                enriched["routing_error"] = {
                                    "code": exc.code,
                                    "message": str(exc),
                                }
                        else:
                            project = target.split("/", 1)[0].casefold()
                            enriched["work_dir"] = project_work_dirs.get(
                                project, work_dir
                            )
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
                if (
                    relevant
                    and not submitted
                    and not held_update
                    and registry is not None
                    and permanent_registry_claim_refusal(
                        registry, board_id, str(ticket.get("target_url", ""))
                    )
                    is not None
                ):
                    relevant = False
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

    async def reconcile(raw: Client, board_id: str) -> list[dict[str, Any]]:
        """Project current claimable state that has no usable journal cue."""
        arguments: dict[str, Any] = {
            "board_id": board_id,
            "status": "submitted" if submitted else "open",
            "include_closed": False,
            "limit": WAIT_RECONCILE_LIMIT,
        }
        if submitted:
            arguments["review_unclaimed_only"] = True
        try:
            listed = BoardClient._decode(
                await raw.call_tool("ticket_list", arguments)
            )
        except (BoardClientError, AttributeError, NotImplementedError):
            return []
        mine = identities[board_id]
        expected_status = "submitted" if submitted else "open"
        offer_key = "review_offer" if submitted else "work_offer"
        offered_kind = REVIEW_OFFERED if submitted else TICKET_OFFERED
        found: list[dict[str, Any]] = []
        for ticket in listed.get("tickets", []):
            if not isinstance(ticket, dict) or ticket.get("status") != expected_status:
                continue
            ticket_id = ticket.get("ticket_id")
            if not isinstance(ticket_id, str) or not ticket_id:
                continue
            offer = ticket.get(offer_key)
            offered_to_me = (
                isinstance(offer, dict) and offer.get("agent_id") == mine
            )
            dispatch_state = ticket.get("dispatch_state")
            if isinstance(dispatch_state, dict):
                broadcast = dispatch_state.get("state") == "broadcast"
                if submitted and isinstance(ticket.get("review_lease"), dict):
                    broadcast = False
                if not offered_to_me and not broadcast:
                    continue
            elif submitted and isinstance(ticket.get("review_lease"), dict):
                continue
            work_dir = work_dirs.get(board_id)
            target = str(ticket.get("target_url", ""))
            routing_error = None
            if registry is not None:
                if (
                    not submitted
                    and permanent_registry_claim_refusal(
                        registry, board_id, target
                    )
                    is not None
                ):
                    continue
                try:
                    work_dir = resolve_registry_target(
                        registry, board_id, target
                    )["work_dir"]
                except RegistryRoutingError as exc:
                    work_dir = None
                    routing_error = {"code": exc.code, "message": str(exc)}
            elif target:
                work_dir = project_work_dirs.get(
                    target.split("/", 1)[0].casefold(), work_dir
                )
            event: dict[str, Any] = {
                "kind": offered_kind if offered_to_me else "ticket_backlog",
                "source": "wait_reconciliation",
                "board_id": board_id,
                "ticket_id": ticket_id,
                "status": expected_status,
                "reason": "offer" if offered_to_me else "broadcast",
                "work_dir": work_dir,
            }
            if routing_error is not None:
                event["routing_error"] = routing_error
            for key in ("target_url", "payload_ref", "updated_at"):
                if ticket.get(key) is not None:
                    event[key] = ticket[key]
            if offered_to_me:
                event["offer"] = {
                    "ticket_id": ticket_id,
                    "board_id": board_id,
                    "expires_at": offer.get("expires_at"),
                    "tier": ticket.get("tier", 2),
                    "skills_required": list(ticket.get("skills_required") or []),
                }
            found.append(event)
        return found[:MAX_EVENTS_PER_BOARD]

    async def reconcile_all(raw: Client) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for board_id in active:
            found.extend(await reconcile(raw, board_id))
        return found

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
            events.extend(await reconcile_all(client._client))
            if events:
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
                        events.extend(await reconcile_all(raw))
                        if events:
                            return response(events)
                        deadline = started + timeout_s
                        pending_cue = asyncio.create_task(anext(subscription))
                        try:
                            while True:
                                remaining = deadline - time.monotonic()
                                if remaining <= 0:
                                    break
                                done, _ = await asyncio.wait(
                                    {pending_cue},
                                    timeout=min(WAIT_RECONCILE_INTERVAL_S, remaining),
                                )
                                if not done:
                                    events.extend(await reconcile_all(raw))
                                    if events:
                                        return response(events)
                                    continue
                                try:
                                    pending_cue.result()
                                except StopAsyncIteration:
                                    break
                                pending = False
                                for board_id in active:
                                    board_events, board_pending = await drain(raw, board_id)
                                    events.extend(board_events)
                                    pending = pending or board_pending
                                if events or pending:
                                    return response(events)
                                pending_cue = asyncio.create_task(anext(subscription))
                        finally:
                            if not pending_cue.done():
                                pending_cue.cancel()
                                await asyncio.gather(
                                    pending_cue, return_exceptions=True
                                )
    except TimeoutError:
        pass
    return response(events)
