from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from mcp import Client  # noqa: E402
from mcp.client.stdio import StdioServerParameters  # noqa: E402
from pursers_client import BoardClientError, JoinedIdentity  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402


class _UnauthorizedHandler(BaseHTTPRequestHandler):
    def _reject(self) -> None:
        self.send_response(401)
        self.end_headers()
        self.wfile.write(b"unauthorized")

    do_DELETE = _reject
    do_GET = _reject
    do_POST = _reject

    def log_message(self, _format: str, *_arguments: object) -> None:
        return


class StartupHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_split_identity_refuses_start_and_matching_token_passes(self) -> None:
        connection = wait_server.DeferredBoardConnection(
            wait_server.BridgeStats(Path(tempfile.gettempdir()) / "unused.json")
        )
        with patch.dict(
            os.environ,
            {
                "PURSERS_REQUIRE_TOKEN_MATCH": "1",
                "PURSERS_BOARD_CONNECTOR_TOKEN": "different-token",
            },
        ):
            with self.assertRaisesRegex(wait_server.BoardJoinFailure, "split identity"):
                await connection.client()
        with patch.dict(
            os.environ,
            {
                "PURSERS_REQUIRE_TOKEN_MATCH": "1",
                "PURSERS_BOARD_CONNECTOR_TOKEN": wait_server.CENTRAL_TOKEN,
            },
        ):
            self.assertIsNone(wait_server._split_identity_failure())

    async def test_missing_connector_token_has_actionable_configuration_error(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PURSERS_REQUIRE_TOKEN_MATCH": "1",
                "PURSERS_BOARD_CONNECTOR_TOKEN": "",
            },
        ):
            failure = wait_server._split_identity_failure()
        self.assertIsNotNone(failure)
        self.assertEqual(
            str(failure),
            "board join failed (configuration): connector token not visible to the "
            "bridge process; see Codex env forwarding",
        )

    async def test_healthy_connection_is_joined_lazily_and_closed_by_owner(
        self,
    ) -> None:
        events: list[str] = []

        class HealthyClient:
            def __init__(self, *_args: object, **kwargs: object) -> None:
                events.append(f"constructed:{kwargs['role']}")
                self.identity: JoinedIdentity | None = None

            async def __aenter__(self) -> "HealthyClient":
                events.append("join")
                self.identity = JoinedIdentity(
                    "pursers", "AI-test", "PR-test", "startup-test", "worker"
                )
                return self

            async def __aexit__(self, *_args: object) -> None:
                events.append("close")

        connection = wait_server.DeferredBoardConnection(
            wait_server.BridgeStats(Path(tempfile.gettempdir()) / "unused.json")
        )
        self.assertEqual(events, [])
        with (
            patch.dict(os.environ, {"PURSERS_ROLE": "reviewer"}),
            patch.object(wait_server, "MeteredBoardClient", HealthyClient),
        ):
            first = await connection.client()
            second = await connection.client()
            self.assertIs(first, second)
            self.assertEqual(events, ["constructed:reviewer", "join"])
            await connection.close()
        self.assertEqual(events, ["constructed:reviewer", "join", "close"])

    async def test_board_join_rejection_has_board_cause_class(self) -> None:
        failure = wait_server._classify_board_join_failure(
            BoardClientError("board does not exist")
        )

        self.assertEqual(failure.cause_class, "board")
        self.assertIn("board join failed (board)", str(failure))

    async def _initialize_then_call(
        self,
        *,
        central_url: str,
        token: str,
        expected_error: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env.update(
                {
                    "ONBOARD_CENTRAL_URL": central_url,
                    "ONBOARD_CENTRAL_TOKEN": token,
                    "ONBOARD_BOARD_ID": "pursers",
                    "ONBOARD_AGENT_NAME": "startup-test",
                    "PURSERS_BRIDGE_STATS": str(
                        Path(temporary) / "bridge-stats.json"
                    ),
                    "PYTHONPATH": os.pathsep.join((str(CLIENT_SRC), str(ROOT))),
                }
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(ROOT / "pursers_wait_server.py")],
                env=env,
            )
            async with Client(
                params,
                mode="2026-07-28",
                read_timeout_seconds=5,
            ) as client:
                tools = await client.list_tools()
                self.assertIn(
                    "project_registry_get", {tool.name for tool in tools.tools}
                )
                result = await client.call_tool("project_registry_get", {})
                self.assertTrue(result.is_error)
                rendered = " ".join(
                    block.text
                    for block in result.content
                    if getattr(block, "type", None) == "text"
                )
                self.assertIn(expected_error, rendered)

    async def test_initialize_succeeds_with_bad_token_then_tool_reports_auth(
        self,
    ) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _UnauthorizedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            await self._initialize_then_call(
                central_url=f"http://127.0.0.1:{server.server_port}/mcp",
                token="known-bad-token",
                expected_error=(
                    "board join failed (auth): Central rejected "
                    "ONBOARD_CENTRAL_TOKEN"
                ),
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    async def test_initialize_succeeds_with_unreachable_central_then_tool_reports_it(
        self,
    ) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        await self._initialize_then_call(
            central_url=f"http://127.0.0.1:{port}/mcp",
            token="syntactically-valid-token",
            expected_error=(
                "board join failed (unreachable): Central is unreachable"
            ),
        )

    async def test_initialize_succeeds_with_empty_token_then_tool_reports_config(
        self,
    ) -> None:
        await self._initialize_then_call(
            central_url="http://127.0.0.1:1/mcp",
            token="",
            expected_error=(
                "board join failed (configuration): "
                "ONBOARD_CENTRAL_TOKEN is not set"
            ),
        )


if __name__ == "__main__":
    unittest.main()
