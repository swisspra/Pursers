#!/usr/bin/env python3
"""Safely inspect and optionally remove explicitly named temporary roots.

The command is a dry run unless ``--delete`` is supplied.  It fails closed when
it cannot prove that a candidate is old and unused.
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


OWNER_FILE = ".pursers-tmp-owner-pid"
LSOF_EXECUTABLE = shutil.which("lsof") or "/usr/sbin/lsof"
TEMP_ENV_PATTERN = re.compile(
    r"(?:^|\s)(?:TMPDIR|TMP|TEMP|TEMPDIR|PYTEST_DEBUG_TEMPROOT)=([^\s]+)"
)


@dataclass(frozen=True)
class TreeStats:
    allocated_bytes: int
    newest_mtime: float
    device: int
    inode: int


@dataclass(frozen=True)
class Inspection:
    path: Path
    selected: bool
    reason: str
    allocated_bytes: int = 0
    age_seconds: int = 0
    device: int | None = None
    inode: int | None = None


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def _allocated_bytes(stat_result: os.stat_result) -> int:
    blocks = getattr(stat_result, "st_blocks", None)
    if blocks is not None:
        return int(blocks) * 512
    return int(stat_result.st_size)


def tree_stats(root: Path) -> TreeStats:
    """Measure a tree without following symlinks outside it."""
    root_stat = root.lstat()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("candidate must be a real directory, not a symlink")

    allocated = _allocated_bytes(root_stat)
    newest = root_stat.st_mtime
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                entry_stat = entry.stat(follow_symlinks=False)
                allocated += _allocated_bytes(entry_stat)
                newest = max(newest, entry_stat.st_mtime)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
    return TreeStats(allocated, newest, root_stat.st_dev, root_stat.st_ino)


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _owner_file_state(path: Path) -> tuple[bool | None, str]:
    marker = path / OWNER_FILE
    if not marker.exists():
        return False, "no owner marker"
    try:
        raw_pid = marker.read_text(encoding="ascii").strip()
        pid = int(raw_pid)
        if pid <= 0:
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        return None, f"cannot validate {OWNER_FILE}"

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, f"owner pid {pid} has exited"
    except PermissionError:
        return True, f"owner pid {pid} exists (permission denied)"
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False, f"owner pid {pid} has exited"
        return None, f"cannot inspect owner pid {pid}: {exc}"
    return True, f"owner pid {pid} is live"


def process_use_state(
    path: Path,
    *,
    runner: RunCommand = subprocess.run,
) -> tuple[bool | None, str]:
    """Return True for active, False for inspected-and-idle, None for unknown."""
    marker_active, marker_reason = _owner_file_state(path)
    if marker_active is None or marker_active:
        return marker_active, marker_reason

    lsof = runner(
        [LSOF_EXECUTABLE, "-nP", "-F", "p", "+D", os.fspath(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if lsof.returncode == 0 and any(
        line.startswith("p") for line in lsof.stdout.splitlines()
    ):
        return True, "an open file handle or working directory is inside the root"
    if lsof.returncode not in (0, 1) or lsof.stderr.strip():
        return None, "lsof could not inspect the complete root"

    process_list = runner(
        ["/bin/ps", "eww", "-axo", "pid=,command="],
        check=False,
        capture_output=True,
        text=True,
    )
    if process_list.returncode != 0 or process_list.stderr.strip():
        return None, "process environment inspection failed"

    for line in process_list.stdout.splitlines():
        stripped = line.lstrip()
        pid_text, separator, command = stripped.partition(" ")
        if not separator or not pid_text.isdigit() or int(pid_text) == os.getpid():
            continue
        for raw_value in TEMP_ENV_PATTERN.findall(command):
            value = Path(raw_value)
            if value.is_absolute() and _paths_overlap(path, value):
                return True, f"live pid {pid_text} has a temp environment under this root"

    return False, marker_reason


def inspect_candidate(
    path: Path,
    *,
    older_than_seconds: int,
    now: float | None = None,
    runner: RunCommand = subprocess.run,
) -> Inspection:
    absolute = path.absolute()
    if absolute == Path(absolute.anchor):
        return Inspection(absolute, False, "refusing to manage a filesystem root")
    try:
        stats = tree_stats(absolute)
    except (OSError, ValueError) as exc:
        return Inspection(absolute, False, f"cannot inspect tree: {exc}")

    age = max(0, int((time.time() if now is None else now) - stats.newest_mtime))
    if age < older_than_seconds:
        return Inspection(
            absolute,
            False,
            f"newest entry is only {age}s old",
            stats.allocated_bytes,
            age,
            stats.device,
            stats.inode,
        )

    try:
        active, reason = process_use_state(absolute, runner=runner)
    except OSError as exc:
        return Inspection(
            absolute,
            False,
            f"cannot run in-use probes: {exc}",
            stats.allocated_bytes,
            age,
            stats.device,
            stats.inode,
        )
    if active is None:
        return Inspection(
            absolute,
            False,
            f"in-use state unknown: {reason}",
            stats.allocated_bytes,
            age,
            stats.device,
            stats.inode,
        )
    if active:
        return Inspection(
            absolute,
            False,
            f"in use: {reason}",
            stats.allocated_bytes,
            age,
            stats.device,
            stats.inode,
        )
    return Inspection(
        absolute,
        True,
        "old and no live use detected",
        stats.allocated_bytes,
        age,
        stats.device,
        stats.inode,
    )


def _same_tree(inspection: Inspection) -> bool:
    try:
        current = inspection.path.lstat()
    except OSError:
        return False
    return (
        not inspection.path.is_symlink()
        and current.st_dev == inspection.device
        and current.st_ino == inspection.inode
    )


def run(
    roots: Sequence[Path],
    *,
    older_than_seconds: int,
    delete: bool,
    runner: RunCommand = subprocess.run,
) -> int:
    if older_than_seconds < 0:
        raise ValueError("age must not be negative")

    selected = 0
    total = 0
    for root in roots:
        inspection = inspect_candidate(
            root,
            older_than_seconds=older_than_seconds,
            runner=runner,
        )
        if not inspection.selected:
            print(f"SKIP {inspection.path} reason={inspection.reason}")
            continue

        if not delete:
            selected += 1
            total += inspection.allocated_bytes
            print(
                f"WOULD_DELETE {inspection.path} "
                f"bytes={inspection.allocated_bytes} age_seconds={inspection.age_seconds}"
            )
            continue

        # Close the inspection/deletion race as far as a process-level tool can:
        # verify the same inode, then repeat every in-use probe immediately before
        # the symlink-safe removal. Any uncertainty skips the candidate.
        if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            print(
                f"SKIP {inspection.path} "
                "reason=this platform lacks symlink-safe recursive removal"
            )
            continue
        if not _same_tree(inspection):
            print(f"SKIP {inspection.path} reason=candidate changed after inspection")
            continue
        final = inspect_candidate(
            inspection.path,
            older_than_seconds=older_than_seconds,
            runner=runner,
        )
        if not final.selected or not _same_tree(final):
            print(f"SKIP {inspection.path} reason=final safety check failed: {final.reason}")
            continue
        shutil.rmtree(final.path)
        selected += 1
        total += final.allocated_bytes
        print(f"DELETED {final.path} bytes={final.allocated_bytes}")

    label = "reclaimed_bytes" if delete else "would_reclaim_bytes"
    print(f"{label}={total} candidates={selected}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        action="append",
        required=True,
        type=Path,
        help="exact temporary directory to inspect; repeat for each managed root",
    )
    parser.add_argument("--older-than-hours", type=float, default=1.0)
    parser.add_argument(
        "--delete",
        action="store_true",
        help="remove selected roots; without this flag the command is a dry run",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        seconds = int(args.older_than_hours * 3600)
        return run(args.root, older_than_seconds=seconds, delete=args.delete)
    except (OSError, ValueError) as exc:
        print(f"temp janitor error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
