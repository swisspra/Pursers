"""Profile-backed MCP Apps and personal chat facade for On Board.

The module has no import-time server and never reads legacy ``ONBOARD_*``
connection variables. A launcher must resolve a verified personal profile and
pass its resulting context to :func:`build_profile_apps_server`.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import importlib
import importlib.resources
import ipaddress
import json
import re
import sys
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol
from urllib.parse import urlsplit

from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import MCPServer

from . import PRODUCT_VERSION
from .artifacts import import_verified_component, verify_component_artifacts

PINNED_CLIENT_VERSION = "0.1.0a16"
MAX_EVENTS = 200
MAX_TICKETS = 500
AGENT_STALE_AFTER_MINUTES = 60
FLEET_SCHEMA_VERSION = 1
FLEET_MAX_PROJECTS = 25
FLEET_SNAPSHOT_LIMIT = 500
FLEET_SNAPSHOT_MAX_BYTES = 250_000
FLEET_RESPONSE_MAX_BYTES = 250_000
FLEET_CLAIM_STATES = frozenset({"claimed", "in_progress", "creating_report"})
DASHBOARD_ACTIVE_TICKET_STATES = frozenset({"open", *FLEET_CLAIM_STATES})
LINK_SCHEMA_VERSION = 1
LINK_MEMORY_LIMIT = 200
LINK_EDGE_LIMIT = 1_000
LINK_VALUE_MAX_LENGTH = 1_000
LINK_RESPONSE_MAX_BYTES = 250_000
UI_URI = "ui://pursers/dashboard"
MODEL_AND_APP = ["model", "app"]
MODEL_ONLY = ["model"]
APP_ONLY = ["app"]
PRIMARY_UI_TOOL_NAMES = frozenset(
    {"board_snapshot", "board_event_feed", "fleet_snapshot", "link_snapshot"}
)
CHAT_TOOL_NAMES = frozenset(
    {
        "board_onboard",
        "board_status",
        "board_catchup",
        "ticket_get",
        "ticket_list",
        "ticket_create",
        "ticket_claim",
        "ticket_submit",
        "ticket_review",
        "lease_renew",
        "ticket_cancel",
        "ticket_terminate",
        "memory_write",
        "memory_read",
        "memory_search",
        "memory_links",
        "memory_checkpoint",
        "memory_handoff",
        "memory_unpin",
        "board_state_get",
        "board_state_update",
    }
)

_WRAPPED_TOOL_ERROR_RE = re.compile(
    r"^\[TextContent\(type='text', text=(?P<text>'.*'), "
    r"annotations=None, meta=None\)\]$",
    re.DOTALL,
)
_CENTRAL_TOOL_ERROR_RE = re.compile(
    r"^Error executing tool [a-z][a-z0-9_]*: (?P<detail>.+)$",
    re.DOTALL,
)
_SAFE_CENTRAL_VALIDATION_DETAILS = (
    re.compile(
        r"^generated-ID tickets require: "
        r"(?:description|target_url|scope|required_fields)"
        r"(?:, (?:description|target_url|scope|required_fields))*$"
    ),
    re.compile(r"^(?:summary|review_notes) is required for generated-ID tickets$"),
    re.compile(r"^priority must be low, medium, high, or critical$"),
    re.compile(r"^scope must be READ-ONLY, interactive-no-send, or interactive$"),
    re.compile(r"^assigned_to and unassigned=true are mutually exclusive$"),
    re.compile(r"^board:intake ticket creation requires coordinator_op_key$"),
    re.compile(r"^verdict must be approve or reject$"),
    re.compile(r"^unsupported ticket status$"),
    re.compile(r"^scope must be private or project$"),
    re.compile(r"^unsupported memory_type$"),
    re.compile(r"^priority must be between 0 and 3$"),
    re.compile(r"^archived must be a boolean$"),
    re.compile(r"^archive provenance requires archived=true$"),
    re.compile(r"^since must be an ISO-8601 timestamp$"),
    re.compile(r"^next_steps must contain at least one item$"),
    re.compile(r"^expected_sha256 must be a lowercase SHA-256 digest$"),
    re.compile(r"^role must be admin, member, or reviewer$"),
    re.compile(r"^token_budget must be between 256 and 50000$"),
    re.compile(r"^scrub_profile must be strict or internal$"),
    re.compile(r"^review_policy must be strict or workflow$"),
    re.compile(r"^invite role must be member or reviewer$"),
    re.compile(r"^expected_status must be open$"),
    re.compile(r"^expires_at must be an ISO-8601 timestamp$"),
    re.compile(r"^expires_at must include a timezone$"),
    re.compile(r"^expires_at must be within the next hour$"),
    re.compile(r"^limit must be between 1 and (?:100|200|500|1000)$"),
    re.compile(r"^since_minutes must be positive$"),
    re.compile(r"^depth must be between 0 and 10$"),
    re.compile(
        r"^expected generation SHA-256 must be 64 lowercase hex characters$"
    ),
    re.compile(
        r"^expected_generation argument conflicts with generation metadata$"
    ),
    re.compile(r"^cursor must be a non-negative integer$"),
    re.compile(r"^max_bytes is too small for snapshot metadata$"),
    re.compile(r"^max_bytes is too small for catchup metadata$"),
    re.compile(r"^max_bytes is too small for one journal event$"),
    re.compile(r"^ticket (?:not found|already exists)$"),
    re.compile(
        r"^ticket is (?:open|claimed|in_progress|creating_report|submitted|"
        r"reviewing|in_review|closed|rejected|canceled|terminated)$"
    ),
)
_SENSITIVE_ERROR_DETAIL_RE = re.compile(
    r"\b(?:auth(?:entication|orization)?|unauthorized|forbidden|permission|"
    r"principal|capability|credential|secret|bearer|password|token|connection|"
    r"transport|host)\b",
    re.IGNORECASE,
)
_HOSTNAME_ERROR_DETAIL_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}"
    r"(?::[0-9]{1,5})?(?![A-Za-z0-9_-])"
)


def _load_board_client() -> tuple[type[Any], type[BaseException]]:
    """Verify the installed a10 package and import it through the safe loader."""
    verified = verify_component_artifacts({"pursers-client"})["pursers-client"]
    if verified["version"] != PINNED_CLIENT_VERSION:
        raise RuntimeError("unsupported pursers-client version")
    client_module = import_verified_component(
        "pursers-client",
        "pursers_client",
        "pursers_client.client",
        package_member="pursers_client/__init__.py",
        module_member="pursers_client/client.py",
    )
    return client_module.BoardClient, client_module.BoardClientError


def load_dashboard_html() -> str:
    """Verify the complete component lock, then load the packaged View."""
    verify_component_artifacts()
    return (
        importlib.resources.files("pursers_personal")
        .joinpath("resources", "dashboard.html")
        .read_text(encoding="utf-8")
    )


class PersonalAppsContext(Protocol):
    """Secret-bearing resolved context supplied by the personal profile layer."""

    central_url: str
    capability_token: str
    board_id: str
    agent_name: str


PostJoinHook = Callable[[Any], Awaitable[Any]]

FALLBACK_TICKETS = [
    {
        "id": "TK-DEMO-1",
        "project": None,
        "title": "Shape the Personal Preview dashboard",
        "description": "Synthetic example — no project data is loaded.",
        "status": "claimed",
        "priority": "high",
        "assigned_to": "agent-alpha",
        "lease_expires_at": None,
        "rejected": False,
        "abandoned_count": 0,
    },
    {
        "id": "TK-DEMO-2",
        "project": None,
        "title": "Review special characters: <safe> & readable",
        "description": "Synthetic content stays inert in the View.",
        "status": "submitted",
        "priority": "medium",
        "assigned_to": "reviewer-beta",
        "lease_expires_at": None,
        "rejected": False,
        "abandoned_count": 0,
    },
]
FALLBACK_AGENTS = [
    {
        "id": "AI-DEMO-1",
        "project": None,
        "current_ticket_id": None,
        "duplicate_name": False,
        "name": "agent-alpha",
        "status": "working",
        "role": "builder",
        "idle_minutes": 1,
        "focus": "Personal UI shell",
        "platform": "synthetic",
        "last_activity_at": "2099-01-01T00:00:00Z",
        "lease_expires_at": None,
        "stale": False,
    },
    {
        "id": "AI-DEMO-2",
        "project": None,
        "current_ticket_id": None,
        "duplicate_name": False,
        "name": "reviewer-beta",
        "status": "idle",
        "role": "reviewer",
        "idle_minutes": 18,
        "focus": "Accessibility review",
        "platform": "synthetic",
        "last_activity_at": "2099-01-01T00:00:00Z",
        "lease_expires_at": None,
        "stale": False,
    },
]
FALLBACK_EVENTS = [
    {
        "id": "EV-DEMO-1",
        "seq": 1,
        "kind": "ticket_status_changed",
        "text": "TK-DEMO-1: open -> claimed",
        "actor_id": "AI-DEMO-1",
        "occurred_at": "2099-01-01T00:01:00Z",
        "ticket_id": "TK-DEMO-1",
        "status_from": "open",
        "status_to": "claimed",
    },
    {
        "id": "EV-DEMO-2",
        "seq": 2,
        "kind": "ticket_status_changed",
        "text": "TK-DEMO-2: claimed -> submitted",
        "actor_id": "AI-DEMO-1",
        "occurred_at": "2099-01-01T00:02:00Z",
        "ticket_id": "TK-DEMO-2",
        "status_from": "claimed",
        "status_to": "submitted",
    },
]
FALLBACK_HIGHLIGHTS = {
    "latest_handoff": {
        "id": "MEM-DEMO-HANDOFF",
        "type": "handoff",
        "title": "UI shell ready for review",
        "summary": "Synthetic handoff with the next checks for the Personal Preview.",
        "author": "agent-alpha",
        "created_at": "2099-01-01T00:03:00Z",
        "next_steps": ["Check narrow layout", "Verify keyboard navigation"],
        "warnings": [],
    },
    "important_pinned": {
        "id": "MEM-DEMO-WARNING",
        "type": "warning",
        "title": "Host proof is still pending",
        "summary": "Synthetic reminder: SDK evidence is not real-host verification.",
        "author": "reviewer-beta",
        "created_at": "2099-01-01T00:04:00Z",
        "next_steps": [],
        "warnings": ["Keep the preview label visible."],
    },
}


def _is_loopback_url(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        parsed.port
    except ValueError:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class DashboardConfig:
    central_url: str
    token: str = field(repr=False)
    board_id: str
    agent_name: str
    reconnect_min_s: float = 0.25
    reconnect_max_s: float = 5.0
    request_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if not _is_loopback_url(self.central_url):
            raise ValueError("central_url must use loopback HTTP(S) without userinfo")
        if not self.token:
            raise ValueError("token must not be empty")
        if not self.board_id or not self.agent_name:
            raise ValueError("board_id and agent_name must not be empty")
        if not 0 < self.reconnect_min_s <= self.reconnect_max_s:
            raise ValueError("reconnect delays must be positive and ordered")
        if self.request_timeout_s <= 0:
            raise ValueError("request_timeout_s must be positive")


def config_from_personal_context(context: PersonalAppsContext) -> DashboardConfig:
    """Create Apps configuration only from a resolved Personal profile context."""
    return DashboardConfig(
        central_url=context.central_url,
        token=context.capability_token,
        board_id=context.board_id,
        agent_name=context.agent_name,
    )


class RawBoardReader:
    """Non-joining MCP session restricted to Central's pure read tools.

    The transport and decoder come from the already verified pursers-client
    module, but its ``BoardClient.__aenter__`` is deliberately never invoked.
    Direct model-style ``board_catchup`` remains unavailable here. The App's
    journal stream instead delegates to public ``BoardClient.events()`` with
    ``acknowledge=False`` and ``touch=False``.
    """

    def __init__(
        self,
        config: DashboardConfig,
        board_client_class: type[Any],
    ) -> None:
        client_module = sys.modules.get(board_client_class.__module__)
        if client_module is None:
            raise RuntimeError("verified pursers-client module is unavailable")
        try:
            self._httpx2 = client_module.httpx2
            self._mcp_client_class = client_module.Client
            self._streamable_http_client = client_module.streamable_http_client
            self._decode = board_client_class._decode
        except AttributeError as exc:
            raise RuntimeError("verified pursers-client read primitives are unavailable") from exc
        self.config = config
        self.agent_name = config.agent_name
        self._board_client_class = board_client_class
        self._stack: AsyncExitStack | None = None
        self._client: Any | None = None

    async def __aenter__(self) -> "RawBoardReader":
        self._stack = AsyncExitStack()
        try:
            http = await self._stack.enter_async_context(
                self._httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {self.config.token}"},
                    timeout=self._httpx2.Timeout(10.0, read=None),
                    trust_env=False,
                )
            )
            transport = self._streamable_http_client(
                self.config.central_url, http_client=http
            )
            self._client = await self._stack.enter_async_context(
                self._mcp_client_class(transport, mode="2026-07-28", cache=None)
            )
        except BaseException:
            try:
                async with asyncio.timeout(min(1.0, self.config.request_timeout_s)):
                    await self._stack.aclose()
            except BaseException:
                pass
            self._stack = None
            self._client = None
            raise
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        try:
            if self._stack is not None:
                async with asyncio.timeout(
                    min(1.0, self.config.request_timeout_s)
                ):
                    await self._stack.aclose()
        finally:
            self._stack = None
            self._client = None

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("raw board reader is not entered")
        if name not in {
            "board_snapshot",
            "board_status",
            "ticket_get",
            "ticket_list",
            "board_state_get",
            "memory_links",
        }:
            raise RuntimeError("raw board reader rejected a non-pure tool")
        result = await self._client.call_tool(
            name, {"board_id": self.config.board_id, **arguments}
        )
        return self._decode(result)

    async def board_snapshot(
        self,
        *,
        limit: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        if limit is not None:
            arguments["limit"] = limit
        if max_bytes is not None:
            arguments["max_bytes"] = max_bytes
        return await self._call("board_snapshot", arguments)

    async def board_status(self) -> dict[str, Any]:
        return await self._call("board_status", {})

    async def ticket_list(
        self,
        *,
        status: str | None = None,
        assigned_to: str | None = None,
        include_closed: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "include_closed": include_closed,
            "limit": limit,
        }
        if status is not None:
            arguments["status"] = status
        if assigned_to is not None:
            arguments["assigned_to"] = assigned_to
        return await self._call("ticket_list", arguments)

    async def ticket_get(self, ticket_id: str) -> dict[str, Any]:
        return await self._call("ticket_get", {"ticket_id": ticket_id})

    async def events(
        self,
        from_cursor: int,
        cursor_callback: Callable[[int], None],
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream journal cues through the verified public client API."""
        client = self._board_client_class(
            self.config.central_url,
            self.config.token,
            self.config.board_id,
            agent_name=self.config.agent_name,
        )
        # Journal-only reads need a non-null identity for client filtering but
        # deliberately do not join or create a dashboard board member.
        client.identity = SimpleNamespace(agent_id="dashboard-read-only")
        async for event in client.events(
            from_cursor=from_cursor,
            only_mine=False,
            resource_subscriptions=[
                f"board://{self.config.board_id}/journal"
            ],
            acknowledge=False,
            touch=False,
            cursor_callback=cursor_callback,
        ):
            yield event

    async def board_state_get(self, *, key: str | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        if key is not None:
            arguments["key"] = key
        return await self._call("board_state_get", arguments)

    async def memory_links(
        self, *, depth: int = 1, limit: int = LINK_MEMORY_LIMIT
    ) -> dict[str, Any]:
        return await self._call("memory_links", {"depth": depth, "limit": limit})


ReadClientFactory = Callable[[DashboardConfig], Any]


class LiveDashboard:
    """Separate non-mutating App reads from serialized model-tool calls."""

    def __init__(
        self,
        config: DashboardConfig,
        *,
        client_class: type[Any] | None = None,
        client_error_class: type[BaseException] | None = None,
        post_join_hook: PostJoinHook | None = None,
        read_client_factory: ReadClientFactory | None = None,
    ):
        if (client_class is None) != (client_error_class is None):
            raise ValueError("client class and error class must be supplied together")
        if client_class is None or client_error_class is None:
            client_class, client_error_class = _load_board_client()
        self.config = config
        self._client_class = client_class
        self._client_error_class = client_error_class
        self._post_join_hook = post_join_hook
        self._read_client_factory = read_client_factory or (
            lambda value: RawBoardReader(value, self._client_class)
        )
        self._commands: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._start_lock = asyncio.Lock()
        self._view_start_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()
        self._first_attempt: asyncio.Event | None = None
        self._view_first_attempt: asyncio.Event | None = None
        self._worker_task: asyncio.Task | None = None
        self._view_task: asyncio.Task | None = None
        self._client: Any | None = None
        self._stopping = False
        self._connected = False
        self._feed_error: str | None = None
        self._view_connected = False
        self._view_error: str | None = None
        self._projection: dict[str, Any] | None = None
        self._projection_ticket_sources: dict[str, dict[str, Any]] = {}
        self._event_cursor: int | None = None
        self._event_ids: set[str] = set()
        self._events: list[dict[str, Any]] = []
        self._dropped_events = 0
        self._has_more = False
        self._resync_notice: str | None = None

    async def start(self) -> None:
        async with self._start_lock:
            if self._worker_task is None or self._worker_task.done():
                self._stopping = False
                self._first_attempt = asyncio.Event()
                self._worker_task = asyncio.create_task(
                    self._worker(), name="dashboard-central"
                )
            first_attempt = self._first_attempt
        assert first_attempt is not None
        try:
            async with asyncio.timeout(
                self.config.request_timeout_s
                + min(1.0, self.config.request_timeout_s)
            ):
                await first_attempt.wait()
        except TimeoutError:
            self._connected = False
            self._feed_error = "Central unavailable (TimeoutError)"
            task = self._worker_task
            if task is not None and not task.done():
                task.cancel()

    async def stop(self) -> None:
        self._stopping = True
        tasks = [
            task
            for task in (self._worker_task, self._view_task)
            if task is not None
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            if task.done():
                continue
            done, _pending = await asyncio.wait(
                {task}, timeout=min(1.0, self.config.request_timeout_s)
            )
            if task not in done:
                self._feed_error = "Central unavailable (TimeoutError)"
        for task in tasks:
            if not task.done():
                continue
            with suppress(asyncio.CancelledError):
                await task
        self._connected = False
        self._view_connected = False
        self._client = None
        self._fail_pending(RuntimeError("dashboard stopped"))

    def _fail_pending(self, exc: BaseException) -> None:
        while True:
            try:
                _method, _args, _kwargs, future = self._commands.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not future.done():
                future.set_exception(exc)

    @staticmethod
    def _safe_connection_error(exc: BaseException) -> str:
        return f"Central unavailable ({type(exc).__name__})"

    @staticmethod
    def _safe_validation_detail(detail: str) -> str | None:
        """Return only validation-shaped text without sensitive markers."""
        if (
            "://" in detail
            or "@" in detail
            or _SENSITIVE_ERROR_DETAIL_RE.search(detail)
            or _HOSTNAME_ERROR_DETAIL_RE.search(detail)
        ):
            return None
        if any(
            pattern.fullmatch(detail) for pattern in _SAFE_CENTRAL_VALIDATION_DETAILS
        ):
            return detail
        return None

    def _wrapped_validation_detail(self, exc: BaseException) -> str | None:
        """Extract only allowlisted Central validation text from client errors."""
        match = _WRAPPED_TOOL_ERROR_RE.fullmatch(str(exc))
        if match is None:
            return None
        try:
            text = ast.literal_eval(match.group("text"))
        except (SyntaxError, ValueError):
            return None
        if not isinstance(text, str):
            return None
        tool_error = _CENTRAL_TOOL_ERROR_RE.fullmatch(text)
        if tool_error is None:
            return None
        return self._safe_validation_detail(tool_error.group("detail"))

    def _safe_request_error(self, exc: BaseException) -> str:
        """Expose operator-authored validation details, never transport/auth text."""
        detail: str | None = None
        if isinstance(exc, (ValueError, TypeError)):
            detail = self._safe_validation_detail(str(exc))
        elif isinstance(exc, self._client_error_class):
            # pursers-client a10 wraps Central tool failures in BoardClientError.
            # Only the pinned, canonical envelope plus a validation allowlist is
            # trusted; generic client/auth failures remain class-name-only.
            detail = self._wrapped_validation_detail(exc)
        prefix = f"Central request failed ({type(exc).__name__})"
        return f"{prefix}: {detail}" if detail else prefix

    @staticmethod
    def _fleet_registry_projects(
        result: dict[str, Any],
    ) -> tuple[list[dict[str, str]], int]:
        state = result.get("state")
        if not isinstance(state, dict):
            raise ValueError("project_registry state is missing")
        raw_value = state.get("value")
        if not isinstance(raw_value, str):
            raise ValueError("project_registry value is missing")
        try:
            registry = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError("project_registry is malformed") from exc
        if not isinstance(registry, dict) or registry.get("schema_version") != 1:
            raise ValueError("project_registry schema is unsupported")
        raw_projects = registry.get("projects")
        if not isinstance(raw_projects, dict):
            raise ValueError("project_registry projects are missing")

        projects: list[dict[str, str]] = []
        board_ids: set[str] = set()
        for name, raw_project in raw_projects.items():
            if not isinstance(name, str) or not name or name != name.strip():
                raise ValueError("project_registry project name is invalid")
            if not isinstance(raw_project, dict):
                raise ValueError("project_registry project is invalid")
            board_id = raw_project.get("board_id")
            work_dir = raw_project.get("work_dir")
            status = raw_project.get("status")
            if (
                not isinstance(board_id, str)
                or not board_id
                or board_id != board_id.strip()
                or not isinstance(work_dir, str)
                or not Path(work_dir).is_absolute()
                or status not in {"active", "paused"}
            ):
                raise ValueError("project_registry project fields are invalid")
            if status != "active":
                continue
            if board_id in board_ids:
                raise ValueError("project_registry has duplicate active boards")
            board_ids.add(board_id)
            projects.append(
                {"name": name, "board_id": board_id, "status": "active"}
            )
        projects.sort(key=lambda item: (item["name"], item["board_id"]))
        omitted = max(0, len(projects) - FLEET_MAX_PROJECTS)
        return projects[:FLEET_MAX_PROJECTS], omitted

    @staticmethod
    def _fleet_status_count(counts: Any, *statuses: str) -> int:
        if not isinstance(counts, dict):
            return 0
        total = 0
        for status in statuses:
            value = counts.get(status, 0)
            if isinstance(value, int) and not isinstance(value, bool):
                total += max(0, value)
        return total

    async def _fleet_board_read(
        self, project: dict[str, str]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        board_config = replace(self.config, board_id=project["board_id"])
        connection = self._read_client_factory(board_config)
        async with AsyncExitStack() as connection_stack:
            reader = await connection_stack.enter_async_context(connection)
            status = await reader.board_status()
            snapshot = await reader.board_snapshot(
                limit=FLEET_SNAPSHOT_LIMIT,
                max_bytes=FLEET_SNAPSHOT_MAX_BYTES,
            )
        return status, snapshot

    async def fleet(self) -> dict[str, Any]:
        """Return a bounded, non-joining pool view across active registry boards."""
        async with self._read_lock:
            await self._probe_central()
            async with asyncio.timeout(self.config.request_timeout_s):
                registry_warning: str | None = None
                projects_omitted = 0
                try:
                    home_connection = self._read_client_factory(self.config)
                    async with AsyncExitStack() as connection_stack:
                        home_reader = await connection_stack.enter_async_context(
                            home_connection
                        )
                        registry = await home_reader.board_state_get(
                            key="project_registry"
                        )
                    projects, projects_omitted = self._fleet_registry_projects(
                        registry
                    )
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    projects = [
                        {
                            "name": self.config.board_id,
                            "board_id": self.config.board_id,
                            "status": "active",
                        }
                    ]
                    registry_warning = (
                        "project_registry unavailable or malformed; using the "
                        "profile board only"
                    )

                project_rows: list[dict[str, Any]] = []
                groups: dict[tuple[str, str], dict[str, Any]] = {}
                unavailable_boards: list[str] = []
                truncation_counts = {
                    "projects": projects_omitted,
                    "boards": 0,
                    "agents": 0,
                    "tickets": 0,
                    "pool": 0,
                }
                for project in projects:
                    try:
                        status, snapshot = await self._fleet_board_read(project)
                    except asyncio.CancelledError:
                        raise
                    except BaseException:
                        unavailable_boards.append(project["board_id"])
                        project_rows.append(
                            {
                                **project,
                                "tickets_open": 0,
                                "tickets_claimed": 0,
                                "tickets_submitted": 0,
                            }
                        )
                        continue

                    counts = status.get("ticket_status_counts", {})
                    project_rows.append(
                        {
                            **project,
                            "tickets_open": self._fleet_status_count(counts, "open"),
                            "tickets_claimed": self._fleet_status_count(
                                counts, *sorted(FLEET_CLAIM_STATES)
                            ),
                            "tickets_submitted": self._fleet_status_count(
                                counts, "submitted"
                            ),
                        }
                    )
                    omitted = snapshot.get("omitted_counts", {})
                    if isinstance(omitted, dict):
                        for name in ("agents", "tickets"):
                            value = omitted.get(name, 0)
                            if isinstance(value, int) and not isinstance(value, bool):
                                truncation_counts[name] += max(0, value)
                    if snapshot.get("truncated"):
                        truncation_counts["boards"] += 1

                    current_by_agent: dict[str, str] = {}
                    for ticket in snapshot.get("tickets", []):
                        if not isinstance(ticket, dict):
                            continue
                        if ticket.get("status") not in FLEET_CLAIM_STATES:
                            continue
                        holder = ticket.get("claimed_by_agent_id")
                        ticket_id = ticket.get("ticket_id")
                        if (
                            isinstance(holder, str)
                            and holder
                            and isinstance(ticket_id, str)
                            and ticket_id
                        ):
                            current_by_agent.setdefault(holder, ticket_id)

                    for agent in snapshot.get("agents", []):
                        if not isinstance(agent, dict):
                            continue
                        principal_id = agent.get("principal_id")
                        agent_name = agent.get("agent_name")
                        agent_id = agent.get("agent_id")
                        if (
                            not isinstance(principal_id, str)
                            or not principal_id
                            or not isinstance(agent_name, str)
                            or not agent_name
                            or not isinstance(agent_id, str)
                        ):
                            truncation_counts["agents"] += 1
                            continue
                        stale = self._agent_activity_age(
                            agent.get("last_activity_at")
                        )[1]
                        handed_off = (
                            agent.get("lifecycle_status") == "handed_off"
                            or agent.get("status") == "handed_off"
                        )
                        live = not stale and not handed_off
                        current_ticket_id = current_by_agent.get(agent_id)
                        if not live:
                            current_ticket_id = None
                        busy = live and (
                            current_ticket_id is not None
                            or (
                                agent.get("status") == "working"
                                and agent.get("lease_expires_at") is not None
                            )
                        )
                        key = (principal_id, agent_name)
                        group = groups.setdefault(
                            key,
                            {
                                "principal_id": principal_id,
                                "agent_name": agent_name,
                                "seats_by_board": {},
                            },
                        )
                        seat = {
                            "board_id": project["board_id"],
                            "project": project["name"],
                            "live": live,
                            "current_ticket_id": current_ticket_id,
                            "_busy": busy,
                        }
                        existing = group["seats_by_board"].get(project["board_id"])
                        if existing is None:
                            group["seats_by_board"][project["board_id"]] = seat
                        else:
                            existing["live"] = existing["live"] or live
                            existing["current_ticket_id"] = (
                                existing["current_ticket_id"] or current_ticket_id
                            )
                            existing["_busy"] = existing["_busy"] or busy

                if unavailable_boards:
                    suffix = "unavailable active boards: " + ", ".join(
                        sorted(unavailable_boards)
                    )
                    registry_warning = (
                        f"{registry_warning}; {suffix}"
                        if registry_warning
                        else suffix
                    )

                pool: list[dict[str, Any]] = []
                for group in groups.values():
                    seats = sorted(
                        group.pop("seats_by_board").values(),
                        key=lambda item: (str(item["project"]), item["board_id"]),
                    )
                    live_seats = [seat for seat in seats if seat["live"]]
                    busy = any(seat["_busy"] for seat in live_seats)
                    for seat in seats:
                        seat.pop("_busy")
                    if busy:
                        pool_status = "busy"
                    elif live_seats:
                        pool_status = "available"
                    else:
                        pool_status = "stale"
                    pool.append({**group, "pool_status": pool_status, "seats": seats})
                pool.sort(
                    key=lambda item: (item["agent_name"], item["principal_id"])
                )
                totals = {
                    "agents": len(pool),
                    "busy": sum(item["pool_status"] == "busy" for item in pool),
                    "available": sum(
                        item["pool_status"] == "available" for item in pool
                    ),
                    "stale": sum(item["pool_status"] == "stale" for item in pool),
                }
                payload = {
                    "schema_version": FLEET_SCHEMA_VERSION,
                    "registry_warning": registry_warning,
                    "projects": project_rows,
                    "pool": pool,
                    "totals": totals,
                    "truncation_counts": truncation_counts,
                }
                while (
                    len(
                        json.dumps(
                            payload, ensure_ascii=False, sort_keys=True
                        ).encode("utf-8")
                    )
                    > FLEET_RESPONSE_MAX_BYTES
                    and payload["pool"]
                ):
                    payload["pool"].pop()
                    truncation_counts["pool"] += 1
                return payload

    @staticmethod
    def _bounded_link_value(value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        return value[:LINK_VALUE_MAX_LENGTH]

    @classmethod
    def _link_projection(
        cls, board_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Project explicit Central memory_links edges without memory content."""
        raw_nodes = result.get("nodes")
        raw_edges = result.get("edges")
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            raise ValueError("memory_links response is malformed")

        nodes: list[dict[str, Any]] = []
        selected: set[str] = set()
        for raw in raw_nodes[:LINK_MEMORY_LIMIT]:
            if not isinstance(raw, dict):
                continue
            memory_id = cls._bounded_link_value(raw.get("memory_id"))
            if memory_id is None or memory_id in selected:
                continue
            selected.add(memory_id)
            nodes.append(
                {
                    "memory_id": memory_id,
                    "title": cls._bounded_link_value(raw.get("title"))
                    or "Untitled memory",
                    "memory_type": cls._bounded_link_value(raw.get("memory_type"))
                    or "context",
                    "created_at": cls._bounded_link_value(raw.get("created_at")),
                    "pinned": raw.get("pinned") is True,
                }
            )

        edges: list[dict[str, str]] = []
        for raw in raw_edges:
            if len(edges) >= LINK_EDGE_LIMIT:
                break
            if not isinstance(raw, dict):
                continue
            kind = cls._bounded_link_value(raw.get("kind"))
            source = cls._bounded_link_value(raw.get("from"))
            target = cls._bounded_link_value(raw.get("to"))
            if (
                kind not in {"ticket", "file", "tag", "retracts"}
                or source not in selected
                or target is None
            ):
                continue
            edges.append(
                {
                    "kind": kind,
                    "from": source,
                    "to": target,
                    "authority": "authoritative",
                }
            )

        raw_node_count = result.get("node_count")
        raw_edge_count = result.get("edge_count")
        node_count = (
            raw_node_count
            if isinstance(raw_node_count, int) and not isinstance(raw_node_count, bool)
            else len(raw_nodes)
        )
        edge_count = (
            raw_edge_count
            if isinstance(raw_edge_count, int) and not isinstance(raw_edge_count, bool)
            else len(raw_edges)
        )
        payload: dict[str, Any] = {
            "schema_version": LINK_SCHEMA_VERSION,
            "board_id": board_id,
            "source_tool": "memory_links",
            "relationship_authority": "authoritative",
            "nodes": nodes,
            "edges": edges,
            "node_count": max(0, node_count),
            "edge_count": max(0, edge_count),
            "truncated": max(0, node_count) >= LINK_MEMORY_LIMIT
            or len(nodes) < max(0, node_count)
            or len(edges) < max(0, edge_count),
            "returned_node_count": len(nodes),
            "returned_edge_count": len(edges),
        }
        while (
            len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            > LINK_RESPONSE_MAX_BYTES
            and (payload["edges"] or payload["nodes"])
        ):
            payload["truncated"] = True
            if payload["edges"]:
                payload["edges"].pop()
            else:
                removed = payload["nodes"].pop()
                removed_id = removed["memory_id"]
                payload["edges"] = [
                    edge for edge in payload["edges"] if edge["from"] != removed_id
                ]
            payload["returned_node_count"] = len(payload["nodes"])
            payload["returned_edge_count"] = len(payload["edges"])
        return payload

    async def links(self) -> dict[str, Any]:
        """Return a bounded, non-joining projection of explicit memory links."""
        async with self._read_lock:
            await self._probe_central()
            async with asyncio.timeout(self.config.request_timeout_s):
                connection = self._read_client_factory(self.config)
                async with AsyncExitStack() as connection_stack:
                    reader = await connection_stack.enter_async_context(connection)
                    result = await reader.memory_links(
                        depth=1, limit=LINK_MEMORY_LIMIT
                    )
        return self._link_projection(self.config.board_id, result)

    async def _worker(self) -> None:
        assert self._first_attempt is not None
        retry_delay = self.config.reconnect_min_s
        active_future: asyncio.Future | None = None
        try:
            while not self._stopping:
                try:
                    await self._probe_central()
                    try:
                        connection = self._client_class(
                            self.config.central_url,
                            self.config.token,
                            self.config.board_id,
                            agent_name=self.config.agent_name,
                            reconnect_delay_s=self.config.reconnect_min_s,
                            capabilities={"can_work": False, "can_review": False},
                        )
                    except TypeError:
                        connection = self._client_class(
                            self.config.central_url,
                            self.config.token,
                            self.config.board_id,
                            agent_name=self.config.agent_name,
                            reconnect_delay_s=self.config.reconnect_min_s,
                        )
                    async with AsyncExitStack() as connection_stack:
                        async with asyncio.timeout(self.config.request_timeout_s):
                            client = await connection_stack.enter_async_context(
                                connection
                            )
                            self._client = client
                            await self._initialize_connection(client)
                        self._connected = True
                        self._feed_error = None
                        self._first_attempt.set()
                        retry_delay = self.config.reconnect_min_s

                        while not self._stopping:
                            method, args, kwargs, future = await self._commands.get()
                            if future.done():
                                continue
                            active_future = future
                            try:
                                async with asyncio.timeout(
                                    self.config.request_timeout_s
                                ):
                                    result = await self._dispatch(
                                        client, method, args, kwargs
                                    )
                            except asyncio.CancelledError:
                                raise
                            except BaseException as exc:
                                if not isinstance(
                                    exc,
                                    (self._client_error_class, ValueError, TypeError),
                                ):
                                    self._connected = False
                                    self._feed_error = self._safe_connection_error(exc)
                                    if not future.done():
                                        future.set_exception(
                                            RuntimeError(self._feed_error)
                                        )
                                    raise
                                if not future.done():
                                    future.set_exception(
                                        RuntimeError(self._safe_request_error(exc))
                                    )
                            else:
                                if not future.done():
                                    future.set_result(result)
                            finally:
                                if future.done():
                                    active_future = None
                except asyncio.CancelledError:
                    self._connected = False
                    message = (
                        "dashboard stopped"
                        if self._stopping
                        else "dashboard worker cancelled"
                    )
                    if active_future is not None and not active_future.done():
                        active_future.set_exception(RuntimeError(message))
                    active_future = None
                    self._fail_pending(RuntimeError(message))
                    self._first_attempt.set()
                    raise
                except BaseException as exc:
                    self._connected = False
                    self._feed_error = self._safe_connection_error(exc)
                    if active_future is not None and not active_future.done():
                        active_future.set_exception(RuntimeError(self._feed_error))
                    active_future = None
                    self._fail_pending(RuntimeError(self._feed_error))
                    self._first_attempt.set()
                finally:
                    self._client = None

                if not self._stopping:
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(
                        self.config.reconnect_max_s, retry_delay * 2
                    )
        finally:
            self._connected = False
            self._client = None
            self._first_attempt.set()
            self._fail_pending(RuntimeError("dashboard worker stopped"))

    async def _initialize_connection(self, client: Any) -> None:
        """Apply Personal policy only after a model tool starts the joined worker."""
        if self._post_join_hook is not None:
            await self._post_join_hook(client)

    async def _dispatch(
        self,
        client: Any,
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        await self._probe_central()
        result = await getattr(client, method)(*args, **kwargs)
        if isinstance(result, dict):
            self._observe_result(result)
        return result

    async def _probe_central(self) -> None:
        """Avoid entering an unbounded MCP call when the loopback service is down."""
        parsed = urlsplit(self.config.central_url)
        assert parsed.hostname is not None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        async with asyncio.timeout(min(1.0, self.config.request_timeout_s)):
            _reader, writer = await asyncio.open_connection(parsed.hostname, port)
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def _load_projection(
        self, client: Any, snapshot: dict[str, Any] | None = None
    ) -> None:
        cold = snapshot if snapshot is not None else await client.board_snapshot()
        status = await client.board_status()
        listed = await client.ticket_list(include_closed=True, limit=MAX_TICKETS)
        board = cold.get("board", {})
        board_id = str(board.get("board_id", self.config.board_id))
        agents = status.get("agents")
        if not isinstance(agents, list):
            agents = cold.get("agents", [])
        bridge_identity = getattr(client, "identity", None)
        bridge_agent_id = getattr(bridge_identity, "agent_id", None)
        bridge_agent_name = getattr(client, "agent_name", None)
        if bridge_agent_id is not None or bridge_agent_name is not None:
            agents = [
                item
                for item in agents
                if not isinstance(item, dict)
                or not (
                    (
                        bridge_agent_id is not None
                        and item.get("agent_id") == bridge_agent_id
                    )
                    or (
                        bridge_agent_name is not None
                        and item.get("agent_name") == bridge_agent_name
                    )
                )
            ]
        tickets = listed.get("tickets", [])
        ticket_sources = {
            str(ticket["ticket_id"]): copy.deepcopy(ticket)
            for ticket in tickets
            if isinstance(ticket, dict) and ticket.get("ticket_id")
        }
        current_tickets_by_agent_id, recent_tickets_by_agent_id = (
            self._ticket_assignments(ticket_sources.values())
        )
        state = cold.get("state", {})
        briefing_entry = state.get("briefing", {}) if isinstance(state, dict) else {}
        briefing_value = (
            briefing_entry.get("value") if isinstance(briefing_entry, dict) else None
        )
        briefing: dict[str, Any] = {}
        if isinstance(briefing_value, str):
            try:
                decoded = json.loads(briefing_value)
            except ValueError:
                decoded = None
            if isinstance(decoded, dict):
                briefing = decoded
        pinned = briefing.get("pinned_digest", [])
        if not isinstance(pinned, list):
            pinned = []
        important = [
            item
            for item in pinned
            if isinstance(item, dict)
            and item.get("memory_type") in {"blocker", "warning", "decision"}
        ]
        important.sort(
            key=lambda item: (
                float(item.get("created_at_epoch", 0)),
                int(item.get("priority", 0)),
                str(item.get("memory_id", "")),
            ),
            reverse=True,
        )
        agent_views = [
            self._agent_view(
                item,
                current_tickets_by_agent_id.get(str(item.get("agent_id", ""))),
            )
            for item in agents
        ]
        for agent in agent_views:
            agent_id = str(agent.get("id") or "")
            current = current_tickets_by_agent_id.get(agent_id)
            project_ticket = (
                current
                if current is not None
                else recent_tickets_by_agent_id.get(agent_id)
            )
            agent["project"] = self._project_from_target(
                project_ticket.get("target_url")
                if project_ticket is not None
                else None
            )
        self._flag_duplicate_agent_names(agent_views)
        agents_live = sum(not bool(agent["stale"]) for agent in agent_views)
        projection = {
            "contract_version": 2,
            "data_mode": "live",
            "fixture_provenance": (
                "authorization-scoped Central state via a verified non-joining "
                "MCP read session"
            ),
            "board": {"id": board_id, "name": board_id},
            "agents": agent_views,
            "tickets": [self._ticket_view(item) for item in tickets],
            "highlights": {
                "latest_handoff": self._memory_highlight(
                    briefing.get("latest_handoff")
                ),
                "important_pinned": self._memory_highlight(
                    important[0] if important else None
                ),
            },
            "status": {
                "ticket_status_counts": copy.deepcopy(
                    status.get("ticket_status_counts", {})
                ),
                "memory_type_counts": copy.deepcopy(
                    status.get("memory_type_counts", {})
                ),
                "visible_memory_count": status.get("visible_memory_count", 0),
                "scrub_profile": status.get("scrub_profile"),
            },
            "ticket_total": int(listed.get("total_matching", len(tickets))),
            "ticket_truncated": int(listed.get("total_matching", len(tickets)))
            > len(tickets),
            "agent_total": len(agents),
            "agents_live": agents_live,
            "agent_truncated": False,
            "latest_seq": max(
                int(cold.get("latest_seq", 0)),
                int(status.get("latest_seq", 0)),
                int(listed.get("latest_seq", 0)),
            ),
            "snapshot_at": cold.get("snapshot_at"),
        }
        self._projection_ticket_sources = ticket_sources
        self._projection = projection

    async def _refresh_view(self) -> None:
        """Refresh via pure Central tools without join, heartbeat, or cursor ack."""
        async with self._read_lock:
            try:
                await self._probe_central()
                connection = self._read_client_factory(self.config)
                async with asyncio.timeout(self.config.request_timeout_s):
                    async with AsyncExitStack() as connection_stack:
                        reader = await connection_stack.enter_async_context(connection)
                        await self._load_projection(reader)
                assert self._projection is not None
                self._event_cursor = int(self._projection.get("latest_seq", 0))
                self._has_more = False
                self._resync_notice = (
                    "Activity follows side-effect-free Central journal cues; "
                    "the UI reads the cached projection."
                )
                self._view_connected = True
                self._view_error = None
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self._view_connected = False
                self._view_error = self._safe_connection_error(exc)

    async def _ensure_view(self) -> None:
        async with self._view_start_lock:
            if self._view_task is None or self._view_task.done():
                self._stopping = False
                self._view_first_attempt = asyncio.Event()
                self._view_task = asyncio.create_task(
                    self._view_worker(), name="dashboard-view-subscription"
                )
            first_attempt = self._view_first_attempt
        assert first_attempt is not None
        try:
            async with asyncio.timeout(
                self.config.request_timeout_s
                + min(1.0, self.config.request_timeout_s)
            ):
                await first_attempt.wait()
        except TimeoutError:
            self._view_connected = False
            self._view_error = "Central unavailable (TimeoutError)"
            task = self._view_task
            if task is not None and not task.done():
                task.cancel()

    async def _apply_view_cue(self, event: dict[str, Any]) -> None:
        self._observe_event(event)
        ticket_id = event.get("ticket_id")
        if isinstance(ticket_id, str) and ticket_id:
            async with self._read_lock:
                await self._probe_central()
                connection = self._read_client_factory(self.config)
                async with asyncio.timeout(self.config.request_timeout_s):
                    async with AsyncExitStack() as connection_stack:
                        reader = await connection_stack.enter_async_context(connection)
                        self._observe_result(await reader.ticket_get(ticket_id))
            return
        # Non-ticket cues have no narrower public projection read. Refresh the
        # snapshot only on that cue, never from the UI timer.
        await self._refresh_view()

    async def _consume_view_events(self, reader: Any) -> None:
        events_method = getattr(reader, "events", None)
        if not callable(events_method):
            # Test and compatibility readers without the public events API keep
            # their initial cached projection without starting the model client.
            self._view_connected = True
            self._view_error = None
            assert self._view_first_attempt is not None
            self._view_first_attempt.set()
            await asyncio.Event().wait()
            return

        ready = asyncio.Event()

        def advance(cursor: int) -> None:
            self._event_cursor = int(cursor)
            ready.set()

        stream = events_method(int(self._event_cursor or 0), advance)
        next_event = asyncio.create_task(anext(stream))
        ready_wait = asyncio.create_task(ready.wait())
        try:
            while not ready.is_set():
                done, _pending = await asyncio.wait(
                    {next_event, ready_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                if ready_wait in done:
                    break
                if next_event in done:
                    await self._apply_view_cue(next_event.result())
                    next_event = asyncio.create_task(anext(stream))
            self._view_connected = True
            self._view_error = None
            assert self._view_first_attempt is not None
            self._view_first_attempt.set()
            while not self._stopping:
                event = await next_event
                await self._apply_view_cue(event)
                next_event = asyncio.create_task(anext(stream))
        finally:
            for task in (next_event, ready_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(next_event, ready_wait, return_exceptions=True)
            close = getattr(stream, "aclose", None)
            if callable(close):
                await close()

    async def _view_worker(self) -> None:
        assert self._view_first_attempt is not None
        retry_delay = self.config.reconnect_min_s
        try:
            while not self._stopping:
                try:
                    await self._refresh_view()
                    if not self._view_connected:
                        self._view_first_attempt.set()
                        if not self._stopping:
                            await asyncio.sleep(retry_delay)
                            retry_delay = min(
                                self.config.reconnect_max_s, retry_delay * 2
                            )
                        continue
                    self._view_connected = False
                    reader = self._read_client_factory(self.config)
                    await self._consume_view_events(reader)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    self._view_connected = False
                    self._view_error = self._safe_connection_error(exc)
                    print(
                        "Personal dashboard subscription lost; cached view is stale "
                        f"({type(exc).__name__})",
                        file=sys.stderr,
                    )
                    self._view_first_attempt.set()
                    if not self._stopping:
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(
                            self.config.reconnect_max_s, retry_delay * 2
                        )
                else:
                    retry_delay = self.config.reconnect_min_s
        finally:
            self._view_connected = False
            self._view_first_attempt.set()

    async def _rpc(self, method: str, *args: Any, **kwargs: Any) -> Any:
        await self.start()
        if self._stopping:
            raise RuntimeError("dashboard stopped")
        if self._client is None or not self._connected:
            raise RuntimeError(self._feed_error or "Central unavailable")
        loop = asyncio.get_running_loop()
        result = loop.create_future()
        try:
            self._commands.put_nowait((method, args, kwargs, result))
        except asyncio.QueueFull as exc:
            result.cancel()
            raise RuntimeError("dashboard busy") from exc
        return await result

    @staticmethod
    def _agent_activity_age(value: Any) -> tuple[int, bool]:
        if not isinstance(value, str) or not value:
            return 0, False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - parsed).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0, False
        elapsed = max(0.0, elapsed)
        idle_minutes = min(525_600, int(elapsed // 60))
        stale = elapsed > AGENT_STALE_AFTER_MINUTES * 60
        return idle_minutes, stale

    @classmethod
    def _idle_minutes(cls, value: Any) -> int:
        return cls._agent_activity_age(value)[0]

    @staticmethod
    def _ticket_claimed_sort_key(ticket: dict[str, Any]) -> tuple[str, ...]:
        return (
            str(ticket.get("claimed_at") or ""),
            str(ticket.get("updated_at") or ""),
            str(ticket.get("created_at") or ""),
            str(ticket.get("ticket_id") or ""),
        )

    @staticmethod
    def _ticket_touched_sort_key(ticket: dict[str, Any]) -> tuple[str, ...]:
        timestamps = [
            str(ticket.get(field) or "")
            for field in (
                "updated_at",
                "closed_at",
                "submitted_at",
                "claimed_at",
                "created_at",
            )
        ]
        return (max(timestamps), str(ticket.get("ticket_id") or ""))

    @classmethod
    def _ticket_assignments(
        cls, tickets: Any
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        current_by_agent: dict[str, dict[str, Any]] = {}
        recent_by_agent: dict[str, dict[str, Any]] = {}
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            holder_ids = {
                ticket.get("claimed_by_agent_id"),
                ticket.get("assigned_to_agent_id"),
            }
            if ticket.get("status") in DASHBOARD_ACTIVE_TICKET_STATES:
                for agent_id in holder_ids - {None, ""}:
                    key = str(agent_id)
                    current = current_by_agent.get(key)
                    if current is None or cls._ticket_claimed_sort_key(
                        ticket
                    ) > cls._ticket_claimed_sort_key(current):
                        current_by_agent[key] = ticket

            touched_ids = holder_ids | {
                ticket.get("last_claimed_by_agent_id"),
                ticket.get("submitted_by_agent_id"),
                ticket.get("reviewed_by_agent_id"),
                ticket.get("created_by_agent_id"),
            }
            recency = cls._ticket_touched_sort_key(ticket)
            for agent_id in touched_ids - {None, ""}:
                key = str(agent_id)
                previous = recent_by_agent.get(key)
                if previous is None or recency > cls._ticket_touched_sort_key(
                    previous
                ):
                    recent_by_agent[key] = ticket
        return current_by_agent, recent_by_agent

    @classmethod
    def _agent_view(
        cls,
        agent: dict[str, Any],
        current_ticket: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        idle_minutes, stale = cls._agent_activity_age(agent.get("last_activity_at"))
        return {
            "id": agent.get("agent_id"),
            "name": agent.get("agent_name", "unknown-agent"),
            "status": agent.get("status", "unknown"),
            "role": agent.get("role"),
            "focus": agent.get("task_focus"),
            "platform": agent.get("agent_platform"),
            "idle_minutes": idle_minutes,
            "last_activity_at": agent.get("last_activity_at"),
            "lease_expires_at": agent.get("lease_expires_at"),
            "stale": stale,
            "duplicate": False,
            "duplicate_name": False,
            "suggested_name": None,
            "current_ticket_id": (
                str(current_ticket["ticket_id"])
                if current_ticket is not None and current_ticket.get("ticket_id")
                else None
            ),
            "current_ticket": (
                cls._ticket_view(current_ticket)
                if current_ticket is not None
                else None
            ),
        }

    @staticmethod
    def _flag_duplicate_agent_names(agents: list[dict[str, Any]]) -> None:
        ids_by_name: dict[str, set[str]] = {}
        for agent in agents:
            agent_id = agent.get("id")
            if agent_id in {None, ""}:
                continue
            name = str(agent.get("name", "unknown-agent"))
            ids_by_name.setdefault(name, set()).add(str(agent_id))

        for agent in agents:
            agent_id = agent.get("id")
            name = str(agent.get("name", "unknown-agent"))
            agent["duplicate_name"] = (
                agent_id not in {None, ""} and len(ids_by_name.get(name, set())) >= 2
            )

        active_by_name: dict[str, list[dict[str, Any]]] = {}
        for agent in agents:
            if agent.get("stale") or str(agent.get("status", "")).lower() != "active":
                continue
            name = str(agent.get("name", "unknown-agent"))
            active_by_name.setdefault(name, []).append(agent)

        for name, matches in active_by_name.items():
            if len(matches) < 2:
                continue
            for agent in matches:
                agent_id = str(agent.get("id", ""))
                suffix = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()[:6]
                agent["duplicate"] = True
                agent["suggested_name"] = f"{name}-{suffix}"

    @staticmethod
    def _project_from_target(target_url: Any) -> str | None:
        if not isinstance(target_url, str):
            return None
        project = target_url.strip().partition("/")[0].strip().lower()
        return project or None

    @classmethod
    def _ticket_view(cls, ticket: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": ticket["ticket_id"],
            "project": cls._project_from_target(ticket.get("target_url")),
            "title": ticket.get("title", "(untitled)"),
            "description": ticket.get("description", ""),
            "status": ticket.get("status", "unknown"),
            "priority": ticket.get("priority", "medium"),
            "assigned_to": ticket.get("assigned_to"),
            "assigned_agent_id": ticket.get("assigned_to_agent_id"),
            "claimed_agent_id": ticket.get("claimed_by_agent_id"),
            "lease_expires_at": ticket.get("lease_expires_at"),
            "ttl_s": ticket.get("ttl_s"),
            "rejected": ticket.get("status") == "rejected",
            "abandoned_count": int(ticket.get("abandoned_count", 0)),
            "rejection_count": int(ticket.get("rejection_count", 0)),
            "created_at": ticket.get("created_at"),
            "updated_at": ticket.get("updated_at"),
            "submitted_at": ticket.get("submitted_at"),
            "closed_at": ticket.get("closed_at"),
        }

    @staticmethod
    def _memory_highlight(memory: Any) -> dict[str, Any] | None:
        if not isinstance(memory, dict) or not memory.get("memory_id"):
            return None
        summary = (
            memory.get("pinned_summary")
            or memory.get("summary")
            or memory.get("title")
            or ""
        )
        next_steps = memory.get("next_steps", [])
        warnings = memory.get("warnings", [])
        if not isinstance(next_steps, list):
            next_steps = []
        if not isinstance(warnings, list):
            warnings = []
        return {
            "id": memory.get("memory_id"),
            "type": memory.get("memory_type", "context"),
            "title": memory.get("title") or summary or "Untitled note",
            "summary": summary,
            "author": memory.get("author_agent_name")
            or memory.get("author_agent_id"),
            "created_at": memory.get("created_at"),
            "next_steps": [str(item) for item in next_steps[:8]],
            "warnings": [str(item) for item in warnings[:8]],
        }

    @staticmethod
    def _ticket_source_from_view(ticket: dict[str, Any]) -> dict[str, Any]:
        """Recover assignment fields when a test supplied only a projection."""
        return {
            "ticket_id": ticket.get("id"),
            "target_url": ticket.get("project"),
            "title": ticket.get("title"),
            "description": ticket.get("description"),
            "status": ticket.get("status"),
            "priority": ticket.get("priority"),
            "assigned_to": ticket.get("assigned_to"),
            "assigned_to_agent_id": ticket.get("assigned_agent_id"),
            "claimed_by_agent_id": ticket.get("claimed_agent_id"),
            "lease_expires_at": ticket.get("lease_expires_at"),
            "ttl_s": ticket.get("ttl_s"),
            "abandoned_count": ticket.get("abandoned_count", 0),
            "rejection_count": ticket.get("rejection_count", 0),
            "created_at": ticket.get("created_at"),
            "updated_at": ticket.get("updated_at"),
            "submitted_at": ticket.get("submitted_at"),
            "closed_at": ticket.get("closed_at"),
        }

    def _refresh_ticket_projection(self, ticket: dict[str, Any]) -> None:
        if self._projection is None:
            return
        projected = copy.deepcopy(self._projection)
        sources = copy.deepcopy(self._projection_ticket_sources)
        if not sources:
            sources = {
                str(item["id"]): self._ticket_source_from_view(item)
                for item in projected.get("tickets", [])
                if isinstance(item, dict) and item.get("id")
            }

        ticket_id = str(ticket["ticket_id"])
        previous = sources.get(ticket_id)
        is_new = previous is None
        sources[ticket_id] = copy.deepcopy(ticket)

        projected["tickets"] = [
            self._ticket_view(sources[key]) for key in sorted(sources)
        ]
        counts: dict[str, int] = {}
        for source in sources.values():
            status = str(source.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        status_view = projected.setdefault("status", {})
        status_view["ticket_status_counts"] = counts

        total = int(projected.get("ticket_total", len(sources)))
        if is_new:
            total += 1
        projected["ticket_total"] = max(total, len(sources))
        projected["ticket_truncated"] = projected["ticket_total"] > len(sources)

        current_by_agent, recent_by_agent = self._ticket_assignments(
            sources.values()
        )
        affected_agent_ids = {
            value
            for source in (previous, ticket)
            if isinstance(source, dict)
            for field in (
                "claimed_by_agent_id",
                "assigned_to_agent_id",
                "last_claimed_by_agent_id",
                "submitted_by_agent_id",
                "reviewed_by_agent_id",
                "created_by_agent_id",
            )
            if (value := source.get(field)) not in {None, ""}
        }
        updated_project = self._project_from_target(ticket.get("target_url"))
        for agent in projected.get("agents", []):
            if not isinstance(agent, dict):
                continue
            agent_id = str(agent.get("id") or "")
            current = current_by_agent.get(agent_id)
            recent = recent_by_agent.get(agent_id)
            agent["current_ticket_id"] = (
                str(current["ticket_id"])
                if current is not None and current.get("ticket_id")
                else None
            )
            agent["current_ticket"] = (
                self._ticket_view(current) if current is not None else None
            )
            project_ticket = current if current is not None else recent
            if project_ticket is not None:
                agent["project"] = self._project_from_target(
                    project_ticket.get("target_url")
                )
            elif agent_id in affected_agent_ids:
                agent["project"] = updated_project

        self._projection_ticket_sources = sources
        self._projection = projected

    def _observe_result(self, result: dict[str, Any]) -> None:
        for key in ("event", "admission_event"):
            event = result.get(key)
            if isinstance(event, dict):
                self._observe_event(event)
        for event in result.get("release_events", []):
            if isinstance(event, dict):
                self._observe_event(event)

        ticket = result.get("ticket")
        if not isinstance(ticket, dict) or not ticket.get("ticket_id"):
            return
        self._refresh_ticket_projection(ticket)

    def _observe_event(self, event: dict[str, Any]) -> None:
        event_id = str(event.get("id", ""))
        if not event_id or event_id in self._event_ids:
            return
        self._event_ids.add(event_id)
        self._events.append(
            {
                "id": event_id,
                "seq": event.get("seq"),
                "kind": event.get("kind"),
                "text": self._event_text(event),
                "actor_id": event.get("actor"),
                "occurred_at": event.get("occurred_at"),
                "ticket_id": event.get("ticket_id"),
                "memory_id": event.get("memory_id"),
                "status_from": event.get("status_from"),
                "status_to": event.get("status_to"),
                "payload_ref": event.get("payload_ref"),
            }
        )
        self._events.sort(
            key=lambda item: (
                item.get("seq") is None,
                int(item.get("seq") or 0),
                item["id"],
            )
        )
        if len(self._events) > MAX_EVENTS:
            evicted = self._events.pop(0)
            self._event_ids.discard(evicted["id"])
            self._dropped_events += 1

    @staticmethod
    def _event_text(event: dict[str, Any]) -> str:
        item = event.get("ticket_id", event.get("memory_id", "board"))
        if event.get("status_to"):
            return f"{item}: {event.get('status_from')} -> {event['status_to']}"
        return f"{item}: {event.get('kind', 'updated')}"

    def _fallback_snapshot(self) -> dict[str, Any]:
        return {
            "contract_version": 2,
            "data_mode": "demo",
            "fixture_provenance": (
                "synthetic, authored for standalone embedded fallback"
            ),
            "board": {"id": self.config.board_id, "name": "Personal Preview Demo"},
            "agents": copy.deepcopy(FALLBACK_AGENTS),
            "tickets": copy.deepcopy(FALLBACK_TICKETS),
            "highlights": copy.deepcopy(FALLBACK_HIGHLIGHTS),
            "status": {
                "ticket_status_counts": {"claimed": 1, "submitted": 1},
                "memory_type_counts": {"handoff": 1, "warning": 1},
                "visible_memory_count": 2,
                "scrub_profile": "synthetic",
            },
            "ticket_total": len(FALLBACK_TICKETS),
            "ticket_truncated": False,
            "agent_total": len(FALLBACK_AGENTS),
            "agents_live": len(FALLBACK_AGENTS),
            "agent_truncated": False,
            "events": copy.deepcopy(FALLBACK_EVENTS),
            "event_cursor": FALLBACK_EVENTS[-1]["seq"],
            "dropped_events": 0,
            "has_more": False,
            "connected": False,
            "stale": True,
            "feed_error": self._view_error or "Central unavailable",
            "resync_notice": None,
            "activity_scope": "synthetic-demo",
        }

    def _snapshot_payload(self) -> dict[str, Any]:
        if self._projection is None:
            return self._fallback_snapshot()
        result = copy.deepcopy(self._projection)
        result.update(
            {
                "data_mode": "live" if self._view_connected else "stale",
                "events": copy.deepcopy(self._events),
                "event_cursor": self._event_cursor,
                "dropped_events": self._dropped_events,
                "has_more": self._has_more,
                "connected": self._view_connected,
                "stale": not self._view_connected,
                "feed_error": self._view_error,
                "resync_notice": self._resync_notice,
                "activity_scope": "local-model-tools",
            }
        )
        return result

    async def ticket_create(self, ticket_id: str, title: str) -> dict[str, Any]:
        return await self._rpc("ticket_create", ticket_id, title)

    async def ticket_claim(self, ticket_id: str) -> dict[str, Any]:
        return await self._rpc("ticket_claim", ticket_id)

    async def ticket_submit(self, ticket_id: str) -> dict[str, Any]:
        return await self._rpc("ticket_submit", ticket_id)

    async def snapshot(self) -> dict[str, Any]:
        await self._ensure_view()
        if self._projection is not None and not self._view_connected:
            await self._refresh_view()
        return self._snapshot_payload()

    async def feed(self) -> dict[str, Any]:
        await self._ensure_view()
        payload = self._snapshot_payload()
        payload["count"] = len(payload["events"])
        payload["error"] = payload.get("feed_error")
        return payload


def build_dashboard_server(
    config: DashboardConfig,
    *,
    client_class: type[Any] | None = None,
    client_error_class: type[BaseException] | None = None,
    post_join_hook: PostJoinHook | None = None,
    read_client_factory: ReadClientFactory | None = None,
) -> tuple[MCPServer, LiveDashboard]:
    """Build one explicit-profile MCP server without creating global state."""
    state = LiveDashboard(
        config,
        client_class=client_class,
        client_error_class=client_error_class,
        post_join_hook=post_join_hook,
        read_client_factory=read_client_factory,
    )
    apps = Apps()

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            await state.stop()

    @apps.tool(
        resource_uri=UI_URI,
        description="Get the current authorization-scoped live board projection.",
        visibility=MODEL_AND_APP,
    )
    async def board_snapshot() -> dict[str, Any]:
        return await state.snapshot()

    @apps.tool(
        resource_uri=UI_URI,
        description=(
            "Get the bounded authorization-scoped fleet projection across active "
            "project-registry boards."
        ),
        visibility=MODEL_AND_APP,
    )
    async def fleet_snapshot() -> dict[str, Any]:
        return await state.fleet()

    @apps.tool(
        resource_uri=UI_URI,
        description=(
            "Get bounded authoritative ticket, memory, file, and tag links "
            "from the selected board's memory_links projection."
        ),
        visibility=APP_ONLY,
    )
    async def link_snapshot() -> dict[str, Any]:
        return await state.links()

    @apps.tool(
        resource_uri=UI_URI,
        description=(
            "Read the in-process subscription-backed projection cache and return "
            "bounded observed events without polling Central."
        ),
        visibility=APP_ONLY,
    )
    async def board_event_feed() -> dict[str, Any]:
        return await state.feed()

    @apps.tool(
        resource_uri=UI_URI,
        description="Join the selected personal board and receive its working context.",
        visibility=MODEL_ONLY,
    )
    async def board_onboard(
        claim_ttl_s: int | None = None,
        agent_platform: str | None = None,
        task_focus: str | None = None,
        token_budget: int = 4_000,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        return await state._rpc(
            "board_onboard",
            claim_ttl_s=claim_ttl_s,
            agent_platform=agent_platform,
            task_focus=task_focus,
            token_budget=token_budget,
            ticket_id=ticket_id,
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Get board health and visible workload counts.",
        visibility=MODEL_ONLY,
    )
    async def board_status() -> dict[str, Any]:
        return await state._rpc("board_status")

    @apps.tool(
        resource_uri=UI_URI,
        description=(
            "Read a bounded Central journal page for the model. touch=false is the "
            "default side-effect-free read and ignores ack without updating activity, "
            "leases, or the durable cursor. With touch=true, ack=false leaves the "
            "cursor unchanged and ack=true advances it."
        ),
        visibility=MODEL_ONLY,
    )
    async def board_catchup(
        cursor: int | None = None,
        limit: int = 100,
        ack: bool = True,
        touch: bool = False,
    ) -> dict[str, Any]:
        return await state._rpc(
            "board_catchup", cursor=cursor, limit=limit, ack=ack, touch=touch
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Read one visible ticket by ID.",
        visibility=MODEL_ONLY,
    )
    async def ticket_get(ticket_id: str) -> dict[str, Any]:
        return await state._rpc("ticket_get", ticket_id)

    @apps.tool(
        resource_uri=UI_URI,
        description="List visible tickets using bounded filters.",
        visibility=MODEL_ONLY,
    )
    async def ticket_list(
        status: str | None = None,
        assigned_to: str | None = None,
        include_closed: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await state._rpc(
            "ticket_list",
            status=status,
            assigned_to=assigned_to,
            include_closed=include_closed,
            limit=limit,
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Create a ticket on the selected personal board.",
        visibility=MODEL_ONLY,
    )
    async def ticket_create(
        title: str,
        ticket_id: str | None = None,
        description: str | None = None,
        scope: str | None = None,
        required_fields: list[str] | None = None,
        forbidden: list[str] | None = None,
        priority: str = "medium",
        tags: list[str] | None = None,
        related_files: list[str] | None = None,
        target_url: str | None = None,
        assigned_to: str | None = None,
        unassigned: bool = False,
    ) -> dict[str, Any]:
        return await state._rpc(
            "ticket_create",
            ticket_id,
            title,
            description=description,
            scope=scope,
            required_fields=required_fields,
            forbidden=forbidden,
            priority=priority,
            tags=tags,
            related_files=related_files,
            target_url=target_url,
            assigned_to=assigned_to,
            unassigned=unassigned,
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Claim one visible ticket for the current agent.",
        visibility=MODEL_ONLY,
    )
    async def ticket_claim(ticket_id: str) -> dict[str, Any]:
        return await state._rpc("ticket_claim", ticket_id)

    @apps.tool(
        resource_uri=UI_URI,
        description="Submit completed work on a claimed ticket.",
        visibility=MODEL_ONLY,
    )
    async def ticket_submit(
        ticket_id: str,
        summary: str | None = None,
        files_changed: list[str] | None = None,
        notes: str | None = None,
        stay_active: bool = True,
    ) -> dict[str, Any]:
        return await state._rpc(
            "ticket_submit",
            ticket_id,
            summary=summary,
            files_changed=files_changed,
            notes=notes,
            stay_active=stay_active,
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Record a review verdict for submitted ticket work.",
        visibility=MODEL_ONLY,
    )
    async def ticket_review(
        ticket_id: str,
        verdict: str,
        review_notes: str | None = None,
        fix_instructions: str | None = None,
    ) -> dict[str, Any]:
        return await state._rpc(
            "ticket_review",
            ticket_id,
            verdict,
            review_notes=review_notes,
            fix_instructions=fix_instructions,
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Renew the current agent's ticket lease.",
        visibility=MODEL_ONLY,
    )
    async def lease_renew(ticket_id: str) -> dict[str, Any]:
        return await state._rpc("lease_renew", ticket_id)

    @apps.tool(
        resource_uri=UI_URI,
        description="Cancel a ticket with an optional reason.",
        visibility=MODEL_ONLY,
    )
    async def ticket_cancel(
        ticket_id: str, reason: str | None = None
    ) -> dict[str, Any]:
        return await state._rpc("ticket_cancel", ticket_id, reason=reason)

    @apps.tool(
        resource_uri=UI_URI,
        description="Terminate a ticket with an optional reason.",
        visibility=MODEL_ONLY,
    )
    async def ticket_terminate(
        ticket_id: str, reason: str | None = None
    ) -> dict[str, Any]:
        return await state._rpc("ticket_terminate", ticket_id, reason=reason)

    @apps.tool(
        resource_uri=UI_URI,
        description="Write authorization-scoped project memory.",
        visibility=MODEL_ONLY,
    )
    async def memory_write(
        title: str,
        content: str,
        scope: str,
        memory_type: str = "context",
        tags: list[str] | None = None,
        priority: int = 0,
        pinned_summary: str | None = None,
        retracts: str | None = None,
        related_files: list[str] | None = None,
        related_tickets: list[str] | None = None,
    ) -> dict[str, Any]:
        return await state._rpc(
            "memory_write",
            title,
            content,
            scope,
            memory_type=memory_type,
            tags=tags,
            priority=priority,
            pinned_summary=pinned_summary,
            retracts=retracts,
            related_files=related_files,
            related_tickets=related_tickets,
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Read bounded authorization-scoped project memory.",
        visibility=MODEL_ONLY,
    )
    async def memory_read(
        memory_type: str | None = None,
        tag: str | None = None,
        author: str | None = None,
        since: str | None = None,
        since_minutes: int | None = None,
        pinned_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await state._rpc(
            "memory_read",
            memory_type=memory_type,
            tag=tag,
            author=author,
            since=since,
            since_minutes=since_minutes,
            pinned_only=pinned_only,
            limit=limit,
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Search authorization-scoped project memory.",
        visibility=MODEL_ONLY,
    )
    async def memory_search(
        query: str,
        tag: str | None = None,
        author: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await state._rpc(
            "memory_search", query, tag=tag, author=author, limit=limit
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Traverse bounded memory relationships.",
        visibility=MODEL_ONLY,
    )
    async def memory_links(
        memory_id: str | None = None,
        ticket_id: str | None = None,
        file: str | None = None,
        author: str | None = None,
        depth: int = 2,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await state._rpc(
            "memory_links",
            memory_id=memory_id,
            ticket_id=ticket_id,
            file=file,
            author=author,
            depth=depth,
            limit=limit,
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Record a compact continuation checkpoint.",
        visibility=MODEL_ONLY,
    )
    async def memory_checkpoint(
        summary: str,
        remaining_tasks: list[str] | None = None,
        files: list[str] | None = None,
        next_steps: list[str] | None = None,
        active_branch: str | None = None,
        blockers: list[str] | None = None,
        scope: str = "project",
    ) -> dict[str, Any]:
        return await state._rpc(
            "memory_checkpoint",
            summary,
            remaining_tasks=remaining_tasks,
            files=files,
            next_steps=next_steps,
            active_branch=active_branch,
            blockers=blockers,
            scope=scope,
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Record a handoff with explicit next steps.",
        visibility=MODEL_ONLY,
    )
    async def memory_handoff(
        summary: str,
        next_steps: list[str],
        files: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return await state._rpc(
            "memory_handoff",
            summary,
            next_steps,
            files=files,
            warnings=warnings,
        )

    @apps.tool(
        resource_uri=UI_URI,
        description="Unpin one memory entry with an optional reason.",
        visibility=MODEL_ONLY,
    )
    async def memory_unpin(
        memory_id: str, reason: str | None = None
    ) -> dict[str, Any]:
        return await state._rpc("memory_unpin", memory_id, reason=reason)

    @apps.tool(
        resource_uri=UI_URI,
        description="Read one or all public board-state values.",
        visibility=MODEL_ONLY,
    )
    async def board_state_get(key: str | None = None) -> dict[str, Any]:
        return await state._rpc("board_state_get", key)

    @apps.tool(
        resource_uri=UI_URI,
        description="Update one public board-state value.",
        visibility=MODEL_ONLY,
    )
    async def board_state_update(key: str, value: str) -> dict[str, Any]:
        return await state._rpc("board_state_update", key, value)

    apps.add_html_resource(
        UI_URI,
        load_dashboard_html(),
        title="On Board Personal",
        description="Local personal board with a clearly labeled standalone fallback",
        csp=ResourceCsp(),
        prefers_border=True,
    )
    return (
        MCPServer(
            "On Board Personal",
            version=PRODUCT_VERSION,
            extensions=[apps],
            lifespan=lifespan,
        ),
        state,
    )


def build_personal_server(
    profile_path: Path,
    host_id: str,
    session: str,
) -> tuple[MCPServer, LiveDashboard]:
    """Build a server from one verified profile and derived host/session identity."""
    client_class, client_error_class = _load_board_client()
    from .profile import (
        bootstrap_personal_review_policy,
        load_personal_profile,
        resolve_personal_context,
    )

    profile = load_personal_profile(Path(profile_path))
    context = resolve_personal_context(profile, host=host_id, session=session)
    return build_dashboard_server(
        config_from_personal_context(context),
        client_class=client_class,
        client_error_class=client_error_class,
        post_join_hook=bootstrap_personal_review_policy,
    )


def run_personal_mcp(profile_path: Path, host_id: str, session: str) -> None:
    """Run the profile-backed personal MCP server over stdio."""
    server, _state = build_personal_server(profile_path, host_id, session)
    server.run()
