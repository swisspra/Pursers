from __future__ import annotations

import asyncio
import json
import os
import sys
from argparse import Namespace
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import Client
from pursers_client import BoardClient
import pursers_acp.agent as agent_module
from pursers_acp.agent import ACP_VERSION, AuthRequired, PursersACPAgent
from pursers_acp.agent import PersonalBoardSurface, StdioWaitBridge
from pursers_central import central

JSON = dict[str, Any]


class FakeBoard:
    board_id = "pursers"

    def __init__(self) -> None:
        self.closed = False
        self.mutations: list[JSON] = []
        self.watch_started = asyncio.Event()

    async def close(self) -> None:
        self.closed = True

    async def my_tickets(self) -> list[JSON]:
        return [{"ticket_id": "TK-owned", "status": "open", "title": "Owned"}]

    async def my_offers(self) -> list[JSON]:
        return [{"ticket_id": "TK-offer", "agent_name": "personal", "kind": "work"}]

    async def board_status(self) -> JSON:
        return {"board_id": self.board_id, "status_counts": {"open": 1}, "latest_seq": 4}

    async def ticket_evidence(self, ticket_id: str) -> JSON:
        return {
            "ticket": {
                "ticket_id": ticket_id,
                "status": "submitted",
                "summary": "Ready for review",
                "files_changed": ["src/example.py"],
                "review_verdict": None,
                "notes": "branch_and_commit: branch@abc\ntest_output: 2 passed",
            }
        }

    def create_action(self, title: str, description: str) -> JSON:
        return {
            "operation": "ticket_create",
            "board_id": self.board_id,
            "params": {"title": title, "description": description},
        }

    def annotate_action(self, ticket_id: str, text: str) -> JSON:
        return {
            "operation": "ticket_annotate",
            "board_id": self.board_id,
            "params": {"ticket_id": ticket_id, "text": text},
        }

    async def answer_action(self, ticket_id: str, text: str) -> JSON:
        return {
            "operation": "ticket_human_resolve",
            "board_id": self.board_id,
            "params": {"ticket_id": ticket_id, "content": text},
        }

    async def mutate(self, action: JSON) -> JSON:
        self.mutations.append(action)
        if action["operation"] == "ticket_create":
            return {"ticket": {"ticket_id": "TK-created"}}
        if action["operation"] == "ticket_human_resolve":
            return {"ticket": {"ticket_id": action["params"]["ticket_id"]}}
        return {"annotation": {"annotation_id": "AN-created"}}

    async def watch(
        self, cursor: int | None, cancel: asyncio.Event
    ) -> AsyncIterator[tuple[int, JSON]]:
        self.watch_started.set()
        yield 5, {"seq": 5, "kind": "ticket_created", "ticket_id": "TK-live"}
        await cancel.wait()


class BlockingBoard(FakeBoard):
    def __init__(self) -> None:
        super().__init__()
        self.read_started = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.mutate_started = asyncio.Event()
        self.mutate_cancelled = asyncio.Event()

    async def my_tickets(self) -> list[JSON]:
        self.read_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.read_cancelled.set()
            raise
        raise AssertionError("unreachable")

    async def mutate(self, action: JSON) -> JSON:
        self.mutate_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.mutate_cancelled.set()
            raise
        raise AssertionError("unreachable")


class FakeACPClient:
    """Small fake IDE client derived from the P0 ACP client harness."""

    def __init__(self, agent: PursersACPAgent, *, allow: bool = True) -> None:
        self.agent = agent
        self.allow = allow
        self.incoming: asyncio.Queue[bytes] = asyncio.Queue()
        self.outgoing: asyncio.Queue[JSON] = asyncio.Queue()
        self.next_id = 0
        self.updates: list[JSON] = []
        self.permissions: list[JSON] = []
        self.task = asyncio.create_task(agent.run(self.incoming.get, self.outgoing.put))

    async def close(self) -> None:
        await self.incoming.put(b"")
        await self.task

    async def notify(self, method: str, params: JSON) -> None:
        await self.incoming.put(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params}).encode()
        )

    async def request(self, method: str, params: JSON) -> Any:
        self.next_id += 1
        request_id = self.next_id
        await self.incoming.put(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            ).encode()
        )
        while True:
            message = await asyncio.wait_for(self.outgoing.get(), 15)
            if message.get("method") == "session/update":
                self.updates.append(message["params"])
                continue
            if message.get("method") == "session/request_permission":
                self.permissions.append(message["params"])
                outcome = {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": "allow-once" if self.allow else "reject-once",
                    }
                }
                await self.incoming.put(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": message["id"], "result": outcome}
                    ).encode()
                )
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]

    async def initialize(self) -> JSON:
        return await self.request(
            "initialize",
            {
                "protocolVersion": ACP_VERSION,
                "clientCapabilities": {"auth": {"terminal": True}},
                "clientInfo": {"name": "fake-ide", "version": "1"},
            },
        )

    async def new_session(self, cwd: Path, mcp_servers: list[JSON] | None = None) -> str:
        result = await self.request(
            "session/new",
            {
                "cwd": str(cwd.resolve()),
                "mcpServers": [] if mcp_servers is None else mcp_servers,
            },
        )
        return result["sessionId"]

    async def prompt(self, session_id: str, text: str) -> JSON:
        return await self.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
        )


