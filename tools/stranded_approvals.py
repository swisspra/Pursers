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
TICKET_PAGE_LIMIT = 500
TICKET_MAX_PAGES = 100
DEFAULT_AUDIT_AGENT_NAME = "stranded-approvals-audit"
DEFAULT_AUDIT_AGENT_PLATFORM = "stranded-approvals"
DEFAULT_AUDIT_TASK_FOCUS = "read-only stranded approval audit"


class AuditError(RuntimeError):
    """Raised when the audit cannot prove it inspected complete inputs."""

    def __init__(self, message: str, *, original_error_class: str | None = None, original_error_code: str | None = None) -> None:
        super().__init__(message)
        self.original_error_class = original_error_class
        self.original_error_code = original_error_code


class TicketClient(Protocol):
    async def ticket_list(self, **arguments: Any) -> dict[str, Any]: ...

    async def board_status(self, **arguments: Any) -> dict[str, Any]: ...

    async def ticket_get(self, ticket_id: str, **arguments: Any) -> dict[str, Any]: ...


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


def _ticket_page(
    result: Mapping[str, Any], *, label: str
) -> tuple[list[dict[str, Any]], int]:
    raw = result.get("tickets")
    if not isinstance(raw, list):
        raise AuditError(f"{label}: board response omitted tickets")
    rows = [item for item in raw if isinstance(item, dict)]
    if len(rows) != len(raw):
        raise AuditError(f"{label}: board response contained a non-object ticket")
    count = result.get("count")
    total = result.get("total_matching")
    if type(count) is not int or count != len(rows) or type(total) is not int or total < len(rows):
        raise AuditError(f"{label}: board response has invalid counts")
    return rows, total


def _ticket_rows(result: Mapping[str, Any], *, label: str) -> list[dict[str, Any]]:
    rows, total = _ticket_page(result, label=label)
    if total != len(rows):
        raise AuditError(
            f"{label}: ticket page is incomplete ({len(rows)}/{total}); "
            "reduce the server-side page size before trusting this audit"
        )
    return rows


def _status_assignees(result: Mapping[str, Any]) -> list[str]:
    agents = result.get("agents")
    if not isinstance(agents, list):
        raise AuditError("board status omitted agents for oversized status split")
    selectors: set[str] = set()
    for agent in agents:
        if not isinstance(agent, Mapping):
            raise AuditError("board status contained a non-object agent")
        agent_id = agent.get("agent_id")
        agent_name = agent.get("agent_name")
        if isinstance(agent_id, str) and agent_id:
            selectors.add(agent_id)
        elif isinstance(agent_name, str) and agent_name:
            selectors.add(agent_name)
    return sorted(selectors)


async def _complete_ticket_pages(
    client: TicketClient,
    *,
    label: str,
    first: Mapping[str, Any] | None = None,
    **arguments: Any,
) -> list[dict[str, Any]]:
    """Traverse bounded live pages without treating deduplication as completeness."""
    page = first if first is not None else await client.ticket_list(**arguments)
    total: int | None = None
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    for page_number in range(1, TICKET_MAX_PAGES + 1):
        current, matching = _ticket_page(page, label=label)
        if total is None:
            total = matching
        elif matching != total:
            raise AuditError(f"{label}: total_matching changed during traversal")
        for row in current:
            ticket_id = row.get("ticket_id")
            if not isinstance(ticket_id, str) or not ticket_id:
                raise AuditError(f"{label}: ticket omitted ticket_id")
            if ticket_id in seen_ids:
                raise AuditError(f"{label}: duplicate ticket {ticket_id}")
            seen_ids.add(ticket_id)
            rows.append(row)
        cursor = page.get("next_cursor")
        if cursor is not None and (
            not isinstance(cursor, str) or not cursor or cursor in seen_cursors
        ):
            raise AuditError(f"{label}: invalid cursor sequence")
        if "has_more" in page and (
            type(page["has_more"]) is not bool
            or page["has_more"] != (cursor is not None)
        ):
            raise AuditError(f"{label}: inconsistent continuation metadata")
        if len(rows) > total or (cursor is not None and len(rows) >= total):
            raise AuditError(f"{label}: continuation exceeds total_matching")
        if cursor is None:
            if len(rows) != total:
                raise AuditError(f"{label}: ticket pages covered {len(rows)} of {total} tickets")
            return rows
        if not current:
            raise AuditError(f"{label}: empty continuation page")
        if page_number == TICKET_MAX_PAGES:
            raise AuditError(f"{label}: ticket traversal page limit exceeded")
        seen_cursors.add(cursor)
        page = await client.ticket_list(**arguments, cursor=cursor)
    raise AuditError(f"{label}: ticket traversal page limit exceeded")


