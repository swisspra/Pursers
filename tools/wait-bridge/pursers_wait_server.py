#!/usr/bin/env python3
"""Poll-based "wait for work" MCP bridge for the Pursers / On Board v5 central.

WHY THIS EXISTS
    The live central (0.1.0a6, dev-auth) does not implement MCP v2
    subscriptions ("subscriptions/listen"), so a listener cannot get pushed a
    wakeup when a peer creates or reassigns a ticket. This bridge replicates
    v4's blocking a2a_wait primitive (see On_Board-local/a2a_wait.py) purely
    with short polling HTTP calls to the central: `board_catchup` for events
    and `lease_renew` as a liveness heartbeat.

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
import hashlib
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, AsyncIterator

from onboard_client import BoardClient, BoardClientError
from mcp.server.mcpserver import Context, MCPServer
from agent_naming import resolve_agent_name
from backlog import backlog_events, ticket_is_relevant

# --- config from env -------------------------------------------------------

CENTRAL_URL = os.environ.get("ONBOARD_CENTRAL_URL", "https://127.0.0.1:8766/mcp")
BOARD_ID = os.environ.get("ONBOARD_BOARD_ID", "pursers")
CENTRAL_TOKEN = os.environ.get("ONBOARD_CENTRAL_TOKEN", "")
BASE_AGENT_NAME = os.environ.get("ONBOARD_AGENT_NAME", "pursers-wait-bridge")
AGENT_NAME = resolve_agent_name(
    BASE_AGENT_NAME, os.environ.get("ONBOARD_AGENT_INSTANCE")
)

if not CENTRAL_TOKEN:
    print("FATAL: ONBOARD_CENTRAL_TOKEN is not set", file=sys.stderr)
    raise SystemExit(1)

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


def clamp_timeout(timeout_s: Any) -> int:
    try:
        t = int(timeout_s)
    except (TypeError, ValueError):
        t = DEFAULT_TIMEOUT_S
    return max(1, min(t, DESKTOP_SAFE_MAX_S))


def _log(msg: str) -> None:
    # stderr only -- stdout is the stdio JSON-RPC channel.
    print(f"[a2a_wait] {msg}", file=sys.stderr, flush=True)


@lru_cache(maxsize=1_024)
def _derived_agent_id(principal_id: str, agent_name: str) -> str:
    """Pure Central-compatible identity derivation; safe to memoize."""
    logical = json.dumps(
        [BOARD_ID, principal_id, agent_name], separators=(",", ":")
    )
    return "AI-" + hashlib.sha256(logical.encode("utf-8")).hexdigest()


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
    client = BoardClient(CENTRAL_URL, CENTRAL_TOKEN, BOARD_ID, agent_name=AGENT_NAME)
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
    since_seq: int = 0,
    timeout_s: int = 180,
    only_mine: bool = True,
    project: str | None = None,
    agent_name: str | None = None,
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
    return await _wait_for_work(
        client,
        since_seq=since_seq,
        timeout_s=timeout_s,
        only_mine=only_mine,
        project=project,
        agent_name=agent_name,
    )


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

    # 2. Poll until relevant work appears or the budget runs out.
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

    # 3. Timed out -- the re-arm cue.
    return {
        "new_seq": cursor,
        "events": [],
        "waited_s": round(time.monotonic() - started, 2),
        "timed_out": True,
        "resynced": resynced,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
