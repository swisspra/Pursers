from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pursers_client.mcp_proxy as mcp_proxy
from mcp import Client, StdioServerParameters, types
from mcp.server.mcpserver import MCPServer
from pursers_client.mcp_proxy import (
    SETUP_STATUS_TOOL,
    SETUP_TOOL,
    CentralRelay,
    RelayFailure,
    build_server,
    central_mcp_url,
    parser,
)


def _result(value: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=value)],
        structuredContent={"value": value},
    )


def _json_result(value: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(value))],
        structuredContent=value,
    )


class FakeClient:
    def __init__(self) -> None:
        self.tool = types.Tool(
            name="board_status",
            description="Read one board without mutation.",
            inputSchema={
                "type": "object",
                "properties": {"board_id": {"type": "string"}},
                "required": ["board_id"],
            },
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self, **_kwargs: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[self.tool])

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, **_kwargs: Any
    ) -> types.CallToolResult:
        payload = arguments or {}
        self.calls.append((name, payload))
        return _result(payload.get("board_id", "missing"))


class ExistingIdentityClient(FakeClient):
    def __init__(
        self,
        names: list[str],
        principal_id: str,
        *,
        lifecycle_statuses: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.names = names
        self.principal_id = principal_id
        self.agent_ids = [f"AI-existing-{index}" for index, _ in enumerate(names)]
        self.lifecycle_statuses = lifecycle_statuses or ["active"] * len(names)
        assert len(self.lifecycle_statuses) == len(names)
        self.tool = types.Tool(
            name="ticket_create",
            description="Create work.",
            inputSchema={
                "type": "object",
                "properties": {
                    "board_id": {"type": "string"},
                    "agent_name": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["board_id", "agent_name", "title"],
            },
        )

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, **_kwargs: Any
    ) -> types.CallToolResult:
        payload = arguments or {}
        self.calls.append((name, payload))
        if name == "board_list":
            return _json_result(
                {
                    "ok": True,
                    "boards": [
                        {
                            "board_id": "existing-board",
                            "agent_ids": self.agent_ids,
                            "agent_names": self.names,
                        }
                    ],
                }
            )
        if name == "board_status":
            return _json_result(
                {
                    "ok": True,
                    "board_id": "existing-board",
                    "agents": [
                        {
                            "agent_id": agent_id,
                            "agent_name": agent_name,
                            "principal_id": self.principal_id,
                            "lifecycle_status": lifecycle_status,
                        }
                        for agent_id, agent_name, lifecycle_status in zip(
                            self.agent_ids,
                            self.names,
                            self.lifecycle_statuses,
                            strict=True,
                        )
                    ],
                }
            )
        return _json_result({"ok": True, "arguments": payload})


def test_central_mcp_url_normalizes_only_an_empty_path() -> None:
    assert central_mcp_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000/mcp"
    assert central_mcp_url("https://central.example/mcp/") == "https://central.example/mcp"


def test_relay_preserves_central_tool_and_supplies_configured_board(
    tmp_path: Path,
) -> None:
    asyncio.run(_relay_preserves_central_tool_and_supplies_configured_board(tmp_path))


async def _relay_preserves_central_tool_and_supplies_configured_board(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "credential.jwt"
    token_file.write_text("opaque-test-credential", encoding="utf-8")
    client = FakeClient()

    @asynccontextmanager
    async def connect(_token: str):
        yield client

    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="board-from-config",
        token_file=token_file,
        connection_factory=connect,
    )
    try:
        tools = await relay.list_tools()
        assert tools == [client.tool]
        assert tools[0].description == "Read one board without mutation."
        assert tools[0].input_schema == client.tool.input_schema
        result = await relay.call_tool("board_status", {})
        assert not result.is_error
        assert client.calls == [
            ("board_list", {}),
            ("board_status", {"board_id": "board-from-config"}),
            ("board_status", {"board_id": "board-from-config"}),
        ]
    finally:
        await relay.aclose()


def test_token_file_is_reread_after_401(tmp_path: Path) -> None:
    asyncio.run(_token_file_is_reread_after_401(tmp_path))


async def _token_file_is_reread_after_401(tmp_path: Path) -> None:
    token_file = tmp_path / "credential.jwt"
    token_file.write_text("first-credential", encoding="utf-8")
    fingerprints: list[str] = []

    class UnauthorizedClient(FakeClient):
        async def list_tools(self, **_kwargs: Any) -> types.ListToolsResult:
            token_file.write_text("rotated-credential", encoding="utf-8")
            response = SimpleNamespace(status_code=401)
            error = RuntimeError("HTTP 401 unauthorized")
            error.response = response  # type: ignore[attr-defined]
            raise error

    @asynccontextmanager
    async def connect(token: str):
        fingerprints.append(hashlib.sha256(token.encode("utf-8")).hexdigest())
        if len(fingerprints) == 1:
            yield UnauthorizedClient()
        else:
            yield FakeClient()

    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="rotation-board",
        token_file=token_file,
        connection_factory=connect,
    )
    try:
        tools = await relay.list_tools()
        assert [tool.name for tool in tools] == ["board_status"]
        assert len(fingerprints) == 4
        assert fingerprints[0] != fingerprints[1]
        assert fingerprints[1] == fingerprints[2]
        assert fingerprints[2] == fingerprints[3]
    finally:
        await relay.aclose()


