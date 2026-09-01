from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

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
        self.events = [{"board_id": "board-one", "ticket_id": "TK-scratch"}]
        self.tickets: dict[str, dict[str, Any]] = {}
        self.identity = "AI-worker-one"

    async def wait(self, _cursors: dict[str, int]) -> dict[str, Any]:
        self.waited = True
        return {
            "new_seq": {"board-one": 1},
            "events": self.events,
        }

    async def claim(self, board_id: str, ticket_id: str) -> dict[str, Any]:
        self.claims.append((board_id, ticket_id))
        return {"ok": True}

    async def ticket(self, _board_id: str, ticket_id: str) -> dict[str, Any]:
        return self.tickets.get(
            ticket_id,
            {
                "ticket_id": ticket_id,
                "status": "open",
                "tags": [],
                "required_fields": ["test_output"],
            },
        )

    async def agent_id(self, _board_id: str) -> str:
        return self.identity

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
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
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


def config(
    root: Path,
    base_url: str,
    *,
    max_iterations: int = 4,
    max_tier: str = "heavy",
    require_assigned_only: bool = False,
) -> Any:
    return worker_module.Config(
        agent_name="worker-one",
        central_url="https://central.invalid/mcp",
        token_file=root / "token",
        boards="registry",
        base_url=base_url,
        api_key_env="WORKER_TEST_KEY",
        api_key_file=None,
        model="test-model",
        max_tokens=500,
        max_iterations=max_iterations,
        command_timeout_s=3,
        log_file=root / "session.log",
        max_tier=max_tier,
        require_assigned_only=require_assigned_only,
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


@pytest.mark.parametrize(
    ("max_tier", "ticket_tier", "expected"),
    [
        ("light", "light", 1),
        ("light", "standard", None),
        ("light", "heavy", None),
        ("standard", "light", 1),
        ("standard", "standard", 1),
        ("standard", "heavy", None),
        ("heavy", "light", 1),
        ("heavy", "standard", 1),
        ("heavy", "heavy", 1),
    ],
)
def test_tier_filter_matrix(
    max_tier: str, ticket_tier: str, expected: int | None
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        selected = config(Path(raw), "http://unused", max_tier=max_tier)
        ticket = {"status": "open", "tags": [f"tier:{ticket_tier}"]}
        assert worker_module.claim_priority(selected, ticket, "AI-worker-one") == expected


def test_absent_tier_defaults_to_standard_and_assigned_only_is_enforced() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        light = config(root, "http://unused", max_tier="light")
        assigned = config(
            root,
            "http://unused",
            max_tier="standard",
            require_assigned_only=True,
        )
        ticket = {"status": "open", "tags": []}

        assert worker_module.ticket_tier(ticket) == "standard"
        assert worker_module.claim_priority(light, ticket, "AI-worker-one") is None
        assert worker_module.claim_priority(assigned, ticket, "AI-worker-one") is None
        ticket["assigned_to_agent_id"] = "AI-worker-one"
        assert worker_module.claim_priority(assigned, ticket, "AI-worker-one") == 0


def test_max_tier_light_skips_heavy_and_claims_light() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        board.work = work
        board.events = [
            {"board_id": "board-one", "ticket_id": "TK-heavy"},
            {"board_id": "board-one", "ticket_id": "TK-light"},
        ]
        board.tickets = {
            "TK-heavy": {
                "ticket_id": "TK-heavy",
                "status": "open",
                "tags": ["tier:heavy"],
            },
            "TK-light": {
                "ticket_id": "TK-light",
                "status": "open",
                "tags": ["tier:light"],
            },
        }
        with FakeLLMServer(
            [tool_call("one", "submit_work", {"summary": "light done"})]
        ) as server:
            selected = config(root, server.url, max_tier="light")
            worker = worker_module.Worker(
                selected,
                board,
                worker_module.OpenAICompatible(selected, "key"),
                worker_module.SessionLog(selected.log_file),
                directive="STATIC",
            )
            board.on_submit = worker.stop.set
            asyncio.run(worker.run())

        assert board.claims == [("board-one", "TK-light")]
        transcript = selected.log_file.read_text()
        assert '"event":"ticket_skipped"' in transcript
        assert '"ticket_id":"TK-heavy"' in transcript
        assert '"ticket_tier":"heavy"' in transcript


def test_fresh_light_api_advertises_before_idle_wait_and_blocks_heavy_dispatch() -> None:
    class Result:
        is_error = False
        content: list[Any] = []

        def __init__(self, value: dict[str, Any]) -> None:
            self.structured_content = {"result": value}

    class Transport:
        def __init__(self) -> None:
            self.principal_id = "PR-tier-integration"
            self.profiles: dict[str, dict[str, Any]] = {}
            self.join_calls: list[tuple[str, dict[str, Any]]] = []

        async def call_tool(
            self, name: str, arguments: dict[str, Any], **_options: Any
        ) -> Result:
            board_id = str(arguments["board_id"])
            if name == "board_join":
                profile = {
                    "board_id": board_id,
                    "agent_id": worker_module.wait_bridge._derived_agent_id(
                        self.principal_id,
                        str(arguments["agent_name"]),
                        board_id,
                    ),
                    "principal_id": self.principal_id,
                    "agent_name": arguments["agent_name"],
                    "role": "worker",
                    "status": "active",
                    "membership_role": "member",
                    "last_activity_at": datetime.now(timezone.utc).isoformat(),
                    "task_focus": arguments.get("task_focus"),
                }
                self.profiles[board_id] = profile
                self.join_calls.append((board_id, dict(arguments)))
                return Result(profile)
            if name == "board_catchup":
                return Result(
                    {
                        "events": [],
                        "next_cursor": 0,
                        "has_more": False,
                        "resync_required": False,
                    }
                )
            if name == "ticket_list":
                return Result({"tickets": []})
            raise AssertionError(f"unexpected tool: {name}")

    async def scenario() -> tuple[Transport, bool]:
        with tempfile.TemporaryDirectory() as raw:
            selected = replace(
                config(Path(raw), "http://unused", max_tier="light"),
                boards=("alpha", "beta"),
            )
            api = worker_module.PursersBoardAPI(selected, "TOKEN_PLACEHOLDER")
            transport = Transport()
            api.client = SimpleNamespace(
                _client=transport,
                agent_name=selected.agent_name,
                identity=worker_module.wait_bridge.JoinedIdentity(
                    worker_module.wait_bridge.BOARD_ID,
                    worker_module.wait_bridge._derived_agent_id(
                        transport.principal_id,
                        selected.agent_name,
                    ),
                    transport.principal_id,
                    selected.agent_name,
                    "worker",
                ),
            )
            all_pending = True
            with (
                patch.object(worker_module.wait_bridge, "WAIT_MODE", "poll"),
                patch.object(
                    worker_module.wait_bridge, "DEFAULT_POLL_INTERVAL_S", 0.01
                ),
            ):
                for expected_joins in (2, 4):
                    waiting = asyncio.create_task(api.wait({}))
                    for _ in range(100):
                        if len(transport.join_calls) == expected_joins:
                            break
                        await asyncio.sleep(0)
                    all_pending = all_pending and not waiting.done()
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
            return transport, all_pending

    transport, was_pending = asyncio.run(scenario())
    assert was_pending is True
    assert set(transport.profiles) == {"alpha", "beta"}
    assert all(
        profile["task_focus"] == "worker-runtime max_tier=light"
        for profile in transport.profiles.values()
    )
    assert all(
        call["task_focus"] == "worker-runtime max_tier=light"
        for _board_id, call in transport.join_calls
    )
    assert [board_id for board_id, _call in transport.join_calls] == [
        "alpha",
        "beta",
        "alpha",
        "beta",
    ]

    coordinator_path = (
        Path(__file__).parents[2] / "coordinator" / "coordinator.py"
    )
    coordinator_spec = importlib.util.spec_from_file_location(
        "worker_tier_coordinator", coordinator_path
    )
    assert coordinator_spec and coordinator_spec.loader
    coordinator = importlib.util.module_from_spec(coordinator_spec)
    sys.modules[coordinator_spec.name] = coordinator
    coordinator_spec.loader.exec_module(coordinator)
    now = datetime.now(timezone.utc)
    snapshot = {
        "alpha": {
            "agents": [transport.profiles["alpha"]],
            "tickets": [
                {
                    "ticket_id": "TK-heavy",
                    "status": "open",
                    "priority": "medium",
                    "created_at": (now - timedelta(hours=2)).isoformat(),
                    "tags": ["tier:heavy"],
                }
            ],
        }
    }
    assert coordinator.plan_actions(
        snapshot, {"alpha": {"drop_history": []}}, {}, now
    ) == []


def test_assigned_ticket_is_claimed_before_earlier_unassigned_ticket() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        board.work = work
        board.events = [
            {"board_id": "board-one", "ticket_id": "TK-unassigned"},
            {"board_id": "board-one", "ticket_id": "TK-assigned"},
        ]
        board.tickets = {
            "TK-unassigned": {
                "ticket_id": "TK-unassigned",
                "status": "open",
                "tags": [],
            },
            "TK-assigned": {
                "ticket_id": "TK-assigned",
                "status": "open",
                "tags": [],
                "assigned_to_agent_id": board.identity,
            },
        }
        with FakeLLMServer(
            [tool_call("one", "submit_work", {"summary": "assigned done"})]
        ) as server:
            selected = config(root, server.url)
            worker = worker_module.Worker(
                selected,
                board,
                worker_module.OpenAICompatible(selected, "key"),
                worker_module.SessionLog(selected.log_file),
                directive="STATIC",
            )
            board.on_submit = worker.stop.set
            asyncio.run(worker.run())

        assert board.claims == [("board-one", "TK-assigned")]


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
        assert loaded.max_tier == "heavy"
        assert loaded.require_assigned_only is False

        document = json.loads(path.read_text())
        document["claim"] = {
            "max_tier": "light",
            "require_assigned_only": True,
        }
        path.write_text(json.dumps(document))
        path.chmod(0o600)
        loaded = worker_module.load_config(path)
        assert loaded.max_tier == "light"
        assert loaded.require_assigned_only is True

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
