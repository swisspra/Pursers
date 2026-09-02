from __future__ import annotations

import asyncio
import importlib.util
import json
import stat
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
        self.principal = "PR-reviewer"
        self.reviews: list[dict[str, Any]] = []

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
        if ticket_id in self.tickets:
            ticket = self.tickets[ticket_id]
            submission = {
                **arguments,
                "submitted_at": f"submission-{len(self.submissions)}",
                "submitted_by_agent_id": self.identity,
                "submitted_by_principal_id": "PR-worker",
            }
            ticket.setdefault("submission_history", []).append(submission)
            ticket.update(submission)
            ticket["status"] = "submitted"
        if self.on_submit is not None:
            self.on_submit()

    async def release(self, _board_id: str, _ticket_id: str, reason: str) -> None:
        self.releases.append(reason)

    async def renew(self, _board_id: str, _ticket_id: str) -> None:
        self.renewals += 1

    async def submitted(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            ("board-one", ticket)
            for ticket in self.tickets.values()
            if ticket.get("status") == "submitted"
        ]

    async def principal_id(self, _board_id: str) -> str:
        return self.principal

    async def review(
        self,
        board_id: str,
        ticket_id: str,
        verdict: str,
        *,
        review_notes: str,
        fix_instructions: str | None,
    ) -> None:
        self.reviews.append(
            {
                "board_id": board_id,
                "ticket_id": ticket_id,
                "verdict": verdict,
                "review_notes": review_notes,
                "fix_instructions": fix_instructions,
            }
        )
        ticket = self.tickets[ticket_id]
        ticket["status"] = "closed" if verdict == "approve" else "open"
        if fix_instructions is not None:
            ticket["fix_instructions"] = fix_instructions


class BlockingBoard(FakeBoard):
    def __init__(self) -> None:
        super().__init__()
        self.wait_cancelled = False

    async def wait(self, _cursors: dict[str, int]) -> dict[str, Any]:
        self.waited = True
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.wait_cancelled = True
            raise


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

    def __enter__(self) -> FakeLLMServer:
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
    role: str = "worker",
    max_reviews_per_hour: int = 12,
) -> Any:
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
        max_tier=max_tier,
        require_assigned_only=require_assigned_only,
        role=role,
        max_reviews_per_hour=max_reviews_per_hour,
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
                tool_call(
                    "one", "write_file", {"path": "result.txt", "content": "done"}
                ),
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
        assert (
            worker_module.claim_priority(selected, ticket, "AI-worker-one") == expected
        )


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


def test_fresh_light_api_advertises_before_idle_wait_and_blocks_heavy_dispatch() -> (
    None
):
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
        profile["task_focus"] == "worker-runtime role=worker max_tier=light"
        for profile in transport.profiles.values()
    )
    assert all(
        call["task_focus"] == "worker-runtime role=worker max_tier=light"
        for _board_id, call in transport.join_calls
    )
    assert [board_id for board_id, _call in transport.join_calls] == [
        "alpha",
        "beta",
        "alpha",
        "beta",
    ]

    coordinator_path = Path(__file__).parents[2] / "coordinator" / "coordinator.py"
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
    assert (
        coordinator.plan_actions(snapshot, {"alpha": {"drop_history": []}}, {}, now)
        == []
    )


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