def test_default_tool_set_is_curated_and_all_is_opt_in(tmp_path: Path) -> None:
    asyncio.run(_default_tool_set_is_curated_and_all_is_opt_in(tmp_path))


async def _default_tool_set_is_curated_and_all_is_opt_in(tmp_path: Path) -> None:
    token_file = tmp_path / "credential.jwt"
    token_file.write_text("opaque-test-credential", encoding="utf-8")

    class BroadClient(FakeClient):
        async def list_tools(self, **_kwargs: Any) -> types.ListToolsResult:
            destructive = types.Tool(
                name="journal_compact",
                description="Not part of the Zed persona.",
                inputSchema={"type": "object", "properties": {}},
            )
            return types.ListToolsResult(tools=[self.tool, destructive])

    client = BroadClient()

    @asynccontextmanager
    async def connect(_token: str):
        yield client

    default_relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="curated-board",
        token_file=token_file,
        connection_factory=connect,
    )
    all_relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="curated-board",
        token_file=token_file,
        tools_mode="all",
        connection_factory=connect,
    )
    assert [tool.name for tool in await default_relay.list_tools()] == ["board_status"]
    assert [tool.name for tool in await all_relay.list_tools()] == [
        "board_status",
        "journal_compact",
    ]


def test_local_setup_identity_is_hidden_and_injected(tmp_path: Path) -> None:
    asyncio.run(_local_setup_identity_is_hidden_and_injected(tmp_path))


async def _local_setup_identity_is_hidden_and_injected(tmp_path: Path) -> None:
    setup_root = tmp_path / "central"
    setup_root.mkdir()
    (setup_root / "worker.jwt").write_text("opaque-test-credential", encoding="utf-8")

    class TicketClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.tool = types.Tool(
                name="ticket_create",
                description="Create work.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "board_id": {"type": "string"},
                        "agent_name": {"type": "string"},
                        "ticket_id": {"type": "string"},
                        "title": {"type": "string"},
                    },
                    "required": ["board_id", "agent_name", "title"],
                },
            )

    client = TicketClient()

    @asynccontextmanager
    async def connect(_token: str):
        yield client

    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="fresh-local-board",
        setup_root=setup_root,
        connection_factory=connect,
    )
    tools = await relay.list_tools()
    assert len(tools) == 1
    assert "agent_name" not in tools[0].input_schema["properties"]
    assert "agent_name" not in tools[0].input_schema["required"]

    result = await relay.call_tool(
        "ticket_create",
        {"ticket_id": "TK-first", "title": "First ticket"},
    )
    assert not result.is_error
    assert client.calls[-1] == (
        "ticket_create",
        {
            "agent_name": "zed-local-owner",
            "board_id": "fresh-local-board",
            "ticket_id": "TK-first",
            "title": "First ticket",
        },
    )


def test_existing_central_single_identity_is_hidden_and_injected(
    tmp_path: Path,
) -> None:
    asyncio.run(_existing_central_single_identity_is_hidden_and_injected(tmp_path))


async def _existing_central_single_identity_is_hidden_and_injected(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "existing-worker.jwt"
    token_file.write_text("opaque-existing-credential", encoding="utf-8")
    client = ExistingIdentityClient(["existing-owner"], "PR-existing")

    @asynccontextmanager
    async def connect(_token: str):
        yield client

    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="existing-board",
        token_file=token_file,
        connection_factory=connect,
    )
    tools = await relay.list_tools()
    assert len(tools) == 1
    assert "agent_name" not in tools[0].input_schema["properties"]
    assert "agent_name" not in tools[0].input_schema["required"]

    result = await relay.call_tool("ticket_create", {"title": "First ticket"})
    assert not result.is_error
    assert result.structured_content == {
        "ok": True,
        "arguments": {
            "agent_name": "existing-owner",
            "board_id": "existing-board",
            "title": "First ticket",
        },
    }
    assert client.calls[-1] == (
        "ticket_create",
        {
            "agent_name": "existing-owner",
            "board_id": "existing-board",
            "title": "First ticket",
        },
    )


