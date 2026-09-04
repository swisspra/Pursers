from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_ROOT))

import pursers_wait_server as wait_server  # noqa: E402


class BridgeLegacyToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_list_tools_callable(self) -> None:
        """Wait bridge list_tools returns available tools."""
        tools = await wait_server.mcp.list_tools()
        names = {t.name for t in tools}
        expected = {
            "project_registry_get",
            "board_digest",
            "board_digest_ack",
            "board_watch",
            "board_unwatch",
            "a2a_wait",
        }
        for name in expected:
            self.assertIn(name, names)

    async def test_board_join_passes_legacy_capability_when_env_set(self) -> None:
        """MeteredBoardClient.board_join includes legacy_tools capability when PURSERS_LEGACY_TOOLS=1."""
        client = wait_server.MeteredBoardClient(
            "https://127.0.0.1:8766/mcp",
            "test-token",
            "pursers",
            agent_name="test-worker",
            meter=wait_server.BridgeStats(wait_server.bridge_stats_path()),
        )
        client._call_refresh = AsyncMock(return_value={
            "board_id": "pursers",
            "agent_id": "AI-test",
            "principal_id": "PR-test",
            "agent_name": "test-worker",
            "role": "worker",
        })

        with patch.dict(os.environ, {"PURSERS_LEGACY_TOOLS": "1"}):
            await client.board_join()
            client._call_refresh.assert_called_once()
            call_args = client._call_refresh.call_args[0]
            self.assertEqual(call_args[0], "board_join")
            self.assertEqual(call_args[1].get("capabilities"), {"legacy_tools": True})

    async def test_board_join_omits_legacy_capability_when_env_unset(self) -> None:
        """MeteredBoardClient.board_join does not set legacy_tools capability by default."""
        client = wait_server.MeteredBoardClient(
            "https://127.0.0.1:8766/mcp",
            "test-token",
            "pursers",
            agent_name="test-worker",
            meter=wait_server.BridgeStats(wait_server.bridge_stats_path()),
        )
        client._call_refresh = AsyncMock(return_value={
            "board_id": "pursers",
            "agent_id": "AI-test",
            "principal_id": "PR-test",
            "agent_name": "test-worker",
            "role": "worker",
        })

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PURSERS_LEGACY_TOOLS", None)
            await client.board_join()
            client._call_refresh.assert_called_once()
            call_args = client._call_refresh.call_args[0]
            self.assertEqual(call_args[0], "board_join")
            self.assertNotIn("capabilities", call_args[1])
