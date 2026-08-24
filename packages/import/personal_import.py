#!/usr/bin/env python3
"""Offline, copy-only legacy import with durable install and rollback receipts."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable

if __package__:
    from .bind_identities import bind_identities, generate_identity_material
    from .native_import import CENTRAL_ID, board_path, canonical_db_hash, load_json, promote
    from .prepare_apply_rehearsal import _tree_state as _snapshot_tree_state
    from .prepare_apply_rehearsal import prepare
    from .reconcile import (
        ACTIONS,
        POLICY_DECISIONS_STATUS,
        _decision_map,
        apply_decisions,
        generate_worksheet,
    )
    from .safe_tree import open_directory_nofollow, require_path_matches_descriptor, walk_tree_fd
    from .scrub import DEFAULT_RULES, Policy, scrub
    from .transactional_sqlite import TransactionalSQLiteStore
else:  # source-checkout execution
    from bind_identities import bind_identities, generate_identity_material
    from native_import import CENTRAL_ID, board_path, canonical_db_hash, load_json, promote
    from prepare_apply_rehearsal import _tree_state as _snapshot_tree_state
    from prepare_apply_rehearsal import prepare
    from reconcile import (
        ACTIONS,
        POLICY_DECISIONS_STATUS,
        _decision_map,
        apply_decisions,
        generate_worksheet,
    )
    from safe_tree import open_directory_nofollow, require_path_matches_descriptor, walk_tree_fd
    from scrub import DEFAULT_RULES, Policy, scrub
    from transactional_sqlite import TransactionalSQLiteStore


__version__ = "5.0.0a2"
RUN_SCHEMA_VERSION = 1
RUN_KIND = "pursers-personal-copy-import"
CENTRAL_URL = "https://personal-preview.invalid/mcp"
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
SUPPORTED_STABLE_VERSION = "4.0.4"
Checkpoint = Callable[[str], None]
CLI_REASON_MAX_CHARS = 500
POLICY_SIGNED_STATUS = "POLICY-SIGNED-READY"
ACTION_RESTRICTIVENESS = {"accept-as-is": 0, "redact-span": 1, "drop": 2}
SECRET_CLASS_RULES = frozenset(
    {
        "pem_private_key",
        "aws_access_key_id",
        "aws_secret_access_key",
        "gcp_api_key",
        "gcp_oauth_token",
        "azure_storage_key",
        "azure_client_secret",
        "azure_sas_signature",
        "bearer_token",
        "jwt",
        "url_password",
    }
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9._-])/(?:[^\r\n)]*)")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._-])[A-Z]:\\(?:[^\r\n)]*)"
)


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_json(path: Path, value: Any) -> None:
    path = Path(os.path.abspath(os.fspath(path)))
    _require_directory(path.parent, "private JSON parent", private=True)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise ValueError(f"private JSON target must be a regular file: {path.name}")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(_canonical_bytes(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_private_bytes(path: Path, payload: bytes) -> None:
    path = _absolute(path)
    _require_directory(path.parent, "private file parent", private=True)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("private file target is unsafe")
        if path.read_bytes() != payload:
            raise RuntimeError("private file target differs from its sealed input")
        return
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
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_private_json(path: Path) -> dict[str, Any]:
    info = path.lstat()
    mode = info.st_mode
    if (
        not stat.S_ISREG(mode)
        or stat.S_ISLNK(mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
    ):
        raise ValueError(f"private state must be a regular file: {path.name}")
    if stat.S_IMODE(mode) != 0o600:
        raise ValueError(f"private state must have mode 0600: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"private state is invalid: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"private state must be an object: {path.name}")
    return value


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _require_directory(path: Path, label: str, *, private: bool = False) -> Path:
    path = _absolute(path)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} must be a real directory")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ValueError(f"{label} must not traverse a symlink")
    path = resolved
    if private:
        if info.st_uid != os.geteuid():
            raise ValueError(f"{label} must be owned by the current user")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError(f"{label} must have mode 0700")
    return path


def _safe_target(path: Path, label: str) -> Path:
    path = _absolute(path)
    parent = _require_directory(path.parent, f"{label} parent")
    parent_info = parent.lstat()
    if parent_info.st_uid != os.geteuid():
        raise ValueError(f"{label} parent must be owned by the current user")
    if stat.S_IMODE(parent_info.st_mode) & 0o022:
        raise ValueError(f"{label} parent must not be group/other writable")
    candidate = parent / path.name
    if candidate.exists() or candidate.is_symlink():
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"{label} must not be a symlink")
        if candidate.resolve(strict=True) != candidate:
            raise ValueError(f"{label} must not traverse a symlink")
    return candidate


@contextlib.contextmanager
def _exclusive_lock(path: Path, label: str, *, create: bool = True):
    path = _safe_target(path, f"{label} lock")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise ValueError(f"{label} lock must be a private regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError(f"{label} lock must have mode 0600")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another {label} operation is active") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _ensure_private_child(parent: Path, name: str, label: str) -> Path:
    """Create or verify one direct private directory below an owned run root."""
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError(f"{label} name is invalid")
    parent = _require_directory(parent, f"{label} parent", private=True)
    child = parent / name
    if child.exists() or child.is_symlink():
        return _require_directory(child, label, private=True)
    os.mkdir(child, 0o700)
    _fsync_directory(parent)
    return _require_directory(child, label, private=True)


def _destination_lock_path(destination: Path) -> Path:
    token = hashlib.sha256(str(destination).encode("utf-8")).hexdigest()[:20]
    return destination.parent / f".pursers-personal-import-{token}.lock"


def _overlaps(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _require_disjoint(named: dict[str, Path]) -> None:
    items = list(named.items())
    for index, (first_name, first) in enumerate(items):
        for second_name, second in items[index + 1 :]:
            if _overlaps(first, second):
                raise ValueError(f"{first_name} and {second_name} must not overlap")


def tree_state(
    root: Path,
    *,
    reject_links: bool = True,
    include_root_metadata: bool = True,
    include_mtimes: bool = True,
    _race_hook: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Hash bytes and stable metadata without following links or special entries."""
    root = _require_directory(root, "tree root")
    content = hashlib.sha256()
    metadata = hashlib.sha256()
    entries = 0
    byte_count = 0

    def record_meta(relative: str, kind: str, info: os.stat_result) -> None:
        metadata.update(relative.encode("utf-8"))
        metadata.update(b"\0")
        metadata.update(kind.encode("ascii"))
        metadata.update(b"\0")
        metadata.update(str(stat.S_IMODE(info.st_mode)).encode("ascii"))
        metadata.update(b"\0")
        metadata.update(
            str(info.st_mtime_ns if include_mtimes else 0).encode("ascii")
        )
        metadata.update(b"\0")
        metadata.update(str(info.st_size).encode("ascii"))
        metadata.update(b"\0")

    descriptor = open_directory_nofollow(root)
    try:
        root_info = os.fstat(descriptor)
        scanned = walk_tree_fd(
            descriptor, reject_links=reject_links, hook=_race_hook
        )
        require_path_matches_descriptor(root, descriptor)
    finally:
        os.close(descriptor)
    if include_root_metadata:
        record_meta(".", "D", root_info)
    for relative, kind, info, payload in scanned:
        record_meta(relative, kind, info)
        if kind == "L":
            content.update(relative.encode("utf-8") + b"\0L\0" + payload + b"\0")
        elif kind == "D":
            content.update(relative.encode("utf-8") + b"\0D\0")
        else:
            metadata.update(str(info.st_nlink).encode("ascii") + b"\0")
            content.update(relative.encode("utf-8") + b"\0F\0" + payload + b"\0")
            byte_count += len(payload)
        entries += 1
    return {
        "content_sha256": content.hexdigest(),
        "state_sha256": metadata.hexdigest(),
        "entries": entries,
        "bytes": byte_count,
    }


def _central_tree_state(root: Path) -> dict[str, Any]:
    root = _require_directory(root, "Central data root", private=True)
    return tree_state(root, reject_links=True, include_mtimes=False)


def stable_install_state(root: Path) -> dict[str, Any]:
    """Bind an installed Homebrew v4 artifact without executing it."""
    root = _require_directory(root, "stable install root")
    if (
        root.name != SUPPORTED_STABLE_VERSION
        or root.parent.name != "onboard-memory"
        or root.parent.parent.name != "Cellar"
    ):
        raise ValueError("stable install root must be the onboard-memory/4.0.4 Cellar root")
    receipt_path = root / "INSTALL_RECEIPT.json"
    receipt_info = receipt_path.lstat()
    if (
        stat.S_ISLNK(receipt_info.st_mode)
        or not stat.S_ISREG(receipt_info.st_mode)
        or receipt_info.st_nlink != 1
        or receipt_info.st_size > 1_000_000
        or receipt_path.resolve(strict=True).parent != root
    ):
        raise ValueError("stable Homebrew receipt is not a bounded regular file")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("stable Homebrew receipt is invalid") from exc
    source = receipt.get("source") if isinstance(receipt, dict) else None
    versions = source.get("versions") if isinstance(source, dict) else None
    if (
        not isinstance(versions, dict)
        or versions.get("stable") != SUPPORTED_STABLE_VERSION
        or source.get("spec") != "stable"
    ):
        raise ValueError("stable Homebrew receipt does not bind version 4.0.4")
    executable = root / "libexec" / "bin" / "onboard-memory-mcp"
    executable_info = executable.lstat()
    if (
        stat.S_ISLNK(executable_info.st_mode)
        or not stat.S_ISREG(executable_info.st_mode)
        or executable_info.st_nlink != 1
        or executable.resolve(strict=True) != executable
        or not executable.is_relative_to(root)
        or stat.S_IMODE(executable_info.st_mode) & 0o111 == 0
    ):
        raise ValueError("stable Homebrew executable is not a contained regular executable")
    command_link = root / "bin" / "onboard-memory-mcp"
    if not command_link.is_symlink() or command_link.resolve(strict=True) != executable:
        raise ValueError("stable Homebrew command link does not target its executable")
    prefix = root.parent.parent.parent
    active_link = prefix / "bin" / "onboard-memory-mcp"
    if not active_link.is_symlink() or active_link.resolve(strict=True) != executable:
        raise ValueError("stable Homebrew keg is not the active onboard-memory command")
    executable_payload = executable.read_bytes()
    return {
        "product": "onboard-memory-mcp",
        "version": SUPPORTED_STABLE_VERSION,
        "tree": tree_state(root, reject_links=False),
        "proof_kind": "homebrew-installed-cellar",
        "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "executable_sha256": hashlib.sha256(executable_payload).hexdigest(),
        "executable_size": len(executable_payload),
        "executable_mode": format(stat.S_IMODE(executable_info.st_mode), "04o"),
        "executable_relative": executable.relative_to(root).as_posix(),
        "command_link_relative": command_link.relative_to(root).as_posix(),
        "command_link_target": os.readlink(command_link),
        "active_command_link_target": os.readlink(active_link),
        "active_command_link_mode": format(
            stat.S_IMODE(active_link.lstat().st_mode), "04o"
        ),
    }


