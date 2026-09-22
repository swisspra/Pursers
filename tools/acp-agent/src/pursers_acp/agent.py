"""ACP v1 stdio agent exposing a deliberately small Personal board surface."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from pursers_client import (
    BoardClient,
    BoardClientError,
    PersonalProfileError,
    coordinator_host_binding,
    parse_project_registry,
    read_capability,
)
from pursers_client.personal_profile import (
    default_profiles_root,
    profile_path_for_project,
    select_personal_profile,
)

JSON = dict[str, Any]
ACP_VERSION = 1
IMPLEMENTATION_VERSION = "0.1.2"
MAX_MESSAGE_BYTES = 1_048_576
MAX_TEXT_CHARS = 8_000
MAX_ROWS = 20
MAX_QUESTION_INBOX = 100
MAX_PLAN_ENTRIES = 20
MAX_PLAN_TITLE_CHARS = 160
WATCH_SNAPSHOT_LIMIT = 500
IN_FLIGHT_TICKET_STATES = frozenset(
    {
        "claimed",
        "in_progress",
        "creating_report",
        "submitted",
        "reviewing",
        "in_review",
        "needs_human",
    }
)
REVIEW_DISPATCH_STATES = frozenset({"review_claimed", "reviewing", "in_review"})
QUESTION_INBOX_UNAVAILABLE = (
    "Seat-question replies are unavailable in this ACP profile because its board "
    "identity is not a registered project coordinator. Ordinary board updates "
    "will continue."
)


class AuthRequired(RuntimeError):
    """No usable human Personal profile is available."""


class PromptCancelled(Exception):
    """The client cancelled the active prompt."""


async def _cancelable_io(
    cancel: asyncio.Event, operation: Callable[[], Awaitable[Any]]
) -> Any:
    """Run one blocking I/O operation until it finishes or the prompt is cancelled."""
    if cancel.is_set():
        raise PromptCancelled
    work = asyncio.ensure_future(operation())
    stopped = asyncio.create_task(cancel.wait())
    try:
        done, _pending = await asyncio.wait(
            {work, stopped}, return_when=asyncio.FIRST_COMPLETED
        )
        if stopped in done:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise PromptCancelled
        return work.result()
    except BaseException:
        if not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        raise
    finally:
        stopped.cancel()
        await asyncio.gather(stopped, return_exceptions=True)


class BoardSurface(Protocol):
    board_id: str

    async def close(self) -> None: ...
    async def my_tickets(self) -> list[JSON]: ...
    async def my_offers(self) -> list[JSON]: ...
    async def board_status(self) -> JSON: ...
    async def ticket_evidence(self, ticket_id: str) -> JSON: ...
    def create_action(self, title: str, description: str) -> JSON: ...
    def annotate_action(self, ticket_id: str, text: str) -> JSON: ...
    async def answer_action(self, ticket_id: str, text: str) -> JSON: ...
    def question_answer_action(
        self, ticket_id: str, question_id: str, text: str
    ) -> JSON: ...
    async def mutate(self, action: JSON) -> JSON: ...
    def watch(
        self, cursor: int | None, cancel: asyncio.Event
    ) -> AsyncIterator[tuple[int, JSON]]: ...


BoardFactory = Callable[[], BoardSurface | Awaitable[BoardSurface]]
AuthSetup = Callable[[], None | Awaitable[None]]


class WaitBridge(Protocol):
    async def close(self) -> None: ...
    async def wait(self, board_id: str, cursor: int | None) -> JSON: ...
    def digests(
        self, board_id: str, cursor: int | None, cancel: asyncio.Event
    ) -> AsyncIterator[JSON]: ...


WaitBridgeFactory = Callable[[], WaitBridge | Awaitable[WaitBridge]]


class StdioWaitBridge:
    """One configured wait-bridge process owned by one ACP watch turn."""

    def __init__(self, command: str, args: list[str], env: dict[str, str]) -> None:
        self.command = command
        self.args = args
        self.env = env
        self._stack = AsyncExitStack()
        self._client: Client | None = None

    @classmethod
    async def connect(
        cls, command: str, args: list[str], env: dict[str, str]
    ) -> "StdioWaitBridge":
        bridge = cls(command, args, env)
        try:
            params = StdioServerParameters(command=command, args=args, env=env)
            bridge._client = await bridge._stack.enter_async_context(
                Client(params, mode="2026-07-28", cache=None)
            )
            return bridge
        except BaseException:
            await bridge.close()
            raise

    async def close(self) -> None:
        await self._stack.aclose()
        self._client = None

    async def wait(self, board_id: str, cursor: int | None) -> JSON:
        if self._client is None:
            raise RuntimeError("wait bridge is closed")
        arguments: JSON = {
            "boards": [board_id],
            "only_mine": False,
            "timeout_s": 180,
        }
        if cursor is not None:
            arguments["since_seq"] = {board_id: cursor}
        result = await self._client.call_tool("a2a_wait", arguments)
        return BoardClient._decode(result)

    async def _digest(self, board_id: str, cursor: int | None) -> JSON:
        if self._client is None:
            raise RuntimeError("wait bridge is closed")
        since = {board_id: cursor} if cursor is not None else {board_id: 0}
        result = await self._client.call_tool(
            "board_digest",
            {
                "since": since,
                "boards": [board_id],
                "include_notes_keys": [],
                "max_transitions_per_ticket": 2,
            },
        )
        return BoardClient._decode(result)

    async def _digest_with_questions(
        self, board_id: str, cursor: int | None
    ) -> JSON:
        """Read one pushed digest and the coordinator's current open questions."""
        if self._client is None:
            raise RuntimeError("wait bridge is closed")
        page = await self._digest(board_id, cursor)
        try:
            result = await self._client.call_tool(
                "board_question_inbox",
                {"state": "open", "limit": MAX_QUESTION_INBOX},
            )
            inbox = BoardClient._decode(result)
        except BoardClientError as exc:
            if not _question_inbox_authority_error(exc):
                raise
            return {
                **page,
                "questions": [],
                "question_inbox_unavailable": QUESTION_INBOX_UNAVAILABLE,
            }
        return {
            **page,
            "questions": [
                question
                for question in inbox.get("questions", [])
                if isinstance(question, dict)
            ],
        }

    async def digests(
        self, board_id: str, cursor: int | None, cancel: asyncio.Event
    ) -> AsyncIterator[JSON]:
        """Yield event-driven digest deltas without adding a polling loop."""
        if self._client is None:
            raise RuntimeError("wait bridge is closed")

        current = cursor
        try:
            initial = await _cancelable_io(
                cancel, lambda: self._digest_with_questions(board_id, current)
            )
        except PromptCancelled:
            return
        current = _digest_cursor(initial, board_id, current)
        yield initial

        resource_uri = f"board://{board_id}/digest"
        async with self._client.listen(
            resource_subscriptions=[resource_uri]
        ) as subscription:
            honored = getattr(subscription, "honored", None)
            selected = getattr(honored, "resource_subscriptions", None)
            if selected is not None and resource_uri not in set(selected):
                raise RuntimeError("wait bridge did not honor the board digest subscription")

            # Live-first splice: the listen is active before this second digest,
            # so an update cannot fall between the first read and the stream.
            try:
                splice = await _cancelable_io(
                    cancel, lambda: self._digest_with_questions(board_id, current)
                )
            except PromptCancelled:
                return
            current = _digest_cursor(splice, board_id, current)
            yield splice

            stream = aiter(subscription)
            while not cancel.is_set():
                cue = asyncio.create_task(anext(stream))
                stopped = asyncio.create_task(cancel.wait())
                done, pending = await asyncio.wait(
                    {cue, stopped}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stopped in done:
                    return
                try:
                    cue.result()
                except StopAsyncIteration:
                    return
                try:
                    page = await _cancelable_io(
                        cancel, lambda: self._digest_with_questions(board_id, current)
                    )
                except PromptCancelled:
                    return
                current = _digest_cursor(page, board_id, current)
                yield page


class PersonalBoardSurface:
    """Minimal Central transport that never joins or onboards a board seat."""

    def __init__(self, profile: Any) -> None:
        self.profile = profile
        self.board_id = profile.board_id
        self.principal_id = profile.principal_id
        self.agent_name = ""
        self.agent_id = ""
        self._coordinator_binding = ""
        self._stack = AsyncExitStack()
        self._client: Client | None = None
        self._wait_bridge_factory: WaitBridgeFactory | None = None

    @classmethod
    async def connect(
        cls, *, profile_path: Path | None, project_root: Path | None
    ) -> "PersonalBoardSurface":
        try:
            profile, _source = select_personal_profile(
                explicit_profile=profile_path,
                project_root=project_root,
            )
        except PersonalProfileError as exc:
            raise AuthRequired(str(exc)) from exc
        board = cls(profile)
        try:
            token = read_capability(profile)
            http = await board._stack.enter_async_context(
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx2.Timeout(10.0, read=None),
                    trust_env=False,
                )
            )
            transport = streamable_http_client(profile.central_url, http_client=http)
            board._client = await board._stack.enter_async_context(
                Client(transport, mode="2026-07-28", cache=None)
            )
            status = await board._call("board_status", {})
            own_agents = sorted(
                (
                    row
                    for row in status.get("agents", [])
                    if row.get("principal_id") == profile.principal_id
                    and isinstance(row.get("agent_name"), str)
                ),
                key=lambda row: (
                    row.get("lifecycle_status") != "active",
                    row.get("role") != "coordinator",
                    row.get("agent_name", ""),
                ),
            )
            if not own_agents:
                raise AuthRequired(
                    "Personal setup has not initialized an identity on this board"
                )
            board.agent_name = own_agents[0]["agent_name"]
            identity = own_agents[0]
            board.agent_id = str(identity["agent_id"])
            board._coordinator_binding = coordinator_host_binding(
                token, board.agent_id
            )
            bridge_env = _wait_bridge_environment(profile, token, identity)

            async def wait_bridge_factory() -> StdioWaitBridge:
                return await StdioWaitBridge.connect(
                    os.environ.get("PURSERS_WAIT_BRIDGE_COMMAND", "pursers-wait-bridge"),
                    [],
                    bridge_env,
                )

            board._wait_bridge_factory = wait_bridge_factory
            return board
        except BaseException:
            await board.close()
            raise

    async def close(self) -> None:
        await self._stack.aclose()
        self._client = None

    async def _call(self, method: str, params: JSON) -> JSON:
        if self._client is None:
            raise RuntimeError("board transport is closed")
        result = await self._client.call_tool(
            method, {"board_id": self.board_id, **params}
        )
        return BoardClient._decode(result)

    async def _status(self) -> JSON:
        return await self._call("board_status", {})

    async def my_tickets(self) -> list[JSON]:
        status, page = await asyncio.gather(
            self._status(),
            self._call(
                "ticket_list",
                {"include_closed": False, "include_archived": False, "limit": 100},
            ),
        )
        own_ids = {
            row.get("agent_id")
            for row in status.get("agents", [])
            if row.get("principal_id") == self.principal_id
        }
        return [
            row
            for row in page.get("tickets", [])
            if row.get("created_by_principal_id") == self.principal_id
            or row.get("claimed_by_principal_id") == self.principal_id
            or row.get("assigned_to_agent_id") in own_ids
        ][:MAX_ROWS]

    async def my_offers(self) -> list[JSON]:
        status = await self._status()
        return [
            {
                "agent_name": row.get("agent_name"),
                **dict(row["current_offer"]),
            }
            for row in status.get("agents", [])
            if row.get("principal_id") == self.principal_id
            and isinstance(row.get("current_offer"), Mapping)
        ][:MAX_ROWS]

    async def board_status(self) -> JSON:
        status, page = await asyncio.gather(
            self._status(),
            self._call(
                "ticket_list",
                {"include_closed": False, "include_archived": False, "limit": 100},
            ),
        )
        registry: JSON | None = None
        try:
            state = await self._call(
                "board_state_get", {"key": "project_registry"}
            )
            registry = parse_project_registry(state)
        except Exception:
            # Older and single-project boards remain usable. Ticket-owned project
            # labels still provide an honest, narrower view.
            pass
        result = {
            key: status.get(key)
            for key in (
                "board_id",
                "status_counts",
                "review_policy",
                "dispatch_enabled",
                "latest_seq",
            )
        }
        projects = _project_summaries(
            self.board_id,
            page.get("tickets", []),
            registry,
        )
        result["project_count"] = len(projects)
        result["projects"] = projects[:MAX_ROWS]
        result["project_ticket_scan_limited"] = bool(
            page.get("next_cursor") or len(page.get("tickets", [])) >= 100
        )
        return result

    async def ticket_evidence(self, ticket_id: str) -> JSON:
        return await self._call("ticket_get", {"ticket_id": ticket_id})

    def create_action(self, title: str, description: str) -> JSON:
        return {
            "operation": "ticket_create",
            "board_id": self.board_id,
            "params": {
                "agent_name": self.agent_name,
                "title": title,
                "description": description,
                "scope": "interactive-no-send",
                "required_fields": ["observations"],
                "target_url": self.board_id,
                "unassigned": True,
            },
        }

    def annotate_action(self, ticket_id: str, text: str) -> JSON:
        return {
            "operation": "ticket_annotate",
            "board_id": self.board_id,
            "params": {
                "agent_name": self.agent_name,
                "ticket_id": ticket_id,
                "text": text,
                "kind": "note",
            },
        }

    async def answer_action(self, ticket_id: str, text: str) -> JSON:
        result = await self.ticket_evidence(ticket_id)
        ticket = result.get("ticket")
        if not isinstance(ticket, Mapping):
            raise ValueError(f"ticket {ticket_id} was not found")
        request = ticket.get("human_request")
        if not isinstance(request, Mapping) or request.get("resolution") is not None:
            raise ValueError(f"ticket {ticket_id} has no pending human request")
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError(f"ticket {ticket_id} has an invalid human request")
        return {
            "operation": "ticket_human_resolve",
            "board_id": self.board_id,
            "params": {
                "agent_name": self.agent_name,
                "ticket_id": ticket_id,
                "request_id": request_id,
                "action": "accept",
                "content": _parse_answer_content(text),
                "disposition": "reopen",
            },
        }

    def question_answer_action(
        self, ticket_id: str, question_id: str, text: str
    ) -> JSON:
        if not self._coordinator_binding:
            raise RuntimeError("coordinator binding is unavailable")
        return {
            "operation": "ticket_question_answer",
            "board_id": self.board_id,
            "params": {
                "agent_name": self.agent_name,
                "ticket_id": ticket_id,
                "question_id": question_id,
                "host_binding": self._coordinator_binding,
                "action": "answer",
                "message": text,
            },
        }

    async def mutate(self, action: JSON) -> JSON:
        if action.get("board_id") != self.board_id:
            raise PermissionError("mutation board differs from the approved board")
        operation = action.get("operation")
        if operation not in {
            "ticket_create",
            "ticket_annotate",
            "ticket_human_resolve",
            "ticket_question_answer",
        }:
            raise PermissionError("unsupported mutation")
        params = action.get("params")
        if not isinstance(params, dict):
            raise ValueError("mutation params must be an object")
        return await self._call(operation, dict(params))

    async def watch(
        self, cursor: int | None, cancel: asyncio.Event
    ) -> AsyncIterator[tuple[int, JSON]]:
        if self._wait_bridge_factory is None:
            raise RuntimeError("wait bridge is unavailable")
        snapshot = await _cancelable_io(
            cancel,
            lambda: self._call(
                "ticket_list",
                {
                    "include_closed": False,
                    "include_archived": False,
                    "limit": WATCH_SNAPSHOT_LIMIT,
                },
            ),
        )
        snapshot_cursor = snapshot.get("latest_seq")
        if isinstance(snapshot_cursor, int) and not isinstance(snapshot_cursor, bool):
            cursor = max(cursor or 0, snapshot_cursor)
        tickets = [
            ticket for ticket in snapshot.get("tickets", []) if isinstance(ticket, dict)
        ]
        total_matching = snapshot.get("total_matching")
        snapshot_truncated = bool(
            isinstance(total_matching, int)
            and not isinstance(total_matching, bool)
            and total_matching > len(tickets)
        )
        yield cursor or 0, {
            "kind": "watch_snapshot",
            "tickets": tickets,
            "truncated": snapshot_truncated,
        }

        selected = self._wait_bridge_factory()
        bridge = await selected if inspect.isawaitable(selected) else selected
        question_notice_sent = False
        try:
            async for page in bridge.digests(self.board_id, cursor, cancel):
                cursor = _digest_cursor(page, self.board_id, cursor)
                question_notice = page.get("question_inbox_unavailable")
                if isinstance(question_notice, str) and not question_notice_sent:
                    question_notice_sent = True
                    yield cursor or 0, {
                        "kind": "question_inbox_unavailable",
                        "message": question_notice,
                    }
                for question in page.get("questions", []):
                    if not isinstance(question, dict):
                        continue
                    yield cursor or 0, {
                        "kind": "coordinator_question_asked",
                        "ticket_id": question.get("ticket_id"),
                        "question_id": question.get("question_id"),
                    }
                human_requests = {
                    request.get("ticket_id"): request
                    for request in page.get("human_requests", [])
                    if isinstance(request, Mapping)
                    and isinstance(request.get("ticket_id"), str)
                }
                for ticket in page.get("tickets", []):
                    if not isinstance(ticket, dict):
                        continue
                    review = ticket.get("review")
                    review_verdict = (
                        review.get("verdict")
                        if isinstance(review, Mapping)
                        else ticket.get("review_verdict")
                    )
                    event = {
                        "kind": "ticket_status_changed",
                        "ticket_id": ticket.get("ticket_id"),
                        "title": ticket.get("title"),
                        "project": ticket.get("project"),
                        "status_to": ticket.get("status_now"),
                        "dispatch_state": ticket.get("dispatch_state"),
                        "claimed_by": ticket.get("claimed_by"),
                        "review_state": ticket.get("review_state"),
                        "review_claimed_by": ticket.get("review_claimed_by"),
                    }
                    if review_verdict is not None:
                        event["review_verdict"] = review_verdict
                    offers = ticket.get("offers")
                    review_offer = (
                        offers.get("review_offer")
                        if isinstance(offers, Mapping)
                        else None
                    )
                    if isinstance(review_offer, Mapping):
                        event["review_offer"] = dict(review_offer)
                    request = human_requests.get(ticket.get("ticket_id"))
                    if request is not None:
                        event["human_request"] = {
                            "asked_by": {"agent_name": request.get("asked_by")}
                        }
                    yield cursor or 0, event
        finally:
            await bridge.close()


