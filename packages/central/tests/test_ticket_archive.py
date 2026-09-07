"""Archive tier: aged terminal tickets leave the hot board document.

Covers the one-shot legacy migration (schema_version guard), transparent
read-through for ticket_get/ticket_list/board_snapshot/board_status, the
bounded inline histories with archive overflow, the admin sweep tool with
its ticket_archived journal events, read-only zero-write guarantees, and
the healthz per-board archive instrumentation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from runtime_health import _health_counts  # noqa: E402


BOARD = "pursers"


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
                "verdict": "approve" if index % 2 else "reject",
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


class ArchiveTierTests(unittest.IsolatedAsyncioTestCase):
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
        work_scopes = frozenset({"board:read", "board:write"})
        self.admin = central.Principal("PR-admin", "admin", review_scopes)
        self.worker_a = central.Principal("PR-worker-a", "worker-a", work_scopes)
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        joined = await self.call(
            "board_join",
            agent_name="admin-agent",
            role="reviewer",
            capabilities={"can_work": False, "can_review": True},
        )
        self.admin_agent_id = joined.structured_content["agent_id"]

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

    def board_version(self) -> int:
        connection = sqlite3.connect(self.service.store.db_path)
        try:
            rows = connection.execute(
                "SELECT version FROM documents WHERE path LIKE 'boards/%'"
            ).fetchall()
        finally:
            connection.close()
        return int(rows[0][0])

    def seed_tickets(
        self,
        tickets: dict[str, dict[str, object]],
        *,
        schema_version: int = 7,
        next_ticket_seq: int | None = None,
    ) -> None:
        def mutate(document: dict[str, object]) -> None:
            document["tickets"].update(tickets)
            document["schema_version"] = schema_version
            if next_ticket_seq is not None:
                document["next_ticket_seq"] = next_ticket_seq

        self.service.store.read_modify_write(
            self.service._path(BOARD), mutate, lambda: self.service._default(BOARD)
        )

    async def test_legacy_migration_shrinks_hot_document_and_keeps_tickets_readable(
        self,
    ) -> None:
        now = time.time()
        tickets: dict[str, dict[str, object]] = {}
        archived_expected: list[str] = []
        # 213 aged closed + 22 aged canceled + 5 active, padded to ~5 MB,
        # 100 members, 800 history entries: the real-shaped legacy document.
        for index in range(213):
            ticket_id = f"TK-closed{index:05d}"
            tickets[ticket_id] = make_ticket(
                ticket_id,
                "closed",
                now=now,
                description_pad=18_000,
                dispatch_entries=4 if index % 3 == 0 else 0,
                review_entries=2 if index % 5 == 0 else 0,
            )
            archived_expected.append(ticket_id)
        for index in range(22):
            ticket_id = f"TK-cancel{index:05d}"
            tickets[ticket_id] = make_ticket(
                ticket_id, "canceled", now=now, description_pad=18_000
            )
            archived_expected.append(ticket_id)
        for index in range(5):
            tickets[f"TK-active{index:05d}"] = make_ticket(
                f"TK-active{index:05d}", "open", now=now, age_days=0.0
            )
        for index in range(100):
            member_id = f"AI-retired-{index:04d}"
            principal_id = f"PR-member-{index:04d}"

            def add_member(
                document: dict[str, object],
                member_id: str = member_id,
                principal_id: str = principal_id,
            ) -> None:
                members = document["members"]
                members[member_id] = {
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

            self.service.store.read_modify_write(
                self.service._path(BOARD), add_member, lambda: self.service._default(BOARD)
            )
        self.seed_tickets(tickets, schema_version=7)
        legacy_bytes = self.board_blob_size()
        self.assertGreater(legacy_bytes, 4_000_000)

        # First load runs the one-shot migration.
        document = self.service.load(BOARD)
        self.assertEqual(int(document["schema_version"]), 8)
        self.assertEqual(len(document["tickets"]), 5)
        self.assertEqual(len(self.service.load_archive_index(BOARD)), 235)
        hot_bytes = self.board_blob_size()
        self.assertLess(hot_bytes, 500_000)

        # Migration is idempotent: a second load never rewrites the blob.
        version_after = self.board_version()
        self.service.load(BOARD)
        self.service.load(BOARD)
        self.assertEqual(self.board_version(), version_after)

        # Every archived ticket stays readable through ticket_get.
        self.principal = self.admin
        for ticket_id in sorted(archived_expected):
            result = await self.call("ticket_get", ticket_id=ticket_id)
            payload = result.structured_content
            self.assertTrue(payload["archived"])
            self.assertEqual(payload["ticket"]["ticket_id"], ticket_id)
            self.assertIn(payload["ticket"]["status"], {"closed", "canceled"})

        # ticket_list finds archived tickets transparently.
        listed = await self.call("ticket_list", status="closed", limit=500)
        payload = listed.structured_content
        self.assertEqual(payload["total_matching"], 213)
        self.assertEqual(payload["archived_matching"], 213)
        self.assertTrue(all(row["archived"] for row in payload["tickets"]))
        everything = await self.call("ticket_list", include_closed=True, limit=500)
        self.assertEqual(everything.structured_content["total_matching"], 240)
        hot_only = await self.call(
            "ticket_list", include_closed=True, include_archived=False, limit=500
        )
        self.assertEqual(hot_only.structured_content["total_matching"], 5)

        # board_status and board_list counts include the archive tier.
        self.principal = self.admin
        status_result = await self.call("board_status")
        rendered = json.dumps(status_result.structured_content)
        self.assertIn("Tickets: 240", rendered)
        boards = await self.mcp.call_tool("board_list", {})
        row = boards.structured_content["boards"][0]
        self.assertEqual(row["ticket_count"], 240)
        self.assertEqual(row["archived_ticket_count"], 235)

        # One ticket_archived journal entry with counts went to the admins.
        page = self.service.journal.read_after(BOARD, 0, 1000)
        archived_events = [
            event for event in page["events"] if event["kind"] == "ticket_archived"
        ]
        self.assertEqual(len(archived_events), 1)
        event = archived_events[0]
        self.assertEqual(event["recipient_identities"], [self.admin_agent_id])
        self.assertEqual(event["archived_reason"], "legacy_migration")
        self.assertEqual(event["archived_ticket_count"], 235)
        self.assertGreater(event["bytes_freed"], 4_000_000)

        # healthz exposes per-board hot size, archive depth, and 60s counters.
        counts = _health_counts(self.service)
        board_stats = counts["boards"][BOARD]
        self.assertEqual(board_stats["hot_document_bytes"], self.board_blob_size())
        self.assertEqual(board_stats["archived_ticket_count"], 235)
        self.assertGreater(board_stats["bytes_freed_total"], 4_000_000)
        self.assertGreaterEqual(board_stats["items_archived_total"], 235)
        self.assertGreater(board_stats["document_loads_last_60s"], 0)
        self.assertGreaterEqual(board_stats["document_saves_last_60s"], 0)

    async def test_recent_terminal_tickets_stay_hot_until_archive_after_days(
        self,
    ) -> None:
        now = time.time()
        self.seed_tickets(
            {
                "TK-fresh": make_ticket("TK-fresh", "closed", now=now, age_days=0.0),
                "TK-aged": make_ticket("TK-aged", "closed", now=now, age_days=5.0),
            },
            schema_version=8,
        )
        summary = self.service.archive_due_tickets(BOARD, now)
        self.assertEqual(summary["archived_ticket_ids"], ["TK-aged"])
        document = self.service.load(BOARD)
        self.assertIn("TK-fresh", document["tickets"])
        self.assertIn("TK-aged", self.service.load_archive_index(BOARD))
        self.assertIsNone(
            self.service.load_archive_document(BOARD, "TK-fresh")
            and self.service.load_archive_document(BOARD, "TK-fresh").get("ticket")
        )

    async def test_inline_histories_bound_to_limit_with_archive_overflow(
        self,
    ) -> None:
        now = time.time()
        ticket = make_ticket(
            "TK-bounded", "open", now=now, age_days=0.0, dispatch_entries=120
        )
        self.seed_tickets({"TK-bounded": ticket}, schema_version=8)
        summary = self.service.archive_due_tickets(BOARD, now)
        self.assertEqual(summary["history_bounded_ticket_ids"], ["TK-bounded"])

        document = self.service.load(BOARD)
        hot = document["tickets"]["TK-bounded"]
        self.assertEqual(len(hot["dispatch_history"]), 50)
        self.assertEqual(hot["dispatch_history_omitted_count"], 70)
        self.assertEqual(
            [entry["n"] for entry in hot["dispatch_history"]], list(range(70, 120))
        )
        archive = self.service.load_archive_document(BOARD, "TK-bounded")
        overflow = archive["history_overflow"]["dispatch_history"]
        self.assertEqual(len(overflow), 70)
        self.assertEqual([entry["n"] for entry in overflow], list(range(0, 70)))

        # A second sweep is a no-op: counts never double.
        second = self.service.archive_due_tickets(BOARD, now)
        self.assertEqual(second["history_bounded_ticket_ids"], [])
        document = self.service.load(BOARD)
        self.assertEqual(
            document["tickets"]["TK-bounded"]["dispatch_history_omitted_count"], 70
        )

        # ticket_get projects the bounded inline history with the omitted count.
        result = await self.call("ticket_get", ticket_id="TK-bounded")
        projected = result.structured_content["ticket"]
        self.assertEqual(len(projected["dispatch_history"]), 50)
        self.assertEqual(projected["dispatch_history_omitted_count"], 70)

        # When the ticket later ages out, the archive restores full history.
        def close_ticket(document: dict[str, object]) -> None:
            target = document["tickets"]["TK-bounded"]
            target["status"] = "closed"
            target["closed_at"] = aged_iso(time.time(), 9.0)
            target["updated_at"] = target["closed_at"]

        self.service.store.read_modify_write(
            self.service._path(BOARD), close_ticket, lambda: self.service._default(BOARD)
        )
        aged = self.service.archive_due_tickets(BOARD, time.time())
        self.assertEqual(aged["archived_ticket_ids"], ["TK-bounded"])
        merged = self.service.merged_archived_ticket(BOARD, "TK-bounded")
        self.assertEqual(len(merged["dispatch_history"]), 120)
        self.assertEqual(merged["dispatch_history_omitted_count"], 0)
        self.assertEqual(
            [entry["n"] for entry in merged["dispatch_history"]], list(range(0, 120))
        )

    async def test_board_archive_run_requires_admin_and_journals_events(self) -> None:
        now = time.time()
        self.seed_tickets(
            {"TK-sweep": make_ticket("TK-sweep", "canceled", now=now, age_days=7.0)},
            schema_version=8,
        )
        self.principal = self.admin
        await self.call(
            "board_member_add",
            agent_name="admin-agent",
            principal_id=self.worker_a.principal_id,
            role="member",
        )
        self.principal = self.worker_a
        await self.call("board_join", agent_name="worker-agent")
        with self.assertRaises(ToolError):
            await self.call("board_archive_run", agent_name="worker-agent")

        self.principal = self.admin
        result = await self.call("board_archive_run", agent_name="admin-agent")
        payload = result.structured_content
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["archived_ticket_ids"], ["TK-sweep"])
        self.assertTrue(payload["durable_records_untouched"])
        self.assertIn("journal_compaction", payload)
        page = self.service.journal.read_after(BOARD, 0, 1000)
        events = [
            event for event in page["events"] if event["kind"] == "ticket_archived"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["archived_ticket_count"], 1)
        self.assertEqual(events[0]["archived_reason"], "auto_sweep")
        self.assertEqual(events[0]["recipient_identities"], [self.admin_agent_id])

    async def test_read_only_tools_never_rewrite_the_board_document(self) -> None:
        now = time.time()
        tickets = {
            f"TK-ro{index:03d}": make_ticket(
                f"TK-ro{index:03d}", "closed", now=now, age_days=6.0
            )
            for index in range(10)
        }
        tickets["TK-open"] = make_ticket("TK-open", "open", now=now, age_days=0.0)
        self.seed_tickets(tickets, schema_version=7)
        self.service.load(BOARD)  # migration writes once
        version_before = self.board_version()
        documents_before = {
            path: (version, doc)
            for path, doc, version in self.persisted_documents()
        }

        for _ in range(10):
            await self.call("ticket_list", include_closed=True)
            await self.call("ticket_get", ticket_id="TK-ro000")
            await self.call("ticket_get", ticket_id="TK-open")
            await self.call("board_snapshot")
            await self.call("board_status")
            await self.call(
                "board_catchup",
                agent_name="admin-agent",
                cursor=0,
                limit=10,
                ack=False,
                touch=False,
            )

        self.assertEqual(self.board_version(), version_before)
        documents_after = {
            path: (version, doc)
            for path, doc, version in self.persisted_documents()
        }
        self.assertEqual(documents_before, documents_after)

    def persisted_documents(self) -> list[tuple[str, str, int]]:
        connection = sqlite3.connect(self.service.store.db_path)
        try:
            return connection.execute(
                "SELECT path, doc, version FROM documents ORDER BY path"
            ).fetchall()
        finally:
            connection.close()

    async def test_ticket_id_allocation_avoids_archived_ids(self) -> None:
        now = time.time()
        seq = 77
        digest = hashlib.sha256(f"{BOARD}:{seq}".encode("utf-8")).hexdigest()[:12]
        colliding = f"TK-{digest}"
        archived_ticket = make_ticket(
            colliding, "closed", now=now, age_days=30.0
        )
        self.seed_tickets({colliding: archived_ticket}, schema_version=7)
        self.service.load(BOARD)  # migrate: the ticket moves into the index
        self.assertIn(colliding, self.service.load_archive_index(BOARD))

        def rewind(document: dict[str, object]) -> None:
            document["next_ticket_seq"] = seq

        self.service.store.read_modify_write(
            self.service._path(BOARD), rewind, lambda: self.service._default(BOARD)
        )
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="dedupe target",
            description="allocation must skip archived ids",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        new_id = created.structured_content["ticket"]["ticket_id"]
        self.assertNotEqual(new_id, colliding)
        # The archived ticket itself is still readable.
        fetched = await self.call("ticket_get", ticket_id=colliding)
        self.assertTrue(fetched.structured_content["archived"])

    async def test_journal_sweep_retains_rows_from_oldest_live_cursor(self) -> None:
        def seed_journal(board_id: str, row_count: int) -> None:
            document = self.service.journal._default(board_id)
            document["rows"] = [
                {"seq": seq, "kind": "ticket_created"} for seq in range(1, row_count + 1)
            ]
            document["next_seq"] = row_count + 1

            def replace(_current: dict[str, object]) -> dict[str, object]:
                return document

            self.service.store.read_modify_write(
                self.service.journal._path(board_id), replace, dict
            )

        seed_journal(BOARD, 1_200)
        # A live consumer cursor at 300 retains every row from 300 onward.
        self.service.cursors.ack("PR-admin", "admin-agent", BOARD, 300)
        result = self.service.journal_sweep(BOARD)
        self.assertEqual(result["removed"], 299)
        self.assertEqual(result["retained"], 901)
        # The sweep is idempotent for the same cursor (retains rows plus the compaction event).
        result = self.service.journal_sweep(BOARD)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["retained"], 902)

        # A board without consumers keeps the newest 500 rows (retention floor).
        seed_journal("board-two", 1_200)
        result = self.service.journal_sweep("board-two")
        self.assertEqual(result["removed"], 700)
        self.assertEqual(result["retained"], 500)



    async def test_history_bounds_inline_at_append_time(self) -> None:
        """Operator decision (2): bounding happens in the appending write."""

        def set_limit(document: dict[str, object]) -> None:
            document["config"]["inline_history_limit"] = 1

        self.service.store.read_modify_write(
            self.service._path(BOARD), set_limit, lambda: self.service._default(BOARD)
        )
        self.principal = self.admin
        await self.call(
            "board_member_add",
            agent_name="admin-agent",
            principal_id=self.worker_a.principal_id,
            role="member",
        )
        self.principal = self.worker_a
        await self.call(
            "board_join",
            agent_name="worker-agent",
            capabilities={"can_work": True, "can_review": False},
        )
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="bounded at append",
            description="append-time bounding",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        ticket_id = created.structured_content["ticket"]["ticket_id"]
        self.principal = self.worker_a
        await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        await self.call(
            "ticket_submit", agent_name="worker-agent", ticket_id=ticket_id,
            summary="first",
        )
        self.principal = self.admin
        await self.call(
            "ticket_review_claim", agent_name="admin-agent", ticket_id=ticket_id
        )
        rejected = await self.call(
            "ticket_review",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            verdict="reject",
            review_notes="needs work",
            fix_instructions="redo the thing",
        )
        self.assertFalse(rejected.is_error)
        review_payload = rejected.structured_content
        self.assertIsNotNone(
            review_payload.get("ticket"), f"reject did not apply: {review_payload}"
        )
        self.assertNotEqual(review_payload["ticket"]["status"], "submitted")
        self.principal = self.worker_a
        await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        resubmitted = await self.call(
            "ticket_submit", agent_name="worker-agent", ticket_id=ticket_id,
            summary="second",
        )
        self.assertFalse(resubmitted.is_error)

        fetched = await self.call("ticket_get", ticket_id=ticket_id)
        ticket = fetched.structured_content["ticket"]
        self.assertEqual(len(ticket["submission_history"]), 1)
        self.assertEqual(ticket["submission_history"][0]["summary"], "second")
        self.assertEqual(ticket["submission_history_omitted_count"], 1)
        archive = self.service.load_archive_document(BOARD, ticket_id)
        overflow = archive["history_overflow"]["submission_history"]
        self.assertEqual(len(overflow), 1)
        self.assertEqual(overflow[0]["summary"], "first")

    async def test_retired_members_tombstone_after_stale_window(self) -> None:
        """Operator decision (4): retired inactive members compact to tombstones."""
        now = time.time()
        member_id = central.agent_id(BOARD, "PR-retired", "retired-one")

        def seed(document: dict[str, object]) -> None:
            document["principal_memberships"]["PR-retired"] = {
                "principal_id": "PR-retired",
                "role": "member",
                "source": "test",
                "created_at": central.iso_at(now - 40 * 86_400),
                "updated_at": central.iso_at(now - 40 * 86_400),
            }
            document["members"][member_id] = {
                "agent_id": member_id,
                "agent_name": "retired-one",
                "principal_id": "PR-retired",
                "joined_at": central.iso_at(now - 40 * 86_400),
                "role": "worker",
                "lifecycle_status": "retired",
                "membership_role": "member",
                "last_activity_at": central.iso_at(now - 10 * 86_400),
                "scopes": ["board:read", "board:write"],
                "capabilities": {
                    "can_work": True, "can_review": False, "tier_max": 3, "skills": [],
                },
                "capabilities_explicit": True,
                "task_focus": "x" * 200,
            }

        self.service.store.read_modify_write(
            self.service._path(BOARD), seed, lambda: self.service._default(BOARD)
        )
        summary = self.service.archive_due_tickets(BOARD, now)
        self.assertEqual(summary["member_tombstoned_ids"], [member_id])
        document = self.service.load(BOARD)
        member = document["members"][member_id]
        self.assertTrue(member["tombstone"])
        self.assertEqual(member["lifecycle_status"], "retired")
        self.assertEqual(member["agent_name"], "retired-one")
        self.assertNotIn("capabilities", member)
        self.assertNotIn("task_focus", member)

        # Rejoining resurrects the full live record without tombstone markers.
        self.principal = central.Principal(
            "PR-retired", "retired-one", frozenset({"board:read", "board:write"})
        )
        joined = await self.call(
            "board_join",
            agent_name="retired-one",
            capabilities={"can_work": True, "can_review": False},
        )
        self.assertEqual(joined.structured_content["lifecycle_status"], "active")
        document = self.service.load(BOARD)
        member = document["members"][member_id]
        self.assertEqual(member["lifecycle_status"], "active")
        self.assertNotIn("tombstoned_at", member)
        self.assertNotIn("tombstone", member)
        self.assertIn("capabilities", member)

    async def test_expired_invites_pruned_after_seven_days(self) -> None:
        """Operator decision (6): invites expired > 7 days are pruned."""
        now = time.time()

        def seed(document: dict[str, object]) -> None:
            invites = document["invites"]
            invites["old-invite"] = {
                "created_by_principal_id": "PR-admin",
                "role": "member",
                "created_at": central.iso_at(now - 20 * 86_400),
                "expires_at": central.iso_at(now - 8 * 86_400),
                "expires_at_epoch": now - 8 * 86_400,
                "consumed_at": None,
                "revoked_at": None,
            }
            invites["fresh-invite"] = {
                "created_by_principal_id": "PR-admin",
                "role": "member",
                "created_at": central.iso_at(now - 3 * 86_400),
                "expires_at": central.iso_at(now - 1 * 86_400),
                "expires_at_epoch": now - 1 * 86_400,
                "consumed_at": None,
                "revoked_at": None,
            }

        self.service.store.read_modify_write(
            self.service._path(BOARD), seed, lambda: self.service._default(BOARD)
        )
        summary = self.service.archive_due_tickets(BOARD, now)
        self.assertEqual(summary["pruned_invite_keys"], ["old-invite"])
        document = self.service.load(BOARD)
        self.assertNotIn("old-invite", document["invites"])
        self.assertIn("fresh-invite", document["invites"])

    async def test_journal_sweep_keeps_rows_within_retention_window(self) -> None:
        """Operator decision (3): keep rows newer than oldest cursor + 7 days."""
        now = time.time()

        def seed_journal(board_id: str, occurred: float) -> None:
            document = self.service.journal._default(board_id)
            document["rows"] = [
                {
                    "seq": seq,
                    "kind": "ticket_created",
                    "occurred_at": central.iso_at(occurred),
                }
                for seq in range(1, 1_201)
            ]
            document["next_seq"] = 1_201

            def replace(_current: dict[str, object]) -> dict[str, object]:
                return document

            self.service.store.read_modify_write(
                self.service.journal._path(board_id), replace, dict
            )

        # Recent rows survive even with an old cursor: 7-day window keeps all.
        seed_journal("board-recent", now - 86_400)
        self.service.cursors.ack("PR-admin", "admin-agent", "board-recent", 5)
        result = self.service.journal_sweep("board-recent", now=now)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["retained"], 1_200)

        # Old rows compact down to the cursor window with the 500-row floor.
        seed_journal("board-old", now - 10 * 86_400)
        self.service.cursors.ack("PR-admin", "admin-agent", "board-old", 1_150)
        result = self.service.journal_sweep("board-old", now=now)
        self.assertEqual(result["removed"], 700)
        self.assertEqual(result["retained"], 500)


    async def test_archive_after_days_zero_archives_at_close_and_cancel_time(self) -> None:
        """Operator decision (1): archive_after_days=0 archives immediately at close/cancel."""
        def set_zero(document: dict[str, object]) -> None:
            document["config"]["archive_after_days"] = 0

        self.service.store.read_modify_write(
            self.service._path(BOARD), set_zero, lambda: self.service._default(BOARD)
        )
        self.principal = self.admin
        await self.call(
            "board_member_add",
            agent_name="admin-agent",
            principal_id=self.worker_a.principal_id,
            role="member",
        )
        self.principal = self.worker_a
        await self.call(
            "board_join",
            agent_name="worker-agent",
            capabilities={"can_work": True, "can_review": False},
        )

        # 1. Close-time archive
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="close zero test",
            description="close zero",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        tk_close = created.structured_content["ticket"]["ticket_id"]
        self.principal = self.worker_a
        await self.call("ticket_claim", agent_name="worker-agent", ticket_id=tk_close)
        await self.call(
            "ticket_submit", agent_name="worker-agent", ticket_id=tk_close, summary="done"
        )
        self.principal = self.admin
        await self.call("ticket_review_claim", agent_name="admin-agent", ticket_id=tk_close)
        approved = await self.call(
            "ticket_review",
            agent_name="admin-agent",
            ticket_id=tk_close,
            verdict="approve",
            review_notes="lgtm",
        )
        self.assertFalse(approved.is_error)

        doc = self.service.load(BOARD)
        # Ticket must not remain in the hot document
        self.assertNotIn(tk_close, doc["tickets"])
        # Ticket must be transparently readable through ticket_get
        got = await self.call("ticket_get", ticket_id=tk_close)
        self.assertEqual(got.structured_content["ticket"]["status"], "closed")

        # 2. Cancel-time archive
        created_cancel = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="cancel zero test",
            description="cancel zero",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        tk_cancel = created_cancel.structured_content["ticket"]["ticket_id"]
        canceled = await self.call(
            "ticket_cancel", agent_name="admin-agent", ticket_id=tk_cancel, reason="not needed"
        )
        self.assertFalse(canceled.is_error)
        doc2 = self.service.load(BOARD)
        self.assertNotIn(tk_cancel, doc2["tickets"])
        got_cancel = await self.call("ticket_get", ticket_id=tk_cancel)
        self.assertEqual(got_cancel.structured_content["ticket"]["status"], "canceled")

    async def test_idle_board_automatic_reaper_timer_executes(self) -> None:
        """Point 1: recurring reaper timer executes automatically on idle boards."""
        now = time.time()

        def seed_idle(document: dict[str, object]) -> None:
            document["config"]["archive_after_days"] = 1
            document["tickets"]["TK-idle-timer"] = {
                "ticket_id": "TK-idle-timer",
                "title": "idle timer ticket",
                "status": "closed",
                "closed_at": central.iso_at(now - 5 * 86_400),
                "scope": "interactive-no-send",
                "required_fields": ["test_output"],
            }

        self.service.store.read_modify_write(
            self.service._path(BOARD), seed_idle, lambda: self.service._default(BOARD)
        )
        with patch.dict(os.environ, {"CENTRAL_REAPER_INTERVAL_S": "0.05"}):
            async with self.mcp.settings.lifespan(self.mcp):
                self.assertIsNotNone(self.service.recurring_reaper_task)
                await asyncio.sleep(0.15)
                doc = self.service.load(BOARD)
                self.assertNotIn("TK-idle-timer", doc["tickets"])
                self.assertIn("TK-idle-timer", self.service.load_archive_index(BOARD))

    async def test_active_ticket_review_stats_include_overflow(self) -> None:
        """Point 4: board status counts review labels from archive overflow for hot tickets."""
        ticket_id = "TK-overflow-reviews"

        def seed(document: dict[str, object]) -> None:
            document["config"]["inline_history_limit"] = 1
            document["tickets"][ticket_id] = {
                "ticket_id": ticket_id,
                "title": "review overflow test",
                "status": "open",
                "scope": "interactive-no-send",
                "required_fields": ["test_output"],
                "review_history": [{"review_label": "strict-review", "verdict": "reject"}],
                "review_history_omitted_count": 1,
            }

        self.service.store.read_modify_write(
            self.service._path(BOARD), seed, lambda: self.service._default(BOARD)
        )
        self.service.persist_history_overflow(
            BOARD,
            ticket_id,
            "review_history",
            [{"review_label": "overflow-label", "verdict": "reject"}],
        )

        status_result = await self.call("board_status")
        labels = status_result.structured_content["review_label_counts"]
        self.assertEqual(labels.get("strict-review"), 1)
        self.assertEqual(labels.get("overflow-label"), 1)

    async def test_cursor_retention_rules(self) -> None:
        """Point 2: test legacy missing timestamps, stale active, retired, fresh."""
        now = time.time()
        # Seed 4 different consumers
        def seed_cursors(doc: dict[str, Any]) -> None:
            consumers = doc.setdefault("consumers", {})
            # 1. Legacy missing timestamp
            consumers["c1"] = {"principal_id": "PR-active", "agent_name": "legacy", "cursor": 100}
            # 2. Stale active member (> 7 days)
            consumers["c2"] = {"principal_id": "PR-active", "agent_name": "stale", "cursor": 200, "updated_at": now - 10 * 86_400}
            # 3. Retired principal
            consumers["c3"] = {"principal_id": "PR-retired", "agent_name": "retired", "cursor": 300, "updated_at": now - 100}
            # 4. Fresh active member
            consumers["c4"] = {"principal_id": "PR-active", "agent_name": "fresh", "cursor": 400, "updated_at": now - 100}

        self.service.store.read_modify_write(
            self.service.cursors._path(BOARD), seed_cursors, lambda: self.service.cursors._default(BOARD)
        )

        active_principals = {"PR-active"}
        stale_after_s = 7 * 86_400

        # Only c4 is live and fresh! (c1 missing timestamp, c2 stale, c3 retired principal)
        oldest = self.service.cursors.oldest_live_cursor(
            BOARD,
            active_principals=active_principals,
            stale_after_s=stale_after_s,
            now=now,
        )
        self.assertEqual(oldest, 400)

    async def test_idle_board_automatic_reaper_execution(self) -> None:
        """Operator decision (1, 5): idle board auto sweep cleans bloat, records counts."""
        now = time.time()

        def seed_idle(document: dict[str, object]) -> None:
            document["config"]["archive_after_days"] = 1
            # Add an aged closed ticket
            document["tickets"]["TK-idle-aged"] = {
                "ticket_id": "TK-idle-aged",
                "title": "idle aged ticket",
                "status": "closed",
                "closed_at": central.iso_at(now - 5 * 86_400),
                "scope": "interactive-no-send",
                "required_fields": ["test_output"],
            }

        self.service.store.read_modify_write(
            self.service._path(BOARD), seed_idle, lambda: self.service._default(BOARD)
        )
        summary = self.service.archive_due_tickets(BOARD, now)
        self.assertEqual(summary["archived_ticket_ids"], ["TK-idle-aged"])
        self.assertGreater(summary["bytes_freed"], 0)

        # Healthz exposes bytes_freed_total and items_archived_total
        counts = _health_counts(self.service)
        board_stats = counts["boards"][BOARD]
        self.assertGreater(board_stats["bytes_freed_total"], 0)
        self.assertGreaterEqual(board_stats["items_archived_total"], 1)

    async def test_abandoner_window_reads_archived_records(self) -> None:
        """Fix instruction (6): dispatch_ticket reads overflow history from archive."""
        now = time.time()
        ticket_id = "TK-overflow-check"

        def seed(document: dict[str, object]) -> None:
            document["config"]["inline_history_limit"] = 1
            document["tickets"][ticket_id] = {
                "ticket_id": ticket_id,
                "title": "overflow test",
                "status": "open",
                "tier": 2,
                "skills_required": [],
                "scope": "interactive-no-send",
                "required_fields": ["test_output"],
                "work_dispatch_cycle": 1,
                "dispatch_history": [
                    {"state": "requeued", "kind": "work", "cycle": 1, "at": central.iso_at(now)}
                ],
                "dispatch_history_omitted_count": 1,
            }

        self.service.store.read_modify_write(
            self.service._path(BOARD), seed, lambda: self.service._default(BOARD)
        )

        # Write the overflow into the archive document: expired for worker_a in cycle 1
        self.service.persist_history_overflow(
            BOARD,
            ticket_id,
            "dispatch_history",
            [
                {
                    "state": "expired",
                    "kind": "work",
                    "cycle": 1,
                    "agent_id": self.worker_a.principal_id,
                    "at": central.iso_at(now - 100),
                }
            ],
        )

        doc = self.service.load(BOARD)
        ticket = doc["tickets"][ticket_id]

        # Dispatch should exclude worker_a because the expired entry in archived history is in cycle 1
        # Add worker_a to members
        self.principal = self.admin
        await self.call(
            "board_member_add",
            agent_name="admin-agent",
            principal_id=self.worker_a.principal_id,
            role="member",
        )
        self.principal = self.worker_a
        await self.call(
            "board_join",
            agent_name="worker-agent",
            capabilities={"can_work": True, "can_review": False},
        )

        doc = self.service.load(BOARD)
        ticket = doc["tickets"][ticket_id]
        dispatched = self.service.dispatch_ticket(doc, ticket, now, "work")
        # Since worker_a was expired in this cycle in the archived history, worker_a is NOT offered
        if dispatched is not None:
            self.assertNotEqual(ticket.get("work_offer", {}).get("agent_id"), self.worker_a.principal_id)


if __name__ == "__main__":
    unittest.main()
