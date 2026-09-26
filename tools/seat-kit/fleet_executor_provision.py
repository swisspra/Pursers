#!/usr/bin/env python3
"""Plan and confirm owner-only macOS Fleet Executor provisioning.

This tool stages files only.  It never invokes ``launchctl`` and never creates
board credentials, authorization envelopes, or seat templates.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import plistlib
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


MODULE_PATH = Path(__file__).with_name("fleet_executor.py")
SPEC = importlib.util.spec_from_file_location("fleet_executor_provision_runtime", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("fleet_executor_module_unavailable")
executor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = executor
SPEC.loader.exec_module(executor)

PROVISION_SCHEMA = "pursers_fleet_executor_provision_v1"
PLAN_SCHEMA = "pursers_fleet_executor_provision_plan_v1"
SPEC_FIELDS = frozenset({"schema", "executor", "caller", "policy", "board_butler"})
EXECUTOR_FIELDS = frozenset(
    {"python", "repository", "config_path", "state_dir", "socket_path", "launch_agent_path"}
)
CALLER_FIELDS = frozenset({"key_id", "private_key_path"})
POLICY_FIELDS = frozenset(
    {
        "authorization_fingerprint_sha256",
        "templates",
        "credential_paths",
        "repository_roots",
        "seat_roots",
        "board_caps",
        "host_cap",
    }
)
BUTLER_FIELDS = frozenset(
    {"launch_agent_path", "observation_file", "state_file"}
)


class ProvisionError(ValueError):
    pass


def _absolute(value: Any, field: str, *, allow_symlink: bool = False) -> Path:
    if not isinstance(value, str):
        raise ProvisionError(f"{field}_invalid")
    path = Path(value).expanduser()
    if not path.is_absolute() or (path.is_symlink() and not allow_symlink):
        raise ProvisionError(f"{field}_invalid")
    return path.resolve(strict=False)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProvisionError("document_invalid")
    return value


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(executor.canonical_json(value)).hexdigest()


def _validate_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != SPEC_FIELDS or value.get("schema") != PROVISION_SCHEMA:
        raise ProvisionError("provision_spec_fields_invalid")
    executor_value = value.get("executor")
    caller = value.get("caller")
    policy = value.get("policy")
    butler = value.get("board_butler")
    if not isinstance(executor_value, dict) or set(executor_value) != EXECUTOR_FIELDS:
        raise ProvisionError("executor_fields_invalid")
    if not isinstance(caller, dict) or set(caller) != CALLER_FIELDS:
        raise ProvisionError("caller_fields_invalid")
    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
        raise ProvisionError("policy_fields_invalid")
    if not isinstance(butler, dict) or set(butler) != BUTLER_FIELDS:
        raise ProvisionError("board_butler_fields_invalid")

    python = _absolute(
        executor_value["python"], "executor_python", allow_symlink=True
    )
    repository = _absolute(executor_value["repository"], "executor_repository")
    config_path = _absolute(executor_value["config_path"], "executor_config_path")
    state_dir = _absolute(executor_value["state_dir"], "executor_state_dir")
    socket_path = _absolute(executor_value["socket_path"], "executor_socket_path")
    executor_plist = _absolute(
        executor_value["launch_agent_path"], "executor_launch_agent_path"
    )
    key_id = executor._require_id(caller.get("key_id"), "caller_key_id")
    private_key = _absolute(caller["private_key_path"], "caller_private_key_path")
    butler_plist = _absolute(butler["launch_agent_path"], "butler_launch_agent_path")
    observation = _absolute(butler["observation_file"], "butler_observation_file")
    state_file = _absolute(butler["state_file"], "butler_state_file")
    if not python.is_file() or not repository.is_dir():
        raise ProvisionError("executor_runtime_unavailable")
    source = repository / "tools/seat-kit/fleet_executor.py"
    if not source.is_file() or source.resolve() != MODULE_PATH.resolve():
        raise ProvisionError("executor_repository_mismatch")
    if not butler_plist.is_file() or butler_plist.is_symlink():
        raise ProvisionError("board_butler_launch_agent_unavailable")
    try:
        plist = plistlib.loads(butler_plist.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ProvisionError("board_butler_launch_agent_invalid") from exc
    if not isinstance(plist, dict) or not isinstance(plist.get("EnvironmentVariables"), dict):
        raise ProvisionError("board_butler_launch_agent_invalid")
    host_cap = policy.get("host_cap")
    if not isinstance(host_cap, int) or isinstance(host_cap, bool) or not 1 <= host_cap <= 12:
        raise ProvisionError("host_cap_invalid")
    fingerprint = policy.get("authorization_fingerprint_sha256")
    if not isinstance(fingerprint, str) or executor.SHA256.fullmatch(fingerprint) is None:
        raise ProvisionError("authorization_fingerprint_invalid")
    templates = policy.get("templates")
    if not isinstance(templates, dict) or not templates:
        raise ProvisionError("templates_invalid")
    for template_id, record in templates.items():
        if not isinstance(record, dict):
            raise ProvisionError("templates_invalid")
        executor.SeatTemplate.from_record(template_id, record)
    for name in ("credential_paths", "board_caps"):
        if not isinstance(policy.get(name), dict) or not policy[name]:
            raise ProvisionError(f"{name}_invalid")
    for reference, raw_path in policy["credential_paths"].items():
        executor._require_id(reference, "credential_ref")
        credential = _absolute(raw_path, "credential_path")
        try:
            info = credential.stat()
        except OSError as exc:
            raise ProvisionError("credential_path_unavailable") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise ProvisionError("credential_path_untrusted")
    if any(
        not isinstance(board, str)
        or executor.SAFE_ID.fullmatch(board) is None
        or not isinstance(cap, int)
        or isinstance(cap, bool)
        or cap < 0
        for board, cap in policy["board_caps"].items()
    ):
        raise ProvisionError("board_caps_invalid")
    for name in ("repository_roots", "seat_roots"):
        if not isinstance(policy.get(name), list) or not policy[name]:
            raise ProvisionError(f"{name}_invalid")
        for raw_root in policy[name]:
            root = _absolute(raw_root, name[:-1])
            if not root.is_dir():
                raise ProvisionError(f"{name}_unavailable")
    if any(
        record.get("credential_ref") not in policy["credential_paths"]
        for record in templates.values()
    ):
        raise ProvisionError("template_credential_reference_unknown")
    if not socket_path.is_relative_to(state_dir):
        raise ProvisionError("executor_socket_outside_state")

    return {
        "schema": PROVISION_SCHEMA,
        "executor": {
            "python": str(python),
            "repository": str(repository),
            "config_path": str(config_path),
            "state_dir": str(state_dir),
            "socket_path": str(socket_path),
            "launch_agent_path": str(executor_plist),
        },
        "caller": {"key_id": key_id, "private_key_path": str(private_key)},
        "policy": dict(policy),
        "board_butler": {
            "launch_agent_path": str(butler_plist),
            "launch_agent_sha256": hashlib.sha256(butler_plist.read_bytes()).hexdigest(),
            "observation_file": str(observation),
            "state_file": str(state_file),
        },
    }


def create_plan(spec_path: Path, output: Path) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise ProvisionError("plan_output_exists")
    normalized = _validate_spec(_read_json(spec_path))
    digest = _digest(normalized)
    plan = {
        "schema": PLAN_SCHEMA,
        "digest": digest,
        "confirmation": f"APPLY-{digest}",
        "actions": [
            "generate_ed25519_caller_key",
            "write_owner_only_executor_config",
            "initialize_fail_closed_snapshots",
            "stage_executor_launch_agent",
            "stage_board_butler_fleet_options",
        ],
        "launchctl_actions": [],
        "spec": normalized,
    }
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.write_bytes(executor.canonical_json(plan) + b"\n")
    output.chmod(0o600)
    return plan


def _atomic_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        raise ProvisionError(f"target_exists:{path.name}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def confirm_plan(plan_path: Path, confirmation: str) -> dict[str, Any]:
    plan = _read_json(plan_path)
    if set(plan) != {"schema", "digest", "confirmation", "actions", "launchctl_actions", "spec"}:
        raise ProvisionError("plan_fields_invalid")
    if plan.get("schema") != PLAN_SCHEMA or plan.get("launchctl_actions") != []:
        raise ProvisionError("plan_invalid")
    raw_spec = plan.get("spec")
    if not isinstance(raw_spec, dict):
        raise ProvisionError("plan_invalid")
    source_spec = json.loads(json.dumps(raw_spec))
    source_butler = source_spec.get("board_butler")
    if not isinstance(source_butler, dict):
        raise ProvisionError("plan_invalid")
    source_butler.pop("launch_agent_sha256", None)
    normalized = _validate_spec(source_spec)
    if normalized != raw_spec:
        raise ProvisionError("plan_inputs_changed")
    digest = _digest(normalized)
    if plan.get("digest") != digest or plan.get("confirmation") != f"APPLY-{digest}":
        raise ProvisionError("plan_digest_mismatch")
    if confirmation != f"APPLY-{digest}":
        raise ProvisionError("confirmation_mismatch")

    executor_value = normalized["executor"]
    caller = normalized["caller"]
    policy = normalized["policy"]
    butler = normalized["board_butler"]
    config_path = Path(executor_value["config_path"])
    state_dir = Path(executor_value["state_dir"])
    socket_path = Path(executor_value["socket_path"])
    executor_plist_path = Path(executor_value["launch_agent_path"])
    private_key_path = Path(caller["private_key_path"])
    butler_plist_path = Path(butler["launch_agent_path"])
    backup = butler_plist_path.with_suffix(butler_plist_path.suffix + ".before-fleet")
    temporary = butler_plist_path.with_suffix(butler_plist_path.suffix + ".staged")
    if hashlib.sha256(butler_plist_path.read_bytes()).hexdigest() != butler["launch_agent_sha256"]:
        raise ProvisionError("board_butler_launch_agent_changed")
    lease_path = state_dir / "leases.json"
    readiness_path = state_dir / "registry-readiness.json"
    for target in (
        config_path,
        executor_plist_path,
        private_key_path,
        lease_path,
        readiness_path,
    ):
        if target.exists() or target.is_symlink():
            raise ProvisionError(f"target_exists:{target.name}")
    if backup.exists() or backup.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise ProvisionError("board_butler_staging_exists")

    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    raw_public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    config = {
        "schema": "pursers_fleet_executor_config_v1",
        "authorization_fingerprint_sha256": policy["authorization_fingerprint_sha256"],
        "caller_keys": {caller["key_id"]: base64.b64encode(raw_public).decode("ascii")},
        "templates": policy["templates"],
        "credential_paths": policy["credential_paths"],
        "repository_roots": policy["repository_roots"],
        "seat_roots": policy["seat_roots"],
        "board_caps": policy["board_caps"],
        "host_cap": policy["host_cap"],
    }
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_private(private_key_path, raw_private)
    _atomic_private(config_path, executor.canonical_json(config) + b"\n")
    _atomic_private(lease_path, b'{"boards":{}}\n')
    _atomic_private(
        readiness_path,
        b'{"schema":"pursers_registry_readiness_v1","stale_after":"1970-01-01T00:00:00+00:00","selected_active_boards":[],"boards":{}}\n',
    )
    executor_plist = {
        "Label": "com.pursers.fleet-executor",
        "ProgramArguments": [
            executor_value["python"],
            str(Path(executor_value["repository"]) / "tools/seat-kit/fleet_executor.py"),
            "--config", str(config_path),
            "--state-dir", str(state_dir),
            "--socket", str(socket_path),
            "--service-manager", "launchd",
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(state_dir / "executor.log"),
        "StandardErrorPath": str(state_dir / "executor.log"),
    }
    _atomic_private(
        executor_plist_path,
        plistlib.dumps(executor_plist, fmt=plistlib.FMT_XML, sort_keys=True),
    )

    original = butler_plist_path.read_bytes()
    _atomic_private(backup, original)
    document = plistlib.loads(original)
    environment = document["EnvironmentVariables"]
    environment.update(
        {
            "PURSERS_BUTLER_FLEET_OBSERVATION_FILE": butler["observation_file"],
            "PURSERS_BUTLER_FLEET_STATE_FILE": butler["state_file"],
            "PURSERS_BUTLER_FLEET_EXECUTOR_SOCKET": str(socket_path),
            "PURSERS_BUTLER_FLEET_EXECUTOR_KEY_ID": caller["key_id"],
            "PURSERS_BUTLER_FLEET_EXECUTOR_PRIVATE_KEY": str(private_key_path),
        }
    )
    _atomic_private(
        temporary, plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)
    )
    temporary.replace(butler_plist_path)
    butler_plist_path.chmod(0o600)
    return {
        "ok": True,
        "digest": digest,
        "launchctl_actions": [],
        "staged": [
            str(config_path), str(private_key_path), str(executor_plist_path),
            str(butler_plist_path), str(lease_path), str(readiness_path),
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--spec", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    confirm = subparsers.add_parser("confirm")
    confirm.add_argument("--plan", type=Path, required=True)
    confirm.add_argument("--confirm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = (
        create_plan(args.spec, args.output)
        if args.command == "plan"
        else confirm_plan(args.plan, args.confirm)
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
