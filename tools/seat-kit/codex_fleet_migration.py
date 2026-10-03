#!/usr/bin/env python3
"""Preview and confirm ownership handoff from one legacy Codex supervisor.

Confirmation writes an owner-only marker only. It never invokes launchctl,
signals a process, edits the legacy supervisor, or starts a seat. The operator
performs the live cutover after reviewing the stable process/lease inventory.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA = "pursers_codex_fleet_migration_v1"
PLAN_SCHEMA = "pursers_codex_fleet_migration_plan_v1"
MARKER_SCHEMA = "pursers_codex_fleet_controller_v1"
SAFE_SEAT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
FIELDS = {
    "schema", "executor_config", "local_config", "lease_snapshot",
    "legacy_supervisor_command", "legacy_supervisor_pid_file", "controller_marker",
}
CODEX_VALUE_FLAGS = {
    "-a", "--ask-for-approval", "-c", "--config", "--color", "-i", "--image",
    "-m", "--model", "-o", "--output-last-message", "--profile", "-s", "--sandbox",
    "--add-dir",
}
CODEX_SWITCH_FLAGS = {
    "--dangerously-bypass-approvals-and-sandbox", "--ephemeral", "--json",
    "--skip-git-repo-check",
}


class MigrationError(ValueError):
    pass


def _executor_api() -> Mapping[str, Any]:
    path = Path(__file__).with_name("fleet_executor.py")
    spec = importlib.util.spec_from_file_location("codex_migration_executor", path)
    if spec is None or spec.loader is None:
        raise MigrationError("executor_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.__dict__


def _private_json(path: Path, label: str) -> dict[str, Any]:
    try:
        info = path.lstat()
        if (
            path.is_symlink() or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_mode & 0o077 or info.st_size > 1024 * 1024
        ):
            raise MigrationError(f"{label}_untrusted")
        value = json.loads(path.read_text(encoding="utf-8"))
    except MigrationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"{label}_unavailable") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"{label}_invalid")
    return value


def _absolute(value: Any, field: str) -> Path:
    if not isinstance(value, str):
        raise MigrationError(f"{field}_invalid")
    path = Path(value)
    if not path.is_absolute():
        raise MigrationError(f"{field}_invalid")
    return path


def _spec(path: Path) -> dict[str, Any]:
    value = _private_json(path, "migration_spec")
    if set(value) != FIELDS or value.get("schema") != SCHEMA:
        raise MigrationError("migration_spec_invalid")
    normalized = {"schema": SCHEMA}
    for field in sorted(FIELDS - {"schema"}):
        normalized[field] = str(_absolute(value[field], field))
    return normalized


def _process_argv(command: str) -> list[str]:
    """Parse only the stable Codex option prefix, never free-form prompt text."""
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    argv: list[str] = []
    codex_exec = False
    while True:
        token = lexer.get_token()
        if token is None:
            return argv
        argv.append(token)
        if not codex_exec:
            codex_exec = token == "exec" and _is_codex_process(argv)
            continue
        flag = token.split("=", 1)[0]
        if flag in ("-C", "--cd"):
            if "=" in token:
                if not token.split("=", 1)[1]:
                    raise ValueError("missing Codex seat root")
                return argv
            root = lexer.get_token()
            if root is None:
                raise ValueError("missing Codex seat root")
            argv.append(root)
            return argv
        if flag in CODEX_VALUE_FLAGS:
            if "=" not in token:
                value = lexer.get_token()
                if value is None:
                    raise ValueError("missing Codex option value")
                argv.append(value)
            continue
        if token in CODEX_SWITCH_FLAGS:
            continue
        if token == "--" or not token.startswith("-"):
            return argv
        raise ValueError("unknown Codex option before seat root")


def _process_rows(output: str) -> list[dict[str, Any]]:
    rows = []
    for raw in output.splitlines():
        fields = raw.strip().split(None, 2)
        if len(fields) != 3 or not all(item.isdigit() for item in fields[:2]):
            continue
        try:
            argv = _process_argv(fields[2])
        except ValueError:
            if "codex" in fields[2].lower() and re.search(r"\bexec\b", fields[2]):
                rows.append({
                    "pid": int(fields[0]), "ppid": int(fields[1]), "argv": [],
                    "ambiguous_codex": True,
                })
            continue
        rows.append({
            "pid": int(fields[0]), "ppid": int(fields[1]), "argv": argv,
            "ambiguous_codex": False,
        })
    return rows


def _seat_root(argv: Sequence[str]) -> str | None:
    for flag in ("-C", "--cd"):
        indexes = [index for index, value in enumerate(argv) if value == flag]
        if len(indexes) != 1 or indexes[0] + 1 >= len(argv):
            continue
        return str(Path(argv[indexes[0] + 1]).resolve(strict=False))
    return None


def _is_codex_process(argv: Sequence[str]) -> bool:
    if not argv or "exec" not in argv:
        return False
    executable = Path(argv[0]).name.lower()
    if "codex" in executable:
        return True
    # npm/homebrew wrappers are commonly a Node process whose script path names
    # Codex, followed by the Rust Codex child. Count the parent/child chain once.
    return executable.startswith("node") and any(
        "codex" in Path(item).name.lower() for item in argv[1:4]
    )


def _trusted_legacy_file(path: Path, label: str, *, executable: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise MigrationError(f"{label}_unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or (executable and not os.access(path, os.X_OK))
    ):
        raise MigrationError(f"{label}_untrusted")


def _runs_script(argv: Sequence[str], script: str) -> bool:
    if not argv:
        return False
    if str(Path(argv[0]).resolve(strict=False)) == script:
        return True
    return Path(argv[0]).name in {"bash", "dash", "sh", "zsh"} and script in argv[1:3]


def _inventory(spec: Mapping[str, Any], ps_output: str) -> dict[str, Any]:
    api = _executor_api()
    policy = api["load_policy"](Path(spec["executor_config"]))
    local = _private_json(Path(spec["local_config"]), "local_config")
    if set(local) not in (
        {"templates", "providers"},
        {"templates", "providers", "host_headroom"},
    ) or set(local["templates"]) != set(policy.templates):
        raise MigrationError("local_config_bindings_invalid")
    leases = _private_json(Path(spec["lease_snapshot"]), "lease_snapshot")
    if set(leases) != {"boards"} or not isinstance(leases["boards"], dict):
        raise MigrationError("lease_snapshot_invalid")
    rows = _process_rows(ps_output)
    supervisor_command = Path(spec["legacy_supervisor_command"])
    supervisor_pid_file = Path(spec["legacy_supervisor_pid_file"])
    _trusted_legacy_file(supervisor_command, "legacy_supervisor_command", executable=True)
    _trusted_legacy_file(supervisor_pid_file, "legacy_supervisor_pid")
    supervisor_path = str(supervisor_command.resolve(strict=False))
    supervisor_pids = [row["pid"] for row in rows if _runs_script(row["argv"], supervisor_path)]
    try:
        pid_file_value = int(supervisor_pid_file.read_text().strip())
    except (OSError, ValueError) as exc:
        raise MigrationError("legacy_supervisor_pid_unavailable") from exc
    blockers: list[str] = []
    if any(row["ambiguous_codex"] for row in rows):
        blockers.append("codex_process_inventory_ambiguous")
    if supervisor_pids != [pid_file_value]:
        blockers.append("legacy_controller_identity_unknown_or_duplicate")
    process_by_root: dict[str, list[tuple[int, int]]] = {}
    for row in rows:
        root = _seat_root(row["argv"])
        if root is not None and _is_codex_process(row["argv"]):
            process_by_root.setdefault(root, []).append((row["pid"], row["ppid"]))
    now = datetime.now(timezone.utc)
    seats = []
    for template_id, template in sorted(policy.templates.items()):
        binding = local["templates"].get(template_id)
        if (
            not isinstance(binding, dict)
            or not {"board_id", "provider", "seat_id"} <= set(binding)
            or set(binding) - {"board_id", "provider", "enabled", "seat_id"}
            or not isinstance(binding.get("seat_id"), str)
            or SAFE_SEAT_ID.fullmatch(binding["seat_id"]) is None
            or type(binding.get("enabled", True)) is not bool
        ):
            raise MigrationError("local_config_bindings_invalid")
        enabled = binding.get("enabled", True)
        root = str(template.seat_root.resolve(strict=False))
        seat_id = binding["seat_id"]
        process_chain = process_by_root.get(root, [])
        chain_pids = {pid for pid, _ppid in process_chain}
        pids = sorted(pid for pid, ppid in process_chain if ppid not in chain_pids)
        if len(pids) > 1:
            blockers.append(f"duplicate_process:{template_id}")
        lease_known, live = True, False
        board = leases["boards"].get(binding["board_id"])
        record = (
            board.get("seats", {}).get(seat_id)
            if isinstance(board, dict)
            else None
        )
        if not isinstance(record, dict) or set(record) != {"stale_after", "work", "review"}:
            lease_known = False
        else:
            try:
                stale_after = datetime.fromisoformat(record["stale_after"].replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError):
                lease_known = False
            else:
                if stale_after.tzinfo is None or stale_after < now:
                    lease_known = False
                live = record.get("work") is True or record.get("review") is True
        if not lease_known:
            blockers.append(f"lease_evidence_unknown:{template_id}")
        if live:
            blockers.append(f"live_lease:{template_id}")
        seats.append({
            "template_id": template_id, "seat_id": seat_id,
            "enabled": enabled, "process_count": len(pids), "pids": pids,
            "child_process_count": max(0, len(process_chain) - len(pids)),
            "lease_known": lease_known, "live_lease": live,
            "cutover_action": "remain_disabled" if not enabled else "operator_restart_after_controller_activation",
        })
    return {
        "legacy_controller_count": len(supervisor_pids),
        "legacy_controller_pid_matches": supervisor_pids == [pid_file_value],
        "managed_process_count": sum(item["process_count"] for item in seats),
        "seats": seats,
        "blockers": sorted(set(blockers)),
    }


def _ps() -> str:
    return subprocess.run(
        ["/bin/ps", "-Ao", "pid=,ppid=,command="], check=True,
        text=True, capture_output=True,
    ).stdout


def preview(
    spec_path: Path,
    output: Path,
    *,
    process_source: Callable[[], str] = _ps,
) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise MigrationError("plan_output_exists")
    spec = _spec(spec_path)
    inventory = _inventory(spec, process_source())
    material = {"spec": spec, "inventory": inventory}
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    plan = {
        "schema": PLAN_SCHEMA, "digest": digest,
        "confirmation": f"CONFIRM-{digest}", "destructive_ready": not inventory["blockers"],
        **material,
        "operator_actions": [
            "stop_legacy_supervisor", "stop_only_idle_enabled_legacy_seats",
            "activate_fleet_executor_and_butler", "verify_single_controller",
        ] if not inventory["blockers"] else [],
    }
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(plan, handle, sort_keys=True)
        handle.write("\n")
    return plan


def confirm(
    plan_path: Path,
    confirmation: str,
    *,
    process_source: Callable[[], str] = _ps,
) -> dict[str, Any]:
    plan = _private_json(plan_path, "migration_plan")
    if plan.get("schema") != PLAN_SCHEMA or confirmation != plan.get("confirmation"):
        raise MigrationError("confirmation_mismatch")
    if plan.get("destructive_ready") is not True or plan.get("inventory", {}).get("blockers"):
        raise MigrationError("migration_blocked")
    current = _inventory(plan["spec"], process_source())
    if current != plan["inventory"]:
        raise MigrationError("migration_inventory_changed")
    marker_path = Path(plan["spec"]["controller_marker"])
    marker = {
        "schema": MARKER_SCHEMA, "plan_digest": plan["digest"],
        "controller": "signed_fleet_executor", "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "disabled_seats": sorted(
            item["seat_id"] for item in current["seats"] if not item["enabled"]
        ),
    }
    if marker_path.exists():
        prior = _private_json(marker_path, "controller_marker")
        if prior.get("plan_digest") != plan["digest"]:
            raise MigrationError("controller_already_owned_by_other_plan")
        return {"ok": True, "duplicate": True, "marker": str(marker_path)}
    marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(marker, handle, sort_keys=True)
        handle.write("\n")
    return {"ok": True, "duplicate": False, "marker": str(marker_path), "live_actions": []}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("preview")
    plan.add_argument("--spec", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    apply = commands.add_parser("confirm")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    result = preview(args.spec, args.output) if args.command == "preview" else confirm(args.plan, args.confirm)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
