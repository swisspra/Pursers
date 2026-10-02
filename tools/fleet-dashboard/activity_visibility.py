"""Bounded display projection for Central's additive ticket activity record."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MAX_EVIDENCE_REFS = 8
MAX_REF_CHARS = 256
STAGES = frozenset(
    {"intake", "queued", "work", "validation", "review", "integration", "delivery", "completed"}
)
STATES = frozenset(
    {"waiting", "running", "blocked", "retrying", "failed", "canceled", "stale", "unknown", "completed"}
)
FRESHNESS = frozenset({"fresh", "stale", "unknown"})
BOUNDARIES = frozenset(
    {"review", "pull_request", "integration", "delivery", "unknown"}
)


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split())
    return compact[:limit] or None


def _estimate(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    low = value.get("low_percent")
    high = value.get("high_percent")
    confidence = value.get("confidence")
    if (
        type(low) is not int
        or type(high) is not int
        or not 0 <= low <= high <= 99
        or confidence not in {"low", "medium", "high"}
    ):
        return None
    result: dict[str, Any] = {
        "low_percent": low,
        "high_percent": high,
        "confidence": confidence,
    }
    evidence = _text(value.get("evidence"), 280)
    assessed_at = _text(value.get("assessed_at"), 40)
    if evidence:
        result["evidence"] = evidence
    if assessed_at:
        result["assessed_at"] = assessed_at
    return result


def project_activity(value: Any) -> dict[str, Any] | None:
    """Allow-list one record; unknown/invalid servers use the legacy UI path."""
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        return None
    stage = value.get("stage")
    state = value.get("state")
    freshness = value.get("freshness")
    boundary = value.get("completion_boundary")
    if (
        stage not in STAGES
        or state not in STATES
        or freshness not in FRESHNESS
        or boundary not in BOUNDARIES
    ):
        return None
    attempt = value.get("attempt_id")
    if attempt is not None and (type(attempt) is not int or attempt < 1):
        return None
    raw_refs = value.get("evidence_refs")
    if not isinstance(raw_refs, list):
        return None
    refs: list[str] = []
    for item in raw_refs:
        ref = _text(item, MAX_REF_CHARS)
        if ref is not None and ref not in refs:
            refs.append(ref)
        if len(refs) >= MAX_EVIDENCE_REFS:
            break
    next_action = _text(value.get("next_action"), 280)
    if next_action is None:
        return None
    result: dict[str, Any] = {
        "schema_version": 1,
        "stage": stage,
        "state": state,
        "attempt_id": attempt,
        "actor_id": _text(value.get("actor_id"), 96),
        "updated_at": _text(value.get("updated_at"), 40),
        "freshness": freshness,
        "evidence_refs": refs,
        "next_action": next_action,
        "completion_boundary": boundary,
    }
    blocking_reason = _text(value.get("blocking_reason"), 280)
    if blocking_reason:
        result["blocking_reason"] = blocking_reason
    estimate = _estimate(value.get("estimate"))
    if estimate is not None:
        result["estimate"] = estimate
    return result
