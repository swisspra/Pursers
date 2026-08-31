#!/usr/bin/env python3
"""Fleet coordinator with phase-2 dispatch and opt-in phase-3 intake."""

from __future__ import annotations

import argparse
import asyncio
import base64
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
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence


STATE_KEY = "coordinator_findings"
INTAKE_STATE_KEY = "coordinator_intake"
CONFIG_STATE_KEY = "coordinator_config"
SCHEMA_VERSION = 2
DEFAULT_URL = "https://127.0.0.1:8766/mcp"
MAX_SNAPSHOT_ITEMS = 1_000
MAX_SNAPSHOT_BYTES = 750_000
MAX_FINDINGS = 50
MAX_FINDING_CHARS = 500
MAX_EVIDENCE_CHARS = 300
MAX_INTAKE_FINDING_CHARS = 4_000
MAX_INTAKE_EVIDENCE_CHARS = 3_500
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
INTAKE_RATE_LIMIT = 5
INTAKE_RATE_WINDOW_SECONDS = 3_600
INTAKE_BREAKER_FAILURES = 3
INTAKE_SCOPE = "board:intake"
INTAKE_CATEGORIES = (
    "docs",
    "tests",
    "audit-analysis",
    "bug",
    "production-code",
    "release-ci",
    "membership-roles",
    "board-registry",
)
DEFAULT_AUTO_CATEGORIES = ("docs", "tests", "audit-analysis", "bug")
DEFAULT_ALWAYS_ASK_CATEGORIES = tuple(
    category for category in INTAKE_CATEGORIES if category not in DEFAULT_AUTO_CATEGORIES
)


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
class CoordinatorConfig:
    thresholds: Thresholds
    integration_watch_since: datetime | None
    intake_enabled: bool
    auto_categories: tuple[str, ...]
    always_ask_categories: tuple[str, ...]
    work_domain_always_ask: bool
    rate_per_hour: int
    effective: dict[str, Any]
    sources: dict[str, str]
    invalid_fields: tuple[str, ...]


@dataclass(frozen=True)
class Project:
    name: str
    board_id: str
    work_dir: Path
    integration_ref: str = "main"
    public: bool = False
    domain: str = "personal"


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
    intake_failures: dict[str, int] | None = None
    intake_breakers: set[str] | None = None

    @classmethod
    def for_mode(cls, mode: str) -> "RuntimeState":
        return cls(
            requested_mode=mode,
            effective_mode=mode,
            intake_failures={},
            intake_breakers=set(),
        )


@dataclass(frozen=True)
class IntakeAsk:
    ask_id: str
    text: str
    requested_by: str
    board_id: str


@dataclass(frozen=True)
class IntakeDraft:
    ticket_id: str
    op_key: str
    title: str
    description: str
    scope: str
    target_url: str
    required_fields: tuple[str, ...]
    category: str
    has_clear_reproduction: bool


class IntakeDrafter(Protocol):
    """Phase-3 drafting seam; deterministic until an approved LLM is supplied."""

    def __call__(self, ask: IntakeAsk, project: Project) -> IntakeDraft: ...


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


def _state_json(raw: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, bool]:
    """Decode one board-state value without letting bad operator input escape."""
    if not raw or not isinstance(raw.get("state"), Mapping):
        return None, False
    value = raw["state"].get("value")
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except json.JSONDecodeError:
        return None, True
    return (dict(parsed), False) if isinstance(parsed, Mapping) else (None, True)


def _csv_categories(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, str):
        rows = tuple(item.strip() for item in value.split(",") if item.strip())
    elif isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        rows = tuple(value)
    else:
        return None
    if len(set(rows)) != len(rows) or any(item not in INTAKE_CATEGORIES for item in rows):
        return None
    return rows