def test_conformance_honest_capabilities_and_read_intents(tmp_path: Path) -> None:
    asyncio.run(_conformance_honest_capabilities_and_read_intents(tmp_path))


async def _conformance_honest_capabilities_and_read_intents(tmp_path: Path) -> None:
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        initialized = await client.initialize()
        assert initialized["protocolVersion"] == 1
        assert initialized["agentCapabilities"] == {"promptCapabilities": {}}
        assert "loadSession" not in initialized["agentCapabilities"]
        assert initialized["authMethods"][0]["id"] == "pursers-personal-profile"
        assert initialized["authMethods"][0]["type"] == "agent"
        session = await client.new_session(tmp_path)
        commands_update = client.updates[-1]["update"]
        assert commands_update["sessionUpdate"] == "available_commands_update"
        assert [row["name"] for row in commands_update["availableCommands"]] == [
            "board",
            "create",
            "watch",
            "evidence",
            "answer",
        ]
        assert (await client.prompt(session, "my tickets"))["stopReason"] == "end_turn"
        text = client.updates[-1]["update"]["content"]["text"]
        assert "TK-owned" in text
        assert str(tmp_path) not in text
    finally:
        await client.close()
    assert board.closed


def test_five_slash_commands_are_executable_and_writes_request_permission(
    tmp_path: Path,
) -> None:
    asyncio.run(_five_slash_commands_are_executable_and_writes_request_permission(tmp_path))


async def _five_slash_commands_are_executable_and_writes_request_permission(
    tmp_path: Path,
) -> None:
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        await client.prompt(session, "/board")
        await client.prompt(session, "/evidence TK-owned")
        await client.prompt(session, "/create Title :: Description")
        await client.prompt(session, "/answer TK-owned approved")

        messages = [
            row["update"].get("content", {}).get("text", "")
            for row in client.updates
            if row["update"].get("sessionUpdate") == "agent_message_chunk"
        ]
        assert any(text.startswith("Board pursers") for text in messages)
        assert any("branch_and_commit: branch@abc" in text for text in messages)
        assert [row["toolCall"]["rawInput"]["operation"] for row in client.permissions] == [
            "ticket_create",
            "ticket_human_resolve",
        ]
    finally:
        await client.close()


def test_board_failure_is_rendered_as_agent_message(tmp_path: Path) -> None:
    asyncio.run(_board_failure_is_rendered_as_agent_message(tmp_path))


async def _board_failure_is_rendered_as_agent_message(tmp_path: Path) -> None:
    class BrokenBoard(FakeBoard):
        async def board_status(self) -> JSON:
            raise RuntimeError("profile points at an unavailable Central")

    client = FakeACPClient(PursersACPAgent(lambda: BrokenBoard()))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        assert await client.prompt(session, "/board") == {"stopReason": "end_turn"}
        text = client.updates[-1]["update"]["content"]["text"]
        assert "Check the selected Personal profile and board access" in text
        assert "unavailable Central" in text
    finally:
        await client.close()


def test_every_write_requires_exact_allow_once_payload(tmp_path: Path) -> None:
    asyncio.run(_every_write_requires_exact_allow_once_payload(tmp_path))


