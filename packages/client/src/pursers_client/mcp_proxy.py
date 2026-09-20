"""Credential-safe stdio MCP relay for Zed and other local MCP hosts."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx2
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer


LOG = logging.getLogger("pursers-mcp")
DIAGNOSTIC_TOOL = "pursers_connection_status"
DEFAULT_TOOLS = frozenset(
    {
        "a2a_wait",
        "board_catchup",
        "board_list",
        "board_question_inbox",
        "board_snapshot",
        "board_status",
        "dispatch_my_offers",
        "memory_links",
        "memory_read",
        "memory_search",
        "ticket_annotate",
        "ticket_create",
        "ticket_get",
        "ticket_human_resolve",
        "ticket_list",
        "ticket_question_answer",
        "ticket_question_ask",
        "ticket_request_human",
    }
)
WAIT_TOOL_NAMES = frozenset({"a2a_wait", "ticket_question_wait"})
MAX_WAIT_SECONDS = 50
PROMPT_BEHAVIOR_INSTRUCTIONS = (
    "Pursers prompt behavior: board summaries stay on the selected board, report "
    "board-wide counts, clearly label the attention rows as a subset of no more than "
    "10 active tickets, and lead each row with its ID and specific reason. Ticket "
    "creation first collects only missing required fields, performs one create call, and "
    "relies on the host's single native confirmation. Watches prefer a2a_wait, otherwise "
    "use bounded board_catchup with a wait-bridge notice; they resume only from a returned "
    "positive cursor. Evidence and answers stay on one exact ticket, and answer resolution "
    "runs once. Results are compact and never expand into fleet or unrelated-board data."
)


class RelayFailure(RuntimeError):
    """A safe-to-display relay failure that never contains credential text."""


class UpstreamClient(Protocol):
    async def list_tools(self, **kwargs: Any) -> types.ListToolsResult: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any
    ) -> types.CallToolResult: ...


ConnectionFactory = Callable[[str], Any]


def package_version() -> str:
    try:
        return version("pursers-client")
    except PackageNotFoundError:
        return "0.1.0"


def central_mcp_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RelayFailure("--central-url must be an absolute http:// or https:// URL")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/mcp"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RelayFailure(f"token file is missing or unreadable: {path}") from exc
    if not token:
        raise RelayFailure(f"token file is empty: {path}")
    if "\n" in token or "\r" in token:
        raise RelayFailure(f"token file must contain exactly one credential line: {path}")
    return token


def _exception_has_status(exc: BaseException, status: int) -> bool:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == status:
        return True
    if getattr(exc, "status_code", None) == status:
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_exception_has_status(item, status) for item in exc.exceptions)
    text = str(exc).lower()
    return str(status) in text and (
        "unauthorized" in text or "status" in text or "http" in text
    )


def _result_is_unauthorized(result: types.CallToolResult) -> bool:
    if not result.is_error:
        return False
    for item in result.content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            lowered = text.lower()
            if "unauthorized" in lowered or "401" in lowered:
                return True
    return False


class CentralRelay:
    """Concurrent, rotating-token relay to one Pursers Central.

    Each request owns its upstream SDK context. The MCP SDK's transports use
    task-bound anyio cancel scopes, so retaining one across independent stdio
    request tasks is invalid. Independent contexts also ensure a blocking wait
    cannot serialize an unrelated call.
    """

    def __init__(
        self,
        *,
        central_url: str,
        board: str,
        token_file: Path,
        ca_file: Path | None = None,
        tools_mode: str = "default",
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self.central_url = central_mcp_url(central_url)
        self.board = board
        self.token_file = token_file
        self.ca_file = ca_file
        self.tools_mode = tools_mode
        self._connection_factory = connection_factory or self._http_connection
        self._known_tools: dict[str, types.Tool] = {}
        self._reported_error: str | None = None

    @asynccontextmanager
    async def _http_connection(self, token: str) -> AsyncIterator[UpstreamClient]:
        verify: str | bool = str(self.ca_file) if self.ca_file is not None else True
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx2.Timeout(10.0, read=None),
                    limits=httpx2.Limits(max_connections=16, max_keepalive_connections=16),
                    verify=verify,
                    trust_env=False,
                )
            )
            transport = streamable_http_client(self.central_url, http_client=http)
            client = await stack.enter_async_context(
                Client(transport, mode="2026-07-28", cache=None)
            )
            yield client

    @asynccontextmanager
    async def session(
        self, *, force_reconnect: bool = False
    ) -> AsyncIterator[UpstreamClient]:
        del force_reconnect
        token = _read_token(self.token_file)
        async with self._connection_factory(token) as client:
            yield client

    async def _retrying(self, operation: Callable[[UpstreamClient], Any]) -> Any:
        try:
            async with self.session() as client:
                result = await operation(client)
        except Exception as exc:
            if not _exception_has_status(exc, 401):
                raise
            async with self.session(force_reconnect=True) as client:
                return await operation(client)
        if isinstance(result, types.CallToolResult) and _result_is_unauthorized(result):
            async with self.session(force_reconnect=True) as client:
                return await operation(client)
        return result

    def _safe_failure(self, exc: BaseException) -> RelayFailure:
        if isinstance(exc, RelayFailure):
            return exc
        if _exception_has_status(exc, 401):
            return RelayFailure("Central rejected the credential after one token-file reload")
        return RelayFailure(f"Central is unreachable at {self.central_url}")

    def _report(self, failure: RelayFailure) -> None:
        message = str(failure)
        if message == self._reported_error:
            return
        self._reported_error = message
        print(f"pursers-mcp: {message}", file=sys.stderr, flush=True)

    def _diagnostic_tool(self, failure: RelayFailure) -> types.Tool:
        return types.Tool(
            name=DIAGNOSTIC_TOOL,
            description=(
                "Report why pursers-mcp cannot currently reach Central. "
                f"Current error: {failure}"
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        )

    async def list_tools(self) -> list[types.Tool]:
        try:
            result = await self._retrying(lambda client: client.list_tools())
        except Exception as exc:
            failure = self._safe_failure(exc)
            self._report(failure)
            self._known_tools = {}
            return [self._diagnostic_tool(failure)]
        self._reported_error = None
        tools = result.tools
        if self.tools_mode == "default":
            tools = [tool for tool in tools if tool.name in DEFAULT_TOOLS]
        self._known_tools = {tool.name: tool for tool in tools}
        return tools

    def _arguments_for(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._known_tools.get(name)
        schema = getattr(tool, "input_schema", None) if tool is not None else None
        if not isinstance(schema, dict):
            schema = getattr(tool, "inputSchema", None) if tool is not None else None
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if "board_id" in properties and "board_id" not in arguments:
            arguments = {"board_id": self.board, **arguments}
        if name in WAIT_TOOL_NAMES:
            for key in ("timeout_s", "wait_seconds", "timeout"):
                if key not in properties:
                    continue
                requested = arguments.get(key, MAX_WAIT_SECONDS)
                try:
                    requested = int(requested)
                except (TypeError, ValueError):
                    requested = MAX_WAIT_SECONDS
                arguments[key] = max(0, min(requested, MAX_WAIT_SECONDS))
                break
        return arguments

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        _context: Any = None,
    ) -> types.CallToolResult:
        if name == DIAGNOSTIC_TOOL:
            message = self._reported_error or "Central connection has not been checked"
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=message)], isError=True
            )
        payload = self._arguments_for(name, dict(arguments))
        try:
            return await self._retrying(
                lambda client: client.call_tool(name, payload)
            )
        except Exception as exc:
            failure = self._safe_failure(exc)
            self._report(failure)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(failure))], isError=True
            )

    async def aclose(self) -> None:
        return None


def _prompt_text(name: str, argument: str | None, board: str) -> str:
    if name == "board":
        return (
            f"Your work on `{board}` at a glance: its board ID and board-wide key "
            "counts, followed by a clearly labeled subset of up to 10 active tickets "
            "needing attention. Each row leads with its ticket ID and gives its specific "
            "reason for needing attention. This view stays on this board."
        )
    if name == "create":
        return (
            f"New work for `{board}`: {argument!r}. Any missing required detail comes "
            "first; creation happens once under Zed's single confirmation, followed by "
            "the new ticket ID and a brief recap."
        )
    if name == "watch":
        return (
            f"Live changes for `{board}`: work, reviews, and questions needing attention, "
            "limited to changed IDs and next steps. The watch resumes from its latest "
            "saved position and stays on this board."
        )
    if name == "evidence":
        return (
            f"Evidence for `{argument}` on `{board}`: the ticket ID first, then its exact "
            "branch and commit, changed files, literal test results, and independent-review "
            "state. The view contains no unrelated tickets."
        )
    return (
        f"Answer for {argument!r} on `{board}`: the exact ticket's pending question is "
        "resolved once, followed by its ticket ID and resulting state. This stays on this board."
    )


def build_server(relay: CentralRelay) -> MCPServer[Any]:
    server = MCPServer(
        "Pursers MCP Relay",
        version=package_version(),
        instructions=(
            "Credential-safe stdio bridge. Downstream clients may negotiate MCP 2025-11-25; "
            "Central is contacted independently with MCP 2026-07-28. "
            f"{PROMPT_BEHAVIOR_INSTRUCTIONS}"
        ),
    )
    server.list_tools = relay.list_tools  # type: ignore[method-assign]
    server.call_tool = relay.call_tool  # type: ignore[method-assign]

    @server.prompt(name="board", description="Compact board summary and Needs you list")
    def board_prompt() -> str:
        return _prompt_text("board", None, relay.board)

    @server.prompt(name="create", description="Create a ticket from one summary")
    def create_prompt(summary: str) -> str:
        return _prompt_text("create", summary, relay.board)

    @server.prompt(name="watch", description="Watch this board for actionable changes")
    def watch_prompt() -> str:
        return _prompt_text("watch", None, relay.board)

    @server.prompt(name="evidence", description="Show bounded evidence for one ticket")
    def evidence_prompt(ticket_id: str) -> str:
        return _prompt_text("evidence", ticket_id, relay.board)

    @server.prompt(name="answer", description="Answer one pending human question")
    def answer_prompt(ticket_and_answer: str) -> str:
        return _prompt_text("answer", ticket_and_answer, relay.board)

    return server


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="pursers-mcp")
    command.add_argument("--central-url", required=True)
    command.add_argument("--board", required=True)
    command.add_argument("--token-file", required=True, type=Path)
    command.add_argument("--ca-file", type=Path)
    command.add_argument("--tools", choices=("default", "all"), default="default")
    command.add_argument("--version", action="version", version=f"pursers-mcp {package_version()}")
    return command


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    args = parser().parse_args(argv)
    try:
        relay = CentralRelay(
            central_url=args.central_url,
            board=args.board,
            token_file=args.token_file,
            ca_file=args.ca_file,
            tools_mode=args.tools,
        )
    except RelayFailure as exc:
        print(f"pursers-mcp: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc
    build_server(relay).run(transport="stdio")


if __name__ == "__main__":
    main()
