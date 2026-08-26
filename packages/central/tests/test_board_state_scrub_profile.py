from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


class BoardStateScrubProfileTests(unittest.IsolatedAsyncioTestCase):
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
        for board_id in ("internal-board", "strict-board"):
            joined = await self.call(
                board_id,
                "board_join",
                agent_name="admin-agent",
            )
            self.assertFalse(joined.is_error)

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, board_id: str, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name,
            {"board_id": board_id, **arguments},
        )

    async def test_board_state_update_honors_board_scrub_profile(self) -> None:
        changed = await self.call(
            "internal-board",
            "board_scrub_profile_set",
            agent_name="admin-agent",
            scrub_profile="internal",
        )
        self.assertFalse(changed.is_error)

        accepted = await self.call(
            "internal-board",
            "board_state_update",
            agent_name="admin-agent",
            key="project_registry",
            value="/Users/example/project",
        )

        self.assertFalse(accepted.is_error)
        self.assertEqual(
            accepted.structured_content["state"]["value"],
            "/Users/example/project",
        )
        readback = await self.call(
            "internal-board",
            "board_state_get",
            key="project_registry",
        )
        self.assertFalse(readback.is_error)
        self.assertEqual(
            readback.structured_content["state"]["value"],
            "/Users/example/project",
        )

        self.service.mutate(
            "strict-board",
            lambda document: document["config"].pop("scrub_profile", None),
        )
        with self.assertRaisesRegex(ToolError, "posix_home"):
            await self.call(
                "strict-board",
                "board_state_update",
                agent_name="admin-agent",
                key="project_registry",
                value="/Users/example/project",
            )
        self.assertNotIn(
            "project_registry",
            self.service.load("strict-board")["state"],
        )


if __name__ == "__main__":
    unittest.main()
