#!/usr/bin/env python3
"""Delete operator-selected remote branches from a verified branch audit."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from tools.branch_audit import SCHEMA
except ModuleNotFoundError:  # Direct execution from the repository root.
    from branch_audit import SCHEMA


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
            if not branch or not commit:
                raise PruneError("candidate branch and commit are required")
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


def _remote_tip(repo: Path, remote: str, branch: str) -> str | None:
    completed = subprocess.run(
        ["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise PruneError(f"cannot read {remote}/{branch}: {completed.stderr.strip()}")
    line = completed.stdout.strip()
    return line.split("\t", 1)[0] if line else None


def execute(rows: Sequence[Mapping[str, Any]], *, repo: Path, remote: str) -> None:
    for row in rows:
        branch = str(row["branch"])
        audited_tip = str(row["commit"])
        current_tip = _remote_tip(repo, remote, branch)
        if current_tip != audited_tip:
            raise PruneError(
                f"{remote}/{branch} moved or disappeared: "
                f"audited={audited_tip} current={current_tip or 'missing'}"
            )
    for row in rows:
        branch = str(row["branch"])
        completed = subprocess.run(
            ["git", "push", remote, "--delete", branch],
            cwd=repo,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            raise PruneError(f"deletion failed for {remote}/{branch}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit_json", type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--remote")
    parser.add_argument("--tier", choices=("A", "B"), action="append", required=True)
    parser.add_argument("--confirm-delete", action="store_true")
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
        execute(rows, repo=args.repo.resolve(), remote=remote)
    except (OSError, ValueError, PruneError) as exc:
        print(f"branch prune failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
