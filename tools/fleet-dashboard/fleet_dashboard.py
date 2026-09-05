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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
from pursers_client import BoardClient, BoardClientError

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))
from seat_config import (  # noqa: I001
    BridgeInstaller,
    DesiredSeat,
    Doctor,
    PromptRenderer,
    SeatInventory,
    adapter_for,
    connector_skill_suggestions,
)


DEFAULT_URL = "https://127.0.0.1:8766/mcp"
DEFAULT_HOME_BOARD = "pursers"
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
MAX_FINDINGS = 50
MAX_FINDING_CHARS = 500
MAX_OVERHEAD_FILE_BYTES = 2_000_000
MAX_OVERHEAD_SEATS = 200
MAX_OVERHEAD_TOOLS = 5
OVERHEAD_DAYS = 7
ATTENTION_RETENTION_DAYS = 7
CONTEXT_ATTENTION_HOURS = 24
CONTEXT_STATS_ANOMALY_TOKENS = 1_000_000
WORKER_API_MAX_BYTES = 20_000
CONFIG_API_MAX_BYTES = 40_000
CONFIG_JOB_LIMIT = 100
CONFIG_PLAN_LIMIT = 50
CONFIG_STATE_DIR = Path("~/.pursers/fleet-dashboard")
MAX_REVIEW_STATE_BYTES = 4_096
REVIEW_STATE_SUFFIX = ".review-state.json"
WORKER_NAME_RE = re.compile(r"^[a-z0-9-]{2,32}$")
WORKER_KEYCHAIN_SERVICE = "pursers-worker"
WORKER_SECURITY_CLI = Path("/usr/bin/security")
WORKER_AUTH_SCHEME_PARTS = ("Bea", "rer")
DEFAULT_WORKERS_DIR = Path("~/.pursers/workers")
DEFAULT_WORKER_SCRIPT = (
    Path(__file__).resolve().parents[1] / "worker-runtime" / "pursers_worker.py"
)
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
BOARD_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
CENTRAL_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
ACTIVE_CLAIM_STATES = frozenset({"claimed", "in_progress", "creating_report"})
SUBMITTED_STATES = frozenset({"submitted", "reviewing", "in_review"})
TERMINAL_STATES = frozenset({"closed", "rejected", "canceled", "terminated"})
CONFIG_STATE_KEY = "coordinator_config"
INTAKE_STATE_KEY = "coordinator_intake"
FINDINGS_STATE_KEY = "coordinator_findings"
DASHBOARD_WRITE_KEYS = frozenset({CONFIG_STATE_KEY, INTAKE_STATE_KEY})
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


class ConfigConflictError(RuntimeError):
    """The dashboard form was based on missing or superseded state."""


class IntakeRateLimitError(RuntimeError):
    """The dashboard intake write rate exceeded its bounded hourly window."""


class FleetClient(Protocol):
    async def board_status(self) -> dict[str, Any]: ...

    async def board_dispatch_events(self, *, limit: int = 25) -> dict[str, Any]: ...

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

    async def ticket_list(
        self, *, include_closed: bool = False, limit: int = 100
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
    if key not in DASHBOARD_WRITE_KEYS:
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
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "thresholds",
        "integration_watch_since",
        "intake",
    }:
        raise ValueError("config must contain only the coordinator schema fields")
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
    return json.loads(json.dumps(value))


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
        root: str | Path = DEFAULT_WORKERS_DIR,
        *,
        worker_script: str | Path = DEFAULT_WORKER_SCRIPT,
        platform: str | None = None,
        command_runner: Callable[..., Any] = subprocess.run,
        process_factory: Callable[..., Any] = subprocess.Popen,
        process_matches: Callable[[int, Path], bool] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.worker_script = Path(worker_script).expanduser().resolve()
        self.platform = sys.platform if platform is None else platform
        self.command_runner = command_runner
        self.process_factory = process_factory
        self.process_matches = process_matches or self._default_process_matches
        self._children: dict[str, Any] = {}
        self._lock = threading.RLock()
        if self.platform == "darwin":
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            self.adopt_orphans()

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
        try:
            result = self.command_runner(
                ["/bin/ps", "-p", str(pid), "-o", "command="],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
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


def bridge_stats_path() -> Path:
    configured = os.environ.get("PURSERS_BRIDGE_STATS", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path(__file__).resolve().parents[1] / "wait-bridge" / "bridge-stats.json"
    )


def _nonnegative_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


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
        text = finding.get("message") or finding.get("summary") or finding.get("detail")
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
        items.append(
            {
                "kind": kind,
                "level": level,
                "text": _clip(text, MAX_FINDING_CHARS),
                "ticket_id": _clip(finding.get("ticket_id"), MAX_LABEL_CHARS) or None,
                "ask_id": _clip(finding.get("ask_id"), 120) or None,
                "draft": draft_preview,
            }
        )
    return {
        "items": items,
        "truncated_count": reported_truncated + max(0, len(findings) - MAX_FINDINGS),
    }


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
            and isinstance(board_id, str)
            and board_id
            and board_id not in seen
        ):
            boards.append((_clip(name, MAX_LABEL_CHARS), board_id))
            seen.add(board_id)
        if len(boards) >= MAX_BOARDS:
            break
    return boards


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
        work_dir = project.get("work_dir")
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


