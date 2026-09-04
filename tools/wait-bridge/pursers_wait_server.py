#!/usr/bin/env python3
"""Wait-for-work MCP bridge for the Pursers / On Board v5 central.

WHY THIS EXISTS
    This bridge replicates v4's blocking a2a_wait primitive using MCP v2
    subscriptions/listen by default. Polling is an explicit compatibility
    fallback. Notifications are cues; committed journal state remains data.

TRANSPORT
    This server MUST run over stdio (the host spawns it as a subprocess).
    stdio has no per-request timer, so the tool call can genuinely block for
    the requested timeout_s. Do not put this behind mcp-remote / HTTP -- an
    HTTP transport would apply its own request timeout and defeat the block.

THE TOOL
    a2a_wait(since_seq=0, timeout_s=180, only_mine=True, wait_for="auto")
      1. CHECK BEFORE BLOCKING: fully drain board_catchup from since_seq and
         scan current claimable or submitted tickets older than the cursor.
         If relevant work is found on either path, return immediately.
      2. Otherwise wait on journal and per-seat subscriptions. Poll only when
         explicitly selected or when listen fails for this call. Entry-snapshot
         held tickets receive lease_renew at min(300s, ttl/3); idle seats issue
         no Central calls while blocked.
      3. Return a bounded shape including reason=journal|backlog|timeout.
         timed_out=True is the re-arm cue: call again with since_seq=new_seq.

RELEVANCE
    The live journal event only carries {kind, ticket_id, status_from,
    status_to, actor, ...} -- no assignee/creator. board_catchup itself
    already drops self-authored events and events this agent is not a
    recipient of (recipient_identities is "every other member" for tickets),
    so what board_catchup hands back is already "not mine to have caused."
    For claimable waits, only_mine=True narrows that further with one ticket_get
    per candidate event: relevant iff the ticket is unclaimed/unassigned (the
    open queue), or the agent created it, is assigned to it, or holds its claim.
    Submitted waits instead accept only submission/resubmission/review-lease
    events and available submitted backlog for a board:review identity.
    memory_written is intentionally ignored -- this tool is a work-arrival
    signal, not a memory watcher (matches v4's DEFAULT_KINDS posture).
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from collections import OrderedDict
from contextlib import aclosing, asynccontextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator

from pursers_client import (
    GENERATION_META_KEY,
    BoardClient,
    BoardClientError,
    JoinedIdentity,
    parse_project_registry,
)
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from agent_naming import resolve_agent_name
from backlog import (
    WAIT_FOR_CLAIMABLE,
    WAIT_FOR_SUBMITTED,
    backlog_events,
    ticket_is_relevant,
)

VERSION = "0.1.0a6"

# --- config from env -------------------------------------------------------

CENTRAL_URL = os.environ.get("ONBOARD_CENTRAL_URL", "https://127.0.0.1:8766/mcp")
BOARD_ID = os.environ.get("ONBOARD_BOARD_ID", "pursers")
CENTRAL_TOKEN = os.environ.get("ONBOARD_CENTRAL_TOKEN", "")
BASE_AGENT_NAME = os.environ.get("ONBOARD_AGENT_NAME", "pursers-wait-bridge")
AGENT_NAME = resolve_agent_name(
    BASE_AGENT_NAME, os.environ.get("ONBOARD_AGENT_INSTANCE")
)
_RAW_WAIT_MODE = os.environ.get("PURSERS_WAIT_MODE", "push").strip().lower()
WAIT_MODE = _RAW_WAIT_MODE if _RAW_WAIT_MODE in {"poll", "push"} else "push"

# --- wait policy (v4-parity constants; see a2a_wait.py) --------------------

DEFAULT_TIMEOUT_S = 180
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_CLAIM_TTL_S = 900
MAX_LEASE_RENEW_INTERVAL_S = 300.0
PROGRESS_INTERVAL_S = 300.0
CATCHUP_PAGE_LIMIT = 100
BACKLOG_SCAN_LIMIT = 100
CLAIMABLE_RELEVANT_KINDS = frozenset(
    {"ticket_created", "ticket_status_changed"}
)
SUBMITTED_RELEVANT_KINDS = frozenset(
    {
        "ticket_status_changed",
        "ticket_submitted",
        "ticket_resubmitted",
        "review_lease_changed",
        "review_lease_released",
        "review_lease_expired",
    }
)
RELEVANT_KINDS = CLAIMABLE_RELEVANT_KINDS | SUBMITTED_RELEVANT_KINDS
WAIT_FOR_AUTO = "auto"
WAIT_FOR_VALUES = frozenset(
    {WAIT_FOR_AUTO, WAIT_FOR_CLAIMABLE, WAIT_FOR_SUBMITTED}
)
BACKLOG_SUPPRESSION_LIMIT = 500
_BACKLOG_SEEN: OrderedDict[tuple[str, str, str], str] = OrderedDict()
CLAIMED_STATES = frozenset({"claimed", "in_progress", "creating_report"})
HANDOFF_REJOIN_MESSAGE = "call board_onboard or board_join before more work"
PROJECT_REGISTRY_KEY = "project_registry"
STATS_SCHEMA_VERSION = 3
STATS_RETENTION_DAYS = 7
POLL_SAMPLE_LIMIT = 24
WAIT_HOUR_RETENTION = 48
CONTEXT_READ_TOOLS = frozenset(
    {"board_get_briefing", "board_onboard", "board_snapshot", "board_catchup"}
)
HOST_TIMEOUTS_S = {
    "codex": 620,
    "codex-cli": 620,
    "goose": 300,
    "claude-code": 21_600,
    "claude-desktop": 240,
    "headless": 21_600,
}
DEFAULT_HOST = "codex"


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


def _parse_stats_hour(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:00:00Z").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


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
        self._poll_cycle: ContextVar[dict[tuple[str, str], int] | None] = (
            ContextVar(f"bridge_poll_cycle_{id(self)}", default=None)
        )

    @asynccontextmanager
    async def poll_cycle(self) -> AsyncIterator[None]:
        """Collect one bounded context-response sample per touched seat."""
        token = self._poll_cycle.set({})
        try:
            yield
        finally:
            samples = self._poll_cycle.get() or {}
            self._poll_cycle.reset(token)
            for (board_id, agent_name), response_bytes in samples.items():
                try:
                    async with self._lock:
                        self._append_poll_sample_sync(
                            board_id, agent_name, response_bytes
                        )
                except Exception as exc:  # noqa: BLE001 - metering never breaks work.
                    _log(f"stats sample write failed: {type(exc).__name__}")

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
                cycle = self._poll_cycle.get()
                if cycle is not None and tool_name in CONTEXT_READ_TOOLS:
                    key = (board_id, agent_name)
                    cycle[key] = cycle.get(key, 0) + max(0, int(response_bytes))
        except Exception as exc:  # noqa: BLE001 - metering never breaks work.
            _log(f"stats write failed: {type(exc).__name__}")

    async def record_wait_return(
        self,
        board_id: str,
        agent_name: str,
        result: dict[str, Any],
    ) -> None:
        """Record exactly one model-visible a2a_wait result."""
        outcome = "timeout" if result.get("timed_out") else "cue"
        try:
            async with self._lock:
                self._record_wait_return_sync(
                    board_id,
                    agent_name,
                    outcome,
                    _meter_bytes(result),
                )
        except Exception as exc:  # noqa: BLE001 - metering never breaks work.
            _log(f"wait-return stats write failed: {type(exc).__name__}")

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
            except (FileNotFoundError, UnicodeError, ValueError, OSError):
                document = {}
            if not isinstance(document, dict):
                document = {}
            days = document.get("days")
            if not isinstance(days, dict):
                days = {}
            today_date = self.clock().astimezone(timezone.utc).date()
            today = today_date.isoformat()
            first_day = (today_date - timedelta(days=STATS_RETENTION_DAYS - 1)).isoformat()
            valid_days = set()
            for raw_day in days:
                try:
                    parsed_day = date.fromisoformat(str(raw_day))
                except ValueError:
                    continue
                day = parsed_day.isoformat()
                if day == raw_day and first_day <= day <= today:
                    valid_days.add(day)
            retained = sorted(
                valid_days | {today}
            )
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
                "poll_cycles": (
                    document.get("poll_cycles")
                    if isinstance(document.get("poll_cycles"), dict)
                    else {}
                ),
                "model_wait": (
                    document.get("model_wait")
                    if isinstance(document.get("model_wait"), dict)
                    else {}
                ),
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

    def _append_poll_sample_sync(
        self, board_id: str, agent_name: str, response_bytes: int
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, UnicodeError, ValueError, OSError):
                document = {}
            if not isinstance(document, dict):
                document = {}
            poll_cycles = document.get("poll_cycles")
            if not isinstance(poll_cycles, dict):
                poll_cycles = {}
            seat_key = json.dumps([board_id, agent_name], separators=(",", ":"))
            raw_seat = poll_cycles.get(seat_key)
            samples = raw_seat.get("samples") if isinstance(raw_seat, dict) else []
            samples = samples if isinstance(samples, list) else []
            at = self.clock().astimezone(timezone.utc).isoformat()
            bounded_samples = [
                sample
                for sample in samples[-(POLL_SAMPLE_LIMIT - 1) :]
                if isinstance(sample, dict)
                and isinstance(sample.get("at"), str)
                and type(sample.get("response_bytes")) is int
                and sample["response_bytes"] >= 0
            ]
            bounded_samples.append(
                {"at": at, "response_bytes": max(0, int(response_bytes))}
            )
            poll_cycles[seat_key] = {
                "board_id": board_id,
                "agent_name": agent_name,
                "latest_at": at,
                "latest_response_bytes": max(0, int(response_bytes)),
                "samples": bounded_samples[-POLL_SAMPLE_LIMIT:],
            }
            output = {
                "schema_version": STATS_SCHEMA_VERSION,
                "days": (
                    document.get("days")
                    if isinstance(document.get("days"), dict)
                    else {}
                ),
                "poll_cycles": poll_cycles,
                "model_wait": (
                    document.get("model_wait")
                    if isinstance(document.get("model_wait"), dict)
                    else {}
                ),
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

    def _record_wait_return_sync(
        self,
        board_id: str,
        agent_name: str,
        outcome: str,
        response_bytes: int,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, UnicodeError, ValueError, OSError):
                document = {}
            if not isinstance(document, dict):
                document = {}
            now = self.clock().astimezone(timezone.utc)
            hour = now.strftime("%Y-%m-%dT%H:00:00Z")
            first = now - timedelta(hours=WAIT_HOUR_RETENTION - 1)
            model_wait = document.get("model_wait")
            if not isinstance(model_wait, dict):
                model_wait = {}
            seat_key = json.dumps([board_id, agent_name], separators=(",", ":"))
            seat = model_wait.get(seat_key)
            if not isinstance(seat, dict):
                seat = {"board_id": board_id, "agent_name": agent_name, "hours": {}}
            hours = seat.get("hours")
            hours = hours if isinstance(hours, dict) else {}
            retained: dict[str, Any] = {}
            for key, value in hours.items():
                parsed = _parse_stats_hour(key)
                if parsed is not None and first <= parsed <= now and isinstance(value, dict):
                    retained[key] = value
            bucket = retained.setdefault(
                hour,
                {"returns": 0, "response_bytes": 0, "outcomes": {}},
            )
            bucket["returns"] = int(bucket.get("returns", 0)) + 1
            bucket["response_bytes"] = int(bucket.get("response_bytes", 0)) + max(
                0, int(response_bytes)
            )
            outcomes = bucket.get("outcomes")
            outcomes = outcomes if isinstance(outcomes, dict) else {}
            outcomes[outcome] = int(outcomes.get(outcome, 0)) + 1
            bucket["outcomes"] = outcomes
            seat["hours"] = retained
            model_wait[seat_key] = seat
            output = {
                "schema_version": STATS_SCHEMA_VERSION,
                "days": document.get("days") if isinstance(document.get("days"), dict) else {},
                "poll_cycles": (
                    document.get("poll_cycles")
                    if isinstance(document.get("poll_cycles"), dict)
                    else {}
                ),
                "model_wait": model_wait,
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


def _host_timeout_s() -> int:
    explicit = os.environ.get("PURSERS_HOST_TIMEOUT_S", "").strip()
    if explicit:
        try:
            value = int(explicit)
            if value > 1:
                return value
        except ValueError:
            pass
        _log("invalid PURSERS_HOST_TIMEOUT_S; using named host profile")
    host = os.environ.get("PURSERS_HOST", DEFAULT_HOST).strip().lower()
    if host not in HOST_TIMEOUTS_S:
        _log(f"invalid PURSERS_HOST={host!r}; using {DEFAULT_HOST!r}")
        host = DEFAULT_HOST
    return HOST_TIMEOUTS_S[host]


def _timeout_margin_s(host_timeout_s: int) -> int:
    margin = min(60, max(30, math.ceil(host_timeout_s * 0.10)))
    if os.environ.get("PURSERS_HOST", DEFAULT_HOST).strip().lower() == "claude-desktop":
        margin = max(margin, 40)
    return min(host_timeout_s - 1, margin)


def host_block_limit_s() -> int:
    timeout = _host_timeout_s()
    return max(1, timeout - _timeout_margin_s(timeout))


def clamp_timeout(timeout_s: Any) -> int:
    try:
        t = int(timeout_s)
    except (TypeError, ValueError):
        t = DEFAULT_TIMEOUT_S
    return max(1, min(t, host_block_limit_s()))


def _progress_cadence_s() -> float | None:
    host = os.environ.get("PURSERS_HOST", DEFAULT_HOST).strip().lower()
    return PROGRESS_INTERVAL_S if host == "claude-code" else None


def _log(msg: str) -> None:
    # stderr only -- stdout is the stdio JSON-RPC channel.
    print(f"[a2a_wait] {msg}", file=sys.stderr, flush=True)


if _RAW_WAIT_MODE not in {"poll", "push"}:
    _log(f"invalid PURSERS_WAIT_MODE={_RAW_WAIT_MODE!r}; using push")


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
        self._parent = parent
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

    async def board_join(
        self,
        *,
        agent_name: str | None = None,
        task_focus: str | None = None,
    ) -> dict[str, Any]:
        selected = self.agent_name if agent_name is None else agent_name
        arguments = {"agent_name": selected}
        if task_focus is not None:
            arguments["task_focus"] = task_focus
        joined = await self._call(
            "board_join", arguments, refresh=True
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


class BoardJoinFailure(ToolError):
    """Stable, non-sensitive classification for deferred startup failures."""

    def __init__(self, cause_class: str, detail: str) -> None:
        self.cause_class = cause_class
        super().__init__(f"board join failed ({cause_class}): {detail}")


def _nested_exceptions(exc: BaseException) -> list[BaseException]:
    pending = [exc]
    nested: list[BaseException] = []
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        nested.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)
    return nested


def _seat_subscription_can_degrade(exc: BaseException) -> bool:
    """Return whether a journal+seat listen should retry as journal-only."""
    detail = " ".join(str(item) for item in _nested_exceptions(exc)).casefold()
    return any(
        marker in detail
        for marker in (
            "subscription denied",
            "server did not honor subscriptions",
        )
    )


def _classify_board_join_failure(exc: BaseException) -> BoardJoinFailure:
    nested = _nested_exceptions(exc)
    names = {type(item).__name__ for item in nested}
    text = " ".join(str(item) for item in nested).casefold()
    unreachable_names = {
        "ConnectError",
        "ConnectTimeout",
        "NetworkError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "TimeoutError",
    }
    if names & unreachable_names or any(
        marker in text
        for marker in (
            "all connection attempts failed",
            "connection refused",
            "name or service not known",
            "nodename nor servname provided",
        )
    ):
        return BoardJoinFailure("unreachable", "Central is unreachable")
    if any(
        marker in text
        for marker in (
            "401",
            "403",
            "authentication",
            "forbidden",
            "invalid token",
            "unauthorized",
        )
    ) or (
        "MCPError" in names and "server returned an error response" in text
    ):
        return BoardJoinFailure(
            "auth", "Central rejected ONBOARD_CENTRAL_TOKEN"
        )
    return BoardJoinFailure("board", "Central rejected board join")


class DeferredBoardConnection:
    """Own a persistent BoardClient without touching Central before initialize."""

    JOIN_TIMEOUT_S = 10.0
    CLOSE_TIMEOUT_S = 5.0

    def __init__(self, meter: BridgeStats) -> None:
        self.meter = meter
        self._client: MeteredBoardClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[
            tuple[MeteredBoardClient | None, BoardJoinFailure | None]
        ] | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()
        self._failure_logged = False

    def _report_failure(self, failure: BoardJoinFailure) -> None:
        if self._failure_logged:
            return
        self._failure_logged = True
        _log(f"board join deferred/failed: {failure.cause_class}")

    async def _run(
        self,
        ready: asyncio.Future[
            tuple[MeteredBoardClient | None, BoardJoinFailure | None]
        ],
        stop: asyncio.Event,
    ) -> None:
        client = MeteredBoardClient(
            CENTRAL_URL,
            CENTRAL_TOKEN,
            BOARD_ID,
            agent_name=AGENT_NAME,
            meter=self.meter,
        )
        entered = False
        try:
            try:
                async with asyncio.timeout(self.JOIN_TIMEOUT_S):
                    await client.__aenter__()
                entered = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = _classify_board_join_failure(exc)
                self._report_failure(failure)
                if not ready.done():
                    ready.set_result((None, failure))
                return
            self._client = client
            if not ready.done():
                ready.set_result((client, None))
            _log(
                f"joined board={BOARD_ID!r} as agent={AGENT_NAME!r} "
                f"agent_id={client.identity.agent_id if client.identity else '?'}"
            )
            await stop.wait()
        finally:
            if not ready.done():
                ready.set_result(
                    (
                        None,
                        BoardJoinFailure(
                            "board", "board join was cancelled before completion"
                        ),
                    )
                )
            if entered:
                await client.__aexit__(None, None, None)
            if self._client is client:
                self._client = None

    async def client(self) -> MeteredBoardClient:
        if not CENTRAL_TOKEN:
            failure = BoardJoinFailure(
                "configuration", "ONBOARD_CENTRAL_TOKEN is not set"
            )
            self._report_failure(failure)
            raise failure
        async with self._lock:
            if self._client is not None:
                return self._client
            if self._task is None or self._task.done():
                loop = asyncio.get_running_loop()
                self._ready = loop.create_future()
                self._stop = asyncio.Event()
                self._task = asyncio.create_task(
                    self._run(self._ready, self._stop),
                    name="pursers-deferred-board-join",
                )
            ready = self._ready
        if ready is None:  # pragma: no cover - guarded by the lock above.
            raise RuntimeError("deferred board join has no readiness signal")
        client, failure = await asyncio.shield(ready)
        if failure is not None:
            raise failure
        if client is None:  # pragma: no cover - readiness tuple is exhaustive.
            raise RuntimeError("deferred board join returned no client")
        return client

    async def close(self) -> None:
        async with self._lock:
            task = self._task
            stop = self._stop
        if task is None:
            return
        if stop is not None:
            stop.set()
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self.CLOSE_TIMEOUT_S
            )
        except TimeoutError:
            task.cancel()
            try:
                await task
            except BaseException:
                pass


@asynccontextmanager
async def _lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Create only local state before initialize; join lazily on first tool."""
    meter = BridgeStats(bridge_stats_path())
    connection = DeferredBoardConnection(meter)
    try:
        yield {"connection": connection}
    finally:
        await connection.close()


