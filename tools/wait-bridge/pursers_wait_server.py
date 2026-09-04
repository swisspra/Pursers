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
import copy
import fcntl
import hashlib
import hmac
import importlib.metadata
import json
import math
import os
import re
import sys
import tempfile
import time
import tomllib
from collections import OrderedDict
from contextlib import aclosing, asynccontextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator

from pursers_client import (
    OFFER_EXPIRED,
    OFFER_REVOKED,
    REVIEW_OFFERED,
    REVIEW_LEASE_KINDS,
    TICKET_OFFERED,
    GENERATION_META_KEY,
    BoardClient,
    BoardClientError,
    JoinedIdentity,
    SUBMITTED_RELEVANT_KINDS,
    parse_project_registry,
)
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.subscriptions import ResourceUpdated
from agent_naming import resolve_agent_name
from backlog import (
    WAIT_FOR_CLAIMABLE,
    WAIT_FOR_SUBMITTED,
    backlog_events,
    ticket_is_relevant,
)

SOURCE_VERSION = "0.1.0a7"


def _source_version() -> str:
    source = Path(__file__).resolve()
    manifest = source.parents[2] / "tools/release_versions.toml"
    pyproject = source.with_name("pyproject.toml")
    if manifest.is_file():
        try:
            value = tomllib.loads(manifest.read_text(encoding="utf-8"))
            version = value.get("packages", {}).get("wait_bridge")
            if version:
                return str(version)
        except (OSError, tomllib.TOMLDecodeError):
            pass
    if pyproject.is_file():
        try:
            value = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            version = value.get("project", {}).get("version")
            if version:
                return str(version)
        except (OSError, tomllib.TOMLDecodeError):
            pass
    if pyproject.exists() or manifest.exists():
        return SOURCE_VERSION
    raise RuntimeError("pursers-wait-bridge distribution metadata is unavailable")


def _runtime_version() -> str:
    try:
        return importlib.metadata.version("pursers-wait-bridge")
    except importlib.metadata.PackageNotFoundError:
        return _source_version()


VERSION = _runtime_version()

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
SEAT_ROLES = frozenset({"worker", "reviewer", "orchestrator", "coordinator"})
CONNECTOR_TOKEN_ENV = "PURSERS_BOARD_CONNECTOR_TOKEN"

# --- wait policy (v4-parity constants; see a2a_wait.py) --------------------

