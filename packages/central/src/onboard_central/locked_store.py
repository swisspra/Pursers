"""Per-file flocked JSON store prototype with one public mutation primitive."""

from __future__ import annotations

import abc
import asyncio
import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

try:
    import fcntl
except ImportError:  # pragma: no cover - the spike targets the deployed macOS/Linux shape
    fcntl = None


T = TypeVar("T")
Mutator = Callable[[T], T | None]
DefaultFactory = Callable[[], T] | T


class Store(abc.ABC, Generic[T]):
    """A1 seam. A DB backend can implement the same atomic document mutation API."""

    @abc.abstractmethod
    def path(self, *parts: str | Path) -> Path:
        raise NotImplementedError

    @abc.abstractmethod
    def load(self, path: str | Path, default: DefaultFactory[T]) -> T:
        raise NotImplementedError

    @abc.abstractmethod
    def read_modify_write(
        self, path: str | Path, mutate_fn: Mutator[T], default: DefaultFactory[T]
    ) -> T:
        raise NotImplementedError

    async def aread_modify_write(
        self, path: str | Path, mutate_fn: Mutator[T], default: DefaultFactory[T]
    ) -> T:
        return await asyncio.to_thread(self.read_modify_write, path, mutate_fn, default)


class LockedJsonStore(Store[Any]):
    """JSON store whose only public write is a locked read-modify-write."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, *parts: str | Path) -> Path:
        if not parts:
            return self.root
        if any(Path(part).is_absolute() for part in parts):
            raise ValueError("store paths must be relative")
        candidate = self.root.joinpath(*parts).resolve(strict=False)
        if not candidate.is_relative_to(self.root):
            raise ValueError("store path escapes root")
        return candidate

    def _target(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            return self.path(candidate)
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise ValueError("store path escapes root")
        return resolved

    @staticmethod
    def _fresh_default(default: DefaultFactory[T]) -> T:
        return default() if callable(default) else copy.deepcopy(default)

    def load(self, path: str | Path, default: DefaultFactory[T]) -> T:
        target = self._target(path)
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._fresh_default(default)

    @staticmethod
    def _lock_path(target: Path) -> Path:
        return target.with_name(f"{target.name}.lock")

    def _before_replace(self, temp: Path, target: Path) -> None:
        """Crash-test hook after temp fsync and before atomic rename."""

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _atomic_write(self, target: Path, value: Any) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(
            f".{target.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._before_replace(temp, target)
            os.replace(temp, target)
            self._fsync_directory(target.parent)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def read_modify_write(
        self, path: str | Path, mutate_fn: Mutator[T], default: DefaultFactory[T]
    ) -> T:
        if fcntl is None:
            raise RuntimeError("LockedJsonStore requires fcntl.flock")
        target = self._target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path(target)
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            current = self.load(target, default)
            replacement = mutate_fn(current)
            updated = current if replacement is None else replacement
            self._atomic_write(target, updated)
            return updated
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
