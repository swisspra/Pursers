"""Persistent, non-secret identity for one Central data directory."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INSTANCE_IDENTITY_NAME = "central-instance.json"
INSTANCE_ID_RE = re.compile(r"^CI-[0-9a-f]{64}$")
INSTANCE_IDENTITY_SCHEMA_VERSION = 1


class CentralInstanceIdentityError(RuntimeError):
    """Raised when a Central instance identity cannot be trusted."""


def _read_identity(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CentralInstanceIdentityError(
                "Central instance identity must be a regular single-link file"
            )
        document = json.loads(path.read_text(encoding="utf-8"))
    except CentralInstanceIdentityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CentralInstanceIdentityError(
            "Central instance identity is unreadable or invalid"
        ) from exc
    if not isinstance(document, dict):
        raise CentralInstanceIdentityError("Central instance identity must be an object")
    if document.get("schema_version") != INSTANCE_IDENTITY_SCHEMA_VERSION:
        raise CentralInstanceIdentityError("unsupported Central instance identity schema")
    instance_id = document.get("instance_id")
    if not isinstance(instance_id, str) or not INSTANCE_ID_RE.fullmatch(instance_id):
        raise CentralInstanceIdentityError("Central instance_id is invalid")
    created_at = document.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise CentralInstanceIdentityError("Central instance created_at is invalid")
    return document


def _write_identity(path: Path, document: dict[str, Any]) -> None:
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise CentralInstanceIdentityError(
            "cannot persist Central instance identity"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _new_identity(*, predecessor: str | None = None) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": INSTANCE_IDENTITY_SCHEMA_VERSION,
        "instance_id": "CI-" + secrets.token_hex(32),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if predecessor is not None:
        document["forked_from_sha256"] = hashlib.sha256(
            predecessor.encode("ascii")
        ).hexdigest()
    return document


def ensure_central_instance_identity(data_dir: Path) -> str:
    """Return the stable ID, creating it once for new or legacy stores."""
    root = data_dir.expanduser().absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise CentralInstanceIdentityError("Central data directory must be a directory")
    lock_path = root / ".central-instance.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        path = root / INSTANCE_IDENTITY_NAME
        if os.path.lexists(path):
            return str(_read_identity(path)["instance_id"])
        document = _new_identity()
        _write_identity(path, document)
        return str(document["instance_id"])
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def fork_central_instance_identity(data_dir: Path) -> tuple[str, str]:
    """Give an offline live-store clone a new identity; backups must not call this."""
    root = data_dir.expanduser().absolute()
    ensure_central_instance_identity(root)
    lock_fd = os.open(
        root / ".central-instance.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        current = str(_read_identity(root / INSTANCE_IDENTITY_NAME)["instance_id"])
        document = _new_identity(predecessor=current)
        _write_identity(root / INSTANCE_IDENTITY_NAME, document)
        return current, str(document["instance_id"])
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