def test_existing_central_ambiguous_identity_requires_an_exact_choice(
    tmp_path: Path,
) -> None:
    asyncio.run(_existing_central_ambiguous_identity_requires_an_exact_choice(tmp_path))


async def _existing_central_ambiguous_identity_requires_an_exact_choice(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "existing-worker.jwt"
    token_file.write_text("opaque-existing-credential", encoding="utf-8")
    client = ExistingIdentityClient(
        ["existing-owner", "existing-worker"], "PR-existing"
    )

    @asynccontextmanager
    async def connect(_token: str):
        yield client

    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="existing-board",
        token_file=token_file,
        connection_factory=connect,
    )
    tools = await relay.list_tools()
    assert len(tools) == 1
    agent_schema = tools[0].input_schema["properties"]["agent_name"]
    assert "agent_name" not in tools[0].input_schema["required"]
    assert "existing-owner, existing-worker" in agent_schema["description"]

    missing = await relay.call_tool("ticket_create", {"title": "First ticket"})
    assert missing.is_error
    assert missing.structured_content == {
        "ok": False,
        "error": (
            "Central authenticated principal PR-existing on board existing-board with "
            "multiple active agent names: existing-owner, existing-worker. Retry with "
            "agent_name set to one of those exact names."
        ),
    }

    invalid = await relay.call_tool(
        "ticket_create", {"agent_name": "someone-else", "title": "First ticket"}
    )
    assert invalid.is_error
    assert "agent_name someone-else is not one of its active agent names" in (
        invalid.structured_content["error"]
    )

    selected = await relay.call_tool(
        "ticket_create", {"agent_name": "existing-worker", "title": "First ticket"}
    )
    assert not selected.is_error
    assert selected.structured_content == {
        "ok": True,
        "arguments": {
            "agent_name": "existing-worker",
            "board_id": "existing-board",
            "title": "First ticket",
        },
    }


def test_wait_timeout_is_capped_at_fifty_seconds(tmp_path: Path) -> None:
    token_file = tmp_path / "credential.jwt"
    token_file.write_text("opaque-test-credential", encoding="utf-8")
    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="wait-board",
        token_file=token_file,
    )
    relay._known_tools["a2a_wait"] = types.Tool(
        name="a2a_wait",
        description="Wait and return a resumable cursor.",
        inputSchema={
            "type": "object",
            "properties": {
                "since_seq": {"type": "integer"},
                "timeout_s": {"type": "integer"},
            },
        },
    )
    assert relay._arguments_for("a2a_wait", {"since_seq": 41}) == {
        "since_seq": 41,
        "timeout_s": 50,
    }
    assert relay._arguments_for("a2a_wait", {"timeout_s": 600}) == {
        "timeout_s": 50
    }


def test_missing_token_serves_setup_tools_instead_of_an_error(
    tmp_path: Path, capsys: Any
) -> None:
    asyncio.run(_missing_token_serves_setup_tools_instead_of_an_error(tmp_path, capsys))


async def _missing_token_serves_setup_tools_instead_of_an_error(
    tmp_path: Path, capsys: Any
) -> None:
    missing = tmp_path / "missing.jwt"
    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="missing-board",
        token_file=missing,
    )
    tools = await relay.list_tools()
    assert [tool.name for tool in tools] == [SETUP_STATUS_TOOL, SETUP_TOOL]
    result = await relay.call_tool(SETUP_STATUS_TOOL, {})
    assert not result.is_error
    assert result.structured_content["central"]["reachable"] is False
    assert result.structured_content["board"] == {
        "id": "missing-board",
        "available": False,
        "checked": False,
        "error": None,
    }
    assert result.structured_content["token_file"]["readable"] is False
    assert capsys.readouterr().out == ""


def test_setup_does_nothing_without_consent(tmp_path: Path) -> None:
    asyncio.run(_setup_does_nothing_without_consent(tmp_path))


async def _setup_does_nothing_without_consent(tmp_path: Path) -> None:
    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="declined-board",
        setup_root=tmp_path / "central",
    )
    relay._provision = AsyncMock()  # type: ignore[method-assign]

    class DecliningContext:
        async def elicit(self, _message: str, _schema: Any) -> Any:
            return SimpleNamespace(action="decline")

    result = await relay.call_tool(SETUP_TOOL, {}, DecliningContext())
    assert not result.is_error
    assert result.structured_content == {
        "ok": False,
        "provisioned": False,
        "reason": "decline",
    }
    relay._provision.assert_not_awaited()  # type: ignore[attr-defined]
    assert not (tmp_path / "central").exists()


def test_existing_central_ignores_inactive_identities_when_injecting(
    tmp_path: Path,
) -> None:
    asyncio.run(_existing_central_ignores_inactive_identities_when_injecting(tmp_path))


