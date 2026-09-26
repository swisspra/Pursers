#!/usr/bin/env python3
"""Loopback-only fleet dashboard and local API-worker controller."""

from __future__ import annotations

import argparse
import asyncio
import copy
import difflib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

import tomllib

# Prefer the sibling source checkout over any installed pursers-client wheel:
# the dashboard depends on keyword arguments newer than the last published wheel.
_CLIENT_SRC = Path(__file__).resolve().parents[2] / "packages" / "client" / "src"
if (_CLIENT_SRC / "pursers_client").is_dir():
    sys.path.insert(0, str(_CLIENT_SRC))
_CENTRAL_SRC = Path(__file__).resolve().parents[2] / "packages" / "central" / "src"
if (_CENTRAL_SRC / "pursers_central").is_dir():
    sys.path.insert(0, str(_CENTRAL_SRC))
from pursers_client import (
    BoardClient,
    BoardClientError,
    human_form_safety,
    parse_project_registry as parse_client_project_registry,
)
from pursers_central.scrub import Policy as BoardScrubPolicy
from pursers_central.scrub import scrub as board_scrub

_WAIT_BRIDGE_DIR = Path(__file__).resolve().parents[1] / "wait-bridge"
if (_WAIT_BRIDGE_DIR / "door_admin.py").is_file() and str(_WAIT_BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(_WAIT_BRIDGE_DIR))
import door_admin

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))
from seat_config import (  # noqa: I001
    BridgeInstaller,
    DesiredSeat,
    Doctor,
    DoctorCheck,
    PromptRenderer,
    SeatInventory,
    _load_seat_new,
    adapter_for,
    connector_skill_suggestions,
    discover_managed_seats,
)
from release_ops import ReleaseOpsManager
import runtime_environment
from warm_home import apply_warm_guided_home
from result_visibility import project_ticket_result
from evidence_trace import CORRELATION_HEADERS, EvidenceTrace, EvidenceTraceConfigError
from butler_settings import (
    ButlerSettingsError,
    ButlerSettingsManager,
    autonomous_butler_view,
    prepare_autonomous_butler_config,
    validate_board_butler_document,
    validate_autonomous_command_request,
)


DEFAULT_URL = "http://127.0.0.1:8766/mcp"
DEFAULT_HOME_BOARD = "pursers"
DISPATCH_ACTIVITY_WINDOW_MULTIPLIER = 3.0
SNAPSHOT_LIMIT = 1_000
DISPATCH_TICKET_LIMIT = 500
TICKET_LIST_LIMIT = 500
SNAPSHOT_MAX_BYTES = 300_000
EVENT_SCAN_LIMIT = 50
EVENT_MAX_BYTES = 100_000
DETAIL_EVENT_SCAN_LIMIT = 100
ROUTE_WINDOW_DAYS = 7
MAX_ROUTE_ROWS = 150
MAX_ROUTE_SEATS = 100
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
MAX_ANNOTATIONS_PER_TICKET = 50
MAX_ANNOTATION_TEXT_CHARS = 4_000
MAX_HANDOFF_MEMORIES = 500
MAX_FINDINGS = 50
MAX_FINDING_CHARS = 500
MAX_OVERHEAD_FILE_BYTES = 2_000_000
MAX_OVERHEAD_SEATS = 200
MAX_OVERHEAD_TOOLS = 5
OVERHEAD_DAYS = 7
ATTENTION_RETENTION_DAYS = 7
CONTEXT_ATTENTION_HOURS = 24
COORDINATOR_FINDINGS_STALE_MINUTES = 15
CONTEXT_STATS_ANOMALY_TOKENS = 1_000_000
WORKER_API_MAX_BYTES = 20_000
CONFIG_API_MAX_BYTES = 40_000
CONFIG_JOB_LIMIT = 100
CONFIG_OPS_PLAN_TTL_SECONDS = 120
CONFIG_PLAN_LIMIT = 50
GIT_TIMEOUT_SECONDS = 120
GIT_ERROR_TAIL_CHARS = 2_000
CONFIG_STATE_DIR = runtime_environment.dashboard_state_dir()
MAX_REVIEW_STATE_BYTES = 4_096
PROJECT_EVIDENCE_MAX_BYTES = 65_536
PROJECT_EVIDENCE_MAX_KEY_FILES = 512
PROJECT_EVIDENCE_MAX_KEY_BYTES = 65_536
REVIEW_STATE_SUFFIX = ".review-state.json"
WORKER_NAME_RE = re.compile(r"^[a-z0-9-]{2,32}$")
WORKER_KEYCHAIN_SERVICE = "pursers-worker"
WORKER_SECURITY_CLI = Path("/usr/bin/security")
WORKER_AUTH_SCHEME_PARTS = ("Bea", "rer")
DEFAULT_WORKERS_DIR = runtime_environment.pursers_state_root() / "workers"
DEFAULT_WORKER_SCRIPT = (
    Path(__file__).resolve().parents[1] / "worker-runtime" / "pursers_worker.py"
)
_IMPORTED_CONFIG_STATE_DIR = CONFIG_STATE_DIR
_IMPORTED_DEFAULT_WORKERS_DIR = DEFAULT_WORKERS_DIR
PROVIDER_PRESETS = {
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1", True),
    "qwen": (
        "Qwen / DashScope intl",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        True,
    ),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", True),
    "azure": (
        "Azure OpenAI",
        "https://resource-name.openai.azure.com/openai/deployments/deployment-name",
        True,
    ),
    "ollama": ("Ollama", "http://127.0.0.1:11434/v1", False),
    "custom": ("Custom", "", True),
}


def _default_path(
    configured: str | Path,
    imported: Path,
    dynamic: Callable[[], Path],
) -> Path:
    selected = dynamic() if Path(configured) == imported else Path(configured)
    return selected.expanduser().resolve()


def _default_workers_dir() -> Path:
    return _default_path(
        DEFAULT_WORKERS_DIR,
        _IMPORTED_DEFAULT_WORKERS_DIR,
        lambda: runtime_environment.pursers_state_root() / "workers",
    )


def _default_config_state_dir() -> Path:
    return _default_path(
        CONFIG_STATE_DIR,
        _IMPORTED_CONFIG_STATE_DIR,
        runtime_environment.dashboard_state_dir,
    )


def _default_butler_secrets_dir() -> Path:
    return (runtime_environment.pursers_state_root() / "board-butler" / "secrets").resolve()


BOARD_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
CENTRAL_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
URL_SCHEME_START_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ\u0130\u0131\u017f\u212a"
)
URL_SCHEME_CHARS = URL_SCHEME_START_CHARS | frozenset("0123456789+.-")
JWT_SEGMENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
DASHBOARD_AGENT_NAME_RE = re.compile(
    r"^fleet-dashboard-session-[a-z0-9][a-z0-9-]{0,39}$"
)
DASHBOARD_AGENT_PLATFORM = "pursers-fleet-dashboard"
DASHBOARD_TASK_FOCUS = "dashboard-session-owner-v1"
DASHBOARD_CAPABILITIES = {"can_work": False, "can_review": False}
ACTIVE_CLAIM_STATES = frozenset({"claimed", "in_progress", "creating_report"})
SUBMITTED_STATES = frozenset({"submitted", "reviewing", "in_review"})
TERMINAL_STATES = frozenset({"closed", "rejected", "canceled", "terminated"})
TICKET_NEXT_LABELS = {
    "open": "Claim when offered",
    "assigned": "Claim when offered",
    "claimed": "Submit for independent review",
    "in_progress": "Submit for independent review",
    "creating_report": "Submit for independent review",
    "submitted": "Complete independent review",
    "reviewing": "Complete independent review",
    "in_review": "Complete independent review",
    "rejected": "Address review feedback",
    "closed": "No next lifecycle action",
    "canceled": "No next lifecycle action",
    "terminated": "No next lifecycle action",
}
CONFIG_STATE_KEY = "coordinator_config"
INTAKE_STATE_KEY = "coordinator_intake"
FINDINGS_STATE_KEY = "coordinator_findings"
BUTLER_EVALUATION_STATE_PREFIX = "board_butler_evaluation."
AUTONOMOUS_FLEET_STATE_KEY = "autonomous_butler_state"
DASHBOARD_WRITE_KEYS = frozenset({CONFIG_STATE_KEY, INTAKE_STATE_KEY})
BUTLER_DRAFT_MARK_VALUES = ("send_as_is", "needed_edits", "wrong")
BUTLER_ROUTING_MARK_VALUES = (
    "correct_escalation",
    "should_have_answered",
    "should_have_escalated",
)
BUTLER_MARK_VALUES = BUTLER_DRAFT_MARK_VALUES + BUTLER_ROUTING_MARK_VALUES
BUTLER_MIN_AGREEMENT_SAMPLES = 3
INTAKE_TEXT_MIN_CHARS = 5
INTAKE_TEXT_MAX_CHARS = 500
INTAKE_RATE_LIMIT = 10
INTAKE_RATE_WINDOW_SECONDS = 3_600
MAX_INTAKE_ROWS = 1_000
INTAKE_DOCUMENT_SCHEMA_VERSION = 1
MAX_INTAKE_TOMBSTONES = 20
INTAKE_TITLE_MAX_CHARS = 200
MAX_INTAKE_DRAFT_EVIDENCE_CHARS = 4_000
CONFIG_CATEGORIES = (
    "docs",
    "tests",
    "audit-analysis",
    "bug",
    "production-code",
    "release-ci",
    "membership-roles",
    "board-registry",
)
CONFIG_THRESHOLD_FIELDS = (
    "stale_seconds",
    "lease_warning_ratio",
    "grace_seconds",
    "starved_seconds",
    "critical_starved_seconds",
    "review_backlog_seconds",
    "abandoner_drops",
    "abandoner_window_days",
)
CONFIG_PRESSURE_FIELDS = (
    "context_watch_tokens_per_poll",
    "context_compact_tokens_per_poll",
    "context_trend_compact_ratio",
)
DEFAULT_CONTEXT_PRESSURE = {
    "context_watch_tokens_per_poll": 30_000,
    "context_compact_tokens_per_poll": 80_000,
    "context_trend_compact_ratio": 1.5,
}

_BOARD_REDACTION_POLICY = BoardScrubPolicy(mode="redact")


def _board_redact(value: str) -> str:
    return board_scrub(value, _BOARD_REDACTION_POLICY)[0]


def _api_exception_payload(
    route: str,
    central: str,
    central_url: str | None,
    exc: BaseException,
) -> dict[str, str]:
    """Return and log a bounded, scrubbed leaf exception for local API failures."""
    leaf = exc
    while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
        leaf = leaf.exceptions[0]

    chain = [leaf]
    seen = {id(leaf)}
    while True:
        nested = leaf.__cause__ or leaf.__context__
        if nested is None or id(nested) in seen:
            break
        seen.add(id(nested))
        leaf = nested
        while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
            leaf = leaf.exceptions[0]
        chain.append(leaf)

    informative = next(
        (item for item in reversed(chain) if str(item).strip()), chain[-1]
    )
    error = type(informative).__name__
    messages = [str(item).strip() for item in chain if str(item).strip()]
    disconnected = any(
        "server disconnected without sending a response" in message.casefold()
        for message in messages
    )
    if (
        disconnected
        and central_url
        and urlsplit(central_url).scheme.casefold() == "http"
    ):
        safe_url = _board_redact(central_url)
        message = (
            f"Central at {safe_url} closed the connection before responding - "
            "if it serves TLS use https://"
        )
    else:
        message = _board_redact(str(informative).strip())
    if len(message) > 500:
        message = f"{message[:499]}…"
    detail = f"{error}: {message}" if message else error
    print(
        f"fleet-dashboard {route} central={central}: {detail}",
        file=sys.stderr,
        flush=True,
    )
    return {"error": error, "detail": detail, "central": central}


class ConfigConflictError(RuntimeError):
    """The dashboard form was based on missing or superseded state."""


class AddProjectPartialFailure(RuntimeError):
    """Bounded add-project progress for a failure after zero or more steps."""

    def __init__(
        self,
        completed_steps: list[dict[str, Any]],
        failed_step: str,
        status_code: int = 409,
    ) -> None:
        self.completed_steps = [
            {
                "step": str(item.get("step", ""))[:32],
                "status": str(item.get("status", ""))[:32],
            }
            for item in completed_steps[:8]
        ]
        self.failed_step = failed_step[:32]
        self.status_code = status_code
        super().__init__("add project could not complete")


class IntakeRateLimitError(RuntimeError):
    """The dashboard intake write rate exceeded its bounded hourly window."""


class FleetCloneGitError(ValueError):
    """A scrubbed git failure safe to return through the local dashboard API."""

    def __init__(self, subcommand: str, message: str) -> None:
        self.subcommand = subcommand
        super().__init__(_board_redact(message))


class FleetClient(Protocol):
    async def board_status(self, *, include_retired: bool = False) -> dict[str, Any]: ...

    async def board_dispatch_events(self, *, limit: int = 25) -> dict[str, Any]: ...

    async def board_state_get(self, key: str | None = None) -> dict[str, Any]: ...

    async def board_snapshot(
        self, *, limit: int | None = None, max_bytes: int | None = None,
        include_retired: bool = False,
    ) -> dict[str, Any]: ...

    async def agent_retire(
        self, target_agent_id: str | None = None
    ) -> dict[str, Any]: ...

    async def agent_retire_inert(self) -> dict[str, Any]: ...

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

    async def ticket_list(
        self,
        *,
        include_closed: bool = False,
        limit: int = 100,
        view: str | None = None,
    ) -> dict[str, Any]: ...

    async def board_dispatch_policy_set(
        self,
        *,
        offer_ttl_s: int,
        second_opinion: bool,
        fallback_broadcast: bool,
    ) -> dict[str, Any]: ...
    async def board_claim_ttl_set(self, claim_ttl_s: int) -> dict[str, Any]: ...
    async def ticket_get(self, ticket_id: str) -> dict[str, Any]: ...

    async def memory_read(
        self, *, memory_type: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]: ...


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