@dataclass
class Session:
    cwd: str
    cursors: dict[str, int] = field(default_factory=dict)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    active: bool = False
    prompt_request_id: Any | None = None
    mcp_stop: asyncio.Event | None = None
    mcp_task: asyncio.Task[None] | None = None
    pending_questions: dict[str, JSON] = field(default_factory=dict)
    next_question_ref: int = 1


class PursersACPAgent:
    def __init__(
        self, board_factory: BoardFactory, *, auth_setup: AuthSetup | None = None
    ) -> None:
        self.board_factory = board_factory
        self.auth_setup = auth_setup
        self.board: BoardSurface | None = None
        self.auth_error: str | None = None
        self.initialized = False
        self.client_capabilities: JSON = {}
        self.sessions: dict[str, Session] = {}
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.next_id = 10_000
        self.write_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[None]] = set()
        self.send: Callable[[JSON], Awaitable[None]] | None = None

    async def close(self) -> None:
        for session in self.sessions.values():
            session.cancel.set()
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        for session in self.sessions.values():
            if session.mcp_stop is not None:
                session.mcp_stop.set()
        mcp_tasks = [
            session.mcp_task
            for session in self.sessions.values()
            if session.mcp_task is not None
        ]
        if mcp_tasks:
            await asyncio.gather(*mcp_tasks, return_exceptions=True)
        if self.board is not None:
            await self.board.close()
            self.board = None

    async def run(
        self,
        receive: Callable[[], Awaitable[bytes]],
        send: Callable[[JSON], Awaitable[None]],
    ) -> None:
        self.send = send
        try:
            while raw := await receive():
                if len(raw) > MAX_MESSAGE_BYTES:
                    await self._error(None, -32700, "ACP message exceeds size limit")
                    continue
                try:
                    message = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await self._error(None, -32700, "invalid JSON")
                    continue
                await self.handle(message)
        finally:
            await self.close()

    async def handle(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            await self._error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid JSON-RPC request")
            return
        method = message.get("method")
        if not isinstance(method, str):
            self._resolve(message)
            return
        params = message.get("params", {})
        request_id = message.get("id")
        if not isinstance(params, dict):
            if request_id is not None:
                await self._error(request_id, -32602, "params must be an object")
            return
        if method == "session/cancel":
            session = self.sessions.get(params.get("sessionId"))
            if session is not None and session.prompt_request_id is not None:
                session.cancel.set()
            return
        if request_id is None:
            return
        # PersonalBoardSurface owns AnyIO/MCP context managers. Enter and exit
        # those contexts in the run-loop task; AnyIO cancel scopes cannot be
        # closed from the short-lived request task used for prompt concurrency.
        if method in {"initialize", "authenticate"}:
            await self._request(request_id, method, params)
            return
        if method == "session/prompt":
            session = self.sessions.get(params.get("sessionId"))
            if session is not None:
                if session.prompt_request_id is not None:
                    await self._error(
                        request_id, -32600, "session prompt already active"
                    )
                    return
                # Reserve and bind cancellation to this turn before scheduling
                # _request. A cancel notification may arrive before the task runs.
                session.prompt_request_id = request_id
                session.cancel = asyncio.Event()
        task = asyncio.create_task(self._request(request_id, method, params))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _resolve(self, message: JSON) -> None:
        request_id = message.get("id")
        future = self.pending.pop(request_id, None)
        if future is None:
            return
        if "result" in message:
            future.set_result(message["result"])
        elif "error" in message:
            future.set_exception(RuntimeError("ACP client rejected request"))

    async def _request(self, request_id: Any, method: str, params: JSON) -> None:
        try:
            if method == "initialize":
                await self._initialize(request_id, params)
            elif method == "authenticate":
                await self._authenticate(request_id, params)
            elif method == "session/new":
                await self._new_session(request_id, params)
            elif method == "session/prompt":
                await self._prompt(request_id, params)
            else:
                await self._error(request_id, -32601, f"unknown method {method}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._error(request_id, -32603, _bounded(str(exc), 500))

    async def _initialize(self, request_id: Any, params: JSON) -> None:
        if self.initialized:
            await self._error(request_id, -32600, "connection already initialized")
            return
        requested = params.get("protocolVersion")
        capabilities = params.get("clientCapabilities", {})
        if not isinstance(capabilities, dict):
            await self._error(request_id, -32602, "clientCapabilities must be an object")
            return
        self.client_capabilities = capabilities
        try:
            board = self.board_factory()
            self.board = await board if inspect.isawaitable(board) else board
        except AuthRequired as exc:
            self.auth_error = _bounded(str(exc), 500)
        self.initialized = True
        auth_methods: list[JSON] = [
            {
                "id": "pursers-personal-profile",
                "name": "Use Pursers Personal",
                "description": "Use the selected existing human Personal profile",
                "type": "agent",
            }
        ]
        auth_capability = capabilities.get("auth")
        if (
            self.board is None
            and isinstance(auth_capability, Mapping)
            and auth_capability.get("terminal") is True
        ):
            auth_methods.append(
                {
                    "id": "pursers-personal-login",
                    "name": "Configure Pursers Personal",
                    "description": "Run the local Pursers Personal setup flow",
                    "type": "terminal",
                    "args": ["--login"],
                }
            )
        await self._result(
            request_id,
            {
                "protocolVersion": ACP_VERSION if requested == ACP_VERSION else ACP_VERSION,
                "agentCapabilities": {"promptCapabilities": {}},
                "agentInfo": {
                    "name": "pursers-acp",
                    "title": "Pursers Board Assistant",
                    "version": IMPLEMENTATION_VERSION,
                },
                "authMethods": auth_methods,
            },
        )

    async def _authenticate(self, request_id: Any, params: JSON) -> None:
        if params.get("methodId") != "pursers-personal-profile":
            await self._error(request_id, -32602, "unknown authentication method")
            return
        if self.board is None:
            try:
                if self.auth_setup is None:
                    raise AuthRequired("Personal setup is unavailable")
                configured = self.auth_setup()
                if inspect.isawaitable(configured):
                    await configured
                board = self.board_factory()
                self.board = await board if inspect.isawaitable(board) else board
                self.auth_error = None
            except AuthRequired as exc:
                self.auth_error = _bounded(str(exc), 500)
                await self._error(
                    request_id,
                    -32000,
                    "auth_required: configure a Pursers Personal profile",
                )
                return
        await self._result(request_id, {})

    async def _new_session(self, request_id: Any, params: JSON) -> None:
        if not self.initialized:
            await self._error(request_id, -32002, "initialize must be called first")
            return
        if self.board is None:
            await self._error(
                request_id,
                -32000,
                "auth_required: configure a Pursers Personal profile",
                {"detail": self.auth_error or "profile unavailable"},
            )
            return
        cwd = params.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            await self._error(request_id, -32602, "cwd must be absolute")
            return
        try:
            mcp_stop, mcp_task = await _start_stdio_mcp_servers(
                params.get("mcpServers"), cwd
            )
        except ValueError as exc:
            await self._error(request_id, -32602, str(exc))
            return
        except RuntimeError as exc:
            await self._error(request_id, -32001, str(exc))
            return
        session_id = f"pursers-{uuid.uuid4().hex}"
        self.sessions[session_id] = Session(
            cwd=cwd, mcp_stop=mcp_stop, mcp_task=mcp_task
        )
        await self._update(
            session_id,
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": _available_commands(),
            },
        )
        await self._result(request_id, {"sessionId": session_id})

    async def _prompt(self, request_id: Any, params: JSON) -> None:
        session_id = params.get("sessionId")
        session = self.sessions.get(session_id)
        if session is None:
            await self._error(request_id, -32602, "unknown session")
            return
        if session.prompt_request_id != request_id:
            await self._error(request_id, -32600, "session prompt is not reserved")
            return
        session.active = True
        try:
            try:
                text = _prompt_text(params.get("prompt"))
            except ValueError as exc:
                await self._error(request_id, -32602, str(exc))
                return
            try:
                stop = await self._dispatch(session_id, session, text)
            except PromptCancelled:
                stop = "cancelled"
            except Exception as exc:
                await self._message(
                    session_id,
                    "Pursers could not complete this command. "
                    f"Check the selected Personal profile and board access: {_bounded(str(exc), 500)}",
                )
                stop = "end_turn"
            if session.cancel.is_set():
                stop = "cancelled"
            await self._result(request_id, {"stopReason": stop})
        finally:
            session.active = False
            if session.prompt_request_id == request_id:
                session.prompt_request_id = None
                session.cancel = asyncio.Event()

    async def _run_cancelable(
        self, session: Session, operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        if session.cancel.is_set():
            raise PromptCancelled
        work = asyncio.ensure_future(operation())
        stopped = asyncio.create_task(session.cancel.wait())
        try:
            done, _pending = await asyncio.wait(
                {work, stopped}, return_when=asyncio.FIRST_COMPLETED
            )
            if stopped in done:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                raise PromptCancelled
            return work.result()
        except BaseException:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            raise
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)

    async def _dispatch(self, session_id: str, session: Session, text: str) -> str:
        assert self.board is not None
        normalized = " ".join(text.strip().split())
        lowered = normalized.casefold()
        if lowered == "my tickets":
            rows = await self._run_cancelable(session, self.board.my_tickets)
            await self._message(session_id, _format_tickets(rows))
            return "end_turn"
        if lowered == "my offers":
            rows = await self._run_cancelable(session, self.board.my_offers)
            await self._message(session_id, _format_offers(rows))
            return "end_turn"
        if lowered in {"board status", "/board"}:
            status, tickets, offers = await self._run_cancelable(
                session,
                lambda: asyncio.gather(
                    self.board.board_status(),
                    self.board.my_tickets(),
                    self.board.my_offers(),
                ),
            )
            await self._message(session_id, _format_board(status, tickets, offers))
            return "end_turn"
        if lowered.startswith(("create ticket ", "/create ")):
            offset = 14 if lowered.startswith("create ticket ") else 8
            title, description = _parse_create(normalized[offset:])
            action = self.board.create_action(title, description)
            return await self._mutation(session_id, session, action)
        if lowered.startswith("annotate "):
            ticket_id, separator, annotation = normalized[9:].partition(" ")
            if not separator or not ticket_id.startswith("TK-") or not annotation:
                await self._message(session_id, "Usage: annotate TK-… <text>")
                return "end_turn"
            action = self.board.annotate_action(ticket_id, annotation)
            return await self._mutation(session_id, session, action)
        if lowered.startswith("/evidence "):
            ticket_id = normalized[10:].strip()
            if not ticket_id.startswith("TK-") or " " in ticket_id:
                await self._message(session_id, "Usage: /evidence TK-…")
                return "end_turn"
            evidence = await self._run_cancelable(
                session, lambda: self.board.ticket_evidence(ticket_id)
            )
            await self._message(session_id, _format_evidence(evidence))
            return "end_turn"
        if lowered == "/answer":
            return await self._answer(session_id, session, "")
        if lowered.startswith("/answer "):
            return await self._answer(session_id, session, normalized[8:])
        if lowered in {"/watch", "watch"} or lowered.startswith(
            ("/watch ", "watch ")
        ):
            if lowered in {"/watch", "watch"}:
                board_id = self.board.board_id
            else:
                board_id = normalized.split(" ", 1)[1].strip()
            if board_id != self.board.board_id:
                await self._message(
                    session_id,
                    f"This profile is bound to {self.board.board_id}; refusing cross-board watch.",
                )
                return "refusal"
            plan_tickets: dict[str, JSON] = {}
            snapshot_truncated = False
            await self._plan(session_id, board_id, "in_progress")
            try:
                async with aclosing(
                    self.board.watch(session.cursors.get(board_id), session.cancel)
                ) as events:
                    async for cursor, event in events:
                        session.cursors[board_id] = cursor
                        question = None
                        if event.get("kind") == "coordinator_question_asked":
                            ticket_id = event.get("ticket_id")
                            if isinstance(ticket_id, str):
                                evidence = await self._run_cancelable(
                                    session,
                                    lambda: self.board.ticket_evidence(ticket_id),
                                )
                                question = _question_from_event(evidence, event)
                        if question is not None:
                            question_id = str(question["question_id"])
                            if question_id in session.pending_questions:
                                continue
                            question["reply_ref"] = session.next_question_ref
                            session.next_question_ref += 1
                            session.pending_questions[question_id] = question
                            await self._message(
                                session_id, _format_question_update(question)
                            )
                            # Completing the prompt is what makes Zed surface its
                            # normal turn-completion notification. A free-standing
                            # session/update alone does not wake the human.
                            break
                        if event.get("kind") == "watch_snapshot":
                            plan_tickets = {
                                item["ticket_id"]: item
                                for row in event.get("tickets", [])
                                if isinstance(row, Mapping)
                                and (item := _plan_ticket(row)) is not None
                            }
                            snapshot_truncated = event.get("truncated") is True
                        else:
                            ticket_id = event.get("ticket_id")
                            prior = (
                                plan_tickets.get(ticket_id)
                                if isinstance(ticket_id, str)
                                else None
                            )
                            item = _plan_ticket(event, prior=prior)
                            if item is not None:
                                plan_tickets[item["ticket_id"]] = item
                            elif isinstance(ticket_id, str):
                                plan_tickets.pop(ticket_id, None)
                            await self._message(session_id, _format_event(event))
                        await self._plan(
                            session_id,
                            board_id,
                            "in_progress",
                            tickets=plan_tickets,
                            snapshot_truncated=snapshot_truncated,
                        )
                        if _event_ends_watch(event):
                            break
                        if session.cancel.is_set():
                            break
            finally:
                await self._plan(session_id, board_id, "completed")
            return "cancelled" if session.cancel.is_set() else "end_turn"
        if session.pending_questions and not normalized.startswith("/"):
            if len(session.pending_questions) == 1:
                return await self._answer_pending(
                    session_id,
                    session,
                    next(iter(session.pending_questions.values())),
                )
            await self._message(session_id, _pending_question_help(session))
            return "end_turn"
        await self._message(session_id, _help())
        return "end_turn"

    async def _answer(self, session_id: str, session: Session, value: str) -> str:
        assert self.board is not None
        target, separator, answer = value.partition(" ")
        if target.startswith("TK-"):
            if not separator or not answer:
                await self._message(session_id, "Usage: /answer TK-… <JSON-or-text>")
                return "end_turn"
            action = await self._run_cancelable(
                session, lambda: self.board.answer_action(target, answer)
            )
            return await self._mutation(session_id, session, action)
        if target.startswith("#"):
            if separator or answer or not target[1:].isdigit():
                await self._message(session_id, _pending_question_help(session))
                return "end_turn"
            selected = next(
                (
                    question
                    for question in session.pending_questions.values()
                    if question.get("reply_ref") == int(target[1:])
                ),
                None,
            )
            if selected is None:
                await self._message(session_id, _pending_question_help(session))
                return "end_turn"
            return await self._answer_pending(session_id, session, selected)
        if len(session.pending_questions) == 1:
            return await self._answer_pending(
                session_id,
                session,
                next(iter(session.pending_questions.values())),
            )
        if session.pending_questions:
            await self._message(session_id, _pending_question_help(session))
            return "end_turn"
        await self._message(session_id, "Usage: /answer TK-… <JSON-or-text>")
        return "end_turn"

    async def _answer_pending(
        self, session_id: str, session: Session, question: JSON
    ) -> str:
        assert self.board is not None
        response = await self._elicit_question_answer(session_id, session, question)
        if response is None:
            return "cancelled" if session.cancel.is_set() else "end_turn"
        action_name = response.get("action")
        if action_name == "decline":
            await self._message(
                session_id,
                "You declined the answer form. The seat question remains unanswered.",
            )
            return "end_turn"
        if action_name == "cancel":
            await self._message(
                session_id,
                "The answer form was cancelled. The seat question remains unanswered.",
            )
            return "end_turn"
        if action_name != "accept":
            await self._message(
                session_id,
                "The answer form returned an unsupported action. "
                "The seat question remains unanswered.",
            )
            return "end_turn"
        content = response.get("content")
        answer = _elicited_answer_text(question, content)
        if answer is None:
            await self._message(
                session_id,
                "The accepted answer form was incomplete. "
                "The seat question remains unanswered.",
            )
            return "end_turn"
        question_id = str(question["question_id"])
        action = self.board.question_answer_action(
            str(question["ticket_id"]), question_id, answer
        )
        return await self._mutation(
            session_id,
            session,
            action,
            on_completed=lambda: session.pending_questions.pop(question_id, None),
        )

    async def _elicit_question_answer(
        self, session_id: str, session: Session, question: JSON
    ) -> JSON | None:
        elicitation = self.client_capabilities.get("elicitation")
        if not (
            isinstance(elicitation, Mapping)
            and isinstance(elicitation.get("form"), Mapping)
        ):
            await self._message(
                session_id,
                "This ACP client does not advertise form elicitation. "
                "The seat question remains unanswered.",
            )
            return None
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "elicitation/create",
                "params": {
                    "sessionId": session_id,
                    "mode": "form",
                    "message": (
                        f"Answer seat {question.get('asked_by_name', '(unknown)')} "
                        f"on {question.get('ticket_id', '?')}: "
                        f"{_bounded(str(question.get('message') or '(empty)'), 2_000)}"
                    ),
                    "requestedSchema": _question_answer_schema(question),
                },
            }
        )
        stopped = asyncio.create_task(session.cancel.wait())
        done, pending = await asyncio.wait(
            {future, stopped}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            if task is not future:
                task.cancel()
        await asyncio.gather(
            *(task for task in pending if task is not future),
            return_exceptions=True,
        )
        if stopped in done:
            self.pending.pop(request_id, None)
            future.cancel()
            return None
        response = future.result()
        return response if isinstance(response, dict) else {}

    async def _mutation(
        self,
        session_id: str,
        session: Session,
        action: JSON,
        *,
        on_completed: Callable[[], object] | None = None,
    ) -> str:
        tool_call_id = f"board-{uuid.uuid4().hex}"
        title = _mutation_title(action)
        public_action = _public_action(action)
        await self._update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_call_id,
                "title": title,
                "kind": "edit",
                "status": "pending",
                "rawInput": public_action,
            },
        )
        allowed = await self._permission(
            session_id, session, tool_call_id, public_action
        )
        if not allowed:
            await self._update(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_call_id,
                    "status": "failed",
                    "content": [_content("Board write was not approved.")],
                },
            )
            return "cancelled" if session.cancel.is_set() else "end_turn"
        await self._update(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "status": "in_progress",
            },
        )
        assert self.board is not None
        try:
            result = await self._run_cancelable(
                session, lambda: self.board.mutate(action)
            )
        except PromptCancelled:
            await self._update(
                session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": tool_call_id,
                    "status": "failed",
                    "content": [_content("Board write was cancelled.")],
                },
            )
            raise
        summary = _mutation_summary(action, result)
        if on_completed is not None:
            on_completed()
        locations = _result_locations(result, Path(session.cwd))
        completed: JSON = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": tool_call_id,
            "status": "completed",
            "content": [_content(summary)],
        }
        if locations:
            completed["locations"] = locations
        await self._update(
            session_id,
            completed,
        )
        await self._message(session_id, summary)
        return "end_turn"

    async def _permission(
        self, session_id: str, session: Session, tool_call_id: str, action: JSON
    ) -> bool:
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": {
                        "toolCallId": tool_call_id,
                        "title": _mutation_title(action),
                        "kind": "edit",
                        "rawInput": action,
                    },
                    "options": [
                        {
                            "optionId": "allow-once",
                            "name": "Allow once",
                            "kind": "allow_once",
                        },
                        {
                            "optionId": "reject-once",
                            "name": "Reject",
                            "kind": "reject_once",
                        },
                    ],
                },
            }
        )
        stopped = asyncio.create_task(session.cancel.wait())
        done, pending = await asyncio.wait(
            {future, stopped}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            if task is not future:
                task.cancel()
        await asyncio.gather(*(task for task in pending if task is not future), return_exceptions=True)
        if stopped in done:
            self.pending.pop(request_id, None)
            future.cancel()
            return False
        outcome = future.result()
        return (
            isinstance(outcome, dict)
            and isinstance(outcome.get("outcome"), dict)
            and outcome["outcome"].get("outcome") == "selected"
            and outcome["outcome"].get("optionId") == "allow-once"
        )

    async def _message(self, session_id: str, text: str) -> None:
        await self._update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": f"msg-{uuid.uuid4().hex}",
                "content": {"type": "text", "text": _bounded(text, MAX_TEXT_CHARS)},
            },
        )

    async def _plan(
        self,
        session_id: str,
        board_id: str,
        status: str,
        *,
        tickets: Mapping[str, JSON] | None = None,
        snapshot_truncated: bool = False,
    ) -> None:
        entries = (
            _plan_entries(tickets, snapshot_truncated=snapshot_truncated)
            if status == "in_progress" and (tickets or snapshot_truncated)
            else []
        )
        if not entries:
            entries = [
                {
                    "content": f"{board_id} is quiet — no active work needs attention",
                    "priority": "low",
                    "status": status,
                }
            ]
        await self._update(
            session_id,
            {
                "sessionUpdate": "plan",
                "entries": entries,
            },
        )

    async def _update(self, session_id: str, update: JSON) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": session_id, "update": update},
            }
        )

    async def _send(self, payload: JSON) -> None:
        if self.send is None:
            raise RuntimeError("agent output is unavailable")
        async with self.write_lock:
            await self.send(payload)

    async def _result(self, request_id: Any, result: Any) -> None:
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _error(
        self, request_id: Any, code: int, message: str, data: Any = None
    ) -> None:
        error: JSON = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self._send({"jsonrpc": "2.0", "id": request_id, "error": error})


