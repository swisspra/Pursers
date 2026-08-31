#!/usr/bin/env python3
"""Loopback-only, read-only fleet dashboard for Pursers boards."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

# Prefer the sibling source checkout over any installed pursers-client wheel:
# the dashboard depends on keyword arguments newer than the last published wheel.
_CLIENT_SRC = Path(__file__).resolve().parents[2] / "packages" / "client" / "src"
if (_CLIENT_SRC / "pursers_client").is_dir():
    sys.path.insert(0, str(_CLIENT_SRC))
from pursers_client import BoardClient, BoardClientError  # noqa: I001


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
MAX_FINDINGS = 50
MAX_FINDING_CHARS = 500
MAX_OVERHEAD_FILE_BYTES = 2_000_000
MAX_OVERHEAD_SEATS = 200
MAX_OVERHEAD_TOOLS = 5
OVERHEAD_DAYS = 7
BOARD_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
ACTIVE_CLAIM_STATES = frozenset({"claimed", "in_progress", "creating_report"})
SUBMITTED_STATES = frozenset({"submitted", "reviewing", "in_review"})
TERMINAL_STATES = frozenset({"closed", "rejected", "canceled", "terminated"})
CONFIG_STATE_KEY = "coordinator_config"
FINDINGS_STATE_KEY = "coordinator_findings"
CONFIG_CATEGORIES = (
    "docs", "tests", "audit-analysis", "bug", "production-code",
    "release-ci", "membership-roles", "board-registry",
)
CONFIG_THRESHOLD_FIELDS = (
    "stale_seconds", "lease_warning_ratio", "grace_seconds", "starved_seconds",
    "critical_starved_seconds", "review_backlog_seconds", "abandoner_drops",
    "abandoner_window_days",
)


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


def _state_value(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    state = raw.get("state") if isinstance(raw, dict) else None
    value = state.get("value") if isinstance(state, dict) else None
    if not isinstance(value, str):
        return None, None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None, value
    return (dict(parsed), value) if isinstance(parsed, dict) else (None, value)


def validate_coordinator_config(value: Any) -> dict[str, Any]:
    """Validate the complete dashboard-owned value; no arbitrary state keys pass."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "thresholds", "integration_watch_since", "intake"
    }:
        raise ValueError("config must contain only the coordinator schema fields")
    if value.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    thresholds = value.get("thresholds")
    if not isinstance(thresholds, dict) or set(thresholds) != set(CONFIG_THRESHOLD_FIELDS):
        raise ValueError("thresholds must contain every known threshold")
    for name in (
        "stale_seconds", "grace_seconds", "starved_seconds",
        "critical_starved_seconds", "review_backlog_seconds",
    ):
        if type(thresholds[name]) is not int or not 10 <= thresholds[name] <= 86_400:
            raise ValueError(f"{name} must be between 10 and 86400")
    ratio = thresholds["lease_warning_ratio"]
    if type(ratio) not in (int, float) or not 0.1 <= ratio <= 1:
        raise ValueError("lease_warning_ratio must be between 0.1 and 1")
    if type(thresholds["abandoner_drops"]) is not int or not 1 <= thresholds["abandoner_drops"] <= 20:
        raise ValueError("abandoner_drops must be between 1 and 20")
    if type(thresholds["abandoner_window_days"]) is not int or not 1 <= thresholds["abandoner_window_days"] <= 365:
        raise ValueError("abandoner_window_days must be between 1 and 365")
    watermark = value.get("integration_watch_since")
    if watermark is not None and _parse_time(watermark) is None:
        raise ValueError("integration_watch_since must be null or ISO-8601")
    intake = value.get("intake")
    if not isinstance(intake, dict) or set(intake) != {
        "enabled", "auto_categories", "always_ask_categories",
        "work_domain_always_ask", "rate_per_hour",
    }:
        raise ValueError("intake must contain every known intake field")
    if type(intake["enabled"]) is not bool or type(intake["work_domain_always_ask"]) is not bool:
        raise ValueError("intake switches must be booleans")
    auto, always = intake["auto_categories"], intake["always_ask_categories"]
    if not all(isinstance(rows, list) and all(type(item) is str for item in rows) for rows in (auto, always)):
        raise ValueError("intake categories must be arrays")
    if len(set(auto)) != len(auto) or len(set(always)) != len(always):
        raise ValueError("intake categories must not contain duplicates")
    if set(auto) & set(always) or set(auto) | set(always) != set(CONFIG_CATEGORIES):
        raise ValueError("intake categories must be known, disjoint, and complete")
    if type(intake["rate_per_hour"]) is not int or not 1 <= intake["rate_per_hour"] <= 20:
        raise ValueError("rate_per_hour must be between 1 and 20")
    return json.loads(json.dumps(value))


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


def bridge_stats_path() -> Path:
    configured = os.environ.get("PURSERS_BRIDGE_STATS", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path(__file__).resolve().parents[1]
        / "wait-bridge"
        / "bridge-stats.json"
    )


