#!/usr/bin/env python3
"""Report approved ticket content that has not reached the main branch."""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence


BRANCH_AND_COMMIT_RE = re.compile(
    r"(?im)^\s*branch_and_commit\s*:\s*"
    r"([A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)+)"
    r"\s*@\s*([0-9a-fA-F]{40})\s*$"
)
TICKET_STATUSES = (
    "open",
    "claimed",
    "in_progress",
    "creating_report",
    "submitted",
    "reviewing",
    "in_review",
    "needs_human",
    "closed",
    "rejected",
    "canceled",
    "terminated",
)
DEFAULT_THRESHOLD = 0.90
TICKET_BATCH_SIZE = 25


class AuditError(RuntimeError):
    """Raised when the audit cannot prove it inspected complete inputs."""


class TicketClient(Protocol):
    async def ticket_list(self, **arguments: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ApprovalResult:
    ticket_id: str
    approved_sha: str | None
    state: str
    title: str
    matched_lines: int | None = None
    added_lines: int | None = None

    @property
    def rendered_state(self) -> str:
        if self.matched_lines is None or self.added_lines is None:
            return self.state
        return f"{self.state} {self.matched_lines}/{self.added_lines}"


def _git(
    repo: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AuditError(
            f"git {' '.join(arguments)} failed ({completed.returncode}): {detail}"
        )
    return completed


def _chunks(values: Sequence[str], size: int = TICKET_BATCH_SIZE) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield list(values[offset : offset + size])


def _ticket_rows(result: Mapping[str, Any], *, label: str) -> list[dict[str, Any]]:
    raw = result.get("tickets")
    if not isinstance(raw, list):
        raise AuditError(f"{label}: board response omitted tickets")
    rows = [item for item in raw if isinstance(item, dict)]
    if len(rows) != len(raw):
        raise AuditError(f"{label}: board response contained a non-object ticket")
    count = result.get("count")
    total = result.get("total_matching")
    if count != len(rows) or not isinstance(total, int):
        raise AuditError(f"{label}: board response has invalid counts")
    if total != len(rows):
        raise AuditError(
            f"{label}: ticket page is incomplete ({len(rows)}/{total}); "
            "reduce the server-side page size before trusting this audit"
        )
    return rows


async def fetch_all_tickets(client: TicketClient) -> list[dict[str, Any]]:
    """Read every ticket using bounded status and ticket-id pages."""
    probe = await client.ticket_list(
        include_closed=True,
        include_archived=True,
        limit=1,
        view="summary",
    )
    total = probe.get("total_matching")
    if not isinstance(total, int):
        raise AuditError("all-ticket probe omitted total_matching")

    ticket_ids: list[str] = []
    seen: set[str] = set()
    for status in TICKET_STATUSES:
        result = await client.ticket_list(
            status=status,
            include_closed=True,
            include_archived=True,
            limit=500,
            view="summary",
        )
        for ticket in _ticket_rows(result, label=f"status {status}"):
            ticket_id = ticket.get("ticket_id")
            if not isinstance(ticket_id, str) or not ticket_id:
                raise AuditError(f"status {status}: ticket omitted ticket_id")
            if ticket_id in seen:
                raise AuditError(f"ticket {ticket_id} appeared in multiple status pages")
            seen.add(ticket_id)
            ticket_ids.append(ticket_id)
    if len(ticket_ids) != total:
        raise AuditError(
            f"status pages covered {len(ticket_ids)} of {total} tickets; "
            "the server may expose an unknown status"
        )

    tickets: list[dict[str, Any]] = []
    for chunk in _chunks(sorted(ticket_ids)):
        result = await client.ticket_list(
            ticket_ids=chunk,
            include_closed=True,
            include_archived=True,
            limit=len(chunk),
            view="full",
        )
        rows = _ticket_rows(result, label=f"ticket batch {chunk[0]}")
        returned = {str(row.get("ticket_id")) for row in rows}
        if returned != set(chunk):
            missing = sorted(set(chunk) - returned)
            extra = sorted(returned - set(chunk))
            raise AuditError(
                f"ticket batch mismatch: missing={missing} extra={extra}"
            )
        tickets.extend(rows)
    return tickets


def _last_submission_notes(ticket: Mapping[str, Any]) -> str | None:
    history = ticket.get("submission_history")
    if not isinstance(history, list) or not history:
        return None
    latest = history[-1]
    if not isinstance(latest, Mapping):
        return None
    notes = latest.get("notes")
    return notes if isinstance(notes, str) else None


def parse_approved_reference(ticket: Mapping[str, Any]) -> tuple[str, str] | None:
    notes = _last_submission_notes(ticket)
    if notes is None:
        return None
    matches = list(BRANCH_AND_COMMIT_RE.finditer(notes))
    if len(matches) != 1:
        return None
    branch, commit = matches[0].groups()
    return branch, commit.lower()


def _commit_exists(repo: Path, commit: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode == 0


def _is_ancestor(repo: Path, commit: str, main_ref: str) -> bool:
    return (
        _git(
            repo,
            "merge-base",
            "--is-ancestor",
            commit,
            main_ref,
            check=False,
        ).returncode
        == 0
    )


def _added_lines_by_path(repo: Path, base: str, commit: str) -> dict[str, list[str]]:
    names = _git(
        repo,
        "diff",
        "--no-renames",
        "--diff-filter=AM",
        "--name-only",
        "-z",
        base,
        commit,
        "--",
    ).stdout.split("\0")
    result: dict[str, list[str]] = {}
    for path in (item for item in names if item):
        patch = _git(
            repo,
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-renames",
            "--format=",
            "--unified=0",
            base,
            commit,
            "--",
            path,
        ).stdout
        added = [
            line[1:]
            for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++ ")
        ]
        if added:
            result[path] = added
    return result


def content_match_counts(
    repo: Path, commit: str, main_ref: str
) -> tuple[int, int]:
    base = _git(repo, "merge-base", commit, main_ref).stdout.strip()
    if not base:
        raise AuditError(f"no merge base between {commit} and {main_ref}")
    matched = 0
    total = 0
    for path, added in _added_lines_by_path(repo, base, commit).items():
        current = _git(repo, "show", f"{main_ref}:{path}", check=False)
        main_lines = set(current.stdout.splitlines()) if current.returncode == 0 else set()
        total += len(added)
        matched += sum(line in main_lines for line in added)
    return matched, total


def classify_approval(
    ticket: Mapping[str, Any],
    *,
    repo: Path,
    main_ref: str,
    threshold: float = DEFAULT_THRESHOLD,
) -> ApprovalResult:
    ticket_id = str(ticket.get("ticket_id") or "-")
    title = " ".join(str(ticket.get("title") or "").split())
    reference = parse_approved_reference(ticket)
    if reference is None:
        return ApprovalResult(ticket_id, None, "UNVERIFIABLE", title)
    _branch, commit = reference
    if not _commit_exists(repo, commit):
        return ApprovalResult(ticket_id, commit, "BRANCH_MISSING", title)
    if _is_ancestor(repo, commit, main_ref):
        return ApprovalResult(ticket_id, commit, "LANDED_ANCESTOR", title)
    matched, total = content_match_counts(repo, commit, main_ref)
    ratio = matched / total if total else 0.0
    state = "LANDED_CONTENT" if ratio >= threshold else "STRANDED"
    return ApprovalResult(ticket_id, commit, state, title, matched, total)


def audit_approvals(
    tickets: Sequence[Mapping[str, Any]],
    *,
    repo: Path,
    main_ref: str,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[ApprovalResult]:
    approved = [ticket for ticket in tickets if ticket.get("review_verdict") == "approve"]
    return sorted(
        (
            classify_approval(
                ticket,
                repo=repo,
                main_ref=main_ref,
                threshold=threshold,
            )
            for ticket in approved
        ),
        key=lambda result: result.ticket_id,
    )


def render_result(result: ApprovalResult) -> str:
    sha = result.approved_sha or "-"
    return f"{result.ticket_id}\t{sha}\t{result.rendered_state}\t{result.title}"


def read_token(path_value: str | None) -> str:
    if not path_value:
        raise AuditError(
            "--token-file, ONBOARD_CENTRAL_TOKEN_FILE, or ONBOARD_TOKEN_FILE "
            "is required"
        )
    path = Path(path_value)
    if not path.is_absolute() or not path.is_file():
        raise AuditError("token file must be an existing absolute path")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise AuditError("token file is empty")
    return token


def _capabilities(role: str) -> dict[str, Any]:
    return {
        "can_work": role == "worker",
        "can_review": role == "reviewer",
        "tier_max": 2,
        "max_parallel": 1,
    }


async def read_live_tickets(args: argparse.Namespace, token: str) -> list[dict[str, Any]]:
    import httpx2
    from pursers_client import BoardClient

    verify: bool | str = str(args.ca_file) if args.ca_file else True
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(30.0, read=None),
        verify=verify,
        trust_env=False,
    ) as http:
        async with BoardClient(
            args.central_url,
            token,
            args.board,
            agent_name=args.agent_name,
            role=args.role,
            capabilities=_capabilities(args.role),
            allow_takeover=True,
            http_client=http,
        ) as client:
            return await fetch_all_tickets(client)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--main-branch", default="main")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--central-url", default=os.environ.get("ONBOARD_CENTRAL_URL"))
    parser.add_argument(
        "--token-file",
        default=(
            os.environ.get("ONBOARD_CENTRAL_TOKEN_FILE")
            or os.environ.get("ONBOARD_TOKEN_FILE")
        ),
    )
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--board", default=os.environ.get("ONBOARD_BOARD_ID"))
    parser.add_argument("--agent-name", default=os.environ.get("ONBOARD_AGENT_NAME"))
    parser.add_argument(
        "--role",
        choices=("worker", "reviewer", "coordinator", "orchestrator"),
        default=os.environ.get("PURSERS_ROLE") or os.environ.get("ONBOARD_ROLE", "worker"),
    )
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    return parser


def run(args: argparse.Namespace) -> list[ApprovalResult]:
    if not args.central_url:
        raise AuditError("--central-url or ONBOARD_CENTRAL_URL is required")
    if not args.board:
        raise AuditError("--board or ONBOARD_BOARD_ID is required")
    if not args.agent_name:
        raise AuditError("--agent-name or ONBOARD_AGENT_NAME is required")
    if not 0.0 <= args.threshold <= 1.0:
        raise AuditError("--threshold must be between 0 and 1")
    if args.ca_file is not None and not args.ca_file.is_file():
        raise AuditError("--ca-file must be an existing file")

    repo = args.repo.resolve()
    if not args.no_fetch:
        _git(repo, "fetch", "--prune", args.remote)
    main_ref = f"refs/remotes/{args.remote}/{args.main_branch}"
    _git(repo, "rev-parse", "--verify", f"{main_ref}^{{commit}}")
    tickets = asyncio.run(read_live_tickets(args, read_token(args.token_file)))
    return audit_approvals(
        tickets,
        repo=repo,
        main_ref=main_ref,
        threshold=args.threshold,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        results = run(build_parser().parse_args(argv))
    except (AuditError, OSError, ValueError) as exc:
        print(f"stranded-approvals: {exc}", file=sys.stderr)
        return 2
    for result in results:
        print(render_result(result))
    return 1 if any(result.state == "STRANDED" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
