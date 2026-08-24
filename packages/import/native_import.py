"""Native one-way import into the transactional central SQLite Store seam."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

if __package__:
    from .journal import _board_token
    from .scrub import Policy, ScrubRejected, scrub
    from .transactional_sqlite import TransactionalSQLiteStore
else:  # source-checkout execution
    from journal import _board_token
    from scrub import Policy, ScrubRejected, scrub
    from transactional_sqlite import TransactionalSQLiteStore


BOARD_ID = re.compile(r"^[A-Za-z0-9._-]+$")
CENTRAL_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
PROJECT_TYPES = {"decision", "handoff", "ticket"}
LEGACY_TICKET_FIELDS = {
    "id",
    "ticket_id",
    "title",
    "description",
    "status",
    "priority",
    "created_by",
    "claimed_by",
    "assigned_to",
    "reviewed_by",
    "submitted_by",
    "last_abandoned_by",
    "rejection_count",
    "abandoned_count",
    "depends_on",
    "blocked_by",
    "tags",
    "related_files",
    "acceptance_criteria",
    "result",
    "review_comment",
    "created_at",
    "updated_at",
    "claimed_at",
    "submitted_at",
    "reviewed_at",
    "target_url",
    "scope",
    "required_fields",
    "forbidden",
    "selector_hints",
    "timestamp",
    "claim_permission",
    "submit_permission",
    "review_permission",
    "review_notes",
    "fix_instructions",
    "canceled_by",
    "cancel_permission",
    "cancel_reason",
    "canceled_at",
    "terminated_by",
    "terminate_permission",
    "terminate_reason",
    "terminated_at",
    "completed_at",
    "abandoned_at",
    "last_abandoned_at",
}
CENTRAL_AUTHORITY_TICKET_FIELDS = {
    "generation_token",
    "generation_revision",
    "role",
    "membership_role",
    "scopes",
    "permissions",
    "capability",
    "authorization",
    "access_token",
}
PHASES = ("memories", "agents", "tickets", "state", "journal-seed")
PROMOTED_SCHEMA_VERSION = 1
GENERATION_REVISION = 1
PROMOTED_TEMP = re.compile(r"^\.PROMOTED\.json\.[0-9]+\.[0-9a-f]{16}\.tmp$")


def mint_generation_token() -> str:
    """Mint an opaque activation epoch; authorization still comes from OAuth."""
    return "GEN-" + secrets.token_urlsafe(32)


def owner_provisioned_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def generation_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_generation_token(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("generation_token must be a non-empty opaque string <= 256 chars")
    return value


def validate_central_url(value: str) -> str:
    """Require the public production MCP URL without credentials or URL secrets."""
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 2048
    ):
        raise ValueError("central_url must be a non-empty canonical URL")
    if any(
        character.isspace()
        or ord(character) < 0x20
        or ord(character) == 0x7F
        for character in value
    ):
        raise ValueError("central_url must not contain whitespace or control characters")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("central_url contains an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "central_url must be public HTTPS ending exactly /mcp with no "
            "userinfo, query, or fragment"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("central_url contains an invalid port")
    return value


def _overlaps(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _resolve_agent_mem_root(path: Path, label: str) -> Path:
    lexical = Path(os.path.abspath(os.fspath(path)))
    try:
        mode = lexical.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is missing: {lexical}") from exc
    if stat.S_ISLNK(mode):
        raise ValueError(f"{label} must not be a symlink: {lexical}")
    if not stat.S_ISDIR(mode):
        raise ValueError(f"{label} must be a directory: {lexical}")
    resolved = lexical.resolve()
    if resolved.name != ".agent-mem" or not (resolved / "memories.json").is_file():
        raise ValueError(f"{label} must be an existing .agent-mem directory")
    return resolved


def _require_regular(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular file: {path}")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_marker(
    board_id: str, digest: str, central_url: str
) -> tuple[dict[str, Any], bytes]:
    document = {
        "schema_version": PROMOTED_SCHEMA_VERSION,
        "board_id": board_id,
        "direction": "local-to-central-only",
        "source_hash": digest,
        "central_url": central_url,
    }
    payload = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return document, payload


def _load_existing_marker(
    path: Path, expected: dict[str, Any], expected_payload: bytes
) -> bytes:
    _require_regular(path, "PROMOTED.json")
    try:
        payload = path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("existing PROMOTED.json is invalid") from exc
    if document != expected:
        raise ValueError("existing PROMOTED.json does not match this promotion")
    if payload != expected_payload:
        raise ValueError("existing PROMOTED.json is not canonical")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("existing PROMOTED.json must have mode 0600")
    return payload


def _cleanup_marker_temps(target: Path) -> None:
    removed = False
    for item in target.iterdir():
        if not PROMOTED_TEMP.fullmatch(item.name):
            continue
        _require_regular(item, "stale PROMOTED.json temp")
        item.unlink()
        removed = True
    if removed:
        _fsync_directory(target)


def arm_promoted_marker(
    source: Path,
    central_root: Path,
    promoted_board_root: Path,
    board_id: str,
    digest: str,
    central_url: str,
) -> dict[str, Any]:
    """Atomically transition a frozen live board to its durable redirect fence."""
    source = _resolve_agent_mem_root(source, "source snapshot")
    central_root = central_root.resolve(strict=False)
    target = _resolve_agent_mem_root(promoted_board_root, "promoted_board_root")
    if _overlaps(target, source):
        raise ValueError("promoted_board_root must be distinct from the source snapshot")
    if _overlaps(target, central_root):
        raise ValueError("promoted_board_root must be outside the central data root")
    lock_path = target / ".board.lock"
    _require_regular(lock_path, "live board lock")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    lock_descriptor = os.open(lock_path, os.O_RDWR | nofollow | cloexec)
    if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
        os.close(lock_descriptor)
        raise ValueError(f"live board lock must be a regular file: {lock_path}")
    marker_path = target / "PROMOTED.json"
    fence_path = target / "WRITE_FENCE.json"
    temp_path: Path | None = None
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        if source_hash(source) != digest:
            raise ValueError("source snapshot changed after its central import")
        expected, expected_payload = _canonical_marker(
            board_id, digest, central_url
        )
        if marker_path.exists() or marker_path.is_symlink():
            marker_payload = _load_existing_marker(
                marker_path, expected, expected_payload
            )
            if transition_hash(target) != transition_hash(source):
                raise ValueError(
                    "promoted live board differs from the imported source snapshot"
                )
            _cleanup_marker_temps(target)
            marker_status = "existing"
        else:
            _require_regular(fence_path, "WRITE_FENCE.json")
            if transition_hash(target) != transition_hash(source):
                raise ValueError(
                    "frozen live board domain differs from the source snapshot"
                )
            _cleanup_marker_temps(target)
            if source_hash(target) != digest:
                raise ValueError(
                    "frozen live board differs from the imported source snapshot"
                )
            temp_path = target / (
                f".PROMOTED.json.{os.getpid()}.{secrets.token_hex(8)}.tmp"
            )
            descriptor = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec,
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    output.write(expected_payload)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            os.replace(temp_path, marker_path)
            temp_path = None
            _fsync_directory(target)
            marker_payload = expected_payload
            marker_status = "created"

        # PROMOTED takes precedence in the proto. Removing the temporary freeze
        # only after its durable replacement leaves every crash state fenced.
        if fence_path.exists() or fence_path.is_symlink():
            _require_regular(fence_path, "WRITE_FENCE.json")
            fence_path.unlink()
            _fsync_directory(target)
        return {
            "status": marker_status,
            "path": str(marker_path),
            "sha256": hashlib.sha256(marker_payload).hexdigest(),
        }
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
                _fsync_directory(target)
            except FileNotFoundError:
                pass
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _tree_hash(
    source: Path,
    excluded_names: frozenset[str],
    *,
    exclude_promoted_temps: bool = False,
) -> str:
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if path.is_symlink():
            raise ValueError(f"board contains symlink: {relative}")
        if not path.is_file() or path.name in excluded_names or (
            exclude_promoted_temps
            and len(relative.parts) == 1
            and PROMOTED_TEMP.fullmatch(relative.name)
        ):
            continue
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def source_hash(source: Path) -> str:
    """Canonical imported snapshot hash: WRITE_FENCE remains provenance."""
    return _tree_hash(source, frozenset({"PROMOTED.json"}))


def transition_hash(source: Path) -> str:
    """Compare domain bytes across the durable WRITE_FENCE -> PROMOTED swap."""
    return _tree_hash(
        source,
        frozenset({"PROMOTED.json", "WRITE_FENCE.json"}),
        exclude_promoted_temps=True,
    )


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object contains duplicate keys")
        result[key] = value
    return result


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
        )
    except FileNotFoundError:
        return copy.deepcopy(default)


def safe_report_id(value: str) -> str:
    try:
        scrub(str(value), Policy(mode="reject"))
        return str(value)
    except ScrubRejected:
        return "sha256-" + hashlib.sha256(str(value).encode()).hexdigest()[:16]


def safe_record(
    value: Any,
    *,
    record_type: str,
    record_id: str,
    quarantine: list[dict[str, Any]],
    field: str = "",
) -> Any:
    all_violations: list[Any] = []

    def display_path(segments: tuple[tuple[str, str], ...]) -> str:
        """Return an unambiguous path while keeping simple root fields readable."""
        first_is_simple = bool(
            segments
            and segments[0][0] == "key"
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", segments[0][1])
        )
        parts = [segments[0][1]] if first_is_simple else ["$"]
        remaining = segments[1:] if first_is_simple else segments
        for kind, segment in remaining:
            if kind == "index":
                parts.append(f"[{segment}]")
            else:
                parts.append(
                    "[" + json.dumps(segment, ensure_ascii=True, separators=(",", ":")) + "]"
                )
        return "".join(parts)

    initial_segments: tuple[tuple[str, str], ...] = ()
    initial_identity: tuple[tuple[str, str], ...] = ()
    if field:
        initial_segments = (("key", field),)
        initial_identity = (("initial-key-value", field),)

    def visit(
        current: Any,
        segments: tuple[tuple[str, str], ...],
        identity: tuple[tuple[str, str], ...],
    ) -> Any:
        if isinstance(current, str):
            try:
                return scrub(current, Policy(mode="reject"))[0]
            except ScrubRejected as exc:
                identity_payload = json.dumps(
                    identity,
                    ensure_ascii=True,
                    sort_keys=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                quarantine.append(
                    {
                        "record_type": record_type,
                        "record_id": safe_report_id(record_id),
                        "field": display_path(segments) if segments else "$",
                        "field_identity": "sha256-"
                        + hashlib.sha256(identity_payload).hexdigest(),
                        "rules": sorted({item.rule for item in exc.violations}),
                        "violation_count": len(exc.violations),
                        "spans": [
                            {"rule": item.rule, "start": item.start, "end": item.end}
                            for item in exc.violations
                        ],
                    }
                )
                all_violations.extend(exc.violations)
                return current
        if isinstance(current, list):
            return [
                visit(
                    child,
                    segments + (("index", str(index)),),
                    identity + (("index", str(index)),),
                )
                for index, child in enumerate(current)
            ]
        if isinstance(current, dict):
            cleaned: dict[str, Any] = {}
            for index, (key, child) in enumerate(current.items()):
                masked_key = safe_report_id(str(key)) != str(key)
                path_key = f"<masked-key-{index}>" if masked_key else str(key)
                key_segments = segments + (("key", path_key),)
                before = len(quarantine)
                clean_key = visit(
                    str(key),
                    key_segments,
                    identity + (("key-name", str(key)),),
                )
                for row in quarantine[before:]:
                    row["field"] = "@key:" + row["field"]
                if masked_key:
                    clean_key = path_key
                cleaned[clean_key] = visit(
                    child,
                    key_segments,
                    identity + (("key-value", str(key)),),
                )
            return cleaned
        return copy.deepcopy(current)

    result = visit(value, initial_segments, initial_identity)
    if all_violations:
        raise ScrubRejected(all_violations)
    return result


def redact_record(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value, Policy(mode="redact"))[0]
    if isinstance(value, list):
        return [redact_record(item) for item in value]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            redacted_key = scrub(str(key), Policy(mode="redact"))[0]
            if redacted_key in cleaned:
                raise ValueError("redacted dictionary keys collide")
            cleaned[redacted_key] = redact_record(child)
        return cleaned
    return copy.deepcopy(value)


def _unwrap(value: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"expected list or object.{key} list")
    return value


def source_memories(source: Path) -> list[dict[str, Any]]:
    return _unwrap(load_json(source / "memories.json", []), "entries")


def source_archive(source: Path) -> list[dict[str, Any]]:
    value = load_json(source / "archive.json", [])
    if isinstance(value, dict):
        for key in ("entries", "memories", "archived_memories", "archive"):
            if key in value:
                value = value[key]
                break
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(
            "archive.json must contain a list or an object with an entries, "
            "memories, archived_memories, or archive list"
        )
    return value


def source_tickets(source: Path) -> list[dict[str, Any]]:
    return _unwrap(load_json(source / "tickets" / "_index.json", []), "tickets")


def _memory_legacy_id(raw: dict[str, Any]) -> str:
    value = raw.get("id")
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("legacy memory id must be a non-empty bounded string")
    return value


def _ticket_legacy_id(raw: dict[str, Any]) -> str:
    first = raw.get("id")
    second = raw.get("ticket_id")
    if first is not None and second is not None and first != second:
        raise ValueError("legacy ticket id fields disagree")
    value = first if first is not None else second
    if not isinstance(value, str) or not CENTRAL_ID.fullmatch(value):
        raise ValueError("legacy ticket id must match the supported identifier shape")
    return value


def _require_string_or_none(value: Any, label: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"legacy {label} must be a string or null")


def _validate_memory_shape(raw: dict[str, Any]) -> None:
    _memory_legacy_id(raw)
    for field in ("agent_name", "memory_type", "title", "content"):
        _require_string_or_none(raw.get(field), f"memory {field}")
    if "pinned" in raw and not isinstance(raw["pinned"], bool):
        raise ValueError("legacy memory pinned must be boolean")
    if "priority" in raw and (
        type(raw["priority"]) is not int or not 0 <= raw["priority"] <= 5
    ):
        raise ValueError("legacy memory priority must be an integer from 0 to 5")
    for field in ("tags", "related_files", "related_tickets"):
        value = raw.get(field, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError(f"legacy memory {field} must be a list of strings")


def _validate_ticket_shape(raw: dict[str, Any]) -> None:
    _ticket_legacy_id(raw)
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError("legacy ticket title must be a non-empty bounded string")
    string_fields = {
        "title",
        "description",
        "target_url",
        "scope",
        "priority",
        "status",
        "created_by",
        "claimed_by",
        "assigned_to",
        "reviewed_by",
        "submitted_by",
        "last_abandoned_by",
        "review_notes",
        "fix_instructions",
        "claim_permission",
        "submit_permission",
        "review_permission",
        "cancel_permission",
        "cancel_reason",
        "canceled_by",
        "terminate_permission",
        "terminate_reason",
        "terminated_by",
        "result",
        "review_comment",
    }
    for field in string_fields:
        _require_string_or_none(raw.get(field), f"ticket {field}")
    if raw.get("priority") is not None and raw["priority"] not in {
        "low",
        "medium",
        "high",
        "critical",
    }:
        raise ValueError("legacy ticket priority is not a supported v4 value")
    if not isinstance(raw.get("status"), str) or raw["status"] not in {
        "open",
        "claimed",
        "in_progress",
        "creating_report",
        "submitted",
        "reviewing",
        "in_review",
        "closed",
        "rejected",
        "canceled",
        "terminated",
    }:
        raise ValueError("legacy ticket status is not a supported v4 value")
    if raw.get("scope") is not None and raw["scope"] not in {
        "READ-ONLY",
        "interactive-no-send",
        "interactive",
    }:
        raise ValueError("legacy ticket scope is not a supported v4 value")
    for field in (
        "required_fields",
        "forbidden",
        "selector_hints",
        "tags",
        "related_files",
        "depends_on",
        "blocked_by",
        "acceptance_criteria",
    ):
        value = raw.get(field)
        if value is not None and (
            not isinstance(value, list)
            or not all(isinstance(item, str) for item in value)
        ):
            raise ValueError(f"legacy ticket {field} must be a list of strings or null")
    for field in ("rejection_count", "abandoned_count"):
        value = raw.get(field)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"legacy ticket {field} must be a non-negative integer")
    for field in (
        "timestamp",
        "created_at",
        "updated_at",
        "claimed_at",
        "submitted_at",
        "reviewed_at",
        "completed_at",
        "canceled_at",
        "terminated_at",
        "abandoned_at",
        "last_abandoned_at",
    ):
        value = raw.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float, str))
        ):
            raise ValueError(f"legacy ticket {field} has an invalid timestamp shape")


def _validate_agent_shape(record_id: str, raw: dict[str, Any]) -> None:
    if not isinstance(record_id, str) or not record_id or len(record_id) > 512:
        raise ValueError("legacy agent record id must be a non-empty bounded string")
    name = raw.get("agent_name")
    if not isinstance(name, str) or not name or len(name) > 100:
        raise ValueError("legacy agent agent_name must be a non-empty bounded string")
    for field, maximum in (
        ("agent_platform", 50),
        ("platform", 50),
        ("agent_role", 50),
        ("role", 50),
        ("task_focus", 500),
        ("kia_reason", 500),
    ):
        value = raw.get(field)
        if value is not None and (
            not isinstance(value, str) or len(value) > maximum
        ):
            raise ValueError(
                f"legacy agent {field} must be a bounded string or null"
            )
    status_value = raw.get("status")
    if not isinstance(status_value, str) or status_value.lower() not in {
        "active",
        "kia",
        "completed",
        "handed_off",
    }:
        raise ValueError("legacy agent status is not a supported v4 value")
    for field in ("joined_at", "handed_off_at", "completed_at", "kia_at"):
        _require_string_or_none(raw.get(field), f"agent {field}")
    for field in ("last_activity", "timestamp"):
        value = raw.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise ValueError(f"legacy agent {field} must be numeric or null")
    memories_written = raw.get("memories_written")
    if memories_written is not None and (
        type(memories_written) is not int or memories_written < 0
    ):
        raise ValueError("legacy agent memories_written must be a non-negative integer")


def _validate_source_shape(source: Path) -> None:
    memory_ids: set[str] = set()
    for raw in source_memories(source):
        _validate_memory_shape(raw)
        identifier = _memory_legacy_id(raw)
        if identifier in memory_ids:
            raise ValueError("duplicate legacy memory identifiers are ambiguous")
        memory_ids.add(identifier)

    archive_ids: set[str] = set()
    for raw in source_archive(source):
        map_archive_memory(raw)
        identifier = archive_record_key(raw)
        if identifier in archive_ids:
            raise ValueError("duplicate archive identifiers are ambiguous")
        archive_ids.add(identifier)

    ticket_ids: set[str] = set()
    for raw in source_tickets(source):
        _validate_ticket_shape(raw)
        identifier = _ticket_legacy_id(raw)
        if identifier in ticket_ids:
            raise ValueError("duplicate legacy ticket identifiers are ambiguous")
        ticket_ids.add(identifier)

    agents = load_json(source / "agents.json", {})
    if not isinstance(agents, dict) or not all(
        isinstance(value, dict) for value in agents.values()
    ):
        raise ValueError("agents.json must contain an object of agent objects")
    for record_id, raw in agents.items():
        _validate_agent_shape(record_id, raw)
    state = load_json(source / "state.json", {})
    if not isinstance(state, dict):
        raise ValueError("state.json must contain an object")
    for artifact in sorted((source / "tickets").rglob("*.md")):
        artifact.read_text(encoding="utf-8")


def memory_id(legacy_id: str) -> str:
    return "LEGACY-" + hashlib.sha256(legacy_id.encode()).hexdigest()[:16]


def archive_record_key(raw: dict[str, Any]) -> str:
    source_id = raw.get("id", raw.get("memory_id", raw.get("source_id")))
    identity: Any = (
        {"source_id": str(source_id)}
        if source_id is not None
        else {"entry": raw}
    )
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "ARCHIVE-" + hashlib.sha256(canonical).hexdigest()[:20]


def map_archive_memory(raw: dict[str, Any]) -> dict[str, Any]:
    record_key = archive_record_key(raw)
    source_id = raw.get("id", raw.get("memory_id", raw.get("source_id")))
    if source_id is not None and (
        isinstance(source_id, bool)
        or not isinstance(source_id, (str, int, float))
        or not str(source_id)
    ):
        raise ValueError("archive source id must be a string or number")
    source_id = str(source_id) if source_id is not None else record_key
    archived_at = raw.get("archived_at")
    if archived_at is not None and (
        isinstance(archived_at, bool)
        or not isinstance(archived_at, (str, int, float))
    ):
        raise ValueError("archive archived_at must be a string, number, or null")
    content = raw.get("content")
    if content is None:
        content = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        title = f"Archived v4 memory {source_id}"
    memory_type = raw.get("memory_type") or "context"
    if memory_type not in {
        "decision", "progress", "blocker", "context", "handoff", "todo",
        "file_change", "discovery", "warning", "checkpoint",
    }:
        memory_type = "context"
    raw_tags = raw.get("tags", [])
    if not isinstance(raw_tags, list) or not all(
        isinstance(item, str) for item in raw_tags
    ):
        raise ValueError("archive tags must be a list of strings")
    for field in ("related_files", "related_tickets"):
        values = raw.get(field, [])
        if not isinstance(values, list) or not all(
            isinstance(item, str) for item in values
        ):
            raise ValueError(f"archive {field} must be a list of strings")
    for field in ("agent_name", "author"):
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"archive {field} must be a string or null")
    tags = list(dict.fromkeys([*raw_tags, "archived"]))
    return {
        "schema_version": 2,
        "memory_id": record_key,
        "title": title,
        "content": content,
        "scope": "project",
        "author_principal_id": "legacy_unbound",
        "author_agent_id": None,
        "author_agent_name": raw.get("agent_name") or raw.get("author"),
        "legacy_agent_name": raw.get("agent_name") or raw.get("author"),
        "memory_type": memory_type,
        "tags": tags,
        "related_files": list(raw.get("related_files") or []),
        "related_tickets": list(raw.get("related_tickets") or []),
        "priority": 0,
        "pinned": False,
        "archived": True,
        "archive_source_id": source_id,
        "archived_at": archived_at,
        "migration_provenance": "v4-archive-import",
        "legacy_record": copy.deepcopy(raw),
    }


def map_memory(raw: dict[str, Any]) -> dict[str, Any]:
    _validate_memory_shape(raw)
    legacy_id = _memory_legacy_id(raw)
    memory_type = raw.get("memory_type") or "context"
    if memory_type not in {
        "decision", "progress", "blocker", "context", "handoff", "todo",
        "file_change", "discovery", "warning", "checkpoint",
    }:
        memory_type = "context"
    pinned = bool(raw.get("pinned"))
    return {
        "schema_version": 2,
        "memory_id": memory_id(legacy_id),
        "legacy_memory_id": legacy_id,
        "title": raw.get("title") or memory_type or "Legacy memory",
        "content": raw.get("content") or "",
        "scope": "project" if memory_type in PROJECT_TYPES else "private",
        "author_principal_id": "legacy_unbound",
        "author_agent_id": None,
        "author_agent_name": raw.get("agent_name"),
        "legacy_agent_name": raw.get("agent_name"),
        "memory_type": memory_type,
        "tags": list(raw.get("tags") or []),
        "related_files": list(raw.get("related_files") or []),
        "related_tickets": list(raw.get("related_tickets") or []),
        "priority": 3 if pinned else (raw.get("priority") or 0),
        "pinned": pinned,
        "migration_provenance": "native-transactional-import",
        "legacy_record": copy.deepcopy(raw),
    }


def map_ticket(raw: dict[str, Any]) -> dict[str, Any]:
    _validate_ticket_shape(raw)
    legacy_id = _ticket_legacy_id(raw)
    mapped = {
        key: copy.deepcopy(value)
        for key, value in raw.items()
        if key in LEGACY_TICKET_FIELDS
    }
    untrusted = {
        key: copy.deepcopy(value)
        for key, value in raw.items()
        if key not in LEGACY_TICKET_FIELDS
        and not key.endswith("_agent_id")
        and not key.endswith("_principal_id")
        and key not in CENTRAL_AUTHORITY_TICKET_FIELDS
    }
    if untrusted:
        mapped["legacy_untrusted_fields"] = untrusted
    mapped.update(
        {
        "ticket_id": legacy_id,
        "legacy_ticket_id": legacy_id,
        "migration_provenance": "native-transactional-import",
        }
    )
    for identity_field in (
        "created_by_agent_id",
        "claimed_by_agent_id",
        "assigned_to_agent_id",
        "reviewed_by_agent_id",
        "submitted_by_agent_id",
        "last_abandoned_by_agent_id",
        "created_by_principal_id",
        "claimed_by_principal_id",
        "assigned_to_principal_id",
        "reviewed_by_principal_id",
        "submitted_by_principal_id",
        "last_abandoned_by_principal_id",
    ):
        mapped[identity_field] = None
    return mapped


def default_board(
    board_id: str,
    owner_principal_id: str,
    owner_agent_name: str,
    provisioned_at: str,
) -> dict[str, Any]:
    return {
        "board_id": board_id,
        "schema_version": 6,
        "generation_token": None,
        "generation_revision": 0,
        "config": {
            "claim_ttl_s": 900,
            "scrub_profile": "strict",
            "review_policy": "strict",
            "scrub_allow_counts": {},
        },
        "members": {},
        "principal_memberships": {
            owner_principal_id: {
                "principal_id": owner_principal_id,
                "role": "admin",
                "source": "offline_import_provisioning",
                "created_by_principal_id": owner_principal_id,
                "created_at": provisioned_at,
                "updated_at": provisioned_at,
                "admission_action": "offline_import_owner_provisioned",
            }
        },
        "principal_revocations": {},
        "invites": {},
        # Offline owner provisioning is audited by the immutable import
        # manifest and the single import_completed journal event.  It does not
        # consume a Central admission-event revision.
        "next_admission_revision": 1,
        "tickets": {},
        "next_ticket_seq": 1,
        "memories": [],
        "next_memory_seq": 1,
        "state": {},
        "legacy_import": {
            "agents": {},
            "identity_mapping": [],
            "ticket_artifacts": {},
            "quarantine": [],
            "watch_migrations": [],
            "owner_principal_id": owner_principal_id,
            "owner_agent_name": owner_agent_name,
            "owner_provisioned_at": provisioned_at,
        },
    }


def board_path(store: TransactionalSQLiteStore, board_id: str) -> Path:
    return store.path("boards", f"{_board_token(board_id)}.json")


def manifest_path(store: TransactionalSQLiteStore, board_id: str) -> Path:
    return store.path("imports", f"{_board_token(board_id)}.json")


def journal_path(store: TransactionalSQLiteStore, board_id: str) -> Path:
    return store.path("journals", f"{_board_token(board_id)}.json")


def _replace(store: TransactionalSQLiteStore, path: Path, value: Any) -> None:
    store.read_modify_write(path, lambda _current: copy.deepcopy(value), dict)


def _append_event(
    store: TransactionalSQLiteStore,
    board_id: str,
    *,
    actor: str,
    payload_ref: str,
    memory_id_value: str,
    fixture_provenance: str,
    generation_token: str | None = None,
    generation_revision: int | None = None,
) -> dict[str, Any]:
    path = journal_path(store, board_id)
    assigned: dict[str, Any] = {}

    def mutate(document: dict[str, Any]) -> None:
        nonlocal assigned
        if not document:
            document.update(
                {"board_id": board_id, "next_seq": 1, "compacted_through": 0, "rows": []}
            )
        if document.get("board_id") != board_id:
            raise ValueError("journal board hash collision or corrupt document")
        seq = int(document["next_seq"])
        assigned = {
            "id": f"EV-{_board_token(board_id)[:12]}-{seq:020d}",
            "seq": seq,
            "board_id": board_id,
            "kind": "memory_written",
            "actor": actor,
            "payload_ref": payload_ref,
            "occurred_at": "1970-01-01T00:00:00+00:00",
            "memory_id": memory_id_value,
            "recipient_identities": [],
            "fixture_provenance": fixture_provenance,
        }
        if generation_token is not None:
            assigned["generation_token"] = _validate_generation_token(
                generation_token
            )
        if generation_revision is not None:
            if generation_revision < 1:
                raise ValueError("generation_revision must be >= 1")
            assigned["generation_revision"] = generation_revision
        document["rows"].append(assigned)
        document["next_seq"] = seq + 1

    store.read_modify_write(path, mutate, dict)
    return assigned


def _phase_memories(source: Path, board: dict[str, Any], quarantine: list[dict[str, Any]]) -> None:
    imported = []
    seen: set[str] = set()
    for raw in source_memories(source):
        rid = _memory_legacy_id(raw)
        if rid in seen:
            raise ValueError("duplicate legacy memory identifiers are ambiguous")
        seen.add(rid)
        try:
            imported.append(
                map_memory(
                    safe_record(
                        raw,
                        record_type="memory",
                        record_id=rid,
                        quarantine=quarantine,
                    )
                )
            )
        except ScrubRejected:
            continue
    archive_seen: set[str] = set()
    for raw in source_archive(source):
        rid = archive_record_key(raw)
        if rid in archive_seen:
            raise ValueError("duplicate archive identifiers are ambiguous")
        archive_seen.add(rid)
        try:
            imported.append(
                map_archive_memory(
                    safe_record(
                        raw,
                        record_type="archive",
                        record_id=rid,
                        quarantine=quarantine,
                    )
                )
            )
        except ScrubRejected:
            continue
    board["memories"] = imported
    board["next_memory_seq"] = len(imported) + 1


def backfill_archive(
    archive_file: Path,
    central_root: Path,
    board_id: str,
) -> dict[str, Any]:
    """Append missing v4 archive rows to one completed import without rewriting rows."""
    if not BOARD_ID.fullmatch(board_id):
        raise ValueError("board_id must match [A-Za-z0-9._-]+")
    archive_file = Path(os.path.abspath(os.fspath(archive_file)))
    _require_regular(archive_file, "archive source")
    if archive_file.name != "archive.json":
        raise ValueError("archive source must be a regular archive.json file")
    archive_file = archive_file.resolve(strict=True)
    source = archive_file.parent
    rows = source_archive(source)
    store = TransactionalSQLiteStore(central_root.resolve(strict=True))
    target = board_path(store, board_id)
    with store.transaction():
        manifest = store.load(manifest_path(store, board_id), dict)
        if manifest.get("status") != "complete":
            raise ValueError("archive backfill requires a completed import manifest")
        board = store.load(target, dict)
        if not board:
            raise ValueError("board not found")
        existing: dict[str, dict[str, Any]] = {}
        for item in board.get("memories", []):
            identifier = str(item.get("memory_id"))
            if identifier in existing:
                raise ValueError("target board contains duplicate memory identifiers")
            existing[identifier] = item
        appended: list[dict[str, Any]] = []
        duplicate_rows = 0
        input_keys: set[str] = set()
        for raw in rows:
            rid = archive_record_key(raw)
            if rid in input_keys:
                raise ValueError("duplicate archive identifiers are ambiguous")
            input_keys.add(rid)
            cleaned = safe_record(
                raw,
                record_type="archive",
                record_id=rid,
                quarantine=[],
            )
            mapped = map_archive_memory(cleaned)
            if rid in existing:
                current = existing[rid]
                if not current.get("archived") or current.get("legacy_record") != cleaned:
                    raise ValueError(
                        "archive record key conflicts with an existing memory"
                    )
                duplicate_rows += 1
                continue
            board.setdefault("memories", []).append(mapped)
            appended.append(mapped)
            existing[rid] = mapped
        board["next_memory_seq"] = max(
            int(board.get("next_memory_seq", 1)), len(board["memories"]) + 1
        )
        if appended:
            _replace(store, target, board)
    return {
        "status": "complete" if appended else "noop",
        "board_id": board_id,
        "inserted": len(appended),
        "already_present": duplicate_rows,
        "record_keys": [item["memory_id"] for item in appended],
    }


def _phase_agents(source: Path, board: dict[str, Any], quarantine: list[dict[str, Any]]) -> None:
    agents = load_json(source / "agents.json", {})
    if not isinstance(agents, dict):
        raise ValueError("agents.json must contain an object")
    original_names = [
        str(value.get("agent_name") or "")
        for value in agents.values()
        if isinstance(value, dict)
    ]
    original_counts = Counter(original_names)
    board["legacy_import"]["ambiguous_agent_name_hashes"] = sorted(
        hashlib.sha256(name.encode("utf-8")).hexdigest()
        for name, count in original_counts.items()
        if count > 1
    )
    mapped: dict[str, Any] = {}
    report = []
    for record_id, raw in agents.items():
        rejected = False
        try:
            safe_record(
                str(record_id),
                record_type="agent",
                record_id=str(record_id),
                quarantine=quarantine,
                field="record_id",
            )
        except ScrubRejected:
            rejected = True
        try:
            item = safe_record(
                raw,
                record_type="agent",
                record_id=str(record_id),
                quarantine=quarantine,
            )
        except ScrubRejected:
            rejected = True
            item = None
        if rejected:
            continue
        assert isinstance(item, dict)
        item.update(
            {
                "legacy_self_asserted": True,
                "principal_id": None,
                "agent_id": None,
                "binding_status": "legacy_unbound",
            }
        )
        mapped[str(record_id)] = item
        report.append(
            {
                "legacy_record_id": str(record_id),
                "agent_name": item.get("agent_name"),
                "principal_binding": "legacy_unbound",
                "requires_operator_review": True,
            }
        )
    board["legacy_import"]["agents"] = mapped
    board["legacy_import"]["identity_mapping"] = report


def _phase_tickets(source: Path, board: dict[str, Any], quarantine: list[dict[str, Any]]) -> None:
    tickets: dict[str, Any] = {}
    for raw in source_tickets(source):
        rid = _ticket_legacy_id(raw)
        if rid in tickets:
            raise ValueError("duplicate legacy ticket identifiers are ambiguous")
        try:
            item = map_ticket(
                safe_record(
                    raw,
                    record_type="ticket",
                    record_id=rid,
                    quarantine=quarantine,
                )
            )
            tickets[item["ticket_id"]] = item
        except ScrubRejected:
            continue
    artifacts: dict[str, Any] = {}
    for path in sorted((source / "tickets").rglob("*.md")):
        relative = path.relative_to(source / "tickets").as_posix()
        try:
            safe_relative = safe_record(
                relative,
                record_type="ticket_artifact",
                record_id=relative,
                quarantine=quarantine,
                field="path",
            )
            artifacts[safe_relative] = safe_record(
                path.read_text(encoding="utf-8"),
                record_type="ticket_artifact",
                record_id=relative,
                quarantine=quarantine,
                field="content",
            )
        except ScrubRejected:
            continue
    board["tickets"] = tickets
    board["next_ticket_seq"] = len(tickets) + 1
    board["legacy_import"]["ticket_artifacts"] = artifacts


def _phase_state(source: Path, board: dict[str, Any], quarantine: list[dict[str, Any]]) -> None:
    try:
        raw_state = safe_record(
            load_json(source / "state.json", {}),
            record_type="state",
            record_id="state",
            quarantine=quarantine,
        )
        board["state"] = map_state(
            raw_state,
            board["legacy_import"]["owner_provisioned_at"],
        )
    except ScrubRejected:
        board["state"] = {}


def map_state(value: Any, imported_at: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError("state.json must contain an object")
    mapped: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        safe_key = safe_report_id(key)
        if safe_key != key:
            key = safe_key
        elif not CENTRAL_ID.fullmatch(key):
            key = "legacy-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        if key in mapped:
            raise ValueError("legacy state keys collide after normalization")
        rendered = (
            raw_value
            if isinstance(raw_value, str)
            else json.dumps(
                raw_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        mapped[key] = {
            "value": rendered,
            "scope": "project",
            "updated_at": imported_at,
            "updated_by_agent_id": None,
            "updated_by_principal_id": "legacy_unbound",
            "migration_provenance": "native-transactional-import",
            "legacy_value_type": type(raw_value).__name__,
        }
    return mapped


PHASE_MUTATORS: dict[str, Callable[[Path, dict[str, Any], list[dict[str, Any]]], None]] = {
    "memories": _phase_memories,
    "agents": _phase_agents,
    "tickets": _phase_tickets,
    "state": _phase_state,
}


def _mint_unique_generation_token(
    store: TransactionalSQLiteStore, factory: Callable[[], str]
) -> str:
    existing = {
        board["generation_token"]
        for board in store.iter_documents("boards")
        if isinstance(board, dict)
        and isinstance(board.get("generation_token"), str)
    }
    for _attempt in range(16):
        candidate = _validate_generation_token(factory())
        if candidate not in existing:
            return candidate
    raise RuntimeError("could not mint a unique generation_token")


def _validate_completed_generation(
    store: TransactionalSQLiteStore,
    board_doc_path: Path,
    import_doc_path: Path,
    board_id: str,
    owner_principal_id: str,
    owner_agent_name: str,
    provisioned_at: str,
) -> tuple[dict[str, Any], str, int]:
    manifest = store.load(import_doc_path, dict)
    if manifest.get("status") != "complete" or PHASES[-1] not in manifest.get(
        "completed_phases", []
    ):
        raise ValueError("import manifest is not complete")
    token = _validate_generation_token(manifest.get("generation_token"))
    revision = manifest.get("generation_revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("complete import has invalid generation_revision")
    board = store.load(board_doc_path, dict)
    if (
        board.get("generation_token") != token
        or type(board.get("generation_revision")) is not int
        or board.get("generation_revision") != revision
    ):
        raise ValueError("board and complete import generation do not match")
    membership = board.get("principal_memberships", {}).get(owner_principal_id)
    if (
        board.get("schema_version") != 6
        or membership is None
        or membership.get("role") != "admin"
        or membership.get("principal_id") != owner_principal_id
        or membership.get("source") != "offline_import_provisioning"
        or membership.get("created_by_principal_id") != owner_principal_id
        or membership.get("created_at") != provisioned_at
        or membership.get("updated_at") != provisioned_at
        or membership.get("admission_action")
        != "offline_import_owner_provisioned"
        or board.get("next_admission_revision") != 1
        or board.get("principal_revocations") != {}
        or board.get("invites") != {}
        or board.get("config")
        != {
            "claim_ttl_s": 900,
            "scrub_profile": "strict",
            "review_policy": "strict",
            "scrub_allow_counts": {},
        }
        or board.get("legacy_import", {}).get("owner_principal_id")
        != owner_principal_id
        or board.get("legacy_import", {}).get("owner_agent_name")
        != owner_agent_name
        or board.get("legacy_import", {}).get("owner_provisioned_at")
        != provisioned_at
        or manifest.get("owner_principal_id") != owner_principal_id
        or manifest.get("owner_agent_name") != owner_agent_name
        or manifest.get("owner_provisioned_at") != provisioned_at
    ):
        raise ValueError("complete import owner provisioning is invalid")
    journal = store.load(journal_path(store, board_id), dict)
    import_events = [
        row
        for row in journal.get("rows", [])
        if row.get("memory_id") == "import_completed"
    ]
    if len(import_events) != 1:
        raise ValueError("complete import must have exactly one import_completed event")
    event = import_events[0]
    if (
        event.get("generation_token") != token
        or type(event.get("generation_revision")) is not int
        or event.get("generation_revision") != revision
    ):
        raise ValueError("import_completed event generation does not match")
    return manifest, token, revision


def promote(
    source: Path,
    central_root: Path,
    board_id: str,
    *,
    central_url: str,
    promoted_board_root: Path,
    owner_principal_id: str,
    owner_agent_name: str,
    generation_token_factory: Callable[[], str] | None = None,
    owner_provisioned_at_factory: Callable[[], str] | None = None,
    before_phase_commit: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    source = _resolve_agent_mem_root(source, "source snapshot")
    central_root = central_root.resolve(strict=False)
    promoted_board_root = _resolve_agent_mem_root(
        promoted_board_root, "promoted_board_root"
    )
    if not BOARD_ID.fullmatch(board_id):
        raise ValueError("board_id must match [A-Za-z0-9._-]+")
    if not CENTRAL_ID.fullmatch(owner_principal_id):
        raise ValueError("owner_principal_id must match [A-Za-z0-9._-]{1,80}")
    if not CENTRAL_ID.fullmatch(owner_agent_name):
        raise ValueError("owner_agent_name must match [A-Za-z0-9._-]{1,80}")
    central_url = validate_central_url(central_url)
    if (source / "PROMOTED.json").exists() or (source / "PROMOTED.json").is_symlink():
        raise ValueError("source snapshot must not contain PROMOTED.json")
    _require_regular(source / ".board.lock", "source snapshot lock")
    _require_regular(source / "WRITE_FENCE.json", "source snapshot WRITE_FENCE.json")
    if _overlaps(source, central_root):
        raise ValueError("central data root must be outside the source snapshot")
    if _overlaps(source, promoted_board_root):
        raise ValueError("promoted_board_root must be distinct from the source snapshot")
    if _overlaps(central_root, promoted_board_root):
        raise ValueError("promoted_board_root must be outside the central data root")
    digest = source_hash(source)
    _validate_source_shape(source)
    store = TransactionalSQLiteStore(central_root)
    board_doc_path = board_path(store, board_id)
    import_doc_path = manifest_path(store, board_id)

    with store.transaction():
        existing_manifest = store.load(import_doc_path, dict)
        existing_board = store.load(board_doc_path, dict)
        if existing_manifest:
            if existing_manifest.get("source_hash") != digest:
                raise ValueError(
                    "target board exists with different source; --merge-into is out of scope"
                )
            if (
                existing_manifest.get("owner_principal_id") != owner_principal_id
                or existing_manifest.get("owner_agent_name") != owner_agent_name
            ):
                raise ValueError("target board owner differs from this import request")
            provisioned_at = existing_manifest.get("owner_provisioned_at")
            if not isinstance(provisioned_at, str) or not provisioned_at:
                raise ValueError("target board owner provisioning timestamp is invalid")
        elif existing_board:
            raise ValueError("target board exists without matching import manifest")
        else:
            provisioned_at = (owner_provisioned_at_factory or owner_provisioned_at)()
            if not isinstance(provisioned_at, str) or not provisioned_at:
                raise ValueError("owner provisioning timestamp is invalid")
            _replace(
                store,
                board_doc_path,
                default_board(
                    board_id,
                    owner_principal_id,
                    owner_agent_name,
                    provisioned_at,
                ),
            )
            _replace(
                store,
                import_doc_path,
                {
                    "board_id": board_id,
                    "source_hash": digest,
                    "status": "in_progress",
                    "completed_phases": [],
                    "backend": "sqlite",
                    "owner_principal_id": owner_principal_id,
                    "owner_agent_name": owner_agent_name,
                    "owner_provisioned_at": provisioned_at,
                },
            )

    changes = 0
    for phase in PHASES:
        with store.transaction():
            manifest = store.load(import_doc_path, dict)
            if phase in manifest.get("completed_phases", []):
                continue
            board = store.load(
                board_doc_path,
                lambda: default_board(
                    board_id,
                    owner_principal_id,
                    owner_agent_name,
                    provisioned_at,
                ),
            )
            quarantine = copy.deepcopy(board["legacy_import"].get("quarantine", []))
            if phase in PHASE_MUTATORS:
                PHASE_MUTATORS[phase](source, board, quarantine)
                board["legacy_import"]["quarantine"] = quarantine
                _replace(store, board_doc_path, board)
            else:
                token = _mint_unique_generation_token(
                    store, generation_token_factory or mint_generation_token
                )
                board["generation_token"] = token
                board["generation_revision"] = GENERATION_REVISION
                _replace(store, board_doc_path, board)
                _append_event(
                    store,
                    board_id,
                    actor="legacy-importer",
                    payload_ref=f"board://{board_id}/import",
                    memory_id_value="import_completed",
                    fixture_provenance="native transactional local-to-central import",
                    generation_token=token,
                    generation_revision=GENERATION_REVISION,
                )
            completed = list(manifest.get("completed_phases", []))
            completed.append(phase)
            manifest["completed_phases"] = completed
            if phase == PHASES[-1]:
                manifest["status"] = "complete"
                manifest["quarantine_count"] = len(quarantine)
                manifest["generation_token"] = token
                manifest["generation_revision"] = GENERATION_REVISION
            _replace(store, import_doc_path, manifest)
            if before_phase_commit is not None:
                before_phase_commit(phase)
            changes += 1

    with store.transaction():
        manifest, token, revision = _validate_completed_generation(
            store,
            board_doc_path,
            import_doc_path,
            board_id,
            owner_principal_id,
            owner_agent_name,
            provisioned_at,
        )
    marker = arm_promoted_marker(
        source,
        central_root,
        promoted_board_root,
        board_id,
        digest,
        central_url,
    )
    return {
        "status": "noop" if changes == 0 else manifest["status"],
        "changes": changes,
        "board_id": board_id,
        "source_hash": digest,
        "quarantined": int(manifest.get("quarantine_count", 0)),
        "backend": "sqlite",
        "generation_revision": revision,
        "generation_fingerprint": generation_fingerprint(token),
        "promoted_marker": marker,
    }


def canonical_db_dump(root: Path) -> bytes:
    database = root.resolve() / "board.sqlite3"
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT path, doc, version FROM documents ORDER BY path"
        ).fetchall()
    finally:
        connection.close()
    normalized = [
        {"path": path, "doc": json.loads(doc), "version": version}
        for path, doc, version in rows
    ]
    return (json.dumps(normalized, sort_keys=True, separators=(",", ":")) + "\n").encode()


def canonical_db_hash(root: Path) -> str:
    return hashlib.sha256(canonical_db_dump(root)).hexdigest()
