"""Bounded, read-only projection of submitted work and review outcomes."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

FULL_SHA = re.compile(r"[0-9a-f]{40}")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,200}")
BRANCH_AND_COMMIT = re.compile(
    r"(?im)^\s*branch_and_commit\s*:\s*"
    r"(?P<branch>[A-Za-z0-9._/-]{1,200})\s+@\s+"
    r"(?P<commit>[0-9a-f]{40})(?:\b|\s|;)"
)
MAX_TITLE_CHARS = 160
MAX_SUMMARY_CHARS = 1_000
MAX_FILE_CHARS = 240
MAX_FILES = 50
RESULT_STATES = frozenset({"missing", "pending", "approved", "rejected", "failed"})


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split())
    return compact[:limit] or None


def _safe_file(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_FILE_CHARS:
        return None
    if "\\" in value or "\x00" in value:
        return None
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        return None
    return candidate.as_posix()


def _safe_branch_and_commit(notes: Any) -> tuple[str | None, str | None]:
    if not isinstance(notes, str):
        return None, None
    match = BRANCH_AND_COMMIT.search(notes)
    if match is None:
        return None, None
    branch = match.group("branch")
    if (
        not BRANCH.fullmatch(branch)
        or branch.startswith("/")
        or any(part in {"", ".", ".."} for part in branch.split("/"))
    ):
        return None, None
    commit = match.group("commit")
    return (branch, commit) if FULL_SHA.fullmatch(commit) else (None, None)


def _latest_submission(ticket: dict[str, Any]) -> dict[str, Any] | None:
    history = ticket.get("submission_history")
    if not isinstance(history, list) or not history:
        return None
    latest = history[-1]
    return latest if isinstance(latest, dict) else None


def _review_is_current(ticket: dict[str, Any], submission: dict[str, Any]) -> bool:
    submitted_at = submission.get("submitted_at") or ticket.get("submitted_at")
    reviewed_at = ticket.get("reviewed_at")
    if not isinstance(submitted_at, str) or not isinstance(reviewed_at, str):
        return False
    try:
        submitted = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
        reviewed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        if submitted.tzinfo is None or reviewed.tzinfo is None:
            return False
        return reviewed.astimezone(timezone.utc) >= submitted.astimezone(timezone.utc)
    except ValueError:
        return False


def _result_state(
    ticket: dict[str, Any], submission: dict[str, Any] | None
) -> str:
    if submission is None:
        return "missing"
    status = str(ticket.get("status") or "").lower()
    if status in {"canceled", "cancelled", "terminated", "failed"}:
        return "failed"
    review_current = _review_is_current(ticket, submission)
    verdict = ticket.get("review_verdict") if review_current else None
    if verdict == "approve" and status == "closed":
        return "approved"
    if verdict == "reject":
        return "rejected"
    if status in {"submitted", "reviewing", "in_review"} or not review_current:
        return "pending"
    return "failed"


def project_ticket_result(ticket: dict[str, Any]) -> dict[str, Any]:
    """Return allow-listed result metadata without submission or review notes."""

    submission = _latest_submission(ticket)
    state = _result_state(ticket, submission)
    summary = None
    branch = None
    commit = None
    files: list[str] = []
    submitted_at = None
    if submission is not None:
        summary = _text(submission.get("summary") or ticket.get("summary"), MAX_SUMMARY_CHARS)
        branch, commit = _safe_branch_and_commit(submission.get("notes"))
        raw_files = submission.get("files_changed")
        if not isinstance(raw_files, list):
            raw_files = ticket.get("files_changed")
        if isinstance(raw_files, list):
            for value in raw_files:
                safe = _safe_file(value)
                if safe is not None and safe not in files:
                    files.append(safe)
                if len(files) >= MAX_FILES:
                    break
        submitted_at = _text(
            submission.get("submitted_at") or ticket.get("submitted_at"), 40
        )

    review_current = submission is not None and _review_is_current(ticket, submission)
    verdict = ticket.get("review_verdict") if review_current else None
    if verdict not in {"approve", "reject"}:
        verdict = None
    return {
        "state": state,
        "summary": summary,
        "branch": branch,
        "commit": commit,
        "files_changed": files,
        "files_omitted": min(10_000, max(
            0,
            len(submission.get("files_changed", [])) - len(files)
            if submission is not None and isinstance(submission.get("files_changed"), list)
            else 0,
        )),
        "submitted_at": submitted_at,
        "review": {
            "verdict": verdict,
            "reviewer": (
                _text(ticket.get("reviewed_by_agent_name"), 96)
                if review_current
                else None
            ),
            "reviewed_at": (
                _text(ticket.get("reviewed_at"), 40) if review_current else None
            ),
            "independent": bool(
                review_current
                and ticket.get("reviewed_by_principal_id")
                and ticket.get("submitted_by_principal_id")
                and ticket.get("reviewed_by_principal_id")
                != ticket.get("submitted_by_principal_id")
            ),
        },
    }
