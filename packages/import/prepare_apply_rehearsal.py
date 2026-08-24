#!/usr/bin/env python3
"""Prepare two private, fenced copies from a lock-bounded live-board snapshot."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Callable

if __package__:
    from .safe_tree import (
        copy_tree_fd,
        open_directory_nofollow,
        require_path_matches_descriptor,
        walk_tree_fd,
    )
else:
    from safe_tree import (
        copy_tree_fd,
        open_directory_nofollow,
        require_path_matches_descriptor,
        walk_tree_fd,
    )


IMPORT_DOMAIN_ROOTS = {
    ".board.lock",
    "agents.json",
    "archive.json",
    "memories.json",
    "project.json",
    "state.json",
    "tickets",
}


def _is_import_domain(relative: Path) -> bool:
    return bool(relative.parts) and relative.parts[0] in IMPORT_DOMAIN_ROOTS


def _tree_state(
    root: Path,
    *,
    import_domain_only: bool = False,
    _descriptor: int | None = None,
) -> tuple[str, str, int, int]:
    content = hashlib.sha256()
    state = hashlib.sha256()
    count = 0
    size = 0
    owned_descriptor = _descriptor is None
    descriptor = _descriptor if _descriptor is not None else open_directory_nofollow(root)
    try:
        root_info = os.fstat(descriptor)
        entries = walk_tree_fd(descriptor, reject_links=True)
        if owned_descriptor:
            require_path_matches_descriptor(root, descriptor)
    finally:
        if owned_descriptor:
            os.close(descriptor)
    # Bind the root even when the import-domain view intentionally filters
    # top-level children. Directory sizes and link counts are filesystem
    # allocation details, so bind stable path/type/mode/mtime plus the complete
    # entry inventory instead.
    state.update(b".\0D\0")
    state.update(str(root_info.st_mtime_ns).encode())
    state.update(b"\0")
    state.update(str(stat.S_IMODE(root_info.st_mode)).encode())
    state.update(b"\0")
    for relative, kind, metadata, payload in entries:
        relative_path = Path(relative)
        if import_domain_only and not _is_import_domain(relative_path):
            continue
        state.update(relative.encode())
        state.update(b"\0")
        state.update(kind.encode("ascii"))
        state.update(b"\0")
        state.update(str(metadata.st_mtime_ns).encode())
        state.update(b"\0")
        state.update(str(stat.S_IMODE(metadata.st_mode)).encode())
        state.update(b"\0")
        if kind == "D":
            continue
        content.update(relative.encode())
        content.update(b"\0")
        content.update(payload)
        content.update(b"\0")
        state.update(str(metadata.st_nlink).encode())
        state.update(b"\0")
        state.update(payload)
        state.update(b"\0")
        count += 1
        size += len(payload)
    return content.hexdigest(), state.hexdigest(), count, size


def _harden(root: Path) -> None:
    os.chmod(root, 0o700)
    for path in root.rglob("*"):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def _write_private_json(path: Path, value: Any) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if path.exists() or path.is_symlink():
        raise FileExistsError("private proof target already exists")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def prepare(
    source: Path,
    run_root: Path,
    board_id: str,
    *,
    _race_hook: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    lexical_source = Path(os.path.abspath(os.fspath(source)))
    if lexical_source.resolve(strict=True) != lexical_source:
        raise ValueError("live source must not traverse a symlink")
    source = lexical_source
    run_root = Path(os.path.abspath(os.fspath(run_root)))
    run_parent = run_root.parent
    if run_parent.resolve(strict=True) != run_parent:
        raise ValueError("run root parent must not traverse a symlink")
    parent_info = run_parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.geteuid()
        or stat.S_IMODE(parent_info.st_mode) & 0o022
    ):
        raise ValueError("run root parent must be an owned non-writable directory")
    if source.name != ".agent-mem" or not (source / "memories.json").is_file():
        raise ValueError("source must be a live .agent-mem directory")
    if run_root.exists():
        raise FileExistsError(f"run root exists: {run_root}")
    if run_root.is_relative_to(source) or source.is_relative_to(run_root):
        raise ValueError("run root must be outside the live source")
    if (source / "PROMOTED.json").exists() or (source / "WRITE_FENCE.json").exists():
        raise ValueError("live source must remain active and unfenced")

    run_root.mkdir(mode=0o700)
    os.chmod(run_root, 0o700)
    source_copy = run_root / "source-snapshot" / ".agent-mem"
    source_copy.parent.mkdir(mode=0o700)
    full_backup = run_root / "full-source-backup" / ".agent-mem"
    full_backup.parent.mkdir(mode=0o700)
    source_descriptor = open_directory_nofollow(source)
    lock_descriptor = os.open(
        ".board.lock",
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=source_descriptor,
    )
    lock_stat = os.fstat(lock_descriptor)
    if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
        os.close(lock_descriptor)
        raise ValueError("live source .board.lock must be a private regular file")
    try:
        with os.fdopen(lock_descriptor, "rb", closefd=True) as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            require_path_matches_descriptor(source, source_descriptor)
            current_lock = os.stat(
                ".board.lock", dir_fd=source_descriptor, follow_symlinks=False
            )
            if (
                current_lock.st_dev != lock_stat.st_dev
                or current_lock.st_ino != lock_stat.st_ino
            ):
                raise RuntimeError("live source lock changed while acquiring it")
            before_full = _tree_state(source, _descriptor=source_descriptor)
            before = _tree_state(
                source, import_domain_only=True, _descriptor=source_descriptor
            )
            copy_tree_fd(
                source_descriptor, full_backup, hook=_race_hook
            )
            copy_tree_fd(
                source_descriptor,
                source_copy,
                top_level_names=IMPORT_DOMAIN_ROOTS,
                hook=_race_hook,
            )
            copied_full = _tree_state(full_backup)
            copied = _tree_state(source_copy)
            after_full = _tree_state(source, _descriptor=source_descriptor)
            after = _tree_state(
                source, import_domain_only=True, _descriptor=source_descriptor
            )
            require_path_matches_descriptor(source, source_descriptor)
            current_lock = os.stat(
                ".board.lock", dir_fd=source_descriptor, follow_symlinks=False
            )
            if (
                current_lock.st_dev != lock_stat.st_dev
                or current_lock.st_ino != lock_stat.st_ino
            ):
                raise RuntimeError("live source lock changed during snapshot")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        os.close(source_descriptor)
    if (
        before_full != copied_full
        or before_full != after_full
        or before != copied
        or before != after
    ):
        raise RuntimeError("live source changed during snapshot or copy differs")

    fence = {
        "schema_version": 1,
        "board_id": board_id,
        "reason": "quarantine-apply-rehearsal",
        "snapshot_source_hash": before[0],
    }
    _write_private_json(source_copy / "WRITE_FENCE.json", fence)
    _harden(source_copy)
    _harden(full_backup)
    promoted_copy = run_root / "promoted-board" / ".agent-mem"
    promoted_copy.parent.mkdir(mode=0o700)
    source_copy_descriptor = open_directory_nofollow(source_copy)
    try:
        copy_tree_fd(source_copy_descriptor, promoted_copy)
    finally:
        os.close(source_copy_descriptor)
    _harden(promoted_copy)
    snapshot_copy_state = _tree_state(source_copy)
    full_backup_copy_state = _tree_state(full_backup)
    promoted_copy_state = _tree_state(promoted_copy)
    if snapshot_copy_state != promoted_copy_state:
        raise RuntimeError("fenced rehearsal copies differ")
    final_live = _tree_state(source, import_domain_only=True)
    post_snapshot_state = "unchanged" if final_live == before else "external-drift-after-unlock"

    result = {
        "status": "complete",
        "board_id": board_id,
        "source_snapshot": str(source_copy),
        "full_source_backup": str(full_backup),
        "promoted_board_root": str(promoted_copy),
        "full_live_content_sha256": before_full[0],
        "full_live_state_sha256": before_full[1],
        "full_files": before_full[2],
        "full_bytes": before_full[3],
        "live_content_sha256": before[0],
        "live_state_sha256": before[1],
        "files": before[2],
        "bytes": before[3],
        "live_source_write": "none",
        "live_source_mtimes_during_lock": "unchanged",
        "post_snapshot_live_state": post_snapshot_state,
        "symlinks": "rejected",
        "snapshot_scope": "native-import-domain",
        "private_modes": "dirs=0700,files=0600",
        "completed_copy_seals": {
            "source_snapshot": list(snapshot_copy_state),
            "full_source_backup": list(full_backup_copy_state),
            "promoted_board": list(promoted_copy_state),
        },
    }
    _write_private_json(run_root / "snapshot-proof.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--board-id", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.run_root, args.board_id), sort_keys=True))


if __name__ == "__main__":
    main()
