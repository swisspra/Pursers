#!/usr/bin/env python3
"""Fleet coordinator with shadow-default phase-2 dispatch and nudge policy."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


STATE_KEY = "coordinator_findings"
SCHEMA_VERSION = 2
DEFAULT_URL = "https://127.0.0.1:8766/mcp"
MAX_SNAPSHOT_ITEMS = 1_000
MAX_SNAPSHOT_BYTES = 750_000
MAX_FINDINGS = 50
MAX_FINDING_CHARS = 500
MAX_EVIDENCE_CHARS = 300
MAX_STATE_CHARS = 5_000
MAX_PRIVACY_COMMITS_PER_CYCLE = 1_000
COMMIT_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{7,64})(?![0-9a-fA-F])")
CLAIMED_STATES = frozenset({"claimed", "in_progress", "creating_report"})
SUBMITTED_STATES = frozenset({"submitted"})
BOARD_DEGRADED_POLLS = 3
COORDINATOR_NAME = "coordinator-1"
ASSIGN_RATE_SECONDS = 600
NUDGE_RATE_SECONDS = 3_600
MAX_NUDGES_PER_SEAT = 3
NUDGE_EXPIRY_SECONDS = 600


@dataclass(frozen=True)
class Thresholds:
    stale_seconds: int = 300
    lease_warning_fraction: float = 0.80
    lease_grace_seconds: int = 600
    starved_seconds: int = 1_800
    critical_starved_seconds: int = 600
    repeat_abandon_count: int = 3
    repeat_abandon_window_seconds: int = 7 * 86_400
    review_backlog_seconds: int = 1_800


@dataclass(frozen=True)
class Project:
    name: str
    board_id: str
    work_dir: Path
    integration_ref: str = "main"
    public: bool = False


@dataclass(frozen=True)
class Action:
    kind: str
    board_id: str
    ticket_id: str
    target_agent_id: str
    target_agent_name: str
    stage: int
    threshold_seconds: int
    threshold_window: int
    op_key: str
    reason: str


@dataclass
class RuntimeState:
    requested_mode: str
    effective_mode: str
    consecutive_failures: int = 0

    @classmethod
    def for_mode(cls, mode: str) -> "RuntimeState":
        return cls(requested_mode=mode, effective_mode=mode)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_seconds(value: Any, now: datetime) -> float | None:
    parsed = parse_time(value)
    return None if parsed is None else max(0.0, (now - parsed).total_seconds())


def parse_registry(raw: Mapping[str, Any]) -> list[Project]:
    state = raw.get("state")
    if not isinstance(state, Mapping):
        raise ValueError("project_registry state is missing")
    value = state.get("value")
    try:
        document = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise ValueError("project_registry is malformed") from exc
    if not isinstance(document, Mapping) or document.get("schema_version") != 1:
        raise ValueError("project_registry schema is unsupported")
    rows = document.get("projects")
    if not isinstance(rows, Mapping):
        raise ValueError("project_registry projects are missing")
    projects: list[Project] = []
    for name, row in rows.items():
        if not isinstance(name, str) or not isinstance(row, Mapping):
            raise ValueError("project_registry project entry is invalid")
        if row.get("status") != "active":
            continue
        board_id, work_dir = row.get("board_id"), row.get("work_dir")
        integration_ref = row.get("integration_ref", "main")
        if not all(isinstance(item, str) and item.strip() for item in (board_id, work_dir, integration_ref)):
            raise ValueError("active project routing is incomplete")
        path = Path(work_dir)
        if not path.is_absolute():
            raise ValueError("active project work_dir must be absolute")
        projects.append(
            Project(
                name=name,
                board_id=board_id,
                work_dir=path,
                integration_ref=integration_ref,
                public=row.get("public") is True,
            )
        )
    return sorted(projects, key=lambda item: (item.board_id, item.name))


def classify_agent(agent: Mapping[str, Any], now: datetime, thresholds: Thresholds) -> str:
    if agent.get("lifecycle_status", "active") != "active":
        return "inactive"
    age = age_seconds(agent.get("last_activity_at") or agent.get("last_seen"), now)
    if age is None or age > thresholds.stale_seconds:
        return "stale"
    if agent.get("status") in {"working", "busy"} or agent.get("lease_expires_at"):
        return "busy"
    return "available"


def classify_lease(
    ticket: Mapping[str, Any], claim_ttl_s: int, now: datetime, thresholds: Thresholds
) -> str:
    expiry = parse_time(ticket.get("lease_expires_at") or ticket.get("last_lease_expires_at"))
    if expiry is None:
        return "unknown"
    seconds_after_expiry = (now - expiry).total_seconds()
    if seconds_after_expiry >= thresholds.lease_grace_seconds:
        return "abandoned"
    if seconds_after_expiry >= 0:
        return "expired"
    remaining = (expiry - now).total_seconds()
    consumed_fraction = (claim_ttl_s - remaining) / claim_ttl_s
    if consumed_fraction >= thresholds.lease_warning_fraction:
        return "at-risk"
    return "healthy"


def starvation_stage(ticket: Mapping[str, Any], now: datetime, thresholds: Thresholds) -> int:
    if (
        ticket.get("status") != "open"
        or ticket.get("claimed_by_agent_id")
        or ticket.get("assigned_to_agent_id")
        or ticket.get("assigned_to")
    ):
        return 0
    age = age_seconds(ticket.get("created_at"), now)
    if age is None:
        return 0
    threshold = (
        thresholds.critical_starved_seconds
        if ticket.get("priority") == "critical"
        else thresholds.starved_seconds
    )
    if age >= 2 * threshold:
        return 2
    return 1 if age >= threshold else 0


def _agent_loads(tickets: Sequence[Mapping[str, Any]]) -> Counter[str]:
    return Counter(
        str(ticket["claimed_by_agent_id"])
        for ticket in tickets
        if ticket.get("status") in CLAIMED_STATES and ticket.get("claimed_by_agent_id")
    )


def choose_assignee(
    agents: Sequence[Mapping[str, Any]],
    tickets: Sequence[Mapping[str, Any]],
    now: datetime,
    thresholds: Thresholds,
    repeat_abandoners: frozenset[str] = frozenset(),
) -> Mapping[str, Any] | None:
    loads = _agent_loads(tickets)
    eligible = [
        agent
        for agent in agents
        if classify_agent(agent, now, thresholds) == "available"
        and agent.get("agent_name") != COORDINATOR_NAME
        and agent.get("agent_id")
        and agent.get("membership_role") in {"member", "admin"}
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (
            str(item["agent_id"]) in repeat_abandoners,
            loads[str(item["agent_id"])],
            str(item.get("last_activity_at", "")),
            str(item["agent_id"]),
        ),
    )


def eligible_agents(
    agents: Sequence[Mapping[str, Any]],
    tickets: Sequence[Mapping[str, Any]],
    now: datetime,
    thresholds: Thresholds,
    repeat_abandoners: frozenset[str] = frozenset(),
) -> list[Mapping[str, Any]]:
    loads = _agent_loads(tickets)
    return sorted(
        (
            agent
            for agent in agents
            if classify_agent(agent, now, thresholds) == "available"
            and agent.get("agent_name") != COORDINATOR_NAME
            and agent.get("agent_id")
            and agent.get("membership_role") in {"member", "admin"}
        ),
        key=lambda item: (
            str(item["agent_id"]) in repeat_abandoners,
            loads[str(item["agent_id"])],
            str(item.get("last_activity_at", "")),
            str(item["agent_id"]),
        ),
    )


def _finding_evidence(board_id: str, values: Mapping[str, Any]) -> str:
    parts = [f"board_id={board_id}"]
    for key, value in values.items():
        if value is None or key in {"error", "evidence", "next_action"}:
            continue
        rendered = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if isinstance(value, (Mapping, list, tuple))
            else str(value)
        )
        candidate = "; ".join([*parts, f"{key}={rendered}"])
        if len(candidate) > MAX_EVIDENCE_CHARS:
            remaining = MAX_EVIDENCE_CHARS - len("; ".join(parts)) - len(key) - 4
            if remaining > 0:
                parts.append(f"{key}={rendered[:remaining]}…")
            break
        parts.append(f"{key}={rendered}")
    return "; ".join(parts)[:MAX_EVIDENCE_CHARS]


def _finding_next_action(
    kind: str, board_id: str, values: Mapping[str, Any]
) -> str:
    ticket_id = str(values.get("ticket_id") or "the affected ticket")
    actions = {
        "starved": f"Review {ticket_id} on {board_id}; claim it or assign an eligible worker.",
        "claim-health": f"Review {ticket_id} on {board_id}; renew or release the stale claim safely.",
        "snapshot-truncated": f"Inspect the bounded snapshot for {board_id} before acting on incomplete counts.",
        "repeat-abandoner": f"Review the named seat on {board_id} before assigning more work to it.",
        "repeat-abandoner-history-incomplete": f"Wait for a complete observation window on {board_id} before penalizing a seat.",
        "closed-but-unmerged": f"Review {ticket_id} and integrate its submitted commit into the configured ref.",
        "unverifiable-commit": f"Fetch or restore the submitted commit for {ticket_id}, then rerun the integration check.",
        "integration-check-unavailable": f"Restore the repository ref check for {board_id}, then rerun the coordinator.",
        "privacy-scan-unavailable": f"Restore the local diff scan for {board_id} before publishing the affected commit.",
        "privacy-leak-suspect": f"Review the flagged commit on {board_id} against the privacy policy before publishing.",
        "privacy-scan-truncated": f"Run another bounded privacy scan cycle for {board_id} before declaring coverage complete.",
        "review-backlog": f"Review {ticket_id} on {board_id} with an available reviewer seat.",
        "board-degraded": f"Restore a complete snapshot read for {board_id}, then confirm one healthy poll.",
        "would_nudge": f"Review the proposed nudge for {ticket_id} before enabling active mode.",
        "would_assign": f"Review the proposed assignment for {ticket_id} before enabling active mode.",
        "nudge": f"Verify the nudged seat acknowledges {ticket_id} on {board_id}.",
        "assign": f"Verify the assigned seat claims {ticket_id} on {board_id}.",
        "mutation_failed": f"Review the failed coordinator mutation for {ticket_id} before retrying.",
        "coordinator_circuit_open": f"Resolve the coordinator mutation failures on {board_id} before restoring active mode.",
    }
    return actions.get(
        kind,
        f"Review the {kind.replace('_', ' ')} finding on {board_id} before taking action.",
    )


def _finding(
    kind: str, level: str, board_id: str, message: str, **details: Any
) -> dict[str, Any]:
    result = {
        "kind": kind,
        "level": level,
        "board_id": board_id,
        "message": message,
        "evidence": _finding_evidence(board_id, details),
        "next_action": _finding_next_action(kind, board_id, details),
    }
    result.update({key: value for key, value in details.items() if value is not None})
    return result


def ticket_findings(
    board_id: str,
    snapshot: Mapping[str, Any],
    now: datetime,
    thresholds: Thresholds = Thresholds(),
    repeat_abandoners: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    agents = [row for row in snapshot.get("agents", []) if isinstance(row, Mapping)]
    tickets = [row for row in snapshot.get("tickets", []) if isinstance(row, Mapping)]
    claim_ttl_s = snapshot.get("board", {}).get("claim_ttl_s", 900)
    if not isinstance(claim_ttl_s, int) or isinstance(claim_ttl_s, bool) or claim_ttl_s <= 0:
        claim_ttl_s = 900
    findings: list[dict[str, Any]] = []
    reviewer_seats = sorted(
        {
            str(agent.get("agent_name"))
            for agent in agents
            if agent.get("agent_name")
            and (
                agent.get("role") == "reviewer"
                or agent.get("membership_role") == "reviewer"
            )
        }
    )[:10]

    for ticket in tickets:
        ticket_id = str(ticket.get("ticket_id", "unknown"))
        stage = starvation_stage(ticket, now, thresholds)
        if stage:
            observed_age = int(age_seconds(ticket.get("created_at"), now) or 0)
            threshold = (
                thresholds.critical_starved_seconds
                if ticket.get("priority") == "critical"
                else thresholds.starved_seconds
            )
            assignee = (
                choose_assignee(
                    agents, tickets, now, thresholds, repeat_abandoners
                )
                if stage == 2
                else None
            )
            findings.append(
                _finding(
                    "starved",
                    "warn",
                    board_id,
                    "An open ticket exceeded its claim-time threshold.",
                    ticket_id=ticket_id,
                    escalation_stage=stage,
                    observed_age_seconds=observed_age,
                    threshold_seconds=threshold,
                    would_assign_to_agent_id=assignee.get("agent_id") if assignee else None,
                    would_assign_to_agent_name=assignee.get("agent_name") if assignee else None,
                )
                )

        if ticket.get("status") in SUBMITTED_STATES:
            submitted_age = age_seconds(ticket.get("submitted_at"), now)
            if (
                submitted_age is not None
                and submitted_age >= thresholds.review_backlog_seconds
            ):
                findings.append(
                    _finding(
                        "review-backlog",
                        (
                            "critical"
                            if submitted_age >= 2 * thresholds.review_backlog_seconds
                            else "warn"
                        ),
                        board_id,
                        "A submitted ticket exceeded the review backlog threshold.",
                        ticket_id=ticket_id,
                        submitted_at=ticket.get("submitted_at"),
                        observed_age_seconds=int(submitted_age),
                        threshold_seconds=thresholds.review_backlog_seconds,
                        reviewer_seats=reviewer_seats,
                    )
                )

        if ticket.get("status") in CLAIMED_STATES:
            lease_state = classify_lease(ticket, claim_ttl_s, now, thresholds)
            holder = next(
                (agent for agent in agents if agent.get("agent_id") == ticket.get("claimed_by_agent_id")),
                None,
            )
            holder_state = classify_agent(holder, now, thresholds) if holder else "missing"
            if lease_state != "healthy" or holder_state in {"stale", "missing", "inactive"}:
                level = "critical" if lease_state == "abandoned" else "warn"
                findings.append(
                    _finding(
                        "claim-health",
                        level,
                        board_id,
                        "A claimed ticket needs lease or holder attention.",
                        ticket_id=ticket_id,
                        lease_state=lease_state,
                        holder_state=holder_state,
                    )
                )

    omitted = snapshot.get("omitted_counts") or snapshot.get("truncation_counts")
    if isinstance(omitted, Mapping) and any(isinstance(value, int) and value > 0 for value in omitted.values()):
        findings.append(
            _finding(
                "snapshot-truncated",
                "warn",
                board_id,
                "The bounded snapshot omitted records; findings are a lower bound.",
                omitted={key: value for key, value in omitted.items() if isinstance(value, int) and value > 0},
            )
        )
    return findings


def board_degradation(
    snapshot: Mapping[str, Any],
) -> tuple[bool, str | None, str | None]:
    error_class = snapshot.get("snapshot_error_class")
    if isinstance(error_class, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,79}", error_class):
        return True, "snapshot-failed", error_class
    omitted = snapshot.get("omitted_counts") or snapshot.get("truncation_counts")
    truncated = snapshot.get("truncated") is True or (
        isinstance(omitted, Mapping)
        and any(
            isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
            for value in omitted.values()
        )
    )
    return (True, "snapshot-truncated", None) if truncated else (False, None, None)


def update_drop_evidence(
    board_id: str,
    tickets: Sequence[Mapping[str, Any]],
    previous: Mapping[str, Any],
    now: datetime,
    thresholds: Thresholds = Thresholds(),
) -> tuple[
    list[dict[str, Any]],
    dict[str, int],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Build a bounded seven-day delta history from durable abandon counters.

    A first observation is only a baseline: a cumulative counter plus one latest
    timestamp cannot prove when or by whom every older drop happened.  Later
    single-step increases are exact observations. Unknown baselines and
    multi-step increases remain explicit uncertainty for a full seven-day
    coverage window; they are never attributed to the latest holder.
    """

    raw_counters = previous.get("drop_counters", {})
    old_counters = (
        {
            str(key): value
            for key, value in raw_counters.items()
            if isinstance(key, str)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        }
        if isinstance(raw_counters, Mapping)
        else {}
    )
    raw_history = previous.get("drop_history", [])
    history: list[dict[str, Any]] = []
    if isinstance(raw_history, list):
        for entry in raw_history:
            if not isinstance(entry, Mapping):
                continue
            entry_age = age_seconds(entry.get("observed_at"), now)
            count = entry.get("count")
            if (
                entry_age is not None
                and entry_age <= thresholds.repeat_abandon_window_seconds
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count > 0
                and entry.get("holder_agent_id")
            ):
                history.append(
                    {
                        "ticket_id": str(entry.get("ticket_id", "unknown")),
                        "holder_agent_id": str(entry["holder_agent_id"]),
                        "observed_at": str(entry["observed_at"]),
                        "count": count,
                    }
                )

    raw_uncertainty = previous.get("drop_uncertainty", [])
    uncertainty: list[dict[str, Any]] = []
    if isinstance(raw_uncertainty, list):
        for entry in raw_uncertainty:
            if not isinstance(entry, Mapping):
                continue
            entry_age = age_seconds(entry.get("observed_at"), now)
            count = entry.get("count")
            if (
                entry_age is not None
                and entry_age <= thresholds.repeat_abandon_window_seconds
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count > 0
            ):
                uncertainty.append(
                    {
                        "ticket_id": str(entry.get("ticket_id", "unknown")),
                        "observed_at": str(entry["observed_at"]),
                        "count": count,
                    }
                )

    findings: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    for ticket in tickets:
        ticket_id = ticket.get("ticket_id")
        current = ticket.get("abandoned_count", 0)
        if (
            not isinstance(ticket_id, str)
            or not isinstance(current, int)
            or isinstance(current, bool)
            or current < 0
        ):
            continue
        counters[ticket_id] = current
        prior = old_counters.get(ticket_id)
        drop_at = ticket.get("last_abandoned_at")
        holder = ticket.get("last_abandoned_by")
        drop_age = age_seconds(drop_at, now)
        is_recent = (
            drop_age is not None
            and drop_age <= thresholds.repeat_abandon_window_seconds
        )
        latest_is_known_outside_window = (
            drop_age is not None
            and drop_age > thresholds.repeat_abandon_window_seconds
        )
        if prior is None and latest_is_known_outside_window:
            # The latest cumulative drop is already outside the window, so all
            # earlier drops are outside it too. Keep only the counter baseline.
            delta = 0
        else:
            delta = current if prior is None else max(0, current - prior)
        if delta:
            proven_latest = bool(holder and is_recent)
            if proven_latest:
                history.append(
                    {
                        "ticket_id": ticket_id,
                        "holder_agent_id": str(holder),
                        "observed_at": str(drop_at),
                        "count": 1,
                    }
                )
            unattributed = delta - int(proven_latest)
            if unattributed:
                uncertainty.append(
                    {
                        "ticket_id": ticket_id,
                        "observed_at": now.isoformat(),
                        "count": unattributed,
                    }
                )

    # Deduplicate stable observations across cycles, then classify exact counts.
    unique = {
        (
            item["ticket_id"],
            item["holder_agent_id"],
            item["observed_at"],
            item["count"],
        ): item
        for item in history
    }
    history = sorted(
        unique.values(),
        key=lambda item: (item["observed_at"], item["ticket_id"], item["holder_agent_id"]),
    )
    uncertainty = sorted(
        {
            (item["ticket_id"], item["observed_at"], item["count"]): item
            for item in uncertainty
        }.values(),
        key=lambda item: (item["observed_at"], item["ticket_id"]),
    )
    counts = Counter()
    for entry in history:
        counts[entry["holder_agent_id"]] += entry["count"]
    for holder, count in sorted(counts.items()):
        if count >= thresholds.repeat_abandon_count:
            findings.append(
                _finding(
                    "repeat-abandoner",
                    "warn",
                    board_id,
                    "A seat reached the repeated dropped-claim threshold within the proven reporting window.",
                    holder_agent_id=holder,
                    dropped_claims=count,
                    window_days=7,
                )
            )

    unknown_count = sum(entry["count"] for entry in uncertainty)
    max_proven_for_one_seat = max(counts.values(), default=0)
    possible = unknown_count + max_proven_for_one_seat
    if unknown_count and possible >= thresholds.repeat_abandon_count:
        findings.append(
            _finding(
                "repeat-abandoner-history-incomplete",
                "warn",
                board_id,
                "Unattributed drops combined with board-level seat evidence could reach the repeat threshold; keep the limitation until seven-day coverage is complete.",
                unattributed_dropped_claims=unknown_count,
                max_proven_for_one_seat=max_proven_for_one_seat,
                possible_dropped_claims=possible,
                observation_window_days=7,
            )
        )
    return findings, counters, history, uncertainty


