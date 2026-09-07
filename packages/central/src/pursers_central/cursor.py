"""Server-side consumer cursor keyed by principal, agent, and board."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from journal import _board_token, _require_text
from locked_store import LockedJsonStore


def _identity_key(principal_id: str, agent_name: str) -> str:
    raw = json.dumps([principal_id, agent_name], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CursorStore:
    def __init__(self, store: LockedJsonStore):
        self.store = store

    def _path(self, board_id: str):
        return self.store.path("cursors", f"{_board_token(board_id)}.json")

    @staticmethod
    def _default(board_id: str) -> dict[str, Any]:
        return {"board_id": board_id, "consumers": {}}

    @staticmethod
    def _check_document(document: dict[str, Any], board_id: str) -> None:
        if document.get("board_id") != board_id:
            raise ValueError("cursor board hash collision or corrupt document")

    def get(self, principal_id: str, agent_name: str, board_id: str) -> int:
        principal_id = _require_text("principal_id", principal_id)
        agent_name = _require_text("agent_name", agent_name)
        board_id = _require_text("board_id", board_id)
        document = self.store.load(self._path(board_id), lambda: self._default(board_id))
        self._check_document(document, board_id)
        entry = document["consumers"].get(_identity_key(principal_id, agent_name))
        return int(entry["cursor"]) if entry else 0

    def oldest_live_cursor(
        self,
        board_id: str,
        *,
        active_principals: set[str] | None = None,
        stale_after_s: float | None = None,
        now: float | None = None,
    ) -> int | None:
        """Return the smallest acknowledged live consumer cursor, or None when unset."""
        import time
        board_id = _require_text("board_id", board_id)
        current_time = time.time() if now is None else now
        document = self.store.load(self._path(board_id), lambda: self._default(board_id))
        self._check_document(document, board_id)
        cursors = []
        for entry in document.get("consumers", {}).values():
            if not isinstance(entry, dict):
                continue
            cursor_val = entry.get("cursor")
            if type(cursor_val) is not int or cursor_val < 0:
                continue
            if active_principals is not None and entry.get("principal_id") not in active_principals:
                continue
            updated_at = entry.get("updated_at")
            if stale_after_s is not None:
                if not isinstance(updated_at, (int, float)):
                    continue
                if current_time - float(updated_at) > stale_after_s:
                    continue
            cursors.append(int(cursor_val))
        return min(cursors) if cursors else None

    def ack(
        self,
        principal_id: str,
        agent_name: str,
        board_id: str,
        cursor: int,
        *,
        now: float | None = None,
    ) -> int:
        import time
        principal_id = _require_text("principal_id", principal_id)
        agent_name = _require_text("agent_name", agent_name)
        board_id = _require_text("board_id", board_id)
        if not isinstance(cursor, int) or cursor < 0:
            raise ValueError("cursor must be a non-negative integer")
        key = _identity_key(principal_id, agent_name)
        acknowledged = 0
        current_time = time.time() if now is None else now

        def mutate(document: dict[str, Any]) -> None:
            nonlocal acknowledged
            self._check_document(document, board_id)
            consumers = document.setdefault("consumers", {})
            current = int(consumers.get(key, {}).get("cursor", 0))
            acknowledged = max(current, cursor)
            consumers[key] = {
                "principal_id": principal_id,
                "agent_name": agent_name,
                "cursor": acknowledged,
                "updated_at": current_time,
            }

        self.store.read_modify_write(
            self._path(board_id), mutate, lambda: self._default(board_id)
        )
        return acknowledged
