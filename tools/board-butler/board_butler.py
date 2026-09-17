#!/usr/bin/env python3
"""Shadow-only coordinator question drafter.

The butler listens on the same journal/seat resource subscriptions used by the
wait bridge.  It never answers a question or mutates a ticket; its only Central
write is a CAS-protected merge into ``coordinator_findings``.
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
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


STATE_KEY = "coordinator_findings"
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
MAX_FINDINGS = 50
MAX_STATE_CHARS = 4_800
QUESTION_EVENT = "coordinator_question_asked"
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


class AlreadyRunning(RuntimeError):
    """Raised before any board access when the singleton is already held."""


class IdentityConflict(RuntimeError):
    """Raised when the butler shares a principal with a worker/reviewer seat."""


class ButlerConfigError(ValueError):
    """Raised when coordinator_config cannot be interpreted safely."""


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
    drafting_model: str | None
    drafting_endpoint_ref: str | None
    drafting_key_ref: str | None
    drafting_extra_headers: dict[str, str]
    drafting_key_header: str
    drafting_key_prefix: str
    drafting_validation_path: str
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
            },
            "drafting": {
                "model": self.drafting_model,
                "endpoint_ref": self.drafting_endpoint_ref,
                "key_ref": self.drafting_key_ref,
                "extra_headers": dict(self.drafting_extra_headers),
                "key_header": self.drafting_key_header,
                "key_prefix": self.drafting_key_prefix,
                "validation_path": self.drafting_validation_path,
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
    return ProviderRuntime(
        endpoint=endpoint,
        model=model,
        credential=credential,
        extra_headers=dict(getattr(config, f"{task}_extra_headers")),
        key_header=getattr(config, f"{task}_key_header"),
        key_prefix=getattr(config, f"{task}_key_prefix"),
        validation_path=getattr(config, f"{task}_validation_path"),
    )


class SingletonLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "SingletonLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
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
        if "validation_path" in selected:
            validation_path = selected["validation_path"]
            if (
                not isinstance(validation_path, str)
                or not validation_path
                or len(validation_path) > 500
                or any(ord(character) < 0x20 for character in validation_path)
            ):
                raise ButlerConfigError(f"{path}.{task}.validation_path is invalid")
            result[task]["validation_path"] = validation_path
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
        },
        "drafting": {
            "model": None,
            "endpoint_ref": None,
            "key_ref": None,
            "extra_headers": {},
            "key_header": "Authorization",
            "key_prefix": "Bearer",
            "validation_path": "models",
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
        drafting_model=merged["drafting"]["model"],
        drafting_endpoint_ref=merged["drafting"]["endpoint_ref"],
        drafting_key_ref=merged["drafting"]["key_ref"],
        drafting_extra_headers=dict(merged["drafting"]["extra_headers"]),
        drafting_key_header=merged["drafting"]["key_header"],
        drafting_key_prefix=merged["drafting"]["key_prefix"],
        drafting_validation_path=merged["drafting"]["validation_path"],
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
            item.get("kind") in {"would_answer", "butler_queued", "butler_config_invalid"}
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


class CentralBackend:
    """Narrow Central adapter: push wait, evidence reads, findings CAS write."""

    def __init__(self, args: argparse.Namespace, token: str) -> None:
        self.args = args
        self.token = token
        self.client: Any = None
        self.identity: Any = None
        self._context: Any = None
        self.latest_seq = 0
        self.project_name: str | None = None

    async def __aenter__(self) -> "CentralBackend":
        from pursers_client import BoardClient

        self._context = BoardClient(
            self.args.url,
            self.token,
            self.args.home_board,
            agent_name=self.args.agent_name,
            role="coordinator",
            capabilities={"can_work": False, "can_review": False, "tier_max": 0, "max_parallel": 1},
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
        resolve_provider_runtime(config, "drafting", secret_root)
    except ButlerConfigError as exc:
        safe_config = resolve_config(
            {}, args, state, now, project_name=getattr(backend, "project_name", None)
        )
        finding = decorate_finding(
            config_invalid_finding(question, exc, now), safe_config, now
        )
        merged = merge_finding(state, finding, now)
        encoded = json.dumps(merged, sort_keys=True, separators=(",", ":"))
        if args.dry_run:
            print(json.dumps(finding, indent=2, sort_keys=True))
        else:
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
    finding = (
        rate_limit_finding(question, reason, now)
        if reason
        else await make_finding(
            question, backend, args.repo, args.integration_ref, now
        )
    )
    finding = decorate_finding(finding, config, now)
    merged = merge_finding(state, finding, now)
    encoded = json.dumps(merged, sort_keys=True, separators=(",", ":"))
    if args.dry_run:
        print(json.dumps(finding, indent=2, sort_keys=True))
    else:
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
        token = _read_token(args.token_path)
        async with backend_factory(args, token) as backend:
            cursor = load_cursor(args.cursor_file)
            if cursor is None:
                cursor = backend.latest_seq
            while True:
                timeout = float(args.wait_timeout) if args.once else None
                cursor, question = await backend.wait_for_question(cursor, timeout)
                if question is not None:
                    await process_question(backend, question, args, utc_now())
                # Printing a proposed draft is not durable processing. Keep
                # dry-run questions replayable by leaving the cursor alone.
                if not args.dry_run:
                    save_cursor(args.cursor_file, cursor)
                if args.once:
                    return
                if question is None:
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