async def _existing_central_ignores_inactive_identities_when_injecting(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "existing-worker.jwt"
    token_file.write_text("opaque-existing-credential", encoding="utf-8")
    client = ExistingIdentityClient(
        ["existing-owner", "retired-worker", "stale-worker", "handed-off-worker"],
        "PR-existing",
        lifecycle_statuses=["active", "retired", "stale", "handed_off"],
    )

    @asynccontextmanager
    async def connect(_token: str):
        yield client

    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="existing-board",
        token_file=token_file,
        connection_factory=connect,
    )
    tools = await relay.list_tools()
    assert len(tools) == 1
    assert "agent_name" not in tools[0].input_schema["properties"]
    assert "agent_name" not in tools[0].input_schema["required"]

    result = await relay.call_tool("ticket_create", {"title": "First ticket"})
    assert not result.is_error
    assert result.structured_content == {
        "ok": True,
        "arguments": {
            "agent_name": "existing-owner",
            "board_id": "existing-board",
            "title": "First ticket",
        },
    }


def test_half_failed_setup_stops_the_started_process(tmp_path: Path) -> None:
    asyncio.run(_half_failed_setup_stops_the_started_process(tmp_path))


async def _half_failed_setup_stops_the_started_process(tmp_path: Path) -> None:
    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="failed-board",
        setup_root=tmp_path / "central",
        uvx_path="/usr/bin/true",
    )
    process = SimpleNamespace(pid=43210, returncode=None)
    relay._port_is_open = AsyncMock(return_value=False)  # type: ignore[method-assign]
    relay._run_init = AsyncMock()  # type: ignore[method-assign]
    relay._start_central = AsyncMock(  # type: ignore[method-assign]
        return_value=(process, tmp_path / "central.log")
    )
    relay._wait_for_central = AsyncMock(return_value="admin-token")  # type: ignore[method-assign]
    relay._create_board = AsyncMock(  # type: ignore[method-assign]
        side_effect=RelayFailure("board creation failed")
    )
    relay._stop_central = AsyncMock()  # type: ignore[method-assign]

    try:
        await relay._provision()
    except RelayFailure as exc:
        assert str(exc) == "board creation failed"
    else:  # pragma: no cover - protects the cleanup assertion below
        raise AssertionError("setup unexpectedly succeeded")

    relay._stop_central.assert_awaited_once_with(process)  # type: ignore[attr-defined]


def _offline_first_run_relay(setup_root: Path) -> CentralRelay:
    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="first-run-board",
        setup_root=setup_root,
        uvx_path="/usr/bin/true",
    )
    process = SimpleNamespace(pid=43210, returncode=None)
    relay._upstream_tools = AsyncMock(  # type: ignore[method-assign]
        side_effect=[OSError("Central is not running"), []]
    )
    relay._port_is_open = AsyncMock(return_value=False)  # type: ignore[method-assign]
    relay._run_init = AsyncMock()  # type: ignore[method-assign]
    relay._start_central = AsyncMock(  # type: ignore[method-assign]
        return_value=(process, setup_root / "central.log")
    )
    relay._wait_for_central = AsyncMock(  # type: ignore[method-assign]
        return_value="admin-token"
    )
    relay._create_board = AsyncMock()  # type: ignore[method-assign]
    return relay


def test_first_run_refuses_deployed_install_in_target_without_writing(
    tmp_path: Path,
) -> None:
    asyncio.run(_first_run_refuses_deployed_install_in_target_without_writing(tmp_path))


async def _first_run_refuses_deployed_install_in_target_without_writing(
    tmp_path: Path,
) -> None:
    setup_root = tmp_path / "occupied-central"
    deployed = setup_root / ".private-arm" / "central-data"
    deployed.mkdir(parents=True)
    marker = deployed / "profile.env"
    marker.write_text("PURSERS_BOARD_ID=production\n", encoding="utf-8")
    before = marker.read_bytes()
    relay = _offline_first_run_relay(setup_root)

    try:
        await relay._provision()
    except RelayFailure as exc:
        message = str(exc)
    else:  # pragma: no cover - protects the no-write assertions below
        raise AssertionError("setup unexpectedly accepted a deployed target")

    assert str(setup_root) in message
    assert str(deployed) in message
    assert "setup_root" in message
    assert marker.read_bytes() == before
    relay._run_init.assert_not_awaited()  # type: ignore[attr-defined]
    relay._start_central.assert_not_awaited()  # type: ignore[attr-defined]


def test_first_run_refuses_unrelated_occupied_target_without_writing(
    tmp_path: Path,
) -> None:
    asyncio.run(_first_run_refuses_unrelated_occupied_target_without_writing(tmp_path))


