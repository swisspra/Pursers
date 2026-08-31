#!/usr/bin/env python3
"""Wait-for-work MCP bridge for the Pursers / On Board v5 central.

WHY THIS EXISTS
    This bridge replicates v4's blocking a2a_wait primitive. Polling remains
    the default and compatibility fallback. A dark-launch push mode can use
    MCP v2 subscriptions/listen only as a wake signal, then refetch the same
    journal/backlog state as polling so notifications never become data.

TRANSPORT
    This server MUST run over stdio (the host spawns it as a subprocess).
    stdio has no per-request timer, so the tool call can genuinely block for
    the requested timeout_s. Do not put this behind mcp-remote / HTTP -- an
    HTTP transport would apply its own request timeout and defeat the block.

THE TOOL
    a2a_wait(since_seq=0, timeout_s=180, only_mine=True)
      1. CHECK BEFORE BLOCKING: fully drain board_catchup from since_seq and
         scan current open tickets older than the cursor. If relevant work is
         found on either path, return immediately (no wait).
      2. Otherwise poll board_catchup every ~2s until a relevant event shows
         up or timeout_s elapses. Fire a lease_renew heartbeat for any ticket
         this agent currently holds every ~20s, so a peer's reaper does not
         treat a long park as an abandoned claim.
      3. Return a small bounded shape: {new_seq, events, waited_s, timed_out}.
         timed_out=True is the re-arm cue: call again with since_seq=new_seq.

RELEVANCE
    The live journal event only carries {kind, ticket_id, status_from,
    status_to, actor, ...} -- no assignee/creator. board_catchup itself
    already drops self-authored events and events this agent is not a
    recipient of (recipient_identities is "every other member" for tickets),
    so what board_catchup hands back is already "not mine to have caused."
    only_mine=True narrows that further with one ticket_get per candidate
    event: relevant iff the ticket is unclaimed/unassigned (the open queue),
    or the agent created it, is assigned to it, or currently holds its claim.
    memory_written is intentionally ignored -- this tool is a work-arrival
    signal, not a memory watcher (matches v4's DEFAULT_KINDS posture).
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator

from pursers_client import (
    GENERATION_META_KEY,
    BoardClient,
    BoardClientError,
    JoinedIdentity,
)
from mcp.server.mcpserver import Context, MCPServer
from agent_naming import resolve_agent_name
from backlog import backlog_events, ticket_is_relevant

VERSION = "0.1.0a1"

# --- config from env -------------------------------------------------------

CENTRAL_URL = os.environ.get("ONBOARD_CENTRAL_URL", "https://127.0.0.1:8766/mcp")
BOARD_ID = os.environ.get("ONBOARD_BOARD_ID", "pursers")
CENTRAL_TOKEN = os.environ.get("ONBOARD_CENTRAL_TOKEN", "")
BASE_AGENT_NAME = os.environ.get("ONBOARD_AGENT_NAME", "pursers-wait-bridge")
AGENT_NAME = resolve_agent_name(
    BASE_AGENT_NAME, os.environ.get("ONBOARD_AGENT_INSTANCE")
)
_RAW_WAIT_MODE = os.environ.get("PURSERS_WAIT_MODE", "poll").strip().lower()
WAIT_MODE = _RAW_WAIT_MODE if _RAW_WAIT_MODE in {"poll", "push"} else "poll"

# --- wait policy (v4-parity constants; see a2a_wait.py) --------------------

DEFAULT_TIMEOUT_S = 180
DESKTOP_SAFE_MAX_S = 200        # stay clear of Claude Desktop's ~240s hard cancel
DEFAULT_POLL_INTERVAL_S = 2.0
HEARTBEAT_INTERVAL_S = 20.0
CATCHUP_PAGE_LIMIT = 100
BACKLOG_SCAN_LIMIT = 100
RELEVANT_KINDS = frozenset({"ticket_created", "ticket_status_changed"})
CLAIMED_STATES = frozenset({"claimed", "in_progress", "creating_report"})
HANDOFF_REJOIN_MESSAGE = "call board_onboard or board_join before more work"
PROJECT_REGISTRY_KEY = "project_registry"
PROJECT_REGISTRY_SCHEMA_VERSION = 1
PROJECT_STATUSES = frozenset({"active", "paused"})
STATS_SCHEMA_VERSION = 1
STATS_RETENTION_DAYS = 7


def bridge_stats_path() -> Path:
    configured = os.environ.get("PURSERS_BRIDGE_STATS", "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path(__file__).resolve().with_name("bridge-stats.json")
    )


def _meter_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


class BridgeStats:
    """Atomic seven-day size/count meter; payload contents are never stored."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()

    async def record(
        self,
        board_id: str,
        agent_name: str,
        tool_name: str,
        request_bytes: int,
        response_bytes: int,
    ) -> None:
        try:
            async with self._lock:
                self._record_sync(
                    board_id,
                    agent_name,
                    tool_name,
                    max(0, int(request_bytes)),
                    max(0, int(response_bytes)),
                )
        except Exception as exc:  # noqa: BLE001 - metering never breaks work.
            _log(f"stats write failed: {type(exc).__name__}")

    def _record_sync(
        self,
        board_id: str,
        agent_name: str,
        tool_name: str,
        request_bytes: int,
        response_bytes: int,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                document = {}
            if not isinstance(document, dict):
                document = {}
            days = document.get("days")
            if not isinstance(days, dict):
                days = {}
            today = self.clock().astimezone(timezone.utc).date().isoformat()
            retained = sorted(
                {str(day) for day in days if str(day) <= today} | {today}
            )[-STATS_RETENTION_DAYS:]
            days = {
                day: value
                for day in retained
                if isinstance((value := days.get(day, {})), dict)
            }
            current = days.setdefault(today, {"seats": {}})
            seats = current.get("seats")
            if not isinstance(seats, dict):
                seats = {}
                current["seats"] = seats
            seat_key = json.dumps([board_id, agent_name], separators=(",", ":"))
            seat = seats.setdefault(
                seat_key,
                {
                    "board_id": board_id,
                    "agent_name": agent_name,
                    "request_bytes": 0,
                    "response_bytes": 0,
                    "calls": {},
                },
            )
            seat["request_bytes"] = int(seat.get("request_bytes", 0)) + request_bytes
            seat["response_bytes"] = int(seat.get("response_bytes", 0)) + response_bytes
            calls = seat.get("calls")
            if not isinstance(calls, dict):
                calls = {}
                seat["calls"] = calls
            tool = calls.setdefault(
                tool_name,
                {"count": 0, "request_bytes": 0, "response_bytes": 0},
            )
            tool["count"] = int(tool.get("count", 0)) + 1
            tool["request_bytes"] = int(tool.get("request_bytes", 0)) + request_bytes
            tool["response_bytes"] = int(tool.get("response_bytes", 0)) + response_bytes
            output = {
                "schema_version": STATS_SCHEMA_VERSION,
                "days": days,
            }
            temporary_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    delete=False,
                ) as stream:
                    temporary_name = stream.name
                    json.dump(
                        output,
                        stream,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, self.path)
                temporary_name = None
            finally:
                if temporary_name is not None:
                    Path(temporary_name).unlink(missing_ok=True)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


