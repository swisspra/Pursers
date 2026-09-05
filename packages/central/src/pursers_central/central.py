"""On Board Central engine for the Personal Preview runtime."""

from __future__ import annotations

import argparse
import asyncio
import copy
import contextvars
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier, principal_components
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp import types
from mcp_types import INTERNAL_ERROR, INVALID_REQUEST
from pydantic import AnyHttpUrl
from pydantic.fields import FieldInfo

types.ToolAnnotations.model_fields["deprecated"] = FieldInfo(
    annotation=bool | None, default=None
)
types.ToolAnnotations.model_rebuild(force=True)
types.Tool.model_rebuild(force=True)
from pursers_client import (
    ADMISSION_EVENT_KINDS,
    CLAIM_TTL_EVENT_KINDS,
    DEPRECATION_EVENT_KINDS,
    DISPATCH_EVENT_KINDS,
    OFFER_EXPIRED,
    OFFER_REVOKED,
    REVIEW_OFFERED,
    REVIEW_LEASE_EXPIRED,
    REVIEW_LEASE_RELEASED,
    REVIEW_EVENT_KINDS,
    SCRUB_EVENT_KINDS,
    TICKET_REVIEW_CLAIMED,
    TICKET_OFFERED,
)

from cursor import CursorStore
from instance_lock import CentralDataLock
from journal import (
    KINDS as CORE_JOURNAL_KINDS,
    MIN_COMPACTION_RETAIN_LAST,
    SEMANTIC_FIELDS as CORE_JOURNAL_FIELDS,
    Journal,
    _board_token,
    _require_text,
)
from jwt_verifier import JWTTokenVerifier, JWTVerifierConfig
from runtime_health import (
    RuntimeDiagnostics,
    create_streamable_http_app,
    log_runtime_error,
    log_runtime_event,
)
from scrub import Policy, ScrubRejected, scrub
from transactional_sqlite import TransactionalSQLiteStore


ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
DEFAULT_CLAIM_TTL_S = 900
MIN_CLAIM_TTL_S = 1
MAX_CLAIM_TTL_S = 86_400
BRANCH_AND_COMMIT_RE = re.compile(
    r"(?im)^\s*branch_and_commit\s*:\s*(.+?)\s*$"
)
PRE_SUBMISSION_STATES = frozenset({"claimed", "in_progress", "creating_report"})
ACTIVE_TICKET_STATES = frozenset(
    {"open", "claimed", "in_progress", "creating_report", "submitted", "reviewing", "in_review"}
)
TERMINAL_TICKET_STATES = frozenset({"closed", "rejected", "canceled", "terminated"})
TICKET_PRIORITIES = frozenset({"low", "medium", "high", "critical"})
TICKET_SCOPES = frozenset({"READ-ONLY", "interactive-no-send", "interactive"})
MEMORY_TYPES = frozenset(
    {
        "decision",
        "progress",
        "blocker",
        "context",
        "handoff",
        "todo",
        "file_change",
        "discovery",
        "warning",
        "checkpoint",
    }
)
PINNED_SUMMARY_MAX_CHARS = 180
ADMISSION_ROLES = frozenset({"admin", "member", "reviewer"})
COORDINATOR_MEMBERSHIP_ROLES = ADMISSION_ROLES
SEAT_ROLES = frozenset({"worker", "reviewer", "orchestrator", "coordinator"})
INVITE_ROLES = frozenset({"member", "reviewer"})
DEFAULT_INVITE_TTL_S = 3_600
MIN_INVITE_TTL_S = 1
MAX_INVITE_TTL_S = 604_800
ADMISSION_EVENT_FIELDS = frozenset(
    {
        "admission_action",
        "target_principal_id",
        "membership_role",
        "previous_role",
        "invite_id",
        "expires_at",
        "revoked_invite_count",
        "admission_revision",
        "fixture_provenance",
        "recipient_identities",
    }
)
SCRUB_PROFILES = frozenset({"strict", "internal"})
SCRUB_EVENT_FIELDS = frozenset(
    {
        "scrub_profile_from",
        "scrub_profile_to",
        "fixture_provenance",
        "recipient_identities",
    }
)
CLAIM_TTL_EVENT_FIELDS = frozenset(
    {"claim_ttl_from", "claim_ttl_to", "fixture_provenance", "recipient_identities"}
)
REVIEW_POLICIES = frozenset({"strict", "workflow"})
DEPRECATION_EVENT_FIELDS = frozenset(
    {
        "tool",
        "message",
        "caller_principal_id",
        "caller_agent_name",
        "fixture_provenance",
        "recipient_identities",
    }
)
DEPRECATION_WARNING_UNIQUE_FIELDS = (
    "tool",
    "caller_principal_id",
    "caller_agent_name",
)
# Journal-local, oldest-sequence-first retention keeps compaction dedupe durable
# without recreating an unbounded warning registry in the board document.
DEPRECATION_WARNING_DEDUPE_MAX_ENTRIES = 4_096
DEPRECATED_TOOLS = frozenset(
    {
        "agent_nudge",
        "board_get_briefing",
        "memory_checkpoint",
        "memory_handoff",
        "memory_links",
        "memory_read",
        "memory_search",
        "memory_unpin",
        "ticket_assign",
        "ticket_terminate",
    }
)
DEPRECATED_READ_TOOLS = frozenset(
    {"board_get_briefing", "memory_read", "memory_search", "memory_links"}
)
REVIEW_CORE_OVERRIDE_FIELDS = frozenset(
    {"review_policy_at_verdict", "review_label", "review_verdict"}
)
REVIEW_EVENT_FIELDS = frozenset(
    {
        "review_policy_from",
        "review_policy_to",
        "review_policy_at_verdict",
        "review_label",
        "review_verdict",
        "submitted_by_agent_id",
        "submitted_by_agent_name",
        "submitted_by_principal_id",
        "reviewed_by_agent_id",
        "reviewed_by_agent_name",
        "reviewed_by_principal_id",
        "reviewer_agent_id",
        "reviewer_agent_name",
        "reviewer_principal_id",
        "review_lease_expires_at",
        "release_reason",
        "fixture_provenance",
        "recipient_identities",
    }
)
DISPATCH_EVENT_FIELDS = frozenset(
    {
        "ticket_id",
        "offer_kind",
        "offered_agent_id",
        "offered_agent_name",
        "offer_expires_at",
        "dispatch_reason",
        "fixture_provenance",
        "recipient_identities",
    }
)
DISPATCH_PROJECTION_FIELDS = frozenset(
    {
        "seq",
        "kind",
        "ticket_id",
        "offer_kind",
        "offered_agent_name",
        "offer_expires_at",
        "dispatch_reason",
        "occurred_at",
    }
)
DEFAULT_OFFER_TTL_S = 120
MIN_OFFER_TTL_S = 1
MAX_OFFER_TTL_S = 86_400
DEFAULT_FALLBACK_AFTER_OFFERS = 3
COORDINATOR_SCOPE = "board:coordinate"
INTAKE_SCOPE = "board:intake"
INTAKE_ORIGIN = "coordinator-intake"
INTAKE_STATE_KEYS = frozenset({"coordinator_intake", "coordinator_findings"})
INTAKE_CORE_OVERRIDE_FIELDS = frozenset({"origin", "coordinator_op_key"})
DEFAULT_INTAKE_RATE_LIMIT_PER_HOUR = 10
MAX_INTAKE_RATE_LIMIT_PER_HOUR = 1_000
INTAKE_RATE_WINDOW_SECONDS = 3_600
COORDINATOR_EVENT_FIELDS = frozenset(
    {
        "ticket_id",
        "origin",
        "target_agent_id",
        "coordinator_op_key",
        "coordination_reason",
        "expires_at",
        "fixture_provenance",
        "recipient_identities",
    }
)
GENERATION_META_KEY = "io.onboard/expected-generation"
GENERATION_ARGUMENT = "expected_generation"
GENERATION_TOKEN_MAX_CHARS = 256
GENERATION_REJOIN_ERROR = (
    "stale or missing board generation; rejoin with board_join or board_onboard "
    "before retrying"
)
DEFAULT_SNAPSHOT_LIMIT = 100
MAX_SNAPSHOT_LIMIT = 1_000
DEFAULT_SNAPSHOT_MAX_BYTES = 300_000
MIN_SNAPSHOT_MAX_BYTES = 4_096
MAX_SNAPSHOT_MAX_BYTES = 750_000
DEFAULT_CATCHUP_MAX_EVENTS = 200
MAX_CATCHUP_MAX_EVENTS = 1_000
DEFAULT_CATCHUP_MAX_BYTES = DEFAULT_SNAPSHOT_MAX_BYTES
MIN_CATCHUP_MAX_BYTES = MIN_SNAPSHOT_MAX_BYTES
DEFAULT_DISPATCH_PROJECTION_LIMIT = 25
MAX_DISPATCH_PROJECTION_LIMIT = 100
MAX_DISPATCH_PROJECTION_SCAN_EVENTS = 10_000
MAX_CATCHUP_MAX_BYTES = MAX_SNAPSHOT_MAX_BYTES
BRIEFING_OPEN_TICKET_LIMIT = 20
BRIEFING_PINNED_DIGEST_LIMIT = 8
BRIEFING_MEMORY_CONTENT_MAX_CHARS = 2_000
BRIEFING_MEMORY_LIST_LIMIT = 20
BRIEFING_HANDOFF_NEXT_STEPS_LIMIT = 8


@dataclass(frozen=True)
class Principal:
    principal_id: str
    canonical: str
    scopes: frozenset[str]


def current_principal() -> Principal:
    access = get_access_token()
    if access is None:
        raise RuntimeError("authenticated access token missing")
    client_id, issuer, subject = principal_components(access)
    canonical = json.dumps([client_id, issuer or "-", subject or "-"], separators=(",", ":"))
    principal_id = "PR-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Principal(principal_id, canonical, frozenset(access.scopes))


def require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise PermissionError(f"authenticated principal lacks {scope} authorization")


def require_board_write_or_coordinate(principal: Principal) -> bool:
    """Authorize a board writer, or return True for the narrow coordinator path."""
    if "board:write" in principal.scopes:
        return False
    require_scope(principal, COORDINATOR_SCOPE)
    return True