def test_stop_interrupts_blocked_board_wait() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        board = BlockingBoard()
        selected = config(root, "http://unused")
        worker = worker_module.Worker(
            selected,
            board,
            object(),
            worker_module.SessionLog(selected.log_file),
            directive="STATIC",
        )

        async def exercise() -> None:
            running = asyncio.create_task(worker.run())
            while not board.waited:
                await asyncio.sleep(0)
            worker.stop.set()
            await asyncio.wait_for(running, timeout=1)

        asyncio.run(exercise())

        assert board.wait_cancelled is True


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
            asyncio.run(worker.run_ticket("board-one", {"ticket_id": "TK-one"}, work))

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
            asyncio.run(worker.run_ticket("board-one", {"ticket_id": "TK-one"}, work))

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
                    {"command": 'cat "$HOME/seat.jwt"'},
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
            asyncio.run(worker.run_ticket("board-one", {"ticket_id": "TK-one"}, work))

        inherited_home_output = server.requests[1]["messages"][-1]["content"]
        direct_path_output = server.requests[2]["messages"][-1]["content"]
        assert secret not in inherited_home_output
        assert secret not in direct_path_output
        if (
            worker_module.sys.platform == "darwin"
            and worker_module.SANDBOX_EXEC.is_file()
        ):
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
        assert loaded.role == "worker"
        assert loaded.max_reviews_per_hour == 12

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

        document["seat"]["role"] = "reviewer"
        document["review"] = {"max_reviews_per_hour": 7}
        path.write_text(json.dumps(document))
        path.chmod(0o600)
        loaded = worker_module.load_config(path)
        assert loaded.role == "reviewer"
        assert loaded.max_reviews_per_hour == 7

        document["seat"]["role"] = "invalid"
        path.write_text(json.dumps(document))
        path.chmod(0o600)
        with pytest.raises(ValueError, match="seat.role"):
            worker_module.load_config(path)

        document["seat"]["role"] = "worker"

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
    assert calls == [
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "pursers-worker",
            "-a",
            "keychain-worker",
            "-w",
        ]
    ]
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


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"verdict": "approve", "review_notes": "All required evidence verified."},
            ("approve", None),
        ),
        (
            {
                "verdict": "reject",
                "review_notes": "Focused test fails.",
                "fix_instructions": "Fix the failing boundary case and rerun tests.",
            },
            ("reject", "Fix the failing boundary case and rerun tests."),
        ),
    ],
)
def test_parse_review_verdict_accepts_only_complete_structured_results(
    arguments: dict[str, Any], expected: tuple[str, str | None]
) -> None:
    parsed = worker_module.parse_review_verdict(arguments)
    assert (parsed.verdict, parsed.fix_instructions) == expected


@pytest.mark.parametrize(
    "garbage",
    [
        "approve",
        {},
        {"verdict": "maybe", "review_notes": "unclear"},
        {"verdict": "reject", "review_notes": "bad"},
        {
            "verdict": "approve",
            "review_notes": "looks fine",
            "fix_instructions": "but change this",
        },
        {"verdict": "approve", "review_notes": "fine", "extra": True},
    ],
)
def test_parse_review_verdict_rejects_garbage(garbage: Any) -> None:
    with pytest.raises(ValueError):
        worker_module.parse_review_verdict(garbage)


