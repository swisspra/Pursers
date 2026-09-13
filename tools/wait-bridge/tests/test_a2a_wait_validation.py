from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402


class A2AWaitValidationTests(unittest.IsolatedAsyncioTestCase):
    async def assert_tool_error(
        self, arguments: dict[str, object], message: str
    ) -> None:
        with self.assertRaises(ToolError) as caught:
            await wait_server.mcp.call_tool("a2a_wait", arguments)
        self.assertEqual(
            str(caught.exception),
            f"Error executing tool a2a_wait: {message}",
        )

    async def test_boards_type_error_reaches_mcp_caller(self) -> None:
        await self.assert_tool_error(
            {"boards": "pursers"},
            'boards must be a list of board IDs or "registry"; '
            "received 'pursers'",
        )

    async def test_wait_for_value_error_reaches_mcp_caller(self) -> None:
        await self.assert_tool_error(
            {"wait_for": "work_offered"},
            "wait_for must be one of 'auto', 'claimable', or 'submitted'; "
            "received 'work_offered'",
        )

    async def test_since_seq_shape_error_reaches_mcp_caller(self) -> None:
        await self.assert_tool_error(
            {"since_seq": {"pursers": 42}},
            "since_seq must be a non-negative integer when boards is omitted; "
            "received {'pursers': 42}",
        )

    async def test_timeout_lower_bound_error_reaches_mcp_caller(self) -> None:
        with patch.dict(os.environ, {"PURSERS_HOST": "codex"}):
            await self.assert_tool_error(
                {"timeout_s": 0},
                "timeout_s must be an integer from 1 to 560; received 0",
            )

    async def test_timeout_upper_bound_error_reaches_mcp_caller(self) -> None:
        with patch.dict(os.environ, {"PURSERS_HOST": "codex"}):
            await self.assert_tool_error(
                {"timeout_s": 561},
                "timeout_s must be an integer from 1 to 560; received 561",
            )

    def test_invalid_cursor_map_reports_the_received_shape(self) -> None:
        with self.assertRaisesRegex(
            ToolError,
            "since_seq maps must use non-empty board ID strings and "
            "non-negative integer cursors; received .*'pursers': -1",
        ):
            wait_server._validate_a2a_wait_arguments(
                since_seq={"pursers": -1},
                timeout_s=180,
                boards=["pursers"],
                wait_for="auto",
            )


if __name__ == "__main__":
    unittest.main()
