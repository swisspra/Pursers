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
from mcp.client.client import Client  # noqa: E402


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
        self.worker_principal = central.Principal(
            "PR-worker",
            "worker-canonical",
            frozenset({"board:read", "board:write"}),
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
        """By default without legacy capability, all 12 deprecated tools are hidden."""
        async with Client(self.mcp, mode="2026-07-28", cache=None) as client:
            res = await client.list_tools()
            tool_names = {t.name for t in res.tools}

            # Deprecated tools must NOT be present
            for dep in central.DEPRECATED_TOOLS:
                self.assertNotIn(
                    dep, tool_names, f"Deprecated tool {dep} should be hidden by default"
                )

            # Core tools must be present
            core_tools = {
                "ticket_claim", "ticket_get", "ticket_list", "ticket_submit",
                "ticket_review", "ticket_create", "ticket_cancel", "lease_renew",
                "board_join", "board_onboard", "board_snapshot", "board_status",
                "board_state_get", "board_state_update", "board_catchup"
            }
            for core in core_tools:
                self.assertIn(
                    core, tool_names, f"Core tool {core} must be visible"
                )

            # Count check: 39 total - 12 deprecated = 27 active tools
            self.assertEqual(len(tool_names), 27)

    async def test_tools_list_with_legacy_tools_capability(self) -> None:
        """When a seat joins declaring legacy_tools=True, all 39 tools are visible."""
        # Join with legacy_tools capability
        joined = await self.call(
            "board_join",
            agent_name="admin-agent",
            capabilities={"legacy_tools": True},
        )
        self.assertFalse(joined.is_error)

        async with Client(self.mcp, mode="2026-07-28", cache=None) as client:
            res = await client.list_tools()
            tool_names = {t.name for t in res.tools}

            # All 12 deprecated tools must now be visible
            for dep in central.DEPRECATED_TOOLS:
                self.assertIn(
                    dep, tool_names, f"Deprecated tool {dep} must be visible when legacy_tools=True"
                )

            # Total count should be all 39 Central tools
            self.assertEqual(len(tool_names), 39)

    async def test_tools_list_with_env_override(self) -> None:
        """When PURSERS_LEGACY_TOOLS=1 env var is set, all 39 tools are visible without join capability."""
        with patch.dict(os.environ, {"PURSERS_LEGACY_TOOLS": "1"}):
            async with Client(self.mcp, mode="2026-07-28", cache=None) as client:
                res = await client.list_tools()
                tool_names = {t.name for t in res.tools}
                self.assertEqual(len(tool_names), 39)
                for dep in central.DEPRECATED_TOOLS:
                    self.assertIn(dep, tool_names)

    async def test_calling_deprecated_tool_emits_annotation_and_one_time_journal_warning(self) -> None:
        """Deprecated tool execution returns _deprecated annotation and emits a one-time journal warning."""
        # Create a ticket first
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="Deprecated Tool Test Ticket",
            description="Testing deprecation warnings",
            target_url="pursers/test",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        self.assertFalse(created.is_error)
        created_dict = json.loads(created.content[0].text)
        ticket_id = created_dict["ticket"]["ticket_id"]

        # Call deprecated ticket_terminate
        res1 = await self.call(
            "ticket_terminate",
            agent_name="admin-agent",
            ticket_id=ticket_id,
            reason="Testing deprecation",
        )
        self.assertFalse(res1.is_error)
        data1 = json.loads(res1.content[0].text)
        # Check deprecated annotation
        self.assertTrue(data1.get("_deprecated"))
        self.assertTrue(data1.get("deprecated"))

        # Inspect board journal for deprecated_tool_warning event
        journal_page = self.service.journal.read_after("pursers", 0, 100)
        warn_events = [e for e in journal_page.get("warnings", []) if e.get("kind") == "deprecated_tool_warning"]
        self.assertEqual(len(warn_events), 1)
        self.assertEqual(warn_events[0].get("tool"), "ticket_terminate")

        # Calling again by the same caller must NOT emit a second journal warning
        # Create another ticket to test second call
        created2 = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="Second Ticket",
            description="Testing second call",
            target_url="pursers/test2",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        ticket_id2 = json.loads(created2.content[0].text)["ticket"]["ticket_id"]

        res2 = await self.call(
            "ticket_terminate",
            agent_name="admin-agent",
            ticket_id=ticket_id2,
            reason="Second termination",
        )
        self.assertFalse(res2.is_error)
        data2 = json.loads(res2.content[0].text)
        self.assertTrue(data2.get("_deprecated"))

        # Still only 1 warning event in the journal for this caller and tool
        journal_page2 = self.service.journal.read_after("pursers", 0, 100)
        warn_events2 = [e for e in journal_page2.get("warnings", []) if e.get("kind") == "deprecated_tool_warning"]
        self.assertEqual(len(warn_events2), 1)
