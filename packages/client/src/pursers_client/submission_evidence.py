"""Deterministic interpretation of structured and legacy submission evidence."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

BRANCH_LINE = re.compile(
    r"(?im)^[ \t]*branch_and_commit[ \t]*:[ \t]*"
    r"([A-Za-z0-9][A-Za-z0-9._/+\-]{0,239})[ \t]*@[ \t]*([0-9a-f]{40})[ \t]*$"
)


def _identity(branch: Any, sha: Any) -> tuple[str, str]:
    if not isinstance(branch, str) or not isinstance(sha, str):
        return "", ""
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/+\-]{0,239}", branch)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)
            or ".." in branch or "//" in branch
            or any(p.endswith((".lock", ".")) for p in branch.split("/"))
            or branch.endswith("/")):
        return "", ""
    return branch, sha.lower()


def submission_identity(submission: Mapping[str, Any]) -> tuple[str, str]:
    """Read one unambiguous identity; conflicting evidence never selects a winner."""
    identities = []
    proof = submission.get("submission_preflight")
    if isinstance(proof, Mapping):
        identities.append(_identity(proof.get("branch"), proof.get("commit")))
    if submission.get("branch") is not None or submission.get("commit_hash") is not None:
        identities.append(_identity(submission.get("branch"), submission.get("commit_hash")))
    notes = submission.get("notes") or ""
    if not isinstance(notes, str):
        return "", ""
    labels = re.findall(r"(?im)^[ \t]*branch_and_commit[ \t]*:", notes)
    matches = BRANCH_LINE.findall(notes)
    if labels:
        if len(labels) != 1 or len(matches) != 1:
            return "", ""
        identities.append(_identity(*matches[0]))
    if not identities or ("", "") in identities or len(set(identities)) != 1:
        return "", ""
    return identities[0]


def submission_test_output(notes: str | None) -> str:
    match = re.search(
        r"(?ims)^[ \t]*(?:Required[ \t]+)?test_output[ \t]*:[ \t]*(.*?)"
        r"(?=^[ \t]*(?:Required[ \t]+)?(?:branch_and_commit|commit_hash|submission_preflight|model)[ \t]*:|\Z)",
        notes or "",
    )
    if match:
        return match.group(1).strip()
    # Older seats put required fields on one line. Keep semicolons inside the
    # quoted Jest/Vitest summary instead of splitting that evidence into fields.
    assignment = re.search(
        r"(?i)(?<![\w])test_output[ \t]*=[ \t]*(`[^`]*`|[^;\n]+)",
        notes or "",
    )
    return assignment.group(1).strip().strip("`") if assignment else ""


def rejection_fingerprint(reason: str | None) -> str:
    normalized = " ".join((reason or "").casefold().split())
    normalized = re.sub(r"\s*@\s*", "@", normalized)
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else ""
