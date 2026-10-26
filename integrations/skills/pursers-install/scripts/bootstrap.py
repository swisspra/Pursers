#!/usr/bin/env python3
"""Plan, apply, or inspect one private Pursers installation."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

RELEASE = "5.2.1"
SCHEMA_VERSION = 1
RECEIPT_NAME = ".pursers-install.json"
STEP_IDS = ("private-venv", "released-package", "central-init")
CAPABILITIES = ["private virtual environment", "released Pursers package", "local Central initialization"]
DEFERRED = ["Central service start", "MCP or host configuration", "Fleet and autonomous harness activation", "board authorization and first connection"]
CENTRAL_FILES = (
    "profile.env",
    "signing-key.pem",
    "jwks.json",
    "admin.jwt",
    "worker.jwt",
)
Runner = Callable[..., subprocess.CompletedProcess[str]]


class BootstrapError(RuntimeError):
    """A bootstrap operation cannot safely continue."""


def _absolute_root(value: Path) -> Path:
    root = value.expanduser().absolute()
    if root == Path(root.anchor) or root.parent == Path(root.anchor):
        raise BootstrapError("installation root must be a private directory below a filesystem root")
    for candidate in (root, *root.parents):
        if os.path.lexists(candidate) and candidate.is_symlink():
            raise BootstrapError("installation root and its parents must not be symlinks")
    return root


def _receipt_path(root: Path) -> Path:
    return root / RECEIPT_NAME


def _read_receipt(root: Path) -> dict[str, Any] | None:
    path = _receipt_path(root)
    if not os.path.lexists(path):
        return None
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_mode & 0o077:
            raise BootstrapError("installation receipt is not a private regular file")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except BootstrapError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("cannot read installation receipt") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("root") != str(root)
        or payload.get("release") != RELEASE
        or not isinstance(payload.get("steps"), dict)
        or any(payload["steps"].get(step) not in {"pending", "in_progress", "complete", "failed"} for step in STEP_IDS)
    ):
        raise BootstrapError("installation receipt is invalid or belongs to another root")
    return payload


def _write_receipt(root: Path, receipt: dict[str, Any]) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".pursers-install.", suffix=".tmp", dir=root)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _receipt_path(root))
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise BootstrapError("cannot persist private installation receipt") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _installed(root: Path) -> bool:
    environment = root / ".venv"
    package_paths = [environment / "Lib" / "site-packages"]
    package_paths.extend((environment / "lib").glob("python*/site-packages"))
    for distribution in importlib.metadata.distributions(
        path=[str(path) for path in package_paths if path.is_dir()]
    ):
        if (distribution.metadata.get("Name") or "").lower() == "pursers":
            return distribution.version == RELEASE
    return False


def _initialized(root: Path) -> bool:
    central = root / "central"
    if central.is_symlink() or not central.is_dir():
        return False
    for name in CENTRAL_FILES:
        path = central / name
        if path.is_symlink() or not path.is_file():
            return False
    return True


def _owned(root: Path) -> dict[str, Any] | None:
    if not root.exists():
        return None
    if root.is_symlink() or not root.is_dir():
        raise BootstrapError("installation root is not a regular directory")
    return _read_receipt(root)


def plan(root_value: Path) -> dict[str, Any]:
    root = _absolute_root(root_value)
    receipt = _owned(root)
    blockers: list[str] = []
    if root.exists() and receipt is None:
        blockers.append("installation root already exists and is not owned by this bootstrap")
    steps = []
    for step in STEP_IDS:
        status = receipt["steps"][step] if receipt else "planned"
        if step == "central-init" and receipt and _initialized(root):
            status = "complete"
        steps.append({"id": step, "status": status})
    return {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "release": RELEASE,
        "steps": steps,
        "blockers": blockers,
        "next_action": (
            "Use $pursers-start to connect an existing installation."
            if blockers
            else "Review this plan, then run apply --apply for this root."
        ),
        "ready_for_work": False,
        "capabilities": CAPABILITIES,
        "deferred": DEFERRED,
    }


def _python_path(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _central_command(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "pursers-central.exe"
    return root / ".venv" / "bin" / "pursers-central"


def _run(runner: Runner, command: Sequence[str]) -> None:
    try:
        result = runner(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise BootstrapError("a required local setup command could not start") from exc
    if result.returncode:
        # Never forward child output: it can contain credentials or environment values.
        raise BootstrapError(f"setup step failed: exit status {result.returncode}")


def _set_step(root: Path, receipt: dict[str, Any], step: str, status: str) -> None:
    receipt["steps"][step] = status
    _write_receipt(root, receipt)


def apply(
    root_value: Path,
    *,
    apply: bool,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    if not apply:
        raise BootstrapError("apply requires --apply")
    root = _absolute_root(root_value)
    existing = _owned(root)
    if root.exists() and existing is None:
        raise BootstrapError("installation root already exists and is not owned by this bootstrap")
    if existing is None:
        root.parent.mkdir(parents=True, exist_ok=True)
        # Recheck after parent creation, before claiming the leaf.
        root = _absolute_root(root)
        try:
            root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise BootstrapError("installation root appeared during apply; no files changed") from exc
        os.chmod(root, 0o700)
        receipt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "root": str(root),
            "release": RELEASE,
            "steps": {step: "pending" for step in STEP_IDS},
        }
        _write_receipt(root, receipt)
    else:
        receipt = existing
        if (root.stat().st_mode & 0o077) != 0:
            raise BootstrapError("owned installation root permissions must be 0700")

    try:
        if receipt["steps"]["private-venv"] != "complete":
            if (root / ".venv").exists():
                if not _python_path(root).is_file():
                    raise BootstrapError("private virtual environment is incomplete; preserving it")
            else:
                _set_step(root, receipt, "private-venv", "in_progress")
                _run(runner, [sys.executable, "-m", "venv", str(root / ".venv")])
            _set_step(root, receipt, "private-venv", "complete")

        if receipt["steps"]["released-package"] != "complete" or not _installed(root):
            _set_step(root, receipt, "released-package", "in_progress")
            _run(
                runner,
                [str(_python_path(root)), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", f"pursers=={RELEASE}"],
            )
            _set_step(root, receipt, "released-package", "complete")

        if receipt["steps"]["central-init"] != "complete":
            if _initialized(root):
                _set_step(root, receipt, "central-init", "complete")
            else:
                central = root / "central"
                if os.path.lexists(central):
                    raise BootstrapError("Central initialization is partial or foreign; preserving it for review")
                _set_step(root, receipt, "central-init", "in_progress")
                _run(runner, [str(_central_command(root)), "init", str(central)])
                if not _initialized(root):
                    raise BootstrapError("Central init returned without all expected private files")
                _set_step(root, receipt, "central-init", "complete")
    except BootstrapError as exc:
        failed = next((step for step in STEP_IDS if receipt["steps"][step] == "in_progress"), None)
        if failed:
            _set_step(root, receipt, failed, "failed")
            receipt["last_error"] = str(exc)
            _write_receipt(root, receipt)
        raise

    return doctor(root)


def doctor(root_value: Path) -> dict[str, Any]:
    root = _absolute_root(root_value)
    receipt = _owned(root)
    installed = bool(
        receipt
        and receipt["steps"]["released-package"] == "complete"
        and _python_path(root).is_file()
        and _installed(root)
    )
    initialized = _initialized(root)
    return {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "release": RELEASE,
        "owned": receipt is not None,
        "installed": installed,
        "initialized": initialized,
        "running": "unknown",
        "service_started_by_helper": False,
        "ready_for_work": False,
        "capabilities": CAPABILITIES,
        "deferred": DEFERRED,
        "next_action": "$pursers-start for authorized connection and identity setup.",
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply", "doctor"):
        child = commands.add_parser(name)
        child.add_argument("--root", required=True, type=Path)
        if name == "apply":
            child.add_argument("--apply", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            payload = plan(args.root)
            _emit(payload)
            return 2 if payload["blockers"] else 0
        if args.command == "apply":
            _emit(apply(args.root, apply=args.apply))
            return 0
        _emit(doctor(args.root))
        return 0
    except BootstrapError as exc:
        print(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "root": str(args.root.expanduser().absolute()),
            "error": str(exc),
            "ready_for_work": False,
        }, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
