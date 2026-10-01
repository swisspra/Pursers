"""Contract tests for application ticket and retained-history pagination."""

from __future__ import annotations

import os
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


def ticket(ticket_id: str, priority: str = "medium", **extra: object) -> dict[str, object]:
    now = central.iso_at(time.time())
    value: dict[str, object] = {
        "ticket_id": ticket_id,
        "title": ticket_id,
        "description": "",
        "status": "open",
        "priority": priority,
        "tier": 2,
        "scope": "interactive-no-send",
        "created_at": now,
        "updated_at": now,
        "annotations": [],
        "dispatch_history": [],
        "submission_history": [],
        "review_history": [],
        "progress_history": [],
    }
    value.update(extra)
    return value


class TicketPaginationTests(unittest.IsolatedAsyncioTestCase):
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
        scopes = frozenset({"board:read", "board:write", "board:review"})
        self.admin = central.Principal("PR-admin", "admin", scopes)
        self.worker = central.Principal(
            "PR-worker", "worker", frozenset({"board:read", "board:write"})
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call(
            "board_join",
            agent_name="admin-agent",
            role="reviewer",
            capabilities={"can_work": False, "can_review": True},
        )

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(name, {"board_id": BOARD, **arguments})

    def seed(self, values: list[dict[str, object]]) -> None:
        def mutate(document: dict[str, object]) -> None:
            tickets = document["tickets"]
            assert isinstance(tickets, dict)
            tickets.update({str(item["ticket_id"]): item for item in values})

        self.service.mutate(BOARD, mutate)

    def add_worker(self) -> str:
        worker_agent_id = central.agent_id(
            BOARD, self.worker.principal_id, "worker-agent"
        )

        def mutate(document: dict[str, object]) -> None:
            now = central.iso_at(time.time())
            document["principal_memberships"][self.worker.principal_id] = {
                "principal_id": self.worker.principal_id,
                "role": "member",
                "source": "test",
                "created_at": now,
                "updated_at": now,
            }
            document["members"][worker_agent_id] = {
                "agent_id": worker_agent_id,
                "agent_name": "worker-agent",
                "principal_id": self.worker.principal_id,
                "role": "worker",
                "membership_role": "member",
                "lifecycle_status": "active",
                "joined_at": now,
                "capabilities": {"can_work": True, "can_review": False},
            }

        self.service.mutate(BOARD, mutate)
        return worker_agent_id

    async def test_quiescent_keyset_traversal_is_complete_and_deterministic(self) -> None:
        self.seed(
            [
                ticket("TK-c", "critical"),
                ticket("TK-h2", "high"),
                ticket("TK-h1", "high"),
                ticket("TK-m2"),
                ticket("TK-m1"),
                ticket("TK-l", "low"),
            ]
        )
        cursor = None
        found: list[str] = []
        while True:
            result = (
                await self.call(
                    "ticket_list", limit=2, view="summary", cursor=cursor
                )
            ).structured_content
            self.assertEqual(result["read_consistency"], "live")
            self.assertLessEqual(result["returned_count"], 2)
            found.extend(row["ticket_id"] for row in result["tickets"])
            cursor = result["next_cursor"]
            self.assertEqual(result["has_more"], cursor is not None)
            if cursor is None:
                break
        self.assertEqual(found, ["TK-c", "TK-h1", "TK-h2", "TK-m1", "TK-m2", "TK-l"])
        self.assertEqual(len(found), len(set(found)))

    async def test_cursor_is_integrity_filter_and_principal_bound(self) -> None:
        self.seed([ticket("TK-a"), ticket("TK-b"), ticket("TK-c")])
        first = (await self.call("ticket_list", limit=1)).structured_content
        cursor = first["next_cursor"]
        self.assertIsInstance(cursor, str)
        tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        with self.assertRaisesRegex(Exception, "integrity check failed"):
            await self.call("ticket_list", limit=1, cursor=tampered)
        with self.assertRaisesRegex(Exception, "filter set"):
            await self.call("ticket_list", status="open", limit=1, cursor=cursor)

        self.add_worker()
        self.principal = self.worker
        with self.assertRaisesRegex(Exception, "principal"):
            await self.call("ticket_list", limit=1, cursor=cursor)

    async def test_authorization_is_rechecked_between_pages(self) -> None:
        self.seed([ticket("TK-a"), ticket("TK-b")])
        worker_agent_id = self.add_worker()
        self.principal = self.worker
        first = (await self.call("ticket_list", limit=1)).structured_content

        def revoke(document: dict[str, object]) -> None:
            document["members"].pop(worker_agent_id)
            document["principal_memberships"].pop(self.worker.principal_id)

        self.service.mutate(BOARD, revoke)
        with self.assertRaisesRegex(Exception, "board access denied"):
            await self.call("ticket_list", limit=1, cursor=first["next_cursor"])

    async def test_archive_page_hydrates_only_returned_rows(self) -> None:
        old = central.iso_at(time.time() - 10 * 86_400)
        rows = [
            ticket(
                f"TK-old{index}",
                status="closed",
                closed_at=old,
                updated_at=old,
                description="x" * 1_000,
            )
            for index in range(7)
        ]
        self.seed(rows)
        self.service.archive_due_tickets(BOARD, time.time())
        page = (
            await self.call(
                "ticket_list", status="closed", include_closed=True, limit=2
            )
        ).structured_content
        self.assertEqual(page["total_matching"], 7)
        self.assertEqual(page["archive_hydrated_count"], 2)
        self.assertTrue(page["has_more"])

    async def test_live_consistency_exposes_concurrent_priority_change(self) -> None:
        self.seed([ticket("TK-a", "high"), ticket("TK-b"), ticket("TK-c", "low")])
        first = (await self.call("ticket_list", limit=1)).structured_content

        def reprioritize(document: dict[str, object]) -> None:
            document["tickets"]["TK-c"]["priority"] = "critical"

        self.service.mutate(BOARD, reprioritize)
        second = (
            await self.call("ticket_list", limit=10, cursor=first["next_cursor"])
        ).structured_content
        self.assertEqual(second["read_consistency"], "live")
        self.assertNotIn("TK-c", [row["ticket_id"] for row in second["tickets"]])
        refreshed = (await self.call("ticket_list", limit=10)).structured_content
        self.assertEqual(refreshed["tickets"][0]["ticket_id"], "TK-c")

    async def test_oversized_full_item_returns_retrievable_summary(self) -> None:
        self.seed([ticket("TK-fat", description="x" * 210_000)])
        result = (
            await self.call("ticket_list", limit=10, view="full")
        ).structured_content
        row = result["tickets"][0]
        self.assertTrue(row["oversized_for_list"])
        self.assertIn("description", row["omitted_sections"])
        self.assertEqual(row["detail_ref"], "board://pursers/ticket/TK-fat")
        stored = self.service.load(BOARD)["tickets"]["TK-fat"]
        self.assertEqual(len(stored["description"]), 210_000)

    async def test_retained_history_pages_oldest_to_newest_with_boundaries(self) -> None:
        values = [
            {"annotation_id": f"AN-{index}", "kind": "note", "text": str(index), "at": f"t{index}"}
            for index in range(6)
        ]
        row = ticket(
            "TK-history",
            annotations=values[3:],
            annotations_omitted_count=3,
        )
        self.seed([row])
        self.service.persist_history_overflow(
            BOARD, "TK-history", "annotations", values[:3]
        )
        cursor = None
        found: list[str] = []
        while True:
            page = (
                await self.call(
                    "ticket_history_list",
                    ticket_id="TK-history",
                    history="annotations",
                    limit=2,
                    cursor=cursor,
                )
            ).structured_content
            found.extend(entry["entry_id"] for entry in page["entries"])
            self.assertTrue(page["retention_complete"])
            self.assertEqual(page["archived_count"], 3)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(found, [f"AN-{index}" for index in range(6)])

    async def test_missing_history_overflow_is_reported_not_reconstructed(self) -> None:
        self.seed(
            [
                ticket(
                    "TK-gap",
                    review_history=[{"review_id": "RV-new", "at": "new"}],
                    review_history_omitted_count=4,
                )
            ]
        )
        page = (
            await self.call(
                "ticket_history_list",
                ticket_id="TK-gap",
                history="reviews",
            )
        ).structured_content
        self.assertFalse(page["retention_complete"])
        self.assertEqual(page["unavailable_before_count"], 4)
        self.assertEqual([entry["entry_id"] for entry in page["entries"]], ["RV-new"])


if __name__ == "__main__":
    unittest.main()