def _butler_evaluations(document: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if isinstance(document, Mapping) and isinstance(document.get("evaluation"), Mapping):
        rows = [document["evaluation"]]
    else:
        rows = document.get("evaluations", []) if isinstance(document, Mapping) else []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def butler_evaluation_state_key(question_id: str) -> str:
    if not isinstance(question_id, str) or not re.fullmatch(
        r"CQ-[0-9A-Za-z-]+", question_id
    ):
        raise ValueError("invalid question_id")
    return f"{BUTLER_EVALUATION_STATE_PREFIX}{question_id}"


def butler_agreement_report(
    evaluations: list[dict[str, Any]],
    *,
    complete: bool = True,
) -> list[dict[str, Any]]:
    """Report only explicit human marks; free text is never scored."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in evaluations:
        if row.get("mark") not in BUTLER_MARK_VALUES:
            continue
        mark = str(row["mark"])
        axis = (
            "draft_quality"
            if mark in BUTLER_DRAFT_MARK_VALUES
            else "routing_quality"
        )
        population = str(row.get("mark_population") or "unknown")
        grouped.setdefault(
            (str(row.get("question_kind", "unknown")), axis, population), []
        ).append(row)
    result: list[dict[str, Any]] = []
    for kind, axis, population in sorted(grouped):
        rows = grouped[(kind, axis, population)]
        values = (
            BUTLER_DRAFT_MARK_VALUES
            if axis == "draft_quality"
            else BUTLER_ROUTING_MARK_VALUES
        )
        counts = {
            mark: sum(row.get("mark") == mark for row in rows)
            for mark in values
        }
        enough = complete and len(rows) >= BUTLER_MIN_AGREEMENT_SAMPLES
        success_mark = (
            "send_as_is" if axis == "draft_quality" else "correct_escalation"
        )
        result.append(
            {
                "question_kind": kind,
                "axis": axis,
                "population": population,
                "sample_count": len(rows),
                "marks": counts,
                "status": (
                    "measured"
                    if enough
                    else "incomplete_bounded_state"
                    if not complete
                    else "insufficient_samples"
                ),
                "agreement_percent": (
                    round(100 * counts[success_mark] / len(rows), 1)
                    if enough
                    else None
                ),
                "first_marked_at": min(
                    (str(row["marked_at"]) for row in rows if row.get("marked_at")),
                    default=None,
                ),
                "last_marked_at": max(
                    (str(row["marked_at"]) for row in rows if row.get("marked_at")),
                    default=None,
                ),
            }
        )
    return result


def butler_agreement_by_ticket(
    evaluations: list[dict[str, Any]],
    *,
    complete: bool = True,
) -> list[dict[str, Any]]:
    remapped = [
        {**row, "question_kind": str(row.get("ticket_id", "unknown"))}
        for row in evaluations
    ]
    report = butler_agreement_report(remapped, complete=complete)
    for row in report:
        row["ticket_id"] = row.pop("question_kind")
    return report


def butler_multi_question_tickets(
    evaluations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, set[str]] = {}
    for row in evaluations:
        ticket_id = row.get("ticket_id")
        question_id = row.get("question_id")
        if isinstance(ticket_id, str) and ticket_id and isinstance(question_id, str):
            grouped.setdefault(ticket_id, set()).add(question_id)
    return [
        {"ticket_id": ticket_id, "question_count": len(question_ids)}
        for ticket_id, question_ids in sorted(
            grouped.items(), key=lambda item: (-len(item[1]), item[0])
        )
        if len(question_ids) > 1
    ]


def _identity_fields(identity: Any) -> dict[str, str]:
    result = {
        name: str(getattr(identity, name, ""))
        for name in ("agent_id", "agent_name", "principal_id")
    }
    if not all(result.values()):
        raise PermissionError("dashboard identity is incomplete")
    return result


def mark_butler_evaluation(
    document: Mapping[str, Any],
    *,
    ticket_id: str,
    question_id: str,
    mark: str,
    question: Mapping[str, Any],
    marker: Mapping[str, str],
    marked_at: str,
) -> dict[str, Any]:
    """Apply one immutable human mark after identity and answer checks."""
    if mark not in BUTLER_MARK_VALUES:
        raise ValueError("mark is not valid")
    if question.get("state") != "answered":
        raise ValueError("question must be answered before its draft can be marked")
    if question.get("question_id") != question_id or question.get("ticket_id") != ticket_id:
        raise ValueError("question identifiers do not match the paired record")
    answered_by = question.get("answered_by")
    if not isinstance(answered_by, Mapping):
        raise PermissionError("answered question has no authenticated answerer")
    if marker.get("principal_id") != answered_by.get("principal_id"):
        raise PermissionError("only the principal that answered may mark this draft")

    result = dict(document)
    rows = _butler_evaluations(result)
    offset = next(
        (
            index
            for index, row in enumerate(rows)
            if row.get("question_id") == question_id
            and row.get("ticket_id") == ticket_id
        ),
        None,
    )
    if offset is None:
        raise ValueError("paired draft record not found")
    producer = rows[offset].get("drafted_by")
    if not isinstance(producer, Mapping):
        raise ValueError("paired draft record has no producer identity")
    if marker.get("principal_id") == producer.get("principal_id"):
        raise PermissionError("the identity that produced a draft cannot mark it")
    draft_status = rows[offset].get("draft_status")
    allowed_marks = (
        BUTLER_DRAFT_MARK_VALUES + ("should_have_escalated",)
        if draft_status == "produced"
        else ("correct_escalation", "should_have_answered")
        if draft_status == "declined"
        else ()
    )
    if mark not in allowed_marks:
        raise ValueError(f"{mark} is not valid for {draft_status or 'unknown'} records")
    previous = rows[offset].get("mark")
    if previous is not None:
        if previous == mark and rows[offset].get("marked_by") == dict(marker):
            return result
        raise ConfigConflictError("draft already has its single human mark")
    rows[offset] = {
        **rows[offset],
        "mark": mark,
        "mark_population": "live_answerer",
        "marked_by": dict(marker),
        "answered_by": {
            name: str(answered_by.get(name, ""))
            for name in ("agent_id", "agent_name", "principal_id")
        },
        "marked_at": marked_at,
    }
    result["schema_version"] = 1
    result["evaluation"] = rows[0]
    result.pop("evaluations", None)
    result.pop("truncated", None)
    return result


def validate_intake_text(value: Any) -> str:
    """Return one bounded non-blank ask without changing its authored text."""
    if not isinstance(value, str):
        raise ValueError("text must be a string")  # noqa: TRY004 - public contract.
    text = value.strip()
    if not INTAKE_TEXT_MIN_CHARS <= len(text) <= INTAKE_TEXT_MAX_CHARS:
        raise ValueError("text must be between 5 and 500 characters")
    return text


def _dashboard_state_update_arguments(
    *,
    agent_name: str,
    key: str,
    value: str,
    expected_sha256: str | None = None,
) -> dict[str, str]:
    """Build the dashboard's only state mutation, guarded by an exact key set."""
    if key not in DASHBOARD_WRITE_KEYS and not re.fullmatch(
        rf"{re.escape(BUTLER_EVALUATION_STATE_PREFIX)}CQ-[0-9A-Za-z-]+", key
    ):
        raise ValueError("dashboard state key is not writable")
    arguments = {"agent_name": agent_name, "key": key, "value": value}
    if expected_sha256 is not None:
        arguments["expected_sha256"] = expected_sha256
    return arguments


def _intake_state_value(
    raw: Any, board_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Parse and preserve the coordinator-compatible intake queue."""
    state = raw.get("state") if isinstance(raw, dict) else None
    value = state.get("value") if isinstance(state, dict) else None
    if value is None:
        return [], [], None
    if not isinstance(value, str):
        raise ConfigConflictError("coordinator_intake state is malformed")
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigConflictError("coordinator_intake state is malformed") from exc
    if isinstance(document, list):
        rows, tombstones = document, []
    elif (
        isinstance(document, dict)
        and set(document) == {"schema_version", "asks", "tombstones"}
        and document.get("schema_version") == INTAKE_DOCUMENT_SCHEMA_VERSION
        and isinstance(document.get("asks"), list)
        and isinstance(document.get("tombstones"), list)
    ):
        rows, tombstones = document["asks"], document["tombstones"]
    else:
        raise ConfigConflictError("coordinator_intake state is malformed")
    if len(rows) > MAX_INTAKE_ROWS or len(tombstones) > MAX_INTAKE_TOMBSTONES:
        raise ConfigConflictError("coordinator_intake state is malformed")
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ConfigConflictError("coordinator_intake state is malformed")
        required = {
            name: row.get(name) for name in ("id", "text", "requested_by", "board_id")
        }
        if not all(
            isinstance(item, str) and item.strip() for item in required.values()
        ):
            raise ConfigConflictError("coordinator_intake state is malformed")
        ask_id = required["id"].strip()
        if ask_id in seen or required["board_id"].strip() != board_id:
            raise ConfigConflictError("coordinator_intake state is malformed")
        approved = row.get("approved", False)
        approval_values = (row.get("approved_by"), row.get("approved_at"))
        approved_title = row.get("approved_title")
        if type(approved) is not bool or (
            approved
            and not all(
                isinstance(item, str) and item.strip() for item in approval_values
            )
        ):
            raise ConfigConflictError("coordinator_intake state is malformed")
        if not approved and any(
            item is not None for item in (*approval_values, approved_title)
        ):
            raise ConfigConflictError("coordinator_intake state is malformed")
        if approved_title is not None and (
            not isinstance(approved_title, str)
            or not approved_title.strip()
            or len(approved_title.strip()) > INTAKE_TITLE_MAX_CHARS
        ):
            raise ConfigConflictError("coordinator_intake state is malformed")
        seen.add(ask_id)
        clean.append(json.loads(json.dumps(row)))
    clean_tombstones: list[dict[str, Any]] = []
    tombstone_ids: set[str] = set()
    for row in tombstones:
        if not isinstance(row, dict):
            raise ConfigConflictError("coordinator_intake state is malformed")
        required = {
            name: row.get(name)
            for name in ("id", "text", "board_id", "declined_by", "declined_at")
        }
        if not all(
            isinstance(item, str) and item.strip() for item in required.values()
        ):
            raise ConfigConflictError("coordinator_intake state is malformed")
        ask_id = required["id"].strip()
        if (
            ask_id in seen
            or ask_id in tombstone_ids
            or required["board_id"].strip() != board_id
        ):
            raise ConfigConflictError("coordinator_intake state is malformed")
        tombstone_ids.add(ask_id)
        clean_tombstones.append(json.loads(json.dumps(row)))
    return clean, clean_tombstones, value


def _encode_intake_document(
    rows: list[dict[str, Any]], tombstones: list[dict[str, Any]]
) -> str:
    return json.dumps(
        {
            "schema_version": INTAKE_DOCUMENT_SCHEMA_VERSION,
            "asks": rows,
            "tombstones": tombstones[-MAX_INTAKE_TOMBSTONES:],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validated_intake_draft(raw: Any, ask_id: str) -> dict[str, str] | None:
    """Return one bounded, matching coordinator draft or fail closed."""
    state = raw.get("state") if isinstance(raw, dict) else None
    value = state.get("value") if isinstance(state, dict) else None
    if isinstance(value, str):
        try:
            document = json.loads(value)
        except json.JSONDecodeError:
            return None
    else:
        document = value
    if isinstance(document, list):
        findings = document
    elif isinstance(document, dict):
        findings = document.get("findings", document.get("items"))
    else:
        return None
    if not isinstance(findings, list):
        return None
    matches: list[dict[str, str]] = []
    for finding in findings[:MAX_FINDINGS]:
        if (
            not isinstance(finding, dict)
            or finding.get("kind") != "intake-pending"
            or finding.get("ask_id") != ask_id
        ):
            continue
        evidence = finding.get("evidence")
        if (
            not isinstance(evidence, str)
            or len(evidence) > MAX_INTAKE_DRAFT_EVIDENCE_CHARS
        ):
            continue
        try:
            evidence_document = json.loads(evidence)
        except json.JSONDecodeError:
            continue
        if not isinstance(evidence_document, dict):
            continue
        draft = evidence_document.get("draft")
        title = draft.get("title") if isinstance(draft, dict) else None
        category = evidence_document.get("category")
        if (
            evidence_document.get("ask_id") != ask_id
            or evidence_document.get("decision") != "ask"
            or not isinstance(title, str)
            or not title.strip()
            or len(title.strip()) > INTAKE_TITLE_MAX_CHARS
            or category not in CONFIG_CATEGORIES
        ):
            continue
        matches.append({"title": title.strip(), "category": category})
    return matches[0] if len(matches) == 1 else None


def validate_coordinator_config(value: Any) -> dict[str, Any]:
    """Validate the complete dashboard-owned value; no arbitrary state keys pass."""
    required_fields = {
        "schema_version",
        "thresholds",
        "integration_watch_since",
        "intake",
    }
    if (
        not isinstance(value, dict)
        or not required_fields.issubset(value)
        or not set(value).issubset(required_fields | {"board_butler"})
    ):
        raise ValueError("config must contain only the coordinator and board_butler schema fields")
    if value.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    thresholds = value.get("thresholds")
    threshold_keys = set(thresholds) if isinstance(thresholds, dict) else set()
    required_thresholds = set(CONFIG_THRESHOLD_FIELDS)
    allowed_thresholds = required_thresholds | set(CONFIG_PRESSURE_FIELDS)
    if (
        not isinstance(thresholds, dict)
        or not required_thresholds.issubset(threshold_keys)
        or not threshold_keys.issubset(allowed_thresholds)
    ):
        raise ValueError("thresholds must contain every required known threshold")
    for name in (
        "stale_seconds",
        "grace_seconds",
        "starved_seconds",
        "critical_starved_seconds",
        "review_backlog_seconds",
    ):
        if type(thresholds[name]) is not int or not 10 <= thresholds[name] <= 86_400:
            raise ValueError(f"{name} must be between 10 and 86400")
    ratio = thresholds["lease_warning_ratio"]
    if type(ratio) not in (int, float) or not 0.1 <= ratio <= 1:
        raise ValueError("lease_warning_ratio must be between 0.1 and 1")
    if (
        type(thresholds["abandoner_drops"]) is not int
        or not 1 <= thresholds["abandoner_drops"] <= 20
    ):
        raise ValueError("abandoner_drops must be between 1 and 20")
    if (
        type(thresholds["abandoner_window_days"]) is not int
        or not 1 <= thresholds["abandoner_window_days"] <= 365
    ):
        raise ValueError("abandoner_window_days must be between 1 and 365")
    pressure = context_pressure_thresholds(thresholds)
    for name in CONFIG_PRESSURE_FIELDS:
        if name in thresholds and thresholds[name] != pressure[name]:
            raise ValueError(f"{name} is invalid")
    if set(CONFIG_PRESSURE_FIELDS) & threshold_keys and (
        pressure["context_compact_tokens_per_poll"]
        <= pressure["context_watch_tokens_per_poll"]
    ):
        raise ValueError("context compact threshold must exceed watch threshold")
    watermark = value.get("integration_watch_since")
    if watermark is not None and _parse_time(watermark) is None:
        raise ValueError("integration_watch_since must be null or ISO-8601")
    intake = value.get("intake")
    required_intake_fields = {
        "enabled",
        "auto_categories",
        "always_ask_categories",
        "work_domain_always_ask",
        "rate_per_hour",
    }
    intake_fields = set(intake) if isinstance(intake, dict) else set()
    if (
        not isinstance(intake, dict)
        or not required_intake_fields.issubset(intake_fields)
        or not intake_fields.issubset(required_intake_fields | {"token_path"})
    ):
        raise ValueError("intake must contain every known intake field")
    if (
        type(intake["enabled"]) is not bool
        or type(intake["work_domain_always_ask"]) is not bool
    ):
        raise ValueError("intake switches must be booleans")
    auto, always = intake["auto_categories"], intake["always_ask_categories"]
    if not all(
        isinstance(rows, list) and all(type(item) is str for item in rows)
        for rows in (auto, always)
    ):
        raise ValueError("intake categories must be arrays")
    if len(set(auto)) != len(auto) or len(set(always)) != len(always):
        raise ValueError("intake categories must not contain duplicates")
    if set(auto) & set(always) or set(auto) | set(always) != set(CONFIG_CATEGORIES):
        raise ValueError("intake categories must be known, disjoint, and complete")
    if (
        type(intake["rate_per_hour"]) is not int
        or not 1 <= intake["rate_per_hour"] <= 20
    ):
        raise ValueError("rate_per_hour must be between 1 and 20")
    token_path = intake.get("token_path")
    if token_path is not None and (
        not isinstance(token_path, str)
        or not token_path
        or token_path != token_path.strip()
        or len(token_path) > 4_096
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token_path)
        or not Path(token_path).is_absolute()
        or ".." in Path(token_path).parts
    ):
        raise ValueError("intake.token_path must be null or a safe absolute path")
    clean = json.loads(json.dumps(value))
    if "board_butler" in clean:
        try:
            clean["board_butler"] = validate_board_butler_document(
                clean["board_butler"]
            )
        except ButlerSettingsError as exc:
            raise ValueError(str(exc)) from exc
    return clean


def context_pressure_thresholds(value: Any) -> dict[str, int | float]:
    """Resolve optional coordinator_config pressure keys independently."""
    raw = value if isinstance(value, dict) else {}
    watch = raw.get("context_watch_tokens_per_poll")
    compact = raw.get("context_compact_tokens_per_poll")
    ratio = raw.get("context_trend_compact_ratio")
    resolved: dict[str, int | float] = dict(DEFAULT_CONTEXT_PRESSURE)
    if type(watch) is int and 1_000 <= watch <= 10_000_000:
        resolved["context_watch_tokens_per_poll"] = watch
    if type(compact) is int and 1_001 <= compact <= 20_000_000:
        resolved["context_compact_tokens_per_poll"] = compact
    if type(ratio) in (int, float) and 1.01 <= ratio <= 10:
        resolved["context_trend_compact_ratio"] = float(ratio)
    if (
        resolved["context_compact_tokens_per_poll"]
        <= resolved["context_watch_tokens_per_poll"]
    ):
        return dict(DEFAULT_CONTEXT_PRESSURE)
    return resolved


@dataclass(frozen=True)
class Config:
    url: str
    token: str
    home_board: str
    agent_name: str
    stale_seconds: int
    cache_seconds: float
    label: str = "default"
    overhead_path: Path | None = None
    doors_keys_dir: Path | None = None
    jwks_path: Path | None = None


def _worker_text(value: Any, label: str, *, limit: int = 500) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > limit
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{label} must be a non-empty bounded string")
    return value.strip()


def validate_worker_url(value: Any) -> str:
    raw = _worker_text(value, "base_url", limit=1_000).rstrip("/")
    parsed = urlsplit(raw)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be an http(s) URL without credentials/query")
    if parsed.scheme == "http" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("base_url must use https unless it is loopback")
    return raw


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _worker_config_bytes(
    *,
    name: str,
    provider: str,
    base_url: str,
    model: str,
    role: str,
    max_tier: str,
    central_url: str,
    token_path: Path,
    log_path: Path,
) -> bytes:
    key_line = (
        "" if provider == "ollama" else f"api_key_keychain = {_toml_string(name)}\n"
    )
    text = (
        "# Generated by the loopback Fleet Dashboard. Contains no API key.\n"
        'boards = "registry"\n'
        f"log_file = {_toml_string(str(log_path))}\n\n"
        "[seat]\n"
        f"agent_name = {_toml_string(name)}\n"
        f"role = {_toml_string(role)}\n"
        f"central_url = {_toml_string(central_url)}\n"
        f"token_file = {_toml_string(str(token_path))}\n\n"
        "[claim]\n"
        f"max_tier = {_toml_string(max_tier)}\n\n"
        "[llm]\n"
        f"provider_label = {_toml_string(PROVIDER_PRESETS[provider][0])}\n"
        f"base_url = {_toml_string(base_url)}\n"
        f"{key_line}"
        f"model = {_toml_string(model)}\n"
    )
    return text.encode("utf-8")


class WorkerManager:
    """Local worker config, Keychain, and child-process lifecycle manager."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        worker_script: str | Path = DEFAULT_WORKER_SCRIPT,
        platform: str | None = None,
        command_runner: Callable[..., Any] = subprocess.run,
        process_factory: Callable[..., Any] = subprocess.Popen,
        process_matches: Callable[[int, Path], bool] | None = None,
        process_provider: Callable[..., runtime_environment.ProcessInspection]
        | None = None,
    ) -> None:
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else _default_workers_dir()
        )
        self.worker_script = Path(worker_script).expanduser().resolve()
        self.platform = sys.platform if platform is None else platform
        self.command_runner = command_runner
        self.process_factory = process_factory
        self.process_provider = (
            process_provider or runtime_environment.PROCESS_LIST_PROVIDER
        )
        self.process_matches = process_matches or self._default_process_matches
        self._process_inspection_available = True
        self._children: dict[str, Any] = {}
        self._lock = threading.RLock()

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    @property
    def enabled(self) -> bool:
        return self.platform == "darwin"

    @property
    def roles(self) -> tuple[str, ...]:
        """Expose reviewer only when the installed runtime advertises it."""
        try:
            raw = self.worker_script.read_bytes()
        except OSError:
            return ("worker",)
        if len(raw) > 1_000_000:
            return ("worker",)
        text = raw.decode("utf-8", errors="ignore")
        reviewer_literal = '"reviewer"' in text or "'reviewer'" in text
        seat_role_read = 'seat.get("role"' in text or "seat.get('role'" in text
        if reviewer_literal and seat_role_read:
            return ("worker", "reviewer")
        return ("worker",)

    def _require_macos(self) -> None:
        if not self.enabled:
            raise RuntimeError("API worker management is available only on macOS")

    def _config_path(self, name: str) -> Path:
        return self.root / f"{name}.toml"

    def _pid_path(self, name: str) -> Path:
        return self.root / f"{name}.pid"

    def _token_path(self, name: str) -> Path:
        return self.root.parent / "seats" / f"{name}.jwt"

    def _log_path(self, name: str) -> Path:
        return self.root / f"{name}.session.log"

    def _review_state_path(self, name: str) -> Path:
        log_path = self._log_path(name)
        return log_path.with_name(log_path.name + REVIEW_STATE_SUFFIX)

    def _read_definition(self, path: Path) -> dict[str, Any]:
        raw = path.read_bytes()
        document = tomllib.loads(raw.decode("utf-8"))
        seat = document.get("seat")
        llm = document.get("llm")
        if not isinstance(seat, dict) or not isinstance(llm, dict):
            raise ValueError(  # noqa: TRY004 - persisted config validation.
                "worker config is malformed"
            )
        name = _worker_text(seat.get("agent_name"), "seat.agent_name", limit=32)
        if not WORKER_NAME_RE.fullmatch(name):
            raise ValueError("worker name is invalid")
        provider_label = _worker_text(
            llm.get("provider_label", "Custom"), "llm.provider_label", limit=80
        )
        base_url = validate_worker_url(llm.get("base_url"))
        return {
            "name": name,
            "provider_label": provider_label,
            "base_url": base_url,
            "base_url_host": urlsplit(base_url).hostname or "",
            "model": _worker_text(llm.get("model"), "llm.model", limit=200),
            "role": _worker_text(seat.get("role", "worker"), "seat.role", limit=16),
            "max_tier": _worker_text(
                document.get("claim", {}).get("max_tier", "heavy"),
                "claim.max_tier",
                limit=16,
            ),
            "api_key_keychain": llm.get("api_key_keychain"),
            "token_path": str(self._token_path(name)),
        }

    def _write_private(self, path: Path, payload: bytes) -> None:
        self._ensure_root()
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _keychain_add(self, name: str, api_key: str) -> None:
        try:
            self.command_runner(
                [
                    str(WORKER_SECURITY_CLI),
                    "add-generic-password",
                    "-s",
                    WORKER_KEYCHAIN_SERVICE,
                    "-a",
                    name,
                    "-U",
                    "-w",
                    api_key,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("macOS Keychain storage failed") from exc

    def _keychain_read(self, name: str) -> str:
        try:
            result = self.command_runner(
                [
                    str(WORKER_SECURITY_CLI),
                    "find-generic-password",
                    "-s",
                    WORKER_KEYCHAIN_SERVICE,
                    "-a",
                    name,
                    "-w",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError("API key is unavailable in macOS Keychain") from exc
        value = str(result.stdout).strip()
        if not value:
            raise RuntimeError("API key is empty in macOS Keychain")
        return value

    def save(self, value: Any, central_url: str) -> dict[str, Any]:
        self._require_macos()
        required = {"name", "provider", "base_url", "model", "api_key"}
        allowed = required | {"role", "max_tier"}
        if (
            not isinstance(value, dict)
            or not required.issubset(value)
            or not set(value).issubset(allowed)
        ):
            raise ValueError("worker request contains unexpected fields")
        name = _worker_text(value.get("name"), "name", limit=32)
        if not WORKER_NAME_RE.fullmatch(name):
            raise ValueError("name must match [a-z0-9-]{2,32}")
        provider = _worker_text(value.get("provider"), "provider", limit=32)
        if provider not in PROVIDER_PRESETS:
            raise ValueError("provider preset is invalid")
        base_url = validate_worker_url(value.get("base_url"))
        preset_url = PROVIDER_PRESETS[provider][1]
        if provider not in {"custom", "azure"} and base_url != preset_url:
            raise ValueError("base_url does not match the selected provider preset")
        model = _worker_text(value.get("model"), "model", limit=200)
        role = _worker_text(value.get("role", "worker"), "role", limit=16)
        if role not in self.roles:
            raise ValueError("role is unavailable in the installed worker runtime")
        max_tier = _worker_text(value.get("max_tier", "heavy"), "max_tier", limit=16)
        if max_tier not in {"light", "standard", "heavy"}:
            raise ValueError("max_tier must be light, standard, or heavy")
        key_required = PROVIDER_PRESETS[provider][2]
        raw_key = value.get("api_key")
        if not isinstance(raw_key, str) or len(raw_key) > 8_192:
            raise ValueError("api_key must be a bounded string")
        if key_required and not raw_key:
            raise ValueError("api_key is required for this provider")
        if not key_required and raw_key:
            raise ValueError("Ollama must not store an API key")
        central_url = validate_worker_url(central_url)
        with self._lock:
            if self.status(name)["running"]:
                raise ValueError("stop the worker before updating its config")
            if key_required:
                self._keychain_add(name, raw_key)
            payload = _worker_config_bytes(
                name=name,
                provider=provider,
                base_url=base_url,
                model=model,
                role=role,
                max_tier=max_tier,
                central_url=central_url,
                token_path=self._token_path(name),
                log_path=self._log_path(name),
            )
            self._write_private(self._config_path(name), payload)
        return {"ok": True, "name": name, "key_stored": key_required}

    def _read_pid(self, name: str) -> int | None:
        try:
            path = self._pid_path(name)
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                return None
            document = json.loads(path.read_text(encoding="utf-8"))
            pid = document.get("pid") if isinstance(document, dict) else None
            return pid if type(pid) is int and pid > 1 else None
        except (OSError, ValueError, UnicodeError):
            return None

    def _default_process_matches(self, pid: int, config_path: Path) -> bool:
        result = self.process_provider(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            runner=self.command_runner,
        )
        self._process_inspection_available = result.available
        if not result.available:
            return False
        command = str(result.stdout)
        return str(self.worker_script) in command and str(config_path) in command

    def status(self, name: str) -> dict[str, Any]:
        child = self._children.get(name)
        if child is not None:
            if child.poll() is None:
                return {"running": True, "pid": int(child.pid), "adopted": False}
            self._children.pop(name, None)
        pid = self._read_pid(name)
        if pid is not None and self.process_matches(pid, self._config_path(name)):
            return {"running": True, "pid": pid, "adopted": True}
        if pid is not None and not self._process_inspection_available:
            return {
                "running": False,
                "pid": pid,
                "adopted": False,
                "process_inspection": runtime_environment.PROCESS_INSPECTION_UNAVAILABLE,
            }
        self._pid_path(name).unlink(missing_ok=True)
        return {"running": False, "pid": None, "adopted": False}

    def adopt_orphans(self) -> None:
        for path in self.root.glob("*.toml"):
            name = path.stem
            if WORKER_NAME_RE.fullmatch(name):
                self.status(name)

    def list(self, seat_names: set[str] | None = None) -> list[dict[str, Any]]:
        self._require_macos()
        seats = seat_names or set()
        rows = []
        with self._lock:
            for path in sorted(self.root.glob("*.toml")):
                try:
                    definition = self._read_definition(path)
                except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError):
                    continue
                state = self.status(definition["name"])
                name = definition["name"]
                rows.append(
                    {
                        **definition,
                        **state,
                        "seat_exists": name in seats,
                        "seat_admin_command": (
                            "python <TOOLS_DIR>/wait-bridge/seat_admin.py add "
                            f"--name {name} --role {definition['role']} --boards registry "
                            "--principal <PRINCIPAL_ID> "
                            f"--token-path ~/.pursers/seats/{name}.jwt"
                        ),
                    }
                )
        return rows

    def log_tail(self, name: str, *, max_lines: int = 20) -> list[str]:
        """Read only a bounded tail from a managed worker's local log."""
        if not WORKER_NAME_RE.fullmatch(name):
            raise ValueError("worker name is invalid")
        path = self._log_path(name)
        try:
            if path.is_symlink() or not path.is_file():
                return []
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - 32_768))
                raw = stream.read(32_768)
        except OSError:
            return []
        lines = raw.decode("utf-8", errors="replace").splitlines()
        saw_lifecycle, active = _review_state_after_log(
            lines, initial=self.active_review(name)
        )
        if saw_lifecycle:
            self._store_active_review(name, active)
        return [
            line[:500]
            for line in lines[-max_lines:]
        ]

    def active_review(self, name: str) -> dict[str, str] | None:
        """Read one bounded durable reviewer lifecycle marker."""
        if not WORKER_NAME_RE.fullmatch(name):
            raise ValueError("worker name is invalid")
        path = self._review_state_path(name)
        try:
            details = path.stat()
            if (
                path.is_symlink()
                or not stat.S_ISREG(details.st_mode)
                or stat.S_IMODE(details.st_mode) != 0o600
                or details.st_size > MAX_REVIEW_STATE_BYTES
            ):
                return None
            document = json.loads(path.read_bytes())
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict) or document.get("schema") != 1:
            return None
        return _review_identity(document)

    def _store_active_review(
        self, name: str, active: dict[str, str] | None
    ) -> None:
        path = self._review_state_path(name)
        if active is None:
            path.unlink(missing_ok=True)
            return
        self._write_private(
            path,
            _json_bytes({"schema": 1, **active}),
        )

    def _fence_active_review(self, name: str, reason: str) -> None:
        """Append a bounded local lifecycle fence before clearing review state."""
        # Lazy-root contract: the fence may be the first private write (e.g.
        # an idempotent stop on a never-created worker), so create the root
        # on demand instead of assuming construction made it.
        self._ensure_root()
        path = self._log_path(name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(
                    _json_bytes(
                        {
                            "event": "review_session_reset",
                            "reason": _clip(reason, 80),
                        }
                    )
                    + b"\n"
                )
            os.chmod(path, 0o600)
        finally:
            self._store_active_review(name, None)

    def restart(self, name: str, *, seat_exists: bool) -> dict[str, Any]:
        self.stop(name)
        return self.start(name, seat_exists=seat_exists)

    def start(self, name: str, *, seat_exists: bool) -> dict[str, Any]:
        self._require_macos()
        if not WORKER_NAME_RE.fullmatch(name):
            raise ValueError("worker name is invalid")
        with self._lock:
            config_path = self._config_path(name)
            if not config_path.is_file():
                raise KeyError(name)
            current = self.status(name)
            if current["running"]:
                return {"ok": True, "name": name, **current}
            if not seat_exists:
                raise ValueError(
                    "seat missing — run the shown seat_admin command first"
                )
            if not self._token_path(name).is_file():
                raise ValueError("seat token file is missing")
            self._fence_active_review(name, "managed_start")
            child = self.process_factory(
                [sys.executable, str(self.worker_script), str(config_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            self._children[name] = child
            self._write_private(
                self._pid_path(name),
                _json_bytes({"pid": int(child.pid), "name": name}) + b"\n",
            )
            return {"ok": True, "name": name, "running": True, "pid": child.pid}

    def stop(self, name: str) -> dict[str, Any]:
        self._require_macos()
        if not WORKER_NAME_RE.fullmatch(name):
            raise ValueError("worker name is invalid")
        with self._lock:
            current = self.status(name)
            if not current["running"]:
                self._fence_active_review(name, "managed_stopped")
                return {"ok": True, "name": name, "running": False}
            child = self._children.get(name)
            if child is not None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("worker did not stop after SIGTERM")
                self._children.pop(name, None)
            else:
                os.kill(int(current["pid"]), signal.SIGTERM)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if not self.process_matches(
                        int(current["pid"]), self._config_path(name)
                    ):
                        break
                    time.sleep(0.05)
                else:
                    raise RuntimeError("adopted worker did not stop after SIGTERM")
            self._pid_path(name).unlink(missing_ok=True)
            self._fence_active_review(name, "managed_stop")
            return {"ok": True, "name": name, "running": False}

    def test_provider(self, name: str) -> dict[str, Any]:
        self._require_macos()
        if not WORKER_NAME_RE.fullmatch(name):
            raise ValueError("worker name is invalid")
        definition = self._read_definition(self._config_path(name))
        headers = {"Accept": "application/json"}
        if definition["api_key_keychain"]:
            api_key = self._keychain_read(name)
            headers["Authorization"] = "".join(WORKER_AUTH_SCHEME_PARTS) + " " + api_key
        request = urllib.request.Request(
            definition["base_url"] + "/models", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("provider test returned a non-success status")
                response.read(1_000)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise RuntimeError("provider test failed") from exc
        return {"ok": True, "name": name, "provider_reachable": True}


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
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def deployment_metadata(source: Path | None = None) -> dict[str, Any]:
    """Return the immutable checkout revision without exposing repository paths."""
    repository = (source or Path(__file__)).resolve().parents[2]
    try:
        revision = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--verify", "HEAD^{commit}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ValueError("git returned a non-commit revision")
        dirty = bool(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "status",
                    "--porcelain",
                    "--untracked-files=no",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            ).stdout
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"schema_version": 1, "running_sha": None, "dirty": None}
    return {"schema_version": 1, "running_sha": revision, "dirty": dirty}


def _bounded_evidence_digest(value: Any, label: str) -> str:
    """Digest one bounded state value without exposing its contents."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > PROJECT_EVIDENCE_MAX_BYTES:
        raise ValueError(f"{label} exceeds evidence byte cap")
    return hashlib.sha256(encoded).hexdigest()


def _door_evidence_digests(config: Config, board_id: str) -> dict[str, str]:
    """Digest board-scoped public credentials and the bounded key store."""
    keys_dir = config.doors_keys_dir
    jwks_path = config.jwks_path
    if keys_dir is None or jwks_path is None:
        raise ValueError("door credential storage is not configured")
    resolved_keys = Path(keys_dir).expanduser().resolve()
    resolved_jwks = Path(jwks_path).expanduser().resolve()
    if resolved_jwks.exists():
        info = resolved_jwks.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > PROJECT_EVIDENCE_MAX_BYTES:
            raise ValueError("door credential JWKS is not a bounded regular file")
        document = json.loads(resolved_jwks.read_text(encoding="utf-8"))
    else:
        document = {"keys": []}
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise ValueError("door credential JWKS is invalid")
    scoped_credentials = [
        item
        for item in document["keys"]
        if isinstance(item, dict)
        and isinstance(item.get(door_admin.METADATA_KEY), dict)
        and item[door_admin.METADATA_KEY].get("board") == board_id
    ]

    key_inventory: list[dict[str, Any]] = []
    if resolved_keys.exists():
        key_paths = sorted(resolved_keys.iterdir(), key=lambda item: item.name)
        if len(key_paths) > PROJECT_EVIDENCE_MAX_KEY_FILES:
            raise ValueError("door key inventory exceeds evidence file cap")
        for path in key_paths:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > PROJECT_EVIDENCE_MAX_KEY_BYTES
                or path.parent != resolved_keys
            ):
                raise ValueError("door key inventory contains an unsafe file")
            key_inventory.append({
                "name_sha256": hashlib.sha256(path.name.encode()).hexdigest(),
                "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    return {
        "credentials": _bounded_evidence_digest(
            scoped_credentials, "door credential state"
        ),
        "keys": _bounded_evidence_digest(key_inventory, "door key state"),
    }


def _door_material_digest(config: Config, board_id: str) -> str:
    """Hash exact persisted door material without returning its bytes or paths."""
    keys_dir = config.doors_keys_dir
    jwks_path = config.jwks_path
    if keys_dir is None or jwks_path is None:
        raise ValueError("door credential storage is not configured")
    resolved_keys = Path(keys_dir).expanduser().resolve()
    resolved_jwks = Path(jwks_path).expanduser().resolve()
    document = (
        json.loads(resolved_jwks.read_text(encoding="utf-8"))
        if resolved_jwks.exists()
        else {"keys": []}
    )
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise ValueError("door credential JWKS is invalid")
    material: list[dict[str, Any]] = []
    for item in document["keys"]:
        metadata = item.get(door_admin.METADATA_KEY) if isinstance(item, dict) else None
        kid = item.get("kid") if isinstance(item, dict) else None
        if (
            not isinstance(metadata, dict)
            or metadata.get("board") != board_id
            or metadata.get("role") not in door_admin.VALID_ROLES
            or not isinstance(kid, str)
            or not door_admin.KID_RE.fullmatch(kid)
        ):
            continue
        key_path = resolved_keys / f"{kid}.pem"
        info = key_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or key_path.parent != resolved_keys
        ):
            raise ValueError("door credential key is not a private regular file")
        material.append({
            "kid": kid,
            "role": metadata["role"],
            "public_jwk": item,
            "private_key_sha256": hashlib.sha256(key_path.read_bytes()).hexdigest(),
        })
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def bridge_stats_path() -> Path:
    configured = os.environ.get("PURSERS_BRIDGE_STATS", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path(__file__).resolve().parents[1] / "wait-bridge" / "bridge-stats.json"
    )


def _nonnegative_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


HUMAN_DISPOSITION_VALUES = ("reopen", "park", "cancel")
HUMAN_ACTION_VALUES = ("accept", "decline", "cancel")


def _pending_human_records(record: Any) -> list[dict[str, Any]]:
    """Unresolved human_request records carried by a needs_human ticket."""
    records = record if isinstance(record, list) else [record]
    pending: list[dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("resolution") is not None:
            continue
        request_id = rec.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            continue
        pending.append(rec)
    return pending


def read_overhead_stats(
    path: str | Path,
    *,
    now: datetime | None = None,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a bounded size/count-only projection; bad files become empty state."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = current.date().isoformat()
    pressure_thresholds = context_pressure_thresholds(thresholds)
    empty = {
        "generated_at": current.isoformat(),
        "today": today,
        "source_status": "missing",
        "note": "protocol overhead (estimated), not provider billing",
        "question": "Is this session's board context bloating; should we compact?",
        "pressure_thresholds": pressure_thresholds,
        "sessions": [],
        "seats": [],
        "model_wait": [],
        "push_unavailable": [],
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
    if not isinstance(document, dict) or document.get("schema_version") not in {1, 2, 3, 4}:
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
            if not all(
                isinstance(value, str) and value for value in (board_id, agent_name)
            ):
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
                tool_bytes = _nonnegative_int(
                    tool_raw.get("request_bytes")
                ) + _nonnegative_int(tool_raw.get("response_bytes"))
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
    rows.sort(
        key=lambda item: (-item["today_bytes"], item["board_id"], item["agent_name"])
    )

    model_wait_rows = []
    push_sessions: list[dict[str, Any]] = []
    recent_push_keys: set[tuple[str, str]] = set()
    raw_model_wait = document.get("model_wait")
    raw_model_wait = raw_model_wait if isinstance(raw_model_wait, dict) else {}
    current_hour = current.replace(minute=0, second=0, microsecond=0)
    window_start = current - timedelta(hours=24)
    for raw in raw_model_wait.values():
        if not isinstance(raw, dict):
            continue
        board_id = raw.get("board_id")
        agent_name = raw.get("agent_name")
        hours = raw.get("hours")
        if (
            not isinstance(board_id, str)
            or not board_id
            or not isinstance(agent_name, str)
            or not agent_name
            or not isinstance(hours, dict)
        ):
            continue
        per_hour_returns = 0
        per_hour_bytes = 0
        last_24h_returns = 0
        last_24h_bytes = 0
        outcomes: dict[str, int] = {}
        latest_bucket_at: datetime | None = None
        latest_bucket_returns = 0
        latest_bucket_bytes = 0
        latest_bucket_outcomes: dict[str, int] = {}
        for hour, bucket in hours.items():
            parsed = _parse_time(hour)
            if parsed is None or not isinstance(bucket, dict):
                continue
            returns = _nonnegative_int(bucket.get("returns"))
            response_bytes = _nonnegative_int(bucket.get("response_bytes"))
            if window_start <= parsed <= current and (
                latest_bucket_at is None or parsed > latest_bucket_at
            ):
                latest_bucket_at = parsed
                latest_bucket_returns = returns
                latest_bucket_bytes = response_bytes
                raw_latest_outcomes = bucket.get("outcomes")
                latest_bucket_outcomes = (
                    dict(raw_latest_outcomes)
                    if isinstance(raw_latest_outcomes, dict)
                    else {}
                )
            if parsed == current_hour:
                per_hour_returns += returns
                per_hour_bytes += response_bytes
            if window_start <= parsed <= current:
                last_24h_returns += returns
                last_24h_bytes += response_bytes
                raw_outcomes = bucket.get("outcomes")
                if isinstance(raw_outcomes, dict):
                    for name, count in raw_outcomes.items():
                        if isinstance(name, str):
                            outcomes[name] = outcomes.get(name, 0) + _nonnegative_int(count)
        model_wait_rows.append(
            {
                "board_id": _clip(board_id, MAX_LABEL_CHARS),
                "agent_name": _clip(agent_name, MAX_LABEL_CHARS),
                "returns_per_hour": per_hour_returns,
                "context_bytes_per_hour": per_hour_bytes,
                "estimated_tokens_per_hour": (per_hour_bytes + 3) // 4,
                "last_24h_returns": last_24h_returns,
                "last_24h_context_bytes": last_24h_bytes,
                "outcomes": outcomes,
            }
        )
        samples = raw.get("returns")
        samples = samples if isinstance(samples, list) else []
        actual: list[tuple[datetime, dict[str, Any], int]] = []
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            parsed = _parse_time(sample.get("at"))
            response_bytes = sample.get("response_bytes")
            if (
                parsed is None
                or parsed < window_start
                or parsed > current
                or type(response_bytes) is not int
                or response_bytes < 0
            ):
                continue
            actual.append((parsed, sample, (response_bytes + 3) // 4))
        push_actual = [row for row in actual if row[1].get("mode") == "push"]
        selected = push_actual or [row for row in actual if row[1].get("mode") != "digest"]
        if push_actual:
            recent_push_keys.add((board_id, agent_name))
        if selected:
            selected.sort(key=lambda row: row[0])
            chosen = max(selected, key=lambda row: (row[2], row[0]))
            sample_at, sample, tokens_per_return = chosen
            pressure = (
                "anomaly"
                if tokens_per_return > CONTEXT_STATS_ANOMALY_TOKENS
                else "compact"
                if tokens_per_return
                > pressure_thresholds["context_compact_tokens_per_poll"]
                else "watch"
                if tokens_per_return
                >= pressure_thresholds["context_watch_tokens_per_poll"]
                else "ok"
            )
            current_samples = [row for row in selected if row[0].replace(minute=0, second=0, microsecond=0) == current_hour]
            hour_bytes = sum(row[1]["response_bytes"] for row in current_samples)
            push_sessions.append(
                {
                    "board_id": _clip(board_id, MAX_LABEL_CHARS),
                    "agent_name": _clip(agent_name, MAX_LABEL_CHARS),
                    "latest_at": sample_at.isoformat(),
                    "latest_response_bytes": sample["response_bytes"],
                    "latest_estimated_tokens": tokens_per_return,
                    "estimated_tokens_per_return": tokens_per_return,
                    "estimated_tokens_per_hour": (hour_bytes + 3) // 4,
                    "returns_per_hour": len(current_samples),
                    "sample_count": len(selected),
                    "median_estimated_tokens": round(statistics.median(row[2] for row in selected), 1),
                    "trend_ratio": None,
                    "trend": "→",
                    "mode": _clip(sample.get("mode") or "unknown", 16),
                    "reason": _clip(sample.get("reason") or "unknown", 32),
                    "raw_record": copy.deepcopy(sample) if pressure == "anomaly" else None,
                    "pressure": pressure,
                    "pressure_rank": {"ok": 0, "watch": 1, "compact": 2, "anomaly": 3}[pressure],
                    "next_action": (
                        "Stats anomaly: inspect the raw wait-return record; do not compact from this value."
                        if pressure == "anomaly"
                        else "Compact only if a real wait return remains over threshold."
                        if pressure == "compact"
                        else "No compaction action is currently indicated."
                    ),
                }
            )
    model_wait_rows.sort(
        key=lambda item: (
            -item["returns_per_hour"],
            -item["context_bytes_per_hour"],
            item["board_id"],
            item["agent_name"],
        )
    )

    sessions = list(push_sessions)
    raw_cycles = document.get("poll_cycles")
    raw_cycles = raw_cycles if isinstance(raw_cycles, dict) else {}
    for raw in raw_cycles.values():
        if not isinstance(raw, dict):
            continue
        board_id = raw.get("board_id")
        agent_name = raw.get("agent_name")
        latest_bytes = raw.get("latest_response_bytes")
        latest_at = raw.get("latest_at")
        if (
            not isinstance(board_id, str)
            or not board_id
            or not isinstance(agent_name, str)
            or not agent_name
            or type(latest_bytes) is not int
            or latest_bytes < 0
            or _parse_time(latest_at) is None
        ):
            continue
        parsed_latest = _parse_time(latest_at)
        if parsed_latest is None or parsed_latest < window_start:
            continue
        mode = str(raw.get("mode") or "poll").lower()
        if (board_id, agent_name) in recent_push_keys and mode == "poll":
            continue
        samples = raw.get("samples")
        sample_bytes = [
            sample["response_bytes"]
            for sample in (samples[-24:] if isinstance(samples, list) else [])
            if isinstance(sample, dict)
            and type(sample.get("response_bytes")) is int
            and sample["response_bytes"] >= 0
            and _parse_time(sample.get("at")) is not None
        ]
        if not sample_bytes:
            sample_bytes = [latest_bytes]
        median_bytes = float(statistics.median(sample_bytes))
        trend_ratio = latest_bytes / median_bytes if median_bytes else None
        latest_tokens = (latest_bytes + 3) // 4
        median_tokens = round(median_bytes / 4, 1)
        trend = (
            "→"
            if median_bytes == latest_bytes
            else "↑"
            if latest_bytes > median_bytes
            else "↓"
        )
        compact = (
            latest_tokens > pressure_thresholds["context_compact_tokens_per_poll"]
            or trend_ratio is not None
            and trend_ratio >= pressure_thresholds["context_trend_compact_ratio"]
        )
        watch = latest_tokens >= pressure_thresholds["context_watch_tokens_per_poll"]
        anomaly = latest_tokens > CONTEXT_STATS_ANOMALY_TOKENS
        pressure = "anomaly" if anomaly else "compact" if compact else "watch" if watch else "ok"
        sessions.append(
            {
                "board_id": _clip(board_id, MAX_LABEL_CHARS),
                "agent_name": _clip(agent_name, MAX_LABEL_CHARS),
                "latest_at": latest_at,
                "latest_response_bytes": latest_bytes,
                "latest_estimated_tokens": latest_tokens,
                "estimated_tokens_per_return": latest_tokens,
                "estimated_tokens_per_hour": None,
                "returns_per_hour": None,
                "sample_count": len(sample_bytes),
                "median_estimated_tokens": median_tokens,
                "trend_ratio": round(trend_ratio, 3)
                if trend_ratio is not None
                else None,
                "trend": trend,
                "mode": mode,
                "reason": _clip(raw.get("reason") or "legacy", 32),
                "raw_record": copy.deepcopy(raw) if anomaly else None,
                "pressure": pressure,
                "pressure_rank": {"ok": 0, "watch": 1, "compact": 2, "anomaly": 3}[pressure],
                "next_action": (
                    "Stats anomaly: inspect the raw wait-return record; do not compact from this value."
                    if anomaly
                    else
                    "Run guarded journal compaction on this board and/or archive old memories."
                    if pressure == "compact"
                    else "No compaction action is currently indicated."
                ),
            }
        )
    sessions.sort(
        key=lambda item: (
            -item["pressure_rank"],
            -item["latest_estimated_tokens"],
            -(item["trend_ratio"] or 0),
            item["board_id"],
            item["agent_name"],
        )
    )
    push_unavailable = []
    raw_push_unavailable = document.get("push_unavailable")
    if isinstance(raw_push_unavailable, dict):
        for raw in raw_push_unavailable.values():
            if not isinstance(raw, dict):
                continue
            board_id = raw.get("board_id")
            agent_name = raw.get("agent_name")
            reason = raw.get("reason")
            observed_at = raw.get("observed_at")
            if (
                not isinstance(board_id, str)
                or not board_id
                or not isinstance(agent_name, str)
                or not agent_name
                or not isinstance(reason, str)
                or not reason
                or _parse_time(observed_at) is None
            ):
                continue
            push_unavailable.append(
                {
                    "board_id": _clip(board_id, MAX_LABEL_CHARS),
                    "agent_name": _clip(agent_name, MAX_LABEL_CHARS),
                    "reason": _clip(reason, 500),
                    "observed_at": observed_at,
                    "warning": f"push unavailable: {_clip(reason, 500)}",
                }
            )
    push_unavailable.sort(
        key=lambda item: (item["observed_at"], item["board_id"], item["agent_name"]),
        reverse=True,
    )
    result = {
        **empty,
        "source_status": "ok",
        "sessions": sessions[:MAX_OVERHEAD_SEATS],
        "seats": rows[:MAX_OVERHEAD_SEATS],
        "model_wait": model_wait_rows[:MAX_OVERHEAD_SEATS],
        "push_unavailable": push_unavailable[:MAX_OVERHEAD_SEATS],
        "truncated_sessions": max(0, len(sessions) - MAX_OVERHEAD_SEATS),
        "truncated_seats": max(0, len(rows) - MAX_OVERHEAD_SEATS),
    }
    while len(_json_bytes(result)) > API_MAX_BYTES and result["seats"]:
        result["seats"].pop()
        result["truncated_seats"] += 1
    while len(_json_bytes(result)) > API_MAX_BYTES and result["sessions"]:
        result["sessions"].pop()
        result["truncated_sessions"] += 1
    while len(_json_bytes(result)) > API_MAX_BYTES and result["model_wait"]:
        result["model_wait"].pop()
    while len(_json_bytes(result)) > API_MAX_BYTES and result["push_unavailable"]:
        result["push_unavailable"].pop()
    return result


def project_coordinator_findings(
    snapshot: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any] | None:
    state = snapshot.get("state")
    if not isinstance(state, dict) or "coordinator_findings" not in state:
        return None
    entry = state["coordinator_findings"]
    if isinstance(entry, dict):
        updated_at = _parse_time(entry.get("updated_at"))
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if (
            updated_at is not None
            and updated_at < current - timedelta(hours=CONTEXT_ATTENTION_HOURS)
        ):
            return None
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
    emitted_board_verification_failure = False
    verification = snapshot.get("_commit_verification")
    verification = verification if isinstance(verification, dict) else {}
    truncation = snapshot.get("_snapshot_truncation")
    truncation = truncation if isinstance(truncation, dict) else {}
    for finding in findings[:MAX_FINDINGS]:
        if not isinstance(finding, dict):
            continue
        level = str(finding.get("level") or "info").lower()
        if level not in {"info", "warn", "critical"}:
            level = "info"
        kind = _clip(finding.get("kind") or "finding", MAX_LABEL_CHARS)
        normalized_kind = kind.casefold().replace("_", "-")
        if normalized_kind == "board-large":
            hidden_active = _nonnegative_int(truncation.get("hidden_active"))
            if hidden_active == 0:
                continue
            kind = "board-active-truncated"
            finding = {
                **finding,
                "message": f"Snapshot truncation hides {hidden_active} open/submitted ticket(s).",
            }
        if normalized_kind == "unverifiable-commit":
            ticket_id = finding.get("ticket_id")
            outcome = verification.get(ticket_id) if isinstance(ticket_id, str) else None
            if isinstance(outcome, dict):
                status = outcome.get("status")
                if status in {"verified", "stale_closed"}:
                    continue
                if status == "cannot_verify":
                    if emitted_board_verification_failure:
                        continue
                    emitted_board_verification_failure = True
                    kind = "cannot-verify-origin"
                    finding = {
                        **finding,
                        "ticket_id": None,
                        "message": f"Cannot verify origin: {outcome.get('reason', 'unknown reason')}",
                    }
                elif status == "mismatch":
                    finding = {
                        **finding,
                        "message": f"Origin does not contain the submitted branch and commit for {ticket_id}.",
                    }
        text = (
            finding.get("reason")
            if normalized_kind == "board-unreachable"
            and isinstance(finding.get("reason"), str)
            else finding.get("message") or finding.get("summary") or finding.get("detail")
        )
        if not isinstance(text, str):
            text = json.dumps(finding, ensure_ascii=False, sort_keys=True)
        ask_id = finding.get("ask_id")
        draft_preview = (
            _validated_intake_draft(
                {"state": {"value": {"findings": findings}}}, ask_id
            )
            if isinstance(ask_id, str)
            else None
        )
        raw_hold = finding.get("hold")
        hold = None
        if isinstance(raw_hold, dict) and raw_hold.get("status") in {
            "shadow",
            "pending",
            "vetoed",
        }:
            hold = {
                "status": raw_hold["status"],
                "release_at": _clip(raw_hold.get("release_at"), 40) or None,
                "vetoable_until": _clip(raw_hold.get("vetoable_until"), 40)
                or None,
                "veto_reason": _clip(raw_hold.get("veto_reason"), MAX_FINDING_CHARS)
                or None,
            }
        projected = {
            "kind": kind,
            "level": level,
            "text": _clip(text, MAX_FINDING_CHARS),
            "ticket_id": _clip(finding.get("ticket_id"), MAX_LABEL_CHARS) or None,
            "ask_id": _clip(finding.get("ask_id"), 120) or None,
            "draft": draft_preview,
        }
        precedents = finding.get("precedents")
        if isinstance(precedents, list):
            projected["precedents"] = [
                {
                    "question_id": _clip(item.get("question_id"), 120),
                    "ticket_id": _clip(item.get("ticket_id"), MAX_LABEL_CHARS),
                }
                for item in precedents[:3]
                if isinstance(item, dict)
                and item.get("question_id")
                and item.get("ticket_id")
            ]
            projected["precedent_status"] = (
                "found" if projected["precedents"] else "none"
            )
        for name, selected in (
            ("question_id", _clip(finding.get("question_id"), 120) or None),
            ("verdict", _clip(finding.get("verdict"), 32) or None),
            (
                "evidence",
                _clip(finding.get("evidence"), MAX_FINDING_CHARS) or None,
            ),
            ("hold", hold),
        ):
            if selected is not None:
                projected[name] = selected
        items.append(projected)
    evaluations: list[dict[str, Any]] = []
    for key, evaluation_entry in state.items():
        if not str(key).startswith(BUTLER_EVALUATION_STATE_PREFIX):
            continue
        evaluation_raw = (
            evaluation_entry.get("value")
            if isinstance(evaluation_entry, dict) and "value" in evaluation_entry
            else evaluation_entry
        )
        if isinstance(evaluation_raw, str):
            try:
                evaluation_raw = json.loads(evaluation_raw)
            except json.JSONDecodeError:
                continue
        if isinstance(evaluation_raw, dict):
            evaluations.extend(_butler_evaluations(evaluation_raw))
    projected_evaluations = [
        {
            "question_id": _clip(row.get("question_id"), 120),
            "ticket_id": _clip(row.get("ticket_id"), MAX_LABEL_CHARS),
            "question_kind": _clip(row.get("question_kind"), 32),
            "draft_status": _clip(row.get("draft_status"), 16),
            "mark": (
                row.get("mark") if row.get("mark") in BUTLER_MARK_VALUES else None
            ),
            "mark_population": _clip(row.get("mark_population"), 32) or None,
            "marked_at": _clip(row.get("marked_at"), 40) or None,
        }
        for row in evaluations
        if row.get("question_id") and row.get("ticket_id")
    ]
    omitted_counts = snapshot.get("omitted_counts")
    omitted_state = (
        _nonnegative_int(omitted_counts.get("state"))
        if isinstance(omitted_counts, Mapping)
        else 0
    )
    evaluation_state_complete = omitted_state == 0
    return {
        "items": items,
        "truncated_count": reported_truncated + max(0, len(findings) - MAX_FINDINGS),
        "butler_evaluations": projected_evaluations,
        "butler_evaluation_truncated": omitted_state,
        "butler_evaluation_complete": evaluation_state_complete,
        "agreement_by_question_kind": butler_agreement_report(
            evaluations, complete=evaluation_state_complete
        ),
        "agreement_by_ticket": butler_agreement_by_ticket(
            evaluations, complete=evaluation_state_complete
        ),
        "multi_question_tickets": butler_multi_question_tickets(evaluations),
        "multi_question_tickets_complete": evaluation_state_complete,
    }


def coordinator_findings_stale(
    snapshot: dict[str, Any], *, now: datetime | None = None
) -> bool:
    state = snapshot.get("state")
    entry = state.get("coordinator_findings") if isinstance(state, dict) else None
    updated_at = _parse_time(entry.get("updated_at")) if isinstance(entry, dict) else None
    if updated_at is None:
        return False
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return updated_at < current - timedelta(
        minutes=COORDINATOR_FINDINGS_STALE_MINUTES
    )


BRANCH_COMMIT_RE = re.compile(
    r"branch_and_commit\s*:\s*([^\s@]+)\s*@\s*([0-9a-fA-F]{40})"
)


def ticket_branch_commit(ticket: dict[str, Any]) -> tuple[str, str] | None:
    """Extract the submitted branch and full commit without guessing."""
    for value in (
        ticket.get("notes"),
        *(entry.get("notes") for entry in ticket.get("submission_history", []) if isinstance(entry, dict)),
    ):
        if not isinstance(value, str):
            continue
        match = BRANCH_COMMIT_RE.search(value)
        if match:
            return match.group(1), match.group(2).lower()
    return None


def verify_ticket_commit_on_origin(
    ticket: dict[str, Any],
    work_dir: str | Path | None,
    *,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Verify branch@commit on origin, returning a bounded diagnostic."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    updated = _parse_time(ticket.get("closed_at") or ticket.get("updated_at"))
    if (
        ticket.get("status") in TERMINAL_STATES
        and updated is not None
        and updated < current - timedelta(days=ATTENTION_RETENTION_DAYS)
    ):
        return {"status": "stale_closed"}
    branch_commit = ticket_branch_commit(ticket)
    if branch_commit is None:
        return {"status": "cannot_verify", "reason": "branch_and_commit is missing"}
    if work_dir is None:
        return {"status": "cannot_verify", "reason": "registry work_dir is missing"}
    source = Path(work_dir).expanduser()
    if not source.is_dir():
        return {"status": "cannot_verify", "reason": "registry work_dir does not exist"}
    command = [
        "git", "-C", str(source), "ls-remote", "--heads", "origin",
        f"refs/heads/{branch_commit[0]}",
    ]
    try:
        result = runner(
            command, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "cannot_verify", "reason": "origin lookup failed"}
    if result.returncode != 0:
        return {"status": "cannot_verify", "reason": "origin lookup failed"}
    refs = {
        parts[0].lower()
        for line in result.stdout.splitlines()
        if len(parts := line.split()) == 2
    }
    return {
        "status": "verified" if branch_commit[1] in refs else "mismatch",
        "branch": branch_commit[0],
        "commit": branch_commit[1],
    }


def reconcile_attention_state(
    previous: dict[str, dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Dedupe current conditions and auto-clear absent, acked, or snoozed items."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_text = current.isoformat()
    state: dict[str, dict[str, Any]] = {}
    visible: list[dict[str, Any]] = []
    for candidate in candidates:
        key = str(candidate.get("key") or "")
        fingerprint = str(candidate.get("fingerprint") or "")
        if not key or key in state:
            continue
        old = previous.get(key) if isinstance(previous, dict) else None
        unchanged = isinstance(old, dict) and old.get("fingerprint") == fingerprint
        row = {
            "fingerprint": fingerprint,
            "first_seen": old.get("first_seen", current_text) if unchanged else current_text,
            "last_seen": current_text,
            "acknowledged": bool(old.get("acknowledged")) if unchanged else False,
            "snooze_until": old.get("snooze_until") if unchanged else None,
        }
        state[key] = row
        snooze_until = _parse_time(row.get("snooze_until"))
        if not row["acknowledged"] and not (
            snooze_until is not None and snooze_until > current
        ):
            visible.append(candidate)
    return state, visible


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
            and project.get("fleet", True)
            and isinstance(board_id, str)
            and board_id
            and board_id not in seen
        ):
            boards.append((_clip(name, MAX_LABEL_CHARS), board_id))
            seen.add(board_id)
        if len(boards) >= MAX_BOARDS:
            break
    return boards


def parse_seat_registry(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return strict durable seat scope declarations keyed by seat name."""
    state = result.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("value"), str):
        raise TypeError("seat registry state is missing")
    try:
        document = json.loads(state["value"])
    except json.JSONDecodeError as exc:
        raise ValueError("seat registry is not valid JSON") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "seats"}
        or document.get("schema_version") != 1
        or not isinstance(document.get("seats"), dict)
    ):
        raise ValueError("seat registry schema is unsupported")
    selected: dict[str, dict[str, Any]] = {}
    for name, definition in document["seats"].items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(definition, dict)
            or set(definition) != {"principal_id", "role", "board_mode"}
        ):
            raise ValueError("seat registry contains an invalid definition")
        principal_id = definition.get("principal_id")
        role = definition.get("role")
        board_mode = definition.get("board_mode")
        if not isinstance(principal_id, str) or not principal_id or role not in {
            "worker", "reviewer"
        }:
            raise ValueError("seat registry contains an invalid identity")
        if board_mode != "registry" and not (
            isinstance(board_mode, list)
            and board_mode
            and all(isinstance(board, str) and board for board in board_mode)
            and len(set(board_mode)) == len(board_mode)
        ):
            raise ValueError("seat registry contains an invalid board_mode")
        selected[name] = {
            "principal_id": principal_id,
            "role": role,
            "board_mode": copy.deepcopy(board_mode),
        }
    return selected