def _detail_ticket(ticket: dict[str, Any]) -> dict[str, Any]:
    required = ticket.get("required_fields")
    if not isinstance(required, list):
        required = []
    submissions = ticket.get("submission_history")
    latest_submission = (
        submissions[-1] if isinstance(submissions, list) and submissions else {}
    )
    if not isinstance(latest_submission, dict):
        latest_submission = {}
    return {
        "id": _clip(ticket.get("ticket_id"), MAX_LABEL_CHARS),
        "title": _clip(ticket.get("title") or "(untitled)", MAX_TITLE_CHARS),
        "status": _clip(_ticket_status_label(ticket, datetime.now(timezone.utc)), 64),
        "priority": _clip(ticket.get("priority") or "medium", 16),
        "abandoned_count": max(0, int(ticket.get("abandoned_count", 0) or 0)),
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
        "review_label": _clip(ticket.get("review_label"), MAX_LABEL_CHARS) or None,
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
    tickets = [
        _detail_ticket(item) for item in source_tickets if isinstance(item, dict)
    ]
    status_rank = {
        **{status: 0 for status in ACTIVE_CLAIM_STATES},
        **{status: 1 for status in SUBMITTED_STATES},
        "open": 2,
    }
    tickets.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    tickets.sort(key=lambda item: status_rank.get(item["status"], 3))

    source_events = raw.get("events") if isinstance(raw.get("events"), list) else []
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
        "routes": routes,
        "coordinator_findings": project_coordinator_findings(snapshot),
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
                },
            )
            group["boards"].add(board_id)
            if isinstance(agent_id, str) and agent_id:
                group["agent_ids_by_board"].setdefault(board_id, set()).add(agent_id)
            current = current_by_agent.get(str(agent_id or ""))
            seat_projection = {
                "board_id": board_id,
                "project": label,
                "role": _clip(agent.get("membership_role") or agent.get("role"), 32)
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
            if "capabilities" in agent:
                seat_projection["capabilities"] = (
                    agent.get("capabilities")
                    if isinstance(agent.get("capabilities"), dict)
                    else {}
                )
            if "current_offer" in agent:
                seat_projection["current_offer"] = (
                    agent.get("current_offer")
                    if isinstance(agent.get("current_offer"), dict)
                    else None
                )
            group["seats"][board_id] = seat_projection
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
                        "status_label": _clip(_ticket_status_label(ticket, now), 64),
                        "claimed_by": _clip(claimed_by, MAX_LABEL_CHARS) or None,
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
                "coordinator_heartbeat": (
                    coordinator_seen_at.isoformat() if coordinator_seen_at else None
                ),
                "coordinator_findings": project_coordinator_findings(snapshot),
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
                "duplicate_name": len(names_to_groups.get(group["agent_name"], set()))
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
        self._intake_submissions: dict[str, list[tuple[str, datetime]]] = {}
        self._board_work_dirs: dict[str, str | None] = {}

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
        self._board_work_dirs = parse_project_work_dirs(
            registry, self.config.home_board
        )
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
        work_dir: str | None = None,
    ) -> dict[str, Any]:
        try:
            async with self._client(board_id) as client:
                snapshot = await client.board_snapshot(
                    limit=SNAPSHOT_LIMIT, max_bytes=SNAPSHOT_MAX_BYTES
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
                events = await self._board_event_feed(
                    client,
                    int(snapshot.get("latest_seq", 0)),
                    event_limit,
                )
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
            return {
                "label": label,
                "board_id": board_id,
                "snapshot": snapshot,
                "events": events,
                "event_window_truncated": latest_seq > event_limit,
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
        return aggregate_fleet(rows, stale_seconds=self.config.stale_seconds)

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

    async def fetch_dispatch(self, board_id: str) -> dict[str, Any]:
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        active = {active_board for _label, active_board in await self._boards()}
        if board_id not in active:
            raise ValueError("board_id is not registry-active")
        async with self._client(board_id) as client:
            status = await client.board_status()
            listed = await client.ticket_list(
                include_closed=False, limit=DISPATCH_TICKET_LIMIT
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
            "offers": offers,
            "timeline": timeline,
        }

    async def save_dispatch(self, board_id: str, payload: Any) -> dict[str, Any]:
        if not BOARD_ID_RE.fullmatch(board_id):
            raise ValueError("invalid board_id")
        if not isinstance(payload, dict) or set(payload) != {
            "claim_ttl_s",
            "offer_ttl_s",
            "second_opinion",
            "fallback_broadcast",
        }:
            raise ValueError("dispatch policy fields are invalid")
        offer_ttl = payload["offer_ttl_s"]
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
                second_opinion=payload["second_opinion"],
                fallback_broadcast=payload["fallback_broadcast"],
            )
            ttl_result = await client.board_claim_ttl_set(claim_ttl)
        return {
            **policy_result,
            "claim_ttl_s": ttl_result.get("claim_ttl_s", claim_ttl),
            "previous_claim_ttl_s": ttl_result.get("previous_claim_ttl_s"),
        }

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


class SeatConfigManager:
    """Secret-safe orchestration layer for the dashboard Config page."""

    def __init__(
        self,
        inventory_path: str | Path | None = None,
        *,
        state_dir: str | Path = CONFIG_STATE_DIR,
        bridge_installer: BridgeInstaller | None = None,
        doctor_factory: Callable[[], Doctor] = Doctor,
        latest_version: Callable[[], str | None] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.inventory = SeatInventory(inventory_path or self.state_dir / "seats.json")
        self.bridge_installer = bridge_installer or BridgeInstaller()
        self.doctor_factory = doctor_factory
        self.latest_version = latest_version or self._pypi_latest
        self._plans: dict[str, tuple[DesiredSeat, list[Any]]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

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
    def _clean_text(value: str) -> str:
        value = re.sub(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
            "[REDACTED JWT]",
            value,
        )
        sensitive = re.compile(
            r"(?im)^(\s*[^:=\n]*(?:token|authorization|secret|password|api[_-]?key|bearer)[^:=\n]*)(\s*[:=]\s*)(.*)$"
        )

        def redact(match: re.Match[str]) -> str:
            key = match.group(1).lower()
            if "file" in key or "path" in key or "env_var" in key:
                return match.group(0)
            return f"{match.group(1)}{match.group(2)}[REDACTED]"

        value = sensitive.sub(redact, value)
        return value

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

    def attention_state(self) -> dict[str, Any]:
        path = self.state_dir / "attention-state.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            value = {}
        return {"items": value if isinstance(value, dict) else {}}

    def save_attention_state(self, value: Any) -> dict[str, Any]:
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
                }
            )
        discovered = []
        for host, path in (
            ("codex", Path("~/.codex/config.toml").expanduser()),
            ("goose", Path("~/.config/goose/config.yaml").expanduser()),
            (
                "claude-desktop",
                Path(
                    "~/Library/Application Support/Claude/claude_desktop_config.json"
                ).expanduser(),
            ),
        ):
            if path.is_file() and not any(
                row.get("config_path") == str(path) for row in rows
            ):
                discovered.append({"host": host, "config_path": str(path)})
        return {"schema_version": 1, "seats": rows, "discovered_configs": discovered}

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

    def registry(self, fleet: dict[str, Any]) -> dict[str, Any]:
        names = {row.get("name") for row in self.seats()["seats"]}
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
        return {"boards": boards, "seats": live_seats, "read_only": True}

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

    def doctor(self, names: Any = None) -> dict[str, str]:
        if names is not None and (
            not isinstance(names, list) or not all(isinstance(v, str) for v in names)
        ):
            raise ValueError("names must be a list")

        def run() -> dict[str, Any]:
            records = self.inventory.load()["seats"]
            if names is not None:
                records = [row for row in records if row.get("name") in names]
            reports = []
            for record in records:
                desired = self._desired(record)
                report = self._report(self.doctor_factory().run(desired))
                self.inventory.upsert(
                    desired, bridge_version=self.bridge_installer.version, doctor=report
                )
                reports.append({"seat": desired.name, **report})
            return {"seats": reports}

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
        self.fetchers: dict[str, FleetFetcher] = {}
        for item in fetchers:
            label = getattr(getattr(item, "config", None), "label", "default")
            if label in self.fetchers:
                raise ValueError(f"duplicate central label: {label}")
            self.fetchers[label] = item
        self.default_central = next(iter(self.fetchers))
        # Preserve these public attributes for single-central callers/tests.
        self.fetcher = self.fetchers[self.default_central]
        self.ttl_seconds = ttl_seconds
        self.fleet = TimedCache(ttl_seconds, self.fetcher.fetch)
        self._fleets = {
            label: self.fleet
            if label == self.default_central
            else TimedCache(ttl_seconds, item.fetch)
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
        return self._labeled(asyncio.run(self.fetchers[label].fetch_config()), label)

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
            asyncio.run(self.fetchers[label].fetch_intake(board_id)), label
        )

    def get_dispatch(
        self, board_id: str, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            asyncio.run(self.fetchers[label].fetch_dispatch(board_id)), label
        )

    def save_dispatch(
        self, board_id: str, value: Any, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            asyncio.run(self.fetchers[label].save_dispatch(board_id, value)), label
        )

    def save_config(
        self,
        value: Any,
        expected_sha256: str | None,
        central: str | None = None,
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            asyncio.run(self.fetchers[label].save_config(value, expected_sha256)),
            label,
        )

    def save_intake(
        self, board_id: Any, text: Any, central: str | None = None
    ) -> dict[str, Any]:
        label = self.resolve_central(central)
        return self._labeled(
            asyncio.run(self.fetchers[label].save_intake(board_id, text)), label
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
            asyncio.run(
                self.fetchers[label].decide_intake(
                    board_id, ask_id, action, expected_sha256, title
                )
            ),
            label,
        )


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleet Dashboard</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151b2d;--panel2:#202942;--line:#29324a;--text:#e7ecf7;--muted:#9aa6bf;--good:#46d39a;--warn:#f4bd55;--bad:#ef6f7d;--accent:#79a8ff;--cell-y:8px;--card-pad:14px;--main-pad:24px}:root[data-theme="light"]{color-scheme:light;--bg:#f5f7fb;--panel:#fff;--panel2:#e9eef7;--line:#ccd4e2;--text:#182033;--muted:#5f6c82;--good:#167a55;--warn:#8b5b00;--bad:#b42332;--accent:#245fcc}:root[data-density="compact"]{--cell-y:4px;--card-pad:10px;--main-pad:16px}*{box-sizing:border-box}html,body{max-width:100%;overflow-x:hidden}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif}main{width:100%;max-width:1500px;min-width:0;margin:auto;padding:var(--main-pad)}.top,.toolbar{display:flex;justify-content:space-between;flex-wrap:wrap;gap:12px}.top{align-items:end}h1,h2,h3,p{margin:0}h1{font-size:24px}h2{font-size:17px}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.muted,.meta{color:var(--muted)}.strip{display:grid;grid-template-columns:repeat(4,minmax(100px,1fr));gap:10px;margin:20px 0}.metric,.card{background:var(--panel);border:1px solid var(--line);border-radius:12px}.metric,.card{padding:var(--card-pad)}.metric b{display:block;font-size:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(390px,100%),1fr));gap:14px}.card{min-width:0}.board-link{display:block;color:inherit}.counts,.tabs,.required,.view-controls{display:flex;flex-wrap:wrap;gap:8px}.counts{margin:12px 0}.pill,.tab{padding:4px 8px;border-radius:999px;background:var(--panel2)}.tab.active{outline:2px solid var(--accent)}.table-scroll{width:100%;max-width:100%;overflow-x:auto}table{width:100%;border-collapse:collapse}th,td{padding:var(--cell-y) 6px;text-align:left;border-top:1px solid var(--line);vertical-align:top}tbody tr:focus{outline:2px solid var(--accent);outline-offset:-2px}th{color:var(--muted);font-weight:500}.id{font-family:ui-monospace,SFMono-Regular,monospace;color:var(--accent);white-space:nowrap}.status{font-size:12px;border-radius:999px;padding:2px 6px;background:var(--panel2)}.pool{margin-top:18px}.warning,.pressure-watch{color:var(--warn)}.error,.pressure-compact{color:var(--bad)}.pressure-ok{color:var(--good)}#state{font-size:12px}.empty{color:var(--muted);padding:10px 0}.agent{border-top:1px solid var(--line)}.agent summary{cursor:pointer;display:grid;grid-template-columns:2fr 1fr 2fr 2fr;gap:8px;padding:10px 6px}.agent-body{padding:0 6px 12px}.toolbar{align-items:center;margin:18px 0}.toolbar select,.toolbar input,.toolbar button,#filter,.view-controls button{background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px}.search-wrap{position:relative}.search{min-width:min(340px,48vw)}.search-results{position:absolute;z-index:20;right:0;width:min(560px,90vw);max-height:60vh;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:0 12px 30px #0006;padding:8px}.search-results h3{padding:7px 8px;color:var(--muted);font-size:12px;text-transform:uppercase}.search-result{display:block;padding:8px;border-radius:7px;color:var(--text)}.search-result[aria-selected="true"],.search-result:hover{background:var(--panel2);text-decoration:none}.search-result .meta{display:block}.connection-banner{position:sticky;top:0;z-index:15;margin:0 0 12px;padding:7px 10px;border:1px solid var(--warn);border-radius:8px;background:var(--panel)}.ticket-detail summary,.timeline summary{cursor:pointer}.ticket-copy{white-space:pre-wrap;overflow-wrap:anywhere;max-width:80ch;margin:8px 0}.back{display:inline-block;margin-bottom:16px}.required{margin-top:8px}.finding-list,.timeline,.flow{display:grid;gap:10px;margin-top:10px}.finding{border-left:3px solid var(--line);padding-left:10px}.timeline-ticket{margin:8px 0 0 14px}.flow{grid-template-columns:repeat(4,minmax(0,1fr))}.flow-column{background:var(--panel2);border-radius:10px;padding:10px;min-width:0}.flow-card{display:block;margin-top:8px;padding:9px;border:1px solid var(--line);border-radius:8px;color:var(--text);overflow-wrap:anywhere}.change-grid{grid-template-columns:repeat(5,minmax(100px,1fr))}.bounded-note{margin-top:10px}.overhead-tools{max-width:36ch}dialog{max-width:min(560px,90vw);background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:12px}dialog::backdrop{background:#0009}.shortcut-grid{display:grid;grid-template-columns:auto 1fr;gap:8px 16px;margin:16px 0}.shortcut-grid dt{font-family:ui-monospace,SFMono-Regular,monospace}@media(max-width:800px){main{padding:14px}.strip,.change-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.grid,.flow{grid-template-columns:1fr}.hide-small{display:none}.agent summary{grid-template-columns:1fr 1fr}.agent summary span:nth-child(n+3){display:none}.search{min-width:0;width:100%}.top{align-items:start}}@media print{:root,:root[data-theme]{color-scheme:light;--bg:#fff;--panel:#fff;--panel2:#eee;--line:#bbb;--text:#111;--muted:#555;--accent:#174ea6}.view-controls,.search-wrap,.connection-banner,dialog{display:none!important}}
</style></head><body><main><div id="connection-banner" class="connection-banner" role="status" hidden></div><div class="top"><div><h1>Fleet Dashboard</h1><p class="muted">Live boards and shared agent pool</p></div><div><div class="view-controls"><button id="theme-toggle" type="button">Light theme</button><button id="density-toggle" type="button">Compact density</button><button id="help-toggle" type="button" aria-label="Keyboard help">?</button></div><div class="search-wrap"><input id="filter" class="search" type="search" placeholder="Search boards, tickets, agents…" aria-label="Global dashboard search" autocomplete="off" aria-controls="search-results" aria-expanded="false"><div id="search-results" class="search-results" role="listbox" hidden></div></div><div id="state" class="muted">Loading…</div></div></div><section id="home-view"><div id="central-sections"></div></section><section id="detail-view" hidden></section><section id="config-view" hidden></section><dialog id="help-overlay"><h2>Keyboard shortcuts</h2><dl class="shortcut-grid"><dt>/</dt><dd>Focus global search</dd><dt>g then f</dt><dd>Fleet overview</dd><dt>g then o</dt><dd>Protocol overhead</dd><dt>g then c</dt><dd>Coordinator config</dd><dt>↑ / ↓</dt><dd>Move within tables or search results</dd><dt>Enter</dt><dd>Open the selected search result</dd><dt>?</dt><dd>Toggle this help</dd><dt>Esc</dt><dd>Close search or help</dd></dl><button id="help-close" type="button">Close</button></dialog></main><script>
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const fmt=v=>v?new Date(v).toLocaleString():'—',centralHref=(central,view)=>`#/central/${encodeURIComponent(central)}/${view}`,boardHref=(central,id,view='tickets')=>`${centralHref(central,`board/${encodeURIComponent(id)}/${view}`)}`,ticketHref=(central,board,id)=>`${boardHref(central,board)}?ticket=${encodeURIComponent(id)}`,apiCentral=central=>`central=${encodeURIComponent(central)}`,matches=(values,needle)=>!needle||values.map(v=>String(v??'').toLocaleLowerCase()).join(' ').includes(needle);
const ticketMatches=(t,needle)=>matches([t.id,t.title,t.status,t.claimed_by,t.description],needle);
const filterHomeBoards=(boards,needle)=>boards.map(b=>({...b,tickets:b.tickets.filter(t=>ticketMatches(t,needle))})).filter(b=>matches([b.label,b.board_id],needle)||b.tickets.length);
const eventMatches=(e,ticketId,needle)=>matches([ticketId,e.kind,e.status_from,e.status_to,e.actor],needle);
const filterChangeEvents=(events,needle)=>events.filter(e=>eventMatches(e,e.ticket_id,needle));
let fleetData={},fleetErrors={},centralLabels=[],defaultCentral='default',detailData=null,detailSort='newest',detailTimer=null,filterNeedle='',searchItems=[],searchSelection=0,theme='dark',density='comfortable',goPrefix=false,goTimer=null,lastSuccessAt=null,showStaleAgents=false;
const sectionStates=new Map(),connectionFailures=new Set();
const agentStatusRank={busy:0,available:1,stale:2};
function compareAgents(a,b){return(agentStatusRank[a.pool_status]??3)-(agentStatusRank[b.pool_status]??3)||String(a.agent_name||'').localeCompare(String(b.agent_name||''))}
function visibleAgents(agents){return agents.filter(a=>showStaleAgents||a.pool_status==='busy'||a.pool_status==='available').sort(compareAgents)}
function relativeAge(value,now=Date.now()){if(!value)return'—';const then=new Date(value).getTime();if(!Number.isFinite(then))return'—';const seconds=Math.max(0,Math.floor((now-then)/1000));if(seconds<60)return`${seconds}s ago`;const minutes=Math.floor(seconds/60);if(minutes<60)return`${minutes}m ago`;const hours=Math.floor(minutes/60);if(hours<24)return`${hours}h ago`;return`${Math.floor(hours/24)}d ago`}
function clippedAgentTitle(value,max=48){const chars=Array.from(String(value||'(untitled)'));return chars.length<=max?chars.join(''):`${chars.slice(0,max-1).join('')}…`}
function agentLiveWork(a){return(a.seats||[]).filter(seat=>seat.current_ticket_id)}
function agentTicketLink(central,seat){const title=String(seat.current_ticket_title||seat.ticket_title||seat.current_ticket_id||seat.ticket_id||'(untitled)'),id=seat.current_ticket_id||seat.ticket_id;return`<a class="agent-ticket" href="${ticketHref(central,seat.board_id,id)}" title="${esc(title)}"><span class="id">${esc(id)}</span> · ${esc(clippedAgentTitle(title))}</a>`}
function agentStateSummary(central,a){const work=agentLiveWork(a);return`<span class="agent-live"><span class="status">${esc(a.pool_status)}</span>${work.length?work.map(seat=>agentTicketLink(central,seat)).join(''):'<span class="meta">ว่าง/idle</span>'}</span>`}
function agentVisibilityToggle(){return`<button class="button" type="button" data-agent-visibility-toggle aria-pressed="${showStaleAgents}">${showStaleAgents?'Show active only':'Show stale'}</button>`}
function groupSearchResults(data,detail,needle){const groups={Boards:[],Tickets:[],Agents:[]},seenTickets=new Set(),q=String(needle||'').trim().toLocaleLowerCase();if(!q)return groups;for(const [central,d] of Object.entries(data)){for(const b of d.boards||[]){if(matches([central,b.label,b.board_id],q))groups.Boards.push({label:b.label,meta:`${central} · ${b.board_id}`,href:boardHref(central,b.board_id)});for(const t of b.tickets||[]){if(ticketMatches(t,q)){const key=`${central}/${b.board_id}/${t.id}`;seenTickets.add(key);groups.Tickets.push({label:t.title||t.id,meta:`${t.id} · ${central} · ${b.board_id}`,href:ticketHref(central,b.board_id,t.id)})}}}for(const a of d.agents||[])if(matches([central,a.agent_name,a.pool_status,...(a.boards||[])],q)){const key=`agent:${central}:${a.agent_name}`;groups.Agents.push({label:a.agent_name,meta:`${central} · ${a.pool_status} · ${(a.boards||[]).join(', ')}`,href:'#/',stateKey:key})}}if(detail?.central&&detail?.board?.board_id)for(const t of detail.tickets||[]){const key=`${detail.central}/${detail.board.board_id}/${t.id}`;if(!seenTickets.has(key)&&ticketMatches(t,q))groups.Tickets.push({label:t.title||t.id,meta:`${t.id} · ${detail.central} · ${detail.board.board_id}`,href:ticketHref(detail.central,detail.board.board_id,t.id)})}return groups}
function renderSearchResults(){const host=document.querySelector('#search-results'),input=document.querySelector('#filter'),groups=groupSearchResults(fleetData,detailData,filterNeedle);searchItems=Object.values(groups).flat();searchSelection=Math.min(searchSelection,Math.max(0,searchItems.length-1));host.innerHTML=Object.entries(groups).filter(([,items])=>items.length).map(([name,items])=>`<section><h3>${esc(name)}</h3>${items.map(item=>{const index=searchItems.indexOf(item);return `<a class="search-result" role="option" aria-selected="${index===searchSelection}" data-search-index="${index}" href="${esc(item.href)}"><b>${esc(item.label)}</b><span class="meta">${esc(item.meta)}</span></a>`}).join('')}</section>`).join('')||(filterNeedle?'<p class="empty">No results</p>':'');host.hidden=!filterNeedle;input.setAttribute('aria-expanded',String(!host.hidden))}
function jumpSearchResult(index){const item=searchItems[index];if(!item)return;document.querySelector('#search-results').hidden=true;if(item.stateKey){location.hash=item.href;sectionStates.set(item.stateKey,true);setTimeout(()=>{renderFleet();const target=[...document.querySelectorAll('details[data-state-key]')].find(x=>x.dataset.stateKey===item.stateKey);target?.scrollIntoView({block:'center'});target?.focus()},0)}else location.hash=item.href}
function bindInteractive(root=document){for(const item of root.querySelectorAll('details[data-state-key]')){const key=item.dataset.stateKey;if(sectionStates.has(key))item.open=sectionStates.get(key);item.addEventListener('toggle',()=>sectionStates.set(key,item.open))}for(const table of root.querySelectorAll('table')){const rows=[...table.querySelectorAll('tbody tr')];rows.forEach((row,index)=>{row.tabIndex=index? -1:0;row.addEventListener('keydown',event=>{if(!['ArrowUp','ArrowDown'].includes(event.key))return;event.preventDefault();const next=Math.max(0,Math.min(rows.length-1,index+(event.key==='ArrowDown'?1:-1)));rows[next]?.focus()})})}}
const connectionBannerText=lastSuccess=>`reconnecting… last success ${lastSuccess?new Date(lastSuccess).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}):'never'}`;
function updateConnectionState(){const banner=document.querySelector('#connection-banner');banner.hidden=!connectionFailures.size;banner.textContent=connectionFailures.size?connectionBannerText(lastSuccessAt):''}
function markConnectionSuccess(key,at=new Date()){connectionFailures.delete(key);lastSuccessAt=at;updateConnectionState()}
function markConnectionFailure(key){connectionFailures.add(key);updateConnectionState()}
function applyPreferences(){document.documentElement.dataset.theme=theme;document.documentElement.dataset.density=density;document.querySelector('#theme-toggle').textContent=theme==='dark'?'Light theme':'Dark theme';document.querySelector('#density-toggle').textContent=density==='comfortable'?'Compact density':'Comfortable density'}
function route(){let m=location.hash.match(/^#\/central\/([^/?]+)\/(board\/([^/?]+)(?:\/(tickets|timeline|changes|flow|routes))?|overhead|config)(?:\?(.*))?$/);try{if(m){const central=decodeURIComponent(m[1]);if(!/^[A-Za-z0-9._-]{1,80}$/.test(central))return null;if(m[2]==='overhead'||m[2]==='config')return{central,kind:m[2]};const board=decodeURIComponent(m[3]);if(!/^[A-Za-z0-9._-]{1,80}$/.test(board))return null;const q=new URLSearchParams(m[5]||'');return{central,kind:'board',board,view:m[4]||'tickets',ticket:q.get('ticket'),since:q.get('since')}}m=location.hash.match(/^#\/board\/([^/?]+)(?:\/(tickets|timeline|changes|flow|routes))?(?:\?(.*))?$/);if(m){const board=decodeURIComponent(m[1]),q=new URLSearchParams(m[3]||'');return/^[A-Za-z0-9._-]{1,80}$/.test(board)?{central:defaultCentral,kind:'board',board,view:m[2]||'tickets',ticket:q.get('ticket'),since:q.get('since')}:null}if(location.hash==='#/config')return{central:defaultCentral,kind:'config'};return null}catch{return null}}
function renderCentral(d){const central=d.central,s=d.pool_summary,boards=filterHomeBoards(d.boards,filterNeedle),agents=d.agents.filter(a=>matches([a.agent_name,a.pool_status,...a.boards],filterNeedle));return `<section class="central-group" data-central="${esc(central)}"><div class="top central-heading"><div><h2>${esc(central)}</h2><p class="muted">Independent central trust domain</p></div><nav class="tabs"><a class="tab" href="${centralHref(central,'overhead')}">Overhead</a><a class="tab" href="${centralHref(central,'config')}">Config</a></nav></div><section class="strip">${['online','busy','available','stale'].map(k=>`<div class="metric"><span>${esc(k)}</span><b>${esc(s[k])}</b></div>`).join('')}</section><section class="grid">${boards.map(b=>`<article class="card"><a class="board-link" href="${boardHref(central,b.board_id)}"><div class="top"><div><h2>${esc(b.label)}</h2><span class="meta">${esc(b.board_id)}</span></div>${b.truncated?'<span class="status">bounded view</span>':''}</div></a>${b.error?`<p class="error">Unavailable: ${esc(b.error)}</p>`:`<div class="counts">${Object.entries(b.counts).map(([k,v])=>`<span class="pill">${esc(k.replace('_',' '))}: <b>${esc(v)}</b></span>`).join('')}</div><div class="table-scroll"><table><thead><tr><th>Ticket</th><th>Title</th><th>Status</th><th class="hide-small">Claimed by</th></tr></thead><tbody>${b.tickets.length?b.tickets.map(t=>`<tr><td><a class="id" href="${ticketHref(central,b.board_id,t.id)}">${esc(t.id)}</a></td><td>${esc(t.title)}</td><td><span class="status">${esc(t.status_label||t.status)}</span></td><td class="hide-small">${esc(t.claimed_by||'—')}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No matching tickets</td></tr>'}</tbody></table></div>`}</article>`).join('')||'<p class="empty">No boards match the filter.</p>'}</section><section class="card pool"><h2>Agent pool · ${esc(central)}</h2>${agents.length?agents.map(a=>`<details class="agent" data-state-key="${esc(`agent:${central}:${a.agent_name}`)}"><summary><b>${esc(a.agent_name)}${a.duplicate_name?' <span class="warning">duplicate name</span>':''}</b><span>${esc(a.pool_status)}</span><span>${esc(a.boards.join(', '))}</span><span>${esc(fmt(a.last_seen))}</span></summary><div class="agent-body table-scroll"><table><thead><tr><th>Project</th><th>Role</th><th>Current claim</th><th>Last seen</th></tr></thead><tbody>${a.seats.map(seat=>`<tr><td><a href="${boardHref(central,seat.board_id)}">${esc(seat.project)}</a><div class="meta">${esc(seat.board_id)}</div></td><td>${esc(seat.role||'—')}</td><td>${seat.current_ticket_id?`<a class="id" href="${ticketHref(central,seat.board_id,seat.current_ticket_id)}">${esc(seat.current_ticket_id)}</a><div>${esc(seat.current_ticket_title)}</div>`:'—'}</td><td>${esc(fmt(seat.last_seen))}</td></tr>`).join('')}</tbody></table></div></details>`).join(''):'<p class="empty">No agents match the filter.</p>'}</section></section>`}
function renderFleet(){document.querySelector('#central-sections').innerHTML=centralLabels.map(label=>fleetData[label]?renderCentral(fleetData[label]):`<section class="central-group unavailable" data-central="${esc(label)}"><div class="top central-heading"><div><h2>${esc(label)}</h2><p class="error">Unavailable: ${esc(fleetErrors[label]||'loading')}</p></div><nav class="tabs"><a class="tab" href="${centralHref(label,'overhead')}">Overhead</a><a class="tab" href="${centralHref(label,'config')}">Config</a></nav></div></section>`).join('');const newest=Object.values(fleetData).map(d=>d.generated_at).sort().at(-1);document.querySelector('#state').textContent=newest?`Updated ${fmt(newest)}`:'No central available';if(typeof bindInteractive==='function')bindInteractive(document.querySelector('#central-sections'));if(typeof renderSearchResults==='function')renderSearchResults()}
const legacyRenderCentralAgentsV1=renderCentral;
renderCentral=function(d){const base=legacyRenderCentralAgentsV1({...d,agents:[]}),marker='<section class="card pool"><h2>Agent pool · ',start=base.lastIndexOf(marker);if(start<0)return base;const central=d.central,agents=visibleAgents((d.agents||[]).filter(a=>matches([a.agent_name,a.pool_status,...(a.boards||[])],filterNeedle))),pool=`<section class="card pool"><div class="section-title"><h2>Agent pool · ${esc(central)}</h2>${agentVisibilityToggle()}</div>${agents.length?agents.map(a=>`<details class="agent" data-state-key="${esc(`agent:${central}:${a.agent_name}`)}"><summary><b>${esc(a.agent_name)}${a.duplicate_name?' <span class="warning">duplicate name</span>':''}</b>${agentStateSummary(central,a)}<span>${esc((a.boards||[]).join(', '))}</span><span class="meta">${esc(relativeAge(a.last_seen))}</span></summary><div class="agent-body table-scroll"><table><thead><tr><th>Project</th><th>Role</th><th>Current claim</th><th>Last seen</th></tr></thead><tbody>${(a.seats||[]).map(seat=>`<tr><td><a href="${boardHref(central,seat.board_id)}">${esc(seat.project)}</a><div class="meta">${esc(seat.board_id)}</div></td><td>${esc(seat.role||'—')}</td><td>${seat.current_ticket_id?agentTicketLink(central,seat):'<span class="meta">ว่าง/idle</span>'}</td><td>${esc(relativeAge(seat.last_seen))}</td></tr>`).join('')}</tbody></table></div></details>`).join(''):`<p class="empty">${showStaleAgents?'No agents match the filter.':'No active agents match the filter.'}</p>`}</section>`;return`${base.slice(0,start)}${pool}</section>`}
function pressureBadge(s){const label=s.pressure==='compact'?'COMPACT':s.pressure;return `<span class="status pressure-${esc(s.pressure)}" title="${esc(s.next_action)}">${esc(label)}</span>`}
function renderOverhead(d){const sessions=d.sessions||[],model=d.model_wait||[],cumulative=d.seats||[];document.querySelector('#detail-view').innerHTML=`<a class="back" href="#/">← All centrals</a><div class="top"><div><h2>Session context pressure · ${esc(d.central)}</h2><p class="muted">Model-visible wait cost is separated from bridge-to-Central diagnostics.</p></div></div><section class="card pool"><h3>Model-visible a2a_wait cost</h3>${model.length?`<div class="table-scroll"><table aria-label="Model-visible wait cost"><thead><tr><th>Seat</th><th>Returns this hour</th><th>Context this hour</th><th>24-hour outcomes</th></tr></thead><tbody>${model.map(s=>`<tr><td><b>${esc(s.agent_name)}</b><div class="meta">${esc(s.board_id)}</div></td><td>${esc(s.returns_per_hour)}</td><td>${esc(s.context_bytes_per_hour)} B<div class="meta">≈ ${esc(s.estimated_tokens_per_hour)} tokens</div></td><td>${esc(JSON.stringify(s.outcomes||{}))}<div class="meta">${esc(s.last_24h_returns)} returns · ${esc(s.last_24h_context_bytes)} B</div></td></tr>`).join('')}</tbody></table></div>`:`<p class="empty">No model-visible wait returns yet.</p>`}</section><section class="card pool">${sessions.length?`<div class="table-scroll"><table aria-label="Session context pressure"><thead><tr><th>Seat</th><th>Board</th><th>Est. tokens / poll</th><th>Trend vs median</th><th>Pressure</th></tr></thead><tbody>${sessions.map(s=>`<tr><td><b>${esc(s.agent_name)}</b><div class="meta">sampled ${esc(fmt(s.latest_at))}</div></td><td>${esc(s.board_id)}</td><td>${esc(s.latest_estimated_tokens)}</td><td>${esc(s.trend)} ${s.trend_ratio===null?'—':`${esc(s.trend_ratio)}×`}<div class="meta">24-sample median ≈ ${esc(s.median_estimated_tokens)} tokens · ${esc(s.sample_count)} samples</div></td><td>${pressureBadge(s)}<div class="meta">${esc(s.next_action)}</div></td></tr>`).join('')}</tbody></table></div>`:`<p class="empty">No session pressure samples yet — context pressure is calm (${esc(d.source_status)}).</p>`}</section><details class="card pool" data-state-key="overhead-details:${esc(d.central)}"><summary>Bridge-to-Central diagnostic details</summary>${cumulative.length?`<div class="table-scroll"><table><thead><tr><th>Seat</th><th>Today</th><th>7-day</th><th>Top tools by bytes</th></tr></thead><tbody>${cumulative.map(s=>`<tr><td><b>${esc(s.agent_name)}</b><div class="meta">${esc(s.board_id)}</div></td><td>${esc(s.today_bytes)} B<div class="meta">≈ ${esc(s.today_estimated_tokens)} tokens · ${esc(s.today_calls)} calls</div></td><td>${esc(s.seven_day_bytes)} B<div class="meta">≈ ${esc(s.seven_day_estimated_tokens)} tokens · ${esc(s.seven_day_calls)} calls</div></td><td class="overhead-tools">${s.top_tools.map(t=>`${esc(t.tool)}: ${esc(t.bytes)} B`).join(' · ')||'—'}</td></tr>`).join('')}</tbody></table></div>`:`<p class="empty">No cumulative debug stats.</p>`}</details>`;bindInteractive(document.querySelector('#detail-view'))}
function sortedTickets(items){const rank=s=>['claimed','in_progress','creating_report'].includes(s)?0:['submitted','reviewing','in_review'].includes(s)?1:s==='open'?2:3;return [...items].sort((a,b)=>rank(a.status)-rank(b.status)||(detailSort==='oldest'?String(a.updated_at||'').localeCompare(String(b.updated_at||'')):String(b.updated_at||'').localeCompare(String(a.updated_at||''))))}
function tabs(d,r){return `<nav class="tabs" aria-label="Board views">${[['tickets','Tickets'],['timeline','Timeline'],['changes','Changes'],['flow','Ticket Flow'],['routes','Routes']].map(([v,label])=>`<a class="tab${r.view===v?' active':''}" href="${boardHref(r.central,d.board.board_id,v)}">${esc(label)}</a>`).join('')}</nav>`}
function ticketView(d,r){const rows=sortedTickets(d.tickets).filter(t=>matches([t.id,t.title,t.status,t.claimed_by,t.description],filterNeedle));const visible=!r.ticket||rows.some(t=>t.id===r.ticket);return `${visible?'':`<p class="warning">Requested ticket ${esc(r.ticket)} is outside this bounded response or filter.</p>`}<div class="toolbar"><span>${esc(rows.length)} of ${esc(d.ticket_returned)} returned tickets</span><label>Updated <select id="ticket-sort"><option value="newest"${detailSort==='newest'?' selected':''}>newest first</option><option value="oldest"${detailSort==='oldest'?' selected':''}>oldest first</option></select></label></div><section class="card"><div class="table-scroll"><table><thead><tr><th>Ticket</th><th>Title and details</th><th>Status</th><th class="hide-small">Updated</th></tr></thead><tbody>${rows.length?rows.map(t=>`<tr><td><span class="id">${esc(t.id)}</span></td><td><details class="ticket-detail" data-ticket="${esc(t.id)}" data-state-key="${esc(`ticket:${r.central}:${d.board.board_id}:${t.id}`)}"${r.ticket===t.id?' open':''}><summary>${esc(t.title)}</summary><p class="ticket-copy">${esc(t.description||'No description')}</p>${t.required_fields.length?`<div class="required">${t.required_fields.map(x=>`<span class="pill">${esc(x)}</span>`).join('')}</div>`:''}${t.latest_submission_summary?`<p class="meta ticket-copy">Latest submission: ${esc(t.latest_submission_summary)}</p>`:''}${t.review_label?`<p class="meta">Review: ${esc(t.review_label)}</p>`:''}</details></td><td><span class="status">${esc(t.status)}</span><div class="meta">${esc(t.claimed_by||'')}</div></td><td class="meta hide-small">${esc(fmt(t.updated_at))}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No tickets match the filter.</td></tr>'}</tbody></table></div></section>`}
function timelineView(d,r){const bySeq=new Map(d.events.map(e=>[e.seq,e]));const groups=d.timeline.map(day=>({...day,tickets:day.tickets.map(t=>({...t,events:t.event_seqs.map(seq=>bySeq.get(seq)).filter(Boolean).filter(e=>eventMatches(e,t.ticket_id,filterNeedle))})).filter(t=>t.events.length)})).filter(day=>day.tickets.length);return `<p class="muted bounded-note">Showing last ${esc(d.event_returned)} events from a read-only bounded catchup (ack=false).</p><section class="timeline">${groups.length?groups.map(day=>`<details class="card" data-state-key="${esc(`timeline-day:${r.central}:${d.board.board_id}:${day.day}`)}" open><summary><b>${esc(day.day)}</b></summary>${day.tickets.map(t=>`<details class="timeline-ticket" data-state-key="${esc(`timeline-ticket:${r.central}:${d.board.board_id}:${t.ticket_id}`)}"><summary><a class="id" href="${ticketHref(r.central,d.board.board_id,t.ticket_id)}">${esc(t.ticket_id)}</a> · ${esc(t.events.length)} event(s)</summary><div class="table-scroll"><table><tbody>${t.events.map(e=>`<tr><td class="id">${esc(e.seq)}</td><td>${esc(e.kind)}</td><td>${esc(e.status_from||'—')} → ${esc(e.status_to||'—')}</td><td class="meta">${esc(fmt(e.occurred_at))}</td></tr>`).join('')}</tbody></table></div></details>`).join('')}</details>`).join(''):'<p class="empty">No timeline events match the filter.</p>'}</section>`}
function changesFor(events,since,generatedAt){const cutoff=since===null?new Date(generatedAt).getTime()-86400000:null,chosen=events.filter(e=>since!==null?Number.isInteger(e.seq)&&e.seq>since:new Date(e.occurred_at).getTime()>=cutoff),counts={created:0,claimed:0,submitted:0,closed:0,rejected:0};for(const e of chosen){if(e.kind==='ticket_created')counts.created++;if(e.status_to==='claimed')counts.claimed++;if(e.status_to==='submitted')counts.submitted++;if(e.status_to==='closed')counts.closed++;if(e.review_verdict==='reject'||(e.status_from==='submitted'&&['open','claimed','rejected'].includes(e.status_to)&&Number(e.rejection_count)>0))counts.rejected++}return{counts,event_count:chosen.length}}
function changesView(d,r){const valid=r.since!==null&&/^\d+$/.test(r.since),since=valid?Number(r.since):null,events=filterChangeEvents(d.events,filterNeedle),summary=changesFor(events,since,d.generated_at);return `<div class="toolbar"><div><b>${since===null?'Last 24 hours':`After seq ${esc(since)}`}</b><p class="muted">Calculated only from the ${esc(d.event_returned)} returned events.</p></div><form id="changes-form"><label>Starting seq <input id="since-seq" inputmode="numeric" pattern="[0-9]*" value="${since===null?'':esc(since)}" placeholder="blank = 24h"></label> <button type="submit">Apply</button></form></div><section class="strip change-grid">${Object.entries(summary.counts).map(([name,count])=>`<div class="metric"><span>${esc(name)}</span><b>${esc(count)}</b></div>`).join('')}</section><p class="muted">${esc(summary.event_count)} bounded event(s) matched.</p>`}
function flowView(d,r){const byId=new Map(d.tickets.map(t=>[t.id,t])),labels={open:'Open',claimed:'Claimed',submitted:'Submitted',closed_today:'Closed today'};return `<p class="muted bounded-note">Classified from ${esc(d.ticket_returned)} returned tickets; omitted snapshot rows are not inferred.</p><section class="flow">${Object.entries(labels).map(([key,label])=>{const tickets=d.ticket_flow[key].map(id=>byId.get(id)).filter(Boolean).filter(t=>matches([t.id,t.title,t.claimed_by,t.status],filterNeedle));return `<div class="flow-column"><h3>${esc(label)} · ${esc(tickets.length)}</h3>${tickets.map(t=>`<a class="flow-card" href="${ticketHref(r.central,d.board.board_id,t.id)}"><span class="id">${esc(t.id)}</span><div>${esc(t.title)}</div><span class="meta">${esc(t.claimed_by||'Unassigned')}</span></a>`).join('')||'<p class="empty">No matching tickets</p>'}</div>`}).join('')}</section>`}
const routeStage=stage=>stage?`<b>${esc(stage.label)}</b><span class="meta">${esc(fmt(stage.at))}</span>`:'<span class="muted">—</span>';
function routesView(d,r){const routeData=d.routes||{rows:[],seats:[],row_returned:0,row_total:0,truncated:true,truncation_note:'Routes source unavailable.'},rows=routeData.rows.filter(t=>matches([t.id,t.title,t.status,t.created?.label,t.executed?.label,t.submitted?.label,t.reviewed?.label,t.rework_count],filterNeedle)),seats=routeData.seats.filter(s=>matches([s.label,s.created,s.executed,s.reviewed,s.rework_received_rate],filterNeedle));return `<section id="routes-view"><p class="${routeData.truncated?'warning':'muted'} bounded-note">${esc(routeData.truncation_note)}</p><h3 class="pool">Seat load in the window</h3><section class="route-load" aria-label="Per-seat route totals">${seats.length?seats.map(s=>`<article class="route-seat" data-route-seat="${esc(s.label)}"><b>${esc(s.label)}</b><div class="counts"><span class="pill">created ${esc(s.created)}</span><span class="pill">executed ${esc(s.executed)}</span><span class="pill">reviewed ${esc(s.reviewed)}</span><span class="pill">rework received ${esc(s.rework_received_rate)}% (${esc(s.rework_received)})</span></div></article>`).join(''):'<p class="empty">No seats match the filter.</p>'}</section><p class="muted">Showing ${esc(rows.length)} matching route(s) from ${esc(routeData.row_returned)} returned; ${esc(routeData.row_total)} assembled before row bounds.</p><section class="card pool"><div class="table-scroll"><table aria-label="Ticket provenance routes"><thead><tr><th>Ticket</th><th>Created by</th><th>Executed by</th><th>Submitted by</th><th>Reviewed / closed by</th><th>Rework</th><th>Updated</th></tr></thead><tbody>${rows.length?rows.map(t=>`<tr data-route-ticket="${esc(t.id)}"><td><a class="id" href="${ticketHref(r.central,d.board.board_id,t.id)}">${esc(t.id)}</a><div>${esc(t.title)}</div><span class="status">${esc(t.status)}</span></td><td class="route-stage">${routeStage(t.created)}</td><td class="route-stage">${routeStage(t.executed)}</td><td class="route-stage">${routeStage(t.submitted)}</td><td class="route-stage">${routeStage(t.reviewed)}</td><td>${esc(t.rework_count)}</td><td class="meta">${esc(fmt(t.updated_at))}</td></tr>`).join(''):'<tr><td colspan="7" class="empty">No routes match the filter.</td></tr>'}</tbody></table></div></section></section>`}
function findings(d){if(!d.coordinator_findings)return'';return `<section class="card"><h3>Coordinator findings</h3><div class="finding-list">${d.coordinator_findings.items.map(f=>`<div class="finding"><b>${esc(f.kind)}</b>${f.ticket_id?` <span class="id">${esc(f.ticket_id)}</span>`:''}<p>${esc(f.text)}</p>${f.draft?`<p class="meta">Draft · ${esc(f.draft.category)} · ${esc(f.draft.title)}</p>`:''}</div>`).join('')||'<p class="empty">No current findings</p>'}</div>${d.coordinator_findings.truncated_count?`<p class="warning">${esc(d.coordinator_findings.truncated_count)} findings omitted by the bounded state.</p>`:''}</section>`}
function renderDetail(d){const r=route();if(!r||r.kind!=='board'||r.central!==d.central||r.board!==d.board.board_id)return;const views={tickets:ticketView,timeline:timelineView,changes:changesView,flow:flowView,routes:routesView};document.querySelector('#detail-view').innerHTML=`<a class="back" href="#/">← All centrals</a><div class="top"><div><h2>${esc(d.board.label)} · ${esc(d.central)}</h2><span class="meta">${esc(d.board.board_id)}</span></div>${d.truncated?`<span class="status">${esc(d.ticket_returned)} of ${esc(d.ticket_total)} tickets shown</span>`:''}</div><div class="toolbar">${tabs(d,r)}<span class="muted">Two guarded writes only: config and intake · ${esc(d.central)}</span></div>${intakePanel(d,r)}${r.view==='tickets'?findings(d):''}${views[r.view](d,r)}`;document.querySelector('#intake-form')?.addEventListener('submit',submitIntake);for(const button of document.querySelectorAll('[data-intake-action]'))button.addEventListener('click',decideIntake);document.querySelector('#ticket-sort')?.addEventListener('change',e=>{detailSort=e.target.value;renderDetail(d)});document.querySelector('#changes-form')?.addEventListener('submit',e=>{e.preventDefault();const value=document.querySelector('#since-seq').value.trim();location.hash=`/central/${encodeURIComponent(r.central)}/board/${encodeURIComponent(d.board.board_id)}/changes${value?`?since=${encodeURIComponent(value)}`:''}`});document.querySelector('#state').textContent=`Updated ${fmt(d.generated_at)} · ${esc(d.central)}`;bindInteractive(document.querySelector('#detail-view'));renderSearchResults();if(r.ticket){const target=[...document.querySelectorAll('[data-ticket]')].find(x=>x.dataset.ticket===r.ticket);if(target){target.open=true;sectionStates.set(target.dataset.stateKey,true);target.scrollIntoView({block:'center'})}}}
const CENTRAL_REQUEST_TIMEOUT_MS=4000;
async function fetchJson(path,options={}){const response=await fetch(path,{cache:'no-store',...options});if(!response.ok)throw new Error(`HTTP ${response.status}`);return response.json()}
async function fetchWithTimeout(path,timeoutMs=CENTRAL_REQUEST_TIMEOUT_MS){const controller=new AbortController();let timer;const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(new Error('central request timed out'))},timeoutMs)});try{return await Promise.race([fetchJson(path,{signal:controller.signal}),timeout])}finally{clearTimeout(timer)}}
async function loadCentrals(){const d=await fetchJson('/api/centrals');centralLabels=d.centrals;defaultCentral=d.default}
async function refreshCentral(label,timeoutMs=CENTRAL_REQUEST_TIMEOUT_MS){const key=`fleet:${label}`;try{fleetData[label]=await fetchWithTimeout(`/api/fleet?${apiCentral(label)}`,timeoutMs);delete fleetErrors[label];if(typeof markConnectionSuccess==='function')markConnectionSuccess(key)}catch(e){fleetErrors[label]=e.message;if(typeof markConnectionFailure==='function')markConnectionFailure(key)}finally{if(!route())renderFleet()}}
async function refreshFleet(timeoutMs=CENTRAL_REQUEST_TIMEOUT_MS){if(!centralLabels.length)await loadCentrals();await Promise.allSettled(centralLabels.map(label=>refreshCentral(label,timeoutMs)))}
async function refreshOverhead(){const r=route();if(!r||r.kind!=='overhead')return;const key=`overhead:${r.central}`;try{const data=await fetchJson(`/api/overhead?${apiCentral(r.central)}`);if(route()?.central!==r.central)return;renderOverhead(data);markConnectionSuccess(key)}catch(e){markConnectionFailure(key);if(!document.querySelector('#detail-view').children.length)document.querySelector('#detail-view').innerHTML=`<a class="back" href="#/">← All centrals</a><p class="error">Overhead unavailable for ${esc(r.central)}.</p>`}}
async function refreshDetail(){const r=route();if(!r||r.kind!=='board')return;const key=`detail:${r.central}:${r.board}`;try{const data=await fetchJson(`/api/board/${encodeURIComponent(r.board)}?${apiCentral(r.central)}`);const current=route();if(current?.central!==r.central||current?.board!==r.board)return;detailData=data;renderDetail(data);refreshIntake(current,true);markConnectionSuccess(key)}catch(e){markConnectionFailure(key);if(!detailData||detailData.central!==r.central||detailData.board?.board_id!==r.board)document.querySelector('#detail-view').innerHTML=`<a class="back" href="#/">← All centrals</a><p class="error">Board detail unavailable for ${esc(r.central)}.</p>`}}
function syncRoute(){const r=route();document.querySelector('#home-view').hidden=!!r;document.querySelector('#detail-view').hidden=!r||r.kind==='config';if(detailTimer){clearInterval(detailTimer);detailTimer=null}for(const key of [...connectionFailures])if(key.startsWith('detail:')||key.startsWith('overhead:'))connectionFailures.delete(key);updateConnectionState();if(r?.kind==='board'){if(detailData?.central===r.central&&detailData?.board?.board_id===r.board)renderDetail(detailData);else document.querySelector('#detail-view').innerHTML='<p class="empty">Loading board detail…</p>';refreshDetail();detailTimer=setInterval(refreshDetail,5000)}else if(r?.kind==='overhead'){document.querySelector('#detail-view').innerHTML='<p class="empty">Loading overhead…</p>';refreshOverhead();detailTimer=setInterval(refreshOverhead,5000)}else if(!r)renderFleet()}
document.querySelector('#filter').addEventListener('input',e=>{filterNeedle=e.target.value.toLocaleLowerCase();searchSelection=0;const r=route();if(r?.kind==='board'&&detailData)renderDetail(detailData);else if(!r)renderFleet();else renderSearchResults()});document.querySelector('#search-results').addEventListener('click',e=>{const target=e.target.closest('[data-search-index]');if(target){e.preventDefault();jumpSearchResult(Number(target.dataset.searchIndex))}});document.querySelector('#theme-toggle').addEventListener('click',()=>{theme=theme==='dark'?'light':'dark';applyPreferences()});document.querySelector('#density-toggle').addEventListener('click',()=>{density=density==='comfortable'?'compact':'comfortable';applyPreferences()});document.querySelector('#help-toggle').addEventListener('click',()=>document.querySelector('#help-overlay').showModal());document.querySelector('#help-close').addEventListener('click',()=>document.querySelector('#help-overlay').close());document.addEventListener('keydown',e=>{const editing=['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName);if(e.key==='Escape'){document.querySelector('#search-results').hidden=true;document.querySelector('#help-overlay').close();return}if(e.key==='?'&&!editing){e.preventDefault();document.querySelector('#help-overlay').showModal();return}if(e.key==='/'&&!editing){e.preventDefault();document.querySelector('#filter').focus();return}if(document.activeElement===document.querySelector('#filter')&&['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();searchSelection=Math.max(0,Math.min(searchItems.length-1,searchSelection+(e.key==='ArrowDown'?1:-1)));renderSearchResults();return}if(document.activeElement===document.querySelector('#filter')&&e.key==='Enter'){e.preventDefault();jumpSearchResult(searchSelection);return}if(editing)return;if(goPrefix){clearTimeout(goTimer);goPrefix=false;if(e.key==='f')location.hash='#/';if(e.key==='o')location.hash=centralHref(defaultCentral,'overhead');if(e.key==='c')location.hash=centralHref(defaultCentral,'config');return}if(e.key==='g'){goPrefix=true;goTimer=setTimeout(()=>{goPrefix=false},800)}});window.addEventListener('hashchange',syncRoute);applyPreferences();loadCentrals().then(()=>{refreshFleet();syncRoute();if(typeof syncConfigRoute==='function')syncConfigRoute()}).catch(e=>{document.querySelector('#state').textContent='Startup failed';markConnectionFailure('startup')});setInterval(refreshFleet,5000);
</script></body></html>"""

HTML = (
    HTML.replace(
        "</style>",
        ".route-load{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin:12px 0 18px}"
        ".route-seat{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px}"
        ".route-seat .counts{margin:8px 0 0}.route-stage{min-width:150px}.route-stage .meta{display:block;white-space:nowrap}</style>",
    )
    .replace(
        "<dt>g then f</dt><dd>Fleet overview</dd><dt>g then o</dt>",
        "<dt>g then f</dt><dd>Fleet overview</dd><dt>g then r</dt><dd>Routes for the current board</dd><dt>g then o</dt>",
    )
    .replace(
        "if(e.key==='f')location.hash='#/';if(e.key==='o')",
        "if(e.key==='f')location.hash='#/';if(e.key==='r'){const current=route();if(current?.kind==='board')location.hash=boardHref(current.central,current.board,'routes')}if(e.key==='o')",
    )
)

# Keep the existing bounded fleet SPA intact; layer the one explicit write surface
# as an isolated hash page and API client.
HTML = (
    HTML.replace(
        "</style>",
        ".config-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}"
        ".config-grid label{display:grid;gap:5px}.config-grid input,.config-grid select,.config-grid button{background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px}"
        ".source{font-size:11px;color:var(--muted)}.central-group{margin-top:28px;padding-top:20px;border-top:2px solid var(--line)}.central-heading{align-items:center}.central-group.unavailable{border:1px solid var(--bad);border-radius:12px;padding:16px}"
        ".intake-layout{display:grid;grid-template-columns:minmax(260px,1fr) minmax(300px,2fr);gap:14px}.intake-form{display:grid;gap:8px}.intake-form textarea{min-height:82px;resize:vertical;background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px}.intake-row{padding:10px 0;border-top:1px solid var(--line)}.intake-preview{display:grid;gap:5px;margin:8px 0}.intake-preview input{width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px}.intake-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.intake-actions button{border:1px solid var(--line);border-radius:8px;padding:7px 10px}.intake-actions .approve{background:var(--good);color:var(--bg)}.intake-actions .decline{background:var(--panel2);color:var(--text)}@media(max-width:800px){.intake-layout{grid-template-columns:1fr}}</style>",
    )
    .replace(
        "Live boards and shared agent pool</p>",
        'Live boards and per-central agent pools · <a href="#/config">Coordinator config</a></p>',
    )
    .replace(
        '<section id="detail-view" hidden></section></main>',
        '<section id="detail-view" hidden></section><section id="config-view" hidden></section></main>',
    )
    .replace(
        "</body>",
        r"""<script>
const CONFIG_CATEGORIES=['docs','tests','audit-analysis','bug','production-code','release-ci','membership-roles','board-registry'];
const intakeQueues=new Map(),recentIntake=new Map();
const intakeKey=r=>`${r.central}/${r.board}`;
function intakeDraft(d,ask){const item=(d.coordinator_findings?.items||[]).find(f=>f.kind==='intake-pending'&&f.ask_id===ask.id&&f.draft?.title&&f.draft?.category);return item?.draft||null}
function intakePanel(d,r){const key=intakeKey(r),state=intakeQueues.get(key),waiting=state?.waiting||[],declined=state?.declined||[],waitingIds=new Set(waiting.map(x=>x.id)),declinedIds=new Set(declined.map(x=>x.id)),recent=recentIntake.get(key)||[],rows=waiting.slice(-25).map(x=>{const draft=intakeDraft(d,x);return{...x,draft,actionable:true,intake_status:x.approved?'approved · waiting for coordinator':draft?'waiting':'waiting for coordinator draft'}});for(const item of declined)rows.push({...item,actionable:false,intake_status:'declined'});for(const ask of recent)if(!waitingIds.has(ask.id)&&!declinedIds.has(ask.id))rows.push({...ask,actionable:false,intake_status:'consumed (gone)'});rows.sort((a,b)=>String(b.created_at||b.declined_at||'').localeCompare(String(a.created_at||a.declined_at||'')));return `<section class="card pool intake-layout"><form id="intake-form" class="intake-form"><div><h3>สั่งงาน / new ask</h3><p class="muted">5–500 characters · maximum 10 asks/hour</p></div><textarea name="text" minlength="5" maxlength="500" required placeholder="Describe one concrete ask for ${esc(d.board.label)}"></textarea><button type="submit">Submit ask</button><span id="intake-status" class="muted">${state?.error?esc(state.error):'Ready'}</span></form><div><h3>Pending asks</h3><p class="muted">Review the drafted title/category, then Approve or Decline. Approval authorizes creation while coordinator limits and idempotency still apply.</p><div>${rows.length?rows.map(x=>`<div class="intake-row" data-ask-id="${esc(x.id)}"><span class="status">${esc(x.intake_status)}</span> <span class="id">${esc(x.id)}</span><p>${esc(x.text)}</p>${x.draft?`<div class="intake-preview"><span class="meta">Draft category · ${esc(x.draft.category)}</span><label>Draft ticket title<input data-intake-title maxlength="200" value="${esc(x.approved_title||x.draft.title)}" ${x.approved?'disabled':''}></label></div>`:x.actionable?'<p class="muted">Waiting for the coordinator draft. Approve becomes available after its title and category arrive.</p>':''}<span class="meta">${esc(fmt(x.created_at||x.declined_at))} · ${esc(x.requested_by||x.declined_by)}</span>${x.actionable&&!x.approved?`<div class="intake-actions">${x.draft?'<button type="button" class="approve" data-intake-action="approve">Approve</button>':''}<button type="button" class="decline" data-intake-action="decline">Decline</button><span class="muted" data-intake-result></span></div>`:''}</div>`).join(''):'<p class="empty">No asks are waiting.</p>'}</div></div></section>`}
async function refreshIntake(r,rerender=false){const key=intakeKey(r);try{const data=await fetchJson(`/api/intake?${apiCentral(r.central)}&board_id=${encodeURIComponent(r.board)}`);intakeQueues.set(key,data)}catch(e){intakeQueues.set(key,{waiting:[],error:`Intake unavailable: ${e.message}`})}const current=route();if(rerender&&detailData&&current?.kind==='board'&&current.central===r.central&&current.board===r.board)renderDetail(detailData)}
async function submitIntake(event){event.preventDefault();const r=route();if(!r||r.kind!=='board')return;const form=event.target,status=form.querySelector('#intake-status'),button=form.querySelector('button'),text=new FormData(form).get('text');button.disabled=true;status.className='muted';status.textContent='Submitting…';try{const response=await fetch(`/api/intake?${apiCentral(r.central)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({board_id:r.board,text})});let body={};try{body=await response.json()}catch(_e){}if(!response.ok)throw new Error(body.error||`HTTP ${response.status}`);const key=intakeKey(r),recent=recentIntake.get(key)||[];recentIntake.set(key,[body.ask,...recent.filter(x=>x.id!==body.ask.id)].slice(0,25));form.reset();status.textContent=`Queued ${body.ask.id}`;await refreshIntake(r,true)}catch(e){status.className='error';status.textContent=`Submit failed: ${e.message}`}finally{button.disabled=false}}
async function decideIntake(event){const r=route();if(!r||r.kind!=='board')return;const button=event.currentTarget,row=button.closest('[data-ask-id]'),state=intakeQueues.get(intakeKey(r)),action=button.dataset.intakeAction,status=row.querySelector('[data-intake-result]'),title=row.querySelector('[data-intake-title]')?.value.trim();for(const item of row.querySelectorAll('button'))item.disabled=true;status.className='muted';status.textContent=`${action==='approve'?'Approving':'Declining'}…`;const payload={board_id:r.board,ask_id:row.dataset.askId,action,expected_sha256:state?.expected_sha256};if(action==='approve'&&title)payload.title=title;try{const response=await fetch(`/api/intake?${apiCentral(r.central)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});let body={};try{body=await response.json()}catch(_error){}if(!response.ok)throw new Error(body.error||`HTTP ${response.status}`);await refreshIntake(r,true)}catch(error){status.className='error';status.textContent=`Decision failed: ${error.message}`;for(const item of row.querySelectorAll('button'))item.disabled=false}}
const CONFIG_NUMBERS=[['stale_seconds','Stale seconds',10,86400],['lease_warning_ratio','Lease warning ratio',.1,1],['grace_seconds','Grace seconds',10,86400],['starved_seconds','Starved seconds',10,86400],['critical_starved_seconds','Critical starved seconds',10,86400],['review_backlog_seconds','Review backlog seconds',10,86400],['abandoner_drops','Abandoner drops',1,20],['abandoner_window_days','Abandoner window days',1,365],['context_watch_tokens_per_poll','Context watch tokens / poll',1000,10000000],['context_compact_tokens_per_poll','Context compact tokens / poll',1001,20000000],['context_trend_compact_ratio','Context trend compact ratio',1.01,10]];
let coordinatorConfig=null;
const sourceFor=(d,path)=>d.sources?.[path]||'unknown';
function configNumber(d,key,label,min,max){const v=d.effective.thresholds[key];return `<label>${esc(label)} <span class="source">source: ${esc(sourceFor(d,`thresholds.${key}`))}</span><input name="${esc(key)}" type="number" min="${min}" max="${max}" step="${key.endsWith('_ratio')?'.01':'1'}" value="${esc(v)}" required></label>`}
function renderConfig(d){coordinatorConfig=d;const e=d.effective||{};if(!e.thresholds||!e.intake){document.querySelector('#config-view').innerHTML=`<a class="back" href="#/">← All centrals</a><h2>Coordinator config · ${esc(d.central)}</h2><p class="warning">Run the coordinator once to publish effective values before editing.</p>`;return}document.querySelector('#config-view').innerHTML=`<a class="back" href="#/">← All centrals</a><div class="top"><div><h2>Coordinator config · ${esc(d.central)}</h2><p class="muted">Live policy document on the ${esc(d.central)} home board</p></div><div><span class="status">mode: ${esc(d.mode)}</span><p class="warning">Mode changes require a restart.</p></div></div><form id="config-form" class="card pool"><div class="config-grid">${CONFIG_NUMBERS.map(x=>configNumber(d,...x)).join('')}<label>Integration watch since <span class="source">source: ${esc(sourceFor(d,'integration_watch_since'))}</span><input name="integration_watch_since" type="text" placeholder="ISO-8601 or blank" value="${esc(e.integration_watch_since||'')}"></label><label>Intake enabled <span class="source">source: ${esc(sourceFor(d,'intake.enabled'))}</span><input name="enabled" type="checkbox" ${e.intake.enabled?'checked':''}></label><label>Intake token path <span class="source">source: ${esc(sourceFor(d,'intake.token_path'))}</span><input name="token_path" type="text" placeholder="/absolute/path/to/intake-token" value="${esc(e.intake.token_path||'')}"></label><label>Work domain always ask <span class="source">source: ${esc(sourceFor(d,'intake.work_domain_always_ask'))}</span><input name="work_domain_always_ask" type="checkbox" ${e.intake.work_domain_always_ask?'checked':''}></label><label>Intake rate per hour <span class="source">source: ${esc(sourceFor(d,'intake.rate_per_hour'))}</span><input name="rate_per_hour" type="number" min="1" max="20" value="${esc(e.intake.rate_per_hour)}" required></label>${CONFIG_CATEGORIES.map(c=>`<label>${esc(c)} policy <span class="source">source: ${esc(sourceFor(d,e.intake.auto_categories.includes(c)?'intake.auto_categories':'intake.always_ask_categories'))}</span><select name="category_${esc(c)}"><option value="auto"${e.intake.auto_categories.includes(c)?' selected':''}>auto</option><option value="ask"${e.intake.always_ask_categories.includes(c)?' selected':''}>always ask</option></select></label>`).join('')}</div><div class="toolbar"><button type="submit">Save config</button><span id="config-status" class="muted">${esc(d.concurrency.toUpperCase())} · updated ${esc(fmt(d.updated_at))} by ${esc(d.updated_by||'—')}</span></div></form>`;document.querySelector('#config-form').addEventListener('submit',saveConfig)}
async function refreshConfig(){const r=route();if(!r||r.kind!=='config')return;const key=`config:${r.central}`;try{const response=await fetch(`/api/config?${apiCentral(r.central)}`,{cache:'no-store'});if(!response.ok)throw new Error(`HTTP ${response.status}`);const data=await response.json();if(route()?.central===r.central){renderConfig(data);markConnectionSuccess(key)}}catch(e){markConnectionFailure(key);if(!coordinatorConfig||coordinatorConfig.central!==r.central)document.querySelector('#config-view').innerHTML=`<a class="back" href="#/">← All centrals</a><p class="error">Config unavailable for ${esc(r.central)}.</p>`}}
async function saveConfig(event){event.preventDefault();const f=new FormData(event.target),thresholds={};for(const [key] of CONFIG_NUMBERS)thresholds[key]=key.endsWith('_ratio')?Number(f.get(key)):Number.parseInt(f.get(key),10);const auto=[],always=[];for(const c of CONFIG_CATEGORIES)(f.get(`category_${c}`)==='auto'?auto:always).push(c);const config={schema_version:1,thresholds,integration_watch_since:f.get('integration_watch_since').trim()||null,intake:{enabled:f.get('enabled')==='on',token_path:f.get('token_path').trim()||null,auto_categories:auto,always_ask_categories:always,work_domain_always_ask:f.get('work_domain_always_ask')==='on',rate_per_hour:Number.parseInt(f.get('rate_per_hour'),10)}};const status=document.querySelector('#config-status'),central=coordinatorConfig.central;status.textContent=`Saving ${central}…`;try{const r=await fetch(`/api/config?${apiCentral(central)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config,expected_sha256:coordinatorConfig.expected_sha256})});const body=await r.json();if(!r.ok)throw new Error(body.error||`HTTP ${r.status}`);status.textContent=`Saved ${body.central} with ${body.concurrency.toUpperCase()}; waiting for coordinator poll`;setTimeout(refreshConfig,1000)}catch(e){status.textContent=`Save failed for ${central}: ${e.message}`;status.className='error'}}
function syncConfigRoute(){const r=route(),active=r?.kind==='config';document.querySelector('#config-view').hidden=!active;if(active){document.querySelector('#home-view').hidden=true;document.querySelector('#detail-view').hidden=true;if(centralLabels.length)refreshConfig()}}
window.addEventListener('hashchange',syncConfigRoute);syncConfigRoute();
</script></body>""",
    )
)

HTML = (
    HTML.replace(
        "|overhead|config)(?:\\?(.*))?$/)",
        "|overhead|config|workers)(?:\\?(.*))?$/)",
    )
    .replace(
        "if(m[2]==='overhead'||m[2]==='config')",
        "if(m[2]==='overhead'||m[2]==='config'||m[2]==='workers')",
    )
    .replace(
        "document.querySelector('#detail-view').hidden=!r||r.kind==='config'",
        "document.querySelector('#detail-view').hidden=!r||r.kind==='config'||r.kind==='workers'",
    )
    .replace(
        '<section id="config-view" hidden></section>',
        '<section id="config-view" hidden></section><section id="workers-view" hidden></section>',
    )
    .replace(
        "<dt>g then c</dt><dd>Coordinator config</dd>",
        "<dt>g then c</dt><dd>Coordinator config</dd><dt>g then w</dt><dd>API workers</dd>",
    )
    .replace(
        "if(e.key==='c')location.hash=centralHref(defaultCentral,'config')",
        "if(e.key==='c')location.hash=centralHref(defaultCentral,'config');if(e.key==='w')location.hash=centralHref(defaultCentral,'workers')",
    )
    .replace(
        "if(typeof syncConfigRoute==='function')syncConfigRoute()",
        "if(typeof syncConfigRoute==='function')syncConfigRoute();if(typeof syncWorkersRoute==='function')syncWorkersRoute()",
    )
    .replace(
        '<a class="tab" href="${centralHref(central,\'overhead\')}">Overhead</a><a class="tab" href="${centralHref(central,\'config\')}">Config</a>',
        '<a class="tab" href="${centralHref(central,\'workers\')}">Workers</a><a class="tab" href="${centralHref(central,\'overhead\')}">Overhead</a><a class="tab" href="${centralHref(central,\'config\')}">Config</a>',
    )
    .replace(
        '<a class="tab" href="${centralHref(label,\'overhead\')}">Overhead</a><a class="tab" href="${centralHref(label,\'config\')}">Config</a>',
        '<a class="tab" href="${centralHref(label,\'workers\')}">Workers</a><a class="tab" href="${centralHref(label,\'overhead\')}">Overhead</a><a class="tab" href="${centralHref(label,\'config\')}">Config</a>',
    )
)
HTML = HTML.replace(
    "</style>",
    ".worker-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:14px}.worker-form label{display:grid;gap:5px}.worker-form input,.worker-form select,.worker-form button,.worker-actions button{background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px}.worker-form .worker-submit{align-self:end}.worker-actions{display:flex;flex-wrap:wrap;gap:6px}.worker-command{display:block;max-width:72ch;white-space:pre-wrap;overflow-wrap:anywhere;margin-top:5px}</style>",
)
HTML = HTML.replace(
    "</script></body>",
    r"""
</script><script>
let workerData=null,workerFormDirty=false,workerActionMessage='',workerActionError=false;
function workerPresetOptions(presets){return Object.entries(presets).map(([key,p])=>`<option value="${esc(key)}" data-url="${esc(p.base_url)}" data-key="${p.key_required}">${esc(p.label)}</option>`).join('')}
function renderWorkers(d){workerData=d;workerFormDirty=false;const rows=d.workers||[];document.querySelector('#workers-view').innerHTML=`<a class="back" href="#/">← All centrals</a><div class="top"><div><h2>API workers · ${esc(d.central)}</h2><p class="muted">Keys go directly to macOS Keychain; local configs contain only a Keychain account reference.</p></div><span class="status">${rows.length} configured</span></div><section class="card pool"><h3>Configured workers</h3><div class="table-scroll"><table><thead><tr><th>Name</th><th>Provider / host</th><th>Model</th><th>Process</th><th>Seat</th><th>Actions</th></tr></thead><tbody>${rows.length?rows.map(w=>`<tr><td><b class="id">${esc(w.name)}</b></td><td>${esc(w.provider_label)}<div class="meta">${esc(w.base_url_host)}</div></td><td>${esc(w.model)}</td><td><span class="status">${w.running?`running · PID ${esc(w.pid)}`:'stopped'}</span>${w.adopted?'<div class="meta">adopted</div>':''}</td><td>${w.seat_exists?'<span class="status">ready</span>':`<span class="warning">missing</span><code class="worker-command">${esc(w.seat_admin_command)}</code>`}</td><td><div class="worker-actions"><button type="button" data-worker-action="test" data-name="${esc(w.name)}">Test</button><button type="button" data-worker-action="start" data-name="${esc(w.name)}" ${w.running?'disabled':''}>Start</button><button type="button" data-worker-action="stop" data-name="${esc(w.name)}" ${w.running?'':'disabled'}>Stop</button>${w.seat_exists?'':`<button type="button" data-copy-command="${esc(w.seat_admin_command)}">Copy seat command</button>`}</div></td></tr>`).join(''):'<tr><td colspan="6" class="empty">No API workers configured.</td></tr>'}</tbody></table></div><p id="worker-action-status" class="${workerActionError?'error':'muted'}">${esc(workerActionMessage)}</p></section><section class="card pool"><h3>Add or update worker</h3><form id="worker-form" class="worker-form"><label>Name<input name="name" pattern="[a-z0-9-]{2,32}" maxlength="32" placeholder="api-worker-1" required></label><label>Provider<select name="provider">${workerPresetOptions(d.presets)}</select></label><label>Base URL<input name="base_url" type="url" required></label><label>Model<input name="model" maxlength="200" placeholder="model-id" required></label><label>API key<input name="api_key" type="password" maxlength="8192" autocomplete="new-password" required></label><button class="worker-submit" type="submit">Save worker</button></form><p id="worker-save-status" class="muted">Saving an existing worker requires it to be stopped.</p></section>`;const form=document.querySelector('#worker-form'),provider=form.elements.provider,base=form.elements.base_url,key=form.elements.api_key;function syncPreset(){const option=provider.selectedOptions[0];base.value=option.dataset.url||'';base.readOnly=!['custom','azure'].includes(provider.value);key.required=option.dataset.key==='true';key.disabled=option.dataset.key!=='true';if(key.disabled)key.value=''}provider.addEventListener('change',syncPreset);syncPreset();form.addEventListener('input',()=>{workerFormDirty=true});form.addEventListener('change',()=>{workerFormDirty=true});form.addEventListener('submit',saveWorker);document.querySelector('#workers-view').onclick=workerClick;bindInteractive(document.querySelector('#workers-view'))}
async function refreshWorkers(force=false){const r=route();if(!r||r.kind!=='workers'||(workerFormDirty&&!force))return;const key=`workers:${r.central}`;try{const response=await fetch(`/api/workers?${apiCentral(r.central)}`,{cache:'no-store'}),body=await response.json();if(!response.ok)throw new Error(body.error||`HTTP ${response.status}`);if(route()?.central===r.central){renderWorkers(body);markConnectionSuccess(key)}}catch(e){markConnectionFailure(key);document.querySelector('#workers-view').innerHTML=`<a class="back" href="#/">← All centrals</a><p class="error">Workers unavailable for ${esc(r.central)}: ${esc(e.message)}</p>`}}
async function workerRequest(path,central,payload={}){const response=await fetch(`${path}?${apiCentral(central)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),body=await response.json();if(!response.ok)throw new Error(body.error||`HTTP ${response.status}`);return body}
async function saveWorker(event){event.preventDefault();const form=event.target,f=new FormData(form),central=route().central,status=document.querySelector('#worker-save-status'),payload={name:f.get('name'),provider:f.get('provider'),base_url:f.get('base_url'),model:f.get('model'),api_key:f.get('api_key')||''};status.textContent='Saving…';try{await workerRequest('/api/workers',central,payload);form.elements.api_key.value='';workerFormDirty=false;status.textContent='Saved. Keychain updated when the provider requires a key.';await refreshWorkers(true)}catch(e){form.elements.api_key.value='';status.textContent=`Save failed: ${e.message}`;status.className='error'}}
async function workerClick(event){const copy=event.target.closest('[data-copy-command]');if(copy){await navigator.clipboard.writeText(copy.dataset.copyCommand);workerActionMessage='Seat command copied.';workerActionError=false;const status=document.querySelector('#worker-action-status');status.textContent=workerActionMessage;status.className='muted';return}const button=event.target.closest('[data-worker-action]');if(!button)return;const central=route().central,status=document.querySelector('#worker-action-status'),label=button.textContent,action=button.dataset.workerAction;button.disabled=true;workerActionMessage=`${label} ${button.dataset.name}…`;workerActionError=false;status.textContent=workerActionMessage;status.className='muted';try{await workerRequest(`/api/workers/${encodeURIComponent(button.dataset.name)}/${action}`,central);workerActionMessage=`${label} succeeded.`;if(action!=='test')await refreshWorkers(true);const current=document.querySelector('#worker-action-status');current.textContent=workerActionMessage;current.className='muted';if(action==='test')button.disabled=false}catch(e){workerActionMessage=`${label} failed: ${e.message}`;workerActionError=true;status.textContent=workerActionMessage;status.className='error';button.disabled=false}}
function syncWorkersRoute(){const r=route(),active=r?.kind==='workers';document.querySelector('#workers-view').hidden=!active;if(active){document.querySelector('#home-view').hidden=true;document.querySelector('#detail-view').hidden=true;document.querySelector('#config-view').hidden=true;if(centralLabels.length)refreshWorkers()}}
window.addEventListener('hashchange',syncWorkersRoute);syncWorkersRoute();setInterval(refreshWorkers,5000);
</script></body>""",
)

# Attention state is local dashboard state, durable across browser/process restarts.
HTML = HTML.replace(
    "</script></body>",
    r"""
</script><script>
let attentionState={},attentionStateLoaded=false,attentionStateSaved='';
function loadAttentionState(){return attentionState}
async function saveAttentionState(value){attentionState=value;if(!attentionStateLoaded)return;const encoded=JSON.stringify(value);if(encoded===attentionStateSaved)return;const response=await fetch('/api/attention',{method:'POST',headers:{'Content-Type':'application/json'},body:encoded});if(!response.ok)throw new Error(`attention state HTTP ${response.status}`);attentionStateSaved=encoded}
async function refreshAttentionState(){try{const result=await fetchJson('/api/attention');attentionState=result.items&&typeof result.items==='object'?result.items:{};attentionStateSaved=JSON.stringify(attentionState);attentionStateLoaded=true;if(navKind()==='overview')renderHub()}catch(_error){attentionStateLoaded=false}}
function attentionCandidates(){const rows=[];for(const [central,d] of Object.entries(fleetData)){for(const b of d.boards||[]){for(const f of b.coordinator_findings?.items||[]){rows.push({key:`finding|${central}|${b.board_id}|${f.kind}|${f.ticket_id||''}`,fingerprint:`${f.level}|${f.kind}|${f.text}`,type:'finding',central,board:b,level:f.level||'info',title:f.kind,text:f.text,ticket_id:f.ticket_id})}for(const t of b.tickets||[]){const age=Date.now()-new Date(t.updated_at||Date.now()).getTime();if(t.status==='open'&&age>1800000)rows.push({key:`starved|${central}|${b.board_id}|${t.id}`,fingerprint:'open-over-30m',type:'starved',central,board:b,level:'warn',title:'Starved ticket',text:t.title,ticket_id:t.id,age});if((t.abandoned_count||0)>0)rows.push({key:`lease-lapsed|${central}|${b.board_id}|${t.id}`,fingerprint:`lease-lapsed-${t.abandoned_count}`,type:'lease-lapsed',central,board:b,level:'warn',title:`Lease lapsed ${t.abandoned_count} times`,text:t.title,ticket_id:t.id,age})}}for(const p of hubOverhead[central]?.sessions||[]){if(p.pressure==='ok')continue;rows.push({key:`context|${central}|${p.board_id}|${p.agent_name}`,fingerprint:`${p.pressure}|${p.mode||'unknown'}|${p.reason||''}`,type:'context',central,board:{board_id:p.board_id,label:p.board_id},level:p.pressure==='compact'||p.pressure==='anomaly'?'critical':'warn',title:p.pressure==='anomaly'?'Context stats anomaly':`Context ${p.pressure}`,text:p.pressure==='anomaly'?`${p.agent_name} · raw wait-return record requires inspection`:`${p.agent_name} · ${p.estimated_tokens_per_return??p.latest_estimated_tokens} tokens / return · ${p.estimated_tokens_per_hour??'—'} / hour · ${p.mode||'unknown'}${p.reason?' · '+p.reason:''}`})}for(const p of hubOverhead[central]?.push_unavailable||[]){rows.push({key:`push-unavailable|${central}|${p.board_id}|${p.agent_name}`,fingerprint:`${p.reason}|${p.observed_at}`,type:'push-unavailable',central,board:{board_id:p.board_id,label:p.board_id},level:'critical',title:'Push unavailable',text:p.warning||`push unavailable: ${p.reason}`})}}return rows}
function reconcileAttention(){const now=new Date(),before=loadAttentionState(),next={},visible=[];for(const item of attentionCandidates()){if(!item.key||next[item.key])continue;const old=before[item.key],same=old?.fingerprint===item.fingerprint;const row={fingerprint:item.fingerprint,first_seen:same&&old.first_seen?old.first_seen:now.toISOString(),last_seen:now.toISOString(),acknowledged:same&&old.acknowledged===true,snooze_until:same?old.snooze_until||null:null};next[item.key]=row;const snoozed=row.snooze_until&&new Date(row.snooze_until)>now;if(!row.acknowledged&&!snoozed)visible.push({...item,...row})}saveAttentionState(next).catch(()=>{});window.__fleetAttentionPanel={generated_at:now.toISOString(),items:visible.map(x=>({key:x.key,type:x.type,central:x.central,board_id:x.board.board_id,ticket_id:x.ticket_id||null,first_seen:x.first_seen,last_seen:x.last_seen}))};return visible}
function attentionRow(x){const link=x.ticket_id?`<a class="id" href="${ticketHref(x.central,x.board.board_id,x.ticket_id)}">${esc(x.ticket_id)}</a>`:`<a href="${centralHref(x.central,'overhead')}">Inspect</a>`;return `<div class="finding-row"><span class="severity ${esc(x.level)}"></span><div><b>${esc(x.title)}</b><p>${esc(x.text)}</p><span class="meta">${esc(x.central)} · ${esc(x.board.label)} · first seen ${esc(fmt(x.first_seen))}</span><div class="attention-actions"><button type="button" data-attention-action="ack" data-attention-key="${esc(x.key)}">Acknowledge</button><button type="button" data-attention-action="snooze" data-attention-key="${esc(x.key)}">Snooze 24h</button></div></div>${link}</div>`}
function renderAttentionOverview(){const centrals=centralLabels.map(label=>{const d=fleetData[label],error=fleetErrors[label];if(!d)return `<article class="health-card"><div class="signal"><span class="signal-dot bad"></span><b>${esc(label)}</b></div><p class="error">${esc(error||'Connecting…')}</p></article>`;const s=d.pool_summary||{},heartbeat=(d.boards||[]).map(b=>b.coordinator_heartbeat).filter(Boolean).sort().at(-1),tc={open:0,claimed:0,submitted:0,closed_today:0};for(const b of d.boards||[])for(const k in tc)tc[k]+=numberCount((b.counts||{})[k]);return `<article class="health-card"><div class="signal"><span class="signal-dot"></span><b>${esc(label)}</b><span class="status">central up</span></div><p class="meta">Coordinator heartbeat ${esc(heartbeat?fmt(heartbeat):'not observed')}</p><div class="health-metrics"><span>Busy<b>${esc(s.busy||0)}</b></span><span>Ready<b>${esc(s.available||0)}</b></span><span>Stale<b>${esc(s.stale||0)}</b></span></div><div class="health-metrics"><span>Open<b>${esc(tc.open)}</b></span><span>Claimed<b>${esc(tc.claimed)}</b></span><span>Submitted${tc.submitted?' ⚠':''}<b>${esc(tc.submitted)}</b></span><span>Closed today<b>${esc(tc.closed_today)}</b></span></div></article>`}).join('');const surfaced=reconcileAttention().sort((a,b)=>(b.level==='critical')-(a.level==='critical')||(b.age||0)-(a.age||0)),attention=surfaced.slice(0,10);return `${pageHead('Home','Fleet overview','Health and attention across every central.')}<section class="health-grid">${centrals||'<div class="skeleton"></div>'}</section><div class="section-title"><h3>Needs attention</h3><span class="status">${surfaced.length} surfaced</span></div><section class="attention-card">${attention.map(attentionRow).join('')||'<p class="empty">Nothing needs attention. The fleet is calm.</p>'}</section>`}
function renderAttentionBoardsHub(){const cards=[];for(const [central,d] of Object.entries(fleetData))for(const b of d.boards||[]){const total=Object.values(b.counts||{}).reduce((sum,v)=>sum+numberCount(v),0),tr=b.snapshot_truncation,info=tr&&tr.total>tr.returned?`<span class="status">snapshot truncated to ${esc(tr.returned)} of ${esc(tr.total)} tickets</span>`:'';cards.push(`<article class="board-card"><div><p class="eyebrow">${esc(central)}</p><h3>${esc(b.label)}</h3><span class="meta">${esc(b.board_id)} · ${esc(total)} visible tickets</span> ${info}</div><div class="counts">${Object.entries(b.counts||{}).map(([k,v])=>`<span class="pill">${esc(k.replace('_',' '))} <b>${esc(v)}</b></span>`).join('')}</div><div class="card-actions"><a class="primary-action" href="${boardHref(central,b.board_id)}">Workspace</a><a href="${boardHref(central,b.board_id,'flow')}">Flow</a><a href="${boardHref(central,b.board_id,'timeline')}">Timeline</a><a href="${boardHref(central,b.board_id,'changes')}">Changes</a><a href="${boardHref(central,b.board_id,'routes')}">Routes</a></div></article>`)}return `${pageHead('Boards','Board workspaces','Open one board, then move through tickets, findings, intake, flow, timeline, changes, and routes.')}<section class="boards-list">${cards.join('')||'<div class="skeleton"></div>'}</section>`}
refreshAttentionState();
</script></body>""",
    1,
).replace(
    "</style>",
    ".attention-actions{display:flex;gap:6px;margin-top:6px}.attention-actions button{background:var(--panel2);border:1px solid var(--line);border-radius:7px;color:var(--text);padding:4px 7px}</style>",
    1,
)

# v2 information architecture: one shell, four primary destinations, and
# progressive detail. Existing board/config/worker routes stay addressable.
HTML = HTML.replace(
    "</style>",
    r"""
:root{--space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;--space-5:24px;--space-6:32px;--radius-sm:8px;--radius-md:14px;--radius-lg:20px;--shadow:0 16px 40px #0002;--sidebar:224px;--surface:var(--panel);--surface-raised:var(--panel2);--focus:0 0 0 3px color-mix(in srgb,var(--accent) 28%,transparent)}
body{background:radial-gradient(circle at 90% -10%,color-mix(in srgb,var(--accent) 10%,transparent),transparent 34rem),var(--bg)}.app-shell{display:grid;grid-template-columns:var(--sidebar) minmax(0,1fr);min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;padding:var(--space-5) var(--space-3);background:color-mix(in srgb,var(--panel) 92%,transparent);border-right:1px solid var(--line);display:flex;flex-direction:column;gap:var(--space-5);z-index:12}.brand{display:flex;align-items:center;gap:10px;padding:0 8px;color:var(--text);font-weight:750;font-size:16px}.brand-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:11px;background:linear-gradient(145deg,var(--accent),color-mix(in srgb,var(--accent) 50%,var(--good)));color:#fff;box-shadow:var(--shadow)}.primary-nav{display:grid;gap:5px}.primary-nav a{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:var(--radius-sm);color:var(--muted);font-weight:650;letter-spacing:.02em}.primary-nav a:hover,.primary-nav a.active{background:var(--panel2);color:var(--text);text-decoration:none}.primary-nav a.active{box-shadow:inset 3px 0 var(--accent)}.nav-icon{width:20px;text-align:center}.sidebar-foot{margin-top:auto;padding:10px;color:var(--muted);font-size:12px}.app-shell>main{max-width:1480px;margin:0;padding:var(--space-5) clamp(16px,3vw,42px)}.app-shell>main>.top{padding-bottom:var(--space-5);border-bottom:1px solid var(--line);align-items:start}.app-shell h1{font-size:clamp(24px,3vw,34px);letter-spacing:-.035em}.page-head{display:flex;align-items:end;justify-content:space-between;gap:var(--space-4);margin:var(--space-6) 0 var(--space-4)}.page-head h2{font-size:clamp(22px,2.4vw,30px);letter-spacing:-.025em}.eyebrow{color:var(--accent);font-size:11px;font-weight:800;letter-spacing:.14em;text-transform:uppercase}.health-grid,.attention-grid,.agent-grid,.ops-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:var(--space-4)}.health-card,.attention-card,.agent-card,.ops-card,.board-card{background:linear-gradient(145deg,color-mix(in srgb,var(--panel) 96%,var(--accent) 4%),var(--panel));border:1px solid var(--line);border-radius:var(--radius-md);padding:var(--space-4);box-shadow:0 1px 0 #fff1;min-width:0}.health-card .signal{display:flex;align-items:center;gap:7px}.signal-dot{width:8px;height:8px;border-radius:50%;background:var(--good);box-shadow:0 0 0 4px color-mix(in srgb,var(--good) 15%,transparent)}.signal-dot.bad{background:var(--bad);box-shadow:0 0 0 4px color-mix(in srgb,var(--bad) 15%,transparent)}.health-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:14px}.health-metrics span{padding:9px;border-radius:9px;background:var(--panel2);color:var(--muted);font-size:11px}.health-metrics b{display:block;color:var(--text);font-size:18px}.section-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:var(--space-6) 0 var(--space-3)}.section-title h3{font-size:17px}.finding-row,.work-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px;align-items:start;padding:10px 0;border-top:1px solid var(--line)}.finding-row:first-child,.work-row:first-child{border-top:0}.work-row b,.work-row .meta{display:block}.severity{width:8px;height:8px;border-radius:50%;margin-top:6px;background:var(--warn)}.severity.critical{background:var(--bad)}.boards-list{display:grid;gap:var(--space-3)}.board-card{display:grid;grid-template-columns:minmax(190px,1fr) minmax(220px,1.4fr) auto;align-items:center;gap:var(--space-4)}.board-card .counts{margin:0}.card-actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:7px}.button,button{cursor:pointer}.button,.card-actions a,.agent-actions button,.primary-action{display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--line);border-radius:9px;background:var(--panel2);color:var(--text);padding:8px 11px;font:inherit;text-decoration:none}.button:hover,.card-actions a:hover,.agent-actions button:hover{border-color:var(--accent);text-decoration:none}.primary-action{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:700}.agent-card{display:grid;gap:12px}.agent-card-head{display:flex;justify-content:space-between;gap:10px}.agent-role{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--accent)}.role-chip{display:inline-block;padding:1px 6px;margin:0 2px;border-radius:5px;background:color-mix(in srgb,var(--accent) 12%,transparent);border:1px solid color-mix(in srgb,var(--accent) 25%,transparent);font-size:10px;text-transform:none;letter-spacing:0}.role-chip .chip-board{font-weight:600;color:var(--muted)}.role-chip .chip-role{color:var(--accent)}.agent-actions{display:flex;flex-wrap:wrap;gap:7px}.agent-actions button{padding:7px 9px}.log-tail{margin:0;max-height:180px;overflow:auto;padding:10px;border-radius:8px;background:#070b15;color:#c7d2eb;font:11px/1.5 ui-monospace,SFMono-Regular,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.pressure-line{display:flex;align-items:center;gap:8px}.agent-dialog{width:min(680px,calc(100vw - 28px));padding:0;border-radius:var(--radius-lg);box-shadow:var(--shadow)}.dialog-head{display:flex;justify-content:space-between;gap:12px;padding:20px 22px;border-bottom:1px solid var(--line)}.dialog-body{padding:20px 22px}.dialog-close{border:0;background:transparent;color:var(--muted);font-size:22px}.agent-form{display:grid;grid-template-columns:1fr 1fr;gap:13px}.agent-form label{display:grid;gap:5px}.agent-form input,.agent-form select{width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:9px;color:var(--text);padding:10px}.agent-form .wide{grid-column:1/-1}.guide{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}.guide-step{padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)}.guide-step b{display:block;margin-bottom:6px}.guide-step code{display:block;overflow-wrap:anywhere;white-space:pre-wrap;font-size:11px}.route-links{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}.route-links a{font-size:12px}.skeleton{min-height:82px;border-radius:12px;background:linear-gradient(90deg,var(--panel),var(--panel2),var(--panel));background-size:220% 100%;animation:shimmer 1.4s infinite}@keyframes shimmer{to{background-position:-220% 0}}:focus-visible{outline:none;box-shadow:var(--focus)}
@media(max-width:800px){.app-shell{display:block}.sidebar{position:sticky;height:auto;padding:8px 10px;gap:8px;border-right:0;border-bottom:1px solid var(--line)}.brand{display:none}.primary-nav{display:grid;grid-template-columns:repeat(4,1fr)}.primary-nav a{justify-content:center;padding:9px 4px;font-size:11px}.primary-nav a.active{box-shadow:inset 0 -3px var(--accent)}.nav-icon{display:none}.sidebar-foot{display:none}.app-shell>main{padding:14px}.app-shell>main>.top{gap:14px}.app-shell>main>.top>div:last-child{width:100%}.view-controls{justify-content:flex-end}.search-wrap,.search{width:100%}.board-card{grid-template-columns:1fr}.card-actions{justify-content:flex-start}.health-grid,.attention-grid,.agent-grid,.ops-grid{grid-template-columns:1fr}.agent-form,.guide{grid-template-columns:1fr}.agent-form .wide{grid-column:auto}.page-head{align-items:start}.finding-row,.work-row{grid-template-columns:auto minmax(0,1fr)}.finding-row>:last-child,.work-row>:last-child{grid-column:2}.dialog-body{padding:16px}.flow{overflow:visible}.table-scroll{max-width:calc(100vw - 28px)}}
@media print{.sidebar{display:none}.app-shell{display:block}.app-shell>main{padding:0}.app-shell>main>.top{border:0}}
</style>""",
)
HTML = HTML.replace(
    "</style>",
    r""".agent-card-state{display:grid;justify-items:end;align-content:start;gap:5px}.agent-live{display:flex;align-items:center;flex-wrap:wrap;gap:6px;min-width:0}.agent-ticket{display:inline-block;max-width:min(42ch,100%);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom}
</style>""",
    1,
)

HTML = HTML.replace(
    "<body><main>",
    r"""<body><div class="app-shell"><aside class="sidebar" aria-label="Primary navigation"><a class="brand" href="#/"><span class="brand-mark">P</span><span>Pursers Fleet</span></a><nav class="primary-nav"><a data-nav="overview" href="#/"><span class="nav-icon">⌂</span>Overview</a><a data-nav="boards" href="#/boards"><span class="nav-icon">▦</span>Boards</a><a data-nav="agents" href="#/agents"><span class="nav-icon">◎</span>Agents</a><a data-nav="operations" href="#/operations"><span class="nav-icon">⚙</span>Operations</a></nav><div class="sidebar-foot">Loopback control plane<br>bounded reads · guarded writes</div></aside><main>""",
    1,
).replace("</dialog></main><script>", "</dialog></main></div><script>", 1)

HTML = HTML.replace(
    "</script></body>",
    r"""
</script><script>
const legacyRouteV1=route,legacyRefreshCentralV1=refreshCentral;
const hubKinds=new Set(['boards','agents','operations']);
let hubWorkers={},hubOverhead={},hubGuide=null,hubExtrasBusy=false;
route=function(){const special=location.hash.match(/^#\/(boards|agents|operations)$/);return special?{kind:special[1]}:legacyRouteV1()};
function navKind(){const r=route();if(!r)return'overview';if(hubKinds.has(r.kind))return r.kind;if(r.kind==='board')return'boards';if(r.kind==='workers')return'agents';return'operations'}
function syncNav(){const kind=navKind();for(const link of document.querySelectorAll('[data-nav]')){const active=link.dataset.nav===kind;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current')}}
function pageHead(kicker,title,copy,action=''){return `<header class="page-head"><div><p class="eyebrow">${esc(kicker)}</p><h2>${esc(title)}</h2><p class="muted">${esc(copy)}</p></div>${action}</header>`}
function numberCount(value){const parsed=Number(String(value??0).replace('>=',''));return Number.isFinite(parsed)?parsed:0}
function renderOverview(){const centrals=centralLabels.map(label=>{const d=fleetData[label],error=fleetErrors[label];if(!d)return `<article class="health-card"><div class="signal"><span class="signal-dot bad"></span><b>${esc(label)}</b></div><p class="error">${esc(error||'Connecting…')}</p></article>`;const s=d.pool_summary||{},heartbeat=(d.boards||[]).map(b=>b.coordinator_heartbeat).filter(Boolean).sort().at(-1),tc={open:0,claimed:0,submitted:0,closed_today:0};for(const b of d.boards||[])for(const k in tc)tc[k]+=(b.counts||{})[k]||0;return `<article class="health-card"><div class="signal"><span class="signal-dot"></span><b>${esc(label)}</b><span class="status">central up</span></div><p class="meta">Coordinator heartbeat ${esc(heartbeat?fmt(heartbeat):'not observed')}</p><div class="health-metrics"><span>Busy<b>${esc(s.busy||0)}</b></span><span>Ready<b>${esc(s.available||0)}</b></span><span>Stale<b>${esc(s.stale||0)}</b></span></div><div class="health-metrics"><span>Open<b>${esc(tc.open)}</b></span><span>Claimed<b>${esc(tc.claimed)}</b></span><span>Submitted${tc.submitted?' ⚠':''}<b>${esc(tc.submitted)}</b></span><span>Closed today<b>${esc(tc.closed_today)}</b></span></div></article>`}).join('');const findings=[],starved=[],pressure=[];for(const [central,d] of Object.entries(fleetData)){for(const b of d.boards||[]){for(const f of b.coordinator_findings?.items||[])findings.push({...f,central,board:b});for(const t of b.tickets||[]){const age=Date.now()-new Date(t.updated_at||Date.now()).getTime();if(t.status==='open'&&age>1800000)starved.push({...t,central,board:b,age})}}for(const p of hubOverhead[central]?.sessions||[])if(p.pressure!=='ok')pressure.push({...p,central})}findings.sort((a,b)=>(b.level==='critical')-(a.level==='critical'));starved.sort((a,b)=>b.age-a.age);pressure.sort((a,b)=>(b.pressure_rank||0)-(a.pressure_rank||0));const attention=[...findings.slice(0,5).map(f=>`<div class="finding-row"><span class="severity ${esc(f.level)}"></span><div><b>${esc(f.kind)}</b><p>${esc(f.text)}</p><span class="meta">${esc(f.central)} · ${esc(f.board.label)}</span></div>${f.ticket_id?`<a class="id" href="${ticketHref(f.central,f.board.board_id,f.ticket_id)}">${esc(f.ticket_id)}</a>`:''}</div>`),...pressure.slice(0,4).map(p=>`<div class="finding-row"><span class="severity ${p.pressure==='compact'?'critical':''}"></span><div><b>Context ${esc(p.pressure)}</b><p>${esc(p.agent_name)} · ${esc(p.latest_estimated_tokens)} tokens / poll</p></div><a href="${centralHref(p.central,'overhead')}">Inspect</a></div>`),...starved.slice(0,5).map(t=>`<div class="finding-row"><span class="severity"></span><div><b>Starved ticket</b><p>${esc(t.title)}</p><span class="meta">${esc(t.central)} · ${esc(t.board.label)}</span></div><a class="id" href="${ticketHref(t.central,t.board.board_id,t.id)}">${esc(t.id)}</a></div>`)].slice(0,10);return `${pageHead('Home','Fleet overview','Health and attention across every central.')}<section class="health-grid">${centrals||'<div class="skeleton"></div>'}</section><div class="section-title"><h3>Needs attention</h3><span class="status">${attention.length} surfaced</span></div><section class="attention-card">${attention.join('')||'<p class="empty">Nothing needs attention. The fleet is calm.</p>'}</section>`}
function renderBoardsHub(){const cards=[];for(const [central,d] of Object.entries(fleetData))for(const b of d.boards||[]){const total=Object.values(b.counts||{}).reduce((sum,v)=>sum+numberCount(v),0);cards.push(`<article class="board-card"><div><p class="eyebrow">${esc(central)}</p><h3>${esc(b.label)}</h3><span class="meta">${esc(b.board_id)} · ${esc(total)} visible tickets</span></div><div class="counts">${Object.entries(b.counts||{}).map(([k,v])=>`<span class="pill">${esc(k.replace('_',' '))} <b>${esc(v)}</b></span>`).join('')}</div><div class="card-actions"><a class="primary-action" href="${boardHref(central,b.board_id)}">Workspace</a><a href="${boardHref(central,b.board_id,'flow')}">Flow</a><a href="${boardHref(central,b.board_id,'timeline')}">Timeline</a><a href="${boardHref(central,b.board_id,'changes')}">Changes</a><a href="${boardHref(central,b.board_id,'routes')}">Routes</a></div></article>`)}return `${pageHead('Boards','Board workspaces','Open one board, then move through tickets, findings, intake, flow, timeline, changes, and routes.')}<section class="boards-list">${cards.join('')||'<div class="skeleton"></div>'}</section>`}
function workerByName(central,name){return (hubWorkers[central]?.workers||[]).find(w=>w.name===name)}
function renderRoleChips(seats,fallback){const roles=[];for(const s of seats||[]){if(!s.role||!s.board_id)continue;roles.push({board_id:s.board_id,role:s.role})}if(!roles.length)return esc(fallback||'worker');const unique=new Set(roles.map(r=>r.role));if(unique.size===1){const role=roles[0].role,boards=roles.map(r=>esc(r.board_id)).join(', ');return '<span class="role-chip" title="'+esc(role)+' on '+boards+'">'+esc(role)+' (all boards)</span>'}return roles.map(r=>'<span class="role-chip" title="'+esc(r.board_id)+': '+esc(r.role)+'"><span class="chip-board">'+esc(r.board_id)+'</span>: <span class="chip-role">'+esc(r.role)+'</span></span>').join('')}
function liveAgentCard(central,a){const managed=workerByName(central,a.agent_name),liveWork=agentLiveWork(a),managedWork=managed?.current_work||[],work=[...liveWork,...managedWork.filter(x=>!liveWork.some(s=>s.board_id===x.board_id&&s.current_ticket_id===x.ticket_id))];return `<article class="agent-card"><div class="agent-card-head"><div><span class="agent-role">${renderRoleChips(a.seats,managed?.role||'worker')}</span><h3>${esc(a.agent_name)}</h3><span class="meta">${esc(central)} · ${esc((a.boards||[]).join(', '))}</span></div><div class="agent-card-state"><span class="status">${esc(a.pool_status)}</span><span class="meta">${esc(relativeAge(a.last_seen))}</span></div></div>${work.length?work.map(s=>`<div class="work-row"><span class="severity"></span><div>${agentTicketLink(central,s)}<span class="meta">${esc(s.project||`${s.role||managed?.role||'worker'} · ${s.board_id}`)}</span></div></div>`).join(''):'<p class="empty">ว่าง/idle</p>'}${managed?managedControls(central,managed,false):'<span class="meta">Live pool seat · not locally managed</span>'}</article>`}
function managedControls(central,w,includeWork=true){const p=w.pressure,work=w.current_work||[],logs=w.log_tail||[];return `<div class="pressure-line">${p?pressureBadge(p):'<span class="status">pressure unavailable</span>'}<span class="meta">${p?`${esc(p.latest_estimated_tokens)} tokens / poll`:'No local sample'}</span></div>${includeWork?work.map(x=>`<div class="work-row"><span class="severity"></span><div><b>${esc(x.ticket_title||x.ticket_id)}</b><span class="meta">${esc(x.role||w.role)} · ${esc(x.board_id)}</span></div><span class="id">${esc(x.ticket_id)}</span></div>`).join(''):''}<div class="agent-actions"><button data-hub-agent-action="test" data-central="${esc(central)}" data-name="${esc(w.name)}">Test</button><button data-hub-agent-action="start" data-central="${esc(central)}" data-name="${esc(w.name)}" ${w.running?'disabled':''}>Start</button><button data-hub-agent-action="stop" data-central="${esc(central)}" data-name="${esc(w.name)}" ${w.running?'':'disabled'}>Stop</button><button data-hub-agent-action="restart" data-central="${esc(central)}" data-name="${esc(w.name)}" ${w.running?'':'disabled'}>Restart</button>${w.seat_exists?'':`<button data-hub-copy="${esc(w.seat_admin_command)}">Copy seat command</button>`}</div><details><summary>Log tail · last 20 lines</summary><pre class="log-tail">${esc(logs.join('\n')||'No log output yet.')}</pre></details>`}
function renderGuide(){if(!hubGuide)return'';return `<section class="card pool"><div class="section-title"><h3>Finish ${esc(hubGuide.name)}</h3><span class="status">2 steps</span></div><div class="guide"><div class="guide-step"><b>1 · Provision seat</b><p class="muted">Run once, copy it, then Refresh to auto-detect.</p><code>${esc(hubGuide.seat_admin_command||'Seat already detected.')}</code>${hubGuide.seat_exists?'':'<button type="button" class="button" data-hub-copy="'+esc(hubGuide.seat_admin_command)+'">Copy command</button>'}</div><div class="guide-step"><b>2 · Start agent</b><p class="muted">Start unlocks after the seat and token are detected.</p><button type="button" class="primary-action" data-hub-agent-action="start" data-central="${esc(hubGuide.central)}" data-name="${esc(hubGuide.name)}" ${hubGuide.seat_exists?'':'disabled'}>Start</button></div></div></section>`}
function renderAgentsHub(){const records=[],seen=new Set();for(const [central,d] of Object.entries(fleetData)){for(const a of d.agents||[]){records.push({central,agent:a});seen.add(`${central}/${a.agent_name}`)}for(const w of hubWorkers[central]?.workers||[])if(!seen.has(`${central}/${w.name}`)){const work=w.current_work||[];records.push({central,agent:{agent_name:w.name,pool_status:work.length?'busy':w.running?'available':'stale',boards:[...new Set(work.map(x=>x.board_id).filter(Boolean))],seats:[],last_seen:w.last_seen||null}})}}const cards=records.filter(x=>showStaleAgents||x.agent.pool_status==='busy'||x.agent.pool_status==='available').sort((a,b)=>compareAgents(a.agent,b.agent)||a.central.localeCompare(b.central)).map(x=>liveAgentCard(x.central,x.agent)),action=`<div class="agent-actions">${agentVisibilityToggle()}<button id="new-agent" class="primary-action" type="button">+ New agent</button></div>`;return `${pageHead('Agents','Unified agent pool','Live workers, reviewers, local API agents, claims, pressure, controls, and bounded logs.',action)}${renderGuide()}<p id="hub-agent-status" class="muted"></p><section class="agent-grid">${cards.join('')||`<p class="empty">${showStaleAgents?'No agents available.':'No active agents available.'}</p>`}</section>`}
function renderOperationsHub(){const cards=centralLabels.map(central=>{const d=fleetData[central],routes=(d?.boards||[]).map(b=>`<a href="${boardHref(central,b.board_id,'routes')}">${esc(b.label)} routes</a>`).join('');return `<article class="ops-card"><p class="eyebrow">${esc(central)}</p><h3>Control plane</h3><p class="muted">Policy, protocol pressure, and provenance routes.</p><div class="card-actions"><a class="primary-action" href="${centralHref(central,'config')}">Config</a><a href="${centralHref(central,'overhead')}">Overhead</a></div><div class="route-links">${routes||'<span class="empty">Boards loading…</span>'}</div></article>`}).join('');return `${pageHead('Operations','Operations','Coordinator policy, overhead details, and routes—without expanding the board write surface.')}<section class="ops-grid">${cards||'<div class="skeleton"></div>'}</section><section class="card pool"><h3>Guardrails</h3><p class="muted">Board writes remain exactly <code>coordinator_config</code> and <code>coordinator_intake</code>. Agent actions stay local under <code>/api/workers</code>.</p></section>`}
function renderHub(){const host=document.querySelector('#central-sections'),kind=navKind();if(!['overview','boards','agents','operations'].includes(kind))return;host.innerHTML=kind==='boards'?renderBoardsHub():kind==='agents'?renderAgentsHub():kind==='operations'?renderOperationsHub():renderOverview();const newest=Object.values(fleetData).map(d=>d.generated_at).sort().at(-1);document.querySelector('#state').textContent=newest?`Updated ${fmt(newest)}`:'Connecting to centrals…';bindInteractive(host);bindHub();renderSearchResults();syncNav()}
renderFleet=renderHub;
refreshCentral=async function(...args){await legacyRefreshCentralV1(...args);if(hubKinds.has(route()?.kind))renderHub()};
async function refreshHubExtras(){if(hubExtrasBusy||!centralLabels.length)return;hubExtrasBusy=true;try{await Promise.allSettled(centralLabels.flatMap(central=>[fetchJson(`/api/workers?${apiCentral(central)}`).then(d=>hubWorkers[central]=d),fetchJson(`/api/overhead?${apiCentral(central)}`).then(d=>hubOverhead[central]=d)]));if(hubGuide){const detected=workerByName(hubGuide.central,hubGuide.name);if(detected)hubGuide={...detected,central:hubGuide.central}}if(['overview','agents'].includes(navKind()))renderHub()}finally{hubExtrasBusy=false}}
function bindHub(){document.querySelector('#new-agent')?.addEventListener('click',openAgentDialog);const host=document.querySelector('#central-sections');host.onclick=hubClick}
function openAgentDialog(){let dialog=document.querySelector('#agent-dialog');if(!dialog){document.body.insertAdjacentHTML('beforeend',`<dialog id="agent-dialog" class="agent-dialog"><div class="dialog-head"><div><p class="eyebrow">Local agent</p><h2>New API agent</h2></div><button class="dialog-close" type="button" aria-label="Close">×</button></div><div class="dialog-body"><form id="agent-dialog-form" class="agent-form"><label>Central<select name="central">${centralLabels.map(c=>`<option>${esc(c)}</option>`).join('')}</select></label><label>Name<input name="name" pattern="[a-z0-9-]{2,32}" maxlength="32" placeholder="api-agent-1" required></label><label>Role<select name="role"></select><span id="role-note" class="meta"></span></label><label>Tier<select name="max_tier"><option value="light">Light</option><option value="standard">Standard</option><option value="heavy" selected>Heavy</option></select></label><label>Provider<select name="provider"></select></label><label>Model<input name="model" maxlength="200" placeholder="model-id" required></label><label class="wide">Base URL<input name="base_url" type="url" required></label><label class="wide">API key · stored in Keychain<input name="api_key" type="password" maxlength="8192" autocomplete="new-password"></label><button class="primary-action wide" type="submit">Save and continue</button><p id="agent-dialog-status" class="muted wide"></p></form></div></dialog>`);dialog=document.querySelector('#agent-dialog');dialog.querySelector('.dialog-close').onclick=()=>dialog.close();dialog.querySelector('#agent-dialog-form').onsubmit=saveHubAgent;const centralSelect=dialog.querySelector('[name=central]');centralSelect.onchange=syncAgentDialog;dialog.querySelector('[name=provider]').onchange=syncAgentPreset}syncAgentDialog();dialog.showModal()}
function syncAgentDialog(){const form=document.querySelector('#agent-dialog-form');if(!form)return;const data=hubWorkers[form.elements.central.value]||{},roles=data.roles||['worker'];form.elements.role.innerHTML=roles.map(r=>`<option value="${esc(r)}">${esc(r)}</option>`).join('');document.querySelector('#role-note').textContent=roles.includes('reviewer')?'Reviewer runtime available.':'Worker-only until reviewer runtime is installed.';form.elements.provider.innerHTML=workerPresetOptions(data.presets||{});syncAgentPreset()}
function syncAgentPreset(){const form=document.querySelector('#agent-dialog-form');if(!form)return;const option=form.elements.provider.selectedOptions[0];form.elements.base_url.value=option?.dataset.url||'';form.elements.base_url.readOnly=!['custom','azure'].includes(form.elements.provider.value);form.elements.api_key.required=option?.dataset.key==='true';form.elements.api_key.disabled=option?.dataset.key!=='true';if(form.elements.api_key.disabled)form.elements.api_key.value=''}
async function saveHubAgent(event){event.preventDefault();const form=event.target,f=new FormData(form),central=f.get('central'),status=document.querySelector('#agent-dialog-status'),payload={name:f.get('name'),role:f.get('role'),provider:f.get('provider'),base_url:f.get('base_url'),model:f.get('model'),api_key:f.get('api_key')||'',max_tier:f.get('max_tier')};status.textContent='Saving to Keychain and local config…';try{await workerRequest('/api/workers',central,payload);form.elements.api_key.value='';hubWorkers[central]=await fetchJson(`/api/workers?${apiCentral(central)}`);const worker=workerByName(central,payload.name);hubGuide={...worker,central};document.querySelector('#agent-dialog').close();renderHub()}catch(e){form.elements.api_key.value='';status.className='error wide';status.textContent=`Save failed: ${e.message}`}}
async function hubClick(event){const visibility=event.target.closest('[data-agent-visibility-toggle]');if(visibility){showStaleAgents=!showStaleAgents;renderHub();return}const copy=event.target.closest('[data-hub-copy]');if(copy){await navigator.clipboard.writeText(copy.dataset.hubCopy);const status=document.querySelector('#hub-agent-status');if(status)status.textContent='Seat command copied.';return}const button=event.target.closest('[data-hub-agent-action]');if(!button)return;const status=document.querySelector('#hub-agent-status');button.disabled=true;if(status)status.textContent=`${button.textContent} ${button.dataset.name}…`;try{await workerRequest(`/api/workers/${encodeURIComponent(button.dataset.name)}/${button.dataset.hubAgentAction}`,button.dataset.central);hubWorkers[button.dataset.central]=await fetchJson(`/api/workers?${apiCentral(button.dataset.central)}`);if(hubGuide?.name===button.dataset.name)hubGuide={...workerByName(button.dataset.central,button.dataset.name),central:button.dataset.central};renderHub()}catch(e){button.disabled=false;if(status){status.className='error';status.textContent=`Action failed: ${e.message}`}}}
function syncHub(){const r=route(),active=!r||hubKinds.has(r.kind);if(active){document.querySelector('#home-view').hidden=false;document.querySelector('#detail-view').hidden=true;document.querySelector('#config-view').hidden=true;document.querySelector('#workers-view').hidden=true;renderHub();refreshHubExtras()}syncNav()}
const legacySyncRouteHubV1=syncRoute;
syncRoute=function(){const r=route();if(hubKinds.has(r?.kind)){syncHub();return}legacySyncRouteHubV1()};
window.addEventListener('hashchange',syncHub);syncHub();setInterval(refreshHubExtras,5000);
</script></body>""",
)

# Seat setup is a fifth top-level surface. It uses only local, secret-safe APIs.
HTML = HTML.replace(
    '<a data-nav="operations" href="#/operations"><span class="nav-icon">⚙</span>Operations</a>',
    '<a data-nav="operations" href="#/operations"><span class="nav-icon">⚙</span>Operations</a><a data-nav="seats" href="#/seats"><span class="nav-icon">⌘</span>Config</a>',
    1,
).replace(
    "</style>",
    r"""
.seat-toolbar,.seat-actions{display:flex;gap:7px;flex-wrap:wrap}.seat-layout{display:grid;grid-template-columns:minmax(0,2fr) minmax(290px,1fr);gap:16px}.seat-stack{display:grid;gap:16px}.seat-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.seat-form label{display:grid;gap:5px}.seat-form input,.seat-form select,.seat-form button,.seat-actions button,.seat-toolbar button{background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px}.seat-form .wide{grid-column:1/-1}.seat-diff{max-height:360px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px}.restart-badge{color:var(--warn);font-weight:750}.doctor-check{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-top:1px solid var(--line)}
@media(max-width:1000px){.seat-layout{grid-template-columns:1fr}}@media(max-width:600px){.seat-form{grid-template-columns:1fr}.seat-form .wide{grid-column:auto}.primary-nav{grid-template-columns:repeat(5,1fr)}}
</style>""",
    1,
)
HTML = HTML.replace(
    "</script></body>",
    r"""
</script><script>
hubKinds.add('seats');
const seatRouteV1=route,seatRenderHubV1=renderHub;
let seatData={seats:[],discovered_configs:[]},seatBridge={},seatRegistry={boards:[]},seatPlan=null,seatDoctorResult=null,seatActionMessage='',seatSessionPrompt='';
route=function(){return location.hash==='#/seats'?{kind:'seats'}:seatRouteV1()};
function seatPayload(form){const f=new FormData(form);return{host:f.get('host'),role:f.get('role'),name:f.get('name'),central_url:f.get('central_url'),home_board:f.get('home_board'),token_file:f.get('token_file'),ca_file:f.get('ca_file'),bridge_command:f.get('bridge_command'),config_path:f.get('config_path'),seat_dir:f.get('seat_dir')||null,repository:f.get('repository')||null}}
function seatForm(record={}){return `<form id="seat-wizard" class="seat-form"><label>Host<select name="host">${['codex','codex-cli','goose','claude-code','claude-desktop','headless'].map(x=>`<option ${record.host===x?'selected':''}>${esc(x)}</option>`).join('')}</select></label><label>Role<select name="role"><option ${record.role==='worker'?'selected':''}>worker</option><option ${record.role==='reviewer'?'selected':''}>reviewer</option><option ${record.role==='orchestrator'?'selected':''}>orchestrator</option></select></label><label>Name<input name="name" value="${esc(record.name||'')}" pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,79}" required></label><label>Home board<input name="home_board" value="${esc(record.home_board||'pursers')}" required></label><label class="wide">Central URL<input name="central_url" type="url" value="${esc(record.central_url||'https://127.0.0.1:8766/mcp')}" required></label><label class="wide">Token file path · token never enters this page<input name="token_file" value="${esc(record.token_file||'')}" required></label><label class="wide">CA file path<input name="ca_file" value="${esc(record.ca_file||'')}" required></label><label class="wide">Bridge command<input name="bridge_command" value="${esc(record.bridge_command||seatBridge.command||'pursers-wait-bridge')}" required></label><label class="wide">Host config path<input name="config_path" value="${esc(record.config_path||'')}" required></label><label>Seat directory (Goose)<input name="seat_dir" value="${esc(record.seat_dir||'')}"></label><label>Repository (optional)<input name="repository" value="${esc(record.repository||'')}"></label><button class="primary-action wide" type="submit">Preview exact changes</button><p id="seat-form-status" class="muted wide">Paths are checked locally; token contents are never read into the browser.</p></form>`}
function seatRows(){const configured=(seatData.seats||[]).map(s=>`<tr><td><b>${esc(s.host)}</b><div class="meta">${esc(s.role)}</div></td><td><span class="id">${esc(s.name)}</span><div class="meta">${esc(s.principal_label)}</div></td><td>${esc(seatBridge.installed_version||'not installed')}<div class="meta">pinned ${esc(s.bridge_version||seatBridge.pinned_version||'unknown')}</div></td><td>${esc(s.profile?.host_timeout_s||'—')}s / ${esc(s.profile?.block_s||'—')}s<div class="meta">push ${s.push_mode===null?'unknown':s.push_mode?'yes':'no'}</div></td><td><span class="status">${esc(s.doctor_status||'not run')}</span>${s.needs_restart?'<div class="restart-badge">NEEDS RESTART</div>':''}</td><td><div class="seat-actions"><button data-seat-action="doctor" data-name="${esc(s.name)}">Doctor</button><button data-seat-action="fix" data-name="${esc(s.name)}">Fix</button><button data-seat-action="prompt" data-name="${esc(s.name)}">Copy prompt</button><button data-seat-action="upgrade">Upgrade bridge</button>${s.host==='goose'?`<button data-seat-action="goose" data-name="${esc(s.name)}">Regenerate Goose</button>`:''}</div></td></tr>`).join('');const discovered=(seatData.discovered_configs||[]).map(s=>`<tr><td><b>${esc(s.host)}</b><div class="meta">discovered</div></td><td><span class="muted">Not inventoried</span><div class="meta">${esc(s.config_path)}</div></td><td>${esc(seatBridge.installed_version||'not installed')}<div class="meta">pinned ${esc(seatBridge.pinned_version||'unknown')}</div></td><td>—</td><td><span class="status">setup needed</span></td><td><button data-seat-action="discover" data-host="${esc(s.host)}" data-path="${esc(s.config_path)}">Use in wizard</button></td></tr>`).join('');return configured+discovered}
function renderSeats(){const installed=seatBridge.installed_version||'not installed',latest=seatBridge.latest_pypi_version||'unavailable',source=seatBridge.resolution_source||'unresolved',bridgeStatus=seatBridge.status||'unknown',bridgeMessage=seatBridge.message||'';return `${pageHead('Config','Seats and host setup','Configure local seats, verify push-wait health, and upgrade the pinned bridge.','<div class="seat-toolbar"><button data-seat-global="doctor">Doctor all</button><button data-seat-global="install">Install / upgrade bridge</button><button data-seat-global="upgrade-all">Upgrade all seats</button></div>')}${seatActionMessage?`<p class="status">${esc(seatActionMessage)} ${seatSessionPrompt?'<button id="copy-session-prompt">Copy session prompt</button>':''}</p>`:''}<div class="seat-layout"><div class="seat-stack"><section class="card pool"><div class="section-title"><h3>Seat inventory</h3><span class="status">${(seatData.seats||[]).length} configured</span></div><div class="table-scroll"><table><thead><tr><th>Host / role</th><th>Name / principal</th><th>Bridge</th><th>Profile / push</th><th>Doctor</th><th>Actions</th></tr></thead><tbody>${seatRows()||'<tr><td colspan="6" class="empty">No configured seats. Use the wizard.</td></tr>'}</tbody></table></div></section><section class="card pool"><h3>Add or update seat</h3>${seatForm()}</section><section id="seat-plan" class="card pool" ${seatPlan?'':'hidden'}><h3>Confirm changes</h3><p class="muted">Review the diff before applying. Existing files are backed up first.</p><pre class="seat-diff">${esc((seatPlan?.changes||[]).map(c=>`${c.description}\n${c.diff||c.action}`).join('\n')||'No changes required.')}</pre><button id="seat-apply" class="primary-action" ${seatPlan?'':'disabled'}>Confirm and apply</button></section></div><aside class="seat-stack"><section class="card pool"><h3>Wait bridge</h3><p><span class="status">${esc(bridgeStatus)}</span>${bridgeMessage?` ${esc(bridgeMessage)}`:''}</p><p>Installed <b>${esc(installed)}</b></p><p class="meta">Pinned ${esc(seatBridge.pinned_version||'unknown')} · PyPI latest ${esc(latest)}</p><p class="meta">Resolved via ${esc(source)}</p><p class="meta">Private CA ${seatBridge.private_ca_active?'active':'not active'}</p></section><section class="card pool"><h3>Doctor</h3><div id="seat-doctor">${seatDoctorResult?doctorResult(seatDoctorResult):'<p class="muted">Run one seat or all seats for config, timeout, token/CA path, bridge, live push, and restart checks.</p>'}</div></section><section class="card pool"><h3>Registry coverage</h3>${(seatRegistry.boards||[]).map(b=>`<div class="doctor-check"><span>${esc(b.label||b.board_id)}</span><span class="status">${esc(b.seat_coverage)}/${esc(b.configured_seats)}</span></div>`).join('')||'<p class="muted">No boards loaded.</p>'}<p class="meta">Read-only registry view.</p></section></aside></div>`}
async function configPost(path,payload={}){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),body=await response.json();if(!response.ok)throw new Error(body.error||`HTTP ${response.status}`);return body}
async function refreshSeats(){try{const [inventory,bridge]=await Promise.all([fetchJson('/api/config/seats'),fetchJson('/api/config/bridge')]);seatData=inventory;seatBridge=bridge;if(navKind()==='seats')renderHub();const central=centralLabels[0];if(central)try{seatRegistry=await fetchJson(`/api/config/registry?${apiCentral(central)}`)}catch(_error){seatRegistry={boards:[],unavailable:true}}if(navKind()==='seats')renderHub()}catch(e){if(navKind()==='seats')document.querySelector('#central-sections').innerHTML=`<p class="error">Config unavailable: ${esc(e.message)}</p>`}}
function findSeat(name){return (seatData.seats||[]).find(s=>s.name===name)}
function doctorResult(result){if(!result?.seats)return `<pre class="seat-diff">${esc(JSON.stringify(result,null,2))}</pre>`;return result.seats.map(s=>`<div><b>${esc(s.seat)} · ${esc(s.overall)}</b>${(s.checks||[]).map(c=>`<div class="doctor-check"><span>${esc(c.check)}<span class="meta">${esc(c.message)}</span></span><span class="status">${esc(c.status)}</span></div>`).join('')}${s.overall==='PASS'?'':'<p class="muted">Fix hint: open this seat with Fix, review the plan, then apply and restart the host if prompted.</p>'}</div>`).join('')}
async function watchConfigJob(job){const host=document.querySelector('#seat-doctor');if(host)host.innerHTML=`<p class="muted">Job ${esc(job.job_id)} running…</p>`;const timer=setInterval(async()=>{try{const state=await fetchJson(`/api/config/jobs/${job.job_id}`);if(state.status==='queued'||state.status==='running')return;clearInterval(timer);if(state.status==='succeeded')seatDoctorResult=state.result;if(host)host.innerHTML=state.status==='succeeded'?doctorResult(state.result):`<p class="error">Job failed: ${esc(state.error)}</p>`;await refreshSeats()}catch(e){clearInterval(timer);if(host)host.innerHTML=`<p class="error">Job polling failed: ${esc(e.message)}</p>`}},1000)}
async function seatSubmit(event){event.preventDefault();const status=document.querySelector('#seat-form-status');status.textContent='Planning…';try{seatPlan=await configPost('/api/config/plan',seatPayload(event.target));renderSeats();const note=document.querySelector('#seat-plan .muted');note.textContent=`Token file ${seatPlan.token_file_exists?'found':'missing'} · CA file ${seatPlan.ca_file_exists?'found':'missing'}. Review before apply.`;bindSeats()}catch(e){status.className='error wide';status.textContent=`Plan failed: ${e.message}`}}
async function applySeat(){if(!seatPlan)return;try{const result=await configPost('/api/config/apply',{plan_id:seatPlan.plan_id});seatPlan=null;seatSessionPrompt=result.prompt||'';seatActionMessage=`${result.restart_prompt} Backups: ${(result.backup_paths||[]).join(', ')||'none'}`;await refreshSeats()}catch(e){seatSessionPrompt='';seatActionMessage=`Apply failed: ${e.message}`;renderHub()}}
async function seatClick(event){const button=event.target.closest('[data-seat-action]');if(!button)return;const action=button.dataset.seatAction;if(action==='discover'){document.querySelector('#seat-wizard').outerHTML=seatForm({host:button.dataset.host,config_path:button.dataset.path});bindSeats();document.querySelector('#seat-wizard').scrollIntoView();return}const seat=findSeat(button.dataset.name);if(action==='doctor')return watchConfigJob(await configPost('/api/config/doctor',{names:[seat.name]}));if(action==='upgrade')return watchConfigJob(await configPost('/api/config/bridge/install',{}));if(action==='prompt'){const result=await configPost('/api/config/prompt',seat);await navigator.clipboard.writeText(result.prompt);button.textContent='Copied';return}if(action==='fix'||action==='goose'){document.querySelector('#seat-wizard').outerHTML=seatForm(seat);bindSeats();document.querySelector('#seat-wizard').requestSubmit()}}
async function seatGlobal(event){const action=event.target.closest('[data-seat-global]')?.dataset.seatGlobal;if(!action)return;const routes={doctor:['/api/config/doctor',{}],install:['/api/config/bridge/install',{}],'upgrade-all':['/api/config/bridge/upgrade-all',{}]};const [path,payload]=routes[action];watchConfigJob(await configPost(path,payload))}
function bindSeats(){document.querySelector('#seat-wizard')?.addEventListener('submit',seatSubmit);document.querySelector('#seat-apply')?.addEventListener('click',applySeat);document.querySelector('#copy-session-prompt')?.addEventListener('click',async event=>{await navigator.clipboard.writeText(seatSessionPrompt);event.target.textContent='Copied'});const host=document.querySelector('#central-sections');host.onclick=seatClick;host.querySelector('.page-head')?.addEventListener('click',seatGlobal)}
renderHub=function(){if(navKind()!=='seats')return seatRenderHubV1();const host=document.querySelector('#central-sections');host.innerHTML=renderSeats();document.querySelector('#state').textContent='Local configuration';bindInteractive(host);bindSeats();syncNav()};
window.addEventListener('hashchange',()=>{if(navKind()==='seats')refreshSeats()});if(navKind()==='seats')refreshSeats();
</script></body>""",
)


HTML = HTML.replace(
    "</script></body>",
    r"""
</script><script>
let dispatchData={},initialSeatsRefreshPending=true;
seatPayload=function(form){const f=new FormData(form);return{host:f.get('host'),role:f.get('role'),name:f.get('name'),central_url:f.get('central_url'),home_board:f.get('home_board'),token_file:f.get('token_file'),ca_file:f.get('ca_file'),bridge_command:f.get('bridge_command'),config_path:f.get('config_path'),seat_dir:f.get('seat_dir')||null,repository:f.get('repository')||null,tier_max:Number(f.get('tier_max')),skills:String(f.get('skills')||'').split(',').map(x=>x.trim()).filter(Boolean),can_review:f.get('can_review')==='on',can_work:f.get('can_work')==='on',model:f.get('model')||null,provider:f.get('provider')||null}}
seatForm=function(record={}){const role=record.role||'worker',tier=record.tier_max||2,review=record.can_review??(role==='reviewer'),work=record.can_work??(role==='worker');return `<form id="seat-wizard" class="seat-form"><label>Host<select name="host">${['codex','codex-cli','goose','claude-code','claude-desktop','headless'].map(x=>`<option ${record.host===x?'selected':''}>${esc(x)}</option>`).join('')}</select></label><label>Role<select name="role"><option ${role==='worker'?'selected':''}>worker</option><option ${role==='reviewer'?'selected':''}>reviewer</option><option ${role==='orchestrator'?'selected':''}>orchestrator</option><option ${role==='coordinator'?'selected':''}>coordinator</option></select></label><label>Name<input name="name" value="${esc(record.name||'')}" pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,79}" required></label><label>Home board<input name="home_board" value="${esc(record.home_board||'pursers')}" required></label><label>Tier max<select name="tier_max">${[1,2,3].map(x=>`<option value="${x}" ${tier===x?'selected':''}>${x}</option>`).join('')}</select></label><label>Skills · comma separated<input name="skills" value="${esc((record.skills||[]).join(','))}" placeholder="git,browser"></label><label><span>Review work</span><input name="can_review" type="checkbox" ${review?'checked':''}></label><label><span>Execute work</span><input name="can_work" type="checkbox" ${work?'checked':''}></label><label>Model<input name="model" value="${esc(record.model||'')}" maxlength="200"></label><label>Provider<input name="provider" value="${esc(record.provider||'')}" maxlength="200"></label><button type="button" data-seat-suggest>Suggest skills from connectors</button><span id="seat-suggestions" class="meta"></span><label class="wide">Central URL<input name="central_url" type="url" value="${esc(record.central_url||'https://127.0.0.1:8766/mcp')}" required></label><label class="wide">Token file path · token never enters this page<input name="token_file" value="${esc(record.token_file||'')}" required></label><label class="wide">CA file path<input name="ca_file" value="${esc(record.ca_file||'')}" required></label><label class="wide">Bridge command<input name="bridge_command" value="${esc(record.bridge_command||seatBridge.command||'pursers-wait-bridge')}" required></label><label class="wide">Host config path<input name="config_path" value="${esc(record.config_path||'')}" required></label><label>Seat directory (Goose)<input name="seat_dir" value="${esc(record.seat_dir||'')}"></label><label>Repository (optional)<input name="repository" value="${esc(record.repository||'')}"></label><button class="primary-action wide" type="submit">Preview exact changes</button><p id="seat-form-status" class="muted wide">Capabilities are generated in every host's managed environment block. Runtime consumption requires Dispatch Part 2, which is not yet merged.</p></form>`}
seatRows=function(){const configured=(seatData.seats||[]).map(s=>{const live=seatRegistry.seats?.[s.name]||{},offer=live.current_offer;return `<tr><td><b>${esc(s.host)}</b><div class="meta">${esc(s.role)}</div></td><td><span class="id">${esc(s.name)}</span><div class="meta">${esc(s.principal_label)}</div></td><td>tier ${esc(s.tier_max||2)} · review ${s.can_review?'yes':'no'} · work ${s.can_work===false?'no':'yes'}<div class="meta">${esc((s.skills||[]).join(', ')||'no skills')}</div></td><td><span class="status">${esc(live.status||'unknown')}</span></td><td>${offer?`<span class="id">${esc(offer.ticket_id)}</span><div class="meta">expires ${esc(fmt(offer.expires_at))}</div>`:'—'}</td><td>${esc(seatBridge.installed_version||'not installed')}<div class="meta">pinned ${esc(s.bridge_version||seatBridge.pinned_version||'unknown')}</div></td><td><div class="seat-actions"><button data-seat-action="doctor" data-name="${esc(s.name)}">Doctor</button><button data-seat-action="fix" data-name="${esc(s.name)}">Fix</button><button data-seat-action="prompt" data-name="${esc(s.name)}">Copy prompt</button><button data-seat-action="upgrade">Upgrade bridge</button>${s.host==='goose'?`<button data-seat-action="goose" data-name="${esc(s.name)}">Regenerate Goose</button>`:''}</div></td></tr>`}).join('');const discovered=(seatData.discovered_configs||[]).map(s=>`<tr><td><b>${esc(s.host)}</b><div class="meta">discovered</div></td><td><span class="muted">Not inventoried</span><div class="meta">${esc(s.config_path)}</div></td><td>—</td><td>setup needed</td><td>—</td><td>${esc(seatBridge.installed_version||'not installed')}</td><td><button data-seat-action="discover" data-host="${esc(s.host)}" data-path="${esc(s.config_path)}">Use in wizard</button></td></tr>`).join('');return configured+discovered}
function dispatchPanels(){return (seatRegistry.boards||[]).map(board=>{const data=dispatchData[board.board_id];if(!data)return `<article class="card"><h3>${esc(board.label||board.board_id)}</h3><p class="muted">Dispatch data loading…</p></article>`;if(data.error)return `<article class="card"><h3>${esc(board.label||board.board_id)} dispatch</h3><p class="error">Dispatch unavailable: ${esc(data.error)}</p></article>`;const p=data.dispatch_policy||{};return `<article class="card"><h3>${esc(board.label||board.board_id)} dispatch</h3><form class="dispatch-form" data-board="${esc(board.board_id)}"><label>Claim TTL seconds<input name="claim_ttl_s" type="number" min="1" max="86400" value="${esc(data.claim_ttl_s||900)}"></label><label>Offer TTL seconds<input name="offer_ttl_s" type="number" min="1" max="86400" value="${esc(p.offer_ttl_s||120)}"></label><label><input name="second_opinion" type="checkbox" ${p.second_opinion?'checked':''}> Second opinion</label><label><input name="fallback_broadcast" type="checkbox" ${p.fallback_broadcast?'checked':''}> Fallback broadcast</label><button type="submit">Save policy</button></form><p class="meta dispatch-status"></p><h4>Unassignable</h4>${(data.unassignable_tickets||[]).map(x=>`<p><span class="id">${esc(x.ticket_id)}</span> ${esc(x.reason)}<span class="meta"> · missing ${(x.missing||[]).map(esc).join(', ')||'unknown'}</span></p>`).join('')||'<p class="empty">None</p>'}<h4>Current offers</h4>${(data.offers||[]).map(x=>`<p><span class="id">${esc(x.ticket_id)}</span> → ${esc(x.agent_name)}<span class="meta"> · expires ${esc(fmt(x.expires_at))}</span></p>`).join('')||'<p class="empty">None</p>'}<details><summary>Recent offer timeline</summary>${(data.timeline||[]).map(x=>`<p>${esc(fmt(x.occurred_at))} · ${esc(x.kind)} · <span class="id">${esc(x.ticket_id||'—')}</span></p>`).join('')||'<p class="empty">No recent offer events.</p>'}</details></article>`}).join('')}
renderSeats=function(){const installed=seatBridge.installed_version||'not installed',latest=seatBridge.latest_pypi_version||'unavailable',source=seatBridge.resolution_source||'unresolved',bridgeStatus=seatBridge.status||'unknown',bridgeMessage=seatBridge.message||'';return `${pageHead('Config','Seats and dispatch','Declare seat capabilities, verify drift, and inspect per-board offers.','<div class="seat-toolbar"><button data-seat-global="doctor">Doctor all</button><button data-seat-global="install">Install / upgrade bridge</button><button data-seat-global="upgrade-all">Upgrade all seats</button></div>')}${seatActionMessage?`<p class="status">${esc(seatActionMessage)} ${seatSessionPrompt?'<button id="copy-session-prompt">Copy session prompt</button>':''}</p>`:''}<section class="card pool"><div class="section-title"><h3>Seat inventory</h3><span class="status">${(seatData.seats||[]).length} configured</span></div><div class="table-scroll"><table><thead><tr><th>Host / role</th><th>Name</th><th>Capabilities</th><th>State</th><th>Current offer</th><th>Bridge</th><th>Actions</th></tr></thead><tbody>${seatRows()||'<tr><td colspan="7" class="empty">No configured seats. Use the wizard.</td></tr>'}</tbody></table></div></section><div class="seat-layout"><div class="seat-stack"><section class="card pool"><h3>Add or update seat</h3>${seatForm()}</section><section id="seat-plan" class="card pool" ${seatPlan?'':'hidden'}><h3>Confirm changes</h3><p class="muted">Review the diff before applying. Existing files are backed up first.</p><pre class="seat-diff">${esc((seatPlan?.changes||[]).map(c=>`${c.description}\n${c.diff||c.action}`).join('\n')||'No changes required.')}</pre><button id="seat-apply" class="primary-action" ${seatPlan?'':'disabled'}>Confirm and apply</button></section></div><aside class="seat-stack"><section class="card pool"><h3>Wait bridge</h3><p><span class="status">${esc(bridgeStatus)}</span>${bridgeMessage?` ${esc(bridgeMessage)}`:''}</p><p>Installed <b>${esc(installed)}</b></p><p class="meta">Pinned ${esc(seatBridge.pinned_version||'unknown')} · PyPI latest ${esc(latest)} · ${esc(source)}</p></section><section class="card pool"><h3>Doctor</h3><div id="seat-doctor">${seatDoctorResult?doctorResult(seatDoctorResult):'<p class="muted">Doctor compares declared capabilities with Central.</p>'}</div></section></aside></div><section class="pool"><div class="section-title"><h3>Dispatch by board</h3><span class="status">policy · gaps · offers</span></div><div class="grid">${dispatchPanels()}</div></section>`}
refreshSeats=async function(){try{const [inventory,bridge]=await Promise.all([fetchJson('/api/config/seats'),fetchJson('/api/config/bridge')]);seatData=inventory;seatBridge=bridge;const central=centralLabels[0];if(central){initialSeatsRefreshPending=false;seatRegistry=await fetchJson(`/api/config/registry?${apiCentral(central)}`);const boards=seatRegistry.boards||[],results=await Promise.allSettled(boards.map(async b=>[b.board_id,await fetchJson(`/api/dispatch?${apiCentral(central)}&board_id=${encodeURIComponent(b.board_id)}`)]));for(let i=0;i<results.length;i++){const result=results[i],board=boards[i];if(result.status==='fulfilled')dispatchData[result.value[0]]=result.value[1];else dispatchData[board.board_id]={error:result.reason?.message||'request failed'}}}if(navKind()==='seats')renderHub()}catch(e){if(navKind()==='seats')document.querySelector('#central-sections').innerHTML=`<p class="error">Config unavailable: ${esc(e.message)}</p>`}}
const refreshCentralBeforeSeats=refreshCentral;refreshCentral=async function(...args){await refreshCentralBeforeSeats(...args);if(initialSeatsRefreshPending&&navKind()==='seats'&&centralLabels.length)await refreshSeats()}
async function suggestSeatSkills(){const form=document.querySelector('#seat-wizard'),status=document.querySelector('#seat-suggestions');try{const result=await configPost('/api/config/suggestions',seatPayload(form)),input=form.elements.skills,current=String(input.value||'').split(',').map(x=>x.trim()).filter(Boolean);input.value=[...new Set([...current,...result.skills])].join(',');status.textContent=result.skills.length?`Suggested: ${result.skills.join(', ')}`:'No mapped connectors found.'}catch(e){status.textContent=`Suggestions failed: ${e.message}`}}
async function saveDispatch(event){event.preventDefault();const form=event.target,central=centralLabels[0],board=form.dataset.board,status=form.querySelector('.dispatch-status')||form.nextElementSibling;try{dispatchData[board]=await configPost(`/api/dispatch?${apiCentral(central)}`,{board_id:board,policy:{claim_ttl_s:Number(form.elements.claim_ttl_s.value),offer_ttl_s:Number(form.elements.offer_ttl_s.value),second_opinion:form.elements.second_opinion.checked,fallback_broadcast:form.elements.fallback_broadcast.checked}});status.textContent='Policy saved.';await refreshSeats()}catch(e){status.textContent=`Save failed: ${e.message}`}}
bindSeats=function(){const wizard=document.querySelector('#seat-wizard');wizard?.addEventListener('submit',seatSubmit);if(wizard){const review=wizard.elements.can_review,work=wizard.elements.can_work,sync=()=>{const role=wizard.elements.role.value;if(!review.dataset.touched)review.checked=role==='reviewer';if(!work.dataset.touched)work.checked=role==='worker'};review.addEventListener('input',()=>review.dataset.touched='true');work.addEventListener('input',()=>work.dataset.touched='true');wizard.elements.role.addEventListener('change',sync)}document.querySelector('[data-seat-suggest]')?.addEventListener('click',suggestSeatSkills);document.querySelector('#seat-apply')?.addEventListener('click',applySeat);document.querySelector('#copy-session-prompt')?.addEventListener('click',async event=>{await navigator.clipboard.writeText(seatSessionPrompt);event.target.textContent='Copied'});document.querySelectorAll('.dispatch-form').forEach(form=>form.addEventListener('submit',saveDispatch));const host=document.querySelector('#central-sections');host.onclick=seatClick;host.querySelector('.page-head')?.addEventListener('click',seatGlobal)}
renderOverview=renderAttentionOverview;
renderBoardsHub=renderAttentionBoardsHub;
const attentionHubClickV1=hubClick;
hubClick=async function(event){const button=event.target.closest('[data-attention-action]');if(!button)return attentionHubClickV1(event);const state=loadAttentionState(),row=state[button.dataset.attentionKey];if(!row)return;if(button.dataset.attentionAction==='ack')row.acknowledged=true;else row.snooze_until=new Date(Date.now()+86400000).toISOString();await saveAttentionState(state);renderHub()};
if(navKind()==='overview')renderHub();
</script></body>""",
    1,
).replace(
    "</style>",
    ".dispatch-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.dispatch-form label{display:grid;gap:4px}.dispatch-form input,.dispatch-form button{background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:7px}</style>",
    1,
)


def make_handler(
    cache: DashboardCache,
    stats_path: str | Path | None = None,
    worker_manager: WorkerManager | None = None,
    seat_manager: SeatConfigManager | None = None,
) -> type[BaseHTTPRequestHandler]:
    selected_stats_path = (
        bridge_stats_path() if stats_path is None else Path(stats_path)
    )
    workers = worker_manager or WorkerManager()
    seats = seat_manager or SeatConfigManager()

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

    def cache_call(name: str, *args: Any, central: str | None) -> dict[str, Any]:
        method = getattr(cache, name)
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
            if route == "/":
                self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
                return
            if route == "/api/centrals":
                labels_method = getattr(cache, "labels", None)
                labels = labels_method() if callable(labels_method) else ["default"]
                body = _json_bytes({"centrals": labels, "default": labels[0]})
                self._send(200, "application/json; charset=utf-8", body)
                return
            config_job = re.fullmatch(r"/api/config/jobs/([a-f0-9]{32})", route)
            if route in {"/api/config/seats", "/api/config/bridge", "/api/attention"} or config_job:
                try:
                    if route == "/api/config/seats":
                        payload = seats.seats()
                    elif route == "/api/config/bridge":
                        payload = seats.bridge()
                    elif route == "/api/attention":
                        payload = seats.attention_state()
                    else:
                        payload = seats.job(config_job.group(1))
                except KeyError:
                    self._send(
                        404,
                        "application/json; charset=utf-8",
                        b'{"error":"job not found"}',
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - bounded type only.
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": type(exc).__name__}),
                    )
                    return
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
                except Exception as exc:  # noqa: BLE001 - return bounded HTTP error.
                    body = _json_bytes({"error": type(exc).__name__, "central": label})
                    self._send(503, "application/json; charset=utf-8", body)
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            if route == "/api/config/registry":
                try:
                    body = _json_bytes(
                        seats.registry(cache_call("get", central=central))
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
                except Exception as exc:  # noqa: BLE001 - bounded type only.
                    self._send(
                        503,
                        "application/json; charset=utf-8",
                        _json_bytes({"error": type(exc).__name__, "central": label}),
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
            config_routes = {
                "/api/config/plan",
                "/api/config/suggestions",
                "/api/config/apply",
                "/api/config/prompt",
                "/api/config/doctor",
                "/api/config/bridge/install",
                "/api/config/bridge/upgrade-all",
                "/api/dispatch",
                "/api/attention",
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
                self._send(
                    400,
                    "application/json; charset=utf-8",
                    _json_bytes({"error": "invalid body size", "central": label}),
                )
                return
            try:
                request = json.loads(self.rfile.read(length))
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
                    body = _json_bytes(seats.doctor(request.get("names")))
                elif route == "/api/config/bridge/install":
                    if request != {}:
                        raise ValueError("bridge install body must be an empty object")
                    body = _json_bytes(seats.install_bridge())
                elif route == "/api/config/bridge/upgrade-all":
                    if request != {}:
                        raise ValueError("bridge upgrade body must be an empty object")
                    body = _json_bytes(seats.upgrade_all())
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
                elif route == "/api/attention":
                    body = _json_bytes(seats.save_attention_state(request))
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
    if not args.centrals:
        return [
            Config(
                url=args.url,
                token=_token_from_args(args.token_file),
                home_board=args.home_board,
                agent_name=args.agent_name,
                stale_seconds=args.stale_seconds,
                cache_seconds=args.cache_seconds,
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
        allowed = required | {"stats_path"}
        if (
            not isinstance(entry, dict)
            or not required <= set(entry)
            or not set(entry) <= allowed
        ):
            raise SystemExit(
                f"centrals entry {index} must contain label, url, token_path, "
                "and home_board, with optional stats_path"
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
        "--centrals",
        help=(
            "0600 JSON list of {label,url,token_path,home_board[,stats_path]} entries"
        ),
    )
    parser.add_argument("--home-board", default=DEFAULT_HOME_BOARD)
    parser.add_argument("--agent-name", default="fleet-dashboard-viewer")
    parser.add_argument("--stale-seconds", type=int, default=300)
    parser.add_argument("--cache-seconds", type=float, default=5.0)
    parser.add_argument("--workers-dir", default=str(DEFAULT_WORKERS_DIR))
    parser.add_argument(
        "--seat-state-dir", default=str(CONFIG_STATE_DIR), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--worker-script", default=str(DEFAULT_WORKER_SCRIPT), help=argparse.SUPPRESS
    )
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
    configs = load_central_configs(args)
    cache = DashboardCache(
        [FleetFetcher(config) for config in configs], args.cache_seconds
    )
    worker_manager = WorkerManager(args.workers_dir, worker_script=args.worker_script)
    seat_state_dir = Path(args.seat_state_dir).expanduser()
    seat_manager = SeatConfigManager(
        seat_state_dir / "seats.json", state_dir=seat_state_dir
    )
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(cache, bridge_stats_path(), worker_manager, seat_manager),
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
