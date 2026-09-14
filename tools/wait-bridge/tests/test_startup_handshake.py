from __future__ import annotations

import base64
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
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


def _segment(value: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def _door() -> str:
    signature = base64.urlsafe_b64encode(b"synthetic-signature").decode().rstrip("=")
    token = (
        f"{_segment({'alg': 'RS256', 'kid': 'test-key'})}."
        f"{_segment({'exp': 2_000_000_000})}.{signature}"
    )
    envelope = {
        "u": "http://127.0.0.1:8766/mcp",
        "b": "sandbox",
        "r": "worker",
        "t": token,
    }
    return f"prs1.{_segment(envelope)}"


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
    async def test_main_refuses_mismatched_explicit_token_sources(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            token_file = Path(raw) / "seat-token"
            token_file.write_text("seat-token\n", encoding="utf-8")
            stderr = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "ONBOARD_CENTRAL_TOKEN": "inherited-admin-token",
                        "ONBOARD_CENTRAL_TOKEN_FILE": str(token_file),
                    },
                    clear=True,
                ),
                patch.object(sys, "argv", ["pursers-wait-bridge"]),
                patch.object(wait_server, "_RUNTIME_CONFIG_ERROR", None),
                patch.object(wait_server.mcp, "run") as run,
                redirect_stderr(stderr),
            ):
                wait_server.main()
            run.assert_not_called()
            self.assertIn("FATAL: split identity", stderr.getvalue())

    async def test_main_refuses_empty_token_file_with_stored_door(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            wait_server.door_state.store(state / "doors.json", _door())
            token_file = state / "seat-token"
            for contents in ("", " \n\t"):
                with self.subTest(contents=repr(contents)):
                    token_file.write_text(contents, encoding="utf-8")
                    stderr = io.StringIO()
                    with (
                        patch.dict(
                            os.environ,
                            {
                                "PURSERS_BRIDGE_STATE_DIR": raw,
                                "ONBOARD_CENTRAL_TOKEN_FILE": str(token_file),
                                "ONBOARD_BOARD_ID": "sandbox",
                                "PURSERS_ROLE": "worker",
                            },
                            clear=True,
                        ),
                        patch.object(sys, "argv", ["pursers-wait-bridge"]),
                        patch.object(wait_server, "_RUNTIME_CONFIG_ERROR", None),
                        patch.object(wait_server.mcp, "run") as run,
                        redirect_stderr(stderr),
                    ):
                        wait_server.main()
                    run.assert_not_called()
                    self.assertIn(
                        "FATAL: ONBOARD_CENTRAL_TOKEN_FILE is empty",
                        stderr.getvalue(),
                    )

    async def test_runtime_config_split_identity_is_a_configuration_failure(
        self,
    ) -> None:
        with patch.object(
            wait_server,
            "_RUNTIME_CONFIG_ERROR",
            "split identity: explicit token sources differ",
        ):
            failure = wait_server._split_identity_failure()
        self.assertIsNotNone(failure)
        self.assertEqual(failure.cause_class, "configuration")
        self.assertIn("split identity", str(failure))

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
        active_seats: set[str] = set()

        class HealthyClient:
            def __init__(self, *_args: object, **kwargs: object) -> None:
                events.append(
                    f"constructed:{kwargs['role']}:{kwargs['allow_takeover']}"
                )
                self.agent_name = str(kwargs["agent_name"])
                self.allow_takeover = bool(kwargs["allow_takeover"])
                self.identity: JoinedIdentity | None = None

            async def __aenter__(self) -> "HealthyClient":
                if self.agent_name in active_seats and not self.allow_takeover:
                    raise BoardClientError(
                        "seat name already active under this principal; "
                        "choose another name or pass allow_takeover=true"
                    )
                active_seats.add(self.agent_name)
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
            self.assertEqual(events, ["constructed:reviewer:True", "join"])
            await connection.close()
        self.assertEqual(
            events, ["constructed:reviewer:True", "join", "close"]
        )

        restarted = wait_server.DeferredBoardConnection(
            wait_server.BridgeStats(Path(tempfile.gettempdir()) / "unused.json")
        )
        with patch.object(wait_server, "MeteredBoardClient", HealthyClient):
            await restarted.client()
            await restarted.close()
        self.assertEqual(
            events,
            [
                "constructed:reviewer:True",
                "join",
                "close",
                "constructed:None:True",
                "join",
                "close",
            ],
        )

    async def test_board_join_rejection_has_board_cause_class(self) -> None:
        failure = wait_server._classify_board_join_failure(
            BoardClientError("board does not exist")
        )

        self.assertEqual(failure.cause_class, "board")
        self.assertIn("board join failed (board)", str(failure))

    async def test_permanent_denials_have_denied_cause_class(self) -> None:
        for message in (
            "board access denied: invite required",
            "authenticated principal lacks board:coordinate authorization",
            "board role not authorized",
        ):
            failure = wait_server._classify_board_join_failure(
                BoardClientError(message)
            )
            self.assertEqual(failure.cause_class, "denied", message)
            self.assertNotIn(message, str(failure))

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