def parse_project_work_dirs(
    result: dict[str, Any], home_board: str
) -> dict[str, str | None]:
    """Return registry-authoritative work directories by active board ID."""
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
    work_dirs: dict[str, str | None] = {home_board: None}
    for project in projects.values():
        if not isinstance(project, dict) or project.get("status") != "active":
            continue
        board_id = project.get("board_id")
        if not isinstance(board_id, str) or not board_id:
            continue
        work_dir = project.get("fleet_clone_dir") or project.get("work_dir")
        work_dirs[board_id] = work_dir if isinstance(work_dir, str) and work_dir else None
    return work_dirs


def coordinator_finding_ticket_ids(snapshot: dict[str, Any], kind: str) -> list[str]:
    """Return bounded ticket IDs named by one coordinator finding kind."""
    state = snapshot.get("state")
    entry = state.get("coordinator_findings") if isinstance(state, dict) else None
    raw = entry.get("value") if isinstance(entry, dict) and "value" in entry else entry
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    findings = raw if isinstance(raw, list) else raw.get("findings", raw.get("items", [])) if isinstance(raw, dict) else []
    wanted = kind.casefold().replace("_", "-")
    result: list[str] = []
    for finding in findings[:MAX_FINDINGS] if isinstance(findings, list) else []:
        if not isinstance(finding, dict):
            continue
        actual = str(finding.get("kind") or "").casefold().replace("_", "-")
        ticket_id = finding.get("ticket_id")
        if actual == wanted and isinstance(ticket_id, str) and ticket_id not in result:
            result.append(ticket_id)
    return result


def _closed_today(ticket: dict[str, Any], today: datetime) -> bool:
    if ticket.get("status") != "closed":
        return False
    closed_at = _parse_time(ticket.get("closed_at") or ticket.get("updated_at"))
    return closed_at is not None and closed_at.date() == today.date()


def _ticket_status_label(ticket: dict[str, Any], now: datetime) -> str:
    status = str(ticket.get("status") or "unknown")
    lease = ticket.get("review_lease")
    expires = _parse_time(lease.get("expires_at")) if isinstance(lease, dict) else None
    if status in SUBMITTED_STATES and expires is not None and expires > now:
        reviewer = _clip(lease.get("reviewer_agent_name"), MAX_LABEL_CHARS)
        return f"in review by {reviewer or 'reviewer'}"
    return status


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
        raw_agent_ids = {
            ticket.get("claimed_by_agent_id"),
            ticket.get("assigned_to_agent_id"),
        }
        review_lease = ticket.get("review_lease")
        if isinstance(review_lease, dict):
            raw_agent_ids.add(review_lease.get("reviewer_agent_id"))
        for raw_agent_id in raw_agent_ids - {None, ""}:
            agent_id = str(raw_agent_id)
            current = selected.get(agent_id)
            if current is None or _ticket_recency(ticket) > _ticket_recency(current):
                selected[agent_id] = ticket
    return selected


def _review_identity(event: Any) -> dict[str, str] | None:
    if not isinstance(event, dict):
        return None
    board_id = event.get("board_id")
    ticket_id = event.get("ticket_id")
    if not (
        isinstance(board_id, str)
        and BOARD_ID_RE.fullmatch(board_id)
        and isinstance(ticket_id, str)
        and ticket_id
    ):
        return None
    return {
        "board_id": board_id,
        "ticket_id": _clip(ticket_id, MAX_LABEL_CHARS),
    }


def _review_state_after_log(
    lines: list[str], *, initial: dict[str, str] | None = None
) -> tuple[bool, dict[str, str] | None]:
    """Apply bounded lifecycle lines to an optional durable active review."""
    active = initial
    saw_lifecycle = False
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        event_name = event.get("event") if isinstance(event, dict) else None
        if event_name in {"runtime_session_started", "review_session_reset"}:
            active = None
            saw_lifecycle = True
            continue
        key = _review_identity(event)
        if key is None:
            continue
        if event_name == "review_started":
            active = key
            saw_lifecycle = True
        elif event_name == "review_finished" and active == key:
            active = None
            saw_lifecycle = True
    return saw_lifecycle, active


def _active_review_from_log(lines: list[str]) -> dict[str, str] | None:
    """Return the latest explicitly-started review not followed by its finish."""
    return _review_state_after_log(lines)[1]


def _review_destination(ticket: dict[str, Any]) -> str | None:
    lease = ticket.get("review_lease")
    if isinstance(lease, dict):
        destination = lease.get("reviewer_agent_name") or lease.get(
            "reviewer_agent_id"
        )
        if isinstance(destination, str) and destination:
            return _clip(destination, MAX_LABEL_CHARS)
    offer = ticket.get("review_offer")
    if isinstance(offer, dict):
        destination = offer.get("agent_name") or offer.get("agent_id")
        if isinstance(destination, str) and destination:
            return _clip(destination, MAX_LABEL_CHARS)
    return None


def _handoff_recency(memory: dict[str, Any]) -> tuple[float, str, str]:
    raw_epoch = memory.get("created_at_epoch")
    epoch = float(raw_epoch) if isinstance(raw_epoch, (int, float)) else 0.0
    return (
        epoch,
        str(memory.get("created_at") or ""),
        str(memory.get("memory_id") or ""),
    )


def _latest_handoffs_by_ticket(memories: Any) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not isinstance(memories, list):
        return latest
    for memory in memories[:MAX_HANDOFF_MEMORIES]:
        if (
            not isinstance(memory, dict)
            or memory.get("memory_type") != "handoff"
            or not isinstance(memory.get("memory_id"), str)
        ):
            continue
        related = memory.get("related_tickets")
        if not isinstance(related, list):
            continue
        for raw_ticket_id in related[:MAX_REQUIRED_FIELDS]:
            if not isinstance(raw_ticket_id, str) or not raw_ticket_id:
                continue
            ticket_id = _clip(raw_ticket_id, MAX_LABEL_CHARS)
            current = latest.get(ticket_id)
            if current is None or _handoff_recency(memory) > _handoff_recency(current):
                latest[ticket_id] = memory
    return latest


def _detail_handoff(
    memory: dict[str, Any] | None, ticket: dict[str, Any]
) -> dict[str, Any] | None:
    if not isinstance(memory, dict):
        return None
    next_steps = memory.get("next_steps")
    if not isinstance(next_steps, list):
        next_steps = []
    return {
        "memory_id": _clip(memory.get("memory_id"), MAX_LABEL_CHARS),
        "ticket_id": _clip(ticket.get("ticket_id"), MAX_LABEL_CHARS),
        "source": _clip(
            memory.get("author_agent_name") or memory.get("author_agent_id"),
            MAX_LABEL_CHARS,
        )
        or None,
        "destination": _review_destination(ticket),
        "summary": _clip(
            memory.get("pinned_summary")
            or memory.get("summary")
            or memory.get("title"),
            MAX_SUBMISSION_CHARS,
        )
        or None,
        "next_steps": [
            _clip(item, MAX_DESCRIPTION_CHARS)
            for item in next_steps[:8]
            if isinstance(item, str) and item
        ],
        "created_at": _clip(memory.get("created_at"), 40) or None,
    }


