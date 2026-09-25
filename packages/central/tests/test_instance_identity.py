from __future__ import annotations

import copy
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))
sys.path.insert(0, str(REPOSITORY_ROOT / "packages" / "client" / "src"))

import central  # noqa: E402
from mcp import Client  # noqa: E402
from pursers_client import (  # noqa: E402
    INSTANCE_META_KEY,
    bind_instance_subscriptions,
    ensure_central_instance_identity,
    fork_central_instance_identity,
)


class CentralInstanceIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=PACKAGE_ROOT)
        self.root = Path(self.temporary.name)
        jwks = self.root / "jwks.json"
        jwks.write_text('{"keys": []}', encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": "https://issuer.example",
                "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
                "CENTRAL_JWKS_PATH": str(jwks),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
            },
        )
        self.environment.start()
        self.principal = central.Principal(
            "PR-instance-test",
            "instance-test",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.original_principal = central.current_principal
        central.current_principal = lambda: self.principal

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_principal
        self.environment.stop()
        self.temporary.cleanup()

    async def _join(self, server) -> None:
        joined = await server.call_tool(
            "board_join", {"board_id": "same-board", "agent_name": "owner"}
        )
        self.assertFalse(joined.is_error)

    @staticmethod
    def _ticket_arguments() -> dict[str, object]:
        return {
            "board_id": "same-board",
            "agent_name": "owner",
            "title": "instance-specific ticket",
            "description": "prove cross-instance ticket isolation",
            "target_url": "pursers/packages/central",
            "scope": "interactive-no-send",
            "required_fields": ["test_output"],
        }

    async def test_aligned_instances_generate_distinct_ids_and_mismatch_precedes_write(
        self,
    ) -> None:
        server_a, service_a = central.build_server(
            "localhost", 8765, self.root / "a"
        )
        server_b, service_b = central.build_server(
            "localhost", 8765, self.root / "b"
        )
        await self._join(server_a)
        await self._join(server_b)

        # This is the pre-fix algorithm: equal board IDs and sequence 1 collide.
        legacy_a = hashlib.sha256(b"same-board:1").hexdigest()[:12]
        legacy_b = hashlib.sha256(b"same-board:1").hexdigest()[:12]
        self.assertEqual(legacy_a, legacy_b)

        async with Client(server_b, mode="2026-07-28", cache=None) as wrong_client:
            for tool_name, arguments in (
                ("ticket_get", {"board_id": "same-board", "ticket_id": "TK-any"}),
                ("ticket_create", self._ticket_arguments()),
                (
                    "ticket_review",
                    {
                        "board_id": "same-board",
                        "agent_name": "owner",
                        "ticket_id": "TK-any",
                        "verdict": "approve",
                    },
                ),
            ):
                wrong = await wrong_client.call_tool(
                    tool_name,
                    arguments,
                    meta={INSTANCE_META_KEY: service_a.instance_id},
                )
                self.assertTrue(wrong.is_error)
                self.assertIn("Central instance mismatch", wrong.content[0].text)
        self.assertEqual(service_b.load("same-board")["tickets"], {})

        created_a = await server_a.call_tool(
            "ticket_create",
            self._ticket_arguments(),
        )
        created_b = await server_b.call_tool(
            "ticket_create",
            self._ticket_arguments(),
        )
        self.assertFalse(created_a.is_error)
        self.assertFalse(created_b.is_error)
        self.assertNotEqual(
            created_a.structured_content["ticket"]["ticket_id"],
            created_b.structured_content["ticket"]["ticket_id"],
        )

    async def test_wrong_instance_subscription_rejects_before_all_side_effects(
        self,
    ) -> None:
        server_a, service_a = central.build_server(
            "localhost", 8765, self.root / "subscription-a"
        )
        server_b, service_b = central.build_server(
            "localhost", 8765, self.root / "subscription-b"
        )
        await self._join(server_a)
        await self._join(server_b)
        document = service_b.load("same-board")
        agent_id = next(iter(document["members"]))
        resources = bind_instance_subscriptions(
            [
                "board://same-board/journal",
                f"board://same-board/agent/{agent_id}",
            ],
            service_a.instance_id,
        )
        before_document = copy.deepcopy(document)
        before_cursor = service_b.cursors.get(
            self.principal.principal_id, "owner", "same-board"
        )
        before_journal = service_b.journal.read_after("same-board", 0, 1)

        async with Client(server_b, mode="2026-07-28", cache=None) as client:
            with self.assertRaisesRegex(Exception, "Central instance mismatch"):
                async with client.listen(resource_subscriptions=resources):
                    self.fail("wrong-instance subscription was accepted")

        self.assertEqual(service_b.active_stream_count, 0)
        self.assertFalse(service_b.active_listeners.get("same-board"))
        self.assertEqual(service_b.last_seen_activity, {})
        self.assertEqual(service_b.load("same-board"), before_document)
        self.assertEqual(
            service_b.cursors.get(
                self.principal.principal_id, "owner", "same-board"
            ),
            before_cursor,
        )
        self.assertEqual(
            service_b.journal.read_after("same-board", 0, 1), before_journal
        )

    async def test_same_instance_subscription_preserves_listener_liveness(
        self,
    ) -> None:
        server, service = central.build_server(
            "localhost", 8765, self.root / "subscription-same"
        )
        await self._join(server)
        agent_id = next(iter(service.load("same-board")["members"]))
        resources = bind_instance_subscriptions(
            [f"board://same-board/agent/{agent_id}"], service.instance_id
        )

        async with Client(server, mode="2026-07-28", cache=None) as client:
            async with client.listen(resource_subscriptions=resources):
                self.assertEqual(service.active_stream_count, 1)
                self.assertTrue(
                    service.agent_has_active_listener("same-board", agent_id)
                )
                self.assertIn(
                    ("same-board", agent_id), service.last_seen_activity
                )

        self.assertEqual(service.active_stream_count, 0)
        self.assertFalse(service.agent_has_active_listener("same-board", agent_id))

    def test_migration_backup_restore_and_live_clone_fork_semantics(self) -> None:
        legacy = self.root / "legacy"
        central.TransactionalSQLiteStore(legacy)
        self.assertFalse((legacy / "central-instance.json").exists())
        migrated = central.CentralBoard(legacy)
        self.assertEqual(
            migrated.instance_id, ensure_central_instance_identity(legacy)
        )

        restored = self.root / "restored-backup"
        shutil.copytree(legacy, restored)
        self.assertEqual(
            ensure_central_instance_identity(restored), migrated.instance_id
        )

        clone = self.root / "live-clone"
        shutil.copytree(legacy, clone)
        old, new = fork_central_instance_identity(clone)
        self.assertEqual(old, migrated.instance_id)
        self.assertNotEqual(new, old)
        self.assertEqual(ensure_central_instance_identity(clone), new)

    def test_concurrent_first_start_converges_on_one_identity(self) -> None:
        data = self.root / "concurrent"
        with ThreadPoolExecutor(max_workers=8) as pool:
            identities = list(
                pool.map(lambda _index: ensure_central_instance_identity(data), range(32))
            )
        self.assertEqual(len(set(identities)), 1)