def _nonnegative_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def read_overhead_stats(
    path: str | Path, *, now: datetime | None = None
) -> dict[str, Any]:
    """Return a bounded size/count-only projection; bad files become empty state."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = current.date().isoformat()
    empty = {
        "generated_at": current.isoformat(),
        "today": today,
        "source_status": "missing",
        "note": "protocol overhead (estimated), not provider billing",
        "seats": [],
        "bounds": {
            "days": OVERHEAD_DAYS,
            "seats": MAX_OVERHEAD_SEATS,
            "top_tools": MAX_OVERHEAD_TOOLS,
        },
    }
    source = Path(path).expanduser().resolve()
    try:
        if source.stat().st_size > MAX_OVERHEAD_FILE_BYTES:
            return {**empty, "source_status": "malformed"}
        document = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty
    except (OSError, UnicodeError, ValueError):
        return {**empty, "source_status": "malformed"}
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        return {**empty, "source_status": "malformed"}
    raw_days = document.get("days")
    if not isinstance(raw_days, dict):
        return {**empty, "source_status": "malformed"}
    first_day = (current.date() - timedelta(days=OVERHEAD_DAYS - 1)).isoformat()
    selected_days = []
    for raw_day, value in raw_days.items():
        if not isinstance(raw_day, str) or not isinstance(value, dict):
            continue
        try:
            parsed_day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        day = parsed_day.isoformat()
        if day == raw_day and first_day <= day <= today:
            selected_days.append(day)
    selected_days.sort()
    aggregate: dict[tuple[str, str], dict[str, Any]] = {}
    for day in selected_days:
        seats = raw_days[day].get("seats")
        if not isinstance(seats, dict):
            continue
        for raw in seats.values():
            if not isinstance(raw, dict):
                continue
            board_id = raw.get("board_id")
            agent_name = raw.get("agent_name")
            if not all(isinstance(value, str) and value for value in (board_id, agent_name)):
                continue
            key = (board_id, agent_name)
            row = aggregate.setdefault(
                key,
                {
                    "board_id": _clip(board_id, MAX_LABEL_CHARS),
                    "agent_name": _clip(agent_name, MAX_LABEL_CHARS),
                    "today_bytes": 0,
                    "seven_day_bytes": 0,
                    "today_calls": 0,
                    "seven_day_calls": 0,
                    "tools": {},
                },
            )
            request_bytes = _nonnegative_int(raw.get("request_bytes"))
            response_bytes = _nonnegative_int(raw.get("response_bytes"))
            total_bytes = request_bytes + response_bytes
            row["seven_day_bytes"] += total_bytes
            calls = raw.get("calls") if isinstance(raw.get("calls"), dict) else {}
            day_calls = 0
            for tool_name, tool_raw in calls.items():
                if not isinstance(tool_name, str) or not isinstance(tool_raw, dict):
                    continue
                count = _nonnegative_int(tool_raw.get("count"))
                tool_bytes = _nonnegative_int(tool_raw.get("request_bytes")) + _nonnegative_int(
                    tool_raw.get("response_bytes")
                )
                day_calls += count
                tool = row["tools"].setdefault(tool_name, {"calls": 0, "bytes": 0})
                tool["calls"] += count
                tool["bytes"] += tool_bytes
            row["seven_day_calls"] += day_calls
            if day == today:
                row["today_bytes"] += total_bytes
                row["today_calls"] += day_calls
    rows = []
    for row in aggregate.values():
        tools = sorted(
            (
                {
                    "tool": _clip(name, MAX_LABEL_CHARS),
                    "bytes": values["bytes"],
                    "estimated_tokens": (values["bytes"] + 3) // 4,
                    "calls": values["calls"],
                }
                for name, values in row.pop("tools").items()
            ),
            key=lambda item: (-item["bytes"], item["tool"]),
        )[:MAX_OVERHEAD_TOOLS]
        row["today_estimated_tokens"] = (row["today_bytes"] + 3) // 4
        row["seven_day_estimated_tokens"] = (row["seven_day_bytes"] + 3) // 4
        row["top_tools"] = tools
        rows.append(row)
    rows.sort(key=lambda item: (-item["today_bytes"], item["board_id"], item["agent_name"]))
    result = {
        **empty,
        "source_status": "ok",
        "seats": rows[:MAX_OVERHEAD_SEATS],
        "truncated_seats": max(0, len(rows) - MAX_OVERHEAD_SEATS),
    }
    while len(_json_bytes(result)) > API_MAX_BYTES and result["seats"]:
        result["seats"].pop()
        result["truncated_seats"] += 1
    return result


def project_coordinator_findings(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    state = snapshot.get("state")
    if not isinstance(state, dict) or "coordinator_findings" not in state:
        return None
    entry = state["coordinator_findings"]
    raw = entry.get("value") if isinstance(entry, dict) and "value" in entry else entry
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    reported_truncated = 0
    if isinstance(raw, list):
        findings = raw
    elif isinstance(raw, dict):
        findings = raw.get("findings", raw.get("items", []))
        truncation = raw.get("truncation")
        if isinstance(truncation, dict):
            reported_truncated = _nonnegative_int(truncation.get("findings"))
        for name in ("truncated_count", "omitted_count", "truncated"):
            value = raw.get(name)
            if reported_truncated == 0 and type(value) is int and value > 0:
                reported_truncated = value
                break
    else:
        return None
    if not isinstance(findings, list):
        return None
    items = []
    for finding in findings[:MAX_FINDINGS]:
        if not isinstance(finding, dict):
            continue
        level = str(finding.get("level") or "info").lower()
        if level not in {"info", "warn", "critical"}:
            level = "info"
        kind = _clip(finding.get("kind") or "finding", MAX_LABEL_CHARS)
        text = finding.get("message") or finding.get("summary") or finding.get("detail")
        if not isinstance(text, str):
            text = json.dumps(finding, ensure_ascii=False, sort_keys=True)
        items.append(
            {
                "kind": kind,
                "level": level,
                "text": _clip(text, MAX_FINDING_CHARS),
                "ticket_id": _clip(finding.get("ticket_id"), MAX_LABEL_CHARS) or None,
            }
        )
    return {
        "items": items,
        "truncated_count": reported_truncated + max(0, len(findings) - MAX_FINDINGS),
    }


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
        "closed_at": _clip(ticket.get("closed_at"), 40) or None,
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


def group_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group bounded events by UTC day then ticket, newest activity first."""
    grouped: dict[str, dict[str, list[int]]] = {}
    for event in events:
        seq = event.get("seq")
        occurred_at = _parse_time(event.get("occurred_at"))
        if type(seq) is not int or occurred_at is None:
            continue
        day = occurred_at.date().isoformat()
        ticket_id = str(event.get("ticket_id") or "Board activity")
        grouped.setdefault(day, {}).setdefault(ticket_id, []).append(seq)
    result = []
    for day in sorted(grouped, reverse=True):
        tickets = [
            {"ticket_id": ticket_id, "event_seqs": sorted(seqs, reverse=True)}
            for ticket_id, seqs in grouped[day].items()
        ]
        tickets.sort(key=lambda item: (-max(item["event_seqs"]), item["ticket_id"]))
        result.append({"day": day, "tickets": tickets})
    return result


