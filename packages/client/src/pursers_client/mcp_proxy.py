"""Credential-safe stdio MCP relay for Zed and other local MCP hosts."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import signal
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
from mcp.server import NotificationOptions
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

LOG = logging.getLogger("pursers-mcp")
SETUP_STATUS_TOOL = "pursers_setup_status"
SETUP_TOOL = "pursers_setup"
CENTRAL_PACKAGE_SPEC = "pursers-central==0.1.1"
DEFAULT_INSTANCE_DIR = Path.home() / ".pursers" / "central"
SETUP_AGENT_NAME = "zed-local-owner"
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


class SetupConsent(BaseModel):
    provision: bool = Field(
        description=(
            "Create a private local Pursers instance and start Central in the background."
        )
    )


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


def _result_error_text(result: types.CallToolResult) -> str | None:
    if not result.is_error:
        return None
    for item in result.content:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip().splitlines()[0]
    return "Central rejected the board request"


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
        token_file: Path | None = None,
        ca_file: Path | None = None,
        tools_mode: str = "default",
        connection_factory: ConnectionFactory | None = None,
        setup_root: Path | None = None,
        uvx_path: str | None = None,
    ) -> None:
        self.central_url = central_mcp_url(central_url)
        self.board = board
        self.setup_root = (setup_root or DEFAULT_INSTANCE_DIR).expanduser()
        if not self.setup_root.is_absolute():
            raise RelayFailure(
                f"setup_root must be an absolute path, got {self.setup_root}"
            )
        self.token_file = (token_file or self.setup_root / "worker.jwt").expanduser()
        self._token_file_was_overridden = token_file is not None
        self.ca_file = ca_file
        self.tools_mode = tools_mode
        self._connection_factory = connection_factory or self._http_connection
        self._uvx_path = uvx_path
        self._known_tools: dict[str, types.Tool] = {}
        self._local_identity_tools: set[str] = set()
        self._identity_selection_tools: set[str] = set()
        self._resolved_agent_name: str | None = None
        self._principal_agent_names: tuple[str, ...] = ()
        self._authenticated_principal_id: str | None = None
        self._identity_discovery_succeeded = False
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

    def _setup_tools(self, failure: RelayFailure) -> list[types.Tool]:
        empty_schema = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        return [
            types.Tool(
                name=SETUP_STATUS_TOOL,
                description=(
                    "Check what this local Pursers connection still needs. "
                    f"Current connection error: {failure}"
                ),
                inputSchema=empty_schema,
            ),
            types.Tool(
                name=SETUP_TOOL,
                description=(
                    "With the user's explicit confirmation, create and start a private "
                    "local Pursers Central or onboard this local caller to its board."
                ),
                inputSchema=empty_schema,
            ),
        ]

    @staticmethod
    def _result_payload(result: types.CallToolResult) -> dict[str, Any] | None:
        payload = result.structured_content
        if isinstance(payload, dict):
            return payload
        for item in result.content:
            text = getattr(item, "text", None)
            if not isinstance(text, str):
                continue
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, dict):
                return decoded
        return None

    def _credential_principal_id(self) -> str | None:
        # board_list has already made Central authenticate this token. Decoding
        # its public claims here only reproduces Central's stable display ID; it
        # never grants authority or substitutes for server verification.
        try:
            token = _read_token(self.token_file)
            encoded = token.split(".")[1]
            padded = encoded + "=" * (-len(encoded) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded))
        except (
            IndexError,
            OSError,
            RelayFailure,
            UnicodeError,
            ValueError,
        ):
            return None
        if not isinstance(claims, dict):
            return None
        client_id = claims.get("client_id") or "-"
        issuer = claims.get("iss") or "-"
        subject = claims.get("sub") or "-"
        if not all(isinstance(item, str) and item for item in (client_id, issuer, subject)):
            return None
        canonical = json.dumps([client_id, issuer, subject], separators=(",", ":"))
        return "PR-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def _discover_existing_identity(self) -> None:
        self._resolved_agent_name = None
        self._principal_agent_names = ()
        self._authenticated_principal_id = None
        self._identity_discovery_succeeded = False
        try:
            boards_result = await self._retrying(
                lambda client: client.call_tool("board_list", {})
            )
        except Exception:
            return
        if boards_result.is_error:
            return
        payload = self._result_payload(boards_result)
        boards = payload.get("boards") if payload is not None else None
        if not isinstance(boards, list):
            return
        board = next(
            (
                item
                for item in boards
                if isinstance(item, dict) and item.get("board_id") == self.board
            ),
            None,
        )
        if board is None:
            return
        self._identity_discovery_succeeded = True
        self._authenticated_principal_id = self._credential_principal_id()
        agent_ids = board.get("agent_ids")
        if not isinstance(agent_ids, list) or not agent_ids:
            return
        known_ids = {item for item in agent_ids if isinstance(item, str)}
        try:
            status_result = await self._retrying(
                lambda client: client.call_tool(
                    "board_status",
                    {"board_id": self.board, "include_retired": True},
                )
            )
        except Exception:
            return
        if status_result.is_error:
            return
        status = self._result_payload(status_result)
        agents = status.get("agents") if status is not None else None
        if not isinstance(agents, list):
            return
        matching_agents = [
            agent
            for agent in agents
            if isinstance(agent, dict) and agent.get("agent_id") in known_ids
        ]
        principal_ids = {
            agent.get("principal_id")
            for agent in matching_agents
            if isinstance(agent.get("principal_id"), str)
        }
        if len(principal_ids) != 1:
            return
        self._authenticated_principal_id = principal_ids.pop()
        self._principal_agent_names = tuple(
            dict.fromkeys(
                agent["agent_name"]
                for agent in matching_agents
                if agent.get("principal_id") == self._authenticated_principal_id
                and agent.get("lifecycle_status", "active") == "active"
                and isinstance(agent.get("agent_name"), str)
                and agent["agent_name"]
            )
        )
        if len(self._principal_agent_names) == 1:
            self._resolved_agent_name = self._principal_agent_names[0]

    def _identity_selection_failure(self, supplied: str | None = None) -> RelayFailure:
        principal = self._authenticated_principal_id or "an unreported principal"
        if self._principal_agent_names:
            choices = ", ".join(self._principal_agent_names)
            if supplied is not None:
                return RelayFailure(
                    f"Central authenticated principal {principal} on board {self.board}, "
                    f"but agent_name {supplied} is not one of its active agent names: "
                    f"{choices}. "
                    "Retry with agent_name set to one of those exact names."
                )
            return RelayFailure(
                f"Central authenticated principal {principal} on board {self.board} "
                f"with multiple active agent names: {choices}. Retry with agent_name "
                "set to one of those exact names."
            )
        return RelayFailure(
            f"Central authenticated principal {principal} on board {self.board}, but it "
            "holds no active agent names there. Ask a board administrator to onboard "
            "or reactivate this principal, then retry with that exact agent_name."
        )

    async def _upstream_tools(self) -> list[types.Tool]:
        result = await self._retrying(lambda client: client.list_tools())
        tools = result.tools
        if self.tools_mode == "default":
            tools = [tool for tool in tools if tool.name in DEFAULT_TOOLS]
        self._local_identity_tools = set()
        self._identity_selection_tools = set()
        if self._token_file_was_overridden:
            await self._discover_existing_identity()
        else:
            self._resolved_agent_name = SETUP_AGENT_NAME
            self._principal_agent_names = (SETUP_AGENT_NAME,)
            self._authenticated_principal_id = None
            self._identity_discovery_succeeded = True
        exposed_tools: list[types.Tool] = []
        for tool in tools:
            schema = tool.input_schema
            properties = schema.get("properties", {})
            if "agent_name" not in properties:
                exposed_tools.append(tool)
                continue
            exposed = tool.model_copy(deep=True)
            required = exposed.input_schema.get("required")
            if self._resolved_agent_name is not None:
                self._local_identity_tools.add(tool.name)
                exposed.input_schema["properties"].pop("agent_name")
                if isinstance(required, list):
                    exposed.input_schema["required"] = [
                        name for name in required if name != "agent_name"
                    ]
            elif self._identity_discovery_succeeded:
                self._identity_selection_tools.add(tool.name)
                if isinstance(required, list):
                    exposed.input_schema["required"] = [
                        name for name in required if name != "agent_name"
                    ]
                agent_schema = exposed.input_schema["properties"].get("agent_name")
                if isinstance(agent_schema, dict):
                    if self._principal_agent_names:
                        choices = ", ".join(self._principal_agent_names)
                        agent_schema["description"] = (
                            "Required when this credential holds several active identities. "
                            f"Choose one exact agent name for board {self.board}: {choices}."
                        )
                    else:
                        agent_schema["description"] = (
                            "This credential holds no active agent name on the configured "
                            "board. Ask a board administrator to onboard or reactivate its "
                            "principal first."
                        )
            exposed_tools.append(exposed)
        tools = exposed_tools
        self._known_tools = {tool.name: tool for tool in tools}
        return tools

    async def _board_access(self) -> tuple[bool, str | None]:
        try:
            result = await self._retrying(
                lambda client: client.call_tool("board_status", {"board_id": self.board})
            )
        except Exception as exc:
            return False, str(self._safe_failure(exc))
        return not result.is_error, _result_error_text(result)

    def _membership_instructions(self, detail: str | None) -> RelayFailure:
        suffix = f" Central reported: {detail}." if detail else ""
        return RelayFailure(
            f"Central is reachable, but this credential cannot read board {self.board}."
            f"{suffix} Ask an administrator of board {self.board} to run "
            "board_invite_create for this principal; then redeem that invite with "
            f"board_join using agent_name {SETUP_AGENT_NAME} and role worker."
        )

    async def list_tools(self) -> list[types.Tool]:
        try:
            tools = await self._upstream_tools()
        except Exception as exc:
            failure = self._safe_failure(exc)
            self._report(failure)
            self._known_tools = {}
            self._local_identity_tools = set()
            return self._setup_tools(failure)
        board_available, board_error = await self._board_access()
        if not board_available:
            failure = self._membership_instructions(board_error)
            self._report(failure)
            self._known_tools = {}
            self._local_identity_tools = set()
            return self._setup_tools(failure)
        self._reported_error = None
        return tools

    def _resolved_uvx(self) -> str | None:
        command = self._uvx_path or "uvx"
        return shutil.which(command)

    def _local_endpoint(self) -> tuple[str, int]:
        parsed = urlsplit(self.central_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "http" or host not in {"127.0.0.1", "localhost", "::1"}:
            raise RelayFailure(
                "automatic setup is available only for a local http:// Central endpoint"
            )
        return host, parsed.port or 80

    async def _port_is_open(self, host: str, port: int) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=0.4
            )
        except (OSError, TimeoutError):
            return False
        del reader
        writer.close()
        await writer.wait_closed()
        return True

    async def _setup_status(self) -> dict[str, Any]:
        uvx = self._resolved_uvx()
        token_readable = False
        try:
            _read_token(self.token_file)
            token_readable = True
        except RelayFailure:
            pass
        try:
            tools = await self._upstream_tools()
        except Exception as exc:
            central_reachable = False
            connection_error = str(self._safe_failure(exc))
            tool_count = 0
        else:
            central_reachable = True
            connection_error = None
            tool_count = len(tools)
        board_available = False
        board_checked = False
        board_error: str | None = None
        if central_reachable:
            board_checked = True
            board_available, board_error = await self._board_access()
        return {
            "ok": True,
            "setup_root": str(self.setup_root),
            "uvx": {"available": uvx is not None, "path": uvx},
            "central": {
                "reachable": central_reachable,
                "url": self.central_url,
                "error": connection_error,
            },
            "board": {
                "id": self.board,
                "available": board_available,
                "checked": board_checked,
                "error": board_error,
            },
            "token_file": {
                "path": str(self.token_file),
                "readable": token_readable,
                "overridden": self._token_file_was_overridden,
            },
            "real_tool_count": tool_count,
        }

    def _json_result(
        self, payload: dict[str, Any], *, is_error: bool = False
    ) -> types.CallToolResult:
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text", text=json.dumps(payload, indent=2, sort_keys=True)
                )
            ],
            structuredContent=payload,
            isError=is_error,
        )

    async def _run_init(self, uvx: str, host_port: int) -> None:
        process = await asyncio.create_subprocess_exec(
            uvx,
            "--from",
            CENTRAL_PACKAGE_SPEC,
            "pursers-central",
            "init",
            str(self.setup_root),
            "--port",
            str(host_port),
            "--board",
            self.board,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", "replace").strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise RelayFailure(f"pursers-central init failed{suffix}")

    def _has_reusable_instance(self, port: int) -> bool:
        profile = self.setup_root / "profile.env"
        credentials = [self.setup_root / "admin.jwt", self.setup_root / "worker.jwt"]
        managed = [profile, *credentials]
        existing = [path for path in managed if path.exists()]
        if not existing:
            if not self.setup_root.exists():
                return False
            deployed_root = self.setup_root / ".private-arm" / "central-data"
            if deployed_root.exists():
                raise RelayFailure(
                    f"the setup directory {self.setup_root} contains a deployed Pursers "
                    f"instance at {deployed_root}; automatic setup will not write beside "
                    "it. Set setup_root to an empty directory"
                )
            try:
                occupied = any(self.setup_root.iterdir())
            except OSError as exc:
                raise RelayFailure(
                    f"the setup directory {self.setup_root} is not readable; set "
                    "setup_root to an empty writable directory"
                ) from exc
            if occupied:
                raise RelayFailure(
                    f"the setup directory {self.setup_root} is already occupied by "
                    "something other than a complete Pursers instance; set setup_root "
                    "to an empty directory"
                )
            return False
        if len(existing) != len(managed):
            raise RelayFailure(
                f"the setup directory {self.setup_root} contains an incomplete Pursers "
                "instance; move it aside or set setup_root to an empty directory"
            )
        try:
            values = dict(
                line.split("=", 1)
                for line in profile.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#") and "=" in line
            )
        except (OSError, UnicodeError) as exc:
            raise RelayFailure("the local Central profile is unreadable") from exc
        if values.get("ONBOARD_CENTRAL_PORT") != str(port):
            raise RelayFailure(
                "the existing local Central uses a different port; use its configured URL"
            )
        if values.get("PURSERS_BOARD_ID") != self.board:
            raise RelayFailure(
                "the existing local Central uses a different board; use its configured board_id"
            )
        for credential in credentials:
            _read_token(credential)
        return True

    async def _start_central(self, uvx: str) -> tuple[asyncio.subprocess.Process, Path]:
        self.setup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_path = self.setup_root / "central.log"
        log_handle = log_path.open("ab", buffering=0)
        try:
            process = await asyncio.create_subprocess_exec(
                uvx,
                "--from",
                CENTRAL_PACKAGE_SPEC,
                "pursers-central",
                "run",
                str(self.setup_root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_handle,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        return process, log_path

    async def _stop_central(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    async def _wait_for_central(
        self, token_file: Path, process: asyncio.subprocess.Process
    ) -> str:
        token = _read_token(token_file)
        deadline = asyncio.get_running_loop().time() + 30
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                raise RelayFailure(
                    f"Central exited before becoming ready (status {process.returncode})"
                )
            try:
                async with self._http_connection(token) as client:
                    await client.list_tools()
                return token
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.2)
        raise RelayFailure("Central did not become ready within 30 seconds") from last_error

    async def _create_board(self, admin_token: str) -> None:
        async with self._http_connection(admin_token) as client:
            result = await client.call_tool(
                "board_onboard",
                {
                    "board_id": self.board,
                    "agent_name": SETUP_AGENT_NAME,
                    "role": "worker",
                    "allow_takeover": True,
                    "capabilities": {
                        "can_work": True,
                        "can_review": False,
                        "tier_max": 2,
                        "max_parallel": 1,
                    },
                },
            )
        if result.is_error:
            detail = "board_onboard returned an error"
            for item in result.content:
                if isinstance(item, types.TextContent) and item.text:
                    detail = item.text.splitlines()[0]
                    break
            raise RelayFailure(f"could not create local board: {detail}")

    async def _onboard_reachable_central(self) -> dict[str, Any]:
        self._local_endpoint()
        admin_file = self.setup_root / "admin.jwt"
        try:
            admin_token = _read_token(admin_file)
        except RelayFailure as exc:
            raise self._membership_instructions(
                f"the local admin credential is unavailable at {admin_file}"
            ) from exc
        try:
            await self._create_board(admin_token)
        except RelayFailure as exc:
            raise self._membership_instructions(str(exc)) from exc
        tools = await self._upstream_tools()
        board_available, board_error = await self._board_access()
        if not board_available:
            raise self._membership_instructions(board_error)
        self._reported_error = None
        log_path = self.setup_root / "central.log"
        return {
            "ok": True,
            "central_url": self.central_url,
            "central_started": False,
            "board_id": self.board,
            "board_onboarded": True,
            "token_file": str(self.token_file),
            "log_file": str(log_path) if log_path.exists() else None,
            "pid": None,
            "real_tools": [tool.name for tool in tools],
        }

    async def _provision(self) -> dict[str, Any]:
        try:
            await self._upstream_tools()
        except Exception:
            pass
        else:
            return await self._onboard_reachable_central()
        if self._token_file_was_overridden and self.token_file != self.setup_root / "worker.jwt":
            raise RelayFailure(
                "automatic setup cannot replace an explicit token_file override; "
                "remove the override or point it at an existing Central"
            )
        uvx = self._resolved_uvx()
        if uvx is None:
            raise RelayFailure("uvx is unavailable; restart Zed after its uv installation finishes")
        host, port = self._local_endpoint()
        if await self._port_is_open(host, port):
            raise RelayFailure(
                f"port {port} is already in use; no second Central was started"
            )
        if not self._has_reusable_instance(port):
            await self._run_init(uvx, port)
        process, log_path = await self._start_central(uvx)
        try:
            admin_token = await self._wait_for_central(
                self.setup_root / "admin.jwt", process
            )
            await self._create_board(admin_token)
            tools = await self._upstream_tools()
        except BaseException:
            await self._stop_central(process)
            raise
        self._reported_error = None
        return {
            "ok": True,
            "central_url": self.central_url,
            "board_id": self.board,
            "token_file": str(self.token_file),
            "log_file": str(log_path),
            "pid": process.pid,
            "real_tools": [tool.name for tool in tools],
        }

    async def _perform_setup(self, context: Any) -> types.CallToolResult:
        if context is None:
            return self._json_result(
                {"ok": False, "error": "setup requires an interactive MCP session"},
                is_error=True,
            )
        status = await self._setup_status()
        if status["central"]["reachable"]:
            consent_message = (
                f"Use the local Central administrator credential in {self.setup_root} "
                f"to create or join board {self.board} and onboard this caller?"
            )
        else:
            consent_message = (
                f"Create a private Pursers Central in {self.setup_root}, start it in "
                f"the background, and create board {self.board}?"
            )
        try:
            consent = await context.elicit(
                consent_message,
                SetupConsent,
            )
        except Exception as exc:
            return self._json_result(
                {"ok": False, "error": f"could not request setup consent: {exc}"},
                is_error=True,
            )
        if consent.action != "accept" or not consent.data.provision:
            return self._json_result(
                {"ok": False, "provisioned": False, "reason": consent.action}
            )
        try:
            payload = await self._provision()
        except Exception as exc:
            failure = self._safe_failure(exc)
            self._report(failure)
            return self._json_result(
                {"ok": False, "provisioned": False, "error": str(failure)},
                is_error=True,
            )
        await context.session.send_tool_list_changed()
        payload["tools_list_changed"] = True
        return self._json_result(payload)

    def _arguments_for(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._known_tools.get(name)
        schema = getattr(tool, "input_schema", None) if tool is not None else None
        if not isinstance(schema, dict):
            schema = getattr(tool, "inputSchema", None) if tool is not None else None
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if "board_id" in properties and "board_id" not in arguments:
            arguments = {"board_id": self.board, **arguments}
        if name in self._local_identity_tools and "agent_name" not in arguments:
            assert self._resolved_agent_name is not None
            arguments = {"agent_name": self._resolved_agent_name, **arguments}
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
        if name == SETUP_STATUS_TOOL:
            return self._json_result(await self._setup_status())
        if name == SETUP_TOOL:
            return await self._perform_setup(_context)
        if name in self._identity_selection_tools:
            supplied = arguments.get("agent_name")
            if not isinstance(supplied, str) or not supplied:
                failure = self._identity_selection_failure()
                return self._json_result(
                    {"ok": False, "error": str(failure)}, is_error=True
                )
            if supplied not in self._principal_agent_names:
                failure = self._identity_selection_failure(supplied)
                return self._json_result(
                    {"ok": False, "error": str(failure)}, is_error=True
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
    if name == "setup":
        return (
            f"Set up Pursers for `{board}` in this chat. Check the local setup status, "
            "explain what is missing, then run the setup tool. The setup tool asks me "
            "for confirmation before it creates files or starts Central. After setup, "
            "continue in this same chat with the newly available board tools."
        )
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

    # MCPServer 2.2 has no public constructor setting for legacy list-changed
    # capabilities. Zed negotiates the 2025-11-25 wire, so advertise the exact
    # notification this relay emits when setup replaces its bootstrap tools.
    lowlevel = server._lowlevel_server  # type: ignore[attr-defined]
    create_options = lowlevel.create_initialization_options

    def create_initialization_options(
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        extensions: dict[str, dict[str, Any]] | None = None,
    ) -> Any:
        options = notification_options or NotificationOptions()
        options.tools_changed = True
        return create_options(options, experimental_capabilities, extensions)

    lowlevel.create_initialization_options = create_initialization_options

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

    @server.prompt(name="setup", description="Set up a local Pursers board in this chat")
    def setup_prompt() -> str:
        return _prompt_text("setup", None, relay.board)

    return server


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="pursers-mcp")
    command.add_argument("--central-url", required=True)
    command.add_argument("--board", required=True)
    command.add_argument("--token-file", type=Path)
    command.add_argument("--ca-file", type=Path)
    command.add_argument("--setup-root", type=Path)
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
            setup_root=args.setup_root,
        )
    except RelayFailure as exc:
        print(f"pursers-mcp: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc
    build_server(relay).run(transport="stdio")


if __name__ == "__main__":
    main()
