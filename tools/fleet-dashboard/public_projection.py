"""Closed, read-only projection and HTTP surface for Fleet public display."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


SCHEMA_VERSION = 1
DEFAULT_COHORT_MINIMUM = 5
ROLLING_WINDOW_DAYS = 30
ACTIVITY_DELAY_MINUTES = 15
PUBLIC_SNAPSHOT_TTL_MINUTES = 60
PUBLIC_NOT_FOUND = b'{"error":"not found"}'
PUBLIC_METHOD_NOT_ALLOWED = b'{"error":"method not allowed"}'
PUBLIC_UNAVAILABLE = (
    b'{"schema_version":1,"mode":"public","freshness":"unavailable",'
    b'"health":"unavailable"}'
)
ALIAS_RE = re.compile(r"^(project|work|agent)-[a-z2-7]{16}$")
RELEASE_RE = re.compile(r"^[0-9]+\.[0-9]+$")

FRESHNESS_VALUES = {"recent", "aging", "stale", "unavailable"}
HEALTH_VALUES = {"operational", "degraded", "unavailable"}
PROJECT_HEALTH_VALUES = {"active", "quiet", "degraded"}
COUNT_VALUES = {"none", "few", "several", "many"}
WORK_STATES = {"queued", "active", "review", "completed", "ended"}
AGE_VALUES = {"recent", "today", "older"}
TRANSITION_VALUES = {
    "created",
    "started",
    "submitted",
    "completed",
    "reworked",
    "ended",
}
ROLE_VALUES = {"worker", "reviewer", "coordinator", "other"}

_STATE_MAP = {
    "open": "queued",
    "offered": "queued",
    "claimed": "active",
    "in_progress": "active",
    "in-progress": "active",
    "reporting": "active",
    "submitted": "review",
    "reviewing": "review",
    "closed": "completed",
    "rejected": "ended",
    "canceled": "ended",
    "cancelled": "ended",
    "terminated": "ended",
    "needs_human": "ended",
    "needs-human": "ended",
}
_TRANSITION_MAP = {
    "ticket_created": "created",
    "ticket_claimed": "started",
    "ticket_started": "started",
    "ticket_submitted": "submitted",
    "ticket_closed": "completed",
    "ticket_approved": "completed",
    "ticket_rejected": "reworked",
    "ticket_canceled": "ended",
    "ticket_cancelled": "ended",
    "ticket_terminated": "ended",
}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


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


def _bucket(count: int, minimum: int) -> tuple[str, bool]:
    if count == 0:
        return "none", False
    if count < minimum:
        return "none", True
    if count < 10:
        return "few", False
    if count < 25:
        return "several", False
    return "many", False


def _age_bucket(value: datetime, now: datetime) -> str:
    age = max(timedelta(), now - value)
    if age < timedelta(hours=1):
        return "recent"
    if age <= timedelta(hours=24):
        return "today"
    return "older"


def _freshness(value: Any, now: datetime) -> str:
    generated = _parse_time(value)
    if generated is None:
        return "unavailable"
    age = max(timedelta(), now - generated)
    if age < timedelta(hours=1):
        return "recent"
    if age <= timedelta(hours=24):
        return "aging"
    return "stale"


def _activity_jitter_minutes(
    key: bytes, transition: str, event: Mapping[str, Any]
) -> int:
    stable_input = next(
        (
            value
            for value in (event.get("id"), event.get("ticket_id"), event.get("kind"))
            if isinstance(value, str) and value
        ),
        transition,
    )
    message = f"v1\0activity\0{transition}\0{stable_input}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).digest()[0] % 6


def load_or_create_alias_key(path: str | Path) -> bytes:
    """Load a private 256-bit alias key, creating it atomically as mode 0600."""
    key_path = Path(path).expanduser().resolve()
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        descriptor = None
    if descriptor is not None:
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            key_path.unlink(missing_ok=True)
            raise
    info = key_path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("public alias key must be a regular 0600 file")
    key = key_path.read_bytes()
    if len(key) != 32:
        raise ValueError("public alias key must contain exactly 32 bytes")
    return key


def public_alias(key: bytes, entity_type: str, canonical_id: str) -> str:
    if len(key) != 32 or entity_type not in {"project", "work", "agent"}:
        raise ValueError("invalid public alias input")
    if not isinstance(canonical_id, str) or not canonical_id:
        raise ValueError("canonical id must be a non-empty string")
    message = f"v1\0{entity_type}\0{canonical_id}".encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()[:10]
    suffix = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"{entity_type}-{suffix}"


def _ticket_id(ticket: Mapping[str, Any]) -> str | None:
    value = ticket.get("id", ticket.get("ticket_id"))
    return value if isinstance(value, str) and value else None


def _ticket_time(ticket: Mapping[str, Any]) -> datetime | None:
    for key in ("updated_at", "created_at", "closed_at"):
        parsed = _parse_time(ticket.get(key))
        if parsed is not None:
            return parsed
    return None


def _project_aliases(
    key: bytes,
    boards: list[Mapping[str, Any]],
    aliaser: Callable[[bytes, str, str], str],
) -> tuple[dict[str, str], set[str]]:
    by_alias: dict[str, list[str]] = {}
    for board in boards:
        board_id = board.get("board_id")
        if not isinstance(board_id, str) or not board_id:
            continue
        alias = aliaser(key, "project", board_id)
        by_alias.setdefault(alias, []).append(board_id)
    collisions = {alias for alias, ids in by_alias.items() if len(set(ids)) > 1}
    return (
        {
            ids[0]: alias
            for alias, ids in by_alias.items()
            if alias not in collisions and ALIAS_RE.fullmatch(alias)
        },
        collisions,
    )


def project_public_snapshot(
    source: Mapping[str, Any],
    alias_key: bytes,
    *,
    now: datetime | None = None,
    release: str = "5.0",
    cohort_minimum: int = DEFAULT_COHORT_MINIMUM,
    aliaser: Callable[[bytes, str, str], str] = public_alias,
) -> dict[str, Any]:
    """Construct new public primitives from one private Fleet generation."""
    if not isinstance(source, Mapping):
        raise ValueError("source snapshot must be an object")
    if not RELEASE_RE.fullmatch(release):
        raise ValueError("public release must be major.minor")
    if cohort_minimum < DEFAULT_COHORT_MINIMUM:
        raise ValueError("public cohort minimum cannot be lower than 5")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(days=ROLLING_WINDOW_DAYS)
    raw_boards = source.get("boards")
    boards = [row for row in raw_boards or [] if isinstance(row, Mapping)]
    aliases, collisions = _project_aliases(alias_key, boards, aliaser)
    suppressed = bool(collisions)
    projects: list[dict[str, Any]] = []
    project_details: dict[str, dict[str, Any]] = {}
    all_tickets: list[tuple[str, Mapping[str, Any], str, datetime]] = []

    for board in boards:
        board_id = board.get("board_id")
        alias = aliases.get(board_id) if isinstance(board_id, str) else None
        if alias is None:
            suppressed = True
            continue
        raw_tickets = board.get("tickets")
        unique: dict[str, tuple[Mapping[str, Any], str, datetime]] = {}
        for ticket in raw_tickets or []:
            if not isinstance(ticket, Mapping):
                suppressed = True
                continue
            identifier = _ticket_id(ticket)
            changed_at = _ticket_time(ticket)
            state = _STATE_MAP.get(str(ticket.get("status") or "").casefold())
            if (
                identifier is None
                or changed_at is None
                or changed_at < cutoff
                or state is None
            ):
                suppressed = True
                continue
            unique[identifier] = (ticket, state, changed_at)
        if len(unique) < cohort_minimum:
            if unique:
                suppressed = True
            continue
        counts = Counter(state for _ticket, state, _at in unique.values())
        public_counts: dict[str, str] = {}
        hidden_state_count = 0
        for state in ("queued", "active", "review"):
            public_counts[state], hidden = _bucket(counts[state], cohort_minimum)
            hidden_state_count += counts[state] if hidden else 0
            suppressed = suppressed or hidden
        workload, workload_hidden = _bucket(len(unique), cohort_minimum)
        if hidden_state_count:
            workload = "none"
            workload_hidden = True
        suppressed = suppressed or workload_hidden
        status = str(board.get("status") or "").casefold()
        project_health = "degraded" if status == "error" else (
            "active" if counts["active"] or counts["review"] else "quiet"
        )
        row = {
            "alias": alias,
            "health": project_health,
            "workload": workload,
            "work": public_counts,
        }
        work_items = []
        work_aliases: dict[str, str] = {}
        duplicate_aliases: set[str] = set()
        for identifier, (_ticket, state, changed_at) in unique.items():
            if counts[state] < cohort_minimum:
                continue
            item_alias = aliaser(alias_key, "work", identifier)
            if not ALIAS_RE.fullmatch(item_alias):
                suppressed = True
                continue
            if item_alias in work_aliases.values():
                duplicate_aliases.add(item_alias)
            work_aliases[identifier] = item_alias
            work_items.append(
                {
                    "alias": item_alias,
                    "state": state,
                    "age_bucket": _age_bucket(changed_at, now),
                }
            )
            all_tickets.append((identifier, _ticket, state, changed_at))
        if duplicate_aliases:
            work_items = [
                item for item in work_items if item["alias"] not in duplicate_aliases
            ]
            suppressed = True
        work_items.sort(key=lambda item: (item["state"], item["alias"]))
        projects.append(row)
        project_details[alias] = {"project": row, "work_items": work_items}

    projects.sort(key=lambda row: row["alias"])
    raw_agents = source.get("agents")
    active_agents = [
        row
        for row in raw_agents or []
        if isinstance(row, Mapping)
        and str(row.get("pool_status") or "").casefold()
        in {"busy", "available", "connected", "working"}
    ]
    active_bucket, hidden = _bucket(len(active_agents), cohort_minimum)
    suppressed = suppressed or hidden
    roles = Counter()
    for agent in active_agents:
        raw_roles = {
            str(seat.get("role") or "").casefold()
            for seat in agent.get("seats") or []
            if isinstance(seat, Mapping)
        }
        if not raw_roles:
            raw_roles = {str(agent.get("role") or "other").casefold()}
        for role in raw_roles:
            roles[role if role in ROLE_VALUES else "other"] += 1
    role_rows = []
    for role, count in sorted(roles.items()):
        bucket, role_hidden = _bucket(count, cohort_minimum)
        if role_hidden:
            suppressed = True
            continue
        role_rows.append({"role": role, "count": bucket})

    awaiting_review = sum(state == "review" for _id, _row, state, _at in all_tickets)
    awaiting_human = sum(
        len(board.get("human_requests") or [])
        for board in boards
        if isinstance(board.get("human_requests") or [], list)
    )
    review_bucket, review_hidden = _bucket(awaiting_review, cohort_minimum)
    human_bucket, human_hidden = _bucket(awaiting_human, cohort_minimum)
    suppressed = suppressed or review_hidden or human_hidden

    transition_counts: Counter[tuple[str, str]] = Counter()
    delay = timedelta(minutes=ACTIVITY_DELAY_MINUTES)
    for board in boards:
        for event in board.get("events") or []:
            if not isinstance(event, Mapping):
                suppressed = True
                continue
            transition = _TRANSITION_MAP.get(str(event.get("kind") or "").casefold())
            occurred_at = _parse_time(event.get("occurred_at"))
            if transition is None or occurred_at is None or occurred_at < cutoff:
                suppressed = True
                continue
            jitter = timedelta(
                minutes=_activity_jitter_minutes(alias_key, transition, event)
            )
            if now - occurred_at < delay + jitter:
                suppressed = True
                continue
            transition_counts[(_age_bucket(occurred_at, now), transition)] += 1
    activity = []
    for (window, transition), count in sorted(transition_counts.items()):
        bucket, activity_hidden = _bucket(count, cohort_minimum)
        if activity_hidden:
            suppressed = True
            continue
        activity.append({"window": window, "transition": transition, "count": bucket})

    freshness = _freshness(source.get("generated_at"), now)
    health = "unavailable" if freshness == "unavailable" else (
        "degraded"
        if any(str(board.get("status") or "").casefold() == "error" for board in boards)
        else "operational"
    )
    document = {
        "schema_version": SCHEMA_VERSION,
        "mode": "public",
        "release": release,
        "freshness": freshness,
        "health": health,
        "projects": projects,
        "fleet": {"active_agents": active_bucket, "roles": role_rows},
        "approvals": {
            "awaiting_review": review_bucket,
            "awaiting_human": human_bucket,
        },
        "activity": activity,
        "suppressed": suppressed,
    }
    generated_at = _parse_time(source.get("generated_at")) or now
    generated_at = min(generated_at, now)
    projection = {
        "summary": document,
        "projects": project_details,
        "valid_until": (
            generated_at + timedelta(minutes=PUBLIC_SNAPSHOT_TTL_MINUTES)
        ).isoformat(),
    }
    validate_public_projection(projection)
    return projection


def _require_keys(value: Any, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("public document has an invalid closed schema")
    return value


def validate_public_document(document: Mapping[str, Any]) -> None:
    root = _require_keys(
        document,
        {
            "schema_version",
            "mode",
            "release",
            "freshness",
            "health",
            "projects",
            "fleet",
            "approvals",
            "activity",
            "suppressed",
        },
    )
    if (
        root["schema_version"] != SCHEMA_VERSION
        or root["mode"] != "public"
        or not isinstance(root["release"], str)
        or not RELEASE_RE.fullmatch(root["release"])
        or root["freshness"] not in FRESHNESS_VALUES
        or root["health"] not in HEALTH_VALUES
        or type(root["suppressed"]) is not bool
    ):
        raise ValueError("public document metadata is invalid")
    if not isinstance(root["projects"], list):
        raise ValueError("public projects must be a list")
    for project in root["projects"]:
        row = _require_keys(project, {"alias", "health", "workload", "work"})
        if (
            not isinstance(row["alias"], str)
            or not ALIAS_RE.fullmatch(row["alias"])
            or not row["alias"].startswith("project-")
            or row["health"] not in PROJECT_HEALTH_VALUES
            or row["workload"] not in COUNT_VALUES
        ):
            raise ValueError("public project row is invalid")
        work = _require_keys(row["work"], {"queued", "active", "review"})
        if any(value not in COUNT_VALUES for value in work.values()):
            raise ValueError("public project work bucket is invalid")
    fleet = _require_keys(root["fleet"], {"active_agents", "roles"})
    if fleet["active_agents"] not in COUNT_VALUES or not isinstance(
        fleet["roles"], list
    ):
        raise ValueError("public fleet row is invalid")
    for role in fleet["roles"]:
        row = _require_keys(role, {"role", "count"})
        if row["role"] not in ROLE_VALUES or row["count"] not in COUNT_VALUES:
            raise ValueError("public role row is invalid")
    approvals = _require_keys(root["approvals"], {"awaiting_review", "awaiting_human"})
    if any(value not in COUNT_VALUES for value in approvals.values()):
        raise ValueError("public approval bucket is invalid")
    if not isinstance(root["activity"], list):
        raise ValueError("public activity must be a list")
    for activity in root["activity"]:
        row = _require_keys(activity, {"window", "transition", "count"})
        if (
            row["window"] not in AGE_VALUES
            or row["transition"] not in TRANSITION_VALUES
            or row["count"] not in COUNT_VALUES
        ):
            raise ValueError("public activity row is invalid")


def validate_public_projection(projection: Mapping[str, Any]) -> None:
    root = _require_keys(projection, {"summary", "projects", "valid_until"})
    validate_public_document(root["summary"])
    if _parse_time(root["valid_until"]) is None or not isinstance(
        root["projects"], Mapping
    ):
        raise ValueError("public projection metadata is invalid")
    expected_aliases = {row["alias"] for row in root["summary"]["projects"]}
    if set(root["projects"]) != expected_aliases:
        raise ValueError("public project details do not match the summary")
    for alias, detail in root["projects"].items():
        if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
            raise ValueError("public project alias is invalid")
        row = _require_keys(detail, {"project", "work_items"})
        project = row["project"]
        if project not in root["summary"]["projects"] or project["alias"] != alias:
            raise ValueError("public project detail is inconsistent")
        if not isinstance(row["work_items"], list):
            raise ValueError("public work items must be a list")
        for item in row["work_items"]:
            work = _require_keys(item, {"alias", "state", "age_bucket"})
            if (
                not isinstance(work["alias"], str)
                or not ALIAS_RE.fullmatch(work["alias"])
                or not work["alias"].startswith("work-")
                or work["state"] not in WORK_STATES
                or work["age_bucket"] not in AGE_VALUES
            ):
                raise ValueError("public work item is invalid")


PUBLIC_HTML = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><link rel="stylesheet" href="/public/assets/public.css">
<script src="/public/assets/public.js" defer></script></head>
<body><header><p>Fleet public display</p><h1>Operations overview</h1><p id="freshness" aria-live="polite">Loading</p></header>
<nav aria-label="Public views"><a href="#home">Home</a><a href="#projects">Projects</a><a href="#work">Work</a><a href="#team">Team</a><a href="#approvals">Approvals</a><a href="#activity">Activity</a></nav>
<main><label for="search">Search projected data</label><input id="search" autocomplete="off"><section id="content" aria-live="polite"></section></main></body></html>"""