class MeteredBoardClient(BoardClient):
    def __init__(self, *args: Any, meter: BridgeStats, **kwargs: Any) -> None:
        self.meter = meter
        super().__init__(*args, **kwargs)

    async def _measure(
        self,
        name: str,
        arguments: dict[str, Any],
        operation: Callable[[], Awaitable[dict[str, Any]]],
        *,
        board_id: str | None = None,
    ) -> dict[str, Any]:
        selected_board = board_id or self.board_id
        selected_agent = str(arguments.get("agent_name") or self.agent_name)
        request = {"name": name, "arguments": {"board_id": selected_board, **arguments}}
        if self.generation_token is not None:
            request["meta"] = {GENERATION_META_KEY: self.generation_token}
        request_bytes = _meter_bytes(request)
        try:
            result = await operation()
        except BaseException as exc:
            await self.meter.record(
                selected_board,
                selected_agent,
                name,
                request_bytes,
                _meter_bytes({"error": type(exc).__name__}),
            )
            raise
        await self.meter.record(
            selected_board,
            selected_agent,
            name,
            request_bytes,
            _meter_bytes(result),
        )
        return result

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._measure(
            name,
            arguments,
            lambda: super(MeteredBoardClient, self)._call(name, arguments),
        )

    async def _call_refresh(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._measure(
            name,
            arguments,
            lambda: super(MeteredBoardClient, self)._call_refresh(name, arguments),
        )

    async def _call_refresh_uncached(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._measure(
            name,
            arguments,
            lambda: super(MeteredBoardClient, self)._call_refresh_uncached(
                name, arguments
            ),
        )

    async def _call_unscoped(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._measure(
            name,
            arguments,
            lambda: super(MeteredBoardClient, self)._call_unscoped(name, arguments),
            board_id=str(arguments.get("board_id") or self.board_id),
        )


def clamp_timeout(timeout_s: Any) -> int:
    try:
        t = int(timeout_s)
    except (TypeError, ValueError):
        t = DEFAULT_TIMEOUT_S
    return max(1, min(t, DESKTOP_SAFE_MAX_S))


def _log(msg: str) -> None:
    # stderr only -- stdout is the stdio JSON-RPC channel.
    print(f"[a2a_wait] {msg}", file=sys.stderr, flush=True)


if _RAW_WAIT_MODE not in {"poll", "push"}:
    _log(f"invalid PURSERS_WAIT_MODE={_RAW_WAIT_MODE!r}; using poll")


@lru_cache(maxsize=1_024)
def _derived_agent_id(
    principal_id: str, agent_name: str, board_id: str = BOARD_ID
) -> str:
    """Pure Central-compatible identity derivation; safe to memoize."""
    logical = json.dumps(
        [board_id, principal_id, agent_name], separators=(",", ":")
    )
    return "AI-" + hashlib.sha256(logical.encode("utf-8")).hexdigest()


class _BoardView:
    """Board-scoped calls over the lifespan client's open transport."""

    def __init__(self, parent: BoardClient, board_id: str) -> None:
        raw_client = getattr(parent, "_client", None)
        if raw_client is None:
            raise RuntimeError("BoardClient is not entered")
        self.board_id = board_id
        self.agent_name = parent.agent_name
        self.identity: JoinedIdentity | None = None
        self.generation_token: str | None = None
        self._client = raw_client
        self.meter = getattr(parent, "meter", None)

    def _refresh_generation(self, result: dict[str, Any]) -> None:
        token = result.get("generation_token")
        if token is None:
            self.generation_token = None
            return
        if (
            not isinstance(token, str)
            or not token
            or token != token.strip()
            or len(token) > 256
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token)
        ):
            raise BoardClientError("server returned an invalid generation_token")
        self.generation_token = token

    async def _call(
        self, name: str, arguments: dict[str, Any], *, refresh: bool = False
    ) -> dict[str, Any]:
        payload = {"board_id": self.board_id, **arguments}
        meta = None
        if not refresh and self.generation_token is not None:
            meta = {GENERATION_META_KEY: self.generation_token}
        request = {"name": name, "arguments": payload}
        if meta is not None:
            request["meta"] = meta
        request_bytes = _meter_bytes(request)
        try:
            if meta is None:
                raw = await self._client.call_tool(name, payload)
            else:
                raw = await self._client.call_tool(name, payload, meta=meta)
            result = BoardClient._decode(raw)
        except BaseException as exc:
            if self.meter is not None:
                await self.meter.record(
                    self.board_id,
                    str(arguments.get("agent_name") or self.agent_name),
                    name,
                    request_bytes,
                    _meter_bytes({"error": type(exc).__name__}),
                )
            raise
        if self.meter is not None:
            await self.meter.record(
                self.board_id,
                str(arguments.get("agent_name") or self.agent_name),
                name,
                request_bytes,
                _meter_bytes(result),
            )
        if refresh:
            self._refresh_generation(result)
        return result

    async def board_join(self, *, agent_name: str | None = None) -> dict[str, Any]:
        selected = self.agent_name if agent_name is None else agent_name
        joined = await self._call(
            "board_join", {"agent_name": selected}, refresh=True
        )
        self.identity = JoinedIdentity(
            joined["board_id"],
            joined["agent_id"],
            joined["principal_id"],
            joined["agent_name"],
            joined["role"],
        )
        return {**joined, "identity": self.identity}

    async def board_catchup(self, **arguments: Any) -> dict[str, Any]:
        arguments.setdefault("agent_name", self.agent_name)
        return await self._call("board_catchup", arguments)

    async def ticket_get(self, ticket_id: str) -> dict[str, Any]:
        return await self._call("ticket_get", {"ticket_id": ticket_id})

    async def ticket_list(self, **arguments: Any) -> dict[str, Any]:
        return await self._call("ticket_list", arguments)

    async def lease_renew(self, ticket_id: str) -> dict[str, Any]:
        return await self._call("lease_renew", {"ticket_id": ticket_id})


async def _join_for_call(
    client: BoardClient, agent_name: str, explicit_name: bool
) -> dict[str, Any]:
    if explicit_name:
        return await client.board_join(agent_name=agent_name)
    return await client.board_join()


@asynccontextmanager
async def _lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Join the board once, under the server's top-level task.

    This matters structurally, not just for efficiency: BoardClient's
    __aenter__ opens an httpx2 client and a streamable-http transport, each
    of which creates its own anyio task group / cancel scope. anyio requires
    those to be entered and exited from a consistent place in the task tree.
    Opening the connection inside a per-request tool-call task (a sibling of
    every other request's task, not an ancestor of them) and then reusing it
    from later requests violates that nesting and crashes the dispatcher with
    "Attempted to exit a cancel scope that isn't the current task's current
    cancel scope." The lifespan runs in the server's top-level task, which
    every per-request task is a descendant of, so the connection it opens
    here is safe to reuse from any later tool call.
    """
    meter = BridgeStats(bridge_stats_path())
    client = MeteredBoardClient(
        CENTRAL_URL,
        CENTRAL_TOKEN,
        BOARD_ID,
        agent_name=AGENT_NAME,
        meter=meter,
    )
    await client.__aenter__()
    _log(
        f"joined board={BOARD_ID!r} as agent={AGENT_NAME!r} "
        f"agent_id={client.identity.agent_id if client.identity else '?'}"
    )
    try:
        yield {"client": client}
    finally:
        await client.__aexit__(None, None, None)


mcp = MCPServer("Pursers Wait Bridge", version="0.1.0", lifespan=_lifespan)


def _parse_project_registry(result: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize Central's string-valued board-state entry."""
    state = result.get("state")
    if not isinstance(state, dict):
        raise ValueError("project_registry state entry is missing")
    raw_value = state.get("value")
    if not isinstance(raw_value, str):
        raise ValueError("project_registry state value must be a JSON string")
    try:
        registry = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("project_registry state value is not valid JSON") from exc
    if not isinstance(registry, dict):
        raise ValueError("project_registry must be a JSON object")
    schema_version = registry.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version != PROJECT_REGISTRY_SCHEMA_VERSION
    ):
        raise ValueError(
            f"project_registry schema_version must be {PROJECT_REGISTRY_SCHEMA_VERSION}"
        )
    projects = registry.get("projects")
    if not isinstance(projects, dict):
        raise ValueError("project_registry projects must be an object")

    normalized_projects: dict[str, dict[str, str]] = {}
    for name, project in projects.items():
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError(
                "project_registry project names must be non-empty strings"
            )
        if not isinstance(project, dict):
            raise ValueError(f"project_registry project {name!r} must be an object")
        board_id = project.get("board_id")
        work_dir = project.get("work_dir")
        status = project.get("status")
        if (
            not isinstance(board_id, str)
            or not board_id
            or board_id != board_id.strip()
        ):
            raise ValueError(
                f"project_registry project {name!r} has an invalid board_id"
            )
        if not isinstance(work_dir, str) or not os.path.isabs(work_dir):
            raise ValueError(
                f"project_registry project {name!r} work_dir must be absolute"
            )
        if status not in PROJECT_STATUSES:
            raise ValueError(
                f"project_registry project {name!r} status must be active or paused"
            )
        normalized_projects[name] = {
            "board_id": board_id,
            "work_dir": work_dir,
            "status": status,
        }
    return {
        "schema_version": PROJECT_REGISTRY_SCHEMA_VERSION,
        "projects": normalized_projects,
    }


async def _read_project_registry(client: BoardClient) -> dict[str, Any]:
    result = await client.board_state_get(key=PROJECT_REGISTRY_KEY)
    return _parse_project_registry(result)


def _registry_boards(registry: dict[str, Any]) -> list[str]:
    """Return active boards in registry order, always led by the home board."""
    selected = [BOARD_ID]
    seen = {BOARD_ID}
    for project in registry["projects"].values():
        board_id = project["board_id"]
        if project["status"] == "active" and board_id not in seen:
            selected.append(board_id)
            seen.add(board_id)
    return selected


def _home_cursor(since_seq: int | dict[str, int]) -> int:
    if isinstance(since_seq, dict):
        return max(0, int(since_seq.get(BOARD_ID, 0)))
    return max(0, int(since_seq))


@mcp.tool()
async def project_registry_get(ctx: Context) -> dict[str, Any]:
    """Return the parsed project registry stored on the home board."""
    client: BoardClient = ctx.request_context.lifespan_context["client"]
    return await _read_project_registry(client)


async def _catchup_all(
    client: BoardClient,
    cursor: int,
    agent_name: str,
    explicit_name: bool,
) -> tuple[list[dict], int, bool]:
    """Fully drain board_catchup pages from cursor. ack=False: this tool owns
    since_seq/new_seq itself via the caller's explicit round trip rather than
    the server's per-(principal,agent) cursor, so it never perturbs cursor
    state any other tool on this identity may depend on.

    Returns (events, next_cursor, resynced). resynced=True means the journal
    was compacted past our cursor and we had to jump forward to the server's
    reset point: the events between the old cursor and that point are gone and
    CANNOT be recovered here. The caller must surface this so the worker
    re-fetches full state (e.g. ticket_list) instead of trusting the returned
    events as the complete backlog."""
    events: list[dict] = []
    resynced = False
    while True:
        catchup_args: dict[str, Any] = {
            "cursor": cursor,
            "limit": CATCHUP_PAGE_LIMIT,
            "ack": False,
        }
        if explicit_name:
            catchup_args["agent_name"] = agent_name
        try:
            page = await client.board_catchup(**catchup_args)
        except BoardClientError as exc:
            if not explicit_name or HANDOFF_REJOIN_MESSAGE not in str(exc):
                raise
            _log(f"agent={agent_name!r}: handed off; rejoining once")
            await _join_for_call(client, agent_name, explicit_name)
            page = await client.board_catchup(**catchup_args)
        if page.get("resync_required"):
            resynced = True
            cursor = int(page["reset_cursor"])
            _log(f"resync_required: journal compacted past cursor; jumped to {cursor} (events lost)")
            continue
        events.extend(page["events"])
        cursor = page["next_cursor"]
        if not page.get("has_more"):
            break
    return events, cursor, resynced


async def _is_relevant(
    client: BoardClient,
    event: dict,
    my_agent_id: str | None,
    only_mine: bool,
    project: str | None,
) -> bool:
    if event.get("kind") not in RELEVANT_KINDS:
        return False
    ticket_id = event.get("ticket_id")
    # We need the ticket body to apply either the project filter or the
    # only_mine ownership check. Fetch it once if either is active.
    if only_mine or project is not None:
        if not ticket_id:
            return False
        try:
            result = await client.ticket_get(ticket_id)
        except BoardClientError:
            return False
        ticket = result.get("ticket", {})
        return ticket_is_relevant(ticket, my_agent_id, only_mine, project)
    # No project filter and not only_mine: every relevant-kind event counts.
    return True


async def _filter_relevant(
    client: BoardClient,
    events: list[dict],
    my_agent_id: str,
    only_mine: bool,
    project: str | None,
) -> list[dict]:
    out = []
    for ev in events:
        if await _is_relevant(client, ev, my_agent_id, only_mine, project):
            out.append(ev)
    return out


async def _scan_open_backlog(
    client: BoardClient,
    my_agent_id: str,
    only_mine: bool,
    project: str | None,
) -> list[dict]:
    """Best-effort scan for open work older than the caller's journal cursor."""
    try:
        listed = await client.ticket_list(
            status="open", include_closed=False, limit=BACKLOG_SCAN_LIMIT
        )
    except Exception as exc:
        _log(f"backlog scan: ticket_list failed: {exc}")
        return []
    return backlog_events(
        listed.get("tickets", []), my_agent_id, only_mine, project
    )


async def _heartbeat(
    client: BoardClient, agent_name: str, my_agent_id: str
) -> None:
    """Best-effort lease_renew for any ticket this agent currently holds.

    Never raises -- a heartbeat failure must not abort the wait. If this
    agent holds no active claim there is nothing to renew, which is the
    common case for an idle listener; that is logged, not treated as error.
    """
    try:
        listed = await client.ticket_list(
            assigned_to=agent_name, include_closed=False, limit=50
        )
    except Exception as exc:
        _log(f"heartbeat: ticket_list failed: {exc}")
        return
    held = [
        t for t in listed.get("tickets", [])
        if t.get("status") in CLAIMED_STATES and t.get("claimed_by_agent_id") == my_agent_id
    ]
    if not held:
        _log("heartbeat: no active claim to renew")
        return
    for ticket in held:
        ticket_id = ticket["ticket_id"]
        try:
            renewed = await client.lease_renew(ticket_id)
            _log(
                f"heartbeat: agent={agent_name!r} lease_renew {ticket_id} "
                f"-> expires {renewed.get('lease_expires_at')}"
            )
        except Exception as exc:
            _log(f"heartbeat: lease_renew {ticket_id} failed: {exc}")


@mcp.tool()
async def a2a_wait(
    ctx: Context,
    since_seq: int | dict[str, int] = 0,
    timeout_s: int = 180,
    only_mine: bool = True,
    project: str | None = None,
    agent_name: str | None = None,
    boards: list[str] | str | None = None,
) -> dict[str, Any]:
    """Block until pursers board work arrives, or until timeout_s elapses.

    CHECK-BEFORE-BLOCKING: journal backlog accrued since since_seq is drained,
    then current open tickets are scanned for work older than the cursor.
    Relevant work is returned without waiting, so a re-arm after a long gap
    costs one call.
    Otherwise polls every ~2s, firing a lease_renew heartbeat roughly every
    20s for any ticket this agent holds, until a relevant event appears or
    timeout_s (clamped to a desktop-safe ceiling) elapses.

    project: when set (case-insensitive), only tickets whose target_url starts
    with "<project>/" match -- this is how one shared Pursers board serves
    several projects without a worker seeing another project's queue. Leave it
    unset to see every project (the cross-project orchestrator view).

    agent_name: optional per-call board identity. Omit it to preserve the
    process-level ONBOARD_AGENT_NAME/INSTANCE identity exactly. An explicit
    name is joined statelessly for this call on the existing connection.

    boards: optional board IDs for a cross-project worker pool, or the string
    sentinel "registry" to resolve active boards from the home board's
    project_registry state at the start of this invocation. Omit it for the
    permanent single-board compatibility path and its original response. When
    supplied, since_seq may be a {board_id: cursor} map. Multi-board events
    include board_id; new_seq and resynced are per-board maps; boards denied at
    join are reported in skipped_boards without aborting the call. An
    unreadable or malformed registry falls back to the single home board and
    adds registry_warning to that otherwise original response shape.

    Returns {new_seq, events, waited_s, timed_out, resynced}. timed_out=True
    means "no work" -- call again with since_seq=new_seq to re-arm. resynced=True
    means the journal was compacted past our cursor and events were lost:
    re-fetch full state (e.g. ticket_list) before trusting events as complete.

    HEARTBEAT SCOPE: the lease_renew heartbeat only fires while THIS call is
    blocking. It does NOT run while you are executing ticket work between
    a2a_wait calls -- during long work you must renew your own claim (lease_renew)
    or the reaper can reclaim it. See WORKER-DIRECTIVE.md step DO.
    """
    client: BoardClient = ctx.request_context.lifespan_context["client"]
    if boards == "registry":
        try:
            registry = await _read_project_registry(client)
        except Exception as exc:
            result = await _wait_for_work(
                client,
                since_seq=_home_cursor(since_seq),
                timeout_s=timeout_s,
                only_mine=only_mine,
                project=project,
                agent_name=agent_name,
            )
            return {
                **result,
                "registry_warning": (
                    f"project_registry unavailable; using {BOARD_ID!r} only: {exc}"
                ),
            }
        return await _wait_for_work_many(
            client,
            boards=_registry_boards(registry),
            since_seq=since_seq,
            timeout_s=timeout_s,
            only_mine=only_mine,
            project=project,
            agent_name=agent_name,
        )
    if isinstance(boards, str):
        raise ValueError('boards must be a list of board IDs or "registry"')
    if boards is not None:
        return await _wait_for_work_many(
            client,
            boards=boards,
            since_seq=since_seq,
            timeout_s=timeout_s,
            only_mine=only_mine,
            project=project,
            agent_name=agent_name,
        )
    if isinstance(since_seq, dict):
        raise ValueError("since_seq must be an integer when boards is omitted")
    return await _wait_for_work(
        client,
        since_seq=since_seq,
        timeout_s=timeout_s,
        only_mine=only_mine,
        project=project,
        agent_name=agent_name,
    )


def _normalize_boards(boards: list[str]) -> list[str]:
    if not isinstance(boards, list):
        raise ValueError("boards must be a list of board IDs")
    normalized: list[str] = []
    seen: set[str] = set()
    for board_id in boards:
        if not isinstance(board_id, str) or not board_id.strip():
            raise ValueError("boards must contain non-empty strings")
        selected = board_id.strip()
        if selected not in seen:
            normalized.append(selected)
            seen.add(selected)
    if not normalized:
        raise ValueError("boards must contain at least one board ID")
    return normalized


def _multi_cursors(
    boards: list[str], since_seq: int | dict[str, int]
) -> dict[str, int]:
    if isinstance(since_seq, dict):
        return {
            board_id: max(0, int(since_seq.get(board_id, 0)))
            for board_id in boards
        }
    cursor = max(0, int(since_seq))
    return {board_id: cursor for board_id in boards}


async def _push_cues(
    board_id: str,
    client: _BoardView,
    queue: asyncio.Queue[tuple[str, str, str | None]],
) -> None:
    """Forward one board's stable journal cues; report failure locally."""
    try:
        listen = getattr(client._client, "listen", None)
        if not callable(listen):
            raise RuntimeError("BoardClient transport does not expose listen()")
        journal_uri = f"board://{board_id}/journal"
        async with listen(resource_subscriptions=[journal_uri]) as subscription:
            honored = {
                str(uri)
                for uri in (subscription.honored.resource_subscriptions or ())
            }
            if journal_uri not in honored:
                raise RuntimeError(
                    f"server did not honor journal subscription: {journal_uri}"
                )
            await queue.put((board_id, "ready", None))
            async for _notification in subscription:
                await queue.put((board_id, "cue", None))
            await queue.put((board_id, "failed", "subscription ended"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await queue.put((board_id, "failed", str(exc)))


async def _wait_for_work_many(
    client: BoardClient,
    *,
    boards: list[str],
    since_seq: int | dict[str, int] = 0,
    timeout_s: int = 180,
    only_mine: bool = True,
    project: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Wait across independent board cursors and identities on one transport."""
    board_order = _normalize_boards(boards)
    cursors = _multi_cursors(board_order, since_seq)
    resynced = {board_id: False for board_id in board_order}
    skipped: dict[str, str] = {}
    views: dict[str, _BoardView] = {}
    agent_ids: dict[str, str] = {}
    budget = clamp_timeout(timeout_s)
    started = time.monotonic()
    deadline = started + budget
    last_heartbeat = started
    proj = project.strip().lower() if isinstance(project, str) and project.strip() else None
    call_agent_name = AGENT_NAME if agent_name is None else agent_name
    if not isinstance(call_agent_name, str) or not call_agent_name:
        raise ValueError("agent_name must be a non-empty string")
    if client.identity is None:
        raise RuntimeError("BoardClient has no default joined identity")
    for board_id in board_order:
        try:
            view = _BoardView(client, board_id)
            joined = await view.board_join(agent_name=call_agent_name)
        except BoardClientError as exc:
            skipped[board_id] = str(exc)
            continue
        expected_id = _derived_agent_id(
            joined["principal_id"], call_agent_name, board_id
        )
        if joined.get("agent_id") != expected_id:
            raise BoardClientError("server returned an unexpected per-board agent_id")
        views[board_id] = view
        agent_ids[board_id] = expected_id

    active = [board_id for board_id in board_order if board_id in views]

    def response(events: list[dict], timed_out: bool) -> dict[str, Any]:
        return {
            "new_seq": dict(cursors),
            "events": events,
            "waited_s": (
                0.0 if events and time.monotonic() - started < 0.005
                else round(time.monotonic() - started, 2)
            ),
            "timed_out": timed_out,
            "resynced": dict(resynced),
            "skipped_boards": dict(skipped),
        }

    async def poll_board(board_id: str, *, backlog: bool = False) -> list[dict]:
        events, next_cursor, did_resync = await _catchup_all(
            views[board_id],
            cursors[board_id],
            call_agent_name,
            True,
        )
        cursors[board_id] = next_cursor
        if did_resync:
            resynced[board_id] = True
        relevant = await _filter_relevant(
            views[board_id],
            events,
            agent_ids[board_id],
            only_mine,
            proj,
        )
        if backlog:
            queued = await _scan_open_backlog(
                views[board_id], agent_ids[board_id], only_mine, proj
            )
            journal_ids = {
                event.get("ticket_id")
                for event in relevant
                if event.get("ticket_id")
            }
            relevant.extend(
                event for event in queued
                if event.get("ticket_id") not in journal_ids
            )
        return [{**event, "board_id": board_id} for event in relevant]

    async def poll_selected(
        selected: list[str], *, backlog: bool = False
    ) -> list[dict]:
        found: list[dict] = []
        for board_id in selected:
            found.extend(await poll_board(board_id, backlog=backlog))
        return found

    # Entry-only backlog scans are interleaved board-by-board with catchup.
    relevant = await poll_selected(active, backlog=True)
    if relevant:
        return response(relevant, False)
    if not active:
        return response([], True)

    final_poll = active
    if WAIT_MODE == "push":
        queue: asyncio.Queue[tuple[str, str, str | None]] = asyncio.Queue()
        tasks = {
            board_id: asyncio.create_task(
                _push_cues(board_id, views[board_id], queue)
            )
            for board_id in active
        }
        fallback: set[str] = set()
        pending_ready = set(active)
        try:
            # Establish every independent subscription, then splice once to
            # cover mutations racing initial catchup and subscription setup.
            while pending_ready and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    async with asyncio.timeout(remaining):
                        board_id, kind, detail = await queue.get()
                except TimeoutError:
                    break
                if kind == "failed":
                    fallback.add(board_id)
                    _log(
                        f"push unavailable for board={board_id!r}; "
                        f"falling back to poll: {detail}"
                    )
                pending_ready.discard(board_id)
            relevant = await poll_selected(active)
            if relevant:
                return response(relevant, False)

            next_poll = time.monotonic() + DEFAULT_POLL_INTERVAL_S
            while True:
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    break
                heartbeat_due = max(
                    0.0, HEARTBEAT_INTERVAL_S - (now - last_heartbeat)
                )
                poll_due = (
                    max(0.0, next_poll - now) if fallback else remaining
                )
                wait_slice = min(remaining, heartbeat_due, poll_due)
                item: tuple[str, str, str | None] | None = None
                if wait_slice > 0:
                    try:
                        async with asyncio.timeout(wait_slice):
                            item = await queue.get()
                    except TimeoutError:
                        pass

                cued: set[str] = set()
                if item is not None:
                    board_id, kind, detail = item
                    if kind == "cue":
                        cued.add(board_id)
                    elif kind == "failed":
                        fallback.add(board_id)
                        _log(
                            f"push lost for board={board_id!r}; "
                            f"falling back to poll: {detail}"
                        )
                    while not queue.empty():
                        board_id, kind, detail = queue.get_nowait()
                        if kind == "cue":
                            cued.add(board_id)
                        elif kind == "failed":
                            fallback.add(board_id)
                            _log(
                                f"push lost for board={board_id!r}; "
                                f"falling back to poll: {detail}"
                            )
                if cued:
                    relevant = await poll_selected(
                        [board_id for board_id in active if board_id in cued]
                    )
                    if relevant:
                        return response(relevant, False)

                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                    for board_id in active:
                        await _heartbeat(
                            views[board_id],
                            call_agent_name,
                            agent_ids[board_id],
                        )
                    last_heartbeat = now
                if fallback and now >= next_poll:
                    relevant = await poll_selected(
                        [board_id for board_id in active if board_id in fallback]
                    )
                    if relevant:
                        return response(relevant, False)
                    next_poll = now + DEFAULT_POLL_INTERVAL_S
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        # Healthy subscriptions are authoritative wake cues. Refetching an
        # uncued healthy board here would break selective push semantics.
        final_poll = [
            board_id for board_id in active if board_id in fallback
        ]
    else:
        cycle = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(DEFAULT_POLL_INTERVAL_S, remaining))
            now = time.monotonic()
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                for board_id in active:
                    await _heartbeat(
                        views[board_id], call_agent_name, agent_ids[board_id]
                    )
                last_heartbeat = now
            offset = cycle % len(active)
            interleaved = active[offset:] + active[:offset]
            cycle += 1
            relevant = await poll_selected(interleaved)
            if relevant:
                return response(relevant, False)

    # Final boundary refetch catches mutations racing timeout.
    relevant = await poll_selected(final_poll)
    if relevant:
        return response(relevant, False)
    return response([], True)


async def _wait_for_work(
    client: BoardClient,
    *,
    since_seq: int = 0,
    timeout_s: int = 180,
    only_mine: bool = True,
    project: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Testable wait implementation with identity kept entirely call-local."""
    budget = clamp_timeout(timeout_s)
    started = time.monotonic()
    deadline = started + budget
    cursor = max(0, int(since_seq))
    last_heartbeat = started
    resynced = False
    proj = project.strip().lower() if isinstance(project, str) and project.strip() else None
    explicit_name = agent_name is not None
    call_agent_name = AGENT_NAME if agent_name is None else agent_name
    if not isinstance(call_agent_name, str) or not call_agent_name:
        raise ValueError("agent_name must be a non-empty string")
    if client.identity is None:
        raise RuntimeError("BoardClient has no default joined identity")
    my_agent_id = _derived_agent_id(
        client.identity.principal_id, call_agent_name
    )
    if explicit_name:
        joined = await _join_for_call(client, call_agent_name, True)
        if joined.get("agent_id") != my_agent_id:
            raise BoardClientError("server returned an unexpected per-call agent_id")

    async def poll_once() -> list[dict]:
        nonlocal cursor, resynced
        events, cursor, did_resync = await _catchup_all(
            client, cursor, call_agent_name, explicit_name
        )
        if did_resync:
            resynced = True
        return await _filter_relevant(
            client, events, my_agent_id, only_mine, proj
        )

    # 1. CHECK BEFORE BLOCKING. Journal events advance the cursor; synthetic
    # backlog cues never carry or fabricate a sequence number.
    relevant = await poll_once()
    backlog = await _scan_open_backlog(
        client, my_agent_id, only_mine, proj
    )
    journal_ticket_ids = {
        event.get("ticket_id") for event in relevant if event.get("ticket_id")
    }
    relevant.extend(
        event for event in backlog
        if event.get("ticket_id") not in journal_ticket_ids
    )
    if relevant:
        return {
            "new_seq": cursor,
            "events": relevant,
            "waited_s": 0.0,
            "timed_out": False,
            "resynced": resynced,
        }

    # 2. In dark-launch push mode, listen only for a wake cue. Every cue is
    # followed by the exact same journal refetch/filter path as polling. The
    # stable journal URI is published alongside every specific payload update,
    # including tickets which did not exist when this call began.
    if WAIT_MODE == "push":
        try:
            mcp_client = getattr(client, "_client", None)
            listen = getattr(mcp_client, "listen", None)
            if not callable(listen):
                raise RuntimeError("BoardClient transport does not expose listen()")
            journal_uri = f"board://{BOARD_ID}/journal"
            async with listen(resource_subscriptions=[journal_uri]) as subscription:
                honored = {
                    str(uri)
                    for uri in (
                        subscription.honored.resource_subscriptions or ()
                    )
                }
                if journal_uri not in honored:
                    raise RuntimeError(
                        "server did not honor journal subscription: "
                        f"{journal_uri}"
                    )
                while True:
                    now = time.monotonic()
                    remaining = deadline - now
                    if remaining <= 0:
                        break
                    heartbeat_due_in = max(
                        0.0, HEARTBEAT_INTERVAL_S - (now - last_heartbeat)
                    )
                    wait_slice = min(remaining, heartbeat_due_in)
                    if wait_slice > 0:
                        try:
                            async with asyncio.timeout(wait_slice):
                                await anext(subscription)
                        except TimeoutError:
                            pass
                        else:
                            relevant = await poll_once()
                            if relevant:
                                return {
                                    "new_seq": cursor,
                                    "events": relevant,
                                    "waited_s": round(
                                        time.monotonic() - started, 2
                                    ),
                                    "timed_out": False,
                                    "resynced": resynced,
                                }

                    now = time.monotonic()
                    if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                        await _heartbeat(client, call_agent_name, my_agent_id)
                        last_heartbeat = now

                # Match poll mode's final boundary refetch: a mutation racing
                # the timeout is still found even if its notification has not
                # reached this process yet.
                relevant = await poll_once()
                if relevant:
                    return {
                        "new_seq": cursor,
                        "events": relevant,
                        "waited_s": round(time.monotonic() - started, 2),
                        "timed_out": False,
                        "resynced": resynced,
                    }
        except Exception as exc:
            # Push is an optimization only. Unsupported protocol versions,
            # rejected filters, stream loss, and transport errors all retain
            # the proven poll path for the rest of this call.
            _log(f"push unavailable; falling back to poll for this call: {exc}")

    # 3. Poll until relevant work appears or the budget runs out. This is the
    # default path and the whole-call fallback after any push failure.
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(DEFAULT_POLL_INTERVAL_S, remaining))

        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
            await _heartbeat(client, call_agent_name, my_agent_id)
            last_heartbeat = now

        relevant = await poll_once()
        if relevant:
            return {
                "new_seq": cursor,
                "events": relevant,
                "waited_s": round(time.monotonic() - started, 2),
                "timed_out": False,
                "resynced": resynced,
            }

    # 4. Timed out -- the re-arm cue.
    return {
        "new_seq": cursor,
        "events": [],
        "waited_s": round(time.monotonic() - started, 2),
        "timed_out": True,
        "resynced": resynced,
    }


def main() -> None:
    if "--version" in sys.argv[1:]:
        print(VERSION)
        return
    if not CENTRAL_TOKEN:
        print("FATAL: ONBOARD_CENTRAL_TOKEN is not set", file=sys.stderr)
        raise SystemExit(1)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
