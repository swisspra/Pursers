#!/usr/bin/env python3
"""Loopback-only, read-only fleet dashboard for Pursers boards."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

# Prefer the sibling source checkout over any installed pursers-client wheel:
# the dashboard depends on keyword arguments newer than the last published wheel.
_CLIENT_SRC = Path(__file__).resolve().parents[2] / "packages" / "client" / "src"
if (_CLIENT_SRC / "pursers_client").is_dir():
    sys.path.insert(0, str(_CLIENT_SRC))
from pursers_client import BoardClient  # noqa: I001


DEFAULT_URL = "https://127.0.0.1:8766/mcp"
DEFAULT_HOME_BOARD = "pursers"
SNAPSHOT_LIMIT = 1_000
SNAPSHOT_MAX_BYTES = 300_000
EVENT_SCAN_LIMIT = 50
EVENT_MAX_BYTES = 100_000
MAX_BOARDS = 50
MAX_TICKET_ROWS = 25
MAX_EVENT_ROWS = 12
MAX_AGENT_ROWS = 100
MAX_TITLE_CHARS = 160
MAX_LABEL_CHARS = 96
ACTIVE_CLAIM_STATES = frozenset({"claimed", "in_progress", "creating_report"})
SUBMITTED_STATES = frozenset({"submitted", "reviewing", "in_review"})


class FleetClient(Protocol):
    async def board_state_get(self, key: str | None = None) -> dict[str, Any]: ...

    async def board_snapshot(
        self, *, limit: int | None = None, max_bytes: int | None = None
    ) -> dict[str, Any]: ...

    async def board_catchup(
        self,
        *,
        cursor: int | None = None,
        limit: int = 100,
        ack: bool = True,
        agent_name: str | None = None,
        max_events: int | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Config:
    url: str
    token: str
    home_board: str
    agent_name: str
    stale_seconds: int
    cache_seconds: float


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_project_registry(
    result: dict[str, Any], home_board: str
) -> list[tuple[str, str]]:
    """Return the home board followed by unique active registry boards."""
    state = result.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("value"), str):
        raise TypeError("project registry state is missing")
    try:
        document = json.loads(state["value"])
    except json.JSONDecodeError as exc:
        raise ValueError("project registry is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("project registry schema is unsupported")
    projects = document.get("projects")
    if not isinstance(projects, dict):
        raise TypeError("project registry projects are missing")

    boards = [(home_board, home_board)]
    seen = {home_board}
    for name, project in projects.items():
        if not isinstance(name, str) or not isinstance(project, dict):
            continue
        board_id = project.get("board_id")
        if (
            project.get("status") == "active"
            and isinstance(board_id, str)
            and board_id
            and board_id not in seen
        ):
            boards.append((_clip(name, MAX_LABEL_CHARS), board_id))
            seen.add(board_id)
        if len(boards) >= MAX_BOARDS:
            break
    return boards


def _closed_today(ticket: dict[str, Any], today: datetime) -> bool:
    if ticket.get("status") != "closed":
        return False
    closed_at = _parse_time(ticket.get("closed_at") or ticket.get("updated_at"))
    return closed_at is not None and closed_at.date() == today.date()


def aggregate_fleet(
    board_rows: list[dict[str, Any]],
    *,
    stale_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the bounded API projection from already-bounded board reads."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    boards: list[dict[str, Any]] = []

    for raw in board_rows[:MAX_BOARDS]:
        board_id = _clip(raw.get("board_id"), MAX_LABEL_CHARS)
        label = _clip(raw.get("label") or board_id, MAX_LABEL_CHARS)
        error = raw.get("error")
        if error:
            boards.append(
                {
                    "board_id": board_id,
                    "label": label,
                    "error": _clip(error, MAX_LABEL_CHARS),
                    "counts": {
                        "open": 0,
                        "claimed": 0,
                        "submitted": 0,
                        "closed_today": 0,
                    },
                    "tickets": [],
                    "events": [],
                    "truncated": False,
                }
            )
            continue

        snapshot = raw.get("snapshot") if isinstance(raw.get("snapshot"), dict) else {}
        agents = (
            snapshot.get("agents") if isinstance(snapshot.get("agents"), list) else []
        )
        tickets = (
            snapshot.get("tickets") if isinstance(snapshot.get("tickets"), list) else []
        )
        agent_keys: dict[str, tuple[str, str]] = {}

        for agent in agents:
            if not isinstance(agent, dict):
                continue
            principal_id = agent.get("principal_id")
            agent_name = agent.get("agent_name")
            agent_id = agent.get("agent_id")
            if not all(
                isinstance(item, str) and item for item in (principal_id, agent_name)
            ):
                continue
            key = (principal_id, agent_name)
            if isinstance(agent_id, str):
                agent_keys[agent_id] = key
            seen_at = _parse_time(
                agent.get("last_activity_at") or agent.get("joined_at")
            )
            group = groups.setdefault(
                key,
                {
                    "principal_id": _clip(principal_id, MAX_LABEL_CHARS),
                    "agent_name": _clip(agent_name, MAX_LABEL_CHARS),
                    "boards": set(),
                    "last_seen": None,
                    "busy": False,
                },
            )
            group["boards"].add(board_id)
            if agent.get("status") == "working" and agent.get(
                "lifecycle_status"
            ) not in {"handed_off", "inactive"}:
                group["busy"] = True
            if seen_at is not None and (
                group["last_seen"] is None or seen_at > group["last_seen"]
            ):
                group["last_seen"] = seen_at

        counts = {"open": 0, "claimed": 0, "submitted": 0, "closed_today": 0}
        ticket_rows: list[dict[str, Any]] = []
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            status = str(ticket.get("status") or "")
            if status == "open":
                counts["open"] += 1
            elif status in ACTIVE_CLAIM_STATES:
                counts["claimed"] += 1
            elif status in SUBMITTED_STATES:
                counts["submitted"] += 1
            elif _closed_today(ticket, now):
                counts["closed_today"] += 1

            claimed_id = ticket.get("claimed_by_agent_id")
            if (
                status == "open"
                or status in ACTIVE_CLAIM_STATES
                or status in SUBMITTED_STATES
            ):
                claimed_by = ticket.get("claimed_by")
                if not claimed_by and isinstance(claimed_id, str):
                    key = agent_keys.get(claimed_id)
                    claimed_by = key[1] if key else claimed_id
                ticket_rows.append(
                    {
                        "id": _clip(ticket.get("ticket_id"), MAX_LABEL_CHARS),
                        "title": _clip(
                            ticket.get("title") or "(untitled)", MAX_TITLE_CHARS
                        ),
                        "status": _clip(status, 32),
                        "claimed_by": _clip(claimed_by, MAX_LABEL_CHARS) or None,
                        "updated_at": _clip(ticket.get("updated_at"), 40) or None,
                    }
                )

        events: list[dict[str, Any]] = []
        raw_events = raw.get("events") if isinstance(raw.get("events"), list) else []
        for event in raw_events[-MAX_EVENT_ROWS:]:
            if not isinstance(event, dict):
                continue
            events.append(
                {
                    "seq": event.get("seq")
                    if isinstance(event.get("seq"), int)
                    else None,
                    "kind": _clip(event.get("kind"), 48),
                    "ticket_id": _clip(event.get("ticket_id"), MAX_LABEL_CHARS) or None,
                    "occurred_at": _clip(event.get("occurred_at"), 40) or None,
                }
            )

        ticket_rows.sort(key=lambda item: item["updated_at"] or "", reverse=True)
        ticket_status_rank = {
            **{status: 0 for status in ACTIVE_CLAIM_STATES},
            **{status: 1 for status in SUBMITTED_STATES},
            "open": 2,
        }
        ticket_rows.sort(key=lambda item: ticket_status_rank.get(item["status"], 3))
        ticket_counts_truncated = bool(snapshot.get("truncated"))
        rendered_counts = {
            name: f">={value}" if ticket_counts_truncated else value
            for name, value in counts.items()
        }
        boards.append(
            {
                "board_id": board_id,
                "label": label,
                "counts": rendered_counts,
                "tickets": ticket_rows[:MAX_TICKET_ROWS],
                "events": events,
                "truncated": bool(
                    snapshot.get("truncated") or len(ticket_rows) > MAX_TICKET_ROWS
                ),
            }
        )

    agent_rows: list[dict[str, Any]] = []
    for group in groups.values():
        last_seen = group["last_seen"]
        if group["busy"]:
            status = "busy"
        elif (
            last_seen is not None and (now - last_seen).total_seconds() <= stale_seconds
        ):
            status = "available"
        else:
            status = "stale"
        agent_rows.append(
            {
                "principal_id": group["principal_id"],
                "agent_name": group["agent_name"],
                "boards": sorted(group["boards"]),
                "last_seen": last_seen.isoformat() if last_seen else None,
                "pool_status": status,
            }
        )
    rank = {"busy": 0, "available": 1, "stale": 2}
    agent_rows.sort(key=lambda item: (rank[item["pool_status"]], item["agent_name"]))
    busy = sum(item["pool_status"] == "busy" for item in agent_rows)
    available = sum(item["pool_status"] == "available" for item in agent_rows)
    stale = sum(item["pool_status"] == "stale" for item in agent_rows)
    agent_rows = agent_rows[:MAX_AGENT_ROWS]
    return {
        "generated_at": now.isoformat(),
        "stale_after_seconds": stale_seconds,
        "pool_summary": {
            "online": busy + available,
            "busy": busy,
            "available": available,
            "stale": stale,
        },
        "agents": agent_rows,
        "boards": boards,
        "bounds": {
            "boards": MAX_BOARDS,
            "snapshot_items_per_collection": SNAPSHOT_LIMIT,
            "snapshot_bytes": SNAPSHOT_MAX_BYTES,
            "ticket_rows_per_board": MAX_TICKET_ROWS,
            "events_per_board": MAX_EVENT_ROWS,
            "agents": MAX_AGENT_ROWS,
        },
    }


class FleetFetcher:
    def __init__(
        self,
        config: Config,
        client_factory: Callable[..., Any] = BoardClient,
    ) -> None:
        self.config = config
        self.client_factory = client_factory

    def _client(self, board_id: str) -> Any:
        return self.client_factory(
            self.config.url,
            self.config.token,
            board_id,
            agent_name=self.config.agent_name,
        )

    async def _boards(self) -> list[tuple[str, str]]:
        async with self._client(self.config.home_board) as client:
            registry = await client.board_state_get(key="project_registry")
        return parse_project_registry(registry, self.config.home_board)

    async def _board_event_feed(
        self, client: FleetClient, latest_seq: int
    ) -> list[dict[str, Any]]:
        result = await client.board_catchup(
            cursor=max(0, latest_seq - EVENT_SCAN_LIMIT),
            limit=EVENT_SCAN_LIMIT,
            ack=False,
            max_events=EVENT_SCAN_LIMIT,
            max_bytes=EVENT_MAX_BYTES,
        )
        events = result.get("events")
        return events if isinstance(events, list) else []

    async def _read_board(self, label: str, board_id: str) -> dict[str, Any]:
        try:
            async with self._client(board_id) as client:
                snapshot = await client.board_snapshot(
                    limit=SNAPSHOT_LIMIT, max_bytes=SNAPSHOT_MAX_BYTES
                )
                events = await self._board_event_feed(
                    client, int(snapshot.get("latest_seq", 0))
                )
            return {
                "label": label,
                "board_id": board_id,
                "snapshot": snapshot,
                "events": events,
            }
        except Exception as exc:  # noqa: BLE001 - isolate one unavailable board.
            return {
                "label": label,
                "board_id": board_id,
                "error": type(exc).__name__,
            }

    async def fetch(self) -> dict[str, Any]:
        boards = await self._boards()
        rows = await asyncio.gather(
            *(self._read_board(label, board_id) for label, board_id in boards)
        )
        return aggregate_fleet(rows, stale_seconds=self.config.stale_seconds)


class TimedCache:
    def __init__(
        self, ttl_seconds: float, loader: Callable[[], Awaitable[dict[str, Any]]]
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.loader = loader
        self._lock = threading.Lock()
        self._expires_at = 0.0
        self._value: dict[str, Any] | None = None

    def get(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._value is None or now >= self._expires_at:
                self._value = asyncio.run(self.loader())
                self._expires_at = time.monotonic() + self.ttl_seconds
            return self._value


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fleet Dashboard</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#151b2d;--line:#29324a;--text:#e7ecf7;--muted:#9aa6bf;--good:#46d39a;--warn:#f4bd55;--bad:#ef6f7d;--accent:#79a8ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif}main{max-width:1500px;margin:auto;padding:24px}.top{display:flex;justify-content:space-between;gap:16px;align-items:end}h1,h2,h3,p{margin:0}h1{font-size:24px}h2{font-size:17px}.muted,.meta{color:var(--muted)}.strip{display:grid;grid-template-columns:repeat(4,minmax(100px,1fr));gap:10px;margin:20px 0}.metric,.card{background:var(--panel);border:1px solid var(--line);border-radius:12px}.metric{padding:14px}.metric b{display:block;font-size:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(390px,1fr));gap:14px}.card{padding:16px;min-width:0}.counts{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.pill{padding:4px 8px;border-radius:999px;background:#202942}.tickets,.events,.agents{width:100%;border-collapse:collapse}.tickets th,.tickets td,.events td,.agents th,.agents td{padding:7px 5px;text-align:left;border-top:1px solid var(--line);vertical-align:top}.tickets th,.agents th{color:var(--muted);font-weight:500}.id{font-family:ui-monospace,SFMono-Regular,monospace;color:var(--accent);white-space:nowrap}.status{font-size:12px;border-radius:999px;padding:2px 6px;background:#26304a}.pool{margin-top:18px}.busy{color:var(--warn)}.available{color:var(--good)}.stale,.error{color:var(--bad)}#state{font-size:12px}.empty{color:var(--muted);padding:8px 0}@media(max-width:600px){main{padding:14px}.strip{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.hide-small{display:none}}
</style></head><body><main><div class="top"><div><h1>Fleet Dashboard</h1><p class="muted">Live boards and shared agent pool</p></div><div id="state" class="muted">Loading…</div></div><section id="summary" class="strip"></section><section id="boards" class="grid"></section><section class="card pool"><h2>Agent pool</h2><div id="agents"></div></section></main><script>
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const fmt=v=>v?new Date(v).toLocaleString():'—';
function render(d){const s=d.pool_summary;document.querySelector('#summary').innerHTML=['online','busy','available','stale'].map(k=>`<div class="metric"><span class="${k}">${esc(k)}</span><b>${esc(s[k])}</b></div>`).join('');document.querySelector('#boards').innerHTML=d.boards.map(b=>`<article class="card"><div class="top"><div><h2>${esc(b.label)}</h2><span class="meta">${esc(b.board_id)}</span></div>${b.truncated?'<span class="status">bounded view</span>':''}</div>${b.error?`<p class="error">Unavailable: ${esc(b.error)}</p>`:`<div class="counts">${Object.entries(b.counts).map(([k,v])=>`<span class="pill">${esc(k.replace('_',' '))}: <b>${esc(v)}</b></span>`).join('')}</div><table class="tickets"><thead><tr><th>Ticket</th><th>Title</th><th>Status</th><th class="hide-small">Claimed by</th></tr></thead><tbody>${b.tickets.length?b.tickets.map(t=>`<tr><td class="id">${esc(t.id)}</td><td>${esc(t.title)}</td><td><span class="status">${esc(t.status)}</span></td><td class="hide-small">${esc(t.claimed_by||'—')}</td></tr>`).join(''):'<tr><td colspan="4" class="empty">No active tickets</td></tr>'}</tbody></table><h3 style="margin-top:14px">Recent activity</h3><table class="events"><tbody>${b.events.length?b.events.map(e=>`<tr><td class="id">${esc(e.seq??'—')}</td><td>${esc(e.kind)}</td><td>${esc(e.ticket_id||'')}</td><td class="meta hide-small">${esc(fmt(e.occurred_at))}</td></tr>`).join(''):'<tr><td class="empty">No recent visible activity</td></tr>'}</tbody></table>`}</article>`).join('');document.querySelector('#agents').innerHTML=`<table class="agents"><thead><tr><th>Agent</th><th>Status</th><th>Boards</th><th>Last seen</th></tr></thead><tbody>${d.agents.map(a=>`<tr><td>${esc(a.agent_name)}</td><td class="${esc(a.pool_status)}">${esc(a.pool_status)}</td><td>${esc(a.boards.join(', '))}</td><td>${esc(fmt(a.last_seen))}</td></tr>`).join('')}</tbody></table>`;document.querySelector('#state').textContent=`Updated ${fmt(d.generated_at)}`}
async function refresh(){try{const r=await fetch('/api/fleet',{cache:'no-store'});if(!r.ok)throw new Error(`HTTP ${r.status}`);render(await r.json())}catch(e){document.querySelector('#state').textContent=`Refresh failed: ${e.message}`}}refresh();setInterval(refresh,5000);
</script></body></html>"""