def _ticket_actor_label(
    *,
    name: Any = None,
    agent_id: Any = None,
    agents_by_id: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    """Return one observed actor label without inventing a display identity."""
    label = _clip(name, MAX_LABEL_CHARS)
    if label:
        return label
    safe_agent_id = _clip(agent_id, MAX_LABEL_CHARS)
    known = (agents_by_id or {}).get(safe_agent_id, {})
    return _clip(known.get("agent_name"), MAX_LABEL_CHARS) or safe_agent_id or None


def _ticket_lifecycle(
    ticket: dict[str, Any],
    events: list[Any],
    *,
    agents_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Project observed lifecycle evidence plus the current protocol stage."""
    submissions = ticket.get("submission_history")
    latest_submission = (
        submissions[-1]
        if isinstance(submissions, list)
        and submissions
        and isinstance(submissions[-1], dict)
        else {}
    )
    reviews = ticket.get("review_history")
    latest_review = (
        reviews[-1]
        if isinstance(reviews, list)
        and reviews
        and isinstance(reviews[-1], dict)
        else {}
    )
    evidence: dict[str, dict[str, Any]] = {
        "created": {
            "at": _clip(ticket.get("created_at"), 40) or None,
            "actor": _ticket_actor_label(
                name=ticket.get("created_by"),
                agent_id=ticket.get("created_by_agent_id"),
                agents_by_id=agents_by_id,
            ),
        },
        "offered": {"at": None, "actor": None},
        "claimed": {
            "at": _clip(ticket.get("claimed_at"), 40) or None,
            "actor": _ticket_actor_label(
                name=ticket.get("claimed_by"),
                agent_id=ticket.get("claimed_by_agent_id"),
                agents_by_id=agents_by_id,
            ),
        },
        "submitted": {
            "at": _clip(
                latest_submission.get("submitted_at") or ticket.get("submitted_at"),
                40,
            )
            or None,
            "actor": _ticket_actor_label(
                name=(
                    latest_submission.get("submitted_by_agent_name")
                    or ticket.get("submitted_by_agent_name")
                ),
                agent_id=(
                    latest_submission.get("submitted_by_agent_id")
                    or ticket.get("submitted_by_agent_id")
                ),
                agents_by_id=agents_by_id,
            ),
        },
        "reviewed": {
            "at": _clip(
                latest_review.get("reviewed_at") or ticket.get("reviewed_at"), 40
            )
            or None,
            "actor": _ticket_actor_label(
                name=(
                    latest_review.get("reviewed_by_agent_name")
                    or ticket.get("reviewed_by_agent_name")
                ),
                agent_id=(
                    latest_review.get("reviewed_by_agent_id")
                    or ticket.get("reviewed_by_agent_id")
                ),
                agents_by_id=agents_by_id,
            ),
        },
    }

    dispatch_history = ticket.get("dispatch_history")
    if isinstance(dispatch_history, list):
        for item in dispatch_history:
            if (
                not isinstance(item, dict)
                or item.get("state") != "offered"
                or item.get("kind") != "work"
            ):
                continue
            evidence["offered"] = {
                "at": _clip(item.get("offered_at") or item.get("at"), 40) or None,
                "actor": _ticket_actor_label(
                    name=item.get("agent_name"),
                    agent_id=item.get("agent_id"),
                    agents_by_id=agents_by_id,
                ),
            }

    ticket_id = str(ticket.get("ticket_id") or "")
    ordered_events = sorted(
        (
            item
            for item in events
            if isinstance(item, dict) and item.get("ticket_id") == ticket_id
        ),
        key=lambda item: item.get("seq") if isinstance(item.get("seq"), int) else -1,
    )
    for event in ordered_events:
        occurred_at = _clip(event.get("occurred_at"), 40) or None
        actor = _ticket_actor_label(
            agent_id=event.get("actor"), agents_by_id=agents_by_id
        )
        if event.get("kind") == "ticket_created" or (
            event.get("status_from") == "missing" and event.get("status_to") == "open"
        ):
            evidence["created"] = {
                "at": occurred_at or evidence["created"]["at"],
                "actor": actor or evidence["created"]["actor"],
            }
        if event.get("kind") == "ticket_offered":
            evidence["offered"] = {
                "at": occurred_at or evidence["offered"]["at"],
                "actor": (
                    _ticket_actor_label(
                        name=event.get("offered_agent_name"),
                        agent_id=event.get("offered_agent_id"),
                        agents_by_id=agents_by_id,
                    )
                    or evidence["offered"]["actor"]
                ),
            }
        if event.get("status_to") in ACTIVE_CLAIM_STATES:
            evidence["claimed"] = {
                "at": occurred_at or evidence["claimed"]["at"],
                "actor": actor or evidence["claimed"]["actor"],
            }
        is_submission_event = event.get("kind") in {
            "ticket_submitted",
            "ticket_resubmitted",
        } or (
            event.get("kind") == "ticket_status_changed"
            and event.get("status_to") in SUBMITTED_STATES
            and event.get("status_from") not in SUBMITTED_STATES
        )
        if is_submission_event:
            evidence["submitted"] = {
                "at": occurred_at or evidence["submitted"]["at"],
                "actor": (
                    _ticket_actor_label(
                        name=event.get("submitted_by_agent_name"),
                        agent_id=(
                            event.get("submitted_by_agent_id") or event.get("actor")
                        ),
                        agents_by_id=agents_by_id,
                    )
                    or evidence["submitted"]["actor"]
                ),
            }
        if event.get("review_verdict") or event.get("status_to") == "closed":
            evidence["reviewed"] = {
                "at": occurred_at or evidence["reviewed"]["at"],
                "actor": (
                    _ticket_actor_label(
                        name=event.get("reviewed_by_agent_name"),
                        agent_id=(
                            event.get("reviewed_by_agent_id") or event.get("actor")
                        ),
                        agents_by_id=agents_by_id,
                    )
                    or evidence["reviewed"]["actor"]
                ),
            }

    status = str(ticket.get("status") or "unknown")
    dispatch_state = ticket.get("dispatch_state")
    offered_now = (
        isinstance(dispatch_state, dict) and dispatch_state.get("state") == "offered"
    )
    if status in {"closed", "rejected"}:
        current_key = "reviewed"
    elif status in SUBMITTED_STATES:
        current_key = "submitted"
    elif status in ACTIVE_CLAIM_STATES:
        current_key = "claimed"
    elif offered_now or status == "assigned":
        current_key = "offered"
    else:
        current_key = "created"

    stages = []
    for key, label in (
        ("created", "Created"),
        ("offered", "Offered"),
        ("claimed", "Claimed"),
        ("submitted", "Submitted"),
        ("reviewed", "Reviewed"),
    ):
        observed = bool(evidence[key]["at"] or evidence[key]["actor"])
        stages.append(
            {
                "key": key,
                "label": label,
                "state": (
                    "current"
                    if key == current_key
                    else "complete"
                    if observed
                    else "pending"
                ),
                "observed": observed,
                **evidence[key],
            }
        )
    return stages


def _ticket_coordination_summary(ticket: dict[str, Any]) -> dict[str, Any]:
    """Translate current protocol fields through explicit, bounded labels."""
    status = str(ticket.get("status") or "unknown")
    review_lease = ticket.get("review_lease")
    dispatch_state = ticket.get("dispatch_state")
    lease_expires_at = None
    lease_expected = False
    if isinstance(review_lease, dict) and status in SUBMITTED_STATES:
        lease_expected = True
        reviewer = _ticket_actor_label(
            name=review_lease.get("reviewer_agent_name"),
            agent_id=review_lease.get("reviewer_agent_id"),
        )
        now_text = f"{reviewer} is reviewing" if reviewer else "Independent review active"
        lease_expires_at = _clip(review_lease.get("expires_at"), 40) or None
    elif status in ACTIVE_CLAIM_STATES:
        lease_expected = True
        worker = _ticket_actor_label(
            name=ticket.get("claimed_by"), agent_id=ticket.get("claimed_by_agent_id")
        )
        now_text = f"{worker} is working" if worker else "Claimed work in progress"
        lease_expires_at = _clip(ticket.get("lease_expires_at"), 40) or None
    elif (
        isinstance(dispatch_state, dict) and dispatch_state.get("state") == "offered"
    ):
        offered_to = _ticket_actor_label(
            name=dispatch_state.get("agent_name"),
            agent_id=dispatch_state.get("agent_id"),
        )
        now_text = f"Offer sent to {offered_to or 'Not supplied'}"
    else:
        now_text = {
            "open": "Awaiting an eligible worker",
            "assigned": "Awaiting the assigned worker",
            "submitted": "Awaiting independent review",
            "reviewing": "Independent review active",
            "in_review": "Independent review active",
            "rejected": "Review requested changes",
            "closed": "Review approved",
            "canceled": "Ticket canceled",
            "terminated": "Ticket terminated",
        }.get(status, "Not supplied")

    raw_annotations = ticket.get("annotations")
    latest_decision = None
    if isinstance(raw_annotations, list):
        latest_decision = next(
            (
                item
                for item in reversed(raw_annotations)
                if isinstance(item, dict)
                and item.get("kind") == "decision"
                and _clip(item.get("text"), MAX_ANNOTATION_TEXT_CHARS)
            ),
            None,
        )
    decision = None
    if latest_decision is not None:
        decision = {
            "kind": "Decision",
            "text": _clip(latest_decision.get("text"), MAX_ANNOTATION_TEXT_CHARS),
            "at": _clip(latest_decision.get("at"), 40) or None,
        }
    return {
        "now": {
            "text": now_text,
            "lease_expires_at": lease_expires_at,
            "lease_expected": lease_expected,
        },
        "next": TICKET_NEXT_LABELS.get(status, "Not supplied"),
        "blocked": decision,
    }


def _detail_ticket(
    ticket: dict[str, Any],
    events: list[Any] | None = None,
    *,
    agents_by_id: dict[str, dict[str, Any]] | None = None,
    handoff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    required = ticket.get("required_fields")
    if not isinstance(required, list):
        required = []
    submissions = ticket.get("submission_history")
    latest_submission = (
        submissions[-1] if isinstance(submissions, list) and submissions else {}
    )
    if not isinstance(latest_submission, dict):
        latest_submission = {}
    raw_annotations = ticket.get("annotations")
    annotations = []
    if isinstance(raw_annotations, list):
        for item in raw_annotations[-MAX_ANNOTATIONS_PER_TICKET:]:
            if not isinstance(item, dict):
                continue
            attribution = item.get("by")
            if not isinstance(attribution, dict):
                attribution = {}
            annotations.append(
                {
                    "annotation_id": _clip(
                        item.get("annotation_id"), MAX_LABEL_CHARS
                    ),
                    "kind": _clip(item.get("kind") or "note", 32),
                    "text": _clip(item.get("text"), MAX_ANNOTATION_TEXT_CHARS),
                    "by": {
                        "agent_name": _clip(
                            attribution.get("agent_name"), MAX_LABEL_CHARS
                        ),
                        "agent_id": _clip(
                            attribution.get("agent_id"), MAX_LABEL_CHARS
                        ),
                        "principal_id": _clip(
                            attribution.get("principal_id"), MAX_LABEL_CHARS
                        ),
                    },
                    "at": _clip(item.get("at"), 40) or None,
                }
            )
    annotations_omitted = max(
        0, int(ticket.get("annotations_omitted_count", 0) or 0)
    )
    return {
        "id": _clip(ticket.get("ticket_id"), MAX_LABEL_CHARS),
        "title": _clip(ticket.get("title") or "Not supplied", MAX_TITLE_CHARS),
        "status": _clip(ticket.get("status") or "unknown", 32),
        "status_label": _clip(
            _ticket_status_label(ticket, datetime.now(timezone.utc)), 64
        ),
        "priority": _clip(ticket.get("priority") or "medium", 16),
        "abandoned_count": max(0, int(ticket.get("abandoned_count", 0) or 0)),
        "claimed_by": _clip(ticket.get("claimed_by"), MAX_LABEL_CHARS) or None,
        "claim_age_s": _nonnegative_int(ticket.get("claim_age_s")),
        "lease_renewal_source": (
            ticket.get("lease_renewal_source")
            if ticket.get("lease_renewal_source") in {"model", "keepalive"}
            else None
        ),
        "lease_keepalive_only_age_s": _nonnegative_int(
            ticket.get("lease_keepalive_only_age_s")
        ),
        "ttl_s": _nonnegative_int(ticket.get("ttl_s")),
        "rejection_count": _nonnegative_int(ticket.get("rejection_count")),
        "review_wait_started_at": (
            _clip(
                latest_submission.get("submitted_at") or ticket.get("submitted_at"),
                40,
            )
            or None
        ),
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
        "result": project_ticket_result(ticket),
        "review_label": _clip(ticket.get("review_label"), MAX_LABEL_CHARS) or None,
        "annotations": annotations,
        "annotation_count": max(
            annotations_omitted + len(annotations),
            int(ticket.get("annotation_count", 0) or 0),
        ),
        "annotations_omitted_count": annotations_omitted,
        "lifecycle": _ticket_lifecycle(
            ticket, events or [], agents_by_id=agents_by_id
        ),
        "coordination": _ticket_coordination_summary(ticket),
        "latest_handoff": _detail_handoff(handoff, ticket),
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
        name: 0 for name in ("created", "claimed", "submitted", "closed", "rejected")
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


def _provenance_identity(
    *,
    name: Any = None,
    agent_id: Any = None,
    principal_id: Any = None,
    agents_by_id: dict[str, dict[str, Any]],
) -> dict[str, str | None] | None:
    """Return one bounded actor identity, enriched from the snapshot seat map."""
    safe_agent_id = _clip(agent_id, MAX_LABEL_CHARS) or None
    known = agents_by_id.get(safe_agent_id or "", {})
    safe_name = _clip(name or known.get("agent_name") or safe_agent_id, MAX_LABEL_CHARS)
    if not safe_name:
        return None
    return {
        "name": safe_name,
        "agent_id": safe_agent_id,
        "principal_id": _clip(
            principal_id or known.get("principal_id"), MAX_LABEL_CHARS
        )
        or None,
        "label": safe_name,
    }


def _provenance_stage(
    identity: dict[str, str | None] | None, at: Any
) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {**identity, "at": _clip(at, 40) or None}


def assemble_provenance(
    snapshot: dict[str, Any],
    events: list[Any],
    *,
    now: datetime,
    event_window_truncated: bool = False,
) -> dict[str, Any]:
    """Assemble a seven-day ticket route from bounded snapshot and journal data."""
    current = now.astimezone(timezone.utc)
    cutoff = current - timedelta(days=ROUTE_WINDOW_DAYS)
    source_agents = snapshot.get("agents")
    agents_by_id = (
        {
            str(agent.get("agent_id")): agent
            for agent in source_agents
            if isinstance(agent, dict) and agent.get("agent_id")
        }
        if isinstance(source_agents, list)
        else {}
    )
    source_tickets = snapshot.get("tickets")
    source_tickets = source_tickets if isinstance(source_tickets, list) else []
    rows: dict[str, dict[str, Any]] = {}

    def identity(
        *, name: Any = None, agent_id: Any = None, principal_id: Any = None
    ) -> dict[str, str | None] | None:
        return _provenance_identity(
            name=name,
            agent_id=agent_id,
            principal_id=principal_id,
            agents_by_id=agents_by_id,
        )

    for ticket in source_tickets:
        if not isinstance(ticket, dict):
            continue
        ticket_id = _clip(ticket.get("ticket_id"), MAX_LABEL_CHARS)
        if not ticket_id:
            continue
        submissions = ticket.get("submission_history")
        latest_submission = (
            submissions[-1]
            if isinstance(submissions, list)
            and submissions
            and isinstance(submissions[-1], dict)
            else {}
        )
        reviews = ticket.get("review_history")
        latest_review = (
            reviews[-1]
            if isinstance(reviews, list) and reviews and isinstance(reviews[-1], dict)
            else {}
        )
        rows[ticket_id] = {
            "id": ticket_id,
            "title": _clip(ticket.get("title") or "(untitled)", MAX_TITLE_CHARS),
            "status": _clip(ticket.get("status") or "unknown", 32),
            "updated_at": _clip(ticket.get("updated_at"), 40) or None,
            "created": _provenance_stage(
                identity(
                    name=ticket.get("created_by"),
                    agent_id=ticket.get("created_by_agent_id"),
                    principal_id=ticket.get("created_by_principal_id"),
                ),
                ticket.get("created_at"),
            ),
            "executed": _provenance_stage(
                identity(
                    name=ticket.get("claimed_by"),
                    agent_id=ticket.get("claimed_by_agent_id"),
                    principal_id=ticket.get("claimed_by_principal_id"),
                ),
                ticket.get("claimed_at"),
            ),
            "submitted": _provenance_stage(
                identity(
                    name=(
                        latest_review.get("submitted_by_agent_name")
                        or ticket.get("submitted_by_agent_name")
                    ),
                    agent_id=(
                        latest_submission.get("submitted_by_agent_id")
                        or ticket.get("submitted_by_agent_id")
                    ),
                    principal_id=(
                        latest_submission.get("submitted_by_principal_id")
                        or ticket.get("submitted_by_principal_id")
                    ),
                ),
                latest_submission.get("submitted_at") or ticket.get("submitted_at"),
            ),
            "reviewed": _provenance_stage(
                identity(
                    name=(
                        latest_review.get("reviewed_by_agent_name")
                        or ticket.get("reviewed_by_agent_name")
                    ),
                    agent_id=(
                        latest_review.get("reviewed_by_agent_id")
                        or ticket.get("reviewed_by_agent_id")
                    ),
                    principal_id=(
                        latest_review.get("reviewed_by_principal_id")
                        or ticket.get("reviewed_by_principal_id")
                    ),
                ),
                latest_review.get("reviewed_at") or ticket.get("reviewed_at"),
            ),
            "rework_count": _nonnegative_int(ticket.get("rejection_count")),
        }

    event_reworks: dict[str, int] = {}
    for event in sorted(
        (item for item in events if isinstance(item, dict)),
        key=lambda item: item.get("seq") if isinstance(item.get("seq"), int) else -1,
    ):
        ticket_id = _clip(event.get("ticket_id"), MAX_LABEL_CHARS)
        if not ticket_id:
            continue
        row = rows.setdefault(
            ticket_id,
            {
                "id": ticket_id,
                "title": "(event-only ticket)",
                "status": "unknown",
                "updated_at": None,
                "created": None,
                "executed": None,
                "submitted": None,
                "reviewed": None,
                "rework_count": 0,
            },
        )
        occurred_at = _clip(event.get("occurred_at"), 40) or None
        if _time_sort_value(occurred_at) >= _time_sort_value(row.get("updated_at")):
            row["updated_at"] = occurred_at
            if event.get("status_to"):
                row["status"] = _clip(event.get("status_to"), 32)
        actor = identity(agent_id=event.get("actor"))
        if event.get("kind") == "ticket_created" or (
            event.get("status_from") == "missing" and event.get("status_to") == "open"
        ):
            row["created"] = row["created"] or _provenance_stage(actor, occurred_at)
        if event.get("status_to") in ACTIVE_CLAIM_STATES:
            row["executed"] = _provenance_stage(actor, occurred_at)
        if event.get("status_to") in SUBMITTED_STATES:
            row["submitted"] = _provenance_stage(actor, occurred_at)

        submitted = identity(
            name=event.get("submitted_by_agent_name"),
            agent_id=event.get("submitted_by_agent_id"),
            principal_id=event.get("submitted_by_principal_id"),
        )
        if submitted is not None:
            previous_at = row["submitted"].get("at") if row["submitted"] else None
            row["submitted"] = _provenance_stage(submitted, previous_at)
        reviewer = identity(
            name=event.get("reviewed_by_agent_name"),
            agent_id=event.get("reviewed_by_agent_id") or event.get("reviewed_by"),
            principal_id=event.get("reviewed_by_principal_id"),
        )
        if reviewer is not None:
            row["reviewed"] = _provenance_stage(reviewer, occurred_at)
        elif event.get("status_to") == "closed":
            row["reviewed"] = _provenance_stage(actor, occurred_at)

        bounced = event.get("review_verdict") == "reject" or (
            event.get("status_from") == "submitted"
            and event.get("status_to") in {"open", "claimed", "rejected"}
        )
        if bounced:
            event_reworks[ticket_id] = event_reworks.get(ticket_id, 0) + 1

    for ticket_id, count in event_reworks.items():
        rows[ticket_id]["rework_count"] = max(rows[ticket_id]["rework_count"], count)

    selected = [
        row
        for row in rows.values()
        if (updated := _parse_time(row.get("updated_at"))) is not None
        and updated >= cutoff
    ]
    selected.sort(key=lambda row: row["updated_at"] or "", reverse=True)

    principals_by_name: dict[str, set[str]] = {}
    for row in selected:
        for field in ("created", "executed", "submitted", "reviewed"):
            stage = row[field]
            if stage and stage.get("principal_id"):
                principals_by_name.setdefault(stage["name"], set()).add(
                    stage["principal_id"]
                )
    for row in selected:
        for field in ("created", "executed", "submitted", "reviewed"):
            stage = row[field]
            if stage and len(principals_by_name.get(stage["name"], set())) > 1:
                stage["label"] = f"{stage['name']} · …{stage['principal_id'][-6:]}"

    seat_sets: dict[tuple[str, str], dict[str, Any]] = {}

    def seat_for(stage: dict[str, Any] | None) -> dict[str, Any] | None:
        if not stage:
            return None
        key = (
            stage.get("name") or "",
            stage.get("principal_id") or stage.get("agent_id") or "",
        )
        return seat_sets.setdefault(
            key,
            {
                "label": stage.get("label") or stage.get("name") or "Unknown",
                "created": set(),
                "executed": set(),
                "reviewed": set(),
                "reworked": set(),
            },
        )

    for row in selected:
        for field in ("created", "executed", "reviewed"):
            seat = seat_for(row[field])
            if seat is not None:
                seat[field].add(row["id"])
        if row["rework_count"]:
            seat = seat_for(row["submitted"] or row["executed"])
            if seat is not None:
                seat["reworked"].add(row["id"])

    seats = []
    for seat in seat_sets.values():
        executed = len(seat["executed"])
        seats.append(
            {
                "label": seat["label"],
                "created": len(seat["created"]),
                "executed": executed,
                "reviewed": len(seat["reviewed"]),
                "rework_received": len(seat["reworked"]),
                "rework_received_rate": round(100 * len(seat["reworked"]) / executed, 1)
                if executed
                else 0.0,
            }
        )
    seats.sort(
        key=lambda seat: (
            -(seat["created"] + seat["executed"] + seat["reviewed"]),
            seat["label"],
        )
    )

    total_counts = snapshot.get("total_counts")
    total_tickets = (
        total_counts.get("tickets")
        if isinstance(total_counts, dict) and type(total_counts.get("tickets")) is int
        else len(source_tickets)
    )
    omitted_counts = snapshot.get("omitted_counts")
    snapshot_omitted = (
        _nonnegative_int(omitted_counts.get("tickets"))
        if isinstance(omitted_counts, dict)
        else max(0, total_tickets - len(source_tickets))
    )
    route_total = len(selected)
    route_truncated = bool(
        snapshot.get("truncated")
        or snapshot_omitted
        or event_window_truncated
        or route_total > MAX_ROUTE_ROWS
        or len(seats) > MAX_ROUTE_SEATS
    )
    note = (
        f"Default window: last {ROUTE_WINDOW_DAYS} days by updated_at. "
        f"Bounded source returned {len(source_tickets)} of {total_tickets} snapshot "
        f"tickets ({snapshot_omitted} omitted) and {len(events)} catchup events "
        f"with ack=false; older lifecycle steps may be absent."
    )
    return {
        "window_days": ROUTE_WINDOW_DAYS,
        "window_start": cutoff.isoformat(),
        "rows": selected[:MAX_ROUTE_ROWS],
        "row_total": route_total,
        "row_returned": min(route_total, MAX_ROUTE_ROWS),
        "row_omitted": max(0, route_total - MAX_ROUTE_ROWS),
        "seats": seats[:MAX_ROUTE_SEATS],
        "seat_omitted": max(0, len(seats) - MAX_ROUTE_SEATS),
        "truncated": route_truncated,
        "truncation_note": note,
    }


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
    source_events = raw.get("events") if isinstance(raw.get("events"), list) else []
    source_agents = snapshot.get("agents")
    agents_by_id = (
        {
            str(agent.get("agent_id")): agent
            for agent in source_agents
            if isinstance(agent, dict) and agent.get("agent_id")
        }
        if isinstance(source_agents, list)
        else {}
    )
    handoffs_by_ticket = _latest_handoffs_by_ticket(raw.get("handoff_memories"))
    tickets = [
        _detail_ticket(
            item,
            source_events,
            agents_by_id=agents_by_id,
            handoff=handoffs_by_ticket.get(str(item.get("ticket_id") or "")),
        )
        for item in source_tickets
        if isinstance(item, dict)
    ]
    status_rank = {
        **{status: 0 for status in ACTIVE_CLAIM_STATES},
        **{status: 1 for status in SUBMITTED_STATES},
        "open": 2,
    }
    tickets.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    tickets.sort(key=lambda item: status_rank.get(item["status"], 3))

    routes = assemble_provenance(
        snapshot,
        source_events,
        now=now,
        event_window_truncated=bool(raw.get("event_window_truncated")),
    )
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
        "event_window_truncated": bool(raw.get("event_window_truncated")),
        "event_resync_required": bool(raw.get("event_resync_required")),
        "routes": routes,
        "coordinator_findings": project_coordinator_findings(snapshot),
        "coordinator_findings_stale": coordinator_findings_stale(
            snapshot, now=now
        ),
        "snapshot_truncation": snapshot.get("_snapshot_truncation"),
        "ticket_total": max(snapshot_ticket_total, len(source_tickets)),
        "ticket_returned": min(len(tickets), MAX_DETAIL_TICKET_ROWS),
        "ticket_omitted": 0,
        "truncated": bool(
            snapshot.get("truncated") or len(tickets) > MAX_DETAIL_TICKET_ROWS
        ),
        "bounds": {
            "snapshot_items_per_collection": SNAPSHOT_LIMIT,
            "snapshot_bytes": SNAPSHOT_MAX_BYTES,
            "api_bytes": API_MAX_BYTES,
            "description_chars": MAX_DESCRIPTION_CHARS,
            "required_fields_per_ticket": MAX_REQUIRED_FIELDS,
            "events": DETAIL_EVENT_SCAN_LIMIT,
            "route_rows": MAX_ROUTE_ROWS,
            "route_seats": MAX_ROUTE_SEATS,
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
    route_rows_trimmed = False
    while len(_json_bytes(result)) > API_MAX_BYTES and result["routes"]["rows"]:
        result["routes"]["rows"].pop()
        result["routes"]["row_returned"] = len(result["routes"]["rows"])
        result["routes"]["row_omitted"] = max(
            0,
            result["routes"]["row_total"] - result["routes"]["row_returned"],
        )
        result["routes"]["truncated"] = True
        result["truncated"] = True
        route_rows_trimmed = True
    if route_rows_trimmed:
        result["routes"]["truncation_note"] += (
            " Additional route rows were omitted by the dashboard API byte cap."
        )
    if len(_json_bytes(result)) > API_MAX_BYTES:
        raise ValueError("detail projection metadata exceeds API byte cap")
    return result


def aggregate_fleet(
    board_rows: list[dict[str, Any]],
    *,
    stale_seconds: int,
    now: datetime | None = None,
    seat_definitions: dict[str, dict[str, Any]] | None = None,
    active_registry_boards: list[str] | None = None,
) -> dict[str, Any]:
    """Build the bounded API projection from already-bounded board reads."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    boards: list[dict[str, Any]] = []
    inactive_agents: list[dict[str, Any]] = []

    for raw in board_rows[:MAX_BOARDS]:
        board_id = _clip(raw.get("board_id"), MAX_LABEL_CHARS)
        label = _clip(raw.get("label") or board_id, MAX_LABEL_CHARS)
        activity_window_seconds = raw.get("activity_window_seconds")
        if (
            isinstance(activity_window_seconds, bool)
            or not isinstance(activity_window_seconds, (int, float))
            or activity_window_seconds <= 0
        ):
            activity_window_seconds = stale_seconds
        activity_window_seconds = int(activity_window_seconds)
        error = raw.get("error")
        if error:
            boards.append(
                {
                    "board_id": board_id,
                    "label": label,
                    "status": "error",
                    "error": _clip(error, MAX_LABEL_CHARS),
                    "counts": {
                        "open": 0,
                        "claimed": 0,
                        "submitted": 0,
                        "closed_today": 0,
                    },
                    "tickets": [],
                    "events": [],
                    "activity_window_seconds": activity_window_seconds,
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
        coordinator_seen_at: datetime | None = None

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
            lifecycle = str(agent.get("lifecycle_status", "active"))
            if lifecycle in {"retired", "stale"}:
                inactive_agents.append(
                    {
                        "agent_id": _clip(agent_id, MAX_LABEL_CHARS) or None,
                        "principal_id": _clip(principal_id, MAX_LABEL_CHARS),
                        "agent_name": _clip(agent_name, MAX_LABEL_CHARS),
                        "board_id": board_id,
                        "project": label,
                        "role": _clip(
                            agent.get("role") or agent.get("membership_role"), 32
                        ) or None,
                        "lifecycle_status": lifecycle,
                        "last_seen": seen_at.isoformat() if seen_at else None,
                    }
                )
                continue
            if (
                "coordinator" in agent_name.lower()
                and seen_at is not None
                and (coordinator_seen_at is None or seen_at > coordinator_seen_at)
            ):
                coordinator_seen_at = seen_at
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
                    "live": False,
                    "dispatch_ready": False,
                },
            )
            group["boards"].add(board_id)
            if isinstance(agent_id, str) and agent_id:
                group["agent_ids_by_board"].setdefault(board_id, set()).add(agent_id)
            current = current_by_agent.get(str(agent_id or ""))
            seat_projection = {
                "board_id": board_id,
                "project": label,
                "role": _clip(agent.get("role") or agent.get("membership_role"), 32)
                or None,
                "current_ticket_id": (
                    _clip(current.get("ticket_id"), MAX_LABEL_CHARS)
                    if current is not None
                    else None
                ),
                "current_ticket_title": (
                    _clip(current.get("title") or "Not supplied", MAX_TITLE_CHARS)
                    if current is not None
                    else None
                ),
                "current_ticket_status": (
                    _clip(current.get("status") or "unknown", 32)
                    if current is not None
                    else None
                ),
                "lease_expires_at": (
                    _clip(
                        (
                            current.get("review_lease", {}).get("expires_at")
                            if isinstance(current.get("review_lease"), dict)
                            and current.get("review_lease", {}).get(
                                "reviewer_agent_id"
                            )
                            == agent_id
                            else current.get("lease_expires_at")
                        ),
                        40,
                    )
                    or None
                    if current is not None
                    else None
                ),
                "last_seen": seen_at.isoformat() if seen_at else None,
            }
            if "capabilities" in agent:
                seat_projection["capabilities"] = (
                    agent.get("capabilities")
                    if isinstance(agent.get("capabilities"), dict)
                    else {}
                )
                tier = seat_projection["capabilities"].get("tier_max")
                if tier in {1, 2, 3}:
                    seat_projection["tier"] = tier
            agent_platform = agent.get("agent_platform")
            if isinstance(agent_platform, str) and agent_platform:
                seat_projection["client"] = _clip(agent_platform, 32)
            elif isinstance(seat_projection.get("capabilities"), dict):
                host = seat_projection["capabilities"].get("host")
                if isinstance(host, str) and host:
                    seat_projection["client"] = _clip(host, 32)
            if "current_offer" in agent:
                seat_projection["current_offer"] = (
                    agent.get("current_offer")
                    if isinstance(agent.get("current_offer"), dict)
                    else None
                )
            readiness = agent.get("readiness")
            if isinstance(readiness, dict):
                seat_projection["readiness"] = readiness
                if readiness.get("dispatch_ready") is True:
                    group["dispatch_ready"] = True
            else:
                # Older Central projections predate readiness. They retain the
                # legacy dispatch contract until they explicitly report state.
                group["dispatch_ready"] = True
            group["seats"][board_id] = seat_projection
            is_live = (
                seen_at is not None
                and (now - seen_at).total_seconds() <= activity_window_seconds
            )
            if (
                is_live
                and agent.get("status") in {"working", "busy"}
                and agent.get("lifecycle_status")
                not in {"handed_off", "inactive"}
            ):
                group["busy"] = True
            if seen_at is not None and (
                group["last_seen"] is None or seen_at > group["last_seen"]
            ):
                group["last_seen"] = seen_at
            if is_live:
                group["live"] = True

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
                        "status_label": _clip(_ticket_status_label(ticket, now), 64),
                        "claimed_by": _clip(claimed_by, MAX_LABEL_CHARS) or None,
                        "claim_age_s": _nonnegative_int(
                            ticket.get("claim_age_s")
                        ),
                        "lease_renewal_source": (
                            ticket.get("lease_renewal_source")
                            if ticket.get("lease_renewal_source")
                            in {"model", "keepalive"}
                            else None
                        ),
                        "lease_keepalive_only_age_s": _nonnegative_int(
                            ticket.get("lease_keepalive_only_age_s")
                        ),
                        "ttl_s": _nonnegative_int(ticket.get("ttl_s")),
                        "updated_at": _clip(ticket.get("updated_at"), 40) or None,
                        "abandoned_count": max(
                            0, int(ticket.get("abandoned_count", 0) or 0)
                        ),
                        "tier": ticket.get("tier")
                        if ticket.get("tier") in {1, 2, 3}
                        else 2,
                        "skills_required": ticket.get("skills_required")
                        if isinstance(ticket.get("skills_required"), list)
                        else [],
                        "dispatch_state": ticket.get("dispatch_state")
                        if isinstance(ticket.get("dispatch_state"), dict)
                        else None,
                        "dispatch_history": ticket.get("dispatch_history")
                        if isinstance(ticket.get("dispatch_history"), list)
                        else [],
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
        human_rows: list[dict[str, Any]] = []
        raw_human = raw.get("human_requests")
        if isinstance(raw_human, list):
            for record in raw_human[:10]:
                if not isinstance(record, dict):
                    continue
                form_safe, safety_reason = human_form_safety(
                    record.get("message"), record.get("requested_schema")
                )
                human_rows.append(
                    {
                        "ticket_id": _clip(record.get("ticket_id"), MAX_LABEL_CHARS) or None,
                        "request_id": _clip(record.get("request_id"), MAX_LABEL_CHARS) or None,
                        "message": _clip(record.get("message"), 500),
                        "kind": _clip(record.get("kind"), 32) or None,
                        "asked_by": _clip(record.get("asked_by"), MAX_LABEL_CHARS) or None,
                        "asked_at": _clip(record.get("asked_at"), 40) or None,
                        "requested_schema": (
                            record.get("requested_schema")
                            if isinstance(record.get("requested_schema"), dict)
                            else None
                        ),
                        "url": _clip(record.get("url"), 2048) or None,
                        "form_safe": form_safe,
                        "safety_reason": safety_reason,
                    }
                )
        boards.append(
            {
                "board_id": board_id,
                "label": label,
                "status": "ready",
                "counts": rendered_counts,
                "human_requests": human_rows,
                "tickets": ticket_rows[:MAX_TICKET_ROWS],
                "events": events,
                "coordinator_heartbeat": (
                    coordinator_seen_at.isoformat() if coordinator_seen_at else None
                ),
                "coordinator_findings": project_coordinator_findings(snapshot),
                "coordinator_findings_stale": coordinator_findings_stale(
                    snapshot, now=now
                ),
                "stale_after_days": (
                    snapshot.get("board", {}).get("stale_after_days", 3)
                    if isinstance(snapshot.get("board"), dict) else 3
                ),
                "activity_window_seconds": activity_window_seconds,
                "snapshot_truncation": snapshot.get("_snapshot_truncation"),
                "truncated": bool(
                    snapshot.get("truncated") or len(ticket_rows) > MAX_TICKET_ROWS
                ),
            }
        )

    names_to_groups: dict[str, set[tuple[str, str]]] = {}
    for key, group in groups.items():
        names_to_groups.setdefault(group["agent_name"], set()).add(key)
    agent_rows: list[dict[str, Any]] = []
    scope_available = (
        active_registry_boards is not None or seat_definitions is not None
    )
    registry_boards = sorted(set(active_registry_boards or []))
    definitions = seat_definitions or {}
    for group in groups.values():
        last_seen = group["last_seen"]
        agent_ids = sorted(
            {
                agent_id
                for board_agent_ids in group["agent_ids_by_board"].values()
                for agent_id in board_agent_ids
            }
        )
        if group["busy"]:
            status = "busy"
        elif group["live"] and group["dispatch_ready"]:
            status = "available"
        elif group["live"]:
            status = "connected"
        else:
            status = "stale"
        definition = definitions.get(group["agent_name"])
        if (
            not isinstance(definition, dict)
            or definition.get("principal_id") != group["principal_id"]
        ):
            definition = None
        joined_boards = sorted(group["boards"])
        observed_roles = sorted(
            {
                str(seat.get("role"))
                for seat in group["seats"].values()
                if seat.get("role")
            }
        )
        if definition is None:
            board_scope = {
                "mode": "unconfigured",
                "status": "misconfigured",
                "active_boards": registry_boards,
                "joined_boards": joined_boards,
                "missing_boards": [],
                "extra_boards": [],
            }
        else:
            mode = definition["board_mode"]
            expected = registry_boards if mode == "registry" else sorted(set(mode))
            missing = sorted(set(expected) - set(joined_boards))
            extra = sorted(set(joined_boards) - set(expected))
            role_mismatch = any(role != definition["role"] for role in observed_roles)
            if mode == "registry":
                if missing:
                    scope_status = "partial"
                elif extra or role_mismatch:
                    scope_status = "misconfigured"
                else:
                    scope_status = "full"
            else:
                scope_status = (
                    "misconfigured" if missing or extra or role_mismatch else "explicit"
                )
            board_scope = {
                "mode": "registry" if mode == "registry" else "explicit",
                "status": scope_status,
                "active_boards": expected,
                "joined_boards": joined_boards,
                "missing_boards": missing,
                "extra_boards": extra,
            }
        agent_rows.append(
            {
                "agent_id": agent_ids[0] if len(agent_ids) == 1 else None,
                "principal_id": group["principal_id"],
                "agent_name": group["agent_name"],
                "boards": joined_boards,
                "seats": sorted(
                    group["seats"].values(),
                    key=lambda item: (item["project"], item["board_id"]),
                ),
                "duplicate_name": len(names_to_groups.get(group["agent_name"], set()))
                > 1
                or any(
                    len(agent_ids) > 1
                    for agent_ids in group["agent_ids_by_board"].values()
                ),
                "last_seen": last_seen.isoformat() if last_seen else None,
                "pool_status": status,
                **({"board_scope": board_scope} if scope_available else {}),
            }
        )
    rank = {"busy": 0, "available": 1, "connected": 2, "stale": 3}
    agent_rows.sort(key=lambda item: (rank[item["pool_status"]], item["agent_name"]))
    busy = sum(item["pool_status"] == "busy" for item in agent_rows)
    available = sum(item["pool_status"] == "available" for item in agent_rows)
    connected = sum(item["pool_status"] == "connected" for item in agent_rows)
    stale = sum(item["pool_status"] == "stale" for item in agent_rows)
    unknown_model = sum(
        not isinstance(seat.get("capabilities", {}).get("model"), str)
        or not seat.get("capabilities", {}).get("model", "").strip()
        for group in groups.values()
        for seat in group["seats"].values()
    )
    agent_rows = agent_rows[:MAX_AGENT_ROWS]
    return {
        "generated_at": now.isoformat(),
        "stale_after_seconds": stale_seconds,
        "activity_window_multiplier": DISPATCH_ACTIVITY_WINDOW_MULTIPLIER,
        "pool_summary": {
            "online": busy + available + connected,
            "busy": busy,
            "available": available,
            "connected": connected,
            "stale": stale,
            "unknown_model": unknown_model,
        },
        "agents": agent_rows,
        "inactive_agents": sorted(
            inactive_agents,
            key=lambda item: (item["project"], item["agent_name"]),
        )[:MAX_AGENT_ROWS],
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


def dispatch_missing_capabilities(
    ticket: dict[str, Any], agents: list[dict[str, Any]], kind: str
) -> list[str]:
    """Explain which declared capability blocks every visible seat."""
    capability_rows = [
        row.get("capabilities")
        for row in agents
        if isinstance(row, dict) and isinstance(row.get("capabilities"), dict)
    ]
    gate = "can_review" if kind == "review" else "can_work"
    candidates = [row for row in capability_rows if row.get(gate) is True]
    missing: list[str] = []
    if not candidates:
        missing.append(gate)
    tier = ticket.get("tier") if ticket.get("tier") in {1, 2, 3} else 2
    if not any(row.get("tier_max", 0) >= tier for row in candidates):
        missing.append(f"tier_max>={tier}")
    required = ticket.get("skills_required")
    required = required if isinstance(required, list) else []
    for skill in required:
        if isinstance(skill, str) and not any(
            skill in row.get("skills", []) for row in candidates
        ):
            missing.append(f"skill:{skill}")
    return missing


def door_principal_id(
    board_id: str,
    role: str,
    central_url: str,
    issuer: str | None = None,
) -> str:
    actual_issuer = issuer if issuer is not None else door_admin.default_issuer(central_url)
    client_id = f"door-{board_id}-{role}"
    subject = f"door:{board_id}:{role}"
    canonical = json.dumps([client_id, actual_issuer, subject], separators=(",", ":"))
    return "PR-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _client_call(client: Any, name: str, arguments: dict[str, Any]) -> Any:
    method = getattr(client, name, None)
    if callable(method):
        try:
            return await method(**arguments)
        except TypeError:
            return await method()
    if hasattr(client, "_call"):
        return await client._call(name, arguments)
    raise AttributeError(f"Client {type(client).__name__} does not support {name}")


class _FleetAsyncRuntime:
    """One process-wide event loop for bounded persistent Central sessions."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name="fleet-dashboard-central-sessions",
            daemon=True,
        )
        self.thread.start()
        self.ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()

    def submit(self, awaitable: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(awaitable, self.loop)


_FLEET_RUNTIME: _FleetAsyncRuntime | None = None
_FLEET_RUNTIME_LOCK = threading.Lock()


def _fleet_runtime() -> _FleetAsyncRuntime:
    global _FLEET_RUNTIME
    with _FLEET_RUNTIME_LOCK:
        if _FLEET_RUNTIME is None:
            _FLEET_RUNTIME = _FleetAsyncRuntime()
        return _FLEET_RUNTIME


@dataclass
class _FleetBoardSession:
    manager: Any
    client: Any
    lock: asyncio.Lock


def _reconnectable_client_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, EOFError, TimeoutError)):
        return True
    if isinstance(exc, BoardClientError):
        return False
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "connection closed",
            "client is closed",
            "server disconnected",
            "session terminated",
            "stream ended",
        )
    )


class _FleetClientPool:
    """Keep one serialized BoardClient session per board and reconnect once."""

    def __init__(self, config: Config, client_factory: Callable[..., Any]) -> None:
        self.config = config
        self.client_factory = client_factory
        self._sessions: dict[str, _FleetBoardSession] = {}
        self._lock: asyncio.Lock | None = None
        self._closed = False

    def options(self) -> dict[str, Any]:
        return {
            "agent_name": self.config.agent_name,
            "role": "worker",
            "capabilities": dict(DASHBOARD_CAPABILITIES),
            "agent_platform": DASHBOARD_AGENT_PLATFORM,
            "task_focus": DASHBOARD_TASK_FOCUS,
            "allow_takeover": True,
        }

    async def _session(self, board_id: str) -> _FleetBoardSession:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._closed:
                raise RuntimeError("fleet dashboard Central session pool is closed")
            session = self._sessions.get(board_id)
            if session is not None:
                return session
            manager = self.client_factory(
                self.config.url,
                self.config.token,
                board_id,
                **self.options(),
            )
            client = await manager.__aenter__()
            session = _FleetBoardSession(manager, client, asyncio.Lock())
            self._sessions[board_id] = session
            return session

    async def _drop(
        self, board_id: str, expected: _FleetBoardSession | None = None
    ) -> None:
        if self._lock is None:
            return
        async with self._lock:
            session = self._sessions.get(board_id)
            if session is None or (expected is not None and session is not expected):
                return
            self._sessions.pop(board_id, None)
        await session.manager.__aexit__(None, None, None)

    async def call(
        self, board_id: str, method_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        session = await self._session(board_id)
        async with session.lock:
            try:
                return await getattr(session.client, method_name)(*args, **kwargs)
            except BaseException as exc:
                if not _reconnectable_client_error(exc):
                    raise
                await self._drop(board_id, session)
        replacement = await self._session(board_id)
        async with replacement.lock:
            return await getattr(replacement.client, method_name)(*args, **kwargs)

    async def identity(self, board_id: str) -> Any:
        session = await self._session(board_id)
        async with session.lock:
            return session.client.identity

    async def _close(self) -> None:
        if self._lock is None:
            self._closed = True
            return
        async with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.manager.__aexit__(None, None, None)

    def close(self) -> None:
        _fleet_runtime().submit(self._close()).result(timeout=15)


class _FleetClientProxy:
    def __init__(self, pool: _FleetClientPool, board_id: str) -> None:
        self.pool = pool
        self.board_id = board_id
        self.agent_name = pool.config.agent_name
        self.role = "worker"
        self.capabilities = dict(DASHBOARD_CAPABILITIES)
        self.agent_platform = DASHBOARD_AGENT_PLATFORM
        self.task_focus = DASHBOARD_TASK_FOCUS
        self.allow_takeover = True
        self.allow_matching_takeover = False

    async def __aenter__(self) -> _FleetClientProxy:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def joined_identity(self) -> Any:
        future = _fleet_runtime().submit(self.pool.identity(self.board_id))
        return await asyncio.wrap_future(future)

    def __getattr__(self, method_name: str) -> Callable[..., Awaitable[Any]]:
        async def forwarded(*args: Any, **kwargs: Any) -> Any:
            future = _fleet_runtime().submit(
                self.pool.call(self.board_id, method_name, args, kwargs)
            )
            return await asyncio.wrap_future(future)

        return forwarded


class FleetFetcher:
    def __init__(
        self,
        config: Config,
        client_factory: Callable[..., Any] = BoardClient,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.client_factory = client_factory
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._intake_write_lock = threading.Lock()
        self._butler_write_lock = threading.Lock()
        self._intake_submissions: dict[str, list[tuple[str, datetime]]] = {}
        self._board_work_dirs: dict[str, str | None] = {}
        self._readable_boards: list[tuple[str, str]] = []
        self._excluded_readable_boards: list[dict[str, str]] = []
        self._configured_but_unreadable: list[str] = []
        self._active_registry_boards: list[str] = [config.home_board]
        self._seat_definitions: dict[str, dict[str, Any]] = {}
        self._client_pool = _FleetClientPool(config, client_factory)

    def enable_client_reuse(self) -> None:
        """Keep one joined client per board for this viewer process."""

        # This candidate's bounded session pool always provides process reuse.
        return None

    def _client(self, board_id: str) -> Any:
        return _FleetClientProxy(self._client_pool, board_id)

    def close(self) -> None:
        self._client_pool.close()

    async def _boards(self) -> list[tuple[str, str]]:
        async with self._client(self.config.home_board) as client:
            registry = await client.board_state_get(key="project_registry")
            try:
                seat_registry = await client.board_state_get(key="seat_registry")
                self._seat_definitions = parse_seat_registry(seat_registry)
            except Exception:  # noqa: BLE001 - older/read-only Centrals may omit it.
                self._seat_definitions = {}
            try:
                listed = await _client_call(client, "board_list", {})
            except (AttributeError, BoardClientError):
                listed = {"boards": []}
        self._board_work_dirs = parse_project_work_dirs(
            registry, self.config.home_board
        )
        registry_boards = parse_project_registry(registry, self.config.home_board)
        self._active_registry_boards = sorted(
            {board_id for _label, board_id in registry_boards}
        )
        labels = {board_id: label for label, board_id in registry_boards}
        readable = listed.get("boards") if isinstance(listed, dict) else None
        readable_ids: list[str] = []
        for row in readable if isinstance(readable, list) else []:
            if not isinstance(row, dict):
                continue
            board_id = row.get("board_id")
            if not isinstance(board_id, str) or not BOARD_ID_RE.fullmatch(board_id):
                continue
            labels.setdefault(board_id, board_id)
            if board_id not in readable_ids:
                readable_ids.append(board_id)
        self._readable_boards = [(labels[board_id], board_id) for board_id in readable_ids]
        configured_only = [
            (label, board_id)
            for label, board_id in registry_boards
            if board_id not in readable_ids
        ]
        candidates = self._readable_boards + configured_only
        included = candidates[:MAX_BOARDS]
        self._excluded_readable_boards = [
            {"board_id": board_id, "reason": "dashboard board limit"}
            for _label, board_id in self._readable_boards
            if (_label, board_id) not in included
        ]
        self._configured_but_unreadable = [board_id for _label, board_id in configured_only]
        return included

    async def fetch_project_registry(self) -> dict[str, Any]:
        """Return validated registry data plus a CAS digest for Config writes."""
        async with self._client(self.config.home_board) as client:
            result = await client.board_state_get(key="project_registry")
        state = result.get("state") if isinstance(result, dict) else None
        raw = state.get("value") if isinstance(state, dict) else None
        if not isinstance(raw, str):
            raise ValueError("project_registry state value is missing")
        try:
            registry = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("project_registry state value is not valid JSON") from exc
        parse_client_project_registry(result)
        return {
            "registry": registry,
            "expected_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }

    async def project_evidence_state(
        self, project_name: str, board_id: str
    ) -> dict[str, Any]:
        """Read bounded project/board state for a traced Add project action."""
        payload = await self.fetch_project_registry()
        registry = payload["registry"]
        projects = registry.get("projects") if isinstance(registry, dict) else None
        if not isinstance(projects, dict):
            raise ValueError("project registry has no projects mapping")
        project_entry = copy.deepcopy(projects.get(project_name))

        async with self._client(self.config.home_board) as home_client:
            listed = await _client_call(home_client, "board_list", {})
        boards = listed.get("boards") if isinstance(listed, dict) else None
        if not isinstance(boards, list):
            raise ValueError("board list has no boards array")
        board_rows = [
            copy.deepcopy(row)
            for row in boards
            if isinstance(row, dict)
            and row.get("board_id") in {self.config.home_board, board_id}
        ]
        target_present = any(row.get("board_id") == board_id for row in board_rows)
        target_state: dict[str, Any] | None = None
        if target_present:
            async with self._client(board_id) as target_client:
                members = await _client_call(target_client, "board_members", {})
                status = await _client_call(target_client, "board_status", {})
            target_state = {"members": members, "status": status}

        return {
            "registry": _bounded_evidence_digest(
                {"project": project_entry}, "project registry state"
            ),
            "board": _bounded_evidence_digest(
                {"board_rows": board_rows, "target": target_state},
                "project board state",
            ),
            "project_entry": project_entry,
        }

    async def save_project_registry(
        self, value: Any, expected_sha256: Any
    ) -> dict[str, Any]:
        if not isinstance(expected_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_sha256
        ):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        parse_client_project_registry({"state": {"value": encoded}})
        async with self._client(self.config.home_board) as client:
            result = await client._call(  # noqa: SLF001 - CAS is not in old clients.
                "board_state_update",
                {
                    "agent_name": self.config.agent_name,
                    "key": "project_registry",
                    "value": encoded,
                    "expected_sha256": expected_sha256,
                },
            )
        return {
            "ok": True,
            "registry": value,
            "result": result,
            "expected_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        }

    async def _board_event_feed(
        self,
        client: FleetClient,
        latest_seq: int,
        event_limit: int = EVENT_SCAN_LIMIT,
    ) -> dict[str, Any]:
        result = await client.board_catchup(
            cursor=max(0, latest_seq - event_limit),
            limit=event_limit,
            ack=False,
            max_events=event_limit,
            max_bytes=EVENT_MAX_BYTES,
        )
        events = result.get("events")
        return {
            "events": events if isinstance(events, list) else [],
            "resync_required": bool(result.get("resync_required")),
            "truncated": bool(result.get("truncated") or result.get("has_more")),
        }

    async def _read_board(
        self,
        label: str,
        board_id: str,
        event_limit: int = EVENT_SCAN_LIMIT,
        work_dir: str | None = None,
    ) -> dict[str, Any]:
        try:
            async with self._client(board_id) as client:
                snapshot = await client.board_snapshot(
                    limit=SNAPSHOT_LIMIT, max_bytes=SNAPSHOT_MAX_BYTES,
                    include_retired=True,
                )
                try:
                    status = await _client_call(
                        client, "board_status", {"include_retired": True}
                    )
                except (AttributeError, BoardClientError):
                    status = {}
                status_agents = status.get("agents") if isinstance(status, dict) else None
                snapshot_agents = snapshot.get("agents")
                agents_by_id = {
                    item.get("agent_id"): item
                    for item in (
                        snapshot_agents if isinstance(snapshot_agents, list) else []
                    )
                    if isinstance(item, dict)
                    and isinstance(item.get("agent_id"), str)
                }
                for item in status_agents if isinstance(status_agents, list) else []:
                    if not isinstance(item, dict):
                        continue
                    agent_id = item.get("agent_id")
                    if isinstance(agent_id, str):
                        agents_by_id[agent_id] = item
                snapshot["agents"] = list(agents_by_id.values())
                dispatch_policy = (
                    status.get("dispatch_policy") if isinstance(status, dict) else None
                )
                offer_ttl_s = (
                    dispatch_policy.get("offer_ttl_s")
                    if isinstance(dispatch_policy, dict)
                    else None
                )
                activity_window_seconds = (
                    int(DISPATCH_ACTIVITY_WINDOW_MULTIPLIER * offer_ttl_s)
                    if isinstance(offer_ttl_s, int) and not isinstance(offer_ttl_s, bool)
                    else self.config.stale_seconds
                )
                snapshot_tickets = snapshot.get("tickets")
                snapshot_tickets = (
                    snapshot_tickets if isinstance(snapshot_tickets, list) else []
                )
                initial_ids = {
                    item.get("ticket_id")
                    for item in snapshot_tickets
                    if isinstance(item, dict) and isinstance(item.get("ticket_id"), str)
                }
                total_counts = snapshot.get("total_counts")
                ticket_total = (
                    total_counts.get("tickets")
                    if isinstance(total_counts, dict)
                    and type(total_counts.get("tickets")) is int
                    else len(snapshot_tickets)
                )
                omitted_counts = snapshot.get("omitted_counts")
                ticket_omitted = (
                    _nonnegative_int(omitted_counts.get("tickets"))
                    if isinstance(omitted_counts, dict)
                    else max(0, ticket_total - len(snapshot_tickets))
                )
                hidden_active = 0
                event_feed = await self._board_event_feed(
                    client,
                    int(snapshot.get("latest_seq", 0)),
                    event_limit,
                )
                events = event_feed["events"]
                if snapshot.get("truncated") or ticket_omitted:
                    active_page = await client.ticket_list(
                        include_closed=False, limit=TICKET_LIST_LIMIT
                    )
                    active_tickets = active_page.get("tickets")
                    active_tickets = (
                        active_tickets if isinstance(active_tickets, list) else []
                    )
                    active_total = _nonnegative_int(
                        active_page.get("total_matching", len(active_tickets))
                    )
                    if active_total > len(active_tickets):
                        for status in ("open", "claimed", "submitted"):
                            page = await client.ticket_list(
                                status=status,
                                include_closed=False,
                                limit=TICKET_LIST_LIMIT,
                            )
                            rows = page.get("tickets")
                            if isinstance(rows, list):
                                active_tickets.extend(rows)
                    by_id = {
                        item.get("ticket_id"): item
                        for item in snapshot_tickets
                        if isinstance(item, dict)
                        and isinstance(item.get("ticket_id"), str)
                    }
                    for ticket in active_tickets:
                        if not isinstance(ticket, dict):
                            continue
                        ticket_id = ticket.get("ticket_id")
                        if not isinstance(ticket_id, str):
                            continue
                        by_id[ticket_id] = ticket
                    active_ids = {
                        ticket.get("ticket_id")
                        for ticket in active_tickets
                        if isinstance(ticket, dict)
                        and isinstance(ticket.get("ticket_id"), str)
                    }
                    hidden_active = max(0, active_total - len(active_ids))
                    event_ticket_ids = []
                    for event in reversed(events):
                        ticket_id = event.get("ticket_id") if isinstance(event, dict) else None
                        if (
                            isinstance(ticket_id, str)
                            and ticket_id not in by_id
                            and ticket_id not in event_ticket_ids
                        ):
                            event_ticket_ids.append(ticket_id)
                    for ticket_id in event_ticket_ids[:TICKET_LIST_LIMIT]:
                        try:
                            exact = await client.ticket_get(ticket_id)
                        except BoardClientError:
                            continue
                        ticket = exact.get("ticket")
                        if isinstance(ticket, dict):
                            by_id[ticket_id] = ticket
                    snapshot["tickets"] = list(by_id.values())
                snapshot["_snapshot_truncation"] = {
                    "returned": len(snapshot_tickets),
                    "total": max(ticket_total, len(snapshot_tickets)),
                    "omitted": ticket_omitted,
                    "hidden_active": hidden_active,
                }

                ticket_index = {
                    item.get("ticket_id"): item
                    for item in snapshot.get("tickets", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("ticket_id"), str)
                }
                verification: dict[str, dict[str, Any]] = {}
                for ticket_id in coordinator_finding_ticket_ids(
                    snapshot, "unverifiable-commit"
                ):
                    ticket = ticket_index.get(ticket_id)
                    if ticket is None:
                        try:
                            exact = await client.ticket_get(ticket_id)
                        except BoardClientError:
                            exact = {}
                        candidate = exact.get("ticket")
                        ticket = candidate if isinstance(candidate, dict) else {}
                    verification[ticket_id] = await asyncio.to_thread(
                        verify_ticket_commit_on_origin,
                        ticket,
                        work_dir,
                        now=self.now_factory(),
                    )
                snapshot["_commit_verification"] = verification
                latest_seq = int(snapshot.get("latest_seq", 0))
                human_requests: list[dict[str, Any]] = []
                try:
                    needs = await client.ticket_list(
                        status="needs_human", include_closed=False, limit=50
                    )
                    rows = needs.get("tickets") if isinstance(needs, dict) else None
                    for item in rows if isinstance(rows, list) else []:
                        if not isinstance(item, dict):
                            continue
                        ticket_id = item.get("ticket_id")
                        if not isinstance(ticket_id, str) or not ticket_id:
                            continue
                        record = item.get("human_request")
                        if not isinstance(record, (dict, list)):
                            try:
                                exact = await client.ticket_get(ticket_id)
                            except BoardClientError:
                                continue
                            ticket = exact.get("ticket")
                            if (
                                not isinstance(ticket, dict)
                                or ticket.get("status") != "needs_human"
                            ):
                                continue
                            record = ticket.get("human_request")
                        for rec in _pending_human_records(record):
                            asked_by = rec.get("asked_by")
                            form_safe, safety_reason = human_form_safety(
                                rec.get("message"), rec.get("requested_schema")
                            )
                            human_requests.append(
                                {
                                    "ticket_id": ticket_id,
                                    "request_id": str(rec.get("request_id")),
                                    "message": str(rec.get("message") or "")[:500],
                                    "kind": str(rec.get("kind") or "")[:32] or None,
                                    "asked_by": (
                                        asked_by.get("agent_name")
                                        if isinstance(asked_by, dict)
                                        else None
                                    ),
                                    "asked_at": rec.get("asked_at"),
                                    "requested_schema": (
                                        rec.get("requested_schema")
                                        if isinstance(rec.get("requested_schema"), dict)
                                        else None
                                    ),
                                    "url": (
                                        str(rec.get("url"))[:2048]
                                        if rec.get("url")
                                        else None
                                    ),
                                    "form_safe": form_safe,
                                    "safety_reason": safety_reason,
                                }
                            )
                except Exception:  # noqa: BLE001 - older centrals lack needs_human.
                    human_requests = []
                try:
                    handoff_memories = await client.memory_read(
                        memory_type="handoff", limit=MAX_HANDOFF_MEMORIES
                    )
                    if not isinstance(handoff_memories, list):
                        handoff_memories = []
                except Exception:  # noqa: BLE001 - older clients may lack memory_read.
                    handoff_memories = []
            return {
                "label": label,
                "board_id": board_id,
                "snapshot": snapshot,
                "events": events,
                "event_window_truncated": bool(
                    latest_seq > event_limit or event_feed["truncated"]
                ),
                "event_resync_required": event_feed["resync_required"],
                "human_requests": human_requests[:10],
                "handoff_memories": handoff_memories,
                "activity_window_seconds": activity_window_seconds,
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
            *(
                self._read_board(
                    label,
                    board_id,
                    work_dir=self._board_work_dirs.get(board_id),
                )
                for label, board_id in boards
            )
        )
        result = aggregate_fleet(
            rows,
            stale_seconds=self.config.stale_seconds,
            now=self.now_factory(),
            seat_definitions=self._seat_definitions,
            active_registry_boards=self._active_registry_boards,
        )
        covered = {
            row.get("board_id")
            for row in rows
            if isinstance(row, dict)
            and not row.get("error")
            and row.get("board_id")
            in {board_id for _label, board_id in self._readable_boards}
        }
        excluded = list(self._excluded_readable_boards)
        excluded.extend(
            {
                "board_id": str(row.get("board_id") or "unknown"),
                "reason": f"read unavailable: {row.get('error')}",
            }
            for row in rows
            if isinstance(row, dict)
            and row.get("error")
            and row.get("board_id")
            in {board_id for _label, board_id in self._readable_boards}
        )
        result["pool_scope"] = {
            "readable_boards": [board_id for _label, board_id in self._readable_boards],
            "covered_boards": sorted(board_id for board_id in covered if board_id),
            "excluded_boards": excluded,
            "configured_but_unreadable": self._configured_but_unreadable,
        }
        return result

    async def fetch_board(self, board_id: str) -> dict[str, Any]:
        if not BOARD_ID_RE.fullmatch(board_id):
            raise KeyError(board_id)
        boards = await self._boards()
        match = next((item for item in boards if item[1] == board_id), None)
        if match is None:
            raise KeyError(board_id)
        row = await self._read_board(
            match[0],
            match[1],
            event_limit=DETAIL_EVENT_SCAN_LIMIT,
            work_dir=self._board_work_dirs.get(match[1]),
        )
        if row.get("error"):
            raise RuntimeError(str(row["error"]))
        return project_board_detail(row)

    async def resolve_human_request(
        self, board_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve a pending needs_human request with the coordinator token."""
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        ticket_id = payload.get("ticket_id")
        request_id = payload.get("request_id")
        action = payload.get("action")
        if not isinstance(ticket_id, str) or not ticket_id:
            raise ValueError("ticket_id is required")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id is required")
        if action not in HUMAN_ACTION_VALUES:
            raise ValueError("action must be accept, decline, or cancel")
        disposition = payload.get("disposition")
        if disposition not in HUMAN_DISPOSITION_VALUES:
            disposition = "reopen" if action == "accept" else "park"
        content = payload.get("content")
        if content is not None and not isinstance(content, dict):
            raise ValueError("content must be an object")
        async with self._client(board_id) as client:
            result = await client.ticket_human_resolve(
                ticket_id,
                request_id=request_id,
                action=str(action),
                content=content,
                disposition=str(disposition),
            )
        return {
            "ok": True,
            "board_id": board_id,
            "ticket_id": ticket_id,
            "result": result,
        }

    async def fetch_dispatch(self, board_id: str) -> dict[str, Any]:
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        active = {active_board for _label, active_board in await self._boards()}
        if board_id not in active:
            raise ValueError("board_id is not registry-active")
        async with self._client(board_id) as client:
            status = await client.board_status()
            listed = await client.ticket_list(
                include_closed=False,
                limit=DISPATCH_TICKET_LIMIT,
                view="work",
            )
            dispatch_events = await client.board_dispatch_events(limit=25)
            events = dispatch_events.get("events", [])
        tickets = {
            row.get("ticket_id"): row
            for row in listed.get("tickets", [])
            if isinstance(row, dict) and isinstance(row.get("ticket_id"), str)
        }
        agents = status.get("agents") if isinstance(status.get("agents"), list) else []
        unassignable = []
        for row in status.get("unassignable_tickets", []):
            if not isinstance(row, dict):
                continue
            ticket_id = row.get("ticket_id")
            ticket = tickets.get(ticket_id, {})
            state = ticket.get("dispatch_state")
            kind = state.get("kind", "work") if isinstance(state, dict) else "work"
            unassignable.append(
                {
                    "ticket_id": ticket_id,
                    "title": _clip(ticket.get("title") or ticket_id, MAX_TITLE_CHARS),
                    "reason": _clip(row.get("reason") or "unknown", 80),
                    "missing": dispatch_missing_capabilities(ticket, agents, kind),
                }
            )
        offers = [
            {
                "agent_name": _clip(agent.get("agent_name"), MAX_LABEL_CHARS),
                **agent["current_offer"],
            }
            for agent in agents
            if isinstance(agent, dict) and isinstance(agent.get("current_offer"), dict)
        ]
        open_tickets = [
            {
                "ticket_id": t_id,
                "title": _clip(ticket.get("title") or t_id, MAX_TITLE_CHARS),
                "status": ticket.get("status"),
                "dispatch_state": ticket.get("dispatch_state")
                if isinstance(ticket.get("dispatch_state"), dict)
                else None,
                "dispatch_summary": ticket.get("dispatch_summary")
                if isinstance(ticket.get("dispatch_summary"), dict)
                else None,
                "dispatch_history": [
                    {
                        "state": h.get("state"),
                        "agent_id": h.get("agent_id"),
                        "agent_name": h.get("agent_name"),
                        "at": h.get("at") or h.get("offered_at"),
                        "reason": h.get("reason"),
                    }
                    for h in (
                        ticket.get("dispatch_history")
                        or (
                            ticket.get("dispatch_summary", {}).get("last", [])
                            if isinstance(ticket.get("dispatch_summary"), dict)
                            else []
                        )
                    )
                    if isinstance(h, dict)
                ],
            }
            for t_id, ticket in tickets.items()
            if isinstance(ticket, dict) and ticket.get("status") in {"open", "submitted"}
        ]
        offer_kinds = {
            "ticket_offered",
            "review_offered",
            "offer_expired",
            "offer_revoked",
            "dispatch_unassignable",
        }
        timeline = [
            {
                "seq": event.get("seq"),
                "kind": event.get("kind"),
                "ticket_id": event.get("ticket_id"),
                "occurred_at": event.get("occurred_at"),
            }
            for event in events
            if isinstance(event, dict) and event.get("kind") in offer_kinds
        ][-25:]
        return {
            "board_id": board_id,
            "claim_ttl_s": status.get("claim_ttl_s", 900),
            "dispatch_policy": status.get("dispatch_policy", {}),
            "unassignable_tickets": unassignable,
            "unclaimed_tickets": status.get("unclaimed_tickets", []),
            "offers": offers,
            "open_tickets": open_tickets,
            "timeline": timeline,
        }

    async def save_dispatch(self, board_id: str, payload: Any) -> dict[str, Any]:
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        if not isinstance(payload, dict) or set(payload) != {
            "claim_ttl_s",
            "offer_ttl_s",
            "broadcast_reoffer_s",
            "second_opinion",
            "fallback_broadcast",
        }:
            raise ValueError("dispatch policy fields are invalid")
        offer_ttl = payload["offer_ttl_s"]
        broadcast_reoffer_s = payload["broadcast_reoffer_s"]
        claim_ttl = payload["claim_ttl_s"]
        if (
            isinstance(offer_ttl, bool)
            or not isinstance(offer_ttl, int)
            or not 1 <= offer_ttl <= 86_400
        ):
            raise ValueError("offer_ttl_s must be between 1 and 86400")
        if (
            isinstance(claim_ttl, bool)
            or not isinstance(claim_ttl, int)
            or not 1 <= claim_ttl <= 86_400
        ):
            raise ValueError("claim_ttl_s must be between 1 and 86400")
        if (
            isinstance(broadcast_reoffer_s, bool)
            or not isinstance(broadcast_reoffer_s, int)
            or not 60 <= broadcast_reoffer_s <= 86_400
        ):
            raise ValueError("broadcast_reoffer_s must be between 60 and 86400")
        if not isinstance(payload["second_opinion"], bool) or not isinstance(
            payload["fallback_broadcast"], bool
        ):
            raise ValueError("dispatch policy booleans are invalid")
        active = {active_board for _label, active_board in await self._boards()}
        if board_id not in active:
            raise ValueError("board_id is not registry-active")
        async with self._client(board_id) as client:
            policy_result = await client.board_dispatch_policy_set(
                offer_ttl_s=offer_ttl,
                broadcast_reoffer_s=broadcast_reoffer_s,
                second_opinion=payload["second_opinion"],
                fallback_broadcast=payload["fallback_broadcast"],
            )
            ttl_result = await client.board_claim_ttl_set(claim_ttl)
        return {
            **policy_result,
            "claim_ttl_s": ttl_result.get("claim_ttl_s", claim_ttl),
            "previous_claim_ttl_s": ttl_result.get("previous_claim_ttl_s"),
        }

    async def retire_agent(self, board_id: str, agent_id: str) -> dict[str, Any]:
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("invalid agent_id")
        active = {active_board for _label, active_board in await self._boards()}
        if board_id not in active:
            raise ValueError("board_id is not registry-active")
        async with self._client(board_id) as client:
            return await client.agent_retire(agent_id)

    async def retire_inert(self, board_id: str) -> dict[str, Any]:
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        active = {active_board for _label, active_board in await self._boards()}
        if board_id not in active:
            raise ValueError("board_id is not registry-active")
        async with self._client(board_id) as client:
            return await client.agent_retire_inert()

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
        effective = findings.get("effective_config", {})
        effective = (
            json.loads(json.dumps(effective)) if isinstance(effective, dict) else {}
        )
        sources = findings.get("config_sources", {})
        sources = dict(sources) if isinstance(sources, dict) else {}
        stored_thresholds = stored.get("thresholds") if stored else None
        pressure = context_pressure_thresholds(stored_thresholds)
        effective_thresholds = effective.get("thresholds")
        if isinstance(effective_thresholds, dict):
            effective_thresholds.update(pressure)
            for name in CONFIG_PRESSURE_FIELDS:
                sources.setdefault(
                    f"thresholds.{name}",
                    "config"
                    if isinstance(stored_thresholds, dict) and name in stored_thresholds
                    else "default",
                )
        return {
            "config": stored,
            "effective": effective,
            "sources": sources,
            "mode": findings.get("effective_mode", "unknown"),
            "updated_at": stored.get("updated_at") if stored else None,
            "updated_by": stored.get("updated_by") if stored else None,
            "expected_sha256": (
                hashlib.sha256(stored_text.encode("utf-8")).hexdigest()
                if stored_text is not None
                else None
            ),
            "concurrency": "cas" if stored_text is not None else "lww",
        }

    async def fetch_autonomous_butler(self, board_id: str) -> dict[str, Any]:
        """Read config, commands, and the product-owned actual-state projection."""
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        active = {active_board for _label, active_board in await self._boards()}
        if board_id not in active:
            raise ValueError("board_id is not registry-active")
        async with self._client(board_id) as client:
            config = await client.butler_config_get()
            commands = await client.butler_command_inspect(limit=50)
            try:
                raw_state = await client.board_state_get(
                    key=AUTONOMOUS_FLEET_STATE_KEY
                )
            except BoardClientError as exc:
                if "state key not found" not in str(exc).casefold():
                    raise
                raw_state = {}
        actual_state, _raw_text = _state_value(raw_state)
        return autonomous_butler_view(config, commands, actual_state)

    async def save_autonomous_butler(
        self, board_id: str, request: Any
    ) -> dict[str, Any]:
        """CAS-write only the mutable desired section of a provisioned config."""
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        active = {active_board for _label, active_board in await self._boards()}
        if board_id not in active:
            raise ValueError("board_id is not registry-active")
        async with self._client(board_id) as client:
            current = await client.butler_config_get()
            updated, expected, mutation_id = prepare_autonomous_butler_config(
                current, request
            )
            try:
                saved = await client.butler_config_set(
                    mutation_id, "human", updated, expected
                )
            except BoardClientError as exc:
                if "cas conflict" in str(exc).casefold():
                    raise ConfigConflictError(
                        "Autonomous Butler config changed; reload before saving"
                    ) from exc
                raise
            config = await client.butler_config_get()
            commands = await client.butler_command_inspect(limit=50)
            try:
                raw_state = await client.board_state_get(
                    key=AUTONOMOUS_FLEET_STATE_KEY
                )
            except BoardClientError as exc:
                if "state key not found" not in str(exc).casefold():
                    raise
                raw_state = {}
        actual_state, _raw_text = _state_value(raw_state)
        return {
            **autonomous_butler_view(config, commands, actual_state),
            "mutation": {
                "idempotent_replay": saved.get("idempotent_replay") is True,
                "rollback_evidence": saved.get("rollback_evidence"),
            },
        }

    async def submit_autonomous_butler_command(
        self, board_id: str, request: Any
    ) -> dict[str, Any]:
        """Submit one allowlisted human command; never accept raw tool or shell data."""
        clean = validate_autonomous_command_request(request)
        if clean["board_id"] != board_id or not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        boards = await self._boards()
        labels = {active_board: label for label, active_board in boards}
        if board_id not in labels:
            raise ValueError("board_id is not registry-active")
        now = self.now_factory()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        expires_at = (now.astimezone(timezone.utc) + timedelta(minutes=5)).isoformat()
        project_id = labels[board_id]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", project_id):
            project_id = board_id
        async with self._client(board_id) as client:
            current = await client.butler_config_get()
            if current.get("revision", 0) != clean["expected_config_revision"]:
                raise ConfigConflictError(
                    "Autonomous Butler config changed; refresh before sending the command"
                )
            try:
                result = await client.butler_command_submit(
                    clean["request_id"],
                    project_id,
                    "human",
                    clean["intent"],
                    clean["parameters"],
                    clean["expected_config_revision"],
                    expires_at,
                    priority="emergency" if clean["intent"] == "kill" else "normal",
                )
            except BoardClientError as exc:
                if "config precondition failed" in str(exc).casefold():
                    raise ConfigConflictError(
                        "Autonomous Butler config changed; refresh before sending the command"
                    ) from exc
                raise
            commands = await client.butler_command_inspect(limit=50)
            try:
                raw_state = await client.board_state_get(
                    key=AUTONOMOUS_FLEET_STATE_KEY
                )
            except BoardClientError as exc:
                if "state key not found" not in str(exc).casefold():
                    raise
                raw_state = {}
        actual_state, _raw_text = _state_value(raw_state)
        return {
            **autonomous_butler_view(current, commands, actual_state),
            "submitted_command": result.get("command"),
            "idempotent_replay": result.get("idempotent_replay") is True,
        }

    async def fetch_intake(self, board_id: str) -> dict[str, Any]:
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        active = {active_board for _label, active_board in await self._boards()}
        if board_id not in active:
            raise ValueError("board_id is not registry-active")
        async with self._client(board_id) as client:
            try:
                raw = await client.board_state_get(key=INTAKE_STATE_KEY)
            except BoardClientError as exc:
                if "state key not found" not in str(exc):
                    raise
                raw = {}
        rows, tombstones, current_text = _intake_state_value(raw, board_id)
        return {
            "board_id": board_id,
            "waiting": rows,
            "declined": tombstones,
            "expected_sha256": (
                hashlib.sha256(current_text.encode("utf-8")).hexdigest()
                if current_text is not None
                else None
            ),
            "rate_limit": {
                "asks": INTAKE_RATE_LIMIT,
                "window_seconds": INTAKE_RATE_WINDOW_SECONDS,
            },
        }

    async def save_intake(self, board_id: Any, text: Any) -> dict[str, Any]:
        if not isinstance(board_id, str) or not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        clean_text = validate_intake_text(text)
        active = {active_board for _label, active_board in await self._boards()}
        if board_id not in active:
            raise ValueError("board_id is not registry-active")
        now = self.now_factory()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        cutoff = now - timedelta(seconds=INTAKE_RATE_WINDOW_SECONDS)

        # One process-side critical section makes concurrent dashboard requests
        # deterministic. Central's expected_sha256 remains the cross-process gate.
        with self._intake_write_lock:
            async with self._client(board_id) as client:
                try:
                    raw = await client.board_state_get(key=INTAKE_STATE_KEY)
                except BoardClientError as exc:
                    if "state key not found" not in str(exc):
                        raise
                    raw = {}
                rows, tombstones, current_text = _intake_state_value(raw, board_id)
                recent_queue = {
                    row["id"]
                    for row in rows
                    if (created := _parse_time(row.get("created_at"))) is not None
                    and created > cutoff
                }
                history = [
                    (ask_id, created)
                    for ask_id, created in self._intake_submissions.get(board_id, [])
                    if created > cutoff
                ]
                self._intake_submissions[board_id] = history
                if (
                    len(recent_queue | {ask_id for ask_id, _created in history})
                    >= INTAKE_RATE_LIMIT
                ):
                    raise IntakeRateLimitError("intake rate limit exceeded")

                created_at = now.isoformat()
                ask_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"pursers-dashboard-intake\0{board_id}\0{clean_text}\0{created_at}",
                    )
                )
                ask = {
                    "id": ask_id,
                    "text": clean_text,
                    "requested_by": self.config.agent_name,
                    "board_id": board_id,
                    "created_at": created_at,
                }
                encoded = _encode_intake_document([*rows, ask], tombstones)
                if len(rows) >= MAX_INTAKE_ROWS:
                    raise IntakeRateLimitError("intake queue is full")
                expected = None
                if current_text is not None:
                    expected = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
                arguments = _dashboard_state_update_arguments(
                    agent_name=self.config.agent_name,
                    key=INTAKE_STATE_KEY,
                    value=encoded,
                    expected_sha256=expected,
                )
                try:
                    await client._call("board_state_update", arguments)
                except BoardClientError as exc:
                    raise ConfigConflictError(
                        "coordinator_intake changed; retry the ask"
                    ) from exc
                history.append((ask_id, now))
                self._intake_submissions[board_id] = history
        return {
            "ok": True,
            "ask": ask,
            "expected_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "concurrency": "cas" if current_text is not None else "lww",
        }

    async def decide_intake(
        self,
        board_id: Any,
        ask_id: Any,
        action: Any,
        expected_sha256: Any,
        title: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(board_id, str) or not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        if (
            not isinstance(ask_id, str)
            or not ask_id.strip()
            or len(ask_id.strip()) > 120
        ):
            raise ValueError("invalid ask_id")
        if action not in {"approve", "decline"}:
            raise ValueError("action must be approve or decline")
        if not isinstance(expected_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_sha256
        ):
            raise ValueError("expected_sha256 must be a SHA-256 digest")
        if title is not None and (
            not isinstance(title, str)
            or not title.strip()
            or len(title.strip()) > INTAKE_TITLE_MAX_CHARS
        ):
            raise ValueError("title must be 1 to 200 characters")
        if action == "decline" and title is not None:
            raise ValueError("decline does not accept a title")
        active = {active_board for _label, active_board in await self._boards()}
        if board_id not in active:
            raise ValueError("board_id is not registry-active")
        now = self.now_factory()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        decided_at = now.astimezone(timezone.utc).isoformat()

        with self._intake_write_lock:
            async with self._client(board_id) as client:
                raw = await client.board_state_get(key=INTAKE_STATE_KEY)
                rows, tombstones, current_text = _intake_state_value(raw, board_id)
                if current_text is None or not hmac.compare_digest(
                    hashlib.sha256(current_text.encode("utf-8")).hexdigest(),
                    expected_sha256,
                ):
                    raise ConfigConflictError(
                        "coordinator_intake changed; refresh pending asks"
                    )
                index = next(
                    (offset for offset, row in enumerate(rows) if row["id"] == ask_id),
                    None,
                )
                if index is None:
                    raise ConfigConflictError("pending ask changed; refresh pending asks")
                ask = rows[index]
                if action == "approve":
                    if ask.get("approved") is True:
                        raise ConfigConflictError("pending ask is already approved")
                    try:
                        raw_findings = await client.board_state_get(
                            key=FINDINGS_STATE_KEY
                        )
                    except BoardClientError as exc:
                        if "state key not found" not in str(exc):
                            raise
                        raw_findings = {}
                    draft = _validated_intake_draft(raw_findings, ask_id)
                    if draft is None:
                        raise ConfigConflictError(
                            "coordinator draft is not ready; refresh pending asks"
                        )
                    decided = {
                        **ask,
                        "approved": True,
                        "approved_by": self.config.agent_name,
                        "approved_at": decided_at,
                        "approved_title": (
                            title.strip() if title is not None else draft["title"]
                        ),
                    }
                    rows[index] = decided
                    result = {"ask": decided}
                else:
                    rows.pop(index)
                    tombstone = {
                        "id": ask["id"],
                        "text": ask["text"],
                        "board_id": board_id,
                        "declined_by": self.config.agent_name,
                        "declined_at": decided_at,
                    }
                    tombstones.append(tombstone)
                    tombstones = tombstones[-MAX_INTAKE_TOMBSTONES:]
                    result = {"tombstone": tombstone}
                encoded = _encode_intake_document(rows, tombstones)
                arguments = _dashboard_state_update_arguments(
                    agent_name=self.config.agent_name,
                    key=INTAKE_STATE_KEY,
                    value=encoded,
                    expected_sha256=expected_sha256,
                )
                try:
                    await client._call("board_state_update", arguments)
                except BoardClientError as exc:
                    raise ConfigConflictError(
                        "coordinator_intake changed; refresh pending asks"
                    ) from exc
        return {
            "ok": True,
            "action": action,
            **result,
            "expected_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "concurrency": "cas",
        }

    async def save_config(
        self, value: Any, expected_sha256: str | None
    ) -> dict[str, Any]:
        clean = validate_coordinator_config(value)
        clean["updated_at"] = datetime.now(timezone.utc).isoformat()
        clean["updated_by"] = self.config.agent_name
        encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        async with self._client(self.config.home_board) as client:
            try:
                current = await client.board_state_get(key=CONFIG_STATE_KEY)
            except BoardClientError as exc:
                if "state key not found" not in str(exc):
                    raise
                current_text = None
            else:
                _current_document, current_text = _state_value(current)
                if current_text is None:
                    raise ConfigConflictError("coordinator_config state is malformed")
            current_digest = (
                hashlib.sha256(current_text.encode("utf-8")).hexdigest()
                if current_text is not None
                else None
            )
            if current_digest is None:
                if expected_sha256 is not None:
                    raise ConfigConflictError("coordinator_config does not exist")
            elif expected_sha256 is None:
                raise ConfigConflictError(
                    "expected_sha256 is required for an existing config"
                )
            elif expected_sha256 != current_digest:
                raise ConfigConflictError(
                    "coordinator_config changed; reload before saving"
                )
            expected = None
            if expected_sha256 is not None:
                if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                    raise ValueError(
                        "expected_sha256 must be a lowercase SHA-256 digest"
                    )
                expected = expected_sha256
            arguments = _dashboard_state_update_arguments(
                agent_name=self.config.agent_name,
                key=CONFIG_STATE_KEY,
                value=encoded,
                expected_sha256=expected,
            )
            await client._call("board_state_update", arguments)
        return {
            "ok": True,
            "config": clean,
            "expected_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "concurrency": "cas" if expected_sha256 is not None else "lww",
        }

    async def mark_butler_draft(
        self, board_id: Any, payload: Any
    ) -> dict[str, Any]:
        if not isinstance(board_id, str) or not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        if not isinstance(payload, dict) or set(payload) != {
            "ticket_id",
            "question_id",
            "mark",
        }:
            raise ValueError("request must contain ticket_id, question_id, and mark")
        ticket_id = payload["ticket_id"]
        question_id = payload["question_id"]
        mark = payload["mark"]
        if not isinstance(ticket_id, str) or not re.fullmatch(r"TK-[0-9A-Za-z-]+", ticket_id):
            raise ValueError("invalid ticket_id")
        if not isinstance(question_id, str) or not re.fullmatch(r"CQ-[0-9A-Za-z-]+", question_id):
            raise ValueError("invalid question_id")
        if mark not in BUTLER_MARK_VALUES:
            raise ValueError("invalid mark")
        active = {active_board for _label, active_board in await self._boards()}
        if board_id not in active:
            raise ValueError("board_id is not registry-active")
        marked_at = self.now_factory()
        if marked_at.tzinfo is None:
            marked_at = marked_at.replace(tzinfo=timezone.utc)
        marked_at_text = marked_at.astimezone(timezone.utc).isoformat()
        evaluation_key = butler_evaluation_state_key(question_id)

        with self._butler_write_lock:
            async with self._client(board_id) as client:
                raw = await client.board_state_get(key=evaluation_key)
                document, current_text = _state_value(raw)
                if document is None or current_text is None:
                    raise ConfigConflictError("board butler evaluation state is malformed")
                inbox = await client.board_question_inbox(
                    state="answered", ticket_id=ticket_id, limit=100
                )
                questions = inbox.get("questions", [])
                question = next(
                    (
                        row
                        for row in questions
                        if isinstance(row, dict)
                        and row.get("question_id") == question_id
                    ),
                    None,
                )
                if question is None:
                    raise ValueError("answered question not found")
                marker = _identity_fields(await client.joined_identity())
                updated = mark_butler_evaluation(
                    document,
                    ticket_id=ticket_id,
                    question_id=question_id,
                    mark=mark,
                    question=question,
                    marker=marker,
                    marked_at=marked_at_text,
                )
                encoded = json.dumps(updated, sort_keys=True, separators=(",", ":"))
                expected = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
                arguments = _dashboard_state_update_arguments(
                    agent_name=self.config.agent_name,
                    key=evaluation_key,
                    value=encoded,
                    expected_sha256=expected,
                )
                try:
                    await client._call("board_state_update", arguments)
                except BoardClientError as exc:
                    raise ConfigConflictError(
                        "board butler evaluation changed; refresh before marking"
                    ) from exc
        return {
            "ok": True,
            "board_id": board_id,
            "ticket_id": ticket_id,
            "question_id": question_id,
            "mark": mark,
        }

    def _require_doors_config(self) -> tuple[Path, Path]:
        keys_dir = self.config.doors_keys_dir
        jwks_path = self.config.jwks_path
        if not keys_dir or not jwks_path:
            raise ValueError(
                "Doors configuration missing: PURSERS_DOORS_KEYS_DIR and PURSERS_JWKS_PATH must be set"
            )
        return Path(keys_dir).expanduser().resolve(), Path(jwks_path).expanduser().resolve()

    async def fetch_doors(self) -> dict[str, Any]:
        keys_dir, jwks_path = self._require_doors_config()
        reg_payload = await self.fetch_project_registry()
        registry = reg_payload.get("registry", {})
        projects = registry.get("projects", {}) if isinstance(registry, dict) else {}

        doors_meta = door_admin.list_doors(jwks_path)
        doors_by_key = {(d["board"], d["role"]): d for d in doors_meta}

        fleet = await self.fetch()
        agent_activity: dict[str, str | None] = {}
        for agent in fleet.get("agents", []):
            if isinstance(agent, dict) and "agent_name" in agent:
                agent_activity[agent["agent_name"]] = agent.get("last_seen")

        rows: list[dict[str, Any]] = []
        for project_name, entry in projects.items():
            if not isinstance(entry, dict) or entry.get("status") != "active":
                continue
            board_id = entry.get("board_id")
            if not board_id:
                continue

            await self._require_board_admin(str(board_id))

            board_members_list: list[dict[str, Any]] = []
            try:
                async with self._client(board_id) as client:
                    res = await _client_call(client, "board_members", {})
                    board_members_list = res.get("members", [])
            except PermissionError:
                raise
            except Exception:
                board_members_list = []

            members_by_pid = {
                m.get("principal_id"): m
                for m in board_members_list
                if isinstance(m, dict) and "principal_id" in m
            }

            for role in ("worker", "reviewer"):
                key_info = doors_by_key.get((board_id, role))
                door_pid = door_principal_id(board_id, role, self.config.url)
                mem = members_by_pid.get(door_pid, {})
                agent_names = mem.get("agent_names", [])
                seats = [
                    {
                        "agent_name": name,
                        "last_activity": agent_activity.get(name),
                    }
                    for name in agent_names
                ]
                rows.append({
                    "project": project_name,
                    "board_id": board_id,
                    "role": role,
                    "kid": key_info.get("kid") if key_info else None,
                    "exp": key_info.get("exp") if key_info else None,
                    "seats": seats,
                })

        return {"ok": True, "doors": rows}

    async def _require_board_admin(self, board_id: str) -> None:
        async with self._client(board_id) as client:
            response = await _client_call(client, "board_list", {})
        boards = response.get("boards", []) if isinstance(response, dict) else []
        membership = next(
            (
                row.get("membership_role")
                for row in boards
                if isinstance(row, dict) and row.get("board_id") == board_id
            ),
            None,
        )
        if membership != "admin":
            raise PermissionError(
                f"board access denied: admin membership required for {board_id!r}"
            )

    async def _authorize_door_action(self, board_id: str) -> None:
        reg_payload = await self.fetch_project_registry()
        registry = reg_payload.get("registry", {}) if isinstance(reg_payload, dict) else {}
        active_boards = {self.config.home_board}
        active_boards.update(
            project.get("board_id")
            for project in registry.get("projects", {}).values()
            if isinstance(project, dict) and project.get("status") == "active"
        )
        if board_id not in active_boards:
            raise ValueError(f"board {board_id!r} is not an active registry project")
        await self._require_board_admin(board_id)

    async def copy_door(self, board_id: str, role: str) -> dict[str, Any]:
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        if role not in door_admin.VALID_ROLES:
            raise ValueError("role must be worker or reviewer")
        keys_dir, jwks_path = self._require_doors_config()
        await self._authorize_door_action(board_id)
        issued = door_admin.issue_credential(
            board=board_id,
            role=role,
            central_url=self.config.url,
            jwks_path=jwks_path,
            keys_dir=keys_dir,
            rotate=False,
        )
        return {
            "ok": True,
            "door_string": issued.door_string,
            "kid": issued.kid,
            "exp": issued.claims.get("exp"),
        }

    async def rotate_door(self, board_id: str, role: str) -> dict[str, Any]:
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        if role not in door_admin.VALID_ROLES:
            raise ValueError("role must be worker or reviewer")
        keys_dir, jwks_path = self._require_doors_config()
        await self._authorize_door_action(board_id)
        issued = door_admin.issue_credential(
            board=board_id,
            role=role,
            central_url=self.config.url,
            jwks_path=jwks_path,
            keys_dir=keys_dir,
            rotate=True,
        )
        return {
            "ok": True,
            "door_string": issued.door_string,
            "kid": issued.kid,
            "exp": issued.claims.get("exp"),
            "warning": "Seats on the previous key must re-join with the new door string.",
        }

    async def add_project(
        self,
        *,
        project_name: str,
        board_id: str,
        work_dir: str,
        integration_ref: str = "main",
        seats_manager: Any = None,
    ) -> dict[str, Any]:
        progress: dict[str, Any] = {}
        try:
            return await self._add_project_steps(
                project_name=project_name,
                board_id=board_id,
                work_dir=work_dir,
                integration_ref=integration_ref,
                seats_manager=seats_manager,
                progress=progress,
            )
        except asyncio.CancelledError:
            raise
        except AddProjectPartialFailure:
            raise
        except Exception as exc:
            if not progress.get("started"):
                raise
            status_code = 403 if isinstance(exc, PermissionError) else 409
            if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
                status_code = 400
            raise AddProjectPartialFailure(
                progress.get("steps", []),
                str(progress.get("failed_step", "unknown")),
                status_code,
            ) from exc

    async def _add_project_steps(
        self,
        *,
        project_name: str,
        board_id: str,
        work_dir: str,
        integration_ref: str = "main",
        seats_manager: Any = None,
        progress: dict[str, Any],
    ) -> dict[str, Any]:
        keys_dir, jwks_path = self._require_doors_config()
        if not isinstance(project_name, str) or not project_name.strip():
            raise ValueError("project name must be a non-empty string")
        project_name = project_name.strip()
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError(f"board_id must match {BOARD_ID_RE.pattern}")
        if not isinstance(work_dir, str) or not os.path.isabs(work_dir):
            raise ValueError("work_dir must be an absolute path")
        if (
            not isinstance(integration_ref, str)
            or not integration_ref.strip()
            or integration_ref.startswith("-")
        ):
            raise ValueError("integration_ref must be a valid git reference")
        integration_ref = integration_ref.strip()

        # Authorize the control-plane action before reading or mutating the
        # registry, boards, credentials, or clone state.
        await self._require_board_admin(self.config.home_board)

        steps: list[dict[str, Any]] = []
        progress.update(started=True, steps=steps, failed_step="registry_admin")

        # Step a: registry_admin add (schema v1)
        reg_payload = await self.fetch_project_registry()
        registry = copy.deepcopy(reg_payload["registry"])
        expected_sha256 = reg_payload["expected_sha256"]
        projects = registry.setdefault("projects", {})

        existing_entry = projects.get(project_name)
        if (
            isinstance(existing_entry, dict)
            and existing_entry.get("board_id") == board_id
            and existing_entry.get("work_dir") == work_dir
            and existing_entry.get("status") == "active"
        ):
            steps.append({
                "step": "registry_admin",
                "status": "already present",
                "message": f"Project {project_name!r} already registered.",
            })
        else:
            new_entry = {
                "board_id": board_id,
                "work_dir": work_dir,
                "status": "active",
            }
            if isinstance(existing_entry, dict) and "fleet_clone_dir" in existing_entry:
                new_entry["fleet_clone_dir"] = existing_entry["fleet_clone_dir"]
            if isinstance(existing_entry, dict) and "repository_url" in existing_entry:
                new_entry["repository_url"] = existing_entry["repository_url"]
            if integration_ref != "main":
                new_entry["integration_ref"] = integration_ref
            projects[project_name] = new_entry
            saved = await self.save_project_registry(registry, expected_sha256)
            expected_sha256 = saved["expected_sha256"]
            steps.append({
                "step": "registry_admin",
                "status": "created",
                "message": f"Project {project_name!r} added to project registry.",
            })

        # Step b: board create + first board_onboard as admin (or detect existing)
        progress["failed_step"] = "board_create"
        board_already_present = False
        try:
            async with self._client(self.config.home_board) as home_client:
                bl = await _client_call(home_client, "board_list", {})
                existing_boards = {
                    b.get("board_id")
                    for b in bl.get("boards", [])
                    if isinstance(b, dict)
                }
                board_already_present = board_id in existing_boards
        except Exception:
            board_already_present = False

        async with self._client(board_id) as client:
            if board_already_present:
                steps.append({
                    "step": "board_create",
                    "status": "already present",
                    "message": f"Board {board_id!r} already present.",
                })
            else:
                await _client_call(client, "board_onboard", {"role": "coordinator"})
                steps.append({
                    "step": "board_create",
                    "status": "created",
                    "message": f"Board {board_id!r} created with admin onboard.",
                })

            # Step c: board_member_add for worker and reviewer door principals
            progress["failed_step"] = "door_principals"
            worker_pid = door_principal_id(board_id, "worker", self.config.url)
            reviewer_pid = door_principal_id(board_id, "reviewer", self.config.url)
            existing_members = {}
            try:
                bm = await _client_call(client, "board_members", {})
                for m in bm.get("members", []):
                    if isinstance(m, dict) and "principal_id" in m:
                        existing_members[m["principal_id"]] = m.get("role")
            except Exception:
                existing_members = {}

            worker_present = existing_members.get(worker_pid) in {"member", "admin"}
            reviewer_present = existing_members.get(reviewer_pid) in {"reviewer", "admin"}

            if not worker_present:
                await _client_call(
                    client,
                    "board_member_add",
                    {
                        "agent_name": self.config.agent_name,
                        "principal_id": worker_pid,
                        "role": "member",
                    },
                )
            if not reviewer_present:
                await _client_call(
                    client,
                    "board_member_add",
                    {
                        "agent_name": self.config.agent_name,
                        "principal_id": reviewer_pid,
                        "role": "reviewer",
                    },
                )

            if worker_present and reviewer_present:
                steps.append({
                    "step": "door_principals",
                    "status": "already present",
                    "message": "Door principals (worker and reviewer) already present.",
                })
            else:
                steps.append({
                    "step": "door_principals",
                    "status": "created",
                    "message": "Door principals provisioned on board.",
                })

            # Step d: dispatch policy defaults and review policy default
            progress["failed_step"] = "policies"
            status = await _client_call(client, "board_status", {})
            board_obj = (
                status.get("board", {})
                if isinstance(status.get("board"), dict)
                else status
            )
            dp = board_obj.get("dispatch_policy") or {}
            rp = board_obj.get("review_policy")

            dp_match = (
                dp.get("offer_ttl_s") == 600
                and dp.get("broadcast_reoffer_s") == 180
                and dp.get("second_opinion") is True
                and dp.get("fallback_broadcast") is True
            )
            rp_match = (rp == "strict")

            if dp_match and rp_match:
                steps.append({
                    "step": "policies",
                    "status": "already present",
                    "message": "Dispatch and review policy defaults already present.",
                })
            else:
                await _client_call(
                    client,
                    "board_dispatch_policy_set",
                    {
                        "offer_ttl_s": 600,
                        "broadcast_reoffer_s": 180,
                        "second_opinion": True,
                        "fallback_broadcast": True,
                    },
                )
                await _client_call(
                    client,
                    "board_review_policy_set",
                    {"review_policy": "strict"},
                )
                steps.append({
                    "step": "policies",
                    "status": "configured",
                    "message": "Dispatch and review policy defaults configured.",
                })

        # Step e: fleet clone prepare (existing prepare_fleet_clone path)
        progress["failed_step"] = "fleet_clone"
        if seats_manager is not None and hasattr(seats_manager, "prepare_fleet_clone"):
            reg_payload = await self.fetch_project_registry()
            reg_entry = reg_payload["registry"]["projects"].get(project_name, {})
            clone_dir_str = reg_entry.get("fleet_clone_dir")
            already_cloned = False
            if clone_dir_str:
                clone_path = Path(clone_dir_str).expanduser().resolve()
                if clone_path.exists():
                    try:
                        cstate = seats_manager._clone_state(clone_path, integration_ref)
                        if cstate.get("status") == "ready" and not cstate.get("dirty"):
                            already_cloned = True
                    except Exception:
                        already_cloned = False

            if already_cloned:
                steps.append({
                    "step": "fleet_clone",
                    "status": "already present",
                    "message": f"Fleet clone already present at {clone_dir_str}.",
                })
            else:
                prepared = seats_manager.prepare_fleet_clone(reg_payload, project_name)
                await self.save_project_registry(
                    prepared["registry"], prepared["expected_sha256"]
                )
                steps.append({
                    "step": "fleet_clone",
                    "status": "prepared",
                    "message": f"Fleet clone prepared at {prepared['clone']['path']}.",
                })
        else:
            steps.append({
                "step": "fleet_clone",
                "status": "already present",
                "message": "Fleet clone step skipped (no seats manager).",
            })

        # Issue only missing door strings. Re-running Add project must not
        # silently mint fresh JWTs or rewrite otherwise unchanged key state.
        progress["failed_step"] = "door_credentials"
        existing_doors = {
            (str(item.get("board")), str(item.get("role")))
            for item in door_admin.list_doors(jwks_path)
        }
        issued_doors: dict[str, str] = {}
        for role in ("worker", "reviewer"):
            if (board_id, role) in existing_doors:
                continue
            credential = door_admin.issue_credential(
                board=board_id,
                role=role,
                central_url=self.config.url,
                jwks_path=jwks_path,
                keys_dir=keys_dir,
                rotate=False,
            )
            issued_doors[role] = credential.door_string
        steps.append({
            "step": "door_credentials",
            "status": "created" if issued_doors else "already present",
            "message": (
                "Missing door credentials issued."
                if issued_doors
                else "Door credentials already present; no credentials changed."
            ),
        })

        return {
            "ok": True,
            "steps": steps,
            "doors": issued_doors or None,
        }


class _ReusableAsyncRunner:
    """Run dashboard coroutines on one event loop across HTTP requests."""

    def __init__(self) -> None:
        self._runner = asyncio.Runner()
        self._lock = threading.Lock()
        self._closed = False

    def run(self, awaitable: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("dashboard async runner is closed")
            return self._runner.run(awaitable)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._runner.close()
                self._closed = True


class TimedCache:
    """Serve the last value while one background load refreshes it.

    Only the first load blocks. After expiry the previous value is returned at
    once and a single background thread reloads it, so a slow Central read never
    makes the browser's 4-second request time out and flash the connection
    banner. A value older than ``max_stale_seconds`` whose refresh failed is not
    served; the refresh error is raised instead so a real outage stays visible.
    """

    def __init__(
        self,
        ttl_seconds: float,
        loader: Callable[[], Awaitable[dict[str, Any]]],
        runner: Callable[[Awaitable[dict[str, Any]]], dict[str, Any]] = asyncio.run,
        *,
        max_stale_seconds: float | None = None,
        background: bool = True,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.loader = loader
        self.runner = runner
        self.max_stale_seconds = (
            max(60.0, 12 * ttl_seconds) if max_stale_seconds is None else max_stale_seconds
        )
        self.background = background
        self._lock = threading.Lock()
        self._expires_at = 0.0
        self._loaded_at = 0.0
        self._value: dict[str, Any] | None = None
        self._refreshing = False
        self._refresh_error: BaseException | None = None

    def _store(self, value: dict[str, Any]) -> None:
        now = time.monotonic()
        self._value = value
        self._loaded_at = now
        self._expires_at = now + self.ttl_seconds
        self._refresh_error = None

    def _refresh(self) -> None:
        try:
            value = self.runner(self.loader())
        except BaseException as exc:  # noqa: BLE001 - surfaced by get() once stale.
            with self._lock:
                self._refresh_error = exc
                self._refreshing = False
            return
        with self._lock:
            self._store(value)
            self._refreshing = False

    def get(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._value is None or (not self.background and now >= self._expires_at):
                self._store(self.runner(self.loader()))
                return self._value
            if now >= self._expires_at and not self._refreshing:
                self._refreshing = True
                threading.Thread(
                    target=self._refresh, name="fleet-cache-refresh", daemon=True
                ).start()
            if (
                self._refresh_error is not None
                and now - self._loaded_at > self.max_stale_seconds
            ):
                raise self._refresh_error
            return self._value


class SeatConfigManager:
    """Secret-safe orchestration layer for the dashboard Config page."""

    def __init__(
        self,
        inventory_path: str | Path | None = None,
        *,
        state_dir: str | Path | None = None,
        bridge_installer: BridgeInstaller | None = None,
        doctor_factory: Callable[[], Doctor] = Doctor,
        latest_version: Callable[[], str | None] | None = None,
        release_ops_manager: ReleaseOpsManager | None = None,
        git_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        discovered_configs: list[tuple[str, str | Path]] | None = None,
    ) -> None:
        self.state_dir = (
            Path(state_dir).expanduser()
            if state_dir is not None
            else _default_config_state_dir()
        )
        self.inventory = SeatInventory(inventory_path or self.state_dir / "seats.json")
        self.bridge_installer = bridge_installer or BridgeInstaller()
        self.doctor_factory = doctor_factory
        self.latest_version = latest_version or self._pypi_latest
        self.git_runner = git_runner
        self.discovered_configs = tuple(
            (host, Path(path).expanduser())
            for host, path in (
                discovered_configs
                if discovered_configs is not None
                else (
                    ("codex", "~/.codex/config.toml"),
                    ("goose", "~/.config/goose/config.yaml"),
                    (
                        "claude-desktop",
                        "~/Library/Application Support/Claude/claude_desktop_config.json",
                    ),
                )
            )
        )
        self.release_ops = release_ops_manager or ReleaseOpsManager(
            state_dir=self.state_dir,
            bridge_installer=self.bridge_installer,
            inventory=self.inventory,
        )
        self._plans: dict[str, tuple[DesiredSeat, list[Any]]] = {}
        self._ops_plans: dict[str, dict[str, Any]] = {}
        self._active_ops: set[str] = set()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._attention_lock = threading.RLock()

    def release_status(self) -> dict[str, Any]:
        return self.release_ops.release_card_status()

    def _start_ops_job(
        self,
        action: str,
        command: str,
        target: Callable[[Callable[[str], None]], Any],
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        with self._lock:
            if action in {"stage_central", "publish_from_tag"}:
                if action in self._active_ops:
                    self.release_ops.record_attempt(
                        action,
                        phase="queue",
                        ok=False,
                        error="duplicate or concurrent job refused",
                    )
                    raise RuntimeError(f"{action} already queued or running")
                self._active_ops.add(action)
            if len(self._jobs) >= CONFIG_JOB_LIMIT:
                self._jobs.pop(next(iter(self._jobs)))
            self._jobs[job_id] = {
                "job_id": job_id,
                "action": action,
                "command": command,
                "status": "queued",
                "logs": [f"Queued {action}: {command}"],
            }
            self.release_ops.record_attempt(
                action, phase="queue", ok=True, job_id=job_id, command=command
            )

        def work() -> None:
            with self._lock:
                self._jobs[job_id]["status"] = "running"

            def emit(msg: str) -> None:
                with self._lock:
                    logs = self._jobs[job_id].setdefault("logs", [])
                    logs.append(msg)

            try:
                result = target(emit)
            except Exception as exc:  # noqa: BLE001
                err_msg = _clean_text(str(exc))
                self.release_ops.record_attempt(
                    action,
                    phase="job",
                    ok=False,
                    job_id=job_id,
                    error=err_msg,
                )
                with self._lock:
                    self._jobs[job_id].update(status="failed", error=err_msg)
                    self._jobs[job_id].setdefault("logs", []).append(f"FAILED: {err_msg}")
            else:
                self.release_ops.record_attempt(
                    action, phase="job", ok=True, job_id=job_id
                )
                with self._lock:
                    self._jobs[job_id].update(status="succeeded", result=result)
                    self._jobs[job_id].setdefault("logs", []).append("SUCCEEDED")
            finally:
                with self._lock:
                    self._active_ops.discard(action)

        threading.Thread(target=work, daemon=True, name=f"ops-{action}").start()
        return {"job_id": job_id, "action": action, "command": command, "status": "queued"}

    @staticmethod
    def _canonical_ops_action(action: str) -> str:
        aliases = {
            "publish": "publish_from_tag",
            "publish_from_tag": "publish_from_tag",
            "stage": "stage_central",
            "stage_central": "stage_central",
            "kickstart": "kickstart_central",
            "kickstart_central": "kickstart_central",
            "restart-dash": "restart_dashboard",
            "restart_dashboard": "restart_dashboard",
        }
        try:
            return aliases[action]
        except KeyError as exc:
            raise ValueError(f"unknown ops action: {action}") from exc

    def prepare_ops_action(self, action: str, **kwargs: Any) -> dict[str, Any]:
        canonical = self._canonical_ops_action(action)
        allowed = {"tag"} if canonical == "publish_from_tag" else set()
        unknown = set(kwargs) - allowed
        if unknown:
            raise ValueError(f"unknown parameters for {canonical}: {unknown}")
        plan = self.release_ops.resolve_action_plan(
            canonical,
            tag=kwargs.get("tag") if canonical == "publish_from_tag" else None,
        )
        plan_id = uuid.uuid4().hex
        digest = self.release_ops.plan_digest(plan)
        expires_at = time.monotonic() + CONFIG_OPS_PLAN_TTL_SECONDS
        with self._lock:
            self._ops_plans = {
                key: value
                for key, value in self._ops_plans.items()
                if value["expires_at"] > time.monotonic()
            }
            if len(self._ops_plans) >= CONFIG_JOB_LIMIT:
                self._ops_plans.pop(next(iter(self._ops_plans)))
            self._ops_plans[plan_id] = {
                "plan": plan,
                "digest": digest,
                "expires_at": expires_at,
            }
        return {
            "plan_id": plan_id,
            "digest": digest,
            "action": canonical,
            "command": str(plan["command"]),
            "expires_in_s": CONFIG_OPS_PLAN_TTL_SECONDS,
        }

    def ops_action(self, plan_id: str, digest: str) -> dict[str, Any]:
        if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", plan_id):
            raise ValueError("valid plan_id is required")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("valid plan digest is required")
        with self._lock:
            stored = self._ops_plans.pop(plan_id, None)
        if stored is None:
            raise KeyError(plan_id)
        plan = stored["plan"]
        action = str(plan["action"])
        if stored["expires_at"] <= time.monotonic():
            self.release_ops.record_attempt(
                action, phase="confirm", ok=False, error="plan expired"
            )
            raise RuntimeError("ops confirmation plan expired")
        actual_digest = self.release_ops.plan_digest(plan)
        if not hmac.compare_digest(digest, stored["digest"]) or not hmac.compare_digest(
            actual_digest, stored["digest"]
        ):
            self.release_ops.record_attempt(
                action, phase="confirm", ok=False, error="plan digest mismatch"
            )
            raise ValueError("ops confirmation plan digest mismatch")
        return self._start_ops_job(
            action,
            str(plan["command"]),
            lambda emit: self.release_ops.execute_action_plan(
                plan, log_callback=emit
            ),
        )

    @staticmethod
    def _pypi_latest() -> str | None:
        request = urllib.request.Request(
            "https://pypi.org/pypi/pursers-wait-bridge/json",
            headers={"Accept": "application/json", "User-Agent": "pursers-dashboard"},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                value = json.load(response).get("info", {}).get("version")
            return value if isinstance(value, str) else None
        except Exception:  # noqa: BLE001 - optional version hint.
            return None

    @staticmethod
    def _desired(value: Any) -> DesiredSeat:
        if not isinstance(value, dict):
            raise ValueError("seat must be an object")  # noqa: TRY004 - API contract.
        return DesiredSeat.from_dict(value)

    @staticmethod
    def _report(rows: list[Any]) -> dict[str, Any]:
        order = {"PASS": 0, "WARN": 1, "FAIL": 2}
        checks = [
            {
                "seat": row.seat,
                "check": row.check,
                "status": row.status,
                "message": row.message,
            }
            for row in rows
        ]
        overall = max((row["status"] for row in checks), key=order.get, default="PASS")
        return {"schema_version": 1, "overall": overall, "checks": checks}

    @staticmethod
    def _redact_sensitive_assignments(value: str) -> str:
        redacted: list[str] = []
        line_endings = (
            "\r\n", "\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e",
            "\x85", "\u2028", "\u2029",
        )
        for line in value.splitlines(keepends=True):
            ending = next(
                (candidate for candidate in line_endings if line.endswith(candidate)),
                "",
            )
            content = line[:-len(ending)] if ending else line
            colon = content.find(":")
            equals = content.find("=")
            delimiters = [index for index in (colon, equals) if index >= 0]
            if not delimiters:
                redacted.append(line)
                continue
            delimiter = min(delimiters)
            key = content[:delimiter].casefold()
            if (
                not any(
                    word in key
                    for word in (
                        "token",
                        "authorization",
                        "secret",
                        "password",
                        "apikey",
                        "api_key",
                        "api-key",
                        "bearer",
                    )
                )
                or "file" in key
                or "path" in key
                or "env_var" in key
            ):
                redacted.append(line)
                continue
            separator_end = delimiter + 1
            while (
                separator_end < len(content)
                and content[separator_end].isspace()
            ):
                separator_end += 1
            redacted.append(f"{content[:separator_end]}[REDACTED]{ending}")
        return "".join(redacted)

    @staticmethod
    def _redact_url_passwords(value: str) -> str:
        """Redact URL credentials without rescanning failed candidate prefixes."""
        redacted: list[str] = []
        emit_from = 0
        cursor = 0
        username_probe = 0
        password_probe = 0
        size = len(value)
        while cursor < size:
            if value[cursor] not in URL_SCHEME_CHARS:
                cursor += 1
                continue

            # Consume each maximal scheme-character run once. A run can contain
            # several regex word boundaries (for example ``bad-http``), but the
            # regex can only use the first eligible start: every later start has
            # the same greedy scheme end.
            scheme_start: int | None = None
            while cursor < size and value[cursor] in URL_SCHEME_CHARS:
                if (
                    scheme_start is None
                    and value[cursor] in URL_SCHEME_START_CHARS
                    and (
                        cursor == 0
                        or not (
                            value[cursor - 1].isalnum()
                            or value[cursor - 1] == "_"
                        )
                    )
                ):
                    scheme_start = cursor
                cursor += 1
            scheme_end = cursor
            if scheme_start is None or not value.startswith("://", scheme_end):
                continue

            username_start = scheme_end + 3
            username_probe = max(username_probe, username_start)
            while (
                username_probe < size
                and value[username_probe] not in ":/@"
                and not value[username_probe].isspace()
            ):
                username_probe += 1
            username_end = username_probe
            if (
                username_end == username_start
                or username_end >= size
                or value[username_end] != ":"
            ):
                continue

            password_start = username_end + 1
            password_probe = max(password_probe, password_start)
            while (
                password_probe < size
                and value[password_probe] not in "/@"
                and not value[password_probe].isspace()
            ):
                password_probe += 1
            password_end = password_probe
            if (
                password_end == password_start
                or password_end >= size
                or value[password_end] != "@"
            ):
                continue

            # re.sub ignores matches starting inside the preceding match.
            if scheme_start < emit_from:
                continue

            redacted.extend(
                (
                    value[emit_from:password_start],
                    "[REDACTED:URL_PASSWORD]@",
                )
            )
            emit_from = password_end + 1
        redacted.append(value[emit_from:])
        return "".join(redacted)

    @staticmethod
    def _redact_jwts(value: str) -> str:
        """Redact JWTs with monotonic segment probes for overlapping starts."""

        def boundary(index: int) -> bool:
            before = index > 0 and (
                value[index - 1].isalnum() or value[index - 1] == "_"
            )
            after = index < size and (value[index].isalnum() or value[index] == "_")
            return before != after

        redacted: list[str] = []
        emit_from = 0
        cursor = 0
        first_probe = 0
        second_probe = 0
        third_probe = 0
        third_boundaries: list[int] = []
        first_boundary = 0
        size = len(value)
        while cursor < size:
            if (
                not value.startswith("eyJ", cursor)
                or (
                    cursor > 0
                    and (value[cursor - 1].isalnum() or value[cursor - 1] == "_")
                )
            ):
                cursor += 1
                continue

            first_start = cursor + 3
            first_probe = max(first_probe, first_start)
            while first_probe < size and value[first_probe] in JWT_SEGMENT_CHARS:
                first_probe += 1
            first_end = first_probe
            if (
                first_end - first_start < 8
                or first_end >= size
                or value[first_end] != "."
            ):
                cursor += 1
                continue

            second_start = first_end + 1
            second_probe = max(second_probe, second_start)
            while second_probe < size and value[second_probe] in JWT_SEGMENT_CHARS:
                second_probe += 1
            second_end = second_probe
            if (
                second_end - second_start < 8
                or second_end >= size
                or value[second_end] != "."
            ):
                cursor += 1
                continue

            third_start = second_end + 1
            third_probe = max(third_probe, third_start)
            while third_probe < size and value[third_probe] in JWT_SEGMENT_CHARS:
                third_probe += 1
                if boundary(third_probe):
                    third_boundaries.append(third_probe)
            if boundary(third_probe) and (
                not third_boundaries or third_boundaries[-1] != third_probe
            ):
                third_boundaries.append(third_probe)

            minimum_end = third_start + 8
            while (
                first_boundary < len(third_boundaries)
                and third_boundaries[first_boundary] < minimum_end
            ):
                first_boundary += 1
            if (
                first_boundary >= len(third_boundaries)
                or third_boundaries[-1] < minimum_end
            ):
                cursor += 1
                continue
            match_end = third_boundaries[-1]

            if cursor >= emit_from:
                redacted.extend((value[emit_from:cursor], "[REDACTED JWT]"))
                emit_from = match_end
            cursor += 1
        redacted.append(value[emit_from:])
        return "".join(redacted)

    @staticmethod
    def _clean_text(value: str) -> str:
        value = SeatConfigManager._redact_url_passwords(value)
        value = SeatConfigManager._redact_jwts(value)
        value = re.sub(
            r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{8,}",
            "Bearer [REDACTED]",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(
            r"(?i)\b(token|authorization|secret|password|api[_-]?key)"
            r"([ \t]*[:=][ \t]*)[^\s,;]+",
            r"\1\2[REDACTED]",
            value,
        )
        value = re.sub(r"/Users/[^/\s]+", "/Users/[REDACTED:POSIX_HOME]", value)
        value = re.sub(r"/home/[^/\s]+", "/home/[REDACTED:LINUX_HOME]", value)
        value = re.sub(
            r"[A-Za-z]:\\Users\\[^\\\s]+",
            r"C:\\Users\\[REDACTED:WINDOWS_HOME]",
            value,
        )
        return SeatConfigManager._redact_sensitive_assignments(value)

    @classmethod
    def _diff(cls, change: Any) -> str:
        before = "" if change.before is None else cls._clean_text(change.before)
        after = "" if change.after is None else cls._clean_text(change.after)
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=str(change.path),
                tofile=str(change.path),
            )
        )[:100_000]

    def _journal(self, action: str, **fields: Any) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "config-actions.jsonl"
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "action": action,
            **fields,
        }
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    def _attention_state_unlocked(self) -> dict[str, Any]:
        path = self.state_dir / "attention-state.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            value = {}
        return {"items": value if isinstance(value, dict) else {}}

    def attention_state(self) -> dict[str, Any]:
        with self._attention_lock:
            return self._attention_state_unlocked()

    def _save_attention_state_unlocked(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or len(value) > 500:
            raise ValueError("attention state must be an object with at most 500 items")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 100_000:
            raise ValueError("attention state is too large")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "attention-state.json"
        descriptor, temporary = tempfile.mkstemp(
            dir=self.state_dir, prefix=".attention-state.", text=True
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"items": value}

    def save_attention_state(self, value: Any) -> dict[str, Any]:
        with self._attention_lock:
            return self._save_attention_state_unlocked(value)

    def observe_attention_action(
        self, value: Any,
    ) -> tuple[
        dict[str, Any], dict[str, Any] | None, dict[str, Any], Exception | None,
    ]:
        """Run one attention save and snapshot its causal state under one lock."""
        with self._attention_lock:
            before = self._attention_state_unlocked()
            try:
                result = self._save_attention_state_unlocked(value)
            except Exception as exc:  # noqa: BLE001 - handler preserves API mapping.
                return before, None, self._attention_state_unlocked(), exc
            return before, result, result, None

    def _bridge_inspection(self) -> dict[str, Any]:
        status = dict(self.bridge_installer.inspect())
        status["pinned_version"] = status.get(
            "pinned_version", status.pop("version", self.bridge_installer.version)
        )
        latest = self.latest_version()
        status["latest_pypi_version"] = latest
        status["latest_version"] = latest
        status["upgrade_available"] = bool(
            latest and latest != status.get("installed_version")
        )
        return status

    def seats(self) -> dict[str, Any]:
        document = self.inventory.load()
        rows = []
        for record in document["seats"]:
            desired = self._desired(record)
            doctor = record.get("last_doctor")
            checks = doctor.get("checks", []) if isinstance(doctor, dict) else []
            restart = next(
                (row for row in checks if row.get("check") == "restart"), None
            )
            live = next(
                (row for row in checks if row.get("check") == "live-smoke"), None
            )
            rows.append(
                {
                    **{
                        key: value
                        for key, value in record.items()
                        if key != "last_doctor"
                    },
                    "principal_label": (
                        "worker" if desired.role == "worker" else "review"
                    ),
                    "profile": {
                        "host_timeout_s": desired.profile.host_timeout_s,
                        "block_s": desired.profile.block_s,
                    },
                    "doctor": doctor,
                    "doctor_status": doctor.get("overall")
                    if isinstance(doctor, dict)
                    else None,
                    "push_mode": "mode=push" in str(live.get("message", ""))
                    if live
                    else None,
                    "needs_restart": bool(restart and restart.get("status") == "WARN"),
                    "token_file_exists": Path(desired.token_file)
                    .expanduser()
                    .is_file(),
                    "ca_file_exists": Path(desired.ca_file).expanduser().is_file(),
                    "doctor_summary": self._doctor_summary(checks),
                }
            )
        discovered = []
        for host, path in self.discovered_configs:
            if path.is_file():
                discovered.append({"host": host, "config_path": str(path)})
        return {
            "schema_version": 1,
            "seats": rows,
            "discovered_configs": discovered,
            "import_review": self.import_review(),
        }

    @staticmethod
    def _doctor_summary(checks: list[dict[str, Any]]) -> dict[str, str | None]:
        order = {"PASS": 0, "WARN": 1, "FAIL": 2}

        def status(*names: str) -> str | None:
            values = [
                row.get("status")
                for row in checks
                if row.get("check") in names and row.get("status") in order
            ]
            return max(values, key=order.get) if values else None

        return {
            "config": status("config", "host-timeout"),
            "tokens": status("token-file", "token-env", "ca-file"),
            "identity": status("split-identity", "identity"),
            "runtime": status("host-runtime"),
        }

    def import_review(self) -> dict[str, Any]:
        parsed, conflicts = discover_managed_seats(self.discovered_configs)
        existing = {
            row.get("name"): row
            for row in self.inventory.load()["seats"]
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }
        candidates: list[dict[str, Any]] = []
        already_imported: list[dict[str, Any]] = []
        for desired in parsed:
            mapping = {
                "name": desired.name,
                "host": desired.host,
                "role": desired.role,
                "config_path": desired.config_path,
                "bridge_connector_name": desired.connector_name,
                "board_connector_name": desired.http_connector_name,
                "home_board": desired.home_board,
                "boards": desired.boards,
                "tier_max": desired.tier_max,
                "skills": list(desired.skills),
                "can_review": desired.can_review,
                "can_work": desired.can_work,
                "model": desired.model,
                "provider": desired.provider,
                "token_source": "file" if desired.token_file else "environment",
                "token_env_var": desired.token_env_var,
            }
            current = existing.get(desired.name)
            if current is not None:
                if asdict(self._desired(current)) == asdict(desired):
                    already_imported.append(mapping)
                else:
                    conflicts.append(
                        {
                            "host": desired.host,
                            "config_path": desired.config_path,
                            "connector_name": desired.connector_name,
                            "reason": f"inventory seat {desired.name} has different settings",
                        }
                    )
                continue
            candidates.append(
                {
                    **mapping,
                    "zero_diff": not bool(adapter_for(desired).plan(desired)),
                    "seat": asdict(desired),
                }
            )
        return {
            "candidates": candidates,
            "conflicts": conflicts,
            "already_imported": already_imported,
        }

    def import_discovered(self, names: Any = None) -> dict[str, Any]:
        if names is not None and (
            not isinstance(names, list)
            or not all(isinstance(name, str) for name in names)
        ):
            raise ValueError("names must be a list")
        review = self.import_review()
        selected = set(names) if names is not None else None
        available = {row["name"] for row in review["candidates"]}
        if selected is not None and not selected <= available:
            raise ValueError("names must select importable seats")
        imported = []
        for row in review["candidates"]:
            if selected is not None and row["name"] not in selected:
                continue
            desired = self._desired(row["seat"])
            self.inventory.upsert(
                desired, bridge_version=self.bridge_installer.version, doctor=None
            )
            imported.append(
                {key: value for key, value in row.items() if key != "seat"}
            )
        self._journal(
            "import",
            seats=[row["name"] for row in imported],
            conflicts=len(review["conflicts"]),
        )
        doctor_job = (
            self.doctor([row["name"] for row in imported]) if imported else None
        )
        return {
            "imported": imported,
            "conflicts": review["conflicts"],
            "doctor_job": doctor_job,
        }

    def bridge(self) -> dict[str, Any]:
        return self._bridge_inspection()

    def plan(self, payload: Any) -> dict[str, Any]:
        desired = self._desired(payload)
        changes = list(adapter_for(desired).plan(desired))
        plan_id = uuid.uuid4().hex
        with self._lock:
            if len(self._plans) >= CONFIG_PLAN_LIMIT:
                self._plans.pop(next(iter(self._plans)))
            self._plans[plan_id] = (desired, changes)
        self._journal("plan", seat=desired.name, changes=len(changes))
        return {
            "plan_id": plan_id,
            "seat": desired.name,
            "token_file_exists": Path(desired.token_file).expanduser().is_file(),
            "ca_file_exists": Path(desired.ca_file).expanduser().is_file(),
            "changes": [
                {
                    "path": str(change.path),
                    "description": change.description,
                    "action": change.action,
                    "diff": self._diff(change),
                }
                for change in changes
            ],
        }

    def suggestions(self, payload: Any) -> dict[str, Any]:
        desired = self._desired(payload)
        return {
            "seat": desired.name,
            "skills": connector_skill_suggestions(adapter_for(desired).inspect()),
        }

    def apply(self, plan_id: Any) -> dict[str, Any]:
        if not isinstance(plan_id, str):
            raise ValueError("plan_id is required")  # noqa: TRY004 - API contract.
        with self._lock:
            pending = self._plans.pop(plan_id, None)
        if pending is None:
            raise KeyError(plan_id)
        desired, changes = pending
        result = adapter_for(desired).apply(changes)
        self.inventory.upsert(
            desired, bridge_version=self.bridge_installer.version, doctor=None
        )
        backups = list(result.backups)
        self._journal(
            "apply", seat=desired.name, changed=len(result.changed), backups=backups
        )
        return {
            "seat": desired.name,
            "changed": list(result.changed),
            "backup_paths": backups,
            "backup_path": backups[0] if backups else None,
            "needs_restart": bool(result.changed),
            "restart_prompt": f"Restart {desired.host} to load the updated seat.",
            "prompt": PromptRenderer().render(desired),
        }

    def prompt(self, payload: Any) -> dict[str, Any]:
        desired = self._desired(payload)
        self._journal("prompt", seat=desired.name)
        return {"seat": desired.name, "prompt": PromptRenderer().render(desired)}

    def _fleet_clone_default(self, project_name: str) -> Path:
        slug = re.sub(r"[^a-z0-9]+", "-", project_name.casefold()).strip("-")
        slug = (slug or "project")[:40]
        suffix = hashlib.sha256(project_name.encode("utf-8")).hexdigest()[:8]
        return self.state_dir.expanduser().resolve() / "clones" / f"{slug}-{suffix}"

    def _git(
        self,
        cwd: Path,
        *arguments: str,
        check: bool = False,
        timeout: int = GIT_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self.git_runner(
                ["git", *arguments],
                cwd=cwd,
                check=check,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=self._git_environment(),
            )
        except subprocess.CalledProcessError as exc:
            subcommand = arguments[0] if arguments else "command"
            detail = self._git_error_tail(exc.stderr or exc.stdout)
            raise FleetCloneGitError(
                subcommand, f"git {subcommand} failed: {detail}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            subcommand = arguments[0] if arguments else "command"
            detail = self._git_error_tail(exc.stderr or exc.stdout)
            raise FleetCloneGitError(
                subcommand,
                f"git {subcommand} timed out after {timeout}s: {detail}",
            ) from exc
        except OSError as exc:
            subcommand = arguments[0] if arguments else "command"
            detail = self._git_error_tail(str(exc))
            raise FleetCloneGitError(
                subcommand, f"git {subcommand} could not start: {detail}"
            ) from exc

    @staticmethod
    def _git_environment() -> dict[str, str]:
        env = os.environ.copy()
        paths = [item for item in env.get("PATH", "").split(os.pathsep) if item]
        for candidate in reversed(("/opt/homebrew/bin", "/usr/local/bin")):
            if Path(candidate).is_dir() and candidate not in paths:
                paths.insert(0, candidate)
        env["PATH"] = os.pathsep.join(paths)
        env["HOME"] = env.get("HOME") or str(Path.home())
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    @classmethod
    def _git_error_tail(cls, value: Any) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        text = value if isinstance(value, str) else "git returned no error output"
        text = _board_redact(text)
        text = cls._clean_text(text[-GIT_ERROR_TAIL_CHARS:])
        return " ".join(text.split()) or "git returned no error output"

    def _clone_origin_preflight(self, source: Path, integration_ref: str) -> str:
        try:
            remote = self._git(source, "remote", "get-url", "origin")
        except OSError as exc:
            raise FleetCloneGitError(
                "remote", "git remote failed: operator checkout is unavailable"
            ) from exc
        if remote.returncode or not remote.stdout.strip():
            detail = self._git_error_tail(remote.stderr)
            raise FleetCloneGitError(
                "remote", f"git remote failed: operator checkout has no origin remote; {detail}"
            )
        origin = remote.stdout.strip()
        try:
            preflight = self._git(
                source, "ls-remote", "--heads", origin, integration_ref
            )
        except FleetCloneGitError as exc:
            raise FleetCloneGitError(
                "ls-remote",
                "cannot reach origin from the dashboard service; clone from a shell "
                f"then re-run to adopt; {exc}",
            ) from exc
        except OSError as exc:
            raise FleetCloneGitError(
                "ls-remote",
                "cannot reach origin from the dashboard service; clone from a shell "
                "then re-run to adopt; git executable is unavailable",
            ) from exc
        if preflight.returncode or not preflight.stdout.strip():
            detail = self._git_error_tail(preflight.stderr or preflight.stdout)
            raise FleetCloneGitError(
                "ls-remote",
                "cannot reach origin from the dashboard service; clone from a shell "
                f"then re-run to adopt; git ls-remote failed: {detail}",
            )
        return origin

    def _log_clone_failure(
        self, project_name: str, error: FleetCloneGitError
    ) -> None:
        project = self._clean_text(project_name)
        message = self._clean_text(_board_redact(str(error)))
        record = {
            "event": "registry_clone_failed",
            "project": project,
            "subcommand": error.subcommand,
            "error": message,
        }
        print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)
        self._journal(
            "registry-clone",
            project=project,
            status="failed",
            subcommand=error.subcommand,
            error=message,
        )

    def _clone_state(
        self, path: Path, integration_ref: str = "main"
    ) -> dict[str, Any]:
        if not path.exists():
            return {
                "path": str(path),
                "status": "missing",
                "dirty": False,
                "empty_worktree": False,
                "detached": None,
                "ahead": None,
                "behind": None,
            }
        inside = self._git(path, "rev-parse", "--is-inside-work-tree")
        if inside.returncode or inside.stdout.strip() != "true":
            return {
                "path": str(path),
                "status": "invalid",
                "dirty": None,
                "empty_worktree": None,
            }
        porcelain = self._git(path, "status", "--porcelain")
        tracked = self._git(path, "ls-tree", "-r", "--name-only", "-z", "HEAD")
        tracked_paths = (
            [item for item in tracked.stdout.split("\0") if item]
            if tracked.returncode == 0
            else []
        )
        tracked_files_present = any(
            (path / item).exists() or (path / item).is_symlink()
            for item in tracked_paths
        )
        git_index = self._git(path, "rev-parse", "--git-path", "index")
        index_path = Path(git_index.stdout.strip()) if git_index.returncode == 0 else None
        if index_path is not None and not index_path.is_absolute():
            index_path = path / index_path
        index_missing = index_path is not None and not index_path.exists()
        status_lines = porcelain.stdout.splitlines()
        legacy_status_shape = not status_lines or (
            len(status_lines) == len(tracked_paths)
            and all(line.startswith("D  ") for line in status_lines)
        )
        empty_worktree = bool(
            tracked_paths
            and not tracked_files_present
            and index_missing
            and legacy_status_shape
        )
        dirty = bool(porcelain.stdout.strip()) and not empty_worktree
        symbolic = self._git(path, "symbolic-ref", "-q", "HEAD")
        counts = self._git(
            path,
            "rev-list",
            "--left-right",
            "--count",
            f"HEAD...origin/{integration_ref}",
        )
        ahead = behind = None
        if counts.returncode == 0:
            parts = counts.stdout.split()
            if len(parts) == 2 and all(item.isdigit() for item in parts):
                ahead, behind = (int(parts[0]), int(parts[1]))
        return {
            "path": str(path),
            "status": (
                "empty_worktree"
                if empty_worktree
                else "dirty" if dirty else "ready"
            ),
            "dirty": dirty,
            "empty_worktree": empty_worktree,
            "detached": symbolic.returncode != 0,
            "ahead": ahead,
            "behind": behind,
        }

    def project_clone_evidence_state(
        self,
        project_name: str,
        project_entry: Any,
        integration_ref: str,
    ) -> str:
        """Digest the intended fleet clone without exposing its local path."""
        entry = project_entry if isinstance(project_entry, dict) else {}
        target = Path(
            entry.get("fleet_clone_dir") or self._fleet_clone_default(project_name)
        ).expanduser().resolve()
        state = self._clone_state(target, integration_ref)
        state.pop("path", None)
        if target.exists() and state.get("status") != "invalid":
            head = self._git(target, "rev-parse", "HEAD")
            state["head"] = head.stdout.strip() if head.returncode == 0 else None
        else:
            state["head"] = None
        return _bounded_evidence_digest(state, "fleet clone state")

    def prepare_fleet_clone(
        self, registry_payload: Any, project_name: Any
    ) -> dict[str, Any]:
        if not isinstance(registry_payload, dict):
            raise ValueError("project_registry payload is missing")
        registry = copy.deepcopy(registry_payload.get("registry"))
        expected = registry_payload.get("expected_sha256")
        if not isinstance(registry, dict) or not isinstance(
            registry.get("projects"), dict
        ):
            raise ValueError("project_registry is malformed")
        if not isinstance(project_name, str) or project_name not in registry["projects"]:
            raise ValueError("project is not registered")
        entry = registry["projects"][project_name]
        source = Path(entry["work_dir"]).expanduser().resolve()
        target = Path(
            entry.get("fleet_clone_dir") or self._fleet_clone_default(project_name)
        ).expanduser().resolve()
        if source == target:
            raise ValueError("fleet_clone_dir must differ from the operator checkout")
        for other_name, other_entry in registry["projects"].items():
            other_target = other_entry.get("fleet_clone_dir")
            if (
                other_name != project_name
                and isinstance(other_target, str)
                and Path(other_target).expanduser().resolve() == target
            ):
                raise ValueError("fleet_clone_dir is already assigned to another project")
        integration_ref = str(entry.get("integration_ref", "main"))
        creating = not (target.exists() or target.is_symlink())
        try:
            origin = self._clone_origin_preflight(source, integration_ref)
            target.parent.mkdir(parents=True, exist_ok=True)
            if creating:
                self._git(
                    target.parent,
                    "clone",
                    "--branch",
                    integration_ref,
                    "--single-branch",
                    "--origin",
                    "origin",
                    "--",
                    origin,
                    str(target),
                    check=True,
                )
            state = self._clone_state(target, integration_ref)
            if state["status"] == "invalid":
                raise ValueError("fleet_clone_dir exists but is not a git repository")
            if state.get("dirty"):
                inspect = f"git -C {shlex.quote(str(target))} status --short"
                raise ValueError(
                    "fleet clone is dirty; refusing to overwrite local changes at "
                    f"{target}; inspect with: {inspect}"
                )
            clone_origin = self._git(target, "remote", "get-url", "origin")
            if clone_origin.returncode or clone_origin.stdout.strip() != origin:
                raise ValueError("fleet clone origin differs from the operator checkout")
            self._git(
                target, "fetch", "--prune", "origin", integration_ref, check=True
            )
            if state.get("empty_worktree"):
                self._git(
                    target,
                    "checkout",
                    "--detach",
                    "--force",
                    f"origin/{integration_ref}",
                    check=True,
                )
            else:
                self._git(
                    target,
                    "merge",
                    "--ff-only",
                    f"origin/{integration_ref}",
                    check=True,
                )
                self._git(target, "checkout", "--detach", "HEAD", check=True)
            final_state = self._clone_state(target, integration_ref)
            if final_state["status"] != "ready" or (
                final_state.get("ahead"), final_state.get("behind")
            ) != (0, 0):
                raise ValueError("fleet clone checkout verification failed")
        except FleetCloneGitError as exc:
            if creating and (target.exists() or target.is_symlink()):
                shutil.rmtree(target)
            self._log_clone_failure(project_name, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - clean partial first-time clones.
            if creating and (target.exists() or target.is_symlink()):
                shutil.rmtree(target)
            if isinstance(exc, ValueError):
                raise
            raise ValueError(f"failed to prepare fleet clone at {target}") from exc
        entry["work_dir_owner"] = "operator"
        entry["fleet_clone_dir"] = str(target)
        return {
            "registry": registry,
            "expected_sha256": expected,
            "project": project_name,
            "clone": final_state,
        }

    def _seat_effective_work_dir(
        self, desired: DesiredSeat, project_name: str, project_entry: dict[str, Any]
    ) -> Path | None:
        if not isinstance(project_entry, dict):
            return None
        fleet_clone = project_entry.get("fleet_clone_dir")
        if isinstance(fleet_clone, str) and fleet_clone.strip():
            return Path(fleet_clone).expanduser().resolve()
        work_dir = project_entry.get("work_dir")
        if desired.seat_dir and desired.repository and isinstance(work_dir, str):
            try:
                repo_leaf = _load_seat_new()._repo_leaf(desired.repository)
            except ValueError:
                repo_leaf = ""
            project_aliases = {
                project_name.casefold(),
                Path(work_dir).name.casefold(),
            }
            clone = Path(desired.seat_dir).expanduser() / repo_leaf
            if repo_leaf.casefold() in project_aliases and (clone / ".git").exists():
                return clone.resolve()
        if isinstance(work_dir, str) and work_dir.strip():
            return Path(work_dir).expanduser().resolve()
        return None

    def _is_seat_using_operator_checkout(
        self, desired: DesiredSeat, project_name: str, project_entry: dict[str, Any]
    ) -> bool:
        if not isinstance(project_entry, dict):
            return False
        if project_entry.get("work_dir_owner", "operator") != "operator":
            return False
        operator_dir = (
            Path(project_entry["work_dir"]).expanduser().resolve()
            if project_entry.get("work_dir")
            else None
        )
        if operator_dir is None:
            return False
        effective = self._seat_effective_work_dir(desired, project_name, project_entry)
        return effective is not None and effective == operator_dir

    def registry(
        self, fleet: dict[str, Any], registry_payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        names = {row.get("name") for row in self.seats()["seats"]}
        records = self.inventory.load()["seats"]
        desired_seats = [self._desired(row) for row in records]
        covered: dict[str, set[str]] = {}
        live_seats: dict[str, dict[str, Any]] = {}
        for agent in fleet.get("agents", []):
            if not isinstance(agent, dict) or agent.get("agent_name") not in names:
                continue
            for seat in agent.get("seats", []):
                if isinstance(seat, dict) and isinstance(seat.get("board_id"), str):
                    covered.setdefault(seat["board_id"], set()).add(agent["agent_name"])
                    live_seats[agent["agent_name"]] = {
                        "status": agent.get("pool_status"),
                        "current_offer": seat.get("current_offer"),
                        "capabilities": seat.get("capabilities", {}),
                    }
        boards = []
        for board in fleet.get("boards", []):
            if not isinstance(board, dict):
                continue
            boards.append(
                {
                    "board_id": board.get("board_id"),
                    "label": board.get("label") or board.get("board_id"),
                    "seat_coverage": len(
                        covered.get(str(board.get("board_id")), set())
                    ),
                    "configured_seats": len(names),
                }
            )
        projects = []
        registry = (
            registry_payload.get("registry")
            if isinstance(registry_payload, dict)
            else None
        )
        for name, entry in (
            registry.get("projects", {}).items() if isinstance(registry, dict) else []
        ):
            if not isinstance(entry, dict):
                continue
            clone_path = Path(
                entry.get("fleet_clone_dir") or self._fleet_clone_default(name)
            ).expanduser().resolve()
            unsafe_seats = [
                d.name
                for d in desired_seats
                if self._is_seat_using_operator_checkout(d, name, entry)
            ]
            projects.append(
                {
                    "name": name,
                    "board_id": entry.get("board_id"),
                    "status": entry.get("status"),
                    "work_dir": entry.get("work_dir"),
                    "work_dir_owner": entry.get("work_dir_owner", "operator"),
                    "fleet_clone_dir": entry.get("fleet_clone_dir"),
                    "default_fleet_clone_dir": str(self._fleet_clone_default(name)),
                    "clone": self._clone_state(clone_path),
                    "operator_checkout_seats": sorted(unsafe_seats),
                }
            )
        result = {
            "boards": boards,
            "seats": live_seats,
            "read_only": registry_payload is None,
        }
        if registry_payload is not None:
            result["projects"] = projects
        return result

    def _start_job(self, action: str, target: Callable[[], Any]) -> dict[str, str]:
        job_id = uuid.uuid4().hex
        with self._lock:
            if len(self._jobs) >= CONFIG_JOB_LIMIT:
                self._jobs.pop(next(iter(self._jobs)))
            self._jobs[job_id] = {
                "job_id": job_id,
                "action": action,
                "status": "queued",
            }

        def work() -> None:
            with self._lock:
                self._jobs[job_id]["status"] = "running"
            try:
                result = target()
            except Exception as exc:  # noqa: BLE001 - return type only.
                with self._lock:
                    self._jobs[job_id].update(status="failed", error=type(exc).__name__)
                self._journal(
                    action, job_id=job_id, status="failed", error=type(exc).__name__
                )
            else:
                with self._lock:
                    self._jobs[job_id].update(status="succeeded", result=result)
                self._journal(action, job_id=job_id, status="succeeded")

        threading.Thread(target=work, daemon=True, name=f"config-{action}").start()
        return {"job_id": job_id, "status": "queued"}

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            value = self._jobs.get(job_id)
            if value is None:
                raise KeyError(job_id)
            return dict(value)

    def doctor(
        self, names: Any = None, registry_payload: dict[str, Any] | None = None
    ) -> dict[str, str]:
        if names is not None and (
            not isinstance(names, list) or not all(isinstance(v, str) for v in names)
        ):
            raise ValueError("names must be a list")

        def run() -> dict[str, Any]:
            records = self.inventory.load()["seats"]
            if names is not None:
                records = [row for row in records if row.get("name") in names]
            reports = []
            registry = (
                registry_payload.get("registry")
                if isinstance(registry_payload, dict)
                else None
            )
            active_projects = {
                name: entry
                for name, entry in (
                    registry.get("projects", {}).items()
                    if isinstance(registry, dict)
                    else []
                )
                if isinstance(entry, dict) and entry.get("status") == "active"
            }
            project_reports = []
            for project_name, entry in sorted(active_projects.items()):
                try:
                    source = Path(str(entry["work_dir"])).expanduser().resolve()
                    integration_ref = str(entry.get("integration_ref", "main"))
                    self._clone_origin_preflight(source, integration_ref)
                except (FleetCloneGitError, KeyError, TypeError, ValueError) as exc:
                    project_reports.append(
                        {
                            "project": project_name,
                            "check": "clone-preflight",
                            "status": "FAIL",
                            "message": self._clean_text(str(exc)),
                        }
                    )
                else:
                    project_reports.append(
                        {
                            "project": project_name,
                            "check": "clone-preflight",
                            "status": "PASS",
                            "message": f"origin exposes {integration_ref}",
                        }
                    )
            for record in records:
                desired = self._desired(record)
                checks = self.doctor_factory().run(desired)
                unsafe_projects = [
                    name
                    for name, entry in active_projects.items()
                    if self._is_seat_using_operator_checkout(desired, name, entry)
                ]
                checks.append(
                    DoctorCheck(
                        desired.name,
                        "working-tree",
                        "FAIL" if unsafe_projects else "PASS",
                        (
                            "operator checkout is read-only for seats; missing "
                            "fleet or seat clones: " + ", ".join(sorted(unsafe_projects))
                            if unsafe_projects
                            else "working tree routes to fleet-owned or seat-owned clone"
                        ),
                    )
                )
                clone_issues: dict[str, list[str]] = {
                    "empty worktree": [],
                    "local changes": [],
                    "unavailable": [],
                }
                checked_clones = 0
                for project_name, entry in active_projects.items():
                    clone_dir = entry.get("fleet_clone_dir")
                    if not isinstance(clone_dir, str) or not clone_dir.strip():
                        continue
                    checked_clones += 1
                    clone_path = Path(clone_dir).expanduser().resolve()
                    clone_state = self._clone_state(clone_path)
                    status = clone_state.get("status")
                    label = f"{project_name} ({clone_path})"
                    if status == "empty_worktree":
                        clone_issues["empty worktree"].append(label)
                    elif status == "dirty":
                        clone_issues["local changes"].append(label)
                    elif status != "ready":
                        clone_issues["unavailable"].append(label)
                if checked_clones:
                    messages = [
                        f"{kind}: {', '.join(values)}"
                        for kind, values in clone_issues.items()
                        if values
                    ]
                    checks.append(
                        DoctorCheck(
                            desired.name,
                            "fleet-clones",
                            "FAIL" if messages else "PASS",
                            "; ".join(messages) if messages else "fleet clones are clean",
                        )
                    )
                report = self._report(checks)
                self.inventory.upsert(
                    desired, bridge_version=self.bridge_installer.version, doctor=report
                )
                reports.append({"seat": desired.name, **report})
            return {"seats": reports, "projects": project_reports}

        return self._start_job("doctor", run)

    def install_bridge(self) -> dict[str, str]:
        return self._start_job(
            "bridge-install", lambda: {"command": self.bridge_installer.install()}
        )

    def upgrade_all(self) -> dict[str, str]:
        def run() -> dict[str, Any]:
            command = self.bridge_installer.install()
            applied = []
            for record in self.inventory.load()["seats"]:
                desired = self._desired({**record, "bridge_command": command})
                adapter = adapter_for(desired)
                result = adapter.apply(adapter.plan(desired))
                self.inventory.upsert(
                    desired, bridge_version=self.bridge_installer.version, doctor=None
                )
                applied.append(
                    {
                        "seat": desired.name,
                        "changed": list(result.changed),
                        "backup_paths": list(result.backups),
                        "backup_path": result.backups[0] if result.backups else None,
                        "needs_restart": bool(result.changed),
                    }
                )
            return {"command": command, "seats": applied}

        return self._start_job("bridge-upgrade-all", run)


class DashboardCache:
    def __init__(
        self, fetcher: FleetFetcher | list[FleetFetcher], ttl_seconds: float
    ) -> None:
        fetchers = fetcher if isinstance(fetcher, list) else [fetcher]
        if not fetchers:
            raise ValueError("at least one central is required")
        self._async_runner = _ReusableAsyncRunner()
        self.fetchers: dict[str, FleetFetcher] = {}
        for item in fetchers:
            label = getattr(getattr(item, "config", None), "label", "default")
            if label in self.fetchers:
                raise ValueError(f"duplicate central label: {label}")
            self.fetchers[label] = item
            enable_reuse = getattr(item, "enable_client_reuse", None)
            if callable(enable_reuse):
                enable_reuse()
        self.default_central = next(iter(self.fetchers))
        # Preserve these public attributes for single-central callers/tests.
        self.fetcher = self.fetchers[self.default_central]
        self.ttl_seconds = ttl_seconds
        self.fleet = TimedCache(ttl_seconds, self.fetcher.fetch, self._async_runner.run)
        self._fleets = {
            label: self.fleet
            if label == self.default_central
            else TimedCache(ttl_seconds, item.fetch, self._async_runner.run)
            for label, item in self.fetchers.items()
        }
        self._detail_lock = threading.Lock()
        self._details: dict[tuple[str, str], TimedCache] = {}

    def labels(self) -> list[str]:
        return list(self.fetchers)

    def resolve_central(self, central: str | None = None) -> str:
        label = self.default_central if central is None else central
        if label not in self.fetchers:
            raise KeyError(label)
        return label

    def overhead_path(self, central: str | None, single_central_fallback: Path) -> Path:
        label = self.resolve_central(central)
        configured = self.fetchers[label].config.overhead_path
        if configured is not None:
            return configured
        if len(self.fetchers) == 1:
            return single_central_fallback
        raise RuntimeError("central overhead source is not configured")

    def central_url(self, central: str | None = None) -> str:
        label = self.resolve_central(central)
        return self.fetchers[label].config.url

    @staticmethod
    def _labeled(value: dict[str, Any], label: str) -> dict[str, Any]:
        return {**value, "central": label}

    def get(self, central: str | None = None) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(self._fleets[label].get(), label)

    def get_board(self, board_id: str, central: str | None = None) -> dict[str, Any]:
        label = self.resolve_central(central)
        key = (label, board_id)
        with self._detail_lock:
            cache = self._details.get(key)
            if cache is None:
                cache = TimedCache(
                    self.ttl_seconds,
                    lambda: self.fetchers[label].fetch_board(board_id),
                    self._async_runner.run,
                )
                self._details[key] = cache
        try:
            return self._labeled(cache.get(), label)
        except Exception:
            # Unknown or unavailable board IDs must not grow the cache forever.
            with self._detail_lock:
                if self._details.get(key) is cache:
                    self._details.pop(key, None)
            raise

    def get_config(self, central: str | None = None) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(self.fetchers[label].fetch_config()), label
        )

    def get_autonomous_butler(
        self, board_id: str, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].fetch_autonomous_butler(board_id)
            ),
            label,
        )

    def save_autonomous_butler(
        self, board_id: str, request: Any, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].save_autonomous_butler(board_id, request)
            ),
            label,
        )

    def submit_autonomous_butler_command(
        self, board_id: str, request: Any, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].submit_autonomous_butler_command(
                    board_id, request
                )
            ),
            label,
        )

    def get_project_registry(
        self, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(self.fetchers[label].fetch_project_registry()),
            label,
        )

    def get_project_evidence_state(
        self,
        project_name: str,
        board_id: str,
        central: str | None = None,
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._async_runner.run(
            self.fetchers[label].project_evidence_state(project_name, board_id)
        )

    def save_project_registry(
        self,
        value: Any,
        expected_sha256: Any,
        central: str | None = None,
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].save_project_registry(value, expected_sha256)
            ),
            label,
        )

    def get_overhead_thresholds(
        self, central: str | None = None
    ) -> dict[str, int | float]:
        payload = self.get_config(central)
        config = payload.get("config")
        thresholds = config.get("thresholds") if isinstance(config, dict) else None
        return context_pressure_thresholds(thresholds)

    def get_intake(self, board_id: str, central: str | None = None) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(self.fetchers[label].fetch_intake(board_id)), label
        )

    def get_dispatch(
        self, board_id: str, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(self.fetchers[label].fetch_dispatch(board_id)), label
        )

    def save_dispatch(
        self, board_id: str, value: Any, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].save_dispatch(board_id, value)
            ),
            label,
        )

    def retire_agent(
        self, board_id: str, agent_id: str, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].retire_agent(board_id, agent_id)
            ),
            label,
        )

    def retire_inert(
        self, board_id: str, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(self.fetchers[label].retire_inert(board_id)), label
        )

    def resolve_human_request(
        self,
        board_id: str,
        payload: dict[str, Any],
        central: str | None = None,
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].resolve_human_request(board_id, payload)
            ),
            label,
        )

    def mark_butler_draft(
        self,
        board_id: str,
        payload: dict[str, Any],
        central: str | None = None,
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].mark_butler_draft(board_id, payload)
            ),
            label,
        )

    def save_config(
        self,
        value: Any,
        expected_sha256: str | None,
        central: str | None = None,
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].save_config(value, expected_sha256)
            ),
            label,
        )

    def save_intake(
        self, board_id: Any, text: Any, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].save_intake(board_id, text)
            ),
            label,
        )

    def decide_intake(
        self,
        board_id: Any,
        ask_id: Any,
        action: Any,
        expected_sha256: Any,
        title: Any = None,
        central: str | None = None,
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].decide_intake(
                    board_id, ask_id, action, expected_sha256, title
                )
            ),
            label,
        )

    def get_doors(self, central: str | None = None) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(self.fetchers[label].fetch_doors()), label
        )

    def copy_door(
        self, board_id: str, role: str, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(self.fetchers[label].copy_door(board_id, role)),
            label,
        )

    def rotate_door(
        self, board_id: str, role: str, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(self.fetchers[label].rotate_door(board_id, role)),
            label,
        )

    def add_project(
        self,
        project_name: str,
        board_id: str,
        work_dir: str,
        integration_ref: str = "main",
        seats_manager: Any = None,
        central: str | None = None,
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            self._async_runner.run(
                self.fetchers[label].add_project(
                    project_name=project_name,
                    board_id=board_id,
                    work_dir=work_dir,
                    integration_ref=integration_ref,
                    seats_manager=seats_manager,
                )
            ),
            label,
        )

    def close(self) -> None:
        for fetcher in self.fetchers.values():
            close = getattr(fetcher, "close", None)
            if callable(close):
                close()
        self._async_runner.close()