def summarize_changes(
    events: list[dict[str, Any]],
    *,
    since_seq: int | None = None,
    since_time: datetime | None = None,
) -> dict[str, Any]:
    """Count ticket lifecycle changes in a deterministic bounded event window."""
    if since_seq is not None and (type(since_seq) is not int or since_seq < 0):
        raise ValueError("since_seq must be a non-negative integer")
    cutoff = since_time.astimezone(timezone.utc) if since_time is not None else None
    counts = {
        name: 0
        for name in ("created", "claimed", "submitted", "closed", "rejected")
    }
    selected = 0
    for event in events:
        seq = event.get("seq")
        occurred_at = _parse_time(event.get("occurred_at"))
        if since_seq is not None:
            if type(seq) is not int or seq <= since_seq:
                continue
        elif cutoff is not None and (occurred_at is None or occurred_at < cutoff):
            continue
        selected += 1
        kind = event.get("kind")
        status_from = event.get("status_from")
        status_to = event.get("status_to")
        if kind == "ticket_created":
            counts["created"] += 1
        if status_to == "claimed":
            counts["claimed"] += 1
        if status_to == "submitted":
            counts["submitted"] += 1
        if status_to == "closed":
            counts["closed"] += 1
        if event.get("review_verdict") == "reject" or (
            status_from == "submitted"
            and status_to in {"open", "claimed", "rejected"}
            and _nonnegative_int(event.get("rejection_count")) > 0
        ):
            counts["rejected"] += 1
    return {"counts": counts, "event_count": selected}


def classify_ticket_flow(
    tickets: list[dict[str, Any]], *, now: datetime
) -> dict[str, list[str]]:
    """Classify bounded ticket rows into the four dashboard flow columns."""
    today = now.astimezone(timezone.utc).date()
    flow = {name: [] for name in ("open", "claimed", "submitted", "closed_today")}
    for ticket in tickets:
        ticket_id = str(ticket.get("id") or "")
        if not ticket_id:
            continue
        status = ticket.get("status")
        if status == "open":
            flow["open"].append(ticket_id)
        elif status in ACTIVE_CLAIM_STATES:
            flow["claimed"].append(ticket_id)
        elif status in SUBMITTED_STATES:
            flow["submitted"].append(ticket_id)
        elif status == "closed":
            closed_at = _parse_time(ticket.get("closed_at") or ticket.get("updated_at"))
            if closed_at is not None and closed_at.date() == today:
                flow["closed_today"].append(ticket_id)
    return flow


def _refresh_detail_views(result: dict[str, Any], now: datetime) -> None:
    result["timeline"] = group_timeline(result["events"])
    result["changes_24h"] = summarize_changes(
        result["events"], since_time=now - timedelta(hours=24)
    )
    result["ticket_flow"] = classify_ticket_flow(result["tickets"], now=now)


