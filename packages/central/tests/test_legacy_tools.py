from __future__ import annotations

import ast
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

REMOVED_TOOLS = {"agent_nudge", "board_get_briefing", "ticket_terminate"}


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
        """Default discovery hides the retained assignment escape hatch."""
        async with Client(self.mcp, mode="2026-07-28", cache=None) as client:
            res = await client.list_tools()
            tool_names = {t.name for t in res.tools}

            # Deprecated tools must NOT be present
            for dep in central.DEPRECATED_TOOLS:
                self.assertNotIn(
                    dep, tool_names, f"Deprecated tool {dep} should be hidden by default"
                )

            # Core and Personal memory tools remain visible by default.
            core_tools = {
                "ticket_claim", "ticket_get", "ticket_list", "ticket_submit",
                "ticket_request_human", "ticket_human_resolve",
                "ticket_review", "ticket_create", "ticket_cancel", "lease_renew",
                "ticket_unclaim", "memory_write", "memory_read", "memory_search",
                "memory_links", "memory_checkpoint", "memory_handoff", "memory_unpin",
                "board_join", "board_onboard", "board_snapshot", "board_status",
                "board_state_get", "board_state_update", "board_catchup"
            }
            for core in core_tools:
                self.assertIn(
                    core, tool_names, f"Active tool {core} must be visible"
                )

            # Three retired tools are gone; the one deprecated tool stays hidden.
            self.assertEqual(len(tool_names), 46)
            self.assertIn("ticket_annotate", tool_names)
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

            # legacy-seat sees the 46 active tools plus ticket_assign.
            self.assertEqual(len(res_legacy.tools), 47)
            legacy_names = {t.name for t in res_legacy.tools}
            for dep in central.DEPRECATED_TOOLS:
                self.assertIn(dep, legacy_names)

            # modern-seat under same principal remains on the 46-tool surface.
            self.assertEqual(len(res_modern.tools), 46)
            modern_names = {t.name for t in res_modern.tools}
            for dep in central.DEPRECATED_TOOLS:
                self.assertNotIn(dep, modern_names)

            # Verify actual deprecated Tool.annotations on the legacy connection
            dep_tools = [t for t in res_legacy.tools if t.name in central.DEPRECATED_TOOLS]
            self.assertEqual(len(dep_tools), 1)
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
                    "allow_takeover": True,
                },
            )
            self.assertFalse(rejoin.is_error)

            # Now legacy-seat immediately drops to the 46-tool surface.
            res_rejoin = await c_legacy.list_tools()
            self.assertEqual(len(res_rejoin.tools), 46)
            rejoin_names = {t.name for t in res_rejoin.tools}
            for dep in central.DEPRECATED_TOOLS:
                self.assertNotIn(dep, rejoin_names)

    async def test_tools_list_with_env_override(self) -> None:
        """The environment override exposes the retained deprecated tool."""
        with patch.dict(os.environ, {"PURSERS_LEGACY_TOOLS": "1"}):
            async with Client(self.mcp, mode="2026-07-28", cache=None) as client:
                res = await client.list_tools()
                tool_names = {t.name for t in res.tools}
                self.assertEqual(len(tool_names), 47)
                for dep in central.DEPRECATED_TOOLS:
                    self.assertIn(dep, tool_names)
                self.assertTrue(REMOVED_TOOLS.isdisjoint(tool_names))

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

        self.assertEqual(len(result.tools), 46)
        self.assertTrue(
            central.DEPRECATED_TOOLS.isdisjoint(
                {tool.name for tool in result.tools}
            )
        )

    def test_shipped_callers_do_not_reference_deprecated_tools(self) -> None:
        repo_root = PACKAGE_ROOT.parents[1]
        caller_roots = (
            repo_root / "packages" / "personal" / "src",
            repo_root / "tools" / "fleet-dashboard",
            repo_root / "tools" / "coordinator",
            repo_root / "tools" / "worker-runtime",
            repo_root / "tools" / "seat-kit",
        )
        allowed_deprecated_callers = {
            ("tools/coordinator/coordinator.py", "ticket_assign"),
        }
        observed_exceptions: set[tuple[str, str]] = set()
        violations: list[str] = []
        for root in caller_roots:
            for path in root.rglob("*.py"):
                if "tests" in path.parts:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    deprecated_name = None
                    if (
                        isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and node.value in central.DEPRECATED_TOOLS
                    ):
                        deprecated_name = node.value
                    elif (
                        isinstance(node, ast.Attribute)
                        and node.attr in central.DEPRECATED_TOOLS
                    ):
                        deprecated_name = node.attr
                    elif (
                        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name in central.DEPRECATED_TOOLS
                    ):
                        deprecated_name = node.name
                    if deprecated_name is not None:
                        exception = (
                            path.relative_to(repo_root).as_posix(),
                            deprecated_name,
                        )
                        if exception in allowed_deprecated_callers:
                            observed_exceptions.add(exception)
                            continue
                        violations.append(
                            f"{path.relative_to(repo_root)}:{node.lineno}:"
                            f"{deprecated_name}"
                        )
        self.assertEqual(observed_exceptions, allowed_deprecated_callers)
        self.assertEqual(violations, [])

    async def test_warning_dedupe_survives_compaction_and_restart(self) -> None:
        warning = {
            "kind": "deprecated_tool_warning",
            "actor": "AI-admin",
            "payload_ref": "board://pursers/tool/ticket_assign",
            "tool": "ticket_assign",
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
                        "payload_ref": "board://pursers/tool/ticket_assign",
                        "tool": "ticket_assign",
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
        outsider_principal = central.Principal(
            "PR-outsider", "outsider-canonical", frozenset({"board:read"})
        )
        central.current_principal = lambda: outsider_principal

        before_seq = self.service.journal.read_after("pursers", 0, 1)["latest_cursor"]
        with self.assertRaises(ToolError):
            await self.call(
                "ticket_assign",
                agent_name="outsider-agent",
                ticket_id="TK-nonexistent",
                assigned_to_agent_id="AI-nonexistent",
                expected_status="open",
                coordinator_op_key="adversarial-assignment",
                reason="Adversarial attempt",
            )

        after_denied_journal = self.service.journal.read_after("pursers", before_seq, 100)
        self.assertEqual(len(after_denied_journal["events"]), 0)
        self.assertEqual(after_denied_journal["latest_cursor"], before_seq)

        central.current_principal = lambda: self.admin_principal
        target_name = "assignment-target"
        joined = await self.call("board_join", agent_name=target_name)
        self.assertFalse(joined.is_error)
        target_id = central.agent_id("pursers", "PR-admin", target_name)

        async def create_and_assign(index: int):
            created = await self.call(
                "ticket_create",
                agent_name="admin-agent",
                title=f"Deprecated Tool Test Ticket {index}",
                description="Testing deprecation warnings",
                target_url=f"pursers/test{index}",
                scope="interactive-no-send",
                required_fields=["test_output"],
            )
            self.assertFalse(created.is_error)
            ticket_id = json.loads(created.content[0].text)["ticket"]["ticket_id"]
            return await self.call(
                "ticket_assign",
                agent_name="admin-agent",
                ticket_id=ticket_id,
                assigned_to_agent_id=target_id,
                expected_status="open",
                coordinator_op_key=f"deprecated-assignment-{index}",
                reason="Testing deprecation",
            )

        seq_before_first = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]
        first = await create_and_assign(1)
        self.assertFalse(first.is_error)
        first_data = json.loads(first.content[0].text)
        self.assertTrue(first_data.get("_deprecated"))
        self.assertTrue(first_data.get("deprecated"))

        events_after = self.service.journal.read_after(
            "pursers", seq_before_first, 100
        )["events"]
        warn_events = [
            event
            for event in events_after
            if event.get("kind") == "deprecated_tool_warning"
        ]
        self.assertEqual(len(warn_events), 1)
        self.assertEqual(warn_events[0].get("tool"), "ticket_assign")

        seq_before_second = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]
        second = await create_and_assign(2)
        self.assertFalse(second.is_error)
        events_after_second = self.service.journal.read_after(
            "pursers", seq_before_second, 100
        )["events"]
        self.assertEqual(
            [
                event
                for event in events_after_second
                if event.get("kind") == "deprecated_tool_warning"
            ],
            [],
        )

        restarted_mcp, restarted_service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.mcp = restarted_mcp
        self.service = restarted_service
        restart_cursor = self.service.journal.read_after("pursers", 0, 1)[
            "latest_cursor"
        ]
        third = await create_and_assign(3)
        self.assertFalse(third.is_error)
        events_after_restart = self.service.journal.read_after(
            "pursers", restart_cursor, 100
        )["events"]
        self.assertEqual(
            [
                event
                for event in events_after_restart
                if event.get("kind") == "deprecated_tool_warning"
            ],
            [],
        )