UI_ROOT = Path(__file__).resolve().with_name("ui")
PRIMARY_VIEW_NAMES = (
    "home",
    "projects",
    "work",
    "team",
    "approvals",
    "activity",
    "settings",
)


def _route_css_asset_paths(
    ui_root: Path = UI_ROOT,
) -> dict[str, tuple[str, Path]]:
    return {
        f"/ui/views/{name}.css": (
            "text/css; charset=utf-8",
            ui_root / "views" / f"{name}.css",
        )
        for name in PRIMARY_VIEW_NAMES
        if (ui_root / "views" / f"{name}.css").is_file()
    }


UI_ASSET_PATHS = {
    "/ui/assets/fleet.css": ("text/css; charset=utf-8", UI_ROOT / "assets" / "fleet.css"),
    "/ui/assets/app.js": ("text/javascript; charset=utf-8", UI_ROOT / "assets" / "app.js"),
    "/ui/view-registry.js": ("text/javascript; charset=utf-8", UI_ROOT / "view-registry.js"),
    **{
        f"/ui/views/{name}.js": (
            "text/javascript; charset=utf-8",
            UI_ROOT / "views" / f"{name}.js",
        )
        for name in PRIMARY_VIEW_NAMES
    },
    **_route_css_asset_paths(),
}


