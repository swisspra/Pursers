#!/usr/bin/env python3
"""Registry-wide coordinator findings refresher and policy-gated question answerer.

The butler runs the real coordinator derivation for every active registry board
on a bounded cycle and listens for coordinator questions through the same
journal/seat resource subscriptions used by the wait bridge. Question answers
are disabled or assist-only unless a board explicitly selects ``autonomous``;
even then only deterministic, fully evidenced information answers cross the
existing coordinator binding. Its Central writes are CAS-protected findings
and per-question audit records. The other ticket mutations remain the two
explicitly configured, mechanically checkable safety actions: parking repeated
``no_live_candidates`` loops and recording refusal of an escalation target
that cannot work.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
import re
import runpy
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.error
import time
import urllib.parse
import urllib.request
from collections import deque
from contextlib import (
    AsyncExitStack,
    aclosing,
    asynccontextmanager,
    contextmanager,
    suppress,
)
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    AsyncContextManager,
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Protocol,
    Sequence,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


STATE_KEY = "coordinator_findings"
FLEET_STATE_KEY = "autonomous_butler_state"
EVALUATION_STATE_PREFIX = "board_butler_evaluation."
CONFIG_KEY = "coordinator_config"
SCHEMA_VERSION = 1
DEFAULT_URL = "http://127.0.0.1:8766/mcp"
DEFAULT_AGENT_NAME = "board-butler-1"
DEFAULT_DRAFTS_PER_HOUR = 5
DEFAULT_DRAFTS_PER_TICKET = 2
DEFAULT_DRAFTS_PER_BOARD = 20
DEFAULT_HOLD_BEFORE_POST_S = 3_600
DEFAULT_VETO_COUNT = 3
DEFAULT_FAILURE_COUNT = 3
DEFAULT_VETO_WINDOW_S = 3_600
DEFAULT_REFRESH_SECONDS = 60
DEFAULT_NO_LIVE_CANDIDATES_CYCLES = 3
DEFAULT_ACTION_HOLD_SECONDS = 60
ACTIVE_AUTHORIZATION_SCHEMA_VERSION = 1
ACTIVE_AUTHORIZATION_KEYS = {"schema_version", "mode", "authorized"}
MECHANICAL_ACTION_CLASSES = (
    "park_no_live_candidates",
    "refuse_incapable_target",
)
MAX_FINDINGS = 50
MAX_STATE_CHARS = 4_800
MAX_PROVIDER_RESPONSE_BYTES = 1_000_000
MAX_PROVIDER_DRAFT_CHARS = 2_000
MAX_PROVIDER_PROMPT_CHARS = 12_000
MAX_AUTONOMOUS_ANSWER_CHARS = 2_000
PROVIDER_TIMEOUT_S = 30.0
MAX_MODEL_RUN_SECONDS = 600.0
PROVIDER_DRAFT_PROTOCOLS = frozenset(
    {"pursers_json_v1", "openai_chat_completions_v1"}
)
SUPPORTED_MCP_PROTOCOL_REVISIONS = frozenset({"2026-07-28"})
SUPPORTED_MCP_TRANSPORTS = frozenset({"stdio", "streamable_http"})
MAX_CONNECTOR_AUDIT_DETAIL_CHARS = 1_000
QUESTION_EVENT = "coordinator_question_asked"
OBSERVATION_TICKET_LIMIT = 100
OBSERVATION_HISTORY_DAYS = 7
OBSERVATION_FINDING_KIND = "butler_observation"
PARK_ANNOTATION_MARKER = "board-butler:no-live-candidates"
REFUSAL_ANNOTATION_MARKER = "board-butler:incapable-target-refusal"
MIN_AGREEMENT_SAMPLES = 3
DRAFT_MARK_VALUES = ("send_as_is", "needed_edits", "wrong")
ROUTING_MARK_VALUES = (
    "correct_escalation",
    "should_have_answered",
    "should_have_escalated",
)
MARK_VALUES = DRAFT_MARK_VALUES + ROUTING_MARK_VALUES
RETROSPECTIVE_BACKFILL_BOARD_ID = "pursers"
RETROSPECTIVE_EVALUATION_BACKFILL: tuple[dict[str, Any], ...] = (
    {
        "question_id": "CQ-53524d65cdb51016",
        "ticket_id": "TK-02bf4d01d662",
        "question_kind": "decision",
        "draft_status": "produced",
        "decline_reason": None,
        "mark": "should_have_escalated",
    },
    {
        "question_id": "CQ-7bf548bf5e084198",
        "ticket_id": "TK-02bf4d01d662",
        "question_kind": "decision",
        "draft_status": "declined",
        "decline_reason": "historical-replay",
        "mark": "correct_escalation",
    },
    {
        "question_id": "CQ-08843e9944e1cf22",
        "ticket_id": "TK-02bf4d01d662",
        "question_kind": "decision",
        "draft_status": "declined",
        "decline_reason": "historical-replay",
        "mark": "correct_escalation",
    },
)
RETROSPECTIVE_MARKER = {
    "agent_id": "AI-3f94318ce493a803e96c3e61f12459caf5c44eff7490a1db94484d75e95ee93c",
    "agent_name": "fable5-main",
    "principal_id": "PR-5e5c0f9104864e83c2d62e0c15c6081736f230b3f2963c66d8e8f85000691b70",
}
RETROSPECTIVE_MARKED_AT = "2026-09-17T12:08:46.324109+00:00"
RETROSPECTIVE_MARK_SOURCE_QUESTION_ID = "CQ-d57827ee2d7e88ed"
# Mirrored from coordinator.DEFAULT_ALWAYS_ASK_CATEGORIES.  The policy rules
# below express these as gate/scope/release, membership, and registry hazards.
COORDINATOR_ALWAYS_ASK_CATEGORIES = (
    "production-code",
    "release-ci",
    "membership-roles",
    "board-registry",
)
AUTO_CANDIDATE_CLASSES = (
    "ancestry",
    "ticket_status",
    "seat_capability",
    "waiver_applicability",
    "corpus_lookup",
    "coverage_check",
)
NEVER_AUTO_CLASSES = (
    "scope_change",
    "gate_waiver",
    "release",
    "membership",
    "registry",
)
ANSWER_CLASSES = AUTO_CANDIDATE_CLASSES + NEVER_AUTO_CLASSES
CITABLE_EVIDENCE_KINDS = (
    "git_ancestry",
    "ticket_status",
    "annotation",
    "seat_capability",
    "manifest_coverage",
    "corpus",
)
POLICY_CLASS = {
    "gate-waiver": "gate_waiver",
    "scope-change": "scope_change",
    "release-decision": "release",
    "membership-or-registry": "registry",
    "coverage-blindness": "coverage_check",
    "production-code-authority": "scope_change",
    "git-ancestry": "ancestry",
    "ticket-status": "ticket_status",
    "annotation-coverage": "waiver_applicability",
    "seat-capability": "seat_capability",
}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Declared beside coordinator_config and deliberately narrower than arbitrary
# JSON.  Validation below enforces this schema, including nested unknown keys.
BOARD_BUTLER_CONFIG_SCHEMA: dict[str, Any] = {
    "schema_version": 1,
    "keys": ("schema_version", "global", "projects", "boards"),
    "setting_keys": (
        "mode",
        "answering_mode",
        "answer_scope",
        "required_evidence_kinds",
        "ceilings",
        "hold_before_post_s",
        "active_windows",
        "kill_switch",
        "auto_demote",
        "classification",
        "drafting",
    ),
    "precedence": ("safe_defaults", "global", "project", "board"),
    "runtime": "policy-gated",
}

# Coordinators are ineligible for work and review dispatch, so their tier does
# not affect routing.  Central nevertheless requires tier_max to be 1, 2, or 3;
# use the least permissive valid value and keep every connection consistent.
BOARD_BUTLER_CAPABILITIES: dict[str, Any] = {
    "can_work": False,
    "can_review": False,
    "tier_max": 1,
    "max_parallel": 1,
}


class Outcome(str, Enum):
    MECHANICAL = "MECHANICAL"
    ESCALATE = "ESCALATE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PolicyRule:
    name: str
    outcome: Outcome
    pattern: re.Pattern[str]
    evaluator: str | None = None


# Safety rules must remain above mechanical rules.  A message containing both
# "is this SHA merged" and "waive the gate" is an escalation, never a lookup.
POLICY_TABLE: tuple[PolicyRule, ...] = (
    PolicyRule(
        "credentials-or-secrets",
        Outcome.ESCALATE,
        re.compile(
            r"\b(?:credential(?:s)?|password(?:s)?|bearer[ -]?token|access[ -]?token|"
            r"api[ -]?key|private[ -]?key|signing[ -]?key|secret(?:s)?|"
            r"host[ -]?binding)\b",
            re.I,
        ),
    ),
    PolicyRule(
        "authority-or-budget-change",
        Outcome.ESCALATE,
        re.compile(
            r"(?=.*\b(?:change|raise|increase|lower|decrease|reduce|expand|grant|"
            r"extend|override|set|"
            r"modify|amend)\w*\b)(?=.*\b(?:authority|authorities|budget|ceiling|"
            r"concurrency|limit)\w*\b)",
            re.I | re.S,
        ),
    ),
    PolicyRule(
        "review-policy",
        Outcome.ESCALATE,
        re.compile(
            r"\b(?:review[ -]?policy|independent[ -]?review|self[ -]?review|"
            r"reviewer[ -]?(?:assignment|authority|requirement))\b",
            re.I,
        ),
    ),
    PolicyRule(
        "gate-waiver",
        Outcome.ESCALATE,
        re.compile(
            r"\b(?:waiv(?:e|er)|bypass|override|accept(?:able)?|satisfy)\b.{0,160}\b(?:gate|check|failure|requirement|required|acceptance|evidence|result|replay)\b"
            r"|\b(?:gate|check|failure|requirement|required|acceptance|evidence|result|replay)\b.{0,160}\b(?:waiv(?:e|er)|bypass|override|accept(?:able)?|satisfy)\b"
            r"|\b(?:carry(?:ing)? forward|substitut\w*|replace)\b.{0,160}\b(?:evidence|result|replay|requirement|acceptance)\b"
            r"|\b(?:evidence|result|replay|requirement|acceptance)\b.{0,160}\b(?:carry(?:ing)? forward|substitut\w*|replace)\b",
            re.I | re.S,
        ),
    ),
    PolicyRule(
        "scope-change",
        Outcome.ESCALATE,
        re.compile(r"\b(?:change|expand|reduce|amend|override)\b.{0,60}\bscope\b|\bout[- ]of[- ]scope\b", re.I),
    ),
    PolicyRule(
        "release-decision",
        Outcome.ESCALATE,
        re.compile(
            r"\b(?:should|may|can|could|please|do we|must we|ready to)\b.{0,60}\b(?:release(?!-)|publish|tag|ship|promote)\b"
            r"|\bversion bump\b"
            r"|\b(?:release(?!-)|publish|tag|ship|promote)\b.{0,60}"
            r"\b(?:now|to production|this release|package|artifact|version)\b"
            r"|\b(?:publish|release|tag)\b.{0,60}\b(?:package|artifact|version|commit|sha)\b",
            re.I | re.S,
        ),
    ),
    PolicyRule(
        "membership-or-registry",
        Outcome.ESCALATE,
        re.compile(
            r"\b(?:membership|invite|admit|retire seat|registry|register board|"
            r"project registry|change role|(?:add|remove|delete)\w*\s+(?:a\s+)?"
            r"(?:member|seat|agent|worker|reviewer))\b",
            re.I,
        ),
    ),
    PolicyRule(
        "coverage-blindness",
        Outcome.ESCALATE,
        re.compile(
            r"(?=.*\b(?:suite|tests?|manifest)\b)"
            r"(?=.*\b(?:blocked|skipped|not run|never[- ]reached|fail(?:ed|ure|ures)?|denied|gap)\b)"
            r"(?=.*\b(?:may|can|should|please|accept|authoriz\w*|proceed|approve|submit|merge|continue|treat|waiv\w*|require\w*|rerun)\b)",
            re.I | re.S,
        ),
        "coverage_blindness",
    ),
    PolicyRule(
        "production-code-authority",
        Outcome.ESCALATE,
        re.compile(
            r"\b(?:may|can|could|should|please|authorize|approve)\b.{0,100}"
            r"\b(?:merge|land|change|modify|edit|patch|write|deploy|ship)\w*\b",
            re.I | re.S,
        ),
    ),
    PolicyRule(
        "pr-review-merge",
        Outcome.ESCALATE,
        re.compile(
            r"\b(?:approve|review|merge|land)\w*\b.{0,80}\b(?:PR|pull request)\b"
            r"|\b(?:PR|pull request)\b.{0,80}\b(?:approve|review|merge|land)\w*\b",
            re.I | re.S,
        ),
    ),
    PolicyRule(
        "git-ancestry",
        Outcome.MECHANICAL,
        re.compile(r"\b(?:ancestor|descendant|merged|contained in|reachable from)\b.{0,120}\b(?:main|origin/main|[0-9a-f]{7,40})\b|\b[0-9a-f]{7,40}\b.{0,120}\b(?:ancestor|descendant|merged|contained in|reachable from)\b", re.I),
        "git_ancestry",
    ),
    PolicyRule(
        "ticket-status",
        Outcome.MECHANICAL,
        re.compile(r"\bTK-[0-9A-Za-z-]+\b.{0,100}\b(?:status|closed|open|submitted|rejected|claimed|canceled)\b|\b(?:status|closed|open|submitted|rejected|claimed|canceled)\b.{0,100}\bTK-[0-9A-Za-z-]+\b", re.I),
        "ticket_status",
    ),
    PolicyRule(
        "annotation-coverage",
        Outcome.MECHANICAL,
        re.compile(r"\b(?:AN-[0-9A-Za-z-]+|annotation)\b.{0,120}\b(?:cover|authoriz|waiver|decision|allow)\b|\b(?:cover|authoriz|waiver|decision|allow)\b.{0,120}\b(?:AN-[0-9A-Za-z-]+|annotation)\b", re.I),
        "annotation_coverage",
    ),
    PolicyRule(
        "seat-capability",
        Outcome.MECHANICAL,
        re.compile(r"\b(?:seat|agent|worker|reviewer)\b.{0,100}\b(?:sandbox|can_work|can_review|capabilit|role)\b|\b(?:sandbox|can_work|can_review|capabilit|role)\b.{0,100}\b(?:seat|agent|worker|reviewer)\b", re.I),
        "seat_capability",
    ),
)


MECHANICAL_REQUEST_PATTERNS: dict[str, re.Pattern[str]] = {
    "git-ancestry": re.compile(
        r"\s*(?:is|was)\s+(?:(?:the\s+)?mentioned\s+commit|[0-9a-f]{7,40})\s+"
        r"(?:(?:an?\s+)?(?:ancestor|descendant)\s+of|"
        r"(?:merged\s+into|contained\s+in|reachable\s+from))\s+"
        r"(?:main|origin/main|[0-9a-f]{7,40})\s*[?.]?\s*",
        re.I,
    ),
    "ticket-status": re.compile(
        r"\s*(?:(?:what\s+is|what's)\s+the\s+status\s+of\s+"
        r"TK-[0-9A-Za-z-]+|is\s+TK-[0-9A-Za-z-]+\s+"
        r"(?:closed|open|submitted|rejected|claimed|canceled))\s*[?.]?\s*",
        re.I,
    ),
    "annotation-coverage": re.compile(
        r"\s*does\s+AN-[0-9A-Za-z-]+\s+on\s+TK-[0-9A-Za-z-]+\s+"
        r"cover\s+(?:this|the)\s+(?:decision|waiver|requirement|failure)\s*[?.]?\s*",
        re.I,
    ),
    "seat-capability": re.compile(
        r"\s*is\s+(?:seat|agent|worker|reviewer)\s+`?[0-9A-Za-z_.-]+`?\s+"
        r"capable\s+of\s+(?:can_work|can_review)\s*[?.]?\s*",
        re.I,
    ),
}


@dataclass(frozen=True)
class Classification:
    outcome: Outcome
    rule: str
    evaluator: str | None = None


@dataclass(frozen=True)
class Evidence:
    kind: str
    source: str
    detail: str
    answer: str
    outcome: Outcome | None = None


class EvidenceSource(Protocol):
    async def ticket_get(self, ticket_id: str) -> Mapping[str, Any]: ...
    async def board_status(self) -> Mapping[str, Any]: ...
    async def answered_questions(self) -> Sequence[Mapping[str, Any]]: ...


class AlreadyRunning(RuntimeError):
    """Raised before any board access when the singleton is already held."""


class IdentityConflict(RuntimeError):
    """Raised when the butler shares a principal with a worker/reviewer seat."""


class ButlerConfigError(ValueError):
    """Raised when coordinator_config cannot be interpreted safely."""


class ConnectorError(RuntimeError):
    """Base class for bounded, secret-free connector failures."""


class ConnectorConfigError(ConnectorError, ValueError):
    """A connector declaration or resolved endpoint is invalid."""


class ConnectorDenied(ConnectorError):
    """The exact allowlist or deterministic policy gate denied an operation."""


class ConnectorProtocolError(ConnectorError):
    """The remote server did not satisfy the pinned MCP v2 contract."""


class ConnectorResultError(ConnectorError):
    """A connector result was malformed, unsafe, or larger than its bound."""


@dataclass(frozen=True)
class MechanicalAction:
    kind: str
    board_id: str
    ticket_id: str
    identity_name: str | None
    identity_id: str | None
    observed_cycles: int | None
    reason: str
    annotation_required: bool = True


FLEET_ROLES = ("worker", "reviewer", "acp_worker")
FLEET_LIFECYCLES = (
    "starting",
    "ready",
    "busy",
    "draining",
    "unhealthy",
    "stopped",
)
MAX_FLEET_OPERATION_HISTORY = 512
MAX_FLEET_EXPLANATIONS = 300


@dataclass(frozen=True)
class FleetRolePolicy:
    """Human-owned bounds for one role; the reconciler cannot raise them."""

    minimum: int
    target: int
    maximum: int
    backlog_per_seat: int = 1

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in (self.minimum, self.target, self.maximum, self.backlog_per_seat)
        ):
            raise ValueError("fleet role bounds are invalid")
        if not (
            0 <= self.minimum <= self.target <= self.maximum <= 100
            and self.backlog_per_seat >= 1
        ):
            raise ValueError("fleet role bounds are invalid")


@dataclass(frozen=True)
class FleetBoardPolicy:
    board_id: str
    roles: Mapping[str, FleetRolePolicy]
    board_maximum: int
    provider_maximums: Mapping[str, int]
    approved_template_ids: frozenset[str]
    idle_grace_s: int
    scale_up_cooldown_s: int
    scale_down_cooldown_s: int
    failure_backoff_s: int
    provider_latency_limit_ms: int = 30_000

    def __post_init__(self) -> None:
        if set(self.roles) != set(FLEET_ROLES):
            raise ValueError("fleet policy must bound every role")
        if not 0 <= self.board_maximum <= 300:
            raise ValueError("board fleet maximum is invalid")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in self.provider_maximums.values()
        ):
            raise ValueError("provider fleet maximum is invalid")
        if not self.approved_template_ids or any(
            not isinstance(value, str) or not value for value in self.approved_template_ids
        ):
            raise ValueError("approved fleet templates are invalid")
        if any(
            value < 0
            for value in (
                self.idle_grace_s,
                self.scale_up_cooldown_s,
                self.scale_down_cooldown_s,
            )
        ) or self.failure_backoff_s < 1:
            raise ValueError("fleet cooldown is invalid")
        if self.provider_latency_limit_ms < 1:
            raise ValueError("provider latency limit is invalid")


@dataclass(frozen=True)
class FleetHostPolicy:
    agent_process_ceiling: int
    control_plane_processes: int
    total_process_ceiling: int

    def __post_init__(self) -> None:
        if (
            self.agent_process_ceiling < 0
            or self.control_plane_processes < 0
            or self.total_process_ceiling < self.control_plane_processes
        ):
            raise ValueError("host fleet limits are invalid")

    @property
    def role_capacity(self) -> int:
        return min(
            self.agent_process_ceiling,
            self.total_process_ceiling - self.control_plane_processes,
        )


@dataclass(frozen=True)
class FleetDemand:
    """Product-produced registry projection consumed by desired-state policy."""

    board_id: str
    open_by_tier: Mapping[int, int]
    review_backlog: int
    acp_backlog: int
    oldest_ticket_age_s: int
    expiring_offers: int
    provider_health: Mapping[str, str]
    provider_latency_ms: Mapping[str, int]

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (
                *self.open_by_tier.values(),
                self.review_backlog,
                self.acp_backlog,
                self.oldest_ticket_age_s,
                self.expiring_offers,
                *self.provider_latency_ms.values(),
            )
        ):
            raise ValueError("fleet demand counts are invalid")
        if any(tier not in {0, 1, 2} for tier in self.open_by_tier):
            raise ValueError("fleet demand tier is invalid")
        if any(
            status not in {"healthy", "degraded", "unavailable", "unknown"}
            for status in self.provider_health.values()
        ):
            raise ValueError("provider health is invalid")

    @property
    def work_pressure(self) -> int:
        weights = {0: 1, 1: 2, 2: 4}
        pressure = sum(weights[tier] * count for tier, count in self.open_by_tier.items())
        # An offer near expiry and an aged queue are starvation signals, not
        # permission to exceed any human-owned maximum.
        pressure += self.expiring_offers
        if pressure and self.oldest_ticket_age_s >= 300:
            pressure += 1
        return pressure

    @property
    def has_work(self) -> bool:
        return self.work_pressure > 0 or self.review_backlog > 0 or self.acp_backlog > 0


@dataclass(frozen=True)
class FleetSeat:
    seat_id: str
    board_id: str
    role: str
    provider: str
    template_id: str
    template_digest_sha256: str
    generation: int
    lifecycle: str
    ready: bool
    busy: bool
    live_lease: bool
    transition_at: datetime
    managed: bool = True

    def __post_init__(self) -> None:
        if self.role not in FLEET_ROLES or self.lifecycle not in FLEET_LIFECYCLES:
            raise ValueError("fleet seat role or lifecycle is invalid")
        if self.generation < 1 or self.transition_at.tzinfo is None:
            raise ValueError("fleet seat generation or transition time is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.template_digest_sha256):
            raise ValueError("fleet seat template digest is invalid")

    @property
    def active(self) -> bool:
        return self.lifecycle in {"starting", "ready", "busy", "draining", "unhealthy"}


@dataclass(frozen=True)
class FleetSnapshot:
    observed_at: datetime
    demands: Mapping[str, FleetDemand]
    seats: tuple[FleetSeat, ...]
    host_load_ratio: float
    host_capacity_available: bool
    executor_healthy: bool

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or not 0 <= self.host_load_ratio <= 1:
            raise ValueError("fleet snapshot is invalid")
        if set(self.demands) != {item.board_id for item in self.demands.values()}:
            raise ValueError("fleet demand keys do not match board ids")
        # Executor inventory can include seats on another board controlled by
        # the same host. They are immutable to this plan but still consume the
        # host ceiling, so dropping them would make scale-up unsafe.


@dataclass(frozen=True)
class FleetOperation:
    operation_id: str
    board_id: str
    action: str
    seat_id: str
    template_id: str
    template_digest_sha256: str
    expected_seat_generation: int
    authorization_fingerprint_sha256: str

    def __post_init__(self) -> None:
        if self.action not in {"start", "drain", "stop"}:
            raise ValueError("fleet operation action is invalid")


@dataclass(frozen=True)
class FleetPlan:
    desired: Mapping[str, Mapping[str, int]]
    provider_desired: Mapping[str, Mapping[str, int]]
    operations: tuple[FleetOperation, ...]
    explanations: tuple[Mapping[str, Any], ...]


class FleetExecutorClient(Protocol):
    def execute(self, operation: FleetOperation) -> Mapping[str, Any]: ...


class UnixFleetExecutorClient:
    """Least-privilege signed client for the host-local fleet executor."""

    def __init__(
        self,
        socket_path: Path,
        key_id: str,
        private_key_path: Path,
        *,
        timeout_s: float = 30.0,
        max_response_bytes: int = 64 * 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not socket_path.is_absolute()
            or not private_key_path.is_absolute()
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", key_id)
            or timeout_s <= 0
            or max_response_bytes < 1
        ):
            raise ValueError("fleet executor client configuration is invalid")
        self.socket_path = socket_path
        self.key_id = key_id
        self.private_key_path = private_key_path
        self.timeout_s = timeout_s
        self.max_response_bytes = max_response_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _private_key(self) -> Ed25519PrivateKey:
        if self.private_key_path.is_symlink():
            raise RuntimeError("fleet executor signing key is a symlink")
        try:
            info = self.private_key_path.stat()
            raw = self.private_key_path.read_bytes()
        except OSError as exc:
            raise RuntimeError("fleet executor signing key is unavailable") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or len(raw) != 32
        ):
            raise RuntimeError("fleet executor signing key is not a 0600 raw Ed25519 key")
        return Ed25519PrivateKey.from_private_bytes(raw)

    @staticmethod
    def _request_digest(request: Mapping[str, Any]) -> str:
        unsigned = {key: value for key, value in request.items() if key != "caller_auth"}
        return hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def execute(self, operation: FleetOperation) -> Mapping[str, Any]:
        if self.socket_path.is_symlink():
            raise RuntimeError("fleet executor socket is a symlink")
        try:
            socket_info = self.socket_path.stat()
        except OSError as exc:
            raise RuntimeError("fleet executor socket is unavailable") from exc
        if (
            not stat.S_ISSOCK(socket_info.st_mode)
            or stat.S_IMODE(socket_info.st_mode) != 0o600
            or socket_info.st_uid != os.getuid()
        ):
            raise RuntimeError("fleet executor socket is not owner-only")
        now = self.clock().astimezone(timezone.utc)
        request: dict[str, Any] = {
            "schema": "autonomous_butler_executor_v1",
            "schema_version": 1,
            "message_type": "request",
            "operation_id": operation.operation_id,
            "board_id": operation.board_id,
            "action": operation.action,
            "seat_id": operation.seat_id,
            "template_id": operation.template_id,
            "template_digest_sha256": operation.template_digest_sha256,
            "expected_seat_generation": operation.expected_seat_generation,
            "authorization_fingerprint_sha256": (
                operation.authorization_fingerprint_sha256
            ),
            "deadline": (now + timedelta(seconds=self.timeout_s)).isoformat(),
            "caller_auth": {},
        }
        digest = self._request_digest(request)
        nonce = "nonce:" + hashlib.sha256(
            operation.operation_id.encode("utf-8")
        ).hexdigest()[:32]
        signed_at = now.isoformat()
        message = b"\0".join(
            (
                b"pursers-executor-v1",
                digest.encode("ascii"),
                self.key_id.encode("utf-8"),
                nonce.encode("utf-8"),
                signed_at.encode("utf-8"),
            )
        )
        request["caller_auth"] = {
            "scheme": "local_ed25519_v1",
            "key_id": self.key_id,
            "nonce": nonce,
            "signed_at": signed_at,
            "request_digest_sha256": digest,
            "signature_base64": base64.b64encode(
                self._private_key().sign(message)
            ).decode("ascii"),
        }
        encoded = json.dumps(
            request,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        chunks = bytearray()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout_s)
            connection.connect(os.fspath(self.socket_path))
            connection.sendall(encoded)
            while not chunks.endswith(b"\n"):
                block = connection.recv(min(4096, self.max_response_bytes + 1 - len(chunks)))
                if not block:
                    raise RuntimeError("fleet executor response ended early")
                chunks.extend(block)
                if len(chunks) > self.max_response_bytes:
                    raise RuntimeError("fleet executor response exceeded the safe bound")
        try:
            result = json.loads(bytes(chunks))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("fleet executor response is invalid") from exc
        if (
            not isinstance(result, dict)
            or result.get("schema") != "autonomous_butler_executor_v1"
            or result.get("message_type") != "result"
            or result.get("operation_id") != operation.operation_id
            or result.get("board_id") != operation.board_id
            or result.get("request_digest_sha256") != digest
            or result.get("outcome")
            not in {"succeeded", "rejected", "failed", "cancelled", "unknown"}
            or not isinstance(result.get("committed"), bool)
        ):
            raise RuntimeError("fleet executor response contract is invalid")
        return result


class FleetStateStore(Protocol):
    def load(self) -> tuple[int, Mapping[str, Any]]: ...

    def compare_and_swap(
        self, expected_revision: int, value: Mapping[str, Any]
    ) -> bool: ...

    def execute_if_current(
        self,
        config_revisions: Mapping[str, int],
        authorization_fingerprints: Mapping[str, str],
        operation: FleetOperation,
        attempted_at: datetime,
        executor: FleetExecutorClient,
    ) -> tuple[bool, Mapping[str, Any]]: ...


def _execute_fleet_operation(
    value: Mapping[str, Any],
    config_revisions: Mapping[str, int],
    authorization_fingerprints: Mapping[str, str],
    operation: FleetOperation,
    attempted_at: datetime,
    executor: FleetExecutorClient,
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    """Execute and record one operation while the caller holds the CAS lock."""
    operations = value.get("operations")
    row = (
        operations.get(operation.operation_id)
        if isinstance(operations, Mapping)
        else None
    )
    current_revisions = value.get("config_revisions")
    current_fingerprints = value.get("authorization_fingerprints")
    expected_row = {
        "operation_id": operation.operation_id,
        "board_id": operation.board_id,
        "action": operation.action,
        "seat_id": operation.seat_id,
        "template_id": operation.template_id,
        "template_digest_sha256": operation.template_digest_sha256,
        "expected_seat_generation": operation.expected_seat_generation,
        "authorization_fingerprint_sha256": operation.authorization_fingerprint_sha256,
        "status": "pending",
    }
    if (
        not isinstance(current_revisions, Mapping)
        or dict(current_revisions) != dict(config_revisions)
        or not isinstance(current_fingerprints, Mapping)
        or dict(current_fingerprints) != dict(authorization_fingerprints)
        or not isinstance(row, Mapping)
        or any(row.get(key) != expected for key, expected in expected_row.items())
    ):
        return False, copy.deepcopy(dict(value)), {}
    try:
        receipt = dict(executor.execute(operation))
        if (
            receipt.get("operation_id") != operation.operation_id
            or receipt.get("outcome")
            not in {"succeeded", "rejected", "failed", "cancelled", "unknown"}
            or not isinstance(receipt.get("committed"), bool)
        ):
            raise RuntimeError("executor receipt contract mismatch")
    except Exception as exc:
        receipt = {
            "operation_id": operation.operation_id,
            "outcome": "unknown",
            "committed": False,
            "reason_code": type(exc).__name__.casefold(),
        }
    updated = copy.deepcopy(dict(value))
    operation_state = updated["operations"]
    completed = dict(operation_state[operation.operation_id])
    completed["attempts"] = int(completed.get("attempts", 0) or 0) + 1
    completed["last_attempt_at"] = attempted_at.isoformat()
    completed["status"] = (
        "terminal"
        if receipt["outcome"] in {"succeeded", "rejected", "failed", "cancelled"}
        else "unknown"
    )
    completed["outcome"] = receipt["outcome"]
    completed["committed"] = receipt.get("committed") is True
    if isinstance(receipt.get("reason_code"), str):
        completed["reason_code"] = receipt["reason_code"]
    operation_state[operation.operation_id] = completed
    return True, updated, receipt


class MemoryFleetStateStore:
    """Deterministic CAS store used by simulations and embedders."""

    def __init__(self, value: Mapping[str, Any] | None = None) -> None:
        self._lock = threading.RLock()
        self.revision = 0
        self.value: dict[str, Any] = copy.deepcopy(dict(value or {}))

    def load(self) -> tuple[int, Mapping[str, Any]]:
        with self._lock:
            return self.revision, copy.deepcopy(self.value)

    def compare_and_swap(
        self, expected_revision: int, value: Mapping[str, Any]
    ) -> bool:
        with self._lock:
            if expected_revision != self.revision:
                return False
            self.value = copy.deepcopy(dict(value))
            self.revision += 1
            return True

    def execute_if_current(
        self,
        config_revisions: Mapping[str, int],
        authorization_fingerprints: Mapping[str, str],
        operation: FleetOperation,
        attempted_at: datetime,
        executor: FleetExecutorClient,
    ) -> tuple[bool, Mapping[str, Any]]:
        with self._lock:
            executed, updated, receipt = _execute_fleet_operation(
                self.value,
                config_revisions,
                authorization_fingerprints,
                operation,
                attempted_at,
                executor,
            )
            if executed:
                self.value = updated
                self.revision += 1
            return executed, receipt


class FileFleetStateStore:
    """Owner-only durable CAS state for restart-safe operation replay."""

    def __init__(self, path: Path, *, max_bytes: int = 1_000_000) -> None:
        if not path.is_absolute() or max_bytes < 1:
            raise ValueError("fleet state path or size bound is invalid")
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.max_bytes = max_bytes

    _held_locks = threading.local()

    @contextmanager
    def _locked(self) -> Any:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        key = os.fspath(self.lock_path.resolve())
        held = getattr(self._held_locks, "paths", set())
        if key in held:
            yield
            return
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.chmod(self.lock_path, 0o600)
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            held.add(key)
            self._held_locks.paths = held
            try:
                yield
            finally:
                held.remove(key)

    def _read_unlocked(self) -> tuple[int, dict[str, Any]]:
        if not self.path.exists():
            return 0, {}
        if self.path.is_symlink():
            raise RuntimeError("fleet state path is a symlink")
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("fleet state path is not an owner-only file")
        if info.st_size > self.max_bytes:
            raise RuntimeError("fleet state exceeds the safe bound")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("fleet state is unreadable") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"schema", "revision", "value"}
            or document.get("schema") != "pursers_fleet_state_store_v1"
            or not isinstance(document.get("revision"), int)
            or isinstance(document.get("revision"), bool)
            or document["revision"] < 0
            or not isinstance(document.get("value"), dict)
        ):
            raise RuntimeError("fleet state envelope is invalid")
        return document["revision"], document["value"]

    def load(self) -> tuple[int, Mapping[str, Any]]:
        with self._locked():
            revision, value = self._read_unlocked()
            return revision, copy.deepcopy(value)

    def compare_and_swap(
        self, expected_revision: int, value: Mapping[str, Any]
    ) -> bool:
        with self._locked():
            revision, _ = self._read_unlocked()
            if revision != expected_revision:
                return False
            document = {
                "schema": "pursers_fleet_state_store_v1",
                "revision": revision + 1,
                "value": copy.deepcopy(dict(value)),
            }
            encoded = json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > self.max_bytes:
                raise RuntimeError("fleet state exceeds the safe bound")
            descriptor, raw = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent
            )
            temporary = Path(raw)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.write(b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return True

    def execute_if_current(
        self,
        config_revisions: Mapping[str, int],
        authorization_fingerprints: Mapping[str, str],
        operation: FleetOperation,
        attempted_at: datetime,
        executor: FleetExecutorClient,
    ) -> tuple[bool, Mapping[str, Any]]:
        with self._locked():
            revision, value = self._read_unlocked()
            executed, updated, receipt = _execute_fleet_operation(
                value,
                config_revisions,
                authorization_fingerprints,
                operation,
                attempted_at,
                executor,
            )
            if not executed:
                return False, receipt
            document = {
                "schema": "pursers_fleet_state_store_v1",
                "revision": revision + 1,
                "value": updated,
            }
            encoded = json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > self.max_bytes:
                raise RuntimeError("fleet state exceeds the safe bound")
            descriptor, raw = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent
            )
            temporary = Path(raw)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.write(b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return True, receipt


class FileFleetObservationSource:
    """Read one fresh, owner-only executor/provider/host observation."""

    REQUIRED_FIELDS = frozenset(
        {
            "schema",
            "schema_version",
            "observed_at",
            "stale_after",
            "executor_seats",
            "provider_observations",
            "provider_maximums",
            "host_observation",
        }
    )

    def __init__(self, path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> None:
        if not path.is_absolute() or max_bytes < 1:
            raise ValueError("fleet observation path or size bound is invalid")
        self.path = path
        self.max_bytes = max_bytes

    def load(self, now: datetime) -> Mapping[str, Any]:
        if now.tzinfo is None or self.path.is_symlink():
            raise RuntimeError("fleet observation source is untrusted")
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags)
            try:
                info = os.fstat(descriptor)
                raw = os.read(descriptor, self.max_bytes + 1)
                if len(raw) > self.max_bytes or os.read(descriptor, 1):
                    raise RuntimeError("fleet observation exceeds the safe bound")
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise RuntimeError("fleet observation is unavailable") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RuntimeError("fleet observation source is not owner-only")
        try:
            document = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("fleet observation is invalid") from exc
        if (
            not isinstance(document, dict)
            or set(document) != self.REQUIRED_FIELDS
            or document.get("schema") != "pursers_fleet_observation_v1"
            or document.get("schema_version") != 1
        ):
            raise RuntimeError("fleet observation envelope is invalid")
        observed_at = parse_time(document.get("observed_at"))
        stale_after = parse_time(document.get("stale_after"))
        if (
            observed_at is None
            or stale_after is None
            or observed_at > now
            or stale_after < now
            or observed_at > stale_after
        ):
            raise RuntimeError("fleet observation is stale")
        if (
            not isinstance(document.get("executor_seats"), list)
            or not isinstance(document.get("provider_observations"), Mapping)
            or not isinstance(document.get("provider_maximums"), Mapping)
            or not isinstance(document.get("host_observation"), Mapping)
        ):
            raise RuntimeError("fleet observation payload is invalid")
        return document


def _fleet_operation_id(
    *,
    config_revision: int,
    authorization_fingerprint_sha256: str,
    seat: FleetSeat,
    action: str,
) -> str:
    material = json.dumps(
        {
            "action": action,
            "authorization": authorization_fingerprint_sha256,
            "board_id": seat.board_id,
            "config_revision": config_revision,
            "generation": seat.generation,
            "seat_id": seat.seat_id,
            "template_digest": seat.template_digest_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "fleet:" + hashlib.sha256(material).hexdigest()


def _healthy_provider(
    demand: FleetDemand, policy: FleetBoardPolicy, provider: str
) -> bool:
    return (
        demand.provider_health.get(provider) == "healthy"
        and demand.provider_latency_ms.get(provider, policy.provider_latency_limit_ms + 1)
        <= policy.provider_latency_limit_ms
        and policy.provider_maximums.get(provider, 0) > 0
    )


def _role_pressure(demand: FleetDemand, role: str) -> int:
    if role == "worker":
        return demand.work_pressure
    if role == "reviewer":
        return demand.review_backlog
    return demand.acp_backlog


def _fleet_state_time(state: Mapping[str, Any], key: str) -> datetime | None:
    value = state.get(key)
    return parse_time(value) if isinstance(value, str) else None


def fleet_policies_from_config(
    configs: Mapping[str, Mapping[str, Any]],
    provider_maximums: Mapping[str, Mapping[str, int]],
    now: datetime,
) -> tuple[FleetHostPolicy, dict[str, FleetBoardPolicy]]:
    """Validate active human configuration and derive immutable policy bounds."""
    if now.tzinfo is None or not configs:
        raise ButlerConfigError("fleet configuration time or registry is invalid")
    policies: dict[str, FleetBoardPolicy] = {}
    host_identity: tuple[Any, ...] | None = None
    effective_host_cap: int | None = None
    host_document: Mapping[str, Any] | None = None
    for board_id in sorted(configs):
        document = configs[board_id]
        if (
            document.get("schema") != "autonomous_butler_config_v1"
            or document.get("schema_version") != 1
            or document.get("board_id") != board_id
            or document.get("enabled") is not True
        ):
            raise ButlerConfigError(f"{board_id}: autonomous fleet config is invalid")
        revision = document.get("revision")
        desired = document.get("desired")
        envelope = document.get("envelope")
        authorization = document.get("authorization")
        host = document.get("host_runtime")
        if not all(
            isinstance(value, Mapping)
            for value in (desired, envelope, authorization, host)
        ) or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ButlerConfigError(f"{board_id}: autonomous fleet config is incomplete")
        if desired.get("mode") != "autonomous":
            raise ButlerConfigError(f"{board_id}: autonomous fleet mode is not enabled")
        fingerprint = envelope.get("fingerprint_sha256")
        expires_at = parse_time(authorization.get("expires_at"))
        if (
            not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or authorization.get("config_revision") != revision
            or authorization.get("envelope_fingerprint_sha256") != fingerprint
            or expires_at is None
            or expires_at <= now
        ):
            raise ButlerConfigError(f"{board_id}: autonomous authorization is invalid")
        desired_capacity = desired.get("capacity")
        envelope_capacity = envelope.get("max_capacity")
        approved_templates = envelope.get("approved_template_ids")
        cooldowns = desired.get("cooldowns")
        if (
            not isinstance(desired_capacity, Mapping)
            or not isinstance(envelope_capacity, Mapping)
            or not isinstance(cooldowns, Mapping)
            or not isinstance(approved_templates, list)
            or not approved_templates
            or any(not isinstance(item, str) or not item for item in approved_templates)
        ):
            raise ButlerConfigError(f"{board_id}: autonomous fleet bounds are missing")
        roles: dict[str, FleetRolePolicy] = {}
        for role in FLEET_ROLES:
            row = desired_capacity.get(role)
            ceiling = envelope_capacity.get(role)
            if not isinstance(row, Mapping) or not isinstance(ceiling, int):
                raise ButlerConfigError(f"{board_id}: {role} bounds are invalid")
            try:
                role_policy = FleetRolePolicy(
                    row["min"], row["target"], row["max"], 1
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ButlerConfigError(f"{board_id}: {role} bounds are invalid") from exc
            if role_policy.maximum > ceiling:
                raise ButlerConfigError(f"{board_id}: {role} exceeds immutable envelope")
            roles[role] = role_policy
        desired_board_cap = desired.get("board_concurrency")
        envelope_board_cap = envelope.get("max_board_concurrency")
        desired_host_cap = desired.get("host_concurrency")
        envelope_host_cap = envelope.get("max_host_concurrency")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in (
                desired_board_cap,
                envelope_board_cap,
                desired_host_cap,
                envelope_host_cap,
            )
        ):
            raise ButlerConfigError(f"{board_id}: concurrency bounds are invalid")
        if (
            desired_board_cap > envelope_board_cap
            or desired_host_cap > envelope_host_cap
            or sum(item.maximum for item in roles.values()) > desired_board_cap
        ):
            raise ButlerConfigError(f"{board_id}: desired capacity exceeds an envelope")
        try:
            agent_ceiling = int(host["agent_process_ceiling"])
            control_processes = int(host["control_plane_processes"])
            total_ceiling = int(host["total_process_ceiling"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ButlerConfigError(f"{board_id}: host runtime bounds are invalid") from exc
        identity = (
            host.get("host_ref"),
            host.get("revision"),
            agent_ceiling,
            control_processes,
            total_ceiling,
        )
        if host_identity is not None and identity != host_identity:
            raise ButlerConfigError("registry configs disagree on host runtime")
        host_identity = identity
        host_document = host
        board_host_cap = min(agent_ceiling, desired_host_cap, envelope_host_cap)
        effective_host_cap = (
            board_host_cap
            if effective_host_cap is None
            else min(effective_host_cap, board_host_cap)
        )
        try:
            policies[board_id] = FleetBoardPolicy(
                board_id=board_id,
                roles=roles,
                board_maximum=desired_board_cap,
                provider_maximums=dict(provider_maximums[board_id]),
                approved_template_ids=frozenset(str(item) for item in approved_templates),
                idle_grace_s=int(cooldowns["scale_down_s"]),
                scale_up_cooldown_s=int(cooldowns["scale_up_s"]),
                scale_down_cooldown_s=int(cooldowns["scale_down_s"]),
                failure_backoff_s=int(cooldowns["failure_backoff_s"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ButlerConfigError(f"{board_id}: fleet policy is invalid") from exc
    assert host_document is not None and effective_host_cap is not None
    host_policy = FleetHostPolicy(
        effective_host_cap,
        int(host_document["control_plane_processes"]),
        int(host_document["total_process_ceiling"]),
    )
    if host_policy.role_capacity < effective_host_cap:
        raise ButlerConfigError("host total process ceiling is smaller than desired capacity")
    return host_policy, policies


def fleet_snapshot_from_products(
    board_snapshots: Mapping[str, Mapping[str, Any]],
    executor_seats: Sequence[Mapping[str, Any]],
    provider_observations: Mapping[str, Mapping[str, Mapping[str, Any]]],
    host_observation: Mapping[str, Any],
    now: datetime,
    *,
    offer_horizon_s: int = 120,
) -> FleetSnapshot:
    """Consume actual Central/executor observations; incomplete data fails closed."""
    if now.tzinfo is None or offer_horizon_s < 0:
        raise ValueError("fleet observation time is invalid")
    demands: dict[str, FleetDemand] = {}
    agent_indexes: dict[str, dict[str, Mapping[str, Any]]] = {}
    for board_id in sorted(board_snapshots):
        board = board_snapshots[board_id]
        if board.get("truncated") is True and board.get("coordination_tickets_complete") is not True:
            raise ValueError(f"{board_id}: ticket snapshot is incomplete")
        rows = board.get("coordination_tickets", board.get("tickets"))
        agents = board.get("agents")
        if not isinstance(rows, list) or not isinstance(agents, list):
            raise ValueError(f"{board_id}: product snapshot is incomplete")
        index: dict[str, Mapping[str, Any]] = {}
        for agent in agents:
            if not isinstance(agent, Mapping):
                continue
            for key in (agent.get("agent_name"), agent.get("agent_id")):
                if isinstance(key, str) and key:
                    index[key] = agent
        agent_indexes[board_id] = index
        open_by_tier = {0: 0, 1: 0, 2: 0}
        review_backlog = 0
        acp_backlog = 0
        ages: list[int] = []
        expiring = 0
        for ticket in rows:
            if not isinstance(ticket, Mapping):
                continue
            status = ticket.get("status")
            if status == "submitted":
                review_backlog += 1
            if status != "open":
                continue
            tags = ticket.get("tags", [])
            is_acp = isinstance(tags, list) and any(
                tag in {"acp", "acp-worker"} for tag in tags
            )
            tier = ticket.get("tier", 0)
            if not isinstance(tier, int) or isinstance(tier, bool) or tier not in {0, 1, 2}:
                raise ValueError(f"{board_id}: ticket tier is invalid")
            if is_acp:
                acp_backlog += 1
            else:
                open_by_tier[tier] += 1
            created = parse_time(ticket.get("created_at"))
            if created is not None and created <= now:
                ages.append(int((now - created).total_seconds()))
            dispatch = ticket.get("dispatch_state")
            offer = ticket.get("current_offer")
            candidate = dispatch if isinstance(dispatch, Mapping) else offer
            if isinstance(candidate, Mapping) and candidate.get("state", "offered") == "offered":
                expiry = parse_time(candidate.get("expires_at"))
                if expiry is not None and now <= expiry <= now + timedelta(seconds=offer_horizon_s):
                    expiring += 1
        providers = provider_observations.get(board_id)
        if not isinstance(providers, Mapping) or not providers:
            raise ValueError(f"{board_id}: provider observations are missing")
        health: dict[str, str] = {}
        latency: dict[str, int] = {}
        for provider, observation in providers.items():
            if not isinstance(observation, Mapping):
                raise ValueError(f"{board_id}: provider observation is invalid")
            health[str(provider)] = str(observation.get("status", "unknown"))
            raw_latency = observation.get("latency_ms")
            if not isinstance(raw_latency, int) or isinstance(raw_latency, bool):
                raise ValueError(f"{board_id}: provider latency is invalid")
            latency[str(provider)] = raw_latency
        demands[board_id] = FleetDemand(
            board_id=board_id,
            open_by_tier=open_by_tier,
            review_backlog=review_backlog,
            acp_backlog=acp_backlog,
            oldest_ticket_age_s=max(ages, default=0),
            expiring_offers=expiring,
            provider_health=health,
            provider_latency_ms=latency,
        )
    seats: list[FleetSeat] = []
    required = {
        "seat_id",
        "board_id",
        "role",
        "provider",
        "template_id",
        "template_digest_sha256",
        "generation",
        "lifecycle",
        "transition_at",
        "managed",
    }
    for row in executor_seats:
        if not isinstance(row, Mapping) or not required <= set(row):
            raise ValueError("executor seat observation is incomplete")
        board_id = str(row["board_id"])
        if not isinstance(row["managed"], bool):
            raise ValueError("executor managed flag is invalid")
        transition_at = parse_time(row["transition_at"])
        if transition_at is None:
            raise ValueError("executor transition time is invalid")
        agent = agent_indexes.get(board_id, {}).get(str(row["seat_id"]))
        lease_expiry = (
            parse_time(agent.get("lease_expires_at"))
            if isinstance(agent, Mapping)
            else None
        )
        live = lease_expiry is not None and lease_expiry > now
        lifecycle = str(row["lifecycle"])
        agent_ready = (
            isinstance(agent, Mapping)
            and agent.get("lifecycle_status", "active") == "active"
            and agent.get("status") not in {"offline", "retired"}
        )
        seats.append(
            FleetSeat(
                seat_id=str(row["seat_id"]),
                board_id=board_id,
                role=str(row["role"]),
                provider=str(row["provider"]),
                template_id=str(row["template_id"]),
                template_digest_sha256=str(row["template_digest_sha256"]),
                generation=int(row["generation"]),
                lifecycle=lifecycle,
                ready=lifecycle in {"ready", "busy"} and agent_ready,
                busy=lifecycle == "busy"
                or (isinstance(agent, Mapping) and agent.get("status") == "busy"),
                live_lease=live,
                transition_at=transition_at,
                managed=row["managed"],
            )
        )
    load_ratio = host_observation.get("load_ratio")
    capacity_available = host_observation.get("capacity_available")
    executor_status = host_observation.get("executor_status")
    if (
        not isinstance(load_ratio, (int, float))
        or isinstance(load_ratio, bool)
        or not isinstance(capacity_available, bool)
        or executor_status not in {"healthy", "degraded", "unavailable", "unknown"}
    ):
        raise ValueError("host observation is invalid")
    return FleetSnapshot(
        observed_at=now,
        demands=demands,
        seats=tuple(seats),
        host_load_ratio=float(load_ratio),
        host_capacity_available=capacity_available,
        executor_healthy=executor_status == "healthy",
    )


class FleetReconciler:
    """Registry-wide deterministic desired-state and executor reconciler.

    Model output is intentionally absent from this class. Every ceiling comes
    from ``FleetHostPolicy`` or ``FleetBoardPolicy`` and every mutation is sent
    through the typed host executor with a stable operation id.
    """

    def __init__(
        self,
        host_policy: FleetHostPolicy,
        board_policies: Mapping[str, FleetBoardPolicy],
        *,
        config_revision: int,
        authorization_fingerprint_sha256: str,
        config_revisions: Mapping[str, int] | None = None,
        authorization_fingerprints: Mapping[str, str] | None = None,
        max_cas_retries: int = 3,
        max_operation_attempts: int = 3,
    ) -> None:
        if set(board_policies) != {item.board_id for item in board_policies.values()}:
            raise ValueError("fleet board policy keys do not match board ids")
        if config_revision < 1 or not re.fullmatch(
            r"[0-9a-f]{64}", authorization_fingerprint_sha256
        ):
            raise ValueError("fleet authorization is invalid")
        if max_cas_retries < 1 or max_operation_attempts < 1:
            raise ValueError("fleet retry bounds are invalid")
        self.host_policy = host_policy
        self.board_policies = dict(board_policies)
        self.config_revision = config_revision
        self.authorization_fingerprint_sha256 = authorization_fingerprint_sha256
        self.config_revisions = dict(
            config_revisions
            or {board_id: config_revision for board_id in board_policies}
        )
        self.authorization_fingerprints = dict(
            authorization_fingerprints
            or {
                board_id: authorization_fingerprint_sha256
                for board_id in board_policies
            }
        )
        if (
            set(self.config_revisions) != set(board_policies)
            or set(self.authorization_fingerprints) != set(board_policies)
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
                for value in self.config_revisions.values()
            )
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in self.authorization_fingerprints.values()
            )
        ):
            raise ValueError("fleet per-board authorization is invalid")
        self.max_cas_retries = max_cas_retries
        self.max_operation_attempts = max_operation_attempts

    def _policy_matches(self, prior: Mapping[str, Any]) -> bool:
        revisions = prior.get("config_revisions")
        fingerprints = prior.get("authorization_fingerprints")
        return (
            isinstance(revisions, Mapping)
            and dict(revisions) == self.config_revisions
            and isinstance(fingerprints, Mapping)
            and dict(fingerprints) == self.authorization_fingerprints
        )

    def _stale_config_revisions(
        self, prior: Mapping[str, Any]
    ) -> dict[str, dict[str, int]]:
        """Return boards whose authoritative input is older than durable state."""
        revisions = prior.get("config_revisions")
        if not isinstance(revisions, Mapping):
            return {}
        stale: dict[str, dict[str, int]] = {}
        for board_id in sorted(set(revisions) & set(self.config_revisions)):
            durable = revisions[board_id]
            current = self.config_revisions[board_id]
            if (
                isinstance(durable, int)
                and not isinstance(durable, bool)
                and durable > current
            ):
                stale[board_id] = {
                    "current_revision": current,
                    "durable_revision": durable,
                }
        return stale

    @staticmethod
    def _durable_desired(prior: Mapping[str, Any]) -> dict[str, dict[str, int]]:
        boards = prior.get("boards")
        if not isinstance(boards, Mapping):
            return {}
        desired: dict[str, dict[str, int]] = {}
        for board_id, raw in boards.items():
            if not isinstance(board_id, str) or not isinstance(raw, Mapping):
                continue
            counts = raw.get("desired")
            if not isinstance(counts, Mapping):
                continue
            desired[board_id] = {
                role: int(counts.get(role, 0) or 0)
                for role in FLEET_ROLES
                if isinstance(counts.get(role, 0), int)
                and not isinstance(counts.get(role, 0), bool)
            }
        return desired

    def _stale_config_report(
        self,
        prior: Mapping[str, Any],
        stale: Mapping[str, Mapping[str, int]],
        now: datetime,
    ) -> dict[str, Any]:
        evidence = [
            {
                "board_id": board_id,
                "action": "reconcile",
                "outcome": "denied",
                "reason_code": "stale_config_revision",
                "current_revision": int(revisions["current_revision"]),
                "durable_revision": int(revisions["durable_revision"]),
                "observed_at": now.isoformat(),
                "detail_redacted": True,
            }
            for board_id, revisions in sorted(stale.items())
        ]
        return {
            "status": "shadow",
            "effective_state": "shadow",
            "reason_code": "stale_config_revision",
            "desired": self._durable_desired(prior),
            "operations": [],
            "receipts": [],
            "explanations": [
                {
                    "scope": "policy",
                    "reason": "stale_config_revision",
                    "boards": sorted(stale),
                }
            ],
            "audit_evidence": evidence,
            "state_documents": {},
        }

    @staticmethod
    def _terminal_operation_history(prior: Mapping[str, Any]) -> dict[str, Any]:
        """Keep only validated, non-replayable audit rows across policy changes."""
        source = prior.get("operations", {})
        if not isinstance(source, Mapping):
            return {}
        terminal: dict[str, Any] = {}
        for operation_id, raw in source.items():
            if (
                not isinstance(operation_id, str)
                or not re.fullmatch(r"fleet:[0-9a-f]{64}", operation_id)
                or not isinstance(raw, Mapping)
                or raw.get("operation_id") != operation_id
                or raw.get("status") != "terminal"
                or raw.get("outcome")
                not in {"succeeded", "rejected", "failed", "cancelled"}
                or raw.get("action") not in {"start", "drain", "stop"}
                or not isinstance(raw.get("board_id"), str)
                or not isinstance(raw.get("seat_id"), str)
                or not isinstance(raw.get("attempts"), int)
                or isinstance(raw.get("attempts"), bool)
                or raw["attempts"] < 1
                or not isinstance(raw.get("committed"), bool)
                or parse_time(raw.get("last_attempt_at")) is None
            ):
                continue
            row = {
                "operation_id": operation_id,
                "board_id": raw["board_id"],
                "action": raw["action"],
                "seat_id": raw["seat_id"],
                "status": "terminal",
                "attempts": raw["attempts"],
                "last_attempt_at": raw["last_attempt_at"],
                "outcome": raw["outcome"],
                "committed": raw["committed"],
            }
            if isinstance(raw.get("reason_code"), str):
                row["reason_code"] = raw["reason_code"]
            terminal[operation_id] = row
        return terminal

    def prior_for_current_policy(
        self, prior: Mapping[str, Any], now: datetime
    ) -> Mapping[str, Any]:
        """Create a fail-closed CAS migration view for an authoritative policy."""
        if not prior or self._policy_matches(prior):
            return prior
        operations = prior.get("operations", {})
        old_operation_count = len(operations) if isinstance(operations, Mapping) else 0
        terminal = self._terminal_operation_history(prior)
        return {
            "operations": terminal,
            "policy_transition": {
                "at": now.isoformat(),
                "from_config_revisions": copy.deepcopy(
                    dict(prior.get("config_revisions", {}))
                    if isinstance(prior.get("config_revisions"), Mapping)
                    else {}
                ),
                "to_config_revisions": dict(self.config_revisions),
                "from_authorization_fingerprints": copy.deepcopy(
                    dict(prior.get("authorization_fingerprints", {}))
                    if isinstance(
                        prior.get("authorization_fingerprints"), Mapping
                    )
                    else {}
                ),
                "to_authorization_fingerprints": dict(
                    self.authorization_fingerprints
                ),
                "preserved_terminal_operations": len(terminal),
                "discarded_nonterminal_operations": max(
                    0, old_operation_count - len(terminal)
                ),
            },
        }

    def _requested_counts(
        self, snapshot: FleetSnapshot, prior: Mapping[str, Any]
    ) -> tuple[dict[str, dict[str, int]], list[dict[str, Any]]]:
        now = snapshot.observed_at
        prior_boards = prior.get("boards", {})
        result: dict[str, dict[str, int]] = {}
        explanations: list[dict[str, Any]] = []
        for board_id in sorted(snapshot.demands):
            demand = snapshot.demands[board_id]
            policy = self.board_policies[board_id]
            board_prior = (
                prior_boards.get(board_id, {})
                if isinstance(prior_boards, Mapping)
                else {}
            )
            role_prior = (
                board_prior.get("desired", {})
                if isinstance(board_prior, Mapping)
                else {}
            )
            idle_since = _fleet_state_time(board_prior, "idle_since")
            last_up = _fleet_state_time(board_prior, "last_scale_up_at")
            last_down = _fleet_state_time(board_prior, "last_scale_down_at")
            seats = [seat for seat in snapshot.seats if seat.board_id == board_id]
            counts: dict[str, int] = {}
            for role in FLEET_ROLES:
                role_policy = policy.roles[role]
                live = sum(
                    1
                    for seat in seats
                    if seat.managed and seat.role == role and seat.live_lease
                )
                pressure = _role_pressure(demand, role)
                available = sum(
                    1
                    for seat in seats
                    if seat.managed
                    and seat.role == role
                    and seat.template_id in policy.approved_template_ids
                    and _healthy_provider(demand, policy, seat.provider)
                )
                if pressure:
                    requested = max(
                        role_policy.minimum,
                        role_policy.target,
                        math.ceil(pressure / role_policy.backlog_per_seat),
                    )
                else:
                    requested = role_policy.minimum
                requested = max(requested, live)
                requested = min(requested, role_policy.maximum, available)
                previous = int(role_prior.get(role, 0) or 0)
                if pressure == 0:
                    active_count = sum(
                        1
                        for seat in seats
                        if seat.managed and seat.role == role and seat.active
                    )
                    if idle_since is None:
                        # The first zero-demand observation starts the durable
                        # grace window; it never drains immediately after a restart.
                        requested = max(
                            requested,
                            min(active_count, role_policy.maximum, available),
                        )
                    elif (now - idle_since).total_seconds() < policy.idle_grace_s:
                        requested = max(
                            requested,
                            min(previous, active_count, role_policy.maximum, available),
                        )
                if requested > previous and last_up is not None:
                    cooldown = (now - last_up).total_seconds() < policy.scale_up_cooldown_s
                    starvation = pressure > previous * role_policy.backlog_per_seat
                    if cooldown and not starvation:
                        requested = previous
                if requested < previous and last_down is not None:
                    if (now - last_down).total_seconds() < policy.scale_down_cooldown_s:
                        requested = previous
                # A live-lease oversubscription is observed, never amplified:
                # desired state remains within the immutable role maximum while
                # operation selection separately refuses to stop a live holder.
                counts[role] = min(role_policy.maximum, max(live, requested))
                explanations.append(
                    {
                        "board_id": board_id,
                        "role": role,
                        "pressure": pressure,
                        "live_leases": live,
                        "healthy_approved_seats": available,
                        "requested": counts[role],
                        "reason": (
                            "demand"
                            if pressure
                            else "idle_grace"
                            if counts[role] > role_policy.minimum
                            else "minimum"
                        ),
                    }
                )
            # Board maxima are applied without sacrificing a live lease. Any
            # impossible live-lease oversubscription is reported and no stop is planned.
            provider_budget = sum(
                min(
                    maximum,
                    sum(
                        1
                        for seat in seats
                        if seat.managed
                        and seat.template_id in policy.approved_template_ids
                        and seat.provider == provider
                        and _healthy_provider(demand, policy, provider)
                    ),
                )
                for provider, maximum in policy.provider_maximums.items()
            )
            budget = min(policy.board_maximum, provider_budget)
            self._trim_counts(counts, budget, demand)
            result[board_id] = counts
        return result, explanations

    @staticmethod
    def _trim_counts(
        counts: dict[str, int],
        budget: int,
        demand: FleetDemand,
    ) -> None:
        # Reviewer demand is allocated before worker and ACP increments so an
        # independent review bottleneck cannot be created by worker scale-up.
        while sum(counts.values()) > budget:
            candidates: list[tuple[int, str]] = []
            for role in FLEET_ROLES:
                floor = 0
                if counts[role] > floor:
                    priority = (
                        1_000_000
                        if role == "reviewer" and demand.review_backlog
                        else 0
                    ) + _role_pressure(demand, role)
                    candidates.append((priority, role))
            if not candidates:
                break
            _, role = min(candidates)
            counts[role] -= 1

    def _apply_host_cap(
        self,
        requested: dict[str, dict[str, int]],
        snapshot: FleetSnapshot,
    ) -> None:
        unmanaged_active = sum(
            1 for seat in snapshot.seats if not seat.managed and seat.active
        )
        host_budget = max(0, self.host_policy.role_capacity - unmanaged_active)
        # High load is a deterministic no-scale-up signal. Existing desired
        # state is allowed to drain normally; live holders are always preserved.
        if not snapshot.host_capacity_available or snapshot.host_load_ratio >= 0.95:
            managed_active = sum(
                1 for seat in snapshot.seats if seat.managed and seat.active
            )
            host_budget = managed_active
        while sum(sum(row.values()) for row in requested.values()) > host_budget:
            candidates: list[tuple[int, str, str]] = []
            for board_id, counts in requested.items():
                demand = snapshot.demands[board_id]
                for role in FLEET_ROLES:
                    floor = 0
                    if counts[role] <= floor:
                        continue
                    priority = (
                        1_000_000
                        if role == "reviewer" and demand.review_backlog
                        else 0
                    ) + _role_pressure(demand, role)
                    candidates.append((priority, board_id, role))
            if not candidates:
                return
            _, board_id, role = min(candidates)
            requested[board_id][role] -= 1

    def _provider_desired(
        self,
        requested: Mapping[str, Mapping[str, int]],
        snapshot: FleetSnapshot,
    ) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for board_id, counts in requested.items():
            demand = snapshot.demands[board_id]
            policy = self.board_policies[board_id]
            remaining = sum(counts.values())
            provider_counts: dict[str, int] = {
                provider: 0 for provider in sorted(policy.provider_maximums)
            }
            providers = sorted(
                provider_counts,
                key=lambda provider: (
                    demand.provider_latency_ms.get(provider, 10**9),
                    provider,
                ),
            )
            for provider in providers:
                if not _healthy_provider(demand, policy, provider):
                    continue
                approved = sum(
                    1
                    for seat in snapshot.seats
                    if seat.managed
                    and seat.board_id == board_id
                    and seat.template_id in policy.approved_template_ids
                    and seat.provider == provider
                )
                assigned = min(remaining, policy.provider_maximums[provider], approved)
                provider_counts[provider] = assigned
                remaining -= assigned
            result[board_id] = provider_counts
        return result

    def _operations(
        self,
        requested: Mapping[str, Mapping[str, int]],
        provider_desired: Mapping[str, Mapping[str, int]],
        snapshot: FleetSnapshot,
        prior: Mapping[str, Any],
    ) -> tuple[FleetOperation, ...]:
        now = snapshot.observed_at
        prior_operations = prior.get("operations", {})
        operations: list[FleetOperation] = []
        active_total = sum(1 for seat in snapshot.seats if seat.active)
        start_budget = max(0, self.host_policy.role_capacity - active_total)
        if not snapshot.host_capacity_available or snapshot.host_load_ratio >= 0.95:
            start_budget = 0
        for board_id in sorted(requested):
            policy = self.board_policies[board_id]
            demand = snapshot.demands[board_id]
            board_inventory = [
                seat for seat in snapshot.seats if seat.board_id == board_id
            ]
            seats = [
                seat
                for seat in board_inventory
                if seat.managed
                and seat.template_id in policy.approved_template_ids
            ]
            board_start_budget = max(
                0,
                policy.board_maximum
                - sum(1 for seat in board_inventory if seat.active),
            )
            role_start_budget = {
                role: max(
                    0,
                    policy.roles[role].maximum
                    - sum(
                        1
                        for seat in board_inventory
                        if seat.role == role and seat.active
                    ),
                )
                for role in FLEET_ROLES
            }
            provider_start_budget = {
                provider: max(
                    0,
                    maximum
                    - sum(
                        1
                        for seat in board_inventory
                        if seat.provider == provider and seat.active
                    ),
                )
                for provider, maximum in policy.provider_maximums.items()
            }
            selected: set[str] = set()
            provider_started = {
                provider: 0 for provider in provider_desired[board_id]
            }
            for role in FLEET_ROLES:
                target = requested[board_id][role]
                role_seats = [seat for seat in seats if seat.role == role]
                active = [seat for seat in role_seats if seat.active]
                # Live holders and busy seats are stable first choices, then
                # healthy low-latency providers.
                keep = sorted(
                    active,
                    key=lambda seat: (
                        not seat.live_lease,
                        not seat.busy,
                        demand.provider_latency_ms.get(seat.provider, 10**9),
                        seat.seat_id,
                    ),
                )[:target]
                selected.update(seat.seat_id for seat in keep)
                for seat in keep:
                    provider_started[seat.provider] = provider_started.get(seat.provider, 0) + 1
                needed = max(0, target - len(active))
                candidates = sorted(
                    (
                        seat
                        for seat in role_seats
                        if not seat.active
                        and _healthy_provider(demand, policy, seat.provider)
                    ),
                    key=lambda seat: (
                        demand.provider_latency_ms.get(seat.provider, 10**9),
                        seat.seat_id,
                    ),
                )
                for seat in candidates:
                    if (
                        needed <= 0
                        or start_budget <= 0
                        or board_start_budget <= 0
                        or role_start_budget[role] <= 0
                    ):
                        break
                    if provider_start_budget.get(seat.provider, 0) <= 0:
                        continue
                    if provider_started.get(seat.provider, 0) >= provider_desired[board_id].get(
                        seat.provider, 0
                    ):
                        continue
                    operations.append(self._operation(seat, "start"))
                    provider_started[seat.provider] = provider_started.get(seat.provider, 0) + 1
                    selected.add(seat.seat_id)
                    needed -= 1
                    start_budget -= 1
                    board_start_budget -= 1
                    role_start_budget[role] -= 1
                    provider_start_budget[seat.provider] -= 1
                excess = [
                    seat
                    for seat in active
                    if seat.seat_id not in selected
                    and not seat.live_lease
                    and not seat.busy
                ]
                for seat in sorted(excess, key=lambda item: item.seat_id):
                    idle_for = (now - seat.transition_at).total_seconds()
                    if idle_for < policy.idle_grace_s:
                        continue
                    action = "stop" if seat.lifecycle == "draining" else "drain"
                    operations.append(self._operation(seat, action))
        # Stable ids make retries and crash recovery safe. Suppress terminal
        # successes; replay unknown/in-flight operations with the exact same id.
        filtered: list[FleetOperation] = []
        for operation in operations:
            prior_row = (
                prior_operations.get(operation.operation_id, {})
                if isinstance(prior_operations, Mapping)
                else {}
            )
            if isinstance(prior_row, Mapping) and prior_row.get("outcome") == "succeeded":
                continue
            attempts = int(prior_row.get("attempts", 0) or 0) if isinstance(prior_row, Mapping) else 0
            last_attempt = (
                parse_time(prior_row.get("last_attempt_at"))
                if isinstance(prior_row, Mapping)
                else None
            )
            backoff_active = (
                last_attempt is not None
                and (
                    snapshot.observed_at - last_attempt
                ).total_seconds()
                < self.board_policies[operation.board_id].failure_backoff_s
            )
            if attempts < self.max_operation_attempts and not backoff_active:
                filtered.append(operation)
        return tuple(filtered)

    def _operation(self, seat: FleetSeat, action: str) -> FleetOperation:
        config_revision = self.config_revisions[seat.board_id]
        fingerprint = self.authorization_fingerprints[seat.board_id]
        return FleetOperation(
            operation_id=_fleet_operation_id(
                config_revision=config_revision,
                authorization_fingerprint_sha256=fingerprint,
                seat=seat,
                action=action,
            ),
            board_id=seat.board_id,
            action=action,
            seat_id=seat.seat_id,
            template_id=seat.template_id,
            template_digest_sha256=seat.template_digest_sha256,
            expected_seat_generation=seat.generation,
            authorization_fingerprint_sha256=fingerprint,
        )

    def plan(self, snapshot: FleetSnapshot, prior: Mapping[str, Any]) -> FleetPlan:
        if set(snapshot.demands) != set(self.board_policies):
            raise ValueError("snapshot does not cover the configured registry")
        desired, explanations = self._requested_counts(snapshot, prior)
        self._apply_host_cap(desired, snapshot)
        provider_desired = self._provider_desired(desired, snapshot)
        operations = self._operations(
            desired, provider_desired, snapshot, prior
        )
        explanations.append(
            {
                "scope": "host",
                "role_capacity": self.host_policy.role_capacity,
                "host_load_ratio": snapshot.host_load_ratio,
                "capacity_available": snapshot.host_capacity_available,
                "desired_total": sum(sum(row.values()) for row in desired.values()),
                "reason": "deterministic_hard_cap",
            }
        )
        return FleetPlan(desired, provider_desired, operations, tuple(explanations))

    def _persisted_plan(
        self,
        plan: FleetPlan,
        snapshot: FleetSnapshot,
        prior: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = snapshot.observed_at
        prior_boards = prior.get("boards", {})
        boards: dict[str, Any] = {}
        for board_id, desired in plan.desired.items():
            demand = snapshot.demands[board_id]
            previous = (
                prior_boards.get(board_id, {})
                if isinstance(prior_boards, Mapping)
                else {}
            )
            previous_desired = previous.get("desired", {}) if isinstance(previous, Mapping) else {}
            old_total = sum(int(previous_desired.get(role, 0) or 0) for role in FLEET_ROLES)
            new_total = sum(desired.values())
            boards[board_id] = {
                "desired": dict(desired),
                "provider_desired": dict(plan.provider_desired[board_id]),
                "idle_since": (
                    previous.get("idle_since")
                    if not demand.has_work and previous.get("idle_since")
                    else now.isoformat()
                    if not demand.has_work
                    else None
                ),
                "last_scale_up_at": (
                    now.isoformat() if new_total > old_total else previous.get("last_scale_up_at")
                ),
                "last_scale_down_at": (
                    now.isoformat() if new_total < old_total else previous.get("last_scale_down_at")
                ),
            }
        operations = copy.deepcopy(dict(prior.get("operations", {})))
        for operation in plan.operations:
            row = dict(operations.get(operation.operation_id, {}))
            row.update(
                {
                    "operation_id": operation.operation_id,
                    "board_id": operation.board_id,
                    "action": operation.action,
                    "seat_id": operation.seat_id,
                    "template_id": operation.template_id,
                    "template_digest_sha256": operation.template_digest_sha256,
                    "expected_seat_generation": operation.expected_seat_generation,
                    "authorization_fingerprint_sha256": (
                        operation.authorization_fingerprint_sha256
                    ),
                    "status": "pending",
                    "attempts": int(row.get("attempts", 0) or 0),
                }
            )
            operations[operation.operation_id] = row
        if len(operations) > MAX_FLEET_OPERATION_HISTORY:
            ranked = sorted(
                operations.items(),
                key=lambda item: (
                    item[1].get("status") in {"pending", "unknown"},
                    str(item[1].get("last_attempt_at", "")),
                    item[0],
                ),
                reverse=True,
            )
            operations = dict(ranked[:MAX_FLEET_OPERATION_HISTORY])
        persisted = {
            "schema": "pursers_fleet_reconciler_state_v1",
            "config_revision": self.config_revision,
            "config_revisions": dict(self.config_revisions),
            "authorization_fingerprints": dict(
                self.authorization_fingerprints
            ),
            "observed_at": now.isoformat(),
            "boards": boards,
            "operations": operations,
            "explanations": [
                dict(item) for item in plan.explanations[:MAX_FLEET_EXPLANATIONS]
            ],
        }
        transition = prior.get("policy_transition")
        if isinstance(transition, Mapping):
            persisted["policy_transition"] = copy.deepcopy(dict(transition))
            persisted["explanations"] = [
                {
                    "scope": "policy",
                    "reason": "authoritative_config_changed",
                    "discarded_nonterminal_operations": int(
                        transition.get("discarded_nonterminal_operations", 0) or 0
                    ),
                    "preserved_terminal_operations": int(
                        transition.get("preserved_terminal_operations", 0) or 0
                    ),
                },
                *persisted["explanations"],
            ][:MAX_FLEET_EXPLANATIONS]
        return persisted

    def reconcile(
        self,
        snapshot: FleetSnapshot,
        store: FleetStateStore,
        executor: FleetExecutorClient,
    ) -> Mapping[str, Any]:
        if not snapshot.executor_healthy:
            raise RuntimeError("fleet executor is unavailable")
        for _ in range(self.max_cas_retries):
            revision, prior = store.load()
            stale = self._stale_config_revisions(prior)
            if stale:
                return self._stale_config_report(
                    prior, stale, snapshot.observed_at
                )
            prior = self.prior_for_current_policy(prior, snapshot.observed_at)
            plan = self.plan(snapshot, prior)
            persisted = self._persisted_plan(plan, snapshot, prior)
            if store.compare_and_swap(revision, persisted):
                break
        else:
            raise RuntimeError("fleet state CAS retry exhausted")
        receipts: list[dict[str, Any]] = []
        for operation in plan.operations:
            executed, receipt = store.execute_if_current(
                self.config_revisions,
                self.authorization_fingerprints,
                operation,
                snapshot.observed_at,
                executor,
            )
            if not executed:
                _current_revision, current = store.load()
                stale = self._stale_config_revisions(current)
                if stale:
                    return self._stale_config_report(
                        current, stale, snapshot.observed_at
                    )
                return {
                    "status": "shadow",
                    "effective_state": "shadow",
                    "reason_code": "superseded_policy_generation",
                    "desired": self._durable_desired(current),
                    "operations": [],
                    "receipts": [],
                    "explanations": [
                        {
                            "scope": "policy",
                            "reason": "superseded_policy_generation",
                        }
                    ],
                    "audit_evidence": [],
                    "state_documents": {},
                }
            receipts.append(dict(receipt))
        if not plan.operations:
            # Preserve the historical two-CAS cycle while refusing to touch a
            # state that changed policy generation after planning.
            result_revision, current = store.load()
            if self._policy_matches(current):
                store.compare_and_swap(result_revision, current)
        return {
            "desired": copy.deepcopy(dict(plan.desired)),
            "operations": [operation.operation_id for operation in plan.operations],
            "receipts": receipts,
            "explanations": [dict(item) for item in plan.explanations],
            "state_documents": {
                board_id: self.desired_state_document(board_id, snapshot, plan)
                for board_id in sorted(self.board_policies)
            },
        }

    def desired_state_document(
        self, board_id: str, snapshot: FleetSnapshot, plan: FleetPlan
    ) -> dict[str, Any]:
        """Render the landed autonomous_butler_state_v1 product contract."""
        now = snapshot.observed_at
        seats = [
            seat
            for seat in snapshot.seats
            if seat.managed and seat.board_id == board_id
        ]
        capacity: dict[str, Any] = {}
        for role in FLEET_ROLES:
            role_seats = [seat for seat in seats if seat.role == role]
            capacity[role] = {
                "desired": plan.desired[board_id][role],
                "ready": sum(1 for seat in role_seats if seat.lifecycle == "ready"),
                "busy": sum(1 for seat in role_seats if seat.lifecycle == "busy"),
                "starting": sum(1 for seat in role_seats if seat.lifecycle == "starting"),
                "draining": sum(1 for seat in role_seats if seat.lifecycle == "draining"),
                "unhealthy": sum(1 for seat in role_seats if seat.lifecycle == "unhealthy"),
                "stopped": sum(1 for seat in role_seats if seat.lifecycle == "stopped"),
                "seat_ids": [seat.seat_id for seat in sorted(role_seats, key=lambda item: item.seat_id)],
                "template_ids": [seat.template_id for seat in sorted(role_seats, key=lambda item: item.seat_id)],
            }
        demand = snapshot.demands[board_id]
        connectors = [
            {
                "connector_id": provider,
                "status": demand.provider_health[provider],
                "observed_at": now.isoformat(),
            }
            for provider in sorted(demand.provider_health)
        ]
        return {
            "schema": "autonomous_butler_state_v1",
            "schema_version": 1,
            "board_id": board_id,
            "config_revision": self.config_revisions[board_id],
            "effective_state": "autonomous" if snapshot.executor_healthy else "degraded",
            "reason_code": "desired_state_reconciled" if snapshot.executor_healthy else "executor_unavailable",
            "observed_at": now.isoformat(),
            "stale_after": (now + timedelta(seconds=120)).isoformat(),
            "capacity": capacity,
            "host_processes": {
                "role_agents": sum(1 for seat in snapshot.seats if seat.active),
                "control_plane": self.host_policy.control_plane_processes,
                "agent_process_ceiling": self.host_policy.agent_process_ceiling,
                "total_process_ceiling": self.host_policy.total_process_ceiling,
                "observed_at": now.isoformat(),
            },
            "executor": {
                "status": "healthy" if snapshot.executor_healthy else "unavailable",
                "observed_at": now.isoformat(),
            },
            "connectors": connectors,
            "kill_latched": False,
        }


@dataclass(frozen=True)
class ObservationContext:
    """One bounded, board-owned input shared by every observer."""

    board_id: str
    tickets: Mapping[str, Mapping[str, Any]]
    questions: tuple[Mapping[str, Any], ...]
    now: datetime
    questions_complete: bool = True
    tickets_complete: bool = True


@dataclass(frozen=True)
class ObservationRule:
    """A read-only predicate registered with the common observation engine."""

    name: str
    priority: int
    evaluate: Callable[[ObservationContext], Sequence[Mapping[str, Any]]]


@dataclass(frozen=True)
class EffectiveConfig:
    configured_mode: str
    answering_mode: str
    effective_answering_mode: str
    runtime_authorized: bool
    future_active_state: str
    demotion_reason: str | None
    answer_scope: dict[str, str]
    required_evidence_kinds: tuple[str, ...]
    drafts_per_hour: int
    drafts_per_ticket: int
    drafts_per_board: int
    hold_before_post_s: int
    active_windows: tuple[dict[str, Any], ...]
    kill_switch: bool
    veto_count: int
    failure_count: int
    veto_window_s: int
    classification_model: str | None
    classification_endpoint_ref: str | None
    classification_key_ref: str | None
    classification_extra_headers: dict[str, str]
    classification_key_header: str
    classification_key_prefix: str
    classification_validation_path: str
    classification_draft_path: str
    classification_draft_protocol: str
    drafting_model: str | None
    drafting_endpoint_ref: str | None
    drafting_key_ref: str | None
    drafting_extra_headers: dict[str, str]
    drafting_key_header: str
    drafting_key_prefix: str
    drafting_validation_path: str
    drafting_draft_path: str
    drafting_draft_protocol: str
    source_layers: tuple[str, ...]

    def as_finding(self) -> dict[str, Any]:
        return {
            "schema_version": BOARD_BUTLER_CONFIG_SCHEMA["schema_version"],
            "configured_mode": self.configured_mode,
            "answering_mode": self.answering_mode,
            "effective_mode": self.effective_answering_mode,
            "runtime_authorized": self.runtime_authorized,
            "future_active_state": self.future_active_state,
            "demotion_reason": self.demotion_reason,
            "answer_scope": dict(self.answer_scope),
            "required_evidence_kinds": list(self.required_evidence_kinds),
            "ceilings": {
                "per_hour": self.drafts_per_hour,
                "per_ticket": self.drafts_per_ticket,
                "per_board": self.drafts_per_board,
            },
            "hold_before_post_s": self.hold_before_post_s,
            "active_windows": [dict(item) for item in self.active_windows],
            "kill_switch": self.kill_switch,
            "auto_demote": {
                "veto_count": self.veto_count,
                "failure_count": self.failure_count,
                "window_s": self.veto_window_s,
            },
            "classification": {
                "model": self.classification_model,
                "endpoint_ref": self.classification_endpoint_ref,
                "key_ref": self.classification_key_ref,
                "extra_headers": dict(self.classification_extra_headers),
                "key_header": self.classification_key_header,
                "key_prefix": self.classification_key_prefix,
                "validation_path": self.classification_validation_path,
                "draft_path": self.classification_draft_path,
                "draft_protocol": self.classification_draft_protocol,
            },
            "drafting": {
                "model": self.drafting_model,
                "endpoint_ref": self.drafting_endpoint_ref,
                "key_ref": self.drafting_key_ref,
                "extra_headers": dict(self.drafting_extra_headers),
                "key_header": self.drafting_key_header,
                "key_prefix": self.drafting_key_prefix,
                "validation_path": self.drafting_validation_path,
                "draft_path": self.drafting_draft_path,
                "draft_protocol": self.drafting_draft_protocol,
            },
            "source_layers": list(self.source_layers),
            "precedence": list(BOARD_BUTLER_CONFIG_SCHEMA["precedence"]),
        }


@dataclass(frozen=True)
class ProviderRuntime:
    """Cycle-local provider values; the credential is deliberately non-representable."""

    endpoint: str
    model: str
    credential: str = field(repr=False)
    extra_headers: dict[str, str] = field(default_factory=dict)
    key_header: str = "Authorization"
    key_prefix: str = "Bearer"
    validation_path: str = "models"
    draft_path: str = "draft"
    draft_protocol: str = "pursers_json_v1"

    def request_headers(self) -> dict[str, str]:
        headers = dict(self.extra_headers)
        if self.credential:
            headers[self.key_header] = f"{self.key_prefix} {self.credential}".strip()
        return headers


def resolve_provider_runtime(
    config: EffectiveConfig, task: str, secrets_dir: str | Path | None = None
) -> ProviderRuntime | None:
    """Resolve one provider at cycle time while keeping its key out of findings."""
    if task not in {"classification", "drafting"}:
        raise ButlerConfigError("provider task is invalid")
    endpoint = getattr(config, f"{task}_endpoint_ref")
    model = getattr(config, f"{task}_model")
    key_ref = getattr(config, f"{task}_key_ref")
    if endpoint is None or model is None:
        return None
    credential = ""
    if key_ref is not None:
        match = re.fullmatch(r"file:([A-Za-z0-9._-]{1,160}\.key)", key_ref)
        if match is not None:
            root = (
                Path(secrets_dir).expanduser()
                if secrets_dir is not None
                else Path(os.environ.get("PURSERS_STATE_DIR", "~/.pursers")).expanduser()
                / "board-butler"
                / "secrets"
            )
            path = root / match.group(1)
        else:
            path = Path(key_ref)
        try:
            info = path.stat()
        except OSError as exc:
            raise ButlerConfigError(f"{task} credential reference is unavailable") from exc
        if (
            not path.is_absolute()
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 8_192
        ):
            raise ButlerConfigError(f"{task} credential reference is not a 0600 file")
        try:
            credential = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ButlerConfigError(f"{task} credential reference is unreadable") from exc
        if credential != credential.strip() or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in credential
        ):
            raise ButlerConfigError(
                f"{task} credential reference contains unsafe whitespace"
            )
    return ProviderRuntime(
        endpoint=endpoint,
        model=model,
        credential=credential,
        extra_headers=dict(getattr(config, f"{task}_extra_headers")),
        key_header=getattr(config, f"{task}_key_header"),
        key_prefix=getattr(config, f"{task}_key_prefix"),
        validation_path=getattr(config, f"{task}_validation_path"),
        draft_path=getattr(config, f"{task}_draft_path"),
        draft_protocol=getattr(config, f"{task}_draft_protocol"),
    )


def _provider_draft_text(document: Any) -> str | None:
    if not isinstance(document, Mapping):
        return None
    draft = document.get("draft")
    return draft if isinstance(draft, str) else None


def _openai_chat_draft_text(document: Any) -> str | None:
    if not isinstance(document, Mapping):
        return None
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _provider_origin(value: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("provider URL is invalid") from exc
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), port


class _ProviderRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep provider credentials on their originally configured origin."""

    def __init__(self, request_url: str) -> None:
        super().__init__()
        self._origin = _provider_origin(request_url)

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> urllib.request.Request | None:
        if _provider_origin(newurl) != self._origin:
            raise urllib.error.URLError("provider redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _provider_request_body(
    runtime: ProviderRuntime,
    question: Mapping[str, Any],
    finding: Mapping[str, Any],
) -> bytes:
    inputs = {
        "question": str(question.get("message", "")),
        "question_kind": str(question.get("kind", "information")),
        "verdict": finding.get("verdict"),
        "policy_rule": finding.get("policy_rule"),
        "evidence": finding.get("evidence"),
        "fallback_draft": finding.get("message"),
    }
    if runtime.draft_protocol == "pursers_json_v1":
        document = {
            "protocol": runtime.draft_protocol,
            "model": runtime.model,
            "input": inputs,
            "max_output_chars": MAX_PROVIDER_DRAFT_CHARS,
        }
    elif runtime.draft_protocol == "openai_chat_completions_v1":
        prompt = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
        if len(prompt) > MAX_PROVIDER_PROMPT_CHARS:
            raise ValueError("provider prompt exceeded the safe bound")
        document = {
            "model": runtime.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Draft one concise coordinator response from the supplied "
                        "policy result. Return only the draft text."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": MAX_PROVIDER_DRAFT_CHARS,
        }
    else:
        raise ValueError("provider draft protocol is unsupported")
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


