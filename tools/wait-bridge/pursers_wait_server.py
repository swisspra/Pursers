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
      1. CHECK BEFORE BLOCKING: fully drain board_catchup from since_seq. If
         any relevant event already accrued, return immediately (no wait).
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
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from onboard_client import BoardClient, BoardClientError
from mcp.server.mcpserver import Context, MCPServer
from agent_naming import resolve_agent_name

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
RELEVANT_KINDS = frozenset({"ticket_created", "ticket_status_changed"})
CLAIMED_STATES = frozenset({"claimed", "in_progress", "creating_report"})
# Only a genuinely open ticket is claimable. Waking a worker for a ticket that
# is already claimed by someone else, submitted, or closed just races into a
# failed claim and a busy re-arm, so those are excluded from open-queue wakeups.
CLAIMABLE_STATES = frozenset({"open"})


def clamp_timeout(timeout_s: Any) -> int:
    try:
        t = int(timeout_s)
    except (TypeError, ValueError):
        t = DEFAULT_TIMEOUT_S
    return max(1, min(t, DESKTOP_SAFE_MAX_S))


def _log(msg: str) -> None:
    # stderr only -- stdout is the stdio JSON-RPC channel.
    print(f"[a2a_wait] {msg}", file=sys.stderr, flush=True)


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


async def _catchup_all(client: BoardClient, cursor: int) -> tuple[list[dict], int, bool]:
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
        page = await client.board_catchup(cursor=cursor, limit=CATCHUP_PAGE_LIMIT, ack=False)
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


def _ticket_project(ticket: dict) -> str | None:
    """Project slug of a ticket = the first path segment of its target_url.

    Convention (see the worker directive): a work-item ticket sets
    target_url = "<project-slug>/<path...>", so one shared Pursers board can
    carry many projects and a worker can wait on only its own. Returns a
    lowercased slug, or None when target_url is empty/untagged (an untagged
    ticket never matches a project filter, so it stays visible only to an
    unfiltered listener / the cross-project orchestrator)."""
    target = (ticket.get("target_url") or "").strip()
    if not target:
        return None
    first = target.replace(":", "/").split("/", 1)[0].strip().lower()
    return first or None


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
        if project is not None and _ticket_project(ticket) != project:
            return False
        if not only_mine:
            return True
        status = ticket.get("status")
        claimed_by = ticket.get("claimed_by_agent_id")
        assigned = ticket.get("assigned_to_agent_id")
        # Work I should resume: I created it, hold its claim, or it's assigned to me.
        mine = (
            ticket.get("created_by_agent_id") == my_agent_id
            or claimed_by == my_agent_id
            or assigned == my_agent_id
        )
        # Claimable open work: genuinely open, unclaimed, and unassigned -- so a
        # wakeup will not race into a failed claim on a ticket already taken.
        claimable = (
            status in CLAIMABLE_STATES
            and claimed_by in (None, my_agent_id)
            and assigned in (None, my_agent_id)
        )
        return mine or claimable
    # No project filter and not only_mine: every relevant-kind event counts.
    return True


async def _filter_relevant(
    client: BoardClient, events: list[dict], only_mine: bool, project: str | None
) -> list[dict]:
    my_agent_id = client.identity.agent_id if client.identity else None
    out = []
    for ev in events:
        if await _is_relevant(client, ev, my_agent_id, only_mine, project):
            out.append(ev)
    return out


async def _heartbeat(client: BoardClient) -> None:
    """Best-effort lease_renew for any ticket this agent currently holds.

    Never raises -- a heartbeat failure must not abort the wait. If this
    agent holds no active claim there is nothing to renew, which is the
    common case for an idle listener; that is logged, not treated as error.
    """
    try:
        listed = await client.ticket_list(assigned_to=AGENT_NAME, include_closed=False, limit=50)
    except Exception as exc:
        _log(f"heartbeat: ticket_list failed: {exc}")
        return
    my_agent_id = client.identity.agent_id if client.identity else None
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
            _log(f"heartbeat: lease_renew {ticket_id} -> expires {renewed.get('lease_expires_at')}")
        except Exception as exc:
            _log(f"heartbeat: lease_renew {ticket_id} failed: {exc}")


@mcp.tool()
async def a2a_wait(
    ctx: Context,
    since_seq: int = 0,
    timeout_s: int = 180,
    only_mine: bool = True,
    project: str | None = None,
) -> dict[str, Any]:
    """Block until pursers board work arrives, or until timeout_s elapses.

    CHECK-BEFORE-BLOCKING: any backlog accrued since since_seq is drained and
    returned without waiting, so a re-arm after a long gap costs one call.
    Otherwise polls every ~2s, firing a lease_renew heartbeat roughly every
    20s for any ticket this agent holds, until a relevant event appears or
    timeout_s (clamped to a desktop-safe ceiling) elapses.

    project: when set (case-insensitive), only tickets whose target_url starts
    with "<project>/" match -- this is how one shared Pursers board serves
    several projects without a worker seeing another project's queue. Leave it
    unset to see every project (the cross-project orchestrator view).

    Returns {new_seq, events, waited_s, timed_out, resynced}. timed_out=True
    means "no work" -- call again with since_seq=new_seq to re-arm. resynced=True
    means the journal was compacted past our cursor and events were lost:
    re-fetch full state (e.g. ticket_list) before trusting events as complete.

    HEARTBEAT SCOPE: the lease_renew heartbeat only fires while THIS call is
    blocking. It does NOT run while you are executing ticket work between
    a2a_wait calls -- during long work you must renew your own claim (lease_renew)
    or the reaper can reclaim it. See WORKER-DIRECTIVE.md step DO.
    """
    budget = clamp_timeout(timeout_s)
    started = time.monotonic()
    deadline = started + budget
    cursor = max(0, int(since_seq))
    last_heartbeat = started
    resynced = False
    proj = project.strip().lower() if isinstance(project, str) and project.strip() else None

    client: BoardClient = ctx.request_context.lifespan_context["client"]

    async def poll_once() -> list[dict]:
        nonlocal cursor, resynced
        events, cursor, did_resync = await _catchup_all(client, cursor)
        if did_resync:
            resynced = True
        return await _filter_relevant(client, events, only_mine, proj)

    # 1. CHECK BEFORE BLOCKING.
    relevant = await poll_once()
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
            await _heartbeat(client)
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
