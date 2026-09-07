"""Central-specific multi-document transaction adapter for vendored SQLiteStore."""

from __future__ import annotations

import copy
import contextvars
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from locked_store import DefaultFactory, Mutator, T
from sqlite_store import SQLiteStore


class TransactionalSQLiteStore(SQLiteStore):
    """Reuse one BEGIN IMMEDIATE connection across nested Store operations."""

    def __init__(self, root: str | Path, *, busy_timeout_ms: int = 30_000):
        self._transaction_connection: contextvars.ContextVar[sqlite3.Connection | None] = (
            contextvars.ContextVar("central_sqlite_transaction", default=None)
        )
        super().__init__(root, busy_timeout_ms=busy_timeout_ms)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        active = self._transaction_connection.get()
        if active is not None:
            yield
            return
        connection = self._connect()
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        token = self._transaction_connection.set(connection)
        try:
            yield
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            self._transaction_connection.reset(token)
            connection.close()

    def load(self, path: str | Path, default: DefaultFactory[T]) -> T:
        connection = self._transaction_connection.get()
        if connection is None:
            return super().load(path, default)
        key = self._key(path)
        row = connection.execute(
            "SELECT version FROM documents WHERE path = ?", (key,)
        ).fetchone()
        version: int | None = None
        if row is not None:
            version = int(row[0])
            cached = self._cache_get_shared(key, version)
            if cached is not None:
                self._record_activity(self._load_activity, key)
                return cached
            row = connection.execute(
                "SELECT doc FROM documents WHERE path = ?", (key,)
            ).fetchone()
        self._record_activity(self._load_activity, key)
        if row is None:
            return self._fresh_default(default)
        document = json.loads(row[0])
        if version is not None:
            self._cache_put(key, version, document)
        return document

    def read_modify_write(
        self, path: str | Path, mutate_fn: Mutator[T], default: DefaultFactory[T]
    ) -> T:
        connection = self._transaction_connection.get()
        if connection is None:
            return super().read_modify_write(path, mutate_fn, default)
        key = self._key(path)
        row = connection.execute(
            "SELECT doc, version FROM documents WHERE path = ?", (key,)
        ).fetchone()
        if row is None:
            current = self._fresh_default(default)
            version = 0
            stored_blob = None
        else:
            version = int(row[1])
            stored_blob = row[0]
            cached = self._cache_get_copy(key, version)
            current = json.loads(stored_blob) if cached is None else cached
        replacement = mutate_fn(current)
        updated = current if replacement is None else replacement
        encoded = json.dumps(
            updated, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if row is not None and encoded == stored_blob:
            # No-op mutation: never bump the version or rewrite the blob.
            return copy.deepcopy(updated)
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
        self._record_activity(self._save_activity, key)
        self._cache_put(key, next_version, updated)
        return copy.deepcopy(updated)

    def iter_documents(self, prefix: str) -> list[dict[str, Any]]:
        normalized = prefix.strip("/") + "/"
        connection = self._transaction_connection.get()
        owns_connection = connection is None
        if connection is None:
            connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT doc FROM documents WHERE path LIKE ? ORDER BY path",
                (normalized + "%",),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            if owns_connection:
                connection.close()