def make_handler(cache: TimedCache) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
                return
            if self.path == "/api/fleet":
                try:
                    body = json.dumps(
                        cache.get(), ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                except Exception as exc:  # noqa: BLE001 - return bounded HTTP error.
                    body = json.dumps({"error": type(exc).__name__}).encode("utf-8")
                    self._send(503, "application/json; charset=utf-8", body)
                    return
                self._send(200, "application/json; charset=utf-8", body)
                return
            self._send(404, "application/json; charset=utf-8", b'{"error":"not found"}')

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return Handler


def _token_from_args(token_file: str | None) -> str:
    if token_file:
        token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    else:
        token = os.environ.get("ONBOARD_CENTRAL_TOKEN", "").strip()
    if not token:
        raise SystemExit("ONBOARD_CENTRAL_TOKEN or --token-file is required")
    return token


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the loopback fleet dashboard")
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument(
        "--url", default=os.environ.get("ONBOARD_CENTRAL_URL", DEFAULT_URL)
    )
    parser.add_argument("--token-file")
    parser.add_argument("--home-board", default=DEFAULT_HOME_BOARD)
    parser.add_argument("--agent-name", default="fleet-dashboard-viewer")
    parser.add_argument("--stale-seconds", type=int, default=300)
    parser.add_argument("--cache-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1":
        parser.error("--host must be 127.0.0.1; non-loopback binding is refused")
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    if args.stale_seconds < 1 or args.cache_seconds <= 0:
        parser.error("stale and cache intervals must be positive")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = Config(
        url=args.url,
        token=_token_from_args(args.token_file),
        home_board=args.home_board,
        agent_name=args.agent_name,
        stale_seconds=args.stale_seconds,
        cache_seconds=args.cache_seconds,
    )
    fetcher = FleetFetcher(config)
    cache = TimedCache(config.cache_seconds, fetcher.fetch)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(cache))
    print(f"Fleet Dashboard: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
