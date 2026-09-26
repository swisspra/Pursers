from __future__ import annotations

import importlib.util
import json
import plistlib
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "tools/seat-kit/fleet_executor_provision.py"
SPEC = importlib.util.spec_from_file_location("fleet_executor_provision", MODULE_PATH)
assert SPEC and SPEC.loader
provision = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = provision
SPEC.loader.exec_module(provision)


def provision_spec(tmp_path: Path) -> dict[str, object]:
    repository_root = tmp_path / "repositories"
    seat_root = tmp_path / "seats"
    repository = repository_root / "project"
    seat = seat_root / "reviewer-a"
    repository.mkdir(parents=True)
    seat.mkdir(parents=True)
    credential = tmp_path / "reviewer-a.env"
    credential.write_text("TOKEN=synthetic\n", encoding="utf-8")
    credential.chmod(0o600)
    butler_plist = tmp_path / "LaunchAgents/com.pursers.board-butler.plist"
    butler_plist.parent.mkdir()
    butler_plist.write_bytes(
        (ROOT / "tools/board-butler/com.pursers.board-butler.plist.template").read_bytes()
    )
    state = tmp_path / "fleet-executor"
    return {
        "schema": provision.PROVISION_SCHEMA,
        "executor": {
            "python": sys.executable,
            "repository": str(ROOT),
            "config_path": str(state / "executor.json"),
            "state_dir": str(state),
            "socket_path": str(state / "executor.sock"),
            "launch_agent_path": str(
                tmp_path / "LaunchAgents/com.pursers.fleet-executor.plist"
            ),
        },
        "caller": {
            "key_id": "board-butler-local",
            "private_key_path": str(state / "board-butler-local.key"),
        },
        "policy": {
            "authorization_fingerprint_sha256": "a" * 64,
            "templates": {
                "reviewer-standard": {
                    "role": "reviewer",
                    "principal_id": "PR-reviewer-a",
                    "credential_ref": "credential.reviewer-a",
                    "repository_root": str(repository),
                    "seat_root": str(seat),
                    "command": [sys.executable, "-c", "raise SystemExit(0)"],
                    "boards": "registry",
                    "capabilities": {
                        "can_work": False,
                        "can_review": True,
                        "tier_max": 2,
                        "max_parallel": 1,
                    },
                }
            },
            "credential_paths": {"credential.reviewer-a": str(credential)},
            "repository_roots": [str(repository_root)],
            "seat_roots": [str(seat_root)],
            "board_caps": {"pursers": 1},
            "host_cap": 12,
        },
        "board_butler": {
            "launch_agent_path": str(butler_plist),
            "observation_file": str(tmp_path / "observation.json"),
            "state_file": str(tmp_path / "fleet-state.json"),
        },
    }


def test_plan_confirm_stages_owner_only_runtime_without_launchctl(tmp_path: Path) -> None:
    specification = provision_spec(tmp_path)
    spec_path = tmp_path / "spec.json"
    plan_path = tmp_path / "plan.json"
    spec_path.write_text(json.dumps(specification), encoding="utf-8")

    plan = provision.create_plan(spec_path, plan_path)

    assert plan["launchctl_actions"] == []
    assert plan["confirmation"] == f"APPLY-{plan['digest']}"
    assert "private_key_base64" not in json.dumps(plan)
    with pytest.raises(provision.ProvisionError, match="confirmation_mismatch"):
        provision.confirm_plan(plan_path, "APPLY-wrong")

    result = provision.confirm_plan(plan_path, plan["confirmation"])
    state = Path(specification["executor"]["state_dir"])
    config_path = Path(specification["executor"]["config_path"])
    private_key = Path(specification["caller"]["private_key_path"])
    executor_plist = Path(specification["executor"]["launch_agent_path"])
    butler_plist = Path(specification["board_butler"]["launch_agent_path"])
    assert result["ok"] is True
    assert result["launchctl_actions"] == []
    for path in (
        config_path,
        private_key,
        executor_plist,
        state / "leases.json",
        state / "registry-readiness.json",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(private_key.read_bytes()) == 32
    assert provision.executor.load_policy(config_path).host_cap == 12
    executor_document = plistlib.loads(executor_plist.read_bytes())
    assert executor_document["ProgramArguments"][-2:] == [
        "--service-manager",
        "launchd",
    ]
    environment = plistlib.loads(butler_plist.read_bytes())["EnvironmentVariables"]
    assert environment["PURSERS_BUTLER_FLEET_EXECUTOR_SOCKET"] == str(
        state / "executor.sock"
    )
    assert environment["PURSERS_BUTLER_FLEET_EXECUTOR_KEY_ID"] == "board-butler-local"
    assert "synthetic" not in json.dumps(environment)
    assert (butler_plist.with_suffix(".plist.before-fleet")).exists()


def test_plan_rejects_host_ceiling_above_twelve(tmp_path: Path) -> None:
    specification = provision_spec(tmp_path)
    specification["policy"]["host_cap"] = 13
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(specification), encoding="utf-8")
    with pytest.raises(provision.ProvisionError, match="host_cap_invalid"):
        provision.create_plan(spec_path, tmp_path / "plan.json")