mcp = MCPServer("Pursers Wait Bridge", version="0.1.0", lifespan=_lifespan)


async def _client_for_tool(ctx: Context) -> BoardClient:
    lifespan = ctx.request_context.lifespan_context
    client = lifespan.get("client")
    if client is not None:
        return client
    connection: DeferredBoardConnection = lifespan["connection"]
    return await connection.client()


def _parse_project_registry(result: dict[str, Any]) -> dict[str, Any]:
    """Compatibility wrapper around the client package's shared parser."""
    return parse_project_registry(result)


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
    client = await _client_for_tool(ctx)
    return await _read_project_registry(client)


async def _catchup_all(
    client: BoardClient,
    cursor: int,
    agent_name: str,
    explicit_name: bool,
    *,
    prefer_pure: bool = False,
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
        pure_requested = prefer_pure and getattr(
            client, "_pursers_pure_catchup", None
        ) is not False
        if pure_requested:
            catchup_args["touch"] = False
        if explicit_name:
            catchup_args["agent_name"] = agent_name
        try:
            page = await client.board_catchup(**catchup_args)
        except BoardClientError as exc:
            message = str(exc).lower()
            if pure_requested and any(
                marker in message
                for marker in ("touch", "unexpected keyword", "extra input", "not permitted")
            ):
                setattr(client, "_pursers_pure_catchup", False)
                catchup_args.pop("touch", None)
                _log(
                    "WARNING: side-effect-free board_catchup is unavailable; "
                    "using ack=False compatibility refetch for this deployment"
                )
                page = await client.board_catchup(**catchup_args)
            elif not explicit_name or HANDOFF_REJOIN_MESSAGE not in str(exc):
                raise
            else:
                _log(f"agent={agent_name!r}: handed off; rejoining once")
                await _join_for_call(client, agent_name, explicit_name)
                page = await client.board_catchup(**catchup_args)
        else:
            if pure_requested:
                setattr(client, "_pursers_pure_catchup", True)
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


def _normalize_wait_for(wait_for: str) -> str:
    if not isinstance(wait_for, str):
        raise ValueError("wait_for must be 'auto', 'claimable', or 'submitted'")
    normalized = wait_for.strip().lower()
    if normalized not in WAIT_FOR_VALUES:
        raise ValueError("wait_for must be 'auto', 'claimable', or 'submitted'")
    return normalized


def _resolve_wait_for(wait_for: str, role: str | None) -> str:
    selected = _normalize_wait_for(wait_for)
    if selected == WAIT_FOR_AUTO:
        return WAIT_FOR_SUBMITTED if role == "reviewer" else WAIT_FOR_CLAIMABLE
    if selected == WAIT_FOR_SUBMITTED and role != "reviewer":
        raise ToolError("wait_for='submitted' requires board:review authorization")
    return selected


def _event_matches_wait(event: dict[str, Any], wait_for: str) -> bool:
    kind = event.get("kind")
    if wait_for == WAIT_FOR_SUBMITTED:
        if kind not in SUBMITTED_RELEVANT_KINDS:
            return False
        if kind == "ticket_status_changed":
            return event.get("status_to") == "submitted"
        return True
    return kind in CLAIMABLE_RELEVANT_KINDS


def _forget_backlog_for_events(board_id: str, events: list[dict]) -> None:
    ticket_ids = {
        event.get("ticket_id")
        for event in events
        if isinstance(event.get("ticket_id"), str)
    }
    if not ticket_ids:
        return
    for key in list(_BACKLOG_SEEN):
        if key[0] == board_id and key[2] in ticket_ids:
            del _BACKLOG_SEEN[key]


def _fresh_backlog_events(
    board_id: str, wait_for: str, events: list[dict]
) -> list[dict]:
    fresh: list[dict] = []
    for event in events:
        ticket_id = event.get("ticket_id")
        if not isinstance(ticket_id, str) or not ticket_id:
            continue
        fingerprint = json.dumps(
            {
                "status": event.get("status"),
                "updated_at": event.get("updated_at"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        key = (board_id, wait_for, ticket_id)
        if _BACKLOG_SEEN.get(key) == fingerprint:
            _BACKLOG_SEEN.move_to_end(key)
            continue
        _BACKLOG_SEEN[key] = fingerprint
        _BACKLOG_SEEN.move_to_end(key)
        fresh.append(event)
    while len(_BACKLOG_SEEN) > BACKLOG_SUPPRESSION_LIMIT:
        _BACKLOG_SEEN.popitem(last=False)
    return fresh


async def _is_relevant(
    client: BoardClient,
    event: dict,
    my_agent_id: str | None,
    only_mine: bool,
    project: str | None,
    wait_for: str = WAIT_FOR_CLAIMABLE,
) -> bool:
    if not _event_matches_wait(event, wait_for):
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
        return ticket_is_relevant(
            ticket, my_agent_id, only_mine, project, wait_for
        )
    # No project filter and not only_mine: every relevant-kind event counts.
    return True


async def _filter_relevant(
    client: BoardClient,
    events: list[dict],
    my_agent_id: str,
    only_mine: bool,
    project: str | None,
    wait_for: str = WAIT_FOR_CLAIMABLE,
) -> list[dict]:
    out = []
    for ev in events:
        if await _is_relevant(
            client, ev, my_agent_id, only_mine, project, wait_for
        ):
            out.append(ev)
    return out


async def _scan_open_backlog(
    client: BoardClient,
    my_agent_id: str,
    only_mine: bool,
    project: str | None,
    held: dict[str, float] | None = None,
    wait_for: str = WAIT_FOR_CLAIMABLE,
    board_id: str = BOARD_ID,
) -> list[dict]:
    """Best-effort scan for work older than the caller's journal cursor."""
    try:
        arguments: dict[str, Any] = {
            "include_closed": False,
            "limit": BACKLOG_SCAN_LIMIT,
        }
        if wait_for == WAIT_FOR_SUBMITTED:
            arguments["status"] = "submitted"
        listed = await client.ticket_list(**arguments)
    except Exception as exc:
        _log(f"backlog scan: ticket_list failed: {exc}")
        return []
    tickets = listed.get("tickets", [])
    if held is not None:
        for ticket in tickets:
            if (
                ticket.get("status") in CLAIMED_STATES
                and ticket.get("claimed_by_agent_id") == my_agent_id
                and isinstance(ticket.get("ticket_id"), str)
            ):
                try:
                    ttl_s = max(3, int(ticket.get("ttl_s") or DEFAULT_CLAIM_TTL_S))
                except (TypeError, ValueError):
                    ttl_s = DEFAULT_CLAIM_TTL_S
                held[ticket["ticket_id"]] = min(
                    MAX_LEASE_RENEW_INTERVAL_S, ttl_s / 3
                )
    projected = backlog_events(
        tickets, my_agent_id, only_mine, project, wait_for
    )
    return _fresh_backlog_events(board_id, wait_for, projected)


async def _renew_due_leases(
    client: BoardClient,
    held: dict[str, float],
    due: dict[str, float],
    now: float,
) -> None:
    """Renew the entry-snapshot of held claims without discovery polling."""
    for ticket_id, interval_s in held.items():
        if now < due.setdefault(ticket_id, now + interval_s):
            continue
        try:
            renewed = await client.lease_renew(ticket_id)
            _log(
                f"lease maintenance: lease_renew {ticket_id} "
                f"-> expires {renewed.get('lease_expires_at')}"
            )
        except Exception as exc:
            _log(f"lease maintenance: lease_renew {ticket_id} failed: {exc}")
        finally:
            due[ticket_id] = now + interval_s


def _maintenance_due_in(
    now: float,
    remaining: float,
    lease_due: dict[str, float],
    next_progress: float | None,
) -> float:
    due = [remaining]
    if lease_due:
        due.append(max(0.0, min(lease_due.values()) - now))
    if next_progress is not None:
        due.append(max(0.0, next_progress - now))
    return min(due)


async def _run_progress(
    callback: Callable[[float, float], Awaitable[None]] | None,
    started: float,
    budget: float,
) -> None:
    if callback is None:
        return
    try:
        await callback(max(0.0, time.monotonic() - started), budget)
    except Exception as exc:
        _log(f"progress notification failed: {type(exc).__name__}")


async def _a2a_wait_impl(
    client: BoardClient,
    since_seq: int | dict[str, int] = 0,
    timeout_s: int = 180,
    only_mine: bool = True,
    project: str | None = None,
    agent_name: str | None = None,
    boards: list[str] | str | None = None,
    wait_for: str = WAIT_FOR_AUTO,
    progress_callback: Callable[[float, float], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Block until pursers board work arrives, or until timeout_s elapses.

    CHECK-BEFORE-BLOCKING: journal backlog accrued since since_seq is drained,
    then current open tickets are scanned for work older than the cursor.
    Relevant work is returned without waiting, so a re-arm after a long gap
    costs one call.
    Otherwise uses subscription-first delivery, with polling only as an
    explicit or per-call compatibility fallback. timeout_s is capped by the
    configured host deadline minus its safety margin.

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

    wait_for: "auto" (default) derives reviewer -> "submitted" and every
    other joined role -> "claimable". An explicit "submitted" request requires
    the joined principal's board:review-backed reviewer role.

    Returns {new_seq, events, waited_s, timed_out, reason, resynced}.
    reason is "journal", "backlog", or "timeout". timed_out=True
    means "no work" -- call again with since_seq=new_seq to re-arm. resynced=True
    means the journal was compacted past our cursor and events were lost:
    re-fetch full state (e.g. ticket_list) before trusting events as complete.

    LEASE SCOPE: the entry snapshot records claims held by this identity, and
    only those ticket IDs are renewed while THIS call blocks. No ticket_list
    heartbeat runs during the wait. During work outside a2a_wait, renew the
    claim directly. See WORKER-DIRECTIVE.md step DO.
    """
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
                wait_for=wait_for,
                progress_callback=progress_callback,
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
            wait_for=wait_for,
            progress_callback=progress_callback,
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
            wait_for=wait_for,
            progress_callback=progress_callback,
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
        wait_for=wait_for,
        progress_callback=progress_callback,
    )


@mcp.tool()
async def a2a_wait(
    ctx: Context,
    since_seq: int | dict[str, int] = 0,
    timeout_s: int = 180,
    only_mine: bool = True,
    project: str | None = None,
    agent_name: str | None = None,
    boards: list[str] | str | None = None,
    wait_for: str = WAIT_FOR_AUTO,
) -> dict[str, Any]:
    """Wait for work and record one model-visible return."""
    client = await _client_for_tool(ctx)
    meter = getattr(client, "meter", None)
    async def progress(elapsed: float, total: float) -> None:
        await ctx.report_progress(
            elapsed,
            total,
            "Waiting for a Pursers subscription cue",
        )

    async def run() -> dict[str, Any]:
        return await _a2a_wait_impl(
            client,
            since_seq,
            timeout_s,
            only_mine,
            project,
            agent_name,
            boards,
            wait_for,
            progress,
        )
    if meter is None:
        return await run()
    async with meter.poll_cycle():
        result = await run()
    selected_agent = AGENT_NAME if agent_name is None else agent_name
    await meter.record_wait_return(BOARD_ID, selected_agent, result)
    return result


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


async def _event_stream(
    parent: BoardClient,
    board_id: str,
    identity: JoinedIdentity,
    generation_token: str | None,
    from_cursor: int,
    cursor_callback: Callable[[int], None],
    *,
    pure_catchup: bool,
) -> AsyncIterator[dict[str, Any]]:
    """Use BoardClient.events for reconnect/dedup over stable cue URIs."""
    custom = getattr(parent, "events_for_board", None)
    if callable(custom):
        custom_stream = custom(
            board_id,
            from_cursor,
            identity,
            cursor_callback,
            generation_token=generation_token,
            pure_catchup=pure_catchup,
        )
        async with aclosing(custom_stream):
            async for event in custom_stream:
                yield event
        return
    journal_uri = f"board://{board_id}/journal"
    seat_uri = f"board://{board_id}/agent/{identity.agent_id}"

    async def stream(resources: list[str]) -> AsyncIterator[dict[str, Any]]:
        # Use a fresh client for each attempt because BoardClient retains every
        # watched URI. Reusing it would silently put the rejected seat URI back.
        event_client = BoardClient(
            parent.url,
            parent.token,
            board_id,
            agent_name=identity.agent_name,
            reconnect_delay_s=parent.reconnect_delay_s,
        )
        event_client.identity = identity
        event_client.generation_token = generation_token
        events = event_client.events(
            from_cursor=from_cursor,
            only_mine=False,
            kinds=RELEVANT_KINDS,
            resource_subscriptions=resources,
            acknowledge=False,
            touch=False if pure_catchup else None,
            cursor_callback=cursor_callback,
        )
        async with aclosing(events):
            async for event in events:
                yield event

    try:
        async for event in stream([journal_uri, seat_uri]):
            yield event
    except Exception as exc:
        if not _seat_subscription_can_degrade(exc):
            raise
        _log(
            "per-seat subscription unavailable; retrying journal-only before "
            f"poll fallback: {exc}"
        )
        async for event in stream([journal_uri]):
            yield event


async def _push_cues(
    board_id: str,
    client: _BoardView,
    cursor: int,
    queue: asyncio.Queue[tuple[str, str, dict[str, Any] | str | None]],
) -> None:
    """Forward authoritative events from BoardClient.events."""
    try:
        def advance(value: int) -> None:
            queue.put_nowait((board_id, "cursor", str(value)))

        events = _event_stream(
            client._parent,
            board_id,
            client.identity,
            client.generation_token,
            cursor,
            advance,
            pure_catchup=getattr(client, "_pursers_pure_catchup", None) is True,
        )
        async with aclosing(events):
            async for event in events:
                await queue.put((board_id, "event", event))
        await queue.put((board_id, "failed", "subscription event stream ended"))
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
    task_focus: str | None = None,
    wait_for: str = WAIT_FOR_AUTO,
    progress_callback: Callable[[float, float], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Wait across independent board cursors and identities on one transport."""
    board_order = _normalize_boards(boards)
    cursors = _multi_cursors(board_order, since_seq)
    resynced = {board_id: False for board_id in board_order}
    skipped: dict[str, str] = {}
    views: dict[str, _BoardView] = {}
    agent_ids: dict[str, str] = {}
    wait_for_by_board: dict[str, str] = {}
    budget = clamp_timeout(timeout_s)
    started = time.monotonic()
    deadline = started + budget
    held_by_board: dict[str, dict[str, float]] = {}
    lease_due_by_board: dict[str, dict[str, float]] = {}
    progress_cadence = _progress_cadence_s() if progress_callback else None
    next_progress = started + progress_cadence if progress_cadence else None
    proj = project.strip().lower() if isinstance(project, str) and project.strip() else None
    call_agent_name = AGENT_NAME if agent_name is None else agent_name
    requested_wait_for = _normalize_wait_for(wait_for)
    if not isinstance(call_agent_name, str) or not call_agent_name:
        raise ValueError("agent_name must be a non-empty string")
    if client.identity is None:
        raise RuntimeError("BoardClient has no default joined identity")
    for board_id in board_order:
        try:
            view = _BoardView(client, board_id)
            joined = await view.board_join(
                agent_name=call_agent_name,
                task_focus=task_focus,
            )
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
        wait_for_by_board[board_id] = _resolve_wait_for(
            requested_wait_for,
            view.identity.role if view.identity is not None else None,
        )
        held_by_board[board_id] = {}
        lease_due_by_board[board_id] = {}

    active = [board_id for board_id in board_order if board_id in views]

    def response(events: list[dict], timed_out: bool) -> dict[str, Any]:
        reason = "timeout"
        if events:
            reason = (
                "backlog"
                if all(event.get("source") == "backlog_scan" for event in events)
                else "journal"
            )
        return {
            "new_seq": dict(cursors),
            "events": events,
            "waited_s": (
                0.0 if events and time.monotonic() - started < 0.005
                else round(time.monotonic() - started, 2)
            ),
            "timed_out": timed_out,
            "reason": reason,
            "resynced": dict(resynced),
            "skipped_boards": dict(skipped),
        }

    async def poll_board(board_id: str, *, backlog: bool = False) -> list[dict]:
        events, next_cursor, did_resync = await _catchup_all(
            views[board_id],
            cursors[board_id],
            call_agent_name,
            True,
            prefer_pure=WAIT_MODE == "push",
        )
        cursors[board_id] = next_cursor
        if did_resync:
            resynced[board_id] = True
        _forget_backlog_for_events(board_id, events)
        relevant = await _filter_relevant(
            views[board_id],
            events,
            agent_ids[board_id],
            only_mine,
            proj,
            wait_for_by_board[board_id],
        )
        if backlog:
            queued = await _scan_open_backlog(
                views[board_id],
                agent_ids[board_id],
                only_mine,
                proj,
                held_by_board[board_id],
                wait_for_by_board[board_id],
                board_id,
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
    for board_id in active:
        lease_due_by_board[board_id] = {
            ticket_id: started + interval
            for ticket_id, interval in held_by_board[board_id].items()
        }
    if relevant:
        return response(relevant, False)
    if not active:
        return response([], True)

    async def maintain(now: float) -> None:
        nonlocal next_progress
        for board_id in active:
            await _renew_due_leases(
                views[board_id],
                held_by_board[board_id],
                lease_due_by_board[board_id],
                now,
            )
        if next_progress is not None and now >= next_progress:
            await _run_progress(progress_callback, started, budget)
            next_progress = now + (progress_cadence or PROGRESS_INTERVAL_S)

    def maintenance_wait(now: float, remaining: float) -> float:
        flat_due = {
            f"{board_id}:{ticket_id}": value
            for board_id, due in lease_due_by_board.items()
            for ticket_id, value in due.items()
        }
        return _maintenance_due_in(now, remaining, flat_due, next_progress)

    final_poll: list[str] = list(active)
    if WAIT_MODE == "push":
        queue: asyncio.Queue[
            tuple[str, str, dict[str, Any] | str | None]
        ] = asyncio.Queue()
        tasks = {
            board_id: asyncio.create_task(
                _push_cues(
                    board_id,
                    views[board_id],
                    cursors[board_id],
                    queue,
                )
            )
            for board_id in active
        }
        fallback: set[str] = set()
        try:
            next_poll = time.monotonic() + DEFAULT_POLL_INTERVAL_S
            while True:
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    break
                poll_due = (
                    max(0.0, next_poll - now) if fallback else remaining
                )
                wait_slice = min(
                    poll_due,
                    maintenance_wait(now, remaining),
                )
                item: tuple[
                    str, str, dict[str, Any] | str | None
                ] | None = None
                if wait_slice > 0:
                    try:
                        async with asyncio.timeout(wait_slice):
                            item = await queue.get()
                    except TimeoutError:
                        pass

                items = [item] if item is not None else []
                while not queue.empty():
                    items.append(queue.get_nowait())
                found: list[dict[str, Any]] = []
                for board_id, kind, detail in items:
                    if kind == "cursor":
                        cursors[board_id] = max(
                            cursors[board_id], int(str(detail))
                        )
                    elif kind == "event" and isinstance(detail, dict):
                        _forget_backlog_for_events(board_id, [detail])
                        sequence = detail.get("seq")
                        if isinstance(sequence, int):
                            cursors[board_id] = max(cursors[board_id], sequence)
                        if await _is_relevant(
                            views[board_id],
                            detail,
                            agent_ids[board_id],
                            only_mine,
                            proj,
                            wait_for_by_board[board_id],
                        ):
                            found.append({**detail, "board_id": board_id})
                    elif kind == "failed":
                        fallback.add(board_id)
                        _log(
                            "WARNING: subscriptions/listen unavailable for "
                            f"board={board_id!r}; polling this board for the "
                            f"remainder of this call and retrying push on re-arm: {detail}"
                        )
                if found:
                    return response(found, False)

                now = time.monotonic()
                await maintain(now)
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
        final_poll = [board_id for board_id in active if board_id in fallback]
    else:
        cycle = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(
                min(
                    DEFAULT_POLL_INTERVAL_S,
                    maintenance_wait(time.monotonic(), remaining),
                )
            )
            now = time.monotonic()
            await maintain(now)
            offset = cycle % len(active)
            interleaved = active[offset:] + active[:offset]
            cycle += 1
            relevant = await poll_selected(interleaved)
            if relevant:
                return response(relevant, False)

    # A healthy subscription is the boundary authority; timeout does not
    # trigger an idle Central read. Explicit poll/fallback mode retains the
    # final compatibility refetch.
    if final_poll:
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
    wait_for: str = WAIT_FOR_AUTO,
    progress_callback: Callable[[float, float], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Testable wait implementation with identity kept entirely call-local."""
    budget = clamp_timeout(timeout_s)
    started = time.monotonic()
    deadline = started + budget
    cursor = max(0, int(since_seq))
    held: dict[str, float] = {}
    lease_due: dict[str, float] = {}
    progress_cadence = _progress_cadence_s() if progress_callback else None
    next_progress = started + progress_cadence if progress_cadence else None
    resynced = False
    proj = project.strip().lower() if isinstance(project, str) and project.strip() else None
    explicit_name = agent_name is not None
    if client.identity is None:
        raise RuntimeError("BoardClient has no default joined identity")
    if agent_name is None:
        call_agent_name = (
            getattr(client.identity, "agent_name", None)
            or getattr(client, "agent_name", None)
            or AGENT_NAME
        )
    else:
        call_agent_name = agent_name
    if not isinstance(call_agent_name, str) or not call_agent_name:
        raise ValueError("agent_name must be a non-empty string")
    my_agent_id = _derived_agent_id(
        client.identity.principal_id, call_agent_name
    )
    role = getattr(client.identity, "role", "worker")
    if explicit_name:
        joined = await _join_for_call(client, call_agent_name, True)
        if joined.get("agent_id") != my_agent_id:
            raise BoardClientError("server returned an unexpected per-call agent_id")
        role = joined.get("role", role)
    selected_wait_for = _resolve_wait_for(wait_for, role)

    async def poll_once() -> list[dict]:
        nonlocal cursor, resynced
        events, cursor, did_resync = await _catchup_all(
            client,
            cursor,
            call_agent_name,
            explicit_name,
            prefer_pure=WAIT_MODE == "push",
        )
        if did_resync:
            resynced = True
        _forget_backlog_for_events(BOARD_ID, events)
        return await _filter_relevant(
            client,
            events,
            my_agent_id,
            only_mine,
            proj,
            selected_wait_for,
        )

    # 1. CHECK BEFORE BLOCKING. Journal events advance the cursor; synthetic
    # backlog cues never carry or fabricate a sequence number.
    relevant = await poll_once()
    backlog = await _scan_open_backlog(
        client,
        my_agent_id,
        only_mine,
        proj,
        held,
        selected_wait_for,
        BOARD_ID,
    )
    lease_due = {
        ticket_id: started + interval
        for ticket_id, interval in held.items()
    }
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
            "reason": (
                "backlog"
                if all(event.get("source") == "backlog_scan" for event in relevant)
                else "journal"
            ),
            "resynced": resynced,
        }

    async def maintain(now: float) -> None:
        nonlocal next_progress
        await _renew_due_leases(client, held, lease_due, now)
        if next_progress is not None and now >= next_progress:
            await _run_progress(progress_callback, started, budget)
            next_progress = now + (progress_cadence or PROGRESS_INTERVAL_S)

    def advance_cursor(value: int) -> None:
        nonlocal cursor
        cursor = max(cursor, value)

    # 2. Push mode uses BoardClient.events() for live-first subscription,
    # authoritative drain, cursor tracking, deduplication, and reconnect.
    if WAIT_MODE == "push":
        try:
            identity = JoinedIdentity(
                BOARD_ID,
                my_agent_id,
                client.identity.principal_id,
                call_agent_name,
                role,
            )
            events = _event_stream(
                client,
                BOARD_ID,
                identity,
                getattr(client, "generation_token", None),
                cursor,
                advance_cursor,
                pure_catchup=getattr(
                    client, "_pursers_pure_catchup", None
                ) is True,
            )
            pending_event = asyncio.create_task(anext(events))
            try:
                while True:
                    now = time.monotonic()
                    remaining = deadline - now
                    if remaining <= 0:
                        return {
                            "new_seq": cursor,
                            "events": [],
                            "waited_s": round(now - started, 2),
                            "timed_out": True,
                            "reason": "timeout",
                            "resynced": resynced,
                        }
                    wait_slice = _maintenance_due_in(
                        now, remaining, lease_due, next_progress
                    )
                    done, _ = await asyncio.wait(
                        {pending_event}, timeout=wait_slice
                    )
                    if not done:
                        await maintain(time.monotonic())
                        continue
                    event = pending_event.result()
                    found: list[dict[str, Any]] = []
                    while True:
                        _forget_backlog_for_events(BOARD_ID, [event])
                        if await _is_relevant(
                            client,
                            event,
                            my_agent_id,
                            only_mine,
                            proj,
                            selected_wait_for,
                        ):
                            found.append(event)
                        pending_event = asyncio.create_task(anext(events))
                        await asyncio.sleep(0)
                        if not pending_event.done():
                            break
                        event = pending_event.result()
                    if found:
                        return {
                            "new_seq": cursor,
                            "events": found,
                            "waited_s": round(time.monotonic() - started, 2),
                            "timed_out": False,
                            "reason": "journal",
                            "resynced": resynced,
                        }
            finally:
                pending_event.cancel()
                await asyncio.gather(pending_event, return_exceptions=True)
                await events.aclose()
        except Exception as exc:
            _log(
                "WARNING: subscriptions/listen unavailable; falling back to "
                f"poll for this call and retrying push on re-arm: {exc}"
            )

    # 3. Poll until relevant work appears or the budget runs out. This is the
    # default path and the whole-call fallback after any push failure.
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(
            min(
                DEFAULT_POLL_INTERVAL_S,
                _maintenance_due_in(
                    time.monotonic(), remaining, lease_due, next_progress
                ),
            )
        )

        now = time.monotonic()
        await maintain(now)

        relevant = await poll_once()
        if relevant:
            return {
                "new_seq": cursor,
                "events": relevant,
                "waited_s": round(time.monotonic() - started, 2),
                "timed_out": False,
                "reason": "journal",
                "resynced": resynced,
            }

    # 4. Timed out -- the re-arm cue.
    return {
        "new_seq": cursor,
        "events": [],
        "waited_s": round(time.monotonic() - started, 2),
        "timed_out": True,
        "reason": "timeout",
        "resynced": resynced,
    }


def main() -> None:
    if "--version" in sys.argv[1:]:
        print(VERSION)
        return
    if not CENTRAL_TOKEN:
        print("FATAL: ONBOARD_CENTRAL_TOKEN is not set", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