PUBLIC_CSS = b""":root{color-scheme:light dark;font-family:ui-sans-serif,system-ui,sans-serif;background:#101513;color:#edf4ee}
body{max-width:72rem;margin:auto;padding:2rem}header,main{background:#17201b;border:1px solid #34483b;border-radius:1rem;padding:1.5rem;margin-block:1rem}
nav{display:flex;gap:1rem;flex-wrap:wrap}a{color:#a9e4ba}input{display:block;max-width:28rem;width:100%;margin-block:1rem;padding:.7rem}
article{border-top:1px solid #34483b;padding-block:1rem}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:1rem}.muted{color:#aebbb1}
@media(max-width:40rem){body{padding:1rem}}"""

PUBLIC_JS = b"""'use strict';
const content=document.getElementById('content');
const freshness=document.getElementById('freshness');
const search=document.getElementById('search');
let data=null;let details=[];
function node(tag,text,cls=''){const el=document.createElement(tag);el.textContent=String(text);if(cls)el.className=cls;return el}
function card(title,rows,alias=''){const item=node('article','');if(alias)item.dataset.publicAlias=alias;item.append(node('h2',title));for(const row of rows)item.append(node('p',row,'muted'));return item}
function visible(value,q){return !q||JSON.stringify(value).toLowerCase().includes(q)}
function render(){
  if(!data)return;content.replaceChildren();
  const q=search.value.trim().toLowerCase();const view=(location.hash.slice(1)||'home');const grid=node('div','','grid');
  if(view==='home')grid.append(card('Fleet',[`Health: ${data.health}`,`Freshness: ${data.freshness}`,`Active agents: ${data.fleet.active_agents}`]));
  if(view==='projects')for(const project of data.projects){if(visible(project,q))grid.append(card(project.alias,[project.health,`Workload: ${project.workload}`,...Object.entries(project.work).map(([state,count])=>`${state}: ${count}`)],project.alias))}
  if(view==='work')for(const detail of details)for(const item of (detail.work_items||[])){if(visible(item,q))grid.append(card(item.alias,[item.state,item.age_bucket],item.alias))}
  if(view==='team')for(const role of data.fleet.roles){if(visible(role,q))grid.append(card(role.role,[`Count: ${role.count}`]))}
  if(view==='approvals')grid.append(card('Approvals',[`Awaiting review: ${data.approvals.awaiting_review}`,`Awaiting human: ${data.approvals.awaiting_human}`]));
  if(view==='activity')for(const item of data.activity){if(visible(item,q))grid.append(card(item.transition,[item.window,`Count: ${item.count}`]))}
  content.append(grid);if(!grid.children.length)content.append(node('p','No projected rows'));
}
async function load(){
  const response=await fetch('/api/public/v1/summary',{credentials:'same-origin'});if(!response.ok)throw new Error('unavailable');data=await response.json();
  details=await Promise.all(data.projects.map(async project=>{const result=await fetch(`/api/public/v1/projects/${project.alias}`,{credentials:'same-origin'});if(!result.ok)throw new Error('unavailable');return result.json()}));
  freshness.textContent=`${data.health} / ${data.freshness}`;render();
}
load().catch(()=>{freshness.textContent='unavailable';content.replaceChildren(node('p','Public data is unavailable'))});
search.addEventListener('input',render);window.addEventListener('hashchange',render);"""