def _packaged_ui_assets() -> dict[str, tuple[str, bytes, str]]:
    assets: dict[str, tuple[str, bytes, str]] = {}
    for route, (content_type, path) in UI_ASSET_PATHS.items():
        body = path.read_bytes()
        assets[route] = (content_type, body, f'"{hashlib.sha256(body).hexdigest()}"')
    return assets


UI_ASSETS = _packaged_ui_assets()
HTML_SHELL = (UI_ROOT / "index.html").read_text(encoding="utf-8")

# Compatibility fixture for source-level tests that predate packaged assets. The
# server never sends this reconstructed inline document; production serves
# HTML_SHELL and the separately cached UI_ASSETS below.
_route_module_scripts = "\n".join(
    UI_ASSETS[route][1].decode("utf-8")
    for route in (
        "/ui/view-registry.js",
        "/ui/views/home.js",
        "/ui/views/projects.js",
        "/ui/views/work.js",
        "/ui/views/team.js",
        "/ui/views/approvals.js",
        "/ui/views/activity.js",
        "/ui/views/settings.js",
    )
)
_app_script_parts = UI_ASSETS["/ui/assets/app.js"][1].decode("utf-8").split(
    "/*__FLEET_SCRIPT_BOUNDARY__*/"
)
_legacy_script_parts = [
    *_app_script_parts[:8],
    _route_module_scripts,
    *_app_script_parts[8:],
]
HTML = re.sub(
    r'<script src="/ui/[^>]+></script>',
    "",
    HTML_SHELL.replace(
        '<link rel="stylesheet" href="/ui/assets/fleet.css">',
        f"<style>{UI_ASSETS['/ui/assets/fleet.css'][1].decode('utf-8')}</style>",
    ),
)
HTML = HTML.replace(
    "</body>",
    "".join(f"<script>\n{part}\n</script>" for part in _legacy_script_parts)
    + "</body>",
    1,
)



