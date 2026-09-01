"""Owned, hash-bound macOS service and host-config integration."""

from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import json
import os
import plistlib
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


ENTRY_NAME = "pursers-personal"
LAUNCHCTL_PATH = "/bin/launchctl"
RECEIPT_NAME = "integration-receipt.json"
LOCK_NAME = "integration.lock"
MAX_CONFIG_BYTES = 4 * 1024 * 1024
LABEL_RE = re.compile(r"^com\.onboard\.personal\.[a-f0-9]{32}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NO_PRECONDITION = object()
_RENAME_SWAP = 0x00000002
_RENAME_EXCL = 0x00000004
_RENAME_NOFOLLOW_ANY = 0x00000010


class IntegrationError(RuntimeError):
    """Raised when an integration target is unsafe, unowned, or drifted."""


class _PreserveTemporary(IntegrationError):
    """Internal signal that a recovery file must not be removed."""


@dataclass(frozen=True)
class IntegrationPlan:
    label: str
    profile_path: Path
    console_path: Path
    service_target: Path
    host_target: Path
    host_id: str
    session: str
    console_sha256: str
    console_mode: int
    service_payload: bytes
    host_payload: bytes
    receipt_path: Path

    def safe_summary(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "profile_path": str(self.profile_path),
            "service_target": str(self.service_target),
            "host_target": str(self.host_target),
            "host_id": self.host_id,
            "session": self.session,
            "console_sha256": self.console_sha256,
            "console_mode": self.console_mode,
            "entry": ENTRY_NAME,
            "service_sha256": _sha256(self.service_payload),
            "host_sha256": _sha256(self.host_payload),
            "receipt_path": str(self.receipt_path),
            "contains_credential": False,
        }


def service_label(profile_id: str) -> str:
    label = f"com.onboard.personal.{profile_id}"
    if not LABEL_RE.fullmatch(label):
        raise IntegrationError("profile_id cannot form a safe Personal service label")
    if label.startswith("com.onboard.central"):
        raise AssertionError("Personal service label collides with Central development")
    return label


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_nofollow() -> None:
    if not _O_NOFOLLOW:
        raise IntegrationError("this platform does not provide O_NOFOLLOW")


def _owned(value: os.stat_result, label: str) -> None:
    getuid = getattr(os, "getuid", None)
    if getuid is not None and value.st_uid != getuid():
        raise IntegrationError(f"{label} is not owned by the current user")


def _open_directory(path: Path) -> int:
    _require_nofollow()
    try:
        descriptor = os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError as exc:
        raise IntegrationError(f"cannot securely open directory: {path}") from exc
    value = os.fstat(descriptor)
    if not stat.S_ISDIR(value.st_mode):
        os.close(descriptor)
        raise IntegrationError(f"target parent is not a directory: {path}")
    _owned(value, str(path))
    return descriptor


def _read_regular(path: Path, *, max_bytes: int = MAX_CONFIG_BYTES) -> bytes:
    parent_fd = _open_directory(path.parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(path.name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=parent_fd)
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise IntegrationError(f"target must be a single-link regular file: {path}")
        _owned(value, str(path))
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise IntegrationError(f"target is larger than {max_bytes} bytes: {path}")
        return b"".join(chunks)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise IntegrationError(f"cannot securely read target: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _snapshot(path: Path) -> dict[str, Any]:
    try:
        payload = _read_regular(path)
    except FileNotFoundError:
        return {"exists": False, "sha256": None, "content_b64": None, "mode": None}
    mode = stat.S_IMODE(path.lstat().st_mode)
    return {
        "exists": True,
        "sha256": _sha256(payload),
        "content_b64": base64.b64encode(payload).decode("ascii"),
        "mode": mode,
    }


def _scrub_terminal_backups(receipt: dict[str, Any]) -> None:
    """Drop rollback payloads only after an integration reaches a terminal state."""
    for item in receipt.get("targets", []):
        if not isinstance(item, dict):
            continue
        before = item.get("before")
        if isinstance(before, dict):
            before["content_b64"] = None


def _console_snapshot(path: Path) -> dict[str, Any]:
    value = _snapshot(path)
    mode = value.get("mode")
    if not value["exists"] or not isinstance(mode, int):
        raise IntegrationError("installed pursers-personal console is unavailable")
    if not mode & stat.S_IXUSR or mode & 0o022:
        raise IntegrationError(
            "installed pursers-personal console must be owner-executable and not group/world writable"
        )
    return value


def _same_snapshot(current: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return (
        bool(current.get("exists")) == bool(expected.get("exists"))
        and current.get("sha256") == expected.get("sha256")
        and current.get("mode") == expected.get("mode")
    )


def _renameatx(
    from_fd: int, from_name: str, to_fd: int, to_name: str, flags: int
) -> None:
    """Call macOS renameatx_np or fail closed before replacing a managed file."""
    try:
        function = ctypes.CDLL(None, use_errno=True).renameatx_np
    except AttributeError as exc:
        raise IntegrationError(
            "atomic integration compare-and-swap is unavailable on this platform"
        ) from exc
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        from_fd,
        os.fsencode(from_name),
        to_fd,
        os.fsencode(to_name),
        flags,
    )
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _replace_if_unchanged(
    parent_fd: int,
    parent: Path,
    temporary: str,
    target: str,
    expected: Mapping[str, Any],
) -> None:
    """Atomically install new bytes or restore a raced preimage without loss."""
    flags = _RENAME_NOFOLLOW_ANY
    if not expected.get("exists"):
        try:
            _renameatx(
                parent_fd,
                temporary,
                parent_fd,
                target,
                flags | _RENAME_EXCL,
            )
        except OSError as exc:
            raise IntegrationError(
                "integration target appeared while the update was being prepared"
            ) from exc
        return

    installed = _snapshot(parent / temporary)
    try:
        _renameatx(
            parent_fd,
            temporary,
            parent_fd,
            target,
            flags | _RENAME_SWAP,
        )
    except OSError as exc:
        raise IntegrationError("atomic integration swap failed") from exc
    swapped = True
    try:
        displaced = _snapshot(parent / temporary)
        if not _same_snapshot(displaced, expected):
            raise IntegrationError(
                "integration target changed while the update was being prepared"
            )
    except BaseException:
        if swapped:
            try:
                _renameatx(
                    parent_fd,
                    temporary,
                    parent_fd,
                    target,
                    flags | _RENAME_SWAP,
                )
            except OSError as restore_error:
                raise _PreserveTemporary(
                    f"atomic integration rollback failed; recovery file retained as {temporary}"
                ) from restore_error
            swapped = False
            recovered = _snapshot(parent / temporary)
            if not _same_snapshot(recovered, installed):
                raise _PreserveTemporary(
                    f"concurrent integration versions retained; recovery file is {temporary}"
                )
        raise
    os.unlink(temporary, dir_fd=parent_fd)


def _atomic_write(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    expected_before: Mapping[str, Any] | object = _NO_PRECONDITION,
) -> None:
    if len(payload) > MAX_CONFIG_BYTES:
        raise IntegrationError("integration payload is too large")
    parent_fd = _open_directory(path.parent)
    temporary = f".{path.name}.{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    preserve_temporary = False
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                raise IntegrationError(f"refusing unsafe integration target: {path}")
            _owned(existing, str(path))
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
            mode,
            dir_fd=parent_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if expected_before is _NO_PRECONDITION:
            os.replace(
                temporary,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        else:
            assert isinstance(expected_before, Mapping)
            try:
                _replace_if_unchanged(
                    parent_fd,
                    path.parent,
                    temporary,
                    path.name,
                    expected_before,
                )
            except _PreserveTemporary:
                preserve_temporary = True
                raise
        os.fsync(parent_fd)
    except OSError as exc:
        raise IntegrationError(f"cannot atomically write integration target: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not preserve_temporary:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


@contextmanager
def _integration_lock(profile_path: Path):
    """Serialize the complete receipt/service/host-config transaction."""
    parent = profile_path.expanduser().absolute().parent
    parent_fd = _open_directory(parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            LOCK_NAME,
            os.O_RDWR | os.O_CREAT | _O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise IntegrationError("integration lock is unsafe")
        _owned(value, "integration lock")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IntegrationError("another Personal integration operation is active") from exc
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _remove_if_unchanged(path: Path, expected: Mapping[str, Any]) -> None:
    """Atomically remove only the expected bytes, retaining raced bytes."""
    if not expected.get("exists"):
        raise IntegrationError("remove precondition must describe an existing target")
    parent_fd = _open_directory(path.parent)
    recovery = f".{path.name}.{secrets.token_hex(12)}.recovery"
    preserve_recovery = False
    try:
        try:
            _renameatx(
                parent_fd,
                path.name,
                parent_fd,
                recovery,
                _RENAME_NOFOLLOW_ANY | _RENAME_EXCL,
            )
        except OSError as exc:
            raise IntegrationError(
                "managed integration target disappeared during removal"
            ) from exc
        moved = _snapshot(path.parent / recovery)
        if not _same_snapshot(moved, expected):
            try:
                _renameatx(
                    parent_fd,
                    recovery,
                    parent_fd,
                    path.name,
                    _RENAME_NOFOLLOW_ANY | _RENAME_EXCL,
                )
            except OSError as restore_error:
                preserve_recovery = True
                raise _PreserveTemporary(
                    f"concurrent integration versions retained; recovery file is {recovery}"
                ) from restore_error
            raise IntegrationError(
                "integration target changed while removal was being prepared"
            )
        os.unlink(recovery, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        raise IntegrationError(f"cannot remove integration target: {path}") from exc
    finally:
        if not preserve_recovery:
            try:
                os.unlink(recovery, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def _safe_identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise IntegrationError(f"{label} must use 1-120 safe identifier characters")
    return value


def _service_payload(label: str, console: Path, profile_path: Path) -> bytes:
    document = {
        "Label": label,
        "ProgramArguments": [
            str(console),
            "central",
            "--profile",
            str(profile_path),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
    }
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)


def _host_payload(
    current: bytes | None,
    *,
    console: Path,
    profile_path: Path,
    host_id: str,
    session: str,
) -> bytes:
    if current is None:
        document: dict[str, Any] = {}
    else:
        try:
            document = json.loads(current.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationError("host config is not valid UTF-8 JSON") from exc
        if not isinstance(document, dict):
            raise IntegrationError("host config root must be an object")
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise IntegrationError("host config mcpServers must be an object")
    if ENTRY_NAME in servers:
        raise IntegrationError("unowned pursers-personal host entry already exists")
    servers[ENTRY_NAME] = {
        "command": str(console),
        "args": [
            "mcp",
            "--profile",
            str(profile_path),
            "--host-id",
            host_id,
            "--session",
            session,
        ],
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def prepare_integration(
    profile: Any,
    *,
    console_path: Path,
    launch_agents_dir: Path,
    host_config_path: Path,
    host_id: str,
    session: str,
) -> IntegrationPlan:
    """Build a no-secret integration plan without changing either target."""
    label = service_label(str(profile.profile_id))
    console = console_path.expanduser().absolute()
    profile_path = Path(profile.profile_path).expanduser().absolute()
    if not console.is_file() or console.is_symlink():
        raise IntegrationError("installed pursers-personal console is unavailable or unsafe")
    if not profile_path.is_file() or profile_path.is_symlink():
        raise IntegrationError("selected profile is unavailable or unsafe")
    host_value = _safe_identity(host_id, "host_id")
    session_value = _safe_identity(session, "session")
    service_parent = launch_agents_dir.expanduser().absolute()
    host_target = host_config_path.expanduser().absolute()
    _open = _open_directory(service_parent)
    os.close(_open)
    _open = _open_directory(host_target.parent)
    os.close(_open)
    service_target = service_parent / f"{label}.plist"
    service_before = _snapshot(service_target)
    if service_before["exists"]:
        raise IntegrationError("unowned Personal service target already exists")
    host_before = _snapshot(host_target)
    current_host = (
        base64.b64decode(host_before["content_b64"])
        if host_before["exists"]
        else None
    )
    host_payload = _host_payload(
        current_host,
        console=console,
        profile_path=profile_path,
        host_id=host_value,
        session=session_value,
    )
    console_state = _console_snapshot(console)
    return IntegrationPlan(
        label=label,
        profile_path=profile_path,
        console_path=console,
        service_target=service_target,
        host_target=host_target,
        host_id=host_value,
        session=session_value,
        console_sha256=str(console_state["sha256"]),
        console_mode=int(console_state["mode"]),
        service_payload=_service_payload(label, console, profile_path),
        host_payload=host_payload,
        receipt_path=profile_path.parent / RECEIPT_NAME,
    )


def _receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_regular(path).decode("utf-8"))
    except FileNotFoundError:
        raise IntegrationError("integration receipt does not exist")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError("integration receipt is invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise IntegrationError("integration receipt schema is unsupported")
    return value


def apply_integration(plan: IntegrationPlan) -> dict[str, Any]:
    with _integration_lock(plan.profile_path):
        return _apply_integration_locked(plan)


def _apply_integration_locked(plan: IntegrationPlan) -> dict[str, Any]:
    """Atomically own both targets, compensating if the second write fails."""
    if plan.receipt_path.exists() or plan.receipt_path.is_symlink():
        existing = _receipt(plan.receipt_path)
        if existing.get("state") == "applied":
            _verify_applied(existing)
            _verify_plan_matches_receipt(plan, existing)
            return {"status": "existing", "label": plan.label, "receipt": str(plan.receipt_path)}
        if existing.get("state") == "preparing":
            raise IntegrationError("interrupted integration requires rollback recovery")
        if existing.get("state") not in {"rolled_back", "uninstalled"}:
            raise IntegrationError("integration receipt has an unknown state")

    service_before = _snapshot(plan.service_target)
    host_before = _snapshot(plan.host_target)
    if service_before["exists"]:
        raise IntegrationError("Personal service target appeared after planning")
    current_host = (
        base64.b64decode(host_before["content_b64"])
        if host_before["exists"]
        else None
    )
    expected_host = _host_payload(
        current_host,
        console=plan.console_path,
        profile_path=plan.profile_path,
        host_id=plan.host_id,
        session=plan.session,
    )
    if expected_host != plan.host_payload:
        raise IntegrationError("host config changed after planning")

    service_mode = 0o600
    host_mode = int(host_before["mode"] or 0o600)
    receipt = {
        "schema_version": 1,
        "state": "preparing",
        "label": plan.label,
        "entry": ENTRY_NAME,
        "profile_path": str(plan.profile_path),
        "console_path": str(plan.console_path),
        "console_sha256": plan.console_sha256,
        "console_mode": plan.console_mode,
        "host_id": plan.host_id,
        "session": plan.session,
        "targets": [
            {
                "kind": "service",
                "path": str(plan.service_target),
                "before": service_before,
                "applied_sha256": _sha256(plan.service_payload),
                "applied_mode": service_mode,
            },
            {
                "kind": "host-config",
                "path": str(plan.host_target),
                "before": host_before,
                "applied_sha256": _sha256(plan.host_payload),
                "applied_mode": host_mode,
            },
        ],
    }
    receipt_payload = lambda: (
        json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    receipt_before = _snapshot(plan.receipt_path)
    _atomic_write(
        plan.receipt_path,
        receipt_payload(),
        mode=0o600,
        expected_before=receipt_before,
    )
    preparing_receipt = _snapshot(plan.receipt_path)
    try:
        _atomic_write(
            plan.service_target,
            plan.service_payload,
            mode=service_mode,
            expected_before=service_before,
        )
        _atomic_write(
            plan.host_target,
            plan.host_payload,
            mode=host_mode,
            expected_before=host_before,
        )
    except BaseException:
        _restore_if_applied(
            plan.host_target, host_before, _sha256(plan.host_payload), host_mode
        )
        _restore_if_applied(
            plan.service_target,
            service_before,
            _sha256(plan.service_payload),
            service_mode,
        )
        receipt["state"] = "rolled_back"
        _scrub_terminal_backups(receipt)
        _atomic_write(
            plan.receipt_path,
            receipt_payload(),
            mode=0o600,
            expected_before=preparing_receipt,
        )
        raise
    receipt["state"] = "applied"
    _atomic_write(
        plan.receipt_path,
        receipt_payload(),
        mode=0o600,
        expected_before=preparing_receipt,
    )
    _verify_applied(receipt)
    return {"status": "applied", "label": plan.label, "receipt": str(plan.receipt_path)}


def _verify_applied(receipt: Mapping[str, Any]) -> None:
    console_path = receipt.get("console_path")
    if not isinstance(console_path, str) or not console_path:
        raise IntegrationError("integration receipt console is invalid")
    console = _console_snapshot(Path(console_path))
    if (
        console["sha256"] != receipt.get("console_sha256")
        or console["mode"] != receipt.get("console_mode")
    ):
        raise IntegrationError("managed pursers-personal console drifted")
    targets = receipt.get("targets")
    if not isinstance(targets, list) or len(targets) != 2:
        raise IntegrationError("integration receipt targets are invalid")
    for item in targets:
        if not isinstance(item, dict):
            raise IntegrationError("integration receipt target is invalid")
        current = _snapshot(Path(str(item.get("path", ""))))
        if (
            not current["exists"]
            or current["sha256"] != item.get("applied_sha256")
            or current["mode"] != item.get("applied_mode")
        ):
            raise IntegrationError("managed integration target drifted")


def _verify_plan_matches_receipt(
    plan: IntegrationPlan, receipt: Mapping[str, Any]
) -> None:
    expected = {
        "label": plan.label,
        "profile_path": str(plan.profile_path),
        "console_path": str(plan.console_path),
        "console_sha256": plan.console_sha256,
        "console_mode": plan.console_mode,
        "host_id": plan.host_id,
        "session": plan.session,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise IntegrationError(
            "requested profile, console, host, or session differs from installed integration"
        )
    targets = receipt.get("targets")
    if not isinstance(targets, list):
        raise IntegrationError("integration receipt targets are invalid")
    by_kind = {
        item.get("kind"): item for item in targets if isinstance(item, dict)
    }
    expected_targets = {
        "service": (plan.service_target, plan.service_payload),
        "host-config": (plan.host_target, plan.host_payload),
    }
    for kind, (path, payload) in expected_targets.items():
        item = by_kind.get(kind)
        if (
            not isinstance(item, dict)
            or item.get("path") != str(path)
            or item.get("applied_sha256") != _sha256(payload)
        ):
            raise IntegrationError("requested integration differs from installed integration")


def _restore(
    path: Path,
    before: Mapping[str, Any],
    *,
    expected_before: Mapping[str, Any],
) -> None:
    if before.get("exists"):
        encoded = before.get("content_b64")
        if not isinstance(encoded, str):
            raise IntegrationError("receipt backup is missing")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise IntegrationError("receipt backup is invalid") from exc
        if _sha256(payload) != before.get("sha256"):
            raise IntegrationError("receipt backup hash is invalid")
        _atomic_write(
            path,
            payload,
            mode=int(before.get("mode") or 0o600),
            expected_before=expected_before,
        )
    else:
        _remove_if_unchanged(path, expected_before)


def _restore_if_applied(
    path: Path,
    before: Mapping[str, Any],
    applied_sha256: str,
    applied_mode: int,
) -> None:
    """Compensate only our bytes; preserve any concurrent external update."""
    current = _snapshot(path)
    if _same_snapshot(current, before):
        return
    if (
        current.get("exists")
        and current.get("sha256") == applied_sha256
        and current.get("mode") == applied_mode
    ):
        _restore(path, before, expected_before=current)


def rollback_integration(profile_path: Path, *, terminal_state: str = "rolled_back") -> dict[str, Any]:
    with _integration_lock(profile_path):
        return _rollback_integration_locked(
            profile_path, terminal_state=terminal_state
        )


def _rollback_integration_locked(
    profile_path: Path, *, terminal_state: str = "rolled_back"
) -> dict[str, Any]:
    """Restore exact prior bytes, refusing to overwrite post-apply drift."""
    if terminal_state not in {"rolled_back", "uninstalled"}:
        raise ValueError("terminal_state must be rolled_back or uninstalled")
    receipt_path = profile_path.expanduser().absolute().parent / RECEIPT_NAME
    receipt = _receipt(receipt_path)
    receipt_before = _snapshot(receipt_path)
    if receipt.get("state") in {"rolled_back", "uninstalled"}:
        return {"status": "existing", "state": receipt["state"], "profile_retained": True}
    if receipt.get("state") not in {"applied", "preparing", "rolling_back"}:
        raise IntegrationError("integration receipt is not applied")
    initial_state = str(receipt.get("state"))
    preparing = initial_state == "preparing"
    if initial_state == "applied":
        _verify_applied(receipt)
    if initial_state == "rolling_back":
        recorded_terminal = receipt.get("rollback_terminal_state")
        if recorded_terminal != terminal_state:
            raise IntegrationError(
                f"rollback is already in progress toward {recorded_terminal}"
            )
    else:
        receipt["state"] = "rolling_back"
        receipt["rollback_terminal_state"] = terminal_state
        _atomic_write(
            receipt_path,
            (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
            expected_before=receipt_before,
        )
        receipt_before = _snapshot(receipt_path)
    targets = list(receipt["targets"])
    for item in reversed(targets):
        path = Path(item["path"])
        current_snapshot = _snapshot(path)
        before = item["before"]
        before_matches = _same_snapshot(current_snapshot, before)
        applied_matches = (
            current_snapshot["exists"]
            and current_snapshot["sha256"] == item.get("applied_sha256")
            and current_snapshot["mode"] == item.get("applied_mode")
        )
        if before_matches and (preparing or initial_state == "rolling_back"):
            continue
        if not applied_matches:
            raise IntegrationError("managed integration target drifted")
        _restore(path, before, expected_before=current_snapshot)
        restored_snapshot = _snapshot(path)
        if not _same_snapshot(restored_snapshot, before):
            raise IntegrationError("restored integration target did not verify")
    receipt["state"] = terminal_state
    receipt.pop("rollback_terminal_state", None)
    _scrub_terminal_backups(receipt)
    _atomic_write(
        receipt_path,
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o600,
        expected_before=receipt_before,
    )
    return {"status": "complete", "state": terminal_state, "profile_retained": True}


def uninstall_integration(profile_path: Path) -> dict[str, Any]:
    return rollback_integration(profile_path, terminal_state="uninstalled")


def integration_status(profile_path: Path) -> dict[str, Any]:
    """Return receipt/target state without exposing backed-up config bytes."""
    receipt_path = profile_path.expanduser().absolute().parent / RECEIPT_NAME
    try:
        receipt = _receipt(receipt_path)
    except IntegrationError as exc:
        if "does not exist" in str(exc):
            return {"state": "not-installed", "receipt": str(receipt_path)}
        raise
    state = receipt.get("state")
    if state == "applied":
        _verify_applied(receipt)
    targets = receipt.get("targets", [])
    return {
        "state": state,
        "label": receipt.get("label"),
        "profile_path": receipt.get("profile_path"),
        "console_path": receipt.get("console_path"),
        "host_id": receipt.get("host_id"),
        "session": receipt.get("session"),
        "receipt": str(receipt_path),
        "targets": [
            {"kind": item.get("kind"), "path": item.get("path")}
            for item in targets
            if isinstance(item, dict)
        ],
        "profile_retained": True,
    }


def launchctl_commands(label: str, plist_path: Path) -> dict[str, list[str]]:
    """Return exact owned-label commands; no command is executed here."""
    if not LABEL_RE.fullmatch(label):
        raise IntegrationError("unsafe Personal service label")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"
    return {
        "start": [LAUNCHCTL_PATH, "bootstrap", domain, str(plist_path)],
        "restart": [LAUNCHCTL_PATH, "kickstart", "-k", service],
        "stop": [LAUNCHCTL_PATH, "bootout", service],
    }