async def draft_with_provider(
    runtime: ProviderRuntime,
    question: Mapping[str, Any],
    finding: Mapping[str, Any],
) -> str:
    """Create one bounded shadow draft without exposing provider credentials."""
    request_body = _provider_request_body(runtime, question, finding)

    document = await _post_provider_json(
        runtime,
        request_body,
        timeout_s=PROVIDER_TIMEOUT_S,
        max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
    )
    text = (
        _provider_draft_text(document)
        if runtime.draft_protocol == "pursers_json_v1"
        else _openai_chat_draft_text(document)
    )
    if text is None:
        raise ValueError("provider response had no draft text")
    text = text.strip()
    if (
        not text
        or len(text) > MAX_PROVIDER_DRAFT_CHARS
        or any(ord(character) < 0x20 and character not in "\n\t" for character in text)
        or (runtime.credential and runtime.credential in text)
    ):
        raise ValueError("provider draft was unsafe")
    return text


async def _post_provider_json(
    runtime: ProviderRuntime,
    request_body: bytes,
    *,
    timeout_s: float,
    max_response_bytes: int,
) -> Any:
    """Use the reviewed provider transport for every direct model request."""
    if timeout_s <= 0:
        raise ValueError("provider timeout must be positive")
    if max_response_bytes <= 0 or max_response_bytes > MAX_PROVIDER_RESPONSE_BYTES:
        raise ValueError("provider response limit is invalid")

    def request() -> Any:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **runtime.request_headers(),
        }
        raw = urllib.request.Request(
            urllib.parse.urljoin(
                runtime.endpoint.rstrip("/") + "/", runtime.draft_path.lstrip("/")
            ),
            data=request_body,
            headers=headers,
            method="POST",
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _ProviderRedirectHandler(raw.full_url),
        )
        with opener.open(raw, timeout=timeout_s) as response:
            geturl = getattr(response, "geturl", None)
            final_url = geturl() if callable(geturl) else raw.full_url
            if _provider_origin(final_url) != _provider_origin(raw.full_url):
                raise ValueError("provider response changed origin")
            payload = response.read(max_response_bytes + 1)
        if len(payload) > max_response_bytes:
            raise ValueError("provider response exceeded the safe bound")
        return json.loads(payload)

    return await asyncio.to_thread(request)


