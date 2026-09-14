from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from mcp import Client


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

    async def protocol_call(self, name: str, **arguments: object):
        async with Client(self.mcp, mode="2026-07-28", cache=None) as client:
            return await client.call_tool(
                name,
                {"board_id": "pursers", **arguments},
            )

    async def create_fat_ticket(self) -> str:
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="response projection target",
            description="d" * 5_000,
            target_url="pursers/packages/central",
            scope="interactive",
            required_fields=["test-output"],
            unassigned=True,
        )
        ticket_id = created.structured_content["ticket"]["ticket_id"]

        def fatten(document: dict[str, object]) -> None:
            ticket = document["tickets"][ticket_id]
            ticket["submission_history"] = [
                {"summary": f"submission-{index}-" + "s" * 3_000}
                for index in range(20)
            ]
            ticket["review_history"] = [
                {"review_notes": f"review-{index}-" + "r" * 3_000}
                for index in range(20)
            ]

        self.service.mutate("pursers", fatten)
        return ticket_id

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

    async def test_onboard_bounds_fat_snapshot_under_byte_ceiling(self) -> None:
        self.seed_fat_briefing()

        result = await self.call(
            "board_onboard",
            agent_name="admin-agent",
            allow_takeover=True,
            token_budget=256,
        )

        self.assertFalse(result.is_error)
        payload = result.structured_content
        snapshot = payload["snapshot"]
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        snapshot_bytes = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        self.assertLess(len(serialized), 800_000)
        self.assertLessEqual(
            len(snapshot_bytes), central.DEFAULT_SNAPSHOT_MAX_BYTES
        )
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(
            snapshot["bounds"],
            {
                "limit_per_collection": central.DEFAULT_SNAPSHOT_LIMIT,
                "max_bytes": central.DEFAULT_SNAPSHOT_MAX_BYTES,
            },
        )
        self.assertEqual(snapshot["total_counts"]["tickets"], 25)
        self.assertGreater(snapshot["omitted_counts"]["tickets"], 0)
        for name, total in snapshot["total_counts"].items():
            self.assertEqual(
                total,
                snapshot["returned_counts"][name]
                + snapshot["omitted_counts"][name],
            )
        self.assertFalse(snapshot["memories_included"])
        self.assertEqual(snapshot["latest_seq"], payload["briefing"]["latest_seq"])
        datetime.fromisoformat(snapshot["snapshot_at"])
        print(
            "fat-onboard bytes: "
            f"after={len(serialized)} snapshot={len(snapshot_bytes)}"
        )

    async def test_onboard_small_snapshot_preserves_all_collections(self) -> None:
        onboard = await self.call(
            "board_onboard",
            agent_name="admin-agent",
            allow_takeover=True,
            snapshot_limit=10,
            snapshot_max_bytes=100_000,
        )
        expected = await self.call(
            "board_snapshot",
            limit=10,
            max_bytes=100_000,
        )

        self.assertFalse(onboard.is_error)
        self.assertFalse(expected.is_error)
        snapshot = onboard.structured_content["snapshot"]
        expected_snapshot = expected.structured_content
        for name in ("board", "agents", "tickets", "state"):
            self.assertEqual(snapshot[name], expected_snapshot[name])
        self.assertFalse(snapshot["truncated"])
        self.assertTrue(
            all(count == 0 for count in snapshot["omitted_counts"].values())
        )
        self.assertEqual(snapshot["total_counts"], snapshot["returned_counts"])
        self.assertFalse(snapshot["memories_included"])
        self.assertIsInstance(snapshot["latest_seq"], int)
        datetime.fromisoformat(snapshot["snapshot_at"])

    async def test_onboard_rejects_invalid_snapshot_bounds(self) -> None:
        invalid = (
            {"snapshot_limit": -1},
            {"snapshot_limit": 1_001},
            {"snapshot_max_bytes": 4_095},
            {"snapshot_max_bytes": 750_001},
        )
        for bounds in invalid:
            with self.subTest(bounds=bounds), self.assertRaisesRegex(
                Exception, "must be between"
            ):
                await self.call(
                    "board_onboard",
                    agent_name="admin-agent",
                    **bounds,
                )

    async def test_onboard_applies_custom_snapshot_bounds(self) -> None:
        self.seed_fat_briefing()

        result = await self.call(
            "board_onboard",
            agent_name="admin-agent",
            allow_takeover=True,
            token_budget=256,
            snapshot_limit=2,
            snapshot_max_bytes=100_000,
        )

        self.assertFalse(result.is_error)
        snapshot = result.structured_content["snapshot"]
        serialized = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        self.assertLessEqual(len(serialized), 100_000)
        self.assertEqual(
            snapshot["bounds"],
            {"limit_per_collection": 2, "max_bytes": 100_000},
        )
        self.assertLessEqual(snapshot["returned_counts"]["tickets"], 2)
        self.assertGreater(snapshot["omitted_counts"]["tickets"], 0)

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

    async def test_catchup_has_a_byte_stable_prefix_before_dynamic_events(
        self,
    ) -> None:
        cursor = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]
        first_event = self.service.journal.append(
            "pursers",
            {
                "kind": "ticket_created",
                "actor": "AI-source",
                "payload_ref": "board://pursers/ticket/TK-stable-1",
                "ticket_id": "TK-stable-1",
                "status_to": "open",
                "recipient_identities": [self.agent_id],
            },
        )
        first = await self.call(
            "board_catchup",
            agent_name="admin-agent",
            cursor=cursor,
            ack=False,
        )
        repeated = await self.call(
            "board_catchup",
            agent_name="admin-agent",
            cursor=cursor,
            ack=False,
        )
        self.assertFalse(first.is_error)
        self.assertFalse(repeated.is_error)
        first_bytes = json.dumps(first.structured_content, ensure_ascii=False)
        repeated_bytes = json.dumps(repeated.structured_content, ensure_ascii=False)
        self.assertEqual(first_bytes, repeated_bytes)
        self.assertEqual(
            list(first.structured_content)[:4],
            ["ok", "board_id", "bounds", "events"],
        )

        self.service.journal.append(
            "pursers",
            {
                "kind": "ticket_created",
                "actor": "AI-source",
                "payload_ref": "board://pursers/ticket/TK-stable-2",
                "ticket_id": "TK-stable-2",
                "status_to": "open",
                "recipient_identities": [self.agent_id],
            },
        )
        changed = await self.call(
            "board_catchup",
            agent_name="admin-agent",
            cursor=cursor,
            ack=False,
        )
        changed_bytes = json.dumps(changed.structured_content, ensure_ascii=False)
        self.assertEqual(
            first_bytes.partition('"events":')[0],
            changed_bytes.partition('"events":')[0],
        )
        self.assertEqual(
            list(first_event),
            [
                "id",
                "seq",
                "board_id",
                "kind",
                "actor",
                "payload_ref",
                "occurred_at",
                "recipient_identities",
                "status_to",
                "ticket_id",
            ],
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

    async def test_compact_write_receipts_have_byte_ceilings(self) -> None:
        ticket_id = await self.create_fat_ticket()

        raw_update = await self.call(
            "ticket_update",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            parked=True,
        )
        compact_update = await self.protocol_call(
            "ticket_update",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            parked=False,
        )
        raw_annotation = await self.call(
            "ticket_annotate",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            text="raw annotation",
        )
        compact_annotation = await self.protocol_call(
            "ticket_annotate",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            text="compact annotation",
        )
        raw_checkpoint = await self.call(
            "memory_checkpoint",
            agent_name="admin-agent",
            summary="raw checkpoint " + "c" * 4_000,
            remaining_tasks=["t" * 1_000],
            files=["f" * 1_000],
            next_steps=["n" * 1_000],
            blockers=["b" * 1_000],
        )
        compact_checkpoint = await self.protocol_call(
            "memory_checkpoint",
            agent_name="admin-agent",
            summary="compact checkpoint",
        )
        claimed = await self.protocol_call(
            "ticket_claim",
            agent_name="admin-agent",
            ticket_id=ticket_id,
        )
        renewed = await self.protocol_call(
            "lease_renew",
            agent_name="admin-agent",
            ticket_id=ticket_id,
        )
        unclaimed = await self.protocol_call(
            "ticket_unclaim",
            agent_name="admin-agent",
            ticket_id=ticket_id,
        )
        memory_written = await self.protocol_call(
            "memory_write",
            agent_name="admin-agent",
            title="compact memory",
            content="one compact receipt",
            scope="project",
        )

        pairs = {
            "ticket_update": (raw_update, compact_update),
            "ticket_annotate": (raw_annotation, compact_annotation),
            "memory_checkpoint": (raw_checkpoint, compact_checkpoint),
        }
        observed: dict[str, tuple[int, int]] = {}
        for name, (before, after) in pairs.items():
            before_bytes = len(
                json.dumps(
                    before.structured_content, ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
            )
            after_bytes = len(
                json.dumps(
                    after.structured_content, ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
            )
            observed[name] = (before_bytes, after_bytes)
            self.assertGreater(before_bytes, after_bytes * 10)
            self.assertLessEqual(after_bytes, 1_024)
            self.assertNotIn(
                "recipient_identities",
                json.dumps(after.structured_content, ensure_ascii=False),
            )
        self.assertEqual(
            set(compact_update.structured_content),
            {
                "ok", "ticket_id", "status", "parked", "generation",
                "dispatch_state", "revoked_offer", "at",
            },
        )
        self.assertEqual(
            compact_annotation.structured_content["annotation_id"],
            raw_annotation.structured_content["annotation"]["annotation_id"].replace(
                "000001", "000002"
            ),
        )
        self.assertEqual(
            set(compact_checkpoint.structured_content),
            {
                "ok", "memory_id", "scope", "generation", "at",
            },
        )
        self.assertEqual(compact_checkpoint.structured_content["scope"], "project")
        self.assertEqual(
            set(memory_written.structured_content),
            {"ok", "memory_id", "scope", "generation", "at"},
        )
        self.assertEqual(memory_written.structured_content["scope"], "project")
        self.assertEqual(
            set(renewed.structured_content),
            {"ok", "ticket_id", "lease_expires_at", "at"},
        )
        self.assertEqual(renewed.structured_content["ticket_id"], ticket_id)
        self.assertIsNotNone(renewed.structured_content["lease_expires_at"])
        for compact_only in (claimed, renewed, unclaimed, memory_written):
            self.assertLessEqual(
                len(
                    json.dumps(
                        compact_only.structured_content,
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ),
                1_024,
            )
        print(
            "compact-write bytes: "
            + " ".join(
                f"{name}={before}/{after}"
                for name, (before, after) in observed.items()
            )
        )

    async def test_scrub_rejected_memory_write_preserves_error_contract(self) -> None:
        rejected = await self.protocol_call(
            "memory_write",
            agent_name="admin-agent",
            title="unsafe memory",
            content="Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            scope="project",
        )

        self.assertFalse(rejected.is_error)
        self.assertEqual(rejected.structured_content["ok"], False)
        self.assertEqual(
            rejected.structured_content["error"],
            "write rejected by scrub policy",
        )
        self.assertEqual(rejected.structured_content["fields"], ["content"])
        self.assertEqual(rejected.structured_content["rules"], ["bearer_token"])
        self.assertNotIn(
            "recipient_identities",
            json.dumps(rejected.structured_content, ensure_ascii=False),
        )

    async def test_full_response_view_restores_shape_but_not_routing_lists(self) -> None:
        ticket_id = await self.create_fat_ticket()
        configured = await self.protocol_call(
            "board_response_view_set",
            agent_name="admin-agent",
            response_view="full",
        )
        self.assertEqual(configured.structured_content["response_view"], "full")

        annotated = await self.protocol_call(
            "ticket_annotate",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            text="full response",
        )
        self.assertIn("ticket", annotated.structured_content)
        self.assertIn("annotation", annotated.structured_content)
        self.assertNotIn(
            "recipient_identities",
            json.dumps(annotated.structured_content, ensure_ascii=False),
        )

    def test_compact_review_lease_receipt_preserves_expiry(self) -> None:
        document = {
            "generation_revision": 7,
            "members": {
                "AI-reviewer": {"agent_name": "reviewer-one"},
            },
            "tickets": {
                "TK-review": {
                    "ticket_id": "TK-review",
                    "status": "submitted",
                    "parked": False,
                    "updated_at": "2026-09-14T16:45:00+00:00",
                    "dispatch_state": {
                        "state": "review_claimed",
                        "kind": "review",
                        "agent_id": "AI-reviewer",
                    },
                    "review_lease": {
                        "reviewer_agent_name": "reviewer-one",
                        "expires_at": "2026-09-14T17:00:00+00:00",
                    },
                },
            },
        }

        receipt = central.compact_write_response(
            "lease_renew",
            {
                "ok": True,
                "ticket_id": "TK-review",
                "lease_expires_at": "2026-09-14T17:00:00+00:00",
            },
            document,
            {"board_id": "pursers", "ticket_id": "TK-review"},
        )

        self.assertEqual(
            receipt,
            {
                "ok": True,
                "ticket_id": "TK-review",
                "lease_expires_at": "2026-09-14T17:00:00+00:00",
                "at": "2026-09-14T16:45:00+00:00",
            },
        )
        self.assertLessEqual(
            len(json.dumps(receipt, sort_keys=True).encode("utf-8")),
            1_024,
        )

    async def test_structured_memories_do_not_repeat_rendered_content(self) -> None:
        written = await self.call(
            "memory_checkpoint",
            agent_name="admin-agent",
            summary="one representation",
            remaining_tasks=["ship"],
            next_steps=["verify"],
        )
        memory_id = written.structured_content["memory"]["memory_id"]
        read = await self.call(
            "memory_read",
            agent_name="admin-agent",
            memory_type="checkpoint",
        )
        memory = next(
            item
            for item in read.structured_content["memories"]
            if item["memory_id"] == memory_id
        )
        self.assertEqual(memory["summary"], "one representation")
        self.assertEqual(memory["remaining_tasks"], ["ship"])
        self.assertEqual(memory["next_steps"], ["verify"])
        self.assertNotIn("content", memory)


if __name__ == "__main__":
    unittest.main()
