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
from mcp import types  # noqa: E402
from mcp.client.client import Client  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


class LegacyToolsTests(unittest.IsolatedAsyncioTestCase):
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
        self.admin_principal = central.Principal(
            "PR-admin",
            "admin-canonical",
            frozenset({"board:read", "board:write", "board:review", "board:coordinate"}),
        )
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.admin_principal
        joined = await self.call("board_join", agent_name="admin-agent")
        self.assertFalse(joined.is_error)

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name,
            {"board_id": "pursers", **arguments},
        )

    async def test_tools_list_default_hides_deprecated_tools(self) -> None:
        """By default without legacy capability, 10 deprecated tools are hidden, leaving 29 active tools."""
        async with Client(self.mcp, mode="2026-07-28", cache=None) as client:
            res = await client.list_tools()
            tool_names = {t.name for t in res.tools}

            # Deprecated tools must NOT be present
            for dep in central.DEPRECATED_TOOLS:
                self.assertNotIn(
                    dep, tool_names, f"Deprecated tool {dep} should be hidden by default"
                )

            # Core and active tools must be present, including memory_write and ticket_unclaim
            core_tools = {
                "ticket_claim", "ticket_get", "ticket_list", "ticket_submit",
                "ticket_review", "ticket_create", "ticket_cancel", "lease_renew",
                "ticket_unclaim", "memory_write",
                "board_join", "board_onboard", "board_snapshot", "board_status",
                "board_state_get", "board_state_update", "board_catchup"
            }
            for core in core_tools:
                self.assertIn(
                    core, tool_names, f"Active tool {core} must be visible"
                )

            # Count check: 39 total - 10 deprecated = 29 active tools
            self.assertEqual(len(tool_names), 29)

    async def test_authenticated_two_connection_seat_scoped_capability_and_rejoin(self) -> None:
        """Legacy visibility is strictly seat/session scoped: two seats under same principal remain isolated."""
        central.current_principal = lambda: self.admin_principal

        c_legacy = Client(
            self.mcp,
            client_info=types.Implementation(name="legacy-seat", version="1.0"),
            mode="2026-07-28",
            cache=None,
        )
        c_modern = Client(
            self.mcp,
            client_info=types.Implementation(name="modern-seat", version="1.0"),
            mode="2026-07-28",
            cache=None,
        )

        async with c_legacy, c_modern:
            # 1. Join legacy-seat with legacy_tools=True
            join1 = await c_legacy.call_tool(
                "board_join",
                {
                    "board_id": "pursers",
                    "agent_name": "legacy-seat",
                    "capabilities": {"legacy_tools": True},
                },
            )
            self.assertFalse(join1.is_error)

            # 2. Join modern-seat under SAME principal with legacy_tools=False
            join2 = await c_modern.call_tool(
                "board_join",
                {
                    "board_id": "pursers",
                    "agent_name": "modern-seat",
                    "capabilities": {"legacy_tools": False},
                },
            )
            self.assertFalse(join2.is_error)

            # 3. List tools on both connections
            res_legacy = await c_legacy.list_tools()
            res_modern = await c_modern.list_tools()

            # legacy-seat sees all 39 tools
            self.assertEqual(len(res_legacy.tools), 39)
            legacy_names = {t.name for t in res_legacy.tools}
            for dep in central.DEPRECATED_TOOLS:
                self.assertIn(dep, legacy_names)

            # modern-seat under same principal remains strictly on the 29-tool surface
            self.assertEqual(len(res_modern.tools), 29)
            modern_names = {t.name for t in res_modern.tools}
            for dep in central.DEPRECATED_TOOLS:
                self.assertNotIn(dep, modern_names)

            # Verify actual deprecated Tool.annotations on the legacy connection
            dep_tools = [t for t in res_legacy.tools if t.name in central.DEPRECATED_TOOLS]
            self.assertEqual(len(dep_tools), 10)
            for dt in dep_tools:
                self.assertIsNotNone(dt.annotations)
                self.assertTrue(dt.annotations.title.startswith("[DEPRECATED]"))
                self.assertEqual(getattr(dt, "meta", {}).get("deprecated"), True)

            # 4. Opt-out/rejoin coverage: legacy-seat rejoins with legacy_tools=False
            rejoin = await c_legacy.call_tool(
                "board_join",
                {
                    "board_id": "pursers",
                    "agent_name": "legacy-seat",
                    "capabilities": {"legacy_tools": False},
                },
            )
            self.assertFalse(rejoin.is_error)

            # Now legacy-seat immediately drops to the 29-tool surface
            res_rejoin = await c_legacy.list_tools()
            self.assertEqual(len(res_rejoin.tools), 29)
            rejoin_names = {t.name for t in res_rejoin.tools}
            for dep in central.DEPRECATED_TOOLS:
                self.assertNotIn(dep, rejoin_names)

    async def test_tools_list_with_env_override(self) -> None:
        """When PURSERS_LEGACY_TOOLS=1 env var is set, all 39 tools are visible without join capability."""
        with patch.dict(os.environ, {"PURSERS_LEGACY_TOOLS": "1"}):
            async with Client(self.mcp, mode="2026-07-28", cache=None) as client:
                res = await client.list_tools()
                tool_names = {t.name for t in res.tools}
                self.assertEqual(len(tool_names), 39)
                for dep in central.DEPRECATED_TOOLS:
                    self.assertIn(dep, tool_names)

    async def test_calling_deprecated_tool_post_authorization_durable_dedupe_and_restart(self) -> None:
        """Denials cause zero mutation/events; authorized calls emit sequenced journal warning and dedupe survives restart."""
        # 1. Adversarial test: unjoined outsider calling ticket_terminate against pursers board
        outsider_principal = central.Principal(
            "PR-outsider", "outsider-canonical", frozenset({"board:read"})
        )
        central.current_principal = lambda: outsider_principal

        before_seq = self.service.journal.read_after("pursers", 0, 1)["latest_cursor"]
        with self.assertRaises(ToolError):
            await self.call(
                "ticket_terminate",
                agent_name="outsider-agent",
                ticket_id="TK-nonexistent",
                reason="Adversarial attempt",
            )

        # Assert zero mutations/events caused by denied call
        after_denied_journal = self.service.journal.read_after("pursers", before_seq, 100)
        self.assertEqual(len(after_denied_journal["events"]), 0)
        self.assertEqual(after_denied_journal["latest_cursor"], before_seq)

        # 2. Authorized call: switch back to admin principal
        central.current_principal = lambda: self.admin_principal
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="Deprecated Tool Test Ticket 1",
            description="Testing deprecation warnings",
            target_url="pursers/test",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        self.assertFalse(created.is_error)
        ticket_id1 = json.loads(created.content[0].text)["ticket"]["ticket_id"]

        seq_before_term = self.service.journal.read_after("pursers", 0, 1)["latest_cursor"]

        # Call deprecated ticket_terminate
        res1 = await self.call(
            "ticket_terminate",
            agent_name="admin-agent",
            ticket_id=ticket_id1,
            reason="Testing deprecation",
        )
        self.assertFalse(res1.is_error)
        data1 = json.loads(res1.content[0].text)
        self.assertTrue(data1.get("_deprecated"))
        self.assertTrue(data1.get("deprecated"))

        # Check normal sequenced journal path: exactly one deprecated_tool_warning event
        events_after = self.service.journal.read_after("pursers", seq_before_term, 100)["events"]
        warn_events = [e for e in events_after if e.get("kind") == "deprecated_tool_warning"]
        self.assertEqual(len(warn_events), 1)
        self.assertEqual(warn_events[0].get("tool"), "ticket_terminate")
        self.assertGreater(warn_events[0].get("seq", 0), seq_before_term)

        # 3. Repeat call by same caller on a second ticket: NO duplicate warning
        created2 = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="Deprecated Tool Test Ticket 2",
            description="Testing repeat call",
            target_url="pursers/test2",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        ticket_id2 = json.loads(created2.content[0].text)["ticket"]["ticket_id"]

        seq_before_term2 = self.service.journal.read_after("pursers", 0, 1)["latest_cursor"]

        res2 = await self.call(
            "ticket_terminate",
            agent_name="admin-agent",
            ticket_id=ticket_id2,
            reason="Second termination",
        )
        self.assertFalse(res2.is_error)
        events_after2 = self.service.journal.read_after("pursers", seq_before_term2, 100)["events"]
        warn_events2 = [e for e in events_after2 if e.get("kind") == "deprecated_tool_warning"]
        self.assertEqual(len(warn_events2), 0)

        # 4. Durable restart test: rebuild Central service from same data directory
        restarted_mcp, restarted_service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.mcp = restarted_mcp
        self.service = restarted_service

        created3 = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="Deprecated Tool Test Ticket 3",
            description="Testing restart deduplication",
            target_url="pursers/test3",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        ticket_id3 = json.loads(created3.content[0].text)["ticket"]["ticket_id"]

        seq_before_term3 = self.service.journal.read_after("pursers", 0, 1)["latest_cursor"]

        res3 = await self.call(
            "ticket_terminate",
            agent_name="admin-agent",
            ticket_id=ticket_id3,
            reason="Termination after restart",
        )
        self.assertFalse(res3.is_error)

        # Assert no duplicate warning event emitted after restart!
        events_after3 = self.service.journal.read_after("pursers", seq_before_term3, 100)["events"]
        warn_events3 = [e for e in events_after3 if e.get("kind") == "deprecated_tool_warning"]
        self.assertEqual(len(warn_events3), 0)
