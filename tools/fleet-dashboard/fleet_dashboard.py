#!/usr/bin/env python3
"""Loopback-only, read-only fleet dashboard for Pursers boards."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

# Prefer the sibling source checkout over any installed pursers-client wheel:
# the dashboard depends on keyword arguments newer than the last published wheel.
_CLIENT_SRC = Path(__file__).resolve().parents[2] / "packages" / "client" / "src"
if (_CLIENT_SRC / "pursers_client").is_dir():
    sys.path.insert(0, str(_CLIENT_SRC))
from pursers_client import BoardClient  # noqa: I001


DEFAULT_URL = "https://127.0.0.1:8766/mcp"
DEFAULT_HOME_BOARD = "pursers"
SNAPSHOT_LIMIT = 1_000
SNAPSHOT_MAX_BYTES = 300_000
EVENT_SCAN_LIMIT = 50
EVENT_MAX_BYTES = 100_000
DETAIL_EVENT_SCAN_LIMIT = 100
API_MAX_BYTES = 300_000
MAX_BOARDS = 50
MAX_TICKET_ROWS = 25
MAX_DETAIL_TICKET_ROWS = SNAPSHOT_LIMIT
MAX_EVENT_ROWS = 12
MAX_AGENT_ROWS = 100
MAX_TITLE_CHARS = 160
MAX_LABEL_CHARS = 96
MAX_DESCRIPTION_CHARS = 800
MAX_REQUIRED_FIELDS = 20
MAX_SUBMISSION_CHARS = 500
BOARD_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
ACTIVE_CLAIM_STATES = frozenset({"claimed", "in_progress", "creating_report"})
SUBMITTED_STATES = frozenset({"submitted", "reviewing", "in_review"})
TERMINAL_STATES = frozenset({"closed", "rejected", "canceled", "terminated"})


class FleetClient(Protocol):
    async def board_state_get(self, key: str | None = None) -> dict[str, Any]: ...

    async def board_snapshot(
        self, *, limit: int | None = None, max_bytes: int | None = None
    ) -> dict[str, Any]: ...

    async def board_catchup(
        self,
        *,
        cursor: int | None = None,
        limit: int = 100,
        ack: bool = True,
        agent_name: str | None = None,
        max_events: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Config:
    url: str
    token: str
    home_board: str
    agent_name: str
    stale_seconds: int
    cache_seconds: float


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


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


def _time_sort_value(value: Any) -> float:
    parsed = _parse_time(value)
    return parsed.timestamp() if parsed is not None else 0.0


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def board_id_from_api_path(path: str) -> str | None:
    """Return one safe decoded board ID for an exact detail API route."""
    route = urlsplit(path).path
    prefix = "/api/board/"
    if not route.startswith(prefix):
        return None
    encoded = route[len(prefix) :]
    if not encoded or "/" in encoded:
        return None
    try:
        board_id = unquote(encoded, errors="strict")
    except UnicodeDecodeError:
        return None
    return board_id if BOARD_ID_RE.fullmatch(board_id) else None


def parse_project_registry(
    result: dict[str, Any], home_board: str
) -> list[tuple[str, str]]:
    """Return the home board followed by unique active registry boards."""
    state = result.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("value"), str):
        raise TypeError("project registry state is missing")
    try:
        document = json.loads(state["value"])
    except json.JSONDecodeError as exc:
        raise ValueError("project registry is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("project registry schema is unsupported")
    projects = document.get("projects")
    if not isinstance(projects, dict):
        raise TypeError("project registry projects are missing")

    boards = [(home_board, home_board)]
    seen = {home_board}
    for name, project in projects.items():
        if not isinstance(name, str) or not isinstance(project, dict):
            continue
        board_id = project.get("board_id")
        if (
            project.get("status") == "active"
            and isinstance(board_id, str)
            and board_id
            and board_id not in seen
        ):
            boards.append((_clip(name, MAX_LABEL_CHARS), board_id))
            seen.add(board_id)
        if len(boards) >= MAX_BOARDS:
            break
    return boards


def _closed_today(ticket: dict[str, Any], today: datetime) -> bool:
    if ticket.get("status") != "closed":
        return False
    closed_at = _parse_time(ticket.get("closed_at") or ticket.get("updated_at"))
    return closed_at is not None and closed_at.date() == today.date()


def _ticket_recency(ticket: dict[str, Any]) -> tuple[float, str]:
    timestamps = [
        _time_sort_value(ticket.get(name))
        for name in ("claimed_at", "submitted_at", "updated_at", "created_at")
    ]
    return max(timestamps), str(ticket.get("ticket_id") or "")


def _current_tickets_by_agent(
    tickets: list[Any],
) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for ticket in tickets:
        if not isinstance(ticket, dict) or ticket.get("status") in TERMINAL_STATES:
            continue
        for raw_agent_id in {
            ticket.get("claimed_by_agent_id"),
            ticket.get("assigned_to_agent_id"),
        } - {None, ""}:
            agent_id = str(raw_agent_id)
            current = selected.get(agent_id)
            if current is None or _ticket_recency(ticket) > _ticket_recency(current):
                selected[agent_id] = ticket
    return selected


def _detail_ticket(ticket: dict[str, Any]) -> dict[str, Any]:
    required = ticket.get("required_fields")
    if not isinstance(required, list):
        required = []
    submissions = ticket.get("submission_history")
    latest_submission = submissions[-1] if isinstance(submissions, list) and submissions else {}
    if not isinstance(latest_submission, dict):
        latest_submission = {}
    return {
        "id": _clip(ticket.get("ticket_id"), MAX_LABEL_CHARS),
        "title": _clip(ticket.get("title") or "(untitled)", MAX_TITLE_CHARS),
        "status": _clip(ticket.get("status") or "unknown", 32),
        "priority": _clip(ticket.get("priority") or "medium", 16),
        "claimed_by": _clip(ticket.get("claimed_by"), MAX_LABEL_CHARS) or None,
        "updated_at": _clip(ticket.get("updated_at"), 40) or None,
        "description": _clip(ticket.get("description"), MAX_DESCRIPTION_CHARS),
        "required_fields": [
            _clip(item, MAX_LABEL_CHARS)
            for item in required[:MAX_REQUIRED_FIELDS]
            if isinstance(item, str) and item
        ],
        "latest_submission_summary": _clip(
            latest_submission.get("summary") or ticket.get("summary"),
            MAX_SUBMISSION_CHARS,
        )
        or None,
        "review_label": _clip(ticket.get("review_label"), MAX_LABEL_CHARS)
        or None,
    }


def project_board_detail(raw: dict[str, Any]) -> dict[str, Any]:
    """Project one bounded snapshot and catchup page for the browser."""
    snapshot = raw.get("snapshot") if isinstance(raw.get("snapshot"), dict) else {}
    source_tickets = (
        snapshot.get("tickets") if isinstance(snapshot.get("tickets"), list) else []
    )
    tickets = [_detail_ticket(item) for item in source_tickets if isinstance(item, dict)]
    status_rank = {
        **{status: 0 for status in ACTIVE_CLAIM_STATES},
        **{status: 1 for status in SUBMITTED_STATES},
        "open": 2,
    }
    tickets.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    tickets.sort(key=lambda item: status_rank.get(item["status"], 3))

    source_events = raw.get("events") if isinstance(raw.get("events"), list) else []
    events = []
    for event in source_events[-DETAIL_EVENT_SCAN_LIMIT:]:
        if not isinstance(event, dict):
            continue
        events.append(
            {
                "seq": event.get("seq") if isinstance(event.get("seq"), int) else None,
                "kind": _clip(event.get("kind"), 48),
                "ticket_id": _clip(event.get("ticket_id"), MAX_LABEL_CHARS) or None,
                "occurred_at": _clip(event.get("occurred_at"), 40) or None,
            }
        )
    events.sort(key=lambda item: item["seq"] if item["seq"] is not None else -1)

    total_counts = snapshot.get("total_counts")
    snapshot_ticket_total = (
        total_counts.get("tickets")
        if isinstance(total_counts, dict)
        and isinstance(total_counts.get("tickets"), int)
        else len(source_tickets)
    )
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "board": {
            "board_id": _clip(raw.get("board_id"), MAX_LABEL_CHARS),
            "label": _clip(raw.get("label") or raw.get("board_id"), MAX_LABEL_CHARS),
        },
        "tickets": tickets[:MAX_DETAIL_TICKET_ROWS],
        "events": events,
        "ticket_total": max(snapshot_ticket_total, len(source_tickets)),
        "ticket_returned": min(len(tickets), MAX_DETAIL_TICKET_ROWS),
        "ticket_omitted": 0,
        "truncated": bool(snapshot.get("truncated") or len(tickets) > MAX_DETAIL_TICKET_ROWS),
        "bounds": {
            "snapshot_items_per_collection": SNAPSHOT_LIMIT,
            "snapshot_bytes": SNAPSHOT_MAX_BYTES,
            "api_bytes": API_MAX_BYTES,
            "description_chars": MAX_DESCRIPTION_CHARS,
            "required_fields_per_ticket": MAX_REQUIRED_FIELDS,
            "events": DETAIL_EVENT_SCAN_LIMIT,
        },
    }
    result["ticket_omitted"] = max(
        0, result["ticket_total"] - result["ticket_returned"]
    )
    while len(_json_bytes(result)) > API_MAX_BYTES and result["tickets"]:
        result["tickets"].pop()
        result["ticket_returned"] = len(result["tickets"])
        result["ticket_omitted"] = max(
            0, result["ticket_total"] - result["ticket_returned"]
        )
        result["truncated"] = True
    while len(_json_bytes(result)) > API_MAX_BYTES and result["events"]:
        result["events"].pop(0)
        result["truncated"] = True
    if len(_json_bytes(result)) > API_MAX_BYTES:
        raise ValueError("detail projection metadata exceeds API byte cap")
    return result


def aggregate_fleet(
    board_rows: list[dict[str, Any]],
    *,
    stale_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the bounded API projection from already-bounded board reads."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    boards: list[dict[str, Any]] = []

    for raw in board_rows[:MAX_BOARDS]:
        board_id = _clip(raw.get("board_id"), MAX_LABEL_CHARS)
        label = _clip(raw.get("label") or board_id, MAX_LABEL_CHARS)
        error = raw.get("error")
        if error:
            boards.append(
                {
                    "board_id": board_id,
                    "label": label,
                    "error": _clip(error, MAX_LABEL_CHARS),
                    "counts": {
                        "open": 0,
                        "claimed": 0,
                        "submitted": 0,
                        "closed_today": 0,
                    },
                    "tickets": [],
                    "events": [],
                    "truncated": False,
                }
            )
            continue

        snapshot = raw.get("snapshot") if isinstance(raw.get("snapshot"), dict) else {}
        agents = (
            snapshot.get("agents") if isinstance(snapshot.get("agents"), list) else []
        )
        tickets = (
            snapshot.get("tickets") if isinstance(snapshot.get("tickets"), list) else []
        )
        current_by_agent = _current_tickets_by_agent(tickets)
        agent_keys: dict[str, tuple[str, str]] = {}

        for agent in agents:
            if not isinstance(agent, dict):
                continue
            principal_id = agent.get("principal_id")
            agent_name = agent.get("agent_name")
            agent_id = agent.get("agent_id")
            if not all(
                isinstance(item, str) and item for item in (principal_id, agent_name)
            ):
                continue
            key = (principal_id, agent_name)
            if isinstance(agent_id, str):
                agent_keys[agent_id] = key
            seen_at = _parse_time(
                agent.get("last_activity_at") or agent.get("joined_at")
            )
            group = groups.setdefault(
                key,
                {
                    "principal_id": _clip(principal_id, MAX_LABEL_CHARS),
                    "agent_name": _clip(agent_name, MAX_LABEL_CHARS),
                    "boards": set(),
                    "seats": {},
                    "agent_ids_by_board": {},
                    "last_seen": None,
                    "busy": False,
                },
            )
            group["boards"].add(board_id)
            if isinstance(agent_id, str) and agent_id:
                group["agent_ids_by_board"].setdefault(board_id, set()).add(agent_id)
            current = current_by_agent.get(str(agent_id or ""))
            group["seats"][board_id] = {
                "board_id": board_id,
                "project": label,
                "role": _clip(
                    agent.get("membership_role") or agent.get("role"), 32
                )
                or None,
                "current_ticket_id": (
                    _clip(current.get("ticket_id"), MAX_LABEL_CHARS)
                    if current is not None
                    else None
                ),
                "current_ticket_title": (
                    _clip(current.get("title") or "(untitled)", MAX_TITLE_CHARS)
                    if current is not None
                    else None
                ),
                "last_seen": seen_at.isoformat() if seen_at else None,
            }
            if agent.get("status") == "working" and agent.get(
                "lifecycle_status"
            ) not in {"handed_off", "inactive"}:
                group["busy"] = True
            if seen_at is not None and (
                group["last_seen"] is None or seen_at > group["last_seen"]
            ):
                group["last_seen"] = seen_at

        counts = {"open": 0, "claimed": 0, "submitted": 0, "closed_today": 0}
        ticket_rows: list[dict[str, Any]] = []
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            status = str(ticket.get("status") or "")
            if status == "open":
                counts["open"] += 1
            elif status in ACTIVE_CLAIM_STATES:
                counts["claimed"] += 1
            elif status in SUBMITTED_STATES:
                counts["submitted"] += 1
            elif _closed_today(ticket, now):
                counts["closed_today"] += 1

            claimed_id = ticket.get("claimed_by_agent_id")
            if (
                status == "open"
                or status in ACTIVE_CLAIM_STATES
                or status in SUBMITTED_STATES
            ):
                claimed_by = ticket.get("claimed_by")
                if not claimed_by and isinstance(claimed_id, str):
                    key = agent_keys.get(claimed_id)
                    claimed_by = key[1] if key else claimed_id
                ticket_rows.append(
                    {
                        "id": _clip(ticket.get("ticket_id"), MAX_LABEL_CHARS),
                        "title": _clip(
                            ticket.get("title") or "(untitled)", MAX_TITLE_CHARS
                        ),
                        "status": _clip(status, 32),
                        "claimed_by": _clip(claimed_by, MAX_LABEL_CHARS) or None,
                        "updated_at": _clip(ticket.get("updated_at"), 40) or None,
                    }
                )

        events: list[dict[str, Any]] = []
        raw_events = raw.get("events") if isinstance(raw.get("events"), list) else []
        for event in raw_events[-MAX_EVENT_ROWS:]:
            if not isinstance(event, dict):
                continue
            events.append(
                {
                    "seq": event.get("seq")
                    if isinstance(event.get("seq"), int)
                    else None,
                    "kind": _clip(event.get("kind"), 48),
                    "ticket_id": _clip(event.get("ticket_id"), MAX_LABEL_CHARS) or None,
                    "occurred_at": _clip(event.get("occurred_at"), 40) or None,
                }
            )

        ticket_rows.sort(key=lambda item: item["updated_at"] or "", reverse=True)
        ticket_status_rank = {
            **{status: 0 for status in ACTIVE_CLAIM_STATES},
            **{status: 1 for status in SUBMITTED_STATES},
            "open": 2,
        }
        ticket_rows.sort(key=lambda item: ticket_status_rank.get(item["status"], 3))
        ticket_counts_truncated = bool(snapshot.get("truncated"))
        rendered_counts = {
            name: f">={value}" if ticket_counts_truncated else value
            for name, value in counts.items()
        }
        boards.append(
            {
                "board_id": board_id,
                "label": label,
                "counts": rendered_counts,
                "tickets": ticket_rows[:MAX_TICKET_ROWS],
                "events": events,
                "truncated": bool(
                    snapshot.get("truncated") or len(ticket_rows) > MAX_TICKET_ROWS
                ),
            }
        )

    names_to_groups: dict[str, set[tuple[str, str]]] = {}
    for key, group in groups.items():
        names_to_groups.setdefault(group["agent_name"], set()).add(key)
    agent_rows: list[dict[str, Any]] = []
    for group in groups.values():
        last_seen = group["last_seen"]
        if group["busy"]:
            status = "busy"
        elif (
            last_seen is not None and (now - last_seen).total_seconds() <= stale_seconds
        ):
            status = "available"
        else:
            status = "stale"
        agent_rows.append(
            {
                "principal_id": group["principal_id"],
                "agent_name": group["agent_name"],
                "boards": sorted(group["boards"]),
                "seats": sorted(
                    group["seats"].values(),
                    key=lambda item: (item["project"], item["board_id"]),
                ),
                "duplicate_name": len(
                    names_to_groups.get(group["agent_name"], set())
                )
                > 1
                or any(
                    len(agent_ids) > 1
                    for agent_ids in group["agent_ids_by_board"].values()
                ),
                "last_seen": last_seen.isoformat() if last_seen else None,
                "pool_status": status,
            }
        )
    rank = {"busy": 0, "available": 1, "stale": 2}
    agent_rows.sort(key=lambda item: (rank[item["pool_status"]], item["agent_name"]))
    busy = sum(item["pool_status"] == "busy" for item in agent_rows)
    available = sum(item["pool_status"] == "available" for item in agent_rows)
    stale = sum(item["pool_status"] == "stale" for item in agent_rows)
    agent_rows = agent_rows[:MAX_AGENT_ROWS]
    return {
        "generated_at": now.isoformat(),
        "stale_after_seconds": stale_seconds,
        "pool_summary": {
            "online": busy + available,
            "busy": busy,
            "available": available,
            "stale": stale,
        },
        "agents": agent_rows,
        "boards": boards,
        "bounds": {
            "boards": MAX_BOARDS,
            "snapshot_items_per_collection": SNAPSHOT_LIMIT,
            "snapshot_bytes": SNAPSHOT_MAX_BYTES,
            "ticket_rows_per_board": MAX_TICKET_ROWS,
            "events_per_board": MAX_EVENT_ROWS,
            "agents": MAX_AGENT_ROWS,
        },
    }


class FleetFetcher:
    def __init__(
        self,
        config: Config,
        client_factory: Callable[..., Any] = BoardClient,
    ) -> None:
        self.config = config
        self.client_factory = client_factory

    def _client(self, board_id: str) -> Any:
        return self.client_factory(
            self.config.url,
            self.config.token,
            board_id,
            agent_name=self.config.agent_name,
        )

    async def _boards(self) -> list[tuple[str, str]]:
        async with self._client(self.config.home_board) as client:
            registry = await client.board_state_get(key="project_registry")
        return parse_project_registry(registry, self.config.home_board)

    async def _board_event_feed(
        self,
        client: FleetClient,
        latest_seq: int,
        event_limit: int = EVENT_SCAN_LIMIT,
    ) -> list[dict[str, Any]]:
        result = await client.board_catchup(
            cursor=max(0, latest_seq - event_limit),
            limit=event_limit,
            ack=False,
            max_events=event_limit,
            max_bytes=EVENT_MAX_BYTES,
        )
        events = result.get("events")
        return events if isinstance(events, list) else []

    async def _read_board(
        self,
        label: str,
        board_id: str,
        event_limit: int = EVENT_SCAN_LIMIT,
    ) -> dict[str, Any]:
        try:
            async with self._client(board_id) as client:
                snapshot = await client.board_snapshot(
                    limit=SNAPSHOT_LIMIT, max_bytes=SNAPSHOT_MAX_BYTES
                )
                events = await self._board_event_feed(
                    client,
                    int(snapshot.get("latest_seq", 0)),
                    event_limit,
                )
            return {
                "label": label,
                "board_id": board_id,
                "snapshot": snapshot,
                "events": events,
            }
        except Exception as exc:  # noqa: BLE001 - isolate one unavailable board.
            return {
                "label": label,
                "board_id": board_id,
                "error": type(exc).__name__,
            }

    async def fetch(self) -> dict[str, Any]:
        boards = await self._boards()
        rows = await asyncio.gather(
            *(self._read_board(label, board_id) for label, board_id in boards)
        )
        return aggregate_fleet(rows, stale_seconds=self.config.stale_seconds)

    async def fetch_board(self, board_id: str) -> dict[str, Any]:
        if not BOARD_ID_RE.fullmatch(board_id):
            raise KeyError(board_id)
        boards = await self._boards()
        match = next((item for item in boards if item[1] == board_id), None)
        if match is None:
            raise KeyError(board_id)
        row = await self._read_board(
            match[0], match[1], event_limit=DETAIL_EVENT_SCAN_LIMIT
        )
        if row.get("error"):
            raise RuntimeError(str(row["error"]))
        return project_board_detail(row)


class TimedCache:
    def __init__(
        self, ttl_seconds: float, loader: Callable[[], Awaitable[dict[str, Any]]]
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.loader = loader
        self._lock = threading.Lock()
        self._expires_at = 0.0
        self._value: dict[str, Any] | None = None

    def get(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._value is None or now >= self._expires_at:
                self._value = asyncio.run(self.loader())
                self._expires_at = time.monotonic() + self.ttl_seconds
            return self._value


class DashboardCache:
    def __init__(self, fetcher: FleetFetcher, ttl_seconds: float) -> None:
        self.fetcher = fetcher
        self.ttl_seconds = ttl_seconds
        self.fleet = TimedCache(ttl_seconds, fetcher.fetch)
        self._detail_lock = threading.Lock()
        self._details: dict[str, TimedCache] = {}

    def get(self) -> dict[str, Any]:
        return self.fleet.get()

    def get_board(self, board_id: str) -> dict[str, Any]:
        with self._detail_lock:
            cache = self._details.get(board_id)
            if cache is None:
                cache = TimedCache(
                    self.ttl_seconds,
                    lambda: self.fetcher.fetch_board(board_id),
                )
                self._details[board_id] = cache
        try:
            return cache.get()
        except Exception:  # noqa: BLE001 - discard failed cache entries.
            # Unknown or unavailable board IDs must not grow the cache forever.
            with self._detail_lock:
                if self._details.get(board_id) is cache:
                    self._details.pop(board_id, None)
            raise


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleet Dashboard</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151b2d;--panel2:#202942;--line:#29324a;--text:#e7ecf7;--muted:#9aa6bf;--good:#46d39a;--warn:#f4bd55;--bad:#ef6f7d;--accent:#79a8ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif}main{max-width:1500px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;gap:16px;align-items:end}h1,h2,h3,p{margin:0}h1{font-size:24px}h2{font-size:17px}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.muted,.meta{color:var(--muted)}.strip{display:grid;grid-template-columns:repeat(4,minmax(100px,1fr));gap:10px;margin:20px 0}.metric,.card{background:var(--panel);border:1px solid var(--line);border-radius:12px}.metric{padding:14px}.metric b{display:block;font-size:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(390px,1fr));gap:14px}.card{padding:16px;min-width:0}.board-link{display:block;color:inherit}.counts{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.pill{padding:4px 8px;border-radius:999px;background:var(--panel2)}table{width:100%;border-collapse:collapse}th,td{padding:8px 6px;text-align:left;border-top:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}.id{font-family:ui-monospace,SFMono-Regular,monospace;color:var(--accent);white-space:nowrap}.status{font-size:12px;border-radius:999px;padding:2px 6px;background:#26304a}.pool{margin-top:18px}.busy{color:var(--warn)}.available{color:var(--good)}.stale,.error{color:var(--bad)}#state{font-size:12px}.empty{color:var(--muted);padding:10px 0}.agent{border-top:1px solid var(--line)}.agent summary{cursor:pointer;display:grid;grid-template-columns:2fr 1fr 2fr 2fr;gap:8px;padding:10px 6px}.agent-body{padding:0 6px 12px}.warning{color:var(--warn)}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:18px 0}.toolbar select{background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:7px}.ticket-detail summary{cursor:pointer;color:var(--text)}.ticket-copy{white-space:pre-wrap;max-width:80ch;margin:8px 0}.back{display:inline-block;margin-bottom:16px}.detail-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(300px,1fr);gap:14px}.detail-card{overflow-x:auto}.required{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}.activity td{font-size:13px}@media(max-width:800px){main{padding:14px}.strip{grid-template-columns:repeat(2,1fr)}.grid,.detail-grid{grid-template-columns:1fr}.hide-small{display:none}.agent summary{grid-template-columns:1fr 1fr}.agent summary span:nth-child(n+3){display:none}}
</style></head><body><main><div class="top"><div><h1>Fleet Dashboard</h1><p class="muted">Live boards and shared agent pool</p></div><div id="state" class="muted">Loading…</div></div><section id="home-view"><section id="summary" class="strip"></section><section id="boards" class="grid"></section><section class="card pool"><h2>Agent pool</h2><div id="agents"></div></section></section><section id="detail-view" hidden></section></main><script>
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const fmt=v=>v?new Date(v).toLocaleString():'—';
const boardHref=id=>`#/board/${encodeURIComponent(id)}`;
const ticketHref=(board,id)=>`${boardHref(board)}?ticket=${encodeURIComponent(id)}`;
let fleetData=null,detailData=null,detailSort='newest',detailTimer=null;
function route(){const m=location.hash.match(/^#\/board\/([^?]+)(?:\?(.*))?$/);if(!m)return null;try{const board=decodeURIComponent(m[1]);if(!/^[A-Za-z0-9._-]{1,80}$/.test(board))return null;return{board,ticket:new URLSearchParams(m[2]||'').get('ticket')}}catch{return null}}
function renderFleet(d){const s=d.pool_summary;document.querySelector('#summary').innerHTML=['online','busy','available','stale'].map(k=>`<div class="metric"><span class="${k}">${esc(k)}</span><b>${esc(s[k])}</b></div>`).join('');document.querySelector('#boards').innerHTML=d.boards.map(b=>`<article class="card"><a class="board-link" href="${boardHref(b.board_id)}"><div class="top"><div><h2>${esc(b.label)}</h2><span class="meta">${esc(b.board_id)}</span></div>${b.truncated?'<span class="status">bounded view</span>':''}</div></a>${b.error?`<p class="error">Unavailable: ${esc(b.error)}</p>`:`<div class="counts">${Object.entries(b.counts).map(([k,v])=>`<span class="pill">${esc(k.replace('_',' '))}: <b>${esc(v)}</b></span>`).join('')}</div><table><thead><tr><th>Ticket</th><th>Title</th><th>Status</th><th class="hide-small">Claimed by</th></tr></thead><tbody>${b.tickets.length?b.tickets.map(t=>`<tr><td><a class="id" href="${ticketHref(b.board_id,t.id)}">${esc(t.id)}</a></td><td>${esc(t.title)}</td><td><span class="status">${esc(t.status)}</span></td><td class="hide-small">${esc(t.claimed_by||'—')}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No active tickets</td></tr>'}</tbody></table>`}</article>`).join('');document.querySelector('#agents').innerHTML=d.agents.length?d.agents.map(a=>`<details class="agent"><summary><b>${esc(a.agent_name)}${a.duplicate_name?' <span class="warning">duplicate name</span>':''}</b><span class="${esc(a.pool_status)}">${esc(a.pool_status)}</span><span>${esc(a.boards.join(', '))}</span><span>${esc(fmt(a.last_seen))}</span></summary><div class="agent-body"><table><thead><tr><th>Project</th><th>Role</th><th>Current claim</th><th>Last seen</th></tr></thead><tbody>${a.seats.map(seat=>`<tr><td><a href="${boardHref(seat.board_id)}">${esc(seat.project)}</a><div class="meta">${esc(seat.board_id)}</div></td><td>${esc(seat.role||'—')}</td><td>${seat.current_ticket_id?`<a class="id" href="${ticketHref(seat.board_id,seat.current_ticket_id)}">${esc(seat.current_ticket_id)}</a><div>${esc(seat.current_ticket_title)}</div>`:'—'}</td><td>${esc(fmt(seat.last_seen))}</td></tr>`).join('')}</tbody></table></div></details>`).join(''):'<p class="empty">No visible agents</p>';document.querySelector('#state').textContent=`Updated ${fmt(d.generated_at)}`}
function sortedTickets(items){const rank=s=>['claimed','in_progress','creating_report'].includes(s)?0:['submitted','reviewing','in_review'].includes(s)?1:s==='open'?2:3;return [...items].sort((a,b)=>rank(a.status)-rank(b.status)||(detailSort==='oldest'?String(a.updated_at||'').localeCompare(String(b.updated_at||'')):String(b.updated_at||'').localeCompare(String(a.updated_at||''))))}
function renderDetail(d){const r=route();if(!r||r.board!==d.board.board_id)return;const rows=sortedTickets(d.tickets),requestedVisible=!r.ticket||rows.some(t=>t.id===r.ticket);document.querySelector('#detail-view').innerHTML=`<a class="back" href="#/">← All boards</a><div class="top"><div><h2>${esc(d.board.label)}</h2><span class="meta">${esc(d.board.board_id)}</span></div>${d.truncated?`<span class="status">${esc(d.ticket_returned)} of ${esc(d.ticket_total)} tickets shown</span>`:''}</div>${requestedVisible?'':`<p class="warning">Requested ticket ${esc(r.ticket)} is outside this bounded response.</p>`}<div class="toolbar"><span>${esc(d.ticket_total)} bounded tickets</span><label>Updated <select id="ticket-sort"><option value="newest"${detailSort==='newest'?' selected':''}>newest first</option><option value="oldest"${detailSort==='oldest'?' selected':''}>oldest first</option></select></label></div><div class="detail-grid"><section class="card detail-card"><table><thead><tr><th>Ticket</th><th>Title and details</th><th>Status</th><th class="hide-small">Updated</th></tr></thead><tbody>${rows.length?rows.map(t=>`<tr><td><span class="id">${esc(t.id)}</span></td><td><details class="ticket-detail" data-ticket="${esc(t.id)}"${r.ticket===t.id?' open':''}><summary>${esc(t.title)}</summary><p class="ticket-copy">${esc(t.description||'No description')}</p>${t.required_fields.length?`<div class="required">${t.required_fields.map(x=>`<span class="pill">${esc(x)}</span>`).join('')}</div>`:''}${t.latest_submission_summary?`<p class="meta ticket-copy">Latest submission: ${esc(t.latest_submission_summary)}</p>`:''}${t.review_label?`<p class="meta">Review: ${esc(t.review_label)}</p>`:''}</details></td><td><span class="status">${esc(t.status)}</span><div class="meta">${esc(t.claimed_by||'')}</div></td><td class="meta hide-small">${esc(fmt(t.updated_at))}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No visible tickets</td></tr>'}</tbody></table></section><section class="card detail-card"><h3>Recent activity</h3><table class="activity"><tbody>${d.events.length?d.events.map(e=>`<tr><td class="id">${esc(e.seq??'—')}</td><td>${esc(e.kind)}</td><td>${e.ticket_id?`<a href="${ticketHref(d.board.board_id,e.ticket_id)}">${esc(e.ticket_id)}</a>`:''}<div class="meta">${esc(fmt(e.occurred_at))}</div></td></tr>`).join(''):'<tr><td class="empty">No recent visible activity</td></tr>'}</tbody></table></section></div>`;document.querySelector('#ticket-sort').addEventListener('change',e=>{detailSort=e.target.value;renderDetail(d)});document.querySelector('#state').textContent=`Updated ${fmt(d.generated_at)}`;if(r.ticket){const target=[...document.querySelectorAll('[data-ticket]')].find(x=>x.dataset.ticket===r.ticket);target?.scrollIntoView({block:'center'})}}
async function fetchJson(url){const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw new Error(`HTTP ${response.status}`);return response.json()}
async function refreshFleet(){try{fleetData=await fetchJson('/api/fleet');if(!route())renderFleet(fleetData)}catch(e){document.querySelector('#state').textContent=`Fleet refresh failed: ${e.message}`}}
async function refreshDetail(){const r=route();if(!r)return;try{const data=await fetchJson(`/api/board/${encodeURIComponent(r.board)}`);if(route()?.board!==r.board)return;detailData=data;renderDetail(data)}catch(e){document.querySelector('#detail-view').innerHTML=`<a class="back" href="#/">← All boards</a><p class="error">Board detail unavailable: ${esc(e.message)}</p>`;document.querySelector('#state').textContent='Detail refresh failed'}}
function syncRoute(){const r=route();document.querySelector('#home-view').hidden=!!r;document.querySelector('#detail-view').hidden=!r;if(detailTimer){clearInterval(detailTimer);detailTimer=null}if(r){document.querySelector('#detail-view').innerHTML='<p class="empty">Loading board detail…</p>';refreshDetail();detailTimer=setInterval(refreshDetail,5000)}else if(fleetData){renderFleet(fleetData)}}
window.addEventListener('hashchange',syncRoute);refreshFleet();setInterval(refreshFleet,5000);syncRoute();
</script></body></html>"""


