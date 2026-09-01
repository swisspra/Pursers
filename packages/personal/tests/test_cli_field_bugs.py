from __future__ import annotations

import argparse
import json
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest
from pursers_personal import cli
from pursers_personal.integration import IntegrationError, prepare_integration


class PersonalProfileError(RuntimeError):
    pass


class ProfileSecurityError(PersonalProfileError):
    pass


def _setup_args(
    tmp_path: Path,
    *,
    apply: bool,
    host_id: str = "claude-desktop",
    host_config: Path | None = None,
) -> argparse.Namespace:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return argparse.Namespace(
        project=project,
        profiles_root=tmp_path / "profiles",
        port=None,
        host_id=host_id,
        session="primary",
        console=None,
        launch_agents_dir=tmp_path / "LaunchAgents",
        host_config=host_config or tmp_path / "host" / "settings.json",
        apply=apply,
        activate=False,
    )


def _api_for(root: Path, profile_path: Path):
    class API:
        PersonalProfileError = PersonalProfileError
        ProfileSecurityError = ProfileSecurityError

        @staticmethod
        def default_profiles_root() -> Path:
            return root

        @staticmethod
        def profile_path_for_project(_project: Path, _root: Path) -> Path:
            return profile_path

    return API


class ReachedProfileCreation(RuntimeError):
    pass


@pytest.mark.parametrize("desktop_target", [False, True])
@pytest.mark.parametrize("desktop_open", [False, True])
@pytest.mark.parametrize("host_id", ["claude-desktop", "claude-code"])
def test_setup_lifecycle_gate_matrix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    desktop_target: bool,
    desktop_open: bool,
    host_id: str,
) -> None:
    default_config = tmp_path / "desktop" / "claude_desktop_config.json"
    custom_config = tmp_path / "terminal" / "settings.json"
    args = _setup_args(
        tmp_path,
        apply=True,
        host_id=host_id,
        host_config=default_config if desktop_target else custom_config,
    )
    profile_path = args.profiles_root / ("project-" + "1" * 24) / "profile.json"
    api = _api_for(args.profiles_root, profile_path)
    probes: list[list[str]] = []

    def run(command, **_kwargs):
        probes.append(command)
        return SimpleNamespace(returncode=0 if desktop_open else 1)

    monkeypatch.setattr(cli, "safe_component_summary", dict)
    monkeypatch.setattr(cli, "_default_claude_config", lambda: default_config)
    monkeypatch.setattr(cli, "_profile_api", lambda: api)
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_system_binary", lambda _path, _label: None)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(
        cli,
        "_setup_port",
        lambda _api, _args: (_ for _ in ()).throw(ReachedProfileCreation()),
    )

    must_stop = desktop_target and host_id == "claude-desktop" and desktop_open
    if must_stop:
        with pytest.raises(IntegrationError, match="quit Claude Desktop"):
            cli.command_setup(args)
    else:
        with pytest.raises(ReachedProfileCreation):
            cli.command_setup(args)
    assert bool(probes) is (desktop_target and host_id == "claude-desktop")