def _prompt_text(prompt: Any) -> str:
    if not isinstance(prompt, list) or not prompt:
        raise ValueError("prompt must be a non-empty content block array")
    chunks: list[str] = []
    for block in prompt:
        if not isinstance(block, dict):
            raise ValueError("prompt content blocks must be objects")
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            chunks.append(block["text"])
        elif kind == "resource_link":
            continue
        else:
            raise ValueError("pursers-acp accepts only text and resource_link prompts")
    text = "\n".join(chunks).strip()
    if not text or len(text) > MAX_TEXT_CHARS:
        raise ValueError("prompt text is empty or exceeds the 8000-character limit")
    return text


def _parse_create(value: str) -> tuple[str, str]:
    for separator in (" :: ", " / "):
        title, found, description = value.partition(separator)
        if found and title.strip() and description.strip():
            if len(title.strip()) > 200 or len(description.strip()) > 5_000:
                raise ValueError("ticket title or description exceeds Central limits")
            return title.strip(), description.strip()
    raise ValueError("Usage: create ticket <title> :: <description>")


def _parse_answer_content(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _digest_cursor(page: Mapping[str, Any], board_id: str, prior: int | None) -> int:
    raw = page.get("cursor_map")
    value = raw.get(board_id) if isinstance(raw, Mapping) else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return max(prior or 0, value)
    return prior or 0


def _plan_ticket(
    ticket: Mapping[str, Any], *, prior: Mapping[str, Any] | None = None
) -> JSON | None:
    ticket_id = ticket.get("ticket_id")
    if not isinstance(ticket_id, str) or not ticket_id:
        return None
    raw_status = ticket.get("status_to") or ticket.get("status_now") or ticket.get(
        "status"
    )
    if not isinstance(raw_status, str) or raw_status not in IN_FLIGHT_TICKET_STATES:
        return None

    dispatch = ticket.get("dispatch_state")
    dispatch_state = dispatch.get("state") if isinstance(dispatch, Mapping) else None
    review_state = ticket.get("review_state")
    reviewing = bool(
        raw_status in {"reviewing", "in_review"}
        or dispatch_state in REVIEW_DISPATCH_STATES
        or isinstance(ticket.get("review_lease"), Mapping)
        or review_state in {"claimed", "claimed_by_me", "claimed_by_other", "reviewing"}
    )
    phase = (
        "needs_human"
        if raw_status == "needs_human"
        else "review"
        if reviewing
        else "submitted"
        if raw_status == "submitted"
        else "work"
    )
    title = ticket.get("title")
    if not (isinstance(title, str) and title.strip()) and prior is not None:
        title = prior.get("title")
    safe_title = _bounded(
        title.strip() if isinstance(title, str) and title.strip() else "(untitled)",
        MAX_PLAN_TITLE_CHARS,
    )
    project = _ticket_project(ticket)
    if project == "(unassigned)" and prior is not None:
        prior_project = prior.get("project")
        if isinstance(prior_project, str) and prior_project:
            project = prior_project
    review_offer = ticket.get("review_offer")
    if not isinstance(review_offer, Mapping):
        offers = ticket.get("offers")
        review_offer = (
            offers.get("review_offer") if isinstance(offers, Mapping) else None
        )
    offered_reviewer_name = (
        review_offer.get("agent_name") if isinstance(review_offer, Mapping) else None
    )
    offered_reviewer_id = (
        review_offer.get("agent_id") if isinstance(review_offer, Mapping) else None
    )
    active_reviewer_id: Any = None
    holder: Any = None
    if phase == "needs_human":
        request = ticket.get("human_request")
        asked_by = request.get("asked_by") if isinstance(request, Mapping) else None
        if isinstance(asked_by, Mapping):
            holder = asked_by.get("agent_name")
        holder = holder or ticket.get("last_claimed_by")
    elif phase == "review":
        lease = ticket.get("review_lease")
        if isinstance(lease, Mapping):
            holder = lease.get("reviewer_agent_name")
            active_reviewer_id = lease.get("reviewer_agent_id")
        holder = holder or ticket.get("review_claimed_by")
        dispatch = ticket.get("dispatch_state")
        if isinstance(dispatch, Mapping):
            holder = holder or dispatch.get("agent_name")
            active_reviewer_id = active_reviewer_id or dispatch.get("agent_id")
        # board_digest intentionally omits review_lease and exposes only the
        # active reviewer's agent_id. Retain the human name from the preceding
        # review offer, but only when its ID matches the new active holder.
        if (
            not (isinstance(holder, str) and holder.strip())
            and isinstance(active_reviewer_id, str)
            and prior is not None
            and prior.get("reviewer_agent_id") == active_reviewer_id
        ):
            holder = prior.get("reviewer_name")
    else:
        holder = ticket.get("claimed_by") or ticket.get("assigned_to")
    if not (isinstance(holder, str) and holder.strip()):
        if prior is not None and prior.get("phase") == phase:
            holder = prior.get("holder")
    safe_holder = (
        _bounded(holder.strip(), 100)
        if isinstance(holder, str) and holder.strip()
        else None
    )
    return {
        "ticket_id": ticket_id,
        "title": safe_title,
        "project": project,
        "phase": phase,
        "holder": safe_holder,
        "reviewer_name": (
            safe_holder
            if phase == "review"
            else _bounded(offered_reviewer_name.strip(), 100)
            if isinstance(offered_reviewer_name, str)
            and offered_reviewer_name.strip()
            else None
        ),
        "reviewer_agent_id": (
            active_reviewer_id if phase == "review" else offered_reviewer_id
        ),
    }


def _plan_content(item: Mapping[str, Any]) -> str:
    phase = item["phase"]
    holder = item.get("holder")
    if phase == "needs_human":
        detail = f"Needs you · asked by {holder}" if holder else "Needs you"
    elif phase == "review":
        detail = f"In review · {holder}" if holder else "In review · reviewer not reported"
    elif phase == "work":
        detail = f"Working · {holder}" if holder else "Working · holder not reported"
    else:
        detail = (
            f"Awaiting reviewer · submitted by {holder}"
            if holder
            else "Awaiting reviewer"
        )
    return _bounded(
        f"{item.get('project', '(unassigned)')} · "
        f"{item['ticket_id']} — {item['title']} · {detail}",
        MAX_TEXT_CHARS,
    )


def _plan_entries(
    tickets: Mapping[str, JSON], *, snapshot_truncated: bool = False
) -> list[JSON]:
    phase_order = {"needs_human": 0, "review": 1, "work": 2, "submitted": 3}
    rows = sorted(
        tickets.values(),
        key=lambda item: (
            phase_order[item["phase"]],
            str(item.get("project", "(unassigned)")).casefold(),
            item["ticket_id"],
        ),
    )
    needs_overflow = len(rows) > MAX_PLAN_ENTRIES or snapshot_truncated
    visible_limit = MAX_PLAN_ENTRIES - 1 if needs_overflow else MAX_PLAN_ENTRIES
    entries: list[JSON] = []
    for item in rows[:visible_limit]:
        # ACP v1 has pending/in_progress/completed but no review status. A
        # submitted ticket is pending review; active review stays in_progress
        # and is never reported completed before the board closes it.
        status = (
            "pending"
            if item["phase"] in {"needs_human", "submitted"}
            else "in_progress"
        )
        # Priority describes required attention: high needs the human, medium
        # is active work/review, and low is deliberate queueing or summary.
        priority = (
            "high"
            if item["phase"] == "needs_human"
            else "low"
            if item["phase"] == "submitted"
            else "medium"
        )
        entries.append(
            {
                "content": _plan_content(item),
                "priority": priority,
                "status": status,
            }
        )

    if needs_overflow:
        known_omitted = max(0, len(rows) - visible_limit)
        if snapshot_truncated:
            content = (
                f"{known_omitted} more known in-flight; additional active tickets "
                f"are outside the {WATCH_SNAPSHOT_LIMIT}-ticket snapshot"
            )
        else:
            content = (
                f"{known_omitted} more in-flight tickets "
                f"({MAX_PLAN_ENTRIES}-entry plan cap)"
            )
        entries.append(
            {"content": content, "priority": "low", "status": "in_progress"}
        )
    return entries


def _mutation_title(action: Mapping[str, Any]) -> str:
    operation = action.get("operation")
    board_id = str(action.get("board_id") or "board")
    params = action.get("params")
    ticket_id = params.get("ticket_id") if isinstance(params, Mapping) else None
    if operation == "ticket_create":
        return f"Create ticket on {board_id}"
    if operation == "ticket_annotate":
        return f"Add note to {ticket_id or board_id}"
    if operation == "ticket_human_resolve":
        return f"Answer request on {ticket_id or board_id}"
    return f"Update {board_id}"


def _result_locations(result: Mapping[str, Any], cwd: Path) -> list[JSON]:
    ticket = result.get("ticket")
    if not isinstance(ticket, Mapping):
        return []
    raw_paths: list[Any] = []
    for key in ("files_changed", "related_files"):
        values = ticket.get(key)
        if isinstance(values, list):
            raw_paths.extend(values)
    root = cwd.resolve()
    locations: list[JSON] = []
    seen: set[Path] = set()
    for value in raw_paths:
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = (
            (root / value).resolve()
            if not Path(value).is_absolute()
            else Path(value).resolve()
        )
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        locations.append({"path": str(candidate), "line": 1})
    return locations[:MAX_ROWS]


def _content(text: str) -> JSON:
    return {"type": "content", "content": {"type": "text", "text": text}}


def _public_action(action: JSON) -> JSON:
    public = dict(action)
    params = action.get("params")
    if isinstance(params, dict):
        public["params"] = {
            key: value for key, value in params.items() if key != "host_binding"
        }
    return public


def _format_tickets(rows: list[JSON]) -> str:
    if not rows:
        return "No active tickets are associated with this Personal principal."
    grouped: dict[str, list[JSON]] = {}
    for row in rows[:MAX_ROWS]:
        grouped.setdefault(_ticket_project(row), []).append(row)
    lines = [
        "My Pursers tickets by project "
        f"(showing {len(rows[:MAX_ROWS])}, limit {MAX_ROWS})"
    ]
    for project in sorted(grouped, key=str.casefold):
        lines.append(f"{project}")
        for row in grouped[project]:
            lines.append(
                f"- {row.get('ticket_id', '?')} · "
                f"{_ticket_state_label(row)} · {_ticket_owner_label(row)} — "
                f"{_bounded(str(row.get('title', '(untitled)')), 200)}"
            )
    return "\n".join(lines)


def _format_offers(rows: list[JSON]) -> str:
    if not rows:
        return "No current offers for identities owned by this Personal principal."
    lines = ["My offers"]
    for row in rows[:MAX_ROWS]:
        lines.append(
            f"- {row.get('ticket_id', '?')} -> {row.get('agent_name', '?')} "
            f"({row.get('kind', 'work')})"
        )
    return "\n".join(lines)


def _format_board(status: JSON, tickets: list[JSON], offers: list[JSON]) -> str:
    needs_answer = sum(1 for ticket in tickets if _ticket_needs_answer(ticket))
    lines = [
        "Pursers board overview",
        "",
        f"Review policy: {status.get('review_policy', '?')}",
        f"Latest event: {status.get('latest_seq', '?')}",
        f"Associated tickets shown: {len(tickets)} (limit {MAX_ROWS})",
        f"Offers shown: {len(offers)} (limit {MAX_ROWS})",
        f"Needs your answer: {needs_answer}",
    ]
    projects = status.get("projects")
    if isinstance(projects, list) and projects:
        total_projects = status.get("project_count", len(projects))
        heading = f"Pursers projects (showing {len(projects)} of {total_projects})"
        lines.extend(["", heading])
        for project in projects:
            if not isinstance(project, Mapping):
                continue
            counts = project.get("ticket_counts")
            if not isinstance(counts, Mapping):
                counts = {}
            count_text = ", ".join(
                f"{count} {str(ticket_status).replace('_', ' ')}"
                for ticket_status, count in sorted(counts.items())
            ) or "no active tickets"
            lines.append(
                f"- {project.get('name', '(unassigned)')} "
                f"[{project.get('status', 'observed')}] — {count_text}"
            )
        if status.get("project_ticket_scan_limited"):
            lines.append("Ticket counts cover the first 100 active board tickets.")
    if tickets:
        lines.extend(["", _format_tickets(tickets)])
    if offers:
        lines.extend(["", _format_offers(offers)])
    return "\n".join(lines)


def _format_evidence(result: JSON) -> str:
    ticket = result.get("ticket")
    if not isinstance(ticket, Mapping):
        raise TypeError("ticket evidence response did not contain a ticket")
    lines = [
        f"Evidence for {ticket.get('ticket_id', '?')}",
        "",
        f"Status: {ticket.get('status', '?')}",
        f"Summary: {_bounded(str(ticket.get('summary') or '(none)'), 500)}",
        f"Files: {json.dumps(ticket.get('files_changed') or [], sort_keys=True)}",
        f"Review: {ticket.get('review_verdict') or 'not reviewed'}",
    ]
    notes = ticket.get("notes")
    if isinstance(notes, str):
        selected = [
            line
            for line in notes.splitlines()
            if line.startswith(
                (
                    "branch_and_commit:",
                    "files_changed:",
                    "test_output:",
                    "test-output:",
                )
            )
        ][:12]
        if selected:
            lines.extend(["", "Submitted evidence", *selected])
    return "\n".join(lines)


def _format_event(event: JSON) -> str:
    if event.get("kind") == "question_inbox_unavailable":
        return _bounded(str(event.get("message") or QUESTION_INBOX_UNAVAILABLE), 500)
    kind = str(event.get("kind", "board update")).replace("_", " ").capitalize()
    lines = ["Pursers board update", "", kind]
    if event.get("project") is not None:
        lines.append(f"Project: {_bounded(str(event['project']), 100)}")
    if event.get("ticket_id") is not None:
        lines.append(f"Ticket: {_bounded(str(event['ticket_id']), 100)}")
    if event.get("status_to") is not None:
        lines.append(
            f"Now: {_bounded(str(event['status_to']).replace('_', ' '), 100).capitalize()}"
        )
    if event.get("seq") is not None:
        lines.append(f"Board event: {_bounded(str(event['seq']), 40)}")
    return "\n".join(lines)


def _ticket_needs_answer(ticket: Mapping[str, Any]) -> bool:
    request = ticket.get("human_request")
    return ticket.get("status") == "needs_human" or (
        isinstance(request, Mapping) and request.get("resolution") is None
    )


def _ticket_state_label(ticket: Mapping[str, Any]) -> str:
    status = str(ticket.get("status", "unknown")).replace("_", " ")
    return status.capitalize()


def _ticket_owner_label(ticket: Mapping[str, Any]) -> str:
    if _ticket_needs_answer(ticket):
        return "Needs your answer"
    if ticket.get("status") == "submitted":
        return "Awaiting review"
    claimed_by = ticket.get("claimed_by")
    if isinstance(claimed_by, str) and claimed_by:
        return f"Held by {_bounded(claimed_by, 80)}"
    assigned_to = ticket.get("assigned_to")
    if isinstance(assigned_to, str) and assigned_to:
        return f"Assigned to {_bounded(assigned_to, 80)}"
    return "Unassigned"


def _ticket_project(ticket: Mapping[str, Any]) -> str:
    project = ticket.get("project")
    if isinstance(project, str) and project.strip():
        return project.strip()
    target = ticket.get("target_url")
    if isinstance(target, str) and target and "://" not in target:
        candidate = target.split("/", 1)[0].strip()
        if candidate:
            return candidate
    return "(unassigned)"


def _project_summaries(
    board_id: str, tickets: Any, registry: JSON | None
) -> list[JSON]:
    projects: dict[str, JSON] = {}
    if isinstance(registry, Mapping):
        registry_projects = registry.get("projects")
        if isinstance(registry_projects, Mapping):
            for name, project in registry_projects.items():
                if (
                    isinstance(name, str)
                    and isinstance(project, Mapping)
                    and project.get("board_id") == board_id
                ):
                    projects[name] = {
                        "name": name,
                        "status": project.get("status", "observed"),
                        "ticket_counts": {},
                    }
    if isinstance(tickets, list):
        for ticket in tickets:
            if not isinstance(ticket, Mapping):
                continue
            name = _ticket_project(ticket)
            project = projects.setdefault(
                name,
                {"name": name, "status": "observed", "ticket_counts": {}},
            )
            ticket_status = str(ticket.get("status", "unknown"))
            counts = project["ticket_counts"]
            counts[ticket_status] = counts.get(ticket_status, 0) + 1
    return sorted(projects.values(), key=lambda project: project["name"].casefold())


def _event_ends_watch(event: JSON) -> bool:
    kind = event.get("kind")
    return (
        event.get("review_verdict") is not None
        or event.get("status_to") in {"failed", "rejected"}
        or (isinstance(kind, str) and "failed" in kind)
    )


def _question_inbox_authority_error(exc: BoardClientError) -> bool:
    message = str(exc).casefold()
    return (
        "question inbox requires role coordinator" in message
        or "coordinator inbox requires registered project coordinator ownership"
        in message
    )


def _question_from_event(result: JSON, event: JSON) -> JSON | None:
    ticket = result.get("ticket")
    if not isinstance(ticket, Mapping):
        return None
    question_id = event.get("question_id")
    questions = ticket.get("coordinator_questions")
    if not isinstance(question_id, str) or not isinstance(questions, list):
        return None
    for raw in questions:
        if (
            isinstance(raw, Mapping)
            and raw.get("question_id") == question_id
            and raw.get("state") != "answered"
        ):
            question = dict(raw)
            question["ticket_id"] = event.get("ticket_id")
            required_fields = ticket.get("required_fields")
            question["required_fields"] = (
                [
                    field
                    for field in required_fields
                    if isinstance(field, str) and field and len(field) <= 100
                ][:MAX_ROWS]
                if isinstance(required_fields, list)
                else []
            )
            asked_by = question.get("asked_by")
            question["asked_by_name"] = (
                asked_by.get("agent_name")
                if isinstance(asked_by, Mapping)
                else None
            )
            return question
    return None


def _question_answer_schema(question: JSON) -> JSON:
    message = _bounded(str(question.get("message") or "(empty)"), 2_000)
    properties: JSON = {
        "answer": {
            "type": "string",
            "title": "Answer",
            "description": message,
            "minLength": 1,
            "maxLength": MAX_TEXT_CHARS,
        }
    }
    required = ["answer"]
    for field in question.get("required_fields", []):
        if not isinstance(field, str) or field in properties:
            continue
        properties[field] = {
            "type": "string",
            "title": field.replace("_", " ").strip().title() or field,
            "description": f"Ticket required field: {field}",
            "maxLength": 2_000,
        }
        required.append(field)
    return {
        "type": "object",
        "title": f"Seat question on {question.get('ticket_id', '?')}",
        "description": (
            f"From {question.get('asked_by_name') or '(unknown)'}; "
            "accept to continue to board-write permission, or decline/cancel "
            "to leave the board unchanged."
        ),
        "properties": properties,
        "required": required,
    }


def _elicited_answer_text(question: JSON, content: Any) -> str | None:
    if not isinstance(content, Mapping):
        return None
    required = ["answer", *question.get("required_fields", [])]
    selected: JSON = {}
    for field in required:
        value = content.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        selected[field] = _bounded(value.strip(), MAX_TEXT_CHARS)
    if len(selected) == 1:
        return str(selected["answer"])
    lines = [str(selected.pop("answer"))]
    lines.extend(f"{field}: {value}" for field, value in selected.items())
    return _bounded("\n\n".join(lines), MAX_TEXT_CHARS)


def _waiting_duration(asked_at: Any) -> str:
    if not isinstance(asked_at, str):
        return "an unknown time"
    try:
        asked = datetime.fromisoformat(asked_at.replace("Z", "+00:00"))
        if asked.tzinfo is None:
            asked = asked.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - asked).total_seconds()))
    except ValueError:
        return "an unknown time"
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _format_question_update(question: JSON) -> str:
    asked_by = question.get("asked_by")
    seat = asked_by.get("agent_name") if isinstance(asked_by, Mapping) else None
    reference = question.get("reply_ref", "?")
    return "\n".join(
        [
            "SEAT QUESTION — ANSWER NEEDED",
            f"Seat: {seat or '(unknown)'}",
            f"Ticket: {question.get('ticket_id', '?')}",
            f"Waiting: {_waiting_duration(question.get('asked_at'))}",
            f"Question: {_bounded(str(question.get('message') or '(empty)'), 2_000)}",
            "",
            "This watch turn is ending now so Zed can notify you. Reply /answer "
            "in this thread to open a form. With several pending questions, use "
            f"/answer #{reference}. Accepting the form still requires board-write permission.",
        ]
    )