def make_handler(cache: DashboardCache) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
                return
            if self.path == "/api/fleet":
                try:
                    body = _json_bytes(cache.get())
                except Exception as exc:  # noqa: BLE001 - return bounded HTTP error.
                    body = json.dumps({"error": type(exc).__name__}).encode("utf-8")
                    self._send(503, "application/json; charset=utf-8", body)
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            board_id = board_id_from_api_path(self.path)
            if board_id is not None:
                try:
                    body = _json_bytes(cache.get_board(board_id))
                except KeyError:
                    self._send(
                        404,
                        "application/json; charset=utf-8",
                        b'{"error":"board not found"}',
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - bounded type only.
                    body = json.dumps({"error": type(exc).__name__}).encode("utf-8")
                    self._send(503, "application/json; charset=utf-8", body)
                    return
                if len(body) > API_MAX_BYTES:
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        b'{"error":"detail response exceeds byte cap"}',
                    )
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return Handler


def _token_from_args(token_file: str | None) -> str:
    if token_file:
        token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    else:
        token = os.environ.get("ONBOARD_CENTRAL_TOKEN", "").strip()
    if not token:
        raise SystemExit("ONBOARD_CENTRAL_TOKEN or --token-file is required")
    return token


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the loopback fleet dashboard")
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument(
        "--url", default=os.environ.get("ONBOARD_CENTRAL_URL", DEFAULT_URL)
    )
    parser.add_argument("--token-file")
    parser.add_argument("--home-board", default=DEFAULT_HOME_BOARD)
    parser.add_argument("--agent-name", default="fleet-dashboard-viewer")
    parser.add_argument("--stale-seconds", type=int, default=300)
    parser.add_argument("--cache-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        parser.error("--host must be 127.0.0.1; non-loopback binding is refused")
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    if args.stale_seconds < 1 or args.cache_seconds <= 0:
        parser.error("stale and cache intervals must be positive")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = Config(
        url=args.url,
        token=_token_from_args(args.token_file),
        home_board=args.home_board,
        agent_name=args.agent_name,
        stale_seconds=args.stale_seconds,
        cache_seconds=args.cache_seconds,
    )
    fetcher = FleetFetcher(config)
    cache = DashboardCache(fetcher, config.cache_seconds)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(cache))
    print(f"Fleet Dashboard: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