async def _every_write_requires_exact_allow_once_payload(tmp_path: Path) -> None:
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        result = await client.prompt(session, "create ticket Title :: Description")
        assert result == {"stopReason": "end_turn"}
        approved = client.permissions[0]["toolCall"]["rawInput"]
        assert approved == board.mutations[0]
        assert [row["kind"] for row in client.permissions[0]["options"]] == [
            "allow_once",
            "reject_once",
        ]
    finally:
        await client.close()


def test_rejected_write_makes_no_board_call(tmp_path: Path) -> None:
    asyncio.run(_rejected_write_makes_no_board_call(tmp_path))


async def _rejected_write_makes_no_board_call(tmp_path: Path) -> None:
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board), allow=False)
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        assert (await client.prompt(session, "annotate TK-one note"))["stopReason"] == "end_turn"
        assert board.mutations == []
    finally:
        await client.close()


def test_watch_streams_then_cancel_stops_prompt(tmp_path: Path) -> None:
    asyncio.run(_watch_streams_then_cancel_stops_prompt(tmp_path))


async def _watch_streams_then_cancel_stops_prompt(tmp_path: Path) -> None:
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        prompt = asyncio.create_task(client.prompt(session, "watch pursers"))
        await asyncio.wait_for(board.watch_started.wait(), 1)
        while not any(
            row["update"].get("content", {}).get("text", "").startswith("Board event")
            for row in client.updates
        ):
            await asyncio.sleep(0)
        await client.notify("session/cancel", {"sessionId": session})
        assert await prompt == {"stopReason": "cancelled"}
        plans = [
            row["update"]
            for row in client.updates
            if row["update"].get("sessionUpdate") == "plan"
        ]
        assert [row["entries"][0]["status"] for row in plans] == [
            "in_progress",
            "completed",
        ]
    finally:
        await client.close()


def _stdio_server_script(path: Path, source: str) -> None:
    path.write_text(
        "from mcp.server.mcpserver import MCPServer\n"
        + source
        + "\nserver.run(transport='stdio')\n",
        encoding="utf-8",
    )


def test_personal_watch_uses_wait_bridge_cursor_and_cancel_teardown(
    tmp_path: Path,
) -> None:
    asyncio.run(_personal_watch_uses_wait_bridge_cursor_and_cancel_teardown(tmp_path))


