"""SQLite document backend implementing the approved Store seam.

Fixture provenance: synthetic transactional-store implementation.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

if __package__:
    from .locked_store import DefaultFactory, Mutator, Store, T
else:  # source-checkout execution
    from locked_store import DefaultFactory, Mutator, Store, T


class SQLiteStore(Store[Any]):
    """Transactional JSON-document store with one connection per operation."""

    def __init__(self, root: str | Path, *, busy_timeout_ms: int = 30_000):
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "board.sqlite3"
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize()

    def path(self, *parts: str | Path) -> Path:
        if not parts:
            return self.root
        if any(Path(part).is_absolute() for part in parts):
            raise ValueError("store paths must be relative")
        candidate = self.root.joinpath(*parts).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise ValueError("store path escapes root")
        return candidate

    def _key(self, path: str | Path) -> str:
        candidate = Path(path)
        target = self.path(candidate) if not candidate.is_absolute() else candidate.resolve(strict=False)
        if not target.is_relative_to(self.root):
            raise ValueError("store path escapes root")
        relative = target.relative_to(self.root).as_posix()
        if not relative or relative == "." or relative == self.db_path.name:
            raise ValueError("document path must name a logical document")
        return relative

    @staticmethod
    def _fresh_default(default: DefaultFactory[T]) -> T:
        return default() if callable(default) else copy.deepcopy(default)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError(f"SQLite refused WAL mode: {mode}")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    path TEXT PRIMARY KEY,
                    doc JSON NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1)
                )
                """
            )
        finally:
            connection.close()

    def load(self, path: str | Path, default: DefaultFactory[T]) -> T:
        key = self._key(path)
        connection = self._connect()
        try:
            row = connection.execute("SELECT doc FROM documents WHERE path = ?", (key,)).fetchone()
        finally:
            connection.close()
        if row is None:
            return self._fresh_default(default)
        return json.loads(row[0])

    def _before_commit(
        self,
        connection: sqlite3.Connection,
        key: str,
        value: Any,
        version: int,
    ) -> None:
        """Crash-test hook after SQL mutation and before COMMIT."""

    def read_modify_write(
        self, path: str | Path, mutate_fn: Mutator[T], default: DefaultFactory[T]
    ) -> T:
        key = self._key(path)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT doc, version FROM documents WHERE path = ?", (key,)
            ).fetchone()
            if row is None:
                current = self._fresh_default(default)
                version = 0
            else:
                current = json.loads(row[0])
                version = int(row[1])
            before = copy.deepcopy(current)
            replacement = mutate_fn(current)
            updated = current if replacement is None else replacement
            if row is not None and updated == before:
                connection.commit()
                return copy.deepcopy(updated)
            encoded = json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            next_version = version + 1
            if row is None:
                connection.execute(
                    "INSERT INTO documents(path, doc, version) VALUES (?, ?, ?)",
                    (key, encoded, next_version),
                )
            else:
                cursor = connection.execute(
                    "UPDATE documents SET doc = ?, version = ? WHERE path = ? AND version = ?",
                    (encoded, next_version, key, version),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("optimistic version conflict inside write transaction")
            self._before_commit(connection, key, updated, next_version)
            connection.commit()
            return copy.deepcopy(updated)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