def _pending_question_help(session: Session) -> str:
    lines = [
        "More than one seat question is pending. Choose one; Pursers will not guess:"
    ]
    for question in session.pending_questions.values():
        asked_by = question.get("asked_by")
        seat = asked_by.get("agent_name") if isinstance(asked_by, Mapping) else None
        lines.append(
            f"- #{question.get('reply_ref', '?')} {question.get('ticket_id', '?')} "
            f"from {seat or '(unknown)'}: "
            f"{_bounded(str(question.get('message') or '(empty)'), 200)}"
        )
    lines.append("Reply with /answer #N to open that question's answer form.")
    return "\n".join(lines)


def _mutation_summary(action: JSON, result: JSON) -> str:
    if action.get("operation") == "ticket_create":
        ticket = result.get("ticket", {})
        return f"Created ticket {ticket.get('ticket_id', '(unknown)')}."
    if action.get("operation") == "ticket_human_resolve":
        return f"Answered the pending request on {action['params']['ticket_id']}."
    if action.get("operation") == "ticket_question_answer":
        return f"Answered the seat question on {action['params']['ticket_id']}."
    annotation = result.get("annotation", {})
    return f"Added annotation {annotation.get('annotation_id', '(recorded)')}."


def _available_commands() -> list[JSON]:
    return [
        {
            "name": "board",
            "description": "Show this Personal profile's board summary and work",
        },
        {
            "name": "create",
            "description": "Preview and create a ticket after native permission",
            "input": {"hint": "Title :: Description"},
        },
        {
            "name": "watch",
            "description": "Watch until an important update ends this turn",
        },
        {
            "name": "evidence",
            "description": "Show bounded submission and review evidence for a ticket",
            "input": {"hint": "TK-…"},
        },
        {
            "name": "answer",
            "description": "Answer a human request or open a watched-question form",
            "input": {"hint": "TK-… <JSON-or-text> or [#N]"},
        },
    ]


