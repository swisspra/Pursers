from __future__ import annotations

import base64
import json
import os
import plistlib
import stat
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason=(
        "host integration performs its compare-and-swap with the macOS-only "
        "renameatx_np syscall; the feature (and these tests) are Darwin-only "
        "by design — integration.py raises IntegrationError elsewhere"
    ),
)

import pursers_personal.integration as integration_module
from pursers_personal.integration import (
    ENTRY_NAME,
    IntegrationError,
    apply_integration,
    launchctl_commands,
    prepare_integration,
    rollback_integration,
    service_label,
    uninstall_integration,
    integration_status,
)


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def fixture_profile(tmp_path: Path):
    root = private_dir(tmp_path / "profile")
    profile_path = root / "profile.json"
    profile_path.write_text("{}\n", encoding="utf-8")
    profile_path.chmod(0o600)
    data = private_dir(root / "central-data")
    (data / "retained.txt").write_text("retain me\n", encoding="utf-8")
    return SimpleNamespace(
        profile_id="0123456789abcdef0123456789abcdef",
        profile_path=profile_path,
        central_data_dir=data,
    )


def fixture_targets(tmp_path: Path) -> tuple[Path, Path]:
    launch_agents = private_dir(tmp_path / "LaunchAgents")
    claude = private_dir(tmp_path / "Claude")
    config = claude / "claude_desktop_config.json"
    config.write_text(
        json.dumps(
            {
                "keep": {"theme": "dark"},
                "mcpServers": {
                    "on-board-a2a": {
                        "command": "/approved/v4/console",
                        "args": [],
                    }
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    return launch_agents, config


def fixture_console(tmp_path: Path) -> Path:
    binary = private_dir(tmp_path / "bin") / "pursers-personal"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    return binary


def make_plan(tmp_path: Path):
    profile = fixture_profile(tmp_path)
    launch_agents, config = fixture_targets(tmp_path)
    plan = prepare_integration(
        profile,
        console_path=fixture_console(tmp_path),
        launch_agents_dir=launch_agents,
        host_config_path=config,
        host_id="claude-desktop",
        session="primary-agent",
    )
    return profile, config, plan


def test_plan_is_additive_token_free_and_service_label_is_isolated(tmp_path: Path) -> None:
    _profile, _config, plan = make_plan(tmp_path)
    assert plan.label == "com.onboard.personal.0123456789abcdef0123456789abcdef"
    assert not plan.label.startswith("com.onboard.central")
    assert service_label("0123456789abcdef0123456789abcdef") == plan.label
    service = plistlib.loads(plan.service_payload)
    assert service["Label"] == plan.label
    assert service["ProgramArguments"][:2] == [str(plan.console_path), "central"]
    host = json.loads(plan.host_payload)
    assert host["mcpServers"]["on-board-a2a"]["command"] == "/approved/v4/console"
    personal = host["mcpServers"][ENTRY_NAME]
    assert personal["command"] == str(plan.console_path)
    assert personal["args"][0] == "mcp"
    rendered = plan.service_payload + plan.host_payload
    assert b"Bearer " not in rendered
    assert b"Authorization" not in rendered
    assert b"ONBOARD_CENTRAL_TOKEN" not in rendered
    assert b"eyJ" not in rendered


def test_apply_is_hash_bound_and_rollback_restores_exact_bytes(tmp_path: Path) -> None:
    profile, config, plan = make_plan(tmp_path)
    before = config.read_bytes()
    result = apply_integration(plan)
    assert result["status"] == "applied"
    assert apply_integration(plan)["status"] == "existing"
    assert plan.service_target.read_bytes() == plan.service_payload
    assert config.read_bytes() == plan.host_payload
    assert stat.S_IMODE(plan.service_target.stat().st_mode) == 0o600
    receipt = json.loads(plan.receipt_path.read_text(encoding="utf-8"))
    assert receipt["state"] == "applied"
    assert len(receipt["targets"]) == 2

    rolled = rollback_integration(profile.profile_path)
    assert rolled == {
        "status": "complete",
        "state": "rolled_back",
        "profile_retained": True,
    }
    assert not plan.service_target.exists()
    assert config.read_bytes() == before
    assert profile.profile_path.exists()
    assert (profile.central_data_dir / "retained.txt").exists()
    assert rollback_integration(profile.profile_path)["status"] == "existing"


def test_rollback_refuses_drift_without_changing_either_target(tmp_path: Path) -> None:
    profile, config, plan = make_plan(tmp_path)
    apply_integration(plan)
    drift = plan.host_payload + b"\n"
    config.write_bytes(drift)
    config.chmod(0o600)
    service_before = plan.service_target.read_bytes()
    with pytest.raises(IntegrationError, match="drifted"):
        rollback_integration(profile.profile_path)
    assert config.read_bytes() == drift
    assert plan.service_target.read_bytes() == service_before


def test_uninstall_is_idempotent_and_retains_profile_data(tmp_path: Path) -> None:
    profile, config, plan = make_plan(tmp_path)
    before = config.read_bytes()
    apply_integration(plan)
    first = uninstall_integration(profile.profile_path)
    second = uninstall_integration(profile.profile_path)
    assert first["state"] == "uninstalled"
    assert second == {
        "status": "existing",
        "state": "uninstalled",
        "profile_retained": True,
    }
    assert config.read_bytes() == before
    assert not plan.service_target.exists()
    assert profile.profile_path.exists()
    assert profile.central_data_dir.exists()

    repeated_plan = prepare_integration(
        profile,
        console_path=plan.console_path,
        launch_agents_dir=plan.service_target.parent,
        host_config_path=config,
        host_id="claude-desktop",
        session="primary-agent",
    )
    assert apply_integration(repeated_plan)["status"] == "applied"


@pytest.mark.parametrize("terminal_state", ["rolled_back", "uninstalled"])
def test_terminal_receipt_scrubs_private_host_backup(
    tmp_path: Path, terminal_state: str
) -> None:
    profile, config, plan = make_plan(tmp_path)
    sentinel = "host-existing-secret-sentinel"
    original = json.loads(config.read_text(encoding="utf-8"))
    original["unrelatedService"] = {"apiKey": sentinel}
    config.write_text(
        json.dumps(original, sort_keys=True) + "\n", encoding="utf-8"
    )
    config.chmod(0o600)
    original_bytes = config.read_bytes()
    plan = prepare_integration(
        profile,
        console_path=plan.console_path,
        launch_agents_dir=plan.service_target.parent,
        host_config_path=config,
        host_id="claude-desktop",
        session="primary-agent",
    )

    apply_integration(plan)
    active = json.loads(plan.receipt_path.read_text(encoding="utf-8"))
    host_before = next(
        item["before"] for item in active["targets"] if item["kind"] == "host-config"
    )
    assert sentinel.encode() in base64.b64decode(host_before["content_b64"])
    assert stat.S_IMODE(plan.receipt_path.stat().st_mode) == 0o600

    result = rollback_integration(profile.profile_path, terminal_state=terminal_state)
    terminal = json.loads(plan.receipt_path.read_text(encoding="utf-8"))
    assert result["state"] == terminal_state
    assert config.read_bytes() == original_bytes
    assert all(item["before"]["content_b64"] is None for item in terminal["targets"])
    assert sentinel not in plan.receipt_path.read_text(encoding="utf-8")
    assert sentinel not in json.dumps(result, sort_keys=True)


def test_preparing_receipt_recovers_a_partial_apply(tmp_path: Path) -> None:
    profile, config, plan = make_plan(tmp_path)
    before = config.read_bytes()
    apply_integration(plan)
    receipt = json.loads(plan.receipt_path.read_text(encoding="utf-8"))
    receipt["state"] = "preparing"
    plan.receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    plan.receipt_path.chmod(0o600)
    config.write_bytes(before)
    config.chmod(0o600)

    recovered = rollback_integration(profile.profile_path)
    assert recovered["state"] == "rolled_back"
    assert not plan.service_target.exists()
    assert config.read_bytes() == before


def test_unowned_targets_and_symlinks_fail_closed(tmp_path: Path) -> None:
    profile = fixture_profile(tmp_path)
    launch_agents, config = fixture_targets(tmp_path)
    label = service_label(profile.profile_id)
    target = launch_agents / f"{label}.plist"
    replacement = launch_agents / "replacement.plist"
    replacement.write_text("not owned\n", encoding="utf-8")
    replacement.chmod(0o600)
    target.symlink_to(replacement)
    with pytest.raises(IntegrationError):
        prepare_integration(
            profile,
            console_path=fixture_console(tmp_path),
            launch_agents_dir=launch_agents,
            host_config_path=config,
            host_id="claude-desktop",
            session="primary-agent",
        )

    target.unlink()
    document = json.loads(config.read_text(encoding="utf-8"))
    document["mcpServers"][ENTRY_NAME] = {"command": "/unowned"}
    config.write_text(json.dumps(document), encoding="utf-8")
    config.chmod(0o600)
    with pytest.raises(IntegrationError, match="unowned"):
        prepare_integration(
            profile,
            console_path=fixture_console(tmp_path),
            launch_agents_dir=launch_agents,
            host_config_path=config,
            host_id="claude-desktop",
            session="primary-agent",
        )


def test_launchctl_commands_target_only_owned_label(tmp_path: Path) -> None:
    label = service_label("0123456789abcdef0123456789abcdef")
    plist = tmp_path / f"{label}.plist"
    commands = launchctl_commands(label, plist)
    domain = f"gui/{os.getuid()}"
    assert commands["start"] == [
        integration_module.LAUNCHCTL_PATH,
        "bootstrap",
        domain,
        str(plist),
    ]
    assert commands["restart"] == [
        integration_module.LAUNCHCTL_PATH,
        "kickstart",
        "-k",
        f"{domain}/{label}",
    ]
    assert commands["stop"] == [
        integration_module.LAUNCHCTL_PATH,
        "bootout",
        f"{domain}/{label}",
    ]
    with pytest.raises(IntegrationError):
        launchctl_commands("com.onboard.central", plist)


def test_applied_receipt_rejects_identity_console_and_mode_drift(tmp_path: Path) -> None:
    _profile, config, plan = make_plan(tmp_path)
    apply_integration(plan)

    with pytest.raises(IntegrationError, match="requested profile, console, host, or session"):
        apply_integration(replace(plan, session="different-agent"))

    config.chmod(0o644)
    with pytest.raises(IntegrationError, match="drifted"):
        integration_status(plan.profile_path)
    config.chmod(0o600)

    plan.console_path.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    plan.console_path.chmod(0o700)
    with pytest.raises(IntegrationError, match="console drifted"):
        integration_status(plan.profile_path)


def test_concurrent_host_edit_is_preserved_and_service_is_compensated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _profile, _config, plan = make_plan(tmp_path)
    actual_rename = integration_module._renameatx
    concurrent = b'{"concurrent": true}\n'
    injected = False

    def racing_rename(
        from_fd: int, from_name: str, to_fd: int, to_name: str, flags: int
    ) -> None:
        nonlocal injected
        if to_name == plan.host_target.name and flags & integration_module._RENAME_SWAP and not injected:
            injected = True
            plan.host_target.write_bytes(concurrent)
            plan.host_target.chmod(0o600)
        actual_rename(from_fd, from_name, to_fd, to_name, flags)

    monkeypatch.setattr(integration_module, "_renameatx", racing_rename)
    with pytest.raises(IntegrationError, match="changed while"):
        apply_integration(plan)
    assert injected
    assert plan.host_target.read_bytes() == concurrent
    assert not plan.service_target.exists()
    receipt = json.loads(plan.receipt_path.read_text(encoding="utf-8"))
    assert receipt["state"] == "rolled_back"


def test_two_concurrent_host_versions_are_retained_as_target_and_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _profile, _config, plan = make_plan(tmp_path)
    actual_rename = integration_module._renameatx
    first = b'{"concurrent": "first"}\n'
    second = b'{"concurrent": "second"}\n'
    swaps = 0

    def racing_rename(
        from_fd: int, from_name: str, to_fd: int, to_name: str, flags: int
    ) -> None:
        nonlocal swaps
        if to_name == plan.host_target.name and flags & integration_module._RENAME_SWAP:
            swaps += 1
            plan.host_target.write_bytes(first if swaps == 1 else second)
            plan.host_target.chmod(0o600)
        actual_rename(from_fd, from_name, to_fd, to_name, flags)

    monkeypatch.setattr(integration_module, "_renameatx", racing_rename)
    with pytest.raises(IntegrationError, match="recovery file"):
        apply_integration(plan)
    recovery = list(plan.host_target.parent.glob(f".{plan.host_target.name}.*.tmp"))
    assert swaps == 2
    assert plan.host_target.read_bytes() == first
    assert len(recovery) == 1
    assert recovery[0].read_bytes() == second
    assert not plan.service_target.exists()


def test_rollback_race_preserves_external_versions_and_applied_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile, _config, plan = make_plan(tmp_path)
    apply_integration(plan)
    actual_rename = integration_module._renameatx
    first = b'{"rollback": "first"}\n'
    second = b'{"rollback": "second"}\n'
    swaps = 0

    def racing_rename(
        from_fd: int, from_name: str, to_fd: int, to_name: str, flags: int
    ) -> None:
        nonlocal swaps
        if to_name == plan.host_target.name and flags & integration_module._RENAME_SWAP:
            swaps += 1
            plan.host_target.write_bytes(first if swaps == 1 else second)
            plan.host_target.chmod(0o600)
        actual_rename(from_fd, from_name, to_fd, to_name, flags)

    monkeypatch.setattr(integration_module, "_renameatx", racing_rename)
    with pytest.raises(IntegrationError, match="recovery file"):
        rollback_integration(profile.profile_path)
    recovery = list(plan.host_target.parent.glob(f".{plan.host_target.name}.*.tmp"))
    assert swaps == 2
    assert plan.host_target.read_bytes() == first
    assert len(recovery) == 1
    assert recovery[0].read_bytes() == second
    assert plan.service_target.read_bytes() == plan.service_payload
    assert (
        json.loads(plan.receipt_path.read_text(encoding="utf-8"))["state"]
        == "rolling_back"
    )


def test_interrupted_rollback_resumes_from_durable_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile, config, plan = make_plan(tmp_path)
    original_host = config.read_bytes()
    apply_integration(plan)
    actual_restore = integration_module._restore
    calls = 0

    def interrupt_after_first(path: Path, before, *, expected_before) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic interruption")
        actual_restore(path, before, expected_before=expected_before)

    monkeypatch.setattr(integration_module, "_restore", interrupt_after_first)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        rollback_integration(profile.profile_path)
    receipt = json.loads(plan.receipt_path.read_text(encoding="utf-8"))
    assert receipt["state"] == "rolling_back"
    assert receipt["rollback_terminal_state"] == "rolled_back"
    assert config.read_bytes() == original_host
    assert plan.service_target.read_bytes() == plan.service_payload

    monkeypatch.setattr(integration_module, "_restore", actual_restore)
    assert rollback_integration(profile.profile_path)["state"] == "rolled_back"
    assert config.read_bytes() == original_host
    assert not plan.service_target.exists()


@pytest.mark.parametrize("credential_state", ["missing", "corrupt"])
def test_removal_uses_owned_receipt_when_profile_credentials_are_unusable(
    tmp_path: Path, credential_state: str
) -> None:
    profile, config, plan = make_plan(tmp_path)
    before = config.read_bytes()
    apply_integration(plan)
    if credential_state == "missing":
        profile.profile_path.unlink()
    else:
        profile.profile_path.write_text("not-json\n", encoding="utf-8")
        profile.profile_path.chmod(0o600)
    result = uninstall_integration(profile.profile_path)
    assert result["state"] == "uninstalled"
    assert config.read_bytes() == before
    assert not plan.service_target.exists()


def test_integration_lock_rejects_overlapping_lifecycle_operation(
    tmp_path: Path,
) -> None:
    profile = fixture_profile(tmp_path)
    with integration_module._integration_lock(profile.profile_path):
        with pytest.raises(
            IntegrationError, match="another Personal integration operation is active"
        ):
            with integration_module._integration_lock(profile.profile_path):
                raise AssertionError("overlapping lifecycle lock was acquired")
