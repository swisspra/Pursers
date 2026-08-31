#!/usr/bin/env python3
"""Replay visible Central history through the proposed dispatch policy."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
CLIENT_SRC = ROOT / "packages" / "client" / "src"
if str(CLIENT_SRC) not in sys.path:
    sys.path.insert(0, str(CLIENT_SRC))

from pursers_client import BoardClient  # noqa: E402


PROJECT_REGISTRY_KEY = "project_registry"
SEAT_REGISTRY_KEY = "seat_registry"
CLAIM_STATES = frozenset({"claimed", "in_progress", "creating_report"})
RELEASE_STATES = frozenset(
    {"submitted", "closed", "rejected", "canceled", "terminated"}
)
WORKER_RELEASE_STATES = frozenset(
    {"closed", "rejected", "canceled", "terminated"}
)
MAX_SNAPSHOT_ITEMS = 1_000
MAX_RESPONSE_BYTES = 750_000
MAX_CATCHUP_EVENTS = 1_000
MAX_CATCHUP_PAGES = 100


class SimulationError(RuntimeError):
    """An expected configuration, coverage, or input failure."""


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso(epoch: float | None) -> str:
    if epoch is None:
        return "unknown"
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _ticket_id(event: dict[str, Any]) -> str | None:
    value = event.get("ticket_id")
    if isinstance(value, str) and value:
        return value
    payload_ref = event.get("payload_ref")
    if not isinstance(payload_ref, str):
        return None
    parts = payload_ref.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] in {"ticket", "tickets"}:
        return parts[-1]
    return None


def _state_document(result: dict[str, Any], key: str) -> dict[str, Any]:
    state = result.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("value"), str):
        raise SimulationError(f"{key} state entry is missing")
    try:
        document = json.loads(state["value"])
    except json.JSONDecodeError as exc:
        raise SimulationError(f"{key} state value is not valid JSON") from exc
    if not isinstance(document, dict):
        raise SimulationError(f"{key} state value must be an object")
    return document


def _active_boards(registry: dict[str, Any], home_board: str) -> list[str]:
    if registry.get("schema_version") != 1:
        raise SimulationError("project_registry schema_version must be 1")
    projects = registry.get("projects")
    if not isinstance(projects, dict):
        raise SimulationError("project_registry projects must be an object")
    boards = [home_board]
    seen = {home_board}
    for name, value in sorted(projects.items()):
        if not isinstance(name, str) or not isinstance(value, dict):
            raise SimulationError("project_registry contains an invalid project")
        board_id = value.get("board_id")
        status = value.get("status")
        if status == "paused":
            continue
        if status != "active" or not isinstance(board_id, str) or not board_id:
            raise SimulationError(f"project_registry project {name!r} is invalid")
        if board_id not in seen:
            boards.append(board_id)
            seen.add(board_id)
    return boards


def _worker_seats(document: dict[str, Any]) -> dict[tuple[str, str], Any]:
    if document.get("schema_version") != 1:
        raise SimulationError("seat_registry schema_version must be 1")
    seats = document.get("seats")
    if not isinstance(seats, dict):
        raise SimulationError("seat_registry seats must be an object")
    workers: dict[tuple[str, str], Any] = {}
    for name, value in sorted(seats.items()):
        if not isinstance(name, str) or not isinstance(value, dict):
            raise SimulationError("seat_registry contains an invalid seat")
        principal_id = value.get("principal_id")
        if value.get("role") != "worker":
            continue
        if not isinstance(principal_id, str) or not principal_id:
            raise SimulationError(f"seat_registry seat {name!r} has no principal")
        mode = value.get("board_mode")
        if mode != "registry" and not (
            isinstance(mode, list) and all(isinstance(item, str) for item in mode)
        ):
            raise SimulationError(f"seat_registry seat {name!r} has invalid board_mode")
        workers[(principal_id, name)] = mode
    return workers


class RawHistoryReader:
    """Non-joining transport that calls only Central read tools.

    ``board_catchup(ack=False)`` never advances the durable cursor. Central may
    still update compatibility activity for a write-scoped capability, so the
    report records that caveat and strict phase-1 audits should use a
    board:read-only capability.
    """

    def __init__(
        self, central_url: str, token: str, board_id: str, agent_name: str
    ) -> None:
        module = sys.modules.get(BoardClient.__module__)
        if module is None:
            raise SimulationError("pursers-client module is unavailable")
        self._httpx2 = module.httpx2
        self._client_class = module.Client
        self._transport = module.streamable_http_client
        self._decode = BoardClient._decode
        self.central_url = central_url
        self.token = token
        self.board_id = board_id
        self.agent_name = agent_name
        self._stack: AsyncExitStack | None = None
        self._client: Any | None = None

    async def __aenter__(self) -> "RawHistoryReader":
        self._stack = AsyncExitStack()
        http = await self._stack.enter_async_context(
            self._httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self._httpx2.Timeout(10.0, read=None),
                trust_env=False,
            )
        )
        transport = self._transport(self.central_url, http_client=http)
        self._client = await self._stack.enter_async_context(
            self._client_class(transport, mode="2026-07-28", cache=None)
        )
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise SimulationError("history reader is not entered")
        allowed = {
            "board_catchup",
            "board_snapshot",
            "board_state_get",
            "board_status",
        }
        if name not in allowed:
            raise SimulationError("history reader rejected a non-read tool")
        result = await self._client.call_tool(
            name, {"board_id": self.board_id, **arguments}
        )
        return self._decode(result)

    async def state(self, key: str) -> dict[str, Any]:
        return await self._call("board_state_get", {"key": key})

    async def snapshot(self) -> dict[str, Any]:
        return await self._call(
            "board_snapshot",
            {"limit": MAX_SNAPSHOT_ITEMS, "max_bytes": MAX_RESPONSE_BYTES},
        )

    async def catchup(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        cursor = 0
        events: list[dict[str, Any]] = []
        pages = 0
        scanned = 0
        visible = 0
        resync_required = False
        compacted_through = 0
        latest_cursor = 0
        while True:
            page = await self._call(
                "board_catchup",
                {
                    "agent_name": self.agent_name,
                    "cursor": cursor,
                    "limit": MAX_CATCHUP_EVENTS,
                    "max_events": MAX_CATCHUP_EVENTS,
                    "max_bytes": MAX_RESPONSE_BYTES,
                    "ack": False,
                },
            )
            pages += 1
            latest_cursor = int(page.get("latest_cursor", cursor))
            compacted_through = int(page.get("compacted_through", 0) or 0)
            scanned += int(page.get("scan_count", 0) or 0)
            visible += int(page.get("visible_count", 0) or 0)
            if page.get("resync_required"):
                resync_required = True
                break
            rows = page.get("events", [])
            if not isinstance(rows, list):
                raise SimulationError("board_catchup events must be a list")
            events.extend(item for item in rows if isinstance(item, dict))
            next_cursor = int(page.get("next_cursor", cursor))
            if next_cursor < cursor:
                raise SimulationError("board_catchup cursor moved backwards")
            has_more = bool(page.get("has_more"))
            if has_more and next_cursor == cursor:
                raise SimulationError("board_catchup made no paging progress")
            cursor = next_cursor
            if not has_more:
                break
            if pages >= MAX_CATCHUP_PAGES:
                raise SimulationError("board_catchup exceeded bounded page limit")
        return events, {
            "pages": pages,
            "scanned_events": scanned,
            "visible_events": visible,
            "latest_cursor": latest_cursor,
            "compacted_through": compacted_through,
            "resync_required": resync_required,
            "ack": False,
        }


async def collect_live_history(
    central_url: str,
    token: str,
    home_board: str,
    agent_name: str,
) -> dict[str, Any]:
    warnings: list[str] = [
        "Journal visibility is per agent; invisible and self-authored events "
        "are not replayed.",
        "ack=false preserves the durable cursor, but a write-scoped capability "
        "may still update compatibility activity; strict audit runs require a "
        "board:read-only capability.",
    ]
    async with RawHistoryReader(
        central_url, token, home_board, agent_name
    ) as home:
        registry = _state_document(
            await home.state(PROJECT_REGISTRY_KEY), PROJECT_REGISTRY_KEY
        )
        try:
            seats = _worker_seats(
                _state_document(await home.state(SEAT_REGISTRY_KEY), SEAT_REGISTRY_KEY)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            seats = {}
            warnings.append(
                "seat_registry was unavailable; a seat becomes eligible only "
                "after its first visible claim."
            )
    boards = _active_boards(registry, home_board)
    histories: list[dict[str, Any]] = []
    for board_id in boards:
        board_warnings: list[str] = []
        async with RawHistoryReader(
            central_url, token, board_id, agent_name
        ) as reader:
            snapshot = await reader.snapshot()
            try:
                events, coverage = await reader.catchup()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                events = []
                coverage = {
                    "pages": 0,
                    "scanned_events": 0,
                    "visible_events": 0,
                    "latest_cursor": int(snapshot.get("latest_seq", 0)),
                    "compacted_through": 0,
                    "resync_required": False,
                    "ack": False,
                }
                board_warnings.append(
                    f"catchup unavailable ({type(exc).__name__}); replay uses "
                    "durable ticket projections only."
                )
        omitted = snapshot.get("omitted_counts", {})
        if snapshot.get("truncated"):
            board_warnings.append(
                "snapshot is truncated: " + json.dumps(omitted, sort_keys=True)
            )
        if coverage.get("resync_required"):
            board_warnings.append(
                "journal cursor zero predates compaction; retained events were "
                "not recoverable through catchup."
            )
        histories.append(
            {
                "board_id": board_id,
                "agents": snapshot.get("agents", []),
                "tickets": snapshot.get("tickets", []),
                "events": events,
                "coverage": coverage,
                "warnings": board_warnings,
            }
        )
    return {
        "home_board": home_board,
        "boards": histories,
        "worker_seats": [
            {
                "principal_id": principal_id,
                "agent_name": agent_name,
                "board_mode": mode,
            }
            for (principal_id, agent_name), mode in sorted(seats.items())
        ],
        "warnings": warnings,
    }


def _transition(
    board_id: str,
    ticket_id: str,
    when: Any,
    actor: Any,
    status_to: str,
    **fields: Any,
) -> dict[str, Any] | None:
    if _epoch(when) is None or not isinstance(actor, str) or not actor:
        return None
    return {
        "board_id": board_id,
        "ticket_id": ticket_id,
        "kind": "ticket_status_changed",
        "occurred_at": when,
        "actor": actor,
        "status_to": status_to,
        "projection_fallback": True,
        **fields,
    }


def _normalized_events(board: dict[str, Any]) -> list[dict[str, Any]]:
    board_id = str(board["board_id"])
    events = [dict(item) for item in board.get("events", []) if isinstance(item, dict)]

    def already(ticket_id: str, status: str, when: Any) -> bool:
        target = _epoch(when)
        if target is None:
            return True
        return any(
            _ticket_id(item) == ticket_id
            and (
                item.get("status_to") == status
                or (status == "open" and item.get("kind") == "ticket_created")
            )
            and (value := _epoch(item.get("occurred_at"))) is not None
            and abs(value - target) <= 2.0
            for item in events
        )

    for ticket in board.get("tickets", []):
        if not isinstance(ticket, dict):
            continue
        ticket_id = ticket.get("ticket_id")
        if not isinstance(ticket_id, str) or not ticket_id:
            continue
        created_at = ticket.get("created_at")
        created_by = ticket.get("created_by_agent_id") or ticket.get("created_by")
        if not already(ticket_id, "open", created_at):
            event = _transition(
                board_id, ticket_id, created_at, created_by, "open"
            )
            if event is not None:
                event["kind"] = "ticket_created"
                events.append(event)
        claimed_at = ticket.get("claimed_at")
        if not already(ticket_id, "claimed", claimed_at):
            event = _transition(
                board_id,
                ticket_id,
                claimed_at,
                ticket.get("claimed_by_agent_id") or ticket.get("claimed_by"),
                "claimed",
            )
            if event is not None:
                events.append(event)
        abandoned_at = ticket.get("last_abandoned_at")
        abandoned_by = ticket.get("last_abandoned_by_agent_id") or ticket.get(
            "last_abandoned_by"
        )
        if not already(ticket_id, "open", abandoned_at):
            event = _transition(
                board_id,
                ticket_id,
                abandoned_at,
                f"board-reaper:{abandoned_by or 'unknown'}",
                "open",
                last_abandoned_by=abandoned_by,
                abandoned_count=ticket.get("abandoned_count", 0),
            )
            if event is not None:
                events.append(event)
        for submission in ticket.get("submission_history", []):
            if not isinstance(submission, dict):
                continue
            when = submission.get("submitted_at")
            if already(ticket_id, "submitted", when):
                continue
            event = _transition(
                board_id,
                ticket_id,
                when,
                submission.get("submitted_by_agent_id")
                or submission.get("submitted_by")
                or ticket.get("submitted_by_agent_id")
                or ticket.get("submitted_by"),
                "submitted",
            )
            if event is not None:
                events.append(event)
        for review in ticket.get("review_history", []):
            if not isinstance(review, dict):
                continue
            status = review.get("status_to")
            when = review.get("reviewed_at")
            if not isinstance(status, str) or already(ticket_id, status, when):
                continue
            event = _transition(
                board_id,
                ticket_id,
                when,
                review.get("reviewed_by_agent_id")
                or review.get("reviewed_by")
                or ticket.get("reviewed_by_agent_id")
                or ticket.get("reviewed_by"),
                status,
                submitted_by_agent_id=review.get("submitted_by_agent_id")
                or review.get("submitted_by")
                or ticket.get("submitted_by_agent_id")
                or ticket.get("submitted_by"),
            )
            if event is not None:
                events.append(event)
        current_status = ticket.get("status")
        if current_status in RELEASE_STATES:
            if current_status == "submitted":
                when = ticket.get("submitted_at") or ticket.get("updated_at")
                actor = ticket.get("submitted_by_agent_id") or ticket.get(
                    "submitted_by"
                ) or ticket.get("claimed_by_agent_id") or ticket.get(
                    "claimed_by"
                )
            elif current_status == "canceled":
                when = ticket.get("canceled_at") or ticket.get("updated_at")
                actor = ticket.get("canceled_by_agent_id") or ticket.get(
                    "canceled_by"
                )
            elif current_status == "terminated":
                when = ticket.get("terminated_at") or ticket.get("updated_at")
                actor = ticket.get("terminated_by_agent_id") or ticket.get(
                    "terminated_by"
                )
            else:
                when = ticket.get("reviewed_at") or ticket.get("updated_at")
                actor = ticket.get("reviewed_by_agent_id") or ticket.get(
                    "reviewed_by"
                ) or ticket.get("submitted_by_agent_id") or ticket.get(
                    "submitted_by"
                )
            if not already(ticket_id, str(current_status), when):
                event = _transition(
                    board_id,
                    ticket_id,
                    when,
                    actor,
                    str(current_status),
                    submitted_by_agent_id=ticket.get("submitted_by_agent_id")
                    or ticket.get("submitted_by"),
                )
                if event is not None:
                    events.append(event)
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in events:
        ticket_id = _ticket_id(event)
        when = _epoch(event.get("occurred_at"))
        if ticket_id is None or when is None:
            continue
        event["board_id"] = board_id
        event["ticket_id"] = ticket_id
        key = (
            board_id,
            ticket_id,
            event.get("kind"),
            event.get("status_to"),
            round(when, 6),
            event.get("actor"),
        )
        current = deduped.get(key)
        if current is None or current.get("projection_fallback"):
            deduped[key] = event
    return sorted(
        deduped.values(),
        key=lambda item: (
            _epoch(item.get("occurred_at")) or 0,
            board_id,
            int(item.get("seq", 0) or 0),
            str(item.get("ticket_id")),
            str(item.get("status_to")),
        ),
    )


@dataclass
class Seat:
    board_id: str
    agent_id: str
    joined_at: float
    handed_off_at: float | None


@dataclass
class Worker:
    key: tuple[str, str]
    seats: dict[str, Seat] = field(default_factory=dict)
    available_since: float = 0.0
    busy_ticket: tuple[str, str] | None = None
    abandonments: int = 0
    known_worker_at: float = float("inf")
    registry_mode: Any = None
    observed_claimer: bool = False

    @property
    def label(self) -> str:
        return f"{self.key[1]} ({self.key[0]})"


def _eligible(worker: Worker, board_id: str, when: float) -> bool:
    seat = worker.seats.get(board_id)
    configured = worker.registry_mode == "registry" or (
        isinstance(worker.registry_mode, list) and board_id in worker.registry_mode
    )
    return bool(
        seat is not None
        and worker.known_worker_at < when
        and (configured or worker.observed_claimer)
        and seat.joined_at <= when
        and (seat.handed_off_at is None or seat.handed_off_at > when)
        and worker.busy_ticket is None
    )


def _ticket_rank(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        0 if item.get("priority") == "critical" else 1,
        float(item["opened_at"]),
        str(item["board_id"]),
        str(item["ticket_id"]),
    )


def replay(history: dict[str, Any], *, starvation_seconds: int = 300) -> dict[str, Any]:
    if starvation_seconds < 0:
        raise SimulationError("starvation_seconds must be non-negative")
    boards = history.get("boards")
    if not isinstance(boards, list) or not boards:
        raise SimulationError("history must contain at least one board")
    worker_registry = {
        (item.get("principal_id"), item.get("agent_name")): item.get("board_mode")
        for item in history.get("worker_seats", [])
        if isinstance(item, dict)
        and isinstance(item.get("principal_id"), str)
        and isinstance(item.get("agent_name"), str)
    }
    workers: dict[tuple[str, str], Worker] = {}
    agent_lookup: dict[tuple[str, str], tuple[str, str]] = {}
    tickets: dict[tuple[str, str], dict[str, Any]] = {}
    all_events: list[dict[str, Any]] = []
    warnings = [str(item) for item in history.get("warnings", [])]
    coverage: list[dict[str, Any]] = []
    active_boards = {str(item.get("board_id")) for item in boards}

    for board in boards:
        if not isinstance(board, dict) or not isinstance(board.get("board_id"), str):
            raise SimulationError("board history is invalid")
        board_id = board["board_id"]
        coverage.append({"board_id": board_id, **dict(board.get("coverage", {}))})
        warnings.extend(f"{board_id}: {item}" for item in board.get("warnings", []))
        for ticket in board.get("tickets", []):
            if not isinstance(ticket, dict) or not isinstance(ticket.get("ticket_id"), str):
                continue
            tickets[(board_id, ticket["ticket_id"])] = dict(ticket)
        for agent in board.get("agents", []):
            if not isinstance(agent, dict):
                continue
            principal = agent.get("principal_id")
            name = agent.get("agent_name")
            agent_id = agent.get("agent_id")
            if not all(isinstance(item, str) and item for item in (principal, name, agent_id)):
                continue
            explicit_role = agent.get("dispatch_role")
            mode = worker_registry.get((principal, name))
            registry_worker = mode == "registry" or (
                isinstance(mode, list) and board_id in mode
            )
            joined = _epoch(agent.get("joined_at"))
            if joined is None:
                warnings.append(f"{board_id}: seat {name} has no joined_at and was excluded")
                continue
            key = (principal, name)
            worker = workers.setdefault(
                key,
                Worker(
                    key=key,
                    available_since=joined,
                    registry_mode=mode,
                ),
            )
            worker.available_since = min(worker.available_since, joined)
            if explicit_role == "worker":
                worker.registry_mode = "registry"
                worker.known_worker_at = min(worker.known_worker_at, joined - 1e-6)
            elif registry_worker:
                worker.registry_mode = mode
                worker.known_worker_at = min(worker.known_worker_at, joined - 1e-6)
            worker.seats[board_id] = Seat(
                board_id=board_id,
                agent_id=agent_id,
                joined_at=joined,
                handed_off_at=_epoch(agent.get("handed_off_at")),
            )
            agent_lookup[(board_id, agent_id)] = key
        all_events.extend(_normalized_events(board))

    for definition, mode in worker_registry.items():
        if mode == "registry" and definition not in workers:
            warnings.append(
                f"registered worker {definition[1]} has no visible joined seat and was excluded"
            )
        elif isinstance(mode, list) and not active_boards.intersection(mode):
            warnings.append(
                f"registered worker {definition[1]} has no active configured board"
            )

    all_events.sort(
        key=lambda item: (
            _epoch(item.get("occurred_at")) or 0,
            str(item.get("board_id")),
            int(item.get("seq", 0) or 0),
            str(item.get("ticket_id")),
            str(item.get("status_to")),
        )
    )
    open_tickets: dict[tuple[str, str], dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []
    starvation: dict[tuple[str, str], dict[str, Any]] = {}

    def worker_for(board_id: str, agent_id: Any) -> Worker | None:
        if not isinstance(agent_id, str):
            return None
        key = agent_lookup.get((board_id, agent_id))
        return workers.get(key) if key is not None else None

    def candidates(board_id: str, when: float) -> list[Worker]:
        return sorted(
            (item for item in workers.values() if _eligible(item, board_id, when)),
            key=lambda item: (
                item.abandonments,
                max(item.available_since, item.seats[board_id].joined_at),
                item.key[1],
                item.key[0],
            ),
        )

    for event in all_events:
        when = _epoch(event.get("occurred_at"))
        board_id = str(event["board_id"])
        ticket_id = str(event["ticket_id"])
        key = (board_id, ticket_id)
        if when is None:
            continue
        status_to = event.get("status_to")
        kind = event.get("kind")
        metadata = tickets.get(key, {})
        if kind == "ticket_created" or status_to == "open":
            open_tickets[key] = {
                "board_id": board_id,
                "ticket_id": ticket_id,
                "priority": metadata.get("priority", "medium"),
                "opened_at": when,
            }
            abandoned = worker_for(board_id, event.get("last_abandoned_by"))
            if abandoned is not None:
                abandoned.abandonments += 1
                abandoned.busy_ticket = None
                abandoned.available_since = when

        # Evaluate starvation against the state immediately before this event's
        # claim/release transition. A claim at t=120 must not erase evidence
        # that an idle worker could have been proposed at t=60.
        for open_key, opened in sorted(open_tickets.items()):
            if open_key in starvation:
                continue
            age = when - float(opened["opened_at"])
            eligible = candidates(str(opened["board_id"]), when)
            if age >= starvation_seconds and eligible:
                detected_at = float(opened["opened_at"]) + starvation_seconds
                starvation[open_key] = {
                    "board_id": opened["board_id"],
                    "ticket_id": opened["ticket_id"],
                    "detected_at": detected_at,
                    "observed_at": when,
                    "idle_worker": eligible[0].label,
                    "lead_seconds": max(0.0, when - detected_at),
                }
        if status_to in CLAIM_STATES:
            actual = worker_for(board_id, event.get("actor"))
            ranked = min(open_tickets.values(), key=_ticket_rank) if open_tickets else None
            proposal_board = (
                str(ranked["board_id"]) if ranked is not None else board_id
            )
            choices = candidates(proposal_board, when)
            proposed = choices[0] if choices else None
            available_at = None
            if proposed is not None:
                available_at = max(
                    proposed.available_since,
                    proposed.seats[proposal_board].joined_at,
                    float(ranked["opened_at"]) if ranked is not None else when,
                )
            reasons: list[str] = []
            if proposed is None:
                reasons.append("no eligible idle worker was visible")
            elif proposed.abandonments:
                reasons.append(
                    f"selected after abandonment penalty={proposed.abandonments}"
                )
            else:
                reasons.append("longest-idle eligible worker with no abandonment penalty")
            if ranked is not None and (
                ranked["board_id"], ranked["ticket_id"]
            ) != key:
                reasons.append(
                    f"global queue preferred {ranked['board_id']}/{ranked['ticket_id']}"
                )
            worker_agreement = (
                None
                if proposed is None or actual is None
                else proposed.key == actual.key
            )
            queue_order_agreement = (
                None
                if ranked is None
                else (ranked["board_id"], ranked["ticket_id"]) == key
            )
            agreement = worker_agreement
            decisions.append(
                {
                    "at": when,
                    "board_id": board_id,
                    "ticket_id": ticket_id,
                    "proposed": proposed.label if proposed else None,
                    "actual": actual.label if actual else str(event.get("actor")),
                    "agreement": agreement,
                    "worker_agreement": worker_agreement,
                    "queue_order_agreement": queue_order_agreement,
                    "start_delta_seconds": (
                        None if available_at is None else max(0.0, when - available_at)
                    ),
                    "reason": "; ".join(reasons),
                }
            )
            if actual is not None:
                # The first visible claim causally establishes this identity as
                # a worker only for later events; it is never fed back into the
                # proposal for the same claim.
                actual.known_worker_at = min(actual.known_worker_at, when)
                actual.observed_claimer = True
                actual.busy_ticket = key
            open_tickets.pop(key, None)
        elif status_to in RELEASE_STATES:
            # Submitted work leaves the open queue but still occupies its seat
            # while review is pending. Review events are authored by the
            # reviewer, while the submitter is the worker whose seat is freed.
            if status_to in WORKER_RELEASE_STATES:
                if status_to in {"closed", "rejected"}:
                    released = worker_for(
                        board_id, event.get("submitted_by_agent_id")
                    )
                    if released is None:
                        released = worker_for(board_id, event.get("actor"))
                else:
                    released = worker_for(board_id, event.get("actor"))
                    if released is None:
                        released = worker_for(
                            board_id, event.get("submitted_by_agent_id")
                        )
                if released is not None:
                    released.busy_ticket = None
                    released.available_since = when
            open_tickets.pop(key, None)

    evaluable = [item for item in decisions if item["agreement"] is not None]
    agreements = sum(item["agreement"] is True for item in evaluable)
    deltas = [
        float(item["start_delta_seconds"])
        for item in decisions
        if item["start_delta_seconds"] is not None
    ]
    agreement_rate = (agreements / len(evaluable)) if evaluable else None
    return {
        "boards": [str(item["board_id"]) for item in boards],
        "coverage": coverage,
        "warnings": sorted(set(warnings)),
        "event_count": len(all_events),
        "worker_count": sum(
            item.known_worker_at < float("inf") for item in workers.values()
        ),
        "claim_count": len(decisions),
        "evaluable_claim_count": len(evaluable),
        "agreement_count": agreements,
        "agreement_rate": agreement_rate,
        "mean_start_delta_seconds": (
            sum(deltas) / len(deltas) if deltas else None
        ),
        "starvation_events": list(starvation.values()),
        "decisions": decisions,
        "mismatches": [
            item
            for item in decisions
            if item["agreement"] is not True
            or item["queue_order_agreement"] is not True
        ],
    }


def render_markdown(result: dict[str, Any], *, starvation_seconds: int) -> str:
    rate = result.get("agreement_rate")
    rate_text = "N/A" if rate is None else f"{100 * float(rate):.1f}%"
    delta = result.get("mean_start_delta_seconds")
    delta_text = "N/A" if delta is None else f"{float(delta):.1f}s"
    lines = [
        "# Coordinator Dispatch Simulation",
        "",
        "## Headline",
        "",
        f"- Agreement rate: **{rate_text}** "
        f"({result['agreement_count']}/{result['evaluable_claim_count']} evaluable claims)",
        f"- Visible claim events: **{result['claim_count']}**",
        f"- Mean earliest-start delta: **{delta_text}**",
        f"- Starvation findings at {starvation_seconds}s: **{len(result['starvation_events'])}**",
        f"- Active boards replayed: **{len(result['boards'])}**",
        "",
        "## Coverage",
        "",
        "| Board | Pages | Scanned | Visible | Latest cursor | Compacted through | Resync gap |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in result["coverage"]:
        lines.append(
            f"| {row['board_id']} | {row.get('pages', 0)} | "
            f"{row.get('scanned_events', 0)} | {row.get('visible_events', 0)} | "
            f"{row.get('latest_cursor', 0)} | {row.get('compacted_through', 0)} | "
            f"{'yes' if row.get('resync_required') else 'no'} |"
        )
    lines.extend(["", "Coverage caveats:"])
    if result["warnings"]:
        lines.extend(f"- {item}" for item in result["warnings"])
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Claim replay",
            "",
            "| At | Ticket | Proposed | Actual | Agent match | Queue-order match "
            "| Earlier start | Why |",
            "| --- | --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    if not result["decisions"]:
        lines.append("| — | — | — | — | — | — | — | No visible claim events |")
    for item in result["decisions"]:
        worker_match = (
            "unknown"
            if item["worker_agreement"] is None
            else "yes" if item["worker_agreement"] else "no"
        )
        queue_match = (
            "unknown"
            if item["queue_order_agreement"] is None
            else "yes" if item["queue_order_agreement"] else "no"
        )
        delta_value = item.get("start_delta_seconds")
        earlier = "—" if delta_value is None else f"{float(delta_value):.1f}s"
        lines.append(
            f"| {_iso(item['at'])} | {item['board_id']}/{item['ticket_id']} | "
            f"{item['proposed'] or 'none'} | {item['actual']} | {worker_match} | "
            f"{queue_match} | {earlier} | "
            f"{item['reason']} |"
        )
    lines.extend(["", "## Starvation findings", ""])
    if not result["starvation_events"]:
        lines.append("No starvation finding was observable in the covered history.")
    else:
        lines.extend(
            [
                "| Ticket | Detectable at | First observed at | Idle worker | Lead |",
                "| --- | --- | --- | --- | ---: |",
            ]
        )
        for item in result["starvation_events"]:
            lines.append(
                f"| {item['board_id']}/{item['ticket_id']} | "
                f"{_iso(item['detected_at'])} | {_iso(item['observed_at'])} | "
                f"{item['idle_worker']} | {float(item['lead_seconds']):.1f}s |"
            )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "This is shadow evidence only. It proposes no assignment and performs "
            "no workflow mutation. A go/no-go decision must account for every "
            "coverage caveat above; an incomplete or visibility-filtered history "
            "is not proof of policy correctness.",
            "",
        ]
    )
    return "\n".join(lines)


def _loopback_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise argparse.ArgumentTypeError("central URL must be HTTP(S) without userinfo")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise argparse.ArgumentTypeError("central URL must use a loopback host")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay visible multi-board history through the dispatch policy"
    )
    parser.add_argument(
        "--central-url",
        type=_loopback_url,
        default=os.environ.get("ONBOARD_CENTRAL_URL", "https://127.0.0.1:8766/mcp"),
    )
    parser.add_argument(
        "--home-board", default=os.environ.get("ONBOARD_BOARD_ID", "home")
    )
    parser.add_argument(
        "--agent-name", default=os.environ.get("ONBOARD_AGENT_NAME", "coordinator-sim")
    )
    parser.add_argument(
        "--token-env",
        default="ONBOARD_CENTRAL_TOKEN",
        help="environment variable containing the capability; its value is never printed",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--starvation-seconds", type=int, default=300)
    return parser


async def _execute(args: argparse.Namespace) -> str:
    token = os.environ.get(args.token_env, "")
    if not token:
        raise SimulationError(f"capability environment variable {args.token_env!r} is empty")
    history = await collect_live_history(
        args.central_url, token, args.home_board, args.agent_name
    )
    result = replay(history, starvation_seconds=args.starvation_seconds)
    return render_markdown(result, starvation_seconds=args.starvation_seconds)


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        report = asyncio.run(_execute(args))
        if args.output is None:
            print(report)
        else:
            args.output.write_text(report, encoding="utf-8")
            print(f"wrote {args.output}")
        return 0
    except (SimulationError, OSError, ValueError) as exc:
        print(f"simulation failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
