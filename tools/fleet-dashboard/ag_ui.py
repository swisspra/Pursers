"""Finite, read-only AG-UI v1 observation over bounded Fleet snapshots."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


MAX_INPUT_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
MAX_TICKETS = 100
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_INPUT_KEYS = frozenset(
    {
        "threadId",
        "runId",
        "protocolVersion",
        "messages",
        "tools",
        "context",
        "state",
    }
)
_RESULT_STATES = frozenset({"missing", "pending", "approved", "rejected", "failed"})
_COVERAGE_STATES = frozenset(
    {"fresh", "stale", "unknown", "pending", "unavailable", "error"}
)
_COVERAGE_KEYS = frozenset(
    {"summary", "events", "human_requests", "handoffs", "ticket_enrichment"}
)
_ACTIVITY_VALUES = {
    "stage": frozenset(
        {
            "intake",
            "queued",
            "work",
            "validation",
            "review",
            "integration",
            "delivery",
            "completed",
        }
    ),
    "state": frozenset(
        {
            "waiting",
            "running",
            "blocked",
            "retrying",
            "failed",
            "canceled",
            "stale",
            "unknown",
            "completed",
        }
    ),
    "freshness": frozenset({"fresh", "stale", "unknown"}),
    "completion_boundary": frozenset(
        {"review", "pull_request", "integration", "delivery", "unknown"}
    ),
}


def validate_run_input(value: Any) -> tuple[str, str]:
    """Accept the empty, read-only RunAgentInput subset; never accept actions."""
    if not isinstance(value, Mapping) or set(value) - _INPUT_KEYS:
        raise ValueError("unsupported RunAgentInput fields")
    thread_id = value.get("threadId")
    run_id = value.get("runId")
    if not isinstance(thread_id, str) or not _ID_RE.fullmatch(thread_id):
        raise ValueError("invalid threadId")
    if not isinstance(run_id, str) or not _ID_RE.fullmatch(run_id):
        raise ValueError("invalid runId")
    if "protocolVersion" in value and value["protocolVersion"] != "1.0":
        raise ValueError("unsupported protocolVersion")
    if value.get("messages") != []:
        raise ValueError("messages must be empty for read-only observation")
    for key in ("tools", "context"):
        if key in value and value[key] != []:
            raise ValueError(f"{key} must be empty for read-only observation")
    if "state" in value and value["state"] != {}:
        raise ValueError("state must be empty for read-only observation")
    return thread_id, run_id


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split())
    return compact[:limit] or None


def _activity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    projected: dict[str, Any] = {}
    for key, allowed in _ACTIVITY_VALUES.items():
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            projected[key] = item
    if len(projected) != len(_ACTIVITY_VALUES):
        return None
    attempt = value.get("attempt_id")
    if attempt is not None and (type(attempt) is not int or attempt < 1):
        attempt = None
    refs = value.get("evidence_refs")
    evidence_refs = []
    if isinstance(refs, list):
        for ref in refs[:8]:
            safe = _text(ref, 256)
            if safe and safe not in evidence_refs:
                evidence_refs.append(safe)
    projected.update(
        {
            "schema_version": 1,
            "attempt_id": attempt,
            "actor_id": _text(value.get("actor_id"), 96),
            "updated_at": _text(value.get("updated_at"), 40),
            "evidence_refs": evidence_refs,
            "next_action": _text(value.get("next_action"), 280),
        }
    )
    for key in ("blocking_reason",):
        text = _text(value.get(key), 280)
        if text:
            projected[key] = text
    estimate = value.get("estimate")
    if isinstance(estimate, Mapping):
        low, high, confidence = (
            estimate.get("low_percent"),
            estimate.get("high_percent"),
            estimate.get("confidence"),
        )
        if (
            type(low) is int
            and type(high) is int
            and 0 <= low <= high <= 99
            and isinstance(confidence, str)
            and confidence in {"low", "medium", "high"}
        ):
            projected["estimate"] = {
                "low_percent": low,
                "high_percent": high,
                "confidence": confidence,
                "assessed_at": _text(estimate.get("assessed_at"), 40),
            }
    return projected


def _result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    state = value.get("state")
    review = value.get("review")
    review_value = None
    if isinstance(review, Mapping):
        verdict = review.get("verdict")
        independent = review.get("independent")
        review_value = {
            "verdict": verdict
            if isinstance(verdict, str) and verdict in {"approve", "reject"}
            else None,
            "independent": independent if type(independent) is bool else None,
        }
    return {
        "state": state
        if isinstance(state, str) and state in _RESULT_STATES
        else None,
        "review": review_value,
    }


def _delivery(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    state = _text(value.get("state"), 48)
    if state is None:
        return None
    pr_id = value.get("pr_id")
    return {
        "state": state,
        "pr_id": pr_id if type(pr_id) is int and pr_id > 0 else None,
    }


def _optional_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _coverage(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        key: state
        for key in _COVERAGE_KEYS
        if isinstance((state := value.get(key)), str)
        and state in _COVERAGE_STATES
    }


def _truncation_flags(value: Any) -> Any:
    if type(value) is bool:
        return value
    if not isinstance(value, Mapping):
        return None
    return {
        _text(key, 48): item
        for key, item in list(value.items())[:16]
        if _text(key, 48) is not None
        and (type(item) is bool or (type(item) is int and item >= 0))
    }


def project_observation(
    source: Any,
    *,
    central: str,
    board_id: str,
    cache_age_seconds: Any = None,
    cache_fresh: Any = None,
    cache_source: Any = None,
) -> dict[str, Any]:
    """Project an existing bounded board detail into a narrow public snapshot."""
    source = source if isinstance(source, Mapping) else {}
    board = source.get("board")
    board = board if isinstance(board, Mapping) else {}
    raw_tickets = source.get("tickets")
    raw_tickets = raw_tickets if isinstance(raw_tickets, list) else []
    tickets = []
    activities: list[dict[str, Any]] = []
    for row in raw_tickets[:MAX_TICKETS]:
        if not isinstance(row, Mapping):
            continue
        ticket_id = _text(row.get("id"), 96)
        if ticket_id is None:
            continue
        activity = _activity(row.get("activity"))
        summary = {
            "id": ticket_id,
            "title": _text(row.get("title"), 160),
            "status": _text(row.get("status"), 32),
            "status_label": _text(row.get("status_label"), 64),
            "priority": _text(row.get("priority"), 16),
            "progress_freshness": (
                row.get("progress_freshness")
                if isinstance(row.get("progress_freshness"), str)
                and row.get("progress_freshness") in {"fresh", "stale", "unknown"}
                else None
            ),
            "activity": activity,
            "result": _result(row.get("result")),
            "delivery": _delivery(row.get("delivery")),
        }
        tickets.append(summary)
        if activity is not None:
            activities.append({"ticket_id": ticket_id, "activity": activity})

    coverage = {
        "ticket_total": _optional_count(source.get("ticket_total")),
        "ticket_returned": _optional_count(source.get("ticket_returned")),
        "ticket_omitted": _optional_count(source.get("ticket_omitted")),
        "truncated": (
            source.get("truncated")
            if type(source.get("truncated")) is bool
            else None
        ),
        "snapshot_truncation": _truncation_flags(source.get("snapshot_truncation")),
        "event_window_truncated": (
            source.get("event_window_truncated")
            if type(source.get("event_window_truncated")) is bool
            else None
        ),
        "event_resync_required": (
            source.get("event_resync_required")
            if type(source.get("event_resync_required")) is bool
            else None
        ),
        "coordinator_findings_stale": (
            source.get("coordinator_findings_stale")
            if type(source.get("coordinator_findings_stale")) is bool
            else None
        ),
        "source": _coverage(source.get("source_coverage")),
    }
    raw_total = coverage["ticket_total"]
    raw_returned = coverage["ticket_returned"]
    if raw_total is not None and raw_returned is not None:
        coverage["ticket_omitted"] = max(
            coverage["ticket_omitted"] or 0,
            max(0, raw_total - raw_returned)
            + max(0, raw_returned - len(tickets)),
        )
    return {
        "scope": {"central": central, "board_id": board_id},
        "cache": {
            "age_seconds": (
                max(0, int(cache_age_seconds))
                if type(cache_age_seconds) in {int, float}
                else None
            ),
            "fresh": cache_fresh if type(cache_fresh) is bool else None,
            "source": (
                cache_source
                if isinstance(cache_source, str)
                and cache_source in {"board_detail", "fleet_summary"}
                else None
            ),
        },
        "board": {
            "id": _text(board.get("board_id"), 80) or board_id,
            "label": _text(board.get("label"), 96),
        },
        "coverage": coverage,
        "tickets": tickets,
        "activities": activities,
    }


def capabilities() -> dict[str, Any]:
    """Application capabilities; unsupported execution paths stay explicit."""
    return {
        "protocolVersion": "1.0",
        "transport": "http-sse",
        "observe": True,
        "run": False,
        "resume": False,
        "mutations": False,
        "commands": False,
        "limitations": ["finite cached snapshots only", "live journal streaming deferred"],
    }


def observation_events(
    state: dict[str, Any], *, thread_id: str, run_id: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {
            "type": "RUN_STARTED",
            "threadId": thread_id,
            "runId": run_id,
            "protocolVersion": "1.0",
        },
        {
            "type": "STATE_SNAPSHOT",
            "snapshot": {
                key: value for key, value in state.items() if key != "activities"
            },
        },
    ]
    activities = state.get("activities", [])
    if activities:
        events.append(
            {
                "type": "ACTIVITY_SNAPSHOT",
                "messageId": f"{run_id}:activity",
                "activityType": "pursers.board.activity",
                "content": {"scope": state["scope"], "activities": activities},
            }
        )
    events.append({"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id})
    return events


def encode_sse(events: list[dict[str, Any]]) -> bytes:
    """Encode complete AG-UI events as bounded data-only SSE frames."""
    frames = []
    for event in events:
        data = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
        data = (
            data.replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        frames.append(f"data: {data}\n\n")
    encoded = "".join(frames).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("observation exceeds byte cap")
    return encoded
