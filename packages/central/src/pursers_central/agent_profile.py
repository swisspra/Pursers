"""Board-scoped presentation profile helpers.

Display names are presentation metadata only.  Operational identity remains the
immutable ``(board_id, principal_id, agent_name)`` tuple and its derived
``agent_id``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Any


DISPLAY_NAME_MAX_CHARS = 80
DISPLAY_NAME_MAX_BYTES = 256
DISPLAY_NAME_SCHEMA_VERSION = 1
MAX_AGENT_PROFILE_AUDIT = 1_000

_BIDI_CONTROLS = frozenset(
    {
        "LRE",
        "RLE",
        "LRO",
        "RLO",
        "PDF",
        "LRI",
        "RLI",
        "FSI",
        "PDI",
        "BN",
    }
)


def normalize_display_name(value: str | None) -> str | None:
    """Validate one optional display label and return its canonical form."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("display_name must be a string or null")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise ValueError("display_name must not be empty; use null to reset")
    if len(normalized) > DISPLAY_NAME_MAX_CHARS:
        raise ValueError(
            f"display_name must be at most {DISPLAY_NAME_MAX_CHARS} characters"
        )
    if len(normalized.encode("utf-8")) > DISPLAY_NAME_MAX_BYTES:
        raise ValueError(
            f"display_name must be at most {DISPLAY_NAME_MAX_BYTES} UTF-8 bytes"
        )
    for character in normalized:
        if unicodedata.category(character) == "Cc":
            raise ValueError("display_name must not contain control characters")
        if unicodedata.bidirectional(character) in _BIDI_CONTROLS:
            raise ValueError(
                "display_name must not contain bidirectional control characters"
            )
    return normalized


def display_name_revision(member: Mapping[str, Any]) -> int:
    revision = member.get("display_name_revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("agent display-name revision is invalid")
    return revision


def display_label(member: Mapping[str, Any]) -> str:
    value = member.get("display_name")
    if isinstance(value, str) and value:
        return value
    return str(member.get("agent_name") or "")


def profile_projection(member: Mapping[str, Any]) -> dict[str, Any]:
    value = member.get("display_name")
    return {
        "schema_version": DISPLAY_NAME_SCHEMA_VERSION,
        "agent_id": member.get("agent_id"),
        "agent_name": member.get("agent_name"),
        "display_name": value if isinstance(value, str) and value else None,
        "display_label": display_label(member),
        "revision": display_name_revision(member),
        "updated_at": member.get("display_name_updated_at"),
        "updated_by_agent_id": member.get("display_name_updated_by_agent_id"),
    }


def append_profile_audit(document: dict[str, Any], record: dict[str, Any]) -> None:
    audit = document.setdefault("agent_profile_audit", [])
    if not isinstance(audit, list):
        raise ValueError("agent profile audit is invalid")
    audit.append(record)
    if len(audit) > MAX_AGENT_PROFILE_AUDIT:
        del audit[: len(audit) - MAX_AGENT_PROFILE_AUDIT]
