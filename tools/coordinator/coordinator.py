#!/usr/bin/env python3
"""Phase-1 fleet coordinator: bounded observation and reporting only.

The detector is deliberately separate from transport.  Its only live writes are
``coordinator_findings`` board state and daily/weekly digest memories.  It never
claims, assigns, reviews, merges, fetches, checks out, or changes project files.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


STATE_KEY = "coordinator_findings"
SCHEMA_VERSION = 1
DEFAULT_URL = "https://127.0.0.1:8766/mcp"
MAX_SNAPSHOT_ITEMS = 1_000
MAX_SNAPSHOT_BYTES = 300_000
MAX_FINDINGS = 50
MAX_FINDING_CHARS = 500
MAX_STATE_CHARS = 5_000
MAX_PRIVACY_COMMITS_PER_CYCLE = 1_000
COMMIT_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{7,64})(?![0-9a-fA-F])")
CLAIMED_STATES = frozenset({"claimed", "in_progress", "creating_report"})


@dataclass(frozen=True)
class Thresholds:
    stale_seconds: int = 300
    lease_warning_fraction: float = 0.80
    lease_grace_seconds: int = 600
    starved_seconds: int = 1_800
    critical_starved_seconds: int = 600
    repeat_abandon_count: int = 3
    repeat_abandon_window_seconds: int = 7 * 86_400


@dataclass(frozen=True)
class Project:
    name: str
    board_id: str
    work_dir: Path
    integration_ref: str = "main"
    public: bool = False


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
    if ticket.get("status") != "open" or ticket.get("claimed_by_agent_id"):
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
) -> Mapping[str, Any] | None:
    loads = _agent_loads(tickets)
    eligible = [
        agent
        for agent in agents
        if classify_agent(agent, now, thresholds) == "available"
        and agent.get("agent_name") != "coordinator-1"
        and agent.get("agent_id")
        and agent.get("membership_role") in {None, "member", "admin"}
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (
            loads[str(item["agent_id"])],
            str(item.get("last_activity_at", "")),
            str(item["agent_id"]),
        ),
    )


def _finding(kind: str, level: str, board_id: str, message: str, **evidence: Any) -> dict[str, Any]:
    result = {"kind": kind, "level": level, "board_id": board_id, "message": message}
    result.update({key: value for key, value in evidence.items() if value is not None})
    return result


def ticket_findings(
    board_id: str,
    snapshot: Mapping[str, Any],
    now: datetime,
    thresholds: Thresholds = Thresholds(),
) -> list[dict[str, Any]]:
    agents = [row for row in snapshot.get("agents", []) if isinstance(row, Mapping)]
    tickets = [row for row in snapshot.get("tickets", []) if isinstance(row, Mapping)]
    claim_ttl_s = snapshot.get("board", {}).get("claim_ttl_s", 900)
    if not isinstance(claim_ttl_s, int) or isinstance(claim_ttl_s, bool) or claim_ttl_s <= 0:
        claim_ttl_s = 900
    findings: list[dict[str, Any]] = []
    recent_drops: Counter[str] = Counter()

    for ticket in tickets:
        ticket_id = str(ticket.get("ticket_id", "unknown"))
        stage = starvation_stage(ticket, now, thresholds)
        if stage:
            assignee = choose_assignee(agents, tickets, now, thresholds) if stage == 2 else None
            findings.append(
                _finding(
                    "starved",
                    "warn",
                    board_id,
                    "An open ticket exceeded its claim-time threshold.",
                    ticket_id=ticket_id,
                    escalation_stage=stage,
                    would_assign_to_agent_id=assignee.get("agent_id") if assignee else None,
                    would_assign_to_agent_name=assignee.get("agent_name") if assignee else None,
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

        drop_time = ticket.get("last_abandoned_at") or ticket.get("last_unclaimed_at")
        drop_age = age_seconds(drop_time, now)
        drop_holder = ticket.get("last_abandoned_by") or ticket.get("last_claimed_by_agent_id")
        if (
            drop_holder
            and drop_age is not None
            and drop_age <= thresholds.repeat_abandon_window_seconds
        ):
            recent_drops[str(drop_holder)] += 1

    for holder, count in sorted(recent_drops.items()):
        if count >= thresholds.repeat_abandon_count:
            findings.append(
                _finding(
                    "repeat-abandoner",
                    "warn",
                    board_id,
                    "A seat reached the repeated dropped-claim threshold within the reporting window.",
                    holder_agent_id=holder,
                    dropped_claims=count,
                    window_days=7,
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
    if not COMMIT_RE.fullmatch(commit) or not integration_ref.strip() or integration_ref.startswith("-"):
        return None
    if _git(work_dir, ["cat-file", "-e", f"{commit}^{{commit}}"], runner=runner).returncode:
        return None
    if _git(work_dir, ["rev-parse", "--verify", f"{integration_ref}^{{commit}}"], runner=runner).returncode:
        return None
    result = _git(work_dir, ["merge-base", "--is-ancestor", commit, integration_ref], runner=runner)
    return True if result.returncode == 0 else False if result.returncode == 1 else None


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


def integration_findings(project: Project, tickets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for ticket in tickets:
        if ticket.get("status") != "closed" or _review_has_no_merge_label(ticket):
            continue
        target = ticket.get("target_url")
        if isinstance(target, str) and "/" in target and target.split("/", 1)[0] != project.name:
            continue
        commit = extract_commit_hash(ticket)
        if commit is None:
            continue
        ancestor = commit_is_ancestor(project.work_dir, commit, project.integration_ref)
        if ancestor is False:
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
        elif ancestor is None:
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
    message = str(result.get("message", ""))[:MAX_FINDING_CHARS]
    result["message"] = message
    while len(json.dumps(result, sort_keys=True, separators=(",", ":"))) > MAX_FINDING_CHARS:
        if message:
            message = message[:-1]
            result["message"] = message + ("…" if message else "")
            continue
        removable = next((key for key in reversed(result) if key not in {"kind", "level", "board_id", "message"}), None)
        if removable is None:
            break
        result.pop(removable)
    return result


def bound_findings_state(
    findings: Sequence[Mapping[str, Any]],
    generated_at: datetime,
    *,
    privacy_watermarks: Mapping[str, str] | None = None,
    max_findings: int = MAX_FINDINGS,
    max_chars: int = MAX_STATE_CHARS - 200,
) -> dict[str, Any]:
    normalized = [_bounded_finding(item) for item in findings]
    selected: list[dict[str, Any]] = []
    source_watermarks = dict(privacy_watermarks or {})
    base = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "findings": selected,
        "truncation": {
            "findings": len(normalized),
            "privacy_watermarks": len(source_watermarks),
        },
        "privacy_watermarks": {},
    }
    for key in sorted(source_watermarks):
        base["privacy_watermarks"][key] = source_watermarks[key]
        base["truncation"]["privacy_watermarks"] = (
            len(source_watermarks) - len(base["privacy_watermarks"])
        )
        if len(json.dumps(base, sort_keys=True, separators=(",", ":"))) > max_chars:
            base["privacy_watermarks"].pop(key)
            base["truncation"]["privacy_watermarks"] += 1
            break
    for item in normalized[:max_findings]:
        selected.append(item)
        base["truncation"]["findings"] = len(normalized) - len(selected)
        if len(json.dumps(base, sort_keys=True, separators=(",", ":"))) > max_chars:
            selected.pop()
            base["truncation"]["findings"] = len(normalized) - len(selected)
            break
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

    ALLOWED = frozenset({"board_state_get", "board_snapshot"})

    def __init__(self, url: str, token: str):
        self.url = url
        self._token = token
        self._stack: AsyncExitStack | None = None
        self._client: Any = None
        self._decode: Callable[[Any], dict[str, Any]] | None = None

    async def __aenter__(self) -> "RawReader":
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from pursers_client.client import BoardClient, httpx2

        self._decode = BoardClient._decode
        self._stack = AsyncExitStack()
        http = await self._stack.enter_async_context(
            httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=httpx2.Timeout(10.0, read=30.0),
                trust_env=False,
            )
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
        snapshots[board_id] = await reader.call(
            "board_snapshot", board_id, limit=MAX_SNAPSHOT_ITEMS, max_bytes=MAX_SNAPSHOT_BYTES
        )
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
) -> dict[str, dict[str, Any]]:
    findings_by_board: dict[str, list[dict[str, Any]]] = {}
    watermarks_by_board: dict[str, dict[str, str]] = {}
    for board_id, snapshot in snapshots.items():
        findings_by_board[board_id] = ticket_findings(board_id, snapshot, now)
        prior_marks = previous.get(board_id, {}).get("privacy_watermarks", {})
        watermarks_by_board[board_id] = dict(prior_marks) if isinstance(prior_marks, Mapping) else {}
    for project in projects:
        tickets = [row for row in snapshots[project.board_id].get("tickets", []) if isinstance(row, Mapping)]
        findings_by_board[project.board_id].extend(integration_findings(project, tickets))
        prior = watermarks_by_board[project.board_id].get(project.name)
        privacy, watermark = privacy_findings(project, terms, prior)
        findings_by_board[project.board_id].extend(privacy)
        if watermark:
            watermarks_by_board[project.board_id][project.name] = watermark

    # Fleet fairness order: critical first, then oldest-open-first across boards.
    ranks: list[tuple[int, datetime, str, str]] = []
    for board_id, snapshot in snapshots.items():
        for ticket in snapshot.get("tickets", []):
            if isinstance(ticket, Mapping) and starvation_stage(ticket, now, Thresholds()):
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
        )
        for board_id, findings in findings_by_board.items()
    }


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

    # Publish non-home findings first.  The home state, which carries digest
    # markers, is written only after its corresponding memories succeed.
    for board_id, state in states.items():
        if board_id == home_board:
            continue
        async with BoardClient(url, token, board_id, agent_name=agent_name) as client:
            await client.board_state_update(STATE_KEY, json.dumps(state, sort_keys=True, separators=(",", ":")))
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
    if home_board in states:
        async with BoardClient(url, token, home_board, agent_name=agent_name) as client:
            await client.board_state_update(
                STATE_KEY,
                json.dumps(states[home_board], sort_keys=True, separators=(",", ":")),
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
    parser = argparse.ArgumentParser(description="Run the phase-1 fleet coordinator")
    parser.add_argument("--url", default=os.environ.get("ONBOARD_CENTRAL_URL", DEFAULT_URL))
    parser.add_argument("--token-path", default=os.environ.get("PURSERS_COORDINATOR_TOKEN_PATH") or os.environ.get("ONBOARD_TOKEN_FILE"))
    parser.add_argument("--home-board", default=os.environ.get("ONBOARD_BOARD_ID", "pursers"))
    parser.add_argument("--agent-name", default="coordinator-1")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.token_path:
        parser.error("--token-path or PURSERS_COORDINATOR_TOKEN_PATH is required")
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be positive")
    return args


async def run(args: argparse.Namespace) -> None:
    token = _read_token(args.token_path)
    terms_path = os.environ.get("PURSERS_PRIVACY_TERMS")
    while True:
        now = utc_now()
        async with RawReader(args.url, token) as reader:
            projects, snapshots, previous = await read_cycle(reader, args.home_board)
        terms = load_privacy_terms(terms_path, [project.work_dir for project in projects])
        states = analyze_cycle(projects, snapshots, previous, terms, now)
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
