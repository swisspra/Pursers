"""ACP v1 stdio agent exposing a deliberately small Personal board surface."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pursers_client import BoardClient, PersonalProfileError, read_capability
from pursers_client.personal_profile import select_personal_profile

JSON = dict[str, Any]
ACP_VERSION = 1
IMPLEMENTATION_VERSION = "0.1.0"
MAX_MESSAGE_BYTES = 1_048_576
MAX_TEXT_CHARS = 8_000
MAX_ROWS = 20


class AuthRequired(RuntimeError):
    """No usable human Personal profile is available."""


class BoardSurface(Protocol):
    board_id: str

    async def close(self) -> None: ...
    async def my_tickets(self) -> list[JSON]: ...
    async def my_offers(self) -> list[JSON]: ...
    async def board_status(self) -> JSON: ...
    def create_action(self, title: str, description: str) -> JSON: ...
    def annotate_action(self, ticket_id: str, text: str) -> JSON: ...
    async def mutate(self, action: JSON) -> JSON: ...
    def watch(
        self, cursor: int | None, cancel: asyncio.Event
    ) -> AsyncIterator[tuple[int, JSON]]: ...


BoardFactory = Callable[[], BoardSurface | Awaitable[BoardSurface]]


class PersonalBoardSurface:
    """Minimal Central transport that never joins or onboards a board seat."""

    def __init__(self, profile: Any) -> None:
        self.profile = profile
        self.board_id = profile.board_id
        self.principal_id = profile.principal_id
        self.agent_name = ""
        self._stack = AsyncExitStack()
        self._client: Client | None = None

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
                    row.get("agent_name", ""),
                ),
            )
            if not own_agents:
                raise AuthRequired(
                    "Personal setup has not initialized an identity on this board"
                )
            board.agent_name = own_agents[0]["agent_name"]
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
        status = await self._status()
        return {
            key: status.get(key)
            for key in (
                "board_id",
                "status_counts",
                "review_policy",
                "dispatch_enabled",
                "latest_seq",
            )
        }

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

    async def mutate(self, action: JSON) -> JSON:
        if action.get("board_id") != self.board_id:
            raise PermissionError("mutation board differs from the approved board")
        operation = action.get("operation")
        if operation not in {"ticket_create", "ticket_annotate"}:
            raise PermissionError("unsupported mutation")
        params = action.get("params")
        if not isinstance(params, dict):
            raise ValueError("mutation params must be an object")
        return await self._call(operation, dict(params))

    async def _catchup(self, cursor: int) -> JSON:
        return await self._call(
            "board_catchup",
            {
                "agent_name": self.agent_name,
                "cursor": cursor,
                "limit": 100,
                "ack": False,
                "touch": False,
            },
        )

    async def watch(
        self, cursor: int | None, cancel: asyncio.Event
    ) -> AsyncIterator[tuple[int, JSON]]:
        if self._client is None:
            raise RuntimeError("board transport is closed")
        journal = f"board://{self.board_id}/journal"
        async with self._client.listen(resource_subscriptions=[journal]) as stream:
            honored = set(stream.honored.resource_subscriptions or ())
            if journal not in honored:
                raise RuntimeError("Central did not honor the board journal subscription")
            if cursor is None:
                status = await self._status()
                cursor = int(status.get("latest_seq", 0))
            while not cancel.is_set():
                page = await self._catchup(cursor)
                if page.get("resync_required"):
                    cursor = int(page["reset_cursor"])
                    yield cursor, {
                        "kind": "resync_required",
                        "reset_cursor": cursor,
                    }
                    continue
                else:
                    cursor = int(page.get("next_cursor", cursor))
                    for event in page.get("events", []):
                        yield cursor, event
                    if page.get("has_more"):
                        continue
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


@dataclass
class Session:
    cwd: str
    cursors: dict[str, int] = field(default_factory=dict)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    active: bool = False


class PursersACPAgent:
    def __init__(self, board_factory: BoardFactory) -> None:
        self.board_factory = board_factory
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
            if session is not None:
                session.cancel.set()
            return
        if request_id is None:
            return
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
            future.set_exception(RuntimeError("ACP client rejected permission request"))

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
                    "id": "pursers-personal-profile",
                    "name": "Configure Pursers Personal",
                    "description": "Select an existing human Personal profile",
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
        session_id = f"pursers-{uuid.uuid4().hex}"
        self.sessions[session_id] = Session(cwd=cwd)
        await self._result(request_id, {"sessionId": session_id})

    async def _prompt(self, request_id: Any, params: JSON) -> None:
        session_id = params.get("sessionId")
        session = self.sessions.get(session_id)
        if session is None:
            await self._error(request_id, -32602, "unknown session")
            return
        if session.active:
            await self._error(request_id, -32600, "session prompt already active")
            return
        try:
            text = _prompt_text(params.get("prompt"))
        except ValueError as exc:
            await self._error(request_id, -32602, str(exc))
            return
        session.active = True
        session.cancel.clear()
        try:
            stop = await self._dispatch(session_id, session, text)
            await self._result(request_id, {"stopReason": stop})
        finally:
            session.active = False

    async def _dispatch(self, session_id: str, session: Session, text: str) -> str:
        assert self.board is not None
        normalized = " ".join(text.strip().split())
        lowered = normalized.casefold()
        if lowered == "my tickets":
            rows = await self.board.my_tickets()
            await self._message(session_id, _format_tickets(rows))
            return "end_turn"
        if lowered == "my offers":
            rows = await self.board.my_offers()
            await self._message(session_id, _format_offers(rows))
            return "end_turn"
        if lowered == "board status":
            await self._message(
                session_id,
                "Board status\n\n```json\n"
                + json.dumps(await self.board.board_status(), sort_keys=True, indent=2)
                + "\n```",
            )
            return "end_turn"
        if lowered.startswith("create ticket "):
            title, description = _parse_create(normalized[14:])
            action = self.board.create_action(title, description)
            return await self._mutation(session_id, session, action)
        if lowered.startswith("annotate "):
            ticket_id, separator, annotation = normalized[9:].partition(" ")
            if not separator or not ticket_id.startswith("TK-") or not annotation:
                await self._message(session_id, "Usage: annotate TK-… <text>")
                return "end_turn"
            action = self.board.annotate_action(ticket_id, annotation)
            return await self._mutation(session_id, session, action)
        if lowered.startswith("watch "):
            board_id = normalized[6:].strip()
            if board_id != self.board.board_id:
                await self._message(
                    session_id,
                    f"This profile is bound to {self.board.board_id}; refusing cross-board watch.",
                )
                return "refusal"
            async with aclosing(
                self.board.watch(session.cursors.get(board_id), session.cancel)
            ) as events:
                async for cursor, event in events:
                    session.cursors[board_id] = cursor
                    await self._message(session_id, _format_event(event))
                    if session.cancel.is_set():
                        break
            return "cancelled" if session.cancel.is_set() else "end_turn"
        await self._message(session_id, _help())
        return "end_turn"

    async def _mutation(self, session_id: str, session: Session, action: JSON) -> str:
        tool_call_id = f"board-{uuid.uuid4().hex}"
        await self._update(
            session_id,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_call_id,
                "title": f"{action['operation']} on {action['board_id']}",
                "kind": "other",
                "status": "pending",
                "rawInput": action,
            },
        )
        allowed = await self._permission(session_id, session, tool_call_id, action)
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
        result = await self.board.mutate(action)
        summary = _mutation_summary(action, result)
        await self._update(
            session_id,
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "status": "completed",
                "content": [_content(summary)],
            },
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
                        "title": f"{action['operation']} on {action['board_id']}",
                        "kind": "other",
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


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _content(text: str) -> JSON:
    return {"type": "content", "content": {"type": "text", "text": text}}


def _format_tickets(rows: list[JSON]) -> str:
    if not rows:
        return "No active tickets are associated with this Personal principal."
    lines = ["My tickets"]
    for row in rows[:MAX_ROWS]:
        lines.append(
            f"- {row.get('ticket_id', '?')} [{row.get('status', '?')}] "
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


def _format_event(event: JSON) -> str:
    selected = {
        key: event.get(key)
        for key in ("seq", "kind", "ticket_id", "status_to", "payload_ref")
        if event.get(key) is not None
    }
    return "Board event\n\n```json\n" + json.dumps(selected, sort_keys=True) + "\n```"


def _mutation_summary(action: JSON, result: JSON) -> str:
    if action.get("operation") == "ticket_create":
        ticket = result.get("ticket", {})
        return f"Created ticket {ticket.get('ticket_id', '(unknown)')}."
    annotation = result.get("annotation", {})
    return f"Added annotation {annotation.get('annotation_id', '(recorded)')}."


def _help() -> str:
    return (
        "Pursers board commands:\n"
        "- my tickets\n- my offers\n- board status\n"
        "- create ticket <title> :: <description>\n"
        "- annotate TK-… <text>\n- watch <board> (cancel to stop)"
    )


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

    agent = PursersACPAgent(factory)

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
        except PersonalProfileError as exc:
            print(
                f"No usable Personal profile: {exc}. Run pursers-personal setup first.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        print("Pursers Personal profile is ready.", file=sys.stderr)
        return
    asyncio.run(_stdio(args))


if __name__ == "__main__":
    main()
