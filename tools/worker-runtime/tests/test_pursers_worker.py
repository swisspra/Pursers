from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


MODULE_PATH = Path(__file__).parents[1] / "pursers_worker.py"
SPEC = importlib.util.spec_from_file_location("pursers_worker", MODULE_PATH)
assert SPEC and SPEC.loader
worker_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker_module
SPEC.loader.exec_module(worker_module)


class FakeBoard:
    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.releases: list[str] = []
        self.renewals = 0
        self.claims: list[tuple[str, str]] = []
        self.work: Path | None = None
        self.on_submit: Any = None
        self.waited = False

    async def wait(self, _cursors: dict[str, int]) -> dict[str, Any]:
        self.waited = True
        return {
            "new_seq": {"board-one": 1},
            "events": [
                {"board_id": "board-one", "ticket_id": "TK-scratch"}
            ],
        }

    async def claim(self, board_id: str, ticket_id: str) -> dict[str, Any]:
        self.claims.append((board_id, ticket_id))
        return {"ok": True}

    async def ticket(self, _board_id: str, ticket_id: str) -> dict[str, Any]:
        return {"ticket_id": ticket_id, "required_fields": ["test_output"]}

    async def work_dir(self, _board_id: str) -> Path:
        assert self.work is not None
        return self.work

    async def submit(
        self, board_id: str, ticket_id: str, arguments: dict[str, Any]
    ) -> None:
        self.submissions.append(
            {"board_id": board_id, "ticket_id": ticket_id, **arguments}
        )
        if self.on_submit is not None:
            self.on_submit()

    async def release(self, _board_id: str, _ticket_id: str, reason: str) -> None:
        self.releases.append(reason)

    async def renew(self, _board_id: str, _ticket_id: str) -> None:
        self.renewals += 1