def resolve_coordinator_config(
    raw: Mapping[str, Any] | None, args: argparse.Namespace
) -> CoordinatorConfig:
    """Resolve state > explicit CLI flag > built-in, independently per field."""
    document, malformed = _state_json(raw)
    explicit = frozenset(getattr(args, "_explicit_config_flags", ()))
    invalid: list[str] = ["$"] if malformed else []
    sources: dict[str, str] = {}

    def choose(
        path: str,
        state_value: Any,
        valid: Callable[[Any], bool],
        cli_name: str,
        builtin: Any,
        *,
        present: bool,
        transform: Callable[[Any], Any] = lambda value: value,
    ) -> Any:
        if present and valid(state_value):
            sources[path] = "config"
            return transform(state_value)
        if document is not None:
            invalid.append(path)
        cli_value = getattr(args, cli_name, builtin)
        if cli_name in explicit:
            sources[path] = "flag"
            return transform(cli_value)
        sources[path] = "default"
        return builtin

    thresholds_doc = document.get("thresholds") if document else None
    thresholds_doc = thresholds_doc if isinstance(thresholds_doc, Mapping) else {}
    intake_doc = document.get("intake") if document else None
    intake_doc = intake_doc if isinstance(intake_doc, Mapping) else {}
    second = lambda value: type(value) is int and 10 <= value <= 86_400
    ratio = lambda value: type(value) in (int, float) and 0.1 <= value <= 1
    fields = {
        "stale_seconds": choose("thresholds.stale_seconds", thresholds_doc.get("stale_seconds"), second, "stale_seconds", Thresholds.stale_seconds, present="stale_seconds" in thresholds_doc),
        "lease_warning_fraction": choose("thresholds.lease_warning_ratio", thresholds_doc.get("lease_warning_ratio"), ratio, "lease_warning_ratio", Thresholds.lease_warning_fraction, present="lease_warning_ratio" in thresholds_doc, transform=float),
        "lease_grace_seconds": choose("thresholds.grace_seconds", thresholds_doc.get("grace_seconds"), second, "grace_seconds", Thresholds.lease_grace_seconds, present="grace_seconds" in thresholds_doc),
        "starved_seconds": choose("thresholds.starved_seconds", thresholds_doc.get("starved_seconds"), second, "starved_seconds", Thresholds.starved_seconds, present="starved_seconds" in thresholds_doc),
        "critical_starved_seconds": choose("thresholds.critical_starved_seconds", thresholds_doc.get("critical_starved_seconds"), second, "critical_starved_seconds", Thresholds.critical_starved_seconds, present="critical_starved_seconds" in thresholds_doc),
        "review_backlog_seconds": choose("thresholds.review_backlog_seconds", thresholds_doc.get("review_backlog_seconds"), second, "review_backlog_seconds", Thresholds.review_backlog_seconds, present="review_backlog_seconds" in thresholds_doc),
        "repeat_abandon_count": choose("thresholds.abandoner_drops", thresholds_doc.get("abandoner_drops"), lambda value: type(value) is int and 1 <= value <= 20, "abandoner_drops", Thresholds.repeat_abandon_count, present="abandoner_drops" in thresholds_doc),
        "repeat_abandon_window_seconds": choose("thresholds.abandoner_window_days", thresholds_doc.get("abandoner_window_days"), lambda value: type(value) is int and 1 <= value <= 365, "abandoner_window_days", 7, present="abandoner_window_days" in thresholds_doc) * 86_400,
    }
    integration = choose(
        "integration_watch_since",
        document.get("integration_watch_since") if document else None,
        lambda value: value is None or parse_time(value) is not None,
        "integration_watch_since",
        None,
        present=document is not None and "integration_watch_since" in document,
        transform=parse_time,
    )
    if document is not None and document.get("schema_version") != 1:
        invalid.append("schema_version")
    auto = choose("intake.auto_categories", intake_doc.get("auto_categories"), lambda value: _csv_categories(value) is not None, "intake_auto_categories", DEFAULT_AUTO_CATEGORIES, present="auto_categories" in intake_doc, transform=lambda value: _csv_categories(value) or ())
    always = choose("intake.always_ask_categories", intake_doc.get("always_ask_categories"), lambda value: _csv_categories(value) is not None, "intake_always_ask_categories", DEFAULT_ALWAYS_ASK_CATEGORIES, present="always_ask_categories" in intake_doc, transform=lambda value: _csv_categories(value) or ())
    if set(auto) & set(always) or set(auto) | set(always) != set(INTAKE_CATEGORIES):
        invalid.extend(("intake.auto_categories", "intake.always_ask_categories"))
        auto = (
            _csv_categories(getattr(args, "intake_auto_categories", ""))
            if "intake_auto_categories" in explicit else DEFAULT_AUTO_CATEGORIES
        ) or DEFAULT_AUTO_CATEGORIES
        always = (
            _csv_categories(getattr(args, "intake_always_ask_categories", ""))
            if "intake_always_ask_categories" in explicit else DEFAULT_ALWAYS_ASK_CATEGORIES
        ) or DEFAULT_ALWAYS_ASK_CATEGORIES
        sources["intake.auto_categories"] = "flag" if "intake_auto_categories" in explicit else "default"
        sources["intake.always_ask_categories"] = "flag" if "intake_always_ask_categories" in explicit else "default"
    enabled = choose("intake.enabled", intake_doc.get("enabled"), lambda value: type(value) is bool, "intake_enabled", False, present="enabled" in intake_doc)
    work_ask = choose("intake.work_domain_always_ask", intake_doc.get("work_domain_always_ask"), lambda value: type(value) is bool, "work_domain_always_ask", True, present="work_domain_always_ask" in intake_doc)
    rate = choose("intake.rate_per_hour", intake_doc.get("rate_per_hour"), lambda value: type(value) is int and 1 <= value <= 20, "intake_rate_per_hour", INTAKE_RATE_LIMIT, present="rate_per_hour" in intake_doc)
    threshold_values = Thresholds(**fields)
    effective = {
        "schema_version": 1,
        "thresholds": {
            "stale_seconds": threshold_values.stale_seconds,
            "lease_warning_ratio": threshold_values.lease_warning_fraction,
            "grace_seconds": threshold_values.lease_grace_seconds,
            "starved_seconds": threshold_values.starved_seconds,
            "critical_starved_seconds": threshold_values.critical_starved_seconds,
            "review_backlog_seconds": threshold_values.review_backlog_seconds,
            "abandoner_drops": threshold_values.repeat_abandon_count,
            "abandoner_window_days": threshold_values.repeat_abandon_window_seconds // 86_400,
        },
        "integration_watch_since": integration.isoformat() if integration else None,
        "intake": {"enabled": enabled, "auto_categories": list(auto), "always_ask_categories": list(always), "work_domain_always_ask": work_ask, "rate_per_hour": rate},
    }
    return CoordinatorConfig(threshold_values, integration, enabled, tuple(auto), tuple(always), work_ask, rate, effective, sources, tuple(sorted(set(invalid))))


