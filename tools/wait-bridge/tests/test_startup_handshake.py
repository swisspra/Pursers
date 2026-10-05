from __future__ import annotations

import asyncio
import base64
import hashlib
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
from pursers_client import (  # noqa: E402
    BoardClientError,
    CentralInstanceMismatchError,
    JoinedIdentity,
)
import pursers_wait_server as wait_server  # noqa: E402

TEST_TIMEOUT_S = float(os.environ.get("PURSERS_TEST_TIMEOUT_S", "30"))


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
    async def test_zed_stdio_wait_failure_is_structured_and_preserves_cursor(
        self,
    ) -> None:
        fixture = ROOT / "tests" / "fixtures" / "zed_stdio_wait_server.py"
        env = os.environ.copy()
        env.update(
            {
                "ONBOARD_CENTRAL_TOKEN": "TOKEN_PLACEHOLDER",
                "PURSERS_HOST": "zed",
                "PURSERS_WAIT_MODE": "push",
                "PYTHONPATH": os.pathsep.join((str(CLIENT_SRC), str(ROOT))),
            }
        )
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(fixture)],
            env=env,
        )
        async with Client(
            params,
            mode="2026-07-28",
            read_timeout_seconds=TEST_TIMEOUT_S,
        ) as client:
            result = await client.call_tool(
                "a2a_wait",
                {
                    "agent_name": "zed-seat",
                    "boards": ["pursers"],
                    "only_mine": True,
                    "since_seq": {"pursers": 38_519},
                    "timeout_s": 1,
                    "wait_for": "claimable",
                },
            )

        self.assertFalse(result.is_error)
        payload = dict(result.structured_content or {})
        value = dict(payload.get("result", payload))
        self.assertEqual(value["new_seq"], {"pursers": 38_519})
        self.assertEqual(value["events"], [])
        self.assertEqual(value["mode"], "error")
        self.assertEqual(value["reason"], "push_unavailable")
        self.assertEqual(
            value["error"]["action"], "rearm_from_unchanged_cursor"
        )
        rendered = repr(value)
        self.assertNotIn("TOKEN_PLACEHOLDER", rendered)
        self.assertNotIn("/private/host/path", rendered)

    async def test_zed_stdio_deferred_auth_failure_is_structured(
        self,
    ) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _UnauthorizedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                env = os.environ.copy()
                env.update(
                    {
                        "ONBOARD_CENTRAL_URL": (
                            f"http://127.0.0.1:{server.server_port}/mcp"
                        ),
                        "ONBOARD_CENTRAL_TOKEN": "known-bad-token",
                        "ONBOARD_BOARD_ID": "pursers",
                        "ONBOARD_AGENT_NAME": "zed-seat",
                        "PURSERS_BRIDGE_STATS": str(
                            Path(temporary) / "bridge-stats.json"
                        ),
                        "PURSERS_HOST": "zed",
                        "PURSERS_WAIT_MODE": "push",
                        "PYTHONPATH": os.pathsep.join(
                            (str(CLIENT_SRC), str(ROOT))
                        ),
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
                    read_timeout_seconds=TEST_TIMEOUT_S,
                ) as client:
                    result = await client.call_tool(
                        "a2a_wait",
                        {
                            "agent_name": "zed-seat",
                            "boards": ["pursers"],
                            "only_mine": True,
                            "since_seq": {"pursers": 38_519},
                            "timeout_s": 1,
                            "wait_for": "claimable",
                        },
                    )

            self.assertFalse(result.is_error)
            payload = dict(result.structured_content or {})
            value = dict(payload.get("result", payload))
            self.assertEqual(value["new_seq"], {"pursers": 38_519})
            self.assertEqual(value["events"], [])
            self.assertEqual(value["mode"], "error")
            self.assertEqual(value["reason"], "push_unavailable")
            self.assertEqual(value["error"]["cause_class"], "authentication")
            self.assertFalse(value["error"]["retryable"])
            self.assertEqual(
                value["error"]["action"],
                "repair_authentication_then_rearm_from_unchanged_cursor",
            )
            rendered = repr(value)
            self.assertNotIn("known-bad-token", rendered)
            self.assertNotIn(temporary, rendered)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=TEST_TIMEOUT_S)

    async def test_main_defers_mismatched_explicit_token_sources(self) -> None:
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
            run.assert_called_once_with(transport="stdio")
            self.assertNotIn("inherited-admin-token", stderr.getvalue())
            self.assertNotIn(raw, stderr.getvalue())

    async def test_main_defers_empty_token_file_with_stored_door(self) -> None:
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
                    run.assert_called_once_with(transport="stdio")
                    self.assertNotIn(raw, stderr.getvalue())

    async def test_stdio_runtime_config_failures_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            mismatch = temporary / "mismatch-token"
            mismatch.write_text("file-token\n", encoding="utf-8")
            empty = temporary / "empty-token"
            empty.write_text("", encoding="utf-8")
            unreadable = temporary / "missing-token"

            cases = (
                ("mismatch", mismatch, "inherited-admin-token"),
                ("empty", empty, "inherited-admin-token"),
                ("unreadable", unreadable, "inherited-admin-token"),
            )
            for label, token_file, direct_token in cases:
                with self.subTest(label=label):
                    env = os.environ.copy()
                    env.update(
                        {
                            "ONBOARD_CENTRAL_URL": "http://127.0.0.1:1/mcp",
                            "ONBOARD_CENTRAL_TOKEN": direct_token,
                            "ONBOARD_CENTRAL_TOKEN_FILE": str(token_file),
                            "ONBOARD_BOARD_ID": "pursers",
                            "ONBOARD_AGENT_NAME": "startup-test",
                            "PURSERS_BRIDGE_STATE_DIR": str(temporary / "state"),
                            "PURSERS_BRIDGE_STATS": str(
                                temporary / f"{label}-bridge-stats.json"
                            ),
                            "PURSERS_ROLE": "worker",
                            "PURSERS_WAIT_MODE": "push",
                            "PYTHONPATH": os.pathsep.join(
                                (str(CLIENT_SRC), str(ROOT))
                            ),
                        }
                    )
                    env.pop("PURSERS_ALLOW_ENV_TOKEN", None)
                    params = StdioServerParameters(
                        command=sys.executable,
                        args=[str(ROOT / "pursers_wait_server.py")],
                        env=env,
                    )
                    async with Client(
                        params,
                        mode="2026-07-28",
                        read_timeout_seconds=TEST_TIMEOUT_S,
                    ) as client:
                        result = await client.call_tool(
                            "a2a_wait",
                            {
                                "agent_name": "startup-test",
                                "boards": ["pursers"],
                                "only_mine": True,
                                "since_seq": {"pursers": 38_519},
                                "timeout_s": 1,
                                "wait_for": "claimable",
                            },
                        )

                    self.assertFalse(result.is_error)
                    payload = dict(result.structured_content or {})
                    value = dict(payload.get("result", payload))
                    self.assertEqual(value["new_seq"], {"pursers": 38_519})
                    self.assertEqual(value["events"], [])
                    self.assertEqual(value["mode"], "error")
                    self.assertEqual(value["reason"], "push_unavailable")
                    self.assertEqual(
                        value["error"]["cause_class"], "configuration"
                    )
                    self.assertFalse(value["error"]["retryable"])
                    self.assertEqual(
                        value["error"]["action"],
                        "repair_configuration_then_rearm_from_unchanged_cursor",
                    )
                    rendered = repr(value)
                    self.assertNotIn(direct_token, rendered)
                    self.assertNotIn(raw, rendered)

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

    async def test_fingerprint_mismatch_refuses_start_and_match_passes(self) -> None:
        connection = wait_server.DeferredBoardConnection(
            wait_server.BridgeStats(Path(tempfile.gettempdir()) / "unused.json")
        )
        expected = hashlib.sha256(wait_server.CENTRAL_TOKEN.encode("utf-8")).hexdigest()
        with patch.dict(
            os.environ,
            {
                "PURSERS_REQUIRE_TOKEN_MATCH": "1",
                "PURSERS_BOARD_CONNECTOR_TOKEN_SHA256": "0" * 64,
                "PURSERS_BOARD_CONNECTOR_TOKEN": wait_server.CENTRAL_TOKEN,
            },
        ):
            with self.assertRaisesRegex(
                wait_server.BoardJoinFailure, "split identity"
            ) as raised:
                await connection.client()
            self.assertNotIn(wait_server.CENTRAL_TOKEN, str(raised.exception))
        with patch.dict(
            os.environ,
            {
                "PURSERS_REQUIRE_TOKEN_MATCH": "1",
                "PURSERS_BOARD_CONNECTOR_TOKEN_SHA256": expected,
                "PURSERS_BOARD_CONNECTOR_TOKEN": "different-token",
            },
        ):
            self.assertIsNone(wait_server._split_identity_failure())

    async def test_raw_token_fallback_remains_compatible(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PURSERS_REQUIRE_TOKEN_MATCH": "1",
                "PURSERS_BOARD_CONNECTOR_TOKEN_SHA256": "",
                "PURSERS_BOARD_CONNECTOR_TOKEN": wait_server.CENTRAL_TOKEN,
            },
        ):
            self.assertIsNone(wait_server._split_identity_failure())

    async def test_missing_connector_token_has_actionable_configuration_error(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PURSERS_REQUIRE_TOKEN_MATCH": "1",
                "PURSERS_BOARD_CONNECTOR_TOKEN_SHA256": "",
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

    async def test_failed_teardown_does_not_reuse_closed_client(self) -> None:
        for error_type in (RuntimeError, asyncio.CancelledError):
            with self.subTest(error_type=error_type.__name__):
                clients: list[object] = []

                class FailingCloseClient:
                    def __init__(self, *_args: object, **_kwargs: object) -> None:
                        self.identity = None
                        self.closed = False
                        clients.append(self)

                    async def __aenter__(self) -> "FailingCloseClient":
                        return self

                    async def __aexit__(self, *_args: object) -> None:
                        self.closed = True
                        if self is clients[0]:
                            raise error_type("synthetic transport teardown failure")

                connection = wait_server.DeferredBoardConnection(
                    wait_server.BridgeStats(
                        Path(tempfile.gettempdir()) / "unused.json"
                    )
                )
                with patch.object(
                    wait_server, "MeteredBoardClient", FailingCloseClient
                ):
                    try:
                        first = await connection.client()
                        with self.assertRaises(error_type):
                            await connection.close()
                        second = await connection.client()
                        self.assertIsNot(second, first)
                        self.assertTrue(first.closed)
                        self.assertFalse(second.closed)
                        self.assertEqual(len(clients), 2)
                    finally:
                        try:
                            await connection.close()
                        except (RuntimeError, asyncio.CancelledError):
                            pass

    async def test_transport_failure_recycles_once_and_three_rearms_recover(
        self,
    ) -> None:
        constructed: list[object] = []
        closed: list[object] = []

        class ReconnectClient:
            def __init__(self, *_args: object, **kwargs: object) -> None:
                self.meter = kwargs["meter"]
                self.identity: JoinedIdentity | None = None
                constructed.append(self)

            async def __aenter__(self) -> "ReconnectClient":
                return self

            async def __aexit__(self, *_args: object) -> None:
                closed.append(self)
                if self is constructed[0]:
                    raise ConnectionError("synthetic close transport failure")

            async def board_join(self, **_kwargs: object) -> dict[str, object]:
                self.identity = JoinedIdentity(
                    "pursers", "AI-test", "PR-test", "startup-test", "worker"
                )
                return {
                    "agent_id": "AI-test",
                    "agent_name": "startup-test",
                    "principal_id": "PR-test",
                    "role": "worker",
                }

        class Keepalive:
            def start(self) -> None:
                return None

        async def wait_once(
            client: ReconnectClient,
            since_seq: dict[str, int],
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            if client is constructed[0]:
                raise ConnectionError("synthetic Central handshake timeout")
            return {
                "new_seq": dict(since_seq),
                "events": [],
                "waited_s": 1.0,
                "timed_out": True,
                "mode": "push",
                "mode_by_board": {"pursers": "push"},
                "reason": "timeout",
                "resynced": {"pursers": False},
                "skipped_boards": {},
            }

        with tempfile.TemporaryDirectory() as raw:
            meter = wait_server.BridgeStats(Path(raw) / "stats.json")
            connection = wait_server.DeferredBoardConnection(meter)
            context = type("Context", (), {})()
            context.request_context = type("RequestContext", (), {})()
            context.request_context.lifespan_context = {
                "connection": connection,
                "lease_keepalive": Keepalive(),
                "meter": meter,
            }
            cursor = {"pursers": 48_476}
            results = []
            with (
                patch.object(wait_server, "MeteredBoardClient", ReconnectClient),
                patch.object(wait_server, "_a2a_wait_impl", wait_once),
                patch.object(wait_server, "WAIT_MODE", "push"),
            ):
                for _ in range(4):
                    result = await wait_server.a2a_wait(
                        context,
                        since_seq=cursor,
                        timeout_s=1,
                        boards=["pursers"],
                        agent_name="startup-test",
                        wait_for="claimable",
                    )
                    results.append(result)
                    cursor = dict(result["new_seq"])
                await connection.close()

        self.assertEqual(results[0]["reason"], "push_unavailable")
        self.assertEqual(results[0]["error"]["cause_class"], "transport")
        self.assertEqual(
            [result["mode"] for result in results[1:]],
            ["push", "push", "push"],
        )
        self.assertEqual(
            [result["new_seq"] for result in results],
            [
                {"pursers": 48_476},
                {"pursers": 48_476},
                {"pursers": 48_476},
                {"pursers": 48_476},
            ],
        )
        self.assertEqual(len(constructed), 2)
        self.assertEqual(closed, constructed)

    async def test_board_join_rejection_has_board_cause_class(self) -> None:
        failure = wait_server._classify_board_join_failure(
            BoardClientError("board does not exist")
        )

        self.assertEqual(failure.cause_class, "board")
        self.assertIn("board join failed (board)", str(failure))

    async def test_instance_mismatch_preserves_safe_typed_detail(self) -> None:
        detail = (
            "Central instance mismatch: selected profile does not match "
            "the connected Central"
        )
        failure = wait_server._classify_board_join_failure(
            CentralInstanceMismatchError(detail)
        )

        self.assertEqual(failure.cause_class, "instance_mismatch")
        self.assertEqual(str(failure), detail)

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
                read_timeout_seconds=TEST_TIMEOUT_S,
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
            thread.join(timeout=TEST_TIMEOUT_S)

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
