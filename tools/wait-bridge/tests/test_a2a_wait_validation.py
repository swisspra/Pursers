from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from pursers_client import BoardClientError  # noqa: E402
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

    async def test_unexpected_push_failure_returns_safe_structured_result(
        self,
    ) -> None:
        class Client:
            meter = None

        async def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError("TOKEN_PLACEHOLDER /private/host/path")

        context = SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context={"client": Client()}
            )
        )
        with (
            patch.object(wait_server, "WAIT_MODE", "push"),
            patch.object(wait_server, "_a2a_wait_impl", fail),
        ):
            result = await wait_server.a2a_wait(
                context,
                since_seq={"pursers": 38_519},
                timeout_s=1,
                boards=["pursers"],
                agent_name="zed-seat",
                wait_for="claimable",
            )

        self.assertEqual(result["new_seq"], {"pursers": 38_519})
        self.assertEqual(result["events"], [])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["mode"], "error")
        self.assertEqual(result["reason"], "push_unavailable")
        self.assertEqual(result["mode_by_board"], {"pursers": "error"})
        self.assertEqual(result["resynced"], {"pursers": False})
        self.assertEqual(
            result["error"],
            {
                "code": "push_unavailable",
                "cause_class": "runtime",
                "exception_classes": ["RuntimeError"],
                "message": (
                    "a2a_wait could not complete; no caller cursor was advanced. "
                    "Check the wait-bridge/Central transport, then re-arm from "
                    "new_seq."
                ),
                "retryable": True,
                "cursor_preserved": True,
                "action": "rearm_from_unchanged_cursor",
            },
        )
        rendered = repr(result)
        self.assertNotIn("TOKEN_PLACEHOLDER", rendered)
        self.assertNotIn("/private/host/path", rendered)

    async def test_unexpected_client_setup_failure_is_also_structured(
        self,
    ) -> None:
        async def fail_setup(_context: object) -> object:
            raise ConnectionError("TOKEN_PLACEHOLDER /private/host/path")

        context = SimpleNamespace(request_context=SimpleNamespace())
        with (
            patch.object(wait_server, "WAIT_MODE", "push"),
            patch.object(wait_server, "_client_for_tool", fail_setup),
        ):
            result = await wait_server.a2a_wait(
                context,
                since_seq={"pursers": 38_519},
                timeout_s=1,
                boards=["pursers"],
                agent_name="zed-seat",
                wait_for="claimable",
            )

        self.assertEqual(result["new_seq"], {"pursers": 38_519})
        self.assertEqual(result["reason"], "push_unavailable")
        self.assertEqual(result["error"]["cause_class"], "transport")
        self.assertEqual(
            result["error"]["exception_classes"], ["ConnectionError"]
        )
        rendered = repr(result)
        self.assertNotIn("TOKEN_PLACEHOLDER", rendered)
        self.assertNotIn("/private/host/path", rendered)

    async def test_deferred_join_failure_is_structured_but_argument_errors_are_not(
        self,
    ) -> None:
        async def fail_setup(_context: object) -> object:
            raise wait_server.BoardJoinFailure(
                "configuration", "TOKEN_PLACEHOLDER /private/host/path"
            )

        context = SimpleNamespace(request_context=SimpleNamespace())
        with (
            patch.object(wait_server, "WAIT_MODE", "push"),
            patch.object(wait_server, "_client_for_tool", fail_setup),
        ):
            result = await wait_server.a2a_wait(
                context,
                since_seq={"pursers": 38_519},
                timeout_s=1,
                boards=["pursers"],
                agent_name="zed-seat",
                wait_for="claimable",
            )

        self.assertEqual(result["new_seq"], {"pursers": 38_519})
        self.assertEqual(result["error"]["cause_class"], "configuration")
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(
            result["error"]["action"],
            "repair_configuration_then_rearm_from_unchanged_cursor",
        )
        rendered = repr(result)
        self.assertNotIn("TOKEN_PLACEHOLDER", rendered)
        self.assertNotIn("/private/host/path", rendered)

    async def test_runtime_auth_failure_is_redacted_and_not_blindly_retryable(
        self,
    ) -> None:
        class Client:
            meter = None

        async def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise BoardClientError(
                "401 unauthorized TOKEN_PLACEHOLDER /private/host/path"
            )

        context = SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context={"client": Client()}
            )
        )
        with (
            patch.object(wait_server, "WAIT_MODE", "push"),
            patch.object(wait_server, "_a2a_wait_impl", fail),
        ):
            result = await wait_server.a2a_wait(
                context,
                since_seq={"pursers": 38_519},
                timeout_s=1,
                boards=["pursers"],
                agent_name="zed-seat",
                wait_for="claimable",
            )

        self.assertEqual(result["new_seq"], {"pursers": 38_519})
        self.assertEqual(result["error"]["cause_class"], "authentication")
        self.assertFalse(result["error"]["retryable"])
        self.assertEqual(
            result["error"]["action"],
            "repair_authentication_then_rearm_from_unchanged_cursor",
        )
        rendered = repr(result)
        self.assertNotIn("TOKEN_PLACEHOLDER", rendered)
        self.assertNotIn("/private/host/path", rendered)


if __name__ == "__main__":
    unittest.main()
