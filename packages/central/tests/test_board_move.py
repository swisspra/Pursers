from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
CLIENT_SOURCE = REPOSITORY_ROOT / "packages" / "client" / "src"
sys.path.insert(0, str(REPOSITORY_ROOT / "tools" / "board_move"))
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import board_move  # noqa: E402
from instance_lock import CentralDataLock  # noqa: E402


class BoardMoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=PACKAGE_ROOT)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.archive = self.root / "board.json"
        self.board_id = "example-board"
        self.board = {
            "board_id": self.board_id,
            "schema_version": 6,
            "generation_token": None,
            "generation_revision": 0,
            "config": {
                "claim_ttl_s": 900,
                "scrub_profile": "strict",
                "review_policy": "strict",
                "scrub_allow_counts": {},
            },
            "members": {
                "AI-worker": {
                    "agent_id": "AI-worker",
                    "agent_name": "worker",
                    "principal_id": "PR-old",
                }
            },
            "principal_memberships": {
                "PR-old": {"principal_id": "PR-old", "role": "admin"}
            },
            "principal_revocations": {},
            "invites": {},
            "next_admission_revision": 1,
            "tickets": {
                "TK-keep": {
                    "ticket_id": "TK-keep",
                    "title": "Portable ticket",
                    "claimed_by_principal_id": "PR-old",
                }
            },
            "next_ticket_seq": 2,
            "memories": [{"memory_id": "MM-keep", "content": "portable memory"}],
            "next_memory_seq": 2,
            "state": {},
        }
        self.journal = {
            "board_id": self.board_id,
            "next_seq": 8,
            "compacted_through": 3,
            "rows": [
                {
                    "id": "EV-keep-0007",
                    "seq": 7,
                    "board_id": self.board_id,
                    "kind": "ticket_created",
                    "actor": "PR-old",
                    "payload_ref": "board://example-board/tickets/TK-keep",
                }
            ],
        }
        self._write_source(self.source, self.board, self.journal)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_source(
        root: Path, board: dict[str, object], journal: dict[str, object]
    ) -> None:
        root.mkdir(parents=True)
        connection = sqlite3.connect(root / "board.sqlite3")
        try:
            connection.execute(
                "CREATE TABLE documents (path TEXT PRIMARY KEY, doc JSON NOT NULL, "
                "version INTEGER NOT NULL CHECK (version >= 1))"
            )
            paths = board_move._paths(str(board["board_id"]))
            for path, document in (
                (paths["board"], board),
                (paths["journal"], journal),
            ):
                connection.execute(
                    "INSERT INTO documents(path, doc, version) VALUES (?, ?, 1)",
                    (path, json.dumps(document, sort_keys=True)),
                )
            connection.commit()
        finally:
            connection.close()

    def test_roundtrip_preserves_ids_sequences_and_manifest_hashes(self) -> None:
        exported = board_move.export_board(
            self.source, self.board_id, self.archive, commit=True
        )
        imported = board_move.import_board(self.target, self.archive, commit=True)
        reexported = board_move.make_archive(self.target, self.board_id)

        self.assertEqual(reexported["manifest"], exported["manifest"])
        self.assertEqual(imported["readback_manifest"], exported["manifest"])
        self.assertEqual(imported["generation_revision"], 1)
        self.assertNotEqual(
            reexported["board"]["generation_token"],
            self.board["generation_token"],
        )
        self.assertIn("TK-keep", reexported["board"]["tickets"])
        self.assertEqual(reexported["board"]["memories"][0]["memory_id"], "MM-keep")
        self.assertEqual(reexported["journal"]["next_seq"], 8)
        self.assertEqual(reexported["journal"]["rows"][0]["id"], "EV-keep-0007")

        with self.assertRaisesRegex(FileExistsError, "target board already exists"):
            board_move.import_board(self.target, self.archive, commit=True)

    def test_namespaced_package_import_does_not_need_flat_module_path(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                (
                    "import sys; "
                    f"sys.path.insert(0, {str(CLIENT_SOURCE)!r}); "
                    f"sys.path.insert(0, {str(PACKAGE_ROOT / 'src')!r}); "
                    "import pursers_central; "
                    "from pursers_central import central; "
                    "assert pursers_central.build_server is central.build_server; "
                    "assert hasattr(central, 'CentralDataLock')"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_principal_map_and_require_full_map(self) -> None:
        board_move.export_board(self.source, self.board_id, self.archive, commit=True)
        dry_run = board_move.import_board(self.target, self.archive)
        self.assertEqual(dry_run["unmapped_principals"], ["PR-old"])
        self.assertFalse(self.target.exists())
        with self.assertRaisesRegex(ValueError, "unmapped principals: PR-old"):
            board_move.import_board(
                self.target, self.archive, require_full_map=True
            )

        board_move.import_board(
            self.target,
            self.archive,
            principal_map={"PR-old": "PR-new"},
            require_full_map=True,
            commit=True,
        )
        moved = board_move.make_archive(self.target, self.board_id)
        self.assertIn("PR-new", moved["board"]["principal_memberships"])
        self.assertEqual(
            moved["board"]["tickets"]["TK-keep"]["claimed_by_principal_id"],
            "PR-new",
        )
        self.assertEqual(moved["journal"]["rows"][0]["actor"], "PR-new")

    def test_strict_scrub_gate_reports_then_refuses_commit(self) -> None:
        unsafe_board = copy.deepcopy(self.board)
        unsafe_board["memories"][0]["content"] = "".join(
            ("Bear", "er ", "ABCDEFGHIJKLM", "NOPQRSTUVWXYZ")
        )
        unsafe_source = self.root / "unsafe-source"
        unsafe_archive = self.root / "unsafe.json"
        self._write_source(unsafe_source, unsafe_board, self.journal)
        exported = board_move.export_board(
            unsafe_source, self.board_id, unsafe_archive, commit=True
        )
        self.assertEqual(exported["scrub_violations"][0]["rule"], "bearer_token")
        dry_run = board_move.import_board(self.target, unsafe_archive)
        self.assertEqual(dry_run["scrub_violations"][0]["rule"], "bearer_token")
        with self.assertRaisesRegex(ValueError, "target scrub profile: bearer_token"):
            board_move.import_board(self.target, unsafe_archive, commit=True)

    def test_live_data_directory_is_refused(self) -> None:
        with CentralDataLock(self.source):
            with self.assertRaisesRegex(RuntimeError, "data directory is live"):
                board_move.make_archive(self.source, self.board_id)

        board_move.export_board(self.source, self.board_id, self.archive, commit=True)
        with CentralDataLock(self.target):
            with self.assertRaisesRegex(RuntimeError, "data directory is live"):
                board_move.import_board(self.target, self.archive)


if __name__ == "__main__":
    unittest.main()