def repeat_abandoner_ids(
    history: Sequence[Mapping[str, Any]], thresholds: Thresholds
) -> frozenset[str]:
    counts: Counter[str] = Counter()
    for item in history:
        holder = item.get("holder_agent_id")
        count = item.get("count")
        if holder and isinstance(count, int) and not isinstance(count, bool):
            counts[str(holder)] += count
    return frozenset(
        holder
        for holder, count in counts.items()
        if count >= thresholds.repeat_abandon_count
    )


def _ticket_threshold(ticket: Mapping[str, Any], thresholds: Thresholds) -> int:
    return (
        thresholds.critical_starved_seconds
        if ticket.get("priority") == "critical"
        else thresholds.starved_seconds
    )


def action_op_key(
    board_id: str,
    ticket_id: str,
    kind: str,
    stage: int,
    threshold_window: int,
    target_agent_id: str,
) -> str:
    material = json.dumps(
        [board_id, ticket_id, kind, stage, threshold_window, target_agent_id],
        separators=(",", ":"),
    )
    return "coord-v1-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _recent_action_history(
    previous: Mapping[str, Mapping[str, Any]], now: datetime
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for board_id, state in previous.items():
        rows = state.get("action_history", [])
        kept: list[dict[str, Any]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                age = age_seconds(row.get("performed_at"), now)
                if age is not None and age <= NUDGE_RATE_SECONDS:
                    kept.append(dict(row))
        result[board_id] = kept
    return result


def _history_incomplete_until(
    state: Mapping[str, Any], now: datetime
) -> str | None:
    value = state.get("action_history_incomplete_until")
    parsed = parse_time(value)
    return str(value) if parsed is not None and parsed > now else None


def plan_actions(
    snapshots: Mapping[str, Mapping[str, Any]],
    states: Mapping[str, Mapping[str, Any]],
    previous: Mapping[str, Mapping[str, Any]],
    now: datetime,
    thresholds: Thresholds = Thresholds(),
) -> list[Action]:
    """Plan deterministic fleet-fair actions from current live projections."""
    history = _recent_action_history(previous, now)
    ranked: list[tuple[int, datetime, str, Mapping[str, Any]]] = []
    eligible_by_board: dict[str, list[Mapping[str, Any]]] = {}
    for board_id, snapshot in snapshots.items():
        omitted = snapshot.get("omitted_counts") or snapshot.get("truncation_counts")
        history_incomplete = _history_incomplete_until(
            previous.get(board_id, {}), now
        )
        omitted_agents = omitted.get("agents", 0) if isinstance(omitted, Mapping) else 0
        omitted_tickets = omitted.get("tickets", 0) if isinstance(omitted, Mapping) else 0
        unsafe_snapshot = (
            bool(omitted_agents)
            or (
                bool(omitted_tickets)
                and snapshot.get("coordination_tickets_complete") is not True
            )
        )
        if unsafe_snapshot or history_incomplete:
            eligible_by_board[board_id] = []
            continue
        agents = [row for row in snapshot.get("agents", []) if isinstance(row, Mapping)]
        tickets = [
            row
            for row in snapshot.get(
                "coordination_tickets", snapshot.get("tickets", [])
            )
            if isinstance(row, Mapping)
        ]
        repeated = repeat_abandoner_ids(
            [
                row
                for row in states.get(board_id, {}).get("drop_history", [])
                if isinstance(row, Mapping)
            ],
            thresholds,
        )
        eligible_by_board[board_id] = eligible_agents(
            agents, tickets, now, thresholds, repeated
        )
        for ticket in tickets:
            if starvation_stage(ticket, now, thresholds):
                ranked.append(
                    (
                        0 if ticket.get("priority") == "critical" else 1,
                        parse_time(ticket.get("created_at")) or now,
                        board_id,
                        ticket,
                    )
                )

    actions: list[Action] = []
    assignment_planned: set[str] = set()
    planned_nudges: Counter[str] = Counter()
    for _priority, _created, board_id, ticket in sorted(
        ranked, key=lambda row: (row[0], row[1], row[2], str(row[3].get("ticket_id", "")))
    ):
        stage = starvation_stage(ticket, now, thresholds)
        threshold = _ticket_threshold(ticket, thresholds)
        age = age_seconds(ticket.get("created_at"), now) or 0
        window = max(1, int(age // threshold))
        ticket_id = str(ticket.get("ticket_id"))
        eligible = eligible_by_board.get(board_id, [])
        if stage == 2:
            recent_assign = any(
                row.get("kind") == "assign"
                and (age_seconds(row.get("performed_at"), now) or 0) < ASSIGN_RATE_SECONDS
                for row in history.get(board_id, [])
            )
            if eligible and board_id not in assignment_planned and not recent_assign:
                target = eligible[0]
                target_id = str(target["agent_id"])
                actions.append(
                    Action(
                        "assign",
                        board_id,
                        ticket_id,
                        target_id,
                        str(target.get("agent_name", target_id)),
                        stage,
                        threshold,
                        window,
                        action_op_key(
                            board_id, ticket_id, "assign", stage, window, target_id
                        ),
                        "Oldest fleet-fair starved ticket reached twice its threshold.",
                    )
                )
                assignment_planned.add(board_id)
            continue
        if stage != 1:
            continue
        for target in eligible:
            target_id = str(target["agent_id"])
            recent_count = sum(
                1
                for row in history.get(board_id, [])
                if row.get("kind") == "nudge"
                and row.get("target_agent_id") == target_id
                and (age_seconds(row.get("performed_at"), now) or 0) < NUDGE_RATE_SECONDS
            )
            if recent_count + planned_nudges[f"{board_id}\x00{target_id}"] >= MAX_NUDGES_PER_SEAT:
                continue
            actions.append(
                Action(
                    "nudge",
                    board_id,
                    ticket_id,
                    target_id,
                    str(target.get("agent_name", target_id)),
                    stage,
                    threshold,
                    window,
                    action_op_key(
                        board_id, ticket_id, "nudge", stage, window, target_id
                    ),
                    "Open ticket reached its starvation threshold; wake an idle eligible seat.",
                )
            )
            planned_nudges[f"{board_id}\x00{target_id}"] += 1
    return actions


def action_finding(action: Action, kind: str, mode: str, **extra: Any) -> dict[str, Any]:
    return _finding(
        kind,
        "info" if kind.startswith("would_") or kind in {"nudge", "assign"} else "warn",
        action.board_id,
        action.reason,
        ticket_id=action.ticket_id,
        target_agent_id=action.target_agent_id,
        target_agent_name=action.target_agent_name,
        escalation_stage=action.stage,
        threshold_seconds=action.threshold_seconds,
        threshold_window=action.threshold_window,
        coordinator_op_key=action.op_key,
        mode=mode,
        **extra,
    )


def _git(
    work_dir: Path,
    args: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(
        ["git", *args],
        cwd=work_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def commit_is_ancestor(
    work_dir: Path,
    commit: str,
    integration_ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool | None:
    status = commit_integration_status(
        work_dir, commit, integration_ref, runner=runner
    )
    return True if status == "ancestor" else False if status == "not-ancestor" else None


def commit_integration_status(
    work_dir: Path,
    commit: str,
    integration_ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    if not COMMIT_RE.fullmatch(commit) or not integration_ref.strip() or integration_ref.startswith("-"):
        return "unavailable"
    if _git(work_dir, ["rev-parse", "--verify", f"{integration_ref}^{{commit}}"], runner=runner).returncode:
        return "unavailable"
    if _git(work_dir, ["cat-file", "-e", f"{commit}^{{commit}}"], runner=runner).returncode:
        return "missing-commit"
    result = _git(work_dir, ["merge-base", "--is-ancestor", commit, integration_ref], runner=runner)
    if result.returncode == 0:
        return "ancestor"
    if result.returncode == 1:
        return "not-ancestor"
    return "unavailable"


def _review_has_no_merge_label(ticket: Mapping[str, Any]) -> bool:
    if ticket.get("review_label") == "no-merge-needed":
        return True
    histories = ticket.get("review_history", [])
    if not isinstance(histories, list):
        return False
    for review in histories:
        if not isinstance(review, Mapping):
            continue
        if review.get("review_label") == "no-merge-needed":
            return True
        labels = review.get("labels", review.get("review_labels", []))
        if isinstance(labels, str):
            labels = [labels]
        if isinstance(labels, list) and "no-merge-needed" in labels:
            return True
    return False


def extract_commit_hash(ticket: Mapping[str, Any]) -> str | None:
    histories = ticket.get("submission_history")
    latest = histories[-1] if isinstance(histories, list) and histories else ticket
    if not isinstance(latest, Mapping):
        return None
    direct = latest.get("commit_hash")
    if isinstance(direct, str) and COMMIT_RE.fullmatch(direct.strip()):
        return direct.strip().lower()
    matches: list[str] = []
    for key in ("notes", "summary"):
        value = latest.get(key)
        if isinstance(value, str):
            for line in value.splitlines():
                for match in COMMIT_RE.finditer(line):
                    prefix = line[: match.start()].casefold()
                    labeled = re.search(r"(?:commit(?:_hash)?|sha(?:256)?)\s*[:=]\s*$", prefix)
                    if len(match.group(1)) >= 40 or labeled:
                        matches.append(match.group(1).lower())
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _ticket_closed_at(ticket: Mapping[str, Any]) -> datetime | None:
    for key in ("closed_at", "reviewed_at", "updated_at"):
        parsed = parse_time(ticket.get(key))
        if parsed is not None:
            return parsed
    return None


def evaluate_integration_watch(
    project: Project,
    tickets: Sequence[Mapping[str, Any]],
    integration_watch_since: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    findings: list[dict[str, Any]] = []
    suppressed_pre_watermark = 0
    for ticket in tickets:
        if ticket.get("status") != "closed" or _review_has_no_merge_label(ticket):
            continue
        target = ticket.get("target_url")
        if isinstance(target, str) and "/" in target and target.split("/", 1)[0] != project.name:
            continue
        commit = extract_commit_hash(ticket)
        if commit is None:
            continue
        closed_at = _ticket_closed_at(ticket)
        if (
            integration_watch_since is not None
            and closed_at is not None
            and closed_at < integration_watch_since
        ):
            suppressed_pre_watermark += 1
            continue
        status = commit_integration_status(
            project.work_dir, commit, project.integration_ref
        )
        if status == "not-ancestor":
            findings.append(
                _finding(
                    "closed-but-unmerged",
                    "warn",
                    project.board_id,
                    "A closed ticket's submitted commit is not reachable from the integration ref.",
                    ticket_id=ticket.get("ticket_id"),
                    commit_hash=commit,
                    integration_ref=project.integration_ref,
                    project=project.name,
                )
            )
        elif status == "missing-commit":
            findings.append(
                _finding(
                    "unverifiable-commit",
                    "info",
                    project.board_id,
                    "A submitted commit object does not exist in the local repository.",
                    ticket_id=ticket.get("ticket_id"),
                    commit_hash=commit,
                    integration_ref=project.integration_ref,
                    project=project.name,
                )
            )
        elif status == "unavailable":
            findings.append(
                _finding(
                    "integration-check-unavailable",
                    "warn",
                    project.board_id,
                    "A submitted commit or integration ref could not be verified locally.",
                    ticket_id=ticket.get("ticket_id"),
                    commit_hash=commit,
                    integration_ref=project.integration_ref,
                    project=project.name,
                )
            )
    return findings, suppressed_pre_watermark


def integration_findings(project: Project, tickets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    findings, _ = evaluate_integration_watch(project, tickets)
    return findings


def load_privacy_terms(
    path_value: str | None, forbidden_roots: Sequence[Path] = ()
) -> tuple[str, ...]:
    if not path_value:
        return ()
    path = Path(path_value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("PURSERS_PRIVACY_TERMS must name an existing absolute file")
    resolved = path.resolve(strict=True)
    for root in forbidden_roots:
        try:
            resolved.relative_to(root.resolve(strict=True))
        except ValueError:
            continue
        raise ValueError("PURSERS_PRIVACY_TERMS must be outside registered work directories")
    terms = tuple(dict.fromkeys(line.strip().casefold() for line in resolved.read_text(encoding="utf-8").splitlines() if line.strip()))
    return terms


def _commits_since(project: Project, watermark: str | None) -> tuple[list[str], int]:
    revision = project.integration_ref if not watermark else f"{watermark}..{project.integration_ref}"
    result = _git(project.work_dir, ["rev-list", "--reverse", revision])
    if result.returncode:
        return [], 0
    commits = [line for line in result.stdout.splitlines() if COMMIT_RE.fullmatch(line)]
    omitted = max(0, len(commits) - MAX_PRIVACY_COMMITS_PER_CYCLE)
    return commits[:MAX_PRIVACY_COMMITS_PER_CYCLE], omitted


def _matching_diff_file_count(patch: str, terms: Sequence[str]) -> int:
    current: str | None = None
    matched: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[-1] if " b/" in line else None
            continue
        if current and line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            folded = line[1:].casefold()
            if any(term in folded for term in terms):
                matched.add(current)
    return len(matched)


def privacy_findings(
    project: Project, terms: Sequence[str], watermark: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    if not project.public or not terms:
        return [], watermark
    commits, omitted = _commits_since(project, watermark)
    findings: list[dict[str, Any]] = []
    last_scanned = watermark
    for commit in commits:
        result = _git(
            project.work_dir,
            ["show", "--format=", "--no-ext-diff", "--no-renames", "--unified=0", commit],
        )
        if result.returncode:
            findings.append(
                _finding(
                    "privacy-scan-unavailable",
                    "critical",
                    project.board_id,
                    "A commit diff could not be scanned against the privacy policy.",
                    commit_hash=commit,
                    project=project.name,
                )
            )
            break
        count = _matching_diff_file_count(result.stdout, terms)
        if count:
            findings.append(
                _finding(
                    "privacy-leak-suspect",
                    "critical",
                    project.board_id,
                    "A public integration commit changed files containing privacy-policy matches.",
                    commit_hash=commit,
                    matched_file_count=count,
                    project=project.name,
                )
            )
        last_scanned = commit
    if omitted:
        findings.append(
            _finding(
                "privacy-scan-truncated",
                "warn",
                project.board_id,
                "The privacy scan commit window was bounded; remaining commits will be scanned later.",
                omitted_commit_count=omitted,
                project=project.name,
            )
        )
    return findings, last_scanned


def _bounded_finding(item: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(item)
    result["kind"] = str(result.get("kind", "finding"))[:80]
    result["level"] = str(result.get("level", "info"))[:16]
    result["board_id"] = str(result.get("board_id", "unknown"))[:80]
    message = str(result.get("message", ""))[:MAX_FINDING_CHARS]
    result["message"] = message
    result["evidence"] = str(
        result.get("evidence")
        or _finding_evidence(result["board_id"], result)
    )[:MAX_EVIDENCE_CHARS]
    result["next_action"] = str(
        result.get("next_action")
        or _finding_next_action(result["kind"], result["board_id"], result)
    )[:200]
    protected = {
        "kind",
        "level",
        "board_id",
        "message",
        "evidence",
        "next_action",
    }
    while len(json.dumps(result, sort_keys=True, separators=(",", ":"))) > MAX_FINDING_CHARS:
        removable = next(
            (key for key in reversed(result) if key not in protected), None
        )
        if removable is not None:
            result.pop(removable)
            continue
        if len(message) > 40:
            message = message[:-1]
            result["message"] = message + ("…" if message else "")
            continue
        if len(result["evidence"]) > 40:
            result["evidence"] = result["evidence"][:-1]
            continue
        if len(result["next_action"]) > 40:
            result["next_action"] = result["next_action"][:-1]
            continue
        break
    return result


def _finding_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    severity = {"critical": 0, "warn": 1, "info": 2}
    operational = {
        "privacy-leak-suspect": 0,
        "privacy-scan-unavailable": 1,
        "claim-health": 2,
        "starved": 3,
        "repeat-abandoner": 4,
        "closed-but-unmerged": 5,
    }
    return (
        severity.get(str(item.get("level")), 9),
        operational.get(str(item.get("kind")), 50),
        str(item.get("board_id", "")),
        str(item.get("ticket_id", "")),
        str(item.get("commit_hash", "")),
        str(item.get("kind", "")),
        str(item.get("message", "")),
    )


def bound_findings_state(
    findings: Sequence[Mapping[str, Any]],
    generated_at: datetime,
    *,
    privacy_watermarks: Mapping[str, str] | None = None,
    drop_counters: Mapping[str, int] | None = None,
    drop_history: Sequence[Mapping[str, Any]] | None = None,
    drop_uncertainty: Sequence[Mapping[str, Any]] | None = None,
    integration_watch_since: datetime | None = None,
    suppressed_pre_watermark: int = 0,
    action_history: Sequence[Mapping[str, Any]] | None = None,
    action_history_incomplete_until: str | None = None,
    effective_mode: str = "shadow",
    board_health: Mapping[str, Any] | None = None,
    max_findings: int = MAX_FINDINGS,
    max_chars: int = MAX_STATE_CHARS - 200,
) -> dict[str, Any]:
    normalized = sorted(
        (_bounded_finding(item) for item in findings), key=_finding_sort_key
    )
    selected: list[dict[str, Any]] = []
    source_watermarks = dict(privacy_watermarks or {})
    source_counters = dict(drop_counters or {})
    source_history = [dict(item) for item in (drop_history or [])]
    source_uncertainty = [dict(item) for item in (drop_uncertainty or [])]
    source_actions = [dict(item) for item in (action_history or [])]
    base = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "integration_watch_since": (
            integration_watch_since.isoformat()
            if integration_watch_since is not None
            else None
        ),
        "suppressed_pre_watermark": suppressed_pre_watermark,
        "effective_mode": effective_mode,
        "board_health": dict(board_health or {}),
        "findings": selected,
        "truncation": {
            "findings": len(normalized),
            "privacy_watermarks": len(source_watermarks),
            "drop_counters": len(source_counters),
            "drop_history": len(source_history),
            "drop_uncertainty": len(source_uncertainty),
            "action_history": len(source_actions),
        },
        "privacy_watermarks": {},
        "drop_counters": {},
        "drop_history": [],
        "drop_uncertainty": [],
        "action_history": [],
    }
    inherited_incomplete = parse_time(action_history_incomplete_until)
    if inherited_incomplete is not None and inherited_incomplete > generated_at:
        base["action_history_incomplete_until"] = inherited_incomplete.isoformat()
    critical = [item for item in normalized if item.get("level") == "critical"]
    remaining = [item for item in normalized if item.get("level") != "critical"]

    def add_findings(rows: Sequence[dict[str, Any]]) -> None:
        for item in rows:
            if len(selected) >= max_findings:
                break
            selected.append(item)
            base["truncation"]["findings"] = len(normalized) - len(selected)
            if len(json.dumps(base, sort_keys=True, separators=(",", ":"))) > max_chars:
                selected.pop()
                base["truncation"]["findings"] = len(normalized) - len(selected)
                break

    # Critical alerts get first claim on the payload.  Delta evidence is then
    # reserved before warnings so reporting volume cannot erase history.
    add_findings(critical)
    for item in sorted(
        source_actions,
        key=lambda entry: str(entry.get("performed_at", "")),
        reverse=True,
    ):
        base["action_history"].append(item)
        base["truncation"]["action_history"] = (
            len(source_actions) - len(base["action_history"])
        )
        if len(json.dumps(base, sort_keys=True, separators=(",", ":"))) > max_chars:
            base["action_history"].pop()
            base["truncation"]["action_history"] += 1
            base["action_history_incomplete_until"] = (
                generated_at + timedelta(seconds=NUDGE_RATE_SECONDS)
            ).isoformat()
            break
    for item in sorted(
        source_history,
        key=lambda entry: (
            str(entry.get("observed_at", "")),
            str(entry.get("ticket_id", "")),
        ),
        reverse=True,
    ):
        base["drop_history"].append(item)
        base["truncation"]["drop_history"] = (
            len(source_history) - len(base["drop_history"])
        )
        if len(json.dumps(base, sort_keys=True, separators=(",", ":"))) > max_chars:
            base["drop_history"].pop()
            base["truncation"]["drop_history"] += 1
            break
    for item in sorted(
        source_uncertainty,
        key=lambda entry: (
            str(entry.get("observed_at", "")),
            str(entry.get("ticket_id", "")),
        ),
        reverse=True,
    ):
        base["drop_uncertainty"].append(item)
        base["truncation"]["drop_uncertainty"] = (
            len(source_uncertainty) - len(base["drop_uncertainty"])
        )
        if len(json.dumps(base, sort_keys=True, separators=(",", ":"))) > max_chars:
            base["drop_uncertainty"].pop()
            base["truncation"]["drop_uncertainty"] += 1
            break
    for key in sorted(source_counters):
        base["drop_counters"][key] = source_counters[key]
        base["truncation"]["drop_counters"] = (
            len(source_counters) - len(base["drop_counters"])
        )
        if len(json.dumps(base, sort_keys=True, separators=(",", ":"))) > max_chars:
            base["drop_counters"].pop(key)
            base["truncation"]["drop_counters"] += 1
            break
    for key in sorted(source_watermarks):
        base["privacy_watermarks"][key] = source_watermarks[key]
        base["truncation"]["privacy_watermarks"] = (
            len(source_watermarks) - len(base["privacy_watermarks"])
        )
        if len(json.dumps(base, sort_keys=True, separators=(",", ":"))) > max_chars:
            base["privacy_watermarks"].pop(key)
            base["truncation"]["privacy_watermarks"] += 1
            break
    add_findings(remaining)
    return base


def format_digest(
    period: str,
    generated_at: datetime,
    states: Mapping[str, Mapping[str, Any]],
) -> str:
    levels: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    lines = [
        f"Coordinator {period} digest — {generated_at.date().isoformat()}",
        "Coverage: bounded current snapshots; counts are lower bounds where truncation is reported.",
    ]
    for board_id in sorted(states):
        findings = states[board_id].get("findings", [])
        for item in findings if isinstance(findings, list) else []:
            if isinstance(item, Mapping):
                levels[str(item.get("level", "unknown"))] += 1
                kinds[str(item.get("kind", "unknown"))] += 1
        truncated = states[board_id].get("truncation", {}).get("findings", 0)
        lines.append(f"- {board_id}: {len(findings)} finding(s), {truncated} omitted")
    lines.append("Levels: " + (", ".join(f"{key}={levels[key]}" for key in sorted(levels)) or "none"))
    lines.append("Kinds: " + (", ".join(f"{key}={kinds[key]}" for key in sorted(kinds)) or "none"))
    lines.append("Retention: daily digests older than 30 days are superseded by weekly rollups.")
    return "\n".join(lines)


class RawReader:
    """Non-joining Central session restricted to bounded, pure read tools."""

    ALLOWED = frozenset({"board_state_get", "board_snapshot", "ticket_list"})

    def __init__(self, url: str, token: str):
        self.url = url
        self._token = token
        self._stack: AsyncExitStack | None = None
        self._client: Any = None
        self._decode: Callable[[Any], dict[str, Any]] | None = None

    async def __aenter__(self) -> "RawReader":
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from pursers_client.client import BoardClient

        self._decode = BoardClient._decode
        self._stack = AsyncExitStack()
        # Reuse the verified client's secret-bearing HTTP construction without
        # entering BoardClient itself; entering it would join/mutate a seat.
        transport_owner = BoardClient(self.url, self._token, "read-only")
        http = await self._stack.enter_async_context(
            transport_owner._http()  # noqa: SLF001 - intentional non-joining path.
        )
        transport = streamable_http_client(self.url, http_client=http)
        self._client = await self._stack.enter_async_context(Client(transport, mode="2026-07-28", cache=None))
        return self

    async def __aexit__(self, *_args: Any) -> None:
        if self._stack:
            await self._stack.aclose()

    async def call(self, name: str, board_id: str, **arguments: Any) -> dict[str, Any]:
        if name not in self.ALLOWED or self._decode is None:
            raise RuntimeError("raw reader rejected a non-pure tool")
        result = await self._client.call_tool(name, {"board_id": board_id, **arguments})
        return self._decode(result)


def _previous_payload(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    try:
        state = raw["state"] if raw else None
        value = state["value"] if isinstance(state, Mapping) else None
        parsed = json.loads(value) if isinstance(value, str) else {}
        return parsed if isinstance(parsed, dict) else {}
    except (KeyError, json.JSONDecodeError, TypeError):
        return {}


async def read_cycle(reader: RawReader, home_board: str) -> tuple[list[Project], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    registry = await reader.call("board_state_get", home_board, key="project_registry")
    projects = parse_registry(registry)
    snapshots: dict[str, dict[str, Any]] = {}
    previous: dict[str, dict[str, Any]] = {}
    for board_id in sorted({project.board_id for project in projects}):
        try:
            snapshots[board_id] = await reader.call(
                "board_snapshot",
                board_id,
                limit=MAX_SNAPSHOT_ITEMS,
                max_bytes=MAX_SNAPSHOT_BYTES,
            )
        except Exception as exc:
            snapshots[board_id] = {
                "board": {"board_id": board_id},
                "agents": [],
                "tickets": [],
                "snapshot_error_class": type(exc).__name__,
                "truncated": True,
                "omitted_counts": {"agents": 1, "tickets": 1},
            }
        else:
            try:
                active = await reader.call(
                    "ticket_list", board_id, include_closed=False, limit=500
                )
            except Exception:
                active = {}
            if active.get("count") == active.get("total_matching"):
                snapshots[board_id]["coordination_tickets"] = [
                    row
                    for row in active.get("tickets", [])
                    if isinstance(row, Mapping)
                ]
                snapshots[board_id]["coordination_tickets_complete"] = True
        try:
            prior = await reader.call("board_state_get", board_id, key=STATE_KEY)
        except Exception:  # Missing optional state or an unavailable board.
            prior = None
        previous[board_id] = _previous_payload(prior)
    return projects, snapshots, previous


def analyze_cycle(
    projects: Sequence[Project],
    snapshots: Mapping[str, Mapping[str, Any]],
    previous: Mapping[str, Mapping[str, Any]],
    terms: Sequence[str],
    now: datetime,
    integration_watch_since: datetime | None = None,
    thresholds: Thresholds = Thresholds(),
    effective_mode: str = "shadow",
    degraded_streaks: dict[str, int] | None = None,
) -> dict[str, dict[str, Any]]:
    findings_by_board: dict[str, list[dict[str, Any]]] = {}
    watermarks_by_board: dict[str, dict[str, str]] = {}
    drop_counters_by_board: dict[str, dict[str, int]] = {}
    drop_history_by_board: dict[str, list[dict[str, Any]]] = {}
    drop_uncertainty_by_board: dict[str, list[dict[str, Any]]] = {}
    suppressed_by_board: dict[str, int] = {}
    board_health_by_board: dict[str, dict[str, Any]] = {}
    for board_id, snapshot in snapshots.items():
        coordination_snapshot = dict(snapshot)
        coordination_snapshot["tickets"] = snapshot.get(
            "coordination_tickets", snapshot.get("tickets", [])
        )
        tickets = [
            row for row in snapshot.get("tickets", []) if isinstance(row, Mapping)
        ]
        drop_findings, counters, history, uncertainty = update_drop_evidence(
            board_id, tickets, previous.get(board_id, {}), now, thresholds
        )
        findings_by_board[board_id] = ticket_findings(
            board_id,
            coordination_snapshot,
            now,
            thresholds,
            repeat_abandoner_ids(history, thresholds),
        )
        findings_by_board[board_id].extend(drop_findings)
        degraded, reason, error_class = board_degradation(snapshot)
        prior_health = previous.get(board_id, {}).get("board_health", {})
        persisted_streak = (
            prior_health.get("consecutive_degraded_polls", 0)
            if isinstance(prior_health, Mapping)
            else 0
        )
        if not isinstance(persisted_streak, int) or isinstance(persisted_streak, bool):
            persisted_streak = 0
        runtime_streak = (
            degraded_streaks.get(board_id, 0)
            if degraded_streaks is not None
            else 0
        )
        streak = max(0, persisted_streak, runtime_streak) + 1 if degraded else 0
        if degraded_streaks is not None:
            degraded_streaks[board_id] = streak
        board_health_by_board[board_id] = {
            "status": "degraded" if degraded else "healthy",
            "consecutive_degraded_polls": streak,
            "reason": reason,
            "error_class": error_class,
        }
        if streak >= BOARD_DEGRADED_POLLS:
            findings_by_board[board_id].append(
                _finding(
                    "board-degraded",
                    "critical",
                    board_id,
                    "A registry-active board failed to return a complete snapshot repeatedly.",
                    degradation_reason=reason,
                    error_class=error_class,
                    observed_consecutive_polls=streak,
                    threshold_polls=BOARD_DEGRADED_POLLS,
                )
            )
        drop_counters_by_board[board_id] = counters
        drop_history_by_board[board_id] = history
        drop_uncertainty_by_board[board_id] = uncertainty
        suppressed_by_board[board_id] = 0
        prior_marks = previous.get(board_id, {}).get("privacy_watermarks", {})
        watermarks_by_board[board_id] = dict(prior_marks) if isinstance(prior_marks, Mapping) else {}
    for project in projects:
        tickets = [row for row in snapshots[project.board_id].get("tickets", []) if isinstance(row, Mapping)]
        integration, suppressed = evaluate_integration_watch(
            project, tickets, integration_watch_since
        )
        findings_by_board[project.board_id].extend(integration)
        suppressed_by_board[project.board_id] += suppressed
        prior = watermarks_by_board[project.board_id].get(project.name)
        privacy, watermark = privacy_findings(project, terms, prior)
        findings_by_board[project.board_id].extend(privacy)
        if watermark:
            watermarks_by_board[project.board_id][project.name] = watermark

    # Fleet fairness order: critical first, then oldest-open-first across boards.
    ranks: list[tuple[int, datetime, str, str]] = []
    for board_id, snapshot in snapshots.items():
        for ticket in snapshot.get(
            "coordination_tickets", snapshot.get("tickets", [])
        ):
            if isinstance(ticket, Mapping) and starvation_stage(ticket, now, thresholds):
                created = parse_time(ticket.get("created_at")) or now
                ranks.append((0 if ticket.get("priority") == "critical" else 1, created, board_id, str(ticket.get("ticket_id"))))
    rank_map = {(board, ticket): index + 1 for index, (_, _, board, ticket) in enumerate(sorted(ranks))}
    for board_id, findings in findings_by_board.items():
        for finding in findings:
            if finding.get("kind") == "starved":
                finding["fleet_fairness_rank"] = rank_map.get((board_id, str(finding.get("ticket_id"))))

    return {
        board_id: bound_findings_state(
            findings,
            now,
            privacy_watermarks=watermarks_by_board[board_id],
            drop_counters=drop_counters_by_board[board_id],
            drop_history=drop_history_by_board[board_id],
            drop_uncertainty=drop_uncertainty_by_board[board_id],
            integration_watch_since=integration_watch_since,
            suppressed_pre_watermark=suppressed_by_board[board_id],
            action_history=_recent_action_history(previous, now).get(board_id, []),
            action_history_incomplete_until=_history_incomplete_until(
                previous.get(board_id, {}), now
            ),
            effective_mode=effective_mode,
            board_health=board_health_by_board[board_id],
        )
        for board_id, findings in findings_by_board.items()
    }


def merge_action_results(
    states: Mapping[str, Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
    histories: Mapping[str, Sequence[Mapping[str, Any]]],
    now: datetime,
    effective_mode: str,
) -> dict[str, dict[str, Any]]:
    additions: dict[str, list[Mapping[str, Any]]] = {}
    for item in findings:
        additions.setdefault(str(item.get("board_id", "")), []).append(item)
    merged: dict[str, dict[str, Any]] = {}
    for board_id, state in states.items():
        merged[board_id] = bound_findings_state(
            [
                item
                for item in state.get("findings", [])
                if isinstance(item, Mapping)
            ]
            + additions.get(board_id, []),
            now,
            privacy_watermarks=state.get("privacy_watermarks", {}),
            drop_counters=state.get("drop_counters", {}),
            drop_history=state.get("drop_history", []),
            drop_uncertainty=state.get("drop_uncertainty", []),
            integration_watch_since=parse_time(
                state.get("integration_watch_since")
            ),
            suppressed_pre_watermark=int(
                state.get("suppressed_pre_watermark", 0) or 0
            ),
            action_history=histories.get(board_id, []),
            action_history_incomplete_until=state.get(
                "action_history_incomplete_until"
            ),
            effective_mode=effective_mode,
            board_health=(
                state.get("board_health", {})
                if isinstance(state.get("board_health"), Mapping)
                else {}
            ),
        )
    return merged


async def execute_actions(
    actions: Sequence[Action],
    mutate: Callable[[Action], Any],
    runtime: RuntimeState,
    now: datetime,
    previous: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    findings: list[dict[str, Any]] = []
    histories = _recent_action_history(previous, now)
    for action in actions:
        if runtime.effective_mode != "active":
            findings.append(
                action_finding(action, f"would_{action.kind}", "shadow")
            )
            continue
        try:
            await mutate(action)
        except Exception as exc:
            runtime.consecutive_failures += 1
            findings.append(
                action_finding(
                    action,
                    "mutation_failed",
                    "active",
                    attempted_action=action.kind,
                    error=str(exc)[:200],
                    error_class=type(exc).__name__,
                    consecutive_failures=runtime.consecutive_failures,
                )
            )
            if runtime.consecutive_failures >= 3:
                runtime.effective_mode = "shadow"
                findings.append(
                    _finding(
                        "coordinator_circuit_open",
                        "critical",
                        action.board_id,
                        "Three consecutive coordination mutations failed; effective mode dropped to shadow.",
                        mode="shadow",
                        requested_mode=runtime.requested_mode,
                        consecutive_failures=runtime.consecutive_failures,
                    )
                )
            continue
        runtime.consecutive_failures = 0
        findings.append(action_finding(action, action.kind, "active"))
        histories.setdefault(action.board_id, []).append(
            {
                "kind": action.kind,
                "target_agent_id": action.target_agent_id,
                "performed_at": now.isoformat(),
            }
        )
    return findings, histories


async def mutate_action(
    url: str, token: str, agent_name: str, action: Action, now: datetime
) -> dict[str, Any]:
    from pursers_client import BoardClient

    async with BoardClient(
        url, token, action.board_id, agent_name=agent_name
    ) as client:
        if action.kind == "assign":
            return await client._call(  # noqa: SLF001 - phase-2 primitive wrapper.
                "ticket_assign",
                {
                    "agent_name": agent_name,
                    "ticket_id": action.ticket_id,
                    "assigned_to_agent_id": action.target_agent_id,
                    "expected_status": "open",
                    "expected_assigned_to_agent_id": None,
                    "coordinator_op_key": action.op_key,
                    "reason": action.reason,
                },
            )
        return await client._call(  # noqa: SLF001 - phase-2 primitive wrapper.
            "agent_nudge",
            {
                "agent_name": agent_name,
                "ticket_id": action.ticket_id,
                "target_agent_id": action.target_agent_id,
                "coordinator_op_key": action.op_key,
                "reason": action.reason,
                "expires_at": (now + timedelta(seconds=NUDGE_EXPIRY_SECONDS)).isoformat(),
            },
        )


def home_audit_state(
    home_board: str,
    states: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any] | None:
    """Mirror fleet-critical board health onto the reachable home state."""
    source = states.get(home_board)
    if source is None:
        return None

    findings = [
        dict(item)
        for item in source.get("findings", [])
        if isinstance(item, Mapping)
    ]
    seen = {
        (
            str(item.get("kind", "")),
            str(item.get("board_id", "")),
            str(item.get("evidence", "")),
        )
        for item in findings
    }
    for board_id in sorted(states):
        if board_id == home_board:
            continue
        board_findings = states[board_id].get("findings", [])
        for item in board_findings if isinstance(board_findings, list) else []:
            if not isinstance(item, Mapping) or item.get("kind") != "board-degraded":
                continue
            mirrored = dict(item)
            mirrored["board_id"] = str(item.get("board_id") or board_id)
            key = (
                "board-degraded",
                mirrored["board_id"],
                str(mirrored.get("evidence", "")),
            )
            if key not in seen:
                findings.append(mirrored)
                seen.add(key)

    rebuilt = bound_findings_state(
        findings,
        now,
        privacy_watermarks=source.get("privacy_watermarks", {}),
        drop_counters=source.get("drop_counters", {}),
        drop_history=source.get("drop_history", []),
        drop_uncertainty=source.get("drop_uncertainty", []),
        integration_watch_since=parse_time(source.get("integration_watch_since")),
        suppressed_pre_watermark=int(source.get("suppressed_pre_watermark", 0) or 0),
        action_history=source.get("action_history", []),
        action_history_incomplete_until=source.get("action_history_incomplete_until"),
        effective_mode=str(source.get("effective_mode", "shadow")),
        board_health=(
            source.get("board_health", {})
            if isinstance(source.get("board_health"), Mapping)
            else {}
        ),
    )
    source_truncation = source.get("truncation", {})
    if isinstance(source_truncation, Mapping):
        rebuilt["truncation"]["findings"] += max(
            0, int(source_truncation.get("findings", 0) or 0)
        )
    for marker in ("last_daily_digest", "last_weekly_digest"):
        if marker in source:
            rebuilt[marker] = source[marker]
    return rebuilt


async def write_reports(
    url: str,
    token: str,
    home_board: str,
    agent_name: str,
    states: Mapping[str, Mapping[str, Any]],
    previous: Mapping[str, Mapping[str, Any]],
    now: datetime,
) -> None:
    from pursers_client import BoardClient

    # Publish non-home findings first. A board that cannot accept its report
    # must not prevent healthy boards or the home audit surface from updating.
    for board_id, state in states.items():
        if board_id == home_board:
            continue
        try:
            async with BoardClient(
                url, token, board_id, agent_name=agent_name
            ) as client:
                await client.board_state_update(
                    STATE_KEY,
                    json.dumps(state, sort_keys=True, separators=(",", ":")),
                )
        except Exception:
            # The snapshot-side finding already carries a scrubbed error class.
            # Never retain transport exception text in coordinator state.
            continue
    previous_home = previous.get(home_board, {})
    today = now.date().isoformat()
    week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    write_daily = previous_home.get("last_daily_digest") != today
    write_weekly = previous_home.get("last_weekly_digest") != week
    if write_daily or write_weekly:
        async with BoardClient(url, token, home_board, agent_name=agent_name) as client:
            if write_daily:
                await client.memory_write(
                    f"Coordinator daily digest {today}",
                    format_digest("daily", now, states),
                    "project",
                    memory_type="checkpoint",
                    tags=["coordinator", "digest", "daily"],
                )
            if write_weekly:
                await client.memory_write(
                    f"Coordinator weekly rollup {week}",
                    format_digest("weekly", now, states),
                    "project",
                    memory_type="checkpoint",
                    tags=["coordinator", "digest", "weekly"],
                )
    home_state = home_audit_state(home_board, states, now)
    if home_state is not None:
        async with BoardClient(url, token, home_board, agent_name=agent_name) as client:
            await client.board_state_update(
                STATE_KEY,
                json.dumps(home_state, sort_keys=True, separators=(",", ":")),
            )


def _read_token(path_value: str) -> str:
    path = Path(path_value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("coordinator token path must name an existing absolute file")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("coordinator token file is empty")
    return token


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the fleet coordinator")
    parser.add_argument("--url", default=os.environ.get("ONBOARD_CENTRAL_URL", DEFAULT_URL))
    parser.add_argument("--token-path", default=os.environ.get("PURSERS_COORDINATOR_TOKEN_PATH") or os.environ.get("ONBOARD_TOKEN_FILE"))
    parser.add_argument("--home-board", default=os.environ.get("ONBOARD_BOARD_ID", "pursers"))
    parser.add_argument("--agent-name", default="coordinator-1")
    parser.add_argument("--mode", choices=("shadow", "active"), default="shadow")
    parser.add_argument("--starved-seconds", type=int, default=Thresholds.starved_seconds)
    parser.add_argument(
        "--critical-starved-seconds",
        type=int,
        default=Thresholds.critical_starved_seconds,
    )
    parser.add_argument(
        "--review-backlog-seconds",
        type=int,
        default=Thresholds.review_backlog_seconds,
    )
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--integration-watch-since",
        help="Ignore closed-ticket integration checks before this ISO-8601 timestamp",
    )
    args = parser.parse_args(argv)
    if not args.token_path:
        parser.error("--token-path or PURSERS_COORDINATOR_TOKEN_PATH is required")
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be positive")
    if args.integration_watch_since and parse_time(args.integration_watch_since) is None:
        parser.error("--integration-watch-since must be an ISO-8601 timestamp")
    if (
        args.starved_seconds < 1
        or args.critical_starved_seconds < 1
        or args.review_backlog_seconds < 1
    ):
        parser.error("coordinator thresholds must be positive")
    return args


async def run(args: argparse.Namespace) -> None:
    token = _read_token(args.token_path)
    terms_path = os.environ.get("PURSERS_PRIVACY_TERMS")
    integration_watch_since = parse_time(args.integration_watch_since)
    runtime = RuntimeState.for_mode("shadow" if args.dry_run else args.mode)
    degraded_streaks: dict[str, int] = {}
    thresholds = Thresholds(
        starved_seconds=args.starved_seconds,
        critical_starved_seconds=args.critical_starved_seconds,
        review_backlog_seconds=args.review_backlog_seconds,
    )
    while True:
        now = utc_now()
        async with RawReader(args.url, token) as reader:
            projects, snapshots, previous = await read_cycle(reader, args.home_board)
        terms = load_privacy_terms(terms_path, [project.work_dir for project in projects])
        states = analyze_cycle(
            projects,
            snapshots,
            previous,
            terms,
            now,
            integration_watch_since=integration_watch_since,
            thresholds=thresholds,
            effective_mode=runtime.effective_mode,
            degraded_streaks=degraded_streaks,
        )
        actions = plan_actions(snapshots, states, previous, now, thresholds)
        action_findings, histories = await execute_actions(
            actions,
            lambda action, cycle_now=now: mutate_action(
                args.url, token, args.agent_name, action, cycle_now
            ),
            runtime,
            now,
            previous,
        )
        states = merge_action_results(
            states,
            action_findings,
            histories,
            now,
            runtime.effective_mode,
        )
        if args.dry_run:
            print(json.dumps(states, indent=2, sort_keys=True))
        else:
            # Digest markers share the one allowed state key.
            home = states.get(args.home_board)
            if home is not None:
                home["last_daily_digest"] = now.date().isoformat()
                home["last_weekly_digest"] = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
            await write_reports(args.url, token, args.home_board, args.agent_name, states, previous, now)
        if args.once:
            return
        await asyncio.sleep(args.poll_seconds)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
