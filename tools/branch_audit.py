#!/usr/bin/env python3
"""Audit remote branches against Git history and authoritative board tickets.

The audit is read-only with respect to both Git remotes and Pursers boards.  It
may refresh local remote-tracking refs unless ``--no-fetch`` is supplied.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "pursers.branch-audit/v1"
TICKET_ID_RE = re.compile(r"(?i)(TK-[a-z0-9]+)")
BRANCH_AND_COMMIT_RE = re.compile(
    r"(?im)^\s*branch_and_commit\s*:\s*([^\s@]+)\s*@\s*([0-9a-f]{40})\s*$"
)
TERMINAL_STATUSES = frozenset({"closed", "rejected", "canceled", "terminated"})
AWAITING_REVIEW_STATUSES = frozenset({"submitted", "reviewing", "in_review"})


class AuditError(RuntimeError):
    """Raised when an audit cannot prove a complete, safe result."""


@dataclass(frozen=True)
class RemoteBranch:
    name: str
    commit: str
    last_commit_date: str
    merged_into_main: bool


def _git(repo: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        raise AuditError(
            f"git {' '.join(arguments)} failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout


def collect_remote_branches(
    repo: Path,
    *,
    remote: str,
    main_branch: str,
    fetch: bool,
) -> list[RemoteBranch]:
    """Return every remote head after proving local refs match the remote."""
    if fetch:
        _git(repo, "fetch", "--prune", remote)
    main_ref = f"refs/remotes/{remote}/{main_branch}"
    _git(repo, "rev-parse", "--verify", f"{main_ref}^{{commit}}")

    raw_remote = _git(repo, "ls-remote", "--heads", remote)
    remote_heads = {
        ref.removeprefix("refs/heads/"): commit
        for line in raw_remote.splitlines()
        if line.strip()
        for commit, ref in [line.split("\t", 1)]
    }
    raw_local = _git(
        repo,
        "for-each-ref",
        "--format=%(refname:lstrip=3)\x1f%(objectname)\x1f%(committerdate:iso8601-strict)\x1f%(symref)",
        f"refs/remotes/{remote}/",
    )
    local: dict[str, tuple[str, str]] = {}
    for line in raw_local.splitlines():
        name, commit, committed_at, symref = line.split("\x1f", 3)
        if symref:
            continue
        local[name] = (commit, committed_at)
    if set(local) != set(remote_heads):
        missing = sorted(set(remote_heads) - set(local))
        extra = sorted(set(local) - set(remote_heads))
        raise AuditError(
            "remote-tracking refs do not match remote heads; "
            f"missing={missing[:10]} extra={extra[:10]}"
        )
    moved = sorted(
        name for name, commit in remote_heads.items() if local[name][0] != commit
    )
    if moved:
        raise AuditError(f"remote-tracking refs have stale tips: {moved[:10]}")

    merged = set(
        _git(
            repo,
            "for-each-ref",
            f"--merged={main_ref}",
            "--format=%(refname:lstrip=3)",
            f"refs/remotes/{remote}/",
        ).splitlines()
    )
    return [
        RemoteBranch(
            name=name,
            commit=commit,
            last_commit_date=local[name][1],
            merged_into_main=name in merged,
        )
        for name, commit in sorted(remote_heads.items())
    ]


def ticket_id_from_branch(branch: str) -> str | None:
    match = TICKET_ID_RE.search(branch)
    if match is None:
        return None
    value = match.group(1)
    return f"TK-{value[3:]}"


def _canonical_branch(value: str) -> str:
    branch = value.strip()
    for prefix in ("refs/heads/", "origin/"):
        if branch.startswith(prefix):
            branch = branch[len(prefix) :]
    return branch


def submitted_references(
    tickets: Iterable[Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    branches: set[str] = set()
    commits: set[str] = set()
    for ticket in tickets:
        if ticket.get("status") not in AWAITING_REVIEW_STATUSES:
            continue
        texts = [ticket.get("notes")]
        if not isinstance(texts[0], str):
            history = ticket.get("submission_history")
            if isinstance(history, list) and history and isinstance(history[-1], Mapping):
                texts = [history[-1].get("notes")]
        for text in texts:
            if not isinstance(text, str):
                continue
            for match in BRANCH_AND_COMMIT_RE.finditer(text):
                branches.add(_canonical_branch(match.group(1)))
                commits.add(match.group(2))
    return branches, commits


def _ticket_index(
    tickets_by_id: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Mapping[str, Any] | None]:
    return {
        ticket_id: matches[0] if len(matches) == 1 else None
        for ticket_id, matches in tickets_by_id.items()
    }


def build_report(
    branches: Sequence[RemoteBranch],
    *,
    remote: str,
    main_branch: str,
    tickets_by_id: Mapping[str, Sequence[Mapping[str, Any]]],
    submitted_branches: set[str],
    submitted_commits: set[str],
    boards: Sequence[str],
) -> dict[str, Any]:
    """Classify branches and construct deletion candidates with protections."""
    ticket_index = _ticket_index(tickets_by_id)
    rows: list[dict[str, Any]] = []
    for branch in branches:
        ticket_id = ticket_id_from_branch(branch.name)
        matches = tickets_by_id.get(ticket_id, ()) if ticket_id else ()
        ticket = ticket_index.get(ticket_id) if ticket_id else None
        status = str(ticket.get("status")) if ticket else None
        board_id = str(ticket.get("board_id")) if ticket else None
        unresolved_reason = None
        if ticket_id is None:
            unresolved_reason = "missing_ticket_id"
        elif len(matches) == 0:
            unresolved_reason = "ticket_not_found"
        elif len(matches) > 1:
            unresolved_reason = "ticket_id_is_ambiguous_across_boards"

        if branch.merged_into_main:
            category = "merged_into_main"
        elif unresolved_reason:
            category = "no_resolvable_ticket"
        elif status in TERMINAL_STATUSES:
            category = "unmerged_terminal_ticket"
        else:
            category = "unmerged_live_ticket"

        protected_reasons: list[str] = []
        if branch.name in {main_branch, "main"}:
            protected_reasons.append("default_branch")
        if branch.name.startswith("integration/"):
            protected_reasons.append("integration_branch")
        if ticket is not None and status not in TERMINAL_STATUSES:
            protected_reasons.append(f"live_ticket_status:{status}")
        if ticket_id is not None and unresolved_reason:
            protected_reasons.append(f"unresolved_ticket:{unresolved_reason}")
        if branch.name in submitted_branches:
            protected_reasons.append("submitted_branch_awaiting_review")
        if branch.commit in submitted_commits:
            protected_reasons.append("submitted_commit_awaiting_review")

        deletion_tier = None
        if not protected_reasons:
            if category == "merged_into_main":
                deletion_tier = "A"
            elif category == "unmerged_terminal_ticket":
                deletion_tier = "B"
        rows.append(
            {
                "branch": branch.name,
                "commit": branch.commit,
                "last_commit_date": branch.last_commit_date,
                "merged_into_main": branch.merged_into_main,
                "category": category,
                "ticket_id": ticket_id,
                "ticket_status": status,
                "ticket_board": board_id,
                "unresolved_reason": unresolved_reason,
                "protected_reasons": protected_reasons,
                "deletion_tier": deletion_tier,
            }
        )

    category_names = (
        "merged_into_main",
        "unmerged_terminal_ticket",
        "unmerged_live_ticket",
        "no_resolvable_ticket",
    )
    category_counts = {
        name: sum(row["category"] == name for row in rows) for name in category_names
    }
    if sum(category_counts.values()) != len(rows):
        raise AuditError("branch categories do not sum to the audited total")
    tier_a = sorted(
        (row for row in rows if row["deletion_tier"] == "A"),
        key=lambda row: (row["last_commit_date"], row["branch"]),
    )
    tier_b = sorted(
        (row for row in rows if row["deletion_tier"] == "B"),
        key=lambda row: (row["last_commit_date"], row["branch"]),
    )
    return {
        "schema": SCHEMA,
        "remote": remote,
        "main_branch": main_branch,
        "boards": list(boards),
        "total_branches": len(rows),
        "category_counts": category_counts,
        "policy": {
            "terminal_statuses": sorted(TERMINAL_STATUSES),
            "awaiting_review_statuses": sorted(AWAITING_REVIEW_STATUSES),
        },
        "protections": {
            "submitted_branches": sorted(submitted_branches),
            "submitted_commits": sorted(submitted_commits),
        },
        "deletion_candidates": {"tier_a": tier_a, "tier_b": tier_b},
        "branches": rows,
    }


def render_human(report: Mapping[str, Any]) -> str:
    counts = report["category_counts"]
    candidates = report["deletion_candidates"]
    lines = [
        f"Remote branches: {report['total_branches']}",
        (
            "Buckets: "
            f"merged={counts['merged_into_main']}  "
            f"terminal-unmerged={counts['unmerged_terminal_ticket']}  "
            f"live-unmerged={counts['unmerged_live_ticket']}  "
            f"no-resolvable-ticket={counts['no_resolvable_ticket']}"
        ),
        f"Deletion candidates: tier A={len(candidates['tier_a'])}  tier B={len(candidates['tier_b'])}",
    ]
    for key, label in (("tier_a", "Tier A oldest"), ("tier_b", "Tier B oldest")):
        lines.append(f"\n{label} (up to 10)")
        lines.append("DATE                       STATUS       TICKET             BRANCH")
        for row in candidates[key][:10]:
            lines.append(
                f"{row['last_commit_date']:<26} "
                f"{str(row['ticket_status'] or '-'):<12} "
                f"{str(row['ticket_id'] or '-'):<18} {row['branch']}"
            )
        if not candidates[key]:
            lines.append("(none)")
    return "\n".join(lines)


def _chunks(values: Sequence[str], size: int = 500) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield list(values[offset : offset + size])


def _decode_tool_result(result: Any) -> dict[str, Any]:
    if result.is_error:
        message = next(
            (
                item.text
                for item in result.content
                if isinstance(getattr(item, "text", None), str)
            ),
            "board tool call failed",
        )
        raise AuditError(message)
    value = result.structured_content
    if isinstance(value, Mapping):
        value = value.get("result", value)
    else:
        value = json.loads(result.content[0].text)
    if not isinstance(value, dict):
        raise AuditError("board returned a non-object result")
    return value


async def read_board_tickets(
    *,
    central_url: str,
    token: str,
    ca_file: Path | None,
    board_ids: Sequence[str],
    ticket_ids: Sequence[str],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[str]]:
    """Read tickets through MCP without joining, onboarding, or mutating a seat."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(
            httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=httpx2.Timeout(30.0, read=None),
                verify=str(ca_file) if ca_file else True,
                trust_env=False,
            )
        )
        transport = streamable_http_client(central_url, http_client=http)
        client = await stack.enter_async_context(
            Client(transport, mode="2026-07-28", cache=None)
        )
        selected_boards = list(board_ids)
        if not selected_boards:
            listed = _decode_tool_result(await client.call_tool("board_list", {}))
            selected_boards = sorted(
                str(item["board_id"])
                for item in listed.get("boards", [])
                if isinstance(item, Mapping) and item.get("board_id")
            )
        if not selected_boards:
            raise AuditError("no readable boards were returned")

        tickets_by_id: dict[str, list[dict[str, Any]]] = {}
        active_tickets: list[dict[str, Any]] = []
        for board_id in selected_boards:
            active = _decode_tool_result(
                await client.call_tool(
                    "ticket_list",
                    {
                        "board_id": board_id,
                        "include_closed": False,
                        "include_archived": True,
                        "limit": 500,
                        "view": "full",
                    },
                )
            )
            if active.get("truncated"):
                raise AuditError(
                    f"active ticket list for {board_id} exceeds the safe 500-row bound"
                )
            for item in active.get("tickets", []):
                if isinstance(item, dict):
                    item = {**item, "board_id": board_id}
                    active_tickets.append(item)
                    tickets_by_id.setdefault(str(item.get("ticket_id")), []).append(item)
            for chunk in _chunks(list(ticket_ids)):
                result = _decode_tool_result(
                    await client.call_tool(
                        "ticket_list",
                        {
                            "board_id": board_id,
                            "ticket_ids": chunk,
                            "include_closed": True,
                            "include_archived": True,
                            "limit": len(chunk),
                            "view": "full",
                        },
                    )
                )
                for item in result.get("tickets", []):
                    if not isinstance(item, dict):
                        continue
                    item = {**item, "board_id": board_id}
                    key = str(item.get("ticket_id"))
                    existing = tickets_by_id.setdefault(key, [])
                    if not any(entry.get("board_id") == board_id for entry in existing):
                        existing.append(item)
        return tickets_by_id, active_tickets, selected_boards


