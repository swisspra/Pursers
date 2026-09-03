from __future__ import annotations

import asyncio
import importlib.util
import json
import stat
import subprocess
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
        self.live_claims: set[tuple[str, str]] = set()

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

    async def integration_ref(self, _board_id: str) -> str:
        return "main"

    async def work_specs(self) -> list[tuple[Path, str]]:
        return [] if self.work is None else [(self.work, "main")]

    async def active_claims(self) -> set[tuple[str, str]]:
        return set(self.live_claims)

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

    async def ticket_list(self, board_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        tickets = list(self.tickets.values())
        if "status" in kwargs:
            tickets = [t for t in tickets if t.get("status") == kwargs["status"]]
        if "claimed_by_agent_id" in kwargs:
            tickets = [t for t in tickets if t.get("claimed_by_agent_id") == kwargs["claimed_by_agent_id"]]
        if "claimed_by" in kwargs:
            tickets = [t for t in tickets if t.get("claimed_by") == kwargs["claimed_by"]]
        if "include_closed" in kwargs and not kwargs["include_closed"]:
            tickets = [t for t in tickets if t.get("status") != "closed"]
        return tickets

    async def boards(self) -> list[str]:
        return ["board-one"]

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


def init_git_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Pursers Test"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "pursers-test@example.invalid"],
        cwd=repo,
        check=True,
    )
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True
    )
    return repo


def test_fake_server_happy_path_claim_edit_submit_and_secret_free_log() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        selected = config(Path(raw), "http://unused", max_tier=max_tier)
        ticket = {"status": "open", "tags": [f"tier:{ticket_tier}"]}
        assert (
            worker_module.claim_priority(selected, ticket, "AI-worker-one") == expected
        )


def test_absent_tier_defaults_to_standard_and_assigned_only_is_enforced() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
            and worker_module.Worker._sandbox_available()
        ):
            assert "[REDACTED]" not in direct_path_output
        assert secret not in selected.log_file.read_text()
        assert worker.log.redact(secret) == "[REDACTED]"


def test_max_iterations_releases_claim() -> None:
    class NoTools:
        async def complete(self, _messages: Any, _tools: Any) -> dict[str, Any]:
            return {"content": "still thinking"}

    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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


def test_session_log_write_includes_ts_timestamp() -> None:
    """Every SessionLog.write() call includes a 'ts' field that parses as UTC datetime."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        log = worker_module.SessionLog(Path(raw) / "session.log")
        log.write("test_event", detail="hello")
        line = json.loads(log.path.read_text().splitlines()[-1])
        assert "ts" in line, "ts field must be present"
        ts = datetime.fromisoformat(line["ts"].rstrip("Z"))
        assert ts.tzinfo is not None or line["ts"].endswith("Z"), (
            "ts must be timezone-aware UTC"
        )
        # Verify it parses to a reasonable recent time
        now = datetime.now(timezone.utc)
        delta = now - ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else now - ts
        assert 0 <= delta.total_seconds() < 60, (
            f"ts {line['ts']} is not within the last 60 seconds"
        )


def test_reviewer_refuses_verdict_when_submission_changes_during_review() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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

    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
        if worker_module.Worker._sandbox_available():
            assert not (work / "sentinel.txt").exists()


def test_reviewer_concurrent_review_guard() -> None:
    """Reviewer refuses to start a second review while one is in-flight."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        root = Path(raw)
        work = root / 'work'
        work.mkdir()
        board = FakeBoard()
        board.tickets['TK-review'] = {
            'ticket_id': 'TK-review',
            'status': 'submitted',
            'submitted_at': 'submission-1',
            'submitted_by_principal_id': 'PR-other',
            'submission_history': [
                {
                    'submitted_at': 'submission-1',
                    'submitted_by_principal_id': 'PR-other',
                }
            ],
        }
        selected = config(root, 'http://unused', role='reviewer')
        reviewer = worker_module.Reviewer(
            selected,
            board,
            object(),
            worker_module.SessionLog(selected.log_file),
            directive='STATIC REVIEWER',
        )
        reviewer._active_review = ('board-one', 'TK-already-reviewing')

        result = asyncio.run(
            reviewer.run_review('board-one', board.tickets['TK-review'], work)
        )

        assert result == 'skipped'
        assert board.reviews == []
        transcript = selected.log_file.read_text()
        assert 'concurrent_review_refused' in transcript
        assert 'TK-already-reviewing' in transcript


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

    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
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


# ---------------------------------------------------------------------------
# Single-claim invariant tests
# ---------------------------------------------------------------------------

