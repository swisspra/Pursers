from __future__ import annotations

import argparse
import base64
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

import door_state  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402


def _segment(value: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def door(
    *,
    url: str = "http://127.0.0.1:8766/mcp",
    board: str = "sandbox",
    role: str = "worker",
    kid: str = "test-key",
    exp: int = 2_000_000_000,
    signature: str = "synthetic-signature",
) -> str:
    encoded_signature = base64.urlsafe_b64encode(signature.encode()).decode().rstrip("=")
    token = f"{_segment({'alg': 'RS256', 'kid': kid})}.{_segment({'exp': exp})}.{encoded_signature}"
    envelope = {"u": url, "b": board, "r": role, "t": token}
    return f"prs1.{_segment(envelope)}"


class DoorStateTests(unittest.TestCase):
    def test_store_is_atomic_private_and_selects_by_board_role(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state" / "doors.json"
            replacements: list[tuple[object, object]] = []
            real_replace = os.replace

            def observed_replace(source: object, target: object) -> None:
                replacements.append((source, target))
                real_replace(source, target)

            with patch.object(door_state.os, "replace", observed_replace):
                door_state.store(path, door(board="alpha"))
                door_state.store(path, door(board="beta", role="reviewer"))

            self.assertTrue(replacements)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            document = door_state.load(path)
            selected = door_state.select(document, board="beta", role="reviewer")
            self.assertEqual((selected["b"], selected["r"]), ("beta", "reviewer"))

    def test_explicit_environment_wins_field_by_field(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "doors.json"
            door_state.store(path, door(url="http://127.0.0.1:9000/mcp"))
            stored_token = door_state.load(path)["doors"][0]["t"]
            env = {
                "PURSERS_BRIDGE_STATE_DIR": raw,
                "ONBOARD_CENTRAL_URL": "https://explicit.example/mcp",
                "ONBOARD_BOARD_ID": "sandbox",
                "PURSERS_ROLE": "worker",
            }
            resolved = door_state.resolve(env)
            self.assertEqual(resolved["url"], "https://explicit.example/mcp")
            self.assertEqual(resolved["token"], stored_token)
            env["ONBOARD_CENTRAL_TOKEN"] = "explicit-token"
            self.assertEqual(door_state.resolve(env)["token"], "explicit-token")

    def test_token_file_precedes_stored_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            door_state.store(state / "doors.json", door())
            token_file = state / "explicit.txt"
            token_file.write_text("explicit-file-token\n", encoding="utf-8")
            resolved = door_state.resolve(
                {
                    "PURSERS_BRIDGE_STATE_DIR": raw,
                    "ONBOARD_CENTRAL_TOKEN_FILE": str(token_file),
                    "ONBOARD_BOARD_ID": "sandbox",
                    "PURSERS_ROLE": "worker",
                }
            )
            self.assertEqual(resolved["token"], "explicit-file-token")

    def test_omitted_board_and_role_fail_closed_when_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "doors.json"
            door_state.store(path, door(board="alpha"))
            door_state.store(path, door(board="beta"))
            with self.assertRaisesRegex(ValueError, "ONBOARD_BOARD_ID"):
                door_state.resolve({"PURSERS_BRIDGE_STATE_DIR": raw})
            door_state.store(path, door(board="alpha", role="reviewer"))
            with self.assertRaisesRegex(ValueError, "PURSERS_ROLE"):
                door_state.resolve(
                    {
                        "PURSERS_BRIDGE_STATE_DIR": raw,
                        "ONBOARD_BOARD_ID": "alpha",
                    }
                )

    def test_remote_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "doors.json"
            with self.assertRaisesRegex(ValueError, "--allow-remote"):
                door_state.store(path, door(url="https://central.example/mcp"))
            door_state.store(
                path,
                door(url="https://central.example/mcp"),
                allow_remote=True,
            )

    def test_rotate_preserves_names_and_forget_removes_entry(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "doors.json"
            door_state.store(path, door(signature="first"))
            with patch.object(door_state.socket, "gethostname", return_value="Host.One"):
                first = door_state.reserve_name(path, "sandbox", "worker")
                second = door_state.reserve_name(path, "sandbox", "worker")
            self.assertEqual(first, "worker-host-1")
            self.assertEqual(second, "worker-host-2")
            with self.assertRaisesRegex(ValueError, "use --rotate"):
                door_state.store(path, door(signature="second"))
            door_state.store(path, door(signature="second"), rotate=True)
            self.assertEqual(
                door_state.load(path)["doors"][0]["seat_names_used"],
                [first, second],
            )
            self.assertTrue(door_state.forget(path, "sandbox", "worker"))
            self.assertFalse(door_state.load(path)["doors"])


class DoorCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_never_enables_takeover_after_successful_enter(self) -> None:
        calls: dict[str, dict[str, object]] = {}

        class RecordingClient:
            def __init__(self, *_args: object, **kwargs: object) -> None:
                calls["init"] = kwargs

            async def __aenter__(self) -> object:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def board_onboard(self, **kwargs: object) -> dict[str, str]:
                calls["onboard"] = kwargs
                return {"agent_id": "AI-test"}

        with tempfile.TemporaryDirectory() as raw:
            args = argparse.Namespace(
                door=door(),
                name="worker-fixed-1",
                state_dir=raw,
                allow_remote=False,
                rotate=False,
            )
            with (
                patch.object(wait_server, "BoardClient", RecordingClient),
                redirect_stdout(io.StringIO()),
            ):
                await wait_server._door_join(args)

        self.assertIs(calls["init"]["allow_takeover"], False)
        self.assertIs(calls["onboard"]["allow_takeover"], False)

    async def test_join_surfaces_central_collision_verbatim(self) -> None:
        class RefusingClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            async def __aenter__(self) -> object:
                raise wait_server.BoardClientError("seat name already active under this principal")

            async def __aexit__(self, *_args: object) -> None:
                return None

        with tempfile.TemporaryDirectory() as raw:
            args = argparse.Namespace(
                door=door(),
                name="worker-fixed-1",
                state_dir=raw,
                allow_remote=False,
                rotate=False,
            )
            with patch.object(wait_server, "BoardClient", RefusingClient):
                with self.assertRaisesRegex(
                    wait_server.BoardClientError,
                    "seat name already active under this principal",
                ):
                    await wait_server._door_join(args)

    def test_status_and_forget_never_print_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            value = door()
            path = door_state.state_path(raw)
            entry = door_state.store(path, value)
            door_state.reserve_name(path, "sandbox", "worker", "worker-a")
            status_args = argparse.Namespace(state_dir=raw)
            output = io.StringIO()
            with redirect_stdout(output):
                wait_server._door_status(status_args)
            rendered = output.getvalue()
            self.assertNotIn(entry["t"], rendered)
            self.assertIn("board=sandbox role=worker kid=test-key", rendered)
            forget_args = argparse.Namespace(
                state_dir=raw, board="sandbox", role="worker"
            )
            with redirect_stdout(io.StringIO()):
                wait_server._door_forget(forget_args)
            self.assertFalse(door_state.load(path)["doors"])

    def test_runtime_door_reserves_explicit_name_and_disables_takeover(self) -> None:
        original = (
            wait_server.CENTRAL_URL,
            wait_server.CENTRAL_TOKEN,
            wait_server.BOARD_ID,
            wait_server.RUNTIME_ROLE,
            wait_server.RUNTIME_FROM_DOOR,
            wait_server.BASE_AGENT_NAME,
            wait_server.AGENT_NAME,
        )
        with tempfile.TemporaryDirectory() as raw:
            path = door_state.state_path(raw)
            door_state.store(path, door())
            try:
                with patch.dict(
                    os.environ,
                    {
                        "PURSERS_BRIDGE_STATE_DIR": raw,
                        "PURSERS_ROLE": "worker",
                        "ONBOARD_AGENT_NAME": "worker-explicit",
                    },
                    clear=True,
                ):
                    wait_server._configure_runtime()
                self.assertTrue(wait_server.RUNTIME_FROM_DOOR)
                self.assertEqual(wait_server.AGENT_NAME, "worker-explicit")
                self.assertEqual(
                    door_state.load(path)["doors"][0]["seat_names_used"],
                    ["worker-explicit"],
                )
            finally:
                (
                    wait_server.CENTRAL_URL,
                    wait_server.CENTRAL_TOKEN,
                    wait_server.BOARD_ID,
                    wait_server.RUNTIME_ROLE,
                    wait_server.RUNTIME_FROM_DOOR,
                    wait_server.BASE_AGENT_NAME,
                    wait_server.AGENT_NAME,
                ) = original


if __name__ == "__main__":
    unittest.main()