@pytest.mark.parametrize(
    ("arguments", "expected_result", "expected_reviews"),
    [
        (
            {"verdict": "approve", "review_notes": "Verified commit and tests."},
            "approve",
            1,
        ),
        (
            {
                "verdict": "reject",
                "review_notes": "Regression reproduced.",
                "fix_instructions": "Correct the regression and add a test.",
            },
            "reject",
            1,
        ),
        (
            {"verdict": "reject", "review_notes": "Missing fix instructions."},
            "skipped",
            0,
        ),
    ],
)
def test_fake_llm_reviewer_approve_reject_and_garbage(
    arguments: dict[str, Any], expected_result: str, expected_reviews: int
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        board = FakeBoard()
        board.tickets["TK-review"] = {
            "ticket_id": "TK-review",
            "status": "submitted",
            "tags": ["tier:light"],
            "required_fields": ["test_output"],
            "submitted_by_principal_id": "PR-worker",
            "submission_history": [
                {
                    "summary": "candidate",
                    "notes": "test_output: passed",
                    "submitted_at": "submission-1",
                    "submitted_by_principal_id": "PR-worker",
                }
            ],
        }
        with FakeLLMServer(
            [tool_call("verdict", "submit_review", arguments)]
        ) as server:
            selected = config(root, server.url, role="reviewer")
            reviewer = worker_module.Reviewer(
                selected,
                board,
                worker_module.OpenAICompatible(selected, "key"),
                worker_module.SessionLog(selected.log_file),
                directive="STATIC REVIEWER",
            )
            result = asyncio.run(
                reviewer.run_review("board-one", board.tickets["TK-review"], root)
            )

        assert result == expected_result
        assert len(board.reviews) == expected_reviews
        assert board.claims == []
        assert board.submissions == []
        assert server.requests[0]["messages"][0] == {
            "role": "system",
            "content": "STATIC REVIEWER",
        }
        assert not reviewer.log.review_state_path.exists()


def test_session_log_persists_and_clears_bounded_review_state(
    tmp_path: Path,
) -> None:
    log = worker_module.SessionLog(tmp_path / "reviewer.log")

    log.write(
        "review_started",
        board_id="board-one",
        ticket_id="TK-active",
        submitted_at="submission-1",
        submission_digest="abc123",
    )
    state = json.loads(log.review_state_path.read_text())
    assert state == {
        "schema": 1,
        "board_id": "board-one",
        "ticket_id": "TK-active",
        "submitted_at": "submission-1",
        "submission_digest": "abc123",
    }
    assert stat.S_IMODE(log.review_state_path.stat().st_mode) == 0o600

    for index in range(25):
        log.write("review_run_shell", command=f"check-{index}")
    assert json.loads(log.review_state_path.read_text()) == state

    log.write(
        "review_finished",
        board_id="board-one",
        ticket_id="TK-active",
        outcome="approve",
    )
    assert not log.review_state_path.exists()


def test_session_log_runtime_session_fences_stale_review_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reviewer.log"
    original = worker_module.SessionLog(path)
    original.begin_session("reviewer")
    original.write(
        "review_started",
        board_id="board-one",
        ticket_id="TK-stale",
    )
    for index in range(25):
        original.write("review_run_shell", command=f"stale-{index}")

    replacement = worker_module.SessionLog(path)
    replacement.begin_session("reviewer")
    assert not replacement.review_state_path.exists()
    assert json.loads(path.read_text().splitlines()[-1])["event"] == (
        "runtime_session_started"
    )

    replacement.write(
        "review_started",
        board_id="board-one",
        ticket_id="TK-active",
    )
    for index in range(25):
        replacement.write("review_run_shell", command=f"replacement-{index}")
    assert json.loads(replacement.review_state_path.read_text())["ticket_id"] == (
        "TK-active"
    )

    replacement.write(
        "review_finished",
        board_id="board-one",
        ticket_id="TK-active",
        outcome="approve",
    )
    assert not replacement.review_state_path.exists()


def test_reviewer_refuses_verdict_when_submission_changes_during_review() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        board = FakeBoard()
        board.tickets["TK-race"] = {
            "ticket_id": "TK-race",
            "status": "submitted",
            "tags": ["tier:light"],
            "submitted_at": "submission-1",
            "submitted_by_principal_id": "PR-worker",
            "submission_history": [
                {
                    "summary": "revision one",
                    "notes": "test_output: first",
                    "submitted_at": "submission-1",
                    "submitted_by_principal_id": "PR-worker",
                }
            ],
        }

        class ResubmittingLLM:
            async def complete(
                self, _messages: list[dict[str, Any]], _tools: list[dict[str, Any]]
            ) -> dict[str, Any]:
                state = json.loads(reviewer.log.review_state_path.read_text())
                assert state["board_id"] == "board-one"
                assert state["ticket_id"] == "TK-race"
                await board.submit(
                    "board-one",
                    "TK-race",
                    {"summary": "revision two", "notes": "test_output: second"},
                )
                return tool_call(
                    "approve-stale",
                    "submit_review",
                    {
                        "verdict": "approve",
                        "review_notes": "Verified revision one.",
                    },
                )

        class ApprovingLLM:
            async def complete(
                self, _messages: list[dict[str, Any]], _tools: list[dict[str, Any]]
            ) -> dict[str, Any]:
                return tool_call(
                    "approve-current",
                    "submit_review",
                    {
                        "verdict": "approve",
                        "review_notes": "Verified revision two.",
                    },
                )

        selected = config(root, "http://unused", role="reviewer")
        reviewer = worker_module.Reviewer(
            selected,
            board,
            ResubmittingLLM(),
            worker_module.SessionLog(selected.log_file),
            directive="STATIC REVIEWER",
        )
        first = asyncio.run(
            reviewer.run_review("board-one", board.tickets["TK-race"], root)
        )

        assert first == "skipped"
        assert board.reviews == []
        assert board.tickets["TK-race"]["status"] == "submitted"
        assert board.tickets["TK-race"]["summary"] == "revision two"
        assert "submission_revision_changed" in selected.log_file.read_text()
        assert not reviewer.log.review_state_path.exists()

        reviewer.llm = ApprovingLLM()
        second = asyncio.run(
            reviewer.run_review("board-one", board.tickets["TK-race"], root)
        )
        assert second == "approve"
        assert board.tickets["TK-race"]["status"] == "closed"
        assert [review["review_notes"] for review in board.reviews] == [
            "Verified revision two."
        ]
        log_events = [
            json.loads(line)["event"]
            for line in selected.log_file.read_text().splitlines()
        ]
        assert log_events.count("review_started") == 2
        assert log_events.count("review_finished") == 2
        assert not reviewer.log.review_state_path.exists()


def test_reviewer_self_review_probe_skips_before_calling_llm() -> None:
    class NoCallLLM:
        async def complete(self, *_args: Any) -> dict[str, Any]:
            raise AssertionError("self-authored submission must not reach the LLM")

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        board = FakeBoard()
        board.tickets["TK-self"] = {
            "ticket_id": "TK-self",
            "status": "submitted",
            "submitted_at": "submission-1",
            "submitted_by_principal_id": board.principal,
            "submission_history": [
                {
                    "submitted_at": "submission-1",
                    "submitted_by_principal_id": board.principal,
                }
            ],
        }
        selected = config(root, "http://unused", role="reviewer")
        reviewer = worker_module.Reviewer(
            selected,
            board,
            NoCallLLM(),
            worker_module.SessionLog(selected.log_file),
            directive="STATIC REVIEWER",
        )

        async def exercise() -> None:
            running = asyncio.create_task(reviewer.run())
            for _ in range(100):
                if "self_review_refused" in selected.log_file.read_text():
                    break
                await asyncio.sleep(0)
            reviewer.stop.set()
            await asyncio.wait_for(running, timeout=1)

        asyncio.run(exercise())
        assert board.reviews == []
        assert "self_review_refused" in selected.log_file.read_text()


def test_reviewer_write_tool_attempt_is_blocked() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        selected = config(root, "http://unused", role="reviewer")
        reviewer = worker_module.Reviewer(
            selected,
            FakeBoard(),
            object(),
            worker_module.SessionLog(selected.log_file),
            directive="STATIC REVIEWER",
        )
        with pytest.raises(PermissionError, match="unavailable in reviewer mode"):
            asyncio.run(
                reviewer._tool(
                    "write_file", {"path": "forbidden", "content": "no"}, root
                )
            )
        assert not (root / "forbidden").exists()
        with pytest.raises(PermissionError, match="read-only allowlist"):
            worker_module._readonly_command("touch forbidden")
        with pytest.raises(PermissionError, match="write-capable option"):
            worker_module._readonly_command(
                "git diff --no-index /etc/hosts /etc/passwd"
            )


def test_reviewer_test_command_cannot_mutate_project() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        (work / "test_mutation.py").write_text(
            "import pathlib\n"
            "import unittest\n"
            "class MutationTest(unittest.TestCase):\n"
            "    def test_write(self):\n"
            "        pathlib.Path('sentinel.txt').write_text('forbidden')\n"
        )
        selected = config(root, "http://unused", role="reviewer")
        reviewer = worker_module.Reviewer(
            selected,
            FakeBoard(),
            object(),
            worker_module.SessionLog(selected.log_file),
            directive="STATIC REVIEWER",
        )
        asyncio.run(
            reviewer._run_readonly_shell(
                f"'{sys.executable}' -m unittest test_mutation.py", work
            )
        )
        assert not (work / "sentinel.txt").exists()


def test_review_rate_limiter_uses_rolling_hour() -> None:
    now = [100.0]
    limiter = worker_module.ReviewRateLimiter(2, clock=lambda: now[0])
    assert limiter.acquire() is True
    assert limiter.acquire() is True
    assert limiter.acquire() is False
    assert limiter.retry_after() == 3_600
    now[0] += 3_600
    assert limiter.acquire() is True


def test_submitted_ticket_discovery_spans_all_configured_boards() -> None:
    class View:
        def __init__(self, ticket_id: str) -> None:
            self.ticket_id = ticket_id
            self.calls: list[dict[str, Any]] = []

        async def ticket_list(self, **arguments: Any) -> dict[str, Any]:
            self.calls.append(arguments)
            return {
                "tickets": [
                    {"ticket_id": self.ticket_id, "status": "submitted"},
                    {"ticket_id": "TK-open", "status": "open"},
                ]
            }

    with tempfile.TemporaryDirectory() as raw:
        selected = replace(
            config(Path(raw), "http://unused", role="reviewer"),
            boards=("alpha", "beta"),
        )
        api = worker_module.PursersBoardAPI(selected, "TOKEN_PLACEHOLDER")
        alpha = View("TK-alpha")
        beta = View("TK-beta")
        api.views = {"alpha": alpha, "beta": beta}
        discovered = asyncio.run(api.submitted())

        assert [(board, ticket["ticket_id"]) for board, ticket in discovered] == [
            ("alpha", "TK-alpha"),
            ("beta", "TK-beta"),
        ]
        assert (
            alpha.calls
            == beta.calls
            == [{"status": "submitted", "include_closed": False, "limit": 100}]
        )


def test_scratch_board_worker_reject_resubmit_approve_e2e() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        board.work = work
        board.tickets["TK-e2e"] = {
            "ticket_id": "TK-e2e",
            "status": "claimed",
            "tags": ["tier:light"],
            "required_fields": ["test_output"],
        }
        transcript: list[str] = []

        with FakeLLMServer(
            [
                tool_call(
                    "submit-1",
                    "submit_work",
                    {"summary": "first pass", "notes": "test_output: failing"},
                )
            ]
        ) as server:
            worker_config = config(root, server.url)
            worker = worker_module.Worker(
                worker_config,
                board,
                worker_module.OpenAICompatible(worker_config, "key"),
                worker_module.SessionLog(worker_config.log_file),
                directive="STATIC WORKER",
            )
            assert (
                asyncio.run(
                    worker.run_ticket("board-one", board.tickets["TK-e2e"], work)
                )
                == "submitted"
            )
        transcript.append("worker submits -> submitted")

        with FakeLLMServer(
            [
                tool_call(
                    "reject",
                    "submit_review",
                    {
                        "verdict": "reject",
                        "review_notes": "Required test evidence is failing.",
                        "fix_instructions": "Fix the test and resubmit passing output.",
                    },
                )
            ]
        ) as server:
            reviewer_config = config(root, server.url, role="reviewer")
            reviewer = worker_module.Reviewer(
                reviewer_config,
                board,
                worker_module.OpenAICompatible(reviewer_config, "key"),
                worker_module.SessionLog(root / "reviewer.log"),
                directive="STATIC REVIEWER",
            )
            assert (
                asyncio.run(
                    reviewer.run_review("board-one", board.tickets["TK-e2e"], work)
                )
                == "reject"
            )
        transcript.append("API reviewer rejects -> open with fix_instructions")
        assert board.tickets["TK-e2e"]["status"] == "open"
        assert board.tickets["TK-e2e"]["fix_instructions"]

        with FakeLLMServer(
            [
                tool_call(
                    "submit-2",
                    "submit_work",
                    {"summary": "fixed pass", "notes": "test_output: passing"},
                )
            ]
        ) as server:
            worker_config = config(root, server.url)
            worker = worker_module.Worker(
                worker_config,
                board,
                worker_module.OpenAICompatible(worker_config, "key"),
                worker_module.SessionLog(worker_config.log_file),
                directive="STATIC WORKER",
            )
            assert (
                asyncio.run(
                    worker.run_ticket("board-one", board.tickets["TK-e2e"], work)
                )
                == "submitted"
            )
        transcript.append("worker resubmits -> submitted")

        with FakeLLMServer(
            [
                tool_call(
                    "approve",
                    "submit_review",
                    {
                        "verdict": "approve",
                        "review_notes": "Passing evidence verified.",
                    },
                )
            ]
        ) as server:
            reviewer_config = config(root, server.url, role="reviewer")
            reviewer = worker_module.Reviewer(
                reviewer_config,
                board,
                worker_module.OpenAICompatible(reviewer_config, "key"),
                worker_module.SessionLog(root / "reviewer.log"),
                directive="STATIC REVIEWER",
            )
            assert (
                asyncio.run(
                    reviewer.run_review("board-one", board.tickets["TK-e2e"], work)
                )
                == "approve"
            )
        transcript.append("API reviewer approves -> closed")

        assert board.tickets["TK-e2e"]["status"] == "closed"
        assert transcript == [
            "worker submits -> submitted",
            "API reviewer rejects -> open with fix_instructions",
            "worker resubmits -> submitted",
            "API reviewer approves -> closed",
        ]