_CONNECTOR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_SENSITIVE_FIELD_RE = re.compile(
    r"authorization|cookie|token|secret|credential|environment|env|private[_-]?path",
    re.I,
)
_PRIVATE_PATH_RE = re.compile(
    r"(?:/(?:Users|home|private|var/folders)/[^\s\"']+|[A-Za-z]:\\[^\s\"']+)"
)


def _connector_id(value: Any, path: str) -> str:
    if not isinstance(value, str) or _CONNECTOR_ID_RE.fullmatch(value) is None:
        raise ConnectorConfigError(f"{path} must be an opaque identifier")
    return value


def _connector_int(value: Any, path: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ConnectorConfigError(
            f"{path} must be an integer from {minimum} to {maximum}"
        )
    return value


def _connector_keys(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConnectorConfigError(f"{path} has unknown keys: {', '.join(unknown)}")


@dataclass(frozen=True)
class ConnectorToolDeclaration:
    name: str
    effect: str
    replay: str
    stable_call_id_field: str | None

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, Any], path: str
    ) -> "ConnectorToolDeclaration":
        _connector_keys(
            value,
            {"name", "effect", "replay", "stable_call_id_field"},
            path,
        )
        if set(value) != {"name", "effect", "replay", "stable_call_id_field"}:
            raise ConnectorConfigError(f"{path} is missing required fields")
        name = _connector_id(value["name"], f"{path}.name")
        effect = value["effect"]
        replay = value["replay"]
        stable_field = value["stable_call_id_field"]
        if effect not in {"read_only", "mutating"}:
            raise ConnectorConfigError(f"{path}.effect is invalid")
        if replay not in {"safe_with_stable_call_id", "never"}:
            raise ConnectorConfigError(f"{path}.replay is invalid")
        if replay == "safe_with_stable_call_id":
            stable_field = _connector_id(
                stable_field, f"{path}.stable_call_id_field"
            )
        elif stable_field is not None:
            raise ConnectorConfigError(
                f"{path}.stable_call_id_field must be null when replay is never"
            )
        if effect == "read_only" and replay != "safe_with_stable_call_id":
            raise ConnectorConfigError(f"{path} read-only tools must be safely replayable")
        return cls(name, effect, replay, stable_field)


@dataclass(frozen=True)
class ConnectorLimits:
    timeout_ms: int
    max_input_bytes: int
    max_output_bytes: int
    max_concurrency: int
    calls_per_minute: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], path: str) -> "ConnectorLimits":
        required = {
            "timeout_ms",
            "max_input_bytes",
            "max_output_bytes",
            "max_concurrency",
            "calls_per_minute",
        }
        _connector_keys(value, required, path)
        if set(value) != required:
            raise ConnectorConfigError(f"{path} is missing required fields")
        return cls(
            _connector_int(value["timeout_ms"], f"{path}.timeout_ms", 100, 600_000),
            _connector_int(
                value["max_input_bytes"], f"{path}.max_input_bytes", 1, 1_048_576
            ),
            _connector_int(
                value["max_output_bytes"],
                f"{path}.max_output_bytes",
                1,
                4_194_304,
            ),
            _connector_int(
                value["max_concurrency"], f"{path}.max_concurrency", 1, 32
            ),
            _connector_int(
                value["calls_per_minute"], f"{path}.calls_per_minute", 1, 1_000
            ),
        )


@dataclass(frozen=True)
class ConnectorDeclaration:
    connector_id: str
    enabled: bool
    transport: str
    protocol_revision: str
    endpoint_ref: str
    secret_ref: str | None
    tools: tuple[ConnectorToolDeclaration, ...]
    resources: tuple[str, ...]
    risky_tools: frozenset[str]
    limits: ConnectorLimits

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ConnectorDeclaration":
        required = {
            "connector_id",
            "enabled",
            "transport",
            "protocol_revision",
            "endpoint_ref",
            "secret_ref",
            "tools",
            "resources",
            "limits",
        }
        _connector_keys(value, required | {"risky_tools"}, "connector")
        if not required.issubset(value):
            raise ConnectorConfigError("connector is missing required fields")
        connector_id = _connector_id(value["connector_id"], "connector.connector_id")
        if not isinstance(value["enabled"], bool):
            raise ConnectorConfigError("connector.enabled must be boolean")
        transport = value["transport"]
        if transport not in SUPPORTED_MCP_TRANSPORTS:
            raise ConnectorConfigError("connector.transport is unsupported")
        revision = value["protocol_revision"]
        if revision not in SUPPORTED_MCP_PROTOCOL_REVISIONS:
            raise ConnectorConfigError("connector.protocol_revision is unsupported")
        endpoint_ref = _connector_id(value["endpoint_ref"], "connector.endpoint_ref")
        secret_ref = value["secret_ref"]
        if secret_ref is not None:
            secret_ref = _connector_id(secret_ref, "connector.secret_ref")
        raw_tools = value["tools"]
        raw_resources = value["resources"]
        raw_risky = value.get("risky_tools", [])
        if not isinstance(raw_tools, list) or len(raw_tools) > 100:
            raise ConnectorConfigError("connector.tools must be a bounded array")
        if not isinstance(raw_resources, list) or len(raw_resources) > 100:
            raise ConnectorConfigError("connector.resources must be a bounded array")
        if not isinstance(raw_risky, list) or len(raw_risky) > 100:
            raise ConnectorConfigError("connector.risky_tools must be a bounded array")
        tools = tuple(
            ConnectorToolDeclaration.from_mapping(item, f"connector.tools[{index}]")
            if isinstance(item, Mapping)
            else (_ for _ in ()).throw(
                ConnectorConfigError(f"connector.tools[{index}] must be an object")
            )
            for index, item in enumerate(raw_tools)
        )
        tool_names = [item.name for item in tools]
        if len(set(tool_names)) != len(tool_names):
            raise ConnectorConfigError("connector tool names must be unique")
        resources: list[str] = []
        for index, item in enumerate(raw_resources):
            if not isinstance(item, str) or not 1 <= len(item) <= 300:
                raise ConnectorConfigError(
                    f"connector.resources[{index}] must be a bounded URI"
                )
            resources.append(item)
        if len(set(resources)) != len(resources):
            raise ConnectorConfigError("connector resources must be unique")
        risky = frozenset(
            _connector_id(item, f"connector.risky_tools[{index}]")
            for index, item in enumerate(raw_risky)
        )
        if len(risky) != len(raw_risky) or not risky.issubset(tool_names):
            raise ConnectorConfigError("connector.risky_tools must name declared tools")
        if any(tool.effect != "mutating" for tool in tools if tool.name in risky):
            raise ConnectorConfigError("connector.risky_tools must be mutating tools")
        raw_limits = value["limits"]
        if not isinstance(raw_limits, Mapping):
            raise ConnectorConfigError("connector.limits must be an object")
        return cls(
            connector_id,
            value["enabled"],
            transport,
            revision,
            endpoint_ref,
            secret_ref,
            tools,
            tuple(resources),
            risky,
            ConnectorLimits.from_mapping(raw_limits, "connector.limits"),
        )


@dataclass(frozen=True)
class StdioConnectorEndpoint:
    executable: str = field(repr=False)
    args: tuple[str, ...] = field(default=(), repr=False)
    cwd: str | None = field(default=None, repr=False)
    secret_env_name: str = "PURSERS_CONNECTOR_SECRET"

    def __post_init__(self) -> None:
        if not self.executable or "\x00" in self.executable:
            raise ConnectorConfigError("resolved stdio executable is invalid")
        if any(not isinstance(item, str) or "\x00" in item for item in self.args):
            raise ConnectorConfigError("resolved stdio arguments are invalid")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", self.secret_env_name):
            raise ConnectorConfigError("resolved stdio secret environment name is invalid")


@dataclass(frozen=True)
class HttpConnectorEndpoint:
    url: str = field(repr=False)
    secret_header: str = "Authorization"
    secret_prefix: str = "Bearer"

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ConnectorConfigError("resolved HTTP endpoint is invalid")
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise ConnectorConfigError("resolved HTTP endpoint requires TLS")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,79}", self.secret_header):
            raise ConnectorConfigError("resolved HTTP secret header is invalid")


ConnectorEndpoint = StdioConnectorEndpoint | HttpConnectorEndpoint


@dataclass(frozen=True)
class ConnectorPolicyRequest:
    board_id: str
    project_id: str
    connector_id: str
    operation_id: str
    tool_name: str
    effect: str
    arguments_sha256: str


@dataclass(frozen=True)
class ConnectorPolicyDecision:
    allowed: bool
    decision_id: str
    reason_code: str

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise ConnectorConfigError("policy decision allowed must be boolean")
        _connector_id(self.decision_id, "policy decision id")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", self.reason_code) is None:
            raise ConnectorConfigError("policy reason code is invalid")


class ConnectorPersistence(Protocol):
    async def reserve_call(self, call_id: str, payload_sha256: str) -> None: ...
    async def next_audit_sequence(self, board_id: str) -> int: ...
    async def append_audit(self, record: Mapping[str, Any]) -> None: ...


class InMemoryConnectorPersistence:
    """Test/local persistence; production integrations must supply durable storage."""

    def __init__(self) -> None:
        self.reservations: dict[str, str] = {}
        self.audit_records: list[dict[str, Any]] = []
        self.audit_sequences: dict[str, int] = {}

    async def reserve_call(self, call_id: str, payload_sha256: str) -> None:
        previous = self.reservations.setdefault(call_id, payload_sha256)
        if previous != payload_sha256:
            raise ConnectorDenied("stable connector call payload changed")

    async def next_audit_sequence(self, board_id: str) -> int:
        sequence = self.audit_sequences.get(board_id, 0) + 1
        self.audit_sequences[board_id] = sequence
        return sequence

    async def append_audit(self, record: Mapping[str, Any]) -> None:
        self.audit_records.append(dict(record))


@dataclass(frozen=True)
class ConnectorDiscovery:
    connector_id: str
    protocol_revision: str
    tools: tuple[dict[str, Any], ...]
    resources: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ConnectorResult:
    connector_id: str
    operation_id: str
    call_id: str
    payload_sha256: str
    payload: Any


@dataclass(frozen=True)
class ConnectorHealth:
    connector_id: str
    status: str
    observed_at: str
    reason_code: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None


SecretResolver = Callable[[str], str]
EndpointResolver = Callable[[str], ConnectorEndpoint]
PolicyGate = Callable[[ConnectorPolicyRequest], Awaitable[ConnectorPolicyDecision]]
ConnectorClientFactory = Callable[
    [ConnectorDeclaration, ConnectorEndpoint, str], AsyncContextManager[Any]
]


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ConnectorResultError("connector data is not canonical JSON") from exc


def _redact_untrusted(value: Any, secret: str, depth: int = 0) -> Any:
    if depth > 20:
        raise ConnectorResultError("connector result nesting exceeded the safe bound")
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key == "_meta":
                continue
            if (secret and secret in key) or _PRIVATE_PATH_RE.search(key):
                key = "[REDACTED_KEY]"
            if _SENSITIVE_FIELD_RE.search(key):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_untrusted(item, secret, depth + 1)
        return redacted
    if isinstance(value, list):
        return [_redact_untrusted(item, secret, depth + 1) for item in value]
    if isinstance(value, str):
        text = value.replace(secret, "[REDACTED]") if secret else value
        return _PRIVATE_PATH_RE.sub("[REDACTED_PATH]", text)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ConnectorResultError("connector result contains unsupported data")


def _model_payload(value: Any, limit: int, secret: str) -> tuple[Any, str]:
    try:
        dumped = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    except Exception as exc:
        raise ConnectorResultError("connector result is malformed") from exc
    raw = _canonical_json(dumped)
    if len(raw) > limit:
        raise ConnectorResultError("connector result exceeded the output byte limit")
    clean = _redact_untrusted(dumped, secret)
    clean_raw = _canonical_json(clean)
    if len(clean_raw) > limit:
        raise ConnectorResultError("connector result exceeded the output byte limit")
    return clean, hashlib.sha256(clean_raw).hexdigest()


def _connector_http_origin(value: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ConnectorConfigError("resolved HTTP endpoint is invalid") from exc
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), port


async def _resolve_connector_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
        )
        addresses = {
            str(ipaddress.ip_address(record[4][0].split("%", 1)[0]))
            for record in records
        }
    except (OSError, ValueError) as exc:
        raise ConnectorConfigError("resolved HTTP endpoint address is unavailable") from exc
    if not addresses:
        raise ConnectorConfigError("resolved HTTP endpoint address is unavailable")
    parsed = [ipaddress.ip_address(address) for address in addresses]
    if _is_loopback_host(host):
        if any(not address.is_loopback for address in parsed):
            raise ConnectorConfigError("resolved loopback HTTP endpoint escaped loopback")
    elif any(not address.is_global for address in parsed):
        raise ConnectorConfigError("resolved HTTP endpoint address is not public")
    return tuple(
        str(address)
        for address in sorted(parsed, key=lambda item: (item.version, item.packed))
    )


def _pinned_http_transport(
    origin_url: str,
    pinned_address: str,
    max_output_bytes: int,
) -> Any:
    """Create an HTTP transport that pins DNS and bounds MCP wire frames."""
    import httpx2

    origin = _connector_http_origin(origin_url)

    class BoundedResponseStream(httpx2.AsyncByteStream):
        def __init__(self, stream: Any, *, event_stream: bool) -> None:
            self._stream = stream
            self._event_stream = event_stream
            self._total = 0
            self._frame = 0
            self._tail = b""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            async for chunk in self._stream:
                if self._event_stream:
                    for byte in chunk:
                        self._frame += 1
                        self._tail = (self._tail + bytes((byte,)))[-4:]
                        delimiter = 0
                        if self._tail.endswith(b"\r\n\r\n"):
                            delimiter = 4
                        elif self._tail.endswith(b"\n\n") or self._tail.endswith(b"\r\r"):
                            delimiter = 2
                        if delimiter:
                            if self._frame - delimiter > max_output_bytes:
                                raise ConnectorResultError(
                                    "connector frame exceeded the output byte limit"
                                )
                            self._frame = 0
                            self._tail = b""
                        elif self._frame > max_output_bytes + 4:
                            raise ConnectorResultError(
                                "connector frame exceeded the output byte limit"
                            )
                else:
                    self._total += len(chunk)
                    if self._total > max_output_bytes:
                        raise ConnectorResultError(
                            "connector frame exceeded the output byte limit"
                        )
                yield chunk

        async def aclose(self) -> None:
            await self._stream.aclose()

    class PinnedTransport(httpx2.AsyncBaseTransport):
        def __init__(self) -> None:
            self._transport = httpx2.AsyncHTTPTransport(trust_env=False)

        async def handle_async_request(self, request: Any) -> Any:
            if _connector_http_origin(str(request.url)) != origin:
                raise ConnectorDenied("connector HTTP origin changed")
            extensions = dict(request.extensions)
            if origin[0] == "https":
                extensions["sni_hostname"] = origin[1]
            headers = request.headers.copy()
            headers["accept-encoding"] = "identity"
            pinned_request = httpx2.Request(
                request.method,
                request.url.copy_with(host=pinned_address),
                headers=headers,
                stream=request.stream,
                extensions=extensions,
            )
            response = await self._transport.handle_async_request(pinned_request)
            content_encoding = response.headers.get("content-encoding", "")
            encodings = tuple(
                encoding.strip().casefold()
                for encoding in content_encoding.split(",")
                if encoding.strip()
            )
            if content_encoding and (
                not encodings or any(encoding != "identity" for encoding in encodings)
            ):
                await response.aclose()
                raise ConnectorResultError(
                    "connector HTTP content encoding is not allowed"
                )
            event_stream = response.headers.get("content-type", "").casefold().startswith(
                "text/event-stream"
            )
            return httpx2.Response(
                response.status_code,
                headers=response.headers,
                stream=BoundedResponseStream(
                    response.stream,
                    event_stream=event_stream,
                ),
                extensions=response.extensions,
                request=request,
            )

        async def aclose(self) -> None:
            await self._transport.aclose()

    return PinnedTransport()


@asynccontextmanager
async def _bounded_stdio_client(
    server: Any,
    *,
    errlog: Any,
    max_output_bytes: int,
) -> AsyncIterator[Any]:
    """MCP SDK stdio transport with a raw newline-frame limit before parsing."""
    import anyio
    from mcp.client import stdio as sdk_stdio

    command = sdk_stdio._get_executable_command(server.command)
    process = await sdk_stdio._create_platform_compatible_process(
        command=command,
        args=server.args,
        env=sdk_stdio.get_default_environment() | (server.env or {}),
        errlog=errlog,
        cwd=server.cwd,
    )
    read_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_reader = anyio.create_memory_object_stream(0)
    shutting_down = False
    writer_done = anyio.Event()

    async def stdout_reader() -> None:
        assert process.stdout
        buffer = bytearray()
        frame_rejected = False
        try:
            async with read_writer:
                try:
                    while True:
                        chunk = await process.stdout.receive()
                        buffer.extend(chunk)
                        while True:
                            newline = buffer.find(b"\n")
                            if newline < 0:
                                break
                            raw_line = bytes(buffer[:newline])
                            del buffer[: newline + 1]
                            if len(raw_line) > max_output_bytes:
                                frame_rejected = True
                                await read_writer.send(
                                    ConnectorResultError(
                                        "connector frame exceeded the output byte limit"
                                    )
                                )
                                return
                            line = raw_line.decode(
                                server.encoding,
                                errors=server.encoding_error_handler,
                            )
                            await read_writer.send(sdk_stdio._parse_line(line))
                        if len(buffer) > max_output_bytes:
                            frame_rejected = True
                            await read_writer.send(
                                ConnectorResultError(
                                    "connector frame exceeded the output byte limit"
                                )
                            )
                            return
                finally:
                    if not frame_rejected:
                        await sdk_stdio._drain_stdout(process)
        except (
            anyio.EndOfStream,
            anyio.ClosedResourceError,
            anyio.BrokenResourceError,
        ):
            pass
        except (ConnectionError, OSError):
            if not shutting_down:
                raise

    async def stdin_writer() -> None:
        assert process.stdin
        try:
            async with write_reader:
                async for session_message in write_reader:
                    payload = session_message.message.model_dump_json(
                        by_alias=True,
                        exclude_unset=True,
                    )
                    data = (payload + "\n").encode(
                        encoding=server.encoding,
                        errors=server.encoding_error_handler,
                    )
                    await process.stdin.send(data)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            await read_writer.aclose()
        finally:
            writer_done.set()

    async def shutdown() -> None:
        read_stream.close()
        write_stream.close()
        with anyio.move_on_after(sdk_stdio._WRITER_FLUSH_TIMEOUT):
            await writer_done.wait()
        await sdk_stdio._stop_server_process(process)
        await sdk_stdio._aclose_all(
            read_stream,
            write_stream,
            read_writer,
            write_reader,
        )
        await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(stdout_reader)
        task_group.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream
        finally:
            shutting_down = True
            with anyio.CancelScope(shield=True):
                await shutdown()
            task_group.cancel_scope.cancel()
    await anyio.lowlevel.cancel_shielded_checkpoint()