def require_id(field: str, value: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(f"{field} must match {ID_RE.pattern}")
    return value


def _memory_is_visible(entry: Mapping[str, Any], principal_id: str) -> bool:
    """Fail closed for unknown/missing scopes before projection or ranking."""
    scope = entry.get("scope")
    return scope == "project" or (
        scope == "private" and entry.get("author_principal_id") == principal_id
    )


def agent_id(board_id: str, principal_id: str, agent_name: str) -> str:
    logical = json.dumps([board_id, principal_id, agent_name], separators=(",", ":"))
    return "AI-" + hashlib.sha256(logical.encode("utf-8")).hexdigest()


def resource_uri(board_id: str, kind: str, object_id: str) -> str:
    return f"board://{board_id}/{kind}/{object_id}"


def iso_at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class CentralJournal(Journal):
    """Extend the pinned core journal with sanitized board-control events."""

    @staticmethod
    def _deprecation_warning_key(event: Mapping[str, Any]) -> str:
        return json.dumps(
            [event.get(field) for field in DEPRECATION_WARNING_UNIQUE_FIELDS],
            separators=(",", ":"),
        )

    @staticmethod
    def _deprecation_warning_entries(
        document: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        idempotency = document.setdefault("idempotency", {})
        if not isinstance(idempotency, dict):
            raise ValueError("journal idempotency state is corrupt")
        bucket = idempotency.setdefault(
            "deprecated_tool_warning",
            {
                "max_entries": DEPRECATION_WARNING_DEDUPE_MAX_ENTRIES,
                "eviction": "oldest-sequence-first",
                "entries": {},
            },
        )
        if not isinstance(bucket, dict):
            raise ValueError("deprecated warning idempotency state is corrupt")
        bucket["max_entries"] = DEPRECATION_WARNING_DEDUPE_MAX_ENTRIES
        bucket["eviction"] = "oldest-sequence-first"
        entries = bucket.setdefault("entries", {})
        if not isinstance(entries, dict):
            raise ValueError("deprecated warning idempotency entries are corrupt")
        return entries

    @classmethod
    def _remember_deprecation_warning(
        cls,
        document: dict[str, Any],
        event: Mapping[str, Any],
        *,
        trim: bool = True,
    ) -> None:
        if event.get("kind") != "deprecated_tool_warning":
            return
        if any(
            not isinstance(event.get(field), str) or not event.get(field)
            for field in DEPRECATION_WARNING_UNIQUE_FIELDS
        ):
            raise ValueError("deprecated warning identity is incomplete")
        entries = cls._deprecation_warning_entries(document)
        key = cls._deprecation_warning_key(event)
        entries.setdefault(key, {"event": copy.deepcopy(dict(event))})
        if not trim:
            return
        cls._trim_deprecation_warnings(entries)

    @staticmethod
    def _trim_deprecation_warnings(
        entries: dict[str, dict[str, Any]],
    ) -> None:
        overflow = len(entries) - DEPRECATION_WARNING_DEDUPE_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(
                entries,
                key=lambda item: (
                    int(entries[item].get("event", {}).get("seq", 0)),
                    item,
                ),
            )[:overflow]
            for item in oldest:
                entries.pop(item, None)

    def append(self, board_id: str, event: dict[str, Any]) -> dict[str, Any]:
        kind = _require_text("kind", event.get("kind"))
        custom_core_fields = REVIEW_CORE_OVERRIDE_FIELDS | INTAKE_CORE_OVERRIDE_FIELDS
        if kind in CORE_JOURNAL_KINDS and not custom_core_fields.intersection(event):
            return super().append(board_id, event)
        if kind not in (
            CORE_JOURNAL_KINDS
            | ADMISSION_EVENT_KINDS
            | SCRUB_EVENT_KINDS
            | CLAIM_TTL_EVENT_KINDS
            | REVIEW_EVENT_KINDS
            | DISPATCH_EVENT_KINDS
            | DEPRECATION_EVENT_KINDS
        ):
            raise ValueError(f"unsupported event kind: {kind}")
        board_id = _require_text("board_id", board_id)
        actor = _require_text("actor", event.get("actor"))
        payload_ref = _require_text("payload_ref", event.get("payload_ref"))
        semantic_fields = (
            CORE_JOURNAL_FIELDS
            | ADMISSION_EVENT_FIELDS
            | SCRUB_EVENT_FIELDS
            | CLAIM_TTL_EVENT_FIELDS
            | REVIEW_EVENT_FIELDS
            | COORDINATOR_EVENT_FIELDS
            | DISPATCH_EVENT_FIELDS
            | DEPRECATION_EVENT_FIELDS
        )
        semantic = {
            key: copy.deepcopy(event[key])
            for key in sorted(semantic_fields)
            if key in event
        }
        assigned: dict[str, Any] = {}

        def mutate(document: dict[str, Any]) -> None:
            nonlocal assigned
            self._check_document(document, board_id)
            seq = int(document["next_seq"])
            if seq <= int(document.get("compacted_through", 0)):
                raise ValueError("journal next_seq moved backwards")
            assigned = {
                "id": f"EV-{_board_token(board_id)[:12]}-{seq:020d}",
                "seq": seq,
                "board_id": board_id,
                "kind": kind,
                "actor": actor,
                "payload_ref": payload_ref,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                **semantic,
            }
            rows = document.setdefault("rows", [])
            if rows and int(rows[-1]["seq"]) >= seq:
                raise ValueError("journal sequence is not increasing")
            rows.append(assigned)
            document["next_seq"] = seq + 1

        self.store.read_modify_write(
            self._path(board_id), mutate, lambda: self._default(board_id)
        )
        return copy.deepcopy(assigned)

    def append_once(
        self,
        board_id: str,
        event: dict[str, Any],
        *,
        unique_fields: tuple[str, ...],
    ) -> tuple[dict[str, Any], bool]:
        """Append one custom event, or return its existing durable equivalent."""
        kind = _require_text("kind", event.get("kind"))
        if kind not in (
            CORE_JOURNAL_KINDS
            | ADMISSION_EVENT_KINDS
            | SCRUB_EVENT_KINDS
            | CLAIM_TTL_EVENT_KINDS
            | REVIEW_EVENT_KINDS
            | DISPATCH_EVENT_KINDS
            | DEPRECATION_EVENT_KINDS
        ):
            raise ValueError(f"unsupported event kind: {kind}")
        if not unique_fields:
            raise ValueError("unique_fields must not be empty")
        board_id = _require_text("board_id", board_id)
        actor = _require_text("actor", event.get("actor"))
        payload_ref = _require_text("payload_ref", event.get("payload_ref"))
        semantic_fields = (
            CORE_JOURNAL_FIELDS
            | ADMISSION_EVENT_FIELDS
            | SCRUB_EVENT_FIELDS
            | CLAIM_TTL_EVENT_FIELDS
            | REVIEW_EVENT_FIELDS
            | COORDINATOR_EVENT_FIELDS
            | DISPATCH_EVENT_FIELDS
            | DEPRECATION_EVENT_FIELDS
        )
        semantic = {
            key: copy.deepcopy(event[key])
            for key in sorted(semantic_fields)
            if key in event
        }
        for field in unique_fields:
            if field not in semantic:
                raise ValueError(f"idempotent event is missing {field}")
        assigned: dict[str, Any] = {}
        created = False

        def mutate(document: dict[str, Any]) -> None:
            nonlocal assigned, created
            self._check_document(document, board_id)
            rows = document.setdefault("rows", [])
            warning_key = None
            if (
                kind == "deprecated_tool_warning"
                and unique_fields == DEPRECATION_WARNING_UNIQUE_FIELDS
            ):
                warning_key = self._deprecation_warning_key(semantic)
                prior = self._deprecation_warning_entries(document).get(
                    warning_key
                )
                if prior is not None:
                    prior_event = prior.get("event")
                    if not isinstance(prior_event, dict):
                        raise ValueError(
                            "deprecated warning idempotency event is corrupt"
                        )
                    assigned = copy.deepcopy(prior_event)
                    return
            for row in rows:
                if row.get("kind") == kind and all(
                    row.get(field) == semantic[field] for field in unique_fields
                ):
                    assigned = copy.deepcopy(row)
                    if warning_key is not None:
                        self._remember_deprecation_warning(document, row)
                    return
            seq = int(document["next_seq"])
            if seq <= int(document.get("compacted_through", 0)):
                raise ValueError("journal next_seq moved backwards")
            assigned = {
                "id": f"EV-{_board_token(board_id)[:12]}-{seq:020d}",
                "seq": seq,
                "board_id": board_id,
                "kind": kind,
                "actor": actor,
                "payload_ref": payload_ref,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                **semantic,
            }
            if rows and int(rows[-1]["seq"]) >= seq:
                raise ValueError("journal sequence is not increasing")
            rows.append(assigned)
            document["next_seq"] = seq + 1
            if warning_key is not None:
                self._remember_deprecation_warning(document, assigned)
            created = True

        self.store.read_modify_write(
            self._path(board_id), mutate, lambda: self._default(board_id)
        )
        return copy.deepcopy(assigned), created

    def compact(self, board_id: str, retain_last: int) -> dict[str, int]:
        """Compact rows while preserving bounded warning idempotency state."""
        board_id = _require_text("board_id", board_id)
        if (
            type(retain_last) is not int
            or retain_last < MIN_COMPACTION_RETAIN_LAST
        ):
            raise ValueError(
                f"retain_last must be an integer of at least "
                f"{MIN_COMPACTION_RETAIN_LAST}"
            )
        result: dict[str, int] = {}

        def mutate(document: dict[str, Any]) -> None:
            nonlocal result
            self._check_document(document, board_id)
            rows = document["rows"]
            for row in rows:
                self._remember_deprecation_warning(document, row, trim=False)
            warning_entries = document.get("idempotency", {}).get(
                "deprecated_tool_warning", {}
            ).get("entries", {})
            self._trim_deprecation_warnings(warning_entries)
            remove_count = max(0, len(rows) - retain_last)
            removed = rows[:remove_count]
            if removed:
                document["compacted_through"] = int(removed[-1]["seq"])
                document["rows"] = rows[remove_count:]
            result = {
                "removed": len(removed),
                "retained": len(document["rows"]),
                "compacted_through": int(document.get("compacted_through", 0)),
                "latest_cursor": int(document["next_seq"]) - 1,
                "deprecation_dedupe_entries": len(warning_entries),
                "deprecation_dedupe_limit": (
                    DEPRECATION_WARNING_DEDUPE_MAX_ENTRIES
                ),
            }

        self.store.read_modify_write(
            self._path(board_id), mutate, lambda: self._default(board_id)
        )
        return result


class CentralBoard:
    def __init__(self, root: Path):
        self.diagnostics = RuntimeDiagnostics()
        self.backend = os.environ.get("STORE_BACKEND", "sqlite").strip().lower()
        if self.backend != "sqlite":
            raise ValueError("Personal Central requires SQLite storage")
        self.store = TransactionalSQLiteStore(root)
        self.admission = os.environ.get("CENTRAL_ADMISSION", "invite").strip().lower()
        if self.admission != "invite":
            raise ValueError("Personal Central requires invite admission")
        self.journal = CentralJournal(self.store)
        self.cursors = CursorStore(self.store)
        # The JSON skeleton needs one service-wide boundary so a cold snapshot and
        # its journal watermark cannot split a domain-write/journal-append pair.
        self.tool_lock = asyncio.Lock()
        self.pending_notifications: contextvars.ContextVar[list[tuple[Any, str]] | None] = (
            contextvars.ContextVar("central_pending_notifications", default=None)
        )
        self.expected_generation: contextvars.ContextVar[Any] = contextvars.ContextVar(
            "central_expected_generation", default=None
        )

    def has_seat_legacy_capability(
        self, principal_id: str | None, agent_name: str | None
    ) -> bool:
        if os.environ.get("PURSERS_LEGACY_TOOLS") == "1":
            return True
        if not principal_id or not agent_name:
            return False
        try:
            for doc in self.store.iter_documents("boards"):
                aid = agent_id(doc.get("board_id", ""), principal_id, agent_name)
                member = doc.get("members", {}).get(aid)
                if member is not None:
                    caps = member.get("capabilities", {})
                    if isinstance(caps, Mapping) and caps.get("legacy_tools") is True:
                        return True
        except Exception:
            pass
        return False

    def transaction(self):
        transaction = getattr(self.store, "transaction", None)
        return transaction() if transaction is not None else nullcontext()

    @contextmanager
    def board_operation(self, board_id: str):
        """Serialize a whole JSON board operation across server/CLI processes."""
        if self.backend != "json":
            yield
            return
        require_id("board_id", board_id)
        lock_path = self.store.path(
            "operation-locks", f"{_board_token(board_id)}.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _path(self, board_id: str) -> Path:
        require_id("board_id", board_id)
        return self.store.path("boards", f"{_board_token(board_id)}.json")

    def _import_path(self, board_id: str) -> Path:
        require_id("board_id", board_id)
        return self.store.path("imports", f"{_board_token(board_id)}.json")

    @staticmethod
    def _new_generation_token() -> str:
        return "GEN-" + secrets.token_urlsafe(32)

    @staticmethod
    def _checked_generation_token(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or len(value) > GENERATION_TOKEN_MAX_CHARS
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            raise ValueError("board generation metadata is invalid")
        return value

    @staticmethod
    def _checked_generation_revision(value: Any, *, default: int = 1) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("board generation metadata is invalid")
        return value

    @staticmethod
    def _checked_generation_fingerprint(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("expected generation SHA-256 must be 64 lowercase hex characters")
        return value

    def normalize_expected_generation(
        self, metadata_value: Any, argument_value: Any
    ) -> str | None:
        """Unify BoardClient metadata and raw-host argument carriage."""
        metadata = (
            None
            if metadata_value is None
            else self._checked_generation_token(metadata_value)
        )
        argument = (
            None
            if argument_value is None
            else self._checked_generation_token(argument_value)
        )
        if (
            metadata is not None
            and argument is not None
            and not hmac.compare_digest(metadata, argument)
        ):
            raise ValueError(
                "expected_generation argument conflicts with generation metadata"
            )
        return metadata if metadata is not None else argument

    def _import_manifest(self, board_id: str) -> dict[str, Any] | None:
        manifest = self.store.load(self._import_path(board_id), dict)
        if manifest == {}:
            return None
        if not isinstance(manifest, dict):
            raise ValueError("import manifest is corrupt")
        if manifest.get("board_id") != board_id:
            raise ValueError("import manifest board hash collision or corrupt document")
        if manifest.get("status") != "complete":
            raise PermissionError("board import is not complete")
        return manifest

    @staticmethod
    def _default(board_id: str) -> dict[str, Any]:
        return {
            "board_id": board_id,
            "schema_version": 6,
            "generation_token": None,
            "generation_revision": 0,
            "config": {
                "claim_ttl_s": DEFAULT_CLAIM_TTL_S,
                "scrub_profile": "strict",
                "review_policy": "strict",
                "dispatch_policy": {
                    "offer_ttl_s": DEFAULT_OFFER_TTL_S,
                    "second_opinion": True,
                    "fallback_broadcast": True,
                },
                "scrub_allow_counts": {},
                "intake_rate_limit_per_hour": DEFAULT_INTAKE_RATE_LIMIT_PER_HOUR,
            },
            "members": {},
            "principal_memberships": {},
            "principal_revocations": {},
            "invites": {},
            "next_admission_revision": 1,
            "tickets": {},
            "next_ticket_seq": 1,
            "memories": [],
            "next_memory_seq": 1,
            "state": {},
        }

    def ensure_schema(self, document: dict[str, Any]) -> None:
        """Normalize open-mode legacy documents without caller-based promotion."""
        if "principal_memberships" not in document:
            if self.admission == "invite":
                raise PermissionError(
                    "board access denied: legacy board requires offline admin provisioning"
                )
            grouped: dict[str, list[dict[str, Any]]] = {}
            for item in document.get("members", {}).values():
                principal_id = item.get("principal_id")
                if principal_id:
                    grouped.setdefault(str(principal_id), []).append(item)
            ordered = sorted(
                grouped,
                key=lambda principal_id: min(
                    (
                        str(item.get("joined_at", "")),
                        str(item.get("agent_id", "")),
                    )
                    for item in grouped[principal_id]
                ),
            )
            now = iso_at(time.time())
            memberships: dict[str, dict[str, Any]] = {}
            for index, principal_id in enumerate(ordered):
                sessions = grouped[principal_id]
                role = (
                    "admin"
                    if index == 0
                    else "reviewer"
                    if any(item.get("role") == "reviewer" for item in sessions)
                    else "member"
                )
                joined_at = min(str(item.get("joined_at", now)) for item in sessions)
                memberships[principal_id] = {
                    "principal_id": principal_id,
                    "role": role,
                    "source": "open_legacy_migration",
                    "created_at": joined_at,
                    "updated_at": now,
                }
            document["principal_memberships"] = memberships
        document.setdefault("invites", {})
        document.setdefault("principal_revocations", {})
        document.setdefault("next_admission_revision", 1)
        config = document.setdefault("config", {})
        if not isinstance(config, dict):
            raise ValueError("board config is invalid")
        profile = config.setdefault("scrub_profile", "strict")
        if profile not in SCRUB_PROFILES:
            raise ValueError("board scrub profile is invalid")
        review_policy = config.setdefault("review_policy", "strict")
        if review_policy not in REVIEW_POLICIES:
            raise ValueError("board review policy is invalid")
        dispatch_policy = config.setdefault(
            "dispatch_policy",
            {
                "offer_ttl_s": DEFAULT_OFFER_TTL_S,
                "second_opinion": True,
                "fallback_broadcast": True,
            },
        )
        if not isinstance(dispatch_policy, dict):
            raise ValueError("board dispatch policy is invalid")
        offer_ttl_s = dispatch_policy.setdefault("offer_ttl_s", DEFAULT_OFFER_TTL_S)
        if (
            isinstance(offer_ttl_s, bool)
            or not isinstance(offer_ttl_s, int)
            or not MIN_OFFER_TTL_S <= offer_ttl_s <= MAX_OFFER_TTL_S
        ):
            raise ValueError("board dispatch offer_ttl_s is invalid")
        for field, default in (
            ("second_opinion", True),
            ("fallback_broadcast", True),
        ):
            value = dispatch_policy.setdefault(field, default)
            if not isinstance(value, bool):
                raise ValueError(f"board dispatch {field} is invalid")
        intake_rate_limit = config.setdefault(
            "intake_rate_limit_per_hour", DEFAULT_INTAKE_RATE_LIMIT_PER_HOUR
        )
        if (
            isinstance(intake_rate_limit, bool)
            or not isinstance(intake_rate_limit, int)
            or not 1 <= intake_rate_limit <= MAX_INTAKE_RATE_LIMIT_PER_HOUR
        ):
            raise ValueError("board intake rate limit is invalid")
        allow_counts = config.setdefault("scrub_allow_counts", {})
        if not isinstance(allow_counts, dict) or any(
            not isinstance(rule, str)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            for rule, count in allow_counts.items()
        ):
            raise ValueError("board scrub allow counters are invalid")
        board_id = document.get("board_id")
        if not isinstance(board_id, str) or not board_id:
            raise ValueError("board document is missing board_id")
        manifest = self._import_manifest(board_id)
        generation_token = document.get("generation_token")
        if manifest is None:
            if generation_token is not None:
                raise ValueError("fenced board requires a complete import manifest")
            document["generation_token"] = None
            document["generation_revision"] = 0
        else:
            manifest_token = self._checked_generation_token(
                manifest.get("generation_token")
            )
            manifest_revision = self._checked_generation_revision(
                manifest.get("generation_revision")
            )
            board_token = self._checked_generation_token(generation_token)
            board_revision = self._checked_generation_revision(
                document.get("generation_revision")
            )
            if (
                not hmac.compare_digest(board_token, manifest_token)
                or board_revision != manifest_revision
            ):
                raise ValueError("board and import manifest generation metadata disagree")
            document["generation_token"] = board_token
            document["generation_revision"] = board_revision
        document["schema_version"] = max(7, int(document.get("schema_version", 0)))

    @staticmethod
    def is_fresh(document: dict[str, Any]) -> bool:
        return not any(
            (
                document.get("principal_memberships"),
                document.get("principal_revocations"),
                document.get("members"),
                document.get("invites"),
                document.get("tickets"),
                document.get("memories"),
                document.get("state"),
            )
        )

    def resolve_board_context(
        self,
        document: dict[str, Any],
        principal_id: str,
        allowed_roles: frozenset[str] | set[str] | None = None,
    ) -> dict[str, Any]:
        self.ensure_schema(document)
        membership = document["principal_memberships"].get(principal_id)
        if membership is None:
            raise PermissionError("board access denied")
        role = membership.get("role")
        if role not in ADMISSION_ROLES:
            raise PermissionError("board access denied")
        if allowed_roles is not None and role not in allowed_roles:
            raise PermissionError("board role not authorized")
        return membership

    @staticmethod
    def _bootstrap_admin_candidate(document: dict[str, Any]) -> str | None:
        memberships = document.get("principal_memberships", {})
        if not memberships or any(
            item.get("role") == "admin" for item in memberships.values()
        ):
            return None
        # Review-capable board roles are the preferred recovery authority. A
        # deterministic all-member fallback keeps malformed legacy boards from
        # remaining permanently unadministrable when no reviewer survived.
        reviewers = [
            (principal_id, item)
            for principal_id, item in memberships.items()
            if item.get("role") == "reviewer"
        ]
        candidates = reviewers or list(memberships.items())
        return min(
            candidates,
            key=lambda pair: (
                str(pair[1].get("created_at", "")),
                str(pair[0]),
            ),
        )[0]

    def _bootstrap_admin_on_load(
        self, board_id: str, loaded: dict[str, Any]
    ) -> dict[str, Any]:
        marker = loaded.get("bootstrap_admin_migration")
        pending = isinstance(marker, dict) and marker.get("status") == "journal_pending"
        if not pending and self._bootstrap_admin_candidate(loaded) is None:
            return loaded

        migration: dict[str, Any] = {}

        def promote(document: dict[str, Any]) -> None:
            nonlocal migration
            if document.get("board_id") != board_id:
                raise ValueError("board hash collision or corrupt document")
            self.ensure_schema(document)
            existing_marker = document.get("bootstrap_admin_migration")
            if (
                isinstance(existing_marker, dict)
                and existing_marker.get("status") == "journal_pending"
            ):
                migration = copy.deepcopy(existing_marker)
                return
            candidate = self._bootstrap_admin_candidate(document)
            if candidate is None:
                return
            now = time.time()
            membership = document["principal_memberships"][candidate]
            previous_role = str(membership.get("role", "member"))
            membership["role"] = "admin"
            membership["updated_at"] = iso_at(now)
            membership["updated_by_principal_id"] = "board-bootstrap-admin"
            for member in document.get("members", {}).values():
                if member.get("principal_id") == candidate:
                    member["membership_role"] = "admin"
            migration = {
                "version": 1,
                "status": "journal_pending",
                "target_principal_id": candidate,
                "previous_role": previous_role,
                "admission_revision": int(
                    document.setdefault("next_admission_revision", 1)
                ),
                "promoted_at": iso_at(now),
            }
            document["next_admission_revision"] = migration["admission_revision"] + 1
            document["bootstrap_admin_migration"] = copy.deepcopy(migration)

        self.store.read_modify_write(
            self._path(board_id), promote, lambda: self._default(board_id)
        )
        if not migration:
            document = self.store.load(
                self._path(board_id), lambda: self._default(board_id)
            )
            self.ensure_schema(document)
            return document

        event, _created = self.journal.append_once(
            board_id,
            {
                "kind": "board_membership_changed",
                "actor": "board-bootstrap-admin",
                "payload_ref": resource_uri(
                    board_id, "member", migration["target_principal_id"]
                ),
                "recipient_identities": self.admitted_agent_ids(
                    self.store.load(
                        self._path(board_id), lambda: self._default(board_id)
                    )
                ),
                "fixture_provenance": "pursers-personal-runtime",
                "admission_action": "bootstrap_admin_promoted",
                "target_principal_id": migration["target_principal_id"],
                "membership_role": "admin",
                "previous_role": migration["previous_role"],
                "admission_revision": migration["admission_revision"],
            },
            unique_fields=(
                "admission_action",
                "target_principal_id",
                "admission_revision",
            ),
        )

        def complete(document: dict[str, Any]) -> None:
            current = document.get("bootstrap_admin_migration")
            if not isinstance(current, dict):
                raise ValueError("bootstrap admin migration marker is missing")
            if (
                current.get("target_principal_id")
                != migration["target_principal_id"]
                or current.get("admission_revision")
                != migration["admission_revision"]
            ):
                raise ValueError("bootstrap admin migration marker changed")
            current["status"] = "complete"
            current["journal_event_id"] = event["id"]
            current["journal_seq"] = event["seq"]
            current["completed_at"] = iso_at(time.time())

        self.store.read_modify_write(
            self._path(board_id), complete, lambda: self._default(board_id)
        )
        document = self.store.load(
            self._path(board_id), lambda: self._default(board_id)
        )
        self.ensure_schema(document)
        return document

    def load(self, board_id: str) -> dict[str, Any]:
        document = self.store.load(self._path(board_id), lambda: self._default(board_id))
        if document.get("board_id") != board_id:
            raise ValueError("board hash collision or corrupt document")
        self.ensure_schema(document)
        return self._bootstrap_admin_on_load(board_id, document)

    def _assert_expected_generation(self, document: dict[str, Any]) -> None:
        current = document.get("generation_token")
        if current is None:
            return
        expected = self.expected_generation.get()
        if (
            not isinstance(expected, str)
            or not expected
            or len(expected) > GENERATION_TOKEN_MAX_CHARS
            or not hmac.compare_digest(expected, current)
        ):
            raise PermissionError(GENERATION_REJOIN_ERROR)

    def mutate(
        self, board_id: str, callback, *, require_generation: bool = True
    ) -> dict[str, Any]:
        # Admission paths skip the middleware's membership preflight, so force
        # the same durable on-load schema/migration path before every board RMW.
        self.load(board_id)
        result: dict[str, Any] = {}

        def mutate_document(document: dict[str, Any]) -> None:
            nonlocal result
            if document.get("board_id") != board_id:
                raise ValueError("board hash collision or corrupt document")
            self.ensure_schema(document)
            if require_generation:
                self._assert_expected_generation(document)
            result = callback(document)

        self.store.read_modify_write(self._path(board_id), mutate_document, lambda: self._default(board_id))
        return copy.deepcopy(result)

    def validate_generation(self, board_id: str) -> None:
        """Fence a non-board side effect while holding the authoritative board lock."""

        def validate(document: dict[str, Any]) -> None:
            if document.get("board_id") != board_id:
                raise ValueError("board hash collision or corrupt document")
            self.ensure_schema(document)
            self._assert_expected_generation(document)

        self.store.read_modify_write(
            self._path(board_id), validate, lambda: self._default(board_id)
        )

    def advance_generation(
        self, board_id: str, expected_generation_sha256: str
    ) -> dict[str, Any]:
        """Atomically rotate one fenced board generation for maintenance/import."""
        board_id = require_id("board_id", board_id)
        expected_generation_sha256 = self._checked_generation_fingerprint(
            expected_generation_sha256
        )
        result: dict[str, Any] = {}

        def advance_board(document: dict[str, Any]) -> None:
            nonlocal result
            if document.get("board_id") != board_id:
                raise ValueError("board hash collision or corrupt document")
            self.ensure_schema(document)
            current = self._checked_generation_token(
                document.get("generation_token")
            )
            revision = self._checked_generation_revision(
                document.get("generation_revision")
            )
            current_fingerprint = hashlib.sha256(current.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(expected_generation_sha256, current_fingerprint):
                raise PermissionError(GENERATION_REJOIN_ERROR)
            next_revision = revision + 1
            next_token = self._new_generation_token()

            def advance_manifest(manifest: dict[str, Any]) -> None:
                if (
                    manifest.get("board_id") != board_id
                    or manifest.get("status") != "complete"
                ):
                    raise ValueError(
                        "complete import manifest is required for generation advance"
                    )
                manifest_token = self._checked_generation_token(
                    manifest.get("generation_token")
                )
                manifest_revision = self._checked_generation_revision(
                    manifest.get("generation_revision")
                )
                if (
                    not hmac.compare_digest(manifest_token, current)
                    or manifest_revision != revision
                ):
                    raise ValueError(
                        "board and import manifest generation metadata disagree"
                    )
                manifest["generation_token"] = next_token
                manifest["generation_revision"] = next_revision

            self.store.read_modify_write(
                self._import_path(board_id), advance_manifest, dict
            )
            document["generation_token"] = next_token
            document["generation_revision"] = next_revision
            result = {
                "board_id": board_id,
                "generation_revision": next_revision,
                "generation_token_sha256": hashlib.sha256(
                    next_token.encode("utf-8")
                ).hexdigest(),
            }

        with self.board_operation(board_id):
            with self.transaction():
                self.store.read_modify_write(
                    self._path(board_id), advance_board, lambda: self._default(board_id)
                )
        return copy.deepcopy(result)

    def member(
        self, document: dict[str, Any], principal: Principal, agent_name: str
    ) -> dict[str, Any]:
        membership = self.resolve_board_context(document, principal.principal_id)
        key = agent_id(document["board_id"], principal.principal_id, agent_name)
        member = document["members"].get(key)
        if member is None or member["principal_id"] != principal.principal_id:
            raise PermissionError("agent is not a member of this board")
        member["membership_role"] = membership["role"]
        return member

    def principal_is_member(self, board_id: str, principal_id: str) -> bool:
        try:
            self.resolve_board_context(self.load(board_id), principal_id)
            return True
        except PermissionError:
            return False

    def principal_members(
        self, document: dict[str, Any], principal_id: str
    ) -> list[dict[str, Any]]:
        membership = self.resolve_board_context(document, principal_id)
        members = [
            copy.deepcopy(item)
            for item in document["members"].values()
            if item["principal_id"] == principal_id
        ]
        for item in members:
            item["membership_role"] = membership["role"]
        return sorted(members, key=lambda item: item["agent_id"])

    def admitted_agent_ids(
        self, document: dict[str, Any], exclude_agent_id: str | None = None
    ) -> list[str]:
        self.ensure_schema(document)
        admitted = set(document["principal_memberships"])
        return sorted(
            item["agent_id"]
            for item in document.get("members", {}).values()
            if item.get("principal_id") in admitted
            and item.get("agent_id") != exclude_agent_id
        )

    def board_documents_for(self, principal_id: str) -> list[dict[str, Any]]:
        iterator = getattr(self.store, "iter_documents", None)
        if iterator is not None:
            documents = iterator("boards")
        else:
            boards_dir = self.store.path("boards")
            if not boards_dir.exists():
                return []
            documents = [self.store.load(path, {}) for path in sorted(boards_dir.glob("*.json"))]
        visible: list[dict[str, Any]] = []
        for discovered in documents:
            if not isinstance(discovered, dict) or not discovered.get("board_id"):
                raise ValueError("invalid board document")
            document = self.load(str(discovered["board_id"]))
            try:
                self.resolve_board_context(document, principal_id)
            except PermissionError:
                continue
            else:
                visible.append(document)
        return visible

    def subscription_allowed(self, uri: str, principal_id: str) -> bool:
        parsed = urlparse(uri)
        if (
            parsed.scheme != "board"
            or not parsed.netloc
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            return False
        try:
            board_id = require_id("board_id", parsed.netloc)
            document = self.load(board_id)
            self.resolve_board_context(document, principal_id)
            if parsed.path == "/agent" or parsed.path.startswith("/agent/"):
                segments = parsed.path.split("/")
                if len(segments) != 3 or segments[:2] != ["", "agent"]:
                    return False
                requested_agent_id = require_id("agent_id", segments[2])
                member = document.get("members", {}).get(requested_agent_id)
                return bool(
                    member
                    and member.get("principal_id") == principal_id
                )
            return True
        except (PermissionError, ValueError):
            return False


class SubscriptionAuthorization:
    """Deny board subscription URIs unless the bearer principal is a member."""

    def __init__(self, service: CentralBoard):
        self.service = service

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next) -> HandlerResult:
        if ctx.method == "tools/call":
            async with self.service.tool_lock:
                pending: list[tuple[Any, str]] = []
                pending_token = self.service.pending_notifications.set(pending)
                raw: Any = ctx.params
                if hasattr(raw, "model_dump"):
                    raw = raw.model_dump(by_alias=True, exclude_none=True)
                tool_name = raw.get("name") if isinstance(raw, Mapping) else None
                arguments = raw.get("arguments", {}) if isinstance(raw, Mapping) else {}
                raw_meta: Any = ctx.meta
                metadata_generation = (
                    raw_meta.get(GENERATION_META_KEY)
                    if isinstance(raw_meta, Mapping)
                    else None
                )
                argument_generation = (
                    arguments.get(GENERATION_ARGUMENT)
                    if isinstance(arguments, Mapping)
                    else None
                )
                generation_error: str | None = None
                try:
                    expected_generation = self.service.normalize_expected_generation(
                        metadata_generation, argument_generation
                    )
                except ValueError as exc:
                    expected_generation = None
                    generation_error = str(exc)
                generation_token = self.service.expected_generation.set(
                    expected_generation
                )
                try:
                    if generation_error is not None:
                        log_runtime_error(
                            self.service.diagnostics,
                            "tool_error",
                            ValueError(generation_error),
                            include_traceback=False,
                            tool=str(tool_name or "unknown"),
                        )
                        return {
                            "content": [{"type": "text", "text": generation_error}],
                            "isError": True,
                            "resultType": "complete",
                        }
                    board_id = (
                        arguments.get("board_id")
                        if isinstance(arguments, Mapping)
                        else None
                    )
                    operation = (
                        self.service.board_operation(board_id)
                        if isinstance(board_id, str) and ID_RE.fullmatch(board_id)
                        else nullcontext()
                    )
                    with operation:
                        with self.service.transaction():
                            if (
                                board_id is not None
                                and tool_name not in {"board_join", "board_onboard"}
                            ):
                                principal = current_principal()
                                try:
                                    checked_board = require_id("board_id", board_id)
                                    self.service.resolve_board_context(
                                        self.service.load(checked_board),
                                        principal.principal_id,
                                    )
                                except (PermissionError, ValueError):
                                    log_runtime_error(
                                        self.service.diagnostics,
                                        "tool_error",
                                        PermissionError(
                                            "board access denied: principal is not a member"
                                        ),
                                        include_traceback=False,
                                        tool=str(tool_name or "unknown"),
                                    )
                                    # Match ordinary tool authorization failures so
                                    # callers receive a model-visible tool error,
                                    # while still short-circuiting before schema and
                                    # domain handling for every board-scoped tool.
                                    return {
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": (
                                                    "board access denied: principal "
                                                    "is not a member"
                                                ),
                                            }
                                        ],
                                        "isError": True,
                                        "resultType": "complete",
                                    }
                            result = await call_next(ctx)
                    for notification_context, uri in pending:
                        await notification_context.notify_resource_updated(uri)
                    return result
                finally:
                    self.service.expected_generation.reset(generation_token)
                    self.service.pending_notifications.reset(pending_token)
        if ctx.method == "subscriptions/listen":
            raw: Any = ctx.params
            if hasattr(raw, "model_dump"):
                raw = raw.model_dump(by_alias=True, exclude_none=True)
            notifications = raw.get("notifications", {}) if isinstance(raw, Mapping) else {}
            uris = notifications.get("resourceSubscriptions") or notifications.get("resource_subscriptions") or []
            principal = current_principal()
            require_scope(principal, "board:read")
            for uri in uris:
                if not self.service.subscription_allowed(str(uri), principal.principal_id):
                    raise MCPError(INVALID_REQUEST, "subscription denied: principal is not a board member")
        return await call_next(ctx)


def build_server(host: str, port: int, data_root: Path) -> tuple[MCPServer[Any], CentralBoard]:
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("Personal Central host must be loopback")
        except ValueError as exc:
            raise ValueError("Personal Central host must be loopback") from exc
    resource_url = f"http://{host}:{port}/mcp"
    service = CentralBoard(data_root)
    auth_mode = os.environ.get("CENTRAL_AUTH_MODE", "jwt").strip().lower()
    if auth_mode != "jwt":
        raise ValueError("Personal Central requires JWT authentication")
    issuer_url = os.environ.get("CENTRAL_JWT_ISSUER", "").strip()
    audience = os.environ.get("CENTRAL_JWT_AUDIENCE", resource_url).strip()
    jwks_path = os.environ.get("CENTRAL_JWKS_PATH", "").strip()
    if not issuer_url or not audience or not jwks_path:
        raise ValueError(
            "jwt mode requires CENTRAL_JWT_ISSUER, CENTRAL_JWT_AUDIENCE, and CENTRAL_JWKS_PATH"
        )
    try:
        clock_skew_s = int(os.environ.get("CENTRAL_JWT_CLOCK_SKEW", "30"))
    except ValueError as exc:
        raise ValueError("CENTRAL_JWT_CLOCK_SKEW must be an integer") from exc
    token_verifier: TokenVerifier = JWTTokenVerifier(
        JWTVerifierConfig(
            issuer=issuer_url,
            audience=audience,
            jwks_path=Path(jwks_path),
            clock_skew_s=clock_skew_s,
        )
    )
    mcp = MCPServer(
        "On Board Central Skeleton",
        version="0.1.0a9",
        token_verifier=token_verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer_url),
            resource_server_url=AnyHttpUrl(resource_url),
            required_scopes=["board:read"],
        ),
        middleware=[SubscriptionAuthorization(service)],
    )
    deprecated_read_warnings: set[tuple[str, str, str, str]] = set()

    def tool() -> Any:
        """Register tools while preserving intentional client-facing failures."""

        def register(function: Any) -> Any:
            tool_name = function.__name__
            is_deprecated = tool_name in DEPRECATED_TOOLS

            @wraps(function)
            async def wrapped(*args: Any, **kwargs: Any) -> Any:
                try:
                    result = await function(*args, **kwargs)
                except (PermissionError, ValueError) as exc:
                    log_runtime_error(
                        service.diagnostics,
                        "tool_error",
                        exc,
                        include_traceback=False,
                        tool=function.__name__,
                    )
                    raise ToolError(str(exc)) from exc
                except Exception as exc:
                    log_runtime_error(
                        service.diagnostics,
                        "tool_error",
                        exc,
                        include_traceback=True,
                        tool=function.__name__,
                    )
                    raise

                if is_deprecated:
                    if isinstance(result, dict):
                        result.setdefault("_deprecated", True)
                        result.setdefault("deprecated", True)

                    board_id = kwargs.get("board_id")
                    if not board_id and args:
                        board_id = args[0] if isinstance(args[0], str) else None
                    ctx = kwargs.get("ctx")
                    if ctx is None:
                        ctx = next(
                            (value for value in args if isinstance(value, Context)),
                            None,
                        )
                    agent_name = kwargs.get("agent_name")
                    if not agent_name and ctx is not None:
                        try:
                            client_params = ctx.session.client_params
                        except (AttributeError, ValueError):
                            client_params = None
                        client_info = (
                            client_params.client_info
                            if client_params is not None
                            else None
                        )
                        agent_name = getattr(client_info, "name", None)

                    if board_id and isinstance(board_id, str):
                        principal = current_principal()
                        caller_name = str(agent_name or "unknown")
                        if tool_name in DEPRECATED_READ_TOOLS:
                            warning_key = (
                                board_id,
                                tool_name,
                                principal.principal_id,
                                caller_name,
                            )
                            if warning_key not in deprecated_read_warnings:
                                deprecated_read_warnings.add(warning_key)
                                log_runtime_event(
                                    "deprecated_tool_warning",
                                    board_id=board_id,
                                    tool=tool_name,
                                    caller_principal_id=principal.principal_id,
                                    caller_agent_name=caller_name,
                                )
                        else:
                            actor_agent = agent_id(
                                board_id, principal.principal_id, caller_name
                            )
                            _warning, created = await append_once_and_publish(
                                board_id,
                                {"agent_id": actor_agent},
                                "deprecated_tool_warning",
                                f"board://{board_id}/tool/{tool_name}",
                                [],
                                ctx,
                                unique_fields=DEPRECATION_WARNING_UNIQUE_FIELDS,
                                tool=tool_name,
                                caller_principal_id=principal.principal_id,
                                caller_agent_name=caller_name,
                                message=(
                                    f"Tool '{tool_name}' is deprecated in a18 and "
                                    "scheduled for removal in a19."
                                ),
                            )
                            if created and isinstance(result, dict):
                                cur_seq = latest_seq(board_id)
                                if "latest_seq" in result:
                                    result["latest_seq"] = cur_seq
                                if (
                                    "briefing" in result
                                    and isinstance(result["briefing"], dict)
                                    and "latest_seq" in result["briefing"]
                                ):
                                    result["briefing"]["latest_seq"] = cur_seq

                return result

            return mcp.tool()(wrapped)

        return register

    async def append_and_publish(
        board_id: str,
        actor: dict[str, Any],
        kind: str,
        payload_ref: str,
        recipients: list[str],
        ctx: Context | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        try:
            event = service.journal.append(
                board_id,
                {
                    "kind": kind,
                    "actor": actor["agent_id"],
                    "payload_ref": payload_ref,
                    "recipient_identities": recipients,
                    "fixture_provenance": "pursers-personal-runtime",
                    **fields,
                },
            )
        except MCPError:
            raise
        except Exception as exc:
            # MCPServer converts ordinary exceptions to a successful middleware
            # return value. MCPError must escape so SQLite's outer transaction
            # observes the failure and rolls the domain mutation back.
            raise MCPError(INTERNAL_ERROR, "Internal server error") from exc
        if ctx is not None:
            pending = service.pending_notifications.get()
            journal_uri = f"board://{board_id}/journal"
            if pending is None:
                await ctx.notify_resource_updated(payload_ref)
                await ctx.notify_resource_updated(journal_uri)
            else:
                pending.append((ctx, payload_ref))
                pending.append((ctx, journal_uri))
            for agent_id in sorted(set(recipients)):
                if not isinstance(agent_id, str) or not agent_id:
                    continue
                seat_uri = f"board://{board_id}/agent/{agent_id}"
                if pending is None:
                    await ctx.notify_resource_updated(seat_uri)
                else:
                    pending.append((ctx, seat_uri))
        return event

    async def append_once_and_publish(
        board_id: str,
        actor: dict[str, Any],
        kind: str,
        payload_ref: str,
        recipients: list[str],
        ctx: Context | None,
        *,
        unique_fields: tuple[str, ...],
        **fields: Any,
    ) -> tuple[dict[str, Any], bool]:
        try:
            event, created = service.journal.append_once(
                board_id,
                {
                    "kind": kind,
                    "actor": actor["agent_id"],
                    "payload_ref": payload_ref,
                    "recipient_identities": recipients,
                    "fixture_provenance": "pursers-personal-runtime",
                    **fields,
                },
                unique_fields=unique_fields,
            )
        except MCPError:
            raise
        except Exception as exc:
            raise MCPError(INTERNAL_ERROR, "Internal server error") from exc
        if created and ctx is not None:
            pending = service.pending_notifications.get()
            journal_uri = f"board://{board_id}/journal"
            if pending is None:
                await ctx.notify_resource_updated(payload_ref)
                await ctx.notify_resource_updated(journal_uri)
            else:
                pending.append((ctx, payload_ref))
                pending.append((ctx, journal_uri))
            for agent_id in sorted(set(recipients)):
                if not isinstance(agent_id, str) or not agent_id:
                    continue
                seat_uri = f"board://{board_id}/agent/{agent_id}"
                if pending is None:
                    await ctx.notify_resource_updated(seat_uri)
                else:
                    pending.append((ctx, seat_uri))
        return event, created

    async def publish_admission_event(
        board_id: str,
        actor: dict[str, Any],
        change: dict[str, Any] | None,
        recipients: list[str],
        ctx: Context,
    ) -> dict[str, Any] | None:
        if change is None:
            return None
        invite_event = change["kind"] == "board_invite_created"
        object_kind = "invite" if invite_event else "member"
        object_id = (
            change["invite_id"]
            if invite_event
            else change["target_principal_id"]
        )
        fields = {
            key: value
            for key, value in change.items()
            if key != "kind" and value is not None
        }
        return await append_and_publish(
            board_id,
            actor,
            change["kind"],
            resource_uri(board_id, object_kind, str(object_id)),
            recipients,
            ctx,
            **fields,
        )

    def claim_ttl(document: dict[str, Any]) -> int:
        return int(document.setdefault("config", {}).setdefault("claim_ttl_s", DEFAULT_CLAIM_TTL_S))

    def renew_claim(ticket: dict[str, Any], now: float, ttl_s: int) -> None:
        expires = now + ttl_s
        ticket["ttl_s"] = ttl_s
        ticket["lease_expires_at_epoch"] = expires
        ticket["lease_expires_at"] = iso_at(expires)
        ticket["lease_renewed_at"] = iso_at(now)

    def renew_review_lease(lease: dict[str, Any], now: float, ttl_s: int) -> None:
        expires = now + ttl_s
        lease["ttl_s"] = ttl_s
        lease["expires_at_epoch"] = expires
        lease["expires_at"] = iso_at(expires)
        lease["renewed_at"] = iso_at(now)

    def review_lease_is_live(ticket: Mapping[str, Any], now: float) -> bool:
        lease = ticket.get("review_lease")
        if not isinstance(lease, Mapping):
            return False
        expires = lease.get("expires_at_epoch")
        try:
            return expires is not None and float(expires) > now
        except (TypeError, ValueError):
            return False

    def claim_review_lease(
        ticket: dict[str, Any], actor: dict[str, Any], principal: Principal,
        now: float, ttl_s: int,
    ) -> dict[str, Any]:
        lease = {
            "reviewer_agent_id": actor["agent_id"],
            "reviewer_agent_name": actor["agent_name"],
            "reviewer_principal_id": principal.principal_id,
            "claimed_at": iso_at(now),
        }
        renew_review_lease(lease, now, ttl_s)
        ticket["review_lease"] = lease
        ticket["updated_at"] = iso_at(now)
        return lease

    def dispatch_enabled(document: Mapping[str, Any]) -> bool:
        return any(
            bool(member.get("capabilities_explicit"))
            for member in document.get("members", {}).values()
        )

    def dispatch_policy(document: Mapping[str, Any]) -> dict[str, Any]:
        return dict(document.get("config", {}).get("dispatch_policy", {}))

    def agent_matches(values: Iterable[str], member: Mapping[str, Any]) -> bool:
        wanted = {str(value).casefold() for value in values}
        identities = {
            str(member.get("agent_id", "")).casefold(),
            str(member.get("agent_name", "")).casefold(),
        }
        return bool(identities & wanted)

    def agent_is_busy(
        document: Mapping[str, Any], agent_id_value: str, now: float,
        *, excluding_ticket_id: str,
    ) -> bool:
        for candidate in document.get("tickets", {}).values():
            if candidate.get("ticket_id") == excluding_ticket_id:
                continue
            if (
                candidate.get("status") in PRE_SUBMISSION_STATES
                and candidate.get("claimed_by_agent_id") == agent_id_value
            ):
                return True
            lease = candidate.get("review_lease")
            if (
                isinstance(lease, Mapping)
                and lease.get("reviewer_agent_id") == agent_id_value
                and float(lease.get("expires_at_epoch", 0)) > now
            ):
                return True
            for key in ("work_offer", "review_offer"):
                offer = candidate.get(key)
                if (
                    isinstance(offer, Mapping)
                    and offer.get("agent_id") == agent_id_value
                    and float(offer.get("expires_at_epoch", 0)) > now
                ):
                    return True
        return False

    def dispatch_ticket(
        document: dict[str, Any], ticket: dict[str, Any], now: float, kind: str,
    ) -> dict[str, Any] | None:
        wanted_status = "open" if kind == "work" else "submitted"
        if ticket.get("status") != wanted_status:
            return None
        if not dispatch_enabled(document):
            return None
        offer_key = f"{kind}_offer"
        current = ticket.get(offer_key)
        if isinstance(current, Mapping) and float(current.get("expires_at_epoch", 0)) > now:
            return None
        policy = dispatch_policy(document)
        attempts = int(ticket.get(f"{kind}_offer_expirations", 0))
        if policy.get("fallback_broadcast", True) and attempts >= DEFAULT_FALLBACK_AFTER_OFFERS:
            ticket["dispatch_state"] = {
                "state": "broadcast", "kind": kind, "reason": "offer_limit_reached"
            }
            return None
        required_tier = int(ticket.get("tier", 2))
        required_skills = set(ticket.get("skills_required", []))
        excluded = list(ticket.get("exclude_agents", []))
        preferred = list(ticket.get("prefer_agents", []))
        assigned = ticket.get("assigned_to_agent_id") if kind == "work" else None
        requested_assignment = ticket.get("assigned_to") if kind == "work" else None
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for member in document.get("members", {}).values():
            membership = document.get("principal_memberships", {}).get(member.get("principal_id"))
            if membership is None or member.get("lifecycle_status", "active") != "active":
                continue
            if assigned is not None and member.get("agent_id") != assigned:
                continue
            if assigned is None and requested_assignment and not assignment_matches(
                member, str(requested_assignment)
            ):
                continue
            if agent_matches(excluded, member):
                continue
            caps = member_capabilities(member)
            if int(caps["tier_max"]) < required_tier or not required_skills.issubset(caps["skills"]):
                continue
            if kind == "work" and not caps["can_work"]:
                continue
            if kind == "review":
                if not caps["can_review"] or membership.get("role") not in {"admin", "reviewer"}:
                    continue
                if member.get("principal_id") == ticket.get("submitted_by_principal_id"):
                    continue
                if (
                    board_review_policy(document) == "workflow"
                    and member.get("agent_id") == ticket.get("submitted_by_agent_id")
                ):
                    continue
            if agent_is_busy(
                document, str(member["agent_id"]), now,
                excluding_ticket_id=str(ticket["ticket_id"]),
            ):
                continue
            candidates.append((member, caps))
        avoid = (
            ticket.get("last_claimed_by_agent_id") or ticket.get("last_work_offered_agent_id")
        ) if kind == "work" else (
            (
                ticket.get("reviewed_by_agent_id")
                or ticket.get("last_review_released_by_agent_id")
                or ticket.get("last_review_offered_agent_id")
            )
            if policy.get("second_opinion", True) else None
        )
        alternatives = [pair for pair in candidates if pair[0].get("agent_id") != avoid]
        if alternatives:
            candidates = alternatives
        if not candidates:
            reason = f"no_eligible_{'worker' if kind == 'work' else 'reviewer'}"
            state = {"state": "unassignable", "kind": kind, "reason": reason}
            if ticket.get("dispatch_state") == state:
                return None
            ticket["dispatch_state"] = state
            ticket.setdefault("dispatch_history", []).append({**state, "at": iso_at(now)})
            return {
                "kind": "dispatch_unassignable", "ticket_id": ticket["ticket_id"],
                "offer_kind": kind, "dispatch_reason": reason,
                "recipients": service.admitted_agent_ids(document),
            }
        candidates.sort(
            key=lambda pair: (
                0 if agent_matches(preferred, pair[0]) else 1,
                int(pair[1]["tier_max"]) - required_tier,
                str(pair[0].get("last_work_at", pair[0].get("joined_at", ""))),
                str(pair[0]["agent_id"]),
            )
        )
        selected, _ = candidates[0]
        expires_epoch = now + int(policy.get("offer_ttl_s", DEFAULT_OFFER_TTL_S))
        offer = {
            "ticket_id": ticket["ticket_id"], "kind": kind,
            "agent_id": selected["agent_id"], "agent_name": selected["agent_name"],
            "offered_at": iso_at(now), "expires_at": iso_at(expires_epoch),
            "expires_at_epoch": expires_epoch,
        }
        ticket[offer_key] = offer
        ticket["dispatch_state"] = {"state": "offered", **copy.deepcopy(offer)}
        ticket.setdefault("dispatch_history", []).append(
            {"state": "offered", **copy.deepcopy(offer)}
        )
        return {
            "kind": TICKET_OFFERED if kind == "work" else REVIEW_OFFERED,
            "ticket_id": ticket["ticket_id"], "offer_kind": kind,
            "offered_agent_id": selected["agent_id"],
            "offered_agent_name": selected["agent_name"],
            "offer_expires_at": offer["expires_at"],
            "recipients": [selected["agent_id"]],
        }

    def expire_dispatch_offers(
        document: dict[str, Any], ticket: dict[str, Any], now: float,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for kind in ("work", "review"):
            key = f"{kind}_offer"
            offer = ticket.get(key)
            if not isinstance(offer, Mapping) or float(offer.get("expires_at_epoch", 0)) > now:
                continue
            expired = ticket.pop(key)
            ticket[f"last_{kind}_offered_agent_id"] = expired.get("agent_id")
            ticket[f"{kind}_offer_expirations"] = int(
                ticket.get(f"{kind}_offer_expirations", 0)
            ) + 1
            ticket.setdefault("dispatch_history", []).append(
                {
                    "state": "expired", "kind": kind,
                    "agent_id": expired.get("agent_id"), "at": iso_at(now),
                }
            )
            events.append(
                {
                    "kind": OFFER_EXPIRED, "ticket_id": ticket["ticket_id"],
                    "offer_kind": kind, "offered_agent_id": expired.get("agent_id"),
                    "offered_agent_name": expired.get("agent_name"),
                    "offer_expires_at": expired.get("expires_at"),
                    "recipients": [expired.get("agent_id")],
                }
            )
            next_event = dispatch_ticket(document, ticket, now, kind)
            if next_event is not None:
                events.append(next_event)
        return events

    def redispatch_queue(
        document: dict[str, Any], now: float,
    ) -> list[dict[str, Any]]:
        if not dispatch_enabled(document):
            return []
        priority_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        events: list[dict[str, Any]] = []
        for ticket in sorted(
            document["tickets"].values(),
            key=lambda item: (
                priority_rank.get(str(item.get("priority", "medium")), 2),
                str(item.get("created_at", "")),
                str(item.get("ticket_id", "")),
            ),
        ):
            kind = "work" if ticket.get("status") == "open" else (
                "review" if ticket.get("status") == "submitted" else None
            )
            if kind is None:
                continue
            event = dispatch_ticket(document, ticket, now, kind)
            if event is not None:
                events.append(event)
        return events

    def reap_expired(document: dict[str, Any], now: float) -> list[dict[str, Any]]:
        released: list[dict[str, Any]] = []
        recipients = service.admitted_agent_ids(document)
        for ticket in document["tickets"].values():
            released.extend(expire_dispatch_offers(document, ticket, now))
            review_lease = ticket.get("review_lease")
            if isinstance(review_lease, dict):
                review_expires = review_lease.get("expires_at_epoch")
                if review_expires is not None and not review_lease_is_live(ticket, now):
                    expired = ticket.pop("review_lease")
                    ticket["updated_at"] = iso_at(now)
                    released.append(
                        {
                            "kind": "review",
                            "ticket_id": ticket["ticket_id"],
                            "reviewer_agent_id": expired.get("reviewer_agent_id"),
                            "reviewer_agent_name": expired.get("reviewer_agent_name"),
                            "reviewer_principal_id": expired.get("reviewer_principal_id"),
                            "review_lease_expires_at": expired.get("expires_at"),
                            "recipients": recipients,
                        }
                    )
                    dispatch_event = dispatch_ticket(document, ticket, now, "review")
                    if dispatch_event is not None:
                        released.append(dispatch_event)
            if ticket.get("status") not in PRE_SUBMISSION_STATES:
                continue
            expires = ticket.get("lease_expires_at_epoch")
            if expires is None or float(expires) > now:
                continue
            old_status = str(ticket["status"])
            old_holder = str(ticket.get("claimed_by_agent_id", "unknown"))
            old_holder_name = ticket.get("claimed_by")
            abandoned_at = iso_at(now)
            ticket["status"] = "open"
            ticket["abandoned_count"] = int(ticket.get("abandoned_count", 0)) + 1
            ticket["last_abandoned_by"] = old_holder
            if old_holder_name:
                ticket["last_abandoned_by_name"] = old_holder_name
            ticket["last_claimed_by_agent_id"] = ticket.get("claimed_by_agent_id")
            ticket["last_claimed_by_principal_id"] = ticket.get("claimed_by_principal_id")
            ticket["last_claimed_by"] = old_holder_name
            ticket["last_claimed_at"] = ticket.get("claimed_at")
            ticket["last_abandoned_at"] = abandoned_at
            ticket["last_release_reason"] = "lease expired"
            for key in (
                "claimed_by_agent_id",
                "claimed_by_principal_id",
                "claimed_by",
                "claimed_at",
                "lease_expires_at_epoch",
                "lease_expires_at",
                "lease_renewed_at",
                "ttl_s",
            ):
                ticket.pop(key, None)
            released.append(
                {
                    "kind": "work",
                    "ticket_id": ticket["ticket_id"],
                    "status_from": old_status,
                    "status_to": "open",
                    "last_abandoned_by": old_holder,
                    "last_abandoned_at": abandoned_at,
                    "abandoned_count": ticket["abandoned_count"],
                    "recipients": recipients,
                }
            )
            dispatch_event = dispatch_ticket(document, ticket, now, "work")
            if dispatch_event is not None:
                released.append(dispatch_event)
        return released

    def prepare_board_call(
        document: dict[str, Any], principal: Principal, agent_name: str, now: float
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        released = reap_expired(document, now)
        actor = service.member(document, principal, agent_name)
        if actor.get("lifecycle_status", "active") == "handed_off":
            raise PermissionError("agent handed off; call board_onboard or board_join before more work")
        actor["last_activity_at"] = iso_at(now)
        renewed: list[str] = []
        ttl_s = claim_ttl(document)
        for ticket in document["tickets"].values():
            if (
                ticket.get("status") in PRE_SUBMISSION_STATES
                and ticket.get("claimed_by_agent_id") == actor["agent_id"]
            ):
                renew_claim(ticket, now, ttl_s)
                renewed.append(ticket["ticket_id"])
        return actor, released, renewed

    async def publish_releases(
        board_id: str,
        released: list[dict[str, Any]],
        principal: Principal,
        ctx: Context,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        reaper = {"agent_id": f"board-reaper:{principal.principal_id}"}
        for item in released:
            if item.get("kind") in DISPATCH_EVENT_KINDS:
                events.append(
                    await append_and_publish(
                        board_id, reaper, item["kind"],
                        resource_uri(board_id, "ticket", item["ticket_id"]),
                        [value for value in item["recipients"] if value], ctx,
                        ticket_id=item["ticket_id"],
                        offer_kind=item.get("offer_kind"),
                        offered_agent_id=item.get("offered_agent_id"),
                        offered_agent_name=item.get("offered_agent_name"),
                        offer_expires_at=item.get("offer_expires_at"),
                        dispatch_reason=item.get("dispatch_reason"),
                    )
                )
                continue
            if item.get("kind") == "review":
                events.append(
                    await append_and_publish(
                        board_id,
                        reaper,
                        REVIEW_LEASE_EXPIRED,
                        resource_uri(board_id, "ticket", item["ticket_id"]),
                        item["recipients"],
                        ctx,
                        ticket_id=item["ticket_id"],
                        status_from="submitted",
                        status_to="submitted",
                        reviewer_agent_id=item.get("reviewer_agent_id"),
                        reviewer_agent_name=item.get("reviewer_agent_name"),
                        reviewer_principal_id=item.get("reviewer_principal_id"),
                        review_lease_expires_at=item.get("review_lease_expires_at"),
                    )
                )
                continue
            events.append(
                await append_and_publish(
                    board_id,
                    reaper,
                    "ticket_status_changed",
                    resource_uri(board_id, "ticket", item["ticket_id"]),
                    item["recipients"],
                    ctx,
                    ticket_id=item["ticket_id"],
                    status_from=item["status_from"],
                    status_to="open",
                    last_abandoned_by=item["last_abandoned_by"],
                    last_reaped_at=item["last_abandoned_at"],
                    abandoned_count=item["abandoned_count"],
                )
            )
        return events

    def latest_seq(board_id: str) -> int:
        # read_after exposes the watermark even when cursor zero predates retention.
        return int(service.journal.read_after(board_id, 0, 1)["latest_cursor"])

    def project_ticket(board_id: str, ticket: dict[str, Any]) -> dict[str, Any]:
        projected = copy.deepcopy(ticket)
        projected.setdefault("description", "")
        projected.setdefault("scope", None)
        projected.setdefault("required_fields", [])
        projected.setdefault("forbidden", [])
        projected.setdefault("priority", "medium")
        projected.setdefault("tier", 2)
        projected.setdefault("skills_required", [])
        projected.setdefault("exclude_agents", [])
        projected.setdefault("prefer_agents", [])
        projected.setdefault("tags", [])
        projected.setdefault("related_files", [])
        projected.setdefault("target_url", "")
        projected.setdefault("assigned_to", None)
        projected.setdefault("abandoned_count", 0)
        projected.setdefault("rejection_count", 0)
        projected["payload_ref"] = resource_uri(board_id, "ticket", ticket["ticket_id"])
        return projected

    def continuation_hint(ticket: Mapping[str, Any]) -> dict[str, Any] | None:
        prior_name = ticket.get("last_claimed_by")
        prior_agent_id = ticket.get("last_claimed_by_agent_id")
        branch_and_commit = None
        for submission in reversed(ticket.get("submission_history", [])):
            if not isinstance(submission, Mapping):
                continue
            notes = submission.get("notes")
            if not isinstance(notes, str):
                continue
            match = BRANCH_AND_COMMIT_RE.search(notes)
            if match:
                branch_and_commit = match.group(1).strip()
                break
        if not prior_name and not prior_agent_id and not branch_and_commit:
            return None
        prior_holder = None
        if prior_name or prior_agent_id:
            prior_holder = {
                "agent_name": prior_name,
                "agent_id": prior_agent_id,
                "principal_id": ticket.get("last_claimed_by_principal_id"),
                "claimed_at": ticket.get("last_claimed_at"),
                "release_reason": ticket.get("last_release_reason"),
            }
        return {
            "prior_holder": prior_holder,
            "branch_and_commit": branch_and_commit,
            "abandoned_count": int(ticket.get("abandoned_count", 0) or 0),
        }

    def project_agents(document: dict[str, Any]) -> list[dict[str, Any]]:
        service.ensure_schema(document)
        lease_by_agent: dict[str, list[str]] = {}
        offer_by_agent: dict[str, dict[str, Any]] = {}
        for ticket in document["tickets"].values():
            holder = ticket.get("claimed_by_agent_id")
            expiry = ticket.get("lease_expires_at")
            if holder and expiry and ticket.get("status") in PRE_SUBMISSION_STATES:
                lease_by_agent.setdefault(holder, []).append(expiry)
            review_lease = ticket.get("review_lease")
            if isinstance(review_lease, Mapping) and ticket.get("status") == "submitted":
                review_holder = review_lease.get("reviewer_agent_id")
                review_expiry = review_lease.get("expires_at")
                if review_holder and review_expiry:
                    lease_by_agent.setdefault(str(review_holder), []).append(
                        str(review_expiry)
                    )
            for key in ("work_offer", "review_offer"):
                offer = ticket.get(key)
                if isinstance(offer, Mapping) and offer.get("agent_id"):
                    offer_by_agent[str(offer["agent_id"])] = copy.deepcopy(dict(offer))
        projected: list[dict[str, Any]] = []
        for member in document["members"].values():
            membership = document["principal_memberships"].get(member.get("principal_id"))
            if membership is None:
                continue
            leases = sorted(lease_by_agent.get(member["agent_id"], []))
            lifecycle = member.get("lifecycle_status", "active")
            current_offer = offer_by_agent.get(member["agent_id"])
            projected.append(
                {
                    **copy.deepcopy(member),
                    "capabilities": member_capabilities(member),
                    "membership_role": membership["role"],
                    "status": (
                        "handed_off" if lifecycle == "handed_off"
                        else "busy" if dispatch_enabled(document) and (leases or current_offer)
                        else "idle" if dispatch_enabled(document)
                        else "working" if leases else lifecycle
                    ),
                    "lease_expires_at": leases[0] if leases else None,
                    "current_offer": current_offer,
                }
            )
        return sorted(projected, key=lambda item: item["agent_id"])

    def clean_text(
        field: str,
        value: str | None,
        *,
        required: bool = False,
        max_length: int = 5_000,
        scrub_profile: str = "strict",
        allow_counts: dict[str, int] | None = None,
    ) -> str | None:
        if value is None:
            if required:
                raise ValueError(f"{field} is required")
            return None
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        value = value.strip()
        if required and not value:
            raise ValueError(f"{field} is required")
        if len(value) > max_length:
            raise ValueError(f"{field} must be at most {max_length} characters")
        if scrub_profile not in SCRUB_PROFILES:
            raise ValueError("board scrub profile is invalid")
        _, violations = scrub(value, Policy(mode="redact"))
        rejected = violations
        if scrub_profile == "internal":
            rejected = [item for item in violations if item.rule != "posix_home"]
            allowed = len(violations) - len(rejected)
            if allowed and allow_counts is not None:
                allow_counts["posix_home"] = (
                    int(allow_counts.get("posix_home", 0)) + allowed
                )
        if rejected:
            raise ScrubRejected(rejected)
        return value

    def clean_list(
        field: str,
        values: list[str] | None,
        *,
        max_items: int = 100,
        max_length: int = 500,
        scrub_profile: str = "strict",
        allow_counts: dict[str, int] | None = None,
    ) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list) or len(values) > max_items:
            raise ValueError(f"{field} must be a list of at most {max_items} strings")
        cleaned: list[str] = []
        for index, item in enumerate(values):
            text = clean_text(
                f"{field}[{index}]", item, required=True, max_length=max_length,
                scrub_profile=scrub_profile, allow_counts=allow_counts,
            )
            assert text is not None
            if text not in cleaned:
                cleaned.append(text)
        return cleaned

    def board_scrub_profile(document: dict[str, Any]) -> str:
        service.ensure_schema(document)
        return str(document["config"]["scrub_profile"])

    def board_review_policy(document: dict[str, Any]) -> str:
        service.ensure_schema(document)
        return str(document["config"]["review_policy"])

    @staticmethod
    def review_label(review_policy: str) -> str:
        if review_policy == "strict":
            return "independent-principal-review"
        if review_policy == "workflow":
            return "workflow-review"
        raise ValueError("board review policy is invalid")

    def record_scrub_allows(
        document: dict[str, Any],
        actor: dict[str, Any],
        now: float,
        allow_counts: Mapping[str, int],
    ) -> dict[str, Any] | None:
        accepted = {
            rule: int(count)
            for rule, count in allow_counts.items()
            if isinstance(count, int) and not isinstance(count, bool) and count > 0
        }
        if not accepted:
            return None
        config = document["config"]
        durable = config.setdefault("scrub_allow_counts", {})
        for rule, count in accepted.items():
            durable[rule] = int(durable.get(rule, 0)) + count
        audit = {
            "profile": board_scrub_profile(document),
            "allowed_rules": sorted(accepted),
            "allowed_match_count": sum(accepted.values()),
            "allowed_at": iso_at(now),
            "actor_agent_id": actor["agent_id"],
        }
        config["last_scrub_allow"] = copy.deepcopy(audit)
        return audit

    def compact_summary(title: str, content: str, explicit: str | None) -> str:
        source = explicit or content or title
        one_line = re.sub(r"\s+", " ", source).strip()
        if len(one_line) <= PINNED_SUMMARY_MAX_CHARS:
            return one_line
        return one_line[: PINNED_SUMMARY_MAX_CHARS - 1].rstrip() + "…"

    def member_name(document: dict[str, Any], identity_id: str | None) -> str | None:
        if not identity_id:
            return None
        member = document.get("members", {}).get(identity_id)
        return member.get("agent_name") if member else None

    def assignment_matches(member: dict[str, Any], requested: str) -> bool:
        requested_key = requested.casefold()
        candidates = {
            str(member.get("agent_id", "")).casefold(),
            str(member.get("agent_name", "")).casefold(),
            str(member.get("agent_platform", "")).casefold(),
        }
        return requested_key in candidates

    def resolve_assignment(
        document: dict[str, Any], requested: str | None
    ) -> tuple[str | None, str | None]:
        if not requested:
            return None, None
        exact = document["members"].get(requested)
        if exact is not None:
            return exact["agent_id"], "agent_id"
        requested_key = requested.casefold()
        name_matches = [
            item for item in document["members"].values()
            if str(item.get("agent_name", "")).casefold() == requested_key
        ]
        if len(name_matches) > 1:
            raise ValueError("assigned_to is ambiguous; use an exact agent_id")
        if name_matches:
            return name_matches[0]["agent_id"], "agent_name"
        platform_matches = [
            item for item in document["members"].values()
            if str(item.get("agent_platform", "")).casefold() == requested_key
        ]
        if platform_matches:
            # A platform assignment is intentionally a queue for any matching
            # identity, not an unstable binding to the first joined member.
            return None, "agent_platform"
        return None, "unresolved"

    def ticket_recipients(document: dict[str, Any], actor: dict[str, Any]) -> list[str]:
        return service.admitted_agent_ids(document, actor["agent_id"])

    def selected_ticket_recipients(
        document: dict[str, Any],
        actor: dict[str, Any],
        candidate_ids: Iterable[str | None],
    ) -> list[str]:
        admitted = set(service.admitted_agent_ids(document, actor["agent_id"]))
        return sorted(
            candidate
            for candidate in set(candidate_ids)
            if isinstance(candidate, str) and candidate in admitted
        )

    def ticket_creation_recipients(
        document: dict[str, Any], actor: dict[str, Any], ticket: dict[str, Any]
    ) -> list[str]:
        assigned_id = ticket.get("assigned_to_agent_id")
        if isinstance(assigned_id, str):
            return selected_ticket_recipients(document, actor, [assigned_id])
        assigned_to = ticket.get("assigned_to")
        assignment_kind = ticket.get("assigned_to_kind")
        if isinstance(assigned_to, str) and assignment_kind in {
            "agent_name",
            "agent_platform",
            "unresolved",
        }:
            field = (
                "agent_platform"
                if assignment_kind == "agent_platform"
                else "agent_name"
            )
            requested = assigned_to.casefold()
            matches = [
                member.get("agent_id")
                for member in document.get("members", {}).values()
                if str(member.get(field, "")).casefold() == requested
            ]
            return selected_ticket_recipients(document, actor, matches)
        return ticket_recipients(document, actor)

    def memory_recipients(
        document: dict[str, Any], actor: dict[str, Any], scope: str
    ) -> list[str]:
        if scope == "project":
            return ticket_recipients(document, actor)
        service.resolve_board_context(document, actor["principal_id"])
        return [
            item["agent_id"]
            for item in document["members"].values()
            if item.get("principal_id") == actor.get("principal_id")
            and item.get("agent_id") != actor.get("agent_id")
        ]

    def visible_memories(
        document: dict[str, Any], principal: Principal
    ) -> list[dict[str, Any]]:
        return [
            entry
            for entry in document.get("memories", [])
            if _memory_is_visible(entry, principal.principal_id)
        ]

    def memory_identifier(entry: dict[str, Any]) -> str:
        existing = entry.get("memory_id", entry.get("id"))
        if existing:
            return str(existing)
        canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
        return "MEM-LEGACY-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

    def project_memory(entry: dict[str, Any]) -> dict[str, Any]:
        projected = copy.deepcopy(entry)
        if not projected.get("memory_id"):
            projected["memory_id"] = memory_identifier(entry)
        if not projected.get("scope"):
            projected["scope"] = "project"
        projected.setdefault(
            "author_agent_name", projected.get("agent_name", projected.get("author_agent_id"))
        )
        projected.setdefault("memory_type", "context")
        projected.setdefault("tags", [])
        projected.setdefault("related_files", [])
        projected.setdefault("related_tickets", [])
        projected.setdefault("priority", 0)
        projected.setdefault("pinned", False)
        projected.setdefault("created_at", None)
        projected.setdefault("created_at_epoch", projected.get("timestamp", 0.0))
        return projected

    def briefing_ticket_payload(ticket: dict[str, Any]) -> dict[str, Any]:
        """Return only the fields needed to identify and rank active work."""
        title = str(ticket.get("title", ""))
        if len(title) > 120:
            title = title[:119].rstrip() + "…"
        return {
            "ticket_id": ticket["ticket_id"],
            "title": title,
            "status": ticket.get("status"),
            "claimed_by": ticket.get("claimed_by"),
            "priority": ticket.get("priority", "medium"),
            "updated_at": ticket.get("updated_at"),
        }

    def briefing_memory_payload(entry: dict[str, Any]) -> dict[str, Any]:
        """Project one memory without returning legacy or arbitrary raw payloads."""
        projected = project_memory(entry)
        scalar_fields = (
            "memory_id",
            "title",
            "scope",
            "author_principal_id",
            "author_agent_id",
            "author_agent_name",
            "memory_type",
            "priority",
            "pinned",
            "pinned_summary",
            "created_at",
            "created_at_epoch",
        )
        result = {
            key: copy.deepcopy(projected[key])
            for key in scalar_fields
            if key in projected
        }
        omitted: dict[str, int] = {}

        content = projected.get("content", "")
        content = content if isinstance(content, str) else str(content)
        content_truncated = len(content) > BRIEFING_MEMORY_CONTENT_MAX_CHARS
        if content_truncated:
            content = (
                content[: BRIEFING_MEMORY_CONTENT_MAX_CHARS - 1].rstrip() + "…"
            )
        result["content"] = content
        result["content_truncated"] = content_truncated

        summary = projected.get("summary")
        if isinstance(summary, str):
            summary_truncated = len(summary) > BRIEFING_MEMORY_CONTENT_MAX_CHARS
            if summary_truncated:
                summary = (
                    summary[: BRIEFING_MEMORY_CONTENT_MAX_CHARS - 1].rstrip()
                    + "…"
                )
            result["summary"] = summary
            result["summary_truncated"] = summary_truncated

        list_limits = {
            "tags": BRIEFING_MEMORY_LIST_LIMIT,
            "related_files": BRIEFING_MEMORY_LIST_LIMIT,
            "related_tickets": BRIEFING_MEMORY_LIST_LIMIT,
            "files": BRIEFING_MEMORY_LIST_LIMIT,
            "warnings": BRIEFING_MEMORY_LIST_LIMIT,
            "next_steps": BRIEFING_HANDOFF_NEXT_STEPS_LIMIT,
        }
        for field, field_limit in list_limits.items():
            values = projected.get(field)
            if not isinstance(values, list):
                continue
            result[field] = copy.deepcopy(values[:field_limit])
            if len(values) > field_limit:
                omitted[field] = len(values) - field_limit
        result["omitted_counts"] = omitted
        result["truncated"] = bool(
            content_truncated
            or result.get("summary_truncated")
            or omitted
        )
        return result

    def memory_target(
        document: dict[str, Any], principal: Principal, memory_id: str
    ) -> dict[str, Any] | None:
        target = next(
            (
                entry for entry in document.get("memories", [])
                if memory_identifier(entry) == memory_id
            ),
            None,
        )
        if target is None:
            return None
        visible = _memory_is_visible(target, principal.principal_id)
        return target if visible else None

    def board_role_allows_review(
        document: dict[str, Any], principal: Principal
    ) -> bool:
        if "board:review" not in principal.scopes:
            return False
        role = service.resolve_board_context(document, principal.principal_id)["role"]
        return role in {"admin", "reviewer"}

    def coordinator_actor(
        document: dict[str, Any], principal: Principal, agent_name: str
    ) -> dict[str, Any]:
        require_scope(principal, COORDINATOR_SCOPE)
        service.resolve_board_context(
            document, principal.principal_id, COORDINATOR_MEMBERSHIP_ROLES
        )
        actor = service.member(document, principal, agent_name)
        if actor.get("lifecycle_status", "active") != "active":
            raise PermissionError("coordinator seat is not active")
        return actor

    def can_moderate_memory(
        document: dict[str, Any], target: dict[str, Any], principal: Principal
    ) -> bool:
        if target.get("scope") == "private":
            return target.get("author_principal_id") == principal.principal_id
        return (
            target.get("author_principal_id") == principal.principal_id
            or board_role_allows_review(document, principal)
        )

    def unpin_memory(
        target: dict[str, Any], actor: dict[str, Any], principal: Principal,
        now: float, reason: str | None, *, force_audit: bool = False,
    ) -> bool:
        changed = bool(
            target.get("pinned")
            or target.get("pinned_summary")
            or int(target.get("priority", 0) or 0) >= 3
        )
        if not changed and not force_audit:
            return False
        target["pinned"] = False
        target["priority"] = 1
        target.pop("pinned_summary", None)
        target["unpinned_by_principal_id"] = principal.principal_id
        target["unpinned_by_agent_id"] = actor["agent_id"]
        target["unpinned_at"] = iso_at(now)
        if reason:
            target["unpin_reason"] = reason
        return changed or force_audit

    def allocate_memory_id(document: dict[str, Any]) -> str:
        seq = int(document.setdefault("next_memory_seq", 1))
        existing = {
            memory_identifier(entry) for entry in document.get("memories", [])
        }
        while True:
            candidate = f"MEM-{seq:06d}"
            seq += 1
            if candidate not in existing:
                document["next_memory_seq"] = seq
                return candidate

    def allocate_ticket_id(document: dict[str, Any]) -> str:
        seq = int(document.setdefault("next_ticket_seq", 1))
        while True:
            digest = hashlib.sha256(
                f"{document['board_id']}:{seq}".encode("utf-8")
            ).hexdigest()[:12]
            candidate = f"TK-{digest}"
            seq += 1
            if candidate not in document["tickets"]:
                document["next_ticket_seq"] = seq
                return candidate

    def allocate_admission_revision(document: dict[str, Any]) -> int:
        revision = int(document.setdefault("next_admission_revision", 1))
        if revision < 1:
            raise ValueError("admission revision moved backwards")
        document["next_admission_revision"] = revision + 1
        return revision

    def create_principal_membership(
        document: dict[str, Any], principal_id: str, role: str,
        now: float, source: str, created_by_principal_id: str,
    ) -> dict[str, Any]:
        if role not in ADMISSION_ROLES:
            raise ValueError("role must be admin, member, or reviewer")
        memberships = document.setdefault("principal_memberships", {})
        if principal_id in memberships:
            raise ValueError("principal is already a board member")
        entry = {
            "principal_id": principal_id,
            "role": role,
            "source": source,
            "created_at": iso_at(now),
            "updated_at": iso_at(now),
            "created_by_principal_id": created_by_principal_id,
        }
        memberships[principal_id] = entry
        return entry

    def invite_digest(board_id: str, token: str) -> str:
        material = f"pursers-central-invite-v1\x00{board_id}\x00{token}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def generic_invite_denial() -> PermissionError:
        return PermissionError("invite is invalid, expired, or already used")

    def ensure_join_admission(
        document: dict[str, Any], principal: Principal, now: float,
        invite_token: str | None, seat_role: str,
    ) -> dict[str, Any] | None:
        service.ensure_schema(document)
        memberships = document["principal_memberships"]
        existing = memberships.get(principal.principal_id)
        if invite_token is not None:
            if existing is not None or service.is_fresh(document):
                raise generic_invite_denial()
            digest = invite_digest(document["board_id"], invite_token)
            invite = document["invites"].get(digest)
            revoked_through_revision = int(
                document.get("principal_revocations", {}).get(
                    principal.principal_id, {}
                ).get("revoked_through_revision", 0)
            )
            if (
                invite is None
                or invite.get("consumed_at") is not None
                or invite.get("revoked_at") is not None
                or float(invite.get("expires_at_epoch", 0)) <= now
                or int(invite.get("issued_revision", 0))
                <= revoked_through_revision
                or (
                    invite.get("principal_hint") is not None
                    and invite.get("principal_hint") != principal.principal_id
                )
            ):
                raise generic_invite_denial()
            role = str(invite.get("role", "member"))
            if role not in INVITE_ROLES:
                raise generic_invite_denial()
            membership = create_principal_membership(
                document,
                principal.principal_id,
                role,
                now,
                "invite",
                str(invite.get("created_by_principal_id", "unknown")),
            )
            invite["consumed_at"] = iso_at(now)
            invite["consumed_by_principal_id"] = principal.principal_id
            admission_revision = allocate_admission_revision(document)
            return {
                "kind": "board_membership_changed",
                "admission_action": "invite_redeemed",
                "target_principal_id": principal.principal_id,
                "membership_role": membership["role"],
                "invite_id": invite["invite_id"],
                "admission_revision": admission_revision,
            }
        if existing is not None:
            if (
                service.admission == "open"
                and existing.get("role") == "member"
                and seat_role == "reviewer"
            ):
                previous_role = str(existing["role"])
                existing["role"] = "reviewer"
                existing["updated_at"] = iso_at(now)
                existing["updated_by_principal_id"] = principal.principal_id
                existing["role_upgrade_source"] = "open_rejoin_scope_upgrade"
                for member in document.get("members", {}).values():
                    if member.get("principal_id") == principal.principal_id:
                        member["membership_role"] = "reviewer"
                admission_revision = allocate_admission_revision(document)
                return {
                    "kind": "board_membership_changed",
                    "admission_action": "open_rejoin_role_upgraded",
                    "target_principal_id": principal.principal_id,
                    "membership_role": "reviewer",
                    "previous_role": previous_role,
                    "admission_revision": admission_revision,
                }
            return None
        if service.is_fresh(document):
            membership = create_principal_membership(
                document,
                principal.principal_id,
                "admin",
                now,
                "board_creator",
                principal.principal_id,
            )
            if service.admission == "invite":
                admission_revision = allocate_admission_revision(document)
                return {
                    "kind": "board_membership_changed",
                    "admission_action": "board_created",
                    "target_principal_id": principal.principal_id,
                    "membership_role": membership["role"],
                    "admission_revision": admission_revision,
                }
            return None
        if service.admission == "open":
            role = "reviewer" if seat_role == "reviewer" else "member"
            create_principal_membership(
                document,
                principal.principal_id,
                role,
                now,
                "open_admission",
                principal.principal_id,
            )
            return None
        raise PermissionError("board access denied: invite required")

    def require_admin_actor(
        document: dict[str, Any], principal: Principal, agent_name: str
    ) -> dict[str, Any]:
        actor = service.member(document, principal, agent_name)
        service.resolve_board_context(
            document, principal.principal_id, {"admin"}
        )
        return actor

    def revoke_unspent_invites(
        document: dict[str, Any], issuer_principal_id: str, now: float, reason: str
    ) -> int:
        revoked = 0
        for invite in document.get("invites", {}).values():
            if (
                invite.get("created_by_principal_id") == issuer_principal_id
                and invite.get("consumed_at") is None
                and invite.get("revoked_at") is None
            ):
                invite["revoked_at"] = iso_at(now)
                invite["revoked_reason"] = reason
                revoked += 1
        return revoked

    def revoke_targeted_invites(
        document: dict[str, Any], target_principal_id: str, now: float, reason: str
    ) -> int:
        revoked = 0
        for invite in document.get("invites", {}).values():
            if (
                invite.get("principal_hint") == target_principal_id
                and invite.get("consumed_at") is None
                and invite.get("revoked_at") is None
            ):
                invite["revoked_at"] = iso_at(now)
                invite["revoked_reason"] = reason
                revoked += 1
        return revoked

    def normalized_capabilities(
        value: Mapping[str, Any] | None,
        *,
        role: str,
    ) -> dict[str, Any]:
        raw = dict(value or {})
        allowed = {
            "model", "provider", "tier_max", "skills", "can_review",
            "can_work", "host", "max_parallel", "legacy_tools",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError("unsupported capability fields: " + ", ".join(unknown))
        tier_max = raw.get("tier_max", 2)
        max_parallel = raw.get("max_parallel", 1)
        if isinstance(tier_max, bool) or not isinstance(tier_max, int) or tier_max not in {1, 2, 3}:
            raise ValueError("capabilities.tier_max must be 1, 2, or 3")
        if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or max_parallel != 1:
            raise ValueError("capabilities.max_parallel must be 1")
        skills = raw.get("skills", [])
        if not isinstance(skills, list) or any(
            not isinstance(item, str) or not item.strip() for item in skills
        ):
            raise ValueError("capabilities.skills must be a list of non-empty strings")
        role_defaults = {
            "worker": (True, False),
            "reviewer": (False, True),
            "orchestrator": (False, False),
            "coordinator": (False, False),
        }
        default_can_work, default_can_review = role_defaults.get(
            role, role_defaults["worker"]
        )
        result: dict[str, Any] = {
            "model": raw.get("model"),
            "provider": raw.get("provider"),
            "tier_max": tier_max,
            "skills": sorted(set(item.strip() for item in skills)),
            "can_review": raw.get("can_review", default_can_review),
            "can_work": raw.get("can_work", default_can_work),
            "host": raw.get("host"),
            "max_parallel": 1,
        }
        for field in ("can_review", "can_work"):
            if not isinstance(result[field], bool):
                raise ValueError(f"capabilities.{field} must be boolean")
        if "legacy_tools" in raw:
            if not isinstance(raw["legacy_tools"], bool):
                raise ValueError("capabilities.legacy_tools must be boolean")
            result["legacy_tools"] = raw["legacy_tools"]
        if role == "reviewer" and result["can_work"]:
            raise ValueError(
                "capabilities.can_work must be false for role reviewer"
            )
        if role == "worker" and result["can_review"]:
            raise ValueError(
                "capabilities.can_review must be false for role worker"
            )
        if role in {"orchestrator", "coordinator"} and (
            result["can_work"] or result["can_review"]
        ):
            raise ValueError(
                f"capabilities.can_work and capabilities.can_review must be false "
                f"for role {role}"
            )
        for field in ("model", "provider", "host"):
            item = result[field]
            if item is not None and (not isinstance(item, str) or not item.strip()):
                raise ValueError(f"capabilities.{field} must be a non-empty string")
            if isinstance(item, str):
                result[field] = item.strip()
        return result

    def member_capabilities(member: Mapping[str, Any]) -> dict[str, Any]:
        raw = member.get("capabilities")
        return normalized_capabilities(
            raw if isinstance(raw, Mapping) else None,
            role=str(member.get("role") or "worker"),
        )

    def validate_seat_role(principal: Principal, role: str) -> str:
        if role not in SEAT_ROLES:
            raise ValueError(
                "role must be worker, reviewer, orchestrator, or coordinator"
            )
        required_scope = {
            "worker": "board:write",
            "reviewer": "board:review",
            "orchestrator": "board:coordinate",
            "coordinator": "board:coordinate",
        }[role]
        if required_scope is not None:
            require_scope(principal, required_scope)
        return role

    def join_member(
        document: dict[str, Any], principal: Principal, agent_name: str,
        now: float, claim_ttl_s: int | None,
        agent_platform: str | None, task_focus: str | None,
        role: str,
        capabilities: Mapping[str, Any] | None = None,
        *, allow_workflow_side_effects: bool = True,
    ) -> dict[str, Any]:
        membership = service.resolve_board_context(document, principal.principal_id)
        configured_ttl = claim_ttl(document)
        if claim_ttl_s is not None:
            if document["members"] and claim_ttl_s != configured_ttl:
                raise ValueError(
                    "claim_ttl_s changes require board_claim_ttl_set after the first board member joins"
                )
            document["config"]["claim_ttl_s"] = claim_ttl_s
            configured_ttl = claim_ttl_s
        released = reap_expired(document, now) if allow_workflow_side_effects else []
        identity_id = agent_id(document["board_id"], principal.principal_id, agent_name)
        existing = document["members"].get(identity_id)
        rejoined = existing is not None
        previous_role = existing.get("role") if existing is not None else None
        member = existing or {
            "agent_id": identity_id,
            "agent_name": agent_name,
            "principal_id": principal.principal_id,
            "joined_at": iso_at(now),
        }
        member.update(
            {
                "role": role,
                "lifecycle_status": "active",
                "last_activity_at": iso_at(now),
                "last_onboarded_at": iso_at(now),
                "membership_role": membership["role"],
                "scopes": sorted(principal.scopes),
            }
        )
        if agent_platform is not None:
            member["agent_platform"] = agent_platform
        if task_focus is not None:
            member["task_focus"] = task_focus
        if capabilities is not None:
            member["capabilities"] = normalized_capabilities(
                capabilities, role=role
            )
            member["capabilities_explicit"] = True
        else:
            previous = member.get("capabilities")
            preserved = dict(previous) if isinstance(previous, Mapping) else {}
            if previous_role != role:
                preserved.pop("can_work", None)
                preserved.pop("can_review", None)
            elif role == "worker":
                preserved["can_review"] = False
            elif role == "reviewer":
                preserved["can_work"] = False
            else:
                preserved["can_work"] = False
                preserved["can_review"] = False
            member["capabilities"] = normalized_capabilities(
                preserved, role=role
            )
            member["capabilities_explicit"] = bool(
                member.get("capabilities_explicit", False)
            )
        if os.environ.get("PURSERS_LEGACY_TOOLS") == "1":
            member["capabilities"]["legacy_tools"] = True
        document["members"][identity_id] = member
        if allow_workflow_side_effects and dispatch_enabled(document):
            released.extend(redispatch_queue(document, now))
        renewed: list[str] = []
        renewed_leases: list[dict[str, Any]] = []
        for ticket in (
            document["tickets"].values() if allow_workflow_side_effects else ()
        ):
            if (
                ticket.get("status") in PRE_SUBMISSION_STATES
                and ticket.get("claimed_by_agent_id") == identity_id
            ):
                renew_claim(ticket, now, configured_ttl)
                renewed.append(ticket["ticket_id"])
                renewed_leases.append(
                    {
                        "ticket_id": ticket["ticket_id"],
                        "lease_kind": "work",
                        "ttl_s": ticket["ttl_s"],
                        "lease_expires_at": ticket["lease_expires_at"],
                    }
                )
            review_lease = ticket.get("review_lease")
            if (
                ticket.get("status") == "submitted"
                and isinstance(review_lease, dict)
                and review_lease.get("reviewer_agent_id") == identity_id
            ):
                renew_review_lease(review_lease, now, configured_ttl)
                ticket["updated_at"] = iso_at(now)
                renewed.append(ticket["ticket_id"])
                renewed_leases.append(
                    {
                        "ticket_id": ticket["ticket_id"],
                        "lease_kind": "review",
                        "ttl_s": review_lease["ttl_s"],
                        "lease_expires_at": review_lease["expires_at"],
                    }
                )
        return {
            "actor": member,
            "rejoined": rejoined,
            "released": released,
            "renewed": renewed,
            "renewed_leases": renewed_leases,
            "claim_ttl_s": configured_ttl,
        }

    def snapshot_payload(document: dict[str, Any]) -> dict[str, Any]:
        tickets = [
            project_ticket(document["board_id"], ticket)
            for ticket in sorted(
                document["tickets"].values(), key=lambda item: item["ticket_id"]
            )
        ]
        return {
            "board": {
                "board_id": document["board_id"],
                "claim_ttl_s": claim_ttl(document),
                "scrub_profile": board_scrub_profile(document),
                "review_policy": board_review_policy(document),
                "dispatch_policy": dispatch_policy(document),
                "scrub_allow_counts": copy.deepcopy(
                    document["config"].get("scrub_allow_counts", {})
                ),
                "member_count": len(document["members"]),
                "principal_member_count": len(document["principal_memberships"]),
                "ticket_count": len(tickets),
            },
            "agents": project_agents(document),
            "tickets": tickets,
            "state": copy.deepcopy(document.get("state", {})),
        }

    def validate_snapshot_bounds(limit: int, max_bytes: int) -> None:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 0 <= limit <= MAX_SNAPSHOT_LIMIT
        ):
            raise ValueError(f"limit must be between 0 and {MAX_SNAPSHOT_LIMIT}")
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not MIN_SNAPSHOT_MAX_BYTES <= max_bytes <= MAX_SNAPSHOT_MAX_BYTES
        ):
            raise ValueError(
                f"max_bytes must be between {MIN_SNAPSHOT_MAX_BYTES} "
                f"and {MAX_SNAPSHOT_MAX_BYTES}"
            )

    def bounded_snapshot_payload(
        document: dict[str, Any],
        *,
        limit: int,
        max_bytes: int,
        watermark: int,
        snapshot_at: str,
    ) -> dict[str, Any]:
        """Build a deterministic snapshot with explicit collection and byte bounds."""
        snapshot = snapshot_payload(document)
        scrub_items = sorted(snapshot["board"]["scrub_allow_counts"].items())
        collections: dict[str, Any] = {
            "agents": snapshot["agents"][:limit],
            "tickets": snapshot["tickets"][:limit],
            "state": dict(sorted(snapshot["state"].items())[:limit]),
            "scrub_allow_counts": dict(scrub_items[:limit]),
        }
        totals = {
            "agents": len(snapshot["agents"]),
            "tickets": len(snapshot["tickets"]),
            "state": len(snapshot["state"]),
            "scrub_allow_counts": len(scrub_items),
        }
        board = copy.deepcopy(snapshot["board"])
        board["scrub_allow_counts"] = collections["scrub_allow_counts"]
        result = {
            "ok": True,
            "board": board,
            "agents": collections["agents"],
            "tickets": collections["tickets"],
            "state": collections["state"],
            "latest_seq": watermark,
            "snapshot_at": snapshot_at,
            "memories_included": False,
            "bounds": {"limit_per_collection": limit, "max_bytes": max_bytes},
            "total_counts": totals,
            "returned_counts": {},
            "omitted_counts": {},
            "truncated": False,
        }

        def refresh_counts() -> None:
            returned = {
                "agents": len(result["agents"]),
                "tickets": len(result["tickets"]),
                "state": len(result["state"]),
                "scrub_allow_counts": len(result["board"]["scrub_allow_counts"]),
            }
            omitted = {key: totals[key] - returned[key] for key in totals}
            result["returned_counts"] = returned
            result["omitted_counts"] = omitted
            result["truncated"] = any(omitted.values())

        def serialized_size(value: Any) -> int:
            return len(
                json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )

        refresh_counts()
        while serialized_size(result) > max_bytes:
            candidates: list[tuple[int, str, Any]] = []
            for name in ("agents", "tickets"):
                for index, item in enumerate(result[name]):
                    candidates.append((serialized_size(item), name, index))
            for name, values in (
                ("state", result["state"]),
                ("scrub_allow_counts", result["board"]["scrub_allow_counts"]),
            ):
                for key, value in values.items():
                    candidates.append(
                        (serialized_size({key: value}), name, key)
                    )
            if not candidates:
                raise ValueError("max_bytes is too small for snapshot metadata")
            _, name, locator = max(
                candidates,
                key=lambda item: (item[0], item[1], str(item[2])),
            )
            if name in {"agents", "tickets"}:
                result[name].pop(locator)
            elif name == "state":
                result["state"].pop(locator)
            else:
                result["board"]["scrub_allow_counts"].pop(locator)
            refresh_counts()
        return result

    def briefing_payload(
        document: dict[str, Any], principal: Principal, token_budget: int,
        *, ticket_id: str | None = None,
    ) -> dict[str, Any]:
        if not 256 <= token_budget <= 50_000:
            raise ValueError("token_budget must be between 256 and 50000")
        memories = [project_memory(item) for item in visible_memories(document, principal)]
        project_handoffs = [
            item for item in memories
            if item.get("memory_type") == "handoff"
            and item.get("scope") == "project"
            and not item.get("retracted_by")
        ]
        project_handoffs.sort(
            key=lambda item: (float(item.get("created_at_epoch", 0)), item.get("memory_id", "")),
            reverse=True,
        )
        pinned = [
            item for item in memories
            if item.get("pinned") and item.get("memory_type") != "handoff"
        ]
        pinned.sort(
            key=lambda item: (
                int(item.get("priority", 0)),
                float(item.get("created_at_epoch", 0)),
                item.get("memory_id", ""),
            ),
            reverse=True,
        )
        tickets = [
            project_ticket(document["board_id"], item)
            for item in document["tickets"].values()
            if item.get("status") in ACTIVE_TICKET_STATES
        ]
        tickets.sort(
            key=lambda item: (
                {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(item["priority"], 9),
                item["ticket_id"],
            )
        )
        focus = None
        if ticket_id:
            focus = next((item for item in tickets if item["ticket_id"] == ticket_id), None)
            if focus is None:
                raise ValueError("ticket not found or not active")

        current_review_policy = board_review_policy(document)
        review_label_counts: dict[str, int] = {}
        for item in document["tickets"].values():
            for review in item.get("review_history", []):
                label = review.get("review_label")
                if isinstance(label, str) and label:
                    review_label_counts[label] = review_label_counts.get(label, 0) + 1
        review_policy_text = (
            "workflow (agent cross-checks are workflow-review, not "
            "independent-principal approval)"
            if current_review_policy == "workflow"
            else "strict (review requires an independent principal)"
        )
        lines = [
            f"# ON BOARD: {document['board_id']}",
            f"Review policy: {review_policy_text}",
            f"Members: {len(document['members'])} | Open tickets: {len(tickets)} | Visible memories: {len(memories)}",
        ]
        if focus:
            lines.extend(
                [
                    "\n## Ticket focus",
                    f"- `{focus['ticket_id']}` [{focus['priority']}] {focus['title']} ({focus['status']})",
                    f"  {focus.get('description', '')}",
                ]
            )
        if project_handoffs:
            handoff = project_handoffs[0]
            lines.extend(
                [
                    "\n## Latest handoff",
                    f"- `{handoff.get('memory_id')}` from `{handoff.get('author_agent_name', handoff.get('author_agent_id', '?'))}`: {handoff.get('summary') or handoff.get('title', '')}",
                ]
            )
            for step in handoff.get("next_steps", [])[:8]:
                lines.append(f"  - {step}")
        if pinned:
            lines.append("\n## Pinned digest")
            for item in pinned[:8]:
                summary = item.get("pinned_summary") or compact_summary(
                    str(item.get("title", "")), str(item.get("content", "")), None
                )
                lines.append(
                    f"- [{str(item.get('memory_type', 'context')).upper()}] `{item.get('memory_id')}` {summary}"
                )
            if len(pinned) > 8:
                lines.append(f"- … {len(pinned) - 8} more pinned memories")
        if tickets:
            lines.append("\n## Open tickets")
            for ticket in tickets[:20]:
                assignee = ticket.get("assigned_to") or "any"
                latest_review = ticket.get("review_label")
                review_suffix = f" | last review: {latest_review}" if latest_review else ""
                lines.append(
                    f"- `{ticket['ticket_id']}` [{ticket['priority']}] {ticket['title']} ({ticket['status']}) -> {assignee}{review_suffix}"
                )
            if len(tickets) > 20:
                lines.append(f"- … {len(tickets) - 20} more open tickets")
        if document.get("state"):
            lines.append("\n## Project state")
            for key, entry in sorted(document["state"].items())[:20]:
                lines.append(f"- `{key}` = {entry.get('value', '')}")

        limit = token_budget * 4
        rendered_lines: list[str] = []
        used = 0
        truncated = False
        for line in lines:
            cost = len(line) + 1
            if used + cost > limit:
                truncated = True
                remaining = max(0, limit - used - 2)
                if remaining:
                    rendered_lines.append(line[:remaining].rstrip() + "…")
                break
            rendered_lines.append(line)
            used += cost
        rendered = "\n".join(rendered_lines)
        compact_open_tickets = [
            briefing_ticket_payload(item)
            for item in tickets[:BRIEFING_OPEN_TICKET_LIMIT]
        ]
        compact_pinned = [
            briefing_memory_payload(item)
            for item in pinned[:BRIEFING_PINNED_DIGEST_LIMIT]
        ]
        compact_handoff = (
            briefing_memory_payload(project_handoffs[0])
            if project_handoffs
            else None
        )
        omitted_open_tickets = max(
            0, len(tickets) - len(compact_open_tickets)
        )
        omitted_pinned_digest = max(0, len(pinned) - len(compact_pinned))
        memory_payload_truncated = any(
            item["truncated"] for item in compact_pinned
        ) or bool(compact_handoff and compact_handoff["truncated"])
        return {
            "token_budget": token_budget,
            "payload_bounds": {
                "open_tickets": BRIEFING_OPEN_TICKET_LIMIT,
                "pinned_digest": BRIEFING_PINNED_DIGEST_LIMIT,
                "memory_content_chars": BRIEFING_MEMORY_CONTENT_MAX_CHARS,
                "memory_list_items": BRIEFING_MEMORY_LIST_LIMIT,
                "handoff_next_steps": BRIEFING_HANDOFF_NEXT_STEPS_LIMIT,
            },
            "review_policy": current_review_policy,
            "rendered": rendered,
            "estimated_tokens": max(1, (len(rendered) + 3) // 4),
            "truncated": truncated,
            "latest_handoff": compact_handoff,
            "pinned_digest": compact_pinned,
            "open_tickets": compact_open_tickets,
            "omitted_open_tickets": omitted_open_tickets,
            "payload_total_counts": {
                "open_tickets": len(tickets),
                "pinned_digest": len(pinned),
                "latest_handoff": int(bool(project_handoffs)),
            },
            "payload_returned_counts": {
                "open_tickets": len(compact_open_tickets),
                "pinned_digest": len(compact_pinned),
                "latest_handoff": int(compact_handoff is not None),
            },
            "payload_omitted_counts": {
                "open_tickets": omitted_open_tickets,
                "pinned_digest": omitted_pinned_digest,
                "latest_handoff": 0,
            },
            "payload_truncated": bool(
                omitted_open_tickets
                or omitted_pinned_digest
                or memory_payload_truncated
            ),
            "review_label_counts": dict(sorted(review_label_counts.items())),
        }

    @tool()
    async def board_join(
        board_id: str,
        agent_name: str,
        ctx: Context,
        claim_ttl_s: int | None = None,
        agent_platform: str | None = None,
        task_focus: str | None = None,
        invite_token: str | None = None,
        role: str = "worker",
        capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Join one explicit board under the verified bearer principal."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        if claim_ttl_s is not None and not MIN_CLAIM_TTL_S <= claim_ttl_s <= MAX_CLAIM_TTL_S:
            raise ValueError(
                f"claim_ttl_s must be between {MIN_CLAIM_TTL_S} and {MAX_CLAIM_TTL_S}"
            )
        principal = current_principal()
        role = validate_seat_role(principal, role)
        coordinate_only = role in {"orchestrator", "coordinator"}
        if coordinate_only and claim_ttl_s is not None:
            raise PermissionError(
                "coordinator authorization cannot change board claim policy"
            )
        safe_platform = clean_text("agent_platform", agent_platform, max_length=80)
        safe_focus = clean_text("task_focus", task_focus, max_length=500)
        if invite_token is not None and (
            not isinstance(invite_token, str)
            or not invite_token.strip()
            or len(invite_token) > 512
        ):
            raise generic_invite_denial()
        invite_token = invite_token.strip() if invite_token is not None else None

        def join(document: dict[str, Any]) -> dict[str, Any]:
            now = time.time()
            if coordinate_only:
                if invite_token is not None:
                    raise PermissionError(
                        "coordinator authorization cannot change board membership"
                    )
                service.resolve_board_context(
                    document,
                    principal.principal_id,
                    COORDINATOR_MEMBERSHIP_ROLES,
                )
                admission_change = None
            else:
                admission_change = ensure_join_admission(
                    document, principal, now, invite_token, role
                )
            joined = join_member(
                document, principal, agent_name, now, claim_ttl_s,
                safe_platform, safe_focus, role, capabilities,
                allow_workflow_side_effects=not coordinate_only,
            )
            member = joined["actor"]
            return {
                "ok": True,
                "board_id": board_id,
                "agent_id": member["agent_id"],
                "agent_name": agent_name,
                "principal_id": principal.principal_id,
                "identity_tuple": [board_id, principal.principal_id, agent_name],
                "role": member["role"],
                "membership_role": member["membership_role"],
                "lifecycle_status": member["lifecycle_status"],
                "capabilities": member_capabilities(member),
                "generation_token": document.get("generation_token"),
                "generation_revision": int(document.get("generation_revision", 0)),
                "rejoined": joined["rejoined"],
                "member_count": len(document["members"]),
                "claim_ttl_s": joined["claim_ttl_s"],
                "renewed_ticket_ids": joined["renewed"],
                "renewed_leases": joined["renewed_leases"],
                "released": joined["released"],
                "admission_change": admission_change,
                "admission_recipients": service.admitted_agent_ids(
                    document, member["agent_id"]
                ),
                "capabilities": member.get("capabilities", {}),
            }

        result = service.mutate(board_id, join, require_generation=False)
        result["release_events"] = await publish_releases(
            board_id, result.pop("released"), principal, ctx
        )
        result["admission_event"] = await publish_admission_event(
            board_id,
            {"agent_id": result["agent_id"]},
            result.pop("admission_change"),
            result.pop("admission_recipients"),
            ctx,
        )
        return result

    @tool()
    async def board_onboard(
        board_id: str,
        agent_name: str,
        ctx: Context,
        claim_ttl_s: int | None = None,
        agent_platform: str | None = None,
        task_focus: str | None = None,
        token_budget: int = 4_000,
        ticket_id: str | None = None,
        snapshot_limit: int = DEFAULT_SNAPSHOT_LIMIT,
        snapshot_max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
        role: str = "worker",
        capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Join or reactivate an identity and return a compact bounded board briefing."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        if claim_ttl_s is not None and not MIN_CLAIM_TTL_S <= claim_ttl_s <= MAX_CLAIM_TTL_S:
            raise ValueError(
                f"claim_ttl_s must be between {MIN_CLAIM_TTL_S} and {MAX_CLAIM_TTL_S}"
            )
        if not 256 <= token_budget <= 50_000:
            raise ValueError("token_budget must be between 256 and 50000")
        validate_snapshot_bounds(snapshot_limit, snapshot_max_bytes)
        if ticket_id is not None:
            ticket_id = require_id("ticket_id", ticket_id)
        principal = current_principal()
        role = validate_seat_role(principal, role)
        coordinate_only = role in {"orchestrator", "coordinator"}
        safe_platform = clean_text("agent_platform", agent_platform, max_length=80)
        safe_focus = clean_text("task_focus", task_focus, max_length=500)

        def onboard(document: dict[str, Any]) -> dict[str, Any]:
            now = time.time()
            if coordinate_only:
                service.resolve_board_context(
                    document,
                    principal.principal_id,
                    COORDINATOR_MEMBERSHIP_ROLES,
                )
                admission_change = None
            else:
                admission_change = ensure_join_admission(
                    document, principal, now, None, role
                )
            joined = join_member(
                document, principal, agent_name, now, claim_ttl_s,
                safe_platform, safe_focus, role, capabilities,
                allow_workflow_side_effects=not coordinate_only,
            )
            briefing = briefing_payload(
                document, principal, token_budget, ticket_id=ticket_id
            )
            return {
                "actor": copy.deepcopy(joined["actor"]),
                "rejoined": joined["rejoined"],
                "released": joined["released"],
                "renewed": joined["renewed"],
                "renewed_leases": joined["renewed_leases"],
                "claim_ttl_s": joined["claim_ttl_s"],
                "generation_token": document.get("generation_token"),
                "generation_revision": int(document.get("generation_revision", 0)),
                "briefing": briefing,
                "admission_change": admission_change,
                "admission_recipients": service.admitted_agent_ids(
                    document, joined["actor"]["agent_id"]
                ),
            }

        result = service.mutate(board_id, onboard, require_generation=False)
        release_events = await publish_releases(
            board_id, result["released"], principal, ctx
        )
        admission_event = await publish_admission_event(
            board_id,
            result["actor"],
            result["admission_change"],
            result["admission_recipients"],
            ctx,
        )
        briefing = result["briefing"]
        snapshot_document = service.load(board_id)
        watermark = latest_seq(board_id)
        snapshot_at = datetime.now(timezone.utc).isoformat()
        snapshot = bounded_snapshot_payload(
            snapshot_document,
            limit=snapshot_limit,
            max_bytes=snapshot_max_bytes,
            watermark=watermark,
            snapshot_at=snapshot_at,
        )
        briefing["latest_seq"] = watermark
        return {
            "ok": True,
            "board_id": board_id,
            "agent_id": result["actor"]["agent_id"],
            "agent_name": agent_name,
            "principal_id": principal.principal_id,
            "role": result["actor"]["role"],
            "membership_role": result["actor"]["membership_role"],
            "lifecycle_status": result["actor"]["lifecycle_status"],
            "capabilities": member_capabilities(result["actor"]),
            "generation_token": result["generation_token"],
            "generation_revision": result["generation_revision"],
            "rejoined": result["rejoined"],
            "claim_ttl_s": result["claim_ttl_s"],
            "renewed_ticket_ids": result["renewed"],
            "renewed_leases": result["renewed_leases"],
            "release_events": release_events,
            "admission_event": admission_event,
            "snapshot": snapshot,
            "briefing": briefing,
            "capabilities": result["actor"].get("capabilities", {}),
        }

    @tool()
    async def board_snapshot(
        board_id: str,
        limit: int = DEFAULT_SNAPSHOT_LIMIT,
        max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES,
    ) -> dict[str, Any]:
        """Return a bounded cold projection and exact journal splice watermark.

        ``limit`` caps each projected collection. ``max_bytes`` caps the UTF-8
        JSON payload; explicit counts report every omitted entry.
        """
        board_id = require_id("board_id", board_id)
        validate_snapshot_bounds(limit, max_bytes)
        principal = current_principal()
        require_scope(principal, "board:read")
        document = service.load(board_id)
        service.principal_members(document, principal.principal_id)
        watermark = latest_seq(board_id)
        return bounded_snapshot_payload(
            document,
            limit=limit,
            max_bytes=max_bytes,
            watermark=watermark,
            snapshot_at=datetime.now(timezone.utc).isoformat(),
        )

    @tool()
    async def board_list() -> dict[str, Any]:
        """List only boards containing the authenticated principal."""
        principal = current_principal()
        require_scope(principal, "board:read")
        boards: list[dict[str, Any]] = []
        for document in service.board_documents_for(principal.principal_id):
            board_id = document["board_id"]
            memberships = service.principal_members(document, principal.principal_id)
            board_membership = service.resolve_board_context(
                document, principal.principal_id
            )
            boards.append(
                {
                    "board_id": board_id,
                    "agent_ids": [item["agent_id"] for item in memberships],
                    "agent_names": [item["agent_name"] for item in memberships],
                    "roles": sorted({item["role"] for item in memberships}),
                    "membership_role": board_membership["role"],
                    "scrub_profile": board_scrub_profile(document),
                    "review_policy": board_review_policy(document),
                    "member_count": len(document["members"]),
                    "principal_member_count": len(document["principal_memberships"]),
                    "ticket_count": len(document["tickets"]),
                    "latest_seq": latest_seq(board_id),
                }
            )
        return {"ok": True, "boards": sorted(boards, key=lambda item: item["board_id"])}

    @tool()
    async def board_scrub_profile_set(
        board_id: str,
        agent_name: str,
        scrub_profile: str,
        ctx: Context,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Set the server-enforced board scrub profile as a board admin."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        if scrub_profile not in SCRUB_PROFILES:
            raise ValueError("scrub_profile must be strict or internal")
        principal = current_principal()
        require_scope(principal, "board:write")
        now = time.time()

        def set_profile(document: dict[str, Any]) -> dict[str, Any]:
            actor = require_admin_actor(document, principal, agent_name)
            previous = board_scrub_profile(document)
            changed = previous != scrub_profile
            if changed:
                document["config"]["scrub_profile"] = scrub_profile
                document["config"]["scrub_profile_updated_at"] = iso_at(now)
                document["config"]["scrub_profile_updated_by_agent_id"] = actor[
                    "agent_id"
                ]
            actor["last_activity_at"] = iso_at(now)
            return {
                "actor": copy.deepcopy(actor),
                "previous": previous,
                "current": scrub_profile,
                "changed": changed,
                "recipients": service.admitted_agent_ids(
                    document, actor["agent_id"]
                ),
                "allow_counts": copy.deepcopy(
                    document["config"].get("scrub_allow_counts", {})
                ),
            }

        result = service.mutate(board_id, set_profile)
        event = None
        if result["changed"]:
            event = await append_and_publish(
                board_id,
                result["actor"],
                "board_scrub_profile_changed",
                resource_uri(board_id, "config", "scrub-profile"),
                result["recipients"],
                ctx,
                scrub_profile_from=result["previous"],
                scrub_profile_to=result["current"],
            )
        return {
            "ok": True,
            "board_id": board_id,
            "scrub_profile": result["current"],
            "previous_scrub_profile": result["previous"],
            "changed": result["changed"],
            "scrub_allow_counts": result["allow_counts"],
            "event": event,
        }

    @tool()
    async def board_review_policy_set(
        board_id: str,
        agent_name: str,
        review_policy: str,
        ctx: Context,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Set strict authorization or workflow agent cross-check review as admin."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        if review_policy not in REVIEW_POLICIES:
            raise ValueError("review_policy must be strict or workflow")
        principal = current_principal()
        require_scope(principal, "board:write")
        now = time.time()

        def set_policy(document: dict[str, Any]) -> dict[str, Any]:
            actor = require_admin_actor(document, principal, agent_name)
            previous = board_review_policy(document)
            changed = previous != review_policy
            if changed:
                document["config"]["review_policy"] = review_policy
                document["config"]["review_policy_updated_at"] = iso_at(now)
                document["config"]["review_policy_updated_by_agent_id"] = actor[
                    "agent_id"
                ]
            actor["last_activity_at"] = iso_at(now)
            return {
                "actor": copy.deepcopy(actor),
                "previous": previous,
                "current": review_policy,
                "changed": changed,
                "recipients": service.admitted_agent_ids(
                    document, actor["agent_id"]
                ),
            }

        result = service.mutate(board_id, set_policy)
        event = None
        if result["changed"]:
            event = await append_and_publish(
                board_id,
                result["actor"],
                "board_review_policy_changed",
                resource_uri(board_id, "config", "review-policy"),
                result["recipients"],
                ctx,
                review_policy_from=result["previous"],
                review_policy_to=result["current"],
            )
        return {
            "ok": True,
            "board_id": board_id,
            "review_policy": result["current"],
            "previous_review_policy": result["previous"],
            "changed": result["changed"],
            "event": event,
        }

    @tool()
    async def agent_capabilities_set(
        board_id: str,
        agent_name: str,
        capabilities: dict[str, Any],
        ctx: Context,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Set the authenticated seat's dispatch capabilities."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        principal = current_principal()
        require_scope(principal, "board:write")
        now = time.time()

        def set_capabilities(document: dict[str, Any]) -> dict[str, Any]:
            actor, released, renewed = prepare_board_call(
                document, principal, agent_name, now
            )
            normalized = normalized_capabilities(
                capabilities, role=str(actor.get("role") or "worker")
            )
            actor["capabilities"] = normalized
            actor["capabilities_explicit"] = True
            actor["capabilities_updated_at"] = iso_at(now)
            for ticket in document["tickets"].values():
                kind = "work" if ticket.get("status") == "open" else (
                    "review" if ticket.get("status") == "submitted" else None
                )
                if kind is None:
                    continue
                key = f"{kind}_offer"
                offer = ticket.get(key)
                if isinstance(offer, Mapping) and offer.get("agent_id") == actor["agent_id"]:
                    revoked = ticket.pop(key)
                    released.append(
                        {
                            "kind": OFFER_REVOKED, "ticket_id": ticket["ticket_id"],
                            "offer_kind": kind, "offered_agent_id": actor["agent_id"],
                            "offered_agent_name": actor["agent_name"],
                            "offer_expires_at": revoked.get("expires_at"),
                            "dispatch_reason": "capabilities_changed",
                            "recipients": [actor["agent_id"]],
                        }
                    )
                dispatched = dispatch_ticket(document, ticket, now, kind)
                if dispatched is not None:
                    released.append(dispatched)
            return {
                "actor": copy.deepcopy(actor), "capabilities": normalized,
                "released": released, "renewed": renewed,
            }

        result = service.mutate(board_id, set_capabilities)
        events = await publish_releases(board_id, result["released"], principal, ctx)
        return {
            "ok": True, "board_id": board_id,
            "agent_id": result["actor"]["agent_id"],
            "capabilities": result["capabilities"],
            "dispatch_events": events,
            "implicitly_renewed": result["renewed"],
        }

    @tool()
    async def board_dispatch_policy_set(
        board_id: str,
        agent_name: str,
        ctx: Context,
        offer_ttl_s: int = DEFAULT_OFFER_TTL_S,
        second_opinion: bool = True,
        fallback_broadcast: bool = True,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Set per-seat offer expiry and fallback behavior as board admin."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        if (
            isinstance(offer_ttl_s, bool) or not isinstance(offer_ttl_s, int)
            or not MIN_OFFER_TTL_S <= offer_ttl_s <= MAX_OFFER_TTL_S
        ):
            raise ValueError(
                f"offer_ttl_s must be between {MIN_OFFER_TTL_S} and {MAX_OFFER_TTL_S}"
            )
        if not isinstance(second_opinion, bool) or not isinstance(fallback_broadcast, bool):
            raise ValueError("second_opinion and fallback_broadcast must be boolean")
        principal = current_principal()
        require_board_write_or_coordinate(principal)
        now = time.time()

        def set_policy(document: dict[str, Any]) -> dict[str, Any]:
            actor = require_admin_actor(document, principal, agent_name)
            policy = {
                "offer_ttl_s": offer_ttl_s,
                "second_opinion": second_opinion,
                "fallback_broadcast": fallback_broadcast,
            }
            previous = copy.deepcopy(dispatch_policy(document))
            document["config"]["dispatch_policy"] = policy
            actor["last_activity_at"] = iso_at(now)
            return {"previous": previous, "current": policy}

        result = service.mutate(board_id, set_policy)
        return {
            "ok": True, "board_id": board_id,
            "dispatch_policy": result["current"],
            "previous_dispatch_policy": result["previous"],
            "changed": result["current"] != result["previous"],
        }

    @tool()
    async def board_claim_ttl_set(
        board_id: str,
        agent_name: str,
        claim_ttl_s: int,
        ctx: Context,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Set the live per-board claim TTL as an admin or coordinator seat."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        if (
            isinstance(claim_ttl_s, bool)
            or not isinstance(claim_ttl_s, int)
            or not MIN_CLAIM_TTL_S <= claim_ttl_s <= MAX_CLAIM_TTL_S
        ):
            raise ValueError(
                f"claim_ttl_s must be between {MIN_CLAIM_TTL_S} and {MAX_CLAIM_TTL_S}"
            )
        principal = current_principal()
        now = time.time()

        def set_ttl(document: dict[str, Any]) -> dict[str, Any]:
            if "board:write" in principal.scopes:
                actor = require_admin_actor(document, principal, agent_name)
            else:
                actor = coordinator_actor(document, principal, agent_name)
                if actor.get("role") not in {"coordinator", "orchestrator"}:
                    raise PermissionError(
                        "claim TTL changes require an active coordinator seat"
                    )
            previous = claim_ttl(document)
            document["config"]["claim_ttl_s"] = claim_ttl_s
            actor["last_activity_at"] = iso_at(now)
            return {
                "actor": copy.deepcopy(actor),
                "recipients": service.admitted_agent_ids(
                    document, actor["agent_id"]
                ),
                "previous": previous,
                "current": claim_ttl_s,
            }

        result = service.mutate(board_id, set_ttl)
        event = None
        if result["current"] != result["previous"]:
            event = await append_and_publish(
                board_id,
                result["actor"],
                "board_claim_ttl_changed",
                f"board://{board_id}/config/claim-ttl",
                result["recipients"],
                ctx,
                claim_ttl_from=result["previous"],
                claim_ttl_to=result["current"],
            )
        return {
            "ok": True,
            "board_id": board_id,
            "claim_ttl_s": result["current"],
            "previous_claim_ttl_s": result["previous"],
            "changed": result["current"] != result["previous"],
            "event": event,
        }

    @tool()
    async def board_invite(
        board_id: str,
        agent_name: str,
        ctx: Context,
        principal_hint: str | None = None,
        role: str = "member",
        ttl_s: int = DEFAULT_INVITE_TTL_S,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Issue a short-lived single-use board invite as a board admin."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        if role not in INVITE_ROLES:
            raise ValueError("invite role must be member or reviewer")
        if not isinstance(ttl_s, int) or not MIN_INVITE_TTL_S <= ttl_s <= MAX_INVITE_TTL_S:
            raise ValueError(
                f"ttl_s must be between {MIN_INVITE_TTL_S} and {MAX_INVITE_TTL_S}"
            )
        if principal_hint is not None:
            principal_hint = require_id("principal_hint", principal_hint)
        principal = current_principal()
        require_scope(principal, "board:write")
        token = secrets.token_urlsafe(32)
        digest = invite_digest(board_id, token)
        invite_id = "IV-" + digest[:20]

        def issue(document: dict[str, Any]) -> dict[str, Any]:
            now = time.time()
            expires = now + ttl_s
            actor = require_admin_actor(document, principal, agent_name)
            if (
                principal_hint is not None
                and principal_hint in document["principal_memberships"]
            ):
                raise ValueError(
                    "principal_hint is already an active board member"
                )
            if digest in document["invites"]:
                raise RuntimeError("invite digest collision")
            admission_revision = allocate_admission_revision(document)
            document["invites"][digest] = {
                "invite_id": invite_id,
                "digest": digest,
                "role": role,
                "principal_hint": principal_hint,
                "created_at": iso_at(now),
                "created_at_epoch": now,
                "issued_revision": admission_revision,
                "created_by_agent_id": actor["agent_id"],
                "created_by_principal_id": principal.principal_id,
                "expires_at": iso_at(expires),
                "expires_at_epoch": expires,
            }
            actor["last_activity_at"] = iso_at(now)
            return {
                "actor": copy.deepcopy(actor),
                "recipients": service.admitted_agent_ids(
                    document, actor["agent_id"]
                ),
                "change": {
                    "kind": "board_invite_created",
                    "admission_action": "invite_issued",
                    "target_principal_id": principal_hint,
                    "membership_role": role,
                    "invite_id": invite_id,
                    "expires_at": iso_at(expires),
                    "admission_revision": admission_revision,
                },
                "expires_at": iso_at(expires),
                "expires_at_epoch": expires,
                "admission_revision": admission_revision,
            }

        changed = service.mutate(board_id, issue)
        event = await publish_admission_event(
            board_id,
            changed["actor"],
            changed["change"],
            changed["recipients"],
            ctx,
        )
        return {
            "ok": True,
            "board_id": board_id,
            "invite_id": invite_id,
            "invite_token": token,
            "role": role,
            "principal_hint": principal_hint,
            "expires_at": changed["expires_at"],
            "expires_at_epoch": changed["expires_at_epoch"],
            "admission_revision": changed["admission_revision"],
            "single_use": True,
            "event": event,
        }

    @tool()
    async def board_member_add(
        board_id: str,
        agent_name: str,
        principal_id: str,
        ctx: Context,
        role: str = "member",
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Provision one verified principal directly as a board admin."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        principal_id = require_id("principal_id", principal_id)
        if role not in ADMISSION_ROLES:
            raise ValueError("role must be admin, member, or reviewer")
        principal = current_principal()
        require_scope(principal, "board:write")

        def add(document: dict[str, Any]) -> dict[str, Any]:
            now = time.time()
            actor = require_admin_actor(document, principal, agent_name)
            membership = create_principal_membership(
                document,
                principal_id,
                role,
                now,
                "admin_provisioned",
                principal.principal_id,
            )
            admission_revision = allocate_admission_revision(document)
            actor["last_activity_at"] = iso_at(now)
            return {
                "actor": copy.deepcopy(actor),
                "membership": copy.deepcopy(membership),
                "recipients": service.admitted_agent_ids(
                    document, actor["agent_id"]
                ),
                "change": {
                    "kind": "board_membership_changed",
                    "admission_action": "member_added",
                    "target_principal_id": principal_id,
                    "membership_role": role,
                    "admission_revision": admission_revision,
                },
            }

        changed = service.mutate(board_id, add)
        event = await publish_admission_event(
            board_id,
            changed["actor"],
            changed["change"],
            changed["recipients"],
            ctx,
        )
        return {
            "ok": True,
            "board_id": board_id,
            "membership": changed["membership"],
            "principal_member_count": len(service.load(board_id)["principal_memberships"]),
            "event": event,
        }

    @tool()
    async def board_member_remove(
        board_id: str,
        agent_name: str,
        principal_id: str,
        ctx: Context,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Remove one principal board-wide while preserving durable domain data."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        principal_id = require_id("principal_id", principal_id)
        principal = current_principal()
        require_scope(principal, "board:write")

        def remove(document: dict[str, Any]) -> dict[str, Any]:
            now = time.time()
            actor = copy.deepcopy(require_admin_actor(document, principal, agent_name))
            memberships = document["principal_memberships"]
            target = memberships.get(principal_id)
            if target is None:
                raise ValueError("principal is not a board member")
            previous_role = str(target["role"])
            admin_count = sum(
                item.get("role") == "admin" for item in memberships.values()
            )
            if previous_role == "admin" and admin_count <= 1:
                raise PermissionError("cannot remove the last board admin")
            admission_revision = allocate_admission_revision(document)
            revoked = revoke_unspent_invites(
                document, principal_id, now, "issuer removed from board"
            )
            revoked += revoke_targeted_invites(
                document, principal_id, now, "target removed from board"
            )
            del memberships[principal_id]
            document.setdefault("principal_revocations", {})[principal_id] = {
                "removed_at": iso_at(now),
                "removed_at_epoch": now,
                "revoked_through_revision": admission_revision,
                "removed_by_principal_id": principal.principal_id,
            }
            removed_agents = [
                key
                for key, item in document["members"].items()
                if item.get("principal_id") == principal_id
            ]
            for key in removed_agents:
                del document["members"][key]
            return {
                "actor": actor,
                "previous_role": previous_role,
                "removed_agent_ids": removed_agents,
                "revoked_invite_count": revoked,
                "principal_member_count": len(memberships),
                "recipients": service.admitted_agent_ids(
                    document, actor["agent_id"]
                ),
                "change": {
                    "kind": "board_membership_changed",
                    "admission_action": "member_removed",
                    "target_principal_id": principal_id,
                    "previous_role": previous_role,
                    "revoked_invite_count": revoked,
                    "admission_revision": admission_revision,
                },
            }

        changed = service.mutate(board_id, remove)
        event = await publish_admission_event(
            board_id,
            changed["actor"],
            changed["change"],
            changed["recipients"],
            ctx,
        )
        return {
            "ok": True,
            "board_id": board_id,
            "principal_id": principal_id,
            "previous_role": changed["previous_role"],
            "removed_agent_ids": changed["removed_agent_ids"],
            "revoked_invite_count": changed["revoked_invite_count"],
            "principal_member_count": changed["principal_member_count"],
            "event": event,
        }

    @tool()
    async def board_member_set_role(
        board_id: str,
        agent_name: str,
        principal_id: str,
        role: str,
        ctx: Context,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Change one principal's board role without changing OAuth capability."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        principal_id = require_id("principal_id", principal_id)
        if role not in ADMISSION_ROLES:
            raise ValueError("role must be admin, member, or reviewer")
        principal = current_principal()
        require_scope(principal, "board:write")

        def set_role(document: dict[str, Any]) -> dict[str, Any]:
            now = time.time()
            actor = copy.deepcopy(require_admin_actor(document, principal, agent_name))
            memberships = document["principal_memberships"]
            target = memberships.get(principal_id)
            if target is None:
                raise ValueError("principal is not a board member")
            previous_role = str(target["role"])
            if previous_role == role:
                return {
                    "actor": actor,
                    "membership": copy.deepcopy(target),
                    "changed": False,
                    "revoked_invite_count": 0,
                    "recipients": [],
                    "change": None,
                }
            admin_count = sum(
                item.get("role") == "admin" for item in memberships.values()
            )
            if previous_role == "admin" and role != "admin" and admin_count <= 1:
                raise PermissionError("cannot demote the last board admin")
            revoked = 0
            if previous_role == "admin" and role != "admin":
                revoked = revoke_unspent_invites(
                    document, principal_id, now, "issuer no longer a board admin"
                )
            target["role"] = role
            target["updated_at"] = iso_at(now)
            target["updated_by_principal_id"] = principal.principal_id
            admission_revision = allocate_admission_revision(document)
            for member in document["members"].values():
                if member.get("principal_id") == principal_id:
                    member["membership_role"] = role
            return {
                "actor": actor,
                "membership": copy.deepcopy(target),
                "changed": True,
                "revoked_invite_count": revoked,
                "recipients": service.admitted_agent_ids(
                    document, actor["agent_id"]
                ),
                "change": {
                    "kind": "board_membership_changed",
                    "admission_action": "member_role_changed",
                    "target_principal_id": principal_id,
                    "membership_role": role,
                    "previous_role": previous_role,
                    "revoked_invite_count": revoked,
                    "admission_revision": admission_revision,
                },
            }

        changed = service.mutate(board_id, set_role)
        event = await publish_admission_event(
            board_id,
            changed["actor"],
            changed["change"],
            changed["recipients"],
            ctx,
        )
        return {
            "ok": True,
            "board_id": board_id,
            "membership": changed["membership"],
            "changed": changed["changed"],
            "revoked_invite_count": changed["revoked_invite_count"],
            "event": event,
        }

    @tool()
    async def board_members(board_id: str) -> dict[str, Any]:
        """List principal-level memberships without exposing canonical identities."""
        board_id = require_id("board_id", board_id)
        principal = current_principal()
        require_scope(principal, "board:read")
        document = service.load(board_id)
        service.resolve_board_context(document, principal.principal_id)
        rows: list[dict[str, Any]] = []
        for principal_id, membership in sorted(
            document["principal_memberships"].items()
        ):
            sessions = sorted(
                (
                    item
                    for item in document["members"].values()
                    if item.get("principal_id") == principal_id
                ),
                key=lambda item: item["agent_id"],
            )
            rows.append(
                {
                    "principal_id": principal_id,
                    "role": membership["role"],
                    "source": membership.get("source"),
                    "created_at": membership.get("created_at"),
                    "agent_ids": [item["agent_id"] for item in sessions],
                    "agent_names": [item["agent_name"] for item in sessions],
                    "agent_platforms": sorted(
                        {
                            str(item["agent_platform"])
                            for item in sessions
                            if item.get("agent_platform")
                        }
                    ),
                }
            )
        return {
            "ok": True,
            "board_id": board_id,
            "members": rows,
            "principal_member_count": len(rows),
        }

    @tool()
    async def ticket_get(board_id: str, ticket_id: str) -> dict[str, Any]:
        """Refetch one full authorized ticket after a resource-updated cue."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        principal = current_principal()
        require_scope(principal, "board:read")
        document = service.load(board_id)
        service.principal_members(document, principal.principal_id)
        ticket = document["tickets"].get(ticket_id)
        if ticket is None:
            raise ValueError("ticket not found")
        return {
            "ok": True,
            "ticket": project_ticket(board_id, ticket),
            "latest_seq": latest_seq(board_id),
        }

    @tool()
    async def ticket_create(
        board_id: str,
        agent_name: str,
        title: str,
        ctx: Context,
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
        coordinator_op_key: str | None = None,
        expected_generation: str | None = None,
        tier: int = 2,
        skills_required: list[str] | None = None,
        exclude_agents: list[str] | None = None,
        prefer_agents: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a ticket; omitted IDs are generated inside the board transaction.

        If ``ticket_id`` is omitted, ``description``, ``target_url``, ``scope``,
        and ``required_fields`` are all required. Supplying ``ticket_id``
        currently enforces none of those fields. The first path segment of
        ``target_url`` is the project slug used to route work to
        project-filtered workers.
        """
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        explicit_id = ticket_id is not None
        if ticket_id is not None:
            ticket_id = require_id("ticket_id", ticket_id)
        if priority not in TICKET_PRIORITIES:
            raise ValueError("priority must be low, medium, high, or critical")
        if isinstance(tier, bool) or tier not in {1, 2, 3}:
            raise ValueError("tier must be 1, 2, or 3")
        if scope is not None and scope not in TICKET_SCOPES:
            raise ValueError(
                "scope must be READ-ONLY, interactive-no-send, or interactive"
            )
        if assigned_to is not None and unassigned:
            raise ValueError("assigned_to and unassigned=true are mutually exclusive")
        principal = current_principal()
        intake_only = "board:write" not in principal.scopes
        if intake_only:
            require_scope(principal, INTAKE_SCOPE)
            if coordinator_op_key is None:
                raise ValueError("board:intake ticket creation requires coordinator_op_key")
            coordinator_op_key = require_id(
                "coordinator_op_key", coordinator_op_key
            )
            if assigned_to is not None or not unassigned:
                raise PermissionError(
                    "board:intake creates only unassigned tickets"
                )
        elif coordinator_op_key is not None:
            raise PermissionError(
                "coordinator_op_key is reserved for board:intake"
            )
        now = time.time()

        def create(document: dict[str, Any]) -> dict[str, Any]:
            profile = board_scrub_profile(document)
            allow_counts: dict[str, int] = {}
            safe_title = clean_text(
                "title", title, required=True, max_length=200,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_description = clean_text(
                "description", description, max_length=5_000,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_target = clean_text(
                "target_url", target_url, max_length=500,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_assigned = clean_text(
                "assigned_to", assigned_to, max_length=100,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_required = clean_list(
                "required_fields", required_fields,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_forbidden = clean_list(
                "forbidden", forbidden,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_tags = clean_list(
                "tags", tags, max_length=100,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_files = clean_list(
                "related_files", related_files, max_length=1_000,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_skills = clean_list(
                "skills_required", skills_required, max_length=100,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_excluded = clean_list(
                "exclude_agents", exclude_agents, max_length=100,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_preferred = clean_list(
                "prefer_agents", prefer_agents, max_length=100,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            assert safe_title is not None
            if not explicit_id or intake_only:
                missing = []
                if not safe_description:
                    missing.append("description")
                if not safe_target:
                    missing.append("target_url")
                if scope is None:
                    missing.append("scope")
                if not safe_required:
                    missing.append("required_fields")
                if missing:
                    raise ValueError(
                        "generated-ID tickets require: " + ", ".join(missing)
                    )
            if intake_only:
                service.resolve_board_context(
                    document,
                    principal.principal_id,
                    COORDINATOR_MEMBERSHIP_ROLES,
                )
                actor = service.member(document, principal, agent_name)
                released, renewed = [], []
                actual_id = ticket_id or allocate_ticket_id(document)
                if actual_id in document["tickets"]:
                    raise ValueError("ticket already exists")
                cutoff = now - INTAKE_RATE_WINDOW_SECONDS
                recent = 0
                for existing in document["tickets"].values():
                    if existing.get("origin") != INTAKE_ORIGIN:
                        continue
                    created_epoch = existing.get("created_at_epoch")
                    if not isinstance(created_epoch, (int, float)) or isinstance(
                        created_epoch, bool
                    ):
                        try:
                            created_epoch = datetime.fromisoformat(
                                str(existing.get("created_at", "")).replace(
                                    "Z", "+00:00"
                                )
                            ).timestamp()
                        except (TypeError, ValueError):
                            created_epoch = now
                    if float(created_epoch) >= cutoff:
                        recent += 1
                rate_limit = int(
                    document["config"]["intake_rate_limit_per_hour"]
                )
                if recent >= rate_limit:
                    raise PermissionError(
                        "board:intake hourly ticket creation limit reached"
                    )
            else:
                actor, released, renewed = prepare_board_call(
                    document, principal, agent_name, now
                )
                actual_id = ticket_id or allocate_ticket_id(document)
                if actual_id in document["tickets"]:
                    raise ValueError("ticket already exists")
            requested_assignment = safe_assigned
            if explicit_id and safe_assigned is None and not unassigned:
                requested_assignment = actor["agent_name"]
                assigned_identity = actor["agent_id"]
                assignment_kind = "agent_name"
            else:
                assigned_identity, assignment_kind = resolve_assignment(
                    document, requested_assignment
                )
            ticket = {
                "ticket_id": actual_id,
                "title": safe_title,
                "description": safe_description or "",
                "scope": scope,
                "required_fields": safe_required,
                "forbidden": safe_forbidden,
                "priority": priority,
                "tier": tier,
                "skills_required": sorted(set(safe_skills)),
                "exclude_agents": sorted(set(safe_excluded)),
                "prefer_agents": sorted(set(safe_preferred)),
                "tags": safe_tags,
                "related_files": safe_files,
                "target_url": safe_target or "",
                "status": "open",
                "created_by_agent_id": actor["agent_id"],
                "created_by_principal_id": principal.principal_id,
                "created_by": actor["agent_name"],
                "assigned_to": requested_assignment,
                "assigned_to_agent_id": assigned_identity,
                "assigned_to_kind": assignment_kind,
                "server_generated_id": not explicit_id,
                "created_at": iso_at(now),
                "updated_at": iso_at(now),
            }
            if intake_only:
                ticket["created_at_epoch"] = now
                ticket["origin"] = INTAKE_ORIGIN
                ticket["coordinator_op_key"] = coordinator_op_key
            document["tickets"][actual_id] = ticket
            dispatch_event = dispatch_ticket(document, ticket, now, "work")
            scrub_audit = record_scrub_allows(
                document, actor, now, allow_counts
            )
            recipients = (
                selected_ticket_recipients(document, actor, [actor["agent_id"]])
                if dispatch_enabled(document)
                else ticket_creation_recipients(document, actor, ticket)
            )
            return {
                "actor": actor,
                "ticket": copy.deepcopy(ticket),
                "recipients": recipients,
                "count": len(document["tickets"]),
                "released": released,
                "renewed": renewed,
                "scrub_audit": scrub_audit,
                "dispatch_event": dispatch_event,
            }

        changed = service.mutate(board_id, create)
        actual_id = changed["ticket"]["ticket_id"]
        release_events = await publish_releases(board_id, changed["released"], principal, ctx)
        uri = resource_uri(board_id, "ticket", actual_id)
        intake_event = (
            {
                "origin": INTAKE_ORIGIN,
                "coordinator_op_key": coordinator_op_key,
            }
            if intake_only
            else {}
        )
        event = await append_and_publish(
            board_id,
            changed["actor"],
            "ticket_created",
            uri,
            changed["recipients"],
            ctx,
            ticket_id=actual_id,
            status_from="missing",
            status_to="open",
            **intake_event,
        )
        dispatch_events = await publish_releases(
            board_id,
            [changed["dispatch_event"]] if changed["dispatch_event"] else [],
            principal,
            ctx,
        )
        return {
            "ok": True,
            "ticket": changed["ticket"],
            "ticket_count": changed["count"],
            "event": event,
            "dispatch_event": dispatch_events[0] if dispatch_events else None,
            "release_events": release_events,
            "implicitly_renewed": changed["renewed"],
            "scrub_audit": changed["scrub_audit"],
        }

    @tool()
    async def ticket_update(
        board_id: str,
        agent_name: str,
        ticket_id: str,
        ctx: Context,
        tier: int | None = None,
        skills_required: list[str] | None = None,
        exclude_agents: list[str] | None = None,
        prefer_agents: list[str] | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Update dispatch requirements on a live ticket."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        agent_name = require_id("agent_name", agent_name)
        if tier is not None and (
            isinstance(tier, bool) or not isinstance(tier, int) or tier not in {1, 2, 3}
        ):
            raise ValueError("tier must be 1, 2, or 3")
        if all(value is None for value in (tier, skills_required, exclude_agents, prefer_agents)):
            raise ValueError("at least one dispatch field is required")
        principal = current_principal()
        coordinate_only = require_board_write_or_coordinate(principal)
        now = time.time()

        def update(document: dict[str, Any]) -> dict[str, Any]:
            profile = board_scrub_profile(document)
            allow_counts: dict[str, int] = {}
            if coordinate_only:
                actor = coordinator_actor(document, principal, agent_name)
                released, renewed = [], []
            else:
                actor, released, renewed = prepare_board_call(
                    document, principal, agent_name, now
                )
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                raise ValueError("ticket not found")
            membership = service.resolve_board_context(document, principal.principal_id)
            if (
                ticket.get("created_by_principal_id") != principal.principal_id
                and membership.get("role") != "admin"
            ):
                raise PermissionError("ticket update requires creator or board admin")
            if ticket.get("status") not in ACTIVE_TICKET_STATES:
                raise ValueError(f"ticket is {ticket.get('status')}")
            if tier is not None:
                ticket["tier"] = tier
            for field, value in (
                ("skills_required", skills_required),
                ("exclude_agents", exclude_agents),
                ("prefer_agents", prefer_agents),
            ):
                if value is not None:
                    ticket[field] = sorted(set(clean_list(
                        field, value, max_length=100, scrub_profile=profile,
                        allow_counts=allow_counts,
                    )))
            kind = "work" if ticket.get("status") == "open" else (
                "review" if ticket.get("status") == "submitted" else None
            )
            if kind is not None:
                offer = ticket.pop(f"{kind}_offer", None)
                if isinstance(offer, Mapping):
                    released.append(
                        {
                            "kind": OFFER_REVOKED, "ticket_id": ticket_id,
                            "offer_kind": kind,
                            "offered_agent_id": offer.get("agent_id"),
                            "offered_agent_name": offer.get("agent_name"),
                            "offer_expires_at": offer.get("expires_at"),
                            "dispatch_reason": "ticket_updated",
                            "recipients": [offer.get("agent_id")],
                        }
                    )
                dispatched = dispatch_ticket(document, ticket, now, kind)
                if dispatched is not None:
                    released.append(dispatched)
            ticket["updated_at"] = iso_at(now)
            return {
                "ticket": copy.deepcopy(ticket), "released": released,
                "renewed": renewed,
                "scrub_audit": record_scrub_allows(document, actor, now, allow_counts),
            }

        result = service.mutate(board_id, update)
        events = await publish_releases(board_id, result["released"], principal, ctx)
        return {
            "ok": True, "ticket": project_ticket(board_id, result["ticket"]),
            "dispatch_events": events,
            "implicitly_renewed": result["renewed"],
            "scrub_audit": result["scrub_audit"],
        }

    @tool()
    async def ticket_assign(
        board_id: str,
        agent_name: str,
        ticket_id: str,
        assigned_to_agent_id: str,
        expected_status: str,
        coordinator_op_key: str,
        reason: str,
        ctx: Context,
        expected_assigned_to_agent_id: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Atomically assign one still-open ticket under narrow coordination authority."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        assigned_to_agent_id = require_id(
            "assigned_to_agent_id", assigned_to_agent_id
        )
        if expected_assigned_to_agent_id is not None:
            expected_assigned_to_agent_id = require_id(
                "expected_assigned_to_agent_id", expected_assigned_to_agent_id
            )
        if expected_status != "open":
            raise ValueError("expected_status must be open")
        principal = current_principal()
        require_scope(principal, COORDINATOR_SCOPE)
        now = time.time()

        def assign(document: dict[str, Any]) -> dict[str, Any]:
            actor = coordinator_actor(document, principal, agent_name)
            profile = board_scrub_profile(document)
            safe_key = clean_text(
                "coordinator_op_key", coordinator_op_key,
                required=True, max_length=256, scrub_profile=profile,
            )
            safe_reason = clean_text(
                "reason", reason, required=True, max_length=500,
                scrub_profile=profile,
            )
            assert safe_key is not None and safe_reason is not None
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                raise ValueError("ticket not found")
            prior = ticket.get("coordinator_assignment")
            if isinstance(prior, Mapping) and prior.get("op_key") == safe_key:
                if prior.get("assigned_to_agent_id") != assigned_to_agent_id:
                    raise ValueError("idempotency key conflicts with prior assignment")
                return {
                    "actor": copy.deepcopy(actor),
                    "ticket": copy.deepcopy(ticket),
                    "previous_assigned_to_agent_id": prior.get(
                        "previous_assigned_to_agent_id"
                    ),
                    "op_key": safe_key,
                    "reason": safe_reason,
                    "replayed": True,
                }
            current_assignee = ticket.get("assigned_to_agent_id")
            if (
                ticket.get("status") != expected_status
                or ticket.get("claimed_by_agent_id") is not None
                or current_assignee != expected_assigned_to_agent_id
            ):
                raise ValueError(
                    "assignment state precondition failed: ticket must remain open, "
                    "unclaimed, and at the expected assignee"
                )
            target = document["members"].get(assigned_to_agent_id)
            if (
                target is None
                or assigned_to_agent_id == actor["agent_id"]
                or target.get("lifecycle_status", "active") != "active"
                or target.get("membership_role") not in {"member", "admin"}
            ):
                raise ValueError("assignment target is not an active eligible seat")
            ticket["assigned_to"] = assigned_to_agent_id
            ticket["assigned_to_agent_id"] = assigned_to_agent_id
            ticket["assigned_to_kind"] = "agent_id"
            ticket["updated_at"] = iso_at(now)
            ticket["coordinator_assignment"] = {
                "op_key": safe_key,
                "reason": safe_reason,
                "assigned_to_agent_id": assigned_to_agent_id,
                "previous_assigned_to_agent_id": current_assignee,
                "assigned_at": iso_at(now),
                "assigned_by_agent_id": actor["agent_id"],
            }
            return {
                "actor": copy.deepcopy(actor),
                "ticket": copy.deepcopy(ticket),
                "previous_assigned_to_agent_id": current_assignee,
                "op_key": safe_key,
                "reason": safe_reason,
                "replayed": False,
            }

        changed = service.mutate(board_id, assign)
        if changed["replayed"]:
            return {
                "ok": True,
                "ticket": changed["ticket"],
                "event": None,
                "event_created": False,
                "idempotent_replay": True,
            }
        uri = resource_uri(board_id, "ticket", ticket_id)
        event, created = await append_once_and_publish(
            board_id,
            changed["actor"],
            "coordinator_assignment",
            uri,
            [assigned_to_agent_id],
            ctx,
            unique_fields=("coordinator_op_key",),
            ticket_id=ticket_id,
            status_from="open",
            status_to="open",
            assigned_to_agent_id=assigned_to_agent_id,
            previous_assigned_to_agent_id=changed[
                "previous_assigned_to_agent_id"
            ],
            coordinator_op_key=changed["op_key"],
            coordination_reason=changed["reason"],
        )
        return {
            "ok": True,
            "ticket": changed["ticket"],
            "event": event,
            "event_created": created,
            "idempotent_replay": changed["replayed"],
        }

    @tool()
    async def agent_nudge(
        board_id: str,
        agent_name: str,
        ticket_id: str,
        target_agent_id: str,
        coordinator_op_key: str,
        reason: str,
        expires_at: str,
        ctx: Context,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Emit one deduplicated ticket wake cue to one exact eligible seat."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        target_agent_id = require_id("target_agent_id", target_agent_id)
        try:
            parsed_expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
        if parsed_expiry.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        expiry_utc = parsed_expiry.astimezone(timezone.utc)
        current_utc = datetime.now(timezone.utc)
        if not current_utc < expiry_utc <= current_utc + timedelta(hours=1):
            raise ValueError("expires_at must be within the next hour")
        principal = current_principal()
        require_scope(principal, COORDINATOR_SCOPE)

        def nudge(document: dict[str, Any]) -> dict[str, Any]:
            actor = coordinator_actor(document, principal, agent_name)
            profile = board_scrub_profile(document)
            safe_key = clean_text(
                "coordinator_op_key", coordinator_op_key,
                required=True, max_length=256, scrub_profile=profile,
            )
            safe_reason = clean_text(
                "reason", reason, required=True, max_length=500,
                scrub_profile=profile,
            )
            assert safe_key is not None and safe_reason is not None
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                raise ValueError("ticket not found")
            raw_nudges = ticket.setdefault("coordinator_nudges", [])
            if isinstance(raw_nudges, Mapping):
                nudges = [
                    dict(item)
                    for item in raw_nudges.values()
                    if isinstance(item, Mapping)
                ]
                ticket["coordinator_nudges"] = nudges
            elif isinstance(raw_nudges, list):
                nudges = raw_nudges
            else:
                raise ValueError("ticket coordinator nudge history is invalid")
            prior = next(
                (
                    item
                    for item in nudges
                    if isinstance(item, Mapping) and item.get("op_key") == safe_key
                ),
                None,
            )
            if prior is not None:
                if prior.get("target_agent_id") != target_agent_id:
                    raise ValueError("idempotency key conflicts with prior nudge")
                return {
                    "actor": copy.deepcopy(actor),
                    "ticket": copy.deepcopy(ticket),
                    "op_key": safe_key,
                    "reason": safe_reason,
                    "replayed": True,
                }
            if ticket.get("status") != "open" or ticket.get("claimed_by_agent_id"):
                raise ValueError(
                    "nudge state precondition failed: ticket must remain open and unclaimed"
                )
            target = document["members"].get(target_agent_id)
            if (
                target is None
                or target_agent_id == actor["agent_id"]
                or target.get("lifecycle_status", "active") != "active"
                or target.get("membership_role") not in {"member", "admin"}
            ):
                raise ValueError("nudge target is not an active eligible seat")
            nudges.append({
                "op_key": safe_key,
                "reason": safe_reason,
                "target_agent_id": target_agent_id,
                "expires_at": expiry_utc.isoformat(),
                "nudged_by_agent_id": actor["agent_id"],
            })
            return {
                "actor": copy.deepcopy(actor),
                "ticket": copy.deepcopy(ticket),
                "op_key": safe_key,
                "reason": safe_reason,
                "replayed": False,
            }

        changed = service.mutate(board_id, nudge)
        if changed["replayed"]:
            return {
                "ok": True,
                "ticket": changed["ticket"],
                "event": None,
                "event_created": False,
                "idempotent_replay": True,
            }
        uri = resource_uri(board_id, "ticket", ticket_id)
        event, created = await append_once_and_publish(
            board_id,
            changed["actor"],
            "coordinator_nudge",
            uri,
            [target_agent_id],
            ctx,
            unique_fields=("coordinator_op_key",),
            ticket_id=ticket_id,
            status_from="open",
            status_to="open",
            target_agent_id=target_agent_id,
            coordinator_op_key=changed["op_key"],
            coordination_reason=changed["reason"],
            expires_at=expiry_utc.isoformat(),
        )
        return {
            "ok": True,
            "ticket": changed["ticket"],
            "event": event,
            "event_created": created,
            "idempotent_replay": changed["replayed"],
        }

    @tool()
    async def ticket_claim(
        board_id: str,
        agent_name: str,
        ticket_id: str,
        ctx: Context,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Claim only a ticket assigned to the authenticated agent identity."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        principal = current_principal()
        require_scope(principal, "board:write")
        now = time.time()

        def claim(document: dict[str, Any]) -> dict[str, Any]:
            actor, released, renewed = prepare_board_call(document, principal, agent_name, now)
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                raise ValueError("ticket not found")
            continuation = continuation_hint(ticket)
            if ticket.get("status") == "claimed":
                if (
                    ticket.get("claimed_by_agent_id") != actor["agent_id"]
                    or ticket.get("claimed_by_principal_id") != principal.principal_id
                ):
                    raise ValueError("ticket is claimed by another identity")
                ticket["status"] = "in_progress"
                ticket["updated_at"] = iso_at(now)
                renew_claim(ticket, now, claim_ttl(document))
                return {
                    "actor": actor,
                    "ticket": copy.deepcopy(ticket),
                    "recipients": ticket_recipients(document, actor),
                    "status_from": "claimed",
                    "status_to": "in_progress",
                    "released": released,
                    "renewed": renewed,
                    "continuation": continuation,
                }
            assigned_identity = ticket.get("assigned_to_agent_id")
            requested = ticket.get("assigned_to")
            assignment_kind = ticket.get("assigned_to_kind")
            coordinator_override = board_role_allows_review(document, principal)
            if assigned_identity not in {None, actor["agent_id"]} and not coordinator_override:
                raise PermissionError("ticket assigned to another authenticated identity")
            if assigned_identity is None and requested and not coordinator_override:
                requested_key = str(requested).casefold()
                name_matches = [
                    item for item in document["members"].values()
                    if str(item.get("agent_name", "")).casefold() == requested_key
                ]
                if assignment_kind != "agent_platform" and len(name_matches) > 1:
                    raise PermissionError("ticket assignee is ambiguous; creator must use an exact agent_id")
                if assignment_kind == "agent_platform":
                    matches_actor = (
                        str(actor.get("agent_platform", "")).casefold() == requested_key
                    )
                elif name_matches:
                    matches_actor = actor["agent_id"] == name_matches[0]["agent_id"]
                else:
                    # Legacy/unresolved assignments may become claimable after
                    # a matching agent or platform joins the board.
                    matches_actor = assignment_matches(actor, requested)
                if not matches_actor:
                    raise PermissionError("ticket assigned to another agent or platform")
            if ticket["status"] != "open":
                raise ValueError(f"ticket is {ticket['status']}")
            if dispatch_enabled(document):
                offer = ticket.get("work_offer")
                if not isinstance(offer, Mapping) and ticket.get("dispatch_state", {}).get("state") != "broadcast":
                    offered = dispatch_ticket(document, ticket, now, "work")
                    if offered is not None:
                        released.append(offered)
                    offer = ticket.get("work_offer")
                state = ticket.get("dispatch_state")
                broadcast = isinstance(state, Mapping) and state.get("state") == "broadcast"
                if not isinstance(offer, Mapping) and not broadcast:
                    return {
                        "error": {
                            "code": "claim_not_offered",
                            "reason": state.get("reason", "no_live_offer") if isinstance(state, Mapping) else "no_live_offer",
                            "dispatch_state": copy.deepcopy(state),
                        },
                        "released": released,
                        "renewed": renewed,
                    }
                if isinstance(offer, Mapping) and offer.get("agent_id") != actor["agent_id"]:
                    return {
                        "error": {
                            "code": "claim_not_offered",
                            "reason": "offered_to_another_agent",
                            "offered_agent_id": offer.get("agent_id"),
                            "expires_at": offer.get("expires_at"),
                        },
                        "released": released,
                        "renewed": renewed,
                    }
                if isinstance(offer, Mapping):
                    ticket.pop("work_offer", None)
                    ticket.setdefault("dispatch_history", []).append(
                        {"state": "accepted", "kind": "work", "agent_id": actor["agent_id"], "at": iso_at(now)}
                    )
                ticket["dispatch_state"] = {
                    "state": "claimed", "kind": "work",
                    "agent_id": actor["agent_id"], "at": iso_at(now),
                }
            ticket["status"] = "claimed"
            ticket["claimed_by_agent_id"] = actor["agent_id"]
            ticket["claimed_by_principal_id"] = principal.principal_id
            ticket["claimed_by"] = actor["agent_name"]
            ticket["claimed_at"] = iso_at(now)
            ticket["updated_at"] = iso_at(now)
            actor["last_work_at"] = iso_at(now)
            renew_claim(ticket, now, claim_ttl(document))
            recipients = ticket_recipients(document, actor)
            return {
                "actor": actor,
                "ticket": copy.deepcopy(ticket),
                "recipients": recipients,
                "status_from": "open",
                "status_to": "claimed",
                "released": released,
                "renewed": renewed,
                "continuation": continuation,
            }

        changed = service.mutate(board_id, claim)
        release_events = await publish_releases(board_id, changed["released"], principal, ctx)
        if "error" in changed:
            return {"ok": False, "error": changed["error"], "release_events": release_events}
        uri = resource_uri(board_id, "ticket", ticket_id)
        event = await append_and_publish(
            board_id, changed["actor"], "ticket_status_changed", uri, changed["recipients"], ctx,
            ticket_id=ticket_id,
            status_from=changed["status_from"], status_to=changed["status_to"],
        )
        return {
            "ok": True,
            "ticket": changed["ticket"],
            "lease_expires_at": changed["ticket"]["lease_expires_at"],
            "ttl_s": changed["ticket"]["ttl_s"],
            "continuation": changed["continuation"],
            "event": event,
            "release_events": release_events,
            "implicitly_renewed": changed["renewed"],
        }

    @tool()
    async def ticket_unclaim(
        board_id: str,
        agent_name: str,
        ticket_id: str,
        ctx: Context,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Release a held pre-submission ticket back to the open queue."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        principal = current_principal()
        require_scope(principal, "board:write")
        now = time.time()

        def unclaim(document: dict[str, Any]) -> dict[str, Any]:
            actor, released, renewed = prepare_board_call(
                document, principal, agent_name, now
            )
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                raise ValueError("ticket not found")
            if ticket.get("status") not in PRE_SUBMISSION_STATES:
                raise ValueError(f"ticket is {ticket['status']}")
            membership = service.resolve_board_context(
                document, principal.principal_id
            )
            is_claimer = ticket.get("claimed_by_agent_id") == actor["agent_id"]
            is_admin = membership.get("role") == "admin"
            if not is_claimer and not is_admin:
                raise PermissionError(
                    "unclaim denied: requires current claiming agent or board admin"
                )
            old_status = str(ticket["status"])
            permission = "current claiming agent" if is_claimer else "board admin"
            ticket["last_claimed_by_agent_id"] = ticket.get("claimed_by_agent_id")
            ticket["last_claimed_by_principal_id"] = ticket.get(
                "claimed_by_principal_id"
            )
            ticket["last_claimed_by"] = ticket.get("claimed_by")
            ticket["last_claimed_at"] = ticket.get("claimed_at")
            ticket["last_unclaimed_by_agent_id"] = actor["agent_id"]
            ticket["last_unclaimed_by_principal_id"] = principal.principal_id
            ticket["last_unclaimed_at"] = iso_at(now)
            ticket["last_release_reason"] = "explicit unclaim"
            ticket["status"] = "open"
            ticket["updated_at"] = iso_at(now)
            for key in (
                "claimed_by_agent_id",
                "claimed_by_principal_id",
                "claimed_by",
                "claimed_at",
                "lease_expires_at_epoch",
                "lease_expires_at",
                "lease_renewed_at",
                "ttl_s",
            ):
                ticket.pop(key, None)
            dispatch_event = dispatch_ticket(document, ticket, now, "work")
            released.extend(redispatch_queue(document, now))
            return {
                "actor": actor,
                "ticket": copy.deepcopy(ticket),
                "recipients": ticket_recipients(document, actor),
                "old_status": old_status,
                "released": released,
                "renewed": [
                    renewed_ticket_id
                    for renewed_ticket_id in renewed
                    if renewed_ticket_id != ticket_id
                ],
                "permission": permission,
                "dispatch_event": dispatch_event,
            }

        changed = service.mutate(board_id, unclaim)
        release_events = await publish_releases(
            board_id, changed["released"], principal, ctx
        )
        uri = resource_uri(board_id, "ticket", ticket_id)
        event = await append_and_publish(
            board_id,
            changed["actor"],
            "ticket_status_changed",
            uri,
            changed["recipients"],
            ctx,
            ticket_id=ticket_id,
            status_from=changed["old_status"],
            status_to="open",
        )
        dispatch_events = await publish_releases(
            board_id,
            [changed["dispatch_event"]] if changed["dispatch_event"] else [],
            principal,
            ctx,
        )
        return {
            "ok": True,
            "ticket": changed["ticket"],
            "permission": changed["permission"],
            "event": event,
            "dispatch_event": dispatch_events[0] if dispatch_events else None,
            "release_events": release_events,
            "implicitly_renewed": changed["renewed"],
        }

    @tool()
    async def lease_renew(
        board_id: str,
        ticket_id: str,
        ctx: Context,
        expected_generation: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        """Renew the authenticated holder's work or review lease."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        principal = current_principal()
        if not ({"board:write", "board:review"} & principal.scopes):
            raise PermissionError(
                "authenticated principal lacks board:write or board:review authorization"
            )
        now = time.time()

        def renew(document: dict[str, Any]) -> dict[str, Any]:
            service.resolve_board_context(document, principal.principal_id)
            released = reap_expired(document, now)
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                return {"error": "ticket not found", "released": released}
            review_lease = ticket.get("review_lease")
            if ticket.get("status") == "submitted" and isinstance(review_lease, dict):
                member = None
                if agent_name is not None:
                    member = service.member(document, principal, agent_name)
                if (
                    review_lease.get("reviewer_principal_id") == principal.principal_id
                    and (
                        member is None
                        or review_lease.get("reviewer_agent_id") == member["agent_id"]
                    )
                ):
                    renew_review_lease(review_lease, now, claim_ttl(document))
                    ticket["updated_at"] = iso_at(now)
                    return {
                        "ticket": copy.deepcopy(ticket),
                        "released": released,
                        "lease_kind": "review",
                    }
            if (
                ticket.get("status") not in PRE_SUBMISSION_STATES
                or ticket.get("claimed_by_principal_id") != principal.principal_id
            ):
                released_at = ticket.get("last_abandoned_at")
                detail = f" at {released_at}" if released_at else " or reassigned"
                return {
                    "error": f"lease is not held by this principal; lease was released{detail}",
                    "released": released,
                }
            renew_claim(ticket, now, claim_ttl(document))
            return {
                "ticket": copy.deepcopy(ticket),
                "released": released,
                "lease_kind": "work",
            }

        result = service.mutate(board_id, renew)
        release_events = await publish_releases(board_id, result["released"], principal, ctx)
        if "error" in result:
            raise PermissionError(result["error"])
        return {
            "ok": True,
            "ticket_id": ticket_id,
            "lease_kind": result["lease_kind"],
            "lease_expires_at": (
                result["ticket"]["review_lease"]["expires_at"]
                if result["lease_kind"] == "review"
                else result["ticket"]["lease_expires_at"]
            ),
            "ttl_s": (
                result["ticket"]["review_lease"]["ttl_s"]
                if result["lease_kind"] == "review"
                else result["ticket"]["ttl_s"]
            ),
            "release_events": release_events,
        }

    @tool()
    async def board_reap(
        board_id: str,
        ctx: Context,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Release expired pre-submission leases; submitted work is durable."""
        board_id = require_id("board_id", board_id)
        principal = current_principal()
        require_scope(principal, "board:write")
        now = time.time()

        def reap(document: dict[str, Any]) -> dict[str, Any]:
            service.resolve_board_context(document, principal.principal_id)
            released = reap_expired(document, now)
            renewed: list[str] = []
            ttl_s = claim_ttl(document)
            for ticket in document["tickets"].values():
                if (
                    ticket.get("status") in PRE_SUBMISSION_STATES
                    and ticket.get("claimed_by_principal_id") == principal.principal_id
                ):
                    renew_claim(ticket, now, ttl_s)
                    renewed.append(ticket["ticket_id"])
            preserved = [
                ticket["ticket_id"]
                for ticket in document["tickets"].values()
                if ticket.get("status") in {"submitted", "closed", "rejected"}
            ]
            return {
                "released": released,
                "renewed": renewed,
                "post_submission_preserved": preserved,
            }

        result = service.mutate(board_id, reap)
        events = await publish_releases(board_id, result["released"], principal, ctx)
        return {
            "ok": True,
            "released": [item["ticket_id"] for item in result["released"]],
            "release_events": events,
            "implicitly_renewed": result["renewed"],
            "post_submission_preserved": result["post_submission_preserved"],
        }

    @tool()
    async def ticket_submit(
        board_id: str,
        agent_name: str,
        ticket_id: str,
        ctx: Context,
        summary: str | None = None,
        files_changed: list[str] | None = None,
        notes: str | None = None,
        stay_active: bool = True,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Submit only work claimed by this authenticated agent identity."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        principal = current_principal()
        require_scope(principal, "board:write")
        now = time.time()

        def submit(document: dict[str, Any]) -> dict[str, Any]:
            profile = board_scrub_profile(document)
            allow_counts: dict[str, int] = {}
            safe_summary = clean_text(
                "summary", summary, required=summary is not None,
                max_length=5_000, scrub_profile=profile,
                allow_counts=allow_counts,
            )
            safe_files = clean_list(
                "files_changed", files_changed, max_length=1_000,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_notes = clean_text(
                "notes", notes, required=notes is not None, max_length=2_000,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            actor, released, renewed = prepare_board_call(document, principal, agent_name, now)
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                return {"error": "ticket not found", "released": released, "renewed": renewed}
            if ticket.get("server_generated_id") and safe_summary is None:
                raise ValueError("summary is required for generated-ID tickets")
            if ticket.get("claimed_by_agent_id") != actor["agent_id"]:
                release = ticket.get("last_abandoned_at")
                suffix = f" at {release}" if release else " or reassigned"
                return {
                    "error": f"caller is not current executor; lease was released{suffix}",
                    "released": released,
                    "renewed": renewed,
                }
            if ticket["status"] not in PRE_SUBMISSION_STATES:
                return {
                    "error": f"ticket is {ticket['status']}",
                    "released": released,
                    "renewed": renewed,
                }
            old_status = str(ticket["status"])
            submission = {
                "summary": safe_summary or "",
                "files_changed": safe_files,
                "notes": safe_notes,
                "submitted_by_agent_id": actor["agent_id"],
                "submitted_by_principal_id": principal.principal_id,
                "submitted_at": iso_at(now),
            }
            ticket.setdefault("submission_history", []).append(copy.deepcopy(submission))
            ticket.update(submission)
            ticket["status"] = "submitted"
            ticket["submitted_by_agent_id"] = actor["agent_id"]
            ticket["submitted_by_principal_id"] = principal.principal_id
            ticket["updated_at"] = iso_at(now)
            ticket["last_lease_expires_at"] = ticket.pop("lease_expires_at", None)
            for key in ("lease_expires_at_epoch", "lease_renewed_at", "ttl_s"):
                ticket.pop(key, None)
            if not stay_active:
                actor["lifecycle_status"] = "handed_off"
                actor["handed_off_at"] = iso_at(now)
            dispatch_event = dispatch_ticket(document, ticket, now, "review")
            released.extend(redispatch_queue(document, now))
            recipients = (
                selected_ticket_recipients(
                    document, actor,
                    [actor["agent_id"], ticket.get("created_by_agent_id")],
                )
                if dispatch_enabled(document)
                else ticket_recipients(document, actor)
            )
            scrub_audit = record_scrub_allows(
                document, actor, now, allow_counts
            )
            return {
                "actor": actor,
                "ticket": copy.deepcopy(ticket),
                "recipients": recipients,
                "old_status": old_status,
                "released": released,
                "renewed": renewed,
                "scrub_audit": scrub_audit,
                "dispatch_event": dispatch_event,
            }

        changed = service.mutate(board_id, submit)
        release_events = await publish_releases(board_id, changed["released"], principal, ctx)
        if "error" in changed:
            raise PermissionError(changed["error"])
        uri = resource_uri(board_id, "ticket", ticket_id)
        event = await append_and_publish(
            board_id, changed["actor"], "ticket_status_changed", uri, changed["recipients"], ctx,
            ticket_id=ticket_id, status_from=changed["old_status"], status_to="submitted",
        )
        dispatch_events = await publish_releases(
            board_id,
            [changed["dispatch_event"]] if changed["dispatch_event"] else [],
            principal,
            ctx,
        )
        return {
            "ok": True,
            "ticket": changed["ticket"],
            "event": event,
            "dispatch_event": dispatch_events[0] if dispatch_events else None,
            "release_events": release_events,
            "implicitly_renewed": changed["renewed"],
            "scrub_audit": changed["scrub_audit"],
        }

    @tool()
    async def ticket_review_claim(
        board_id: str,
        agent_name: str,
        ticket_id: str,
        ctx: Context,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve one submitted ticket for one reviewer seat."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        principal = current_principal()
        require_scope(principal, "board:review")
        now = time.time()

        def claim(document: dict[str, Any]) -> dict[str, Any]:
            actor, released, renewed = prepare_board_call(
                document, principal, agent_name, now
            )
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                raise ValueError("ticket not found")
            if ticket.get("status") != "submitted":
                raise ValueError(f"ticket is {ticket.get('status')}")
            if not board_role_allows_review(document, principal):
                raise PermissionError(
                    "reviewing agent lacks reviewer board role and board:review authorization"
                )
            if ticket.get("submitted_by_principal_id") == principal.principal_id:
                raise PermissionError(
                    "self-review denied: authenticated principal submitted this work"
                )
            submitted_by_agent_id = ticket.get("submitted_by_agent_id")
            if (
                board_review_policy(document) == "workflow"
                and submitted_by_agent_id == actor["agent_id"]
            ):
                raise PermissionError(
                    "workflow review denied: submitting and reviewing agent must differ"
                )
            existing = ticket.get("review_lease")
            live_review = review_lease_is_live(ticket, now)
            if dispatch_enabled(document) and not live_review:
                offer = ticket.get("review_offer")
                if not isinstance(offer, Mapping) and ticket.get("dispatch_state", {}).get("state") != "broadcast":
                    offered = dispatch_ticket(document, ticket, now, "review")
                    if offered is not None:
                        released.append(offered)
                    offer = ticket.get("review_offer")
                state = ticket.get("dispatch_state")
                broadcast = isinstance(state, Mapping) and state.get("state") == "broadcast"
                if not isinstance(offer, Mapping) and not broadcast:
                    return {
                        "error": {
                            "code": "review_not_offered",
                            "reason": state.get("reason", "no_live_offer") if isinstance(state, Mapping) else "no_live_offer",
                            "dispatch_state": copy.deepcopy(state),
                        },
                        "released": released,
                        "renewed": renewed,
                    }
                if isinstance(offer, Mapping) and offer.get("agent_id") != actor["agent_id"]:
                    return {
                        "error": {
                            "code": "review_not_offered",
                            "reason": "offered_to_another_agent",
                            "offered_agent_id": offer.get("agent_id"),
                            "expires_at": offer.get("expires_at"),
                        },
                        "released": released,
                        "renewed": renewed,
                    }
                if isinstance(offer, Mapping):
                    ticket.pop("review_offer", None)
                    ticket.setdefault("dispatch_history", []).append(
                        {"state": "accepted", "kind": "review", "agent_id": actor["agent_id"], "at": iso_at(now)}
                    )
                ticket["dispatch_state"] = {
                    "state": "review_claimed", "kind": "review",
                    "agent_id": actor["agent_id"], "at": iso_at(now),
                }
            if review_lease_is_live(ticket, now):
                if (
                    existing.get("reviewer_agent_id") == actor["agent_id"]
                    and existing.get("reviewer_principal_id") == principal.principal_id
                ):
                    renew_review_lease(existing, now, claim_ttl(document))
                    ticket["updated_at"] = iso_at(now)
                    return {
                        "actor": actor,
                        "ticket": copy.deepcopy(ticket),
                        "lease": copy.deepcopy(existing),
                        "released": released,
                        "renewed": renewed,
                        "created": False,
                    }
                return {
                    "error": {
                        "code": "review_already_claimed",
                        "holder_name": existing.get("reviewer_agent_name"),
                        "expires_at": existing.get("expires_at"),
                    },
                    "released": released,
                }
            lease = claim_review_lease(
                ticket, actor, principal, now, claim_ttl(document)
            )
            actor["last_work_at"] = iso_at(now)
            return {
                "actor": actor,
                "ticket": copy.deepcopy(ticket),
                "lease": copy.deepcopy(lease),
                "recipients": ticket_recipients(document, actor),
                "released": released,
                "renewed": renewed,
                "created": True,
            }

        changed = service.mutate(board_id, claim)
        release_events = await publish_releases(
            board_id, changed["released"], principal, ctx
        )
        if "error" in changed:
            return {"ok": False, "error": changed["error"], "release_events": release_events}
        event = None
        if changed["created"]:
            event = await append_and_publish(
                board_id,
                changed["actor"],
                TICKET_REVIEW_CLAIMED,
                resource_uri(board_id, "ticket", ticket_id),
                changed["recipients"],
                ctx,
                ticket_id=ticket_id,
                status_from="submitted",
                status_to="submitted",
                reviewer_agent_id=changed["lease"]["reviewer_agent_id"],
                reviewer_agent_name=changed["lease"]["reviewer_agent_name"],
                reviewer_principal_id=changed["lease"]["reviewer_principal_id"],
                review_lease_expires_at=changed["lease"]["expires_at"],
            )
        return {
            "ok": True,
            "ticket": changed["ticket"],
            "review_lease": changed["lease"],
            "event": event,
            "event_created": changed["created"],
            "release_events": release_events,
            "implicitly_renewed": changed["renewed"],
        }

    @tool()
    async def ticket_review_release(
        board_id: str,
        agent_name: str,
        ticket_id: str,
        ctx: Context,
        reason: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Release a review reservation held by the current reviewer seat."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        principal = current_principal()
        require_scope(principal, "board:review")
        now = time.time()

        def release(document: dict[str, Any]) -> dict[str, Any]:
            profile = board_scrub_profile(document)
            allow_counts: dict[str, int] = {}
            safe_reason = clean_text(
                "reason", reason, required=reason is not None, max_length=2_000,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            actor, released, renewed = prepare_board_call(
                document, principal, agent_name, now
            )
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                raise ValueError("ticket not found")
            lease = ticket.get("review_lease")
            if not review_lease_is_live(ticket, now) or not isinstance(lease, dict):
                raise PermissionError("review lease is not held by this reviewer")
            if (
                lease.get("reviewer_agent_id") != actor["agent_id"]
                or lease.get("reviewer_principal_id") != principal.principal_id
            ):
                raise PermissionError("review lease is held by another reviewer")
            released_lease = copy.deepcopy(ticket.pop("review_lease"))
            ticket["last_review_released_by_agent_id"] = actor["agent_id"]
            ticket["updated_at"] = iso_at(now)
            dispatch_event = dispatch_ticket(document, ticket, now, "review")
            released.extend(redispatch_queue(document, now))
            return {
                "actor": actor,
                "ticket": copy.deepcopy(ticket),
                "lease": released_lease,
                "reason": safe_reason,
                "recipients": ticket_recipients(document, actor),
                "released": released,
                "renewed": renewed,
                "scrub_audit": record_scrub_allows(document, actor, now, allow_counts),
                "dispatch_event": dispatch_event,
            }

        changed = service.mutate(board_id, release)
        release_events = await publish_releases(
            board_id, changed["released"], principal, ctx
        )
        event = await append_and_publish(
            board_id,
            changed["actor"],
            REVIEW_LEASE_RELEASED,
            resource_uri(board_id, "ticket", ticket_id),
            changed["recipients"],
            ctx,
            ticket_id=ticket_id,
            status_from="submitted",
            status_to="submitted",
            reviewer_agent_id=changed["lease"].get("reviewer_agent_id"),
            reviewer_agent_name=changed["lease"].get("reviewer_agent_name"),
            reviewer_principal_id=changed["lease"].get("reviewer_principal_id"),
            release_reason=changed["reason"],
        )
        dispatch_events = await publish_releases(
            board_id,
            [changed["dispatch_event"]] if changed["dispatch_event"] else [],
            principal,
            ctx,
        )
        return {
            "ok": True,
            "ticket": changed["ticket"],
            "event": event,
            "dispatch_event": dispatch_events[0] if dispatch_events else None,
            "release_events": release_events,
            "implicitly_renewed": changed["renewed"],
            "scrub_audit": changed["scrub_audit"],
        }

    @tool()
    async def ticket_review(
        board_id: str,
        agent_name: str,
        ticket_id: str,
        verdict: str,
        ctx: Context,
        review_notes: str | None = None,
        fix_instructions: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Review under the board's strict or workflow policy with board:review."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        principal = current_principal()
        require_scope(principal, "board:review")
        now = time.time()
        if verdict not in {"approve", "reject"}:
            raise ValueError("verdict must be approve or reject")
        # Compatibility: the pre-P1 explicit-ID payload had neither review field
        # and ended rejection at `rejected`. Generated P1 tickets always reopen;
        # an explicit-ID ticket opts into the P1 path by carrying review data.
        rich_review_requested = (
            review_notes is not None or fix_instructions is not None
        )

        def review(document: dict[str, Any]) -> dict[str, Any]:
            profile = board_scrub_profile(document)
            allow_counts: dict[str, int] = {}
            safe_notes = clean_text(
                "review_notes", review_notes,
                required=review_notes is not None, max_length=5_000,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            safe_fix = clean_text(
                "fix_instructions", fix_instructions,
                required=fix_instructions is not None, max_length=5_000,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            actor, released, renewed = prepare_board_call(document, principal, agent_name, now)
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                raise ValueError("ticket not found")
            if ticket["status"] != "submitted":
                raise ValueError(f"ticket is {ticket['status']}")
            policy = board_review_policy(document)
            submitted_by_agent_id = ticket.get("submitted_by_agent_id")
            submitted_by_principal_id = ticket.get("submitted_by_principal_id")
            if submitted_by_principal_id == principal.principal_id:
                raise PermissionError("self-review denied: authenticated principal submitted this work")
            if policy == "workflow":
                if not submitted_by_agent_id or not submitted_by_principal_id:
                    raise ValueError("submitted ticket is missing review provenance")
                if submitted_by_agent_id == actor["agent_id"]:
                    raise PermissionError(
                        "workflow review denied: submitting and reviewing agent must differ"
                    )
            if not board_role_allows_review(document, principal):
                if policy == "strict":
                    raise PermissionError(
                        "independent principal lacks reviewer board role and board:review authorization"
                    )
                raise PermissionError(
                    "reviewing agent lacks reviewer board role and board:review authorization"
                )
            if ticket.get("server_generated_id") and safe_notes is None:
                raise ValueError("review_notes is required for generated-ID tickets")
            held_review = ticket.get("review_lease")
            live_review = review_lease_is_live(ticket, now)
            if dispatch_enabled(document) and not live_review:
                offer = ticket.get("review_offer")
                if not isinstance(offer, Mapping) and ticket.get("dispatch_state", {}).get("state") != "broadcast":
                    offered = dispatch_ticket(document, ticket, now, "review")
                    if offered is not None:
                        released.append(offered)
                    offer = ticket.get("review_offer")
                state = ticket.get("dispatch_state")
                broadcast = isinstance(state, Mapping) and state.get("state") == "broadcast"
                if not isinstance(offer, Mapping) and not broadcast:
                    return {
                        "error": {
                            "code": "review_not_offered",
                            "reason": state.get("reason", "no_live_offer") if isinstance(state, Mapping) else "no_live_offer",
                            "dispatch_state": copy.deepcopy(state),
                        },
                        "released": released,
                        "renewed": renewed,
                    }
                if isinstance(offer, Mapping) and offer.get("agent_id") != actor["agent_id"]:
                    return {
                        "error": {
                            "code": "review_not_offered",
                            "reason": "offered_to_another_agent",
                            "offered_agent_id": offer.get("agent_id"),
                            "expires_at": offer.get("expires_at"),
                        },
                        "released": released,
                        "renewed": renewed,
                    }
                if isinstance(offer, Mapping):
                    ticket.pop("review_offer", None)
                ticket["dispatch_state"] = {
                    "state": "review_claimed", "kind": "review",
                    "agent_id": actor["agent_id"], "at": iso_at(now),
                }
            lease = ticket.get("review_lease")
            lease_claimed = False
            if review_lease_is_live(ticket, now):
                if (
                    lease.get("reviewer_agent_id") != actor["agent_id"]
                    or lease.get("reviewer_principal_id") != principal.principal_id
                ):
                    return {
                        "error": (
                            "review lease is held by "
                            f"{lease.get('reviewer_agent_name') or 'another reviewer'} "
                            f"until {lease.get('expires_at')}"
                        ),
                        "released": released,
                    }
            else:
                lease = claim_review_lease(
                    ticket, actor, principal, now, claim_ttl(document)
                )
                lease_claimed = True
            released_lease = copy.deepcopy(lease)
            retryable_rejection = verdict == "reject" and (
                bool(ticket.get("server_generated_id")) or rich_review_requested
            )
            new_status = (
                "closed" if verdict == "approve"
                else "open" if retryable_rejection
                else "rejected"
            )
            ticket["status"] = new_status
            if verdict == "reject":
                ticket["rejection_count"] = int(ticket.get("rejection_count", 0)) + 1
            ticket["reviewed_by_agent_id"] = actor["agent_id"]
            ticket["reviewed_by_principal_id"] = principal.principal_id
            ticket["reviewed_at"] = iso_at(now)
            ticket["review_notes"] = safe_notes or ""
            label = review_label(policy)
            submitted_by_agent_name = member_name(
                document, str(submitted_by_agent_id) if submitted_by_agent_id else None
            ) or ticket.get("claimed_by")
            ticket["review_policy_at_verdict"] = policy
            ticket["review_label"] = label
            ticket["review_verdict"] = verdict
            ticket["reviewed_by_agent_name"] = actor["agent_name"]
            if safe_fix is not None:
                ticket["fix_instructions"] = safe_fix
            else:
                ticket.pop("fix_instructions", None)
            review_record = {
                "verdict": verdict,
                "review_notes": safe_notes or "",
                "fix_instructions": safe_fix,
                "review_policy_at_verdict": policy,
                "review_label": label,
                "submitted_by_agent_id": submitted_by_agent_id,
                "submitted_by_agent_name": submitted_by_agent_name,
                "submitted_by_principal_id": submitted_by_principal_id,
                "reviewed_by_agent_id": actor["agent_id"],
                "reviewed_by_agent_name": actor["agent_name"],
                "reviewed_by_principal_id": principal.principal_id,
                "reviewed_at": iso_at(now),
                "status_to": new_status,
            }
            ticket.setdefault("review_history", []).append(review_record)
            ticket.pop("review_lease", None)
            if retryable_rejection:
                ticket["last_claimed_by_agent_id"] = ticket.get("claimed_by_agent_id")
                ticket["last_claimed_by_principal_id"] = ticket.get("claimed_by_principal_id")
                ticket["last_claimed_by"] = ticket.get("claimed_by")
                ticket["last_claimed_at"] = ticket.get("claimed_at")
                ticket["last_release_reason"] = "review rejected"
                for key in (
                    "claimed_by_agent_id",
                    "claimed_by_principal_id",
                    "claimed_by",
                    "claimed_at",
                    "submitted_by_agent_id",
                    "submitted_by_principal_id",
                    "submitted_at",
                ):
                    ticket.pop(key, None)
            ticket["updated_at"] = iso_at(now)
            if new_status != "open" and dispatch_enabled(document):
                ticket["dispatch_state"] = {
                    "state": new_status, "kind": "review", "at": iso_at(now)
                }
            dispatch_event = (
                dispatch_ticket(document, ticket, now, "work")
                if new_status == "open" else None
            )
            released.extend(redispatch_queue(document, now))
            recipients = selected_ticket_recipients(
                document,
                actor,
                [submitted_by_agent_id, ticket.get("created_by_agent_id")],
            )
            scrub_audit = record_scrub_allows(
                document, actor, now, allow_counts
            )
            return {
                "actor": actor,
                "ticket": copy.deepcopy(ticket),
                "recipients": recipients,
                "new_status": new_status,
                "review_record": copy.deepcopy(review_record),
                "review_lease": released_lease,
                "review_lease_claimed": lease_claimed,
                "retryable_rejection": retryable_rejection,
                "released": released,
                "renewed": renewed,
                "scrub_audit": scrub_audit,
                "dispatch_event": dispatch_event,
            }

        changed = service.mutate(board_id, review)
        release_events = await publish_releases(board_id, changed["released"], principal, ctx)
        if "error" in changed:
            if isinstance(changed["error"], Mapping):
                return {
                    "ok": False, "error": changed["error"],
                    "release_events": release_events,
                    "implicitly_renewed": changed.get("renewed", []),
                }
            raise PermissionError(changed["error"])
        uri = resource_uri(board_id, "ticket", ticket_id)
        claim_event = None
        if changed["review_lease_claimed"]:
            claim_event = await append_and_publish(
                board_id, changed["actor"], TICKET_REVIEW_CLAIMED, uri,
                changed["recipients"], ctx, ticket_id=ticket_id,
                status_from="submitted", status_to="submitted",
                reviewer_agent_id=changed["review_lease"].get("reviewer_agent_id"),
                reviewer_agent_name=changed["review_lease"].get("reviewer_agent_name"),
                reviewer_principal_id=changed["review_lease"].get("reviewer_principal_id"),
                review_lease_expires_at=changed["review_lease"].get("expires_at"),
            )
        event = await append_and_publish(
            board_id, changed["actor"], "ticket_status_changed", uri, changed["recipients"], ctx,
            ticket_id=ticket_id, status_from="submitted", status_to=changed["new_status"],
            reviewed_by=changed["actor"]["agent_id"],
            review_policy_at_verdict=changed["review_record"]["review_policy_at_verdict"],
            review_label=changed["review_record"]["review_label"],
            review_verdict=changed["review_record"]["verdict"],
            submitted_by_agent_id=changed["review_record"]["submitted_by_agent_id"],
            submitted_by_agent_name=changed["review_record"]["submitted_by_agent_name"],
            submitted_by_principal_id=changed["review_record"]["submitted_by_principal_id"],
            reviewed_by_agent_id=changed["review_record"]["reviewed_by_agent_id"],
            reviewed_by_agent_name=changed["review_record"]["reviewed_by_agent_name"],
            reviewed_by_principal_id=changed["review_record"]["reviewed_by_principal_id"],
            rejection_count=changed["ticket"].get("rejection_count", 0),
            review_notes_ref=f"{uri}#review-{len(changed['ticket'].get('review_history', []))}",
            fix_instructions_ref=(
                f"{uri}#fix-{changed['ticket'].get('rejection_count', 0)}"
                if changed["ticket"].get("fix_instructions") else None
            ),
        )
        review_release_event = await append_and_publish(
            board_id, changed["actor"], REVIEW_LEASE_RELEASED, uri,
            changed["recipients"], ctx, ticket_id=ticket_id,
            status_from="submitted", status_to=changed["new_status"],
            reviewer_agent_id=changed["review_lease"].get("reviewer_agent_id"),
            reviewer_agent_name=changed["review_lease"].get("reviewer_agent_name"),
            reviewer_principal_id=changed["review_lease"].get("reviewer_principal_id"),
            release_reason=f"verdict:{changed['review_record']['verdict']}",
        )
        dispatch_events = await publish_releases(
            board_id,
            [changed["dispatch_event"]] if changed["dispatch_event"] else [],
            principal,
            ctx,
        )
        return {
            "ok": True,
            "ticket": changed["ticket"],
            "event": event,
            "review_claim_event": claim_event,
            "review_release_event": review_release_event,
            "dispatch_event": dispatch_events[0] if dispatch_events else None,
            "release_events": release_events,
            "implicitly_renewed": changed["renewed"],
            "retryable_rejection": changed["retryable_rejection"],
            "scrub_audit": changed["scrub_audit"],
        }

    @tool()
    async def ticket_cancel(
        board_id: str,
        agent_name: str,
        ticket_id: str,
        ctx: Context,
        reason: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a live ticket as its creator, current executor, or a reviewer."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        principal = current_principal()
        require_scope(principal, "board:write")
        now = time.time()

        def cancel(document: dict[str, Any]) -> dict[str, Any]:
            profile = board_scrub_profile(document)
            allow_counts: dict[str, int] = {}
            safe_reason = clean_text(
                "reason", reason, required=reason is not None, max_length=2_000,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            actor, released, renewed = prepare_board_call(
                document, principal, agent_name, now
            )
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                raise ValueError("ticket not found")
            if ticket.get("status") in TERMINAL_TICKET_STATES:
                raise ValueError(f"ticket is already {ticket['status']}")
            basis = None
            if ticket.get("created_by_principal_id") == principal.principal_id:
                basis = "creator principal"
            elif ticket.get("claimed_by_principal_id") == principal.principal_id:
                basis = "current executor principal"
            elif board_role_allows_review(document, principal):
                basis = "board:review"
            if basis is None:
                raise PermissionError(
                    "cancel denied: requires creator, current executor, or board:review"
                )
            old_status = str(ticket["status"])
            ticket["status"] = "canceled"
            ticket["canceled_by_agent_id"] = actor["agent_id"]
            ticket["canceled_by_principal_id"] = principal.principal_id
            ticket["cancel_permission"] = basis
            ticket["canceled_at"] = iso_at(now)
            ticket["updated_at"] = iso_at(now)
            if safe_reason:
                ticket["cancel_reason"] = safe_reason
            for kind in ("work", "review"):
                offer = ticket.pop(f"{kind}_offer", None)
                if isinstance(offer, Mapping):
                    released.append(
                        {
                            "kind": OFFER_REVOKED, "ticket_id": ticket_id,
                            "offer_kind": kind,
                            "offered_agent_id": offer.get("agent_id"),
                            "offered_agent_name": offer.get("agent_name"),
                            "offer_expires_at": offer.get("expires_at"),
                            "dispatch_reason": "ticket_canceled",
                            "recipients": [offer.get("agent_id")],
                        }
                    )
            if dispatch_enabled(document):
                ticket["dispatch_state"] = {
                    "state": "canceled", "at": iso_at(now)
                }
            released.extend(redispatch_queue(document, now))
            for key in (
                "lease_expires_at_epoch", "lease_expires_at",
                "lease_renewed_at", "ttl_s",
            ):
                ticket.pop(key, None)
            scrub_audit = record_scrub_allows(
                document, actor, now, allow_counts
            )
            return {
                "actor": actor,
                "ticket": copy.deepcopy(ticket),
                "recipients": ticket_recipients(document, actor),
                "old_status": old_status,
                "released": released,
                "renewed": renewed,
                "permission": basis,
                "scrub_audit": scrub_audit,
            }

        changed = service.mutate(board_id, cancel)
        release_events = await publish_releases(
            board_id, changed["released"], principal, ctx
        )
        uri = resource_uri(board_id, "ticket", ticket_id)
        event = await append_and_publish(
            board_id, changed["actor"], "ticket_status_changed", uri,
            changed["recipients"], ctx, ticket_id=ticket_id,
            status_from=changed["old_status"], status_to="canceled",
        )
        return {
            "ok": True,
            "ticket": changed["ticket"],
            "permission": changed["permission"],
            "event": event,
            "release_events": release_events,
            "implicitly_renewed": changed["renewed"],
            "scrub_audit": changed["scrub_audit"],
        }

    @tool()
    async def ticket_terminate(
        board_id: str,
        agent_name: str,
        ticket_id: str,
        ctx: Context,
        reason: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Force a live ticket terminal as its creator or a board reviewer."""
        board_id = require_id("board_id", board_id)
        ticket_id = require_id("ticket_id", ticket_id)
        principal = current_principal()
        require_scope(principal, "board:write")
        now = time.time()

        def terminate(document: dict[str, Any]) -> dict[str, Any]:
            profile = board_scrub_profile(document)
            allow_counts: dict[str, int] = {}
            safe_reason = clean_text(
                "reason", reason, required=reason is not None, max_length=2_000,
                scrub_profile=profile, allow_counts=allow_counts,
            )
            actor, released, renewed = prepare_board_call(
                document, principal, agent_name, now
            )
            ticket = document["tickets"].get(ticket_id)
            if ticket is None:
                raise ValueError("ticket not found")
            if ticket.get("status") in TERMINAL_TICKET_STATES:
                raise ValueError(f"ticket is already {ticket['status']}")
            basis = None
            if ticket.get("created_by_principal_id") == principal.principal_id:
                basis = "creator principal"
            elif board_role_allows_review(document, principal):
                basis = "board:review"
            if basis is None:
                raise PermissionError(
                    "terminate denied: requires creator or board:review"
                )
            old_status = str(ticket["status"])
            ticket["status"] = "terminated"
            ticket["terminated_by_agent_id"] = actor["agent_id"]
            ticket["terminated_by_principal_id"] = principal.principal_id
            ticket["terminate_permission"] = basis
            ticket["terminated_at"] = iso_at(now)
            ticket["updated_at"] = iso_at(now)
            if safe_reason:
                ticket["terminate_reason"] = safe_reason
            for kind in ("work", "review"):
                offer = ticket.pop(f"{kind}_offer", None)
                if isinstance(offer, Mapping):
                    released.append(
                        {
                            "kind": OFFER_REVOKED, "ticket_id": ticket_id,
                            "offer_kind": kind,
                            "offered_agent_id": offer.get("agent_id"),
                            "offered_agent_name": offer.get("agent_name"),
                            "offer_expires_at": offer.get("expires_at"),
                            "dispatch_reason": "ticket_terminated",
                            "recipients": [offer.get("agent_id")],
                        }
                    )
            if dispatch_enabled(document):
                ticket["dispatch_state"] = {
                    "state": "terminated", "at": iso_at(now)
                }
            released.extend(redispatch_queue(document, now))
            for key in (
                "lease_expires_at_epoch", "lease_expires_at",
                "lease_renewed_at", "ttl_s",
            ):
                ticket.pop(key, None)
            scrub_audit = record_scrub_allows(
                document, actor, now, allow_counts
            )
            return {
                "actor": actor,
                "ticket": copy.deepcopy(ticket),
                "recipients": ticket_recipients(document, actor),
                "old_status": old_status,
                "released": released,
                "renewed": renewed,
                "permission": basis,
                "scrub_audit": scrub_audit,
            }

        changed = service.mutate(board_id, terminate)
        release_events = await publish_releases(
            board_id, changed["released"], principal, ctx
        )
        uri = resource_uri(board_id, "ticket", ticket_id)
        event = await append_and_publish(
            board_id, changed["actor"], "ticket_status_changed", uri,
            changed["recipients"], ctx, ticket_id=ticket_id,
            status_from=changed["old_status"], status_to="terminated",
        )
        return {
            "ok": True,
            "ticket": changed["ticket"],
            "permission": changed["permission"],
            "event": event,
            "release_events": release_events,
            "implicitly_renewed": changed["renewed"],
            "scrub_audit": changed["scrub_audit"],
        }

    @tool()
    async def ticket_list(
        board_id: str,
        status: str | None = None,
        assigned_to: str | None = None,
        include_closed: bool = False,
        limit: int = 100,
        agent_name: str | None = None,
        review_unclaimed_only: bool = False,
    ) -> dict[str, Any]:
        """List authorized tickets with server-side status and assignee filters."""
        board_id = require_id("board_id", board_id)
        if status is not None and status not in ACTIVE_TICKET_STATES | TERMINAL_TICKET_STATES:
            raise ValueError("unsupported ticket status")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        principal = current_principal()
        require_scope(principal, "board:read")
        document = service.load(board_id)
        service.principal_members(document, principal.principal_id)
        tickets = list(document["tickets"].values())
        now = time.time()
        reviewer_agent_id = (
            agent_id(board_id, principal.principal_id, agent_name)
            if agent_name is not None else None
        )
        if status is not None:
            tickets = [item for item in tickets if item.get("status") == status]
        elif not include_closed:
            tickets = [
                item for item in tickets
                if item.get("status") not in TERMINAL_TICKET_STATES
            ]
        if assigned_to:
            needle = assigned_to.casefold()
            filtered: list[dict[str, Any]] = []
            for item in tickets:
                identity_ids = {
                    str(item.get("assigned_to_agent_id") or ""),
                    str(item.get("claimed_by_agent_id") or ""),
                }
                names = {
                    str(item.get("assigned_to") or ""),
                    str(item.get("claimed_by") or ""),
                    *(member_name(document, identity) or "" for identity in identity_ids),
                }
                if any(needle in value.casefold() for value in identity_ids | names):
                    filtered.append(item)
            tickets = filtered
        if review_unclaimed_only:
            tickets = [
                item for item in tickets
                if item.get("status") == "submitted"
                and not review_lease_is_live(item, now)
            ]
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        tickets.sort(
            key=lambda item: (
                priority_order.get(item.get("priority", "medium"), 9),
                item["ticket_id"],
            )
        )
        projected = []
        for item in tickets[:limit]:
            row = project_ticket(board_id, item)
            lease = item.get("review_lease")
            if item.get("status") != "submitted":
                projected.append(row)
                continue
            if not review_lease_is_live(item, now) or not isinstance(lease, Mapping):
                row["review_state"] = "unclaimed"
            elif (
                lease.get("reviewer_principal_id") == principal.principal_id
                and (
                    reviewer_agent_id is None
                    or lease.get("reviewer_agent_id") == reviewer_agent_id
                )
            ):
                row["review_state"] = "claimed_by_me"
                row["review_claimed_by"] = lease.get("reviewer_agent_name")
                row["review_lease_expires_at"] = lease.get("expires_at")
            else:
                row["review_state"] = "claimed_by_other"
                row["review_claimed_by"] = lease.get("reviewer_agent_name")
                row["review_lease_expires_at"] = lease.get("expires_at")
            projected.append(row)
        return {
            "ok": True,
            "tickets": projected,
            "count": len(projected),
            "total_matching": len(tickets),
            "filters": {
                "status": status,
                "assigned_to": assigned_to,
                "include_closed": include_closed,
                "review_unclaimed_only": review_unclaimed_only,
            },
            "latest_seq": latest_seq(board_id),
        }

    @tool()
    async def memory_write(
        board_id: str,
        agent_name: str,
        title: str,
        content: str,
        scope: str,
        ctx: Context,
        memory_type: str = "context",
        tags: list[str] | None = None,
        priority: int = 0,
        pinned_summary: str | None = None,
        retracts: str | None = None,
        related_files: list[str] | None = None,
        related_tickets: list[str] | None = None,
        archived: bool = False,
        archive_source_id: str | None = None,
        archived_at: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Write one private/project memory; archived=true preserves oversize content."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        principal = current_principal()
        coordinate_only = require_board_write_or_coordinate(principal)
        now = time.time()
        if scope not in {"private", "project"}:
            raise ValueError("scope must be private or project")
        if memory_type not in MEMORY_TYPES:
            raise ValueError("unsupported memory_type")
        if not 0 <= priority <= 3:
            raise ValueError("priority must be between 0 and 3")
        if type(archived) is not bool:
            raise ValueError("archived must be a boolean")
        if not archived and (archive_source_id is not None or archived_at is not None):
            raise ValueError("archive provenance requires archived=true")
        if coordinate_only:
            period = "daily" if title.startswith("Coordinator daily digest ") else (
                "weekly" if title.startswith("Coordinator weekly rollup ") else None
            )
            if (
                period is None
                or scope != "project"
                or memory_type != "checkpoint"
                or set(tags or []) != {"coordinator", "digest", period}
                or priority != 0
                or pinned_summary is not None
                or retracts is not None
                or related_files
                or related_tickets
                or archived
            ):
                raise PermissionError(
                    "coordinator authorization permits only coordinator digest memories"
                )

        def write(document: dict[str, Any]) -> dict[str, Any]:
            profile = board_scrub_profile(document)
            allow_counts: dict[str, int] = {}
            violations: dict[str, list[str]] = {}
            scalar_inputs = {
                "title": (title, True, 200),
                "content": (
                    content,
                    True,
                    max(10_000, len(content)) if archived and isinstance(content, str)
                    else 10_000,
                ),
                "pinned_summary": (
                    pinned_summary, pinned_summary is not None, 500
                ),
                "retracts": (retracts, retracts is not None, 100),
                "archive_source_id": (
                    archive_source_id, archive_source_id is not None, 512
                ),
                "archived_at": (archived_at, archived_at is not None, 100),
            }
            cleaned_scalars: dict[str, str | None] = {}
            for field, (value, required, max_length) in scalar_inputs.items():
                try:
                    cleaned_scalars[field] = clean_text(
                        field, value, required=required, max_length=max_length,
                        scrub_profile=profile, allow_counts=allow_counts,
                    )
                except ScrubRejected as exc:
                    violations[field] = sorted(
                        {item.rule for item in exc.violations}
                    )
            list_inputs = {
                "tags": (tags, 100),
                "related_files": (related_files, 1_000),
                "related_tickets": (related_tickets, 100),
            }
            cleaned_lists: dict[str, list[str]] = {}
            for field, (values, max_length) in list_inputs.items():
                try:
                    cleaned_lists[field] = clean_list(
                        field, values, max_length=max_length,
                        scrub_profile=profile, allow_counts=allow_counts,
                    )
                except ScrubRejected as exc:
                    violations[field] = sorted(
                        {item.rule for item in exc.violations}
                    )
            if violations:
                return {
                    "scrub_rejected": {
                        "ok": False,
                        "error": "write rejected by scrub policy",
                        "fields": sorted(violations),
                        "rules": sorted(
                            {rule for rules in violations.values() for rule in rules}
                        ),
                    }
                }
            safe_title = cleaned_scalars["title"]
            cleaned_content = cleaned_scalars["content"]
            assert safe_title is not None and cleaned_content is not None
            safe_content = content if archived else cleaned_content
            safe_summary = cleaned_scalars["pinned_summary"]
            safe_retracts = cleaned_scalars["retracts"]
            safe_archive_source_id = cleaned_scalars["archive_source_id"]
            safe_archived_at = cleaned_scalars["archived_at"]
            safe_tags = cleaned_lists.get("tags", [])
            if archived and "archived" not in safe_tags:
                safe_tags.append("archived")
            safe_files = cleaned_lists.get("related_files", [])
            safe_tickets = cleaned_lists.get("related_tickets", [])
            if coordinate_only:
                service.resolve_board_context(
                    document,
                    principal.principal_id,
                    COORDINATOR_MEMBERSHIP_ROLES,
                )
                actor = service.member(document, principal, agent_name)
                released, renewed = [], []
            else:
                actor, released, renewed = prepare_board_call(
                    document, principal, agent_name, now
                )
            memory_id = allocate_memory_id(document)
            entry = {
                "schema_version": 2,
                "memory_id": memory_id,
                "title": safe_title,
                "content": safe_content,
                "scope": scope,
                "author_principal_id": principal.principal_id,
                "author_agent_id": actor["agent_id"],
                "author_agent_name": actor["agent_name"],
                "memory_type": memory_type,
                "tags": safe_tags,
                "related_files": safe_files,
                "related_tickets": safe_tickets,
                "priority": priority,
                "pinned": priority >= 3,
                "created_at": iso_at(now),
                "created_at_epoch": now,
            }
            if archived:
                entry.update(
                    {
                        "archived": True,
                        "archive_source_id": safe_archive_source_id,
                        "archived_at": safe_archived_at or iso_at(now),
                    }
                )
            retracted = None
            if safe_retracts:
                target = memory_target(document, principal, safe_retracts)
                if target is None or not can_moderate_memory(
                    document, target, principal
                ):
                    raise PermissionError("memory not found or not authorized")
                if (target.get("scope") or "project") != scope:
                    raise PermissionError("memory not found or not authorized")
                if target.get("retracted_by"):
                    raise ValueError("memory is already retracted")
                unpin_memory(
                    target, actor, principal, now, f"retracted by {memory_id}",
                    force_audit=True,
                )
                if not str(target.get("title", "")).startswith("[RETRACTED]"):
                    target["title"] = f"[RETRACTED] {target.get('title', '')}"
                target["retracted_by"] = memory_id
                target["retracted_at"] = iso_at(now)
                entry["retracts"] = safe_retracts
                retracted = copy.deepcopy(target)
            if entry["pinned"]:
                entry["pinned_summary"] = compact_summary(
                    safe_title, safe_content, safe_summary
                )
            document["memories"].append(entry)
            scrub_audit = record_scrub_allows(
                document, actor, now, allow_counts
            )
            return {
                "actor": actor,
                "entry": copy.deepcopy(entry),
                "retracted": retracted,
                "recipients": memory_recipients(document, actor, scope),
                "released": released,
                "renewed": renewed,
                "scrub_audit": scrub_audit,
            }

        changed = service.mutate(board_id, write)
        if "scrub_rejected" in changed:
            return changed["scrub_rejected"]
        release_events = await publish_releases(board_id, changed["released"], principal, ctx)
        uri = resource_uri(board_id, "memory", changed["entry"]["memory_id"])
        event = await append_and_publish(
            board_id, changed["actor"], "memory_written", uri, changed["recipients"], ctx,
            memory_id=changed["entry"]["memory_id"],
        )
        return {
            "ok": True,
            "memory": changed["entry"],
            "event": event,
            "release_events": release_events,
            "implicitly_renewed": changed["renewed"],
            "retracted": changed["retracted"],
            "scrub_audit": changed["scrub_audit"],
        }

    @tool()
    async def memory_read(
        board_id: str,
        agent_name: str,
        ctx: Context,
        memory_type: str | None = None,
        tag: str | None = None,
        author: str | None = None,
        since: str | None = None,
        since_minutes: int | None = None,
        pinned_only: bool = False,
        include_archived: bool = False,
        limit: int = 50,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Return visible live memories, plus archives when explicitly requested."""
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        if memory_type is not None and memory_type not in MEMORY_TYPES:
            raise ValueError("unsupported memory_type")
        if since_minutes is not None and since_minutes < 1:
            raise ValueError("since_minutes must be positive")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        cutoff = 0.0
        if since is not None:
            try:
                cutoff = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError) as exc:
                raise ValueError("since must be an ISO-8601 timestamp") from exc
        if since_minutes is not None:
            cutoff = max(cutoff, time.time() - since_minutes * 60)
        principal = current_principal()
        require_scope(principal, "board:read")
        document = service.load(board_id)
        service.member(document, principal, agent_name)
        visible = [project_memory(entry) for entry in visible_memories(document, principal)]
        if not include_archived:
            visible = [item for item in visible if not item.get("archived")]
        if memory_type is not None:
            visible = [item for item in visible if item["memory_type"] == memory_type]
        if tag is not None:
            visible = [item for item in visible if tag in item.get("tags", [])]
        if author is not None:
            visible = [
                item for item in visible
                if author in {
                    item.get("author_agent_name"),
                    item.get("author_agent_id"),
                    item.get("author_principal_id"),
                }
            ]
        if cutoff:
            visible = [
                item for item in visible
                if float(item.get("created_at_epoch", 0)) >= cutoff - 1e-6
            ]
        if pinned_only:
            visible = [item for item in visible if item.get("pinned")]
        visible.sort(
            key=lambda item: (
                float(item.get("created_at_epoch", 0)), item.get("memory_id", "")
            ),
            reverse=True,
        )
        total_matching = len(visible)
        visible = visible[:limit]
        return {
            "ok": True,
            "memories": visible,
            "visible_count": len(visible),
            "total_matching": total_matching,
            "release_events": [],
            "implicitly_renewed": [],
        }

    @tool()
    async def memory_unpin(
        board_id: str,
        agent_name: str,
        memory_id: str,
        ctx: Context,
        reason: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Demote a visible authorized memory while retaining its audit record."""
        board_id = require_id("board_id", board_id)
        principal = current_principal()
        require_scope(principal, "board:write")
        safe_id = clean_text("memory_id", memory_id, required=True, max_length=100)
        safe_reason = clean_text(
            "reason", reason, required=reason is not None, max_length=500
        )
        assert safe_id is not None
        now = time.time()

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            actor, released, renewed = prepare_board_call(
                document, principal, agent_name, now
            )
            target = memory_target(document, principal, safe_id)
            if target is None or not can_moderate_memory(
                document, target, principal
            ):
                raise PermissionError("memory not found or not authorized")
            changed = unpin_memory(
                target, actor, principal, now, safe_reason
            )
            return {
                "actor": actor,
                "entry": copy.deepcopy(target),
                "changed": changed,
                "recipients": memory_recipients(
                    document, actor, target.get("scope") or "project"
                ),
                "released": released,
                "renewed": renewed,
            }

        result = service.mutate(board_id, mutate)
        release_events = await publish_releases(
            board_id, result["released"], principal, ctx
        )
        event = None
        if result["changed"]:
            uri = resource_uri(board_id, "memory", safe_id)
            event = await append_and_publish(
                board_id, result["actor"], "memory_written", uri,
                result["recipients"], ctx, memory_id=safe_id,
            )
        return {
            "ok": True,
            "changed": result["changed"],
            "memory": result["entry"],
            "event": event,
            "release_events": release_events,
            "implicitly_renewed": result["renewed"],
        }

    @tool()
    async def memory_search(
        board_id: str,
        query: str,
        ctx: Context,
        tag: str | None = None,
        author: str | None = None,
        include_archived: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search visible live memories, plus archives when explicitly requested."""
        board_id = require_id("board_id", board_id)
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        safe_query = clean_text("query", query, required=True, max_length=200)
        assert safe_query is not None
        principal = current_principal()
        require_scope(principal, "board:read")
        document = service.load(board_id)
        service.principal_members(document, principal.principal_id)
        needle = safe_query.casefold()
        ranked: list[tuple[int, float, str, dict[str, Any]]] = []
        for raw in visible_memories(document, principal):
            item = project_memory(raw)
            if item.get("archived") and not include_archived:
                continue
            if tag is not None and tag not in item.get("tags", []):
                continue
            if author is not None and author not in {
                item.get("author_agent_name"), item.get("author_agent_id"),
                item.get("author_principal_id"),
            }:
                continue
            title = str(item.get("title", "")).casefold()
            content = str(item.get("content", "")).casefold()
            tags_text = " ".join(item.get("tags", [])).casefold()
            files_text = " ".join(item.get("related_files", [])).casefold()
            tickets_text = " ".join(item.get("related_tickets", [])).casefold()
            score = (
                5 * (needle in title)
                + 3 * (needle in tags_text)
                + 2 * (needle in files_text or needle in tickets_text)
                + (needle in content)
            )
            if score:
                ranked.append(
                    (
                        int(score), float(item.get("created_at_epoch", 0)),
                        str(item.get("memory_id", "")), item,
                    )
                )
        ranked.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        results = [dict(item, score=score) for score, _, _, item in ranked[:limit]]
        return {
            "ok": True,
            "query": safe_query,
            "results": results,
            "count": len(results),
            "total_matching": len(ranked),
            "include_archived": include_archived,
        }

    @tool()
    async def memory_links(
        board_id: str,
        ctx: Context,
        memory_id: str | None = None,
        ticket_id: str | None = None,
        file: str | None = None,
        author: str | None = None,
        depth: int = 2,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Traverse visible retraction links and return explicit ticket/file/tag edges."""
        board_id = require_id("board_id", board_id)
        if not 0 <= depth <= 10:
            raise ValueError("depth must be between 0 and 10")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        principal = current_principal()
        require_scope(principal, "board:read")
        document = service.load(board_id)
        service.principal_members(document, principal.principal_id)
        items = [project_memory(raw) for raw in visible_memories(document, principal)]
        by_id = {
            memory_identifier(item): item for item in items
        }
        if memory_id is not None and memory_id not in by_id:
            raise ValueError("memory not found")
        seeds: list[str]
        if memory_id is not None:
            seeds = [memory_id]
        else:
            seeds = [
                item_id for item_id, item in by_id.items()
                if (ticket_id is None or ticket_id in item.get("related_tickets", []))
                and (file is None or file in item.get("related_files", []))
                and (
                    author is None
                    or author in {
                        item.get("author_agent_name"), item.get("author_agent_id"),
                        item.get("author_principal_id"),
                    }
                )
            ]
        adjacency: dict[str, set[str]] = {item_id: set() for item_id in by_id}
        for item_id, item in by_id.items():
            for neighbor in (item.get("retracts"), item.get("retracted_by")):
                if neighbor in by_id:
                    adjacency[item_id].add(str(neighbor))
                    adjacency[str(neighbor)].add(item_id)
        visited: set[str] = set()
        frontier = [(seed, 0) for seed in seeds]
        while frontier and len(visited) < limit:
            current, level = frontier.pop(0)
            if current in visited or current not in by_id:
                continue
            visited.add(current)
            if level < depth:
                frontier.extend(
                    (neighbor, level + 1)
                    for neighbor in sorted(adjacency[current])
                    if neighbor not in visited
                )
        nodes = [by_id[item_id] for item_id in sorted(visited)[:limit]]
        edges: list[dict[str, Any]] = []
        selected = set(visited)
        for item_id in sorted(selected):
            item = by_id[item_id]
            if item.get("retracts") in selected:
                edges.append(
                    {"kind": "retracts", "from": item_id, "to": item["retracts"]}
                )
            for related_ticket in item.get("related_tickets", []):
                edges.append(
                    {"kind": "ticket", "from": item_id, "to": related_ticket}
                )
            for related_file in item.get("related_files", []):
                edges.append({"kind": "file", "from": item_id, "to": related_file})
            for related_tag in item.get("tags", []):
                edges.append({"kind": "tag", "from": item_id, "to": related_tag})
        return {
            "ok": True,
            "nodes": nodes,
            "edges": edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "depth": depth,
        }

    @tool()
    async def memory_checkpoint(
        board_id: str,
        agent_name: str,
        summary: str,
        ctx: Context,
        remaining_tasks: list[str] | None = None,
        files: list[str] | None = None,
        next_steps: list[str] | None = None,
        active_branch: str | None = None,
        blockers: list[str] | None = None,
        scope: str = "project",
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Persist a structured checkpoint memory atomically with agent activity."""
        board_id = require_id("board_id", board_id)
        if scope not in {"private", "project"}:
            raise ValueError("scope must be private or project")
        principal = current_principal()
        require_scope(principal, "board:write")
        safe_summary = clean_text("summary", summary, required=True, max_length=5_000)
        safe_tasks = clean_list("remaining_tasks", remaining_tasks, max_length=1_000)
        safe_files = clean_list("files", files, max_length=1_000)
        safe_steps = clean_list("next_steps", next_steps, max_length=1_000)
        safe_branch = clean_text("active_branch", active_branch, max_length=500)
        safe_blockers = clean_list("blockers", blockers, max_length=1_000)
        assert safe_summary is not None
        now = time.time()

        def checkpoint(document: dict[str, Any]) -> dict[str, Any]:
            actor, released, renewed = prepare_board_call(
                document, principal, agent_name, now
            )
            memory_id = allocate_memory_id(document)
            structured = {
                "summary": safe_summary,
                "remaining_tasks": safe_tasks,
                "files": safe_files,
                "next_steps": safe_steps,
                "active_branch": safe_branch,
                "blockers": safe_blockers,
            }
            content = json.dumps(structured, sort_keys=True, ensure_ascii=False)
            entry = {
                "schema_version": 2,
                "memory_id": memory_id,
                "title": f"Checkpoint: {safe_summary[:80]}",
                "content": content,
                "scope": scope,
                "author_principal_id": principal.principal_id,
                "author_agent_id": actor["agent_id"],
                "author_agent_name": actor["agent_name"],
                "memory_type": "checkpoint",
                "tags": ["checkpoint"],
                "related_files": safe_files,
                "related_tickets": [],
                "priority": 2,
                "pinned": True,
                "pinned_summary": compact_summary(
                    f"Checkpoint: {safe_summary[:80]}", safe_summary, None
                ),
                "created_at": iso_at(now),
                "created_at_epoch": now,
                **structured,
            }
            document["memories"].append(entry)
            return {
                "actor": actor,
                "entry": copy.deepcopy(entry),
                "recipients": memory_recipients(document, actor, scope),
                "released": released,
                "renewed": renewed,
            }

        changed = service.mutate(board_id, checkpoint)
        release_events = await publish_releases(
            board_id, changed["released"], principal, ctx
        )
        uri = resource_uri(board_id, "memory", changed["entry"]["memory_id"])
        event = await append_and_publish(
            board_id, changed["actor"], "memory_written", uri,
            changed["recipients"], ctx,
            memory_id=changed["entry"]["memory_id"],
        )
        return {
            "ok": True,
            "memory": changed["entry"],
            "event": event,
            "release_events": release_events,
            "implicitly_renewed": changed["renewed"],
        }

    @tool()
    async def memory_handoff(
        board_id: str,
        agent_name: str,
        summary: str,
        next_steps: list[str],
        ctx: Context,
        files: list[str] | None = None,
        warnings: list[str] | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Write a project handoff and flip the exact identity to handed_off."""
        board_id = require_id("board_id", board_id)
        principal = current_principal()
        require_scope(principal, "board:write")
        safe_summary = clean_text("summary", summary, required=True, max_length=5_000)
        safe_steps = clean_list("next_steps", next_steps, max_length=1_000)
        if not safe_steps:
            raise ValueError("next_steps must contain at least one item")
        safe_files = clean_list("files", files, max_length=1_000)
        safe_warnings = clean_list("warnings", warnings, max_length=1_000)
        assert safe_summary is not None
        now = time.time()

        def handoff(document: dict[str, Any]) -> dict[str, Any]:
            released = reap_expired(document, now)
            actor = service.member(document, principal, agent_name)
            if actor.get("lifecycle_status", "active") == "handed_off":
                raise PermissionError("agent already handed off; call board_onboard first")
            memory_id = allocate_memory_id(document)
            superseded = 0
            for prior in document.get("memories", []):
                if (
                    prior.get("memory_type") == "handoff"
                    and prior.get("author_agent_id") == actor["agent_id"]
                    and prior.get("scope") == "project"
                    and prior.get("pinned")
                ):
                    if unpin_memory(
                        prior, actor, principal, now,
                        f"superseded by {memory_id}",
                    ):
                        superseded += 1
            structured = {
                "summary": safe_summary,
                "next_steps": safe_steps,
                "files": safe_files,
                "warnings": safe_warnings,
            }
            content_lines = ["## Summary", safe_summary, "", "## Next Steps"]
            content_lines.extend(
                f"{index}. {step}" for index, step in enumerate(safe_steps, 1)
            )
            if safe_warnings:
                content_lines.extend(["", "## Warnings", *[f"- {item}" for item in safe_warnings]])
            entry = {
                "schema_version": 2,
                "memory_id": memory_id,
                "title": f"Handoff from {actor['agent_name']}",
                "content": "\n".join(content_lines),
                "scope": "project",
                "author_principal_id": principal.principal_id,
                "author_agent_id": actor["agent_id"],
                "author_agent_name": actor["agent_name"],
                "memory_type": "handoff",
                "tags": ["handoff"],
                "related_files": safe_files,
                "related_tickets": [],
                "priority": 3,
                "pinned": True,
                "pinned_summary": compact_summary(
                    f"Handoff from {actor['agent_name']}", safe_summary, None
                ),
                "created_at": iso_at(now),
                "created_at_epoch": now,
                **structured,
            }
            document["memories"].append(entry)
            actor["lifecycle_status"] = "handed_off"
            actor["handed_off_at"] = iso_at(now)
            actor["last_activity_at"] = iso_at(now)
            return {
                "actor": copy.deepcopy(actor),
                "entry": copy.deepcopy(entry),
                "superseded": superseded,
                "recipients": memory_recipients(document, actor, "project"),
                "released": released,
            }

        changed = service.mutate(board_id, handoff)
        release_events = await publish_releases(
            board_id, changed["released"], principal, ctx
        )
        uri = resource_uri(board_id, "memory", changed["entry"]["memory_id"])
        event = await append_and_publish(
            board_id, changed["actor"], "memory_written", uri,
            changed["recipients"], ctx,
            memory_id=changed["entry"]["memory_id"],
        )
        return {
            "ok": True,
            "memory": changed["entry"],
            "lifecycle_status": "handed_off",
            "superseded_handoffs": changed["superseded"],
            "event": event,
            "release_events": release_events,
        }

    @tool()
    async def board_get_briefing(
        board_id: str,
        ctx: Context,
        token_budget: int = 4_000,
        ticket_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a pure, principal-filtered, token-bounded board briefing."""
        board_id = require_id("board_id", board_id)
        if ticket_id is not None:
            require_id("ticket_id", ticket_id)
        principal = current_principal()
        require_scope(principal, "board:read")
        document = service.load(board_id)
        service.principal_members(document, principal.principal_id)
        result = briefing_payload(
            document, principal, token_budget, ticket_id=ticket_id
        )
        return {
            "ok": True,
            "board_id": board_id,
            **result,
            "latest_seq": latest_seq(board_id),
        }

    @tool()
    async def board_status(board_id: str) -> dict[str, Any]:
        """Return a pure structured and rendered board status summary."""
        board_id = require_id("board_id", board_id)
        principal = current_principal()
        require_scope(principal, "board:read")
        document = service.load(board_id)
        service.principal_members(document, principal.principal_id)
        memories = [project_memory(item) for item in visible_memories(document, principal)]
        type_counts: dict[str, int] = {}
        for item in memories:
            kind = str(item.get("memory_type", "context"))
            type_counts[kind] = type_counts.get(kind, 0) + 1
        status_counts: dict[str, int] = {}
        review_label_counts: dict[str, int] = {}
        unassignable: list[dict[str, Any]] = []
        for ticket in document["tickets"].values():
            status = str(ticket.get("status", "unknown"))
            status_counts[status] = status_counts.get(status, 0) + 1
            state = ticket.get("dispatch_state")
            if isinstance(state, Mapping) and state.get("state") == "unassignable":
                unassignable.append(
                    {"ticket_id": ticket.get("ticket_id"), "reason": state.get("reason")}
                )
            for review in ticket.get("review_history", []):
                label = review.get("review_label")
                if isinstance(label, str) and label:
                    review_label_counts[label] = review_label_counts.get(label, 0) + 1
        agents = project_agents(document)
        current_review_policy = board_review_policy(document)
        review_policy_text = (
            "workflow (agent cross-check; never independent-principal approval)"
            if current_review_policy == "workflow"
            else "strict (independent principal required)"
        )
        rendered = "\n".join(
            [
                f"# Board status: {board_id}",
                f"Agents: {len(agents)} | Tickets: {len(document['tickets'])} | Visible memories: {len(memories)}",
                f"Review policy: {review_policy_text}",
                "Unassignable: " + (
                    ", ".join(
                        f"{item['ticket_id']} ({item['reason']})" for item in unassignable
                    ) or "none"
                ),
                "Review labels: " + (
                    ", ".join(
                        f"{key}={value}"
                        for key, value in sorted(review_label_counts.items())
                    )
                    or "none"
                ),
                "Ticket states: " + ", ".join(
                    f"{key}={value}" for key, value in sorted(status_counts.items())
                ),
                "Memory types: " + ", ".join(
                    f"{key}={value}" for key, value in sorted(type_counts.items())
                ),
            ]
        )
        return {
            "ok": True,
            "board_id": board_id,
            "agents": agents,
            "claim_ttl_s": claim_ttl(document),
            "scrub_profile": board_scrub_profile(document),
            "review_policy": current_review_policy,
            "dispatch_policy": dispatch_policy(document),
            "unassignable_tickets": unassignable,
            "review_label_counts": review_label_counts,
            "scrub_allow_counts": copy.deepcopy(
                document["config"].get("scrub_allow_counts", {})
            ),
            "ticket_status_counts": status_counts,
            "memory_type_counts": type_counts,
            "visible_memory_count": len(memories),
            "latest_seq": latest_seq(board_id),
            "rendered": rendered,
        }

    @tool()
    async def board_state_update(
        board_id: str,
        agent_name: str,
        key: str,
        value: str,
        ctx: Context,
        expected_generation: str | None = None,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Atomically set one project-scoped board state value."""
        board_id = require_id("board_id", board_id)
        key = require_id("key", key)
        principal = current_principal()
        if "board:write" in principal.scopes:
            authority = "write"
        elif INTAKE_SCOPE in principal.scopes:
            authority = "intake"
        else:
            require_scope(principal, COORDINATOR_SCOPE)
            authority = "coordinate"
        if authority == "coordinate" and key != "coordinator_findings":
            raise PermissionError(
                "coordinator authorization permits only coordinator_findings state"
            )
        if authority == "intake" and key not in INTAKE_STATE_KEYS:
            raise PermissionError(
                "board:intake authorization permits only coordinator_intake "
                "and coordinator_findings state"
            )
        if expected_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", expected_sha256
        ):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        now = time.time()

        def update(document: dict[str, Any]) -> dict[str, Any]:
            safe_value = clean_text(
                "value",
                value,
                max_length=5_000,
                scrub_profile=board_scrub_profile(document),
            )
            assert safe_value is not None
            if expected_sha256 is not None:
                current = document.setdefault("state", {}).get(key)
                current_value = (
                    current.get("value") if isinstance(current, dict) else None
                )
                if (
                    not isinstance(current_value, str)
                    or not hmac.compare_digest(
                        hashlib.sha256(current_value.encode("utf-8")).hexdigest(),
                        expected_sha256,
                    )
                ):
                    raise ValueError("state precondition failed")
            if authority != "write":
                service.resolve_board_context(
                    document,
                    principal.principal_id,
                    COORDINATOR_MEMBERSHIP_ROLES,
                )
                actor = service.member(document, principal, agent_name)
                released, renewed = [], []
            else:
                actor, released, renewed = prepare_board_call(
                    document, principal, agent_name, now
                )
            entry = {
                "value": safe_value,
                "scope": "project",
                "updated_at": iso_at(now),
                "updated_by_agent_id": actor["agent_id"],
                "updated_by_principal_id": principal.principal_id,
            }
            document.setdefault("state", {})[key] = entry
            return {
                "actor": actor,
                "entry": copy.deepcopy(entry),
                "released": released,
                "renewed": renewed,
            }

        result = service.mutate(board_id, update)
        release_events = await publish_releases(
            board_id, result["released"], principal, ctx
        )
        return {
            "ok": True,
            "key": key,
            "state": result["entry"],
            "release_events": release_events,
            "implicitly_renewed": result["renewed"],
        }

    @tool()
    async def board_state_get(
        board_id: str,
        key: str | None = None,
    ) -> dict[str, Any]:
        """Read one or all project-scoped board state values without mutation."""
        board_id = require_id("board_id", board_id)
        if key is not None:
            key = require_id("key", key)
        principal = current_principal()
        if "board:read" not in principal.scopes:
            require_scope(principal, INTAKE_SCOPE)
            if key is None or key not in INTAKE_STATE_KEYS:
                raise PermissionError(
                    "board:intake authorization reads only coordinator_intake "
                    "and coordinator_findings state"
                )
        document = service.load(board_id)
        service.principal_members(document, principal.principal_id)
        state = document.get("state", {})
        if key is not None and key not in state:
            raise ValueError("state key not found")
        return {
            "ok": True,
            "scope": "project",
            "key": key,
            "state": copy.deepcopy(state[key] if key is not None else state),
        }

    @tool()
    async def journal_compact(
        board_id: str,
        retain_last: int,
    ) -> dict[str, Any]:
        """Manually compact only derivable journal telemetry. Durable tickets,
        memories, agents, and consumer cursors are never deleted or modified.
        """
        board_id = require_id("board_id", board_id)
        if type(retain_last) is not int or retain_last < MIN_COMPACTION_RETAIN_LAST:
            raise ValueError(
                f"retain_last must be an integer of at least "
                f"{MIN_COMPACTION_RETAIN_LAST}"
            )
        principal = current_principal()
        require_scope(principal, "board:write")
        document = service.load(board_id)
        service.resolve_board_context(
            document, principal.principal_id, {"admin"}
        )
        compacted = service.journal.compact(board_id, retain_last)
        return {
            "ok": True,
            "board_id": board_id,
            **compacted,
            "durable_records_untouched": True,
        }

    @tool()
    async def board_dispatch_events(
        board_id: str,
        limit: int = DEFAULT_DISPATCH_PROJECTION_LIMIT,
    ) -> dict[str, Any]:
        """Return a bounded cross-seat projection of recent dispatch events.

        This member-authorized view intentionally omits event actors, recipient
        identities, agent IDs, and payload references. ``board_catchup`` keeps
        its recipient-scoped visibility contract.
        """
        board_id = require_id("board_id", board_id)
        if (
            type(limit) is not int
            or not 1 <= limit <= MAX_DISPATCH_PROJECTION_LIMIT
        ):
            raise ValueError(
                f"limit must be between 1 and {MAX_DISPATCH_PROJECTION_LIMIT}"
            )
        principal = current_principal()
        require_scope(principal, "board:read")
        document = service.load(board_id)
        service.principal_members(document, principal.principal_id)

        latest_cursor = latest_seq(board_id)
        start = max(0, latest_cursor - MAX_DISPATCH_PROJECTION_SCAN_EVENTS)
        probe = service.journal.read_after(board_id, start, 1)
        compacted_through = int(probe["compacted_through"])
        if probe["resync_required"]:
            start = compacted_through
        scan_truncated = compacted_through > 0 or start > compacted_through
        cursor = start
        scanned = 0
        projected: list[dict[str, Any]] = []
        while (
            cursor < latest_cursor
            and scanned < MAX_DISPATCH_PROJECTION_SCAN_EVENTS
        ):
            page_limit = min(
                1_000,
                latest_cursor - cursor,
                MAX_DISPATCH_PROJECTION_SCAN_EVENTS - scanned,
            )
            page = service.journal.read_after(board_id, cursor, page_limit)
            if page["resync_required"]:
                cursor = int(page["compacted_through"])
                continue
            rows = page["events"]
            if not rows:
                break
            scanned += len(rows)
            cursor = int(page["next_cursor"])
            projected.extend(
                {
                    key: copy.deepcopy(event[key])
                    for key in sorted(DISPATCH_PROJECTION_FIELDS)
                    if key in event
                }
                for event in rows
                if event.get("kind") in DISPATCH_EVENT_KINDS
            )

        return {
            "ok": True,
            "board_id": board_id,
            "events": projected[-limit:],
            "latest_seq": latest_cursor,
            "scan_count": scanned,
            "scan_truncated": scan_truncated,
        }

    @tool()
    async def board_catchup(
        board_id: str,
        agent_name: str,
        ctx: Context,
        cursor: int | None = None,
        limit: int = 100,
        ack: bool = True,
        touch: bool = True,
        max_events: int = DEFAULT_CATCHUP_MAX_EVENTS,
        max_bytes: int = DEFAULT_CATCHUP_MAX_BYTES,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        """Drain a byte- and event-bounded journal page.

        Optionally acknowledge only the cursor represented in this response.
        ``touch=False`` is a pure wake-cue refetch: it does not update member
        activity, reap or renew leases, acknowledge a cursor, or validate a
        write generation. Visibility and response bounds remain unchanged.
        """
        board_id = require_id("board_id", board_id)
        agent_name = require_id("agent_name", agent_name)
        if (
            type(max_events) is not int
            or not 1 <= max_events <= MAX_CATCHUP_MAX_EVENTS
        ):
            raise ValueError(
                f"max_events must be between 1 and {MAX_CATCHUP_MAX_EVENTS}"
            )
        if (
            type(max_bytes) is not int
            or not MIN_CATCHUP_MAX_BYTES <= max_bytes <= MAX_CATCHUP_MAX_BYTES
        ):
            raise ValueError(
                f"max_bytes must be between {MIN_CATCHUP_MAX_BYTES} and "
                f"{MAX_CATCHUP_MAX_BYTES}"
            )
        if type(touch) is not bool:
            raise ValueError("touch must be a boolean")
        principal = current_principal()
        require_scope(principal, "board:read")
        document = service.load(board_id)
        actor = service.member(document, principal, agent_name)
        acknowledged_cursor = service.cursors.get(
            principal.principal_id, agent_name, board_id
        )
        start = (
            acknowledged_cursor
            if cursor is None else cursor
        )
        # Validate the caller-controlled page before any compatibility heartbeat.
        service.journal.read_after(board_id, start, limit)
        release_events: list[dict[str, Any]] = []
        renewed: list[str] = []
        if touch and "board:write" in principal.scopes:
            now = time.time()

            def prepare(document: dict[str, Any]) -> dict[str, Any]:
                actor, released, implicit = prepare_board_call(
                    document, principal, agent_name, now
                )
                return {"actor": actor, "released": released, "renewed": implicit}

            prepared = service.mutate(board_id, prepare)
            release_events = await publish_releases(
                board_id, prepared["released"], principal, ctx
            )
            renewed = prepared["renewed"]
        document = service.load(board_id)
        actor = service.member(document, principal, agent_name)
        page = service.journal.read_after(
            board_id, start, min(limit, max_events)
        )

        def is_currently_open_ticket_event(event: dict[str, Any]) -> bool:
            ticket_id = event.get("ticket_id")
            if (
                not isinstance(ticket_id, str)
                or event.get("kind")
                not in {"ticket_created", "ticket_status_changed"}
                or event.get("status_to") != "open"
            ):
                return False
            ticket = document["tickets"].get(ticket_id)
            return ticket is not None and ticket.get("status") == "open"

        def event_is_unexpired(event: dict[str, Any]) -> bool:
            value = event.get("expires_at")
            if value is None:
                return True
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return False
            return parsed.tzinfo is not None and parsed.astimezone(
                timezone.utc
            ) > datetime.now(timezone.utc)

        visible = [
            event
            for event in page["events"]
            if event.get("actor") != actor["agent_id"]
            and event_is_unexpired(event)
            and (
                actor["agent_id"] in event.get("recipient_identities", [])
                or is_currently_open_ticket_event(event)
            )
        ]
        returned = list(visible)

        def payload(events: list[dict[str, Any]]) -> dict[str, Any]:
            if page["resync_required"]:
                effective_cursor = int(page["next_cursor"])
            elif not visible or len(events) == len(visible):
                # Advancing through invisible events is safe only when every
                # visible event from this raw page was returned.
                effective_cursor = int(page["next_cursor"])
            elif events:
                effective_cursor = int(events[-1]["seq"])
            else:
                effective_cursor = start
            latest_cursor = int(page["latest_cursor"])
            has_more = (
                bool(page["has_more"])
                if page["resync_required"]
                else effective_cursor < latest_cursor
            )
            effective_scan_count = sum(
                1
                for event in page["events"]
                if int(event["seq"]) <= effective_cursor
            )
            omitted_visible = len(visible) - len(events)
            total_event_count = max(0, latest_cursor - start)
            omitted_journal = (
                0
                if page["resync_required"]
                else max(0, latest_cursor - effective_cursor)
            )
            return {
                "ok": True,
                "board_id": page["board_id"],
                "bounds": {
                    "limit": limit,
                    "max_events": max_events,
                    "max_bytes": max_bytes,
                },
                # Dynamic journal data begins here. Keep all reusable request
                # metadata ahead of it and preserve a fixed field order after it.
                "events": events,
                "next_cursor": effective_cursor,
                "latest_cursor": latest_cursor,
                "has_more": has_more,
                "resync_required": page["resync_required"],
                "compacted_through": page["compacted_through"],
                "reset_cursor": page["reset_cursor"],
                "new_seq": effective_cursor,
                "scan_count": len(page["events"]),
                "effective_scan_count": effective_scan_count,
                "visible_count": len(events),
                "acknowledged_cursor": acknowledged_cursor,
                "touched": touch,
                "release_events": release_events,
                "implicitly_renewed": renewed,
                "total_counts": {
                    "events": total_event_count,
                    "journal_events_after_cursor": total_event_count,
                    "visible_events_in_page": len(visible),
                },
                "returned_counts": {
                    "events": len(events),
                    "journal_events_scanned": effective_scan_count,
                },
                "omitted_counts": {
                    "events": max(0, total_event_count - len(events)),
                    "visible_events": omitted_visible,
                    "journal_events": omitted_journal,
                },
                "truncated": bool(has_more or omitted_visible),
            }

        result = payload(returned)

        def serialized_size(value: Any) -> int:
            return len(
                json.dumps(value, ensure_ascii=False, sort_keys=True).encode(
                    "utf-8"
                )
            )

        while serialized_size(result) > max_bytes and returned:
            returned.pop()
            result = payload(returned)
        if serialized_size(result) > max_bytes:
            raise ValueError("max_bytes is too small for catchup metadata")
        if visible and not returned:
            raise ValueError("max_bytes is too small for one journal event")

        if touch and ack and not page["resync_required"]:
            service.validate_generation(board_id)
            result["acknowledged_cursor"] = service.cursors.ack(
                principal.principal_id,
                agent_name,
                board_id,
                result["next_cursor"],
            )
        return result

    original_list_tools = mcp.list_tools

    async def custom_handle_list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        meta = ctx.meta if hasattr(ctx, "meta") and isinstance(ctx.meta, Mapping) else {}
        client_info = meta.get("io.modelcontextprotocol/clientInfo") or {}
        agent_name = client_info.get("name") if isinstance(client_info, Mapping) else None

        legacy = False
        if os.environ.get("PURSERS_LEGACY_TOOLS") == "1":
            legacy = True
        elif agent_name:
            try:
                p = current_principal()
                legacy = service.has_seat_legacy_capability(p.principal_id, agent_name)
            except Exception:
                legacy = False

        tools = await original_list_tools()
        if not legacy:
            tools = [t for t in tools if t.name not in DEPRECATED_TOOLS]
        else:
            tools = [
                t.model_copy(
                    update={
                        "annotations": types.ToolAnnotations(
                            title=f"[DEPRECATED] {t.name} is deprecated in a18 and scheduled for removal in a19"
                        ),
                        "meta": {"deprecated": True},
                    }
                )
                if t.name in DEPRECATED_TOOLS
                else t
                for t in tools
            ]
        return types.ListToolsResult(tools=tools)

    mcp._handle_list_tools = custom_handle_list_tools
    mcp._lowlevel_server.add_request_handler(
        "tools/list", types.PaginatedRequestParams, custom_handle_list_tools
    )

    async def custom_list_tools(
        *args: Any, include_legacy: bool | None = None, **kwargs: Any
    ) -> list[Any]:
        tools = await original_list_tools(*args, **kwargs)
        legacy = include_legacy
        if legacy is None:
            legacy = os.environ.get("PURSERS_LEGACY_TOOLS") == "1"
        if not legacy:
            tools = [t for t in tools if t.name not in DEPRECATED_TOOLS]
        else:
            tools = [
                t.model_copy(
                    update={
                        "annotations": types.ToolAnnotations(
                            title=f"[DEPRECATED] {t.name} is deprecated in a18 and scheduled for removal in a19"
                        ),
                        "meta": {"deprecated": True},
                    }
                )
                if t.name in DEPRECATED_TOOLS
                else t
                for t in tools
            ]
        return tools

    mcp.list_tools = custom_list_tools
    return mcp, service


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--advance-generation", metavar="BOARD_ID")
    parser.add_argument("--expect-generation-sha256", metavar="HEX")
    args = parser.parse_args()
    if bool(args.advance_generation) != bool(args.expect_generation_sha256):
        parser.error(
            "--advance-generation and --expect-generation-sha256 must be supplied together"
        )
    if args.advance_generation:
        service = CentralBoard(args.data_root)
        result = service.advance_generation(
            args.advance_generation, args.expect_generation_sha256
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "board_id": result["board_id"],
                    "generation_revision": result["generation_revision"],
                    "generation_token_sha256": result["generation_token_sha256"],
                },
                sort_keys=True,
            )
        )
        return
    with CentralDataLock(args.data_root):
        mcp, service = build_server(args.host, args.port, args.data_root)
        app = create_streamable_http_app(mcp, service, host=args.host)
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            server_header=False,
            access_log=False,
        )


if __name__ == "__main__":
    main()