async def _first_run_refuses_unrelated_occupied_target_without_writing(
    tmp_path: Path,
) -> None:
    setup_root = tmp_path / "occupied-central"
    setup_root.mkdir()
    marker = setup_root / "deployment-script.sh"
    marker.write_text("#!/bin/sh\n", encoding="utf-8")
    relay = _offline_first_run_relay(setup_root)

    try:
        await relay._provision()
    except RelayFailure as exc:
        message = str(exc)
    else:  # pragma: no cover - protects the no-write assertions below
        raise AssertionError("setup unexpectedly accepted an occupied target")

    assert str(setup_root) in message
    assert "already occupied" in message
    assert "setup_root" in message
    assert marker.read_text(encoding="utf-8") == "#!/bin/sh\n"
    relay._run_init.assert_not_awaited()  # type: ignore[attr-defined]
    relay._start_central.assert_not_awaited()  # type: ignore[attr-defined]


def test_first_run_succeeds_at_configured_setup_root(tmp_path: Path) -> None:
    asyncio.run(_first_run_succeeds_at_configured_setup_root(tmp_path))


async def _first_run_succeeds_at_configured_setup_root(tmp_path: Path) -> None:
    setup_root = tmp_path / "chosen-central"
    args = parser().parse_args(
        [
            "--central-url",
            "http://127.0.0.1:9999",
            "--board",
            "first-run-board",
            "--setup-root",
            str(setup_root),
        ]
    )
    assert args.setup_root == setup_root
    relay = _offline_first_run_relay(args.setup_root)

    result = await relay._provision()

    assert result["token_file"] == str(setup_root / "worker.jwt")
    assert result["log_file"] == str(setup_root / "central.log")
    relay._run_init.assert_awaited_once_with("/usr/bin/true", 9999)  # type: ignore[attr-defined]


def test_first_run_succeeds_at_genuinely_empty_default_root(
    tmp_path: Path, monkeypatch: Any
) -> None:
    asyncio.run(
        _first_run_succeeds_at_genuinely_empty_default_root(tmp_path, monkeypatch)
    )


async def _first_run_succeeds_at_genuinely_empty_default_root(
    tmp_path: Path, monkeypatch: Any
) -> None:
    setup_root = tmp_path / ".pursers" / "central"
    setup_root.mkdir(parents=True)
    assert list(setup_root.iterdir()) == []
    monkeypatch.setattr(mcp_proxy, "DEFAULT_INSTANCE_DIR", setup_root)
    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="first-run-board",
        uvx_path="/usr/bin/true",
    )
    prepared = _offline_first_run_relay(relay.setup_root)
    relay._upstream_tools = prepared._upstream_tools  # type: ignore[method-assign]
    relay._port_is_open = prepared._port_is_open  # type: ignore[method-assign]
    relay._run_init = prepared._run_init  # type: ignore[method-assign]
    relay._start_central = prepared._start_central  # type: ignore[method-assign]
    relay._wait_for_central = prepared._wait_for_central  # type: ignore[method-assign]
    relay._create_board = prepared._create_board  # type: ignore[method-assign]

    result = await relay._provision()

    assert relay.setup_root == setup_root
    assert result["token_file"] == str(setup_root / "worker.jwt")
    relay._run_init.assert_awaited_once_with("/usr/bin/true", 9999)  # type: ignore[attr-defined]


def test_setup_switches_tools_and_emits_wire_notification(tmp_path: Path) -> None:
    asyncio.run(_setup_switches_tools_and_emits_wire_notification(tmp_path))


async def _setup_switches_tools_and_emits_wire_notification(tmp_path: Path) -> None:
    token_file = tmp_path / "central" / "worker.jwt"
    available = False
    client = FakeClient()

    @asynccontextmanager
    async def connect(_token: str):
        if not available:
            raise OSError("Central is not running")
        yield client

    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="first-run-board",
        setup_root=tmp_path / "central",
        connection_factory=connect,
    )

    async def provision() -> dict[str, Any]:
        nonlocal available
        token_file.parent.mkdir(parents=True)
        token_file.write_text("opaque-test-credential", encoding="utf-8")
        available = True
        tools = await relay._upstream_tools()
        return {
            "ok": True,
            "central_url": relay.central_url,
            "board_id": relay.board,
            "token_file": str(token_file),
            "log_file": str(tmp_path / "central" / "central.log"),
            "pid": 43210,
            "real_tools": [tool.name for tool in tools],
        }

    relay._provision = provision  # type: ignore[method-assign]
    messages: list[Any] = []

    async def elicit(_context: Any, _params: Any) -> types.ElicitResult:
        return types.ElicitResult(action="accept", content={"provision": True})

    async def record(message: Any) -> None:
        messages.append(message)

    async with Client(
        build_server(relay),
        mode="legacy",
        cache=None,
        elicitation_callback=elicit,
        message_handler=record,
    ) as downstream:
        assert downstream.server_capabilities.tools.list_changed is True
        before = await downstream.list_tools()
        setup = await downstream.call_tool(SETUP_TOOL)
        after = await downstream.list_tools()

    assert [tool.name for tool in before.tools] == [SETUP_STATUS_TOOL, SETUP_TOOL]
    assert not setup.is_error
    assert setup.structured_content["tools_list_changed"] is True
    assert [tool.name for tool in after.tools] == ["board_status"]
    wire_methods = {
        getattr(getattr(message, "root", message), "method", None)
        for message in messages
    }
    assert "notifications/tools/list_changed" in wire_methods


