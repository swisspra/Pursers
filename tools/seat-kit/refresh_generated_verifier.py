#!/usr/bin/env python3
"""Safely refresh one idle generated seat's CLI from the canonical seat-kit template.

The command is dry-run by default. It never opens credential files or contacts
Central. Apply only after an independent operator check confirms there is no live
work/review lease and the seat process is stopped or queued for its next child.
Only the effective generated CLI is replaced: bin/board.py, or the verified
board-verifier-current.py when board.sh routes through it. Identity, launcher and
every credential path stay untouched. Every applied source is snapshotted before
an atomic replacement.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import seat_new  # noqa: E402


class RefreshError(ValueError):
    pass


def _regular(path: Path, *, executable: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RefreshError(f"required file unavailable: {path.name}") from exc
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_mode & 0o022
            or executable and not os.access(path, os.X_OK)):
        raise RefreshError(f"unsafe file layout: {path.name}")
    return info


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_identity(path: Path, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> tuple[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RefreshError(f"identity hints unavailable: {path.name}") from exc
    found: dict[str, str] = {}
    for label, pattern in patterns:
        matches = [match.group(1) for line in lines if (match := pattern.search(line))]
        if len(matches) != 1:
            raise RefreshError(f"unknown identity hints in {path.name}: {label}")
        found[label] = matches[0]
    return found["title"], found["identity declaration"]


def _literal(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise RefreshError(f"unknown generated CLI layout: missing {name}")


def _validate_cli(path: Path, expected_role: str) -> str | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise RefreshError(f"generated CLI is not valid Python: {path.name}") from exc
    allowed_top_level = (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef,
                         ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.Expr, ast.Try)
    if any(not isinstance(node, allowed_top_level) for node in tree.body):
        raise RefreshError(f"unknown generated CLI top-level layout: {path.name}")
    if (not any(isinstance(node, ast.FunctionDef) and node.name == "_parser" for node in tree.body)
            or not any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body)
            or not any(isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                       and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
                       and any(isinstance(value, ast.Constant) and value.value == "__main__"
                               for value in node.test.comparators) for node in tree.body)):
        raise RefreshError(f"unknown generated CLI command layout: {path.name}")
    role = _literal(tree, "ROLE")
    repo_leaf = _literal(tree, "REPO_LEAF")
    old_wait = _literal(tree, "DEFAULT_WAIT_S")
    if role != expected_role or (repo_leaf is not None and not isinstance(repo_leaf, str)):
        raise RefreshError(f"generated CLI role/repository identity does not match seat hints: {path.name}")
    if type(old_wait) is not int:
        raise RefreshError(f"generated CLI wait profile is unknown: {path.name}")
    parser_function = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_parser"),
        None,
    )
    role_branch = next((node for node in ast.walk(parser_function) if isinstance(node, ast.If)
                        and isinstance(node.test, ast.Compare)
                        and isinstance(node.test.left, ast.Name) and node.test.left.id == "ROLE"
                        and len(node.test.comparators) == 1
                        and isinstance(node.test.comparators[0], ast.Constant)
                        and node.test.comparators[0].value == "worker"), None) if parser_function else None
    if role_branch is None:
        raise RefreshError(f"generated CLI role-specific parser layout is unknown: {path.name}")

    def parser_commands(statements: list[ast.stmt]) -> set[str]:
        if any(isinstance(node, ast.If) for statement in statements for node in ast.walk(statement)):
            raise RefreshError(f"generated CLI role parser contains unknown conditionals: {path.name}")
        return {
            node.args[0].value for statement in statements for node in ast.walk(statement)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser" and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
        }

    worker_commands = parser_commands(role_branch.body)
    reviewer_commands = parser_commands(role_branch.orelse)
    if not worker_commands or not reviewer_commands:
        raise RefreshError(f"generated CLI role-specific command lists are empty: {path.name}")
    reviewer_only = {"verify", "approve", "reject", "review-claim", "review-release", "list-all"}
    if expected_role == "worker" and worker_commands.intersection(reviewer_only):
        raise RefreshError(f"worker CLI exposes reviewer commands: {path.name}")
    if expected_role == "reviewer" and "verify" not in reviewer_commands:
        raise RefreshError(f"reviewer CLI is missing verification: {path.name}")
    return repo_leaf


def _inspect(seat_dir: Path) -> dict[str, Any]:
    if (not seat_dir.is_absolute() or seat_dir.is_symlink() or not seat_dir.is_dir()
            or seat_dir.resolve() != seat_dir):
        raise RefreshError("seat directory must be an existing canonical nonsymlink directory")
    bin_dir = seat_dir / "bin"
    if bin_dir.is_symlink() or not bin_dir.is_dir():
        raise RefreshError("generated bin directory is missing or unsafe")
    agents, hints = seat_dir / "AGENTS.md", seat_dir / ".goosehints"
    shell, cli = bin_dir / "board.sh", bin_dir / "board.py"
    for path in (agents, hints, shell, cli):
        _regular(path, executable=path == shell)
    if not seat_new._is_generated_goose_seat(agents, shell):
        raise RefreshError("seat is not a recognized generated Goose seat")
    if agents.read_bytes() != hints.read_bytes():
        raise RefreshError("AGENTS.md and .goosehints differ; refusing unknown identity state")
    role_rows = re.findall(r"(?m)^Role: `(worker|reviewer)`\s*$", agents.read_text(encoding="utf-8"))
    identity, declared = _read_identity(agents, seat_new._GOOSE_AGENTS_IDENTITY_PATTERNS)
    if len(role_rows) != 1 or identity != declared:
        raise RefreshError("seat role or folder identity is ambiguous")
    agent_line = seat_new._source_line(shell, seat_new._SHELL_AGENT_RE)
    if agent_line is None or agent_line[1] != identity:
        raise RefreshError("launcher agent identity does not match seat folder")
    try:
        shell_text = shell.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RefreshError("generated launcher is unreadable") from exc
    required_exports = (
        "ONBOARD_CENTRAL_URL", "ONBOARD_BOARD_ID", "PURSERS_BOARDS",
        "PURSERS_CAN_WORK", "PURSERS_CAN_REVIEW", "PURSERS_HOST",
    )
    for key in required_exports:
        if len(re.findall(rf"(?m)^export {key}=", shell_text)) != 1:
            raise RefreshError(f"launcher has unknown {key} configuration")
    token_lines = re.findall(r"(?m)^TOKEN_FILE=\$\{PURSERS_TOKEN_FILE:-([^}]+)\}$", shell_text)
    if len(token_lines) != 1:
        raise RefreshError("launcher credential path is not a recognized file reference")
    # Only inspect the credential path's metadata. Never read, hash or copy token bytes.
    token_path = Path(token_lines[0].strip("'\""))
    if not token_path.is_absolute():
        raise RefreshError("launcher credential path is not absolute")
    _regular(token_path)
    repo_leaf = _validate_cli(cli, role_rows[0])
    routed_current = "board-verifier-current.py" in shell_text
    effective_cli = bin_dir / "board-verifier-current.py" if routed_current else cli
    if routed_current:
        matching_routes = re.findall(
            r'(?m)^\s*exec\s+[^\n]*"\$SCRIPT_DIR/board-verifier-current\.py"\s+"\$@"\s*$',
            shell_text,
        )
        if len(matching_routes) != 1:
            raise RefreshError("current verifier launcher route is unknown")
        _regular(effective_cli)
        _validate_cli(effective_cli, role_rows[0])
    client = seat_new._source_line(agents, seat_new._CLIENT_RE)
    if client is None or client[1] != "goose":
        raise RefreshError("seat client is not Goose")
    return {
        "seat": str(seat_dir), "identity": identity, "role": role_rows[0],
        "repo_leaf": repo_leaf, "wait_s": seat_new.CLIENT_PROFILES["goose"][1],
        "files": {"AGENTS.md": _sha(agents), ".goosehints": _sha(hints),
                  "board.sh": _sha(shell), "board.py": _sha(cli),
                  effective_cli.name: _sha(effective_cli)},
        "cli": cli, "effective_cli": effective_cli,
        "mode": stat.S_IMODE(cli.stat().st_mode),
        "effective_mode": stat.S_IMODE(effective_cli.stat().st_mode),
        "migrates_effective_verifier": routed_current,
        "central_url_set": bool(re.search(r"(?m)^export ONBOARD_CENTRAL_URL=.+$", shell_text)),
        "board_set": bool(re.search(r"(?m)^export ONBOARD_BOARD_ID=.+$", shell_text)),
    }


def _snapshot(snapshot_dir: Path, filename: str, payload: bytes) -> Path:
    digest = hashlib.sha256(payload).hexdigest()
    backup = snapshot_dir / f"{filename}.{digest}.bak"
    if backup.exists():
        if backup.is_symlink() or not backup.is_file() or _sha(backup) != digest:
            raise RefreshError(f"existing snapshot conflicts with source hash: {filename}")
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(backup, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    return backup


def _atomic_write(path: Path, payload: bytes, mode: int, prefix: str) -> None:
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=prefix, dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def refresh(seat_dir: Path, *, apply: bool, idle_confirmed: bool) -> dict[str, Any]:
    inventory = _inspect(seat_dir)
    cli: Path = inventory.pop("cli")
    effective_cli: Path = inventory.pop("effective_cli")
    generated = seat_new._board_python(inventory["role"], inventory["repo_leaf"], inventory["wait_s"])
    before = effective_cli.read_bytes()
    after = generated.encode("utf-8")
    updates = [(effective_cli, before, after, inventory["effective_mode"])]
    changed_files = [
        {"path": str(path), "before_sha256": hashlib.sha256(old).hexdigest(),
         "candidate_sha256": hashlib.sha256(new).hexdigest(), "changed": old != new}
        for path, old, new, _mode in updates
    ]
    changed = any(item["changed"] for item in changed_files)
    record: dict[str, Any] = {
        **inventory, "target": str(effective_cli), "before_sha256": hashlib.sha256(before).hexdigest(),
        "files_changed": changed_files,
        "candidate_sha256": hashlib.sha256(after).hexdigest(), "changed": changed,
        "applied": False, "backups": [],
    }
    if not apply or not changed:
        record["after_sha256"] = record["before_sha256"]
        return record
    if not idle_confirmed:
        raise RefreshError("apply requires --idle-confirmed after operator lease/process check")
    # Refuse a concurrent edit between inventory and mutation.
    if effective_cli.read_bytes() != before:
        raise RefreshError("generated verifier changed during refresh preflight")
    state_dir = seat_dir / ".pursers"
    if state_dir.exists():
        state_info = state_dir.lstat()
        if (state_dir.is_symlink() or not stat.S_ISDIR(state_info.st_mode)
                or state_info.st_uid != os.getuid() or state_info.st_mode & 0o022):
            raise RefreshError("snapshot parent directory is unsafe")
    else:
        state_dir.mkdir(mode=0o700)
    snapshot_dir = state_dir / "verifier-refresh"
    if snapshot_dir.exists():
        snapshot_info = snapshot_dir.lstat()
        if (snapshot_dir.is_symlink() or not stat.S_ISDIR(snapshot_info.st_mode)
                or snapshot_info.st_uid != os.getuid() or snapshot_info.st_mode & 0o022):
            raise RefreshError("snapshot directory is unsafe")
    else:
        snapshot_dir.mkdir(mode=0o700)
    snapshot_dir.chmod(0o700)
    backups = [_snapshot(snapshot_dir, path.name, old) for path, old, new, _mode in updates
               if old != new]
    for path, _old, new, mode in updates:
        if new != _old:
            _atomic_write(path, new, mode, f".{path.name}.refresh-")
    record.update({"applied": True, "backups": [str(path) for path in backups]})
    record["after_sha256"] = _sha(effective_cli)
    if record["after_sha256"] != record["candidate_sha256"]:
        raise RefreshError("post-write hash did not match generated candidate")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seat", type=Path, help="one generated Goose seat directory")
    parser.add_argument("--apply", action="store_true", help="write the canonical CLI; default is dry-run")
    parser.add_argument("--idle-confirmed", action="store_true",
                        help="operator confirms no live lease/current child before --apply")
    args = parser.parse_args(argv)
    try:
        result = refresh(args.seat, apply=args.apply, idle_confirmed=args.idle_confirmed)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, RefreshError, ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
