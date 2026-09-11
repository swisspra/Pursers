from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402


class BoardListTests(unittest.IsolatedAsyncioTestCase):
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
        self.outsider = central.Principal(
            "PR-outsider",
            "outsider",
            frozenset({"board:read", "board:write"}),
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        for board_id in ("healthy", "with-tombstone"):
            await self.mcp.call_tool(
                "board_join", {"board_id": board_id, "agent_name": "admin-agent"}
            )

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def test_missing_and_invalid_member_roles_do_not_break_discovery(self) -> None:
        now = time.time()

        def seed(document: dict[str, object]) -> None:
            members = document["members"]
            assert isinstance(members, dict)
            members["AI-retired"] = {
                "agent_id": "AI-retired",
                "agent_name": "retired",
                "principal_id": self.admin.principal_id,
                "lifecycle_status": "retired",
                "membership_role": "admin",
                "joined_at": central.iso_at(now - 40 * 86_400),
                "tombstoned_at": central.iso_at(now),
                "tombstone": True,
            }
            members["AI-invalid-role"] = {
                "agent_id": "AI-invalid-role",
                "agent_name": "invalid-role",
                "principal_id": self.admin.principal_id,
                "role": "admin",
                "lifecycle_status": "active",
            }
            members["AI-invalid-role-type"] = {
                "agent_id": "AI-invalid-role-type",
                "agent_name": "invalid-role-type",
                "principal_id": self.admin.principal_id,
                "role": ["worker"],
                "lifecycle_status": "active",
            }
            # A member-shaped row cannot admit a principal by itself.
            members["AI-outsider"] = {
                "agent_id": "AI-outsider",
                "agent_name": "outsider",
                "principal_id": self.outsider.principal_id,
                "role": "worker",
                "lifecycle_status": "active",
            }

        self.service.mutate("with-tombstone", seed, require_generation=False)

        listed = await self.mcp.call_tool("board_list", {})
        rows = {
            row["board_id"]: row for row in listed.structured_content["boards"]
        }
        self.assertEqual(set(rows), {"healthy", "with-tombstone"})
        self.assertEqual(rows["healthy"]["roles"], ["worker"])
        self.assertEqual(rows["with-tombstone"]["roles"], ["worker"])
        self.assertIn("AI-retired", rows["with-tombstone"]["agent_ids"])
        self.assertIn("AI-invalid-role", rows["with-tombstone"]["agent_ids"])
        self.assertIn("AI-invalid-role-type", rows["with-tombstone"]["agent_ids"])
        self.assertNotIn("admin", rows["with-tombstone"]["roles"])

        self.principal = self.outsider
        hidden = await self.mcp.call_tool("board_list", {})
        self.assertEqual(hidden.structured_content["boards"], [])
