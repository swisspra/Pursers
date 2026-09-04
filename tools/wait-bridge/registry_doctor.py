#!/usr/bin/env python3
"""Run a read-only health check across the Pursers project registry."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pursers_client import BoardClient

from registry_admin import CENTRAL_URL_DEFAULT, HOME_BOARD_ID


REGISTRY_KEY = "project_registry"
COORDINATOR_KEY = "coordinator_findings"
DEFAULT_REVIEW_BACKLOG_SECONDS = 1_800
DEFAULT_STALE_SEAT_SECONDS = 300
DEFAULT_COORDINATOR_STALE_SECONDS = 300
SNAPSHOT_LIMIT = 1_000
SNAPSHOT_MAX_BYTES = 300_000
MAX_STATS_BYTES = 1_000_000
MAX_DETAIL_CHARS = 300
MAX_LIST_ITEMS = 20
LEVELS = {"PASS": 0, "WARN": 1, "FAIL": 2}
ACTIVE_CLAIM_STATES = frozenset({"claimed", "in_progress", "creating_report"})


class DoctorError(RuntimeError):
    """An expected registry-doctor setup or validation failure."""


class DoctorBackend(Protocol):
    async def board_status(self, board_id: str) -> dict[str, Any]: ...

    async def board_snapshot(self, board_id: str) -> dict[str, Any]: ...

    async def board_state_get(
        self, board_id: str, key: str
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "check": self.name, "detail": self.detail}


class LiveBackend:
    """Read-only BoardClient adapter; it intentionally exposes no write method."""

    def __init__(
        self,
        central_url: str,
        token: str,
        agent_name: str,
        client_factory: Callable[..., Any] = BoardClient,
    ) -> None:
        self.central_url = central_url
        self._token = token
        self.agent_name = agent_name
        self.client_factory = client_factory

    def _client(self, board_id: str) -> Any:
        return self.client_factory(
            self.central_url,
            self._token,
            board_id,
            agent_name=self.agent_name,
        )

    async def board_status(self, board_id: str) -> dict[str, Any]:
        async with self._client(board_id) as client:
            return await client.board_status()

    async def board_snapshot(self, board_id: str) -> dict[str, Any]:
        async with self._client(board_id) as client:
            return await client.board_snapshot(
                limit=SNAPSHOT_LIMIT,
                max_bytes=SNAPSHOT_MAX_BYTES,
            )

    async def board_state_get(self, board_id: str, key: str) -> dict[str, Any]:
        async with self._client(board_id) as client:
            return await client.board_state_get(key)


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


def _age(value: Any, now: datetime) -> float | None:
    parsed = parse_time(value)
    return None if parsed is None else max(0.0, (now - parsed).total_seconds())


def _bounded_items(values: Sequence[str]) -> str:
    unique = sorted(set(values))
    shown = unique[:MAX_LIST_ITEMS]
    rendered = ", ".join(shown)
    omitted = len(unique) - len(shown)
    return rendered + (f", +{omitted} more" if omitted else "")


def _safe_detail(value: Any, token: str) -> str:
    text = str(value).replace(token, "[REDACTED]") if token else str(value)
    text = " ".join(text.split())
    return text[:MAX_DETAIL_CHARS]


def _add(
    checks: list[Check], status: str, name: str, detail: Any, token: str
) -> None:
    if status not in LEVELS:
        raise ValueError(f"unknown doctor status {status!r}")
    checks.append(Check(status, name, _safe_detail(detail, token)))


def _state_value(result: Any, label: str) -> Any:
    if not isinstance(result, Mapping):
        raise DoctorError(f"{label} returned a non-object response")
    state = result.get("state")
    if not isinstance(state, Mapping):
        raise DoctorError(f"{label} state entry is missing")
    value = state.get("value")
    if not isinstance(value, (str, Mapping)):
        raise DoctorError(f"{label} state value is malformed")
    if isinstance(value, Mapping):
        return dict(value)
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise DoctorError(f"{label} state value is not valid JSON") from exc


def parse_registry(result: Any) -> dict[str, Any]:
    document = _state_value(result, REGISTRY_KEY)
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise DoctorError("project_registry schema_version must be 1")
    projects = document.get("projects")
    if not isinstance(projects, dict):
        raise DoctorError("project_registry projects must be an object")
    normalized: dict[str, Any] = {"schema_version": 1, "projects": {}}
    required = {"board_id", "work_dir", "status"}
    optional = {"integration_ref", "git_repo", "public"}
    for name, raw in projects.items():
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise DoctorError("project_registry project names must be trimmed strings")
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise DoctorError(f"project {name!r} routing is incomplete")
        if set(raw) - required - optional:
            raise DoctorError(f"project {name!r} contains unsupported fields")
        board_id = raw.get("board_id")
        work_dir = raw.get("work_dir")
        status = raw.get("status")
        integration_ref = raw.get("integration_ref", "main")
        if not all(
            isinstance(item, str) and item.strip() and item == item.strip()
            for item in (board_id, work_dir, integration_ref)
        ):
            raise DoctorError(f"project {name!r} routing values are invalid")
        if status not in {"active", "paused"}:
            raise DoctorError(f"project {name!r} status must be active or paused")
        if not os.path.isabs(work_dir):
            raise DoctorError(f"project {name!r} work_dir must be absolute")
        if "git_repo" in raw and type(raw["git_repo"]) is not bool:
            raise DoctorError(f"project {name!r} git_repo must be boolean")
        normalized["projects"][name] = dict(raw)
        normalized["projects"][name]["integration_ref"] = integration_ref
    return normalized


GitRunner = Callable[[Path, Sequence[str]], subprocess.CompletedProcess[str]]


def _run_git(path: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def check_project(
    name: str,
    entry: Mapping[str, Any],
    checks: list[Check],
    token: str,
    git_runner: GitRunner,
) -> None:
    path = Path(str(entry["work_dir"]))
    if not path.is_dir():
        _add(checks, "FAIL", f"project:{name}", f"work_dir missing: {path}", token)
        return
    if entry.get("git_repo") is False:
        _add(
            checks,
            "PASS",
            f"project:{name}",
            f"work_dir exists; explicitly marked non-git: {path}",
            token,
        )
        return
    try:
        inside = git_runner(path, ["rev-parse", "--is-inside-work-tree"])
    except (OSError, subprocess.SubprocessError) as exc:
        _add(
            checks,
            "FAIL",
            f"project:{name}",
            f"git probe failed ({type(exc).__name__})",
            token,
        )
        return
    if inside.returncode or inside.stdout.strip() != "true":
        _add(checks, "FAIL", f"project:{name}", "work_dir is not a git repo", token)
        return
    integration_ref = str(entry.get("integration_ref", "main"))
    if integration_ref.startswith("-") or any(
        ord(character) < 0x20 for character in integration_ref
    ):
        _add(checks, "FAIL", f"project:{name}", "integration_ref is unsafe", token)
        return
    try:
        resolved = git_runner(
            path, ["rev-parse", "--verify", f"{integration_ref}^{{commit}}"]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _add(
            checks,
            "FAIL",
            f"project:{name}",
            f"integration_ref probe failed ({type(exc).__name__})",
            token,
        )
        return
    if resolved.returncode:
        _add(
            checks,
            "FAIL",
            f"project:{name}",
            f"integration_ref {integration_ref!r} is not resolvable",
            token,
        )
        return
    _add(
        checks,
        "PASS",
        f"project:{name}",
        f"git repo at {path}; integration_ref={integration_ref}",
        token,
    )


def _snapshot_omissions(snapshot: Mapping[str, Any]) -> dict[str, int]:
    raw = snapshot.get("omitted_counts", {})
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): max(0, int(value))
        for key, value in raw.items()
        if isinstance(value, int) and value > 0
    }


def _seat_check(
    snapshots: Mapping[str, Mapping[str, Any]],
    now: datetime,
    stale_seconds: int,
    checks: list[Check],
    token: str,
) -> None:
    principals_by_name: dict[str, set[str]] = defaultdict(set)
    stale: list[str] = []
    count = 0
    for board_id, snapshot in snapshots.items():
        agents = snapshot.get("agents", [])
        for agent in agents if isinstance(agents, list) else []:
            if not isinstance(agent, Mapping):
                continue
            name = agent.get("agent_name")
            principal = agent.get("principal_id")
            if not isinstance(name, str) or not name:
                continue
            count += 1
            if isinstance(principal, str) and principal:
                principals_by_name[name].add(principal)
            age = _age(agent.get("last_activity_at") or agent.get("last_seen"), now)
            if agent.get("status") == "stale" or (
                age is not None and age > stale_seconds
            ):
                stale.append(f"{board_id}/{name}")
    duplicates = [
        name for name, principals in principals_by_name.items() if len(principals) > 1
    ]
    issues: list[str] = []
    incomplete = [
        board_id
        for board_id, snapshot in snapshots.items()
        if _snapshot_omissions(snapshot).get("agents", 0)
    ]
    if incomplete:
        issues.append(f"agent scan incomplete: {_bounded_items(incomplete)}")
    if duplicates:
        issues.append(f"duplicate names across principals: {_bounded_items(duplicates)}")
    if stale:
        issues.append(f"stale seats: {_bounded_items(stale)}")
    if issues:
        _add(checks, "WARN", "seats", "; ".join(issues), token)
    else:
        _add(checks, "PASS", "seats", f"{count} observed seats; pool grouping sane", token)


def _ticket_checks(
    snapshots: Mapping[str, Mapping[str, Any]],
    now: datetime,
    review_backlog_seconds: int,
    checks: list[Check],
    token: str,
) -> None:
    expired: list[str] = []
    backlog: list[str] = []
    incomplete = [
        board_id
        for board_id, snapshot in snapshots.items()
        if _snapshot_omissions(snapshot).get("tickets", 0)
    ]
    for board_id, snapshot in snapshots.items():
        tickets = snapshot.get("tickets", [])
        for ticket in tickets if isinstance(tickets, list) else []:
            if not isinstance(ticket, Mapping):
                continue
            ticket_id = str(ticket.get("ticket_id", "unknown"))
            status = ticket.get("status")
            if status in ACTIVE_CLAIM_STATES:
                expiry = parse_time(
                    ticket.get("lease_expires_at")
                    or ticket.get("last_lease_expires_at")
                )
                if expiry is not None and expiry < now:
                    expired.append(f"{board_id}/{ticket_id}")
            if status == "submitted":
                age = _age(ticket.get("submitted_at"), now)
                if age is not None and age > review_backlog_seconds:
                    backlog.append(f"{board_id}/{ticket_id} age={int(age)}s")
    if expired or incomplete:
        details = []
        if expired:
            details.append(f"past-expiry active claims: {_bounded_items(expired)}")
        else:
            details.append("no past-expiry active claims observed")
        if incomplete:
            details.append(f"ticket scan incomplete: {_bounded_items(incomplete)}")
        _add(
            checks,
            "WARN",
            "claims",
            "; ".join(details),
            token,
        )
    else:
        _add(checks, "PASS", "claims", "no past-expiry active claims", token)
    if backlog or incomplete:
        details = []
        if backlog:
            details.append(
                f"submitted>{review_backlog_seconds}s: {_bounded_items(backlog)}"
            )
        else:
            details.append(
                f"no submitted tickets older than {review_backlog_seconds}s observed"
            )
        if incomplete:
            details.append(f"ticket scan incomplete: {_bounded_items(incomplete)}")
        _add(
            checks,
            "WARN",
            "review-backlog",
            "; ".join(details),
            token,
        )
    else:
        _add(
            checks,
            "PASS",
            "review-backlog",
            f"no submitted tickets older than {review_backlog_seconds}s",
            token,
        )


async def _coordinator_check(
    backend: DoctorBackend,
    home_board: str,
    now: datetime,
    stale_seconds: int,
    checks: list[Check],
    token: str,
) -> None:
    try:
        document = _state_value(
            await backend.board_state_get(home_board, COORDINATOR_KEY),
            COORDINATOR_KEY,
        )
        if not isinstance(document, Mapping):
            raise DoctorError("coordinator_findings must be a JSON object")
        generated_at = document.get("generated_at")
        age = _age(generated_at, now)
        if age is None:
            raise DoctorError("coordinator_findings generated_at is missing")
        if age > stale_seconds:
            _add(
                checks,
                "WARN",
                "coordinator",
                f"coordinator may be down; generated_at age={int(age)}s",
                token,
            )
        else:
            _add(
                checks,
                "PASS",
                "coordinator",
                f"coordinator_findings age={int(age)}s",
                token,
            )
    except Exception as exc:
        _add(
            checks,
            "WARN",
            "coordinator",
            f"coordinator liveness unavailable ({type(exc).__name__})",
            token,
        )


def _bridge_stats_check(
    path: Path, now: datetime, checks: list[Check], token: str
) -> None:
    try:
        if not path.is_file():
            raise DoctorError("stats file is missing")
        if path.stat().st_size > MAX_STATS_BYTES:
            raise DoctorError("stats file exceeds the read bound")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema_version") not in {
            1,
            2,
            3,
        }:
            raise DoctorError("stats schema is invalid")
        days = document.get("days")
        if not isinstance(days, dict):
            raise DoctorError("stats days are missing")
        today = now.date().isoformat()
        if today not in days:
            _add(
                checks,
                "WARN",
                "bridge-stats",
                f"no bridge stats for today ({today})",
                token,
            )
            return
        _add(checks, "PASS", "bridge-stats", f"fresh for {today}: {path}", token)
    except Exception as exc:
        _add(
            checks,
            "WARN",
            "bridge-stats",
            f"unavailable ({type(exc).__name__}): {path}",
            token,
        )


def worst_status(checks: Sequence[Check]) -> str:
    return max(checks, key=lambda item: LEVELS[item.status]).status if checks else "FAIL"


def report_document(checks: Sequence[Check], now: datetime) -> dict[str, Any]:
    overall = worst_status(checks)
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "overall": overall,
        "exit_code": LEVELS[overall],
        "checks": [item.as_dict() for item in checks],
    }


async def evaluate(
    backend: DoctorBackend,
    *,
    home_board: str,
    token: str,
    now: datetime,
    review_backlog_seconds: int = DEFAULT_REVIEW_BACKLOG_SECONDS,
    stale_seat_seconds: int = DEFAULT_STALE_SEAT_SECONDS,
    coordinator_stale_seconds: int = DEFAULT_COORDINATOR_STALE_SECONDS,
    stats_path: Path | None = None,
    git_runner: GitRunner = _run_git,
) -> dict[str, Any]:
    checks: list[Check] = []
    home_status: dict[str, Any] | None = None
    try:
        home_status = await backend.board_status(home_board)
        if isinstance(home_status, Mapping) and home_status.get("ok") is False:
            raise DoctorError("home board rejected board_status")
        _add(checks, "PASS", "central", f"reachable; home_board={home_board}", token)
    except Exception as exc:
        _add(
            checks,
            "FAIL",
            "central",
            f"reachability/auth failed ({type(exc).__name__})",
            token,
        )

    registry: dict[str, Any] | None = None
    try:
        registry = parse_registry(
            await backend.board_state_get(home_board, REGISTRY_KEY)
        )
        active_count = sum(
            entry.get("status") == "active"
            for entry in registry["projects"].values()
        )
        _add(
            checks,
            "PASS",
            "registry",
            f"schema v1 parsed; active_projects={active_count}",
            token,
        )
    except Exception as exc:
        _add(
            checks,
            "FAIL",
            "registry",
            f"parse/read failed ({type(exc).__name__})",
            token,
        )

    snapshots: dict[str, Mapping[str, Any]] = {}
    if registry is not None:
        active = {
            name: entry
            for name, entry in registry["projects"].items()
            if entry.get("status") == "active"
        }
        for name, entry in sorted(active.items()):
            check_project(name, entry, checks, token, git_runner)
        boards = [home_board]
        for entry in active.values():
            board_id = str(entry["board_id"])
            if board_id not in boards:
                boards.append(board_id)
        for board_id in boards:
            try:
                status = (
                    home_status
                    if board_id == home_board and home_status is not None
                    else await backend.board_status(board_id)
                )
                if isinstance(status, Mapping) and status.get("ok") is False:
                    raise DoctorError("board_status returned ok=false")
                _add(checks, "PASS", f"board:{board_id}", "exists and accessible", token)
            except Exception as exc:
                _add(
                    checks,
                    "FAIL",
                    f"board:{board_id}",
                    f"access failed ({type(exc).__name__})",
                    token,
                )
            try:
                snapshot = await backend.board_snapshot(board_id)
                if not isinstance(snapshot, Mapping):
                    raise DoctorError("snapshot returned a non-object response")
                omissions = _snapshot_omissions(snapshot)
                if snapshot.get("truncated") is True or omissions:
                    detail = (
                        ", ".join(f"{key}={value}" for key, value in sorted(omissions.items()))
                        or "truncated=true; omitted counts absent"
                    )
                    _add(checks, "WARN", f"snapshot:{board_id}", detail, token)
                else:
                    _add(
                        checks,
                        "PASS",
                        f"snapshot:{board_id}",
                        f"responded within limit={SNAPSHOT_LIMIT}, max_bytes={SNAPSHOT_MAX_BYTES}",
                        token,
                    )
                snapshots[board_id] = snapshot
            except Exception as exc:
                _add(
                    checks,
                    "FAIL",
                    f"snapshot:{board_id}",
                    f"bounded read failed ({type(exc).__name__})",
                    token,
                )
        _seat_check(snapshots, now, stale_seat_seconds, checks, token)
        _ticket_checks(
            snapshots,
            now,
            review_backlog_seconds,
            checks,
            token,
        )
    else:
        for name in ("seats", "claims", "review-backlog"):
            _add(checks, "FAIL", name, "registry unavailable; check skipped", token)

    await _coordinator_check(
        backend,
        home_board,
        now,
        coordinator_stale_seconds,
        checks,
        token,
    )
    selected_stats = stats_path or Path(__file__).resolve().with_name("bridge-stats.json")
    _bridge_stats_check(selected_stats, now, checks, token)
    return report_document(checks, now)


def render_human(report: Mapping[str, Any]) -> str:
    rows = report.get("checks", [])
    width = max([len("CHECK"), *(len(str(row.get("check", ""))) for row in rows)])
    lines = [f"{'STATUS':<6}  {'CHECK':<{width}}  DETAIL"]
    for row in rows:
        lines.append(
            f"{str(row.get('status', '')):<6}  "
            f"{str(row.get('check', '')):<{width}}  "
            f"{row.get('detail', '')}"
        )
    lines.append(f"OVERALL {report.get('overall')} (exit {report.get('exit_code')})")
    return "\n".join(lines)


def _read_token(path_value: str | None) -> str:
    if not path_value:
        raise DoctorError(
            "--token-path, PURSERS_DOCTOR_TOKEN_PATH, or ONBOARD_TOKEN_FILE is required"
        )
    path = Path(path_value)
    if not path.is_absolute() or not path.is_file():
        raise DoctorError("token path must name an existing absolute file")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise DoctorError("token file is empty")
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--central-url",
        default=os.environ.get("ONBOARD_CENTRAL_URL", CENTRAL_URL_DEFAULT),
    )
    parser.add_argument(
        "--home-board",
        default=os.environ.get("ONBOARD_BOARD_ID", HOME_BOARD_ID),
    )
    parser.add_argument(
        "--agent-name",
        default=os.environ.get("ONBOARD_AGENT_NAME", "registry-doctor"),
    )
    parser.add_argument(
        "--token-path",
        default=(
            os.environ.get("PURSERS_DOCTOR_TOKEN_PATH")
            or os.environ.get("ONBOARD_TOKEN_FILE")
        ),
    )
    parser.add_argument(
        "--review-backlog-seconds",
        type=int,
        default=DEFAULT_REVIEW_BACKLOG_SECONDS,
    )
    parser.add_argument(
        "--stale-seat-seconds",
        type=int,
        default=DEFAULT_STALE_SEAT_SECONDS,
    )
    parser.add_argument(
        "--coordinator-stale-seconds",
        type=int,
        default=DEFAULT_COORDINATOR_STALE_SECONDS,
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if min(
        args.review_backlog_seconds,
        args.stale_seat_seconds,
        args.coordinator_stale_seconds,
    ) <= 0:
        raise DoctorError("doctor thresholds must be positive")
    token = _read_token(args.token_path)
    backend = LiveBackend(args.central_url, token, args.agent_name)
    configured_stats = os.environ.get("PURSERS_BRIDGE_STATS", "").strip()
    stats_path = (
        Path(configured_stats).expanduser().resolve()
        if configured_stats
        else Path(__file__).resolve().with_name("bridge-stats.json")
    )
    return await evaluate(
        backend,
        home_board=args.home_board,
        token=token,
        now=utc_now(),
        review_backlog_seconds=args.review_backlog_seconds,
        stale_seat_seconds=args.stale_seat_seconds,
        coordinator_stale_seconds=args.coordinator_stale_seconds,
        stats_path=stats_path,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        print(
            f"ERROR: registry doctor could not start ({type(exc).__name__})",
            file=sys.stderr,
        )
        return LEVELS["FAIL"]
    if args.json_output:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(render_human(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
