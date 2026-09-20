from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcp import Client, StdioServerParameters, types
from mcp.server.mcpserver import MCPServer

from pursers_client.mcp_proxy import (
    DIAGNOSTIC_TOOL,
    CentralRelay,
    build_server,
    central_mcp_url,
)


def _result(value: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=value)],
        structuredContent={"value": value},
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
        assert client.calls == [("board_status", {"board_id": "board-from-config"})]
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
        assert len(fingerprints) == 2
        assert fingerprints[0] != fingerprints[1]
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


def test_missing_token_becomes_clean_tool_error(tmp_path: Path, capsys: Any) -> None:
    asyncio.run(_missing_token_becomes_clean_tool_error(tmp_path, capsys))


async def _missing_token_becomes_clean_tool_error(tmp_path: Path, capsys: Any) -> None:
    missing = tmp_path / "missing.jwt"
    relay = CentralRelay(
        central_url="http://127.0.0.1:9999",
        board="missing-board",
        token_file=missing,
    )
    tools = await relay.list_tools()
    assert [tool.name for tool in tools] == [DIAGNOSTIC_TOOL]
    result = await relay.call_tool(DIAGNOSTIC_TOOL, {})
    assert result.is_error
    assert "missing or unreadable" in result.content[0].text
    assert "credential" not in result.content[0].text
    assert capsys.readouterr().out == ""


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
    assert [tool.name for tool in tools.tools] == [DIAGNOSTIC_TOOL]
    assert [prompt.name for prompt in prompts.prompts] == [
        "board",
        "create",
        "watch",
        "evidence",
        "answer",
    ]
    assert "stdio-board" in rendered.messages[0].content.text