DEFAULT_TIMEOUT_S = 180
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_CLAIM_TTL_S = 900
MAX_LEASE_RENEW_INTERVAL_S = 300.0
PROGRESS_INTERVAL_S = 300.0
CATCHUP_PAGE_LIMIT = 100
BACKLOG_SCAN_LIMIT = 100
CLAIMABLE_RELEVANT_KINDS = frozenset(
    {
        "ticket_created",
        "ticket_status_changed",
        TICKET_OFFERED,
        OFFER_EXPIRED,
        OFFER_REVOKED,
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
WAIT_RETURN_SAMPLE_LIMIT = 256
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


def bridge_state_path() -> Path:
    explicit = os.environ.get("PURSERS_BRIDGE_STATE", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    state_dir = os.environ.get("PURSERS_BRIDGE_STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir).expanduser().resolve() / f"bridge-state-{BOARD_ID}.json"
    return bridge_stats_path().parent / f"bridge-state-{BOARD_ID}.json"


def orchestrator_state_path() -> Path:
    explicit = os.environ.get("PURSERS_BRIDGE_STATE", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    state_dir = os.environ.get("PURSERS_BRIDGE_STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir).expanduser().resolve() / f"orchestrator_state_{BOARD_ID}.json"
    return Path.home() / ".pursers" / "wait-bridge" / f"orchestrator_state_{BOARD_ID}.json"


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
                    str(result.get("mode") or "unknown"),
                    str(result.get("reason") or outcome),
                )
        except Exception as exc:  # noqa: BLE001 - metering never breaks work.
            _log(f"wait-return stats write failed: {type(exc).__name__}")

    async def record_digest_call(
        self,
        board_id: str,
        agent_name: str,
        result: dict[str, Any],
    ) -> None:
        """Record one model-visible board_digest result."""
        try:
            async with self._lock:
                self._record_wait_return_sync(
                    board_id,
                    agent_name,
                    "digest",
                    _meter_bytes(result),
                    "digest",
                    "digest",
                )
        except Exception as exc:  # noqa: BLE001 - metering never breaks work.
            _log(f"digest stats write failed: {type(exc).__name__}")

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
        mode: str,
        reason: str,
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
            samples = seat.get("returns")
            samples = samples if isinstance(samples, list) else []
            samples = [
                sample
                for sample in samples[-(WAIT_RETURN_SAMPLE_LIMIT - 1) :]
                if isinstance(sample, dict)
            ]
            samples.append(
                {
                    "at": now.isoformat(),
                    "response_bytes": max(0, int(response_bytes)),
                    "outcome": outcome,
                    "mode": mode,
                    "reason": reason,
                }
            )
            seat["returns"] = samples[-WAIT_RETURN_SAMPLE_LIMIT:]
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


def _host_name() -> str:
    host = os.environ.get("PURSERS_HOST", DEFAULT_HOST).strip().lower()
    return host if host in HOST_TIMEOUTS_S else DEFAULT_HOST


def _parse_capability_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _seat_capabilities() -> dict[str, Any] | None:
    """Return explicit dispatch capabilities, or None for legacy seats."""
    names = (
        "PURSERS_TIER_MAX",
        "PURSERS_SKILLS",
        "PURSERS_CAN_REVIEW",
        "PURSERS_CAN_WORK",
        "PURSERS_MODEL",
        "PURSERS_PROVIDER",
    )
    if not any(os.environ.get(name, "").strip() for name in names):
        return None
    capabilities: dict[str, Any] = {"host": _host_name(), "max_parallel": 1}
    tier = os.environ.get("PURSERS_TIER_MAX", "").strip()
    if tier:
        try:
            tier_value = int(tier)
        except ValueError as exc:
            raise ValueError("PURSERS_TIER_MAX must be 1, 2, or 3") from exc
        if tier_value not in {1, 2, 3}:
            raise ValueError("PURSERS_TIER_MAX must be 1, 2, or 3")
        capabilities["tier_max"] = tier_value
    skills = os.environ.get("PURSERS_SKILLS", "")
    if skills.strip():
        capabilities["skills"] = sorted(
            {item.strip() for item in skills.split(",") if item.strip()}
        )
    for env_name, field in (
        ("PURSERS_CAN_REVIEW", "can_review"),
        ("PURSERS_CAN_WORK", "can_work"),
    ):
        value = _parse_capability_bool(env_name)
        if value is not None:
            capabilities[field] = value
    for env_name, field in (
        ("PURSERS_MODEL", "model"),
        ("PURSERS_PROVIDER", "provider"),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            capabilities[field] = value
    return capabilities


def _declared_role() -> str:
    role = os.environ.get("PURSERS_ROLE", "worker").strip().lower()
    if role not in SEAT_ROLES:
        raise ValueError(
            "PURSERS_ROLE must be worker, reviewer, orchestrator, or coordinator"
        )
    return role


def _timeout_margin_s(host_timeout_s: int) -> int:
    margin = min(60, max(30, math.ceil(host_timeout_s * 0.10)))
    if os.environ.get("PURSERS_HOST", DEFAULT_HOST).strip().lower() == "claude-desktop":
        margin = max(margin, 40)
    return min(host_timeout_s - 1, margin)


def host_block_limit_s() -> int:
    timeout = _host_timeout_s()
    return max(1, timeout - _timeout_margin_s(timeout))


ORCHESTRATOR_BLOCK_LIMIT_S = 200


def clamp_timeout(timeout_s: Any, role: str | None = None) -> int:
    try:
        t = int(timeout_s)
    except (TypeError, ValueError):
        t = DEFAULT_TIMEOUT_S
    limit = host_block_limit_s()
    env_role = os.environ.get("PURSERS_ROLE", "").strip().lower()
    call_role = (role or "").strip().lower()
    if env_role == "orchestrator" or call_role == "orchestrator":
        limit = min(limit, ORCHESTRATOR_BLOCK_LIMIT_S)
    return max(1, min(t, limit))


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
        raw_client = (
            getattr(parent, "_client", None)
            or getattr(parent, "_raw_client", None)
            or parent
        )
        self.board_id = board_id
        self._parent = parent
        self.agent_name = getattr(parent, "agent_name", AGENT_NAME)
        self.role = getattr(parent, "role", "worker")
        self.identity: JoinedIdentity | None = getattr(parent, "identity", None)
        self.generation_token: str | None = getattr(parent, "generation_token", None)
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
        capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = self.agent_name if agent_name is None else agent_name
        arguments: dict[str, Any] = {
            "agent_name": selected,
            "role": self.role,
        }
        if task_focus is not None:
            arguments["task_focus"] = task_focus
        caps = dict(capabilities or {})
        if os.environ.get("PURSERS_LEGACY_TOOLS") == "1":
            caps.setdefault("legacy_tools", True)
        if caps or capabilities is not None:
            arguments["capabilities"] = caps
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
    capabilities = _seat_capabilities()
    if explicit_name:
        kwargs: dict[str, Any] = {"agent_name": agent_name}
        if capabilities is not None:
            kwargs["capabilities"] = capabilities
        return await client.board_join(**kwargs)
    if capabilities is not None:
        return await client.board_join(capabilities=capabilities)
    return await client.board_join()


class BoardJoinFailure(ToolError):
    """Stable, non-sensitive classification for deferred startup failures."""

    def __init__(self, cause_class: str, detail: str) -> None:
        self.cause_class = cause_class
        super().__init__(f"board join failed ({cause_class}): {detail}")


def _split_identity_failure() -> BoardJoinFailure | None:
    if os.environ.get("PURSERS_REQUIRE_TOKEN_MATCH", "").strip() != "1":
        return None
    connector_token = os.environ.get(CONNECTOR_TOKEN_ENV, "")
    if not connector_token or not CENTRAL_TOKEN or not hmac.compare_digest(
        connector_token, CENTRAL_TOKEN
    ):
        return BoardJoinFailure(
            "configuration",
            "split identity: wait bridge and board connector tokens differ",
        )
    return None


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
            role=_declared_role(),
            meter=self.meter,
        )
        entered = False
        try:
            try:
                async with asyncio.timeout(self.JOIN_TIMEOUT_S):
                    await client.__aenter__()
                    capabilities = _seat_capabilities()
                    if capabilities is not None:
                        await client.board_join(capabilities=capabilities)
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
        try:
            _declared_role()
        except ValueError as exc:
            failure = BoardJoinFailure("configuration", str(exc))
            self._report_failure(failure)
            raise failure
        identity_failure = _split_identity_failure()
        if identity_failure is not None:
            self._report_failure(identity_failure)
            raise identity_failure
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


def _extract_notes_subset(ticket: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not keys:
        return result
    notes_texts: list[str] = []
    if isinstance(ticket.get("notes"), str):
        notes_texts.append(ticket["notes"])
    if isinstance(ticket.get("summary"), str):
        notes_texts.append(ticket["summary"])
    if isinstance(ticket.get("submission_history"), list):
        for sub in reversed(ticket["submission_history"]):
            if isinstance(sub, dict):
                if isinstance(sub.get("notes"), str):
                    notes_texts.append(sub["notes"])
                if isinstance(sub.get("summary"), str):
                    notes_texts.append(sub["summary"])

    for key in keys:
        if key in ticket and ticket[key] is not None:
            result[key] = ticket[key]
            continue
        if isinstance(ticket.get("submission_history"), list):
            found_sub = False
            for sub in reversed(ticket["submission_history"]):
                if isinstance(sub, dict) and key in sub and sub[key] is not None:
                    result[key] = sub[key]
                    found_sub = True
                    break
            if found_sub:
                continue

        found = False
        for text in notes_texts:
            try:
                data = json.loads(text)
                if isinstance(data, dict) and key in data:
                    result[key] = data[key]
                    found = True
                    break
            except Exception:
                pass
            pattern = re.compile(rf"(?im)^\s*{re.escape(key)}\s*[:=]\s*(.+)$")
            match = pattern.search(text)
            if match:
                result[key] = match.group(1).strip()
                found = True
                break
        if not found and key == "branch_and_commit":
            for text in notes_texts:
                match = re.search(r"(?im)\bbranch_and_commit[:=]?\s*([^\n\r]+)", text)
                if match:
                    result[key] = match.group(1).strip()
                    break
    return result


class OrchestratorEngine:
    """Continuous background subscriber and digest aggregator for leader seats."""

    def __init__(
        self,
        connection: Any,
        meter: BridgeStats,
        state_path: Path,
    ) -> None:
        self.connection = connection
        self.meter = meter
        self.state_path = state_path
        self.cursor_map: dict[str, int] = {}
        self.ack_cursor_map: dict[str, int] = {}
        self.ring_buffer: list[dict[str, Any]] = []
        self.seen_event_ids: set[str] = set()
        self.ticket_cache: dict[str, dict[str, Any]] = {}
        self.watched_ticket_ids: set[str] = set()
        self.watched_tags: set[str] = set()
        self.subscription_health: dict[str, Any] = {
            "connected": False,
            "last_event_at": None,
            "reconnects": 0,
        }
        self.active_boards: list[str] = [BOARD_ID]
        self.sessions: set[Any] = set()
        self.lock = asyncio.Lock()
        self._subscriber_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self.ready = asyncio.Event()

    def load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            if isinstance(data.get("cursor_map"), dict):
                self.cursor_map = {k: int(v) for k, v in data["cursor_map"].items()}
            if isinstance(data.get("ack_cursor_map"), dict):
                self.ack_cursor_map = {k: int(v) for k, v in data["ack_cursor_map"].items()}
            if isinstance(data.get("active_boards"), list):
                self.active_boards = [str(b) for b in data["active_boards"]]
            else:
                self.active_boards = list(self.cursor_map.keys()) or [BOARD_ID]
            if BOARD_ID not in self.active_boards:
                self.active_boards.insert(0, BOARD_ID)
            if isinstance(data.get("events"), list):
                self.ring_buffer = [e for e in data["events"] if isinstance(e, dict)][-5000:]
                self.seen_event_ids = {
                    e.get("id") or f"EV-{e.get('board_id')}-{e.get('seq')}"
                    for e in self.ring_buffer
                }
            if isinstance(data.get("tickets"), dict):
                self.ticket_cache = {
                    k: v for k, v in data["tickets"].items() if isinstance(v, dict)
                }
            if isinstance(data.get("watched_ticket_ids"), list):
                self.watched_ticket_ids = set(data["watched_ticket_ids"])
            if isinstance(data.get("watched_tags"), list):
                self.watched_tags = set(data["watched_tags"])
            if "reconnects" in data:
                self.subscription_health["reconnects"] = int(data.get("reconnects", 0))
            if "last_event_at" in data:
                self.subscription_health["last_event_at"] = data.get("last_event_at")
        except Exception as exc:
            _log(f"warning: failed to load orchestrator state: {exc}")

    def get_agent_name(self) -> str:
        if self.connection is not None:
            client = getattr(self.connection, "_client", None)
            if client is not None and getattr(client, "identity", None):
                return getattr(client.identity, "agent_name", AGENT_NAME)
            if getattr(self.connection, "identity", None):
                return getattr(self.connection.identity, "agent_name", AGENT_NAME)
        return AGENT_NAME

    def save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if len(self.ticket_cache) > 5000:
                keys = list(self.ticket_cache.keys())[-5000:]
                self.ticket_cache = {k: self.ticket_cache[k] for k in keys}
            payload = {
                "schema_version": 1,
                "cursor_map": dict(self.cursor_map),
                "ack_cursor_map": dict(self.ack_cursor_map),
                "active_boards": list(self.active_boards),
                "events": self.ring_buffer[-5000:],
                "tickets": self.ticket_cache,
                "watched_ticket_ids": sorted(self.watched_ticket_ids),
                "watched_tags": sorted(self.watched_tags),
                "reconnects": self.subscription_health["reconnects"],
                "last_event_at": self.subscription_health["last_event_at"],
                "last_saved_at": datetime.now(timezone.utc).isoformat(),
            }
            tmp = self.state_path.with_suffix(f".tmp.{os.getpid()}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(tmp, flags, 0o600)
            with open(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            tmp.replace(self.state_path)
            try:
                self.state_path.chmod(0o600)
            except Exception:
                pass
        except Exception as exc:
            _log(f"warning: failed to persist orchestrator state: {exc}")

    async def notify_resource_updated(self) -> None:
        uri = f"board://{BOARD_ID}/digest"
        try:
            if hasattr(mcp, "_subscriptions") and mcp._subscriptions is not None:
                await mcp._subscriptions.publish(ResourceUpdated(uri=uri))
        except Exception:
            pass
        for session in list(self.sessions):
            try:
                if hasattr(session, "send_resource_updated"):
                    await session.send_resource_updated(uri)
            except Exception:
                self.sessions.discard(session)

    async def _get_client(self) -> BoardClient:
        if hasattr(self.connection, "client") and callable(self.connection.client):
            return await self.connection.client()
        return self.connection

    async def start_subscriber(self) -> None:
        if self._subscriber_task is None or self._subscriber_task.done():
            self._stop_event.clear()
            self._subscriber_task = asyncio.create_task(
                self._run_subscriber(), name="pursers-orchestrator-subscriber"
            )

    async def stop_subscriber(self) -> None:
        self._stop_event.set()
        task = self._subscriber_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._subscriber_task = None
        self.save_state()

    async def ensure_subscriber_running(self) -> None:
        if self._subscriber_task is None or self._subscriber_task.done():
            await self.start_subscriber()

    def _open_listen(self, client: Any, resources: list[str]) -> Any:
        custom = getattr(client, "listen_across_boards", None)
        if callable(custom):
            return custom(resources)
        raw = getattr(client, "_raw_client", None)
        if raw is not None and hasattr(raw, "listen"):
            return raw.listen(resource_subscriptions=resources)
        inner = getattr(client, "_client", None)
        if inner is not None and hasattr(inner, "listen"):
            return inner.listen(resource_subscriptions=resources)
        if hasattr(client, "listen"):
            return client.listen(resource_subscriptions=resources)
        raise RuntimeError(f"client {type(client).__name__} does not support listen")

    async def _run_subscriber(self) -> None:
        backoff = 0.5
        while not self._stop_event.is_set():
            try:
                client = await self._get_client()
                try:
                    registry = await _read_project_registry(client)
                    active_boards = _registry_boards(registry)
                    async with self.lock:
                        self.active_boards = list(active_boards)
                    self.save_state()
                except Exception as exc:
                    _log(f"registry read deferred/failed in background subscriber: {exc}")
                    async with self.lock:
                        active_boards = list(self.active_boards or [BOARD_ID])

                async with self.lock:
                    for b in active_boards:
                        if b not in self.cursor_map:
                            self.cursor_map[b] = 0

                resources = [f"board://{b}/journal" for b in active_boards]

                async def catchup_boards() -> bool:
                    buffer_grew = False
                    agent_name = (
                        getattr(client, "agent_name", None)
                        or (client.identity.agent_name if getattr(client, "identity", None) else None)
                        or AGENT_NAME
                    )
                    for b in active_boards:
                        cursor = self.cursor_map.get(b, 0)
                        while True:
                            view = _BoardView(client, b)
                            try:
                                page = await view.board_catchup(
                                    cursor=cursor,
                                    ack=False,
                                    touch=False,
                                    agent_name=agent_name,
                                )
                            except (PermissionError, BoardClientError) as exc:
                                if "not a member" in str(exc).lower() or isinstance(exc, PermissionError):
                                    try:
                                        await view.board_join(agent_name=agent_name)
                                        page = await view.board_catchup(
                                            cursor=cursor,
                                            ack=False,
                                            touch=False,
                                            agent_name=agent_name,
                                        )
                                    except Exception as join_exc:
                                        _log(f"board_join/catchup failed for {b}: {join_exc}")
                                        break
                                else:
                                    _log(f"catchup failed for {b}: {exc}")
                                    break
                            except Exception as c_exc:
                                _log(f"catchup failed for {b}: {c_exc}")
                                break

                            events = page.get("events", [])
                            next_cursor = int(page.get("next_cursor", cursor))
                            cursor = next_cursor
                            async with self.lock:
                                self.cursor_map[b] = max(self.cursor_map.get(b, 0), next_cursor)

                            if events:
                                new_events = []
                                changed_tickets = set()
                                async with self.lock:
                                    for ev in events:
                                        ev_id = ev.get("id") or f"EV-{b}-{ev.get('seq')}"
                                        if ev_id not in self.seen_event_ids:
                                            self.seen_event_ids.add(ev_id)
                                            ev_full = {**ev, "board_id": b, "id": ev_id}
                                            self.ring_buffer.append(ev_full)
                                            new_events.append(ev_full)
                                            if ev.get("ticket_id"):
                                                changed_tickets.add(ev["ticket_id"])
                                    if len(self.ring_buffer) > 5000:
                                        evicted = self.ring_buffer[:-5000]
                                        self.ring_buffer = self.ring_buffer[-5000:]
                                        for ev in evicted:
                                            self.seen_event_ids.discard(ev.get("id"))
                                if new_events:
                                    buffer_grew = True
                                    self.subscription_health["last_event_at"] = datetime.now(timezone.utc).isoformat()
                                    for tid in changed_tickets:
                                        try:
                                            t_resp = await view.ticket_get(tid)
                                            if t_resp and "ticket" in t_resp:
                                                async with self.lock:
                                                    self.ticket_cache[f"{b}:{tid}"] = t_resp["ticket"]
                                        except Exception as exc:
                                            _log(f"ticket_get failed for {tid}: {exc}")
                            if not page.get("has_more"):
                                break
                    if buffer_grew:
                        self.save_state()
                        await self.notify_resource_updated()
                    return buffer_grew

                listen_cm = self._open_listen(client, resources)
                async with listen_cm as subscription:
                    self.subscription_health["connected"] = True
                    backoff = 0.5
                    await catchup_boards()
                    self.ready.set()

                    sub_iter = aiter(subscription)
                    while not self._stop_event.is_set():
                        try:
                            _cue = await asyncio.wait_for(anext(sub_iter), timeout=1.0)
                        except asyncio.TimeoutError:
                            try:
                                reg = await asyncio.wait_for(_read_project_registry(client), timeout=0.5)
                                new_active = _registry_boards(reg)
                                if set(new_active) != set(active_boards):
                                    _log(f"registry boards changed: {active_boards} -> {new_active}; reopening listen")
                                    async with self.lock:
                                        self.active_boards = list(new_active)
                                    self.save_state()
                                    break
                            except Exception:
                                pass
                            continue
                        except StopAsyncIteration:
                            break

                        try:
                            reg = await asyncio.wait_for(_read_project_registry(client), timeout=0.5)
                            new_active = _registry_boards(reg)
                            if set(new_active) != set(active_boards):
                                _log(f"registry boards changed on cue: {active_boards} -> {new_active}; reopening listen")
                                async with self.lock:
                                    self.active_boards = list(new_active)
                                self.save_state()
                                break
                        except Exception:
                            pass
                        await catchup_boards()
            except asyncio.CancelledError:
                self.subscription_health["connected"] = False
                raise
            except Exception as exc:
                self.subscription_health["connected"] = False
                self.subscription_health["reconnects"] += 1
                _log(f"subscription reconnect #{self.subscription_health['reconnects']} after {exc}")
                self.save_state()
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def build_digest(
        self,
        since: dict[str, int] | int | None = None,
        boards: list[str] | str = "registry",
        group_by: str = "ticket",
        include_notes_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        keys = ["branch_and_commit"] if include_notes_keys is None else list(include_notes_keys)

        async with self.lock:
            if isinstance(boards, str):
                if boards == "registry":
                    target_boards = list(self.active_boards or [BOARD_ID])
                else:
                    target_boards = [b.strip() for b in boards.split(",") if b.strip()]
            elif isinstance(boards, list):
                target_boards = _normalize_boards(boards)
            else:
                target_boards = [BOARD_ID]

            if since is None:
                since_map = {b: self.ack_cursor_map.get(b, 0) for b in target_boards}
            elif isinstance(since, int):
                since_map = {b: max(0, since) for b in target_boards}
            elif isinstance(since, dict):
                since_map = {b: max(0, int(since.get(b, 0))) for b in target_boards}
            else:
                since_map = {b: 0 for b in target_boards}

            filtered_events = [
                ev for ev in self.ring_buffer
                if ev.get("board_id") in target_boards
                and int(ev.get("seq", 0)) > since_map.get(ev.get("board_id"), 0)
            ]

        by_ticket: dict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
        for ev in filtered_events:
            tid = ev.get("ticket_id")
            bid = ev.get("board_id")
            if tid and bid:
                by_ticket.setdefault((bid, tid), []).append(ev)

        tickets: list[dict[str, Any]] = []
        new_tickets: list[dict[str, Any]] = []

        for (bid, tid), evs in by_ticket.items():
            cache_key = f"{bid}:{tid}"
            ticket_data = self.ticket_cache.get(cache_key) or {}

            transitions = []
            for ev in evs:
                s_from = ev.get("status_from")
                s_to = ev.get("status_to")
                if ev.get("kind") == "ticket_created":
                    s_from = s_from or None
                    s_to = s_to or "open"
                transitions.append({
                    "from": s_from,
                    "to": s_to or "unknown",
                    "actor": ev.get("actor") or "",
                    "at": ev.get("occurred_at") or "",
                })

            if not any(tr["to"] == "open" for tr in transitions) and ticket_data.get("created_at"):
                transitions.insert(0, {
                    "from": None,
                    "to": "open",
                    "actor": str(ticket_data.get("created_by") or ""),
                    "at": str(ticket_data.get("created_at") or ""),
                })

            status_now = str(ticket_data.get("status") or (transitions[-1]["to"] if transitions else "open"))
            title = str(ticket_data.get("title") or evs[0].get("title") or "")
            claimed_by = ticket_data.get("claimed_by") or ticket_data.get("claimed_by_agent_name") or ticket_data.get("claimed_by_agent_id")

            verdict = ticket_data.get("review_verdict")
            rejection_count = int(ticket_data.get("rejection_count", 0))
            reviewer = ticket_data.get("reviewed_by_agent_name") or ticket_data.get("reviewed_by_agent_id")
            for ev in reversed(evs):
                if verdict is None and ev.get("review_verdict"):
                    verdict = ev["review_verdict"]
                if reviewer is None and (ev.get("reviewed_by_agent_name") or ev.get("reviewed_by_agent_id")):
                    reviewer = ev.get("reviewed_by_agent_name") or ev.get("reviewed_by_agent_id")
                if rejection_count == 0 and ev.get("rejection_count"):
                    rejection_count = int(ev["rejection_count"])

            notes_subset = _extract_notes_subset(ticket_data, keys)
            closed_at = ticket_data.get("closed_at")
            if closed_at is None:
                for tr in reversed(transitions):
                    if tr["to"] == "closed":
                        closed_at = tr["at"]
                        break

            is_watched = (
                tid in self.watched_ticket_ids
                or any(t in self.watched_tags for t in ticket_data.get("tags", []))
            )

            ticket_item = {
                "ticket_id": tid,
                "board_id": bid,
                "title": title,
                "transitions": transitions,
                "status_now": status_now,
                "review": {
                    "verdict": verdict,
                    "rejection_count": rejection_count,
                    "reviewer": reviewer,
                },
                "claimed_by": claimed_by,
                "notes_subset": notes_subset,
                "closed_at": closed_at,
                "watched": is_watched,
                "dispatch_state": copy.deepcopy(ticket_data.get("dispatch_state")),
                "offers": {
                    key: copy.deepcopy(ticket_data[key])
                    for key in ("work_offer", "review_offer")
                    if isinstance(ticket_data.get(key), dict)
                },
            }
            tickets.append(ticket_item)

            if any(ev.get("kind") == "ticket_created" or ev.get("status_from") is None for ev in evs):
                new_tickets.append({
                    "ticket_id": tid,
                    "board_id": bid,
                    "title": title,
                    "created_at": ticket_data.get("created_at") or evs[0].get("occurred_at"),
                    "created_by": ticket_data.get("created_by") or evs[0].get("actor"),
                    "status": status_now,
                })

        tickets.sort(key=lambda t: (not t.get("watched", False), t["ticket_id"]))

        counts = {
            "total_tickets": len(tickets),
            "total_transitions": sum(len(t["transitions"]) for t in tickets),
            "new": len(new_tickets),
            "submitted": sum(1 for t in tickets if any(tr["to"] == "submitted" for tr in t["transitions"])),
            "approved": sum(1 for t in tickets if (t.get("review") or {}).get("verdict") == "approve"),
            "rejected": sum(1 for t in tickets if (t.get("review") or {}).get("verdict") == "reject"),
            "closed": sum(1 for t in tickets if any(tr["to"] == "closed" for tr in t["transitions"])),
            "cancelled": sum(1 for t in tickets if any(tr["to"] == "cancelled" for tr in t["transitions"])),
        }

        current_cursor_map = {
            b: self.cursor_map.get(b, since_map.get(b, 0)) for b in target_boards
        }
        unassignable_tickets = []
        for cache_key, ticket_data in self.ticket_cache.items():
            board_id, separator, ticket_id = cache_key.partition(":")
            state = ticket_data.get("dispatch_state")
            if (
                separator
                and board_id in target_boards
                and isinstance(state, dict)
                and state.get("state") == "unassignable"
            ):
                unassignable_tickets.append(
                    {
                        "board_id": board_id,
                        "ticket_id": ticket_id,
                        "reason": state.get("reason"),
                        "kind": state.get("kind"),
                    }
                )
        unassignable_tickets.sort(
            key=lambda item: (item["board_id"], item["ticket_id"])
        )

        return {
            "cursor_map": current_cursor_map,
            "tickets": tickets,
            "new_tickets": new_tickets,
            "counts": counts,
            "unassignable_tickets": unassignable_tickets,
            "subscription": {
                "connected": bool(self.subscription_health.get("connected", False)),
                "last_event_at": self.subscription_health.get("last_event_at"),
                "reconnects": int(self.subscription_health.get("reconnects", 0)),
            },
        }

    async def ack(self, cursor_map: dict[str, int] | None = None) -> dict[str, Any]:
        async with self.lock:
            if cursor_map is None:
                for b, cur in self.cursor_map.items():
                    self.ack_cursor_map[b] = max(self.ack_cursor_map.get(b, 0), cur)
            else:
                for b, cur in cursor_map.items():
                    self.ack_cursor_map[b] = max(self.ack_cursor_map.get(b, 0), int(cur))
            self.save_state()
            return {"ok": True, "cursor_map": dict(self.ack_cursor_map)}

    async def watch(
        self,
        ticket_ids: list[str] | str | None = None,
        tags: list[str] | str | None = None,
    ) -> dict[str, Any]:
        async with self.lock:
            if isinstance(ticket_ids, str):
                self.watched_ticket_ids.update(s.strip() for s in ticket_ids.split(",") if s.strip())
            elif isinstance(ticket_ids, list):
                self.watched_ticket_ids.update(str(s).strip() for s in ticket_ids if str(s).strip())

            if isinstance(tags, str):
                self.watched_tags.update(s.strip() for s in tags.split(",") if s.strip())
            elif isinstance(tags, list):
                self.watched_tags.update(str(s).strip() for s in tags if str(s).strip())

            self.save_state()
            return {
                "ok": True,
                "watched_ticket_ids": sorted(self.watched_ticket_ids),
                "watched_tags": sorted(self.watched_tags),
            }

    async def unwatch(
        self,
        ticket_ids: list[str] | str | None = None,
        tags: list[str] | str | None = None,
        all: bool = False,
    ) -> dict[str, Any]:
        async with self.lock:
            if all or (ticket_ids is None and tags is None):
                self.watched_ticket_ids.clear()
                self.watched_tags.clear()
            else:
                if isinstance(ticket_ids, str):
                    for s in ticket_ids.split(","):
                        self.watched_ticket_ids.discard(s.strip())
                elif isinstance(ticket_ids, list):
                    for s in ticket_ids:
                        self.watched_ticket_ids.discard(str(s).strip())

                if isinstance(tags, str):
                    for s in tags.split(","):
                        self.watched_tags.discard(s.strip())
                elif isinstance(tags, list):
                    for s in tags:
                        self.watched_tags.discard(str(s).strip())

            self.save_state()
            return {
                "ok": True,
                "watched_ticket_ids": sorted(self.watched_ticket_ids),
                "watched_tags": sorted(self.watched_tags),
            }


_GLOBAL_ENGINE: OrchestratorEngine | None = None


def _get_orchestrator_engine() -> OrchestratorEngine | None:
    return _GLOBAL_ENGINE


class SessionCaptureMiddleware:
    def __init__(self, engine_getter: Callable[[], OrchestratorEngine | None]) -> None:
        self.engine_getter = engine_getter

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        session = getattr(ctx, "session", None)
        if session is not None:
            engine = self.engine_getter()
            if engine is not None:
                engine.sessions.add(session)
        return await call_next(ctx)


@asynccontextmanager
async def _lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Create local state before initialize; start background subscription if orchestrator."""
    global _GLOBAL_ENGINE
    meter = BridgeStats(bridge_stats_path())
    connection = DeferredBoardConnection(meter)
    engine = OrchestratorEngine(connection, meter, orchestrator_state_path())
    engine.load_state()
    _GLOBAL_ENGINE = engine

    role = _declared_role()
    if role == "orchestrator":
        await engine.start_subscriber()
    try:
        yield {
            "connection": connection,
            "meter": meter,
            "orchestrator_engine": engine,
        }
    finally:
        await engine.stop_subscriber()
        await connection.close()
        _GLOBAL_ENGINE = None


BRIDGE_DEPRECATED_TOOLS: frozenset[str] = frozenset()

mcp = MCPServer("Pursers Wait Bridge", version=VERSION, lifespan=_lifespan)
mcp._lowlevel_server.middleware.append(SessionCaptureMiddleware(_get_orchestrator_engine))

_original_bridge_list_tools = mcp.list_tools


async def _custom_bridge_list_tools(
    *args: Any, include_legacy: bool | None = None, **kwargs: Any
) -> list[Any]:
    tools = await _original_bridge_list_tools(*args, **kwargs)
    legacy = include_legacy
    if legacy is None:
        legacy = os.environ.get("PURSERS_LEGACY_TOOLS") == "1"
    if not legacy and BRIDGE_DEPRECATED_TOOLS:
        tools = [t for t in tools if t.name not in BRIDGE_DEPRECATED_TOOLS]
    return tools


mcp.list_tools = _custom_bridge_list_tools


async def _client_for_tool(ctx: Context) -> BoardClient:
    lifespan = ctx.request_context.lifespan_context
    client = lifespan.get("client")
    if client is not None:
        return client
    connection: DeferredBoardConnection = lifespan["connection"]
    return await connection.client()


async def _engine_for_tool(ctx: Context) -> OrchestratorEngine:
    lifespan = ctx.request_context.lifespan_context
    engine = lifespan.get("orchestrator_engine")
    if engine is None:
        global _GLOBAL_ENGINE
        if _GLOBAL_ENGINE is not None:
            engine = _GLOBAL_ENGINE
        else:
            conn = lifespan.get("connection")
            client = lifespan.get("client")
            meter = getattr(client, "meter", None) or BridgeStats(bridge_stats_path())
            if conn is None:
                class _StaticConnection:
                    async def client(self) -> BoardClient:
                        return client
                conn = _StaticConnection()
            engine = OrchestratorEngine(conn, meter, orchestrator_state_path())
            engine.load_state()
            _GLOBAL_ENGINE = engine
    await engine.ensure_subscriber_running()
    return engine


@mcp.tool()
async def project_registry_get(ctx: Context) -> dict[str, Any]:
    """Return the parsed project registry stored on the home board."""
    client = await _client_for_tool(ctx)
    return await _read_project_registry(client)


@mcp.tool()
async def board_digest(
    ctx: Context,
    since: dict[str, int] | int | None = None,
    boards: list[str] | str = "registry",
    group_by: str = "ticket",
    include_notes_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Return grouped, deduplicated board changes since last ack or given cursors."""
    engine = await _engine_for_tool(ctx)
    selected_keys = (
        ["branch_and_commit"] if include_notes_keys is None else list(include_notes_keys)
    )
    result = await engine.build_digest(
        since=since,
        boards=boards,
        group_by=group_by,
        include_notes_keys=selected_keys,
    )
    meter = engine.meter
    if meter is not None:
        selected_agent = engine.get_agent_name()
        await meter.record_digest_call(BOARD_ID, selected_agent, result)
    return result


@mcp.tool()
async def board_digest_ack(
    ctx: Context,
    cursor_map: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Advance the acknowledged cursor so subsequent digests show only newer changes."""
    engine = await _engine_for_tool(ctx)
    return await engine.ack(cursor_map)


@mcp.tool()
async def board_watch(
    ctx: Context,
    ticket_ids: list[str] | str | None = None,
    tags: list[str] | str | None = None,
) -> dict[str, Any]:
    """Watch ticket IDs or tags to prioritize them at the top of the digest."""
    engine = await _engine_for_tool(ctx)
    return await engine.watch(ticket_ids=ticket_ids, tags=tags)


@mcp.tool()
async def board_unwatch(
    ctx: Context,
    ticket_ids: list[str] | str | None = None,
    tags: list[str] | str | None = None,
    all: bool = False,
) -> dict[str, Any]:
    """Remove ticket IDs or tags from watch list, or unwatch all."""
    engine = await _engine_for_tool(ctx)
    return await engine.unwatch(ticket_ids=ticket_ids, tags=tags, all=all)


@mcp.resource("board://{board_id}/digest")
async def board_digest_resource(board_id: str) -> str:
    """Read current board digest JSON without a tool call."""
    engine = _get_orchestrator_engine()
    if engine is None:
        return "{}"
    digest = await engine.build_digest(boards=[board_id])
    return json.dumps(digest, ensure_ascii=False, indent=2)


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
        if kind == TICKET_OFFERED:
            return False
        if kind in {OFFER_EXPIRED, OFFER_REVOKED}:
            return event.get("offer_kind") == "review"
        if kind not in SUBMITTED_RELEVANT_KINDS:
            return False
        if kind == "ticket_status_changed":
            return event.get("status_to") == "submitted"
        return True
    if kind == REVIEW_OFFERED:
        return False
    if kind in {OFFER_EXPIRED, OFFER_REVOKED}:
        return event.get("offer_kind") == "work"
    return kind in CLAIMABLE_RELEVANT_KINDS


def _wait_reason(events: list[dict[str, Any]]) -> str:
    if any(event.get("kind") in {TICKET_OFFERED, REVIEW_OFFERED} for event in events):
        return "offer"
    if events and all(event.get("source") == "backlog_scan" for event in events):
        return "backlog"
    return "journal" if events else "timeout"


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
    if not ticket_id:
        return False
    kind = event.get("kind")
    offered_kind = (
        REVIEW_OFFERED if wait_for == WAIT_FOR_SUBMITTED else TICKET_OFFERED
    )
    if kind in {TICKET_OFFERED, REVIEW_OFFERED}:
        if kind != offered_kind or event.get("offered_agent_id") != my_agent_id:
            return False
        recipients = event.get("recipient_identities")
        if isinstance(recipients, list) and my_agent_id not in recipients:
            return False
    if kind in {OFFER_EXPIRED, OFFER_REVOKED}:
        return bool(
            event.get("offered_agent_id") == my_agent_id
            and event.get("offer_kind")
            == ("review" if wait_for == WAIT_FOR_SUBMITTED else "work")
        )
    try:
        result = await client.ticket_get(ticket_id)
    except Exception as exc:
        if kind in {TICKET_OFFERED, REVIEW_OFFERED}:
            return False
        if isinstance(exc, (BoardClientError, AttributeError, KeyError)):
            return not only_mine and project is None
        raise
    ticket = result.get("ticket", {})
    dispatch_state = ticket.get("dispatch_state")
    if isinstance(dispatch_state, dict):
        state = dispatch_state.get("state")
        if wait_for == WAIT_FOR_SUBMITTED:
            offer = ticket.get("review_offer")
            lease = ticket.get("review_lease")
            relevant = bool(
                (
                    kind == REVIEW_OFFERED
                    and isinstance(offer, dict)
                    and offer.get("agent_id") == my_agent_id
                )
                or (
                    kind in REVIEW_LEASE_KINDS
                    and (
                        event.get("reviewer_agent_id") == my_agent_id
                        or (
                            isinstance(lease, dict)
                            and lease.get("reviewer_agent_id") == my_agent_id
                        )
                    )
                )
                or (
                    state == "broadcast"
                    and kind
                    in {
                        "ticket_status_changed",
                        "ticket_submitted",
                        "ticket_resubmitted",
                    }
                    and event.get("status_to") == "submitted"
                )
            )
        else:
            offer = ticket.get("work_offer")
            relevant = bool(
                (
                    kind == TICKET_OFFERED
                    and isinstance(offer, dict)
                    and offer.get("agent_id") == my_agent_id
                )
                or ticket.get("claimed_by_agent_id") == my_agent_id
                or (state == "broadcast" and ticket.get("status") == "open")
            )
    else:
        relevant = ticket_is_relevant(
            ticket, my_agent_id, only_mine, project, wait_for
        )
    if relevant and event.get("kind") == offered_kind:
        offer = ticket.get(
            "review_offer" if wait_for == WAIT_FOR_SUBMITTED else "work_offer"
        )
        if isinstance(offer, dict) and offer.get("agent_id") == my_agent_id:
            event["offer"] = {
                "ticket_id": ticket_id,
                "board_id": getattr(client, "board_id", BOARD_ID),
                "expires_at": offer.get("expires_at"),
                "tier": ticket.get("tier", 2),
                "skills_required": list(ticket.get("skills_required") or []),
            }
    return relevant


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
            arguments["review_unclaimed_only"] = True
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
        tickets, my_agent_id, only_mine, project, wait_for, board_id
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
            role=identity.role,
            reconnect_delay_s=parent.reconnect_delay_s,
        )
        event_client.identity = identity
        event_client.generation_token = generation_token

        async def redeclare_capabilities() -> None:
            capabilities = _seat_capabilities()
            if capabilities is None:
                return
            view = _BoardView(parent, board_id)
            joined = await view.board_join(
                agent_name=identity.agent_name,
                capabilities=capabilities,
            )
            event_client.generation_token = joined.get("generation_token")

        events = event_client.events(
            from_cursor=from_cursor,
            only_mine=False,
            kinds=RELEVANT_KINDS,
            resource_subscriptions=resources,
            acknowledge=False,
            touch=False if pure_catchup else None,
            cursor_callback=cursor_callback,
            subscription_callback=redeclare_capabilities,
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
    capabilities = _seat_capabilities()
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
                capabilities=capabilities,
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
    # Entry catchup/backlog checks are direct reads. A board becomes push only
    # after its subscription stream proves ready by advancing a cursor or
    # yielding an event.
    mode_by_board = {board_id: "poll" for board_id in active}

    def response(events: list[dict], timed_out: bool) -> dict[str, Any]:
        modes = set(mode_by_board.values())
        actual_mode = next(iter(modes)) if len(modes) == 1 else "mixed"
        return {
            "new_seq": dict(cursors),
            "events": events,
            "waited_s": (
                0.0 if events and time.monotonic() - started < 0.005
                else round(time.monotonic() - started, 2)
            ),
            "timed_out": timed_out,
            "mode": actual_mode if modes else "poll",
            "mode_by_board": dict(mode_by_board),
            "reason": _wait_reason(events),
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
                        mode_by_board[board_id] = "push"
                        cursors[board_id] = max(
                            cursors[board_id], int(str(detail))
                        )
                    elif kind == "event" and isinstance(detail, dict):
                        mode_by_board[board_id] = "push"
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
                        mode_by_board[board_id] = "poll"
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
    budget = clamp_timeout(timeout_s, role)
    deadline = started + budget
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
            "mode": "poll",
            "reason": _wait_reason(relevant),
            "resynced": resynced,
        }

    async def maintain(now: float) -> None:
        nonlocal next_progress
        await _renew_due_leases(client, held, lease_due, now)
        if next_progress is not None and now >= next_progress:
            await _run_progress(progress_callback, started, budget)
            next_progress = now + (progress_cadence or PROGRESS_INTERVAL_S)

    actual_mode = "poll"

    def advance_cursor(value: int) -> None:
        nonlocal cursor, actual_mode
        actual_mode = "push"
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
                            "mode": actual_mode,
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
                    actual_mode = "push"
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
                            "mode": "push",
                            "reason": _wait_reason(found),
                            "resynced": resynced,
                        }
            finally:
                pending_event.cancel()
                await asyncio.gather(pending_event, return_exceptions=True)
                await events.aclose()
        except Exception as exc:
            actual_mode = "poll"
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
                "mode": actual_mode,
                "reason": "journal",
                "resynced": resynced,
            }

    # 4. Timed out -- the re-arm cue.
    return {
        "new_seq": cursor,
        "events": [],
        "waited_s": round(time.monotonic() - started, 2),
        "timed_out": True,
        "mode": actual_mode,
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
