"""Process-wide data-directory lock for the Central runtime and offline tools."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType


LOCK_FILE = "central.live.lock"


class CentralDataLock:
    """Hold an exclusive advisory lock for one Central data directory."""

    def __init__(self, data_dir: str | Path, *, create: bool = True):
        self.data_dir = Path(data_dir).resolve()
        self.create = create
        self._descriptor: int | None = None

    def __enter__(self) -> "CentralDataLock":
        lock_path = self.data_dir / LOCK_FILE
        if self.create:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            flags = os.O_RDWR | os.O_CREAT
        else:
            if not lock_path.exists():
                return self
            flags = os.O_RDWR
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise RuntimeError(
                "central data directory is live; stop the server before offline maintenance"
            ) from exc
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