def _help() -> str:
    return (
        "Pursers board commands:\n"
        "- /board\n- /create <title> :: <description>\n"
        "- /watch (ends after a seat question)\n- /evidence TK-…\n"
        "- /answer TK-… <JSON-or-text>\n"
        "- /answer [#N] (opens a form for a watched seat question)\n\n"
        "Legacy text commands remain supported: my tickets, my offers, board status, "
        "create ticket, annotate, and watch (optionally followed by this board)."
    )


def _wait_bridge_environment(
    profile: Any, token: str, identity: Mapping[str, Any]
) -> dict[str, str]:
    """Build a narrow child environment from the selected human profile."""
    capabilities = identity.get("capabilities")
    if not isinstance(capabilities, Mapping):
        capabilities = {}
    role = identity.get("role")
    if role not in {"worker", "reviewer", "orchestrator", "coordinator"}:
        role = "worker"
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "ONBOARD_CENTRAL_URL": str(profile.central_url),
        "ONBOARD_CENTRAL_TOKEN": token,
        "ONBOARD_BOARD_ID": str(profile.board_id),
        "ONBOARD_AGENT_NAME": str(identity["agent_name"]),
        "PURSERS_ROLE": str(role),
        "PURSERS_CAN_WORK": str(bool(capabilities.get("can_work", False))).lower(),
        "PURSERS_CAN_REVIEW": str(bool(capabilities.get("can_review", False))).lower(),
        "PURSERS_HOST": "ide-acp",
    }
    tier = capabilities.get("tier_max")
    if isinstance(tier, int) and not isinstance(tier, bool) and tier in {1, 2, 3}:
        environment["PURSERS_TIER_MAX"] = str(tier)
    # The bridge stamps these onto the seat's capabilities; without them every
    # IDE seat joins with model and provider null. The joined identity wins,
    # the agent's own environment is the fallback.
    for env_name, field in (("PURSERS_MODEL", "model"), ("PURSERS_PROVIDER", "provider")):
        value = capabilities.get(field)
        if not (isinstance(value, str) and value.strip()):
            value = os.environ.get(env_name, "")
        if value.strip():
            environment[env_name] = value.strip()
    return environment


