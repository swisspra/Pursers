"""Private request-state keyring loading for MCP multi-round trips."""

from __future__ import annotations

import fcntl
import os
import secrets
import stat
from pathlib import Path


REQUEST_STATE_TTL_S = 3600.0
"""One hour per round: long enough for an interactive human interruption."""


def load_or_create_request_state_keys(path: str | Path) -> list[bytes]:
    """Load a private newline-delimited keyring, creating its first key safely.

    The first line seals new state and every line may unseal old state. Each
    line is used verbatim as key material and must contain at least 32 bytes.
    """

    key_path = Path(path).expanduser()
    if not key_path.is_absolute():
        key_path = Path.cwd() / key_path
    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_descriptor = os.open(
        key_path.with_name(key_path.name + ".lock"),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        if not key_path.exists():
            descriptor = os.open(
                key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                os.write(
                    descriptor,
                    secrets.token_urlsafe(32).encode("ascii") + b"\n",
                )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)

    info = key_path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("request-state key path must be a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("request-state key file permissions must be 0600")
    try:
        raw_keys = [line.strip() for line in key_path.read_bytes().splitlines()]
    except OSError as exc:
        raise ValueError("request-state key file is not readable") from exc
    keys = [value for value in raw_keys if value]
    if not keys:
        raise ValueError("request-state key file must contain at least one key")
    if any(len(value) < 32 for value in keys):
        raise ValueError("every request-state key must contain at least 32 bytes")
    return keys


__all__ = ["REQUEST_STATE_TTL_S", "load_or_create_request_state_keys"]