def test_reachable_central_without_board_is_onboarded_after_consent(
    tmp_path: Path,
) -> None:
    asyncio.run(_reachable_central_without_board_is_onboarded_after_consent(tmp_path))


async def _reachable_central_without_board_is_onboarded_after_consent(
    tmp_path: Path,
) -> None:
    setup_root = tmp_path / "central"
    setup_root.mkdir()
    (setup_root / "worker.jwt").write_text("worker-token", encoding="utf-8")
    (setup_root / "admin.jwt").write_text("admin-token", encoding="utf-8")
    board_available = False

    class ReachableClient(FakeClient):
        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None, **_kwargs: Any
        ) -> types.CallToolResult:
            nonlocal board_available
            payload = arguments or {}
            self.calls.append((name, payload))
            if name == "board_onboard":
                board_available = True
                return _result("onboarded")
            if name == "board_status" and not board_available:
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text="board access denied: principal is not a member",
                        )
                    ],
                    isError=True,
                )
            return _result(payload.get("board_id", "missing"))

    client = ReachableClient()

    @asynccontextmanager
    async def connect(_token: str):
        yield client

    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="new-local-board",
        setup_root=setup_root,
        connection_factory=connect,
    )
    relay._http_connection = connect  # type: ignore[method-assign]

    class AcceptingContext:
        session = SimpleNamespace(send_tool_list_changed=AsyncMock())

        async def elicit(self, message: str, _schema: Any) -> Any:
            assert "create or join board new-local-board" in message
            return SimpleNamespace(
                action="accept", data=SimpleNamespace(provision=True)
            )

    assert [tool.name for tool in await relay.list_tools()] == [
        SETUP_STATUS_TOOL,
        SETUP_TOOL,
    ]
    result = await relay.call_tool(SETUP_TOOL, {}, AcceptingContext())
    assert not result.is_error
    assert result.structured_content["central_started"] is False
    assert result.structured_content["board_onboarded"] is True
    assert [tool.name for tool in await relay.list_tools()] == ["board_status"]
    assert any(name == "board_onboard" for name, _ in client.calls)


def test_reachable_non_member_gets_exact_admin_recovery_commands(
    tmp_path: Path,
) -> None:
    asyncio.run(_reachable_non_member_gets_exact_admin_recovery_commands(tmp_path))


async def _reachable_non_member_gets_exact_admin_recovery_commands(
    tmp_path: Path,
) -> None:
    setup_root = tmp_path / "central"
    setup_root.mkdir()
    (setup_root / "worker.jwt").write_text("worker-token", encoding="utf-8")

    class NonMemberClient(FakeClient):
        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None, **_kwargs: Any
        ) -> types.CallToolResult:
            if name == "board_status":
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text="board access denied: principal is not a member",
                        )
                    ],
                    isError=True,
                )
            return await super().call_tool(name, arguments, **_kwargs)

    @asynccontextmanager
    async def connect(_token: str):
        yield NonMemberClient()

    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="admin-owned-board",
        setup_root=setup_root,
        connection_factory=connect,
    )

    class AcceptingContext:
        session = SimpleNamespace(send_tool_list_changed=AsyncMock())

        async def elicit(self, _message: str, _schema: Any) -> Any:
            return SimpleNamespace(
                action="accept", data=SimpleNamespace(provision=True)
            )

    result = await relay.call_tool(SETUP_TOOL, {}, AcceptingContext())
    assert result.is_error
    error = result.structured_content["error"]
    assert "administrator of board admin-owned-board" in error
    assert "board_invite_create" in error
    assert "board_join" in error
    assert "agent_name zed-local-owner" in error


def test_prompts_have_at_most_one_argument_and_human_facing_copy(
    tmp_path: Path,
) -> None:
    asyncio.run(_prompts_have_at_most_one_argument_and_human_facing_copy(tmp_path))