def _stdio_mcp_spec(
    server: Any, names: set[str], cwd: str
) -> StdioServerParameters:
    """Validate one descriptor from the official ACP v1 stdio schema."""
    if not isinstance(server, dict):
        raise ValueError("mcpServers entries must be objects")
    if set(server) != {"name", "command", "args", "env"}:
        raise ValueError("stdio mcpServers require exactly name, command, args, and env")
    name = server.get("name")
    command = server.get("command")
    args = server.get("args")
    env_rows = server.get("env")
    if not isinstance(name, str) or not name or name in names:
        raise ValueError("stdio MCP server names must be non-empty and unique")
    if not isinstance(command, str) or not command or not Path(command).is_absolute():
        raise ValueError("stdio MCP server command must be an absolute path")
    if not isinstance(args, list) or not all(isinstance(value, str) for value in args):
        raise ValueError("stdio MCP server args must be a string array")
    if not isinstance(env_rows, list):
        raise ValueError("stdio MCP server env must be an array")
    env: dict[str, str] = {}
    for row in env_rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "value"}
            or not isinstance(row.get("name"), str)
            or not row["name"]
            or not isinstance(row.get("value"), str)
            or row["name"] in env
        ):
            raise ValueError(
                "stdio MCP server env entries require unique name/value strings"
            )
        env[row["name"]] = row["value"]
    names.add(name)
    return StdioServerParameters(
        command=command, args=list(args), env=env, cwd=cwd
    )