def make_handler(
    cache: DashboardCache,
    stats_path: str | Path | None = None,
    worker_manager: WorkerManager | None = None,
    seat_manager: SeatConfigManager | None = None,
    evidence_trace: EvidenceTrace | None = None,
    butler_manager: ButlerSettingsManager | None = None,
    deployment: dict[str, Any] | None = None,
) -> type[BaseHTTPRequestHandler]:
    selected_stats_path = (
        bridge_stats_path() if stats_path is None else Path(stats_path)
    )
    workers = worker_manager or WorkerManager()
    seats = seat_manager or SeatConfigManager()
    butlers = butler_manager or ButlerSettingsManager(_default_butler_secrets_dir())
    project_operation_lock = threading.RLock()
    deployed_revision = deployment_metadata() if deployment is None else dict(deployment)

    def requested_central(path: str) -> str | None:
        values = parse_qs(urlsplit(path).query, keep_blank_values=True).get("central")
        if values is None:
            return None
        if len(values) != 1 or not CENTRAL_LABEL_RE.fullmatch(values[0]):
            raise ValueError("invalid central")
        return values[0]

    def requested_board(path: str) -> str:
        values = parse_qs(urlsplit(path).query, keep_blank_values=True).get("board_id")
        if values is None or len(values) != 1 or not BOARD_ID_RE.fullmatch(values[0]):
            raise ValueError("invalid board_id")
        return values[0]

    def central_label(value: str | None) -> str:
        resolver = getattr(cache, "resolve_central", None)
        if callable(resolver):
            return resolver(value)
        return value or "default"

    def cache_call(name: str, *args: Any, central: str | None, **kwargs: Any) -> dict[str, Any]:
        method = getattr(cache, name)
        if kwargs:
            return method(*args, central=central, **kwargs)
        return method(*args) if central is None else method(*args, central)

    def fleet_seats(central: str | None) -> set[str]:
        fleet = cache_call("get", central=central)
        return {
            row["agent_name"]
            for row in fleet.get("agents", [])
            if isinstance(row, dict) and isinstance(row.get("agent_name"), str)
        }

    def worker_rows(central: str | None) -> list[dict[str, Any]]:
        fleet = cache_call("get", central=central)
        agents = {
            row["agent_name"]: row
            for row in fleet.get("agents", [])
            if isinstance(row, dict) and isinstance(row.get("agent_name"), str)
        }
        rows = workers.list(set(agents))
        pressure: dict[str, dict[str, Any]] = {}
        try:
            resolver = getattr(cache, "overhead_path", None)
            path = (
                resolver(central, selected_stats_path)
                if callable(resolver)
                else selected_stats_path
            )
            pressure = {
                item["agent_name"]: item
                for item in read_overhead_stats(path).get("sessions", [])
                if isinstance(item, dict) and isinstance(item.get("agent_name"), str)
            }
        except Exception:  # noqa: BLE001 - local telemetry is optional.
            pressure = {}
        for row in rows:
            live = agents.get(row["name"], {})
            seats = live.get("seats", []) if isinstance(live, dict) else []
            row["pool_status"] = live.get("pool_status") if live else None
            row["current_work"] = [
                {
                    "board_id": seat.get("board_id"),
                    "role": seat.get("role"),
                    "ticket_id": seat.get("current_ticket_id"),
                    "ticket_title": seat.get("current_ticket_title"),
                }
                for seat in seats
                if isinstance(seat, dict) and seat.get("current_ticket_id")
            ][:8]
            row["pressure"] = pressure.get(row["name"])
            row["log_tail"] = workers.log_tail(row["name"])
            if not row["running"]:
                workers._store_active_review(row["name"], None)
                continue
            active_review = workers.active_review(row["name"])
            if active_review is not None and not row["current_work"]:
                row["current_work"] = [
                    {
                        **active_review,
                        "role": "reviewer",
                        "ticket_title": active_review["ticket_id"],
                    }
                ]
        return rows

    def selected_central_url(central: str | None) -> str:
        resolver = getattr(cache, "central_url", None)
        if not callable(resolver):
            raise RuntimeError(  # noqa: TRY004 - unavailable runtime capability.
                "central URL is unavailable"
            )
        return str(resolver(central))

    class Handler(BaseHTTPRequestHandler):
        def _prepare_evidence(
            self, method: str, route: str, raw_request: bytes | None = None
        ) -> None:
            self._evidence_context = None
            self._evidence_before = None
            self._evidence_after = None
            self._project_authorization_denied = False
            self._project_evidence_request = None
            if evidence_trace is None:
                return
            context = evidence_trace.context(self.headers, method, route)
            if context is None:
                return
            if method == "POST" and (
                raw_request is None
                or hashlib.sha256(raw_request).hexdigest() != context.action_sha256
            ):
                return
            self._evidence_context = context

        def _prepare_project_evidence(
            self, request: Any, central: str | None
        ) -> dict[str, Any] | None:
            context = getattr(self, "_evidence_context", None)
            if (
                evidence_trace is None
                or context is None
                or not isinstance(request, dict)
                or request.get("name") != context.entity
                or request.get("board_id") != evidence_trace.board_id
            ):
                self._evidence_context = None
                return None
            try:
                state = cache_call(
                    "get_project_evidence_state",
                    request["name"],
                    request["board_id"],
                    central=central,
                )
                label = cache.resolve_central(central)
                fetcher = cache.fetchers[label]
                door_state = _door_evidence_digests(
                    fetcher.config, request["board_id"]
                )
                clone_state = seats.project_clone_evidence_state(
                    request["name"],
                    state.pop("project_entry", None),
                    request.get("integration_ref", "main"),
                )
                snapshot = {
                    "registry": state["registry"],
                    "board": state["board"],
                    **door_state,
                    "clone": clone_state,
                }
                if set(snapshot) != {
                    "registry", "board", "credentials", "keys", "clone"
                } or any(
                    not isinstance(value, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", value)
                    for value in snapshot.values()
                ):
                    raise ValueError("project evidence snapshot is invalid")
                self._project_evidence_request = {
                    "project": request["name"],
                    "board_id": request["board_id"],
                    "action_sha256": context.action_sha256,
                }
                return {
                    domain: digest for domain, digest in snapshot.items()
                }
            except Exception:  # noqa: BLE001 - tracing must stay fail-passive.
                self._evidence_context = None
                return None

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            evidence_headers: dict[str, str] = {}
            context = getattr(self, "_evidence_context", None)
            before = getattr(self, "_evidence_before", None)
            after = getattr(self, "_evidence_after", None)
            if (
                evidence_trace is not None
                and context is not None
                and before is not None
                and after is not None
                and content_type.startswith("application/json")
            ):
                try:
                    if (
                        status == 403
                        and getattr(self, "_project_authorization_denied", False)
                        and isinstance(before, dict)
                        and isinstance(after, dict)
                        and set(before) == set(after)
                        == {"registry", "board", "credentials", "keys", "clone"}
                        and isinstance(
                            getattr(self, "_project_evidence_request", None), dict
                        )
                    ):
                        document = json.loads(body)
                        if isinstance(document, dict):
                            document["_project_preflight"] = {
                                "schema_version": 1,
                                "request": self._project_evidence_request,
                                "result": {
                                    "status": status,
                                    "decision": "denied",
                                },
                                "domains": {
                                    domain: {
                                        "before_sha256": before[domain],
                                        "after_sha256": after[domain],
                                    }
                                    for domain in sorted(before)
                                },
                            }
                            body = _json_bytes(document)
                    metadata, _emitted = evidence_trace.observe(
                        context=context,
                        method=self.command,
                        route=urlsplit(self.path).path,
                        status=status,
                        before=before,
                        after=after,
                        result_body=body,
                    )
                    document = json.loads(body)
                    if metadata is not None and isinstance(document, dict):
                        document["_evidence"] = metadata
                        body = _json_bytes(document)
                        evidence_headers = {
                            header: getattr(context, key)
                            for key, header in CORRELATION_HEADERS.items()
                        }
                except Exception:  # noqa: BLE001 - observability is fail-passive.
                    pass
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'",
            )
            for header, value in evidence_headers.items():
                self.send_header(header, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_ui_asset(self, route: str) -> None:
            content_type, body, etag = UI_ASSETS[route]
            not_modified = self.headers.get("If-None-Match") == etag
            self.send_response(304 if not_modified else 200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", "0" if not_modified else str(len(body)))
            self.send_header("Cache-Control", "public, max-age=0, must-revalidate")
            self.send_header("ETag", etag)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'",
            )
            self.end_headers()
            if not not_modified:
                self.wfile.write(body)

        def _config_post_guard(self) -> tuple[int, bytes] | None:
            host = self.headers.get("Host", "")
            try:
                parsed_host = urlsplit("//" + host)
                hostname = (parsed_host.hostname or "").casefold()
                port = parsed_host.port or 80
                loopback_host = hostname == "localhost" or ipaddress.ip_address(
                    hostname
                ).is_loopback
            except ValueError:
                return 403, b'{"error":"loopback Host required"}'
            server_port = int(getattr(self.server, "server_port", 0))
            if (
                not loopback_host
                or port != server_port
                or parsed_host.username is not None
                or parsed_host.password is not None
                or parsed_host.path
                or parsed_host.query
                or parsed_host.fragment
            ):
                return 403, b'{"error":"loopback Host required"}'
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + host:
                return 403, b'{"error":"same-origin request required"}'
            if self.headers.get_content_type().casefold() != "application/json":
                return 415, b'{"error":"application/json required"}'
            return None

        def do_GET(self) -> None:
            route = urlsplit(self.path).path
            self._prepare_evidence("GET", route)
            if route == "/":
                self._send(200, "text/html; charset=utf-8", HTML_SHELL.encode("utf-8"))
                return
            if route in UI_ASSETS:
                self._send_ui_asset(route)
                return
            if route == "/api/version":
                self._send(
                    200,
                    "application/json; charset=utf-8",
                    _json_bytes(deployed_revision),
                )
                return
            if route == "/api/centrals":
                labels_method = getattr(cache, "labels", None)
                labels = labels_method() if callable(labels_method) else ["default"]
                body = _json_bytes({"centrals": labels, "default": labels[0]})
                self._send(200, "application/json; charset=utf-8", body)
                return
            config_job = re.fullmatch(r"/api/config/jobs/([a-f0-9]{32})", route)
            if (
                route
                in {
                    "/api/config/seats",
                    "/api/config/bridge",
                    "/api/attention",
                    "/api/config/release",
                }
                or config_job
            ):
                try:
                    if route == "/api/config/seats":
                        payload = seats.seats()
                    elif route == "/api/config/bridge":
                        payload = seats.bridge()
                    elif route == "/api/config/release":
                        payload = seats.release_status()
                    elif route == "/api/attention":
                        payload = seats.attention_state()
                    else:
                        payload = seats.job(config_job.group(1))
                except KeyError:
                    payload = {"error": "job not found"}
                    if getattr(self, "_evidence_context", None) is not None:
                        self._evidence_before = payload
                        self._evidence_after = payload
                    self._send(
                        404,
                        "application/json; charset=utf-8",
                        _json_bytes(payload),
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - bounded type only.
                    payload = {"error": type(exc).__name__}
                    if getattr(self, "_evidence_context", None) is not None:
                        self._evidence_before = payload
                        self._evidence_after = payload
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        _json_bytes(payload),
                    )
                    return
                if getattr(self, "_evidence_context", None) is not None:
                    self._evidence_before = payload
                    self._evidence_after = payload
                self._send(200, "application/json; charset=utf-8", _json_bytes(payload))
                return
            try:
                central = requested_central(self.path)
                label = central_label(central)
            except (KeyError, ValueError):
                self._send(
                    404,
                    "application/json; charset=utf-8",
                    b'{"error":"central not found"}',
                )
                return
            if route == "/api/fleet":
                try:
                    body = _json_bytes(cache_call("get", central=central))
                except Exception as exc:  # noqa: BLE001 - return bounded leaf error.
                    try:
                        central_url = selected_central_url(central)
                    except Exception:  # noqa: BLE001 - preserve the original failure.
                        central_url = None
                    body = _json_bytes(
                        _api_exception_payload(route, label, central_url, exc)
                    )
                    self._send(503, "application/json; charset=utf-8", body)
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            if route == "/api/config/registry":
                try:
                    body = _json_bytes(
                        seats.registry(
                            cache_call("get", central=central),
                            cache_call("get_project_registry", central=central),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - bounded type only.
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": type(exc).__name__, "central": label}),
                    )
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            if route == "/api/doors":
                try:
                    is_loopback = ipaddress.ip_address(
                        self.client_address[0]
                    ).is_loopback
                except ValueError:
                    is_loopback = False
                if not is_loopback:
                    self._send(
                        403,
                        "application/json; charset=utf-8",
                        b'{"error":"loopback required"}',
                    )
                    return
                host = self.headers.get("Host", "")
                try:
                    parsed_host = urlsplit("//" + host)
                    hostname = (parsed_host.hostname or "").casefold()
                    port = parsed_host.port or 80
                    loopback_host = hostname == "localhost" or ipaddress.ip_address(
                        hostname
                    ).is_loopback
                except ValueError:
                    loopback_host = False
                server_port = int(getattr(self.server, "server_port", 0))
                if not loopback_host or (server_port and port != server_port):
                    self._send(
                        403,
                        "application/json; charset=utf-8",
                        b'{"error":"loopback Host required"}',
                    )
                    return
                origin = self.headers.get("Origin")
                if origin is not None and origin != "http://" + host:
                    self._send(
                        403,
                        "application/json; charset=utf-8",
                        b'{"error":"same-origin request required"}',
                    )
                    return
                try:
                    body = _json_bytes(cache_call("get_doors", central=central))
                except ValueError as exc:
                    self._send(
                        400,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": str(exc), "central": label}),
                    )
                    return
                except PermissionError as exc:
                    self._send(
                        403,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": str(exc), "central": label}),
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": type(exc).__name__, "central": label}),
                    )
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            if route == "/api/overhead":
                try:
                    resolver = getattr(cache, "overhead_path", None)
                    overhead_path = (
                        resolver(central, selected_stats_path)
                        if callable(resolver)
                        else selected_stats_path
                    )
                    threshold_resolver = getattr(cache, "get_overhead_thresholds", None)
                    try:
                        pressure_thresholds = (
                            threshold_resolver(central=central)
                            if callable(threshold_resolver)
                            else None
                        )
                    except Exception:  # noqa: BLE001 - optional local settings.
                        pressure_thresholds = None
                    body = _json_bytes(
                        {
                            **read_overhead_stats(
                                overhead_path, thresholds=pressure_thresholds
                            ),
                            "central": label,
                        }
                    )
                except RuntimeError as exc:
                    body = _json_bytes({"error": str(exc), "central": label})
                    self._send(503, "application/json; charset=utf-8", body)
                    return
                except Exception as exc:  # noqa: BLE001 - bounded HTTP error.
                    body = _json_bytes({"error": type(exc).__name__, "central": label})
                    self._send(503, "application/json; charset=utf-8", body)
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            if route == "/api/config":
                try:
                    body = _json_bytes(cache_call("get_config", central=central))
                except Exception as exc:  # noqa: BLE001
                    body = _json_bytes({"error": type(exc).__name__, "central": label})
                    self._send(503, "application/json; charset=utf-8", body)
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            if route == "/api/butler":
                try:
                    payload = butlers.view(
                        cache_call("get_config", central=central), label
                    )
                    body = _json_bytes(payload)
                except (ButlerSettingsError, ValueError) as exc:
                    self._send(
                        400,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": str(exc), "central": label}),
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - bounded type only.
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": type(exc).__name__, "central": label}),
                    )
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            if route == "/api/butler/autonomous":
                try:
                    board_id = requested_board(self.path)
                    body = _json_bytes(
                        cache_call(
                            "get_autonomous_butler", board_id, central=central
                        )
                    )
                except (ButlerSettingsError, ValueError) as exc:
                    self._send(
                        400,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": str(exc), "central": label}),
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - bounded type only.
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": type(exc).__name__, "central": label}),
                    )
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            if route == "/api/intake":
                try:
                    board_id = requested_board(self.path)
                    body = _json_bytes(
                        cache_call("get_intake", board_id, central=central)
                    )
                except ValueError as exc:
                    self._send(
                        400,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": str(exc), "central": label}),
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": type(exc).__name__, "central": label}),
                    )
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            if route == "/api/dispatch":
                try:
                    board_id = requested_board(self.path)
                    body = _json_bytes(
                        cache_call("get_dispatch", board_id, central=central)
                    )
                except ValueError as exc:
                    self._send(
                        400,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": str(exc), "central": label}),
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - bounded type only.
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": type(exc).__name__, "central": label}),
                    )
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            if route == "/api/workers":
                try:
                    body = _json_bytes(
                        {
                            "central": label,
                            "enabled": workers.enabled,
                            "workers": worker_rows(central),
                            "roles": list(workers.roles),
                            "presets": {
                                key: {
                                    "label": value[0],
                                    "base_url": value[1],
                                    "key_required": value[2],
                                }
                                for key, value in PROVIDER_PRESETS.items()
                            },
                        }
                    )
                except RuntimeError as exc:
                    self._send(
                        501,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": str(exc), "central": label}),
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - bounded leaf error.
                    try:
                        central_url = selected_central_url(central)
                    except Exception:  # noqa: BLE001 - preserve the original failure.
                        central_url = None
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        _json_bytes(
                            _api_exception_payload(route, label, central_url, exc)
                        ),
                    )
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            board_id = board_id_from_api_path(self.path)
            if board_id is not None:
                try:
                    body = _json_bytes(
                        cache_call("get_board", board_id, central=central)
                    )
                except KeyError:
                    self._send(
                        404,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": "board not found", "central": label}),
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - bounded type only.
                    body = _json_bytes({"error": type(exc).__name__, "central": label})
                    self._send(503, "application/json; charset=utf-8", body)
                    return
                if len(body) > API_MAX_BYTES:
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        _json_bytes(
                            {
                                "error": "detail response exceeds byte cap",
                                "central": label,
                            }
                        ),
                    )
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')

        def do_POST(self) -> None:
            route = urlsplit(self.path).path
            self._evidence_context = None
            self._evidence_before = None
            config_routes = {
                "/api/config/plan",
                "/api/config/suggestions",
                "/api/config/apply",
                "/api/config/prompt",
                "/api/config/doctor",
                "/api/config/import",
                "/api/config/bridge/install",
                "/api/config/bridge/upgrade-all",
                "/api/config/ops/plan",
                "/api/config/ops",
                "/api/config/registry/clone",
                "/api/butler",
                "/api/butler/autonomous",
                "/api/butler/autonomous/command",
                "/api/butler/kill",
                "/api/dispatch",
                "/api/agents/retire",
                "/api/agents/retire-inert",
                "/api/attention",
                "/api/human/resolve",
                "/api/butler/mark",
                "/api/doors/copy",
                "/api/doors/rotate",
                "/api/projects/add",
            }
            worker_action = re.fullmatch(
                r"/api/workers/([a-z0-9-]{2,32})/(test|start|stop|restart)", route
            )
            if (
                route
                not in {"/api/config", "/api/intake", "/api/workers"} | config_routes
                and worker_action is None
            ):
                self._send(
                    404, "application/json; charset=utf-8", b'{"error":"not found"}'
                )
                return
            if route in config_routes:
                try:
                    is_loopback = ipaddress.ip_address(
                        self.client_address[0]
                    ).is_loopback
                except ValueError:
                    is_loopback = False
                if not is_loopback:
                    self._send(
                        403,
                        "application/json; charset=utf-8",
                        b'{"error":"loopback required"}',
                    )
                    return
                guard_error = self._config_post_guard()
                if guard_error is not None:
                    status, body = guard_error
                    self._send(status, "application/json; charset=utf-8", body)
                    return
            try:
                central = requested_central(self.path)
                label = central_label(central)
            except (KeyError, ValueError):
                self._send(
                    404,
                    "application/json; charset=utf-8",
                    b'{"error":"central not found"}',
                )
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            body_limit = (
                CONFIG_API_MAX_BYTES if route in config_routes else WORKER_API_MAX_BYTES
            )
            if not 1 <= length <= body_limit:
                self._evidence_context = None
                self._send(
                    400,
                    "application/json; charset=utf-8",
                    _json_bytes({"error": "invalid body size", "central": label}),
                )
                return
            try:
                raw_request = self.rfile.read(length)
                self._prepare_evidence("POST", route, raw_request)
                request = json.loads(raw_request)
                if route == "/api/config/plan":
                    body = _json_bytes(seats.plan(request))
                elif route == "/api/config/suggestions":
                    body = _json_bytes(seats.suggestions(request))
                elif route == "/api/config/apply":
                    if not isinstance(request, dict) or set(request) != {"plan_id"}:
                        raise ValueError("request must contain only plan_id")
                    body = _json_bytes(seats.apply(request["plan_id"]))
                elif route == "/api/config/prompt":
                    body = _json_bytes(seats.prompt(request))
                elif route == "/api/config/doctor":
                    if not isinstance(request, dict) or not set(request) <= {"names"}:
                        raise ValueError("request may contain only names")
                    body = _json_bytes(
                        seats.doctor(
                            request.get("names"),
                            cache_call("get_project_registry", central=central),
                        )
                    )
                elif route == "/api/config/import":
                    if not isinstance(request, dict) or not set(request) <= {"names"}:
                        raise ValueError("request may contain only names")
                    body = _json_bytes(seats.import_discovered(request.get("names")))
                elif route == "/api/config/bridge/install":
                    if request != {}:
                        raise ValueError("bridge install body must be an empty object")
                    body = _json_bytes(seats.install_bridge())
                elif route == "/api/config/bridge/upgrade-all":
                    if request != {}:
                        raise ValueError("bridge upgrade body must be an empty object")
                    body = _json_bytes(seats.upgrade_all())
                elif route == "/api/config/ops/plan":
                    if not isinstance(request, dict) or "action" not in request:
                        raise ValueError("ops plan request must be an object with an action")
                    body = _json_bytes(seats.prepare_ops_action(**request))
                elif route == "/api/config/ops":
                    if not isinstance(request, dict) or set(request) != {
                        "plan_id",
                        "digest",
                    }:
                        raise ValueError(
                            "ops request must contain only plan_id and digest"
                        )
                    body = _json_bytes(seats.ops_action(**request))
                elif route == "/api/config/registry/clone":
                    if not isinstance(request, dict) or set(request) != {"project"}:
                        raise ValueError("request must contain only project")
                    prepared = seats.prepare_fleet_clone(
                        cache_call("get_project_registry", central=central),
                        request["project"],
                    )
                    saved = cache_call(
                        "save_project_registry",
                        prepared["registry"],
                        prepared["expected_sha256"],
                        central=central,
                    )
                    body = _json_bytes(
                        {
                            "ok": True,
                            "project": prepared["project"],
                            "clone": prepared["clone"],
                            "registry": saved["registry"],
                            "expected_sha256": saved["expected_sha256"],
                            "central": label,
                        }
                    )
                elif route == "/api/butler":
                    current = cache_call("get_config", central=central)
                    body = _json_bytes(
                        butlers.save(
                            current,
                            request,
                            label,
                            lambda value, expected: cache_call(
                                "save_config",
                                value,
                                expected,
                                central=central,
                            ),
                        )
                    )
                elif route == "/api/butler/autonomous":
                    if not isinstance(request, dict) or not isinstance(
                        request.get("board_id"), str
                    ):
                        raise ValueError("request must contain a board_id")
                    body = _json_bytes(
                        cache_call(
                            "save_autonomous_butler",
                            request["board_id"],
                            request,
                            central=central,
                        )
                    )
                elif route == "/api/butler/autonomous/command":
                    if not isinstance(request, dict) or not isinstance(
                        request.get("board_id"), str
                    ):
                        raise ValueError("request must contain a board_id")
                    body = _json_bytes(
                        cache_call(
                            "submit_autonomous_butler_command",
                            request["board_id"],
                            request,
                            central=central,
                        )
                    )
                elif route == "/api/butler/kill":
                    if request != {}:
                        raise ValueError("request must be an empty object")
                    body = _json_bytes(
                        butlers.kill(
                            cache_call("get_config", central=central), label
                        )
                    )
                elif route == "/api/dispatch":
                    if not isinstance(request, dict) or set(request) != {
                        "board_id",
                        "policy",
                    }:
                        raise ValueError("request must contain board_id and policy")
                    body = _json_bytes(
                        cache_call(
                            "save_dispatch",
                            request["board_id"],
                            request["policy"],
                            central=central,
                        )
                    )
                elif route == "/api/agents/retire":
                    if not isinstance(request, dict) or set(request) != {
                        "board_id", "agent_id"
                    }:
                        raise ValueError("request must contain board_id and agent_id")
                    body = _json_bytes(
                        cache_call(
                            "retire_agent",
                            request["board_id"],
                            request["agent_id"],
                            central=central,
                        )
                    )
                elif route == "/api/agents/retire-inert":
                    if not isinstance(request, dict) or set(request) != {"board_id"}:
                        raise ValueError("request must contain only board_id")
                    body = _json_bytes(
                        cache_call(
                            "retire_inert", request["board_id"], central=central
                        )
                    )
                elif route == "/api/attention":
                    if getattr(self, "_evidence_context", None) is None:
                        body = _json_bytes(seats.save_attention_state(request))
                    else:
                        before, result, after, error = (
                            seats.observe_attention_action(request)
                        )
                        self._evidence_before = before
                        self._evidence_after = after
                        if error is not None:
                            raise error
                        body = _json_bytes(result)
                elif route == "/api/human/resolve":
                    if not isinstance(request, dict):
                        raise ValueError("request must be an object")
                    human_required = {"board_id", "ticket_id", "request_id", "action"}
                    missing = human_required - set(request)
                    if missing:
                        raise ValueError(
                            "request must contain " + ", ".join(sorted(missing))
                        )
                    unexpected = (
                        set(request) - human_required - {"content", "disposition"}
                    )
                    if unexpected:
                        raise ValueError(
                            "unexpected fields: " + ", ".join(sorted(unexpected))
                        )
                    body = _json_bytes(
                        cache_call(
                            "resolve_human_request",
                            request["board_id"],
                            request,
                            central=central,
                        )
                    )
                elif route == "/api/butler/mark":
                    if not isinstance(request, dict) or set(request) != {
                        "board_id",
                        "ticket_id",
                        "question_id",
                        "mark",
                    }:
                        raise ValueError(
                            "request must contain board_id, ticket_id, question_id, and mark"
                        )
                    body = _json_bytes(
                        cache_call(
                            "mark_butler_draft",
                            request["board_id"],
                            {
                                "ticket_id": request["ticket_id"],
                                "question_id": request["question_id"],
                                "mark": request["mark"],
                            },
                            central=central,
                        )
                    )
                elif route == "/api/doors/copy":
                    if not isinstance(request, dict) or set(request) != {"board", "role"}:
                        raise ValueError("request must contain only board and role")
                    lock = (
                        project_operation_lock
                        if evidence_trace is not None
                        else nullcontext()
                    )
                    with lock:
                        body = _json_bytes(
                            cache_call(
                                "copy_door",
                                request["board"],
                                request["role"],
                                central=central,
                            )
                        )
                elif route == "/api/doors/rotate":
                    if not isinstance(request, dict) or set(request) != {"board", "role"}:
                        raise ValueError("request must contain only board and role")
                    lock = (
                        project_operation_lock
                        if evidence_trace is not None
                        else nullcontext()
                    )
                    with lock:
                        body = _json_bytes(
                            cache_call(
                                "rotate_door",
                                request["board"],
                                request["role"],
                                central=central,
                            )
                        )
                elif route == "/api/projects/add":
                    if not isinstance(request, dict):
                        raise ValueError("request must be an object")
                    req_fields = {"name", "board_id", "work_dir"}
                    opt_fields = {"integration_ref"}
                    if not req_fields.issubset(set(request)) or not set(request).issubset(
                        req_fields | opt_fields
                    ):
                        raise ValueError(
                            f"request must contain {', '.join(sorted(req_fields))} and optionally integration_ref"
                        )
                    with (
                        project_operation_lock
                        if evidence_trace is not None
                        else nullcontext()
                    ):
                        self._evidence_before = self._prepare_project_evidence(
                            request, central
                        )
                        try:
                            result = cache_call(
                                "add_project",
                                request["name"],
                                request["board_id"],
                                request["work_dir"],
                                request.get("integration_ref", "main"),
                                seats,
                                central=central,
                            )
                        except PermissionError:
                            if self._evidence_context is not None:
                                self._evidence_after = self._prepare_project_evidence(
                                    request, central
                                )
                                self._project_authorization_denied = (
                                    self._evidence_context is not None
                                    and self._evidence_after is not None
                                )
                            raise
                        except Exception:
                            if self._evidence_context is not None:
                                self._evidence_after = self._prepare_project_evidence(
                                    request, central
                                )
                            raise
                        if self._evidence_context is not None:
                            self._evidence_after = self._prepare_project_evidence(
                                request, central
                            )
                        body = _json_bytes(result)
                elif route == "/api/workers":
                    body = _json_bytes(
                        {
                            **workers.save(request, selected_central_url(central)),
                            "central": label,
                        }
                    )
                elif worker_action is not None:
                    if request != {}:
                        raise ValueError("worker action body must be an empty object")
                    name, action = worker_action.groups()
                    if action == "test":
                        result = workers.test_provider(name)
                    elif action == "start":
                        result = workers.start(
                            name, seat_exists=name in fleet_seats(central)
                        )
                    elif action == "restart":
                        result = workers.restart(
                            name, seat_exists=name in fleet_seats(central)
                        )
                    else:
                        result = workers.stop(name)
                    body = _json_bytes({**result, "central": label})
                elif route == "/api/config":
                    if not isinstance(request, dict) or set(request) != {
                        "config",
                        "expected_sha256",
                    }:
                        raise ValueError(
                            "request must contain only config and expected_sha256"
                        )
                    body = _json_bytes(
                        cache_call(
                            "save_config",
                            request["config"],
                            request["expected_sha256"],
                            central=central,
                        )
                    )
                else:
                    if not isinstance(request, dict):
                        raise ValueError("intake request must be an object")
                    if set(request) == {"board_id", "text"}:
                        body = _json_bytes(
                            cache_call(
                                "save_intake",
                                request["board_id"],
                                request["text"],
                                central=central,
                            )
                        )
                    elif set(request) in (
                        {"board_id", "ask_id", "action", "expected_sha256"},
                        {
                            "board_id",
                            "ask_id",
                            "action",
                            "expected_sha256",
                            "title",
                        },
                    ):
                        body = _json_bytes(
                            cache_call(
                                "decide_intake",
                                request["board_id"],
                                request["ask_id"],
                                request["action"],
                                request["expected_sha256"],
                                request.get("title"),
                                central=central,
                            )
                        )
                    else:
                        raise ValueError(
                            "intake request must be a new ask or approve/decline decision"
                        )
            except AddProjectPartialFailure as exc:
                self._send(
                    exc.status_code,
                    "application/json; charset=utf-8",
                    _json_bytes(
                        {
                            "error": "Add project could not complete.",
                            "completed_steps": exc.completed_steps,
                            "failed_step": exc.failed_step,
                            "central": label,
                        }
                    ),
                )
                return
            except KeyError:
                if route in config_routes:
                    self._send(
                        404,
                        "application/json; charset=utf-8",
                        b'{"error":"plan not found"}',
                    )
                    return
                if route == "/api/workers" or worker_action is not None:
                    self._send(
                        404,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": "worker not found", "central": label}),
                    )
                    return
                self._send(
                    409,
                    "application/json; charset=utf-8",
                    _json_bytes({"error": "KeyError", "central": label}),
                )
                return
            except PermissionError as exc:
                self._send(
                    403,
                    "application/json; charset=utf-8",
                    _json_bytes({"error": str(exc), "central": label}),
                )
                return
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._send(
                    400,
                    "application/json; charset=utf-8",
                    _json_bytes({"error": str(exc), "central": label}),
                )
                return
            except ConfigConflictError as exc:
                self._send(
                    409,
                    "application/json; charset=utf-8",
                    _json_bytes({"error": str(exc), "central": label}),
                )
                return
            except IntakeRateLimitError as exc:
                self._send(
                    429,
                    "application/json; charset=utf-8",
                    _json_bytes({"error": str(exc), "central": label}),
                )
                return
            except RuntimeError as exc:
                if route in {"/api/config", "/api/intake"}:
                    self._send(
                        409,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": type(exc).__name__, "central": label}),
                    )
                    return
                self._send(
                    501 if not workers.enabled else 409,
                    "application/json; charset=utf-8",
                    _json_bytes({"error": str(exc), "central": label}),
                )
                return
            except Exception as exc:  # noqa: BLE001 - stale CAS is a safe conflict.
                self._send(
                    409,
                    "application/json; charset=utf-8",
                    _json_bytes({"error": type(exc).__name__, "central": label}),
                )
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


def _read_mode_0600(path: Path, description: str) -> str:
    try:
        info = path.stat()
    except OSError as exc:
        raise SystemExit(f"cannot read {description}: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit(f"{description} must be a regular 0600 file: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"cannot read {description}: {path}") from exc


def load_central_configs(args: argparse.Namespace) -> list[Config]:
    """Load ordered multi-central config without exposing token material."""
    cli_keys_dir = (
        Path(args.doors_keys_dir).expanduser().resolve()
        if getattr(args, "doors_keys_dir", None)
        else None
    )
    cli_jwks = (
        Path(args.jwks_path).expanduser().resolve()
        if getattr(args, "jwks_path", None)
        else None
    )
    if not args.centrals:
        return [
            Config(
                url=args.url,
                token=_token_from_args(args.token_file),
                home_board=args.home_board,
                agent_name=args.agent_name,
                stale_seconds=args.stale_seconds,
                cache_seconds=args.cache_seconds,
                doors_keys_dir=cli_keys_dir,
                jwks_path=cli_jwks,
            )
        ]
    source = Path(args.centrals).expanduser().resolve()
    raw = _read_mode_0600(source, "centrals config")
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("centrals config is not valid JSON") from exc
    if not isinstance(entries, list) or not entries:
        raise SystemExit("centrals config must be a non-empty JSON list")
    configs: list[Config] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        required = {"label", "url", "token_path", "home_board"}
        allowed = required | {"stats_path", "doors_keys_dir", "jwks_path"}
        if (
            not isinstance(entry, dict)
            or not required <= set(entry)
            or not set(entry) <= allowed
        ):
            raise SystemExit(
                f"centrals entry {index} must contain label, url, token_path, "
                "and home_board, with optional stats_path, doors_keys_dir, and jwks_path"
            )
        label, url, token_path, home_board = (
            entry.get("label"),
            entry.get("url"),
            entry.get("token_path"),
            entry.get("home_board"),
        )
        if not isinstance(label, str) or not CENTRAL_LABEL_RE.fullmatch(label):
            raise SystemExit(f"centrals entry {index} has an invalid label")
        if label in seen:
            raise SystemExit(f"duplicate central label: {label}")
        if not isinstance(url, str) or not url.strip():
            raise SystemExit(f"centrals entry {index} has an invalid url")
        if not isinstance(home_board, str) or not BOARD_ID_RE.fullmatch(home_board):
            raise SystemExit(f"centrals entry {index} has an invalid home_board")
        if not isinstance(token_path, str) or not token_path:
            raise SystemExit(f"centrals entry {index} has an invalid token_path")
        raw_stats_path = entry.get("stats_path")
        if raw_stats_path is not None and (
            not isinstance(raw_stats_path, str) or not raw_stats_path
        ):
            raise SystemExit(f"centrals entry {index} has an invalid stats_path")
        raw_keys_dir = entry.get("doors_keys_dir")
        keys_dir = cli_keys_dir
        if raw_keys_dir is not None:
            if not isinstance(raw_keys_dir, str) or not raw_keys_dir:
                raise SystemExit(f"centrals entry {index} has an invalid doors_keys_dir")
            p = Path(raw_keys_dir).expanduser()
            if not p.is_absolute():
                p = source.parent / p
            keys_dir = p.resolve()
        raw_jwks = entry.get("jwks_path")
        jwks_path = cli_jwks
        if raw_jwks is not None:
            if not isinstance(raw_jwks, str) or not raw_jwks:
                raise SystemExit(f"centrals entry {index} has an invalid jwks_path")
            p = Path(raw_jwks).expanduser()
            if not p.is_absolute():
                p = source.parent / p
            jwks_path = p.resolve()
        token_file = Path(token_path).expanduser()
        if not token_file.is_absolute():
            token_file = source.parent / token_file
        token_file = token_file.resolve()
        token = _read_mode_0600(token_file, f"token file for central {label}").strip()
        if not token:
            raise SystemExit(f"token file for central {label} is empty")
        stats_path = None
        if raw_stats_path is not None:
            stats_path = Path(raw_stats_path).expanduser()
            if not stats_path.is_absolute():
                stats_path = source.parent / stats_path
            stats_path = stats_path.resolve()
        configs.append(
            Config(
                url=url.strip(),
                token=token,
                home_board=home_board,
                agent_name=args.agent_name,
                stale_seconds=args.stale_seconds,
                cache_seconds=args.cache_seconds,
                label=label,
                overhead_path=stats_path,
                doors_keys_dir=keys_dir,
                jwks_path=jwks_path,
            )
        )
        seen.add(label)
    return configs


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the loopback fleet dashboard")
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument(
        "--url", default=os.environ.get("ONBOARD_CENTRAL_URL", DEFAULT_URL)
    )
    parser.add_argument("--token-file")
    parser.add_argument(
        "--doors-keys-dir",
        default=os.environ.get("PURSERS_DOORS_KEYS_DIR"),
        help="Directory for door RSA private keys (default: $PURSERS_DOORS_KEYS_DIR)",
    )
    parser.add_argument(
        "--jwks-path",
        default=os.environ.get("PURSERS_JWKS_PATH"),
        help="Path to public JWKS file (default: $PURSERS_JWKS_PATH)",
    )
    parser.add_argument(
        "--centrals",
        help=(
            "0600 JSON list of {label,url,token_path,home_board[,stats_path]} entries"
        ),
    )
    parser.add_argument("--home-board", default=DEFAULT_HOME_BOARD)
    parser.add_argument(
        "--agent-name",
        default="fleet-dashboard-session-default",
        help="Reserved dashboard session identity (fleet-dashboard-session-*)",
    )
    parser.add_argument("--stale-seconds", type=int, default=300)
    parser.add_argument("--cache-seconds", type=float, default=5.0)
    parser.add_argument(
        "--workers-dir",
        default=str(_default_workers_dir()),
    )
    parser.add_argument(
        "--butler-secrets-dir",
        default=str(_default_butler_secrets_dir()),
        help="Private 0700 directory for write-only Board Butler keys",
    )
    parser.add_argument(
        "--seat-state-dir",
        default=str(_default_config_state_dir()),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--worker-script", default=str(DEFAULT_WORKER_SCRIPT), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--evidence-trace-config",
        help="Verifier-owned 0600 config for bounded Fleet evidence tracing",
    )
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        parser.error("--host must be 127.0.0.1; non-loopback binding is refused")
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    if args.stale_seconds < 1 or args.cache_seconds <= 0:
        parser.error("stale and cache intervals must be positive")
    if not DASHBOARD_AGENT_NAME_RE.fullmatch(args.agent_name):
        parser.error(
            "--agent-name must use the reserved fleet-dashboard-session-* namespace"
        )
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    configs = load_central_configs(args)
    cache = DashboardCache(
        [FleetFetcher(config) for config in configs], args.cache_seconds
    )
    worker_manager = WorkerManager(args.workers_dir, worker_script=args.worker_script)
    butler_manager = ButlerSettingsManager(args.butler_secrets_dir)
    seat_state_dir = Path(args.seat_state_dir).expanduser()
    seat_manager = SeatConfigManager(
        seat_state_dir / "seats.json", state_dir=seat_state_dir
    )
    try:
        trace = (
            EvidenceTrace.from_config(args.evidence_trace_config, Path(__file__))
            if args.evidence_trace_config
            else None
        )
    except EvidenceTraceConfigError as exc:
        raise SystemExit(f"invalid evidence trace config: {exc}") from exc
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(
            cache,
            bridge_stats_path(),
            worker_manager,
            seat_manager,
            evidence_trace=trace,
            butler_manager=butler_manager,
        ),
    )
    print(f"Fleet Dashboard: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        cache.close()


if __name__ == "__main__":
    main()