def config_invalid_finding(
    board_id: str, config: CoordinatorConfig
) -> dict[str, Any] | None:
    if not config.invalid_fields:
        return None
    return _finding(
        "config-invalid",
        "warn",
        board_id,
        "Coordinator config contains missing or invalid fields; safe fallbacks are active.",
        invalid_fields=list(config.invalid_fields),
    )


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
        domain = row.get("domain", "personal")
        if not all(isinstance(item, str) and item.strip() for item in (board_id, work_dir, integration_ref)):
            raise ValueError("active project routing is incomplete")
        if domain not in {"personal", "work"}:
            raise ValueError("active project domain must be personal or work")
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
                domain=domain,
            )
        )
    return sorted(projects, key=lambda item: (item.board_id, item.name))


def parse_intake(raw: Mapping[str, Any] | None, board_id: str) -> list[IntakeAsk]:
    """Parse the intentionally small, human-written intake queue."""
    if not raw:
        return []
    state = raw.get("state")
    value = state.get("value") if isinstance(state, Mapping) else None
    try:
        rows = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise ValueError("coordinator_intake is malformed") from exc
    if not isinstance(rows, list):
        raise ValueError("coordinator_intake must be a list")
    asks: list[IntakeAsk] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("coordinator_intake entries must be objects")
        values = {key: row.get(key) for key in ("id", "text", "requested_by", "board_id")}
        if not all(isinstance(value, str) and value.strip() for value in values.values()):
            raise ValueError("coordinator_intake entries require id, text, requested_by, and board_id")
        ask_id = str(values["id"]).strip()
        if len(ask_id) > 120 or len(str(values["text"])) > 2_000 or len(str(values["requested_by"])) > 120:
            raise ValueError("coordinator_intake entry exceeds its size limit")
        if ask_id in seen:
            raise ValueError("coordinator_intake ids must be unique")
        if values["board_id"].strip() != board_id:
            raise ValueError("coordinator_intake entry board_id does not match its board")
        seen.add(ask_id)
        asks.append(
            IntakeAsk(
                ask_id=ask_id,
                text=str(values["text"]).strip(),
                requested_by=str(values["requested_by"]).strip(),
                board_id=board_id,
            )
        )
    return asks


def classify_intake(text: str) -> tuple[str, bool]:
    """Authorize only complete, clearly pure AUTO forms; otherwise fail closed."""
    lowered = text.casefold()
    has_reproduction = bool(
        re.search(r"\b(repro(?:duce|duction)?|steps? to reproduce|failing example|traceback)\b", lowered)
        or re.search(r"\bexpected\b.+\b(actual|got|observed)\b", lowered)
    )
    always_ask_rules = (
        ("release-ci", r"\b(release|publish|pypi|npm|deploy|deployment|ci|github actions?)\b"),
        ("membership-roles", r"\b(member(?:ship)?|role|seat|invite|permission)\b"),
        ("board-registry", r"\b(board|registry|project_registry)\b"),
    )
    for category, pattern in always_ask_rules:
        if re.search(pattern, lowered):
            return category, has_reproduction
    pure_forms = (
        (
            "docs",
            r"(?:update|edit|write|document|clarify|correct|improve|refresh|revise|add|fix)"
            r"\s+(?:the\s+)?(?:(?:setup|operator|user|developer|api|installation|usage)\s+)?"
            r"(?:readme|docs?|documentation|guide|runbook)"
            r"(?:\s+(?:guide|documentation|section|page|examples?|typos?)"
            r"(?:\s+[a-z0-9_.-]+)?|\s+for\s+(?:the\s+)?[a-z0-9_.-]+)?[.!]?",
        ),
        (
            "tests",
            r"(?:add|write|update|improve|expand|run)\s+(?:the\s+)?"
            r"(?:(?:unit|integration|regression|smoke|pytest|unittest|test)\s+)?"
            r"(?:tests?|coverage|fixtures?)(?:\s+(?:suite|cases?))?[.!]?",
        ),
        (
            "audit-analysis",
            r"(?:(?:run|perform|conduct|analy[sz]e)\s+)?(?:a\s+)?read[- ]only\s+"
            r"(?:audit|analysis|inspection|report)"
            r"(?:\s+of\s+[a-z0-9_./ -]+)?[.!]?",
        ),
    )
    for category, pattern in pure_forms:
        if re.fullmatch(pattern, lowered):
            return category, has_reproduction
    pure_bug_forms = (
        r"(?:fix|repair|resolve|diagnose|investigate)\s+(?:the\s+)?"
        r"(?:bug|crash|error|regression|broken\s+[a-z0-9_-]+)"
        r"(?:\s+(?:in|when|on)\s+[a-z0-9_./-]+)?\s*[,;:-]\s*"
        r"(?:steps?\s+to\s+reproduce|failing\s+example|traceback)"
        r"(?:\s+(?:is|are)\s+(?:listed|included|attached|provided)"
        r"|\s+(?:listed|included|attached|provided)"
        r"|:\s*run\s+(?:the\s+)?failing\s+example)?[.!]?",
        r"(?:bug|crash|error|regression|broken\s+[a-z0-9_-]+)\s+"
        r"(?:with|has)\s+(?:traceback|failing\s+example|steps?\s+to\s+reproduce)"
        r"(?:\s+(?:in|from|for)\s+[a-z0-9_./-]+)?[.!]?",
    )
    if has_reproduction and any(
        re.fullmatch(pattern, lowered) for pattern in pure_bug_forms
    ):
        return "bug", True
    bug_marker = bool(re.search(r"\b(bug|broken|crash|error|fails?|regression)\b", lowered))
    if bug_marker or re.search(r"\bfix\b", lowered):
        if has_reproduction:
            return "production-code", False
        return "bug", has_reproduction and bug_marker
    return "production-code", has_reproduction