class SweepBoard(FakeBoard):
    """FakeBoard that simulates orphaned claims for startup sweep tests."""

    def __init__(self) -> None:
        super().__init__()
        self._agent_id = "AI-worker-one"
        self._board_ids = ["board-one"]
        self._listed_tickets: list[dict[str, Any]] = []

    async def agent_id(self, _board_id: str) -> str:
        return self._agent_id

    async def boards(self) -> list[str]:
        return self._board_ids

    async def ticket_list(self, board_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        tickets = self._listed_tickets
        if "status" in kwargs:
            tickets = [t for t in tickets if t.get("status") == kwargs["status"]]
        if "claimed_by_agent_id" in kwargs:
            tickets = [t for t in tickets if t.get("claimed_by_agent_id") == kwargs["claimed_by_agent_id"]]
        if "claimed_by" in kwargs:
            tickets = [t for t in tickets if t.get("claimed_by") == kwargs["claimed_by"]]
        return tickets


def test_startup_sweep_resume_path() -> None:
    """Exactly one orphaned claimed ticket → resume it via run_ticket."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = SweepBoard()
        board.work = work
        board._listed_tickets = [
            {
                "ticket_id": "TK-orphan",
                "status": "claimed",
                "claimed_by_agent_id": "AI-worker-one",
                "tags": [],
                "required_fields": ["test_output"],
            }
        ]
        with FakeLLMServer(
            [
                tool_call(
                    "one", "write_file", {"path": "resumed.txt", "content": "resumed"}
                ),
                tool_call(
                    "two",
                    "submit_work",
                    {
                        "summary": "resumed orphan",
                        "files_changed": ["resumed.txt"],
                        "notes": "test_output: ok",
                    },
                ),
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
            board.on_submit = worker.stop.set
            asyncio.run(worker.run())

        assert (work / "resumed.txt").read_text() == "resumed"
        assert board.claims == []  # sweep resumes directly without claim
        assert board.submissions[0]["ticket_id"] == "TK-orphan"
        transcript = selected.log_file.read_text()
        assert '"event":"startup_sweep_found_orphans"' in transcript
        assert '"event":"startup_sweep_resume"' in transcript
        assert '"ticket_id":"TK-orphan"' in transcript


def test_startup_sweep_release_path() -> None:
    """Multiple orphaned claims → release all with 'orphaned by restart'."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = SweepBoard()
        board.work = work
        board._listed_tickets = [
            {
                "ticket_id": "TK-orphan-1",
                "status": "claimed",
                "claimed_by_agent_id": "AI-worker-one",
                "tags": [],
                "required_fields": ["test_output"],
            },
            {
                "ticket_id": "TK-orphan-2",
                "status": "claimed",
                "claimed_by_agent_id": "AI-worker-one",
                "tags": [],
                "required_fields": ["test_output"],
            },
        ]
        # No LLM needed — sweep releases both, then run() enters wait loop
        board.events = []  # No events → wait blocks
        selected = config(root, "http://unused")
        worker = worker_module.Worker(
            selected,
            board,
            object(),  # LLM won't be called
            worker_module.SessionLog(selected.log_file),
            directive="STATIC",
        )

        async def exercise() -> None:
            running = asyncio.create_task(worker.run())
            # Let the sweep complete, then stop
            while len(board.releases) < 2:
                await asyncio.sleep(0)
            worker.stop.set()
            await asyncio.wait_for(running, timeout=2)

        asyncio.run(exercise())

        assert board.releases == ["orphaned by restart", "orphaned by restart"]
        assert board.claims == []
        transcript = selected.log_file.read_text()
        assert '"event":"startup_sweep_found_orphans"' in transcript
        assert '"count":2' in transcript
        assert '"event":"startup_sweep_resume"' not in transcript




def test_startup_sweep_orphan_worktree_combined(tmp_path: Path) -> None:
    """End-to-end test: killed-process leaves orphan claim + orphan worktree.
    _startup_sweep must release the claim and remove the worktree in one pass."""
    repo = init_git_repo(tmp_path)
    board = SweepBoard()
    board.work = repo
    board.identity = "AI-worker-one"
    board.live_claims = set()

    # Create an orphan worktree for a ticket (simulating a killed process)
    log = worker_module.SessionLog(tmp_path / "session.log")
    manager = worker_module.GitWorktreeManager("worker-one", log)
    orphan_session = asyncio.run(manager.prepare(repo, "TK-orphaned", "main"))
    orphan_workdir = orphan_session.work_dir
    assert orphan_workdir.exists()

    # Set up the orphan claim on the board (matches the worktree's ticket)
    board._listed_tickets = [
        {
            "ticket_id": "TK-orphaned",
            "status": "claimed",
            "claimed_by_agent_id": "AI-worker-one",
            "tags": [],
            "required_fields": ["test_output"],
        }
    ]

    # Create a Worker with the GitWorktreeManager but no usable LLM.
    # run_ticket will fail with AttributeError, triggering the exception path
    # which releases the claim and cleans up the worktree via finally.
    selected = config(tmp_path, "http://unused")
    worker = worker_module.Worker(
        selected,
        board,
        object(),  # No LLM -- run_ticket will fail
        worker_module.SessionLog(selected.log_file),
        directive="STATIC",
        worktrees=manager,
    )

    async def exercise() -> None:
        await worker._startup_sweep()

    asyncio.run(exercise())

    # Verify the claim was released
    # run_ticket catches the exception internally and releases the claim
    # ("LLM or runtime hard failure" from object() LLM, not "orphaned by restart")
    assert len(board.releases) == 1, "claim should have been released exactly once"
    # Verify the orphaned worktree was removed
    assert not orphan_workdir.exists(), (
        "orphaned worktree should be removed by startup sweep"
    )
    # Verify the log shows the full combined recovery
    transcript = selected.log_file.read_text()
    assert '"event":"startup_sweep_found_orphans"' in transcript
    assert '"event":"startup_sweep_resume"' in transcript
    # run_ticket catches the exception internally, so resume_failed not logged
    assert '"event":"hard_failure"' in transcript
    assert '"event":"worktree_removed"' in transcript



def test_startup_sweep_setup_failure_releases_orphan_claim(tmp_path: Path) -> None:
    """Regression test: work_dir() raises before prepare() → no UnboundLocalError,
    claim is released with 'orphaned by restart', cleanup is safe (session is None)."""
    # Board that raises on work_dir() — simulates a setup failure before prepare()
    class FailingBoard(SweepBoard):
        async def work_dir(self, _board_id: str) -> Path:
            raise RuntimeError("board unavailable")

    board = FailingBoard()
    board.work = tmp_path  # not used because work_dir() raises
    board._listed_tickets = [
        {
            "ticket_id": "TK-setup-fail",
            "status": "claimed",
            "claimed_by_agent_id": "AI-worker-one",
            "tags": [],
            "required_fields": ["test_output"],
        }
    ]

    selected = worker_module.config(
        tmp_path, "http://unused"
    ) if hasattr(worker_module, 'config') else config(tmp_path, "http://unused")

    # Use the module-level config function
    selected = config(tmp_path, "http://unused")
    log = worker_module.SessionLog(selected.log_file)
    manager = worker_module.GitWorktreeManager("worker-one", log)
    worker = worker_module.Worker(
        selected,
        board,
        object(),
        log,
        directive="STATIC",
        worktrees=manager,
    )

    async def exercise() -> None:
        # _startup_sweep must complete without UnboundLocalError
        await worker._startup_sweep()

    try:
        asyncio.run(exercise())
    except UnboundLocalError:
        pytest.fail("_startup_sweep raised UnboundLocalError — outcome not initialized")

    # Claim was released with orphaned by restart
    assert len(board.releases) == 1, "claim should have been released exactly once"
    assert board.releases[0] == "orphaned by restart"

    # Log shows the setup failure
    transcript = selected.log_file.read_text()
    assert '"event":"startup_sweep_found_orphans"' in transcript
    assert '"event":"startup_sweep_resume_failed"' in transcript
    assert '"error":"RuntimeError"' in transcript
    # The worktree was never created, so cleanup is a no-op (session is None)
    assert '"event":"startup_sweep_cleanup_failed"' not in transcript
    assert '"event":"worktree_removed"' not in transcript


def test_startup_sweep_prepare_failure_releases_orphan_claim(tmp_path: Path) -> None:
    """Regression test: GitWorktreeManager.prepare() raises → no UnboundLocalError,
    claim is released with 'orphaned by restart', cleanup is safe (session is None)."""
    repo = init_git_repo(tmp_path)

    class FailingPrepareManager(worker_module.GitWorktreeManager):
        async def prepare(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("worktree creation failed")

    board = SweepBoard()
    board.work = repo
    board._listed_tickets = [
        {
            "ticket_id": "TK-prepare-fail",
            "status": "claimed",
            "claimed_by_agent_id": "AI-worker-one",
            "tags": [],
            "required_fields": ["test_output"],
        }
    ]

    selected = config(tmp_path, "http://unused")
    log = worker_module.SessionLog(selected.log_file)
    manager = FailingPrepareManager("worker-one", log)
    worker = worker_module.Worker(
        selected,
        board,
        object(),
        log,
        directive="STATIC",
        worktrees=manager,
    )

    async def exercise() -> None:
        await worker._startup_sweep()

    try:
        asyncio.run(exercise())
    except UnboundLocalError:
        pytest.fail("_startup_sweep raised UnboundLocalError — outcome not initialized")

    # Claim was released with orphaned by restart
    assert len(board.releases) == 1, "claim should have been released exactly once"
    assert board.releases[0] == "orphaned by restart"

    # Log shows the prepare failure
    transcript = selected.log_file.read_text()
    assert '"event":"startup_sweep_found_orphans"' in transcript
    assert '"event":"startup_sweep_resume_failed"' in transcript
    assert '"error":"RuntimeError"' in transcript
    # session is None, so cleanup is skipped — no worktree_removed event
    assert '"event":"worktree_removed"' not in transcript



def test_claim_guard_refuses_while_holding() -> None:
    """When _active_claim is set and server confirms still claimed, refuse new claim."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        board.work = work
        board.events = [
            {"board_id": "board-one", "ticket_id": "TK-open"},
        ]
        board.tickets = {
            "TK-open": {
                "ticket_id": "TK-open",
                "status": "open",
                "tags": [],
                "required_fields": ["test_output"],
            },
        }
        board.tickets["TK-held"] = {
            "ticket_id": "TK-held",
            "status": "claimed",
            "tags": [],
            "required_fields": ["test_output"],
        }
        selected = config(root, "http://unused")
        worker = worker_module.Worker(
            selected,
            board,
            object(),
            worker_module.SessionLog(selected.log_file),
            directive="STATIC",
        )
        worker._active_claim = ("board-one", "TK-held")

        async def exercise() -> None:
            running = asyncio.create_task(worker.run())
            while not board.waited:
                await asyncio.sleep(0)
            await asyncio.sleep(0.1)
            worker.stop.set()
            await asyncio.wait_for(running, timeout=2)

        asyncio.run(exercise())

        transcript = selected.log_file.read_text()
        assert '"event":"claim_blocked_holding"' in transcript
        assert '"active_ticket":"TK-held"' in transcript
        assert board.claims == []

def test_release_read_back_mismatch() -> None:
    """Release read-back fails -> board added to _released_with_issues."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()

        class MismatchBoard(FakeBoard):
            """Returns non-open status after release; blocks second wait."""
            def __init__(self) -> None:
                super().__init__()
                self._wait_count = 0
                self._release_called = False

            async def ticket(self, board_id: str, ticket_id: str) -> dict[str, Any]:
                if ticket_id == "TK-mismatch":
                    status = "claimed" if self._release_called else "open"
                    return {"ticket_id": ticket_id, "status": status}
                return await super().ticket(board_id, ticket_id)

            async def release(self, board_id: str, ticket_id: str, reason: str) -> None:
                self._release_called = True
                await super().release(board_id, ticket_id, reason)

            async def wait(self, cursors: dict[str, int]) -> dict[str, Any]:
                self._wait_count += 1
                if self._wait_count == 1:
                    return await super().wait(cursors)
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    raise

        board = MismatchBoard()
        board.work = work
        board.events = [
            {"board_id": "board-one", "ticket_id": "TK-mismatch"},
        ]
        board.tickets = {
            "TK-mismatch": {
                "ticket_id": "TK-mismatch",
                "status": "open",
                "tags": [],
                "required_fields": ["test_output"],
            },
        }
        with FakeLLMServer(
            [
                tool_call("one", "give_up", {"reason": "test release read-back"}),
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

            async def exercise() -> None:
                running = asyncio.create_task(worker.run())
                while len(board.releases) < 1:
                    await asyncio.sleep(0)
                worker.stop.set()
                await asyncio.wait_for(running, timeout=2)

            asyncio.run(exercise())

        transcript = selected.log_file.read_text()
        assert '"event":"release_unverified"' in transcript
        assert '"actual_status":"claimed"' in transcript


def test_claim_guard_board_check_refuses_existing_claim() -> None:
    """Board-level check refuses new claim when server shows an existing claim by this seat."""
    import asyncio
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        board.work = work
        board.events = [
            {"board_id": "board-one", "ticket_id": "TK-open"},
        ]
        board.tickets = {
            "TK-open": {
                "ticket_id": "TK-open",
                "status": "open",
                "tags": [],
                "required_fields": ["test_output"],
            },
        }
        # Simulate an existing claim on the board by this seat
        board.tickets["TK-orphan"] = {
            "ticket_id": "TK-orphan",
            "status": "claimed",
            "claimed_by_agent_id": "AI-worker-one",
            "tags": [],
            "required_fields": ["test_output"],
        }
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
            await asyncio.sleep(0.1)
            worker.stop.set()
            await asyncio.wait_for(running, timeout=2)

        asyncio.run(exercise())

        transcript = selected.log_file.read_text()
        assert "\"event\":\"claim_blocked_board_check\"" in transcript
        # Should not have claimed the new ticket
        assert len(board.claims) == 0 or board.claims[0][1] == "TK-orphan"


def test_sigterm_path_unchanged() -> None:
    """SIGTERM (stop.set) still gracefully releases the claim."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FakeBoard()
        board.work = work
        board.events = [{"board_id": "board-one", "ticket_id": "TK-sigterm"}]
        board.tickets = {
            "TK-sigterm": {
                "ticket_id": "TK-sigterm",
                "status": "open",
                "tags": [],
                "required_fields": ["test_output"],
            },
        }
        with FakeLLMServer(
            [
                tool_call(
                    "one", "write_file", {"path": "sig.txt", "content": "ok"}
                ),
                tool_call(
                    "two",
                    "submit_work",
                    {
                        "summary": "sigterm done",
                        "files_changed": ["sig.txt"],
                        "notes": "test_output: ok",
                    },
                ),
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

            async def exercise() -> None:
                running = asyncio.create_task(worker.run())
                while not board.claims:
                    await asyncio.sleep(0)
                # Signal stop — next iteration of run_ticket will catch it
                worker.stop.set()
                await asyncio.wait_for(running, timeout=2)

            asyncio.run(exercise())

        assert board.claims == [("board-one", "TK-sigterm")]
        assert any("graceful shutdown" in r for r in board.releases)
        transcript = selected.log_file.read_text()
        assert '"reason":"graceful shutdown"' in transcript


# ── git readonly allowlist tests ─────────────────────────────────────────────


def test_git_readonly_allowlist_allowed_matrix() -> None:
    """Every read-only git command in the verification kit must pass."""
    allowed = [
        # Core verification commands
        "git status",
        "git diff",
        "git diff --name-status",
        "git diff --cached",
        "git show",
        "git show --name-status",
        "git show HEAD",
        "git log",
        "git log --oneline -5",
        "git log --name-status",
        "git rev-parse HEAD",
        "git rev-parse --abbrev-ref HEAD",
        "git cat-file -t HEAD",
        "git cat-file -p HEAD",
        "git merge-base HEAD main",
        "git merge-base --is-ancestor HEAD main",
        "git ls-files",
        "git blame main.py",
        "git grep foo",
        # branch -- read-only operations
        "git branch",
        "git branch --contains HEAD",
        "git branch --contains v1.0",
        "git branch -a",
        "git branch -r",
        "git branch -v",
        "git branch -vv",
        "git branch --list",
        "git branch --merged",
        "git branch --no-merged",
        "git branch --sort=-committerdate",
        # worktree -- only list
        "git worktree list",
    ]
    for cmd in allowed:
        try:
            argv, needs_copy = worker_module._readonly_command(cmd)
        except PermissionError as exc:
            pytest.fail(
                f"allowed command {cmd!r} was blocked: {exc}"
            )


def test_git_readonly_allowlist_mutating_blocked() -> None:
    """Every mutating git command must be blocked."""
    blocked = [
        # Core mutating commands (not in subcommand set at all)
        "git commit -m test",
        "git commit --allow-empty -m test",
        "git checkout -b new",
        "git checkout main",
        "git reset --hard",
        "git reset HEAD",
        "git clean -fd",
        "git clean -n",
        "git fetch origin",
        "git fetch --all",
        "git push origin main",
        "git push --force",
        "git merge feature",
        "git rebase main",
        "git rebase --onto main feature",
        "git stash",
        "git stash pop",
        "git tag v1.0",
        "git tag -d v1.0",
        "git config user.name test",
        "git add .",
        "git rm file",
        "git mv old new",
        "git clone https://example.com/repo",
        "git init",
        "git remote add origin https://example.com/repo",
        "git submodule add https://example.com/repo",
        # branch mutating commands
        "git branch -d feature",
        "git branch -D feature",
        "git branch -m old new",
        "git branch -M old new",
        "git branch -c old new",
        "git branch -C old new",
        "git branch --delete feature",
        "git branch --move old new",
        "git branch --copy old new",
        "git branch --edit-description",
        "git branch -dD",  # combined short flags
        "git branch -mM",  # combined short flags
        # branch positional/hole vectors (from first rejection)
        "git branch newname",
        "git branch newname HEAD",
        "git branch -f main HEAD~1",
        "git branch --force main HEAD~1",
        "git branch -t t main",
        "git branch --track t main",
        "git branch --set-upstream-to=origin/main",
        "git branch -u origin/main",
        "git branch --unset-upstream",
        "git branch -vf",  # combined short flags with force
        "git branch -vt",  # combined short flags with track
        "git branch -vu",  # combined short flags with upstream
        # worktree mutating commands
        "git worktree add ../new",
        "git worktree add ../new main",
        "git worktree remove ../new",
        "git worktree prune",
        "git worktree lock ../new",
        "git worktree unlock ../new",
        "git worktree move ../old ../new",
    ]
    for cmd in blocked:
        try:
            worker_module._readonly_command(cmd)
            pytest.fail(
                f"mutating command {cmd!r} was unexpectedly allowed"
            )
        except PermissionError:
            pass


def test_git_readonly_allowlist_flag_injection_blocked() -> None:
    """Write-capable flags on allowed subcommands must be blocked."""
    injections = [
        # --output flag on fully-read-only subcommands
        "git diff --output /tmp/out",
        "git log --output /tmp/out",
        "git show --output /tmp/out",
        # -o short flag on fully-read-only subcommands
        "git diff -o /tmp/out",
        "git log -o /tmp/out",
        "git show -o /tmp/out",
        # --exec
        "git log --exec=/bin/sh",
        "git diff --exec=/bin/sh",
        # --upload-pack
        "git log --upload-pack=/bin/sh",
        # --receive-pack
        "git log --receive-pack=/bin/sh",
        # --ext-diff
        "git diff --ext-diff",
        # --textconv
        "git show --textconv",
        # --no-index (allows diffing outside repo)
        "git diff --no-index /etc/hosts /etc/passwd",
        # --filters
        "git log --filters",
        # --open-files-in-pager
        "git diff --open-files-in-pager",
        # --output flag on branch subcommand
        "git branch --list --output /tmp/x",
        "git branch -o /tmp/x",
        "git branch --contains HEAD --output /tmp/x",
        # --output flag on worktree list subcommand
        "git worktree list -o /tmp/x",
        "git worktree list --output /tmp/x",
    ]
    for cmd in injections:
        try:
            worker_module._readonly_command(cmd)
            pytest.fail(
                f"flag injection {cmd!r} was unexpectedly allowed"
            )
        except PermissionError:
            pass


def test_git_name_status_works_as_flag() -> None:
    """--name-status must work on diff, show, and log (it's not a subcommand)."""
    for cmd in [
        "git diff --name-status",
        "git show --name-status",
        "git log --name-status",
    ]:
        try:
            argv, needs_copy = worker_module._readonly_command(cmd)
        except PermissionError as exc:
            pytest.fail(
                f"name-status flag {cmd!r} was blocked: {exc}"
            )


def test_git_worktree_list_is_only_allowed_worktree_subcommand() -> None:
    """Only 'git worktree list' is allowed; any other worktree subcommand is blocked."""
    mutating = [
        "git worktree add ../new",
        "git worktree add ../new main",
        "git worktree remove ../new",
        "git worktree prune",
        "git worktree lock ../new",
        "git worktree unlock ../new",
        "git worktree move ../old ../new",
        "git worktree",  # missing sub-subcommand
    ]
    for cmd in mutating:
        try:
            worker_module._readonly_command(cmd)
            pytest.fail(
                f"worktree {cmd!r} was unexpectedly allowed"
            )
        except PermissionError:
            pass
    # The one allowed form
    try:
        argv, needs_copy = worker_module._readonly_command("git worktree list")
    except PermissionError as exc:
        pytest.fail(f"git worktree list was blocked: {exc}")


def test_git_branch_mutation_flags_all_blocked() -> None:
    """Every mutating branch flag (short, long, combined) must be blocked."""
    for cmd in [
        "git branch -d feature",
        "git branch -D feature",
        "git branch -m old new",
        "git branch -M old new",
        "git branch -c old new",
        "git branch -C old new",
        "git branch --delete feature",
        "git branch --move old new",
        "git branch --copy old new",
        "git branch --edit-description",
        # Additional hole vectors from first rejection
        "git branch newname",
        "git branch newname HEAD",
        "git branch -f main HEAD~1",
        "git branch --force main HEAD~1",
        "git branch -t t main",
        "git branch --track t main",
        "git branch --set-upstream-to=origin/main",
        "git branch -u origin/main",
        "git branch --unset-upstream",
    ]:
        try:
            worker_module._readonly_command(cmd)
            pytest.fail(f"branch mutation {cmd!r} was unexpectedly allowed")
        except PermissionError:
            pass


def test_git_non_git_commands_blocked() -> None:
    """Non-git commands must be blocked by the reviewer allowlist."""
    for cmd in [
        "touch forbidden",
        "rm file",
        "mv a b",
        "cp a b",
        "echo hello > file",
        "cat > file",
        "vim file",
        "nano file",
        "mkdir dir",
        "chmod +x file",
        "python3 -c 'print(1)'",
        "ls",
        "whoami",
        "curl http://example.com",
        "wget http://example.com",
    ]:
        try:
            worker_module._readonly_command(cmd)
            pytest.fail(
                f"non-git command {cmd!r} was unexpectedly allowed"
            )
        except PermissionError:
            pass


# ---------------------------------------------------------------------------
# Worktree isolation tests
# ---------------------------------------------------------------------------

def test_git_worktree_creation_jails_ticket_and_prompts_for_commit(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path)
    selected = config(tmp_path, "http://unused")
    log = worker_module.SessionLog(selected.log_file)
    manager = worker_module.GitWorktreeManager("Worker One", log)

    session = asyncio.run(manager.prepare(repo, "TK-alpha123", "main"))

    assert session.isolated is True
    assert session.branch == "api/worker-one-alpha123"
    assert session.work_dir == (
        repo / ".git" / "pursers-worktrees" / "worker-one-tk-alpha123"
    )
    assert worker_module._jailed(session.work_dir, "result.txt") == (
        session.work_dir / "result.txt"
    )
    with pytest.raises(PermissionError, match="escapes assigned work directory"):
        worker_module._jailed(session.work_dir, "../escape.txt")
    worker = worker_module.Worker(
        selected, FakeBoard(), object(), log, directive="STATIC", worktrees=manager
    )
    context = json.loads(
        worker.messages(
            "board-one", {"ticket_id": "TK-alpha123"}, session.work_dir, session.branch
        )[1]["content"].removeprefix("BOARD CONTEXT\n")
    )
    assert context["checkout"] == "dedicated per-ticket git worktree"
    assert context["ticket_branch"] == session.branch
    assert "commit" in context["commit_requirement"]
    created = json.loads(log.path.read_text().splitlines()[-1])
    assert created["event"] == "worktree_created"
    assert created["work_dir"] == str(session.work_dir)
    assert asyncio.run(manager.cleanup(session, submitted=False)) is True


def test_git_worktree_cleanup_release_and_submitted_dirty_checkout(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path)
    log = worker_module.SessionLog(tmp_path / "session.log")
    manager = worker_module.GitWorktreeManager("worker-cleanup", log)
    clean = asyncio.run(manager.prepare(repo, "TK-clean", "main"))

    assert asyncio.run(manager.cleanup(clean, submitted=False)) is True
    assert not clean.work_dir.exists()

    dirty = asyncio.run(manager.prepare(repo, "TK-dirty", "main"))
    (dirty.work_dir / "uncommitted.txt").write_text("ticket output")
    assert asyncio.run(manager.cleanup(dirty, submitted=False)) is False
    assert dirty.work_dir.exists()
    assert asyncio.run(manager.cleanup(dirty, submitted=True)) is True
    assert not dirty.work_dir.exists()
    assert "worktree_retained_dirty" in log.path.read_text()


def test_git_worktree_startup_sweeps_only_clean_inactive_orphans(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path)
    manager = worker_module.GitWorktreeManager(
        "worker-sweep", worker_module.SessionLog(tmp_path / "session.log")
    )
    active = asyncio.run(manager.prepare(repo, "TK-active", "main"))

    asyncio.run(
        manager.sweep([(repo, "main")], {("board-one", "TK-active")})
    )
    assert active.work_dir.exists()
    asyncio.run(manager.sweep([(repo, "main")], set()))
    assert not active.work_dir.exists()

    dirty = asyncio.run(manager.prepare(repo, "TK-dirty-orphan", "main"))
    (dirty.work_dir / "dirty.txt").write_text("preserve me")
    asyncio.run(manager.sweep([(repo, "main")], set()))
    assert dirty.work_dir.exists()
    assert asyncio.run(manager.cleanup(dirty, submitted=True)) is True


def test_git_worktree_non_git_passthrough() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        tmp_path = Path(raw)
        work = tmp_path / "plain"
        work.mkdir()
        manager = worker_module.GitWorktreeManager(
            "worker-plain", worker_module.SessionLog(tmp_path / "session.log")
        )

        session = asyncio.run(manager.prepare(work, "TK-plain", "main"))

        assert session == worker_module.WorktreeSession(
            work.resolve(), work.resolve(), None, isolated=False, readonly=False
        )
        assert asyncio.run(manager.cleanup(session, submitted=False)) is False


def test_two_workers_receive_distinct_ticket_worktrees(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path)
    first = worker_module.GitWorktreeManager(
        "worker-one", worker_module.SessionLog(tmp_path / "one.log")
    )
    second = worker_module.GitWorktreeManager(
        "worker-two", worker_module.SessionLog(tmp_path / "two.log")
    )

    async def simulate_concurrent_claims() -> tuple[Any, Any]:
        return await asyncio.gather(
            first.prepare(repo, "TK-first111", "main"),
            second.prepare(repo, "TK-second222", "main"),
        )

    first_session, second_session = asyncio.run(simulate_concurrent_claims())
    transcript = {
        "worker-one": str(first_session.work_dir),
        "worker-two": str(second_session.work_dir),
    }
    assert first_session.work_dir != second_session.work_dir
    assert first_session.branch == "api/worker-one-first111"
    assert second_session.branch == "api/worker-two-second222"
    assert all(Path(path).is_dir() for path in transcript.values())
    assert asyncio.run(first.cleanup(first_session, submitted=False)) is True
    assert asyncio.run(second.cleanup(second_session, submitted=False)) is True


def test_reviewer_worktree_is_detached_and_readonly_context(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path)
    selected = config(tmp_path, "http://unused", role="reviewer")
    manager = worker_module.GitWorktreeManager(
        "reviewer-one", worker_module.SessionLog(tmp_path / "reviewer.log")
    )

    session = asyncio.run(
        manager.prepare(repo, "TK-review123", "main", readonly=True)
    )
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=session.work_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    reviewer = worker_module.Reviewer(
        selected,
        FakeBoard(),
        object(),
        worker_module.SessionLog(selected.log_file),
        directive="STATIC REVIEWER",
        worktrees=manager,
    )
    context = json.loads(
        reviewer.messages("board-one", {"ticket_id": "TK-review123"}, session.work_dir)[
            1
        ]["content"].removeprefix("BOARD CONTEXT\n")
    )

    assert branch == ""
    assert session.readonly is True
    assert "read-only" in context["access"]
    assert asyncio.run(manager.cleanup(session, submitted=False)) is True


def test_worker_commits_in_ticket_branch_then_submit_cleans_worktree(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path)
    subprocess.run(
        ["git", "config", "--local", "--unset-all", "user.name"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "--local", "--unset-all", "user.email"],
        cwd=repo,
        check=True,
    )
    board = FakeBoard()
    board.work = repo
    with FakeLLMServer(
        [
            tool_call(
                "commit",
                "run_shell",
                {
                    "command": (
                        "printf 'done\n' > result.txt && "
                        "git add result.txt && git commit -m ticket"
                    )
                },
            ),
            tool_call(
                "submit",
                "submit_work",
                {
                    "summary": "committed ticket output",
                    "files_changed": ["result.txt"],
                    "notes": "test_output: simulated pass",
                },
            ),
        ]
    ) as server:
        selected = config(tmp_path, server.url)
        worker = worker_module.Worker(
            selected,
            board,
            worker_module.OpenAICompatible(selected, "key"),
            worker_module.SessionLog(selected.log_file),
            directive="STATIC",
        )
        board.on_submit = worker.stop.set
        asyncio.run(worker.run())

    branch = "api/worker-one-scratch"
    worktree = repo / ".git" / "pursers-worktrees" / "worker-one-tk-scratch"
    committed = subprocess.run(
        ["git", "show", f"{branch}:result.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    board_context = json.loads(
        server.requests[0]["messages"][1]["content"].removeprefix(
            "BOARD CONTEXT\n")
    )
    assert committed == "done\n"
    assert not worktree.exists()
    assert board_context["work_dir"] == str(worktree)
    assert board_context["ticket_branch"] == branch


def test_board_api_exposes_registry_refs_and_only_own_active_claims(
    tmp_path: Path,
) -> None:
    class View:
        identity = SimpleNamespace(agent_id="AI-worker-one", principal_id="PR-one")

        async def ticket_list(self, **arguments: Any) -> dict[str, Any]:
            assert arguments == {
                "status": "claimed",
                "include_closed": False,
                "limit": 500,
            }
            return {
                "tickets": [
                    {
                        "ticket_id": "TK-own",
                        "status": "claimed",
                        "claimed_by_agent_id": "AI-worker-one",
                    },
                    {
                        "ticket_id": "TK-other",
                        "status": "claimed",
                        "claimed_by_agent_id": "AI-other",
                    },
                ]
            }

    first = tmp_path / "first"
    second = tmp_path / "second"
    selected = replace(config(tmp_path, "http://unused"), boards=("alpha",))
    api = worker_module.PursersBoardAPI(selected, "TOKEN_PLACEHOLDER")
    api.registry = {
        "schema_version": 1,
        "projects": {
            "first": {
                "board_id": "alpha",
                "work_dir": str(first),
                "status": "active",
                "integration_ref": "develop",
            },
            "second": {
                "board_id": "beta",
                "work_dir": str(second),
                "status": "paused",
            },
        },
    }
    api.views = {"alpha": View()}

    assert asyncio.run(api.integration_ref("alpha")) == "develop"
    assert asyncio.run(api.work_specs()) == [(first.resolve(), "develop")]
    assert asyncio.run(api.active_claims()) == {("alpha", "TK-own")}


# ---------------------------------------------------------------------------
# Orphan worktree sweep integration tests (combines worktree + single-claim)
# ---------------------------------------------------------------------------

def test_orphan_worktree_sweep_cleans_killed_process_worktree(
    tmp_path: Path,
) -> None:
    """Simulate a killed process leaving an orphaned worktree + orphaned claim.
    The startup sweep must recover both: the claim is released, and the
    orphaned worktree is removed."""
    repo = init_git_repo(tmp_path)
    log = worker_module.SessionLog(tmp_path / "session.log")
    manager = worker_module.GitWorktreeManager("worker-killed", log)

    # Create a worktree for a ticket that was claimed but is now orphaned
    # (as if the process was killed mid-flight)
    orphan_session = asyncio.run(
        manager.prepare(repo, "TK-orphaned", "main")
    )
    orphan_workdir = orphan_session.work_dir
    assert orphan_workdir.exists()

    # Now simulate the sweep with NO active claims — the orphaned worktree
    # should be removed
    asyncio.run(manager.sweep([(repo, "main")], set()))
    assert not orphan_workdir.exists(), (
        "orphaned worktree should have been removed by sweep"
    )
    log_text = log.path.read_text()
    assert "orphan_worktree_removed" in log_text


def test_orphan_worktree_sweep_preserves_active_claim_worktree(
    tmp_path: Path,
) -> None:
    """Ensure that a worktree for an active claim is NOT swept."""
    repo = init_git_repo(tmp_path)
    log = worker_module.SessionLog(tmp_path / "session.log")
    manager = worker_module.GitWorktreeManager("worker-active", log)

    active_session = asyncio.run(
        manager.prepare(repo, "TK-active-claim", "main")
    )
    assert active_session.work_dir.exists()

    # Sweep with the active claim present — worktree should survive
    asyncio.run(
        manager.sweep(
            [(repo, "main")],
            {("board-one", "TK-active-claim")},
        )
    )
    assert active_session.work_dir.exists(), (
        "active claim worktree should survive sweep"
    )
    assert asyncio.run(manager.cleanup(active_session, submitted=False)) is True


class FailingSweepBoard(SweepBoard):
    """SweepBoard that raises on work_dir() to simulate a board failure during startup sweep."""

    def __init__(self, fail_on: str = "work_dir") -> None:
        super().__init__()
        self.fail_on = fail_on

    async def work_dir(self, _board_id: str) -> Path:
        if self.fail_on == "work_dir":
            raise RuntimeError("board work_dir unavailable")
        return await super().work_dir(_board_id)

    async def integration_ref(self, _board_id: str) -> str:
        if self.fail_on == "integration_ref":
            raise RuntimeError("integration_ref unavailable")
        return await super().integration_ref(_board_id)


class FailingGitWorktreeManager(worker_module.GitWorktreeManager):
    """GitWorktreeManager that raises on prepare() to simulate a git or filesystem failure."""

    async def prepare(
        self,
        source_dir: Path,
        ticket_id: str,
        integration_ref: str = "main",
        *,
        readonly: bool = False,
    ) -> worker_module.WorktreeSession:
        raise RuntimeError("worktree prepare failed")


def test_startup_sweep_work_dir_fails_releases_orphan_claim() -> None:
    """When work_dir() raises during _startup_sweep, the claim is released
    and no UnboundLocalError escapes because outcome is initialized before try."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FailingSweepBoard(fail_on="work_dir")
        board.work = work
        board._listed_tickets = [
            {
                "ticket_id": "TK-orphan",
                "status": "claimed",
                "claimed_by_agent_id": "AI-worker-one",
                "tags": [],
                "required_fields": ["test_output"],
            }
        ]
        selected = config(root, "http://unused")
        log = worker_module.SessionLog(selected.log_file)
        worker = worker_module.Worker(
            selected,
            board,
            object(),  # LLM won't be called
            log,
            directive="STATIC",
        )

        async def exercise() -> None:
            await worker._startup_sweep()

        # This must not raise UnboundLocalError
        asyncio.run(exercise())

        # Verify the claim was released with 'orphaned by restart'
        assert board.releases == ["orphaned by restart"], (
            f"expected one release, got {board.releases}"
        )
        # Verify no worktree was created (session stayed None)
        transcript = selected.log_file.read_text()
        assert '"event":"startup_sweep_resume_failed"' in transcript
        assert '"event":"startup_sweep_resume"' not in transcript
        assert '"error":"RuntimeError"' in transcript
        assert '"event":"worktree_created"' not in transcript


def test_startup_sweep_integration_ref_fails_releases_orphan_claim() -> None:
    """When integration_ref() raises during _startup_sweep, the claim is released
    and no UnboundLocalError escapes."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = FailingSweepBoard(fail_on="integration_ref")
        board.work = work
        board._listed_tickets = [
            {
                "ticket_id": "TK-orphan",
                "status": "claimed",
                "claimed_by_agent_id": "AI-worker-one",
                "tags": [],
                "required_fields": ["test_output"],
            }
        ]
        selected = config(root, "http://unused")
        log = worker_module.SessionLog(selected.log_file)
        worker = worker_module.Worker(
            selected,
            board,
            object(),  # LLM won't be called
            log,
            directive="STATIC",
        )

        async def exercise() -> None:
            await worker._startup_sweep()

        # This must not raise UnboundLocalError
        asyncio.run(exercise())

        assert board.releases == ["orphaned by restart"], (
            f"expected one release, got {board.releases}"
        )
        transcript = selected.log_file.read_text()
        assert '"event":"startup_sweep_resume_failed"' in transcript
        assert '"error":"RuntimeError"' in transcript


def test_startup_sweep_prepare_fails_releases_orphan_claim() -> None:
    """When GitWorktreeManager.prepare() raises during _startup_sweep,
    the claim is released and no UnboundLocalError escapes."""
    with tempfile.TemporaryDirectory(dir="/tmp") as raw:
        root = Path(raw)
        work = root / "work"
        work.mkdir()
        board = SweepBoard()
        board.work = work
        board._listed_tickets = [
            {
                "ticket_id": "TK-orphan",
                "status": "claimed",
                "claimed_by_agent_id": "AI-worker-one",
                "tags": [],
                "required_fields": ["test_output"],
            }
        ]
        selected = config(root, "http://unused")
        log = worker_module.SessionLog(selected.log_file)
        failing_manager = FailingGitWorktreeManager("worker-one", log)
        worker = worker_module.Worker(
            selected,
            board,
            object(),  # LLM won't be called
            log,
            directive="STATIC",
            worktrees=failing_manager,
        )

        async def exercise() -> None:
            await worker._startup_sweep()

        # This must not raise UnboundLocalError
        asyncio.run(exercise())

        assert board.releases == ["orphaned by restart"], (
            f"expected one release, got {board.releases}"
        )
        transcript = selected.log_file.read_text()
        assert '"event":"startup_sweep_resume_failed"' in transcript
        assert '"error":"RuntimeError"' in transcript
        assert '"event":"worktree_created"' not in transcript
