"""Durable per-board event journal on the vendored LockedJsonStore."""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any

if __package__:
    from .locked_store import LockedJsonStore
else:  # source-checkout execution
    from locked_store import LockedJsonStore


KINDS = frozenset({"ticket_status_changed", "ticket_created", "memory_written"})
SEMANTIC_FIELDS = frozenset(
    {
        "ticket_id",
        "memory_id",
        "status_from",
        "status_to",
        "reviewed_by",
        "rejection_count",
        "review_notes_ref",
        "fix_instructions_ref",
        "last_reaped_by",
        "last_reaped_at",
        "last_abandoned_by",
        "last_reviewer_abandoned_by",
        "abandoned_count",
        "fixture_provenance",
        "recipient_identities",
    }
)


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _board_token(board_id: str) -> str:
    board_id = _require_text("board_id", board_id)
    return hashlib.sha256(board_id.encode("utf-8")).hexdigest()


class Journal:
    """Append-only semantic cues with monotonic sequence IDs per board."""

    def __init__(self, store: LockedJsonStore):
        self.store = store

    def _path(self, board_id: str):
        return self.store.path("journals", f"{_board_token(board_id)}.json")

    @staticmethod
    def _default(board_id: str) -> dict[str, Any]:
        return {
            "board_id": board_id,
            "next_seq": 1,
            "compacted_through": 0,
            "rows": [],
        }

    @staticmethod
    def _check_document(document: dict[str, Any], board_id: str) -> None:
        if document.get("board_id") != board_id:
            raise ValueError("journal board hash collision or corrupt document")

    def append(self, board_id: str, event: dict[str, Any]) -> dict[str, Any]:
        board_id = _require_text("board_id", board_id)
        kind = _require_text("kind", event.get("kind"))
        if kind not in KINDS:
            raise ValueError(f"unsupported event kind: {kind}")
        actor = _require_text("actor", event.get("actor"))
        payload_ref = _require_text("payload_ref", event.get("payload_ref"))
        semantic = {key: copy.deepcopy(event[key]) for key in SEMANTIC_FIELDS if key in event}
        assigned: dict[str, Any] = {}

        def mutate(document: dict[str, Any]) -> None:
            nonlocal assigned
            self._check_document(document, board_id)
            seq = int(document["next_seq"])
            if seq <= int(document.get("compacted_through", 0)):
                raise ValueError("journal next_seq moved backwards")
            assigned = {
                "id": f"EV-{_board_token(board_id)[:12]}-{seq:020d}",
                "seq": seq,
                "board_id": board_id,
                "kind": kind,
                "actor": actor,
                "payload_ref": payload_ref,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                **semantic,
            }
            rows = document.setdefault("rows", [])
            if rows and int(rows[-1]["seq"]) >= seq:
                raise ValueError("journal sequence is not increasing")
            rows.append(assigned)
            document["next_seq"] = seq + 1

        self.store.read_modify_write(
            self._path(board_id), mutate, lambda: self._default(board_id)
        )
        return copy.deepcopy(assigned)

    def read_after(self, board_id: str, cursor: int, limit: int = 100) -> dict[str, Any]:
        board_id = _require_text("board_id", board_id)
        if not isinstance(cursor, int) or cursor < 0:
            raise ValueError("cursor must be a non-negative integer")
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        document = self.store.load(self._path(board_id), lambda: self._default(board_id))
        self._check_document(document, board_id)
        compacted_through = int(document.get("compacted_through", 0))
        latest_cursor = int(document["next_seq"]) - 1
        if cursor > latest_cursor:
            raise ValueError("cursor is ahead of journal")
        if cursor < compacted_through:
            return {
                "board_id": board_id,
                "events": [],
                "next_cursor": cursor,
                "latest_cursor": latest_cursor,
                "has_more": False,
                "resync_required": True,
                "compacted_through": compacted_through,
                "reset_cursor": latest_cursor,
            }
        events = [copy.deepcopy(row) for row in document["rows"] if int(row["seq"]) > cursor][
            :limit
        ]
        next_cursor = int(events[-1]["seq"]) if events else cursor
        return {
            "board_id": board_id,
            "events": events,
            "next_cursor": next_cursor,
            "latest_cursor": latest_cursor,
            "has_more": next_cursor < latest_cursor,
            "resync_required": False,
            "compacted_through": compacted_through,
            "reset_cursor": None,
        }

    def compact(self, board_id: str, retain_last: int) -> dict[str, int]:
        board_id = _require_text("board_id", board_id)
        if not isinstance(retain_last, int) or retain_last < 0:
            raise ValueError("retain_last must be a non-negative integer")
        result: dict[str, int] = {}

        def mutate(document: dict[str, Any]) -> None:
            nonlocal result
            self._check_document(document, board_id)
            rows = document["rows"]
            remove_count = max(0, len(rows) - retain_last)
            removed = rows[:remove_count]
            if removed:
                document["compacted_through"] = int(removed[-1]["seq"])
                document["rows"] = rows[remove_count:]
            result = {
                "removed": len(removed),
                "retained": len(document["rows"]),
                "compacted_through": int(document.get("compacted_through", 0)),
                "latest_cursor": int(document["next_seq"]) - 1,
            }

        self.store.read_modify_write(
            self._path(board_id), mutate, lambda: self._default(board_id)
        )
        return result