async def _personal_watch_uses_wait_bridge_cursor_and_cancel_teardown(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "wait-calls.jsonl"
    script = tmp_path / "wait_bridge.py"
    _stdio_server_script(
        script,
        """import asyncio, json, os
from pathlib import Path
server = MCPServer('wait-bridge-stub')
@server.tool()
async def a2a_wait(boards: list[str], only_mine: bool, timeout_s: int, since_seq: dict[str, int] | None = None):
    marker = Path(os.environ['WAIT_MARKER'])
    with marker.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'boards': boards, 'only_mine': only_mine, 'timeout_s': timeout_s, 'since_seq': since_seq}, sort_keys=True) + '\\n')
    if since_seq is None:
        return {'new_seq': {'pursers': 7}, 'events': [{'seq': 7, 'kind': 'ticket_created', 'ticket_id': 'TK-live'}], 'timed_out': False, 'resynced': {'pursers': False}}
    await asyncio.sleep(60)
    return {'new_seq': since_seq, 'events': [], 'timed_out': True, 'resynced': {'pursers': False}}
""",
    )
    profile = SimpleNamespace(board_id="pursers", principal_id="PR-human")
    board = PersonalBoardSurface(profile)
    board.agent_name = "human-personal"
    bridges: list[StdioWaitBridge] = []

    class RejectDirectWatchClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call_tool(self, name: str, _arguments: JSON) -> None:
            self.calls.append(name)
            raise AssertionError("watch bypassed the configured wait bridge")

        def listen(self, **_kwargs: object) -> None:
            self.calls.append("listen")
            raise AssertionError("watch opened Central's journal directly")

    central = RejectDirectWatchClient()
    board._client = central  # type: ignore[assignment]

    async def factory() -> StdioWaitBridge:
        bridge = await StdioWaitBridge.connect(
            sys.executable, [str(script)], {"WAIT_MARKER": str(marker)}
        )
        bridges.append(bridge)
        return bridge

    board._wait_bridge_factory = factory
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        prompt = asyncio.create_task(client.prompt(session, "watch pursers"))
        async with asyncio.timeout(5):
            while not marker.exists() or len(marker.read_text().splitlines()) < 2:
                await asyncio.sleep(0.01)
        await client.notify("session/cancel", {"sessionId": session})
        assert await prompt == {"stopReason": "cancelled"}
    finally:
        await client.close()

    calls = [json.loads(line) for line in marker.read_text().splitlines()]
    assert calls[0] == {
        "boards": ["pursers"],
        "only_mine": False,
        "since_seq": None,
        "timeout_s": 180,
    }
    assert calls[1]["since_seq"] == {"pursers": 7}
    assert bridges and bridges[0]._client is None
    assert central.calls == []


def test_nonempty_stdio_mcp_servers_initialize_and_close(tmp_path: Path) -> None:
    asyncio.run(_nonempty_stdio_mcp_servers_initialize_and_close(tmp_path))


async def _nonempty_stdio_mcp_servers_initialize_and_close(tmp_path: Path) -> None:
    marker = tmp_path / "mcp-marker.txt"
    script = tmp_path / "mcp_server.py"
    _stdio_server_script(
        script,
        """import os
from pathlib import Path
Path(os.environ['MCP_MARKER']).write_text(os.environ['PRIVATE_VALUE'], encoding='utf-8')
server = MCPServer('acp-session-mcp-stub')
""",
    )
    secret = "mcp-private-value"
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(
            tmp_path,
            [
                {
                    "name": "fixture",
                    "command": sys.executable,
                    "args": [str(script)],
                    "env": [
                        {"name": "MCP_MARKER", "value": str(marker)},
                        {"name": "PRIVATE_VALUE", "value": secret},
                    ],
                }
            ],
        )
        assert session.startswith("pursers-")
        assert marker.read_text(encoding="utf-8") == secret
        rendered = json.dumps(
            {"updates": client.updates, "permissions": client.permissions},
            sort_keys=True,
        )
        assert secret not in rendered
    finally:
        await client.close()
    assert board.closed


def test_cancel_stops_in_flight_read_and_returns_cancelled(tmp_path: Path) -> None:
    asyncio.run(_cancel_stops_in_flight_read_and_returns_cancelled(tmp_path))


def test_cancel_before_prompt_task_starts_is_bound_to_that_turn(
    tmp_path: Path,
) -> None:
    asyncio.run(_cancel_before_prompt_task_starts_is_bound_to_that_turn(tmp_path))


async def _cancel_before_prompt_task_starts_is_bound_to_that_turn(
    tmp_path: Path,
) -> None:
    board = BlockingBoard()
    agent = PursersACPAgent(lambda: board)
    agent.initialized = True
    agent.board = board
    agent.sessions["session"] = agent_module.Session(cwd=str(tmp_path))
    sent: list[JSON] = []
    response_sent = asyncio.Event()

    async def send(message: JSON) -> None:
        sent.append(message)
        if message.get("id") in {1, 2}:
            response_sent.set()

    agent.send = send
    await agent.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/prompt",
            "params": {
                "sessionId": "session",
                "prompt": [{"type": "text", "text": "my tickets"}],
            },
        }
    )
    await agent.handle(
        {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": "session"},
        }
    )
    await asyncio.wait_for(response_sent.wait(), 1)

    response = next(message for message in sent if message.get("id") == 1)
    assert response["result"] == {"stopReason": "cancelled"}
    assert not board.read_started.is_set()
    assert not agent.sessions["session"].active

    # Cancellation belongs only to request 1; the next turn gets a fresh event.
    agent.board = FakeBoard()
    response_sent.clear()
    await agent.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {
                "sessionId": "session",
                "prompt": [{"type": "text", "text": "my tickets"}],
            },
        }
    )
    await asyncio.wait_for(response_sent.wait(), 1)
    second = next(message for message in sent if message.get("id") == 2)
    assert second["result"] == {"stopReason": "end_turn"}
    await agent.close()


async def _cancel_stops_in_flight_read_and_returns_cancelled(
    tmp_path: Path,
) -> None:
    board = BlockingBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        prompt = asyncio.create_task(client.prompt(session, "my tickets"))
        await asyncio.wait_for(board.read_started.wait(), 1)
        await client.notify("session/cancel", {"sessionId": session})
        assert await asyncio.wait_for(prompt, 1) == {"stopReason": "cancelled"}
        assert board.read_cancelled.is_set()
    finally:
        await client.close()