def _assert_unchanged(label: str, expected: dict[str, Any], actual: dict[str, Any]) -> None:
    if actual != expected:
        raise RuntimeError(f"{label} changed during import")


def _destination_baseline(path: Path) -> dict[str, Any]:
    path = _absolute(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "absent"}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("central data root must be absent or a real empty directory")
    if info.st_uid != os.geteuid():
        raise ValueError("empty central data root must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("empty central data root must have mode 0700")
    current = tree_state(path)
    if current["entries"] != 0:
        raise ValueError(
            "non-empty Central data roots are out of scope; preserve existing boards "
            "and migrate in a dedicated maintenance release"
        )
    return {"kind": "empty", "tree": current}


def _current_destination(path: Path) -> dict[str, Any]:
    try:
        path.lstat()
    except FileNotFoundError:
        return {"kind": "absent"}
    return {"kind": "tree", "tree": tree_state(path)}


def _destination_matches_baseline(path: Path, baseline: dict[str, Any]) -> bool:
    current = _current_destination(path)
    if baseline["kind"] == "absent":
        return current["kind"] == "absent"
    return current == {"kind": "tree", "tree": baseline["tree"]}


def _checkpoint(callback: Checkpoint | None, name: str) -> None:
    if callback is not None:
        callback(name)


def _run_paths(run_root: Path) -> dict[str, Path]:
    return {
        "owner": run_root / ".onboard-import-run.json",
        "lock": run_root / ".run.lock",
        "state": run_root / "state.json",
        "frozen": run_root / "frozen",
        "staging": run_root / "staging-central",
        "backup": run_root / "backup" / "original-central",
        "worksheet": run_root / "evidence" / "quarantine-worksheet.json",
        "identity_worksheet": run_root / "evidence" / "identity-binding-worksheet.json",
        "identity_template": run_root / "evidence" / "identity-bindings-template.json",
        "review_preview": run_root / "review-expected-central",
        "rollback": run_root / "rollback-quarantine" / "imported-central",
        "quarantine": run_root / "recovery-quarantine",
    }


def _load_run(
    run_root: Path, *, expected_run_root: Path | None = None
) -> tuple[dict[str, Any], dict[str, Path]]:
    run_root = _require_directory(run_root, "run directory", private=True)
    paths = _run_paths(run_root)
    owner = _load_private_json(paths["owner"])
    if owner != {"kind": RUN_KIND, "schema_version": RUN_SCHEMA_VERSION}:
        raise ValueError("run directory ownership marker is invalid")
    state = _load_private_json(paths["state"])
    if (
        state.get("kind") != RUN_KIND
        or state.get("schema_version") != RUN_SCHEMA_VERSION
    ):
        raise ValueError("run state contract is invalid")
    expected = _absolute(expected_run_root or run_root)
    if state.get("run_root") != str(expected):
        raise ValueError("run state directory does not match its owned location")
    return state, paths


@contextlib.contextmanager
def _locked_run(run_root: Path, *, create_locks: bool = True):
    preliminary, _preliminary_paths = _load_run(run_root)
    destination = _safe_target(
        Path(preliminary["destination"]), "central data root"
    )
    with _exclusive_lock(
        _destination_lock_path(destination),
        "Central import",
        create=create_locks,
    ):
        state, paths = _load_run(run_root)
        if _absolute(Path(state["destination"])) != destination:
            raise RuntimeError("run destination changed while acquiring its lock")
        with _exclusive_lock(paths["lock"], "import run", create=create_locks):
            state, paths = _load_run(run_root)
            if _absolute(Path(state["destination"])) != destination:
                raise RuntimeError("run destination changed while acquiring its lock")
            yield state, paths


def _save_state(paths: dict[str, Path], state: dict[str, Any]) -> None:
    _write_private_json(paths["state"], state)


def _quarantine_generated(path: Path, paths: dict[str, Path], label: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"generated {label} path is not a real directory")
    root = _ensure_private_child(
        paths["quarantine"].parent,
        paths["quarantine"].name,
        "recovery quarantine",
    )
    index = 1
    while (root / f"{label}-{index}").exists():
        index += 1
    os.replace(path, root / f"{label}-{index}")
    _fsync_directory(path.parent)
    _fsync_directory(root)


def _context(state: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    source = _absolute(Path(state["source"]))
    destination = _absolute(Path(state["destination"]))
    stable_install_root = _absolute(Path(state["stable_install_root"]))
    run_root = _absolute(Path(state["run_root"]))
    _require_disjoint(
        {
            "source": source,
            "central data root": destination,
            "stable install root": stable_install_root,
            "run directory": run_root,
        }
    )
    return source, destination, stable_install_root, run_root


_PROMOTED_TEMP_NAME = re.compile(
    r"^\.PROMOTED\.json\.[0-9]+\.[0-9a-f]{16}\.tmp$"
)


def _transition_tree_entries(root: Path) -> dict[str, tuple[str, int, int, int, str, bytes]]:
    """Read a bounded, no-follow tree for exact transition-state validation."""
    root = _require_directory(root, "transition tree", private=True)
    descriptor = open_directory_nofollow(root)
    try:
        scanned = walk_tree_fd(descriptor, reject_links=True)
        require_path_matches_descriptor(root, descriptor)
    finally:
        os.close(descriptor)
    result: dict[str, tuple[str, int, int, int, str, bytes]] = {}
    for relative, kind, info, payload in scanned:
        result[relative] = (
            kind,
            stat.S_IMODE(info.st_mode),
            info.st_mtime_ns,
            info.st_size,
            hashlib.sha256(payload).hexdigest(),
            payload,
        )
    return result


def _source_hash_from_entries(
    entries: dict[str, tuple[str, int, int, int, str, bytes]]
) -> str:
    digest = hashlib.sha256()
    for relative in sorted(entries):
        kind, _mode, _mtime, _size, _payload_hash, payload = entries[relative]
        if kind != "F" or Path(relative).name == "PROMOTED.json":
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _verify_promoted_transition(
    state: dict[str, Any], snapshot: Path, promoted: Path
) -> None:
    """Accept only durable pre-marker, marker+fence, or final-marker states."""
    snapshot_entries = _transition_tree_entries(snapshot)
    promoted_entries = _transition_tree_entries(promoted)
    special = {"WRITE_FENCE.json", "PROMOTED.json"}

    def base_entries(
        rows: dict[str, tuple[str, int, int, int, str, bytes]]
    ) -> dict[str, tuple[str, int, int, int, str]]:
        return {
            relative: row[:5]
            for relative, row in rows.items()
            if relative not in special and not _PROMOTED_TEMP_NAME.fullmatch(relative)
        }

    if base_entries(promoted_entries) != base_entries(snapshot_entries):
        raise RuntimeError("transitioning promoted source copy changed")
    if stat.S_IMODE(promoted.stat().st_mode) != 0o700 or any(
        row[1] != (0o700 if row[0] == "D" else 0o600)
        for row in promoted_entries.values()
    ):
        raise RuntimeError("transitioning promoted source copy is not private")

    source_digest = _source_hash_from_entries(snapshot_entries)
    marker_payload = _canonical_bytes(
        {
            "schema_version": 1,
            "board_id": state["board_id"],
            "direction": "local-to-central-only",
            "source_hash": source_digest,
            "central_url": CENTRAL_URL,
        }
    )
    snapshot_fence = snapshot_entries.get("WRITE_FENCE.json")
    fence = promoted_entries.get("WRITE_FENCE.json")
    marker = promoted_entries.get("PROMOTED.json")
    temps = [
        row
        for relative, row in promoted_entries.items()
        if _PROMOTED_TEMP_NAME.fullmatch(relative)
    ]
    if snapshot_fence is None or snapshot_fence[0] != "F":
        raise RuntimeError("sealed source snapshot has no valid write fence")
    if fence is not None and fence != snapshot_fence:
        raise RuntimeError("transitioning promoted write fence changed")
    if marker is not None and (
        marker[0] != "F" or marker[1] != 0o600 or marker[5] != marker_payload
    ):
        raise RuntimeError("transitioning promoted marker is invalid")
    if marker is None:
        if fence is None or len(temps) > 1:
            raise RuntimeError("transitioning promoted marker state is invalid")
        if temps and (
            temps[0][0] != "F"
            or temps[0][1] != 0o600
            or len(temps[0][5]) > len(marker_payload)
            or not marker_payload.startswith(temps[0][5])
        ):
            raise RuntimeError("transitioning promoted temporary marker is invalid")
    elif temps:
        raise RuntimeError("transitioning promoted marker state is invalid")


def _verify_invariants(
    state: dict[str, Any],
    *,
    frozen: bool = False,
    live_source: bool = False,
    stable_install: bool = False,
) -> None:
    source, _destination, stable_install_root, run_root = _context(state)
    if live_source:
        _assert_unchanged(
            "source .agent-mem during copy window",
            state["source_before"],
            tree_state(source, reject_links=True),
        )
    if stable_install:
        _assert_unchanged(
            "stable v4 install during import window",
            state["stable_install_before"],
            stable_install_state(stable_install_root),
        )
    if frozen:
        _assert_snapshot_proof_file_anchor(
            state, run_root / "frozen" / "snapshot-proof.json"
        )
        expected_relative_paths = {
            "snapshot_relative": "frozen/source-snapshot/.agent-mem",
            "full_backup_relative": "frozen/full-source-backup/.agent-mem",
            "promoted_relative": "frozen/promoted-board/.agent-mem",
        }
        for key, expected in expected_relative_paths.items():
            if state.get(key) != expected:
                raise ValueError(f"run state {key} is not canonical")
        snapshot = run_root / state["snapshot_relative"]
        _assert_unchanged(
            "sealed source snapshot",
            state["snapshot_seal"],
            tree_state(snapshot, reject_links=True),
        )
        full_backup = run_root / state["full_backup_relative"]
        _assert_unchanged(
            "sealed full source backup",
            state["full_backup_seal"],
            tree_state(full_backup, reject_links=True),
        )
        promoted = run_root / state["promoted_relative"]
        if "promoted_seal" in state:
            _assert_unchanged(
                "sealed promoted source copy",
                state["promoted_seal"],
                tree_state(promoted, reject_links=True),
            )
        elif not state.get("promoted_transition_started", False):
            _assert_unchanged(
                "sealed initial promoted source copy",
                state["promoted_initial_seal"],
                tree_state(promoted, reject_links=True),
            )
        else:
            _verify_promoted_transition(state, snapshot, promoted)
        if "worksheet" in state:
            review_artifacts = (
                (
                    state["worksheet"]["relative"],
                    state["worksheet"]["sha256"],
                    "private quarantine worksheet",
                ),
                (
                    state["identity_worksheet"]["relative"],
                    state["identity_worksheet"]["sha256"],
                    "private identity worksheet",
                ),
                (
                    state["identity_worksheet"]["template_relative"],
                    state["identity_worksheet"]["template_sha256"],
                    "private identity template",
                ),
            )
            for relative, expected_hash, label in review_artifacts:
                artifact = run_root / relative
                document = _load_private_json(artifact)
                del document
                if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected_hash:
                    raise RuntimeError(f"{label} changed")


def _unexpected_generation_token() -> str:
    raise AssertionError("same-source retry must not mint a generation token")


_COMPLETED_FREEZE_PROOF_KEYS = {
    "status",
    "board_id",
    "source_snapshot",
    "full_source_backup",
    "promoted_board_root",
    "full_live_content_sha256",
    "full_live_state_sha256",
    "full_files",
    "full_bytes",
    "live_content_sha256",
    "live_state_sha256",
    "files",
    "bytes",
    "live_source_write",
    "live_source_mtimes_during_lock",
    "post_snapshot_live_state",
    "symlinks",
    "snapshot_scope",
    "private_modes",
    "completed_copy_seals",
}
_POST_SNAPSHOT_OBSERVATIONS = {"unchanged", "external-drift-after-unlock"}


def _completed_freeze_copy_paths(paths: dict[str, Path]) -> dict[str, Path]:
    return {
        "source_snapshot": paths["frozen"] / "source-snapshot" / ".agent-mem",
        "full_source_backup": paths["frozen"]
        / "full-source-backup"
        / ".agent-mem",
        "promoted_board": paths["frozen"] / "promoted-board" / ".agent-mem",
    }


def _canonical_completed_freeze_proof(
    candidate: dict[str, Any], state: dict[str, Any], paths: dict[str, Path]
) -> dict[str, Any]:
    """Recompute every authoritative proof field from trusted state and copies."""
    if set(candidate) != _COMPLETED_FREEZE_PROOF_KEYS:
        raise ValueError("completed frozen-copy proof fields are not canonical")
    post_snapshot = candidate.get("post_snapshot_live_state")
    if post_snapshot not in _POST_SNAPSHOT_OBSERVATIONS:
        raise ValueError("completed frozen-copy observation is invalid")
    copy_paths = _completed_freeze_copy_paths(paths)
    copy_seals = {
        label: list(_snapshot_tree_state(copy_path))
        for label, copy_path in copy_paths.items()
    }
    expected = {
        "status": "complete",
        "board_id": state["board_id"],
        "source_snapshot": str(copy_paths["source_snapshot"]),
        "full_source_backup": str(copy_paths["full_source_backup"]),
        "promoted_board_root": str(copy_paths["promoted_board"]),
        "full_live_content_sha256": state[
            "snapshot_source_content_sha256"
        ],
        "full_live_state_sha256": state["snapshot_source_state_sha256"],
        "full_files": state["snapshot_source_files"],
        "full_bytes": state["snapshot_source_bytes"],
        "live_content_sha256": state[
            "snapshot_import_source_content_sha256"
        ],
        "live_state_sha256": state["snapshot_import_source_state_sha256"],
        "files": state["snapshot_import_source_files"],
        "bytes": state["snapshot_import_source_bytes"],
        "live_source_write": "none",
        "live_source_mtimes_during_lock": "unchanged",
        # This is only an observation made after releasing the source lock. It
        # is intentionally not a retry precondition or provenance authority.
        "post_snapshot_live_state": post_snapshot,
        "symlinks": "rejected",
        "snapshot_scope": "native-import-domain",
        "private_modes": "dirs=0700,files=0600",
        "completed_copy_seals": copy_seals,
    }
    if _canonical_bytes(candidate) != _canonical_bytes(expected):
        raise ValueError("completed frozen-copy proof is not canonical")
    return expected


def _freeze_anchor_value(
    proof_path: Path,
    proof: dict[str, Any],
    paths: dict[str, Path],
) -> dict[str, Any]:
    payload = proof_path.read_bytes()
    if payload != _canonical_bytes(proof):
        raise ValueError("completed frozen-copy proof encoding is not canonical")
    return {
        "schema_version": 1,
        "proof_sha256": hashlib.sha256(payload).hexdigest(),
        "proof_size": len(payload),
        "copy_seals": {
            label: tree_state(copy_path, reject_links=True)
            for label, copy_path in _completed_freeze_copy_paths(paths).items()
        },
    }


def _assert_snapshot_proof_file_anchor(
    state: dict[str, Any], proof_path: Path
) -> dict[str, Any]:
    anchors = state.get("freeze_anchors")
    if not isinstance(anchors, dict) or set(anchors) != {
        "schema_version",
        "proof_sha256",
        "proof_size",
        "copy_seals",
    }:
        raise ValueError("trusted frozen-copy anchors are missing")
    if anchors.get("schema_version") != 1:
        raise ValueError("trusted frozen-copy anchor version is invalid")
    proof = _load_private_json(proof_path)
    payload = proof_path.read_bytes()
    if (
        type(anchors.get("proof_size")) is not int
        or anchors["proof_size"] != len(payload)
        or anchors.get("proof_sha256")
        != hashlib.sha256(payload).hexdigest()
    ):
        raise RuntimeError("completed frozen-copy proof changed after anchoring")
    return proof


def _verify_completed_freeze(
    state: dict[str, Any], paths: dict[str, Path]
) -> dict[str, Any]:
    proof_path = paths["frozen"] / "snapshot-proof.json"
    proof = _assert_snapshot_proof_file_anchor(state, proof_path)
    proof = _canonical_completed_freeze_proof(proof, state, paths)
    anchors = state["freeze_anchors"]
    expected_copy_seals = anchors.get("copy_seals")
    if not isinstance(expected_copy_seals, dict) or set(expected_copy_seals) != {
        "source_snapshot",
        "full_source_backup",
        "promoted_board",
    }:
        raise ValueError("trusted frozen-copy seals are invalid")
    actual_copy_seals = {
        label: tree_state(copy_path, reject_links=True)
        for label, copy_path in _completed_freeze_copy_paths(paths).items()
    }
    _assert_unchanged(
        "trusted completed frozen copies", expected_copy_seals, actual_copy_seals
    )
    return proof


def _prepare_phase(
    state: dict[str, Any], paths: dict[str, Path], callback: Checkpoint | None
) -> dict[str, Any]:
    source, destination, _stable_install_root, run_root = _context(state)
    if not _destination_matches_baseline(destination, state["destination_before"]):
        raise RuntimeError("central data root changed before installation")
    proof_path = paths["frozen"] / "snapshot-proof.json"
    proof: dict[str, Any] | None = None
    if paths["frozen"].exists() and proof_path.exists():
        try:
            if state.get("phase") != "freeze_completed":
                raise ValueError("completed frozen copies have no trusted anchor")
            proof = _verify_completed_freeze(state, paths)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            _quarantine_generated(paths["frozen"], paths, "invalid-completed-freeze")
    if proof is None:
        _verify_invariants(state, live_source=True, stable_install=True)
        _quarantine_generated(paths["frozen"], paths, "incomplete-freeze")
        proof = prepare(source, paths["frozen"], state["board_id"])
        proof = _canonical_completed_freeze_proof(proof, state, paths)
        state["phase"] = "freeze_completed"
        state["freeze_anchors"] = _freeze_anchor_value(
            proof_path, proof, paths
        )
        _save_state(paths, state)
        _checkpoint(callback, "after-freeze-before-state")
        proof = _verify_completed_freeze(state, paths)
        _verify_invariants(state, stable_install=True)
    snapshot = Path(proof["source_snapshot"])
    full_backup = Path(proof["full_source_backup"])
    promoted = Path(proof["promoted_board_root"])
    if not all(
        path.is_relative_to(run_root)
        for path in (snapshot, full_backup, promoted)
    ):
        raise RuntimeError("snapshot preparation escaped the owned run directory")
    if (
        snapshot != paths["frozen"] / "source-snapshot" / ".agent-mem"
        or full_backup != paths["frozen"] / "full-source-backup" / ".agent-mem"
        or promoted != paths["frozen"] / "promoted-board" / ".agent-mem"
    ):
        raise ValueError("completed frozen-copy paths are not canonical")
    state.update(
        {
            "phase": "prepared",
            "snapshot_relative": snapshot.relative_to(run_root).as_posix(),
            "full_backup_relative": full_backup.relative_to(run_root).as_posix(),
            "promoted_relative": promoted.relative_to(run_root).as_posix(),
            "snapshot_seal": tree_state(snapshot, reject_links=True),
            "full_backup_seal": tree_state(full_backup, reject_links=True),
            "promoted_initial_seal": tree_state(promoted, reject_links=True),
            "source_copy_window_unchanged": True,
            "stable_install_import_window_unchanged": True,
            "snapshot_proof": {
                key: proof[key]
                for key in (
                    "live_content_sha256",
                    "live_state_sha256",
                    "files",
                    "bytes",
                    "full_live_content_sha256",
                    "full_live_state_sha256",
                    "full_files",
                    "full_bytes",
                    "live_source_write",
                    "live_source_mtimes_during_lock",
                    "symlinks",
                    "private_modes",
                )
            }
            | {
                "post_snapshot_live_state": "non-authoritative-after-unlock"
            },
        }
    )
    _save_state(paths, state)
    _checkpoint(callback, "after-prepared-state")
    return state


def _stage_phase(
    state: dict[str, Any], paths: dict[str, Path], callback: Checkpoint | None
) -> dict[str, Any]:
    _source, destination, stable_install_root, run_root = _context(state)
    _verify_invariants(state, frozen=True)
    if not _destination_matches_baseline(destination, state["destination_before"]):
        raise RuntimeError("central data root changed before installation")
    _quarantine_generated(paths["staging"], paths, "incomplete-staging")
    snapshot = run_root / state["snapshot_relative"]
    promoted = run_root / state["promoted_relative"]
    state["promoted_transition_started"] = True
    _save_state(paths, state)
    _checkpoint(callback, "after-promoted-transition-state")
    promotion = promote(
        snapshot,
        paths["staging"],
        state["board_id"],
        central_url=CENTRAL_URL,
        promoted_board_root=promoted,
        owner_principal_id=state["owner_principal_id"],
        owner_agent_name=state["owner_agent_name"],
    )
    state["promoted_seal"] = tree_state(promoted, reject_links=True)
    os.chmod(paths["staging"], 0o700)
    _ensure_private_child(run_root, "evidence", "evidence directory")
    worksheet = generate_worksheet(paths["staging"], state["board_id"])
    _write_private_json(paths["worksheet"], worksheet)
    before_retry = canonical_db_hash(paths["staging"])
    retry_result = promote(
        snapshot,
        paths["staging"],
        state["board_id"],
        central_url=CENTRAL_URL,
        promoted_board_root=promoted,
        owner_principal_id=state["owner_principal_id"],
        owner_agent_name=state["owner_agent_name"],
        generation_token_factory=_unexpected_generation_token,
    )
    after_retry = canonical_db_hash(paths["staging"])
    if retry_result.get("status") != "noop" or before_retry != after_retry:
        raise RuntimeError("same-source staging retry was not idempotent")
    _verify_invariants(state, frozen=True)
    stable_install_after = stable_install_state(stable_install_root)
    _assert_unchanged(
        "stable v4 install during import window",
        state["stable_install_before"],
        stable_install_after,
    )
    stage_seal = _central_tree_state(paths["staging"])
    promoted_seal = tree_state(promoted, reject_links=True)
    store = TransactionalSQLiteStore(paths["staging"])
    board = store.load(board_path(store, state["board_id"]), dict)
    legacy_agents = board.get("legacy_import", {}).get("agents", {})
    if not isinstance(legacy_agents, dict):
        raise RuntimeError("staged identity inventory is invalid")
    unmapped = sum(
        item.get("binding_status") == "legacy_unbound"
        for item in legacy_agents.values()
        if isinstance(item, dict)
    )
    quarantined_agent_ids = [
        str(item["record_id"])
        for item in worksheet.get("entries", [])
        if item.get("record_type") == "agent"
    ]
    identity_worksheet, identity_template = generate_identity_material(
        paths["staging"],
        state["board_id"],
        quarantined_agent_record_ids=quarantined_agent_ids,
    )
    _write_private_json(paths["identity_worksheet"], identity_worksheet)
    _write_private_json(paths["identity_template"], identity_template)
    review_required = worksheet["entry_count"] > 0 or unmapped > 0
    _checkpoint(callback, "after-stage-before-state")
    state.update(
        {
            "phase": "review_required" if review_required else "staged",
            "stage_seal": stage_seal,
            "promoted_seal": promoted_seal,
            "stable_install_after": stable_install_after,
            "canonical_db_sha256": after_retry,
            "import_result": {
                "status": promotion["status"],
                "changes": promotion["changes"],
                "quarantined": promotion["quarantined"],
                "generation_revision": promotion["generation_revision"],
                "generation_fingerprint": promotion["generation_fingerprint"],
            },
            "provenance": {
                "original_source_hash": state["snapshot_proof"][
                    "full_live_content_sha256"
                ],
                "original_source_state_hash": state["snapshot_proof"][
                    "full_live_state_sha256"
                ],
                "original_file_count": state["snapshot_proof"]["full_files"],
                "original_byte_count": state["snapshot_proof"]["full_bytes"],
                "import_domain_source_hash": state["snapshot_proof"][
                    "live_content_sha256"
                ],
                "frozen_import_hash": promotion["source_hash"],
                "frozen_tree_content_hash": state["snapshot_seal"][
                    "content_sha256"
                ],
                "frozen_file_count": state["snapshot_seal"]["entries"],
                "frozen_byte_count": state["snapshot_seal"]["bytes"],
                "promoted_marker_hash": promotion["promoted_marker"]["sha256"],
                "native_import_input": state["snapshot_relative"],
                "full_backup": state["full_backup_relative"],
                "live_source_passed_to_native_import": False,
            },
            "idempotent_staging_retry": True,
            "worksheet": {
                "relative": paths["worksheet"].relative_to(run_root).as_posix(),
                "entry_count": worksheet["entry_count"],
                "sha256": hashlib.sha256(paths["worksheet"].read_bytes()).hexdigest(),
                "masked_only": True,
            },
            "identity_worksheet": {
                "relative": paths["identity_worksheet"].relative_to(run_root).as_posix(),
                "sha256": hashlib.sha256(paths["identity_worksheet"].read_bytes()).hexdigest(),
                "entry_count": identity_worksheet["entry_count"],
                "template_relative": paths["identity_template"].relative_to(run_root).as_posix(),
                "template_sha256": hashlib.sha256(paths["identity_template"].read_bytes()).hexdigest(),
                "private_only": True,
            },
            "review_gate": {
                "quarantine_records": worksheet["entry_count"],
                "unmapped_legacy_agents": unmapped,
                "resolved": not review_required,
            },
        }
    )
    _save_state(paths, state)
    if review_required:
        _checkpoint(callback, "after-review-required-state")
    return state


def _copy_review_input(source: Path, directory: Path, label: str) -> dict[str, str]:
    source = _absolute(source)
    parent = _require_directory(source.parent, f"{label} parent")
    if parent / source.name != source:
        raise ValueError(f"{label} path must not traverse a symlink")
    info = source.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 1_000_000
    ):
        raise ValueError(f"{label} must be an owned 0600 bounded regular file")
    descriptor = os.open(
        source,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeError(f"{label} changed while opening")
        payload = b""
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            payload += chunk
            if len(payload) > 1_000_000:
                raise ValueError(f"{label} exceeds the size limit")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise RuntimeError(f"{label} changed while reading")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must contain valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} must contain a JSON object")
    digest = hashlib.sha256(payload).hexdigest()
    target = directory / f"{label}-{digest[:16]}.json"
    _write_private_bytes(target, payload)
    return {
        "relative": target.relative_to(directory.parent.parent).as_posix(),
        "sha256": digest,
    }


def _policy_rule_actions(
    policy: dict[str, Any], worksheet: dict[str, Any], board_id: str
) -> dict[str, str]:
    if (
        policy.get("schema_version") != 1
        or policy.get("status") != POLICY_SIGNED_STATUS
    ):
        raise ValueError("policy must be a POLICY-SIGNED-READY schema v1 document")
    if policy.get("board_id") != board_id:
        raise ValueError("policy board_id does not match the import run")
    if policy.get("worksheet_sha256") != worksheet.get("worksheet_sha256"):
        raise ValueError("policy does not match worksheet_sha256")
    actions = policy.get("rules")
    if not isinstance(actions, dict) or not all(
        isinstance(rule, str)
        and isinstance(action, str)
        and action in ACTIONS
        for rule, action in actions.items()
    ):
        raise ValueError("policy rules must map rule names to supported actions")
    known_rules = {rule.name for rule in DEFAULT_RULES}
    unknown = set(actions) - known_rules
    if unknown:
        raise ValueError("policy contains an unsupported rule")
    encountered = {
        rule
        for entry in worksheet.get("entries", [])
        for rule in entry.get("rules", [])
        if isinstance(rule, str)
    }
    if encountered - set(actions):
        raise ValueError("policy must decide every worksheet rule")
    if any(
        actions.get(rule) == "accept-as-is"
        for rule in SECRET_CLASS_RULES
    ):
        raise ValueError("secret-class policy rules cannot use accept-as-is")
    return dict(actions)


def generate_policy_decisions(
    run_root: Path,
    *,
    policy_path: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Generate complete record-consistent decisions from a sealed policy."""
    with _locked_run(run_root, create_locks=False) as (state, paths):
        if state.get("phase") != "review_required":
            raise ValueError("decide requires a review_required import run")
        _verify_invariants(state, frozen=True)
        worksheet = _load_private_json(paths["worksheet"])
        policy_dir = _ensure_private_child(
            paths["worksheet"].parent, "policies", "policy directory"
        )
        copied = _copy_review_input(policy_path, policy_dir, "policy")
        sealed_policy_path = Path(state["run_root"]) / copied["relative"]
        policy = _load_private_json(sealed_policy_path)
        actions = _policy_rule_actions(policy, worksheet, state["board_id"])

        record_actions: dict[tuple[str, str], str] = {}
        for entry in worksheet.get("entries", []):
            rules = entry.get("rules")
            if not isinstance(rules, list) or not rules:
                raise ValueError("worksheet entry must contain at least one rule")
            row_action = max(
                (actions[rule] for rule in rules),
                key=ACTION_RESTRICTIVENESS.__getitem__,
            )
            record = (entry["record_type"], entry["record_id"])
            previous = record_actions.get(record, "accept-as-is")
            record_actions[record] = max(
                (previous, row_action),
                key=ACTION_RESTRICTIVENESS.__getitem__,
            )

        decisions = {
            "schema_version": 1,
            "board_id": state["board_id"],
            "worksheet_sha256": worksheet["worksheet_sha256"],
            "entry_count": len(worksheet.get("entries", [])),
            "status": POLICY_DECISIONS_STATUS,
            "policy_sha256": copied["sha256"],
            "entries": [
                {
                    **entry,
                    "decision": record_actions[
                        (entry["record_type"], entry["record_id"])
                    ],
                }
                for entry in worksheet.get("entries", [])
            ],
        }
        _decision_map(decisions, worksheet, state["board_id"])
        target = output_path or (
            paths["worksheet"].parent
            / f"policy-decisions-{copied['sha256'][:16]}.json"
        )
        payload = _canonical_bytes(decisions)
        _write_private_bytes(target, payload)
        return {
            "status": "complete",
            "entry_count": decisions["entry_count"],
            "worksheet_sha256": decisions["worksheet_sha256"],
            "policy_sha256": decisions["policy_sha256"],
            "decisions_sha256": hashlib.sha256(payload).hexdigest(),
            "output_name": Path(target).name,
        }


def _review_input_path(state: dict[str, Any], label: str) -> Path | None:
    value = state["review_inputs"].get(label)
    if value is None:
        return None
    relative = value.get("relative")
    digest = value.get("sha256")
    if (
        not isinstance(relative, str)
        or not re.fullmatch(
            rf"evidence/review-inputs/{re.escape(label)}-[0-9a-f]{{16}}\.json",
            relative,
        )
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise ValueError("review input state is invalid")
    path = Path(state["run_root"]) / relative
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise RuntimeError("sealed review input changed")
    _load_private_json(path)
    return path


def _prevalidate_review_inputs(
    state: dict[str, Any], paths: dict[str, Path], inputs: dict[str, Any]
) -> None:
    run_root = Path(state["run_root"])
    worksheet = _load_private_json(run_root / state["worksheet"]["relative"])
    decisions = inputs.get("decisions")
    if int(state["review_gate"]["quarantine_records"]):
        if decisions is None:
            raise ValueError("complete quarantine decisions are required")
        decisions_path = run_root / decisions["relative"]
        _decision_map(
            load_json(decisions_path, {}),
            worksheet,
            state["board_id"],
        )

    bindings = inputs.get("bindings")
    if int(state["review_gate"]["unmapped_legacy_agents"]) and bindings is None:
        raise ValueError("complete bind or RETIRE decisions are required")
    if bindings is not None:
        _binding_mapping(run_root / bindings["relative"], state)


def _binding_mapping(
    path: Path, state: dict[str, Any] | None = None
) -> dict[str, str]:
    raw = load_json(path, {})
    mapping = raw.get("bindings", raw) if isinstance(raw, dict) else None
    if not isinstance(mapping, dict) or not all(
        isinstance(key, str)
        and isinstance(value, str)
        and (value == "RETIRE" or CENTRAL_ID.fullmatch(value))
        for key, value in mapping.items()
    ):
        raise ValueError("binding decisions have an invalid shape")
    if state is not None:
        expected = state["identity_worksheet"]
        if (
            raw.get("schema_version") != 1
            or raw.get("board_id") != state["board_id"]
            or raw.get("identity_worksheet_sha256")
            != _load_private_json(
                Path(state["run_root"]) / expected["relative"]
            ).get("worksheet_sha256")
            or raw.get("entry_count") != len(mapping)
        ):
            raise ValueError("binding decisions do not match the identity worksheet")
    return mapping


def _validate_binding_coverage(
    central_root: Path,
    board_id: str,
    bindings_path: Path,
    state: dict[str, Any],
) -> int:
    mapping = _binding_mapping(bindings_path, state)
    store = TransactionalSQLiteStore(central_root)
    board = store.load(board_path(store, board_id), dict)
    agents = board.get("legacy_import", {}).get("agents", {})
    names = [str(item.get("agent_name") or "") for item in agents.values()]
    duplicates = {name for name, count in Counter(names).items() if count > 1}
    used: set[str] = set()
    unresolved = 0
    for record_id, item in agents.items():
        if item.get("binding_status") != "legacy_unbound":
            continue
        unresolved += 1
        exact = f"record:{record_id}"
        name = str(item.get("agent_name") or "")
        if exact in mapping:
            used.add(exact)
        elif name not in duplicates and name in mapping:
            used.add(name)
        else:
            raise ValueError("bindings must resolve or RETIRE every legacy agent")
    if set(mapping) != used:
        raise ValueError("bindings contain unused or ambiguous decisions")
    return unresolved


def _apply_review_inputs(
    state: dict[str, Any],
    central_root: Path,
    worksheet_path: Path,
    decisions_path: Path | None,
    bindings_path: Path | None,
    callback: Checkpoint | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    stages: list[dict[str, Any]] = [
        {
            "label": "before",
            "seal": _central_tree_state(central_root),
            "canonical_db_sha256": canonical_db_hash(central_root),
        }
    ]
    quarantine_result: dict[str, Any] | None = None
    if int(state["review_gate"]["quarantine_records"]):
        if decisions_path is None:
            raise ValueError("complete quarantine decisions are required")
        quarantine_result = apply_decisions(
            Path(state["run_root"]) / state["snapshot_relative"],
            central_root,
            state["board_id"],
            worksheet_path,
            decisions_path,
        )
        replay = apply_decisions(
            Path(state["run_root"]) / state["snapshot_relative"],
            central_root,
            state["board_id"],
            worksheet_path,
            decisions_path,
        )
        if replay.get("status") != "noop" or replay.get("changes") != 0:
            raise RuntimeError("quarantine decision replay was not idempotent")
        stages.append(
            {
                "label": "after_quarantine",
                "seal": _central_tree_state(central_root),
                "canonical_db_sha256": canonical_db_hash(central_root),
            }
        )
        _checkpoint(callback, "after-quarantine-review")

    store = TransactionalSQLiteStore(central_root)
    current_board = store.load(board_path(store, state["board_id"]), dict)
    current_agents = current_board.get("legacy_import", {}).get("agents", {})
    current_unmapped = sum(
        item.get("binding_status") == "legacy_unbound"
        for item in current_agents.values()
        if isinstance(item, dict)
    )
    identity_result: dict[str, Any] | None = None
    if current_unmapped:
        if bindings_path is None:
            raise ValueError("complete bind or RETIRE decisions are required")
        _validate_binding_coverage(
            central_root, state["board_id"], bindings_path, state
        )
        identity_result = bind_identities(central_root, state["board_id"], bindings_path)
        replay = bind_identities(central_root, state["board_id"], bindings_path)
        if replay.get("status") != "noop":
            raise RuntimeError("identity decision replay was not idempotent")
        stages.append(
            {
                "label": "after_identity",
                "seal": _central_tree_state(central_root),
                "canonical_db_sha256": canonical_db_hash(central_root),
            }
        )
        _checkpoint(callback, "after-identity-review")
    elif bindings_path is not None:
        identity_result = bind_identities(
            central_root, state["board_id"], bindings_path
        )
        if identity_result.get("status") != "noop":
            raise RuntimeError("identity decision replay was not idempotent")
    return quarantine_result, identity_result, stages


def _prepare_review_expectations(
    state: dict[str, Any], paths: dict[str, Path]
) -> dict[str, Any]:
    _quarantine_generated(paths["review_preview"], paths, "incomplete-review-preview")
    shutil.copytree(paths["staging"], paths["review_preview"], symlinks=False)
    os.chmod(paths["review_preview"], 0o700)
    worksheet_path = Path(state["run_root"]) / state["worksheet"]["relative"]
    decisions_path = _review_input_path(state, "decisions")
    bindings_path = _review_input_path(state, "bindings")
    quarantine, identity, stages = _apply_review_inputs(
        state,
        paths["review_preview"],
        worksheet_path,
        decisions_path,
        bindings_path,
    )
    return {
        "stages": stages,
        "quarantine_actions": (
            quarantine.get("actions", {}) if quarantine is not None else {}
        ),
        "identity": (
            {
                "bound": int(identity.get("bound", 0)),
                "retired": int(identity.get("retired", 0)),
                "unmapped": int(identity.get("unmapped", 0)),
            }
            if identity is not None
            else {"bound": 0, "retired": 0, "unmapped": 0}
        ),
    }


def _assert_allowed_review_stage(state: dict[str, Any], root: Path) -> None:
    current_seal = _central_tree_state(root)
    current_canonical = canonical_db_hash(root)
    if not any(
        item.get("seal") == current_seal
        and item.get("canonical_db_sha256") == current_canonical
        for item in state.get("review_expectations", {}).get("stages", [])
    ):
        raise RuntimeError("reviewing Central tree is not an expected durable state")


def _resume_review(
    state: dict[str, Any], paths: dict[str, Path], callback: Checkpoint | None
) -> dict[str, Any]:
    _source, destination, _stable_install_root, run_root = _context(state)
    _verify_invariants(state, frozen=True)
    if not _destination_matches_baseline(destination, state["destination_before"]):
        raise RuntimeError("central data root changed before review completion")
    worksheet_path = run_root / state["worksheet"]["relative"]
    if hashlib.sha256(worksheet_path.read_bytes()).hexdigest() != state["worksheet"]["sha256"]:
        raise RuntimeError("private quarantine worksheet changed")
    decisions_path = _review_input_path(state, "decisions")
    bindings_path = _review_input_path(state, "bindings")
    quarantine_count = int(state["review_gate"]["quarantine_records"])
    unmapped_before = int(state["review_gate"]["unmapped_legacy_agents"])

    _assert_allowed_review_stage(state, paths["staging"])
    quarantine_result, identity_result, _actual_stages = _apply_review_inputs(
        state,
        paths["staging"],
        worksheet_path,
        decisions_path,
        bindings_path,
        callback,
    )
    _assert_allowed_review_stage(state, paths["staging"])
    expected_final = state["review_expectations"]["stages"][-1]
    if (
        _central_tree_state(paths["staging"]) != expected_final["seal"]
        or canonical_db_hash(paths["staging"])
        != expected_final["canonical_db_sha256"]
    ):
        raise RuntimeError("review result differs from the sealed preview")
    if identity_result is not None:
        _write_private_json(
            paths["worksheet"].parent / "identity-binding-report.json",
            identity_result,
        )

    remaining = int(identity_result.get("unmapped", 0)) if identity_result else 0
    review_record = {
        "decisions_sha256": (
            state["review_inputs"]["decisions"]["sha256"]
            if decisions_path is not None
            else None
        ),
        "bindings_sha256": (
            state["review_inputs"]["bindings"]["sha256"]
            if bindings_path is not None
            else None
        ),
        "quarantine_records_resolved": quarantine_count,
        "bound": int(identity_result.get("bound", 0)) if identity_result else 0,
        "retired": int(identity_result.get("retired", 0)) if identity_result else 0,
        "unmapped": remaining,
        "idempotent_replay": True,
    }
    state.setdefault("review_history", []).append(review_record)
    state["stage_seal"] = _central_tree_state(paths["staging"])
    state["canonical_db_sha256"] = canonical_db_hash(paths["staging"])
    state["review_inputs"] = {}
    state["review_gate"] = {
        "quarantine_records": 0,
        "unmapped_legacy_agents": remaining,
        "resolved": remaining == 0,
    }
    state["phase"] = "reviewed" if remaining == 0 else "review_required"
    _save_state(paths, state)
    if remaining:
        return state
    _checkpoint(callback, "after-reviewed-state")
    return _start_install(state, paths, callback)


def review_import(
    run_root: Path,
    *,
    bindings_path: Path | None,
    decisions_path: Path | None,
    confirm_central_stopped: bool,
    checkpoint: Checkpoint | None = None,
) -> dict[str, Any]:
    if not confirm_central_stopped:
        raise ValueError("--confirm-central-stopped is required")
    with _locked_run(run_root, create_locks=False) as (state, paths):
        if state["phase"] == "reviewing":
            return _resume_review(state, paths, checkpoint)
        if state["phase"] != "review_required":
            raise ValueError("review requires a review_required import run")
        _verify_invariants(state, frozen=True)
        _assert_unchanged(
            "staging Central tree",
            state["stage_seal"],
            _central_tree_state(paths["staging"]),
        )
        need_quarantine = int(state["review_gate"]["quarantine_records"]) > 0
        need_bindings = int(state["review_gate"]["unmapped_legacy_agents"]) > 0
        if need_quarantine and decisions_path is None:
            raise ValueError("complete quarantine decisions are required")
        if need_bindings and bindings_path is None:
            raise ValueError("complete bind or RETIRE decisions are required")
        review_dir = _ensure_private_child(
            paths["worksheet"].parent, "review-inputs", "review input directory"
        )
        inputs: dict[str, Any] = {}
        if decisions_path is not None:
            inputs["decisions"] = _copy_review_input(
                decisions_path, review_dir, "decisions"
            )
        if bindings_path is not None:
            inputs["bindings"] = _copy_review_input(
                bindings_path, review_dir, "bindings"
            )
        _prevalidate_review_inputs(state, paths, inputs)
        state["review_inputs"] = inputs
        state["review_expectations"] = _prepare_review_expectations(state, paths)
        state["phase"] = "reviewing"
        _save_state(paths, state)
        _checkpoint(checkpoint, "after-reviewing-state")
        return _resume_review(state, paths, checkpoint)


def _start_install(
    state: dict[str, Any], paths: dict[str, Path], callback: Checkpoint | None
) -> dict[str, Any]:
    _source, destination, _stable_install_root, run_root = _context(state)
    _verify_invariants(state, frozen=True)
    _assert_unchanged(
        "staging Central tree",
        state["stage_seal"],
        _central_tree_state(paths["staging"]),
    )
    if not _destination_matches_baseline(destination, state["destination_before"]):
        raise RuntimeError("central data root changed before atomic installation")
    state["phase"] = "installing"
    _save_state(paths, state)
    _checkpoint(callback, "after-installing-state")
    return _resume_install(state, paths, callback)


def _install_receipt_value(state: dict[str, Any]) -> dict[str, Any]:
    baseline = state["destination_before"]
    installed = state["installed_seal"]
    return {
        "status": "installed",
        "board_id": state["board_id"],
        "source_copy_window_unchanged": True,
        "source_content_sha256": state["source_before"]["content_sha256"],
        "source_state_sha256": state["source_before"]["state_sha256"],
        "original_source_hash": state["provenance"]["original_source_hash"],
        "frozen_import_hash": state["provenance"]["frozen_import_hash"],
        "promoted_marker_hash": state["provenance"]["promoted_marker_hash"],
        "original_file_count": state["provenance"]["original_file_count"],
        "original_byte_count": state["provenance"]["original_byte_count"],
        "frozen_file_count": state["provenance"]["frozen_file_count"],
        "frozen_byte_count": state["provenance"]["frozen_byte_count"],
        "full_source_backup_tree_sha256": state["full_backup_seal"][
            "state_sha256"
        ],
        "full_source_backup_content_sha256": state["full_backup_seal"][
            "content_sha256"
        ],
        "full_source_backup_entries": state["full_backup_seal"]["entries"],
        "full_source_backup_bytes": state["full_backup_seal"]["bytes"],
        "live_source_passed_to_native_import": False,
        "stable_install_version": state["stable_install_before"]["version"],
        "stable_install_tree_sha256": state["stable_install_before"]["tree"][
            "state_sha256"
        ],
        "stable_install_receipt_sha256": state["stable_install_before"][
            "receipt_sha256"
        ],
        "stable_install_executable_sha256": state["stable_install_before"][
            "executable_sha256"
        ],
        "stable_install_active_link_before": state["stable_install_before"][
            "active_command_link_target"
        ],
        "stable_install_after_tree_sha256": state["stable_install_after"]["tree"][
            "state_sha256"
        ],
        "stable_install_after_receipt_sha256": state["stable_install_after"][
            "receipt_sha256"
        ],
        "stable_install_after_executable_sha256": state["stable_install_after"][
            "executable_sha256"
        ],
        "stable_install_active_link_after": state["stable_install_after"][
            "active_command_link_target"
        ],
        "central_tree_sha256": installed["state_sha256"],
        "canonical_db_sha256": state["canonical_db_sha256"],
        "backup_kind": baseline["kind"],
        "backup_tree_sha256": (
            baseline["tree"]["state_sha256"]
            if baseline["kind"] == "empty"
            else None
        ),
        "rollback_available": True,
        "remote_calls": 0,
    }


def _ensure_receipt(
    path: Path,
    expected: dict[str, Any],
    label: str,
    *,
    repair_missing: bool,
) -> None:
    if not path.exists() and not path.is_symlink():
        if not repair_missing:
            raise RuntimeError(f"{label} receipt is missing")
        _write_private_json(path, expected)
        return
    if _load_private_json(path) != expected:
        raise RuntimeError(f"{label} receipt does not match durable run state")


def _resume_install(
    state: dict[str, Any], paths: dict[str, Path], callback: Checkpoint | None
) -> dict[str, Any]:
    _source, destination, _stable_install_root, run_root = _context(state)
    baseline = state["destination_before"]
    backup = paths["backup"]
    staging = paths["staging"]
    _ensure_private_child(run_root, "backup", "Central backup directory")

    if baseline["kind"] == "empty" and not backup.exists():
        if not destination.exists():
            raise RuntimeError("empty Central baseline disappeared before backup")
        if not _destination_matches_baseline(destination, baseline):
            raise RuntimeError("central data root changed before backup")
        os.replace(destination, backup)
        _fsync_directory(destination.parent)
        _fsync_directory(backup.parent)
        _checkpoint(callback, "after-backup-move")
    elif baseline["kind"] == "absent" and backup.exists():
        raise RuntimeError("unexpected backup exists for an absent baseline")
    if baseline["kind"] == "empty":
        _assert_unchanged(
            "Central backup",
            baseline["tree"],
            tree_state(backup, reject_links=True),
        )

    if staging.exists():
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("central destination unexpectedly exists during install")
        _assert_unchanged(
            "staging Central tree",
            state["stage_seal"],
            _central_tree_state(staging),
        )
        os.replace(staging, destination)
        _fsync_directory(destination.parent)
        _fsync_directory(staging.parent)
        _checkpoint(callback, "after-destination-move")
    else:
        if not destination.exists():
            raise RuntimeError("both staging and installed Central roots are missing")
        _assert_unchanged(
            "installed Central tree",
            state["stage_seal"],
            _central_tree_state(destination),
        )

    _verify_invariants(state, frozen=True)
    installed = _central_tree_state(destination)
    _assert_unchanged("installed Central tree", state["stage_seal"], installed)
    state.update(
        {
            "phase": "installed",
            "installed_seal": installed,
            "backup": {
                "kind": baseline["kind"],
                "relative": (
                    backup.relative_to(Path(state["run_root"])).as_posix()
                    if baseline["kind"] == "empty"
                    else None
                ),
                "preserved": baseline["kind"] == "empty",
                "seal": baseline.get("tree"),
            },
            "source_copy_window_unchanged": True,
            "stable_install_import_window_unchanged": True,
        }
    )
    _write_private_json(
        Path(state["run_root"]) / "install-receipt.json",
        _install_receipt_value(state),
    )
    _save_state(paths, state)
    return state


def _installed_retry(
    state: dict[str, Any], paths: dict[str, Path]
) -> dict[str, Any]:
    _source, destination, _stable_install_root, run_root = _context(state)
    _verify_invariants(state, frozen=True)
    _ensure_receipt(
        run_root / "install-receipt.json",
        _install_receipt_value(state),
        "install",
        repair_missing=True,
    )
    _assert_unchanged(
        "installed Central tree",
        state["installed_seal"],
        _central_tree_state(destination),
    )
    before = canonical_db_hash(destination)
    result = promote(
        run_root / state["snapshot_relative"],
        destination,
        state["board_id"],
        central_url=CENTRAL_URL,
        promoted_board_root=run_root / state["promoted_relative"],
        owner_principal_id=state["owner_principal_id"],
        owner_agent_name=state["owner_agent_name"],
        generation_token_factory=_unexpected_generation_token,
    )
    after = canonical_db_hash(destination)
    if result.get("status") != "noop" or before != after:
        raise RuntimeError("installed same-source retry was not idempotent")
    current_seal = _central_tree_state(destination)
    _assert_unchanged("installed Central tree", state["installed_seal"], current_seal)
    state["retry_count"] = int(state.get("retry_count", 0)) + 1
    state["last_retry"] = {
        "status": "noop",
        "canonical_db_sha256": after,
        "source_copy_window_unchanged": True,
        "stable_install_import_window_unchanged": True,
    }
    _save_state(paths, state)
    return state


def _advance(
    state: dict[str, Any],
    paths: dict[str, Path],
    callback: Checkpoint | None = None,
) -> dict[str, Any]:
    while True:
        phase = state["phase"]
        if phase in {"initialized", "freeze_completed"}:
            state = _prepare_phase(state, paths, callback)
        elif phase == "prepared":
            state = _stage_phase(state, paths, callback)
        elif phase == "review_required":
            return state
        elif phase == "reviewing":
            state = _resume_review(state, paths, callback)
        elif phase == "reviewed":
            state = _start_install(state, paths, callback)
        elif phase == "staged":
            state = _start_install(state, paths, callback)
        elif phase == "installing":
            state = _resume_install(state, paths, callback)
        elif phase == "installed":
            return state
        elif phase == "rolling_back":
            return _resume_rollback(state, paths, callback)
        elif phase == "rolled_back":
            return state
        else:
            raise ValueError(f"unsupported import run phase: {phase}")


def start_import(
    source: Path,
    destination: Path,
    run_root: Path,
    *,
    board_id: str,
    owner_principal_id: str,
    owner_agent_name: str,
    stable_install_root: Path,
    confirm_central_stopped: bool,
    checkpoint: Checkpoint | None = None,
) -> dict[str, Any]:
    if not confirm_central_stopped:
        raise ValueError("--confirm-central-stopped is required")
    for name, value in (
        ("board_id", board_id),
        ("owner_principal_id", owner_principal_id),
        ("owner_agent_name", owner_agent_name),
    ):
        if not ID_RE.fullmatch(value):
            raise ValueError(f"{name} must match [A-Za-z0-9._-]{{1,80}}")
    source = _require_directory(source, "source .agent-mem")
    if source.name != ".agent-mem":
        raise ValueError("source must be an .agent-mem directory")
    destination = _safe_target(destination, "central data root")
    run_root = _safe_target(run_root, "run directory")
    stable_install_root = _require_directory(stable_install_root, "stable install root")
    if run_root.exists() or run_root.is_symlink():
        raise FileExistsError("run directory already exists; use retry")
    if destination.parent.stat().st_dev != run_root.parent.stat().st_dev:
        raise ValueError("run directory and central data root must share a filesystem")
    _require_disjoint(
        {
            "source": source,
            "central data root": destination,
            "stable install root": stable_install_root,
            "run directory": run_root,
        }
    )
    initializing = run_root.with_name(f".{run_root.name}.initializing")
    with _exclusive_lock(_destination_lock_path(destination), "Central import"):
        if initializing.exists():
            initializing = _require_directory(
                initializing, "initializing run directory", private=True
            )
            initial_state, initial_paths = _load_run(
                initializing, expected_run_root=run_root
            )
            expected_identity = {
                "run_root": str(run_root),
                "source": str(source),
                "destination": str(destination),
                "board_id": board_id,
                "owner_principal_id": owner_principal_id,
                "owner_agent_name": owner_agent_name,
                "stable_install_root": str(stable_install_root),
            }
            if any(initial_state.get(key) != value for key, value in expected_identity.items()):
                raise ValueError("initializing run belongs to a different import request")
        else:
            source_before = tree_state(source, reject_links=True)
            snapshot_source = _snapshot_tree_state(source)
            snapshot_import_source = _snapshot_tree_state(
                source, import_domain_only=True
            )
            stable_install_before = stable_install_state(stable_install_root)
            destination_before = _destination_baseline(destination)
            build_root = run_root.with_name(
                f".{run_root.name}.init.{secrets.token_hex(8)}"
            )
            os.mkdir(build_root, 0o700)
            os.chmod(build_root, 0o700)
            build_paths = _run_paths(build_root)
            _write_private_json(
                build_paths["owner"],
                {"kind": RUN_KIND, "schema_version": RUN_SCHEMA_VERSION},
            )
            initial_state = {
                "kind": RUN_KIND,
                "schema_version": RUN_SCHEMA_VERSION,
                "tool_version": __version__,
                "phase": "initialized",
                "run_root": str(run_root),
                "source": str(source),
                "destination": str(destination),
                "board_id": board_id,
                "owner_principal_id": owner_principal_id,
                "owner_agent_name": owner_agent_name,
                "stable_install_root": str(stable_install_root),
                "source_before": source_before,
                "snapshot_source_content_sha256": snapshot_source[0],
                "snapshot_source_state_sha256": snapshot_source[1],
                "snapshot_source_files": snapshot_source[2],
                "snapshot_source_bytes": snapshot_source[3],
                "snapshot_import_source_content_sha256": snapshot_import_source[0],
                "snapshot_import_source_state_sha256": snapshot_import_source[1],
                "snapshot_import_source_files": snapshot_import_source[2],
                "snapshot_import_source_bytes": snapshot_import_source[3],
                "stable_install_before": stable_install_before,
                "destination_before": destination_before,
                "remote_calls": 0,
                "source_import_input": "frozen-copy-only",
                "nonempty_central_target_supported": False,
            }
            _save_state(build_paths, initial_state)
            _fsync_directory(build_root)
            os.replace(build_root, initializing)
            _fsync_directory(initializing.parent)
            initial_paths = _run_paths(initializing)
        with _exclusive_lock(initial_paths["lock"], "import run"):
            _checkpoint(checkpoint, "after-initializing-state-before-run-rename")
            os.replace(initializing, run_root)
            _fsync_directory(run_root.parent)
            paths = _run_paths(run_root)
            state = _load_private_json(paths["state"])
            _checkpoint(checkpoint, "after-initialized-state")
            return _advance(state, paths, checkpoint)


def retry_import(
    run_root: Path,
    *,
    confirm_central_stopped: bool,
    checkpoint: Checkpoint | None = None,
) -> dict[str, Any]:
    if not confirm_central_stopped:
        raise ValueError("--confirm-central-stopped is required")
    run_root = _safe_target(run_root, "run directory")
    if not run_root.exists():
        initializing = run_root.with_name(f".{run_root.name}.initializing")
        state, initializing_paths = _load_run(
            initializing, expected_run_root=run_root
        )
        destination = _safe_target(Path(state["destination"]), "central data root")
        with _exclusive_lock(_destination_lock_path(destination), "Central import"):
            with _exclusive_lock(initializing_paths["lock"], "import run"):
                if run_root.exists():
                    raise RuntimeError("run directory appeared during initialization recovery")
                os.replace(initializing, run_root)
                _fsync_directory(run_root.parent)
                paths = _run_paths(run_root)
                state = _load_private_json(paths["state"])
                return _advance(state, paths, checkpoint)
    with _locked_run(run_root, create_locks=False) as (state, paths):
        if state["phase"] == "installed":
            return _installed_retry(state, paths)
        if state["phase"] == "rolled_back":
            _ensure_receipt(
                Path(state["run_root"]) / "rollback-receipt.json",
                _rollback_receipt_value(state),
                "rollback",
                repair_missing=True,
            )
            return state
        return _advance(state, paths, checkpoint)


def _rollback_receipt_value(state: dict[str, Any]) -> dict[str, Any]:
    baseline = state["destination_before"]
    rollback = state["rollback"]
    return {
        "status": "rolled-back",
        "board_id": state["board_id"],
        "baseline_kind": baseline["kind"],
        "baseline_tree_sha256": (
            baseline["tree"]["state_sha256"]
            if baseline["kind"] == "empty"
            else None
        ),
        "current_tree_preserved": True,
        "current_tree_changed_after_install": rollback[
            "current_tree_changed_after_install"
        ],
        "observed_tree_sha256": state["rollback_observed_tree"]["state_sha256"],
        "quarantined_tree_sha256": rollback["quarantined_tree"]["state_sha256"],
        "full_source_backup_tree_sha256": state["full_backup_seal"][
            "state_sha256"
        ],
        "full_source_backup_content_sha256": state["full_backup_seal"][
            "content_sha256"
        ],
        "full_source_backup_entries": state["full_backup_seal"]["entries"],
        "full_source_backup_bytes": state["full_backup_seal"]["bytes"],
        "source_copy_window_unchanged": True,
        "stable_install_import_window_unchanged": True,
        "stable_install_before_tree_sha256": state["stable_install_before"]["tree"][
            "state_sha256"
        ],
        "stable_install_after_tree_sha256": state["stable_install_after"]["tree"][
            "state_sha256"
        ],
        "remote_calls": 0,
    }


def _resume_rollback(
    state: dict[str, Any], paths: dict[str, Path], callback: Checkpoint | None
) -> dict[str, Any]:
    _source, destination, _stable_install_root, run_root = _context(state)
    rollback_target = paths["rollback"]
    backup = paths["backup"]
    _ensure_private_child(
        run_root, "rollback-quarantine", "rollback quarantine directory"
    )

    if destination.exists() and not rollback_target.exists():
        os.replace(destination, rollback_target)
        _fsync_directory(destination.parent)
        _fsync_directory(rollback_target.parent)
        _checkpoint(callback, "after-rollback-quarantine-move")
    elif not destination.exists() and not rollback_target.exists():
        raise RuntimeError("Central tree disappeared before rollback quarantine")

    quarantined = _central_tree_state(rollback_target)
    _assert_unchanged(
        "rollback-quarantined Central tree",
        state["rollback_observed_tree"],
        quarantined,
    )

    baseline = state["destination_before"]
    if baseline["kind"] == "empty":
        if destination.exists():
            if not _destination_matches_baseline(destination, baseline):
                raise RuntimeError("restored Central baseline is invalid")
        else:
            if not backup.exists():
                raise RuntimeError("Central backup is missing during rollback")
            _assert_unchanged(
                "Central backup",
                baseline["tree"],
                tree_state(backup, reject_links=True),
            )
            os.replace(backup, destination)
            _fsync_directory(destination.parent)
            _fsync_directory(backup.parent)
            _checkpoint(callback, "after-backup-restore")
        if not _destination_matches_baseline(destination, baseline):
            raise RuntimeError("restored Central baseline differs from backup receipt")
    else:
        if destination.exists():
            raise RuntimeError("absent Central baseline was not restored")

    _verify_invariants(state, frozen=True)
    state.update(
        {
            "phase": "rolled_back",
            "rollback": {
                "current_tree_preserved": True,
                "current_tree_changed_after_install": state[
                    "rollback_observed_changed"
                ],
                "quarantined_tree": quarantined,
                "baseline_restored": True,
                "baseline_kind": baseline["kind"],
            },
            "source_copy_window_unchanged": True,
            "stable_install_import_window_unchanged": True,
        }
    )
    receipt = _rollback_receipt_value(state)
    _write_private_json(Path(state["run_root"]) / "rollback-receipt.json", receipt)
    _save_state(paths, state)
    return state


def rollback_import(
    run_root: Path,
    *,
    confirm_central_stopped: bool,
    checkpoint: Checkpoint | None = None,
) -> dict[str, Any]:
    if not confirm_central_stopped:
        raise ValueError("--confirm-central-stopped is required")
    with _locked_run(run_root, create_locks=False) as (state, paths):
        if state["phase"] == "rolled_back":
            _ensure_receipt(
                Path(state["run_root"]) / "rollback-receipt.json",
                _rollback_receipt_value(state),
                "rollback",
                repair_missing=True,
            )
            return state
        if state["phase"] == "rolling_back":
            return _resume_rollback(state, paths, checkpoint)
        if state["phase"] != "installed":
            raise ValueError("rollback requires a completed installation")
        _verify_invariants(state, frozen=True)
        _require_directory(Path(state["destination"]), "installed Central data root")
        current = _central_tree_state(Path(state["destination"]))
        state["phase"] = "rolling_back"
        state["rollback_observed_tree"] = current
        state["rollback_observed_changed"] = current != state["installed_seal"]
        _save_state(paths, state)
        _checkpoint(checkpoint, "after-rolling-back-state")
        return _resume_rollback(state, paths, checkpoint)


def status_import(
    run_root: Path, *, confirm_central_stopped: bool
) -> dict[str, Any]:
    """Read-only integrity check for a durable run."""
    if not confirm_central_stopped:
        raise ValueError("--confirm-central-stopped is required")
    with _locked_run(run_root, create_locks=False) as (state, paths):
        phase = state["phase"]
        _source, destination, _stable_install_root, _run_root = _context(state)
        if phase == "initialized":
            _verify_invariants(state, live_source=True, stable_install=True)
            if not _destination_matches_baseline(
                destination, state["destination_before"]
            ):
                raise RuntimeError("central baseline changed before snapshot")
        elif phase == "freeze_completed":
            _verify_completed_freeze(state, paths)
            _verify_invariants(state, stable_install=True)
            if not _destination_matches_baseline(
                destination, state["destination_before"]
            ):
                raise RuntimeError("central baseline changed before snapshot commit")
        elif phase == "prepared":
            _verify_invariants(state, frozen=True)
            if not _destination_matches_baseline(
                destination, state["destination_before"]
            ):
                raise RuntimeError("central baseline changed before staging")
        elif phase in {"review_required", "reviewed"}:
            _verify_invariants(state, frozen=True)
            _assert_unchanged(
                "staging Central tree",
                state["stage_seal"],
                _central_tree_state(paths["staging"]),
            )
            if not _destination_matches_baseline(
                destination, state["destination_before"]
            ):
                raise RuntimeError("central baseline changed before reviewed install")
            if phase == "reviewed" and not state["review_gate"]["resolved"]:
                raise RuntimeError("reviewed state has unresolved review work")
        elif phase == "reviewing":
            _verify_invariants(state, frozen=True)
            if not _destination_matches_baseline(
                destination, state["destination_before"]
            ):
                raise RuntimeError("central baseline changed during review")
            for label in state.get("review_inputs", {}):
                _review_input_path(state, label)
            _assert_allowed_review_stage(state, paths["staging"])
        elif phase == "staged":
            _verify_invariants(state, frozen=True)
            _assert_unchanged(
                "staging Central tree",
                state["stage_seal"],
                _central_tree_state(paths["staging"]),
            )
            if not _destination_matches_baseline(
                destination, state["destination_before"]
            ):
                raise RuntimeError("central baseline changed before installation")
        elif phase == "installing":
            _verify_invariants(state, frozen=True)
            candidates = []
            if paths["staging"].exists():
                candidates.append(_central_tree_state(paths["staging"]))
            if destination.exists():
                candidates.append(_central_tree_state(destination))
            if state["stage_seal"] not in candidates:
                raise RuntimeError("installing state has no intact Central candidate")
            if state["destination_before"]["kind"] == "empty" and paths[
                "backup"
            ].exists():
                _assert_unchanged(
                    "Central backup",
                    state["destination_before"]["tree"],
                    tree_state(paths["backup"], reject_links=True),
                )
        elif phase == "installed":
            _verify_invariants(state, frozen=True)
            _assert_unchanged(
                "installed Central tree",
                state["installed_seal"],
                _central_tree_state(destination),
            )
            if state["destination_before"]["kind"] == "empty":
                _assert_unchanged(
                    "Central backup",
                    state["destination_before"]["tree"],
                    tree_state(paths["backup"], reject_links=True),
                )
            _ensure_receipt(
                Path(state["run_root"]) / "install-receipt.json",
                _install_receipt_value(state),
                "install",
                repair_missing=False,
            )
        elif phase == "rolling_back":
            _verify_invariants(state, frozen=True)
            if paths["rollback"].exists():
                _assert_unchanged(
                    "rollback-quarantined Central tree",
                    state["rollback_observed_tree"],
                    _central_tree_state(paths["rollback"]),
                )
            elif destination.exists():
                _assert_unchanged(
                    "rollback-observed Central tree",
                    state["rollback_observed_tree"],
                    _central_tree_state(destination),
                )
            else:
                raise RuntimeError("rolling-back state lost its Central tree")
        elif phase == "rolled_back":
            _verify_invariants(state, frozen=True)
            if not _destination_matches_baseline(
                destination, state["destination_before"]
            ):
                raise RuntimeError("rolled-back Central baseline is not restored")
            _assert_unchanged(
                "rollback-quarantined Central tree",
                state["rollback"]["quarantined_tree"],
                _central_tree_state(paths["rollback"]),
            )
            _ensure_receipt(
                Path(state["run_root"]) / "rollback-receipt.json",
                _rollback_receipt_value(state),
                "rollback",
                repair_missing=False,
            )
        else:
            raise ValueError(f"unsupported import run phase: {phase}")
        state = dict(state)
        state["integrity"] = "verified"
        return state


def public_summary(state: dict[str, Any]) -> dict[str, Any]:
    result = {
        "status": state["phase"],
        "board_id": state["board_id"],
        "source_import_input": state["source_import_input"],
        "source_copy_window_unchanged": bool(
            state.get("source_copy_window_unchanged", False)
        ),
        "stable_v4_installed_version": state["stable_install_before"]["version"],
        "stable_install_import_window_unchanged": bool(
            state.get("stable_install_import_window_unchanged", False)
        ),
        "remote_calls": state["remote_calls"],
    }
    if "canonical_db_sha256" in state:
        result["canonical_db_sha256"] = state["canonical_db_sha256"]
    if state.get("phase") in {"review_required", "reviewing"}:
        result["review_required"] = {
            "quarantine_records": int(state["review_gate"]["quarantine_records"]),
            "unmapped_legacy_agents": int(
                state["review_gate"]["unmapped_legacy_agents"]
            ),
            "worksheet": "private-run-evidence",
            "identity_binding_template": "private-run-evidence",
        }
    if "rollback" in state:
        result["rollback"] = {
            "baseline_restored": state["rollback"]["baseline_restored"],
            "current_tree_preserved": state["rollback"]["current_tree_preserved"],
            "current_tree_changed_after_install": state["rollback"][
                "current_tree_changed_after_install"
            ],
        }
    return result


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_code": "INVALID_ARGUMENTS",
                    "message": "Command arguments are invalid; use --help for the contract.",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)


def _sanitized_validation_reason(error: ValueError) -> str:
    """Return a bounded operator-facing reason without paths or secrets."""
    try:
        reason = " ".join(str(error).split())
        reason, _ = scrub(reason, Policy(mode="redact"))
        reason = _WINDOWS_ABSOLUTE_PATH_RE.sub("[REDACTED:PATH]", reason)
        reason = _POSIX_ABSOLUTE_PATH_RE.sub("[REDACTED:PATH]", reason)
        reason = reason[:CLI_REASON_MAX_CHARS].strip()
    except Exception:
        reason = ""
    return reason or "Input validation failed."


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Import a stable local board copy into an offline Personal Central root."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("import")
    start.add_argument("source", type=Path)
    start.add_argument("central_data_root", type=Path)
    start.add_argument("--run-dir", required=True, type=Path)
    start.add_argument("--board-id", required=True)
    start.add_argument("--owner-principal-id", required=True)
    start.add_argument("--owner-agent-name", required=True)
    start.add_argument("--stable-install-root", required=True, type=Path)
    start.add_argument("--confirm-central-stopped", action="store_true")
    for name in ("retry", "rollback"):
        command = subparsers.add_parser(name)
        command.add_argument("run_dir", type=Path)
        command.add_argument("--confirm-central-stopped", action="store_true")
    review = subparsers.add_parser("review")
    review.add_argument("run_dir", type=Path)
    review.add_argument("--bindings", type=Path)
    review.add_argument("--decisions", type=Path)
    review.add_argument("--confirm-central-stopped", action="store_true")
    decide = subparsers.add_parser("decide")
    decide.add_argument("run_dir", type=Path)
    decide.add_argument("--policy", required=True, type=Path)
    decide.add_argument("--output", type=Path)
    status = subparsers.add_parser("status")
    status.add_argument("run_dir", type=Path)
    status.add_argument("--confirm-central-stopped", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "import":
            state = start_import(
                args.source,
                args.central_data_root,
                args.run_dir,
                board_id=args.board_id,
                owner_principal_id=args.owner_principal_id,
                owner_agent_name=args.owner_agent_name,
                stable_install_root=args.stable_install_root,
                confirm_central_stopped=args.confirm_central_stopped,
            )
        elif args.command == "retry":
            state = retry_import(
                args.run_dir,
                confirm_central_stopped=args.confirm_central_stopped,
            )
        elif args.command == "rollback":
            state = rollback_import(
                args.run_dir,
                confirm_central_stopped=args.confirm_central_stopped,
            )
        elif args.command == "review":
            state = review_import(
                args.run_dir,
                bindings_path=args.bindings,
                decisions_path=args.decisions,
                confirm_central_stopped=args.confirm_central_stopped,
            )
        elif args.command == "decide":
            result = generate_policy_decisions(
                args.run_dir,
                policy_path=args.policy,
                output_path=args.output,
            )
            print(json.dumps(result, sort_keys=True))
            return
        else:
            state = status_import(
                args.run_dir,
                confirm_central_stopped=args.confirm_central_stopped,
            )
        print(json.dumps(public_summary(state), sort_keys=True))
    except ValueError as error:
        payload = {
            "status": "error",
            "error_code": "IMPORT_FAILED",
            "message": (
                "Import could not be completed safely. Inspect the private run "
                "state, correct the input, and retry."
            ),
            "reason": _sanitized_validation_reason(error),
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        payload = {
            "status": "error",
            "error_code": "IMPORT_FAILED",
            "message": (
                "Import could not be completed safely. Inspect the private run "
                "state, correct the input, and retry."
            ),
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