async def _complete_status_page(
    client: TicketClient, status: str
) -> list[dict[str, Any]]:
    arguments = dict(
        status=status,
        include_closed=True,
        include_archived=True,
        limit=TICKET_PAGE_LIMIT,
        view="summary",
    )
    result = await client.ticket_list(**arguments)
    rows, total = _ticket_page(result, label=f"status {status}")
    if "next_cursor" in result or total == len(rows):
        return await _complete_ticket_pages(
            client, label=f"status {status}", first=result, **arguments
        )

    # Older servers have no cursor: retain the independently counted, fail-closed
    # assignee split. An incomplete full-ID page still cannot be trusted.
    status_result = await client.board_status(include_retired=True)
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        ticket_id = row.get("ticket_id")
        if not isinstance(ticket_id, str) or not ticket_id:
            raise AuditError(f"status {status}: ticket omitted ticket_id")
        by_id[ticket_id] = row
    for assignee in _status_assignees(status_result):
        assigned = await client.ticket_list(
            status=status,
            assigned_to=assignee,
            include_closed=True,
            include_archived=True,
            limit=TICKET_PAGE_LIMIT,
            view="summary",
        )
        for row in _ticket_rows(
            assigned, label=f"status {status} assignee {assignee}"
        ):
            ticket_id = row.get("ticket_id")
            if not isinstance(ticket_id, str) or not ticket_id:
                raise AuditError(
                    f"status {status} assignee {assignee}: ticket omitted ticket_id"
                )
            by_id[ticket_id] = row
    if len(by_id) != total:
        raise AuditError(
            f"status {status}: split pages covered {len(by_id)} of {total} tickets"
        )
    return [by_id[ticket_id] for ticket_id in sorted(by_id)]


def _approval_metadata_ambiguous(ticket: Mapping[str, Any]) -> bool:
    return ticket.get("review_verdict") is None and (
        bool(ticket.get("archived")) or ticket.get("status") == "closed"
    )


def _approval_sections_omitted(ticket: Mapping[str, Any]) -> bool:
    omitted = ticket.get("omitted_sections", [])
    return bool(ticket.get("oversized_for_list")) or not isinstance(omitted, list) or (
        bool(set(str(item) for item in omitted) & {
            "submission_history", "review_history", "review_verdict"
        })
    )


