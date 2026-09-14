from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

from journal import Journal  # noqa: E402
from transactional_sqlite import TransactionalSQLiteStore  # noqa: E402


class JournalIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=PACKAGE_ROOT)
        self.store = TransactionalSQLiteStore(Path(self.temp_dir.name) / "data")
        self.journal = Journal(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def event(seq: int, *, padding: str = "") -> dict[str, object]:
        return {
            "id": f"EV-synthetic-{seq:020d}",
            "seq": seq,
            "board_id": "pursers",
            "kind": "memory_written",
            "actor": "AI-synthetic",
            "payload_ref": f"board://pursers/memory/MEM-{seq}",
            "occurred_at": "2030-01-01T00:00:00+00:00",
            "fixture_provenance": "synthetic journal index test",
            "detail": padding,
        }

    def seed(self, total: int, *, padding: str = "") -> None:
        document = {
            "board_id": "pursers",
            "next_seq": total + 1,
            "compacted_through": 0,
            "rows": [self.event(seq, padding=padding) for seq in range(1, total + 1)],
        }
        self.store.read_modify_write(
            self.journal._path("pursers"), lambda _current: document, dict
        )

    def test_reads_across_index_page_boundaries_without_loading_document(self) -> None:
        self.seed(1_100)
        self.store.invalidate_parsed_cache()

        with patch.object(self.store, "load", side_effect=AssertionError("full load")):
            first = self.journal.read_after("pursers", 510, 5)
            second = self.journal.read_after("pursers", first["next_cursor"], 510)

        self.assertEqual([row["seq"] for row in first["events"]], list(range(511, 516)))
        self.assertEqual(second["events"][0]["seq"], 516)
        self.assertEqual(second["events"][-1]["seq"], 1_025)
        self.assertTrue(second["has_more"])
        self.assertEqual(second["latest_cursor"], 1_100)

    def test_compaction_updates_index_and_preserves_resync_semantics(self) -> None:
        self.seed(1_100)

        compacted = self.journal.compact("pursers", 500)
        stale = self.journal.read_after("pursers", 599, 100)
        boundary = self.journal.read_after("pursers", 600, 3)

        self.assertEqual(compacted["removed"], 600)
        self.assertEqual(compacted["retained"], 500)
        self.assertEqual(compacted["compacted_through"], 600)
        self.assertTrue(stale["resync_required"])
        self.assertEqual(stale["events"], [])
        self.assertEqual(stale["reset_cursor"], 1_100)
        self.assertFalse(boundary["resync_required"])
        self.assertEqual([row["seq"] for row in boundary["events"]], [601, 602, 603])

    def test_existing_document_is_indexed_once_on_first_read(self) -> None:
        document = {
            "board_id": "pursers",
            "next_seq": 4,
            "compacted_through": 0,
            "rows": [self.event(seq) for seq in range(1, 4)],
        }
        key = self.store._key(self.journal._path("pursers"))
        connection = self.store._connect()
        try:
            connection.execute(
                "INSERT INTO documents(path, doc, version) VALUES (?, ?, 1)",
                (key, json.dumps(document, separators=(",", ":"))),
            )
            connection.commit()
        finally:
            connection.close()

        with patch.object(
            self.store,
            "_sync_journal_index",
            wraps=self.store._sync_journal_index,
        ) as sync:
            first = self.journal.read_after("pursers", 0, 2)
            second = self.journal.read_after("pursers", 2, 2)

        self.assertEqual(sync.call_count, 1)
        self.assertEqual([row["seq"] for row in first["events"]], [1, 2])
        self.assertEqual([row["seq"] for row in second["events"]], [3])

    def test_31_mb_journal_tail_read_is_bounded(self) -> None:
        self.seed(23_692, padding="x" * 1_100)
        journal_path = self.journal._path("pursers")
        sizes = dict(self.store.document_sizes("journals"))
        logical_path = journal_path.relative_to(self.store.root).as_posix()
        self.assertGreater(sizes[logical_path], 30_000_000)
        self.store.invalidate_parsed_cache(journal_path)

        started = time.perf_counter()
        with patch.object(self.store, "load", side_effect=AssertionError("full load")):
            page = self.journal.read_after("pursers", 23_688, 4)
        elapsed = time.perf_counter() - started

        self.assertEqual([row["seq"] for row in page["events"]], [23_689, 23_690, 23_691, 23_692])
        self.assertFalse(page["has_more"])
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
