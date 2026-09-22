#!/usr/bin/env python3
"""Safely inspect and optionally remove explicitly named temporary roots.

The command is a dry run unless ``--delete`` is supplied.  It fails closed when
it cannot prove that a candidate is old and unused.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
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
OWNER_LOCK_FILE = ".owner.json"
LSOF_EXECUTABLE = shutil.which("lsof") or "/usr/sbin/lsof"
PURSERS_ROOT_NAME = re.compile(
    r"(?:pursers-review-|pursers-fullgate-|pursers-packaging-gate\.)"
    r"[A-Za-z0-9][A-Za-z0-9._-]*\Z"
)
TEMP_ENV_PATTERN = re.compile(
    r"(?:^|\s)(?:TMPDIR|TMP|TEMP|TEMPDIR|PYTEST_DEBUG_TEMPROOT)="
)
ENV_ASSIGNMENT_PATTERN = re.compile(r"\s+[A-Za-z_][A-Za-z0-9_]*=")


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


def _comparison_path(path: Path) -> Path:
    """Canonicalize aliases for comparison without changing the deletion path."""
    return path.resolve(strict=False)


def _self_environment_use_state(
    comparison_path: Path,
) -> tuple[bool | None, str]:
    """Inspect this process without relying on ``ps`` preserving its environment."""
    try:
        cwd = Path.cwd()
    except OSError as exc:
        return None, f"cannot inspect current process cwd: {exc}"

    for name in ("TMPDIR", "TMP", "TEMP", "TEMPDIR", "PYTEST_DEBUG_TEMPROOT"):
        raw_value = os.environ.get(name)
        if raw_value is None:
            continue
        value = Path(raw_value)
        if not value.is_absolute():
            value = cwd / value
        try:
            comparison_value = _comparison_path(value)
        except OSError as exc:
            return None, f"cannot normalize current process {name}: {exc}"
        if _paths_overlap(comparison_path, comparison_value):
            return True, f"current process {name} overlaps this root"
    return False, "current process temp environment is unrelated"


def _ps_environment_path_is_unambiguous(path: Path) -> bool:
    """Whether a path can be recognized losslessly in whitespace-delimited ps output."""
    raw = os.fspath(path)
    return "\\" not in raw and not any(character.isspace() for character in raw)


def _temp_environment_values(command: str) -> list[str]:
    """Extract temp values through the next environment assignment or line end."""
    values = []
    for match in TEMP_ENV_PATTERN.finditer(command):
        next_assignment = ENV_ASSIGNMENT_PATTERN.search(command, match.end())
        end = next_assignment.start() if next_assignment else len(command)
        value = command[match.end() : end].rstrip()
        if value:
            values.append(value)
    return values


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


def _owner_lock_state(path: Path) -> tuple[bool | None, str]:
    """Honor the advisory owner lock used by managed Pursers temp roots."""
    marker = path / OWNER_LOCK_FILE
    try:
        marker.lstat()
    except FileNotFoundError:
        return False, "no owner lock"
    except OSError as exc:
        return None, f"cannot inspect {OWNER_LOCK_FILE}: {exc}"
    if marker.is_symlink() or not marker.is_file():
        return None, f"cannot validate {OWNER_LOCK_FILE}"

    try:
        with marker.open("r+", encoding="utf-8") as owner:
            try:
                payload = json.load(owner)
            except (OSError, json.JSONDecodeError):
                return None, f"cannot validate {OWNER_LOCK_FILE}"
            if (
                not isinstance(payload, dict)
                or payload.get("schema") != 1
                or not isinstance(payload.get("pid"), int)
                or isinstance(payload.get("pid"), bool)
                or payload["pid"] <= 0
            ):
                return None, f"cannot validate {OWNER_LOCK_FILE}"
            try:
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True, f"owner lock for pid {payload['pid']} is held"
    except OSError as exc:
        return None, f"cannot inspect {OWNER_LOCK_FILE}: {exc}"
    return False, f"owner lock for pid {payload['pid']} is not held"


def _lsof_use_state(
    command: list[str],
    *,
    active_reason: str,
    runner: RunCommand,
) -> tuple[bool | None, str]:
    """Interpret one lsof probe without discarding partial positive results."""
    result = runner(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    lines = result.stdout.splitlines()
    if any(re.fullmatch(r"p\d+", line) for line in lines):
        # macOS lsof can emit a complete matching record but still exit 1 for a
        # +D traversal. Positive evidence wins regardless of that exit status.
        return True, active_reason
    if result.returncode not in (0, 1) or result.stderr.strip():
        return None, "lsof could not complete an in-use probe"
    if result.stdout.strip():
        return None, "lsof returned an unrecognized partial result"
    return False, "no matching lsof records"


def process_use_state(
    path: Path,
    *,
    runner: RunCommand = subprocess.run,
) -> tuple[bool | None, str]:
    """Return True for active, False for inspected-and-idle, None for unknown."""
    if not _ps_environment_path_is_unambiguous(path):
        return None, "candidate path is ambiguous in process environment output"

    marker_active, marker_reason = _owner_file_state(path)
    if marker_active is None or marker_active:
        return marker_active, marker_reason

    lock_active, lock_reason = _owner_lock_state(path)
    if lock_active is None or lock_active:
        return lock_active, lock_reason

    root_active, root_reason = _lsof_use_state(
        [LSOF_EXECUTABLE, "-nP", "-F", "pfn", os.fspath(path)],
        active_reason=(
            "a live process has the root itself open or as its working directory"
        ),
        runner=runner,
    )
    if root_active is None or root_active:
        return root_active, root_reason

    tree_active, tree_reason = _lsof_use_state(
        [LSOF_EXECUTABLE, "-nP", "-F", "p", "+D", os.fspath(path)],
        active_reason="an open file handle or working directory is inside the root",
        runner=runner,
    )
    if tree_active is None or tree_active:
        return tree_active, tree_reason

    # BSD options stay undashed: Linux procps warns on stderr about "-axo",
    # and any stderr here fails closed, so every candidate read as unknown.
    process_list = runner(
        ["/bin/ps", "axeww", "-o", "pid=,command="],
        check=False,
        capture_output=True,
        text=True,
    )
    if process_list.returncode != 0 or process_list.stderr.strip():
        return None, "process environment inspection failed"

    try:
        comparison_path = _comparison_path(path)
    except OSError as exc:
        return None, f"cannot normalize candidate for environment comparison: {exc}"

    self_active, self_reason = _self_environment_use_state(comparison_path)
    if self_active is None or self_active:
        return self_active, self_reason

    for line in process_list.stdout.splitlines():
        stripped = line.lstrip()
        pid_text, separator, command = stripped.partition(" ")
        if not separator or not pid_text.isdigit() or int(pid_text) == os.getpid():
            continue
        for raw_value in _temp_environment_values(command):
            value = Path(raw_value)
            if not value.is_absolute():
                return None, (
                    f"live pid {pid_text} has a relative temp environment; "
                    "cwd-relative ownership cannot be proven"
                )
            try:
                comparison_value = _comparison_path(value)
            except OSError:
                return None, f"cannot normalize temp environment for live pid {pid_text}"
            if _paths_overlap(comparison_path, comparison_value):
                return True, f"live pid {pid_text} has a temp environment under this root"
            if any(character.isspace() for character in raw_value):
                return None, f"live pid {pid_text} has an ambiguous temp environment"

    if lock_reason != "no owner lock":
        return False, lock_reason
    return False, marker_reason


def discover_pursers_roots(parent: Path) -> list[Path]:
    """Return allowlisted real directories that are direct children of parent."""
    if not parent.is_absolute():
        raise ValueError("discovery parent must be an absolute path")
    if parent == Path(parent.anchor):
        raise ValueError("refusing to discover beneath a filesystem root")
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise ValueError(f"cannot inspect discovery parent: {exc}") from exc
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("discovery parent must be a real directory, not a symlink")

    roots: list[Path] = []
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if not PURSERS_ROOT_NAME.fullmatch(entry.name):
                    continue
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                candidate = parent / entry.name
                try:
                    current_parent = candidate.parent.lstat()
                except OSError:
                    continue
                if (
                    current_parent.st_dev != parent_stat.st_dev
                    or current_parent.st_ino != parent_stat.st_ino
                ):
                    continue
                roots.append(candidate)
    except OSError as exc:
        raise ValueError(f"cannot enumerate discovery parent: {exc}") from exc
    return sorted(roots, key=lambda path: path.name)


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


def _restore_quarantined(quarantine: Path, original: Path) -> str:
    if original.exists() or original.is_symlink():
        return f"changed entry retained at quarantine path {quarantine}"
    try:
        os.rename(quarantine, original)
    except OSError as exc:
        return f"cannot restore changed entry from {quarantine}: {exc}"
    return "changed entry restored without deletion"


def _quarantine_and_remove(inspection: Inspection) -> tuple[bool, str]:
    """Atomically detach the inspected inode before recursive deletion."""
    parent = inspection.path.parent
    quarantine = parent / (
        f".pursers-tmp-janitor-{inspection.path.name}-{os.getpid()}-{time.time_ns()}"
    )
    try:
        os.rename(inspection.path, quarantine)
    except OSError as exc:
        return False, f"cannot quarantine candidate: {exc}"

    try:
        moved = quarantine.lstat()
    except OSError as exc:
        return False, f"cannot verify quarantined candidate: {exc}"
    if (
        quarantine.is_symlink()
        or moved.st_dev != inspection.device
        or moved.st_ino != inspection.inode
    ):
        restoration = _restore_quarantined(quarantine, inspection.path)
        return False, f"candidate changed during quarantine; {restoration}"

    try:
        shutil.rmtree(quarantine)
    except OSError as exc:
        restoration = _restore_quarantined(quarantine, inspection.path)
        return False, f"recursive removal failed: {exc}; {restoration}"
    return True, "deleted"


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
        removed, removal_reason = _quarantine_and_remove(final)
        if not removed:
            print(f"SKIP {inspection.path} reason={removal_reason}")
            continue
        selected += 1
        total += final.allocated_bytes
        print(f"DELETED {final.path} bytes={final.allocated_bytes}")

    label = "reclaimed_bytes" if delete else "would_reclaim_bytes"
    print(f"{label}={total} candidates={selected}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--root",
        action="append",
        type=Path,
        help="exact temporary directory to inspect; repeat for each managed root",
    )
    source.add_argument(
        "--discover-pursers-under",
        type=Path,
        metavar="ABSOLUTE_PARENT",
        help=(
            "opt in to direct-child discovery under one exact parent; only known "
            "Pursers review/gate basenames are eligible"
        ),
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
        roots = (
            discover_pursers_roots(args.discover_pursers_under)
            if args.discover_pursers_under is not None
            else args.root
        )
        return run(roots, older_than_seconds=seconds, delete=args.delete)
    except (OSError, ValueError) as exc:
        print(f"temp janitor error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