def test_cancel_stops_approved_in_flight_write(tmp_path: Path) -> None:
    asyncio.run(_cancel_stops_approved_in_flight_write(tmp_path))


async def _cancel_stops_approved_in_flight_write(tmp_path: Path) -> None:
    board = BlockingBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        prompt = asyncio.create_task(
            client.prompt(session, "create ticket Title :: Description")
        )
        await asyncio.wait_for(board.mutate_started.wait(), 1)
        await client.notify("session/cancel", {"sessionId": session})
        assert await asyncio.wait_for(prompt, 1) == {"stopReason": "cancelled"}
        assert board.mutate_cancelled.is_set()
        updates = [row["update"] for row in client.updates]
        assert any(
            row.get("sessionUpdate") == "tool_call_update"
            and row.get("status") == "failed"
            and row.get("content", [{}])[0]
            .get("content", {})
            .get("text")
            == "Board write was cancelled."
            for row in updates
        )
    finally:
        await client.close()


def test_authenticate_runs_setup_without_exposing_credentials(tmp_path: Path) -> None:
    asyncio.run(_authenticate_runs_setup_without_exposing_credentials(tmp_path))


async def _authenticate_runs_setup_without_exposing_credentials(tmp_path: Path) -> None:
    secret = "seat-token-must-not-escape"
    board = FakeBoard()
    configured = False
    settings: JSON = {}

    def board_factory() -> FakeBoard:
        if not configured:
            raise AuthRequired("profile missing")
        return board

    async def setup() -> None:
        nonlocal configured
        configured = True
        settings["profile"] = "/PATH/TO/profile.json"

    client = FakeACPClient(PursersACPAgent(board_factory, auth_setup=setup))
    try:
        initialized = await client.initialize()
        assert [row["id"] for row in initialized["authMethods"]] == [
            "pursers-personal-profile",
            "pursers-personal-login",
        ]
        await client.request(
            "authenticate", {"methodId": "pursers-personal-profile"}
        )
        await client.new_session(tmp_path)
        rendered = json.dumps(
            {
                "initialized": initialized,
                "updates": client.updates,
                "permissions": client.permissions,
                "settings": settings,
            },
            sort_keys=True,
        )
        assert secret not in rendered
        assert "TOKEN" not in settings
    finally:
        await client.close()


def test_login_invokes_personal_setup_when_profile_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = Namespace(profile=None, project=tmp_path, login=True)
    calls: list[Path | None] = []
    selections = 0

    class Parser:
        def parse_args(self) -> Namespace:
            return args

    def select(**_kwargs: object) -> object:
        nonlocal selections
        selections += 1
        if selections == 1:
            raise agent_module.PersonalProfileError("missing")
        return object()

    monkeypatch.setattr(agent_module, "_parser", lambda: Parser())
    monkeypatch.setattr(agent_module, "select_personal_profile", select)
    monkeypatch.setattr(
        agent_module, "_run_personal_setup", lambda project: calls.append(project)
    )
    agent_module.main()
    assert calls == [tmp_path]
    assert selections == 2


def test_setup_environment_excludes_inherited_seat_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "seat-secret")
    monkeypatch.setenv("ONBOARD_TOKEN_FILE", "/secret/token")
    monkeypatch.setenv("PATH", "/usr/bin")
    environment = agent_module._setup_environment()
    assert environment["PATH"] == "/usr/bin"
    assert "ONBOARD_CENTRAL_TOKEN" not in environment
    assert "ONBOARD_TOKEN_FILE" not in environment


def test_personal_setup_command_has_no_inherited_board_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "seat-secret")
    monkeypatch.setenv("ONBOARD_TOKEN_FILE", "/secret/token")
    monkeypatch.setattr(agent_module.shutil, "which", lambda _name: "/bin/setup")
    monkeypatch.setattr(
        agent_module, "default_profiles_root", lambda: tmp_path / "profiles"
    )
    monkeypatch.setattr(
        agent_module,
        "profile_path_for_project",
        lambda _project, root: root / "project-fixture" / "profile.json",
    )

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs["env"]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(agent_module.subprocess, "run", run)
    agent_module._run_personal_setup(tmp_path)
    command, environment = calls[0]
    assert command == [
        "/bin/setup",
        "setup",
        "--project",
        str(tmp_path.resolve()),
        "--apply",
        "--activate",
        "--host-id",
        "pursers-acp",
        "--session",
        "ide",
        "--host-config",
        str(tmp_path / "profiles" / "project-fixture" / "acp-host.json"),
    ]
    assert "seat-secret" not in json.dumps(command)
    assert "ONBOARD_CENTRAL_TOKEN" not in environment
    assert "ONBOARD_TOKEN_FILE" not in environment


