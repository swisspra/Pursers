"""SQLite document backend implementing the approved Store seam."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from locked_store import DefaultFactory, Mutator, Store, T


class SQLiteStore(Store[Any]):
    """Transactional JSON-document store with one connection per operation.

    A bounded in-memory parsed-document cache keyed by the durable version
    column lets read paths validate freshness with ``SELECT version`` instead
    of re-reading and re-parsing the blob. Writes that do not change the
    encoded blob never bump the version, so read-only callers never rewrite
    documents.
    """

    ACTIVITY_WINDOW_S = 60.0
    ACTIVITY_MAX_ENTRIES = 4_096

    def __init__(self, root: str | Path, *, busy_timeout_ms: int = 30_000):
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "board.sqlite3"
        self.busy_timeout_ms = busy_timeout_ms
        self._parsed_cache: dict[str, tuple[int, Any]] = {}
        self._load_activity: dict[str, deque[float]] = {}
        self._save_activity: dict[str, deque[float]] = {}
        self._cache_lock = threading.Lock()
        self._thread_local = threading.local()
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

    def _read_connection(self) -> sqlite3.Connection:
        """One persistent SELECT-only connection per thread (WAL-safe).

        Read paths (version checks, cached and cold loads, size scans) never
        open a transaction; reusing the connection removes the per-call
        connect/PRAGMA overhead that dominated small-document reads.
        """
        connection = getattr(self._thread_local, "read_connection", None)
        if connection is None:
            connection = self._connect()
            self._thread_local.read_connection = connection
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS journal_rows (
                    path TEXT NOT NULL,
                    seq INTEGER NOT NULL CHECK (seq >= 1),
                    event JSON NOT NULL,
                    PRIMARY KEY (path, seq),
                    FOREIGN KEY (path) REFERENCES documents(path) ON DELETE CASCADE
                ) WITHOUT ROWID
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS journal_state (
                    path TEXT PRIMARY KEY,
                    source_version INTEGER NOT NULL CHECK (source_version >= 1),
                    board_id TEXT NOT NULL,
                    next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
                    compacted_through INTEGER NOT NULL CHECK (compacted_through >= 0),
                    retained_count INTEGER NOT NULL CHECK (retained_count >= 0),
                    FOREIGN KEY (path) REFERENCES documents(path) ON DELETE CASCADE
                )
                """
            )
        finally:
            connection.close()

    def _record_activity(self, table: dict[str, deque[float]], key: str) -> None:
        now = time.monotonic()
        with self._cache_lock:
            entries = table.setdefault(key, deque())
            entries.append(now)
            cutoff = now - self.ACTIVITY_WINDOW_S
            while entries and entries[0] < cutoff:
                entries.popleft()
            if len(entries) > self.ACTIVITY_MAX_ENTRIES:
                for _ in range(len(entries) - self.ACTIVITY_MAX_ENTRIES):
                    entries.popleft()

    def activity_counts(self, path: str | Path, window_s: float | None = None) -> dict[str, int]:
        """Return bounded recent load/save counts for one logical document."""
        key = self._key(path)
        cutoff = time.monotonic() - float(
            self.ACTIVITY_WINDOW_S if window_s is None else window_s
        )
        with self._cache_lock:
            return {
                "loads": sum(
                    1 for stamp in self._load_activity.get(key, ()) if stamp >= cutoff
                ),
                "saves": sum(
                    1 for stamp in self._save_activity.get(key, ()) if stamp >= cutoff
                ),
            }

    def _cache_get_shared(self, key: str, version: int) -> Any | None:
        """Return the cached parsed document itself (callers must not mutate).

        Read-only board tools only project/normalize deterministically, and
        ``ensure_schema`` normalization is idempotent, so the shared object is
        safe for read paths; the write path uses ``_cache_get_copy``.
        """
        with self._cache_lock:
            cached = self._parsed_cache.get(key)
            if cached is not None and cached[0] == version:
                return cached[1]
        return None

    def _cache_get_copy(self, key: str, version: int) -> Any | None:
        """Return an isolated deepcopy for mutation inside a write transaction."""
        with self._cache_lock:
            cached = self._parsed_cache.get(key)
            if cached is not None and cached[0] == version:
                return copy.deepcopy(cached[1])
        return None

    def _cache_put(self, key: str, version: int, document: Any) -> None:
        with self._cache_lock:
            self._parsed_cache[key] = (version, document)

    def invalidate_parsed_cache(self, path: str | Path | None = None) -> None:
        """Drop cached parsed documents (one logical path, or all).

        Production write churn bumps versions constantly; tests and operators
        can force the cold read path deterministically.
        """
        with self._cache_lock:
            if path is None:
                self._parsed_cache.clear()
            else:
                self._parsed_cache.pop(self._key(path), None)

    def document_version(self, path: str | Path) -> int | None:
        """Return the durable version of one document without reading its blob."""
        key = self._key(path)
        row = self._read_connection().execute(
            "SELECT version FROM documents WHERE path = ?", (key,)
        ).fetchone()
        return None if row is None else int(row[0])

    def document_sizes(self, prefix: str) -> list[tuple[str, int]]:
        """Return (path, blob-bytes) pairs under a prefix without parsing."""
        normalized = prefix.strip("/") + "/"
        rows = self._read_connection().execute(
            "SELECT path, length(doc) FROM documents WHERE path LIKE ? ORDER BY path",
            (normalized + "%",),
        ).fetchall()
        return [(str(row[0]), int(row[1])) for row in rows]

    def document_values(self, prefix: str, json_field: str) -> list[tuple[str, Any]]:
        """Extract one top-level JSON field per document without full parses."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", json_field):
            raise ValueError("json_field must be a plain identifier")
        normalized = prefix.strip("/") + "/"
        rows = self._read_connection().execute(
            "SELECT path, json_extract(doc, ?) FROM documents "
            "WHERE path LIKE ? ORDER BY path",
            (f"$.{json_field}", normalized + "%"),
        ).fetchall()
        return [(str(row[0]), row[1]) for row in rows]

    @staticmethod
    def _is_journal_key(key: str) -> bool:
        return key.startswith("journals/") and key.endswith(".json")

    def _sync_journal_index(
        self,
        connection: sqlite3.Connection,
        key: str,
        document: Any,
        version: int,
    ) -> None:
        """Synchronize a journal's bounded row index in the write transaction."""
        if not self._is_journal_key(key):
            return
        if not isinstance(document, dict):
            raise ValueError("journal document must be an object")
        board_id = document.get("board_id")
        rows = document.get("rows")
        if not isinstance(board_id, str) or not isinstance(rows, list):
            raise ValueError("journal document metadata is corrupt")
        next_seq = int(document.get("next_seq", 0))
        compacted_through = int(document.get("compacted_through", 0))
        expected_count = next_seq - compacted_through - 1
        contiguous = (
            next_seq >= 1
            and compacted_through >= 0
            and compacted_through < next_seq
            and len(rows) == expected_count
            and (
                not rows
                or (
                    int(rows[0].get("seq", 0)) == compacted_through + 1
                    and int(rows[-1].get("seq", 0)) == next_seq - 1
                )
            )
        )
        if not contiguous:
            raise ValueError("journal rows are not a contiguous sequence")

        state = connection.execute(
            "SELECT source_version, board_id, next_seq, compacted_through, "
            "retained_count FROM journal_state WHERE path = ?",
            (key,),
        ).fetchone()
        if state is not None and int(state[0]) == version:
            return
        incremental = (
            state is not None
            and str(state[1]) == board_id
            and int(state[2]) <= next_seq
            and int(state[3]) <= compacted_through
            and int(state[4]) == int(state[2]) - int(state[3]) - 1
            and (
                int(state[2]) < next_seq
                or int(state[3]) < compacted_through
            )
        )
        if incremental:
            prior_next_seq = int(state[2])
            connection.execute(
                "DELETE FROM journal_rows WHERE path = ? AND seq <= ?",
                (key, compacted_through),
            )
            first_seq = compacted_through + 1
            offset = max(0, prior_next_seq - first_seq)
            new_rows = rows[offset:]
        else:
            connection.execute("DELETE FROM journal_rows WHERE path = ?", (key,))
            new_rows = rows
        encoded_rows = []
        for row in new_rows:
            if not isinstance(row, dict):
                raise ValueError("journal row must be an object")
            encoded_rows.append(
                (
                    key,
                    int(row["seq"]),
                    json.dumps(
                        row,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
        connection.executemany(
            "INSERT OR REPLACE INTO journal_rows(path, seq, event) VALUES (?, ?, ?)",
            encoded_rows,
        )
        indexed_count = int(
            connection.execute(
                "SELECT count(*) FROM journal_rows WHERE path = ?", (key,)
            ).fetchone()[0]
        )
        if indexed_count != len(rows):
            raise ValueError("journal row index is inconsistent")
        connection.execute(
            "INSERT INTO journal_state(path, source_version, board_id, next_seq, "
            "compacted_through, retained_count) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET source_version=excluded.source_version, "
            "board_id=excluded.board_id, next_seq=excluded.next_seq, "
            "compacted_through=excluded.compacted_through, "
            "retained_count=excluded.retained_count",
            (key, version, board_id, next_seq, compacted_through, len(rows)),
        )

    @staticmethod
    def _journal_read_result(
        connection: sqlite3.Connection,
        key: str,
        board_id: str,
        cursor: int,
        limit: int,
    ) -> dict[str, Any]:
        state = connection.execute(
            "SELECT board_id, next_seq, compacted_through FROM journal_state "
            "WHERE path = ?",
            (key,),
        ).fetchone()
        if state is None:
            latest_cursor = 0
            compacted_through = 0
        else:
            if str(state[0]) != board_id:
                raise ValueError("journal board hash collision or corrupt document")
            latest_cursor = int(state[1]) - 1
            compacted_through = int(state[2])
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
        rows = connection.execute(
            "SELECT event FROM journal_rows WHERE path = ? AND seq > ? "
            "ORDER BY seq LIMIT ?",
            (key, cursor, limit),
        ).fetchall()
        events = [json.loads(row[0]) for row in rows]
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

    def journal_read_after(
        self,
        path: str | Path,
        board_id: str,
        cursor: int,
        limit: int,
    ) -> dict[str, Any]:
        """Read a bounded journal page from the durable ``(path, seq)`` index."""
        key = self._key(path)
        read_connection = self._read_connection()
        indexed_result: dict[str, Any] | None = None
        try:
            # Keep version, journal state, and page rows on one WAL snapshot.
            read_connection.execute("BEGIN")
            versions = read_connection.execute(
                "SELECT d.version, s.source_version FROM documents d "
                "LEFT JOIN journal_state s ON s.path = d.path WHERE d.path = ?",
                (key,),
            ).fetchone()
            if versions is None or (
                versions[1] is not None and int(versions[0]) == int(versions[1])
            ):
                indexed_result = self._journal_read_result(
                    read_connection, key, board_id, cursor, limit
                )
            read_connection.commit()
        except BaseException:
            if read_connection.in_transaction:
                read_connection.rollback()
            raise
        if indexed_result is not None:
            return indexed_result
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT doc, version FROM documents WHERE path = ?", (key,)
            ).fetchone()
            if row is not None:
                self._sync_journal_index(
                    connection, key, json.loads(row[0]), int(row[1])
                )
            result = self._journal_read_result(
                connection, key, board_id, cursor, limit
            )
            connection.commit()
            return result
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def load(self, path: str | Path, default: DefaultFactory[T]) -> T:
        key = self._key(path)
        connection = self._read_connection()
        row = connection.execute(
            "SELECT version FROM documents WHERE path = ?", (key,)
        ).fetchone()
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
        self._cache_put(key, version, document)
        return document

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
                self._sync_journal_index(connection, key, updated, version)
                connection.commit()
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
            self._sync_journal_index(connection, key, updated, next_version)
            self._before_commit(connection, key, updated, next_version)
            connection.commit()
            self._record_activity(self._save_activity, key)
            self._cache_put(key, next_version, updated)
            return copy.deepcopy(updated)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