def _read_token(path_value: str | None) -> str:
    if not path_value:
        raise AuditError("--token-file or ONBOARD_TOKEN_FILE is required")
    path = Path(path_value)
    if not path.is_absolute() or not path.is_file():
        raise AuditError("token file must be an existing absolute path")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise AuditError("token file is empty")
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--main-branch", default="main")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument(
        "--central-url", default=os.environ.get("ONBOARD_CENTRAL_URL")
    )
    parser.add_argument("--token-file", default=os.environ.get("ONBOARD_TOKEN_FILE"))
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--board", action="append", default=[])
    parser.add_argument("--json-out", type=Path, required=True)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.central_url:
        raise AuditError("--central-url or ONBOARD_CENTRAL_URL is required")
    repo = args.repo.resolve()
    branches = collect_remote_branches(
        repo,
        remote=args.remote,
        main_branch=args.main_branch,
        fetch=not args.no_fetch,
    )
    ticket_ids = sorted(
        {
            ticket_id
            for branch in branches
            if (ticket_id := ticket_id_from_branch(branch.name)) is not None
        }
    )
    tickets_by_id, active_tickets, boards = asyncio.run(
        read_board_tickets(
            central_url=args.central_url,
            token=_read_token(args.token_file),
            ca_file=args.ca_file,
            board_ids=args.board,
            ticket_ids=ticket_ids,
        )
    )
    protected_branches, protected_commits = submitted_references(active_tickets)
    report = build_report(
        branches,
        remote=args.remote,
        main_branch=args.main_branch,
        tickets_by_id=tickets_by_id,
        submitted_branches=protected_branches,
        submitted_commits=protected_commits,
        boards=boards,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run(args)
    except Exception as exc:
        print(f"branch audit failed: {exc}", file=sys.stderr)
        return 1
    print(render_human(report))
    print(f"\nJSON: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