def intake_matrix_decision(
    category: str,
    domain: str,
    has_clear_reproduction: bool = False,
    *,
    auto_categories: Sequence[str] = DEFAULT_AUTO_CATEGORIES,
    always_ask_categories: Sequence[str] = DEFAULT_ALWAYS_ASK_CATEGORIES,
    work_domain_always_ask: bool = True,
) -> tuple[str, str]:
    if category not in INTAKE_CATEGORIES:
        raise ValueError("unsupported intake category")
    if domain not in {"personal", "work"}:
        raise ValueError("unsupported board domain")
    if domain == "work" and work_domain_always_ask:
        return "ask", "work-domain-always-ask"
    if category in auto_categories:
        if category == "bug" and not has_clear_reproduction:
            return "ask", "bug-requires-clear-reproduction"
        return "auto", f"personal-{category}-auto"
    if category in always_ask_categories:
        return "ask", f"personal-{category}-always-ask"
    return "ask", f"personal-{category}-always-ask"


def _intake_identity(ask: IntakeAsk) -> tuple[str, str]:
    digest = hashlib.sha256(
        f"{ask.board_id}\0{ask.ask_id}".encode("utf-8")
    ).hexdigest()
    return f"TK-intake-{digest[:12]}", f"coord-intake-{digest[:24]}"


def deterministic_intake_draft(ask: IntakeAsk, project: Project) -> IntakeDraft:
    category, has_reproduction = classify_intake(ask.text)
    ticket_id, op_key = _intake_identity(ask)
    required = {
        "docs": ("commit_hash", "test_output"),
        "tests": ("commit_hash", "test_output"),
        "audit-analysis": ("findings", "evidence", "next_action"),
        "bug": ("reproduction", "commit_hash", "test_output"),
        "production-code": ("commit_hash", "test_output"),
        "release-ci": ("commit_hash", "test_output", "release_evidence"),
        "membership-roles": ("change_summary", "approval_evidence"),
        "board-registry": ("change_summary", "approval_evidence"),
    }[category]
    scope = "READ-ONLY" if category == "audit-analysis" else "interactive-no-send"
    compact = " ".join(ask.text.split())
    title = compact[:197] + ("..." if len(compact) > 197 else "")
    description = "\n".join(
        (
            "Structured coordinator intake.",
            f"Requested by: {ask.requested_by}",
            f"Original ask: {compact}",
            f"Category: {category}",
            "Acceptance: complete the requested work and provide every required field.",
            f"Intake op-key: {op_key}",
        )
    )
    return IntakeDraft(
        ticket_id=ticket_id,
        op_key=op_key,
        title=title,
        description=description,
        scope=scope,
        target_url=f"{project.name}/",
        required_fields=required,
        category=category,
        has_clear_reproduction=has_reproduction,
    )


def validate_intake_draft(ask: IntakeAsk, draft: IntakeDraft, project: Project) -> None:
    values = (
        ask.ask_id,
        ask.text,
        ask.requested_by,
        ask.board_id,
        draft.title,
        draft.description,
        draft.scope,
        draft.target_url,
        draft.ticket_id,
        draft.op_key,
    )
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("intake or drafted ticket has an empty required field")
    if not draft.required_fields or any(not value for value in draft.required_fields):
        raise ValueError("drafted ticket required_fields must be non-empty")
    if ask.board_id != project.board_id:
        raise ValueError("intake board is not registry-active")
    intake_matrix_decision(draft.category, project.domain, draft.has_clear_reproduction)


