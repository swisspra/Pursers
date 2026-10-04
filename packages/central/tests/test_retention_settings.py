from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

import central


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class RetentionSettingsTests(unittest.IsolatedAsyncioTestCase):
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
        self.admin = central.Principal(
            "PR-admin",
            "admin",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.member = central.Principal(
            "PR-member",
            "member",
            frozenset({"board:read", "board:write"}),
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        for board_id in ("alpha", "beta"):
            await self.call(board_id, "board_join", agent_name="admin-agent")
            await self.call(
                board_id,
                "board_member_add",
                agent_name="admin-agent",
                principal_id=self.member.principal_id,
                role="member",
            )
            self.principal = self.member
            await self.call(board_id, "board_join", agent_name="member-agent")
            self.principal = self.admin
        created = await self.call(
            "alpha",
            "ticket_create",
            agent_name="admin-agent",
            title="retention invariant",
            description="must not be archived by settings save",
            scope="interactive-no-send",
            target_url="pursers/packages/central",
            required_fields=["test_output"],
        )
        self.assertFalse(created.is_error)

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, board_id: str, name: str, **arguments: object):
        return await self.mcp.call_tool(name, {"board_id": board_id, **arguments})

    async def test_get_preview_apply_readback_and_scope_isolation(self) -> None:
        initial = await self.call(
            "alpha", "board_retention_settings_get", agent_name="admin-agent"
        )
        payload = initial.structured_content
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["revision"], 0)
        self.assertEqual(payload["settings"]["archive_after_days"], 2)
        self.assertEqual(payload["ranges"]["journal_row_cap"]["minimum"], 501)
        self.assertEqual(payload["unsupported_fields"], [])
        self.assertFalse(payload["apply_runs_maintenance"])

        validated = await self.call(
            "alpha",
            "board_retention_settings_validate",
            agent_name="admin-agent",
            changes={"archive_after_days": 12},
        )
        self.assertTrue(validated.structured_content["valid"])
        self.assertEqual(
            validated.structured_content["candidate"]["archive_after_days"], 12
        )

        preview = await self.call(
            "alpha",
            "board_retention_settings_preview",
            agent_name="admin-agent",
            changes={
                "archive_after_days": 12,
                "inline_history_limit": 80,
                "invite_prune_after_days": 14,
                "journal_retention_days": 30,
                "journal_row_cap": 8_000,
            },
            expected_revision=0,
        )
        self.assertTrue(preview.structured_content["changed"])
        self.assertEqual(preview.structured_content["candidate_revision"], 1)
        self.assertFalse(preview.structured_content["apply_runs_maintenance"])
        self.assertEqual(
            (await self.call(
                "alpha", "board_retention_settings_get", agent_name="admin-agent"
            )).structured_content["revision"],
            0,
        )

        beta_before = copy.deepcopy(self.service.load("beta"))
        applied = await self.call(
            "alpha",
            "board_retention_settings_apply",
            agent_name="admin-agent",
            changes=preview.structured_content["candidate"],
            expected_revision=0,
        )
        result = applied.structured_content
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["settings"]["journal_row_cap"], 8_000)
        self.assertFalse(result["maintenance_run"])
        self.assertEqual(result["audit"]["revision_from"], 0)
        self.assertEqual(result["audit"]["revision_to"], 1)
        self.assertEqual(self.service.load("beta"), beta_before)

        readback = await self.call(
            "alpha", "board_retention_settings_get", agent_name="admin-agent"
        )
        self.assertEqual(readback.structured_content["settings"], result["settings"])
        self.assertEqual(readback.structured_content["revision"], 1)

    async def test_apply_has_no_destructive_or_lease_side_effects(self) -> None:
        before = copy.deepcopy(self.service.load("alpha"))
        journal_before = copy.deepcopy(
            self.service.journal.read_after("alpha", 0, 1_000)
        )
        archive_before = copy.deepcopy(self.service.load_archive_index("alpha"))

        applied = await self.call(
            "alpha",
            "board_retention_settings_apply",
            agent_name="admin-agent",
            changes={"archive_after_days": 0, "journal_retention_days": 0},
            expected_revision=0,
        )
        self.assertFalse(applied.is_error)

        after = self.service.load("alpha")
        for field in ("members", "invites", "tickets", "memories", "state"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(
            self.service.journal.read_after("alpha", 0, 1_000), journal_before
        )
        self.assertEqual(self.service.load_archive_index("alpha"), archive_before)

    async def test_unauthorized_stale_invalid_and_unknown_are_rejected(self) -> None:
        self.principal = self.member
        for tool, arguments in (
            ("board_retention_settings_get", {}),
            (
                "board_retention_settings_validate",
                {"changes": {"archive_after_days": 3}},
            ),
            (
                "board_retention_settings_preview",
                {"changes": {"archive_after_days": 3}, "expected_revision": 0},
            ),
            (
                "board_retention_settings_apply",
                {"changes": {"archive_after_days": 3}, "expected_revision": 0},
            ),
        ):
            with self.subTest(tool=tool), self.assertRaisesRegex(ToolError, "admin"):
                await self.call("alpha", tool, agent_name="member-agent", **arguments)

        self.principal = self.admin
        for changes, message in (
            ({"unknown": 1}, "unsupported retention settings"),
            ({"archive_after_days": True}, "archive_after_days"),
            ({"inline_history_limit": 0}, "inline_history_limit"),
            ({"journal_row_cap": 500}, "journal_row_cap"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ToolError, message):
                await self.call(
                    "alpha",
                    "board_retention_settings_preview",
                    agent_name="admin-agent",
                    changes=changes,
                    expected_revision=0,
                )

        await self.call(
            "alpha",
            "board_retention_settings_apply",
            agent_name="admin-agent",
            changes={"archive_after_days": 3},
            expected_revision=0,
        )
        for tool in (
            "board_retention_settings_preview",
            "board_retention_settings_apply",
        ):
            with self.subTest(tool=tool), self.assertRaisesRegex(
                ToolError, "revision conflict"
            ):
                await self.call(
                    "alpha",
                    tool,
                    agent_name="admin-agent",
                    changes={"archive_after_days": 4},
                    expected_revision=0,
                )

    async def test_noop_apply_preserves_revision_and_does_not_audit(self) -> None:
        result = await self.call(
            "alpha",
            "board_retention_settings_apply",
            agent_name="admin-agent",
            changes={"archive_after_days": 2},
            expected_revision=0,
        )
        self.assertFalse(result.structured_content["changed"])
        self.assertEqual(result.structured_content["revision"], 0)
        self.assertIsNone(result.structured_content["audit"])
        self.assertEqual(self.service.load("alpha")["retention_settings_audit"], [])

    async def test_legacy_journal_setter_advances_revision_and_records_audit(self) -> None:
        initial = await self.call(
            "alpha", "board_retention_settings_get", agent_name="admin-agent"
        )
        initial_revision = initial.structured_content["revision"]

        updated = await self.call(
            "alpha",
            "board_journal_retention_set",
            agent_name="admin-agent",
            journal_retention_days=30,
            journal_row_cap=8_000,
        )
        payload = updated.structured_content
        self.assertEqual(payload["revision"], initial_revision + 1)
        self.assertEqual(payload["audit"]["revision_from"], initial_revision)
        self.assertEqual(payload["audit"]["revision_to"], initial_revision + 1)
        self.assertEqual(
            payload["audit"]["changed_fields"],
            ["journal_retention_days", "journal_row_cap"],
        )
        self.assertEqual(
            payload["audit"]["previous"],
            {"journal_retention_days": 7, "journal_row_cap": 50_000},
        )
        self.assertEqual(
            payload["audit"]["current"],
            {"journal_retention_days": 30, "journal_row_cap": 8_000},
        )
        actor = self.service.member(
            self.service.load("alpha"), self.admin, "admin-agent"
        )
        self.assertEqual(payload["audit"]["actor_agent_id"], actor["agent_id"])
        self.assertEqual(payload["audit"]["actor_principal_id"], "PR-admin")
        self.assertTrue(payload["audit"]["maintenance_run"])
        self.assertEqual(
            self.service.load("alpha")["retention_settings_audit"],
            [payload["audit"]],
        )

        for tool in (
            "board_retention_settings_preview",
            "board_retention_settings_apply",
        ):
            with self.subTest(tool=tool), self.assertRaisesRegex(
                ToolError, "revision conflict"
            ):
                await self.call(
                    "alpha",
                    tool,
                    agent_name="admin-agent",
                    changes={"journal_retention_days": 7, "journal_row_cap": 50_000},
                    expected_revision=initial_revision,
                )


if __name__ == "__main__":
    unittest.main()