class FakeLLMServer:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.authorizations: list[str | None] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                owner.authorizations.append(self.headers.get("Authorization"))
                owner.requests.append(json.loads(self.rfile.read(length)))
                message = owner.responses.pop(0)
                body = json.dumps({"choices": [{"message": message}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeLLMServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def config(root: Path, base_url: str, *, max_iterations: int = 4) -> Any:
    return worker_module.Config(
        agent_name="worker-one",
        central_url="https://central.invalid/mcp",
        token_file=root / "token",
        boards="registry",
        base_url=base_url,
        api_key_env="WORKER_TEST_KEY",
        api_key_file=None,
        api_key_keychain=None,
        model="test-model",
        max_tokens=500,
        max_iterations=max_iterations,
        command_timeout_s=3,
        log_file=root / "session.log",
    )


def test_fake_server_happy_path_claim_edit_submit_and_secret_free_log() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        board.work = work
        with FakeLLMServer(
            [
                tool_call("one", "write_file", {"path": "result.txt", "content": "done"}),
                tool_call(
                    "two",
                    "submit_work",
                    {
                        "summary": "completed scratch work",
                        "files_changed": ["result.txt"],
                        "notes": "test_output: fake suite passed",
                    },
                ),
            ]
        ) as server:
            selected = config(root, server.url)
            worker = worker_module.Worker(
                selected,
                board,
                worker_module.OpenAICompatible(selected, "API_KEY_PRIVATE"),
                worker_module.SessionLog(selected.log_file),
                directive="STATIC DIRECTIVE",
            )
            board.on_submit = worker.stop.set
            asyncio.run(worker.run())

        assert board.waited is True
        assert board.claims == [("board-one", "TK-scratch")]
        assert (work / "result.txt").read_text() == "done"
        assert board.submissions[0]["ticket_id"] == "TK-scratch"
        assert server.requests[0]["messages"][0] == {
            "role": "system",
            "content": "STATIC DIRECTIVE",
        }
        log = selected.log_file.read_text()
        assert "API_KEY_PRIVATE" not in log
        assert "TOKEN_PRIVATE" not in log
        assert "done" not in log


def test_path_escape_rejected_then_give_up_releases_claim() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        with FakeLLMServer(
            [
                tool_call("one", "write_file", {"path": "../escape", "content": "bad"}),
                tool_call("two", "give_up", {"reason": "path rejected"}),
            ]
        ) as server:
            selected = config(root, server.url)
            worker = worker_module.Worker(
                selected,
                board,
                worker_module.OpenAICompatible(selected, "key"),
                worker_module.SessionLog(selected.log_file),
                directive="STATIC",
            )
            asyncio.run(
                worker.run_ticket("board-one", {"ticket_id": "TK-one"}, work)
            )

        assert not (root / "escape").exists()
        assert board.releases == ["path rejected"]
        assert "PermissionError" in server.requests[1]["messages"][-1]["content"]


def test_shell_cannot_inherit_configured_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SYNTHETIC_API_KEY_MUST_NOT_ESCAPE"
    monkeypatch.setenv("WORKER_TEST_KEY", secret)
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        with FakeLLMServer(
            [
                tool_call(
                    "one",
                    "run_shell",
                    {"command": "printf '%s' \"$WORKER_TEST_KEY\""},
                ),
                tool_call("two", "give_up", {"reason": "probe complete"}),
            ]
        ) as server:
            selected = config(root, server.url)
            worker = worker_module.Worker(
                selected,
                board,
                worker_module.OpenAICompatible(selected, secret),
                worker_module.SessionLog(selected.log_file),
                directive="STATIC",
            )
            asyncio.run(
                worker.run_ticket("board-one", {"ticket_id": "TK-one"}, work)
            )

        tool_output = server.requests[1]["messages"][-1]["content"]
        assert secret not in tool_output
        assert secret not in selected.log_file.read_text()


def test_shell_cannot_return_or_log_seat_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SYNTHETIC_SEAT_TOKEN_MUST_NOT_ESCAPE"
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        private_home = root / "private-home"
        private_home.mkdir()
        token_file = private_home / "seat.jwt"
        token_file.write_text(secret)
        token_file.chmod(0o600)
        monkeypatch.setenv("HOME", str(private_home))
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        with FakeLLMServer(
            [
                tool_call(
                    "one",
                    "run_shell",
                    {"command": "cat \"$HOME/seat.jwt\""},
                ),
                tool_call(
                    "two",
                    "run_shell",
                    {"command": f"cat {token_file}"},
                ),
                tool_call("three", "give_up", {"reason": secret}),
            ]
        ) as server:
            selected = replace(
                config(root, server.url), token_file=token_file.resolve()
            )
            worker = worker_module.Worker(
                selected,
                board,
                worker_module.OpenAICompatible(selected, "key"),
                worker_module.SessionLog(selected.log_file, secrets=(secret,)),
                directive="STATIC",
            )
            asyncio.run(
                worker.run_ticket("board-one", {"ticket_id": "TK-one"}, work)
            )

        inherited_home_output = server.requests[1]["messages"][-1]["content"]
        direct_path_output = server.requests[2]["messages"][-1]["content"]
        assert secret not in inherited_home_output
        assert secret not in direct_path_output
        if worker_module.sys.platform == "darwin" and worker_module.SANDBOX_EXEC.is_file():
            assert "[REDACTED]" not in direct_path_output
        assert secret not in selected.log_file.read_text()
        assert worker.log.redact(secret) == "[REDACTED]"


def test_max_iterations_releases_claim() -> None:
    class NoTools:
        async def complete(self, _messages: Any, _tools: Any) -> dict[str, Any]:
            return {"content": "still thinking"}

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        selected = config(root, "http://unused", max_iterations=2)
        worker = worker_module.Worker(
            selected,
            board,
            NoTools(),
            worker_module.SessionLog(selected.log_file),
            directive="STATIC",
        )

        result = asyncio.run(
            worker.run_ticket("board-one", {"ticket_id": "TK-one"}, work)
        )

        assert result == "released"
        assert board.releases == ["max iterations reached"]


def test_lease_is_renewed_while_ticket_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowNoTools:
        async def complete(self, _messages: Any, _tools: Any) -> dict[str, Any]:
            await asyncio.sleep(0.03)
            return {"content": "still thinking"}

    monkeypatch.setattr(worker_module, "LEASE_INTERVAL_S", 0.005)
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        selected = config(root, "http://unused", max_iterations=1)
        worker = worker_module.Worker(
            selected,
            board,
            SlowNoTools(),
            worker_module.SessionLog(selected.log_file),
            directive="STATIC",
        )

        asyncio.run(worker.run_ticket("board-one", {"ticket_id": "TK-one"}, work))

        assert board.renewals > 0


def test_config_requires_mode_0600_and_never_accepts_inline_keys() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        token = root / "token"
        token.write_text("TOKEN_PRIVATE")
        token.chmod(0o600)
        path = root / "worker.json"
        path.write_text(
            json.dumps(
                {
                    "seat": {
                        "agent_name": "worker-one",
                        "central_url": "https://central.invalid/mcp",
                        "token_file": str(token),
                    },
                    "boards": "registry",
                    "llm": {
                        "base_url": "http://proxy.invalid/v1",
                        "api_key_env": "PROXY_KEY",
                        "model": "model-one",
                    },
                }
            )
        )
        path.chmod(0o644)
        with pytest.raises(PermissionError, match="0600"):
            worker_module.load_config(path)
        path.chmod(0o600)

        loaded = worker_module.load_config(path)

        assert loaded.api_key_env == "PROXY_KEY"
        assert loaded.api_key_file is None

        document = json.loads(path.read_text())
        document["llm"]["api_key"] = "INLINE_FORBIDDEN"
        path.write_text(json.dumps(document))
        path.chmod(0o600)
        with pytest.raises(ValueError, match="inline"):
            worker_module.load_config(path)


def test_static_directive_prefix_is_byte_identical_across_tickets() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        selected = config(root, "http://unused")
        worker = worker_module.Worker(
            selected,
            FakeBoard(),
            object(),
            worker_module.SessionLog(selected.log_file),
            directive="STATIC DIRECTIVE\nbyte identical",
        )
        first = worker.messages("board-one", {"ticket_id": "TK-one"}, root)
        second = worker.messages("board-two", {"ticket_id": "TK-two"}, root)

        first_prefix = json.dumps(first[0], separators=(",", ":")).encode()
        second_prefix = json.dumps(second[0], separators=(",", ":")).encode()
        assert first_prefix == second_prefix
        assert first[1] != second[1]
        assert first[2] != second[2]


def test_keychain_config_uses_exact_security_argv_and_never_exposes_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token = tmp_path / "seat.jwt"
    token.write_text("TOKEN_PRIVATE")
    token.chmod(0o600)
    config_path = tmp_path / "worker.toml"
    config_path.write_text(
        'boards = "registry"\n'
        '[seat]\nagent_name = "keychain-worker"\n'
        'central_url = "https://central.invalid/mcp"\n'
        f'token_file = "{token}"\n'
        '[llm]\nbase_url = "https://provider.invalid/v1"\n'
        'api_key_keychain = "keychain-worker"\nmodel = "model-one"\n'
    )
    config_path.chmod(0o600)
    loaded = worker_module.load_config(config_path)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        return type("Result", (), {"stdout": "KEYCHAIN_SECRET\n"})()

    monkeypatch.setattr(worker_module.sys, "platform", "darwin")
    monkeypatch.setattr(worker_module.subprocess, "run", fake_run)

    assert worker_module.read_secret(loaded, "api") == "KEYCHAIN_SECRET"
    assert calls == [[
        "/usr/bin/security", "find-generic-password", "-s", "pursers-worker",
        "-a", "keychain-worker", "-w",
    ]]
    assert "KEYCHAIN_SECRET" not in config_path.read_text()


def test_keyless_loopback_is_allowed_but_remote_requires_a_key_source(
    tmp_path: Path,
) -> None:
    token = tmp_path / "seat.jwt"
    token.write_text("TOKEN_PRIVATE")
    token.chmod(0o600)

    def write_config(base_url: str) -> Path:
        path = tmp_path / "worker.toml"
        path.write_text(
            'boards = "registry"\n'
            '[seat]\nagent_name = "ollama-worker"\n'
            'central_url = "https://central.invalid/mcp"\n'
            f'token_file = "{token}"\n'
            f'[llm]\nbase_url = "{base_url}"\nmodel = "local-model"\n'
        )
        path.chmod(0o600)
        return path

    loaded = worker_module.load_config(write_config("http://127.0.0.1:11434/v1"))
    assert worker_module.read_secret(loaded, "api") == ""
    with FakeLLMServer([{"content": "ok"}]) as server:
        loaded = worker_module.load_config(write_config(server.url))
        result = asyncio.run(
            worker_module.OpenAICompatible(loaded, "").complete([], [])
        )
        assert result == {"content": "ok"}
        assert server.authorizations == [None]
    with pytest.raises(ValueError, match="exactly one"):
        worker_module.load_config(write_config("https://provider.invalid/v1"))