class InProcessPersonalBoard(PersonalBoardSurface):
    def __init__(self, raw: Client, principal_id: str) -> None:
        self.profile = None
        self.board_id = "acp-e2e"
        self.principal_id = principal_id
        self.agent_name = "human-personal"
        self._client = raw

    async def close(self) -> None:
        self._client = None


def test_end_to_end_create_against_in_process_central(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_end_to_end_create_against_in_process_central(tmp_path, monkeypatch))


async def _end_to_end_create_against_in_process_central(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jwks = tmp_path / "jwks.json"
    jwks.write_text('{"keys": []}', encoding="utf-8")
    monkeypatch.setenv("CENTRAL_AUTH_MODE", "jwt")
    monkeypatch.setenv("CENTRAL_JWT_ISSUER", "https://issuer.invalid")
    monkeypatch.setenv("CENTRAL_JWT_AUDIENCE", "http://localhost:8765/mcp")
    monkeypatch.setenv("CENTRAL_JWKS_PATH", str(jwks))
    monkeypatch.setenv("CENTRAL_ADMISSION", "invite")
    monkeypatch.setenv("STORE_BACKEND", "sqlite")
    mcp, _service = central.build_server("localhost", 8765, tmp_path / "central")
    principal = central.Principal(
        "PR-acp-human",
        "acp-human",
        frozenset({"board:read", "board:write", "board:review"}),
    )
    monkeypatch.setattr(central, "current_principal", lambda: principal)
    async with Client(mcp, mode="2026-07-28", cache=None) as raw:
        joined = BoardClient._decode(
            await raw.call_tool(
                "board_join",
                {
                    "board_id": "acp-e2e",
                    "agent_name": "human-personal",
                    "role": "worker",
                    "capabilities": {"can_work": False, "can_review": False},
                },
            )
        )
        assert joined["principal_id"] == principal.principal_id
        board = InProcessPersonalBoard(raw, principal.principal_id)
        client = FakeACPClient(PursersACPAgent(lambda: board))
        try:
            await client.initialize()
            session = await client.new_session(tmp_path)
            assert (
                await client.prompt(session, "create ticket ACP E2E :: Created through ACP")
            ) == {"stopReason": "end_turn"}
            created_text = client.updates[-1]["update"]["content"]["text"]
            ticket_id = created_text.removeprefix("Created ticket ").removesuffix(".")
            fetched = BoardClient._decode(
                await raw.call_tool(
                    "ticket_get", {"board_id": "acp-e2e", "ticket_id": ticket_id}
                )
            )
            assert fetched["ticket"]["title"] == "ACP E2E"
            assert fetched["ticket"]["created_by_principal_id"] == principal.principal_id
        finally:
            await client.close()


def _bridge_profile() -> SimpleNamespace:
    return SimpleNamespace(central_url="https://127.0.0.1:1/mcp", board_id="pursers")


def test_wait_bridge_environment_forwards_model_and_provider_from_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PURSERS_MODEL", "env-model")
    identity = {
        "agent_name": "ide-seat",
        "role": "worker",
        "capabilities": {"model": "joined-model", "provider": "joined-provider"},
    }

    environment = agent_module._wait_bridge_environment(
        _bridge_profile(), "not-a-real-token", identity
    )

    assert environment["PURSERS_MODEL"] == "joined-model"
    assert environment["PURSERS_PROVIDER"] == "joined-provider"


def test_wait_bridge_environment_falls_back_to_agent_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PURSERS_MODEL", " env-model ")
    monkeypatch.delenv("PURSERS_PROVIDER", raising=False)
    identity = {"agent_name": "ide-seat", "role": "worker", "capabilities": {"model": None}}

    environment = agent_module._wait_bridge_environment(
        _bridge_profile(), "not-a-real-token", identity
    )

    assert environment["PURSERS_MODEL"] == "env-model"
    assert "PURSERS_PROVIDER" not in environment