async def _start_stdio_mcp_servers(
    raw: Any, cwd: str
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    stop = asyncio.Event()
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def supervise() -> None:
        try:
            if not isinstance(raw, list):
                raise ValueError("mcpServers must be an array")
            if not raw:
                ready.set_result(None)
                await stop.wait()
                return

            # Open and close every stdio transport in this same supervisor task.
            stack = AsyncExitStack()
            try:
                names: set[str] = set()
                for index, server in enumerate(raw):
                    spec = _stdio_mcp_spec(server, names, cwd)
                    try:
                        connected = await stack.enter_async_context(
                            Client(spec, mode="2026-07-28", cache=None)
                        )
                        await connected.list_tools()
                    except BaseException as exc:
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        raise RuntimeError(
                            f"failed to initialize stdio MCP server {index + 1}"
                        ) from exc
                ready.set_result(None)
                await stop.wait()
            finally:
                await stack.aclose()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            elif isinstance(exc, asyncio.CancelledError):
                raise

    task = asyncio.create_task(supervise())
    try:
        await ready
    except BaseException:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
        raise
    return stop, task


def _setup_environment() -> dict[str, str]:
    """Allow local setup necessities while excluding inherited board credentials."""
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SHELL",
        "SYSTEMROOT",
        "TMPDIR",
        "USER",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _run_personal_setup(project_root: Path | None) -> None:
    project = (project_root or Path.cwd()).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise AuthRequired("Personal setup requires a project directory")
    console = shutil.which("pursers-personal")
    if console is None:
        raise AuthRequired("pursers-personal setup command is unavailable")
    host_config = profile_path_for_project(
        project, default_profiles_root()
    ).parent / "acp-host.json"
    completed = subprocess.run(
        [
            console,
            "setup",
            "--project",
            str(project),
            "--apply",
            "--activate",
            "--host-id",
            "pursers-acp",
            "--session",
            "ide",
            "--host-config",
            str(host_config),
        ],
        env=_setup_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        raise AuthRequired("Pursers Personal setup did not complete")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pursers-acp")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--login", action="store_true")
    return parser


async def _stdio(args: argparse.Namespace) -> None:
    async def factory() -> PersonalBoardSurface:
        return await PersonalBoardSurface.connect(
            profile_path=args.profile,
            project_root=args.project,
        )

    async def setup() -> None:
        await asyncio.to_thread(_run_personal_setup, args.project)

    agent = PursersACPAgent(factory, auth_setup=setup)

    async def receive() -> bytes:
        return await asyncio.to_thread(sys.stdin.buffer.readline, MAX_MESSAGE_BYTES + 1)

    async def send(payload: JSON) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()

    await agent.run(receive, send)


def main() -> None:
    args = _parser().parse_args()
    if args.login:
        try:
            select_personal_profile(
                explicit_profile=args.profile,
                project_root=args.project,
            )
        except PersonalProfileError:
            try:
                _run_personal_setup(args.project)
                select_personal_profile(
                    explicit_profile=args.profile,
                    project_root=args.project,
                )
            except (AuthRequired, PersonalProfileError, OSError):
                print("Pursers Personal setup did not complete.", file=sys.stderr)
                raise SystemExit(1) from None
        print("Pursers Personal profile is ready.", file=sys.stderr)
        return
    asyncio.run(_stdio(args))


if __name__ == "__main__":
    main()
