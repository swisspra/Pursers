#!/usr/bin/env python3
"""Delete operator-selected remote branches from a verified branch audit."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from tools.branch_audit import (
        AuditError,
        SCHEMA,
        TERMINAL_STATUSES,
        read_board_tickets,
        read_token,
        submitted_references,
        ticket_id_from_branch,
    )
except ModuleNotFoundError:  # Direct execution from the repository root.
    from branch_audit import (  # type: ignore[no-redef]
        AuditError,
        SCHEMA,
        TERMINAL_STATUSES,
        read_board_tickets,
        read_token,
        submitted_references,
        ticket_id_from_branch,
    )


SHA_RE = re.compile(r"[0-9a-f]{40}")


class PruneError(RuntimeError):
    """Raised when a deletion plan is not safe to execute."""


def _load_report(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise PruneError(f"audit JSON must use schema {SCHEMA}")
    return value


def _candidate_rows(report: Mapping[str, Any], tiers: Sequence[str]) -> list[dict[str, Any]]:
    candidates = report.get("deletion_candidates")
    if not isinstance(candidates, Mapping):
        raise PruneError("audit JSON has no deletion_candidates object")
    selected: list[dict[str, Any]] = []
    submitted_branches = set(report.get("protections", {}).get("submitted_branches", []))
    submitted_commits = set(report.get("protections", {}).get("submitted_commits", []))
    main_branch = str(report.get("main_branch", "main"))
    expected_category = {"A": "merged_into_main", "B": "unmerged_terminal_ticket"}
    for tier in tiers:
        rows = candidates.get(f"tier_{tier.lower()}", [])
        if not isinstance(rows, list):
            raise PruneError(f"tier {tier} candidates are malformed")
        for row in rows:
            if not isinstance(row, dict):
                raise PruneError(f"tier {tier} contains a malformed row")
            branch = str(row.get("branch", ""))
            commit = str(row.get("commit", ""))
            if row.get("deletion_tier") != tier:
                raise PruneError(f"{branch}: deletion tier does not match tier {tier}")
            if row.get("category") != expected_category[tier]:
                raise PruneError(f"{branch}: category is not eligible for tier {tier}")
            if branch in {"main", main_branch} or branch.startswith("integration/"):
                raise PruneError(f"{branch}: protected branch can never be deleted")
            if row.get("protected_reasons"):
                raise PruneError(f"{branch}: candidate unexpectedly has protections")
            if branch in submitted_branches or commit in submitted_commits:
                raise PruneError(f"{branch}: submitted review tip can never be deleted")
            if not branch or SHA_RE.fullmatch(commit) is None:
                raise PruneError("candidate branch and 40-character commit are required")
            ticket_id = ticket_id_from_branch(branch)
            if ticket_id is None or row.get("ticket_id") != ticket_id:
                raise PruneError(
                    f"{branch}: candidate must retain its exact resolvable ticket ID"
                )
            selected.append(row)
    return selected


def render_plan(rows: Sequence[Mapping[str, Any]], remote: str) -> str:
    lines = [f"Remote: {remote}", f"Branches selected: {len(rows)}"]
    for row in rows:
        lines.append(
            f"{row['deletion_tier']}  {row['commit']}  "
            f"{row.get('ticket_id') or '-'}  {row['branch']}"
        )
    return "\n".join(lines)


async def revalidate_board_state(
    rows: Sequence[Mapping[str, Any]],
    *,
    central_url: str,
    token: str,
    ca_file: Path | None,
    board_ids: Sequence[str],
) -> None:
    """Fail closed unless every selected row is still board-safe now."""
    ticket_ids = [str(row["ticket_id"]) for row in rows]
    tickets_by_id, active_tickets, _boards = await read_board_tickets(
        central_url=central_url,
        token=token,
        ca_file=ca_file,
        board_ids=board_ids,
        ticket_ids=ticket_ids,
    )
    submitted_branches, submitted_commits = submitted_references(active_tickets)
    for row in rows:
        branch = str(row["branch"])
        commit = str(row["commit"])
        ticket_id = str(row["ticket_id"])
        matches = tickets_by_id.get(ticket_id, [])
        if len(matches) != 1:
            raise PruneError(
                f"{branch}: ticket {ticket_id} is missing or ambiguous on the current board"
            )
        current = matches[0]
        current_status = str(current.get("status"))
        current_board = str(current.get("board_id"))
        if current_status not in TERMINAL_STATUSES:
            raise PruneError(
                f"{branch}: ticket {ticket_id} is now live ({current_status})"
            )
        if row.get("ticket_board") != current_board:
            raise PruneError(
                f"{branch}: ticket board changed from {row.get('ticket_board')} "
                f"to {current_board}"
            )
        if branch in submitted_branches or commit in submitted_commits:
            raise PruneError(
                f"{branch}: branch or audited commit is now awaiting review"
            )


def execute(rows: Sequence[Mapping[str, Any]], *, repo: Path, remote: str) -> None:
    for row in rows:
        branch = str(row["branch"])
        audited_tip = str(row["commit"])
        completed = subprocess.run(
            [
                "git",
                "push",
                "--porcelain",
                f"--force-with-lease=refs/heads/{branch}:{audited_tip}",
                remote,
                f":refs/heads/{branch}",
            ],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise PruneError(
                f"compare-and-delete failed for {remote}/{branch}: {detail}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_json", type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--remote")
    parser.add_argument("--tier", choices=("A", "B"), action="append", required=True)
    parser.add_argument("--confirm-delete", action="store_true")
    parser.add_argument(
        "--central-url", default=os.environ.get("ONBOARD_CENTRAL_URL")
    )
    parser.add_argument("--token-file", default=os.environ.get("ONBOARD_TOKEN_FILE"))
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--board", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = _load_report(args.audit_json)
        remote = args.remote or str(report.get("remote", "origin"))
        rows = _candidate_rows(report, list(dict.fromkeys(args.tier)))
        print(render_plan(rows, remote), flush=True)
        if not args.confirm_delete:
            print(
                "refusing to delete: review the plan and pass --confirm-delete",
                file=sys.stderr,
            )
            return 2
        board_ids = args.board or list(report.get("boards", []))
        if not args.central_url:
            raise PruneError(
                "--central-url or ONBOARD_CENTRAL_URL is required for current-state revalidation"
            )
        if not board_ids:
            raise PruneError("audit JSON or --board must select at least one board")
        asyncio.run(
            revalidate_board_state(
                rows,
                central_url=args.central_url,
                token=read_token(args.token_file),
                ca_file=args.ca_file,
                board_ids=board_ids,
            )
        )
        execute(rows, repo=args.repo.resolve(), remote=remote)
    except (OSError, ValueError, AuditError, PruneError) as exc:
        print(f"branch prune failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