async def _complete_approval_input(
    client: TicketClient, ticket: dict[str, Any]
) -> dict[str, Any]:
    ticket_id = ticket["ticket_id"]
    if ticket.get("oversized_for_list") or ticket.get("omitted_sections") or _approval_metadata_ambiguous(ticket):
        try:
            result = await client.ticket_get(ticket_id, view="full")
        except Exception as exc:
            name = getattr(exc, "original_error_class", None) or type(exc).__name__
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name) is None:
                name = "ApprovalError"
            code = (getattr(exc, "original_error_code", None) or getattr(exc, "code", None)) if name == "RegistryRoutingError" else None
            if not isinstance(code, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None:
                code = None
            raise AuditError("full-input hydration failed", original_error_class=name, original_error_code=code) from None
        detail = result.get("ticket") if isinstance(result, Mapping) else None
        if not isinstance(detail, dict) or detail.get("ticket_id") != ticket_id:
            raise AuditError(f"ticket {ticket_id}: hydration identity mismatch")
        ticket = detail
    if _approval_sections_omitted(ticket):
        raise AuditError(f"ticket {ticket_id}: approval sections remain omitted")
    if _approval_metadata_ambiguous(ticket):
        raise AuditError(f"ticket {ticket_id}: ambiguous approval metadata")
    for section in ("submission_history", "review_history"):
        counter = section + "_omitted_count"
        if counter in ticket and (type(ticket[counter]) is not int or ticket[counter] != 0):
            # ticket_get(full) does not merge retained inline history overflow.
            # No bounded history recovery is supported by this audit client.
            raise AuditError(f"ticket {ticket_id}: incomplete {section}")
        history = ticket.get(section)
        if history is not None and (
            not isinstance(history, list)
            or any(not isinstance(row, Mapping) for row in history)
        ):
            raise AuditError(f"ticket {ticket_id}: invalid {section}")
    reviewed = ticket.get("review_history")
    if isinstance(reviewed, list) and reviewed and "review_verdict" not in ticket:
        raise AuditError(f"ticket {ticket_id}: reviewed ticket omitted review_verdict")
    if ticket.get("review_verdict") == "approve" and not isinstance(ticket.get("submission_history"), list):
        raise AuditError(f"ticket {ticket_id}: approved ticket omitted submission_history")
    # Unreviewed tickets legitimately have no verdict or history. Legacy approved
    # submissions with no parseable reference remain explicitly UNVERIFIABLE.
    return ticket


async def fetch_all_tickets(client: TicketClient) -> list[dict[str, Any]]:
    """Read every ticket using bounded status and ticket-id pages."""
    probe = await client.ticket_list(
        include_closed=True,
        include_archived=True,
        limit=1,
        view="summary",
    )
    total = probe.get("total_matching")
    if type(total) is not int or total < 0:
        raise AuditError("all-ticket probe omitted total_matching")

    ticket_ids: list[str] = []
    expected_statuses: dict[str, str] = {}
    seen: set[str] = set()
    for status in TICKET_STATUSES:
        for ticket in await _complete_status_page(client, status):
            ticket_id = ticket.get("ticket_id")
            if not isinstance(ticket_id, str) or not ticket_id:
                raise AuditError(f"status {status}: ticket omitted ticket_id")
            if ticket_id in seen:
                raise AuditError(f"ticket {ticket_id} appeared in multiple status pages")
            if ticket.get("status") != status:
                raise AuditError(f"ticket {ticket_id}: status changed during traversal")
            seen.add(ticket_id)
            expected_statuses[ticket_id] = status
            ticket_ids.append(ticket_id)
    if len(ticket_ids) != total:
        raise AuditError(
            f"status pages covered {len(ticket_ids)} of {total} tickets; "
            "the server may expose an unknown status"
        )

    tickets: list[dict[str, Any]] = []
    for chunk in _chunks(sorted(ticket_ids)):
        rows = await _complete_ticket_pages(
            client,
            label=f"ticket batch {chunk[0]}",
            ticket_ids=chunk,
            include_closed=True,
            include_archived=True,
            limit=len(chunk),
            view="full",
        )
        returned = {str(row.get("ticket_id")) for row in rows}
        if returned != set(chunk):
            missing = sorted(set(chunk) - returned)
            extra = sorted(returned - set(chunk))
            raise AuditError(
                f"ticket batch mismatch: missing={missing} extra={extra}"
            )
        for row in rows:
            ticket = await _complete_approval_input(client, row)
            if ticket.get("status") != expected_statuses[ticket["ticket_id"]]:
                raise AuditError(f"ticket {ticket['ticket_id']}: status changed during traversal")
            tickets.append(ticket)
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


def _main_names_ticket(repo: Path, ticket_id: str, main_ref: str) -> bool:
    if not ticket_id or ticket_id == "-":
        return False
    return bool(
        _git(
            repo,
            "log",
            "-1",
            "--format=%H",
            "--fixed-strings",
            f"--grep={ticket_id}",
            main_ref,
        ).stdout.strip()
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
    if _main_names_ticket(repo, ticket_id, main_ref):
        return ApprovalResult(ticket_id, commit, "LANDED_BY_REFERENCE", title)
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


def _capabilities(_role: str) -> dict[str, Any]:
    return {
        "can_work": False,
        "can_review": False,
        "tier_max": 2,
        "max_parallel": 1,
    }


def _client_identity_options(args: argparse.Namespace) -> dict[str, Any]:
    reserved_identity = args.agent_name == DEFAULT_AUDIT_AGENT_NAME
    return {
        "agent_name": args.agent_name,
        "role": args.role,
        "capabilities": _capabilities(args.role),
        "allow_takeover": args.allow_takeover,
        "allow_matching_takeover": reserved_identity and not args.allow_takeover,
        "agent_platform": (
            DEFAULT_AUDIT_AGENT_PLATFORM if reserved_identity else None
        ),
        "task_focus": DEFAULT_AUDIT_TASK_FOCUS if reserved_identity else None,
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
            **_client_identity_options(args),
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
    parser.add_argument("--agent-name", default=DEFAULT_AUDIT_AGENT_NAME)
    parser.add_argument(
        "--allow-takeover",
        action="store_true",
        help="explicitly allow the audit identity to take over an existing seat",
    )
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