async def _prompts_have_at_most_one_argument_and_human_facing_copy(
    tmp_path: Path,
) -> None:
    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="prompt-board",
        token_file=tmp_path / "unused.jwt",
    )
    server = build_server(relay)
    prompts = await server.list_prompts()
    assert [prompt.name for prompt in prompts] == [
        "board",
        "create",
        "watch",
        "evidence",
        "answer",
        "setup",
    ]
    assert all(len(prompt.arguments or []) <= 1 for prompt in prompts)
    rendered = {
        "board": await server.get_prompt("board"),
        "create": await server.get_prompt("create", {"summary": "Ship the guide"}),
        "watch": await server.get_prompt("watch"),
        "evidence": await server.get_prompt("evidence", {"ticket_id": "TK-123"}),
        "answer": await server.get_prompt(
            "answer", {"ticket_and_answer": "TK-123 approved"}
        ),
        "setup": await server.get_prompt("setup"),
    }
    text = {name: value.messages[0].content.text for name, value in rendered.items()}
    assert text == {
        "board": (
            "Your work on `prompt-board` at a glance: its board ID and board-wide key "
            "counts, followed by a clearly labeled subset of up to 10 active tickets "
            "needing attention. Each row leads with its ticket ID and gives its specific "
            "reason for needing attention. This view stays on this board."
        ),
        "create": (
            "New work for `prompt-board`: 'Ship the guide'. Any missing required detail "
            "comes first; creation happens once under Zed's single confirmation, followed "
            "by the new ticket ID and a brief recap."
        ),
        "watch": (
            "Live changes for `prompt-board`: work, reviews, and questions needing "
            "attention, limited to changed IDs and next steps. The watch resumes from its "
            "latest saved position and stays on this board."
        ),
        "evidence": (
            "Evidence for `TK-123` on `prompt-board`: the ticket ID first, then its exact "
            "branch and commit, changed files, literal test results, and independent-review "
            "state. The view contains no unrelated tickets."
        ),
        "answer": (
            "Answer for 'TK-123 approved' on `prompt-board`: the exact ticket's pending "
            "question is resolved once, followed by its ticket ID and resulting state. "
            "This stays on this board."
        ),
        "setup": (
            "Set up Pursers for `prompt-board` in this chat. Check the local setup "
            "status, explain what is missing, then run the setup tool. The setup tool "
            "asks me for confirmation before it creates files or starts Central. After "
            "setup, continue in this same chat with the newly available board tools."
        ),
    }
    forbidden = (
        "board_status",
        "ticket_list",
        "ticket_create",
        "a2a_wait",
        "board_catchup",
        "ticket_get",
        "include_closed",
        "false",
        "Markdown",
    )
    assert all(term not in body for term in forbidden for body in text.values())
    assert "subset of no more than 10 active tickets" in server.instructions
    assert "each row with its ID and specific reason" in server.instructions
    assert "host's single native confirmation" in server.instructions
    assert "resume only from a returned positive cursor" in server.instructions


def test_long_call_does_not_block_another_call(tmp_path: Path) -> None:
    asyncio.run(_long_call_does_not_block_another_call(tmp_path))


async def _long_call_does_not_block_another_call(tmp_path: Path) -> None:
    token_file = tmp_path / "credential.jwt"
    token_file.write_text("opaque-test-credential", encoding="utf-8")
    wait_started = asyncio.Event()
    release_wait = asyncio.Event()

    class ConcurrentClient(FakeClient):
        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None, **_kwargs: Any
        ) -> types.CallToolResult:
            if name == "a2a_wait":
                wait_started.set()
                await release_wait.wait()
                return _result("wait-complete")
            return _result("short-complete")

    @asynccontextmanager
    async def connect(_token: str):
        yield ConcurrentClient()

    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="concurrent-board",
        token_file=token_file,
        connection_factory=connect,
    )
    try:
        long_call = asyncio.create_task(relay.call_tool("a2a_wait", {}))
        await asyncio.wait_for(wait_started.wait(), timeout=1)
        short = await asyncio.wait_for(relay.call_tool("board_status", {}), timeout=1)
        assert short.structured_content == {"value": "short-complete"}
        release_wait.set()
        await asyncio.wait_for(long_call, timeout=1)
    finally:
        await relay.aclose()


def test_relay_calls_read_only_tool_on_throwaway_central(tmp_path: Path) -> None:
    asyncio.run(_relay_calls_read_only_tool_on_throwaway_central(tmp_path))


