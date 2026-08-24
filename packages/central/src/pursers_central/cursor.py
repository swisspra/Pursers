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

    def ack(self, principal_id: str, agent_name: str, board_id: str, cursor: int) -> int:
        principal_id = _require_text("principal_id", principal_id)
        agent_name = _require_text("agent_name", agent_name)
        board_id = _require_text("board_id", board_id)
        if not isinstance(cursor, int) or cursor < 0:
            raise ValueError("cursor must be a non-negative integer")
        key = _identity_key(principal_id, agent_name)
        acknowledged = 0

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
            }

        self.store.read_modify_write(
            self._path(board_id), mutate, lambda: self._default(board_id)
        )
        return acknowledged