def _draft_evidence(
    ask: IntakeAsk, draft: IntakeDraft, decision: str, rule: str
) -> str:
    payload = {
        "ask_id": ask.ask_id,
        "category": draft.category,
        "decision": decision,
        "matrix_rule": rule,
        "draft": {
            "ticket_id": draft.ticket_id,
            "title": draft.title,
            "description": draft.description,
            "scope": draft.scope,
            "target_url": draft.target_url,
            "required_fields": list(draft.required_fields),
            "coordinator_op_key": draft.op_key,
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded) > MAX_INTAKE_EVIDENCE_CHARS:
        raise ValueError("actionable intake draft exceeds its evidence bound")
    return encoded


def intake_finding(
    kind: str,
    level: str,
    ask: IntakeAsk,
    draft: IntakeDraft,
    decision: str,
    rule: str,
    *,
    created_ticket_id: str | None = None,
) -> dict[str, Any]:
    messages = {
        "intake-created": "Structured intake created a ticket.",
        "intake-would-create": "Structured intake would create a ticket in active execution.",
        "intake-pending": "Structured intake requires operator approval.",
    }
    return {
        "kind": kind,
        "level": level,
        "board_id": ask.board_id,
        "message": messages.get(kind, "Structured intake produced a finding."),
        "evidence": _draft_evidence(ask, draft, decision, rule),
        "next_action": (
            (
                f"Grant {INTAKE_SCOPE} alongside board:coordinate, then retry ask "
                f"{ask.ask_id}; the queue remains intact."
                if rule == "missing-board-intake-grant"
                else (
                    f"Review ask {ask.ask_id}; after an explicit approve/create or "
                    "decline decision, remove it from coordinator_intake. Until then "
                    "the pending draft remains queued."
                )
            )
            if decision == "ask"
            else (
                f"Run with --enable-intake without --dry-run to create {draft.ticket_id}."
                if kind == "intake-would-create"
                else f"Review {created_ticket_id or draft.ticket_id} on {ask.board_id}."
            )
        ),
        "ask_id": ask.ask_id,
        "category": draft.category,
        "matrix_rule": rule,
        "op_key": draft.op_key,
        "ticket_id": created_ticket_id or draft.ticket_id,
    }


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
    is_intake = str(result.get("kind", "")).startswith("intake-")
    evidence_limit = (
        MAX_INTAKE_EVIDENCE_CHARS if is_intake else MAX_EVIDENCE_CHARS
    )
    finding_limit = MAX_INTAKE_FINDING_CHARS if is_intake else MAX_FINDING_CHARS
    result["kind"] = str(result.get("kind", "finding"))[:80]
    result["level"] = str(result.get("level", "info"))[:16]
    result["board_id"] = str(result.get("board_id", "unknown"))[:80]
    message = str(result.get("message", ""))[:MAX_FINDING_CHARS]
    result["message"] = message
    result["evidence"] = str(
        result.get("evidence")
        or _finding_evidence(result["board_id"], result)
    )[:evidence_limit]
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
    if is_intake:
        protected.update(
            {"ask_id", "category", "matrix_rule", "ticket_id", "op_key"}
        )
    while len(json.dumps(result, sort_keys=True, separators=(",", ":"))) > finding_limit:
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
        if not is_intake and len(result["evidence"]) > 40:
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
    effective_config: Mapping[str, Any] | None = None,
    config_sources: Mapping[str, str] | None = None,
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
        "effective_config": dict(effective_config or {}),
        "config_sources": dict(config_sources or {}),
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
    intake = [
        item
        for item in normalized
        if item.get("level") != "critical"
        and str(item.get("kind", "")).startswith("intake-")
    ]
    remaining = [
        item
        for item in normalized
        if item.get("level") != "critical"
        and not str(item.get("kind", "")).startswith("intake-")
    ]

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
    # Intake outcomes are also reserved: otherwise a consumed ask could lose
    # its only operator-visible create/approval record under ordinary backlog.
    add_findings(critical)
    add_findings(intake)
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
                all_tickets = await reader.call(
                    "ticket_list", board_id, include_closed=True, limit=500
                )
            except Exception:
                all_tickets = {}
            if all_tickets.get("count") == all_tickets.get("total_matching"):
                snapshots[board_id]["intake_tickets"] = [
                    row
                    for row in all_tickets.get("tickets", [])
                    if isinstance(row, Mapping)
                ]
                snapshots[board_id]["intake_tickets_complete"] = True
        try:
            prior = await reader.call("board_state_get", board_id, key=STATE_KEY)
        except Exception:  # Missing optional state or an unavailable board.
            prior = None
        previous[board_id] = _previous_payload(prior)
        try:
            intake = await reader.call(
                "board_state_get", board_id, key=INTAKE_STATE_KEY
            )
        except Exception:  # An absent opt-in queue is the normal default.
            intake = None
        snapshots[board_id]["coordinator_intake_state"] = intake
    try:
        config = await reader.call("board_state_get", home_board, key=CONFIG_STATE_KEY)
    except Exception:  # An absent config uses flags then built-ins.
        config = None
    if home_board in snapshots:
        snapshots[home_board]["coordinator_config_state"] = config
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
            effective_config=(
                state.get("effective_config", {})
                if isinstance(state.get("effective_config"), Mapping)
                else {}
            ),
            config_sources=(
                state.get("config_sources", {})
                if isinstance(state.get("config_sources"), Mapping)
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


def _serialize_intake(asks: Sequence[IntakeAsk]) -> str:
    return json.dumps(
        [
            {
                "id": ask.ask_id,
                "text": ask.text,
                "requested_by": ask.requested_by,
                "board_id": ask.board_id,
            }
            for ask in asks
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _recent_intake_creates(
    snapshot: Mapping[str, Any], now: datetime
) -> int | None:
    if snapshot.get("intake_tickets_complete") is True:
        ticket_rows = snapshot.get("intake_tickets", [])
    else:
        ticket_rows = snapshot.get("tickets", [])
    omitted = snapshot.get("omitted_counts") or snapshot.get("truncation_counts")
    omitted_tickets = omitted.get("tickets", 0) if isinstance(omitted, Mapping) else 0
    if (
        snapshot.get("intake_tickets_complete") is not True
        and isinstance(omitted_tickets, int)
        and omitted_tickets > 0
    ):
        return None
    count = 0
    for ticket in ticket_rows:
        if not isinstance(ticket, Mapping):
            continue
        tags = ticket.get("tags", [])
        created_age = age_seconds(ticket.get("created_at"), now)
        if (
            isinstance(tags, list)
            and "coordinator-intake" in tags
            and created_age is not None
            and created_age <= INTAKE_RATE_WINDOW_SECONDS
        ):
            count += 1
    return count


def _matches_intake_replay(
    existing: Mapping[str, Any], draft: IntakeDraft
) -> bool:
    expected = {
        "ticket_id": draft.ticket_id,
        "title": draft.title,
        "description": draft.description,
        "scope": draft.scope,
        "target_url": draft.target_url,
        "required_fields": list(draft.required_fields),
        "tags": ["coordinator-intake", f"op:{draft.op_key}"],
        "origin": "coordinator-intake",
        "coordinator_op_key": draft.op_key,
        "priority": "medium",
        "server_generated_id": False,
        "assigned_to": None,
        "assigned_to_agent_id": None,
        "assigned_to_kind": None,
    }
    return all(key in existing and existing[key] == value for key, value in expected.items())


async def create_intake_ticket(
    url: str,
    token: str,
    agent_name: str,
    board_id: str,
    draft: IntakeDraft,
) -> str:
    """Create once using the ask-derived ticket id as the durable op-key."""
    from pursers_client import BoardClient, BoardClientError

    async with BoardClient(url, token, board_id, agent_name=agent_name) as client:
        try:
            result = await client._call(  # noqa: SLF001 - new narrow capability.
                "ticket_create",
                {
                    "agent_name": agent_name,
                    "ticket_id": draft.ticket_id,
                    "title": draft.title,
                    "description": draft.description,
                    "scope": draft.scope,
                    "required_fields": list(draft.required_fields),
                    "tags": ["coordinator-intake", f"op:{draft.op_key}"],
                    "target_url": draft.target_url,
                    "unassigned": True,
                    "coordinator_op_key": draft.op_key,
                },
            )
        except BoardClientError as exc:
            if "ticket already exists" not in str(exc):
                raise
            existing = (await client.ticket_get(draft.ticket_id)).get("ticket", {})
            if not isinstance(existing, Mapping) or not _matches_intake_replay(
                existing, draft
            ):
                raise RuntimeError("intake idempotency collision") from exc
            return draft.ticket_id
        return str(result["ticket"]["ticket_id"])


async def process_intakes(
    projects: Sequence[Project],
    snapshots: Mapping[str, Mapping[str, Any]],
    now: datetime,
    runtime: RuntimeState,
    *,
    enabled: bool,
    dry_run: bool,
    create_ticket: Callable[[str, IntakeDraft], Awaitable[str]],
    drafter: IntakeDrafter = deterministic_intake_draft,
    intake_authorized: bool = True,
    auto_categories: Sequence[str] = DEFAULT_AUTO_CATEGORIES,
    always_ask_categories: Sequence[str] = DEFAULT_ALWAYS_ASK_CATEGORIES,
    work_domain_always_ask: bool = True,
    rate_per_hour: int = INTAKE_RATE_LIMIT,
) -> tuple[list[dict[str, Any]], dict[str, frozenset[str]]]:
    """Classify, validate and consume queues; mutations remain injected/testable."""
    if not enabled:
        return [], {}
    failures = runtime.intake_failures if runtime.intake_failures is not None else {}
    breakers = runtime.intake_breakers if runtime.intake_breakers is not None else set()
    runtime.intake_failures = failures
    runtime.intake_breakers = breakers
    findings: list[dict[str, Any]] = []
    updates: dict[str, frozenset[str]] = {}
    by_board: dict[str, list[Project]] = {}
    for project in projects:
        by_board.setdefault(project.board_id, []).append(project)
    for board_id, board_projects in sorted(by_board.items()):
        snapshot = snapshots.get(board_id, {})
        raw_intake = snapshot.get("coordinator_intake_state")
        try:
            asks = parse_intake(raw_intake if isinstance(raw_intake, Mapping) else None, board_id)
        except ValueError as exc:
            findings.append(
                _finding(
                    "intake-invalid",
                    "warn",
                    board_id,
                    "The structured intake queue is invalid and was left untouched.",
                    error_class=type(exc).__name__,
                )
            )
            continue
        if not asks:
            continue
        domain = "work" if any(item.domain == "work" for item in board_projects) else "personal"
        selected = sorted(board_projects, key=lambda item: item.name)[0]
        project = Project(
            selected.name,
            selected.board_id,
            selected.work_dir,
            selected.integration_ref,
            selected.public,
            domain,
        )
        recent_creates = _recent_intake_creates(snapshot, now)
        processed: set[str] = set()
        for ask in asks:
            draft = drafter(ask, project)
            validate_intake_draft(ask, draft, project)
            decision, rule = intake_matrix_decision(
                draft.category,
                project.domain,
                draft.has_clear_reproduction,
                auto_categories=auto_categories,
                always_ask_categories=always_ask_categories,
                work_domain_always_ask=work_domain_always_ask,
            )
            if decision == "auto" and not intake_authorized and not dry_run:
                decision, rule = "ask", "missing-board-intake-grant"
            if decision == "auto" and board_id in breakers:
                decision, rule = "ask", "create-breaker-draft-only"
            if decision == "auto" and recent_creates is None:
                decision, rule = "ask", "incomplete-rate-history-draft-only"
            if (
                decision == "auto"
                and recent_creates is not None
                and recent_creates >= rate_per_hour
            ):
                decision, rule = "ask", "hourly-auto-create-limit"
            if decision == "ask":
                findings.append(
                    intake_finding("intake-pending", "warn", ask, draft, decision, rule)
                )
                continue
            if dry_run:
                findings.append(
                    intake_finding(
                        "intake-would-create", "info", ask, draft, decision, rule
                    )
                )
                continue
            try:
                ticket_id = await create_ticket(board_id, draft)
            except Exception as exc:
                failures[board_id] = failures.get(board_id, 0) + 1
                findings.append(
                    _finding(
                        "intake-create-failed",
                        "warn",
                        board_id,
                        "A structured intake ticket creation failed.",
                        ask_id=ask.ask_id,
                        error_class=type(exc).__name__,
                        consecutive_failures=failures[board_id],
                    )
                )
                if failures[board_id] >= INTAKE_BREAKER_FAILURES:
                    breakers.add(board_id)
                    findings.append(
                        intake_finding(
                            "intake-pending",
                            "warn",
                            ask,
                            draft,
                            "ask",
                            "create-breaker-draft-only",
                        )
                    )
                else:
                    continue
                continue
            failures[board_id] = 0
            if recent_creates is not None:
                recent_creates += 1
            findings.append(
                intake_finding(
                    "intake-created",
                    "info",
                    ask,
                    draft,
                    decision,
                    rule,
                    created_ticket_id=ticket_id,
                )
            )
            processed.add(ask.ask_id)
        if not dry_run and processed:
            updates[board_id] = frozenset(processed)
    return findings, updates


async def drain_intake(
    client: Any, board_id: str, processed_ids: frozenset[str]
) -> None:
    """Re-read immediately before drain so concurrent appends are preserved."""
    raw = await client.board_state_get(INTAKE_STATE_KEY)
    current = parse_intake(raw, board_id)
    remaining = [ask for ask in current if ask.ask_id not in processed_ids]
    state = raw.get("state") if isinstance(raw, Mapping) else None
    expected_value = state.get("value") if isinstance(state, Mapping) else None
    if not isinstance(expected_value, str):
        raise RuntimeError("coordinator_intake read did not return a string value")
    await client._call(  # noqa: SLF001 - narrow CAS is not yet public client API.
        "board_state_update",
        {
            "agent_name": client.agent_name,
            "key": INTAKE_STATE_KEY,
            "value": _serialize_intake(remaining),
            "expected_sha256": hashlib.sha256(
                expected_value.encode("utf-8")
            ).hexdigest(),
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
        effective_config=(
            source.get("effective_config", {})
            if isinstance(source.get("effective_config"), Mapping)
            else {}
        ),
        config_sources=(
            source.get("config_sources", {})
            if isinstance(source.get("config_sources"), Mapping)
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
    intake_updates: Mapping[str, frozenset[str]] | None = None,
) -> None:
    from pursers_client import BoardClient
    intake_updates = intake_updates or {}

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
                if board_id in intake_updates:
                    await drain_intake(client, board_id, intake_updates[board_id])
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
            if home_board in intake_updates:
                await drain_intake(
                    client, home_board, intake_updates[home_board]
                )


def _read_token(path_value: str) -> str:
    path = Path(path_value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("coordinator token path must name an existing absolute file")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("coordinator token file is empty")
    return token


def capability_scopes(token: str) -> frozenset[str]:
    """Read only the unverified scope hint; Central remains authoritative."""
    try:
        payload = token.split(".")[1]
        padding = "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload + padding))
        raw = claims.get("scope") if isinstance(claims, Mapping) else None
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        return frozenset()
    if isinstance(raw, str):
        return frozenset(raw.split())
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return frozenset(raw)
    return frozenset()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the fleet coordinator")
    parser.add_argument("--url", default=os.environ.get("ONBOARD_CENTRAL_URL", DEFAULT_URL))
    parser.add_argument("--token-path", default=os.environ.get("PURSERS_COORDINATOR_TOKEN_PATH") or os.environ.get("ONBOARD_TOKEN_FILE"))
    parser.add_argument("--home-board", default=os.environ.get("ONBOARD_BOARD_ID", "pursers"))
    parser.add_argument("--agent-name", default="coordinator-1")
    parser.add_argument("--mode", choices=("shadow", "active"), default="shadow")
    parser.add_argument("--stale-seconds", type=int, default=Thresholds.stale_seconds)
    parser.add_argument("--lease-warning-ratio", type=float, default=Thresholds.lease_warning_fraction)
    parser.add_argument("--grace-seconds", type=int, default=Thresholds.lease_grace_seconds)
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
    parser.add_argument("--abandoner-drops", type=int, default=Thresholds.repeat_abandon_count)
    parser.add_argument("--abandoner-window-days", type=int, default=7)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    intake = parser.add_mutually_exclusive_group()
    intake.add_argument("--enable-intake", dest="intake_enabled", action="store_true")
    intake.add_argument("--disable-intake", dest="intake_enabled", action="store_false")
    parser.set_defaults(intake_enabled=False)
    parser.add_argument(
        "--intake-auto-categories",
        default=",".join(DEFAULT_AUTO_CATEGORIES),
    )
    parser.add_argument(
        "--intake-always-ask-categories",
        default=",".join(DEFAULT_ALWAYS_ASK_CATEGORIES),
    )
    work_policy = parser.add_mutually_exclusive_group()
    work_policy.add_argument("--work-domain-always-ask", dest="work_domain_always_ask", action="store_true")
    work_policy.add_argument("--allow-work-domain-auto", dest="work_domain_always_ask", action="store_false")
    parser.set_defaults(work_domain_always_ask=True)
    parser.add_argument("--intake-rate-per-hour", type=int, default=INTAKE_RATE_LIMIT)
    parser.add_argument(
        "--integration-watch-since",
        help="Ignore closed-ticket integration checks before this ISO-8601 timestamp",
    )
    args = parser.parse_args(argv)
    raw_argv = list(argv or ())
    seen_flags = {item.split("=", 1)[0] for item in raw_argv if item.startswith("--")}
    flag_names = {
        "--stale-seconds": "stale_seconds",
        "--lease-warning-ratio": "lease_warning_ratio",
        "--grace-seconds": "grace_seconds",
        "--starved-seconds": "starved_seconds",
        "--critical-starved-seconds": "critical_starved_seconds",
        "--review-backlog-seconds": "review_backlog_seconds",
        "--abandoner-drops": "abandoner_drops",
        "--abandoner-window-days": "abandoner_window_days",
        "--integration-watch-since": "integration_watch_since",
        "--enable-intake": "intake_enabled",
        "--disable-intake": "intake_enabled",
        "--intake-auto-categories": "intake_auto_categories",
        "--intake-always-ask-categories": "intake_always_ask_categories",
        "--work-domain-always-ask": "work_domain_always_ask",
        "--allow-work-domain-auto": "work_domain_always_ask",
        "--intake-rate-per-hour": "intake_rate_per_hour",
    }
    args._explicit_config_flags = frozenset(
        name for flag, name in flag_names.items() if flag in seen_flags
    )
    if not args.token_path:
        parser.error("--token-path or PURSERS_COORDINATOR_TOKEN_PATH is required")
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be positive")
    if args.integration_watch_since and parse_time(args.integration_watch_since) is None:
        parser.error("--integration-watch-since must be an ISO-8601 timestamp")
    if any(
        not 10 <= value <= 86_400
        for value in (
            args.stale_seconds,
            args.grace_seconds,
            args.starved_seconds,
            args.critical_starved_seconds,
            args.review_backlog_seconds,
        )
    ):
        parser.error("coordinator thresholds must be positive")
    if not 0.1 <= args.lease_warning_ratio <= 1:
        parser.error("--lease-warning-ratio must be between 0.1 and 1")
    if not 1 <= args.abandoner_drops <= 20 or not 1 <= args.abandoner_window_days <= 365:
        parser.error("abandoner policy is out of range")
    if _csv_categories(args.intake_auto_categories) is None or _csv_categories(args.intake_always_ask_categories) is None:
        parser.error("intake categories must be fixed known category names")
    if set(_csv_categories(args.intake_auto_categories) or ()) | set(_csv_categories(args.intake_always_ask_categories) or ()) != set(INTAKE_CATEGORIES):
        parser.error("intake category policy must cover every known category")
    if set(_csv_categories(args.intake_auto_categories) or ()) & set(_csv_categories(args.intake_always_ask_categories) or ()):
        parser.error("intake auto and always-ask categories must be disjoint")
    if not 1 <= args.intake_rate_per_hour <= 20:
        parser.error("--intake-rate-per-hour must be between 1 and 20")
    return args


async def run(args: argparse.Namespace) -> None:
    token = _read_token(args.token_path)
    intake_authorized = INTAKE_SCOPE in capability_scopes(token)
    terms_path = os.environ.get("PURSERS_PRIVACY_TERMS")
    runtime = RuntimeState.for_mode("shadow" if args.dry_run else args.mode)
    degraded_streaks: dict[str, int] = {}
    while True:
        now = utc_now()
        async with RawReader(args.url, token) as reader:
            projects, snapshots, previous = await read_cycle(reader, args.home_board)
        live_config = resolve_coordinator_config(
            snapshots.get(args.home_board, {}).get("coordinator_config_state"), args
        )
        thresholds = live_config.thresholds
        terms = load_privacy_terms(terms_path, [project.work_dir for project in projects])
        states = analyze_cycle(
            projects,
            snapshots,
            previous,
            terms,
            now,
            integration_watch_since=live_config.integration_watch_since,
            thresholds=thresholds,
            effective_mode=runtime.effective_mode,
            degraded_streaks=degraded_streaks,
        )
        home_state = states.get(args.home_board)
        if home_state is not None:
            home_state["effective_config"] = live_config.effective
            home_state["config_sources"] = live_config.sources
        invalid_finding = config_invalid_finding(args.home_board, live_config)
        config_findings = [invalid_finding] if invalid_finding else []
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
            [*config_findings, *action_findings],
            histories,
            now,
            runtime.effective_mode,
        )
        intake_findings, intake_updates = await process_intakes(
            projects,
            snapshots,
            now,
            runtime,
            enabled=live_config.intake_enabled,
            dry_run=args.dry_run,
            create_ticket=lambda board_id, draft: create_intake_ticket(
                args.url,
                token,
                args.agent_name,
                board_id,
                draft,
            ),
            intake_authorized=intake_authorized,
            auto_categories=live_config.auto_categories,
            always_ask_categories=live_config.always_ask_categories,
            work_domain_always_ask=live_config.work_domain_always_ask,
            rate_per_hour=live_config.rate_per_hour,
        )
        states = merge_action_results(
            states,
            intake_findings,
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
            await write_reports(
                args.url,
                token,
                args.home_board,
                args.agent_name,
                states,
                previous,
                now,
                intake_updates,
            )
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
