"""Profile-backed MCP Apps and personal chat facade for On Board.

The module has no import-time server and never reads legacy ``ONBOARD_*``
connection variables. A launcher must resolve a verified personal profile and
pass its resulting context to :func:`build_profile_apps_server`.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import importlib.resources
import ipaddress
import sys
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit

from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import MCPServer

from .artifacts import import_verified_component, verify_component_artifacts

PINNED_CLIENT_VERSION = "0.1.0a10"
PRODUCT_VERSION = "5.0.0a1"
MAX_EVENTS = 200
MAX_TICKETS = 500
UI_URI = "ui://onboard/dashboard"
MODEL_AND_APP = ["model", "app"]
MODEL_ONLY = ["model"]
APP_ONLY = ["app"]
PRIMARY_UI_TOOL_NAMES = frozenset({"board_snapshot", "board_event_feed"})
CHAT_TOOL_NAMES = frozenset(
    {
        "board_onboard",
        "board_get_briefing",
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


def _load_board_client() -> tuple[type[Any], type[BaseException]]:
    """Verify the installed a10 package and import it through the safe loader."""
    verified = verify_component_artifacts({"onboard-client"})["onboard-client"]
    if verified["version"] != PINNED_CLIENT_VERSION:
        raise RuntimeError("unsupported onboard-client version")
    client_module = import_verified_component(
        "onboard-client",
        "onboard_client",
        "onboard_client.client",
        package_member="onboard_client/__init__.py",
        module_member="onboard_client/client.py",
    )
    return client_module.BoardClient, client_module.BoardClientError


def load_dashboard_html() -> str:
    """Verify the complete component lock, then load the packaged View."""
    verify_component_artifacts()
    return (
        importlib.resources.files("onboard_personal")
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
        "name": "agent-alpha",
        "status": "working",
        "role": "builder",
        "idle_minutes": 1,
        "focus": "Personal UI shell",
        "platform": "synthetic",
        "last_activity_at": "2099-01-01T00:00:00Z",
        "lease_expires_at": None,
    },
    {
        "id": "AI-DEMO-2",
        "project": None,
        "name": "reviewer-beta",
        "status": "idle",
        "role": "reviewer",
        "idle_minutes": 18,
        "focus": "Accessibility review",
        "platform": "synthetic",
        "last_activity_at": "2099-01-01T00:00:00Z",
        "lease_expires_at": None,
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

    The transport and decoder come from the already verified onboard-client
    module, but its ``BoardClient.__aenter__`` is deliberately never invoked.
    In particular, this reader does not expose ``board_catchup``: Central a9's
    compatibility heartbeat mutates a board for write-scoped principals even
    when ``ack=False``.
    """

    def __init__(
        self,
        config: DashboardConfig,
        board_client_class: type[Any],
    ) -> None:
        client_module = sys.modules.get(board_client_class.__module__)
        if client_module is None:
            raise RuntimeError("verified onboard-client module is unavailable")
        try:
            self._httpx2 = client_module.httpx2
            self._mcp_client_class = client_module.Client
            self._streamable_http_client = client_module.streamable_http_client
            self._decode = board_client_class._decode
        except AttributeError as exc:
            raise RuntimeError("verified onboard-client read primitives are unavailable") from exc
        self.config = config
        self.agent_name = config.agent_name
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
            "ticket_list",
            "board_get_briefing",
        }:
            raise RuntimeError("raw board reader rejected a non-pure tool")
        result = await self._client.call_tool(
            name, {"board_id": self.config.board_id, **arguments}
        )
        return self._decode(result)

    async def board_snapshot(self) -> dict[str, Any]:
        return await self._call("board_snapshot", {})

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

    async def board_get_briefing(
        self, *, token_budget: int = 4_000, ticket_id: str | None = None
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"token_budget": token_budget}
        if ticket_id is not None:
            arguments["ticket_id"] = ticket_id
        return await self._call("board_get_briefing", arguments)


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
        self._read_lock = asyncio.Lock()
        self._first_attempt: asyncio.Event | None = None
        self._worker_task: asyncio.Task | None = None
        self._client: Any | None = None
        self._stopping = False
        self._connected = False
        self._feed_error: str | None = None
        self._view_connected = False
        self._view_error: str | None = None
        self._projection: dict[str, Any] | None = None
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
        task = self._worker_task
        if task is not None and not task.done():
            task.cancel()
            done, _pending = await asyncio.wait(
                {task}, timeout=min(1.0, self.config.request_timeout_s)
            )
            if task not in done:
                self._feed_error = "Central unavailable (TimeoutError)"
        if task is not None and task.done():
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

    async def _worker(self) -> None:
        assert self._first_attempt is not None
        retry_delay = self.config.reconnect_min_s
        active_future: asyncio.Future | None = None
        try:
            while not self._stopping:
                try:
                    await self._probe_central()
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
                                    exc, (self._client_error_class, ValueError)
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
                                        RuntimeError(
                                            f"Central request failed ({type(exc).__name__})"
                                        )
                                    )
                            else:
                                if not future.done():
                                    future.set_result(result)
                            finally:
                                if future.done():
                                    active_future = None
                except asyncio.CancelledError as exc:
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
        briefing = await client.board_get_briefing(token_budget=1_200)
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
        current_tickets_by_agent_id: dict[str, dict[str, Any]] = {}
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            if ticket.get("status") in {"closed", "canceled", "terminated"}:
                continue
            holder_ids = {
                ticket.get("claimed_by_agent_id"),
                ticket.get("assigned_to_agent_id"),
            }
            for agent_id in holder_ids - {None, ""}:
                key = str(agent_id)
                current = current_tickets_by_agent_id.get(key)
                if current is None or self._ticket_claimed_sort_key(
                    ticket
                ) > self._ticket_claimed_sort_key(current):
                    current_tickets_by_agent_id[key] = ticket
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
        agent_projects: dict[str, tuple[str, str]] = {}
        for ticket in tickets:
            if ticket.get("status") not in {
                "open",
                "claimed",
                "in_progress",
                "creating_report",
                "rejected",
            }:
                continue
            project = self._project_from_target(ticket.get("target_url"))
            if project is None:
                continue
            holder = ticket.get("claimed_by_agent_id") or ticket.get(
                "assigned_to_agent_id"
            )
            if not isinstance(holder, str) or not holder:
                continue
            recency = str(
                ticket.get("claimed_at")
                or ticket.get("updated_at")
                or ticket.get("created_at")
                or ""
            )
            previous = agent_projects.get(holder)
            if previous is None or recency >= previous[0]:
                agent_projects[holder] = (recency, project)

        agent_views = [
            self._agent_view(
                item,
                current_tickets_by_agent_id.get(str(item.get("agent_id", ""))),
            )
            for item in agents
        ]
        for agent in agent_views:
            held = agent_projects.get(str(agent.get("id") or ""))
            agent["project"] = held[1] if held is not None else None
        self._flag_duplicate_agent_names(agent_views)
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
            "agent_truncated": False,
            "latest_seq": max(
                int(cold.get("latest_seq", 0)),
                int(status.get("latest_seq", 0)),
                int(listed.get("latest_seq", 0)),
            ),
            "snapshot_at": cold.get("snapshot_at"),
        }
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
                    "Activity includes only events observed from model tools in this "
                    "MCP process; the App does not read or acknowledge Central journal."
                )
                self._view_connected = True
                self._view_error = None
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self._view_connected = False
                self._view_error = self._safe_connection_error(exc)

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
    def _idle_minutes(value: Any) -> int:
        if not isinstance(value, str) or not value:
            return 0
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - parsed).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0
        return min(525_600, max(0, int(elapsed // 60)))

    @staticmethod
    def _ticket_claimed_sort_key(ticket: dict[str, Any]) -> tuple[str, ...]:
        return (
            str(ticket.get("claimed_at") or ""),
            str(ticket.get("updated_at") or ""),
            str(ticket.get("created_at") or ""),
            str(ticket.get("ticket_id") or ""),
        )

    @classmethod
    def _agent_view(
        cls,
        agent: dict[str, Any],
        current_ticket: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": agent.get("agent_id"),
            "name": agent.get("agent_name", "unknown-agent"),
            "status": agent.get("status", "unknown"),
            "role": agent.get("role"),
            "focus": agent.get("task_focus"),
            "platform": agent.get("agent_platform"),
            "idle_minutes": cls._idle_minutes(agent.get("last_activity_at")),
            "last_activity_at": agent.get("last_activity_at"),
            "lease_expires_at": agent.get("lease_expires_at"),
            "duplicate": False,
            "suggested_name": None,
            "current_ticket": (
                cls._ticket_view(current_ticket)
                if current_ticket is not None
                else None
            ),
        }

    @staticmethod
    def _flag_duplicate_agent_names(agents: list[dict[str, Any]]) -> None:
        active_by_name: dict[str, list[dict[str, Any]]] = {}
        for agent in agents:
            if str(agent.get("status", "")).lower() != "active":
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
        if self._projection is None:
            return
        projected = copy.deepcopy(self._projection)
        tickets = {
            item["id"]: item for item in projected.get("tickets", []) if item.get("id")
        }
        tickets[ticket["ticket_id"]] = self._ticket_view(ticket)
        projected["tickets"] = [tickets[key] for key in sorted(tickets)]
        projected["ticket_total"] = max(
            int(projected.get("ticket_total", 0)), len(tickets)
        )
        self._projection = projected

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
        await self._refresh_view()
        return self._snapshot_payload()

    async def feed(self) -> dict[str, Any]:
        await self._refresh_view()
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
            "Refresh the non-mutating live projection and return bounded events "
            "observed from model tools in this MCP process."
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
        description="Get a bounded briefing for the selected personal board.",
        visibility=MODEL_ONLY,
    )
    async def board_get_briefing(
        token_budget: int = 4_000,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        return await state._rpc(
            "board_get_briefing", token_budget=token_budget, ticket_id=ticket_id
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
            "Read a bounded Central journal page for the model. With the personal "
            "write-scoped capability this may renew activity or leases even when "
            "ack is false; ack=true also advances the durable cursor."
        ),
        visibility=MODEL_ONLY,
    )
    async def board_catchup(
        cursor: int | None = None,
        limit: int = 100,
        ack: bool = True,
    ) -> dict[str, Any]:
        return await state._rpc(
            "board_catchup", cursor=cursor, limit=limit, ack=ack
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
