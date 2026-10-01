"""Bounded, read-only projection of submitted work and review outcomes."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from pursers_client.submission_evidence import submission_identity
from pursers_client.delivery_workflow import DELIVERY_STAGES, branch_name

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
    branch, commit = submission_identity({"notes": notes})
    return branch or None, commit or None


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


def project_delivery(ticket: dict[str, Any]) -> dict[str, Any] | None:
    """Separate approval from externally confirmed delivery without guessing a PR."""
    if ticket.get("status") != "closed" or ticket.get("review_verdict") != "approve":
        return None
    submission = _latest_submission(ticket) or {}
    _, sha = submission_identity(submission)
    for annotation in reversed(ticket.get("annotations", [])):
        text = annotation.get("text", "") if isinstance(annotation, dict) else ""
        if not text.startswith("pursers-delivery: "):
            continue
        try:
            delivery = json.loads(text[len("pursers-delivery: "):])
        except (ValueError, TypeError):
            continue
        if not isinstance(delivery, dict) or delivery.get("state") not in DELIVERY_STAGES:
            continue
        if delivery.get("commit_hash") and delivery["commit_hash"] != sha:
            continue
        safe = {"state": delivery["state"], "pr_id": None, "url": None}
        if type(delivery.get("pr_id")) is int and delivery["pr_id"] > 0:
            safe["pr_id"] = delivery["pr_id"]
        url = delivery.get("url")
        if isinstance(url, str) and len(url) <= 1000:
            try:
                parsed = urlsplit(url)
                if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                    safe["url"] = url
            except ValueError:
                pass
        if delivery.get("target_branch"):
            try:
                safe["target_branch"] = branch_name(delivery["target_branch"])
            except ValueError:
                pass
        if re.fullmatch(r"[0-9a-f]{40}", str(delivery.get("merge_sha", ""))):
            safe["merge_sha"] = delivery["merge_sha"]
        return safe
    if any(isinstance(a, dict) and re.match(r"^source-writeback-sha256:[0-9a-f]{64}(?:\n|$)", str(a.get("text", "")))
           for a in ticket.get("annotations", [])):
        return {"state": "delivery_recorded", "pr_id": None, "url": None}
    if "Structured external-source intake." in str(ticket.get("description", "")):
        return {"state": "pr_pending", "pr_id": None, "url": None}
    return None


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
        parsed_branch, parsed_commit = submission_identity(submission)
        branch, commit = parsed_branch or None, parsed_commit or None
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
        "delivery": project_delivery(ticket),
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
