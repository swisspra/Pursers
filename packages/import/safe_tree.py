"""Descriptor-relative tree reads and copies that never follow filesystem links."""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path
from typing import Callable, Iterator


RaceHook = Callable[[str, str], None]
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TREE_BYTES = 256 * 1024 * 1024
MAX_TREE_ENTRIES = 100_000
_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _same_file_state(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _same_object(first, second)
        and first.st_mode == second.st_mode
        and first.st_nlink == second.st_nlink
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def open_directory_nofollow(path: Path) -> int:
    """Open every absolute path component with O_NOFOLLOW."""
    path = _absolute(path)
    descriptor = os.open("/", _DIR_FLAGS)
    try:
        for component in path.parts[1:]:
            if component in {"", ".", ".."}:
                raise ValueError("directory path is not canonical")
            next_descriptor = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("tree root must be a real directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def require_path_matches_descriptor(path: Path, descriptor: int) -> None:
    """Fail if an opened directory was renamed or replaced at its lexical path."""
    check = open_directory_nofollow(path)
    try:
        if not _same_object(os.fstat(descriptor), os.fstat(check)):
            raise RuntimeError("tree root changed while it was open")
    finally:
        os.close(check)


@contextlib.contextmanager
def opened_directory_nofollow(path: Path) -> Iterator[int]:
    descriptor = open_directory_nofollow(path)
    try:
        yield descriptor
        require_path_matches_descriptor(path, descriptor)
    finally:
        os.close(descriptor)


def _read_regular(
    parent_descriptor: int,
    name: str,
    relative: str,
    before: os.stat_result,
    *,
    reject_hardlinks: bool,
    hook: RaceHook | None,
) -> bytes:
    if before.st_size > MAX_FILE_BYTES:
        raise ValueError("tree regular file exceeds the supported size bound")
    if hook is not None:
        hook("after-entry-stat", relative)
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _same_object(before, opened)
            or (reject_hardlinks and opened.st_nlink != 1)
        ):
            raise ValueError("tree regular-file identity changed during traversal")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        entry_after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            len(payload) != opened.st_size
            or not _same_file_state(opened, after)
            or not _same_file_state(opened, entry_after)
        ):
            raise RuntimeError("tree regular file changed during traversal")
        return payload
    finally:
        os.close(descriptor)


def walk_tree_fd(
    root_descriptor: int,
    *,
    reject_links: bool,
    hook: RaceHook | None = None,
) -> list[tuple[str, str, os.stat_result, bytes]]:
    """Return a stable, sorted descriptor-relative tree snapshot.

    Entries are ``(relative, kind, stat, payload)``. Directory payloads are empty;
    symlink payloads contain the encoded target and are only allowed when requested.
    """
    entries: list[tuple[str, str, os.stat_result, bytes]] = []
    total_bytes = 0

    def walk(directory_descriptor: int, prefix: str) -> None:
        nonlocal total_bytes
        directory_before = os.fstat(directory_descriptor)
        for name in sorted(os.listdir(directory_descriptor)):
            if name in {"", ".", ".."} or "/" in name:
                raise ValueError("tree contains an invalid entry name")
            relative = f"{prefix}/{name}" if prefix else name
            before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if len(entries) >= MAX_TREE_ENTRIES:
                raise ValueError("tree exceeds the supported entry-count bound")
            if stat.S_ISLNK(before.st_mode):
                if reject_links:
                    raise ValueError(f"tree contains symlink: {relative}")
                if hook is not None:
                    hook("after-entry-stat", relative)
                target = os.readlink(name, dir_fd=directory_descriptor)
                after = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
                if not _same_file_state(before, after):
                    raise RuntimeError("tree symlink changed during traversal")
                entries.append((relative, "L", before, os.fsencode(target)))
            elif stat.S_ISDIR(before.st_mode):
                if hook is not None:
                    hook("after-entry-stat", relative)
                child_descriptor = os.open(name, _DIR_FLAGS, dir_fd=directory_descriptor)
                try:
                    opened = os.fstat(child_descriptor)
                    if not _same_object(before, opened):
                        raise RuntimeError("tree directory changed during traversal")
                    entries.append((relative, "D", opened, b""))
                    walk(child_descriptor, relative)
                    after = os.fstat(child_descriptor)
                    entry_after = os.stat(
                        name, dir_fd=directory_descriptor, follow_symlinks=False
                    )
                    if (
                        not _same_file_state(opened, after)
                        or not _same_file_state(opened, entry_after)
                    ):
                        raise RuntimeError("tree directory changed during traversal")
                finally:
                    os.close(child_descriptor)
            elif stat.S_ISREG(before.st_mode):
                if reject_links and before.st_nlink != 1:
                    raise ValueError(f"tree contains hard-linked file: {relative}")
                payload = _read_regular(
                    directory_descriptor,
                    name,
                    relative,
                    before,
                    reject_hardlinks=reject_links,
                    hook=hook,
                )
                total_bytes += len(payload)
                if total_bytes > MAX_TREE_BYTES:
                    raise ValueError("tree exceeds the supported byte-size bound")
                entries.append((relative, "F", before, payload))
            else:
                raise ValueError(f"tree contains special entry: {relative}")
        directory_after = os.fstat(directory_descriptor)
        if not _same_file_state(directory_before, directory_after):
            raise RuntimeError("tree directory changed during traversal")

    walk(root_descriptor, "")
    return entries


