#!/usr/bin/env python3
"""Registry-wide coordinator findings refresher and shadow question drafter.

The butler runs the real coordinator derivation for every active registry board
on a bounded cycle and listens for coordinator questions through the same
journal/seat resource subscriptions used by the wait bridge. It never answers a
question. Its Central writes are CAS-protected findings and identifier-only
evaluation records; the only ticket mutations are two explicitly configured,
mechanically checkable safety actions: parking repeated ``no_live_candidates``
loops and recording refusal of an escalation target that cannot work.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import runpy
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


STATE_KEY = "coordinator_findings"
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
PROVIDER_TIMEOUT_S = 30.0
PROVIDER_DRAFT_PROTOCOLS = frozenset(
    {"pursers_json_v1", "openai_chat_completions_v1"}
)
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
    "runtime": "shadow-only",
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
            r"|\b(?:release(?!-)|publish|tag|ship|promote)\b.{0,60}\b(?:now|to production|this release)\b",
            re.I | re.S,
        ),
    ),
    PolicyRule(
        "membership-or-registry",
        Outcome.ESCALATE,
        re.compile(r"\b(?:membership|invite|admit|retire seat|registry|register board|project registry|change role)\b", re.I),
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
            # Sending code intentionally does not exist in this ticket.
            "effective_mode": "shadow",
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

    def request() -> str:
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
        with opener.open(raw, timeout=PROVIDER_TIMEOUT_S) as response:
            geturl = getattr(response, "geturl", None)
            final_url = geturl() if callable(geturl) else raw.full_url
            if _provider_origin(final_url) != _provider_origin(raw.full_url):
                raise ValueError("provider response changed origin")
            payload = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        if len(payload) > MAX_PROVIDER_RESPONSE_BYTES:
            raise ValueError("provider response exceeded the safe bound")
        document = json.loads(payload)
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

    return await asyncio.to_thread(request)


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
            demote, ("veto_count", "window_s"), f"{path}.auto_demote"
        )
        result["auto_demote"] = {
            name: _bounded_int(raw, f"{path}.auto_demote.{name}", minimum, maximum)
            for name, raw, minimum, maximum in (
                ("veto_count", demote.get("veto_count"), 1, 100),
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
    else:
        future_state = "eligible"
    return EffectiveConfig(
        configured_mode=merged["mode"],
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
    # An approval request is itself authority-bearing.  A decision-labelled
    # question can still ask for a deterministic fact, so content rules get a
    # chance to prove it mechanical before the fail-closed kind fallback.
    if kind == "approval":
        return Classification(Outcome.ESCALATE, "question-kind:approval")
    for rule in POLICY_TABLE:
        if rule.pattern.search(message):
            return Classification(rule.outcome, rule.name, rule.evaluator)
    if kind == "decision":
        return Classification(Outcome.ESCALATE, "question-kind:decision")
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
    """Upsert an identifier-only pairing row without authored text."""
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
    result["effective_mode"] = "shadow"
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
    )
    if configured_action == "auto" and not evidence_allowed:
        result["auto_eligible"] = False
        result["next_action"] = (
            "Coordinator handles the question because the configured evidence floor was not met."
        )
    release_at = now + timedelta(seconds=config.hold_before_post_s)
    result["hold"] = {
        "status": "shadow",
        "drafted_at": now.isoformat(),
        "release_at": release_at.isoformat(),
        "vetoable_until": release_at.isoformat(),
        "veto_reason": None,
    }
    result["effective_config"] = config.as_finding()
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
        if drafting_provider is not None:
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
                    if refresh is not None and monotonic_now >= next_refresh:
                        observation = await refresh(utc_now())
                        runtime.mark("registry_refresh")
                        print(
                            "board-butler: refresh "
                            + json.dumps(observation, sort_keys=True),
                            file=sys.stderr,
                        )
                        next_refresh = (
                            asyncio.get_running_loop().time() + args.refresh_seconds
                        )
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
