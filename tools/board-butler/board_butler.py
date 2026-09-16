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
import subprocess
import sys
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


STATE_KEY = "coordinator_findings"
CONFIG_KEY = "coordinator_config"
SCHEMA_VERSION = 1
DEFAULT_URL = "http://127.0.0.1:8766/mcp"
DEFAULT_AGENT_NAME = "board-butler-1"
DEFAULT_DRAFTS_PER_HOUR = 5
DEFAULT_DRAFTS_PER_TICKET = 2
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
        re.compile(r"\b(?:waiv(?:e|er)|bypass|override)\b.{0,80}\b(?:gate|check|failure|requirement)\b|\b(?:gate|check|failure|requirement)\b.{0,80}\b(?:waiv(?:e|er)|bypass|override)\b", re.I),
    ),
    PolicyRule(
        "scope-change",
        Outcome.ESCALATE,
        re.compile(r"\b(?:change|expand|reduce|amend|override)\b.{0,60}\bscope\b|\bout[- ]of[- ]scope\b", re.I),
    ),
    PolicyRule(
        "release-decision",
        Outcome.ESCALATE,
        re.compile(r"\b(?:release|publish|tag|ship|version bump|promote)\b", re.I),
    ),
    PolicyRule(
        "membership-or-registry",
        Outcome.ESCALATE,
        re.compile(r"\b(?:membership|invite|admit|retire seat|registry|register board|project registry|change role)\b", re.I),
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
    source: str
    detail: str
    answer: str


class EvidenceSource(Protocol):
    async def ticket_get(self, ticket_id: str) -> Mapping[str, Any]: ...
    async def board_status(self) -> Mapping[str, Any]: ...


class AlreadyRunning(RuntimeError):
    """Raised before any board access when the singleton is already held."""


class IdentityConflict(RuntimeError):
    """Raised when the butler shares a principal with a worker/reviewer seat."""


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
        source=f"git merge-base --is-ancestor {full_sha} {target}",
        detail=f"exit_code={check.returncode}",
        answer=f"{full_sha} {'is' if yes else 'is not'} an ancestor of {target}.",
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
                source=f"Central ticket_get({target}).annotations",
                detail=f"annotation_id={annotation_id}; found=false",
                answer=f"{annotation_id} is not present on {target}; it cannot be treated as coverage.",
            )
        text = str(annotation.get("text", annotation.get("message", "")))
        kind = str(annotation.get("kind", "unknown"))
        return Evidence(
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
        source=f"policy_table:{classification.rule}",
        detail=f"question_kind={kind}",
        answer=f"Would escalate: {classification.rule}.",
    )
    outcome = classification.outcome
    if outcome is Outcome.MECHANICAL:
        try:
            evidence = await evaluate_mechanical(
                classification, question, source, repo, integration_ref
            )
        except Exception as exc:
            outcome = Outcome.UNKNOWN
            evidence = Evidence(
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
        "message": evidence.answer,
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
    ticket_id: str,
    now: datetime,
    per_hour: int,
    per_ticket: int,
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
            item.get("kind") in {"would_answer", "butler_rate_limited"}
            and item.get("question_id") == finding.get("question_id")
        )
    ]
    rows.append(dict(finding))
    omitted = max(0, len(rows) - MAX_FINDINGS)
    result["findings"] = rows[-MAX_FINDINGS:]
    result["generated_at"] = now.isoformat()
    result["effective_mode"] = "shadow"
    truncation = dict(result.get("truncation", {}))
    truncation["findings"] = int(truncation.get("findings", 0) or 0) + omitted
    result["truncation"] = truncation
    result["board_butler"] = {
        "schema_version": SCHEMA_VERSION,
        "last_question_id": finding.get("question_id"),
        "last_verdict": finding.get("verdict"),
        "updated_at": now.isoformat(),
    }
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
            0,
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
        "kind": "butler_rate_limited",
        "level": "warn",
        "board_id": str(question.get("board_id", "unknown")),
        "ticket_id": str(question.get("ticket_id", "")),
        "question_id": str(question.get("question_id", "")),
        "message": f"Board butler draft cap hit: {reason}.",
        "evidence": f"source=board_butler_rate_limit; limit={reason}",
        "next_action": "Coordinator handles the question without a butler draft.",
        "mode": "shadow",
        "observed_at": now.isoformat(),
    }


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
    return cursor if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0 else None


def save_cursor(path: Path, cursor: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"schema_version": 1, "cursor": cursor}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def limits_from_config(
    config: Mapping[str, Any], args: argparse.Namespace
) -> tuple[int, int]:
    butler = config.get("board_butler", {})
    if not isinstance(butler, Mapping):
        butler = {}
    intake = config.get("intake", {})
    if not isinstance(intake, Mapping):
        intake = {}
    per_hour = butler.get(
        "drafts_per_hour", intake.get("rate_per_hour", args.drafts_per_hour)
    )
    per_ticket = butler.get("drafts_per_ticket", args.drafts_per_ticket)
    if not isinstance(per_hour, int) or isinstance(per_hour, bool) or not 1 <= per_hour <= 100:
        per_hour = args.drafts_per_hour
    if not isinstance(per_ticket, int) or isinstance(per_ticket, bool) or not 1 <= per_ticket <= 20:
        per_ticket = args.drafts_per_ticket
    return per_hour, per_ticket


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
            and item.get("kind") in {"would_answer", "butler_rate_limited"}
            and str(item.get("question_id", "")) == question_id
        ),
        None,
    )
    if existing is not None:
        return existing
    config = await backend.coordinator_config()
    per_hour, per_ticket = limits_from_config(config, args)
    reason = rate_limit_reason(
        state, str(question.get("ticket_id", "")), now, per_hour, per_ticket
    )
    finding = (
        rate_limit_finding(question, reason, now)
        if reason
        else await make_finding(
            question, backend, args.repo, args.integration_ref, now
        )
    )
    merged = merge_finding(state, finding, now)
    encoded = json.dumps(merged, sort_keys=True, separators=(",", ":"))
    if args.dry_run:
        print(json.dumps(finding, indent=2, sort_keys=True))
    else:
        await backend.write_findings(encoded, previous_value)
    return finding


async def run(
    args: argparse.Namespace,
    *,
    backend_factory: Any = CentralBackend,
) -> None:
    # The lock deliberately precedes token reads and backend construction.
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
                save_cursor(args.cursor_file, cursor)
                if args.once:
                    return


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
    parser.add_argument("--drafts-per-hour", type=int, default=DEFAULT_DRAFTS_PER_HOUR)
    parser.add_argument("--drafts-per-ticket", type=int, default=DEFAULT_DRAFTS_PER_TICKET)
    parser.add_argument("--wait-timeout", type=int, default=180)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    for name in ("token_path", "repo", "pid_file", "cursor_file"):
        value = getattr(args, name)
        if not value.is_absolute():
            parser.error(f"--{name.replace('_', '-')} must be absolute")
    if not args.repo.is_dir():
        parser.error("--repo must name an existing directory")
    if not 1 <= args.drafts_per_hour <= 100:
        parser.error("--drafts-per-hour must be between 1 and 100")
    if not 1 <= args.drafts_per_ticket <= 20:
        parser.error("--drafts-per-ticket must be between 1 and 20")
    if args.wait_timeout < 1:
        parser.error("--wait-timeout must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        asyncio.run(run(args))
    except AlreadyRunning as exc:
        print(f"board-butler: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
