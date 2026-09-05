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
        """By default, deprecated tools are hidden while lifecycle tools remain."""
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

            # Count check: 47 total - 10 deprecated = 37 active tools
            self.assertEqual(len(tool_names), 37)
            self.assertIn("board_claim_ttl_set", tool_names)

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

            # legacy-seat sees all 47 tools
            self.assertEqual(len(res_legacy.tools), 47)
            legacy_names = {t.name for t in res_legacy.tools}
            for dep in central.DEPRECATED_TOOLS:
                self.assertIn(dep, legacy_names)

            # modern-seat remains strictly on the 37-tool active surface
            self.assertEqual(len(res_modern.tools), 37)
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

            # Now legacy-seat immediately drops to the 37-tool surface
            res_rejoin = await c_legacy.list_tools()
            self.assertEqual(len(res_rejoin.tools), 37)
            rejoin_names = {t.name for t in res_rejoin.tools}
            for dep in central.DEPRECATED_TOOLS:
                self.assertNotIn(dep, rejoin_names)

    async def test_tools_list_with_env_override(self) -> None:
        """The environment override exposes active and deprecated tools."""
        with patch.dict(os.environ, {"PURSERS_LEGACY_TOOLS": "1"}):
            async with Client(self.mcp, mode="2026-07-28", cache=None) as client:
                res = await client.list_tools()
                tool_names = {t.name for t in res.tools}
                self.assertEqual(len(tool_names), 47)
                for dep in central.DEPRECATED_TOOLS:
                    self.assertIn(dep, tool_names)

    async def test_never_joined_request_metadata_cannot_enable_legacy_tools(
        self,
    ) -> None:
        client = Client(
            self.mcp,
            client_info=types.Implementation(
                name="never-joined-seat", version="1.0"
            ),
            mode="2026-07-28",
            cache=None,
        )
        async with client:
            result = await client.list_tools(
                meta={
                    "io.modelcontextprotocol/clientCapabilities": {
                        "legacy_tools": True
                    },
                    "legacy_tools": True,
                }
            )

        self.assertEqual(len(result.tools), 37)
        self.assertTrue(
            central.DEPRECATED_TOOLS.isdisjoint(
                {tool.name for tool in result.tools}
            )
        )

    async def test_deprecated_read_is_annotated_without_domain_mutation(
        self,
    ) -> None:
        client_info = types.Implementation(name="admin-agent", version="1.0")
        before_document = self.service.load("pursers")
        before_cursor = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]
        calls = [
            ("board_get_briefing", {}),
            ("memory_read", {"agent_name": "admin-agent"}),
            ("memory_search", {"query": "absent"}),
            ("memory_links", {}),
        ]
        with patch.object(central, "log_runtime_event") as runtime_event:
            async with Client(
                self.mcp,
                client_info=client_info,
                mode="2026-07-28",
                cache=None,
            ) as client:
                for tool_name, arguments in calls:
                    result = await client.call_tool(
                        tool_name, {"board_id": "pursers", **arguments}
                    )
                    self.assertFalse(result.is_error)
                    self.assertTrue(json.loads(result.content[0].text)["_deprecated"])
                repeat = await client.call_tool(
                    "board_get_briefing", {"board_id": "pursers"}
                )

        self.assertFalse(repeat.is_error)
        self.assertEqual(runtime_event.call_count, len(calls))
        for tool_name, _arguments in calls:
            runtime_event.assert_any_call(
                "deprecated_tool_warning",
                board_id="pursers",
                tool=tool_name,
                caller_principal_id="PR-admin",
                caller_agent_name="admin-agent",
            )
        self.assertEqual(self.service.load("pursers"), before_document)
        self.assertEqual(
            self.service.journal.read_after("pursers", 0, 1)["latest_cursor"],
            before_cursor,
        )

    async def test_warning_dedupe_survives_compaction_and_restart(self) -> None:
        warning = {
            "kind": "deprecated_tool_warning",
            "actor": "AI-admin",
            "payload_ref": "board://pursers/tool/ticket_terminate",
            "tool": "ticket_terminate",
            "caller_principal_id": "PR-admin",
            "caller_agent_name": "admin-agent",
            "message": "deprecated",
        }
        original_warning, created = self.service.journal.append_once(
            "pursers",
            warning,
            unique_fields=central.DEPRECATION_WARNING_UNIQUE_FIELDS,
        )
        self.assertTrue(created)

        for index in range(central.MIN_COMPACTION_RETAIN_LAST + 1):
            self.service.journal.append(
                "pursers",
                {
                    "kind": "memory_written",
                    "actor": "AI-compaction-fixture",
                    "payload_ref": f"board://pursers/memory/MEM-{index}",
                    "memory_id": f"MEM-{index}",
                    "fixture_provenance": "deprecated warning compaction test",
                },
            )

        compacted = await self.call(
            "journal_compact",
            retain_last=central.MIN_COMPACTION_RETAIN_LAST,
        )
        self.assertFalse(compacted.is_error)
        self.assertGreaterEqual(
            compacted.structured_content["compacted_through"],
            original_warning["seq"],
        )
        self.assertEqual(
            compacted.structured_content["deprecation_dedupe_entries"], 1
        )
        self.assertEqual(
            compacted.structured_content["deprecation_dedupe_limit"],
            central.DEPRECATION_WARNING_DEDUPE_MAX_ENTRIES,
        )

        restarted_mcp, restarted_service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.mcp = restarted_mcp
        self.service = restarted_service
        restart_cursor = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]
        _duplicate, created = self.service.journal.append_once(
            "pursers",
            warning,
            unique_fields=central.DEPRECATION_WARNING_UNIQUE_FIELDS,
        )
        self.assertFalse(created)
        self.assertEqual(
            self.service.journal.read_after("pursers", 0, 1)[
                "latest_cursor"
            ],
            restart_cursor,
        )

        distinct_warning = dict(warning, caller_agent_name="other-admin")
        _distinct, created = self.service.journal.append_once(
            "pursers",
            distinct_warning,
            unique_fields=central.DEPRECATION_WARNING_UNIQUE_FIELDS,
        )
        self.assertTrue(created)
        new_page = self.service.journal.read_after(
            "pursers", restart_cursor, 10
        )
        self.assertEqual(new_page["latest_cursor"], restart_cursor + 1)
        self.assertEqual(len(new_page["events"]), 1)
        self.assertEqual(
            new_page["events"][0]["caller_agent_name"], "other-admin"
        )

    def test_warning_dedupe_summary_evicts_oldest_at_bound(self) -> None:
        with patch.object(
            central, "DEPRECATION_WARNING_DEDUPE_MAX_ENTRIES", 2
        ):
            for index in range(3):
                _event, created = self.service.journal.append_once(
                    "pursers",
                    {
                        "kind": "deprecated_tool_warning",
                        "actor": f"AI-caller-{index}",
                        "payload_ref": "board://pursers/tool/board_get_briefing",
                        "tool": "board_get_briefing",
                        "caller_principal_id": f"PR-caller-{index}",
                        "caller_agent_name": f"caller-{index}",
                        "message": "deprecated",
                    },
                    unique_fields=central.DEPRECATION_WARNING_UNIQUE_FIELDS,
                )
                self.assertTrue(created)

            document = self.service.store.load(
                self.service.journal._path("pursers"),
                lambda: self.service.journal._default("pursers"),
            )
            bucket = document["idempotency"]["deprecated_tool_warning"]
            self.assertEqual(bucket["max_entries"], 2)
            self.assertEqual(bucket["eviction"], "oldest-sequence-first")
            retained = {
                entry["event"]["caller_agent_name"]
                for entry in bucket["entries"].values()
            }
            self.assertEqual(retained, {"caller-1", "caller-2"})

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