async def _relay_calls_read_only_tool_on_throwaway_central(tmp_path: Path) -> None:
    central = MCPServer("Throwaway Pursers Central")

    @central.tool(description="Read the throwaway board.")
    async def board_status(board_id: str) -> dict[str, Any]:
        return {"ok": True, "board_id": board_id, "ticket_count": 0}

    @asynccontextmanager
    async def connect(_token: str):
        async with Client(central, mode="2026-07-28", cache=None) as client:
            yield client

    token_file = tmp_path / "credential.jwt"
    token_file.write_text("throwaway-credential", encoding="utf-8")
    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="throwaway-board",
        token_file=token_file,
        connection_factory=connect,
    )
    try:
        tools = await relay.list_tools()
        assert [(tool.name, tool.description) for tool in tools] == [
            ("board_status", "Read the throwaway board.")
        ]
        result = await relay.call_tool("board_status", {})
        assert not result.is_error
        assert result.structured_content == {
            "ok": True,
            "board_id": "throwaway-board",
            "ticket_count": 0,
        }
    finally:
        await relay.aclose()


def test_board_prompt_matches_board_wide_counts_to_a_bounded_subset(
    tmp_path: Path,
) -> None:
    asyncio.run(_board_prompt_matches_board_wide_counts_to_a_bounded_subset(tmp_path))


async def _board_prompt_matches_board_wide_counts_to_a_bounded_subset(
    tmp_path: Path,
) -> None:
    central = MCPServer("Seeded throwaway Pursers Central")
    calls: list[tuple[str, str, int | None]] = []
    tickets = [
        {
            "ticket_id": f"TK-{index:03d}",
            "status": "open" if index <= 7 else "submitted",
            "attention_reason": (
                "work offer awaiting response"
                if index <= 7
                else "independent review awaiting claim"
            ),
        }
        for index in range(1, 13)
    ]

    @central.tool(description="Read board-wide counts from one board.")
    async def board_status(board_id: str) -> dict[str, Any]:
        calls.append(("board_status", board_id, None))
        return {
            "board_id": board_id,
            "key_counts": {"open": 7, "in_review": 5},
            "needs_attention_total": len(tickets),
        }

    @central.tool(description="Read a bounded subset of tickets from one board.")
    async def ticket_list(board_id: str, limit: int = 10) -> dict[str, Any]:
        calls.append(("ticket_list", board_id, limit))
        return {
            "board_id": board_id,
            "tickets": tickets[:limit],
            "returned": min(limit, len(tickets)),
            "total": len(tickets),
        }

    @asynccontextmanager
    async def connect(_token: str):
        async with Client(central, mode="2026-07-28", cache=None) as client:
            yield client

    token_file = tmp_path / "credential.jwt"
    token_file.write_text("throwaway-credential", encoding="utf-8")
    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="seeded-board",
        token_file=token_file,
        connection_factory=connect,
    )
    try:
        async with Client(build_server(relay), mode="2026-07-28", cache=None) as client:
            await client.list_tools()
            rendered = await client.get_prompt("board")
            status = await client.call_tool("board_status")
            listed = await client.call_tool("ticket_list", {"limit": 10})
    finally:
        await relay.aclose()

    prompt = rendered.messages[0].content.text
    assert "board-wide key counts" in prompt
    assert "clearly labeled subset of up to 10" in prompt
    assert "specific reason for needing attention" in prompt
    assert status.structured_content == {
        "board_id": "seeded-board",
        "key_counts": {"open": 7, "in_review": 5},
        "needs_attention_total": 12,
    }
    assert listed.structured_content == {
        "board_id": "seeded-board",
        "tickets": tickets[:10],
        "returned": 10,
        "total": 12,
    }
    assert calls == [
        ("board_status", "seeded-board", None),
        ("board_status", "seeded-board", None),
        ("ticket_list", "seeded-board", 10),
    ]


def test_stdio_framing_has_no_stdout_noise_and_completes_initialize(
    tmp_path: Path,
) -> None:
    asyncio.run(_stdio_framing_has_no_stdout_noise_and_completes_initialize(tmp_path))


async def _stdio_framing_has_no_stdout_noise_and_completes_initialize(
    tmp_path: Path,
) -> None:
    package_src = Path(__file__).resolve().parents[1] / "src"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(package_src), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "pursers_client.mcp_proxy",
            "--central-url",
            "http://127.0.0.1:9",
            "--board",
            "stdio-board",
            "--token-file",
            str(tmp_path / "missing.jwt"),
        ],
        env=environment,
    )
    # mcp v2 names the pre-2026 handshake family "legacy"; Zed 1.20.2 sends
    # the concrete 2025-11-25 protocolVersion within that family.
    async with Client(params, mode="legacy", cache=None) as client:
        tools = await client.list_tools()
        prompts = await client.list_prompts()
        rendered = await client.get_prompt("board")
    assert [tool.name for tool in tools.tools] == [SETUP_STATUS_TOOL, SETUP_TOOL]
    assert [prompt.name for prompt in prompts.prompts] == [
        "board",
        "create",
        "watch",
        "evidence",
        "answer",
        "setup",
    ]
    assert "stdio-board" in rendered.messages[0].content.text