def copy_tree_fd(
    source_descriptor: int,
    destination: Path,
    *,
    top_level_names: set[str] | None = None,
    hook: RaceHook | None = None,
) -> None:
    """Copy a stable source tree without following source or destination links."""
    destination = _absolute(destination)
    source_root = os.fstat(source_descriptor)
    os.mkdir(destination, stat.S_IMODE(source_root.st_mode))
    destination_descriptor = open_directory_nofollow(destination)
    copied_entries = 0
    copied_bytes = 0

    def copy_directory(
        source_directory: int, destination_directory: int, prefix: str
    ) -> None:
        nonlocal copied_entries, copied_bytes
        source_before = os.fstat(source_directory)
        for name in sorted(os.listdir(source_directory)):
            relative = f"{prefix}/{name}" if prefix else name
            if top_level_names is not None and not prefix and name not in top_level_names:
                continue
            before = os.stat(name, dir_fd=source_directory, follow_symlinks=False)
            copied_entries += 1
            if copied_entries > MAX_TREE_ENTRIES:
                raise ValueError("tree exceeds the supported entry-count bound")
            if stat.S_ISLNK(before.st_mode):
                raise ValueError(f"live board contains symlink: {relative}")
            if stat.S_ISDIR(before.st_mode):
                if hook is not None:
                    hook("after-entry-stat", relative)
                source_child = os.open(name, _DIR_FLAGS, dir_fd=source_directory)
                try:
                    opened = os.fstat(source_child)
                    if not _same_object(before, opened):
                        raise RuntimeError("live directory changed during copy")
                    os.mkdir(name, stat.S_IMODE(opened.st_mode), dir_fd=destination_directory)
                    destination_child = os.open(name, _DIR_FLAGS, dir_fd=destination_directory)
                    try:
                        copy_directory(source_child, destination_child, relative)
                        os.fchmod(destination_child, stat.S_IMODE(opened.st_mode))
                        os.utime(
                            destination_child,
                            ns=(opened.st_atime_ns, opened.st_mtime_ns),
                        )
                        os.fsync(destination_child)
                    finally:
                        os.close(destination_child)
                    after = os.fstat(source_child)
                    entry_after = os.stat(
                        name, dir_fd=source_directory, follow_symlinks=False
                    )
                    if (
                        not _same_file_state(opened, after)
                        or not _same_file_state(opened, entry_after)
                    ):
                        raise RuntimeError("live directory changed during copy")
                finally:
                    os.close(source_child)
            elif stat.S_ISREG(before.st_mode):
                if before.st_nlink != 1:
                    raise ValueError(f"live board contains hard-linked file: {relative}")
                payload = _read_regular(
                    source_directory,
                    name,
                    relative,
                    before,
                    reject_hardlinks=True,
                    hook=hook,
                )
                copied_bytes += len(payload)
                if copied_bytes > MAX_TREE_BYTES:
                    raise ValueError("tree exceeds the supported byte-size bound")
                output = os.open(
                    name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    stat.S_IMODE(before.st_mode),
                    dir_fd=destination_directory,
                )
                try:
                    view = memoryview(payload)
                    while view:
                        written = os.write(output, view)
                        view = view[written:]
                    os.fchmod(output, stat.S_IMODE(before.st_mode))
                    os.utime(output, ns=(before.st_atime_ns, before.st_mtime_ns))
                    os.fsync(output)
                finally:
                    os.close(output)
            else:
                raise ValueError(f"live board contains special entry: {relative}")
        source_after = os.fstat(source_directory)
        if not _same_file_state(source_before, source_after):
            raise RuntimeError("live directory changed during copy")

    try:
        copy_directory(source_descriptor, destination_descriptor, "")
        os.fchmod(destination_descriptor, stat.S_IMODE(source_root.st_mode))
        os.utime(
            destination_descriptor,
            ns=(source_root.st_atime_ns, source_root.st_mtime_ns),
        )
        os.fsync(destination_descriptor)
    finally:
        os.close(destination_descriptor)
