from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402


class ResponseBoundsTests(unittest.IsolatedAsyncioTestCase):
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
        self.principal = central.Principal(
            "PR-admin",
            "admin-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        joined = await self.call("board_join", agent_name="admin-agent")
        self.assertFalse(joined.is_error)
        self.agent_id = central.agent_id(
            "pursers", self.principal.principal_id, "admin-agent"
        )

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name,
            {"board_id": "pursers", **arguments},
        )

    def seed_fat_briefing(self) -> None:
        def mutate(document: dict[str, object]) -> None:
            tickets = document["tickets"]
            memories = document["memories"]
            assert isinstance(tickets, dict)
            assert isinstance(memories, list)
            for index in range(25):
                ticket_id = f"TK-fat-{index:03d}"
                tickets[ticket_id] = {
                    "ticket_id": ticket_id,
                    "title": f"ticket {index} " + "t" * 180,
                    "description": "d" * 20_000,
                    "status": "open",
                    "priority": "medium",
                    "claimed_by": None,
                    "updated_at": f"2026-08-26T00:{index:02d}:00+00:00",
                    "submission_history": [{"summary": "s" * 20_000}],
                    "review_history": [{"review_notes": "r" * 20_000}],
                }
            memories.append(
                {
                    "memory_id": "MEM-handoff",
                    "title": "large handoff",
                    "content": "h" * 20_000,
                    "summary": "summary " + "q" * 5_000,
                    "scope": "project",
                    "author_principal_id": self.principal.principal_id,
                    "author_agent_id": "AI-source",
                    "author_agent_name": "source-agent",
                    "memory_type": "handoff",
                    "priority": 3,
                    "pinned": True,
                    "created_at_epoch": 100.0,
                    "next_steps": ["n" * 1_000 for _ in range(20)],
                    "files": ["f" * 500 for _ in range(30)],
                    "warnings": ["w" * 500 for _ in range(30)],
                    "legacy_record": {"content": "l" * 400_000},
                }
            )
            for index in range(9):
                memories.append(
                    {
                        "memory_id": f"MEM-pinned-{index}",
                        "title": f"large pinned {index}",
                        "content": "p" * 20_000,
                        "scope": "project",
                        "author_principal_id": self.principal.principal_id,
                        "author_agent_id": "AI-source",
                        "author_agent_name": "source-agent",
                        "memory_type": "decision",
                        "priority": 3,
                        "pinned": True,
                        "created_at_epoch": float(index),
                        "related_files": ["x" * 500 for _ in range(30)],
                        "legacy_record": {"content": "l" * 400_000},
                    }
                )

        self.service.mutate("pursers", mutate)

    def seed_fat_journal(self, event_count: int = 240) -> tuple[int, list[dict]]:
        start = self.service.journal.read_after("pursers", 0, 1)["latest_cursor"]
        recipients = [self.agent_id] + [
            f"AI-padding-{index:060d}" for index in range(64)
        ]
        events = []
        for index in range(event_count):
            events.append(
                self.service.journal.append(
                    "pursers",
                    {
                        "kind": "ticket_created",
                        "actor": "AI-source",
                        "payload_ref": f"board://pursers/ticket/TK-event-{index:04d}",
                        "ticket_id": f"TK-event-{index:04d}",
                        "status_to": "open",
                        "recipient_identities": recipients,
                    },
                )
            )
        return int(start), events

    async def test_briefing_returns_compact_bounded_payloads(self) -> None:
        self.seed_fat_briefing()
        document = self.service.load("pursers")
        briefing_before_bytes = len(
            json.dumps(
                {
                    "open_tickets": list(document["tickets"].values()),
                    "latest_handoff": document["memories"][0],
                    "pinned_digest": document["memories"][1:9],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        )

        result = await self.call("board_get_briefing", token_budget=256)

        self.assertFalse(result.is_error)
        payload = result.structured_content
        self.assertEqual(len(payload["open_tickets"]), 20)
        self.assertEqual(payload["omitted_open_tickets"], 5)
        self.assertEqual(payload["payload_omitted_counts"]["open_tickets"], 5)
        self.assertEqual(payload["payload_omitted_counts"]["pinned_digest"], 1)
        self.assertEqual(payload["payload_bounds"]["open_tickets"], 20)
        self.assertEqual(payload["payload_bounds"]["memory_content_chars"], 2_000)
        self.assertTrue(payload["payload_truncated"])
        for ticket in payload["open_tickets"]:
            self.assertLessEqual(len(ticket["title"]), 120)
            self.assertNotIn("description", ticket)
            self.assertNotIn("submission_history", ticket)
            self.assertNotIn("review_history", ticket)
        self.assertEqual(len(payload["pinned_digest"]), 8)
        for memory in [payload["latest_handoff"], *payload["pinned_digest"]]:
            self.assertTrue(memory["content_truncated"])
            self.assertLessEqual(len(memory["content"]), 2_000)
            self.assertNotIn("legacy_record", memory)
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        self.assertGreater(briefing_before_bytes, 800_000)
        self.assertLess(len(serialized), 800_000)
        print(
            "fat-briefing bytes: "
            f"before={briefing_before_bytes} after={len(serialized)}"
        )

    async def test_catchup_pages_fat_journal_losslessly_under_byte_ceiling(
        self,
    ) -> None:
        cursor, seeded = self.seed_fat_journal()
        before_bytes = len(
            json.dumps(
                {"events": seeded}, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
        )
        self.assertGreater(before_bytes, 800_000)

        received: list[dict] = []
        page_sizes: list[int] = []
        pages = 0
        while True:
            result = await self.call(
                "board_catchup",
                agent_name="admin-agent",
                cursor=cursor,
                limit=1_000,
                max_events=200,
                max_bytes=300_000,
                ack=False,
            )
            self.assertFalse(result.is_error)
            payload = result.structured_content
            serialized = json.dumps(
                payload, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            page_sizes.append(len(serialized))
            self.assertLessEqual(len(serialized), 300_000)
            self.assertEqual(payload["new_seq"], payload["next_cursor"])
            self.assertEqual(payload["bounds"]["max_events"], 200)
            self.assertEqual(payload["bounds"]["max_bytes"], 300_000)
            self.assertEqual(
                payload["returned_counts"]["events"], len(payload["events"])
            )
            self.assertEqual(
                payload["total_counts"]["events"],
                payload["returned_counts"]["events"]
                + payload["omitted_counts"]["events"],
            )
            received.extend(payload["events"])
            cursor = payload["next_cursor"]
            pages += 1
            self.assertLess(pages, 20)
            if not payload["has_more"]:
                break

        self.assertEqual(
            [event["seq"] for event in received],
            [event["seq"] for event in seeded],
        )
        print(
            "fat-board bytes: "
            f"before={before_bytes} max_after={max(page_sizes)} "
            f"pages={pages} events={len(received)}"
        )

    async def test_catchup_rejects_invalid_event_and_byte_bounds(self) -> None:
        invalid = (
            {"max_events": 0},
            {"max_events": 1_001},
            {"max_bytes": 4_095},
            {"max_bytes": 750_001},
        )
        for bounds in invalid:
            with self.subTest(bounds=bounds), self.assertRaisesRegex(
                Exception, "must be between"
            ):
                await self.call(
                    "board_catchup",
                    agent_name="admin-agent",
                    cursor=0,
                    ack=False,
                    **bounds,
                )


if __name__ == "__main__":
    unittest.main()
