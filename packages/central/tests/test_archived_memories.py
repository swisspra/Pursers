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


class ArchivedMemoryTests(unittest.IsolatedAsyncioTestCase):
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
        self.mcp, _service = central.build_server(
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

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name, {"board_id": "pursers", **arguments}
        )

    async def test_oversize_archive_is_preserved_and_searchable_only_on_request(
        self,
    ) -> None:
        marker = "needle-in-preserved-archive"
        content = "\n" + "x" * 12_000 + marker + "\n"
        archived = await self.call(
            "memory_write",
            agent_name="admin-agent",
            title="Oversize archived memory",
            content=content,
            scope="project",
            archived=True,
            archive_source_id="v4-memory-22",
            archived_at="2025-02-03T04:05:06Z",
        )

        self.assertFalse(archived.is_error)
        memory = archived.structured_content["memory"]
        self.assertEqual(memory["content"], content)
        self.assertTrue(memory["archived"])
        self.assertIn("archived", memory["tags"])

        default_search = await self.call("memory_search", query=marker)
        self.assertEqual(default_search.structured_content["results"], [])
        archive_search = await self.call(
            "memory_search", query=marker, include_archived=True
        )
        self.assertEqual(len(archive_search.structured_content["results"]), 1)
        self.assertEqual(
            archive_search.structured_content["results"][0]["content"], content
        )
        default_read = await self.call("memory_read", agent_name="admin-agent")
        self.assertEqual(default_read.structured_content["memories"], [])
        archive_read = await self.call(
            "memory_read", agent_name="admin-agent", include_archived=True
        )
        self.assertEqual(len(archive_read.structured_content["memories"]), 1)

    async def test_normal_memory_write_keeps_the_10000_character_limit(self) -> None:
        with self.assertRaisesRegex(Exception, "at most 10000 characters"):
            await self.call(
                "memory_write",
                agent_name="admin-agent",
                title="Too large normal memory",
                content="x" * 10_001,
                scope="project",
            )

    async def test_archive_write_still_applies_scrub_policy(self) -> None:
        result = await self.call(
            "memory_write",
            agent_name="admin-agent",
            title="Unsafe archive",
            content="Bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            scope="project",
            archived=True,
        )

        self.assertFalse(result.is_error)
        self.assertFalse(result.structured_content["ok"])
        self.assertEqual(result.structured_content["error"], "write rejected by scrub policy")


if __name__ == "__main__":
    unittest.main()