class ConnectorRuntime:
    """Board-scoped, fail-closed MCP v2 client boundary.

    Endpoint and secret references are resolved only inside a connection attempt.
    The runtime never places resolved values in results, health, policy requests, or
    audit records. A durable ``ConnectorPersistence`` implementation is required by
    production callers so stable call reservations precede dispatch.
    """

    def __init__(
        self,
        *,
        board_id: str,
        project_id: str,
        actor_id: str,
        policy_digest_sha256: str,
        declaration: ConnectorDeclaration,
        approved_connector_ids: Sequence[str],
        endpoint_resolver: EndpointResolver,
        secret_resolver: SecretResolver,
        persistence: ConnectorPersistence,
        policy_gate: PolicyGate | None = None,
        client_factory: ConnectorClientFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.board_id = _connector_id(board_id, "board_id")
        self.project_id = _connector_id(project_id, "project_id")
        self.actor_id = _connector_id(actor_id, "actor_id")
        if re.fullmatch(r"[0-9a-f]{64}", policy_digest_sha256) is None:
            raise ConnectorConfigError("policy digest is invalid")
        self.policy_digest_sha256 = policy_digest_sha256
        self.declaration = declaration
        if isinstance(approved_connector_ids, (str, bytes)) or len(
            approved_connector_ids
        ) > 32:
            raise ConnectorConfigError("approved connector ids are invalid")
        approved = {
            _connector_id(item, "approved_connector_ids")
            for item in approved_connector_ids
        }
        if declaration.connector_id not in approved:
            raise ConnectorDenied("connector is outside the immutable envelope")
        self.endpoint_resolver = endpoint_resolver
        self.secret_resolver = secret_resolver
        self.persistence = persistence
        self.policy_gate = policy_gate
        self.client_factory = client_factory or self._default_client
        self.clock = clock
        self._slots = asyncio.Semaphore(declaration.limits.max_concurrency)
        self._rate_lock = asyncio.Lock()
        self._rate_events: deque[float] = deque()
        self._http_pin_lock = asyncio.Lock()
        self._http_address_pins: dict[str, tuple[str, ...]] = {}
        now = utc_now().isoformat()
        self._health = ConnectorHealth(declaration.connector_id, "unknown", now)

    @property
    def health(self) -> ConnectorHealth:
        return self._health

    def _resolve_private(self) -> tuple[ConnectorEndpoint, str]:
        endpoint = self.endpoint_resolver(self.declaration.endpoint_ref)
        if self.declaration.transport == "stdio" and not isinstance(
            endpoint, StdioConnectorEndpoint
        ):
            raise ConnectorConfigError("resolved endpoint transport mismatch")
        if self.declaration.transport == "streamable_http" and not isinstance(
            endpoint, HttpConnectorEndpoint
        ):
            raise ConnectorConfigError("resolved endpoint transport mismatch")
        secret = ""
        if self.declaration.secret_ref is not None:
            try:
                secret = self.secret_resolver(self.declaration.secret_ref)
            except Exception:
                raise ConnectorConfigError(
                    "connector secret reference is unavailable"
                ) from None
            if (
                not isinstance(secret, str)
                or not secret
                or len(secret.encode("utf-8")) > 8_192
                or secret != secret.strip()
                or any(ord(char) < 0x20 or ord(char) == 0x7F for char in secret)
            ):
                raise ConnectorConfigError("connector secret reference is invalid")
        return endpoint, secret

    async def _pin_http_endpoint(self, endpoint: HttpConnectorEndpoint) -> str:
        _scheme, host, port = _connector_http_origin(endpoint.url)
        async with self._http_pin_lock:
            resolved = await _resolve_connector_addresses(host, port)
            previous = self._http_address_pins.get(endpoint.url)
            if previous is not None and previous != resolved:
                raise ConnectorConfigError("resolved HTTP endpoint address changed")
            self._http_address_pins[endpoint.url] = resolved
            return resolved[0]

    @asynccontextmanager
    async def _default_client(
        self,
        declaration: ConnectorDeclaration,
        endpoint: ConnectorEndpoint,
        secret: str,
    ) -> AsyncIterator[Any]:
        try:
            from mcp import Client, StdioServerParameters
        except ImportError as exc:
            raise ConnectorProtocolError("MCP v2 client SDK is unavailable") from exc
        timeout_s = declaration.limits.timeout_ms / 1_000
        async with AsyncExitStack() as stack:
            if isinstance(endpoint, StdioConnectorEndpoint):
                environment = (
                    {endpoint.secret_env_name: secret} if declaration.secret_ref else None
                )
                server = StdioServerParameters(
                    command=endpoint.executable,
                    args=list(endpoint.args),
                    env=environment,
                    cwd=endpoint.cwd,
                )
                error_sink = open(os.devnull, "w", encoding="utf-8")
                stack.callback(error_sink.close)
                transport = _bounded_stdio_client(
                    server,
                    errlog=error_sink,
                    max_output_bytes=declaration.limits.max_output_bytes,
                )
                client = Client(
                    transport,
                    mode=declaration.protocol_revision,
                    cache=None,
                    read_timeout_seconds=timeout_s,
                )
            else:
                try:
                    import httpx2
                    from mcp.client.streamable_http import streamable_http_client
                except ImportError as exc:
                    raise ConnectorProtocolError(
                        "MCP v2 HTTP transport is unavailable"
                    ) from exc
                headers = {"Accept-Encoding": "identity"}
                if declaration.secret_ref:
                    headers[endpoint.secret_header] = (
                        f"{endpoint.secret_prefix} {secret}".strip()
                    )
                pinned_address = await self._pin_http_endpoint(endpoint)
                http_client = await stack.enter_async_context(
                    httpx2.AsyncClient(
                        headers=headers,
                        follow_redirects=False,
                        trust_env=False,
                        timeout=httpx2.Timeout(timeout_s),
                        transport=_pinned_http_transport(
                            endpoint.url,
                            pinned_address,
                            declaration.limits.max_output_bytes,
                        ),
                    )
                )
                transport = streamable_http_client(
                    endpoint.url, http_client=http_client
                )
                client = Client(
                    transport,
                    mode=declaration.protocol_revision,
                    cache=None,
                    read_timeout_seconds=timeout_s,
                )
            connected = await stack.enter_async_context(client)
            if connected.protocol_version != declaration.protocol_revision:
                raise ConnectorProtocolError("connector protocol revision mismatch")
            yield connected

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            now = self.clock()
            while self._rate_events and now - self._rate_events[0] >= 60:
                self._rate_events.popleft()
            if len(self._rate_events) >= self.declaration.limits.calls_per_minute:
                raise ConnectorDenied("connector rate limit exceeded")
            self._rate_events.append(now)

    async def _audit(
        self,
        action: str,
        outcome: str,
        operation_id: str,
        *,
        reason_code: str | None = None,
        call_id: str | None = None,
    ) -> None:
        detail = {
            "connector_id": self.declaration.connector_id,
            "operation_id": operation_id,
            "reason_code": reason_code,
            "call_id": call_id,
        }
        detail_json = _canonical_json(detail).decode("utf-8")
        if len(detail_json) > MAX_CONNECTOR_AUDIT_DETAIL_CHARS:
            detail_json = '{"reason_code":"detail_dropped"}'
        sequence = await self.persistence.next_audit_sequence(self.board_id)
        occurred_at = utc_now().isoformat()
        audit_material = (
            f"{self.board_id}\x00{sequence}\x00{action}\x00{operation_id}\x00{occurred_at}"
        ).encode("utf-8")
        record: dict[str, Any] = {
            "schema": "autonomous_butler_audit_v1",
            "schema_version": 1,
            "audit_id": f"audit:{hashlib.sha256(audit_material).hexdigest()}",
            "board_id": self.board_id,
            "sequence": sequence,
            "occurred_at": occurred_at,
            "actor_id": self.actor_id,
            "category": "connector",
            "action": action,
            "outcome": outcome,
            "policy_digest_sha256": self.policy_digest_sha256,
            "operation_id": operation_id,
            "target_ref": self.declaration.connector_id,
            "detail": detail_json,
            "detail_redacted": True,
        }
        if reason_code is not None:
            record["reason_code"] = reason_code
        await self.persistence.append_audit(
            record
        )

    async def _deny(
        self,
        action: str,
        operation_id: str,
        message: str,
        reason_code: str,
        *,
        call_id: str | None = None,
    ) -> None:
        await self._audit(
            action,
            "denied",
            operation_id,
            reason_code=reason_code,
            call_id=call_id,
        )
        raise ConnectorDenied(message)

    def _mark_success(self) -> None:
        now = utc_now().isoformat()
        self._health = ConnectorHealth(
            self.declaration.connector_id,
            "healthy",
            now,
            last_success_at=now,
            last_failure_at=self._health.last_failure_at,
        )

    def _mark_failure(self, reason_code: str) -> None:
        now = utc_now().isoformat()
        self._health = ConnectorHealth(
            self.declaration.connector_id,
            "degraded",
            now,
            reason_code=reason_code,
            last_success_at=self._health.last_success_at,
            last_failure_at=now,
        )

    async def _all_listed(self, client: Any, method: str) -> list[Any]:
        values: list[Any] = []
        cursor: str | None = None
        for _page in range(10):
            result = await getattr(client, method)(cursor=cursor, cache_mode="bypass")
            _model_payload(result, self.declaration.limits.max_output_bytes, "")
            field = "tools" if method == "list_tools" else "resources"
            page_values = getattr(result, field, None)
            if not isinstance(page_values, list):
                raise ConnectorProtocolError("connector discovery result is malformed")
            values.extend(page_values)
            if len(values) > 200:
                raise ConnectorResultError("connector discovery exceeded the item limit")
            cursor = getattr(result, "next_cursor", None)
            if not cursor:
                return values
        raise ConnectorResultError("connector discovery exceeded the page limit")

    async def _discover_on_client(
        self, client: Any, secret: str
    ) -> tuple[ConnectorDiscovery, dict[str, Any]]:
        listed_tools = await self._all_listed(client, "list_tools")
        listed_resources = await self._all_listed(client, "list_resources")
        allowed_tools = {item.name for item in self.declaration.tools}
        tools: list[dict[str, Any]] = []
        schemas: dict[str, Any] = {}
        for item in listed_tools:
            name = getattr(item, "name", None)
            if name not in allowed_tools:
                continue
            schema = getattr(item, "input_schema", None)
            if not isinstance(schema, Mapping):
                raise ConnectorProtocolError("connector tool schema is malformed")
            schemas[name] = dict(schema)
            tools.append(
                {
                    "name": name,
                    "description": _redact_untrusted(
                        getattr(item, "description", None), secret
                    ),
                    "input_schema": _redact_untrusted(dict(schema), secret),
                }
            )
        allowed_resources = set(self.declaration.resources)
        resources: list[dict[str, Any]] = []
        for item in listed_resources:
            uri = str(getattr(item, "uri", ""))
            if uri in allowed_resources:
                resources.append(
                    {
                        "uri": uri,
                        "name": _redact_untrusted(getattr(item, "name", ""), secret),
                        "mime_type": getattr(item, "mime_type", None),
                    }
                )
        discovery = ConnectorDiscovery(
            self.declaration.connector_id,
            self.declaration.protocol_revision,
            tuple(tools),
            tuple(resources),
        )
        raw = _canonical_json(
            {"tools": discovery.tools, "resources": discovery.resources}
        )
        if len(raw) > self.declaration.limits.max_output_bytes:
            raise ConnectorResultError("filtered discovery exceeded the output byte limit")
        return discovery, schemas

    async def discover(self, operation_id: str) -> ConnectorDiscovery:
        operation_id = _connector_id(operation_id, "operation_id")
        if not self.declaration.enabled:
            await self._deny(
                "discover", operation_id, "connector is disabled", "disabled"
            )
        await self._rate_limit()
        timeout_s = self.declaration.limits.timeout_ms / 1_000
        async with self._slots:
            for attempt in range(2):
                try:
                    endpoint, secret = self._resolve_private()
                    async with asyncio.timeout(timeout_s):
                        async with self.client_factory(
                            self.declaration, endpoint, secret
                        ) as client:
                            discovery, _schemas = await self._discover_on_client(
                                client, secret
                            )
                    self._mark_success()
                    await self._audit("discover", "succeeded", operation_id)
                    return discovery
                except asyncio.CancelledError:
                    self._mark_failure("cancelled")
                    await self._audit(
                        "discover", "cancelled", operation_id, reason_code="cancelled"
                    )
                    raise
                except ConnectorDenied:
                    raise
                except (ConnectorConfigError, ConnectorResultError):
                    self._mark_failure("invalid_result")
                    await self._audit(
                        "discover",
                        "failed",
                        operation_id,
                        reason_code="invalid_result",
                    )
                    raise
                except Exception as exc:
                    if attempt == 0:
                        await asyncio.sleep(0)
                        continue
                    self._mark_failure("connection_failed")
                    await self._audit(
                        "discover",
                        "failed",
                        operation_id,
                        reason_code="connection_failed",
                    )
                    raise ConnectorProtocolError("connector discovery failed") from None
        raise AssertionError("unreachable")

    async def call_tool(
        self, operation_id: str, tool_name: str, arguments: Mapping[str, Any]
    ) -> ConnectorResult:
        operation_id = _connector_id(operation_id, "operation_id")
        tool = next(
            (item for item in self.declaration.tools if item.name == tool_name), None
        )
        if tool is None or not self.declaration.enabled:
            await self._deny(
                "call_tool",
                operation_id,
                "connector tool is not enabled and allowlisted",
                "not_allowlisted",
            )
        # Canonical bytes are the ownership boundary for caller-controlled input.
        # Keep them as the single immutable source across every await below: a
        # shallow dict copy would still let the caller mutate nested values after
        # the durable reservation or policy decision.
        original_bytes = _canonical_json(arguments)
        original = json.loads(original_bytes)
        if len(original_bytes) > self.declaration.limits.max_input_bytes:
            await self._deny(
                "call_tool",
                operation_id,
                "connector input exceeded the byte limit",
                "input_too_large",
            )
        call_material = b"\x00".join(
            (
                self.board_id.encode(),
                self.declaration.connector_id.encode(),
                operation_id.encode(),
                tool.name.encode(),
                original_bytes,
            )
        )
        call_id = hashlib.sha256(call_material).hexdigest()
        dispatched = dict(original)
        if tool.stable_call_id_field is not None:
            existing = dispatched.get(tool.stable_call_id_field)
            if existing not in {None, call_id}:
                await self._deny(
                    "call_tool",
                    operation_id,
                    "caller supplied a conflicting stable call id",
                    "call_id_conflict",
                    call_id=call_id,
                )
            dispatched[tool.stable_call_id_field] = call_id
        payload_bytes = _canonical_json(dispatched)
        if len(payload_bytes) > self.declaration.limits.max_input_bytes:
            await self._deny(
                "call_tool",
                operation_id,
                "connector input exceeded the byte limit",
                "input_too_large",
                call_id=call_id,
            )
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        await self.persistence.reserve_call(call_id, payload_sha256)
        if tool.name in self.declaration.risky_tools:
            if self.policy_gate is None:
                await self._deny(
                    "call_tool",
                    operation_id,
                    "risky connector tool has no policy gate",
                    "policy_gate_missing",
                    call_id=call_id,
                )
            decision = await self.policy_gate(
                ConnectorPolicyRequest(
                    self.board_id,
                    self.project_id,
                    self.declaration.connector_id,
                    operation_id,
                    tool.name,
                    tool.effect,
                    hashlib.sha256(original_bytes).hexdigest(),
                )
            )
            if (
                not isinstance(decision, ConnectorPolicyDecision)
                or decision.allowed is not True
            ):
                await self._deny(
                    "call_tool",
                    operation_id,
                    "risky connector tool was denied by policy",
                    "policy_denied",
                    call_id=call_id,
                )
        await self._rate_limit()
        attempts = 2 if tool.replay == "safe_with_stable_call_id" else 1
        timeout_s = self.declaration.limits.timeout_ms / 1_000
        async with self._slots:
            for attempt in range(attempts):
                try:
                    endpoint, secret = self._resolve_private()
                    async with asyncio.timeout(timeout_s):
                        async with self.client_factory(
                            self.declaration, endpoint, secret
                        ) as client:
                            _discovery, schemas = await self._discover_on_client(
                                client, secret
                            )
                            schema = schemas.get(tool.name)
                            if schema is None:
                                await self._deny(
                                    "call_tool",
                                    operation_id,
                                    "connector tool is absent from filtered discovery",
                                    "discovery_denied",
                                    call_id=call_id,
                                )
                            dispatched = json.loads(payload_bytes)
                            try:
                                from jsonschema import Draft202012Validator

                                Draft202012Validator(schema).validate(dispatched)
                            except ConnectorError:
                                raise
                            except Exception:
                                await self._deny(
                                    "call_tool",
                                    operation_id,
                                    "connector arguments failed the discovered schema",
                                    "schema_denied",
                                    call_id=call_id,
                                )
                            result = await client.call_tool(
                                tool.name,
                                dispatched,
                                read_timeout_seconds=timeout_s,
                            )
                            clean, result_sha256 = _model_payload(
                                result,
                                self.declaration.limits.max_output_bytes,
                                secret,
                            )
                            if bool(getattr(result, "is_error", False)):
                                raise ConnectorResultError("connector tool returned an error")
                    self._mark_success()
                    await self._audit(
                        "call_tool", "succeeded", operation_id, call_id=call_id
                    )
                    return ConnectorResult(
                        self.declaration.connector_id,
                        operation_id,
                        call_id,
                        result_sha256,
                        clean,
                    )
                except asyncio.CancelledError:
                    self._mark_failure("cancelled")
                    await self._audit(
                        "call_tool",
                        "cancelled",
                        operation_id,
                        reason_code="cancelled",
                        call_id=call_id,
                    )
                    raise
                except ConnectorDenied:
                    raise
                except (ConnectorConfigError, ConnectorResultError):
                    self._mark_failure("invalid_result")
                    await self._audit(
                        "call_tool",
                        "failed",
                        operation_id,
                        reason_code="invalid_result",
                        call_id=call_id,
                    )
                    raise
                except Exception as exc:
                    if attempt + 1 < attempts:
                        await asyncio.sleep(0)
                        continue
                    self._mark_failure("connection_failed")
                    await self._audit(
                        "call_tool",
                        "failed",
                        operation_id,
                        reason_code="connection_failed",
                        call_id=call_id,
                    )
                    raise ConnectorProtocolError("connector tool call failed") from None
        raise AssertionError("unreachable")

    async def read_resource(
        self, operation_id: str, uri: str
    ) -> ConnectorResult:
        operation_id = _connector_id(operation_id, "operation_id")
        if not self.declaration.enabled or uri not in self.declaration.resources:
            await self._deny(
                "read_resource",
                operation_id,
                "connector resource is not enabled and allowlisted",
                "not_allowlisted",
            )
        request_bytes = _canonical_json({"uri": uri})
        if len(request_bytes) > self.declaration.limits.max_input_bytes:
            await self._deny(
                "read_resource",
                operation_id,
                "connector input exceeded the byte limit",
                "input_too_large",
            )
        call_id = hashlib.sha256(
            b"\x00".join(
                (
                    self.board_id.encode(),
                    self.declaration.connector_id.encode(),
                    operation_id.encode(),
                    b"resources/read",
                    request_bytes,
                )
            )
        ).hexdigest()
        payload_sha256 = hashlib.sha256(request_bytes).hexdigest()
        await self.persistence.reserve_call(call_id, payload_sha256)
        await self._rate_limit()
        timeout_s = self.declaration.limits.timeout_ms / 1_000
        async with self._slots:
            for attempt in range(2):
                try:
                    endpoint, secret = self._resolve_private()
                    async with asyncio.timeout(timeout_s):
                        async with self.client_factory(
                            self.declaration, endpoint, secret
                        ) as client:
                            discovery, _schemas = await self._discover_on_client(
                                client, secret
                            )
                            if uri not in {item["uri"] for item in discovery.resources}:
                                await self._deny(
                                    "read_resource",
                                    operation_id,
                                    "connector resource is absent from filtered discovery",
                                    "discovery_denied",
                                    call_id=call_id,
                                )
                            result = await client.read_resource(uri, cache_mode="bypass")
                            clean, result_sha256 = _model_payload(
                                result,
                                self.declaration.limits.max_output_bytes,
                                secret,
                            )
                    self._mark_success()
                    await self._audit(
                        "read_resource", "succeeded", operation_id, call_id=call_id
                    )
                    return ConnectorResult(
                        self.declaration.connector_id,
                        operation_id,
                        call_id,
                        result_sha256,
                        clean,
                    )
                except asyncio.CancelledError:
                    self._mark_failure("cancelled")
                    await self._audit(
                        "read_resource",
                        "cancelled",
                        operation_id,
                        reason_code="cancelled",
                        call_id=call_id,
                    )
                    raise
                except ConnectorDenied:
                    raise
                except (ConnectorConfigError, ConnectorResultError):
                    self._mark_failure("invalid_result")
                    await self._audit(
                        "read_resource",
                        "failed",
                        operation_id,
                        reason_code="invalid_result",
                        call_id=call_id,
                    )
                    raise
                except Exception as exc:
                    if attempt == 0:
                        await asyncio.sleep(0)
                        continue
                    self._mark_failure("connection_failed")
                    await self._audit(
                        "read_resource",
                        "failed",
                        operation_id,
                        reason_code="connection_failed",
                        call_id=call_id,
                    )
                    raise ConnectorProtocolError("connector resource read failed") from None
        raise AssertionError("unreachable")
def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _bounded_model_id(value: Any, fallback: str) -> str:
    candidate = value if isinstance(value, str) else ""
    return (
        candidate
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", candidate)
        else fallback
    )


def _bounded_sha256(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return "0" * 64


def _parse_utc_deadline(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("model deadline must be a date-time string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("model deadline is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("model deadline must include a timezone")
    return parsed.astimezone(timezone.utc)


class ModelRunnerFailure(Exception):
    """A bounded typed failure that is safe to persist and return."""

    def __init__(self, category: str, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.category = category
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ModelBackendResponse:
    proposal_json: str
    citations: tuple[str, ...]
    usage: Mapping[str, Any]
    provider_request_ref: str


class ModelBackend(Protocol):
    async def run(
        self, request: Mapping[str, Any], *, timeout_s: float
    ) -> ModelBackendResponse: ...


class ModelResultStore(Protocol):
    def get(self, request_id: str) -> tuple[str, dict[str, Any]] | None: ...

    def put(
        self, request_id: str, request_digest: str, result: Mapping[str, Any]
    ) -> None: ...


class MemoryModelResultStore:
    """Cycle-local replay store used by tests and ephemeral runners."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, dict[str, Any]]] = {}

    def get(self, request_id: str) -> tuple[str, dict[str, Any]] | None:
        row = self._rows.get(request_id)
        return None if row is None else (row[0], copy.deepcopy(row[1]))

    def put(
        self, request_id: str, request_digest: str, result: Mapping[str, Any]
    ) -> None:
        self._rows[request_id] = (request_digest, copy.deepcopy(dict(result)))


class FileModelResultStore:
    """Private atomic result-before-reply store for crash-safe replay."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        try:
            info = self.root.lstat()
        except FileNotFoundError:
            self.root.mkdir(parents=True, mode=0o700)
            info = self.root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
        ):
            raise ModelRunnerFailure("policy", "unsafe_replay_directory")

    def _path(self, request_id: str) -> Path:
        name = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self.root / f"{name}.json"

    def get(self, request_id: str) -> tuple[str, dict[str, Any]] | None:
        path = self._path(request_id)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or info.st_size > MAX_PROVIDER_RESPONSE_BYTES + 65_536
        ):
            raise ModelRunnerFailure("policy", "unsafe_replay_record")
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ModelRunnerFailure("malformed", "invalid_replay_record") from exc
        if (
            not isinstance(row, dict)
            or set(row) != {"request_id", "request_digest_sha256", "result"}
            or row.get("request_id") != request_id
            or not isinstance(row.get("request_digest_sha256"), str)
            or not isinstance(row.get("result"), dict)
        ):
            raise ModelRunnerFailure("malformed", "invalid_replay_record")
        return row["request_digest_sha256"], copy.deepcopy(row["result"])

    def put(
        self, request_id: str, request_digest: str, result: Mapping[str, Any]
    ) -> None:
        path = self._path(request_id)
        row = {
            "request_id": request_id,
            "request_digest_sha256": request_digest,
            "result": dict(result),
        }
        encoded = _canonical_json_bytes(row) + b"\n"
        if len(encoded) > MAX_PROVIDER_RESPONSE_BYTES + 65_536:
            raise ModelRunnerFailure("malformed", "replay_record_too_large")
        descriptor, temporary = tempfile.mkstemp(prefix=".model-result-", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)


class TaskSchemaRegistry:
    """Binds schema identifiers to exact canonical bytes and validators."""

    def __init__(self) -> None:
        self._schemas: dict[tuple[str, str], Mapping[str, Any]] = {}

    def register(self, schema_id: str, schema: Mapping[str, Any]) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", schema_id):
            raise ValueError("task schema id is invalid")
        document = copy.deepcopy(dict(schema))
        digest = _sha256_json(document)
        try:
            import jsonschema

            jsonschema.Draft202012Validator.check_schema(document)
        except ImportError as exc:
            raise RuntimeError("jsonschema is required for model task validation") from exc
        self._schemas[(schema_id, digest)] = document
        return digest

    def validate(self, binding: Mapping[str, Any], value: Any) -> None:
        if not isinstance(binding, Mapping):
            raise ModelRunnerFailure("malformed", "invalid_task_schema_binding")
        key = (binding.get("schema_id"), binding.get("schema_sha256"))
        schema = self._schemas.get(key)
        if schema is None:
            raise ModelRunnerFailure("policy", "unknown_task_schema")
        import jsonschema

        try:
            jsonschema.Draft202012Validator(
                schema, format_checker=jsonschema.FormatChecker()
            ).validate(value)
        except jsonschema.ValidationError as exc:
            raise ModelRunnerFailure("malformed", "task_schema_rejected") from exc


class ModelCancellationRegistry:
    """Opaque-token cancellation shared by all model backends."""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def event(self, token: str) -> asyncio.Event:
        return self._events.setdefault(token, asyncio.Event())

    def cancel(self, token: str) -> None:
        self.event(token).set()


def _backend_payload(value: Any, expected_model: str) -> ModelBackendResponse:
    if not isinstance(value, Mapping) or set(value) != {
        "model",
        "proposal",
        "citations",
        "usage",
        "provider_request_ref",
    }:
        raise ModelRunnerFailure("malformed", "invalid_backend_payload")
    if value.get("model") != expected_model:
        raise ModelRunnerFailure("policy", "model_mismatch")
    citations = value.get("citations")
    usage = value.get("usage")
    provider_ref = value.get("provider_request_ref")
    if (
        not isinstance(citations, list)
        or not all(isinstance(item, str) for item in citations)
        or not isinstance(usage, Mapping)
        or not isinstance(provider_ref, str)
    ):
        raise ModelRunnerFailure("malformed", "invalid_backend_payload")
    return ModelBackendResponse(
        proposal_json=_canonical_json_bytes(value["proposal"]).decode("utf-8"),
        citations=tuple(citations),
        usage=dict(usage),
        provider_request_ref=provider_ref,
    )


class DirectAPIModelBackend:
    """OpenAI-compatible backend using the reviewed Board Butler transport."""

    def __init__(self, runtime: ProviderRuntime) -> None:
        if runtime.draft_protocol != "openai_chat_completions_v1":
            raise ValueError("direct model backend requires openai_chat_completions_v1")
        self.runtime = runtime

    async def run(
        self, request: Mapping[str, Any], *, timeout_s: float
    ) -> ModelBackendResponse:
        safe_envelope = {
            key: request[key]
            for key in (
                "request_id",
                "board_id",
                "subject_id",
                "task_kind",
                "policy_digest_sha256",
                "task_input",
                "task_input_digest_sha256",
                "task_schema",
                "evidence_refs",
                "max_output_bytes",
                "usage_limit",
                "deadline",
            )
        }
        prompt = _canonical_json_bytes(safe_envelope).decode("utf-8")
        if len(prompt.encode("utf-8")) > MAX_PROVIDER_PROMPT_CHARS:
            raise ModelRunnerFailure("budget", "provider_prompt_too_large")
        body = _canonical_json_bytes(
            {
                "model": self.runtime.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Return exactly one JSON object with keys proposal and "
                            "citations. Do not call tools."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": request["usage_limit"]["max_output_tokens"],
                "response_format": {"type": "json_object"},
            }
        )
        try:
            document = await _post_provider_json(
                self.runtime,
                body,
                timeout_s=timeout_s,
                max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
            )
            content = _openai_chat_draft_text(document)
            if content is None:
                raise ModelRunnerFailure("malformed", "missing_provider_content")
            if self.runtime.credential and self.runtime.credential in content:
                raise ModelRunnerFailure("policy", "provider_credential_echo")
            payload = json.loads(content)
        except ModelRunnerFailure:
            raise
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            raise ModelRunnerFailure("malformed", "malformed_provider_response") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ModelRunnerFailure(
                "provider", "direct_provider_error", retryable=True
            ) from exc
        if (
            not isinstance(document, Mapping)
            or not isinstance(payload, Mapping)
            or set(payload) != {"proposal", "citations"}
        ):
            raise ModelRunnerFailure("malformed", "invalid_backend_payload")
        if document.get("model") != self.runtime.model:
            raise ModelRunnerFailure("policy", "model_mismatch")
        provider_ref = document.get("id")
        citations = payload.get("citations")
        raw_usage = document.get("usage")
        if (
            not isinstance(provider_ref, str)
            or not isinstance(citations, list)
            or not all(isinstance(item, str) for item in citations)
            or not isinstance(raw_usage, Mapping)
        ):
            raise ModelRunnerFailure("malformed", "invalid_backend_payload")
        if self.runtime.credential and self.runtime.credential in provider_ref:
            raise ModelRunnerFailure("policy", "provider_credential_echo")
        usage = {
            "input_tokens": raw_usage.get("prompt_tokens"),
            "output_tokens": raw_usage.get("completion_tokens"),
            "total_tokens": raw_usage.get("total_tokens"),
            "cost_microunits": raw_usage.get("cost_microunits"),
            "measured": raw_usage.get("measured"),
        }
        return ModelBackendResponse(
            proposal_json=_canonical_json_bytes(payload["proposal"]).decode("utf-8"),
            citations=tuple(citations),
            usage=usage,
            provider_request_ref=provider_ref,
        )


_ACP_CLIENT_MODULE: Any = None
_ACP_SEAT_MODULE: Any = None


def _load_acp_client_module() -> Any:
    global _ACP_CLIENT_MODULE
    if _ACP_CLIENT_MODULE is not None:
        return _ACP_CLIENT_MODULE
    path = Path(__file__).resolve().parents[1] / "acp-seat" / "acp_client.py"
    spec = importlib.util.spec_from_file_location("pursers_butler_acp_client", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("ACP client module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _ACP_CLIENT_MODULE = module
    return module


def _load_acp_seat_module() -> Any:
    """Load the production ACP seat boundary without copying its sandbox policy."""
    global _ACP_SEAT_MODULE
    if _ACP_SEAT_MODULE is not None:
        return _ACP_SEAT_MODULE
    root = Path(__file__).resolve().parents[1] / "acp-seat"
    path = root / "pursers_acp_seat.py"
    spec = importlib.util.spec_from_file_location("pursers_butler_acp_seat", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("ACP seat sandbox module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(root))
    try:
        spec.loader.exec_module(module)
    finally:
        try:
            sys.path.remove(str(root))
        except ValueError:
            pass
    _ACP_SEAT_MODULE = module
    return module


class ACPModelBackend:
    """ACP v1 backend with no MCP servers and deny-by-default permissions."""

    def __init__(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        session_root: str | Path,
        model: str,
        process_env: Mapping[str, str] | None = None,
        readable_roots: Sequence[str | os.PathLike[str]] = (),
        protected_files: Sequence[str | os.PathLike[str]] = (),
    ) -> None:
        if not command or not model:
            raise ValueError("ACP command and model are required")
        root = Path(session_root).expanduser()
        if not root.is_absolute():
            raise ValueError("ACP session root must be absolute")
        try:
            root = root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("ACP session root must exist") from exc
        if not root.is_dir():
            raise ValueError("ACP session root must be a directory")
        self.command = tuple(os.fspath(part) for part in command)
        self.session_root = root
        self.model = model
        self.process_env = dict(
            process_env
            if process_env is not None
            else {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
            }
        )
        for name in ("TMPDIR", "TMP", "TEMP"):
            self.process_env[name] = str(root)
        self.readable_roots = tuple(
            Path(path).expanduser().resolve() for path in readable_roots
        )
        self.protected_files = tuple(
            Path(path).expanduser().resolve() for path in protected_files
        )

    async def run(
        self, request: Mapping[str, Any], *, timeout_s: float
    ) -> ModelBackendResponse:
        acp = _load_acp_client_module()
        try:
            return await self._run_acp(acp, request, timeout_s=timeout_s)
        except ModelRunnerFailure:
            raise
        except acp.ACPTimeoutError as exc:
            raise ModelRunnerFailure(
                "timeout", "acp_timeout", retryable=True
            ) from exc
        except acp.ACPProcessError as exc:
            raise ModelRunnerFailure(
                "provider", "acp_process_error", retryable=True
            ) from exc
        except acp.ACPRemoteError as exc:
            raise ModelRunnerFailure(
                "provider", "acp_remote_error", retryable=False
            ) from exc
        except acp.ACPProtocolError as exc:
            raise ModelRunnerFailure("malformed", "acp_protocol_error") from exc

    async def _run_acp(
        self, acp: Any, request: Mapping[str, Any], *, timeout_s: float
    ) -> ModelBackendResponse:
        try:
            seat = _load_acp_seat_module()
            command = seat.sandboxed_agent_command(
                self.command,
                self.session_root,
                readable_roots=self.readable_roots,
                protected_files=self.protected_files,
                scratch_root=self.session_root,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ModelRunnerFailure(
                "policy", "acp_os_sandbox_unavailable", retryable=False
            ) from exc
        prompt = _canonical_json_bytes(
            {
                "protocol": "autonomous_butler_model_v1",
                "model": self.model,
                "instruction": (
                    "Return exactly one JSON object with keys model, proposal, "
                    "and citations. Request no tools or filesystem access."
                ),
                "request": {
                    key: request[key]
                    for key in (
                        "request_id",
                        "board_id",
                        "subject_id",
                        "task_kind",
                        "policy_digest_sha256",
                        "task_input",
                        "task_input_digest_sha256",
                        "task_schema",
                        "evidence_refs",
                        "max_output_bytes",
                        "usage_limit",
                        "deadline",
                    )
                },
            }
        ).decode("utf-8")
        if len(prompt.encode("utf-8")) > MAX_PROVIDER_PROMPT_CHARS:
            raise ModelRunnerFailure("budget", "provider_prompt_too_large")
        chunks: list[str] = []
        size = 0
        tool_seen = False
        measured_usage: Mapping[str, Any] | None = None
        provider_request_ref: str | None = None

        async with acp.ACPClient(
            command,
            process_cwd=self.session_root,
            env=self.process_env,
            permission_policy=None,
            request_timeout=timeout_s,
        ) as client:
            await client.initialize(
                client_capabilities={
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                timeout=timeout_s,
            )
            session_id = await client.new_session(
                self.session_root, mcp_servers=(), timeout=timeout_s
            )

            async def collect() -> None:
                nonlocal size, tool_seen, measured_usage, provider_request_ref
                while True:
                    row = await client.next_update()
                    try:
                        if row.get("sessionId") != session_id:
                            continue
                        update = row.get("update")
                        if not isinstance(update, Mapping):
                            continue
                        kind = update.get("sessionUpdate")
                        if kind in {"tool_call", "tool_call_update"}:
                            tool_seen = True
                        if kind == "pursers_model_usage":
                            if measured_usage is not None:
                                raise ModelRunnerFailure(
                                    "malformed", "duplicate_acp_usage"
                                )
                            usage = update.get("usage")
                            reference = update.get("providerRequestRef")
                            if (
                                not isinstance(usage, Mapping)
                                or not isinstance(reference, str)
                                or update.get("model") != self.model
                            ):
                                raise ModelRunnerFailure(
                                    "malformed", "invalid_acp_usage"
                                )
                            measured_usage = dict(usage)
                            provider_request_ref = reference
                        if kind != "agent_message_chunk":
                            continue
                        content = update.get("content")
                        text = content.get("text") if isinstance(content, Mapping) else None
                        if not isinstance(text, str):
                            continue
                        size += len(text.encode("utf-8"))
                        if size > int(request["max_output_bytes"]) + 65_536:
                            raise ModelRunnerFailure(
                                "budget", "acp_response_too_large"
                            )
                        chunks.append(text)
                    finally:
                        client.acknowledge_update()

            collector = asyncio.create_task(collect())
            try:
                outcome = await client.prompt(
                    session_id, prompt, timeout=timeout_s
                )
                drained = asyncio.create_task(client.wait_for_updates())
                done, _pending = await asyncio.wait(
                    {collector, drained}, return_when=asyncio.FIRST_COMPLETED
                )
                if collector in done:
                    drained.cancel()
                    with suppress(asyncio.CancelledError):
                        await drained
                    collector.result()
                await drained
            finally:
                collector.cancel()
                with suppress(asyncio.CancelledError):
                    await collector
        if outcome.get("stopReason") == "cancelled":
            raise ModelRunnerFailure("cancelled", "acp_cancelled")
        if outcome.get("stopReason") != "end_turn":
            raise ModelRunnerFailure("provider", "acp_incomplete", retryable=True)
        if tool_seen:
            raise ModelRunnerFailure("policy", "acp_tool_request_refused")
        try:
            payload = json.loads("".join(chunks))
        except json.JSONDecodeError as exc:
            raise ModelRunnerFailure("malformed", "malformed_acp_response") from exc
        if not isinstance(payload, Mapping) or set(payload) != {
            "model",
            "proposal",
            "citations",
        }:
            raise ModelRunnerFailure("malformed", "invalid_backend_payload")
        if measured_usage is None or provider_request_ref is None:
            raise ModelRunnerFailure("budget", "missing_acp_usage")
        return _backend_payload(
            {
                **payload,
                "usage": measured_usage,
                "provider_request_ref": provider_request_ref,
            },
            self.model,
        )


class AutonomousModelRunner:
    """Provider-neutral policy boundary for autonomous model execution."""

    def __init__(
        self,
        backend: ModelBackend,
        *,
        task_schemas: TaskSchemaRegistry,
        policy_digest: Callable[[str], str],
        result_store: ModelResultStore | None = None,
        cancellations: ModelCancellationRegistry | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.backend = backend
        self.task_schemas = task_schemas
        self.policy_digest = policy_digest
        self.result_store = result_store or MemoryModelResultStore()
        self.cancellations = cancellations or ModelCancellationRegistry()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._locks: dict[str, asyncio.Lock] = {}

    def cancel(self, cancellation_token: str) -> None:
        self.cancellations.cancel(cancellation_token)

    async def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        request_view = request if isinstance(request, Mapping) else {}
        request_id = str(request_view.get("request_id", "invalid-request"))
        lock = self._locks.setdefault(request_id, asyncio.Lock())
        async with lock:
            try:
                validated = self._validate_request(request)
            except ModelRunnerFailure as exc:
                return self._failure_result(request_view, exc)
            request_digest = _sha256_json(validated)
            try:
                replay = self.result_store.get(validated["request_id"])
            except ModelRunnerFailure as exc:
                return self._failure_result(validated, exc)
            except Exception:
                return self._failure_result(
                    validated,
                    ModelRunnerFailure(
                        "provider", "result_store_error", retryable=True
                    ),
                )
            if replay is not None:
                if replay[0] != request_digest:
                    return self._failure_result(
                        validated,
                        ModelRunnerFailure("policy", "replay_digest_mismatch"),
                    )
                try:
                    return self._validate_replay_result(validated, replay[1])
                except ModelRunnerFailure as exc:
                    result = self._failure_result(validated, exc)
                    return self._persist_result(validated, request_digest, result)

            event = self.cancellations.event(validated["cancellation_token"])
            if event.is_set():
                result = self._failure_result(
                    validated,
                    ModelRunnerFailure("cancelled", "cancelled_before_dispatch"),
                )
                return self._persist_result(validated, request_digest, result)
            deadline = _parse_utc_deadline(validated["deadline"])
            deadline_remaining = (
                deadline - self.now().astimezone(timezone.utc)
            ).total_seconds()
            if deadline_remaining <= 0:
                result = self._failure_result(
                    validated, ModelRunnerFailure("timeout", "deadline_expired")
                )
                return self._persist_result(validated, request_digest, result)
            remaining = min(deadline_remaining, MAX_MODEL_RUN_SECONDS)

            task = asyncio.create_task(self.backend.run(validated, timeout_s=remaining))
            cancelled = asyncio.create_task(event.wait())
            try:
                done, _pending = await asyncio.wait(
                    {task, cancelled},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancelled in done and cancelled.result():
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
                    raise ModelRunnerFailure(
                        "cancelled", "cancelled_during_dispatch"
                    )
                if task not in done:
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
                    raise ModelRunnerFailure(
                        "timeout", "provider_timeout", retryable=True
                    )
                response = task.result()
                if event.is_set():
                    raise ModelRunnerFailure(
                        "cancelled", "cancelled_before_result"
                    )
                if self.policy_digest(validated["board_id"]) != validated[
                    "policy_digest_sha256"
                ]:
                    raise ModelRunnerFailure("policy", "policy_digest_drift")
                result = self._success_result(validated, response)
            except ModelRunnerFailure as exc:
                result = self._failure_result(validated, exc)
            except asyncio.CancelledError:
                task.cancel()
                raise
            except Exception:
                result = self._failure_result(
                    validated,
                    ModelRunnerFailure("provider", "provider_crash", retryable=True),
                )
            finally:
                cancelled.cancel()
                with suppress(asyncio.CancelledError):
                    await cancelled
            return self._persist_result(validated, request_digest, result)

    def _persist_result(
        self,
        request: Mapping[str, Any],
        request_digest: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            self.result_store.put(request["request_id"], request_digest, result)
        except ModelRunnerFailure as exc:
            return self._failure_result(request, exc)
        except Exception:
            return self._failure_result(
                request,
                ModelRunnerFailure("provider", "result_store_error", retryable=True),
            )
        return dict(result)

    def _validate_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            import jsonschema

            schema_path = (
                Path(__file__).resolve().parents[2]
                / "docs/design/schemas/autonomous-butler-model-v1.schema.json"
            )
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(
                schema, format_checker=jsonschema.FormatChecker()
            ).validate(request)
        except Exception as exc:
            if isinstance(exc, ModelRunnerFailure):
                raise
            raise ModelRunnerFailure("malformed", "invalid_model_request") from exc
        value = copy.deepcopy(dict(request))
        if _sha256_json(value["task_input"]) != value["task_input_digest_sha256"]:
            raise ModelRunnerFailure("policy", "task_input_digest_mismatch")
        if self.policy_digest(value["board_id"]) != value["policy_digest_sha256"]:
            raise ModelRunnerFailure("policy", "policy_digest_mismatch")
        _parse_utc_deadline(value["deadline"])
        return value

    def _success_result(
        self, request: Mapping[str, Any], response: ModelBackendResponse
    ) -> dict[str, Any]:
        encoded = response.proposal_json.encode("utf-8")
        if len(encoded) > request["max_output_bytes"]:
            raise ModelRunnerFailure("budget", "output_bytes_exceeded")
        try:
            proposal = json.loads(response.proposal_json)
        except json.JSONDecodeError as exc:
            raise ModelRunnerFailure("malformed", "invalid_proposal_json") from exc
        self.task_schemas.validate(request["task_schema"], proposal)
        citations = list(response.citations)
        if not citations or len(citations) != len(set(citations)):
            raise ModelRunnerFailure("malformed", "invalid_citations")
        if any(item not in request["evidence_refs"] for item in citations):
            raise ModelRunnerFailure("policy", "unknown_citation")
        usage = self._validate_usage(request["usage_limit"], response.usage)
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", response.provider_request_ref
        ):
            raise ModelRunnerFailure("malformed", "invalid_provider_reference")
        return {
            "schema": "autonomous_butler_model_v1",
            "schema_version": 1,
            "message_type": "result",
            "request_id": request["request_id"],
            "board_id": request["board_id"],
            "policy_digest_sha256": request["policy_digest_sha256"],
            "outcome": "succeeded",
            "proposal_json": response.proposal_json,
            "citations": citations,
            "usage": usage,
            "completed_at": self.now().astimezone(timezone.utc).isoformat(),
            "provider_request_ref": response.provider_request_ref,
            "error": None,
        }

    def _validate_replay_result(
        self, request: Mapping[str, Any], result: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            import jsonschema

            schema_path = (
                Path(__file__).resolve().parents[2]
                / "docs/design/schemas/autonomous-butler-model-v1.schema.json"
            )
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(
                schema, format_checker=jsonschema.FormatChecker()
            ).validate(result)
            if (
                result["message_type"] != "result"
                or result["request_id"] != request["request_id"]
                or result["board_id"] != request["board_id"]
                or result["policy_digest_sha256"]
                != request["policy_digest_sha256"]
            ):
                raise ValueError("replay correlation mismatch")

            if result["outcome"] == "succeeded":
                reference = result.get("provider_request_ref")
                if not isinstance(reference, str):
                    raise ValueError("successful replay lacks provider reference")
                expected = self._success_result(
                    request,
                    ModelBackendResponse(
                        proposal_json=result["proposal_json"],
                        citations=tuple(result["citations"]),
                        usage=result["usage"],
                        provider_request_ref=reference,
                    ),
                )
            else:
                error = result["error"]
                expected = self._failure_result(
                    request,
                    ModelRunnerFailure(
                        error["category"],
                        error["code"],
                        retryable=error["retryable"],
                    ),
                )
            expected["completed_at"] = result["completed_at"]
            if dict(result) != expected:
                raise ValueError("replay result is not normalized")
        except Exception as exc:
            raise ModelRunnerFailure("malformed", "invalid_replay_result") from exc
        return copy.deepcopy(dict(result))

    @staticmethod
    def _validate_usage(
        limit: Mapping[str, Any], usage: Mapping[str, Any]
    ) -> dict[str, Any]:
        keys = {
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_microunits",
            "measured",
        }
        if set(usage) != keys or usage.get("measured") is not True:
            raise ModelRunnerFailure("budget", "unmeasured_usage")
        values = {key: usage[key] for key in keys - {"measured"}}
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values.values()):
            raise ModelRunnerFailure("malformed", "invalid_usage")
        if values["total_tokens"] != values["input_tokens"] + values["output_tokens"]:
            raise ModelRunnerFailure("malformed", "usage_arithmetic_mismatch")
        if (
            values["input_tokens"] > limit["max_input_tokens"]
            or values["output_tokens"] > limit["max_output_tokens"]
            or values["cost_microunits"] > limit["max_cost_microunits"]
        ):
            raise ModelRunnerFailure("budget", "usage_limit_exceeded")
        return {**values, "measured": True}

    def _failure_result(
        self, request: Mapping[str, Any], failure: ModelRunnerFailure
    ) -> dict[str, Any]:
        category = failure.category if failure.category in {
            "cancelled",
            "timeout",
            "provider",
            "malformed",
            "policy",
            "budget",
        } else "provider"
        return {
            "schema": "autonomous_butler_model_v1",
            "schema_version": 1,
            "message_type": "result",
            "request_id": _bounded_model_id(
                request.get("request_id"), "invalid-request"
            ),
            "board_id": _bounded_model_id(request.get("board_id"), "invalid-board"),
            "policy_digest_sha256": _bounded_sha256(
                request.get("policy_digest_sha256")
            ),
            "outcome": "cancelled" if category == "cancelled" else "failed",
            "proposal_json": None,
            "citations": [],
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_microunits": 0,
                "measured": True,
            },
            "completed_at": self.now().astimezone(timezone.utc).isoformat(),
            "reason_code": failure.code,
            "error": {
                "category": category,
                "code": failure.code,
                "retryable": failure.retryable,
            },
        }
class SingletonLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "SingletonLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise AlreadyRunning(f"board butler lock is held: {self.path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return self

    def __exit__(self, *_args: Any) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _private_regular_file(path: Path) -> bool:
    """Return whether path is an owned, non-symlink, mode-0600 regular file."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and not path.is_symlink()
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o600
    )


def validate_active_authorization(path: Path) -> None:
    """Require the separate operator-created authorization for active mode."""
    if not _private_regular_file(path):
        raise ValueError("active authorization must be an owned mode-0600 regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("active authorization is unreadable") from exc
    if (
        not isinstance(document, Mapping)
        or set(document) != ACTIVE_AUTHORIZATION_KEYS
        or document.get("schema_version") != ACTIVE_AUTHORIZATION_SCHEMA_VERSION
        or document.get("mode") != "active"
        or document.get("authorized") is not True
    ):
        raise ValueError("active authorization is invalid")


def local_kill_engaged(path: Path | None) -> bool:
    return path is not None and _private_regular_file(path)


class RuntimeStatus:
    """Publish a secret-free local heartbeat for the loopback dashboard."""

    def __init__(self, path: Path | None, mode: str) -> None:
        self.path = path
        self.mode = mode
        self.started_at = utc_now()
        self.last_activity_at = self.started_at
        self.last_activity = "startup"

    def _write(self, *, running: bool) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        document = {
            "schema_version": 1,
            "pid": os.getpid(),
            "mode": self.mode,
            "running": running,
            "started_at": self.started_at.isoformat(),
            "last_activity_at": self.last_activity_at.isoformat(),
            "last_activity": self.last_activity,
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            Path(temporary).unlink(missing_ok=True)
            raise

    def __enter__(self) -> "RuntimeStatus":
        self._write(running=True)
        return self

    def mark(self, activity: str, at: datetime | None = None) -> None:
        self.last_activity_at = at or utc_now()
        self.last_activity = activity
        self._write(running=True)

    def __exit__(self, *_args: Any) -> None:
        self.last_activity_at = utc_now()
        self.last_activity = "stopped"
        self._write(running=False)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reject_unknown_keys(
    value: Mapping[str, Any], allowed: Sequence[str], path: str
) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ButlerConfigError(f"{path} has unknown keys: {', '.join(unknown)}")


def _bounded_int(value: Any, path: str, minimum: int, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ButlerConfigError(f"{path} must be an integer from {minimum} to {maximum}")
    return value


def _optional_reference(value: Any, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ButlerConfigError(f"{path} must be null or a non-empty reference")
    return value


def _validate_window(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ButlerConfigError(f"{path} must be an object")
    _reject_unknown_keys(value, ("days", "start", "end", "timezone"), path)
    days = value.get("days")
    if (
        not isinstance(days, list)
        or not days
        or any(day not in WEEKDAYS for day in days)
        or len(set(days)) != len(days)
    ):
        raise ButlerConfigError(f"{path}.days must contain unique weekday names")
    start, end = value.get("start"), value.get("end")
    clock = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    if not isinstance(start, str) or not clock.fullmatch(start):
        raise ButlerConfigError(f"{path}.start must be HH:MM")
    if not isinstance(end, str) or not clock.fullmatch(end):
        raise ButlerConfigError(f"{path}.end must be HH:MM")
    if start == end:
        raise ButlerConfigError(f"{path}.start and end must differ")
    zone = value.get("timezone")
    if not isinstance(zone, str) or not zone:
        raise ButlerConfigError(f"{path}.timezone must name an IANA timezone")
    try:
        ZoneInfo(zone)
    except ZoneInfoNotFoundError as exc:
        raise ButlerConfigError(f"{path}.timezone is unknown") from exc
    return {"days": list(days), "start": start, "end": end, "timezone": zone}


def _validate_settings(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ButlerConfigError(f"{path} must be an object")
    _reject_unknown_keys(value, BOARD_BUTLER_CONFIG_SCHEMA["setting_keys"], path)
    result: dict[str, Any] = {}
    if "mode" in value:
        if value["mode"] not in {"shadow", "active"}:
            raise ButlerConfigError(f"{path}.mode must be shadow or active")
        result["mode"] = value["mode"]
    if "answering_mode" in value:
        if value["answering_mode"] not in {"off", "assist", "autonomous"}:
            raise ButlerConfigError(
                f"{path}.answering_mode must be off, assist, or autonomous"
            )
        result["answering_mode"] = value["answering_mode"]
    if "answer_scope" in value:
        scope = value["answer_scope"]
        if not isinstance(scope, Mapping):
            raise ButlerConfigError(f"{path}.answer_scope must be an object")
        _reject_unknown_keys(scope, ANSWER_CLASSES, f"{path}.answer_scope")
        normalized: dict[str, str] = {}
        for name, disposition in scope.items():
            if disposition not in {"auto", "escalate"}:
                raise ButlerConfigError(
                    f"{path}.answer_scope.{name} must be auto or escalate"
                )
            if name in NEVER_AUTO_CLASSES and disposition == "auto":
                raise ButlerConfigError(f"{path}.answer_scope.{name} can never be auto")
            normalized[str(name)] = str(disposition)
        result["answer_scope"] = normalized
    if "required_evidence_kinds" in value:
        kinds = value["required_evidence_kinds"]
        if (
            not isinstance(kinds, list)
            or not kinds
            or any(kind not in CITABLE_EVIDENCE_KINDS for kind in kinds)
            or len(set(kinds)) != len(kinds)
        ):
            raise ButlerConfigError(
                f"{path}.required_evidence_kinds must be a non-empty unique citable list"
            )
        result["required_evidence_kinds"] = list(kinds)
    if "ceilings" in value:
        ceilings = value["ceilings"]
        if not isinstance(ceilings, Mapping):
            raise ButlerConfigError(f"{path}.ceilings must be an object")
        _reject_unknown_keys(
            ceilings, ("per_hour", "per_ticket", "per_board"), f"{path}.ceilings"
        )
        result["ceilings"] = {
            name: _bounded_int(raw, f"{path}.ceilings.{name}", 1, limit)
            for name, raw, limit in (
                ("per_hour", ceilings.get("per_hour"), 100),
                ("per_ticket", ceilings.get("per_ticket"), 20),
                ("per_board", ceilings.get("per_board"), 500),
            )
            if name in ceilings
        }
    if "hold_before_post_s" in value:
        result["hold_before_post_s"] = _bounded_int(
            value["hold_before_post_s"], f"{path}.hold_before_post_s", 0, 604_800
        )
    if "active_windows" in value:
        windows = value["active_windows"]
        if not isinstance(windows, list):
            raise ButlerConfigError(f"{path}.active_windows must be a list")
        result["active_windows"] = [
            _validate_window(item, f"{path}.active_windows[{index}]")
            for index, item in enumerate(windows)
        ]
    if "kill_switch" in value:
        if not isinstance(value["kill_switch"], bool):
            raise ButlerConfigError(f"{path}.kill_switch must be boolean")
        result["kill_switch"] = value["kill_switch"]
    if "auto_demote" in value:
        demote = value["auto_demote"]
        if not isinstance(demote, Mapping):
            raise ButlerConfigError(f"{path}.auto_demote must be an object")
        _reject_unknown_keys(
            demote,
            ("veto_count", "failure_count", "window_s"),
            f"{path}.auto_demote",
        )
        result["auto_demote"] = {
            name: _bounded_int(raw, f"{path}.auto_demote.{name}", minimum, maximum)
            for name, raw, minimum, maximum in (
                ("veto_count", demote.get("veto_count"), 1, 100),
                ("failure_count", demote.get("failure_count"), 1, 100),
                ("window_s", demote.get("window_s"), 60, 2_592_000),
            )
            if name in demote
        }
    for task in ("classification", "drafting"):
        if task not in value:
            continue
        selected = value[task]
        if not isinstance(selected, Mapping):
            raise ButlerConfigError(f"{path}.{task} must be an object")
        _reject_unknown_keys(
            selected,
            (
                "model",
                "endpoint_ref",
                "key_ref",
                "extra_headers",
                "key_header",
                "key_prefix",
                "validation_path",
                "draft_path",
                "draft_protocol",
            ),
            f"{path}.{task}",
        )
        result[task] = {
            name: _optional_reference(selected.get(name), f"{path}.{task}.{name}")
            for name in ("model", "endpoint_ref", "key_ref")
            if name in selected
        }
        if "extra_headers" in selected:
            headers = selected["extra_headers"]
            if (
                not isinstance(headers, Mapping)
                or len(headers) > 16
                or any(
                    not isinstance(name, str)
                    or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", name)
                    or re.search(r"authorization|api[-_]?key|token|secret|cookie", name, re.I)
                    or not isinstance(header_value, str)
                    or len(header_value) > 1_000
                    or any(ord(character) < 0x20 for character in header_value)
                    for name, header_value in headers.items()
                )
            ):
                raise ButlerConfigError(
                    f"{path}.{task}.extra_headers must contain bounded non-secret headers"
                )
            result[task]["extra_headers"] = dict(headers)
        if "key_header" in selected:
            header = selected["key_header"]
            if not isinstance(header, str) or not re.fullmatch(
                r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", header
            ):
                raise ButlerConfigError(f"{path}.{task}.key_header is invalid")
            result[task]["key_header"] = header
        if "key_prefix" in selected:
            prefix = selected["key_prefix"]
            if (
                not isinstance(prefix, str)
                or len(prefix) > 80
                or any(ord(character) < 0x20 for character in prefix)
            ):
                raise ButlerConfigError(f"{path}.{task}.key_prefix is invalid")
            result[task]["key_prefix"] = prefix
        for field_name in ("validation_path", "draft_path"):
            if field_name not in selected:
                continue
            relative_path = selected[field_name]
            parsed_path = (
                urllib.parse.urlsplit(relative_path)
                if isinstance(relative_path, str)
                else None
            )
            if (
                not isinstance(relative_path, str)
                or not relative_path
                or len(relative_path) > 500
                or any(ord(character) < 0x20 for character in relative_path)
                or parsed_path is None
                or parsed_path.scheme
                or parsed_path.netloc
                or parsed_path.query
                or parsed_path.fragment
            ):
                raise ButlerConfigError(f"{path}.{task}.{field_name} is invalid")
            result[task][field_name] = relative_path
        if "draft_protocol" in selected:
            if selected["draft_protocol"] not in PROVIDER_DRAFT_PROTOCOLS:
                raise ButlerConfigError(f"{path}.{task}.draft_protocol is invalid")
            result[task]["draft_protocol"] = selected["draft_protocol"]
    return result


def _merge_settings(base: dict[str, Any], override: Mapping[str, Any]) -> None:
    for key, value in override.items():
        if key in {"answer_scope", "ceilings", "auto_demote", "classification", "drafting"}:
            base[key] = {**base[key], **value}
        else:
            base[key] = value


def _window_allows(now: datetime, windows: Sequence[Mapping[str, Any]]) -> bool:
    for window in windows:
        local = now.astimezone(ZoneInfo(str(window["timezone"])))
        weekday = WEEKDAYS[local.weekday()]
        previous_weekday = WEEKDAYS[(local.weekday() - 1) % len(WEEKDAYS)]
        current = local.hour * 60 + local.minute
        start_h, start_m = (int(item) for item in str(window["start"]).split(":"))
        end_h, end_m = (int(item) for item in str(window["end"]).split(":"))
        start, end = start_h * 60 + start_m, end_h * 60 + end_m
        if start < end and weekday in window["days"] and start <= current < end:
            return True
        if start > end and (
            (weekday in window["days"] and current >= start)
            or (previous_weekday in window["days"] and current < end)
        ):
            return True
    return False


def _recent_vetoes(state: Mapping[str, Any], now: datetime, window_s: int) -> int:
    board_butler = state.get("board_butler", {})
    history = (
        board_butler.get("veto_history", [])
        if isinstance(board_butler, Mapping)
        else []
    )
    if isinstance(history, list):
        def veto_time(item: Any) -> datetime | None:
            if isinstance(item, int) and not isinstance(item, bool):
                return datetime.fromtimestamp(item, tz=timezone.utc)
            if isinstance(item, Mapping):
                return parse_time(item.get("at"))
            return parse_time(item)

        durable = [
            item
            for item in history
            if (stamp := veto_time(item)) is not None
            and now - stamp < timedelta(seconds=window_s)
        ]
        if durable:
            return len(durable)
    total = 0
    for item in state.get("findings", []):
        if not isinstance(item, Mapping):
            continue
        hold = item.get("hold", {})
        if not isinstance(hold, Mapping) or hold.get("status") != "vetoed":
            continue
        vetoed_at = parse_time(hold.get("vetoed_at"))
        if vetoed_at is not None and now - vetoed_at < timedelta(seconds=window_s):
            total += 1
    return total


def _recent_answer_failures(
    state: Mapping[str, Any], now: datetime, window_s: int
) -> int:
    board_butler = state.get("board_butler", {})
    history = (
        board_butler.get("answer_failure_history", [])
        if isinstance(board_butler, Mapping)
        else []
    )
    if not isinstance(history, list):
        return 0
    return sum(
        1
        for item in history
        if isinstance(item, Mapping)
        and (stamp := parse_time(item.get("at"))) is not None
        and now - stamp < timedelta(seconds=window_s)
    )


def resolve_config(
    document: Mapping[str, Any],
    args: argparse.Namespace,
    state: Mapping[str, Any],
    now: datetime,
    *,
    project_name: str | None = None,
) -> EffectiveConfig:
    intake = document.get("intake", {})
    intake_rate = intake.get("rate_per_hour") if isinstance(intake, Mapping) else None
    default_hour = (
        intake_rate
        if isinstance(intake_rate, int)
        and not isinstance(intake_rate, bool)
        and 1 <= intake_rate <= 100
        else args.drafts_per_hour
    )
    merged: dict[str, Any] = {
        "mode": "shadow",
        # Preserve the pre-answering behavior for existing configurations:
        # drafts remain visible, but no answer is sent without explicit opt-in.
        "answering_mode": "assist",
        "answer_scope": {name: "escalate" for name in ANSWER_CLASSES},
        "required_evidence_kinds": list(CITABLE_EVIDENCE_KINDS),
        "ceilings": {
            "per_hour": default_hour,
            "per_ticket": args.drafts_per_ticket,
            "per_board": args.drafts_per_board,
        },
        "hold_before_post_s": DEFAULT_HOLD_BEFORE_POST_S,
        "active_windows": [],
        "kill_switch": True,
        "auto_demote": {
            "veto_count": DEFAULT_VETO_COUNT,
            "failure_count": DEFAULT_FAILURE_COUNT,
            "window_s": DEFAULT_VETO_WINDOW_S,
        },
        "classification": {
            "model": None,
            "endpoint_ref": None,
            "key_ref": None,
            "extra_headers": {},
            "key_header": "Authorization",
            "key_prefix": "Bearer",
            "validation_path": "models",
            "draft_path": "draft",
            "draft_protocol": "pursers_json_v1",
        },
        "drafting": {
            "model": None,
            "endpoint_ref": None,
            "key_ref": None,
            "extra_headers": {},
            "key_header": "Authorization",
            "key_prefix": "Bearer",
            "validation_path": "models",
            "draft_path": "draft",
            "draft_protocol": "pursers_json_v1",
        },
    }
    sources = ["safe_defaults"]
    raw = document.get("board_butler")
    if raw is not None:
        if not isinstance(raw, Mapping):
            raise ButlerConfigError("coordinator_config.board_butler must be an object")
        _reject_unknown_keys(
            raw,
            BOARD_BUTLER_CONFIG_SCHEMA["keys"],
            "coordinator_config.board_butler",
        )
        if raw.get("schema_version") != BOARD_BUTLER_CONFIG_SCHEMA["schema_version"]:
            raise ButlerConfigError("coordinator_config.board_butler.schema_version must be 1")
        global_settings = _validate_settings(
            raw.get("global", {}), "coordinator_config.board_butler.global"
        )
        _merge_settings(merged, global_settings)
        if global_settings:
            sources.append("global")
        for collection_name, selected_name in (
            (
                "projects",
                project_name
                if project_name is not None
                else getattr(args, "project", None),
            ),
            ("boards", args.home_board),
        ):
            collection = raw.get(collection_name, {})
            if not isinstance(collection, Mapping):
                raise ButlerConfigError(
                    f"coordinator_config.board_butler.{collection_name} must be an object"
                )
            for name, settings in collection.items():
                if not isinstance(name, str) or not name:
                    raise ButlerConfigError(
                        f"coordinator_config.board_butler.{collection_name} names must be non-empty"
                    )
                validated = _validate_settings(
                    settings, f"coordinator_config.board_butler.{collection_name}.{name}"
                )
                if selected_name is not None and name == selected_name:
                    _merge_settings(merged, validated)
                    sources.append(f"{collection_name[:-1]}:{name}")
    board_state = state.get("board_butler", {})
    persisted_kill = (
        isinstance(board_state, Mapping)
        and isinstance(board_state.get("kill_switch"), Mapping)
        and board_state["kill_switch"].get("engaged") is True
    )
    kill_switch = bool(merged["kill_switch"] or persisted_kill)
    veto_count = _recent_vetoes(state, now, merged["auto_demote"]["window_s"])
    failure_count = _recent_answer_failures(
        state, now, merged["auto_demote"]["window_s"]
    )
    demotion_reason: str | None = None
    if kill_switch:
        future_state = "killed"
        demotion_reason = "kill_switch"
    elif merged["mode"] == "shadow":
        future_state = "shadow"
    elif not _window_allows(now, merged["active_windows"]):
        future_state = "outside_active_window"
    elif veto_count >= merged["auto_demote"]["veto_count"]:
        future_state = "auto_demoted"
        demotion_reason = (
            f"{veto_count} vetoes in {merged['auto_demote']['window_s']} seconds"
        )
    elif failure_count >= merged["auto_demote"]["failure_count"]:
        future_state = "auto_demoted"
        demotion_reason = (
            f"{failure_count} answer failures in "
            f"{merged['auto_demote']['window_s']} seconds"
        )
    else:
        future_state = "eligible"
    answering_mode = str(merged["answering_mode"])
    runtime_authorized = bool(
        getattr(args, "runtime_mode", "shadow") == "active"
        and args.home_board in getattr(args, "act_on_board", [])
    )
    effective_answering_mode = answering_mode
    if answering_mode == "autonomous" and (
        future_state != "eligible" or not runtime_authorized
    ):
        effective_answering_mode = "assist"
    return EffectiveConfig(
        configured_mode=merged["mode"],
        answering_mode=answering_mode,
        effective_answering_mode=effective_answering_mode,
        runtime_authorized=runtime_authorized,
        future_active_state=future_state,
        demotion_reason=demotion_reason,
        answer_scope=dict(merged["answer_scope"]),
        required_evidence_kinds=tuple(merged["required_evidence_kinds"]),
        drafts_per_hour=merged["ceilings"]["per_hour"],
        drafts_per_ticket=merged["ceilings"]["per_ticket"],
        drafts_per_board=merged["ceilings"]["per_board"],
        hold_before_post_s=merged["hold_before_post_s"],
        active_windows=tuple(dict(item) for item in merged["active_windows"]),
        kill_switch=kill_switch,
        veto_count=merged["auto_demote"]["veto_count"],
        failure_count=merged["auto_demote"]["failure_count"],
        veto_window_s=merged["auto_demote"]["window_s"],
        classification_model=merged["classification"]["model"],
        classification_endpoint_ref=merged["classification"]["endpoint_ref"],
        classification_key_ref=merged["classification"]["key_ref"],
        classification_extra_headers=dict(merged["classification"]["extra_headers"]),
        classification_key_header=merged["classification"]["key_header"],
        classification_key_prefix=merged["classification"]["key_prefix"],
        classification_validation_path=merged["classification"]["validation_path"],
        classification_draft_path=merged["classification"]["draft_path"],
        classification_draft_protocol=merged["classification"]["draft_protocol"],
        drafting_model=merged["drafting"]["model"],
        drafting_endpoint_ref=merged["drafting"]["endpoint_ref"],
        drafting_key_ref=merged["drafting"]["key_ref"],
        drafting_extra_headers=dict(merged["drafting"]["extra_headers"]),
        drafting_key_header=merged["drafting"]["key_header"],
        drafting_key_prefix=merged["drafting"]["key_prefix"],
        drafting_validation_path=merged["drafting"]["validation_path"],
        drafting_draft_path=merged["drafting"]["draft_path"],
        drafting_draft_protocol=merged["drafting"]["draft_protocol"],
        source_layers=tuple(sources),
    )


def classify_question(message: str, kind: str = "information") -> Classification:
    # Kind is an authority boundary, not a hint.  Fail closed before inspecting
    # content so a mechanical substring cannot launder a human-only request.
    if kind in {"approval", "decision", "deliverable"}:
        return Classification(Outcome.ESCALATE, f"question-kind:{kind}")
    mechanical_signal: PolicyRule | None = None
    for rule in POLICY_TABLE:
        if not rule.pattern.search(message):
            continue
        if rule.outcome is Outcome.ESCALATE:
            return Classification(rule.outcome, rule.name, rule.evaluator)
        full_request = MECHANICAL_REQUEST_PATTERNS.get(rule.name)
        if full_request is not None and full_request.fullmatch(message):
            return Classification(rule.outcome, rule.name, rule.evaluator)
        mechanical_signal = mechanical_signal or rule
    if mechanical_signal is not None:
        return Classification(Outcome.ESCALATE, "mixed-or-unsupported-request")
    return Classification(Outcome.UNKNOWN, "no-confident-policy-match")


_PRECEDENT_STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "because",
        "before",
        "could",
        "from",
        "have",
        "into",
        "must",
        "only",
        "please",
        "question",
        "should",
        "that",
        "their",
        "there",
        "these",
        "they",
        "this",
        "ticket",
        "what",
        "when",
        "where",
        "which",
        "with",
        "would",
    }
)


def _precedent_terms(value: Any) -> frozenset[str]:
    return frozenset(
        term
        for term in re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", str(value).casefold())
        if term not in _PRECEDENT_STOP_WORDS
    )


def find_precedents(
    question: Mapping[str, Any],
    answered: Sequence[Mapping[str, Any]],
    *,
    limit: int = 3,
) -> list[dict[str, str]]:
    """Return identifier-only citations for lexically related answered questions."""
    terms = _precedent_terms(question.get("message", ""))
    if not terms:
        return []
    current_id = str(question.get("question_id", ""))
    ranked: list[tuple[float, str, str]] = []
    for row in answered:
        if not isinstance(row, Mapping) or row.get("state") != "answered":
            continue
        question_id = row.get("question_id")
        ticket_id = row.get("ticket_id")
        if (
            not isinstance(question_id, str)
            or not question_id
            or question_id == current_id
            or not isinstance(ticket_id, str)
            or not ticket_id
            or not isinstance(row.get("answer"), str)
        ):
            continue
        candidate = _precedent_terms(row.get("message", ""))
        overlap = len(terms & candidate)
        if overlap == 0:
            continue
        # Ranking is retrieval-only. It is deliberately not stored or reported
        # as agreement; only a human mark can measure draft agreement.
        score = overlap / max(1, len(terms | candidate))
        ranked.append((score, question_id, ticket_id))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [
        {"question_id": question_id, "ticket_id": ticket_id}
        for _score, question_id, ticket_id in ranked[: max(0, limit)]
    ]


def agreement_by_question_kind(
    evaluations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Summarize explicit marks without mixing draft and escalation axes."""
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in evaluations:
        if not isinstance(row, Mapping) or row.get("mark") not in MARK_VALUES:
            continue
        mark = str(row["mark"])
        axis = "draft_quality" if mark in DRAFT_MARK_VALUES else "routing_quality"
        population = str(row.get("mark_population") or "unknown")
        grouped.setdefault(
            (str(row.get("question_kind", "unknown")), axis, population), []
        ).append(row)
    report: list[dict[str, Any]] = []
    for kind, axis, population in sorted(grouped):
        rows = grouped[(kind, axis, population)]
        values = DRAFT_MARK_VALUES if axis == "draft_quality" else ROUTING_MARK_VALUES
        counts = {mark: sum(row.get("mark") == mark for row in rows) for mark in values}
        stamps = sorted(
            str(row.get("marked_at")) for row in rows if row.get("marked_at")
        )
        enough = len(rows) >= MIN_AGREEMENT_SAMPLES
        success_mark = "send_as_is" if axis == "draft_quality" else "correct_escalation"
        report.append(
            {
                "question_kind": kind,
                "axis": axis,
                "population": population,
                "sample_count": len(rows),
                "marks": counts,
                "status": "measured" if enough else "insufficient_samples",
                "agreement_percent": (
                    round(100 * counts[success_mark] / len(rows), 1)
                    if enough
                    else None
                ),
                "first_marked_at": stamps[0] if stamps else None,
                "last_marked_at": stamps[-1] if stamps else None,
            }
        )
    return report


def agreement_by_ticket(
    evaluations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return the same human-only axes grouped by ticket identifier."""
    remapped = [
        {**dict(row), "question_kind": str(row.get("ticket_id", "unknown"))}
        for row in evaluations
        if isinstance(row, Mapping)
    ]
    report = agreement_by_question_kind(remapped)
    for row in report:
        row["ticket_id"] = row.pop("question_kind")
    return report


def multi_question_tickets(
    evaluations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """List tickets with multiple distinct question identifiers."""
    grouped: dict[str, set[str]] = {}
    for row in evaluations:
        if not isinstance(row, Mapping):
            continue
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


def _public_identity(identity: Any) -> dict[str, str]:
    result = {
        name: str(getattr(identity, name, ""))
        for name in ("agent_id", "agent_name", "principal_id")
    }
    if not all(result.values()):
        raise ValueError("board butler identity is incomplete")
    return result


def evaluation_state_key(question_id: str) -> str:
    if not re.fullmatch(r"CQ-[0-9A-Za-z-]+", question_id):
        raise ValueError("evaluation question_id is invalid")
    return f"{EVALUATION_STATE_PREFIX}{question_id}"


def record_draft_evaluation(
    state: Mapping[str, Any],
    question: Mapping[str, Any],
    finding: Mapping[str, Any],
    identity: Any,
    now: datetime,
) -> dict[str, Any]:
    """Upsert a pairing row plus a bounded autonomous-delivery audit."""
    result = dict(state)
    existing = result.get("evaluation")
    question_id = str(question.get("question_id", ""))
    ticket_id = str(question.get("ticket_id", ""))
    if not question_id or not ticket_id:
        raise ValueError("draft evaluation needs question_id and ticket_id")
    status = (
        "produced"
        if finding.get("kind") == "would_answer"
        and (
            finding.get("verdict") == Outcome.MECHANICAL.value
            or finding.get("draft_source") == "configured_provider"
        )
        else "declined"
    )
    row = {
        "question_id": question_id,
        "ticket_id": ticket_id,
        "question_kind": str(question.get("kind", "information")),
        "draft_status": status,
        "decline_reason": (
            None
            if status == "produced"
            else str(finding.get("policy_rule") or finding.get("kind", "declined"))
        ),
        "drafted_by": _public_identity(identity),
        "drafted_at": now.isoformat(),
        "mark": None,
        "mark_population": None,
        "marked_by": None,
        "marked_at": None,
    }
    if finding.get("auto_eligible") is True:
        row["answer_audit"] = {
            "schema": "autonomous_butler_answer_audit_v1",
            "status": "pending",
            "authority_digest_sha256": str(
                (finding.get("authority_proof") or {}).get("digest_sha256", "")
            ),
            "answer_sha256": hashlib.sha256(
                str(finding.get("message", "")).encode("utf-8")
            ).hexdigest(),
            "answer": str(finding.get("message", ""))[:MAX_AUTONOMOUS_ANSWER_CHARS],
            "evidence": str(finding.get("evidence", ""))[:1_000],
            "hold": dict(finding.get("hold", {})),
            "accepted_at": None,
            "answered_at": None,
            "event_id": None,
            "attempts": 0,
            "reason_code": None,
        }
    if isinstance(existing, Mapping):
        if existing.get("question_id") != question_id:
            raise ValueError("evaluation state key contains another question")
        # A retry may repair a missing finding, but it must never erase a human mark.
        row.update(
            {
                key: existing.get(key)
                for key in (
                    "mark",
                    "mark_population",
                    "marked_by",
                    "marked_at",
                    "answered_by",
                    "answer_audit",
                )
                if key in existing
            }
        )
    result["schema_version"] = 1
    result["evaluation"] = row
    return result


def _identifier(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.I)
    return match.group(0) if match else None


def _git_ancestry(repo: Path, sha: str, target: str) -> Evidence:
    resolved = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{sha}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if resolved.returncode != 0:
        raise ValueError(f"commit {sha} is not available in the configured repository")
    full_sha = resolved.stdout.strip()
    check = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", full_sha, target],
        check=False,
        capture_output=True,
        text=True,
    )
    if check.returncode not in {0, 1}:
        raise ValueError(check.stderr.strip() or "git ancestry check failed")
    yes = check.returncode == 0
    return Evidence(
        kind="git_ancestry",
        source=f"git merge-base --is-ancestor {full_sha} {target}",
        detail=f"exit_code={check.returncode}",
        answer=f"{full_sha} {'is' if yes else 'is not'} an ancestor of {target}.",
    )


def _submission_evidence(ticket: Mapping[str, Any]) -> tuple[list[str], str]:
    history = ticket.get("submission_history", [])
    submission = (
        history[-1]
        if isinstance(history, list) and history and isinstance(history[-1], Mapping)
        else {}
    )
    notes = ticket.get("notes") or submission.get("notes") or ""
    if not isinstance(notes, str):
        notes = ""
    match = re.search(r"(?m)^cumulative_files_changed:\s*(\[[^\n]*\])\s*$", notes)
    changed: Any = None
    if match:
        try:
            changed = json.loads(match.group(1))
        except json.JSONDecodeError:
            changed = None
    if not isinstance(changed, list) or not all(isinstance(item, str) for item in changed):
        changed = submission.get("files_changed", ticket.get("files_changed", []))
    if not isinstance(changed, list) or not changed or not all(
        isinstance(item, str) and item for item in changed
    ):
        raise ValueError("submission has no readable changed-file evidence")
    outputs = "\n".join(
        line.partition(":")[2].strip()
        for line in notes.splitlines()
        if line.startswith(("test-output:", "test_output:"))
    )
    if not outputs:
        raise ValueError("submission has no readable test-output evidence")
    return changed, outputs


def _manifest_coverage(repo: Path, changed: Sequence[str]) -> dict[str, tuple[str, ...]]:
    manifest_path = repo / "tools" / "ci_manifest.py"
    if not manifest_path.is_file():
        raise ValueError("tools/ci_manifest.py is unavailable")
    namespace = runpy.run_path(str(manifest_path))
    mapper = namespace.get("covering_suites")
    if not callable(mapper):
        raise ValueError("ci manifest does not declare suite coverage")
    result = mapper(changed)
    if not isinstance(result, dict):
        raise ValueError("ci manifest returned invalid suite coverage")
    return result


def _suite_statuses(output: str, suite_names: Sequence[str]) -> dict[str, str]:
    clauses = [part.strip() for part in re.split(r"[;\n]", output) if part.strip()]
    result: dict[str, str] = {}
    for name in suite_names:
        aliases = {name.lower(), name.lower().replace("-", " ")}
        if name == "aionui-extension":
            aliases.add("aionui")
        matching = [
            clause
            for clause in clauses
            if any(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", clause.lower()) for alias in aliases)
        ]
        combined = " ".join(matching).lower()
        pass_counts = [
            int(match.group(1))
            for match in re.finditer(r"\b(\d+)\s+passed\b", combined)
        ]
        without_pass_counts = re.sub(r"\b\d+\s+passed\b", "", combined)
        has_pass = any(count > 0 for count in pass_counts) or bool(
            re.search(r"\b(?:pass|passed|green)\b", without_pass_counts)
        )
        if not combined:
            result[name] = "never-reached"
        elif re.search(
            r"\b(?:fail(?:ed|ure|ures)?|blocked|denied|errors?|timed? out|not run|never[- ]reached)\b",
            combined,
        ):
            result[name] = "failed"
        elif has_pass:
            result[name] = "passed"
        elif "skipped" in combined:
            result[name] = "skipped"
        else:
            result[name] = "never-reached"
    return result


async def _coverage_blindness(
    question: Mapping[str, Any], source: EvidenceSource, repo: Path
) -> Evidence:
    message = str(question.get("message", ""))
    target = _identifier(r"\bTK-[0-9A-Za-z-]+\b", message) or str(
        question.get("ticket_id", "")
    )
    if not target:
        raise ValueError("coverage check needs a ticket identifier")
    payload = await source.ticket_get(target)
    ticket = payload.get("ticket", payload)
    if not isinstance(ticket, Mapping):
        raise ValueError(f"ticket {target} is unreadable")
    changed, output = _submission_evidence(ticket)
    coverage = _manifest_coverage(repo, changed)
    required = sorted({name for names in coverage.values() for name in names})
    statuses = _suite_statuses(output, required)
    incomplete = sorted(name for name, status in statuses.items() if status != "passed")
    affected = {
        path: [name for name in names if name in incomplete]
        for path, names in coverage.items()
        if any(name in incomplete for name in names)
    }
    source_name = f"Central ticket_get({target}).latest_submission + tools/ci_manifest.py"
    detail = json.dumps(
        {"changed_paths": changed, "covering_suites": coverage, "suite_statuses": statuses},
        sort_keys=True,
        separators=(",", ":"),
    )
    if affected:
        pairs = ", ".join(
            f"{path} -> {','.join(names)}" for path, names in sorted(affected.items())
        )
        return Evidence(
            kind="manifest_coverage",
            source=source_name,
            detail=detail,
            answer=f"Would escalate: covering suite evidence is incomplete ({pairs}).",
            outcome=Outcome.ESCALATE,
        )
    return Evidence(
        kind="manifest_coverage",
        source=source_name,
        detail=detail,
        answer="The blocked or skipped suites do not cover the submitted diff; mechanical review may proceed.",
        outcome=Outcome.MECHANICAL,
    )


async def evaluate_mechanical(
    classification: Classification,
    question: Mapping[str, Any],
    source: EvidenceSource,
    repo: Path,
    integration_ref: str,
) -> Evidence:
    message = str(question.get("message", ""))
    ticket_id = str(question.get("ticket_id", ""))
    if classification.evaluator == "coverage_blindness":
        return await _coverage_blindness(question, source, repo)
    if classification.evaluator == "git_ancestry":
        sha = _identifier(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])", message)
        if sha is None:
            raise ValueError("no commit identifier was present")
        return _git_ancestry(repo, sha, integration_ref)
    if classification.evaluator == "ticket_status":
        target = _identifier(r"\bTK-[0-9A-Za-z-]+\b", message)
        if target is None:
            raise ValueError("no ticket identifier was present")
        payload = await source.ticket_get(target)
        ticket = payload.get("ticket", payload)
        status = ticket.get("status") if isinstance(ticket, Mapping) else None
        if not isinstance(status, str):
            raise ValueError(f"ticket {target} has no readable status")
        return Evidence(
            kind="ticket_status",
            source=f"Central ticket_get({target})",
            detail=f"status={status}",
            answer=f"{target} is {status}.",
        )
    if classification.evaluator == "annotation_coverage":
        annotation_id = _identifier(r"\bAN-[0-9A-Za-z-]+\b", message)
        target = _identifier(r"\bTK-[0-9A-Za-z-]+\b", message) or ticket_id
        if annotation_id is None or not target:
            raise ValueError("annotation coverage needs an annotation and ticket identifier")
        payload = await source.ticket_get(target)
        ticket = payload.get("ticket", payload)
        annotations = ticket.get("annotations", []) if isinstance(ticket, Mapping) else []
        annotation = next(
            (
                item
                for item in annotations
                if isinstance(item, Mapping)
                and str(item.get("annotation_id", item.get("id", ""))) == annotation_id
            ),
            None,
        )
        if annotation is None:
            return Evidence(
                kind="annotation",
                source=f"Central ticket_get({target}).annotations",
                detail=f"annotation_id={annotation_id}; found=false",
                answer=f"{annotation_id} is not present on {target}; it cannot be treated as coverage.",
            )
        text = str(annotation.get("text", annotation.get("message", "")))
        kind = str(annotation.get("kind", "unknown"))
        return Evidence(
            kind="annotation",
            source=f"Central ticket_get({target}).annotations[{annotation_id}]",
            detail=f"kind={kind}; text={text[:180]}",
            answer=f"{annotation_id} exists on {target} as kind={kind}; its recorded text must govern coverage.",
        )
    if classification.evaluator == "seat_capability":
        status = await source.board_status()
        agents = status.get("agents", [])
        named = re.findall(r"`([^`]+)`", message)
        candidates = [
            row
            for row in agents
            if isinstance(row, Mapping)
            and any(
                str(row.get(field, "")) in named
                or str(row.get(field, "")).lower() in message.lower()
                for field in ("agent_id", "agent_name")
            )
        ]
        if len(candidates) != 1:
            raise ValueError("seat capability lookup did not identify exactly one seat")
        agent = candidates[0]
        caps = agent.get("capabilities", {})
        if not isinstance(caps, Mapping):
            caps = {}
        name = str(agent.get("agent_name", agent.get("agent_id", "unknown")))
        detail = {
            "role": agent.get("role"),
            "can_work": caps.get("can_work"),
            "can_review": caps.get("can_review"),
            "lifecycle_status": agent.get("lifecycle_status"),
        }
        return Evidence(
            kind="seat_capability",
            source=f"Central board_snapshot.agents[{name}]",
            detail=json.dumps(detail, sort_keys=True, separators=(",", ":")),
            answer=f"{name} has {detail}.",
        )
    raise ValueError("no mechanical evaluator is configured")


async def make_finding(
    question: Mapping[str, Any],
    source: EvidenceSource,
    repo: Path,
    integration_ref: str,
    now: datetime,
) -> dict[str, Any]:
    message = str(question.get("message", ""))
    kind = str(question.get("kind", "information"))
    classification = classify_question(message, kind)
    evidence = Evidence(
        kind="policy",
        source=f"policy_table:{classification.rule}",
        detail=f"question_kind={kind}",
        answer=f"Would escalate: {classification.rule}.",
    )
    outcome = classification.outcome
    if classification.evaluator is not None:
        try:
            evidence = await evaluate_mechanical(
                classification, question, source, repo, integration_ref
            )
            if evidence.outcome is not None:
                outcome = evidence.outcome
        except Exception as exc:
            outcome = Outcome.UNKNOWN
            evidence = Evidence(
                kind="policy",
                source=f"policy_table:{classification.rule}",
                detail=f"evidence_error={type(exc).__name__}: {exc}",
                answer="Would escalate because the mechanical evidence was incomplete.",
            )
    next_action = (
        "Coordinator reviews the cited evidence and may answer manually."
        if outcome is Outcome.MECHANICAL
        else "Coordinator decides; the butler takes no ticket action."
    )
    return {
        "kind": "would_answer",
        "level": "info" if outcome is Outcome.MECHANICAL else "warn",
        "board_id": str(question.get("board_id", "unknown")),
        "ticket_id": str(question.get("ticket_id", "")),
        "question_id": str(question.get("question_id", "")),
        "question_kind": kind,
        "verdict": outcome.value,
        "policy_rule": classification.rule,
        "answer_class": POLICY_CLASS.get(classification.rule, "unknown"),
        "message": evidence.answer,
        "evidence_kind": evidence.kind,
        "evidence": f"source={evidence.source}; {evidence.detail}",
        "next_action": next_action,
        "mode": "shadow",
        "observed_at": now.isoformat(),
    }


def _decode_state(raw: Mapping[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    state = raw.get("state") if isinstance(raw, Mapping) else None
    value = state.get("value") if isinstance(state, Mapping) else None
    if not isinstance(value, str):
        return {
            "schema_version": 2,
            "generated_at": utc_now().isoformat(),
            "effective_mode": "shadow",
            "findings": [],
            "truncation": {"findings": 0},
        }, None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("coordinator_findings is malformed") from exc
    if not isinstance(parsed, dict):
        raise ValueError("coordinator_findings must be an object")
    parsed.setdefault("findings", [])
    return parsed, value


def _decode_evaluation(
    raw: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str | None]:
    state = raw.get("state") if isinstance(raw, Mapping) else None
    value = state.get("value") if isinstance(state, Mapping) else None
    if not isinstance(value, str):
        return {"schema_version": 1}, None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("board_butler_evaluation is malformed") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version") != 1:
        raise ValueError("board_butler_evaluation schema is unsupported")
    if not isinstance(parsed.get("evaluation"), Mapping):
        raise ValueError("board_butler_evaluation.evaluation must be an object")
    return parsed, value


def retrospective_evaluation_document(
    specification: Mapping[str, Any], identity: Any, now: datetime
) -> dict[str, Any]:
    """Build one authenticated historical mark without authored board text."""
    row = {
        "question_id": specification["question_id"],
        "ticket_id": specification["ticket_id"],
        "question_kind": specification["question_kind"],
        "draft_status": specification["draft_status"],
        "decline_reason": specification["decline_reason"],
        "drafted_by": _public_identity(identity),
        "drafted_at": now.isoformat(),
        "mark": specification["mark"],
        "mark_population": "retrospective_operator",
        "marked_by": dict(RETROSPECTIVE_MARKER),
        "marked_at": RETROSPECTIVE_MARKED_AT,
        "mark_source_question_id": RETROSPECTIVE_MARK_SOURCE_QUESTION_ID,
    }
    return {"schema_version": 1, "evaluation": row}


def _merge_retrospective_evaluation(
    existing: Mapping[str, Any], expected: Mapping[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Merge only a missing authenticated mark; reject conflicting history."""
    current = existing.get("evaluation")
    desired = expected["evaluation"]
    if not isinstance(current, Mapping):
        return dict(expected), True
    for name in (
        "question_id",
        "ticket_id",
        "question_kind",
        "draft_status",
        "decline_reason",
    ):
        if current.get(name) != desired.get(name):
            raise ValueError(
                f"retrospective evaluation conflicts on {name} for "
                f"{desired['question_id']}"
            )
    mark = current.get("mark")
    if mark is not None and any(
        current.get(name) != desired.get(name)
        for name in (
            "mark",
            "mark_population",
            "marked_by",
            "marked_at",
            "mark_source_question_id",
        )
    ):
        raise ValueError(
            f"retrospective evaluation conflicts with an existing mark for "
            f"{desired['question_id']}"
        )
    result = dict(existing)
    merged = dict(current)
    for name in (
        "mark",
        "mark_population",
        "marked_by",
        "marked_at",
        "mark_source_question_id",
    ):
        merged[name] = desired[name]
    result["schema_version"] = 1
    result["evaluation"] = merged
    return result, result != dict(existing)


async def backfill_retrospective_evaluations(
    backend: Any, now: datetime, *, board_id: str
) -> dict[str, int]:
    """Persist the bounded historical sample with CAS and idempotent retries."""
    result = {"created": 0, "updated": 0, "unchanged": 0}
    if board_id != RETROSPECTIVE_BACKFILL_BOARD_ID:
        return result
    for specification in RETROSPECTIVE_EVALUATION_BACKFILL:
        question_id = str(specification["question_id"])
        raw = await backend.evaluation(question_id)
        existing, previous_value = _decode_evaluation(raw)
        desired = retrospective_evaluation_document(
            specification, backend.identity, now
        )
        merged, changed = _merge_retrospective_evaluation(existing, desired)
        if not changed:
            result["unchanged"] += 1
            continue
        await backend.write_evaluation(
            question_id,
            json.dumps(merged, sort_keys=True, separators=(",", ":")),
            previous_value,
        )
        result["created" if previous_value is None else "updated"] += 1
    return result


def _record_time(record: Mapping[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        parsed = parse_time(record.get(key))
        if parsed is not None:
            return parsed
    return None


def _ticket_decisions(ticket: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = ticket.get("annotations", [])
    return [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("kind") == "decision"
    ] if isinstance(rows, list) else []


def _observation_topics(text: Any) -> set[str]:
    """Return exact board-visible identifiers; never infer a semantic topic."""
    value = str(text or "")
    tokens = re.findall(
        r"(?<![A-Za-z0-9])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
        r"|(?<![A-Za-z0-9])[A-Z][A-Z0-9_]{4,}(?:\.[A-Za-z0-9]+)?"
        r"|(?<![A-Za-z0-9])[A-Za-z0-9_.-]+\."
        r"(?:sha256|toml|json|ya?ml|md|py)(?![A-Za-z0-9])",
        value,
    )
    return {
        token.strip("`'\".,:;()[]{}").casefold()
        for token in tokens
        if token and not token.startswith("/PATH/TO/")
    }


def _directed_paths(text: Any) -> set[str]:
    value = str(text or "")
    return {
        match.group(1).strip("`'\".,:;()[]{}")
        for match in re.finditer(
            r"\b(?:change|edit|modify|update|regenerate|write|touch|replace)\w*"
            r"\b[^\n.]{0,120}?`?"
            r"((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)`?",
            value,
            re.I,
        )
        if not match.group(1).startswith("/PATH/TO/")
    }


def _path_is_allowed(path: str, allowed: Sequence[str]) -> bool:
    normalized = path.strip("/")
    for entry in allowed:
        candidate = str(entry).strip("/")
        if not candidate:
            continue
        if normalized == candidate:
            return True
        if entry.endswith("/") and normalized.startswith(candidate + "/"):
            return True
    return False


def _dispatch_time(row: Mapping[str, Any]) -> datetime | None:
    return _record_time(row, "at", "offered_at", "occurred_at", "updated_at")


def _dispatch_after(
    ticket: Mapping[str, Any], start: datetime, cutoff: datetime
) -> list[Mapping[str, Any]]:
    history = ticket.get("dispatch_history", [])
    if not isinstance(history, list):
        return []
    return [
        row
        for row in history
        if isinstance(row, Mapping)
        and row.get("kind", "work") == "work"
        and row.get("state") in {"offered", "broadcast"}
        and (stamp := _dispatch_time(row)) is not None
        and stamp > start
        and stamp >= cutoff
    ]


def _binding_decision(text: Any) -> bool:
    return bool(
        re.search(
            r"\b(?:do not|must not|cannot|blocked|wait|hold|only)\b.{0,180}"
            r"\b(?:until|before|after|unless|first|land|merge|approve)\b"
            r"|\b(?:until|unless|only after)\b.{0,180}\b(?:merge|land|approve|complete)\w*\b",
            str(text or ""),
            re.I | re.S,
        )
    )


def _questions_for_ticket(
    context: ObservationContext, ticket_id: str
) -> list[Mapping[str, Any]]:
    return [
        row
        for row in context.questions
        if str(row.get("ticket_id", "")) == ticket_id
    ]


def _question_matches_decision_identifiers(
    question: Mapping[str, Any], decision: Mapping[str, Any]
) -> bool:
    """Match only exact board-visible identifiers, never prose similarity."""
    decision_topics = _observation_topics(decision.get("text"))
    return bool(decision_topics & _observation_topics(question.get("message")))


def _observe_stale_open_questions(
    context: ObservationContext,
) -> list[Mapping[str, Any]]:
    observations: list[Mapping[str, Any]] = []
    for ticket_id, ticket in context.tickets.items():
        questions = [
            row
            for row in _questions_for_ticket(context, ticket_id)
            if row.get("state") == "open"
        ]
        if not questions:
            continue
        decisions = sorted(
            _ticket_decisions(ticket),
            key=lambda row: _record_time(row, "at") or datetime.min.replace(tzinfo=timezone.utc),
        )
        matched_questions: set[str] = set()
        # Explicit identifiers are authoritative even if timestamps were
        # redacted from a historical projection.
        for question in questions:
            question_id = str(question.get("question_id", ""))
            message_id = str(question.get("message_id") or "")
            decision = next(
                (
                    row
                    for row in decisions
                    if question_id in str(row.get("text", ""))
                    or (message_id and message_id in str(row.get("text", "")))
                    if (
                        (asked_at := _record_time(question, "asked_at")) is None
                        or (decided_at := _record_time(row, "at")) is None
                        or decided_at > asked_at
                    )
                ),
                None,
            )
            if decision is None:
                continue
            annotation_id = str(decision.get("annotation_id", ""))
            matched_questions.add(question_id)
            observations.append(
                {
                    "level": "warn",
                    "ticket_id": ticket_id,
                    "question_id": question_id,
                    "annotation_id": annotation_id,
                    "message": "An open coordinator question has an explicit later decision annotation.",
                    "evidence": f"question_id={question_id}; annotation_id={annotation_id}; match=explicit-id",
                    "next_action": "Coordinator: reconcile the inbox state; the butler does not answer or close it.",
                    "reconciled": True,
                }
            )
        # A later decision without an explicit question or message ID may be
        # an answer, but chronology alone cannot prove that relationship.
        # Report the missing correlation instead of silently pairing records.
        for question in questions:
            question_id = str(question.get("question_id", ""))
            if question_id in matched_questions:
                continue
            asked_at = _record_time(question, "asked_at")
            later_decisions = [
                row
                for row in decisions
                if asked_at is not None
                and (decided_at := _record_time(row, "at")) is not None
                and decided_at > asked_at
            ]
            if not later_decisions:
                continue
            observations.append(
                {
                    "level": "info",
                    "ticket_id": ticket_id,
                    "question_id": question_id,
                    "message": "A later decision exists, but board state does not explicitly link it to this open question.",
                    "evidence": f"question_id={question_id}; missing=question-or-message-id-in-decision",
                    "next_action": "Coordinator: add an explicit correlation before reconciling the inbox; chronology is not treated as an answer.",
                    "reconciled": False,
                }
            )
    return observations


def _observe_held_decisions(
    context: ObservationContext,
) -> list[Mapping[str, Any]]:
    observations: list[Mapping[str, Any]] = []
    cutoff = context.now - timedelta(days=OBSERVATION_HISTORY_DAYS)
    terminal = {"closed", "canceled", "rejected"}
    for ticket_id, ticket in context.tickets.items():
        if ticket.get("status") in terminal:
            continue
        questions = _questions_for_ticket(context, ticket_id)
        for decision in _ticket_decisions(ticket):
            if not _binding_decision(decision.get("text")):
                continue
            decided_at = _record_time(decision, "at")
            if decided_at is None:
                continue
            dispatches = _dispatch_after(ticket, decided_at, cutoff)
            if not dispatches:
                continue
            annotation_id = str(decision.get("annotation_id", ""))
            rediscovery_ids = [
                str(row.get("question_id", ""))
                for row in questions
                if (asked_at := _record_time(row, "asked_at")) is not None
                and asked_at > decided_at
                and asked_at >= cutoff
                and _question_matches_decision_identifiers(row, decision)
            ]
            observations.append(
                {
                    "level": "warn",
                    "ticket_id": ticket_id,
                    "annotation_id": annotation_id,
                    "message": "A binding decision was followed by another work dispatch and must travel with the ticket.",
                    "evidence": f"annotation_id={annotation_id}; later_dispatches={len(dispatches)}",
                    "next_action": "Show this held decision before the next seat derives the same gate again.",
                    "rediscovery_question_ids": rediscovery_ids,
                }
            )
    return observations


def _observe_repeated_standing_decisions(
    context: ObservationContext,
) -> list[Mapping[str, Any]]:
    observations: list[Mapping[str, Any]] = []
    cutoff = context.now - timedelta(days=OBSERVATION_HISTORY_DAYS)
    recent_questions = [
        row
        for row in context.questions
        if (asked_at := _record_time(row, "asked_at")) is not None
        and asked_at >= cutoff
    ]
    for ticket_id, ticket in context.tickets.items():
        for decision in _ticket_decisions(ticket):
            topics = _observation_topics(decision.get("text"))
            if not topics:
                continue
            matches = [
                row
                for row in recent_questions
                if topics & _observation_topics(row.get("message"))
            ]
            askers = {
                str((row.get("asked_by") or {}).get("agent_id", ""))
                for row in matches
                if isinstance(row.get("asked_by"), Mapping)
            }
            if len(matches) < 2 or len(askers - {""}) < 2:
                continue
            decided_at = _record_time(decision, "at")
            rediscovery_ids = [
                str(row.get("question_id", ""))
                for row in matches
                if decided_at is not None
                and (_record_time(row, "asked_at") or decided_at) > decided_at
            ]
            annotation_id = str(decision.get("annotation_id", ""))
            observations.append(
                {
                    "level": "warn",
                    "ticket_id": ticket_id,
                    "annotation_id": annotation_id,
                    "message": "Multiple seats re-escalated an exact board identifier covered by a standing decision.",
                    "evidence": f"annotation_id={annotation_id}; questions={len(matches)}; topic={sorted(topics)[0]}",
                    "next_action": "Surface the standing decision with later matching offers; do not act for the operator.",
                    "rediscovery_question_ids": rediscovery_ids,
                }
            )
    return observations


def _observe_decision_scope_drift(
    context: ObservationContext,
) -> list[Mapping[str, Any]]:
    observations: list[Mapping[str, Any]] = []
    for ticket_id, ticket in context.tickets.items():
        allowed = [
            str(path)
            for path in ticket.get("related_files", [])
            if isinstance(path, str)
        ]
        target = ticket.get("target_url")
        if isinstance(target, str) and "/" in target:
            allowed.append(target)
        if not allowed:
            continue
        for decision in _ticket_decisions(ticket):
            outside = sorted(
                path
                for path in _directed_paths(decision.get("text"))
                if not _path_is_allowed(path, allowed)
            )
            if not outside:
                continue
            annotation_id = str(decision.get("annotation_id", ""))
            observations.append(
                {
                    "level": "warn",
                    "ticket_id": ticket_id,
                    "annotation_id": annotation_id,
                    "message": "A decision directs a file change outside the ticket's declared related-file boundary.",
                    "evidence": f"annotation_id={annotation_id}; outside={','.join(outside[:3])}",
                    "next_action": "Coordinator: reconcile the decision and ticket boundary before final preflight.",
                }
            )
    return observations


OBSERVATION_RULES: tuple[ObservationRule, ...] = (
    ObservationRule("stale_open_question", 1, _observe_stale_open_questions),
    ObservationRule("held_decision", 0, _observe_held_decisions),
    ObservationRule("standing_decision_repeated", 0, _observe_repeated_standing_decisions),
    ObservationRule("decision_scope_drift", 1, _observe_decision_scope_drift),
)


def derive_board_observations(
    context: ObservationContext,
    rules: Sequence[ObservationRule] = OBSERVATION_RULES,
) -> list[dict[str, Any]]:
    """Apply registered read-only observers and return stable finding rows."""
    findings: list[dict[str, Any]] = []
    for rule in rules:
        for candidate in rule.evaluate(context):
            identifiers = {
                key: candidate[key]
                for key in ("ticket_id", "question_id", "annotation_id")
                if candidate.get(key)
            }
            material = json.dumps(
                [context.board_id, rule.name, identifiers],
                sort_keys=True,
                separators=(",", ":"),
            )
            row = {
                "kind": OBSERVATION_FINDING_KIND,
                "level": str(candidate.get("level", "warn")),
                "board_id": context.board_id,
                "observer": rule.name,
                "observer_priority": rule.priority,
                "observation_key": hashlib.sha256(material.encode("utf-8")).hexdigest()[:20],
                "message": str(candidate.get("message", "")),
                "evidence": str(candidate.get("evidence", "")),
                "next_action": str(candidate.get("next_action", "")),
                "mode": "shadow-observation",
                "observed_at": context.now.isoformat(),
                **identifiers,
            }
            if isinstance(candidate.get("rediscovery_question_ids"), list):
                row["rediscovery_question_ids"] = sorted(
                    {
                        str(value)
                        for value in candidate["rediscovery_question_ids"]
                        if value
                    }
                )
            if isinstance(candidate.get("reconciled"), bool):
                row["reconciled"] = candidate["reconciled"]
            findings.append(row)
    if not context.questions_complete or not context.tickets_complete:
        missing = []
        if not context.questions_complete:
            missing.append("question_inbox")
        if not context.tickets_complete:
            missing.append("ticket_details")
        findings.append(
            {
                "kind": OBSERVATION_FINDING_KIND,
                "level": "warn",
                "board_id": context.board_id,
                "observer": "coverage_gap",
                "observer_priority": 0,
                "observation_key": hashlib.sha256(
                    f"{context.board_id}:coverage_gap".encode("utf-8")
                ).hexdigest()[:20],
                "message": "Board Butler observation coverage is incomplete; no negative conclusion is valid.",
                "evidence": f"missing={','.join(missing)}",
                "next_action": "Restore the missing board projection and rerun observation.",
                "mode": "shadow-observation",
                "observed_at": context.now.isoformat(),
            }
        )
    return findings


def observation_replay_metrics(
    context: ObservationContext, findings: Sequence[Mapping[str, Any]]
) -> dict[str, int]:
    reconciled = {
        str(row.get("question_id"))
        for row in findings
        if row.get("observer") == "stale_open_question"
        and row.get("reconciled") is True
        and row.get("question_id")
    }
    rediscovery = {
        str(question_id)
        for row in findings
        for question_id in row.get("rediscovery_question_ids", [])
        if question_id
    }
    return {
        "open_questions": sum(
            1 for row in context.questions if row.get("state") == "open"
        ),
        "reconciled_open_questions": len(reconciled),
        "repeat_rediscovery_escalations": len(rediscovery),
    }


def rate_limit_reason(
    state: Mapping[str, Any],
    board_id: str,
    ticket_id: str,
    now: datetime,
    per_hour: int,
    per_ticket: int,
    per_board: int,
) -> str | None:
    findings = state.get("findings", [])
    drafts = [
        item
        for item in findings
        if isinstance(item, Mapping) and item.get("kind") == "would_answer"
    ]
    recent = [
        item
        for item in drafts
        if (stamp := parse_time(item.get("observed_at"))) is not None
        and now - stamp < timedelta(hours=1)
    ]
    if len(recent) >= per_hour:
        return "per_hour"
    if sum(str(item.get("ticket_id")) == ticket_id for item in drafts) >= per_ticket:
        return "per_ticket"
    if sum(str(item.get("board_id")) == board_id for item in drafts) >= per_board:
        return "per_board"
    return None


def merge_finding(
    state: Mapping[str, Any], finding: Mapping[str, Any], now: datetime
) -> dict[str, Any]:
    result = dict(state)
    rows = [
        dict(item)
        for item in state.get("findings", [])
        if isinstance(item, Mapping)
        and not (
            item.get("kind")
            in {
                "would_answer",
                "butler_queued",
                "butler_config_invalid",
                "butler_action",
            }
            and item.get("question_id") == finding.get("question_id")
        )
    ]
    critical = [item for item in rows if item.get("level") == "critical"]
    if len(critical) >= MAX_FINDINGS:
        raise ValueError("coordinator_findings has no bounded room after critical alerts")
    ordinary = [item for item in rows if item.get("level") != "critical"]
    ordinary_capacity = MAX_FINDINGS - len(critical) - 1
    selected = critical + ordinary[-ordinary_capacity:] if ordinary_capacity else critical
    selected.append(dict(finding))
    omitted = len(rows) + 1 - len(selected)
    result["findings"] = selected
    result["generated_at"] = now.isoformat()
    effective = finding.get("effective_config", {})
    result["effective_mode"] = (
        str(effective.get("effective_mode", "assist"))
        if isinstance(effective, Mapping)
        else "assist"
    )
    truncation = dict(result.get("truncation", {}))
    truncation["findings"] = int(truncation.get("findings", 0) or 0) + omitted
    result["truncation"] = truncation
    board_butler = dict(result.get("board_butler", {}))
    board_butler.update({
        "schema_version": SCHEMA_VERSION,
        "last_question_id": finding.get("question_id"),
        "last_verdict": finding.get("verdict"),
        "updated_at": now.isoformat(),
    })
    result["board_butler"] = board_butler
    # Match the coordinator's bounded state convention and keep the new draft.
    # Older non-critical findings are removed first; the truncation count makes
    # that loss explicit instead of relying on Central's 5,000-character cap.
    while (
        len(json.dumps(result, sort_keys=True, separators=(",", ":")))
        > MAX_STATE_CHARS
        and len(result["findings"]) > 1
    ):
        removable = next(
            (
                index
                for index, item in enumerate(result["findings"][:-1])
                if item.get("level") != "critical"
            ),
            None,
        )
        if removable is None:
            raise ValueError(
                "coordinator_findings has no bounded room after critical alerts"
            )
        result["findings"].pop(removable)
        result["truncation"]["findings"] += 1
    if len(json.dumps(result, sort_keys=True, separators=(",", ":"))) > MAX_STATE_CHARS:
        raise ValueError("coordinator_findings has no bounded room for a butler draft")
    return result


def merge_observation_findings(
    state: Mapping[str, Any], findings: Sequence[Mapping[str, Any]], now: datetime
) -> dict[str, Any]:
    """Replace derived observations without ever evicting a critical alert."""
    result = dict(state)
    rows = [
        dict(item)
        for item in state.get("findings", [])
        if isinstance(item, Mapping)
        and item.get("kind") != OBSERVATION_FINDING_KIND
    ]
    unique = {
        str(item.get("observation_key")): dict(item)
        for item in findings
        if isinstance(item, Mapping) and item.get("observation_key")
    }
    # Lower-priority observations are appended first. Bounded removal takes
    # the oldest non-critical row, so priority 0 observations survive longest.
    observations = sorted(
        unique.values(),
        key=lambda item: (
            -int(item.get("observer_priority", 9)),
            str(item.get("observer", "")),
            str(item.get("observation_key", "")),
        ),
    )
    critical = [item for item in rows if item.get("level") == "critical"]
    ordinary = [item for item in rows if item.get("level") != "critical"]
    ordinary_and_observed = ordinary + observations
    capacity = max(0, MAX_FINDINGS - len(critical))
    selected = critical + ordinary_and_observed[-capacity:] if capacity else critical
    omitted = max(0, len(rows) + len(observations) - len(selected))
    result["findings"] = selected
    result["generated_at"] = now.isoformat()
    truncation = dict(result.get("truncation", {}))
    truncation["findings"] = int(truncation.get("findings", 0) or 0) + omitted
    result["truncation"] = truncation
    board_butler = dict(result.get("board_butler", {}))
    board_butler["observations"] = {
        "derived": len(observations),
        "retained": sum(
            item.get("kind") == OBSERVATION_FINDING_KIND
            for item in result["findings"]
        ),
        "updated_at": now.isoformat(),
    }
    result["board_butler"] = board_butler
    while len(json.dumps(result, sort_keys=True, separators=(",", ":"))) > MAX_STATE_CHARS:
        removable = next(
            (
                index
                for index, item in enumerate(result["findings"])
                if item.get("level") != "critical"
            ),
            None,
        )
        if removable is None:
            # The prior critical-only state was already accepted by Central;
            # retain it byte-for-byte rather than displacing an alert or
            # attempting an oversized observation update.
            return dict(state)
        result["findings"].pop(removable)
        result["truncation"]["findings"] += 1
    retained = sum(
        item.get("kind") == OBSERVATION_FINDING_KIND
        for item in result["findings"]
    )
    if isinstance(result.get("board_butler"), dict):
        result["board_butler"]["observations"]["retained"] = retained
    return result


def rate_limit_finding(
    question: Mapping[str, Any], reason: str, now: datetime
) -> dict[str, Any]:
    return {
        "kind": "butler_queued",
        "level": "warn",
        "board_id": str(question.get("board_id", "unknown")),
        "ticket_id": str(question.get("ticket_id", "")),
        "question_id": str(question.get("question_id", "")),
        "verdict": Outcome.ESCALATE.value,
        "message": f"Board butler draft cap hit: {reason}.",
        "evidence": f"source=board_butler_rate_limit; limit={reason}",
        "queue_reason": reason,
        "next_action": "Queued for the coordinator; the question was not dropped.",
        "mode": "shadow",
        "observed_at": now.isoformat(),
    }


def config_invalid_finding(
    question: Mapping[str, Any], error: ButlerConfigError, now: datetime
) -> dict[str, Any]:
    return {
        "kind": "butler_config_invalid",
        "level": "critical",
        "board_id": str(question.get("board_id", "unknown")),
        "ticket_id": str(question.get("ticket_id", "")),
        "question_id": str(question.get("question_id", "")),
        "verdict": Outcome.ESCALATE.value,
        "message": "Board butler configuration is invalid; queued for the coordinator.",
        "evidence": f"source=coordinator_config; error={error}",
        "next_action": "Correct the declared schema before enabling any automation.",
        "mode": "shadow",
        "observed_at": now.isoformat(),
    }


def decorate_finding(
    finding: Mapping[str, Any], config: EffectiveConfig, now: datetime
) -> dict[str, Any]:
    result = dict(finding)
    answer_class = str(result.get("answer_class", "unknown"))
    configured_action = config.answer_scope.get(answer_class, "escalate")
    evidence_kind = str(result.get("evidence_kind", ""))
    evidence_allowed = evidence_kind in config.required_evidence_kinds
    result["configured_action"] = configured_action
    result["auto_eligible"] = bool(
        result.get("verdict") == Outcome.MECHANICAL.value
        and configured_action == "auto"
        and evidence_allowed
        and config.future_active_state == "eligible"
        and config.effective_answering_mode == "autonomous"
    )
    if configured_action == "auto" and not evidence_allowed:
        result["auto_eligible"] = False
        result["next_action"] = (
            "Coordinator handles the question because the configured evidence floor was not met."
        )
    if config.answering_mode == "off":
        result["auto_eligible"] = False
        result["next_action"] = "Question answering is disabled for this board."
    release_at = now + timedelta(seconds=config.hold_before_post_s)
    hold_status = "pending" if result["auto_eligible"] else config.effective_answering_mode
    result["hold"] = {
        "status": hold_status,
        "drafted_at": now.isoformat(),
        "release_at": release_at.isoformat(),
        "vetoable_until": release_at.isoformat(),
        "veto_reason": None,
    }
    result["effective_config"] = config.as_finding()
    authority = {
        "question_id": str(result.get("question_id", "")),
        "ticket_id": str(result.get("ticket_id", "")),
        "question_kind": str(result.get("question_kind", "")),
        "verdict": str(result.get("verdict", "")),
        "policy_rule": str(result.get("policy_rule", "")),
        "answer_class": answer_class,
        "evidence_kind": evidence_kind,
        "evidence": str(result.get("evidence", "")),
        "configured_action": configured_action,
        "required_evidence_kinds": list(config.required_evidence_kinds),
        "answering_mode": config.effective_answering_mode,
    }
    result["authority_proof"] = {
        "schema": "autonomous_butler_answer_authority_v1",
        "digest_sha256": _sha256_json(authority),
        "policy_rule": authority["policy_rule"],
        "evidence_kind": evidence_kind,
        "config_sources": list(config.source_layers),
    }
    return result


def _bound_control_state(
    state: dict[str, Any], *, preserve_question_id: str | None = None
) -> dict[str, Any]:
    findings = state.get("findings", [])
    if not isinstance(findings, list):
        findings = []
        state["findings"] = findings
    truncation = dict(state.get("truncation", {}))
    while len(json.dumps(state, sort_keys=True, separators=(",", ":"))) > MAX_STATE_CHARS:
        removable = next(
            (
                index
                for index, item in enumerate(findings)
                if isinstance(item, Mapping)
                and item.get("level") != "critical"
                and item.get("question_id") != preserve_question_id
            ),
            None,
        )
        if removable is None:
            raise ValueError("coordinator_findings has no bounded room for control state")
        findings.pop(removable)
        truncation["findings"] = int(truncation.get("findings", 0) or 0) + 1
        state["truncation"] = truncation
    return state


def veto_question(
    state: Mapping[str, Any], question_id: str, reason: str, now: datetime
) -> dict[str, Any]:
    normalized_reason = reason.strip()
    if not normalized_reason or len(normalized_reason) > 200:
        raise ValueError("a veto reason from 1 to 200 characters is required")
    result = dict(state)
    findings = [dict(item) for item in state.get("findings", []) if isinstance(item, Mapping)]
    selected = next(
        (item for item in findings if str(item.get("question_id", "")) == question_id),
        None,
    )
    if selected is None:
        raise ValueError(f"no durable draft exists for {question_id}")
    hold = dict(selected.get("hold", {}))
    hold.update(
        {
            "status": "vetoed",
            "veto_reason": normalized_reason,
            "vetoed_at": now.isoformat(),
        }
    )
    selected["hold"] = hold
    result["findings"] = findings
    board_butler = dict(result.get("board_butler", {}))
    board_butler["last_veto"] = {
        "question_id": question_id,
        "reason": normalized_reason,
        "at": now.isoformat(),
    }
    history = board_butler.get("veto_history", [])
    history = [
        item
        if isinstance(item, (str, int)) and not isinstance(item, bool)
        else str(item.get("at"))
        for item in history
        if (isinstance(item, (str, int)) and not isinstance(item, bool))
        or (isinstance(item, Mapping) and isinstance(item.get("at"), str))
    ]
    history.append(int(now.timestamp()))
    board_butler["veto_history"] = history[-100:]
    board_butler["updated_at"] = now.isoformat()
    result["board_butler"] = board_butler
    result["generated_at"] = now.isoformat()
    result["effective_mode"] = "shadow"
    return _bound_control_state(result, preserve_question_id=question_id)


def engage_kill_switch(
    state: Mapping[str, Any], reason: str, now: datetime
) -> dict[str, Any]:
    normalized_reason = reason.strip() or "operator"
    if len(normalized_reason) > 200:
        raise ValueError("a kill-switch reason cannot exceed 200 characters")
    result = dict(state)
    board_butler = dict(result.get("board_butler", {}))
    board_butler["kill_switch"] = {
        "engaged": True,
        "reason": normalized_reason,
        "at": now.isoformat(),
    }
    board_butler["updated_at"] = now.isoformat()
    result["board_butler"] = board_butler
    result["generated_at"] = now.isoformat()
    result["effective_mode"] = "shadow"
    return _bound_control_state(result)


def assert_independent_identity(
    identity: Any, agents: Sequence[Mapping[str, Any]]
) -> None:
    if getattr(identity, "role", None) != "coordinator":
        raise IdentityConflict("board butler must join with role=coordinator")
    principal_id = getattr(identity, "principal_id", None)
    agent_id = getattr(identity, "agent_id", None)
    own_rows = [agent for agent in agents if agent.get("agent_id") == agent_id]
    if len(own_rows) != 1:
        raise IdentityConflict("board butler identity is absent or duplicated")
    own_caps = own_rows[0].get("capabilities", {})
    if not isinstance(own_caps, Mapping):
        own_caps = {}
    if own_caps.get("can_work") is not False or own_caps.get("can_review") is not False:
        raise IdentityConflict("board butler seat must disable work and review")
    conflicts = []
    for agent in agents:
        if agent.get("agent_id") == agent_id or agent.get("principal_id") != principal_id:
            continue
        caps = agent.get("capabilities", {})
        if not isinstance(caps, Mapping):
            caps = {}
        active_role = (
            agent.get("lifecycle_status") == "active"
            and agent.get("role") in {"worker", "reviewer"}
        )
        if active_role or caps.get("can_work") is True or caps.get("can_review") is True:
            conflicts.append(str(agent.get("agent_name", agent.get("agent_id"))))
    if conflicts:
        raise IdentityConflict(
            "board butler principal also works or reviews: " + ", ".join(conflicts)
        )


def assert_complete_agent_view(status: Mapping[str, Any]) -> None:
    omitted = status.get("omitted_counts", status.get("truncation_counts", {}))
    omitted_agents = omitted.get("agents", 0) if isinstance(omitted, Mapping) else 0
    if omitted_agents:
        raise IdentityConflict(
            "board butler cannot prove principal independence from a truncated agent view"
        )


def _capable_live_worker(
    agent: Mapping[str, Any], now: datetime, *, stale_seconds: int = 300
) -> bool:
    capabilities = agent.get("capabilities")
    if not isinstance(capabilities, Mapping):
        capabilities = {}
    if (
        capabilities.get("can_work") is not True
        or agent.get("capabilities_explicit") is not True
        or agent.get("lifecycle_status", "active") != "active"
        or agent.get("role") in {"coordinator", "orchestrator"}
    ):
        return False
    seen = parse_time(agent.get("last_activity_at") or agent.get("last_seen"))
    return seen is not None and (now - seen).total_seconds() <= stale_seconds


def _no_live_candidate_cycle_count(ticket: Mapping[str, Any]) -> int:
    history = ticket.get("dispatch_history")
    if not isinstance(history, list):
        summary = ticket.get("dispatch_summary")
        history = summary.get("last", []) if isinstance(summary, Mapping) else []
    return sum(
        1
        for row in history
        if isinstance(row, Mapping)
        and row.get("state") == "broadcast"
        and row.get("kind", "work") == "work"
        and row.get("reason") == "no_live_candidates"
    )


def _annotation_has_marker(ticket: Mapping[str, Any], marker: str) -> bool:
    return any(
        isinstance(row, Mapping)
        and marker in str(row.get("text", row.get("message", "")))
        for row in ticket.get("annotations", [])
    )


def mechanical_action_id(action: MechanicalAction) -> str:
    material = json.dumps(
        [
            action.board_id,
            action.ticket_id,
            action.kind,
            action.identity_id or action.identity_name,
        ],
        separators=(",", ":"),
    )
    return "BA-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def mechanical_action_finding(
    action: MechanicalAction, now: datetime, hold_seconds: int
) -> dict[str, Any]:
    action_id = mechanical_action_id(action)
    release_at = now + timedelta(seconds=hold_seconds)
    identity = action.identity_name or action.identity_id
    message = (
        f"Board butler intends to park {action.ticket_id} after "
        f"{action.observed_cycles} no_live_candidates cycles because the board "
        "has no live can_work=true seat."
        if action.kind == "park_no_live_candidates"
        else f"Board butler intends to refuse escalation target {identity} for "
        f"{action.ticket_id} because {action.reason}."
    )
    return {
        # Reuse the existing durable-hold projection consumed by Fleet's
        # Waiting for you surface; action_id/action_class distinguish actions
        # from coordinator-question drafts without changing Fleet assets.
        "kind": "would_answer",
        "level": "warn",
        "board_id": action.board_id,
        "ticket_id": action.ticket_id,
        "question_id": action_id,
        "action_id": action_id,
        "action_class": action.kind,
        "verdict": Outcome.MECHANICAL.value,
        "message": message,
        "evidence": (
            "source=Central board_snapshot+ticket_get; "
            f"reason={action.reason}"
        ),
        "next_action": (
            "Veto during the hold window or allow the configured mechanical "
            "action to execute."
        ),
        "mode": "active-hold",
        "observed_at": now.isoformat(),
        "hold": {
            "status": "pending",
            "drafted_at": now.isoformat(),
            "release_at": release_at.isoformat(),
            "vetoable_until": release_at.isoformat(),
            "veto_reason": None,
        },
    }


def mechanical_hold_status(finding: Mapping[str, Any], now: datetime) -> str:
    hold = finding.get("hold")
    if not isinstance(hold, Mapping):
        return "invalid"
    status = str(hold.get("status", "invalid"))
    if status not in {"held", "pending"}:
        return status
    release_at = parse_time(hold.get("release_at"))
    return "ready" if release_at is not None and release_at <= now else "held"


def reconcile_mechanical_holds(
    state: Mapping[str, Any], active_action_ids: set[str], now: datetime
) -> tuple[dict[str, Any], list[str]]:
    """Withdraw pending action holds whose board-state predicate disappeared."""
    result = dict(state)
    findings = [
        dict(item) for item in state.get("findings", []) if isinstance(item, Mapping)
    ]
    withdrawn: list[str] = []
    for finding in findings:
        action_id = str(finding.get("action_id") or "")
        if (
            not action_id
            or finding.get("action_class") not in MECHANICAL_ACTION_CLASSES
            or action_id in active_action_ids
        ):
            continue
        hold = dict(finding.get("hold", {}))
        if hold.get("status") not in {"held", "pending"}:
            continue
        hold.update(
            {
                "status": "withdrawn",
                "withdrawn_at": now.isoformat(),
                "withdrawal_reason": "board-state predicate no longer holds",
            }
        )
        finding["hold"] = hold
        withdrawn.append(action_id)
    if withdrawn:
        result["findings"] = findings
        result["generated_at"] = now.isoformat()
    return result, withdrawn


def ensure_mechanical_hold(
    state: Mapping[str, Any],
    action: MechanicalAction,
    now: datetime,
    hold_seconds: int,
) -> tuple[dict[str, Any], str]:
    """Register a new hold episode, or report the current episode's status."""
    action_id = mechanical_action_id(action)
    existing = next(
        (
            item
            for item in state.get("findings", [])
            if isinstance(item, Mapping)
            and item.get("kind") in {"would_answer", "butler_action"}
            and item.get("action_id") == action_id
        ),
        None,
    )
    hold = existing.get("hold", {}) if isinstance(existing, Mapping) else {}
    status = hold.get("status") if isinstance(hold, Mapping) else None
    if existing is None or status == "withdrawn":
        finding = mechanical_action_finding(action, now, hold_seconds)
        return merge_finding(state, finding, now), "registered"
    return dict(state), mechanical_hold_status(existing, now)


def plan_mechanical_actions(
    board_id: str,
    snapshot: Mapping[str, Any],
    previous: Mapping[str, Any],
    full_tickets: Mapping[str, Mapping[str, Any]],
    now: datetime,
    *,
    no_live_candidates_cycles: int,
) -> list[MechanicalAction]:
    """Plan only the two state-provable actions authorized for the butler."""
    agents = [row for row in snapshot.get("agents", []) if isinstance(row, Mapping)]
    agents_by_id = {str(row.get("agent_id")): row for row in agents if row.get("agent_id")}
    agents_by_name = {
        str(row.get("agent_name")): row for row in agents if row.get("agent_name")
    }
    actions: list[MechanicalAction] = []
    seen_refusals: set[tuple[str, str]] = set()
    for finding in previous.get("findings", []):
        if not isinstance(finding, Mapping):
            continue
        ticket_id = str(finding.get("ticket_id") or "")
        target_id = str(
            finding.get("would_assign_to_agent_id")
            or finding.get("target_agent_id")
            or ""
        )
        target_name = str(
            finding.get("would_assign_to_agent_name")
            or finding.get("target_agent_name")
            or ""
        )
        if not ticket_id or (not target_id and not target_name):
            continue
        agent = agents_by_id.get(target_id) or agents_by_name.get(target_name)
        if agent is not None and _capable_live_worker(agent, now):
            continue
        ticket = full_tickets.get(ticket_id, {})
        marker_key = (ticket_id, target_id or target_name)
        marker = f"{REFUSAL_ANNOTATION_MARKER}:{target_id or target_name}"
        if marker_key in seen_refusals:
            continue
        seen_refusals.add(marker_key)
        reason = (
            "capabilities.can_work is not true"
            if agent is not None
            else "identity is missing or reaped"
        )
        actions.append(
            MechanicalAction(
                "refuse_incapable_target",
                board_id,
                ticket_id,
                target_name or None,
                target_id or None,
                None,
                reason,
                not _annotation_has_marker(ticket, marker),
            )
        )

    if any(_capable_live_worker(agent, now) for agent in agents):
        return actions
    for ticket_id, ticket in sorted(full_tickets.items()):
        if ticket.get("status") != "open":
            continue
        if ticket.get("parked") is True and not _annotation_has_marker(
            ticket, PARK_ANNOTATION_MARKER
        ):
            continue
        cycles = _no_live_candidate_cycle_count(ticket)
        if cycles < no_live_candidates_cycles:
            continue
        actions.append(
            MechanicalAction(
                "park_no_live_candidates",
                board_id,
                ticket_id,
                None,
                None,
                cycles,
                "repeated no_live_candidates cycles and no live can_work=true seat",
                not _annotation_has_marker(ticket, PARK_ANNOTATION_MARKER),
            )
        )
    return actions


class CentralBackend:
    """Central adapter for push waits, real derivation, reads, and bounded CAS writes."""

    def __init__(self, args: argparse.Namespace, token: str) -> None:
        self.args = args
        self.token = token
        self.client: Any = None
        self.identity: Any = None
        self._context: Any = None
        self.latest_seq = 0
        self.project_name: str | None = None
        self._coordinator: dict[str, Any] | None = None

    async def __aenter__(self) -> "CentralBackend":
        from pursers_client import BoardClient

        self._context = BoardClient(
            self.args.url,
            self.token,
            self.args.home_board,
            agent_name=self.args.agent_name,
            role="coordinator",
            capabilities=dict(BOARD_BUTLER_CAPABILITIES),
            allow_takeover=True,
        )
        self.client = await self._context.__aenter__()
        try:
            self.identity = self.client.identity
            status = await self.client.board_snapshot(limit=1_000, max_bytes=750_000)
            assert_complete_agent_view(status)
            assert_independent_identity(self.identity, status.get("agents", []))
            self.latest_seq = max(0, int(status.get("latest_seq", 0) or 0))
            self.project_name = await self._project_name_from_registry()
            if not self.args.dry_run and not (
                self.args.kill_switch or self.args.veto_question
            ):
                await backfill_retrospective_evaluations(
                    self, utc_now(), board_id=self.args.home_board
                )
        except BaseException:
            await self._context.__aexit__(*sys.exc_info())
            self.client = None
            raise
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._context is not None:
            await self._context.__aexit__(*args)

    async def ticket_get(self, ticket_id: str) -> Mapping[str, Any]:
        return await self.client.ticket_get(ticket_id)

    async def board_status(self) -> Mapping[str, Any]:
        status = await self.client.board_snapshot(limit=1_000, max_bytes=750_000)
        assert_complete_agent_view(status)
        return status

    async def answered_questions(self) -> Sequence[Mapping[str, Any]]:
        result = await self.client.board_question_inbox(state="answered", limit=100)
        rows = result.get("questions", [])
        return rows if isinstance(rows, list) else []

    async def pending_questions(self) -> Sequence[Mapping[str, Any]]:
        """Return coordinator-owned work without exposing the host binding."""
        result = await self.client.board_question_inbox(limit=100)
        rows = result.get("questions", [])
        pending = [
            {**dict(row), "board_id": self.args.home_board}
            for row in rows
            if isinstance(row, Mapping) and row.get("state") in {"open", "accepted"}
        ]
        own_agent_id = str(getattr(self.identity, "agent_id", ""))
        for row in rows:
            if not isinstance(row, Mapping) or row.get("state") != "answered":
                continue
            answered_by = row.get("answered_by") or {}
            if answered_by.get("agent_id") != own_agent_id:
                continue
            question_id = str(row.get("question_id", ""))
            raw = await self.evaluation(question_id)
            evaluation_state, _previous = _decode_evaluation(raw)
            evaluation = evaluation_state.get("evaluation", {})
            audit = (
                evaluation.get("answer_audit", {})
                if isinstance(evaluation, Mapping)
                else {}
            )
            if isinstance(audit, Mapping) and audit.get("status") in {
                "pending",
                "accepted",
            }:
                pending.append({**dict(row), "board_id": self.args.home_board})
        return pending

    async def question(self, ticket_id: str, question_id: str) -> Mapping[str, Any] | None:
        result = await self.client.board_question_inbox(ticket_id=ticket_id, limit=100)
        return next(
            (
                {**dict(row), "board_id": self.args.home_board}
                for row in result.get("questions", [])
                if isinstance(row, Mapping) and row.get("question_id") == question_id
            ),
            None,
        )

    async def accept_question(
        self, ticket_id: str, question_id: str
    ) -> Mapping[str, Any]:
        return await self.client.ticket_question_answer(
            ticket_id, question_id, action="accept"
        )

    async def answer_question(
        self, ticket_id: str, question_id: str, message: str
    ) -> Mapping[str, Any]:
        return await self.client.ticket_question_answer(
            ticket_id, question_id, action="answer", message=message
        )

    async def release_question(
        self, ticket_id: str, question_id: str
    ) -> Mapping[str, Any]:
        return await self.client.ticket_question_answer(
            ticket_id, question_id, action="release"
        )

    async def coordinator_config(self) -> Mapping[str, Any]:
        try:
            raw = await self.client.board_state_get(CONFIG_KEY)
        except Exception:
            return {}
        state = raw.get("state", {})
        value = state.get("value") if isinstance(state, Mapping) else None
        try:
            parsed = json.loads(value) if isinstance(value, str) else {}
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}

    async def _autonomous_fleet_configs(
        self, board_ids: Sequence[str]
    ) -> dict[str, Mapping[str, Any]]:
        """Read authoritative per-board config through Central's typed API."""
        from pursers_client import BoardClient

        configs: dict[str, Mapping[str, Any]] = {}
        for board_id in sorted(set(board_ids)):
            async with BoardClient(
                self.args.url,
                self.token,
                board_id,
                agent_name=self.args.agent_name,
                role="coordinator",
                capabilities=dict(BOARD_BUTLER_CAPABILITIES),
                allow_takeover=True,
            ) as client:
                result = await client.butler_config_get()
            config = result.get("config")
            if result.get("effective_mode") == "autonomous":
                if not isinstance(config, Mapping):
                    raise ButlerConfigError(
                        f"{board_id}: autonomous fleet config is missing"
                    )
                configs[board_id] = config
        return configs

    async def _write_fleet_state(
        self, board_id: str, document: Mapping[str, Any]
    ) -> None:
        """Publish actual/desired state with Central compare-and-swap."""
        from pursers_client import BoardClient

        async with BoardClient(
            self.args.url,
            self.token,
            board_id,
            agent_name=self.args.agent_name,
            role="coordinator",
            capabilities=dict(BOARD_BUTLER_CAPABILITIES),
            allow_takeover=True,
        ) as client:
            try:
                current = await client.board_state_get(FLEET_STATE_KEY)
            except Exception as exc:
                if "state key not found" not in str(exc).lower():
                    raise
                previous = None
            else:
                state = current.get("state", {})
                previous = state.get("value") if isinstance(state, Mapping) else None
                if previous is not None and not isinstance(previous, str):
                    raise RuntimeError("autonomous fleet state is malformed")
            expected = (
                hashlib.sha256(previous.encode("utf-8")).hexdigest()
                if previous is not None
                else None
            )
            encoded = json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            await client.board_state_update(
                FLEET_STATE_KEY, encoded, expected_sha256=expected
            )

    async def _reconcile_fleet(
        self,
        active_boards: Sequence[str],
        board_snapshots: Mapping[str, Mapping[str, Any]],
        now: datetime,
    ) -> Mapping[str, Any]:
        """Run one production desired-state cycle when locally enabled."""
        required_args = (
            "fleet_observation_file",
            "fleet_state_file",
            "fleet_executor_socket",
            "fleet_executor_key_id",
            "fleet_executor_private_key",
        )
        values = [getattr(self.args, name, None) for name in required_args]
        if not any(values):
            return {"status": "disabled"}
        if not all(values):
            raise RuntimeError("fleet runtime configuration is incomplete")
        if getattr(self.args, "runtime_mode", "shadow") != "active":
            raise RuntimeError("fleet reconciliation requires active runtime mode")
        configs = await self._autonomous_fleet_configs(active_boards)
        if not configs:
            return {"status": "shadow", "boards": []}
        observation = FileFleetObservationSource(
            self.args.fleet_observation_file
        ).load(now)
        provider_maximums_raw = observation["provider_maximums"]
        provider_maximums: dict[str, dict[str, int]] = {}
        for board_id in configs:
            row = provider_maximums_raw.get(board_id)
            if (
                not isinstance(row, Mapping)
                or not row
                or any(
                    not isinstance(key, str)
                    or not key
                    or not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                    for key, value in row.items()
                )
            ):
                raise ButlerConfigError(
                    f"{board_id}: provider fleet maximums are invalid"
                )
            provider_maximums[board_id] = dict(row)
        host_policy, policies = fleet_policies_from_config(
            configs, provider_maximums, now
        )
        selected_snapshots = {
            board_id: board_snapshots[board_id] for board_id in configs
        }
        snapshot = fleet_snapshot_from_products(
            selected_snapshots,
            observation["executor_seats"],
            observation["provider_observations"],
            observation["host_observation"],
            now,
        )
        revisions = {
            board_id: int(config["revision"])
            for board_id, config in configs.items()
        }
        fingerprints = {
            board_id: str(config["envelope"]["fingerprint_sha256"])
            for board_id, config in configs.items()
        }
        registry_material = json.dumps(
            {"revisions": revisions, "fingerprints": fingerprints},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        registry_digest = hashlib.sha256(registry_material).hexdigest()
        reconciler = FleetReconciler(
            host_policy,
            policies,
            config_revision=max(1, int(registry_digest[:15], 16)),
            authorization_fingerprint_sha256=registry_digest,
            config_revisions=revisions,
            authorization_fingerprints=fingerprints,
        )
        store = FileFleetStateStore(self.args.fleet_state_file)
        report = reconciler.reconcile(
            snapshot,
            store,
            UnixFleetExecutorClient(
                self.args.fleet_executor_socket,
                self.args.fleet_executor_key_id,
                self.args.fleet_executor_private_key,
            ),
        )
        if report.get("status") == "shadow":
            return {
                "status": "shadow",
                "effective_state": "shadow",
                "boards": sorted(configs),
                "reason_code": str(report.get("reason_code", "unknown")),
                "audit_evidence": copy.deepcopy(
                    list(report.get("audit_evidence", []))
                ),
            }
        for board_id in sorted(configs):
            await self._write_fleet_state(
                board_id,
                report["state_documents"][board_id],
            )
        return {
            "status": "reconciled",
            "boards": sorted(configs),
            "operations": len(report["operations"]),
            "receipt_outcomes": [
                str(item.get("outcome", "unknown"))
                for item in report["receipts"]
            ],
        }

    async def _project_name_from_registry(self) -> str | None:
        try:
            raw = await self.client.board_state_get("project_registry")
        except Exception:
            return None
        state = raw.get("state", {})
        value = state.get("value") if isinstance(state, Mapping) else None
        try:
            document = json.loads(value) if isinstance(value, str) else value
        except json.JSONDecodeError:
            return None
        projects = document.get("projects") if isinstance(document, Mapping) else None
        if not isinstance(projects, Mapping):
            return None
        matches = [
            name
            for name, row in projects.items()
            if isinstance(name, str)
            and isinstance(row, Mapping)
            and row.get("board_id") == self.args.home_board
            and row.get("status", "active") == "active"
        ]
        return matches[0] if len(matches) == 1 else None

    async def findings(self) -> Mapping[str, Any]:
        try:
            return await self.client.board_state_get(STATE_KEY)
        except Exception as exc:
            if "state key not found" in str(exc).lower():
                return {}
            raise

    async def evaluation(self, question_id: str) -> Mapping[str, Any]:
        try:
            return await self.client.board_state_get(evaluation_state_key(question_id))
        except Exception as exc:
            if "state key not found" in str(exc).lower():
                return {}
            raise

    async def write_findings(
        self, value: str, expected_value: str | None
    ) -> Mapping[str, Any]:
        expected = (
            hashlib.sha256(expected_value.encode("utf-8")).hexdigest()
            if expected_value is not None
            else None
        )
        return await self.client.board_state_update(
            STATE_KEY, value, expected_sha256=expected
        )

    async def write_evaluation(
        self, question_id: str, value: str, expected_value: str | None
    ) -> Mapping[str, Any]:
        expected = (
            hashlib.sha256(expected_value.encode("utf-8")).hexdigest()
            if expected_value is not None
            else None
        )
        return await self.client.board_state_update(
            evaluation_state_key(question_id), value, expected_sha256=expected
        )

    async def wait_for_question(
        self, cursor: int, timeout_s: float | None
    ) -> tuple[int, Mapping[str, Any] | None]:
        current = [cursor]
        resources = [
            f"board://{self.args.home_board}/journal",
            f"board://{self.args.home_board}/agent/{self.identity.agent_id}",
        ]
        events = self.client.events(
            from_cursor=cursor,
            only_mine=False,
            kinds=[QUESTION_EVENT],
            resource_subscriptions=resources,
            acknowledge=False,
            touch=False,
            cursor_callback=lambda value: current.__setitem__(0, max(current[0], int(value))),
        )

        async def next_question() -> Mapping[str, Any] | None:
            async with aclosing(events):
                async for event in events:
                    seq = event.get("seq")
                    if isinstance(seq, int) and not isinstance(seq, bool):
                        current[0] = max(current[0], seq)
                    if event.get("kind") != QUESTION_EVENT:
                        continue
                    ticket_id = event.get("ticket_id")
                    question_id = event.get("question_id")
                    if not isinstance(ticket_id, str) or not isinstance(question_id, str):
                        continue
                    inbox = await self.client.board_question_inbox(ticket_id=ticket_id)
                    for question in inbox.get("questions", []):
                        if question.get("question_id") == question_id:
                            return {
                                **question,
                                "board_id": self.args.home_board,
                                "ticket_id": ticket_id,
                            }
            return None

        try:
            if timeout_s is None:
                question = await next_question()
            else:
                async with asyncio.timeout(timeout_s):
                    question = await next_question()
        except TimeoutError:
            question = None
        return current[0], question

    def _coordinator_api(self) -> dict[str, Any]:
        if self._coordinator is None:
            path = self.args.repo / "tools" / "coordinator" / "coordinator.py"
            if not path.is_file():
                raise RuntimeError(f"real coordinator derivation is missing: {path}")
            self._coordinator = runpy.run_path(
                str(path), run_name="board_butler_real_coordinator"
            )
        return self._coordinator

    async def _full_tickets_for_board(
        self, board_id: str, ticket_ids: Sequence[str]
    ) -> dict[str, Mapping[str, Any]]:
        from pursers_client import BoardClient

        result: dict[str, Mapping[str, Any]] = {}
        async with BoardClient(
            self.args.url,
            self.token,
            board_id,
            agent_name=self.args.agent_name,
            role="coordinator",
            capabilities=dict(BOARD_BUTLER_CAPABILITIES),
            allow_takeover=True,
        ) as client:
            for ticket_id in sorted(set(ticket_ids)):
                payload = await client.ticket_get(
                    ticket_id, view="full", include_dispatch_history=True
                )
                ticket = payload.get("ticket", {})
                if isinstance(ticket, Mapping):
                    result[ticket_id] = ticket
        return result

    async def _observation_context_for_board(
        self, board_id: str, snapshot: Mapping[str, Any], now: datetime
    ) -> ObservationContext:
        """Read one bounded board projection; no host or filesystem input."""
        from pursers_client import BoardClient

        questions: list[Mapping[str, Any]] = []
        tickets: dict[str, Mapping[str, Any]] = {}
        questions_complete = True
        tickets_complete = not (
            snapshot.get("truncated") is True
            and snapshot.get("coordination_tickets_complete") is not True
        )
        async with BoardClient(
            self.args.url,
            self.token,
            board_id,
            agent_name=self.args.agent_name,
            role="coordinator",
            capabilities=dict(BOARD_BUTLER_CAPABILITIES),
            allow_takeover=True,
        ) as client:
            question_rows: dict[str, Mapping[str, Any]] = {}
            for question_state in ("open", "accepted", "answered"):
                try:
                    inbox = await client.board_question_inbox(
                        state=question_state, limit=100
                    )
                except Exception:
                    questions_complete = False
                    continue
                inbox_rows = inbox.get("questions", [])
                visible = (
                    [row for row in inbox_rows if isinstance(row, Mapping)]
                    if isinstance(inbox_rows, list)
                    else []
                )
                total = inbox.get("total")
                if not isinstance(total, int) or total != len(visible):
                    questions_complete = False
                for row in visible:
                    question_id = row.get("question_id")
                    if isinstance(question_id, str) and question_id:
                        question_rows[question_id] = row
            questions = list(question_rows.values())

            compact = snapshot.get(
                "coordination_tickets", snapshot.get("tickets", [])
            )
            compact_rows = [
                row for row in compact if isinstance(row, Mapping)
            ] if isinstance(compact, list) else []
            question_ticket_ids = [
                str(row.get("ticket_id"))
                for row in questions
                if row.get("ticket_id")
            ]
            active_ticket_ids = [
                str(row.get("ticket_id"))
                for row in sorted(
                    compact_rows,
                    key=lambda row: str(row.get("updated_at", "")),
                    reverse=True,
                )
                if row.get("ticket_id")
                and isinstance(row.get("annotation_count"), int)
                and not isinstance(row.get("annotation_count"), bool)
                and row.get("annotation_count", 0) > 0
            ]
            ordered_ids = list(dict.fromkeys(question_ticket_ids + active_ticket_ids))
            if len(ordered_ids) > OBSERVATION_TICKET_LIMIT:
                tickets_complete = False
            for ticket_id in ordered_ids[:OBSERVATION_TICKET_LIMIT]:
                try:
                    payload = await client.ticket_get(
                        ticket_id, view="full", include_dispatch_history=True
                    )
                except Exception:
                    tickets_complete = False
                    continue
                ticket = payload.get("ticket", {})
                if isinstance(ticket, Mapping):
                    tickets[ticket_id] = ticket
                    if int(ticket.get("annotations_omitted_count", 0) or 0) > 0:
                        tickets_complete = False
                else:
                    tickets_complete = False
        return ObservationContext(
            board_id=board_id,
            tickets=tickets,
            questions=tuple(questions),
            now=now,
            questions_complete=questions_complete,
            tickets_complete=tickets_complete,
        )

    async def _write_observation_findings(
        self,
        board_id: str,
        findings: Sequence[Mapping[str, Any]],
        now: datetime,
    ) -> None:
        from pursers_client import BoardClient

        async with BoardClient(
            self.args.url,
            self.token,
            board_id,
            agent_name=self.args.agent_name,
            role="coordinator",
            capabilities=dict(BOARD_BUTLER_CAPABILITIES),
            allow_takeover=True,
        ) as client:
            try:
                raw = await client.board_state_get(STATE_KEY)
            except Exception as exc:
                if "state key not found" not in str(exc).lower():
                    raise
                raw = {}
            state, previous_value = _decode_state(raw)
            merged = merge_observation_findings(state, findings, now)
            expected = (
                hashlib.sha256(previous_value.encode("utf-8")).hexdigest()
                if previous_value is not None
                else None
            )
            await client.board_state_update(
                STATE_KEY,
                json.dumps(merged, sort_keys=True, separators=(",", ":")),
                expected_sha256=expected,
            )

    async def _execute_mechanical_action(self, action: MechanicalAction) -> None:
        from pursers_client import BoardClient

        async with BoardClient(
            self.args.url,
            self.token,
            action.board_id,
            agent_name=self.args.agent_name,
            role="coordinator",
            capabilities=dict(BOARD_BUTLER_CAPABILITIES),
            allow_takeover=True,
        ) as client:
            if action.kind == "refuse_incapable_target":
                identity = action.identity_name or action.identity_id or "unknown"
                marker = (
                    f"{REFUSAL_ANNOTATION_MARKER}:"
                    f"{action.identity_id or action.identity_name or 'unknown'}"
                )
                if action.annotation_required:
                    await client.ticket_annotate(
                        action.ticket_id,
                        f"{marker} — refused escalation target {identity}: "
                        f"{action.reason}; no assignment was performed.",
                        kind="decision",
                    )
                return
            if action.kind != "park_no_live_candidates":
                raise ValueError(f"unsupported board-butler action: {action.kind}")
            if action.annotation_required:
                await client.ticket_annotate(
                    action.ticket_id,
                    f"{PARK_ANNOTATION_MARKER} — parked mechanically after "
                    f"{action.observed_cycles} no_live_candidates dispatch cycles; "
                    "the active registry board has no live can_work=true seat. "
                    "The ticket remains open and was not canceled.",
                    kind="decision",
                )
            await client.ticket_update(action.ticket_id, parked=True)

    async def _mechanical_hold_status(
        self, action: MechanicalAction, now: datetime
    ) -> str:
        """Register a durable hold or return its current execution status."""
        from pursers_client import BoardClient

        async with BoardClient(
            self.args.url,
            self.token,
            action.board_id,
            agent_name=self.args.agent_name,
            role="coordinator",
            capabilities=dict(BOARD_BUTLER_CAPABILITIES),
            allow_takeover=True,
        ) as client:
            try:
                raw = await client.board_state_get(STATE_KEY)
            except Exception as exc:
                if "state key not found" not in str(exc).lower():
                    raise
                raw = {}
            state, previous_value = _decode_state(raw)
            merged, status = ensure_mechanical_hold(
                state, action, now, self.args.action_hold_seconds
            )
            if status == "registered":
                expected = (
                    hashlib.sha256(previous_value.encode("utf-8")).hexdigest()
                    if previous_value is not None
                    else None
                )
                await client.board_state_update(
                    STATE_KEY,
                    json.dumps(merged, sort_keys=True, separators=(",", ":")),
                    expected_sha256=expected,
                )
                return "registered"
            return status

    async def _reconcile_mechanical_holds(
        self, board_id: str, active_action_ids: set[str], now: datetime
    ) -> list[str]:
        """Persist withdrawal when an intended action is no longer provable."""
        from pursers_client import BoardClient

        async with BoardClient(
            self.args.url,
            self.token,
            board_id,
            agent_name=self.args.agent_name,
            role="coordinator",
            capabilities=dict(BOARD_BUTLER_CAPABILITIES),
            allow_takeover=True,
        ) as client:
            try:
                raw = await client.board_state_get(STATE_KEY)
            except Exception as exc:
                if "state key not found" not in str(exc).lower():
                    raise
                raw = {}
            state, previous_value = _decode_state(raw)
            reconciled, withdrawn = reconcile_mechanical_holds(
                state, active_action_ids, now
            )
            if not withdrawn:
                return []
            encoded = json.dumps(
                _bound_control_state(reconciled),
                sort_keys=True,
                separators=(",", ":"),
            )
            expected = (
                hashlib.sha256(previous_value.encode("utf-8")).hexdigest()
                if previous_value is not None
                else None
            )
            await client.board_state_update(
                STATE_KEY, encoded, expected_sha256=expected
            )
            return withdrawn

    async def _mark_mechanical_hold_executed(
        self, action: MechanicalAction, now: datetime
    ) -> None:
        from pursers_client import BoardClient

        action_id = mechanical_action_id(action)
        async with BoardClient(
            self.args.url,
            self.token,
            action.board_id,
            agent_name=self.args.agent_name,
            role="coordinator",
            capabilities=dict(BOARD_BUTLER_CAPABILITIES),
            allow_takeover=True,
        ) as client:
            raw = await client.board_state_get(STATE_KEY)
            state, previous_value = _decode_state(raw)
            rows = [
                dict(item)
                for item in state.get("findings", [])
                if isinstance(item, Mapping)
            ]
            selected = next(
                (
                    item
                    for item in rows
                    if item.get("kind") in {"would_answer", "butler_action"}
                    and item.get("action_id") == action_id
                ),
                None,
            )
            if selected is None:
                raise RuntimeError(f"durable action hold disappeared: {action_id}")
            hold = dict(selected.get("hold", {}))
            if hold.get("status") not in {"held", "pending"}:
                raise RuntimeError(f"action hold is no longer executable: {action_id}")
            hold["status"] = "executed"
            hold["executed_at"] = now.isoformat()
            selected["hold"] = hold
            state["findings"] = rows
            state["generated_at"] = now.isoformat()
            encoded = json.dumps(
                _bound_control_state(state, preserve_question_id=action_id),
                sort_keys=True,
                separators=(",", ":"),
            )
            expected = (
                hashlib.sha256(previous_value.encode("utf-8")).hexdigest()
                if previous_value is not None
                else None
            )
            await client.board_state_update(
                STATE_KEY, encoded, expected_sha256=expected
            )

    async def refresh_registry_findings(self, now: datetime) -> dict[str, Any]:
        """Act only on opted-in boards, then run coordinator's real derivation."""
        coordinator = self._coordinator_api()
        async with coordinator["RawReader"](self.args.url, self.token) as reader:
            projects, snapshots, previous = await coordinator["read_cycle"](
                reader, self.args.home_board
            )
        active_boards = {project.board_id for project in projects}
        fleet = await self._reconcile_fleet(
            sorted(active_boards), snapshots, now
        )
        observation_contexts = {
            board_id: await self._observation_context_for_board(
                board_id, snapshots[board_id], now
            )
            for board_id in sorted(active_boards)
        }
        observations = {
            board_id: derive_board_observations(context)
            for board_id, context in observation_contexts.items()
        }
        configured = (
            set(self.args.act_on_board)
            if getattr(self.args, "runtime_mode", "shadow") == "active"
            else set()
        )
        acting_boards = configured & active_boards
        actions: list[MechanicalAction] = []
        if not self.args.dry_run:
            for board_id in sorted(acting_boards):
                snapshot = snapshots[board_id]
                tickets = snapshot.get(
                    "coordination_tickets", snapshot.get("tickets", [])
                )
                ticket_ids = [
                    str(row.get("ticket_id"))
                    for row in tickets
                    if isinstance(row, Mapping) and row.get("ticket_id")
                ]
                for finding in previous.get(board_id, {}).get("findings", []):
                    if isinstance(finding, Mapping) and finding.get("ticket_id"):
                        ticket_ids.append(str(finding["ticket_id"]))
                full_tickets = await self._full_tickets_for_board(
                    board_id, ticket_ids
                )
                board_actions = plan_mechanical_actions(
                    board_id,
                    snapshot,
                    previous.get(board_id, {}),
                    full_tickets,
                    now,
                    no_live_candidates_cycles=(
                        self.args.no_live_candidates_cycles
                    ),
                )
                enabled_actions = [
                    action
                    for action in board_actions
                    if action.kind in self.args.active_action
                ]
                await self._reconcile_mechanical_holds(
                    board_id,
                    {mechanical_action_id(action) for action in enabled_actions},
                    now,
                )
                for action in enabled_actions:
                    hold_status = await self._mechanical_hold_status(action, now)
                    if hold_status == "ready":
                        await self._execute_mechanical_action(action)
                        await self._mark_mechanical_hold_executed(action, now)
                        actions.append(action)

        coordinator_args = coordinator["parse_args"](
            [
                "--url",
                self.args.url,
                "--token-path",
                str(self.args.token_path),
                "--home-board",
                self.args.home_board,
                "--agent-name",
                self.args.agent_name,
                "--mode",
                "shadow",
                "--once",
                *(["--dry-run"] if self.args.dry_run else []),
            ]
        )
        await coordinator["run"](coordinator_args)
        if not self.args.dry_run:
            for board_id in sorted(active_boards):
                await self._write_observation_findings(
                    board_id, observations[board_id], now
                )
        return {
            "active_boards": sorted(active_boards),
            "acting_boards": sorted(acting_boards),
            "ignored_acting_boards": sorted(configured - active_boards),
            "actions": [action.kind for action in actions],
            "observations": {
                board_id: {
                    "findings": len(observations[board_id]),
                    **observation_replay_metrics(
                        observation_contexts[board_id], observations[board_id]
                    ),
                }
                for board_id in sorted(active_boards)
            },
            "fleet": dict(fleet),
            "refreshed_at": now.isoformat(),
        }


def _read_token(path: Path) -> str:
    if not path.is_absolute() or not path.is_file():
        raise ValueError("--token-path must name an existing absolute file")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("token file is empty")
    return token


def load_cursor(path: Path) -> int | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    cursor = value.get("cursor") if isinstance(value, Mapping) else None
    return cursor if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor > 0 else None


def save_cursor(path: Path, cursor: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"schema_version": 1, "cursor": cursor}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _answer_audit_document(
    evaluation_state: Mapping[str, Any], **updates: Any
) -> dict[str, Any]:
    result = copy.deepcopy(dict(evaluation_state))
    evaluation = result.get("evaluation")
    if not isinstance(evaluation, dict):
        raise ValueError("answer audit requires a durable draft evaluation")
    audit = evaluation.get("answer_audit")
    if not isinstance(audit, dict):
        raise ValueError("answer audit requires an autonomous answer plan")
    audit.update(updates)
    evaluation["answer_audit"] = audit
    result["evaluation"] = evaluation
    return result


async def _write_answer_audit(
    backend: CentralBackend,
    question_id: str,
    evaluation_state: Mapping[str, Any],
    previous_value: str,
    **updates: Any,
) -> tuple[dict[str, Any], str]:
    updated = _answer_audit_document(evaluation_state, **updates)
    encoded = json.dumps(updated, sort_keys=True, separators=(",", ":"))
    await backend.write_evaluation(question_id, encoded, previous_value)
    return updated, encoded


async def _record_answer_failure(
    backend: CentralBackend,
    question_id: str,
    reason_code: str,
    now: datetime,
) -> None:
    """Persist only a bounded reason code; provider/credential detail is dropped."""
    raw = await backend.findings()
    state, previous_value = _decode_state(raw)
    board_butler = dict(state.get("board_butler", {}))
    history = [
        dict(item)
        for item in board_butler.get("answer_failure_history", [])
        if isinstance(item, Mapping)
    ]
    history.append(
        {"question_id": question_id, "reason_code": reason_code, "at": now.isoformat()}
    )
    board_butler["answer_failure_history"] = history[-100:]
    board_butler["updated_at"] = now.isoformat()
    state["board_butler"] = board_butler
    state["generated_at"] = now.isoformat()
    bounded = _bound_control_state(state, preserve_question_id=question_id)
    await backend.write_findings(
        json.dumps(bounded, sort_keys=True, separators=(",", ":")), previous_value
    )


async def advance_autonomous_answer(
    backend: CentralBackend,
    question: Mapping[str, Any],
    finding: Mapping[str, Any],
    evaluation_state: Mapping[str, Any],
    previous_evaluation_value: str,
    args: argparse.Namespace,
    now: datetime,
) -> dict[str, Any]:
    """Accept and, after the durable hold, answer through Central's bound client."""
    question_id = str(question.get("question_id", ""))
    ticket_id = str(question.get("ticket_id", ""))
    evaluation = evaluation_state.get("evaluation", {})
    audit = evaluation.get("answer_audit", {}) if isinstance(evaluation, Mapping) else {}
    if not isinstance(audit, Mapping) or audit.get("status") in {
        "answered",
        "escalated",
        "failed",
    }:
        return dict(finding)
    current = await backend.question(ticket_id, question_id)
    if current is None:
        await _write_answer_audit(
            backend,
            question_id,
            evaluation_state,
            previous_evaluation_value,
            status="escalated",
            reason_code="question_missing",
        )
        return dict(finding)
    if current.get("state") == "answered":
        await _write_answer_audit(
            backend,
            question_id,
            evaluation_state,
            previous_evaluation_value,
            status="answered",
            answered_at=current.get("answered_at"),
            reason_code="central_already_answered",
        )
        return dict(finding)

    accepted_by = current.get("accepted_by") or {}
    own_agent_id = str(getattr(backend.identity, "agent_id", ""))
    if accepted_by and accepted_by.get("agent_id") != own_agent_id:
        await _write_answer_audit(
            backend,
            question_id,
            evaluation_state,
            previous_evaluation_value,
            status="escalated",
            reason_code="owned_by_other_coordinator",
        )
        return dict(finding)
    if (
        current.get("state") == "accepted"
        and accepted_by.get("agent_id") == own_agent_id
    ):
        response = await backend.release_question(ticket_id, question_id)
        released = response.get("question", {})
        if released.get("state") != "open" or released.get("accepted_by"):
            raise RuntimeError("Central did not release accepted question ownership")

    # Leave an open question unowned throughout the vetoable hold and every
    # policy/evidence recheck.  Central's atomic answer call is the ownership
    # boundary; if delivery fails, a human coordinator can still answer it.
    audit_state = dict(evaluation_state)
    audit_previous = previous_evaluation_value

    raw = await backend.findings()
    state, _previous_state_value = _decode_state(raw)
    durable_finding = next(
        (
            dict(item)
            for item in state.get("findings", [])
            if isinstance(item, Mapping)
            and item.get("question_id") == question_id
            and item.get("kind") == "would_answer"
        ),
        None,
    )
    if durable_finding is None:
        await _write_answer_audit(
            backend,
            question_id,
            audit_state,
            audit_previous,
            status="escalated",
            reason_code="durable_plan_missing",
        )
        return dict(finding)
    hold = durable_finding.get("hold", {})
    if not isinstance(hold, Mapping) or hold.get("status") == "vetoed":
        await _write_answer_audit(
            backend,
            question_id,
            audit_state,
            audit_previous,
            status="escalated",
            reason_code="vetoed",
        )
        return dict(finding)

    document = await backend.coordinator_config()
    config = resolve_config(
        document,
        args,
        state,
        now,
        project_name=getattr(backend, "project_name", None),
    )
    if config.effective_answering_mode != "autonomous":
        await _write_answer_audit(
            backend,
            question_id,
            audit_state,
            audit_previous,
            status="escalated",
            reason_code="autonomy_disabled",
        )
        return dict(finding)

    refreshed = decorate_finding(
        await make_finding(question, backend, args.repo, args.integration_ref, now),
        config,
        now,
    )
    expected_digest = str(audit.get("authority_digest_sha256", ""))
    actual_digest = str((refreshed.get("authority_proof") or {}).get("digest_sha256", ""))
    if not refreshed.get("auto_eligible") or actual_digest != expected_digest:
        await _write_answer_audit(
            backend,
            question_id,
            audit_state,
            audit_previous,
            status="escalated",
            reason_code="authority_or_evidence_changed",
        )
        return dict(finding)

    release_at = parse_time(hold.get("release_at"))
    if release_at is None or now < release_at:
        return dict(finding)
    answer = str(audit.get("answer", ""))
    if not answer or len(answer) > MAX_AUTONOMOUS_ANSWER_CHARS:
        await _record_answer_failure(backend, question_id, "answer_bounds", now)
        await _write_answer_audit(
            backend,
            question_id,
            audit_state,
            audit_previous,
            status="failed",
            reason_code="answer_bounds",
            attempts=int(audit.get("attempts", 0) or 0) + 1,
        )
        return dict(finding)
    try:
        response = await backend.answer_question(ticket_id, question_id, answer)
    except Exception:
        await _record_answer_failure(backend, question_id, "central_answer_failed", now)
        await _write_answer_audit(
            backend,
            question_id,
            audit_state,
            audit_previous,
            status="failed",
            reason_code="central_answer_failed",
            attempts=int(audit.get("attempts", 0) or 0) + 1,
        )
        return dict(finding)
    answered = response.get("question", {})
    event = response.get("event") or {}
    await _write_answer_audit(
        backend,
        question_id,
        audit_state,
        audit_previous,
        status="answered",
        answered_at=answered.get("answered_at") or now.isoformat(),
        event_id=event.get("id"),
        attempts=int(audit.get("attempts", 0) or 0) + 1,
        reason_code="duplicate" if response.get("duplicate") else None,
    )
    delivered = dict(finding)
    delivered["answer_status"] = "answered"
    return delivered


async def process_question(
    backend: CentralBackend,
    question: Mapping[str, Any],
    args: argparse.Namespace,
    now: datetime,
) -> dict[str, Any]:
    raw = await backend.findings()
    state, previous_value = _decode_state(raw)
    question_id = str(question.get("question_id", ""))
    evaluation_reader = getattr(backend, "evaluation", None)
    raw_evaluation = (
        await evaluation_reader(question_id) if callable(evaluation_reader) else {}
    )
    evaluation_state, previous_evaluation_value = _decode_evaluation(raw_evaluation)
    existing = next(
        (
            dict(item)
            for item in state.get("findings", [])
            if isinstance(item, Mapping)
            and item.get("kind")
            in {"would_answer", "butler_queued", "butler_config_invalid"}
            and str(item.get("question_id", "")) == question_id
        ),
        None,
    )
    if existing is not None:
        evaluation = evaluation_state.get("evaluation", {})
        answer_audit = (
            evaluation.get("answer_audit", {})
            if isinstance(evaluation, Mapping)
            else {}
        )
        if (
            isinstance(answer_audit, Mapping)
            and answer_audit.get("status") in {"pending", "accepted"}
            and previous_evaluation_value is not None
            and callable(getattr(backend, "question", None))
        ):
            return await advance_autonomous_answer(
                backend,
                question,
                existing,
                evaluation_state,
                previous_evaluation_value,
                args,
                now,
            )
        if not isinstance(evaluation_state.get("evaluation"), Mapping):
            repaired = record_draft_evaluation(
                evaluation_state, question, existing, backend.identity, now
            )
            await backend.write_evaluation(
                question_id,
                json.dumps(repaired, sort_keys=True, separators=(",", ":")),
                previous_evaluation_value,
            )
        return existing
    document = await backend.coordinator_config()
    try:
        config = resolve_config(
            document,
            args,
            state,
            now,
            project_name=getattr(backend, "project_name", None),
        )
        # Provider configuration is intentionally re-resolved on every question
        # cycle, so a dashboard save takes effect without restarting the resident.
        # Runtime objects stay local and are never serialized into findings.
        secret_root = getattr(args, "provider_secrets_dir", None)
        resolve_provider_runtime(config, "classification", secret_root)
        drafting_provider = resolve_provider_runtime(config, "drafting", secret_root)
    except ButlerConfigError as exc:
        safe_config = resolve_config(
            {}, args, state, now, project_name=getattr(backend, "project_name", None)
        )
        finding = decorate_finding(
            config_invalid_finding(question, exc, now), safe_config, now
        )
        answered_reader = getattr(backend, "answered_questions", None)
        answered = await answered_reader() if callable(answered_reader) else []
        precedents = find_precedents(question, answered)
        finding["precedents"] = precedents
        finding["precedent_status"] = "found" if precedents else "none"
        paired = record_draft_evaluation(
            evaluation_state, question, finding, backend.identity, now
        )
        merged = merge_finding(state, finding, now)
        encoded = json.dumps(merged, sort_keys=True, separators=(",", ":"))
        if args.dry_run:
            print(json.dumps(finding, indent=2, sort_keys=True))
        else:
            await backend.write_evaluation(
                question_id,
                json.dumps(paired, sort_keys=True, separators=(",", ":")),
                previous_evaluation_value,
            )
            await backend.write_findings(encoded, previous_value)
        return finding
    reason = rate_limit_reason(
        state,
        str(question.get("board_id", args.home_board)),
        str(question.get("ticket_id", "")),
        now,
        config.drafts_per_hour,
        config.drafts_per_ticket,
        config.drafts_per_board,
    )
    if reason:
        finding = rate_limit_finding(question, reason, now)
    else:
        finding = await make_finding(
            question, backend, args.repo, args.integration_ref, now
        )
        if drafting_provider is not None and config.answering_mode != "off":
            try:
                finding["message"] = await draft_with_provider(
                    drafting_provider, question, finding
                )
                finding["draft_source"] = "configured_provider"
            except Exception:
                finding["verdict"] = Outcome.UNKNOWN.value
                finding["message"] = (
                    "Would escalate because the configured drafting provider failed."
                )
                finding["draft_source"] = "configured_provider_failed"
    answered_reader = getattr(backend, "answered_questions", None)
    answered = await answered_reader() if callable(answered_reader) else []
    precedents = find_precedents(question, answered)
    finding["precedents"] = precedents
    finding["precedent_status"] = "found" if precedents else "none"
    finding = decorate_finding(finding, config, now)
    paired = record_draft_evaluation(
        evaluation_state, question, finding, backend.identity, now
    )
    paired_encoded = json.dumps(paired, sort_keys=True, separators=(",", ":"))
    merged = merge_finding(state, finding, now)
    encoded = json.dumps(merged, sort_keys=True, separators=(",", ":"))
    if args.dry_run:
        print(json.dumps(finding, indent=2, sort_keys=True))
    else:
        await backend.write_evaluation(
            question_id,
            paired_encoded,
            previous_evaluation_value,
        )
        await backend.write_findings(encoded, previous_value)
        if finding.get("auto_eligible") is True and callable(
            getattr(backend, "answer_question", None)
        ):
            return await advance_autonomous_answer(
                backend,
                question,
                finding,
                paired,
                paired_encoded,
                args,
                now,
            )
    return finding


async def apply_control_action(
    backend: CentralBackend, args: argparse.Namespace, now: datetime
) -> None:
    raw = await backend.findings()
    state, previous_value = _decode_state(raw)
    if args.kill_switch:
        state = engage_kill_switch(state, args.control_reason, now)
    elif args.veto_question:
        state = veto_question(state, args.veto_question, args.control_reason, now)
    else:
        return
    await backend.write_findings(
        json.dumps(state, sort_keys=True, separators=(",", ":")), previous_value
    )


async def run(
    args: argparse.Namespace,
    *,
    backend_factory: Any = CentralBackend,
) -> None:
    # One-shot controls must remain available while the resident owns the
    # singleton lock. Their coordinator_findings write is CAS-protected by the
    # backend, so they cannot create a second resident or race silently.
    if args.kill_switch or args.veto_question:
        token = _read_token(args.token_path)
        async with backend_factory(args, token) as backend:
            await apply_control_action(backend, args, utc_now())
        return

    # Resident startup deliberately locks before token reads and board access.
    with SingletonLock(args.pid_file):
        if local_kill_engaged(getattr(args, "local_kill_file", None)):
            print("board-butler: local kill switch is engaged", file=sys.stderr)
            return
        token = _read_token(args.token_path)
        with RuntimeStatus(
            getattr(args, "runtime_status_file", None),
            getattr(args, "runtime_mode", "shadow"),
        ) as runtime:
            async with backend_factory(args, token) as backend:
                cursor = load_cursor(args.cursor_file)
                if cursor is None:
                    cursor = backend.latest_seq
                refresh = getattr(backend, "refresh_registry_findings", None)
                next_refresh = 0.0
                while True:
                    monotonic_now = asyncio.get_running_loop().time()
                    refreshed_cycle = False
                    if refresh is not None and monotonic_now >= next_refresh:
                        observation = await refresh(utc_now())
                        refreshed_cycle = True
                        runtime.mark("registry_refresh")
                        print(
                            "board-butler: refresh "
                            + json.dumps(observation, sort_keys=True),
                            file=sys.stderr,
                        )
                        next_refresh = (
                            asyncio.get_running_loop().time() + args.refresh_seconds
                        )
                    pending_reader = getattr(backend, "pending_questions", None)
                    if refreshed_cycle and callable(pending_reader):
                        # Accepted ownership and hold timers are durable. This
                        # replay closes the crash window without polling: it is
                        # tied to the existing bounded registry refresh cycle.
                        for pending in await pending_reader():
                            await process_question(backend, pending, args, utc_now())
                    timeout = (
                        float(args.wait_timeout)
                        if args.once
                        else (
                            max(
                                0.1,
                                next_refresh - asyncio.get_running_loop().time(),
                            )
                            if refresh is not None
                            else None
                        )
                    )
                    cursor, question = await backend.wait_for_question(cursor, timeout)
                    if question is not None:
                        await process_question(backend, question, args, utc_now())
                        runtime.mark("question_processed")
                    # Printing a proposed draft is not durable processing. Keep
                    # dry-run questions replayable by leaving the cursor alone.
                    if not args.dry_run:
                        save_cursor(args.cursor_file, cursor)
                    if args.once:
                        return
                    if question is None:
                        if refresh is not None:
                            continue
                        raise RuntimeError("board butler push subscription ended")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("ONBOARD_CENTRAL_URL", DEFAULT_URL))
    parser.add_argument("--token-path", type=Path, required=True)
    parser.add_argument("--home-board", default="pursers")
    parser.add_argument("--agent-name", default=DEFAULT_AGENT_NAME)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--integration-ref", default="origin/main")
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--cursor-file", type=Path, required=True)
    parser.add_argument("--runtime-status-file", type=Path)
    parser.add_argument("--local-kill-file", type=Path)
    parser.add_argument(
        "--runtime-mode",
        choices=("shadow", "active"),
        default="shadow",
        help="local service mode; active additionally requires an authorization file",
    )
    parser.add_argument("--active-authorization-file", type=Path)
    parser.add_argument(
        "--fleet-observation-file",
        type=Path,
        default=(
            Path(os.environ["PURSERS_BUTLER_FLEET_OBSERVATION_FILE"]).expanduser()
            if os.environ.get("PURSERS_BUTLER_FLEET_OBSERVATION_FILE")
            else None
        ),
    )
    parser.add_argument(
        "--fleet-state-file",
        type=Path,
        default=(
            Path(os.environ["PURSERS_BUTLER_FLEET_STATE_FILE"]).expanduser()
            if os.environ.get("PURSERS_BUTLER_FLEET_STATE_FILE")
            else None
        ),
    )
    parser.add_argument(
        "--fleet-executor-socket",
        type=Path,
        default=(
            Path(os.environ["PURSERS_BUTLER_FLEET_EXECUTOR_SOCKET"]).expanduser()
            if os.environ.get("PURSERS_BUTLER_FLEET_EXECUTOR_SOCKET")
            else None
        ),
    )
    parser.add_argument(
        "--fleet-executor-key-id",
        default=os.environ.get("PURSERS_BUTLER_FLEET_EXECUTOR_KEY_ID"),
    )
    parser.add_argument(
        "--fleet-executor-private-key",
        type=Path,
        default=(
            Path(os.environ["PURSERS_BUTLER_FLEET_EXECUTOR_PRIVATE_KEY"]).expanduser()
            if os.environ.get("PURSERS_BUTLER_FLEET_EXECUTOR_PRIVATE_KEY")
            else None
        ),
    )
    parser.add_argument(
        "--provider-secrets-dir",
        type=Path,
        default=(
            Path(os.environ.get("PURSERS_STATE_DIR", "~/.pursers")).expanduser()
            / "board-butler"
            / "secrets"
        ),
    )
    parser.add_argument("--drafts-per-hour", type=int, default=DEFAULT_DRAFTS_PER_HOUR)
    parser.add_argument("--drafts-per-ticket", type=int, default=DEFAULT_DRAFTS_PER_TICKET)
    parser.add_argument("--drafts-per-board", type=int, default=DEFAULT_DRAFTS_PER_BOARD)
    parser.add_argument("--project", default=os.environ.get("PURSERS_PROJECT"))
    parser.add_argument("--wait-timeout", type=int, default=180)
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=DEFAULT_REFRESH_SECONDS,
        help="maximum interval between real coordinator derivations",
    )
    parser.add_argument(
        "--act-on-board",
        action="append",
        default=[],
        metavar="BOARD_ID",
        help=(
            "explicitly opt one active registry board into the two mechanical "
            "ticket actions; repeat for multiple boards"
        ),
    )
    parser.add_argument(
        "--no-live-candidates-cycles",
        type=int,
        default=DEFAULT_NO_LIVE_CANDIDATES_CYCLES,
    )
    parser.add_argument(
        "--active-action",
        action="append",
        choices=MECHANICAL_ACTION_CLASSES,
        help=(
            "enabled autonomous action class; repeat to configure the acting "
            "set (defaults to the two operator-approved mechanical classes)"
        ),
    )
    parser.add_argument(
        "--action-hold-seconds",
        type=int,
        default=DEFAULT_ACTION_HOLD_SECONDS,
        help="veto window before an enabled mechanical action can execute",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--kill-switch", action="store_true")
    controls.add_argument("--veto-question")
    parser.add_argument("--control-reason", default="operator")
    args = parser.parse_args(argv)
    for name in (
        "token_path",
        "repo",
        "pid_file",
        "cursor_file",
        "provider_secrets_dir",
    ):
        value = getattr(args, name)
        if not value.is_absolute():
            parser.error(f"--{name.replace('_', '-')} must be absolute")
    for name in (
        "runtime_status_file",
        "local_kill_file",
        "active_authorization_file",
        "fleet_observation_file",
        "fleet_state_file",
        "fleet_executor_socket",
        "fleet_executor_private_key",
    ):
        value = getattr(args, name)
        if value is not None and not value.is_absolute():
            parser.error(f"--{name.replace('_', '-')} must be absolute")
    if not args.repo.is_dir():
        parser.error("--repo must name an existing directory")
    if not 1 <= args.drafts_per_hour <= 100:
        parser.error("--drafts-per-hour must be between 1 and 100")
    if not 1 <= args.drafts_per_ticket <= 20:
        parser.error("--drafts-per-ticket must be between 1 and 20")
    if not 1 <= args.drafts_per_board <= 500:
        parser.error("--drafts-per-board must be between 1 and 500")
    if args.wait_timeout < 1:
        parser.error("--wait-timeout must be positive")
    if not 10 <= args.refresh_seconds <= 60:
        parser.error("--refresh-seconds must be between 10 and 60")
    if not 1 <= args.no_live_candidates_cycles <= 50:
        parser.error("--no-live-candidates-cycles must be between 1 and 50")
    if not 1 <= args.action_hold_seconds <= 86_400:
        parser.error("--action-hold-seconds must be between 1 and 86400")
    if args.active_action is None:
        args.active_action = list(MECHANICAL_ACTION_CLASSES)
    if any(not value.strip() for value in args.act_on_board):
        parser.error("--act-on-board values must be non-empty")
    if args.runtime_mode == "shadow" and args.act_on_board:
        parser.error("--act-on-board requires --runtime-mode active")
    if args.runtime_mode == "shadow" and args.active_authorization_file is not None:
        parser.error("--active-authorization-file requires --runtime-mode active")
    if args.runtime_mode == "active":
        if args.dry_run:
            parser.error("active runtime mode cannot be combined with --dry-run")
        if not args.act_on_board:
            parser.error("active runtime mode requires at least one --act-on-board")
        if args.active_authorization_file is None:
            parser.error("active runtime mode requires --active-authorization-file")
        try:
            validate_active_authorization(args.active_authorization_file)
        except ValueError as exc:
            parser.error(str(exc))
    fleet_values = (
        args.fleet_observation_file,
        args.fleet_state_file,
        args.fleet_executor_socket,
        args.fleet_executor_key_id,
        args.fleet_executor_private_key,
    )
    if any(fleet_values) and not all(fleet_values):
        parser.error("fleet runtime options must be configured together")
    if all(fleet_values) and args.runtime_mode != "active":
        parser.error("fleet reconciliation requires --runtime-mode active")
    if args.fleet_executor_key_id is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", args.fleet_executor_key_id
    ):
        parser.error("--fleet-executor-key-id is invalid")
    if (args.kill_switch or args.veto_question) and args.dry_run:
        parser.error("control actions cannot be combined with --dry-run")
    if args.veto_question and not args.control_reason.strip():
        parser.error("--veto-question requires a non-empty --control-reason")
    if len(args.control_reason.strip()) > 200:
        parser.error("--control-reason cannot exceed 200 characters")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        asyncio.run(run(args))
    except AlreadyRunning as exc:
        print(f"board-butler: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
