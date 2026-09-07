"""Before/after benchmark for the archive tier and parsed-document cache.

Generates a legacy-shaped board document (240 tickets, 100 members, 800
dispatch/submission/review history entries, ~4.5 MB), then measures 200
``ticket_list`` reads plus 200 existing-seat ``board_join`` calls against the
legacy document and against the migrated one. The migrated document must read
at least 10x faster, and the read-only phase must never rewrite any document.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = PACKAGE_ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402


BOARD = "pursers"
READ_CALLS = 200
JOIN_CALLS = 200
SPEEDUP_TARGET = 10.0


def aged_iso(now: float, days: float) -> str:
    return central.iso_at(now - days * 86_400)


def make_ticket(
    ticket_id: str,
    status: str,
    *,
    now: float,
    age_days: float = 10.0,
    description_pad: int = 0,
    dispatch_entries: int = 0,
    submission_entries: int = 0,
    review_entries: int = 0,
) -> dict[str, object]:
    created = aged_iso(now, age_days + 1)
    terminal = aged_iso(now, age_days)
    ticket: dict[str, object] = {
        "ticket_id": ticket_id,
        "title": f"ticket {ticket_id}",
        "description": "d" * description_pad,
        "status": status,
        "priority": "medium",
        "tier": 2,
        "scope": "interactive-no-send",
        "created_at": created,
        "updated_at": terminal,
        "annotations": [],
        "dispatch_history": [
            {"state": "offered", "kind": "work", "at": created, "n": index}
            for index in range(dispatch_entries)
        ],
        "submission_history": [
            {"summary": f"s{index}", "at": created}
            for index in range(submission_entries)
        ],
        "review_history": [
            {
                "review_label": "independent-principal-review",
                "verdict": "approve",
                "at": created,
            }
            for index in range(review_entries)
        ],
    }
    if status == "closed":
        ticket["closed_at"] = terminal
    if status == "canceled":
        ticket["canceled_at"] = terminal
    return ticket


class ArchiveBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=PACKAGE_ROOT)
        self.root = Path(self.temp_dir.name)
        jwks_path = self.root / "jwks.json"
        jwks_path.write_text('{"keys": []}', encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": "https://issuer.example",
                "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
                "CENTRAL_JWKS_PATH": str(jwks_path),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
            },
        )
        self.environment.start()
        self.mcp, self.service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        review_scopes = frozenset({"board:read", "board:write", "board:review"})
        self.admin = central.Principal("PR-admin", "admin", review_scopes)
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call("board_join", agent_name="admin-agent")

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(name, {"board_id": BOARD, **arguments})

    def board_blob_size(self) -> int:
        sizes = self.service.store.document_sizes("boards")
        self.assertEqual(len(sizes), 1)
        return sizes[0][1]

    def document_versions(self) -> dict[str, int]:
        connection = sqlite3.connect(self.service.store.db_path)
        try:
            return {
                str(path): int(version)
                for path, version in connection.execute(
                    "SELECT path, version FROM documents ORDER BY path"
                ).fetchall()
            }
        finally:
            connection.close()

    def seed_legacy_document(self) -> tuple[int, int]:
        """Seed the legacy-shaped document; return (ticket_count, member_count)."""
        now = time.time()
        tickets: dict[str, dict[str, object]] = {}
        history_budget = 800
        for index in range(213):
            ticket_id = f"TK-closed{index:05d}"
            dispatch = min(3, history_budget)
            history_budget -= dispatch
            reviews = 1 if (index % 4 == 0 and history_budget > 0) else 0
            history_budget -= reviews
            submissions = 2 if (index % 5 == 0 and history_budget > 1) else 0
            history_budget -= submissions
            tickets[ticket_id] = make_ticket(
                ticket_id,
                "closed",
                now=now,
                description_pad=24_000,
                dispatch_entries=dispatch,
                submission_entries=submissions,
                review_entries=reviews,
            )
        for index in range(22):
            ticket_id = f"TK-cancel{index:05d}"
            dispatch = 1 if history_budget > 0 else 0
            history_budget -= dispatch
            tickets[ticket_id] = make_ticket(
                ticket_id,
                "canceled",
                now=now,
                description_pad=24_000,
                dispatch_entries=dispatch,
            )
        for index in range(5):
            ticket_id = f"TK-active{index:05d}"
            dispatch = min(2, history_budget)
            history_budget -= dispatch
            tickets[ticket_id] = make_ticket(
                ticket_id,
                "open",
                now=now,
                age_days=0.0,
                dispatch_entries=dispatch,
            )

        def mutate(document: dict[str, object]) -> None:
            document["tickets"].update(tickets)
            for index in range(100):
                member_id = f"AI-member-{index:04d}"
                principal_id = f"PR-member-{index:04d}"
                document["members"][member_id] = {
                    "agent_id": member_id,
                    "agent_name": f"member-{index:04d}",
                    "principal_id": principal_id,
                    "joined_at": central.iso_at(now),
                    "role": "worker",
                    "lifecycle_status": "retired",
                    "membership_role": "member",
                }
                document["principal_memberships"][principal_id] = {
                    "principal_id": principal_id,
                    "role": "member",
                    "source": "test",
                    "created_at": central.iso_at(now),
                    "updated_at": central.iso_at(now),
                }
            # Schema 8 keeps the one-shot migration from firing before the
            # "before" measurements capture the legacy-size hot document.
            document["schema_version"] = 8

        self.service.store.read_modify_write(
            self.service._path(BOARD), mutate, lambda: self.service._default(BOARD)
        )
        return len(tickets), 100

    async def measure_reads(self) -> float:
        started = time.perf_counter()
        for _ in range(READ_CALLS):
            await self.call("ticket_list")
        return time.perf_counter() - started

    def measure_service_reads(self) -> float:
        """Document-read cost per call under production write churn.

        Seven live seats bump the board version constantly, so the board
        document's parsed cache is invalidated between calls exactly as
        production churn does; this isolates the load-the-hot-document cost
        from the constant MCP tool-call wrapper overhead.
        """
        board_path = self.service._path(BOARD)
        started = time.perf_counter()
        for _ in range(READ_CALLS):
            self.service.store.invalidate_parsed_cache(board_path)
            self.service.load(BOARD)
        return time.perf_counter() - started

    async def measure_joins(self) -> float:
        started = time.perf_counter()
        for _ in range(JOIN_CALLS):
            # Real seats rejoin with takeover; the seat-name uniqueness rule
            # rejects a plain rejoin of an already-active membership.
            await self.call(
                "board_join", agent_name="admin-agent", allow_takeover=True
            )
        return time.perf_counter() - started

    async def test_migrated_document_reads_ten_times_faster_with_zero_writes(
        self,
    ) -> None:
        ticket_count, member_count = self.seed_legacy_document()
        legacy_bytes = self.board_blob_size()
        self.assertGreater(legacy_bytes, 4_000_000)

        service_reads_before = self.measure_service_reads()
        reads_before = await self.measure_reads()
        joins_before = await self.measure_joins()

        summary = self.service.archive_due_tickets(BOARD, time.time())
        self.assertEqual(len(summary["archived_ticket_ids"]), ticket_count - 5)
        migrated_bytes = self.board_blob_size()
        self.assertLess(migrated_bytes, 500_000)

        versions_before_reads = self.document_versions()
        service_reads_after = self.measure_service_reads()
        reads_after = await self.measure_reads()
        versions_after_reads = self.document_versions()
        # Read-only calls never rewrite any document.
        self.assertEqual(versions_before_reads, versions_after_reads)
        joins_after = await self.measure_joins()

        speedup = service_reads_before / max(service_reads_after, 1e-9)
        report = (
            "legacy_hot_bytes={legacy} migrated_hot_bytes={migrated} "
            "tickets={tickets} members={members} | "
            "document-read x{reads} (production churn, cache invalidated): "
            "before={sb:.3f}s after={sa:.3f}s speedup={speedup:.1f}x | "
            "ticket_list tool x{reads}: before={rb:.3f}s after={ra:.3f}s | "
            "board_join tool x{joins}: before={jb:.3f}s after={ja:.3f}s"
        ).format(
            legacy=legacy_bytes,
            migrated=migrated_bytes,
            tickets=ticket_count,
            members=member_count,
            reads=READ_CALLS,
            sb=service_reads_before,
            sa=service_reads_after,
            speedup=speedup,
            rb=reads_before,
            ra=reads_after,
            joins=JOIN_CALLS,
            jb=joins_before,
            ja=joins_after,
        )
        print("\narchive benchmark:", report)
        self.assertGreater(
            speedup,
            SPEEDUP_TARGET,
            f"document-read speedup below target: {report}",
        )
        self.assertLess(reads_after, reads_before, report)
        self.assertLess(joins_after, joins_before, report)


if __name__ == "__main__":
    unittest.main()
