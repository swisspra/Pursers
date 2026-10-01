"""Opaque, principal-bound cursors for bounded ticket and history reads."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any


CURSOR_SCHEMA_VERSION = 1
DEFAULT_PAGE_MAX_BYTES = 192 * 1024
HISTORY_FIELDS = {
    "annotations": "annotations",
    "dispatch": "dispatch_history",
    "submissions": "submission_history",
    "reviews": "review_history",
    "progress": "progress_history",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise ValueError("cursor is invalid; restart the traversal without cursor")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "cursor is invalid; restart the traversal without cursor"
        ) from exc


def filter_digest(filters: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(filters)).hexdigest()


class TicketCursorCodec:
    """Seal traversal state with a private rotating Central keyring."""

    def __init__(self, keys: Sequence[bytes]):
        if not keys or any(len(key) < 32 for key in keys):
            raise ValueError("ticket cursor keys must contain at least 32 bytes")
        self._keys = tuple(bytes(key) for key in keys)

    def encode(
        self,
        *,
        kind: str,
        board_id: str,
        principal_id: str,
        filters: Mapping[str, Any],
        position: Sequence[Any],
    ) -> str:
        payload = _canonical(
            {
                "v": CURSOR_SCHEMA_VERSION,
                "kind": kind,
                "board": board_id,
                "principal": principal_id,
                "filters": filter_digest(filters),
                "position": list(position),
            }
        )
        signature = hmac.new(self._keys[0], payload, hashlib.sha256).digest()
        return f"{_b64encode(payload)}.{_b64encode(signature)}"

    def decode(
        self,
        cursor: str,
        *,
        kind: str,
        board_id: str,
        principal_id: str,
        filters: Mapping[str, Any],
    ) -> list[Any]:
        if not isinstance(cursor, str) or cursor.count(".") != 1:
            raise ValueError("cursor is invalid; restart the traversal without cursor")
        payload_text, signature_text = cursor.split(".", 1)
        payload = _b64decode(payload_text)
        signature = _b64decode(signature_text)
        if not any(
            hmac.compare_digest(
                hmac.new(key, payload, hashlib.sha256).digest(), signature
            )
            for key in self._keys
        ):
            raise ValueError("cursor integrity check failed; restart without cursor")
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "cursor is invalid; restart the traversal without cursor"
            ) from exc
        if not isinstance(decoded, dict) or decoded.get("v") != CURSOR_SCHEMA_VERSION:
            raise ValueError("cursor version is unsupported; restart without cursor")
        expected = {
            "kind": kind,
            "board": board_id,
            "principal": principal_id,
            "filters": filter_digest(filters),
        }
        if any(decoded.get(field) != value for field, value in expected.items()):
            raise ValueError(
                "cursor does not match this board, principal, or filter set; "
                "restart without cursor"
            )
        position = decoded.get("position")
        if not isinstance(position, list):
            raise ValueError("cursor position is invalid; restart without cursor")
        return position


def entry_id(field: str, entry: Mapping[str, Any], ordinal: int) -> str:
    """Return a stored identifier or a stable identifier for legacy entries."""
    for key in (
        "annotation_id",
        "submission_id",
        "review_id",
        "progress_id",
        "event_id",
        "id",
    ):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    digest = hashlib.sha256(
        _canonical({"field": field, "ordinal": ordinal, "entry": entry})
    ).hexdigest()
    return f"HE-{digest[:24]}"


def history_timestamp(entry: Mapping[str, Any]) -> str | None:
    for key in ("occurred_at", "at", "created_at", "updated_at", "assessed_at"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def serialized_bytes(value: Any) -> int:
    return len(_canonical(value))