def _public_headers(etag: str) -> dict[str, str]:
    return {
        "Cache-Control": "private, max-age=15, stale-if-error=60",
        "ETag": etag,
        "Vary": "Authorization",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        ),
    }


def make_public_handler(
    projection: Mapping[str, Any],
    *,
    clock: Callable[[], datetime] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Create a GET/HEAD-only handler over one validated immutable projection."""
    validate_public_projection(projection)
    summary = projection.get("summary")
    details = projection.get("projects")
    valid_until = _parse_time(projection.get("valid_until"))
    assert isinstance(details, Mapping) and valid_until is not None
    now = clock or (lambda: datetime.now(timezone.utc))
    summary_body = _json_bytes(summary)
    summary_etag = f'"{hashlib.sha256(summary_body).hexdigest()}"'
    assets = {
        "/public": ("text/html; charset=utf-8", PUBLIC_HTML),
        "/public/": ("text/html; charset=utf-8", PUBLIC_HTML),
        "/public/assets/public.css": ("text/css; charset=utf-8", PUBLIC_CSS),
        "/public/assets/public.js": ("text/javascript; charset=utf-8", PUBLIC_JS),
        "/api/public/v1/summary": ("application/json; charset=utf-8", summary_body),
    }
    project_assets: dict[str, bytes] = {}
    for alias, detail in details.items():
        body = {
            "schema_version": SCHEMA_VERSION,
            "mode": "public",
            "release": summary["release"],
            "freshness": summary["freshness"],
            "project": detail["project"],
        }
        if detail["work_items"]:
            body["work_items"] = detail["work_items"]
        project_assets[alias] = _json_bytes(body)

    class PublicHandler(BaseHTTPRequestHandler):
        def _send(
            self, status: int, content_type: str, body: bytes, *, head: bool
        ) -> None:
            etag = f'"{hashlib.sha256(body).hexdigest()}"'
            not_modified = status == 200 and self.headers.get("If-None-Match") == etag
            self.send_response(304 if not_modified else status)
            self.send_header("Content-Type", content_type)
            for name, value in _public_headers(etag).items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0" if not_modified else str(len(body)))
            self.end_headers()
            if not head and not not_modified:
                self.wfile.write(body)

        def _read(self, *, head: bool) -> None:
            route = urlsplit(self.path).path
            asset = assets.get(route)
            prefix = "/api/public/v1/projects/"
            alias = (
                unquote(route.removeprefix(prefix))
                if route.startswith(prefix)
                else ""
            )
            project_body = (
                project_assets.get(alias) if ALIAS_RE.fullmatch(alias) else None
            )
            if asset is None and project_body is None:
                self._send(
                    404,
                    "application/json; charset=utf-8",
                    PUBLIC_NOT_FOUND,
                    head=head,
                )
                return
            if now().astimezone(timezone.utc) > valid_until:
                self._send(
                    503,
                    "application/json; charset=utf-8",
                    PUBLIC_UNAVAILABLE,
                    head=head,
                )
                return
            if asset is not None:
                self._send(200, asset[0], asset[1], head=head)
                return
            assert project_body is not None
            self._send(
                200,
                "application/json; charset=utf-8",
                project_body,
                head=head,
            )

        def do_GET(self) -> None:
            self._read(head=False)

        def do_HEAD(self) -> None:
            self._read(head=True)

        def _reject_mutation(self) -> None:
            self._send(
                405,
                "application/json; charset=utf-8",
                PUBLIC_METHOD_NOT_ALLOWED,
                head=False,
            )

        do_POST = _reject_mutation
        do_PUT = _reject_mutation
        do_PATCH = _reject_mutation
        do_DELETE = _reject_mutation
        do_OPTIONS = _reject_mutation

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    PublicHandler.public_summary_etag = summary_etag
    return PublicHandler


def projection_digest(projection: Mapping[str, Any]) -> str:
    validate_public_projection(projection)
    return hashlib.sha256(_json_bytes(projection)).hexdigest()
