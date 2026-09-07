"""Private persisted-door state for the Pursers wait bridge."""

from __future__ import annotations

import base64
import fcntl
import hmac
import importlib
import ipaddress
import json
import os
import re
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
SEAT_ROLES = frozenset({"worker", "reviewer"})
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def default_state_dir(env: Mapping[str, str] | None = None) -> Path:
    selected = os.environ if env is None else env
    configured = selected.get("PURSERS_BRIDGE_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".pursers" / "wait-bridge"


def state_path(
    state_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    return (
        Path(state_dir).expanduser().resolve()
        if state_dir is not None
        else default_state_dir(env)
    ) / "doors.json"


def _decode_segment(value: str) -> dict[str, Any]:
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        document = json.loads(decoded)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("door is not valid base64url JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("door payload must be a JSON object")
    return document


def _fallback_decode_door(value: str) -> dict[str, Any]:
    """Decode the fixed envelope while the issuer dependency is not installed."""
    prefix, separator, encoded = value.partition(".")
    if prefix != "prs1" or not separator or not encoded or "." in encoded:
        raise ValueError("door must use the prs1.<base64url-json> format")
    return _decode_segment(encoded)


def _door_admin_decode(value: str) -> dict[str, Any]:
    """Use the issuer's public decoder, accepting its bounded result shapes."""
    envelope = _fallback_decode_door(value)
    try:
        module = importlib.import_module("door_admin")
    except ImportError:
        return envelope
    decoder = getattr(module, "decode_door", None) or getattr(
        module, "parse_door", None
    )
    if decoder is None:
        raise ValueError("door_admin has no supported decoder")
    try:
        decoded = decoder(value)
    except Exception as exc:  # door_admin exposes a secret-safe domain error.
        raise ValueError(str(exc)) from None
    if hasattr(decoded, "to_dict"):
        decoded = decoded.to_dict()
    elif not isinstance(decoded, dict) and hasattr(decoded, "__dict__"):
        decoded = vars(decoded)
    if not isinstance(decoded, dict):
        raise ValueError("door_admin returned an invalid door payload")
    nested = decoded.get("claims") or decoded.get("payload")
    validated = dict(nested) if isinstance(nested, dict) else dict(decoded)
    for field in ("u", "b", "r"):
        if field in validated and validated[field] != envelope.get(field):
            raise ValueError(f"door_admin returned mismatched {field}")
    kid, exp = _jwt_metadata(str(envelope.get("t") or ""))
    if validated.get("kid", kid) != kid or validated.get("exp", exp) != exp:
        raise ValueError("door_admin returned mismatched token metadata")
    return envelope


def _jwt_metadata(token: str) -> tuple[str, int]:
    segments = token.split(".")
    if len(segments) != 3:
        raise ValueError("door token must be a compact JWT")
    header = _decode_segment(segments[0])
    claims = _decode_segment(segments[1])
    kid = header.get("kid")
    exp = claims.get("exp")
    if not isinstance(kid, str) or not kid.strip():
        raise ValueError("door token header must contain kid")
    if type(exp) is not int:
        raise ValueError("door token claims must contain integer exp")
    return kid, exp


def decode_door(value: str) -> dict[str, Any]:
    decoded = _door_admin_decode(value)
    expected = {"u", "b", "r", "t"}
    if set(decoded) != expected:
        raise ValueError("door payload must contain exactly u, b, r, and t")
    url = decoded["u"]
    board = decoded["b"]
    role = decoded["r"]
    token = decoded["t"]
    if not all(
        isinstance(item, str) and item.strip()
        for item in (url, board, role, token)
    ):
        raise ValueError("door fields must be non-empty strings")
    normalized_role = role.strip().lower()
    if normalized_role not in SEAT_ROLES:
        raise ValueError("door role is not supported")
    kid, exp = _jwt_metadata(token)
    return {
        "u": url.strip(),
        "b": board.strip(),
        "r": normalized_role,
        "t": token.strip(),
        "kid": kid,
        "exp": exp,
    }


def is_loopback_url(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_url(value: str, *, allow_remote: bool) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("door URL must be an http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("door URL must not include user information")
    if not is_loopback_url(value) and not allow_remote:
        raise ValueError("remote door URL requires --allow-remote confirmation")


def _empty_document() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "doors": []}


def _validate_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("doors.json has an unsupported schema")
    raw_entries = value.get("doors")
    if not isinstance(raw_entries, list):
        raise ValueError("doors.json doors must be a list")
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError("doors.json contains an invalid entry")
        required = {
            "u",
            "b",
            "r",
            "t",
            "kid",
            "exp",
            "joined_at",
            "seat_names_used",
        }
        if set(raw) != required:
            raise ValueError("doors.json entry has invalid fields")
        decoded = decode_door(_encode_for_validation(raw))
        if decoded["kid"] != raw["kid"] or decoded["exp"] != raw["exp"]:
            raise ValueError("doors.json token metadata does not match the token")
        if not isinstance(raw["joined_at"], str) or not raw["joined_at"]:
            raise ValueError("doors.json joined_at must be a timestamp")
        names = raw["seat_names_used"]
        if not isinstance(names, list) or not all(
            isinstance(name, str) and name for name in names
        ) or len(names) != len(set(names)):
            raise ValueError("doors.json seat_names_used must be unique strings")
        key = (decoded["b"], decoded["r"])
        if key in seen:
            raise ValueError("doors.json has duplicate board/role entries")
        seen.add(key)
        entries.append(
            {
                **decoded,
                "joined_at": raw["joined_at"],
                "seat_names_used": list(names),
            }
        )
    return {"schema_version": SCHEMA_VERSION, "doors": entries}


def _encode_for_validation(entry: Mapping[str, Any]) -> str:
    envelope = {key: entry[key] for key in ("u", "b", "r", "t")}
    encoded = base64.urlsafe_b64encode(
        json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"prs1.{encoded}"


def load(path: str | Path) -> dict[str, Any]:
    selected = Path(path)
    try:
        raw = json.loads(selected.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_document()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("doors.json is unreadable or invalid") from exc
    return _validate_document(raw)


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
        path.chmod(0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _mutate(path: Path, callback: Any) -> Any:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        document = load(path)
        result = callback(document)
        _atomic_write(path, document)
        return result
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def store(
    path: str | Path,
    door: str,
    *,
    rotate: bool = False,
    allow_remote: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    entry = decode_door(door)
    validate_url(entry["u"], allow_remote=allow_remote)
    selected_path = Path(path)

    def update(document: dict[str, Any]) -> dict[str, Any]:
        existing = next(
            (
                item
                for item in document["doors"]
                if item["b"] == entry["b"] and item["r"] == entry["r"]
            ),
            None,
        )
        if rotate and existing is None:
            raise ValueError(
                "--rotate requires an existing door for the same board and role"
            )
        if (
            existing is not None
            and not rotate
            and not hmac.compare_digest(existing["t"], entry["t"])
        ):
            raise ValueError(
                "a door already exists for this board and role; use --rotate"
            )
        names = list(existing.get("seat_names_used", [])) if existing else []
        joined_at = (now or datetime.now(timezone.utc)).isoformat()
        replacement = {**entry, "joined_at": joined_at, "seat_names_used": names}
        if existing is None:
            document["doors"].append(replacement)
        else:
            document["doors"][document["doors"].index(existing)] = replacement
        document["doors"].sort(key=lambda item: (item["b"], item["r"]))
        return dict(replacement)

    return _mutate(selected_path, update)


def forget(path: str | Path, board: str, role: str) -> bool:
    selected_path = Path(path)

    def update(document: dict[str, Any]) -> bool:
        before = len(document["doors"])
        document["doors"] = [
            item
            for item in document["doors"]
            if (item["b"], item["r"]) != (board, role)
        ]
        return len(document["doors"]) != before

    return bool(_mutate(selected_path, update))


def select(
    document: Mapping[str, Any],
    *,
    board: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    entries = list(document.get("doors", []))
    if board:
        entries = [item for item in entries if item["b"] == board]
    elif len({item["b"] for item in entries}) > 1:
        raise ValueError("multiple stored boards; set ONBOARD_BOARD_ID")
    if role:
        entries = [item for item in entries if item["r"] == role]
    elif len({item["r"] for item in entries}) > 1:
        raise ValueError("multiple stored roles; set PURSERS_ROLE")
    if not entries:
        raise ValueError("no stored door matches the selected board and role")
    if len(entries) != 1:
        raise ValueError("stored door selection is ambiguous")
    return dict(entries[0])


def reserve_name(
    path: str | Path,
    board: str,
    role: str,
    requested: str | None = None,
) -> str:
    selected_path = Path(path)

    def update(document: dict[str, Any]) -> str:
        entry = select(document, board=board, role=role)
        names = entry["seat_names_used"]
        if requested:
            name = requested
            if not NAME_RE.fullmatch(name):
                raise ValueError("seat name must be a safe 1-80 character identifier")
        else:
            host = socket.gethostname().split(".", 1)[0].strip().lower() or "host"
            safe_host = "".join(
                character if character.isalnum() or character in "-_" else "-"
                for character in host
            ).strip("-") or "host"
            safe_host = safe_host[: max(1, 72 - len(role))].rstrip("-") or "host"
            suffix = 1
            while f"{role}-{safe_host}-{suffix}" in names:
                suffix += 1
            name = f"{role}-{safe_host}-{suffix}"
        if name not in names:
            names.append(name)
        return name

    return str(_mutate(selected_path, update))


def resolve(env: Mapping[str, str] | None = None) -> dict[str, str]:
    selected = os.environ if env is None else env
    direct_token = selected.get("ONBOARD_CENTRAL_TOKEN", "").strip()
    token_file = selected.get("ONBOARD_CENTRAL_TOKEN_FILE", "").strip()
    board = selected.get("ONBOARD_BOARD_ID", "").strip() or None
    role = selected.get("PURSERS_ROLE", "").strip().lower() or None
    needs_stored = not (
        selected.get("ONBOARD_CENTRAL_URL", "").strip()
        and (direct_token or token_file)
        and board
        and role
    )
    stored: dict[str, Any] = {}
    if needs_stored:
        document = load(state_path(env=selected))
        if document["doors"]:
            stored = select(document, board=board, role=role)
    if not direct_token and token_file:
        try:
            direct_token = (
                Path(token_file).expanduser().read_text(encoding="utf-8").strip()
            )
        except OSError as exc:
            raise ValueError("ONBOARD_CENTRAL_TOKEN_FILE is not readable") from exc
    return {
        "url": selected.get("ONBOARD_CENTRAL_URL", "").strip()
        or str(stored.get("u") or "http://127.0.0.1:8766/mcp"),
        "token": direct_token or str(stored.get("t") or ""),
        "board": board or str(stored.get("b") or "pursers"),
        "role": role or str(stored.get("r") or ""),
    }