def test_unknown_host_lifecycle_probe_returns_false_with_note(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli._host_is_running("claude-code") is False
    assert "lifecycle check skipped" in capsys.readouterr().err


def test_custom_host_id_is_rendered_into_custom_host_config(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile" / "profile.json"
    profile_path.parent.mkdir()
    profile_path.write_text("{}\n", encoding="utf-8")
    console = tmp_path / "pursers-personal"
    console.write_text("#!/bin/sh\n", encoding="utf-8")
    console.chmod(0o700)
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    host_config = tmp_path / "terminal" / "settings.json"
    host_config.parent.mkdir()

    plan = prepare_integration(
        SimpleNamespace(profile_id="1" * 32, profile_path=profile_path),
        console_path=console,
        launch_agents_dir=launch_agents,
        host_config_path=host_config,
        host_id="claude-code",
        session="primary",
    )

    entry = json.loads(plan.host_payload)["mcpServers"]["pursers-personal"]
    assert entry["args"][-4:] == [
        "--host-id",
        "claude-code",
        "--session",
        "primary",
    ]


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | None]]:
    if not root.exists():
        return {}
    result: dict[str, tuple[str, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = ("symlink", str(path.readlink()).encode())
        elif path.is_dir():
            result[relative] = ("directory", None)
        else:
            result[relative] = ("file", path.read_bytes())
    return result


def test_setup_plan_mode_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = _setup_args(tmp_path, apply=False)
    profile_path = args.profiles_root / ("project-" + "2" * 24) / "profile.json"
    api = _api_for(args.profiles_root, profile_path)
    console = tmp_path / "runtime" / "pursers-personal"
    console.parent.mkdir()
    console.write_text("#!/bin/sh\n", encoding="utf-8")
    before = _tree_snapshot(tmp_path)
    monkeypatch.setattr(cli, "safe_component_summary", dict)
    monkeypatch.setattr(cli, "_profile_api", lambda: api)
    monkeypatch.setattr(cli, "_console_path", lambda _value: console)

    result = cli.command_setup(args)

    assert _tree_snapshot(tmp_path) == before
    assert result["status"] == "planned"
    assert result["profile"]["profile_path"] == str(profile_path)
    assert result["integration"]["port_strategy"] == "ephemeral-on-apply"
    assert result["integration"]["host_entry_action"] == "create-config-and-add-entry"


def test_apply_failure_removes_only_profile_created_by_this_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = _setup_args(tmp_path, apply=True, host_id="claude-code")
    profile_path = args.profiles_root / ("project-" + "3" * 24) / "profile.json"
    api = _api_for(args.profiles_root, profile_path)

    def ensure(_project: Path, **_kwargs):
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text("{}\n", encoding="utf-8")
        return SimpleNamespace(profile_path=profile_path)

    def identity(**_kwargs):
        raise PersonalProfileError("synthetic setup failure")

    api.ensure_personal_profile = ensure
    api.doctor_identity_summary = identity
    monkeypatch.setattr(cli, "safe_component_summary", dict)
    monkeypatch.setattr(cli, "_profile_api", lambda: api)
    monkeypatch.setattr(cli, "_setup_port", lambda _api, _args: 54321)

    with pytest.raises(IntegrationError, match="missing or invalid"):
        cli.command_setup(args)
    assert not profile_path.parent.exists()


def test_apply_failure_preserves_preexisting_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = _setup_args(tmp_path, apply=True, host_id="claude-code")
    profile_path = args.profiles_root / ("project-" + "7" * 24) / "profile.json"
    profile_path.parent.mkdir(parents=True)
    original = b'{"existing": true}\n'
    profile_path.write_bytes(original)
    api = _api_for(args.profiles_root, profile_path)
    api.ensure_personal_profile = lambda _project, **_kwargs: SimpleNamespace(
        profile_path=profile_path
    )
    api.doctor_identity_summary = lambda **_kwargs: (_ for _ in ()).throw(
        PersonalProfileError("synthetic setup failure")
    )
    monkeypatch.setattr(cli, "safe_component_summary", dict)
    monkeypatch.setattr(cli, "_profile_api", lambda: api)
    monkeypatch.setattr(cli, "_setup_port", lambda _api, _args: 54321)

    with pytest.raises(IntegrationError, match="missing or invalid"):
        cli.command_setup(args)
    assert profile_path.read_bytes() == original


def test_profiles_list_and_prune_protect_referenced_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "profiles"
    root.mkdir()
    names = ["project-" + character * 24 for character in "456"]
    profile_paths = [root / name / "profile.json" for name in names]
    for profile_path in profile_paths:
        profile_path.parent.mkdir()
        profile_path.write_text("{}\n", encoding="utf-8")
    profiles = {
        str(path): SimpleNamespace(
            profile_path=path,
            project_root=tmp_path / f"workspace-{index}",
            profile_id=str(index + 1) * 32,
            board_id=f"board-{index}",
        )
        for index, path in enumerate(profile_paths)
    }

    class API:
        PersonalProfileError = PersonalProfileError
        ProfileSecurityError = ProfileSecurityError

        @staticmethod
        def default_profiles_root() -> Path:
            return root

        @staticmethod
        def load_personal_profile(path: Path):
            return profiles[str(path)]

    host_config = tmp_path / "terminal-settings.json"
    host_config.write_text(
        json.dumps({"mcpServers": {"kept": {"args": [str(profile_paths[1])]}}}),
        encoding="utf-8",
    )
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    launch_agent = (
        launch_agents
        / f"com.onboard.personal.{profiles[str(profile_paths[2])].profile_id}.plist"
    )
    launch_agent.write_text("plist placeholder\n", encoding="utf-8")
    args = argparse.Namespace(
        profiles_root=root,
        host_config=[host_config],
        launch_agents_dir=[launch_agents],
        commit=False,
        dry_run=True,
        orphaned=True,
    )
    monkeypatch.setattr(cli, "_profile_api", lambda: API)
    monkeypatch.setattr(
        cli,
        "integration_status",
        lambda _path: {"state": "not-installed", "targets": []},
    )
    monkeypatch.setattr(
        cli, "_default_claude_config", lambda: tmp_path / "absent-desktop.json"
    )
    monkeypatch.setattr(
        cli, "_default_claude_code_config", lambda: tmp_path / "absent-code.json"
    )
    monkeypatch.setattr(
        cli, "_default_launch_agents", lambda: tmp_path / "absent-agents"
    )

    listed = cli.command_profiles_list(args)
    by_path = {item["profile_path"]: item for item in listed["profiles"]}
    assert by_path[str(profile_paths[0])]["orphaned"] is True
    assert by_path[str(profile_paths[1])]["host_references"] == [str(host_config)]
    assert by_path[str(profile_paths[2])]["launch_agent_references"] == [
        str(launch_agent)
    ]

    planned = cli.command_profiles_prune(args)
    assert planned["candidates"] == [str(profile_paths[0])]
    assert all(path.exists() for path in profile_paths)

    args.commit = True
    args.dry_run = False
    committed = cli.command_profiles_prune(args)
    assert committed["removed"] == [str(profile_paths[0])]
    assert not profile_paths[0].parent.exists()
    assert profile_paths[1].exists()
    assert profile_paths[2].exists()


def test_cli_version_matches_package_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == version("pursers-personal")