def project_board_detail(
    raw: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Project one bounded snapshot and catchup page for the browser."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
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
                "status_from": _clip(event.get("status_from"), 32) or None,
                "status_to": _clip(event.get("status_to"), 32) or None,
                "actor": _clip(event.get("actor"), MAX_LABEL_CHARS) or None,
                "review_verdict": _clip(event.get("review_verdict"), 16) or None,
                "rejection_count": _nonnegative_int(event.get("rejection_count")),
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
        "generated_at": now.isoformat(),
        "board": {
            "board_id": _clip(raw.get("board_id"), MAX_LABEL_CHARS),
            "label": _clip(raw.get("label") or raw.get("board_id"), MAX_LABEL_CHARS),
        },
        "tickets": tickets[:MAX_DETAIL_TICKET_ROWS],
        "events": events,
        "event_returned": len(events),
        "coordinator_findings": project_coordinator_findings(snapshot),
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
    _refresh_detail_views(result, now)
    while len(_json_bytes(result)) > API_MAX_BYTES and result["tickets"]:
        result["tickets"].pop()
        result["ticket_returned"] = len(result["tickets"])
        result["ticket_omitted"] = max(
            0, result["ticket_total"] - result["ticket_returned"]
        )
        result["truncated"] = True
        _refresh_detail_views(result, now)
    while len(_json_bytes(result)) > API_MAX_BYTES and result["events"]:
        result["events"].pop(0)
        result["event_returned"] = len(result["events"])
        result["truncated"] = True
        _refresh_detail_views(result, now)
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

    async def fetch_config(self) -> dict[str, Any]:
        async with self._client(self.config.home_board) as client:
            try:
                raw_config = await client.board_state_get(key=CONFIG_STATE_KEY)
            except BoardClientError as exc:
                if "state key not found" not in str(exc):
                    raise
                raw_config = {}
            try:
                raw_findings = await client.board_state_get(key=FINDINGS_STATE_KEY)
            except BoardClientError as exc:
                if "state key not found" not in str(exc):
                    raise
                raw_findings = {}
        stored, stored_text = _state_value(raw_config)
        findings, _ = _state_value(raw_findings)
        findings = findings or {}
        return {
            "config": stored,
            "effective": findings.get("effective_config", {}),
            "sources": findings.get("config_sources", {}),
            "mode": findings.get("effective_mode", "unknown"),
            "updated_at": stored.get("updated_at") if stored else None,
            "updated_by": stored.get("updated_by") if stored else None,
            "expected_sha256": (
                hashlib.sha256(stored_text.encode("utf-8")).hexdigest()
                if stored_text is not None else None
            ),
            "concurrency": "cas" if stored_text is not None else "lww",
        }

    async def save_config(
        self, value: Any, expected_sha256: str | None
    ) -> dict[str, Any]:
        clean = validate_coordinator_config(value)
        clean["updated_at"] = datetime.now(timezone.utc).isoformat()
        clean["updated_by"] = self.config.agent_name
        encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        async with self._client(self.config.home_board) as client:
            arguments = {
                "agent_name": self.config.agent_name,
                "key": CONFIG_STATE_KEY,
                "value": encoded,
            }
            if expected_sha256 is not None:
                if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                    raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
                arguments["expected_sha256"] = expected_sha256
            await client._call("board_state_update", arguments)  # noqa: SLF001
        return {
            "ok": True,
            "config": clean,
            "expected_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "concurrency": "cas" if expected_sha256 is not None else "lww",
        }


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

    def get_config(self) -> dict[str, Any]:
        return asyncio.run(self.fetcher.fetch_config())

    def save_config(self, value: Any, expected_sha256: str | None) -> dict[str, Any]:
        return asyncio.run(self.fetcher.save_config(value, expected_sha256))


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleet Dashboard</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151b2d;--panel2:#202942;--line:#29324a;--text:#e7ecf7;--muted:#9aa6bf;--good:#46d39a;--warn:#f4bd55;--bad:#ef6f7d;--accent:#79a8ff}*{box-sizing:border-box}html,body{max-width:100%;overflow-x:hidden}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif}main{width:100%;max-width:1500px;min-width:0;margin:auto;padding:24px}.top,.toolbar{display:flex;justify-content:space-between;flex-wrap:wrap;gap:12px}.top{align-items:end}h1,h2,h3,p{margin:0}h1{font-size:24px}h2{font-size:17px}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.muted,.meta{color:var(--muted)}.strip{display:grid;grid-template-columns:repeat(4,minmax(100px,1fr));gap:10px;margin:20px 0}.metric,.card{background:var(--panel);border:1px solid var(--line);border-radius:12px}.metric,.card{padding:14px}.metric b{display:block;font-size:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(390px,100%),1fr));gap:14px}.card{min-width:0}.board-link{display:block;color:inherit}.counts,.tabs,.required{display:flex;flex-wrap:wrap;gap:8px}.counts{margin:12px 0}.pill,.tab{padding:4px 8px;border-radius:999px;background:var(--panel2)}.tab.active{outline:2px solid var(--accent)}.table-scroll{width:100%;max-width:100%;overflow-x:auto}table{width:100%;border-collapse:collapse}th,td{padding:8px 6px;text-align:left;border-top:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}.id{font-family:ui-monospace,SFMono-Regular,monospace;color:var(--accent);white-space:nowrap}.status{font-size:12px;border-radius:999px;padding:2px 6px;background:#26304a}.pool{margin-top:18px}.warning{color:var(--warn)}.error{color:var(--bad)}#state{font-size:12px}.empty{color:var(--muted);padding:10px 0}.agent{border-top:1px solid var(--line)}.agent summary{cursor:pointer;display:grid;grid-template-columns:2fr 1fr 2fr 2fr;gap:8px;padding:10px 6px}.agent-body{padding:0 6px 12px}.toolbar{align-items:center;margin:18px 0}.toolbar select,.toolbar input,.toolbar button,#filter{background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px}.search{min-width:min(300px,45vw)}.ticket-detail summary,.timeline summary{cursor:pointer}.ticket-copy{white-space:pre-wrap;overflow-wrap:anywhere;max-width:80ch;margin:8px 0}.back{display:inline-block;margin-bottom:16px}.required{margin-top:8px}.finding-list,.timeline,.flow{display:grid;gap:10px;margin-top:10px}.finding{border-left:3px solid var(--line);padding-left:10px}.timeline-ticket{margin:8px 0 0 14px}.flow{grid-template-columns:repeat(4,minmax(0,1fr))}.flow-column{background:var(--panel2);border-radius:10px;padding:10px;min-width:0}.flow-card{display:block;margin-top:8px;padding:9px;border:1px solid var(--line);border-radius:8px;color:var(--text);overflow-wrap:anywhere}.change-grid{grid-template-columns:repeat(5,minmax(100px,1fr))}.bounded-note{margin-top:10px}.overhead-tools{max-width:36ch}@media(max-width:800px){main{padding:14px}.strip,.change-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.grid,.flow{grid-template-columns:1fr}.hide-small{display:none}.agent summary{grid-template-columns:1fr 1fr}.agent summary span:nth-child(n+3){display:none}.search{min-width:0;width:100%}.top{align-items:start}}
</style></head><body><main><div class="top"><div><h1>Fleet Dashboard</h1><p class="muted">Live boards and shared agent pool</p></div><div><input id="filter" class="search" type="search" placeholder="Filter tickets, boards, agents…" aria-label="Filter dashboard"><div id="state" class="muted">Loading…</div></div></div><section id="home-view"><section id="summary" class="strip"></section><section id="boards" class="grid"></section><section class="card pool"><h2>Agent pool</h2><div id="agents"></div></section><section class="card pool"><h2>Protocol overhead</h2><p class="muted">Estimated from request and response bytes; not provider billing.</p><div id="overhead"></div></section></section><section id="detail-view" hidden></section></main><script>
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const fmt=v=>v?new Date(v).toLocaleString():'—',boardHref=(id,view='tickets')=>`#/board/${encodeURIComponent(id)}/${view}`,ticketHref=(board,id)=>`${boardHref(board)}?ticket=${encodeURIComponent(id)}`,matches=(values,needle)=>!needle||values.map(v=>String(v??'').toLocaleLowerCase()).join(' ').includes(needle);
const ticketMatches=(t,needle)=>matches([t.id,t.title,t.status,t.claimed_by,t.description],needle);
const filterHomeBoards=(boards,needle)=>boards.map(b=>({...b,tickets:b.tickets.filter(t=>ticketMatches(t,needle))})).filter(b=>matches([b.label,b.board_id],needle)||b.tickets.length);
const eventMatches=(e,ticketId,needle)=>matches([ticketId,e.kind,e.status_from,e.status_to,e.actor],needle);
const filterChangeEvents=(events,needle)=>events.filter(e=>eventMatches(e,e.ticket_id,needle));
let fleetData=null,detailData=null,detailSort='newest',detailTimer=null,filterNeedle='';
function route(){const m=location.hash.match(/^#\/board\/([^/?]+)(?:\/(tickets|timeline|changes|flow))?(?:\?(.*))?$/);if(!m)return null;try{const board=decodeURIComponent(m[1]);if(!/^[A-Za-z0-9._-]{1,80}$/.test(board))return null;const q=new URLSearchParams(m[3]||'');return{board,view:m[2]||'tickets',ticket:q.get('ticket'),since:q.get('since')}}catch{return null}}
function renderFleet(d){const s=d.pool_summary;document.querySelector('#summary').innerHTML=['online','busy','available','stale'].map(k=>`<div class="metric"><span>${esc(k)}</span><b>${esc(s[k])}</b></div>`).join('');const boards=filterHomeBoards(d.boards,filterNeedle);document.querySelector('#boards').innerHTML=boards.map(b=>`<article class="card"><a class="board-link" href="${boardHref(b.board_id)}"><div class="top"><div><h2>${esc(b.label)}</h2><span class="meta">${esc(b.board_id)}</span></div>${b.truncated?'<span class="status">bounded view</span>':''}</div></a>${b.error?`<p class="error">Unavailable: ${esc(b.error)}</p>`:`<div class="counts">${Object.entries(b.counts).map(([k,v])=>`<span class="pill">${esc(k.replace('_',' '))}: <b>${esc(v)}</b></span>`).join('')}</div><div class="table-scroll"><table><thead><tr><th>Ticket</th><th>Title</th><th>Status</th><th class="hide-small">Claimed by</th></tr></thead><tbody>${b.tickets.length?b.tickets.map(t=>`<tr><td><a class="id" href="${ticketHref(b.board_id,t.id)}">${esc(t.id)}</a></td><td>${esc(t.title)}</td><td><span class="status">${esc(t.status)}</span></td><td class="hide-small">${esc(t.claimed_by||'—')}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No matching tickets</td></tr>'}</tbody></table></div>`}</article>`).join('')||'<p class="empty">No boards match the filter.</p>';const agents=d.agents.filter(a=>matches([a.agent_name,a.pool_status,...a.boards],filterNeedle));document.querySelector('#agents').innerHTML=agents.length?agents.map(a=>`<details class="agent"><summary><b>${esc(a.agent_name)}${a.duplicate_name?' <span class="warning">duplicate name</span>':''}</b><span>${esc(a.pool_status)}</span><span>${esc(a.boards.join(', '))}</span><span>${esc(fmt(a.last_seen))}</span></summary><div class="agent-body table-scroll"><table><thead><tr><th>Project</th><th>Role</th><th>Current claim</th><th>Last seen</th></tr></thead><tbody>${a.seats.map(seat=>`<tr><td><a href="${boardHref(seat.board_id)}">${esc(seat.project)}</a><div class="meta">${esc(seat.board_id)}</div></td><td>${esc(seat.role||'—')}</td><td>${seat.current_ticket_id?`<a class="id" href="${ticketHref(seat.board_id,seat.current_ticket_id)}">${esc(seat.current_ticket_id)}</a><div>${esc(seat.current_ticket_title)}</div>`:'—'}</td><td>${esc(fmt(seat.last_seen))}</td></tr>`).join('')}</tbody></table></div></details>`).join(''):'<p class="empty">No agents match the filter.</p>';document.querySelector('#state').textContent=`Updated ${fmt(d.generated_at)}`}
function renderOverhead(d){document.querySelector('#overhead').innerHTML=d.seats.length?`<div class="table-scroll"><table><thead><tr><th>Seat</th><th>Today</th><th>7-day</th><th>Top tools by bytes</th></tr></thead><tbody>${d.seats.map(s=>`<tr><td><b>${esc(s.agent_name)}</b><div class="meta">${esc(s.board_id)}</div></td><td>${esc(s.today_bytes)} B<div class="meta">≈ ${esc(s.today_estimated_tokens)} tokens · ${esc(s.today_calls)} calls</div></td><td>${esc(s.seven_day_bytes)} B<div class="meta">≈ ${esc(s.seven_day_estimated_tokens)} tokens · ${esc(s.seven_day_calls)} calls</div></td><td class="overhead-tools">${s.top_tools.map(t=>`${esc(t.tool)}: ${esc(t.bytes)} B`).join(' · ')||'—'}</td></tr>`).join('')}</tbody></table></div>`:`<p class="empty">No bridge overhead stats yet (${esc(d.source_status)}).</p>`}
function sortedTickets(items){const rank=s=>['claimed','in_progress','creating_report'].includes(s)?0:['submitted','reviewing','in_review'].includes(s)?1:s==='open'?2:3;return [...items].sort((a,b)=>rank(a.status)-rank(b.status)||(detailSort==='oldest'?String(a.updated_at||'').localeCompare(String(b.updated_at||'')):String(b.updated_at||'').localeCompare(String(a.updated_at||''))))}
function tabs(d,r){return `<nav class="tabs" aria-label="Board views">${[['tickets','Tickets'],['timeline','Timeline'],['changes','Changes'],['flow','Ticket Flow']].map(([v,label])=>`<a class="tab${r.view===v?' active':''}" href="${boardHref(d.board.board_id,v)}">${esc(label)}</a>`).join('')}</nav>`}
function ticketView(d,r){const rows=sortedTickets(d.tickets).filter(t=>matches([t.id,t.title,t.status,t.claimed_by,t.description],filterNeedle));const visible=!r.ticket||rows.some(t=>t.id===r.ticket);return `${visible?'':`<p class="warning">Requested ticket ${esc(r.ticket)} is outside this bounded response or filter.</p>`}<div class="toolbar"><span>${esc(rows.length)} of ${esc(d.ticket_returned)} returned tickets</span><label>Updated <select id="ticket-sort"><option value="newest"${detailSort==='newest'?' selected':''}>newest first</option><option value="oldest"${detailSort==='oldest'?' selected':''}>oldest first</option></select></label></div><section class="card"><div class="table-scroll"><table><thead><tr><th>Ticket</th><th>Title and details</th><th>Status</th><th class="hide-small">Updated</th></tr></thead><tbody>${rows.length?rows.map(t=>`<tr><td><span class="id">${esc(t.id)}</span></td><td><details class="ticket-detail" data-ticket="${esc(t.id)}"${r.ticket===t.id?' open':''}><summary>${esc(t.title)}</summary><p class="ticket-copy">${esc(t.description||'No description')}</p>${t.required_fields.length?`<div class="required">${t.required_fields.map(x=>`<span class="pill">${esc(x)}</span>`).join('')}</div>`:''}${t.latest_submission_summary?`<p class="meta ticket-copy">Latest submission: ${esc(t.latest_submission_summary)}</p>`:''}${t.review_label?`<p class="meta">Review: ${esc(t.review_label)}</p>`:''}</details></td><td><span class="status">${esc(t.status)}</span><div class="meta">${esc(t.claimed_by||'')}</div></td><td class="meta hide-small">${esc(fmt(t.updated_at))}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No tickets match the filter.</td></tr>'}</tbody></table></div></section>`}
function timelineView(d){const bySeq=new Map(d.events.map(e=>[e.seq,e]));const groups=d.timeline.map(day=>({...day,tickets:day.tickets.map(t=>({...t,events:t.event_seqs.map(seq=>bySeq.get(seq)).filter(Boolean).filter(e=>eventMatches(e,t.ticket_id,filterNeedle))})).filter(t=>t.events.length)})).filter(day=>day.tickets.length);return `<p class="muted bounded-note">Showing last ${esc(d.event_returned)} events from a read-only bounded catchup (ack=false).</p><section class="timeline">${groups.length?groups.map(day=>`<details class="card" open><summary><b>${esc(day.day)}</b></summary>${day.tickets.map(t=>`<details class="timeline-ticket"><summary><a class="id" href="${ticketHref(d.board.board_id,t.ticket_id)}">${esc(t.ticket_id)}</a> · ${esc(t.events.length)} event(s)</summary><div class="table-scroll"><table><tbody>${t.events.map(e=>`<tr><td class="id">${esc(e.seq)}</td><td>${esc(e.kind)}</td><td>${esc(e.status_from||'—')} → ${esc(e.status_to||'—')}</td><td class="meta">${esc(fmt(e.occurred_at))}</td></tr>`).join('')}</tbody></table></div></details>`).join('')}</details>`).join(''):'<p class="empty">No timeline events match the filter.</p>'}</section>`}
function changesFor(events,since,generatedAt){const cutoff=since===null?new Date(generatedAt).getTime()-86400000:null,chosen=events.filter(e=>since!==null?Number.isInteger(e.seq)&&e.seq>since:new Date(e.occurred_at).getTime()>=cutoff),counts={created:0,claimed:0,submitted:0,closed:0,rejected:0};for(const e of chosen){if(e.kind==='ticket_created')counts.created++;if(e.status_to==='claimed')counts.claimed++;if(e.status_to==='submitted')counts.submitted++;if(e.status_to==='closed')counts.closed++;if(e.review_verdict==='reject'||(e.status_from==='submitted'&&['open','claimed','rejected'].includes(e.status_to)&&Number(e.rejection_count)>0))counts.rejected++}return{counts,event_count:chosen.length}}
function changesView(d,r){const valid=r.since!==null&&/^\d+$/.test(r.since),since=valid?Number(r.since):null,events=filterChangeEvents(d.events,filterNeedle),summary=changesFor(events,since,d.generated_at);return `<div class="toolbar"><div><b>${since===null?'Last 24 hours':`After seq ${esc(since)}`}</b><p class="muted">Calculated only from the ${esc(d.event_returned)} returned events.</p></div><form id="changes-form"><label>Starting seq <input id="since-seq" inputmode="numeric" pattern="[0-9]*" value="${since===null?'':esc(since)}" placeholder="blank = 24h"></label> <button type="submit">Apply</button></form></div><section class="strip change-grid">${Object.entries(summary.counts).map(([name,count])=>`<div class="metric"><span>${esc(name)}</span><b>${esc(count)}</b></div>`).join('')}</section><p class="muted">${esc(summary.event_count)} bounded event(s) matched.</p>`}
function flowView(d){const byId=new Map(d.tickets.map(t=>[t.id,t])),labels={open:'Open',claimed:'Claimed',submitted:'Submitted',closed_today:'Closed today'};return `<p class="muted bounded-note">Classified from ${esc(d.ticket_returned)} returned tickets; omitted snapshot rows are not inferred.</p><section class="flow">${Object.entries(labels).map(([key,label])=>{const tickets=d.ticket_flow[key].map(id=>byId.get(id)).filter(Boolean).filter(t=>matches([t.id,t.title,t.claimed_by,t.status],filterNeedle));return `<div class="flow-column"><h3>${esc(label)} · ${esc(tickets.length)}</h3>${tickets.map(t=>`<a class="flow-card" href="${ticketHref(d.board.board_id,t.id)}"><span class="id">${esc(t.id)}</span><div>${esc(t.title)}</div><span class="meta">${esc(t.claimed_by||'Unassigned')}</span></a>`).join('')||'<p class="empty">No matching tickets</p>'}</div>`}).join('')}</section>`}
function findings(d){if(!d.coordinator_findings)return'';return `<section class="card"><h3>Coordinator findings</h3><div class="finding-list">${d.coordinator_findings.items.map(f=>`<div class="finding"><b>${esc(f.kind)}</b>${f.ticket_id?` <span class="id">${esc(f.ticket_id)}</span>`:''}<p>${esc(f.text)}</p></div>`).join('')||'<p class="empty">No current findings</p>'}</div>${d.coordinator_findings.truncated_count?`<p class="warning">${esc(d.coordinator_findings.truncated_count)} findings omitted by the bounded state.</p>`:''}</section>`}
function renderDetail(d){const r=route();if(!r||r.board!==d.board.board_id)return;const views={tickets:ticketView,timeline:timelineView,changes:changesView,flow:flowView};document.querySelector('#detail-view').innerHTML=`<a class="back" href="#/">← All boards</a><div class="top"><div><h2>${esc(d.board.label)}</h2><span class="meta">${esc(d.board.board_id)}</span></div>${d.truncated?`<span class="status">${esc(d.ticket_returned)} of ${esc(d.ticket_total)} tickets shown</span>`:''}</div><div class="toolbar">${tabs(d,r)}<span class="muted">Read-only bounded view</span></div>${r.view==='tickets'?findings(d):''}${views[r.view](d,r)}`;document.querySelector('#ticket-sort')?.addEventListener('change',e=>{detailSort=e.target.value;renderDetail(d)});document.querySelector('#changes-form')?.addEventListener('submit',e=>{e.preventDefault();const value=document.querySelector('#since-seq').value.trim();location.hash=`/board/${encodeURIComponent(d.board.board_id)}/changes${value?`?since=${encodeURIComponent(value)}`:''}`});document.querySelector('#state').textContent=`Updated ${fmt(d.generated_at)}`;if(r.ticket){const target=[...document.querySelectorAll('[data-ticket]')].find(x=>x.dataset.ticket===r.ticket);target?.scrollIntoView({block:'center'})}}
async function fetchJson(path){const response=await fetch(path,{cache:'no-store'});if(!response.ok)throw new Error(`HTTP ${response.status}`);return response.json()}
async function refreshFleet(){try{fleetData=await fetchJson('/api/fleet');if(!route())renderFleet(fleetData)}catch(e){document.querySelector('#state').textContent=`Refresh failed: ${e.message}`}}
async function refreshOverhead(){try{renderOverhead(await fetchJson('/api/overhead'))}catch(e){document.querySelector('#overhead').innerHTML=`<p class="error">Overhead unavailable: ${esc(e.message)}</p>`}}
async function refreshDetail(){const r=route();if(!r)return;try{const data=await fetchJson(`/api/board/${encodeURIComponent(r.board)}`);if(route()?.board!==r.board)return;detailData=data;renderDetail(data)}catch(e){document.querySelector('#detail-view').innerHTML=`<a class="back" href="#/">← All boards</a><p class="error">Board detail unavailable: ${esc(e.message)}</p>`;document.querySelector('#state').textContent='Detail refresh failed'}}
function syncRoute(){const r=route();document.querySelector('#home-view').hidden=!!r;document.querySelector('#detail-view').hidden=!r;if(detailTimer){clearInterval(detailTimer);detailTimer=null}if(r){document.querySelector('#detail-view').innerHTML='<p class="empty">Loading board detail…</p>';refreshDetail();detailTimer=setInterval(refreshDetail,5000)}else if(fleetData)renderFleet(fleetData)}
document.querySelector('#filter').addEventListener('input',e=>{filterNeedle=e.target.value.toLocaleLowerCase();if(route()&&detailData)renderDetail(detailData);else if(fleetData)renderFleet(fleetData)});document.addEventListener('keydown',e=>{if(e.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)){e.preventDefault();document.querySelector('#filter').focus()}});window.addEventListener('hashchange',syncRoute);refreshFleet();refreshOverhead();setInterval(refreshFleet,5000);setInterval(refreshOverhead,5000);syncRoute();
</script></body></html>"""

# Keep the existing bounded fleet SPA intact; layer the one explicit write surface
# as an isolated hash page and API client.
HTML = HTML.replace(
    "</style>",
    ".config-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}"
    ".config-grid label{display:grid;gap:5px}.config-grid input,.config-grid select,.config-grid button{background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px}"
    ".source{font-size:11px;color:var(--muted)}</style>",
).replace(
    "Live boards and shared agent pool</p>",
    'Live boards and shared agent pool · <a href="#/config">Coordinator config</a></p>',
).replace(
    '<section id="detail-view" hidden></section></main>',
    '<section id="detail-view" hidden></section><section id="config-view" hidden></section></main>',
).replace(
    "</body>",
    r"""<script>
const CONFIG_CATEGORIES=['docs','tests','audit-analysis','bug','production-code','release-ci','membership-roles','board-registry'];
const CONFIG_NUMBERS=[['stale_seconds','Stale seconds',10,86400],['lease_warning_ratio','Lease warning ratio',.1,1],['grace_seconds','Grace seconds',10,86400],['starved_seconds','Starved seconds',10,86400],['critical_starved_seconds','Critical starved seconds',10,86400],['review_backlog_seconds','Review backlog seconds',10,86400],['abandoner_drops','Abandoner drops',1,20],['abandoner_window_days','Abandoner window days',1,365]];
let coordinatorConfig=null;
const sourceFor=(d,path)=>d.sources?.[path]||'unknown';
function configNumber(d,key,label,min,max){const v=d.effective.thresholds[key];return `<label>${esc(label)} <span class="source">source: ${esc(sourceFor(d,`thresholds.${key}`))}</span><input name="${esc(key)}" type="number" min="${min}" max="${max}" step="${key==='lease_warning_ratio'?'.01':'1'}" value="${esc(v)}" required></label>`}
function renderConfig(d){coordinatorConfig=d;const e=d.effective||{};if(!e.thresholds||!e.intake){document.querySelector('#config-view').innerHTML='<a class="back" href="#/">← All boards</a><p class="warning">Run the coordinator once to publish effective values before editing.</p>';return}document.querySelector('#config-view').innerHTML=`<a class="back" href="#/">← All boards</a><div class="top"><div><h2>Coordinator config</h2><p class="muted">One live policy document on the home board</p></div><div><span class="status">mode: ${esc(d.mode)}</span><p class="warning">Mode changes require a restart.</p></div></div><form id="config-form" class="card pool"><div class="config-grid">${CONFIG_NUMBERS.map(x=>configNumber(d,...x)).join('')}<label>Integration watch since <span class="source">source: ${esc(sourceFor(d,'integration_watch_since'))}</span><input name="integration_watch_since" type="text" placeholder="ISO-8601 or blank" value="${esc(e.integration_watch_since||'')}"></label><label>Intake enabled <span class="source">source: ${esc(sourceFor(d,'intake.enabled'))}</span><input name="enabled" type="checkbox" ${e.intake.enabled?'checked':''}></label><label>Work domain always ask <span class="source">source: ${esc(sourceFor(d,'intake.work_domain_always_ask'))}</span><input name="work_domain_always_ask" type="checkbox" ${e.intake.work_domain_always_ask?'checked':''}></label><label>Intake rate per hour <span class="source">source: ${esc(sourceFor(d,'intake.rate_per_hour'))}</span><input name="rate_per_hour" type="number" min="1" max="20" value="${esc(e.intake.rate_per_hour)}" required></label>${CONFIG_CATEGORIES.map(c=>`<label>${esc(c)} policy <span class="source">source: ${esc(sourceFor(d,e.intake.auto_categories.includes(c)?'intake.auto_categories':'intake.always_ask_categories'))}</span><select name="category_${esc(c)}"><option value="auto"${e.intake.auto_categories.includes(c)?' selected':''}>auto</option><option value="ask"${e.intake.always_ask_categories.includes(c)?' selected':''}>always ask</option></select></label>`).join('')}</div><div class="toolbar"><button type="submit">Save config</button><span id="config-status" class="muted">${esc(d.concurrency.toUpperCase())} · updated ${esc(fmt(d.updated_at))} by ${esc(d.updated_by||'—')}</span></div></form>`;document.querySelector('#config-form').addEventListener('submit',saveConfig)}
async function refreshConfig(){try{const r=await fetch('/api/config',{cache:'no-store'});if(!r.ok)throw new Error(`HTTP ${r.status}`);renderConfig(await r.json())}catch(e){document.querySelector('#config-view').innerHTML=`<a class="back" href="#/">← All boards</a><p class="error">Config unavailable: ${esc(e.message)}</p>`}}
async function saveConfig(event){event.preventDefault();const f=new FormData(event.target),thresholds={};for(const [key] of CONFIG_NUMBERS)thresholds[key]=key==='lease_warning_ratio'?Number(f.get(key)):Number.parseInt(f.get(key),10);const auto=[],always=[];for(const c of CONFIG_CATEGORIES)(f.get(`category_${c}`)==='auto'?auto:always).push(c);const config={schema_version:1,thresholds,integration_watch_since:f.get('integration_watch_since').trim()||null,intake:{enabled:f.get('enabled')==='on',auto_categories:auto,always_ask_categories:always,work_domain_always_ask:f.get('work_domain_always_ask')==='on',rate_per_hour:Number.parseInt(f.get('rate_per_hour'),10)}};const status=document.querySelector('#config-status');status.textContent='Saving…';try{const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config,expected_sha256:coordinatorConfig.expected_sha256})});const body=await r.json();if(!r.ok)throw new Error(body.error||`HTTP ${r.status}`);status.textContent=`Saved with ${body.concurrency.toUpperCase()}; waiting for coordinator poll`;setTimeout(refreshConfig,1000)}catch(e){status.textContent=`Save failed: ${e.message}`;status.className='error'}}
function syncConfigRoute(){const active=location.hash==='#/config';document.querySelector('#config-view').hidden=!active;if(active){document.querySelector('#home-view').hidden=true;document.querySelector('#detail-view').hidden=true;refreshConfig()}}
window.addEventListener('hashchange',syncConfigRoute);syncConfigRoute();
</script></body>""",
)


def make_handler(
    cache: DashboardCache, stats_path: str | Path | None = None
) -> type[BaseHTTPRequestHandler]:
    selected_stats_path = (
        bridge_stats_path() if stats_path is None else Path(stats_path)
    )

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
            if self.path == "/api/overhead":
                body = _json_bytes(read_overhead_stats(selected_stats_path))
                self._send(200, "application/json; charset=utf-8", body)
                return
            if self.path == "/api/config":
                try:
                    body = _json_bytes(cache.get_config())
                except Exception as exc:  # noqa: BLE001
                    body = _json_bytes({"error": type(exc).__name__})
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

        def do_POST(self) -> None:
            if self.path != "/api/config":
                self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if not 1 <= length <= 20_000:
                self._send(400, "application/json; charset=utf-8", b'{"error":"invalid body size"}')
                return
            try:
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict) or set(request) != {"config", "expected_sha256"}:
                    raise ValueError("request must contain only config and expected_sha256")
                body = _json_bytes(cache.save_config(request["config"], request["expected_sha256"]))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._send(400, "application/json; charset=utf-8", _json_bytes({"error": str(exc)}))
                return
            except Exception as exc:  # noqa: BLE001 - stale CAS is a safe conflict.
                self._send(409, "application/json; charset=utf-8", _json_bytes({"error": type(exc).__name__}))
                return
            self._send(200, "application/json; charset=utf-8", body)

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
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(cache, bridge_stats_path())
    )
    print(f"Fleet Dashboard: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
