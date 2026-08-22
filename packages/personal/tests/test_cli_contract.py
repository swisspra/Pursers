from __future__ import annotations

import argparse
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import onboard_personal.cli as cli
from onboard_personal.artifacts import ArtifactVerificationError
from onboard_personal.integration import IntegrationError


def test_console_is_bound_to_current_python_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    python = runtime / "python"
    python.write_text("runtime\n", encoding="utf-8")
    console = runtime / "onboard-personal"
    console.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    console.chmod(0o700)
    shadow = tmp_path / "shadow"
    shadow.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shadow.chmod(0o700)
    monkeypatch.setattr(cli.sys, "executable", str(python))

    assert cli._console_path(None) == console
    with pytest.raises(IntegrationError, match="beside this Python runtime"):
        cli._console_path(shadow)


def test_setup_artifact_preflight_precedes_profile_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_profile = False

    def profile_api():
        nonlocal reached_profile
        reached_profile = True
        raise AssertionError("profile must not be created")

    monkeypatch.setattr(cli, "_profile_api", profile_api)
    monkeypatch.setattr(
        cli,
        "safe_component_summary",
        lambda: (_ for _ in ()).throw(ArtifactVerificationError("drifted")),
    )
    with pytest.raises(ArtifactVerificationError, match="drifted"):
        cli.command_setup(argparse.Namespace())
    assert not reached_profile


@pytest.mark.parametrize(
    ("loaded", "expected"), [(False, "started"), (True, "restarted")]
)
def test_activation_bootstraps_unloaded_and_kickstarts_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loaded: bool,
    expected: str,
) -> None:
    calls: list[list[str]] = []
    states = iter([loaded, True])
    label = "com.onboard.personal.0123456789abcdef0123456789abcdef"
    plist = tmp_path / f"{label}.plist"
    monkeypatch.setattr(cli, "_service_is_loaded", lambda _label: next(states))
    monkeypatch.setattr(cli, "_run_launchctl", calls.append)
    assert cli._activate_service(label, plist) == expected
    if loaded:
        assert "kickstart" in calls[0]
    else:
        assert "bootstrap" in calls[0]


@pytest.mark.parametrize(
    ("loaded", "expected_calls", "expected_status"),
    [(False, 0, "already-stopped"), (True, 1, "stopped")],
)
def test_deactivation_is_loaded_state_aware(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loaded: bool,
    expected_calls: int,
    expected_status: str,
) -> None:
    calls: list[list[str]] = []
    label = "com.onboard.personal.0123456789abcdef0123456789abcdef"
    plist = tmp_path / f"{label}.plist"
    states = iter([loaded, False] if loaded else [False])
    monkeypatch.setattr(cli, "_service_is_loaded", lambda _label: next(states))
    monkeypatch.setattr(cli, "_run_launchctl", calls.append)
    assert cli._deactivate_service(label, plist) == expected_status
    assert len(calls) == expected_calls
    if calls:
        assert "bootout" in calls[0]


@pytest.mark.parametrize(
    ("returncode", "expected"), [(0, True), (113, False)]
)
def test_service_state_accepts_only_loaded_or_exact_absent(
    monkeypatch: pytest.MonkeyPatch, returncode: int, expected: bool
) -> None:
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_system_binary", lambda _path, _label: None)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=returncode),
    )
    assert cli._service_is_loaded(
        "com.onboard.personal.0123456789abcdef0123456789abcdef"
    ) is expected


def test_service_state_other_failure_is_not_treated_as_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_system_binary", lambda _path, _label: None)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=5),
    )
    with pytest.raises(IntegrationError, match="cannot verify"):
        cli._service_is_loaded(
            "com.onboard.personal.0123456789abcdef0123456789abcdef"
        )


def test_rotate_preflights_before_revoking_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rotated = False
    profile = SimpleNamespace(profile_path=tmp_path / "profile.json")

    class API:
        @staticmethod
        def load_personal_profile(_path: Path):
            return profile

        @staticmethod
        def rotate_personal_capability(_path: Path):
            nonlocal rotated
            rotated = True
            raise AssertionError("must not rotate")

    monkeypatch.setattr(cli, "_profile_api", lambda: API)
    monkeypatch.setattr(cli, "_require_host_closed", lambda _host: None)
    monkeypatch.setattr(
        cli, "integration_status", lambda _path: {"state": "not-installed"}
    )
    args = argparse.Namespace(profile=profile.profile_path, activate=True)
    with pytest.raises(IntegrationError, match="integration is not applied"):
        cli.command_rotate(args)
    assert not rotated


def test_rotate_without_activation_uses_expiry_recovery_path_directly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = SimpleNamespace(
        profile_path=tmp_path / "profile.json",
        principal_id="PR-stable",
        kid="rotated-kid",
    )

    class API:
        @staticmethod
        def load_personal_profile(_path: Path):
            raise AssertionError("strict profile loading must not precede rotation")

        @staticmethod
        def rotate_personal_capability(_path: Path):
            return profile

    monkeypatch.setattr(cli, "_profile_api", lambda: API)
    monkeypatch.setattr(cli, "_require_host_closed", lambda _host: None)
    result = cli.command_rotate(
        argparse.Namespace(profile=profile.profile_path, activate=False)
    )
    assert result["status"] == "rotated"
    assert result["central_restart_required"] is True
    assert result["host_restart_required"] is True


def test_rotation_activation_failure_is_nonzero_and_honest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = {
        "status": "rotated-central-restart-failed",
        "central_restart_required": True,
        "host_restart_required": True,
        "activation": "failed",
    }
    monkeypatch.setattr(
        cli,
        "command_rotate",
        lambda _args: (_ for _ in ()).throw(cli.RotationActivationError(result)),
    )
    with pytest.raises(SystemExit) as caught:
        cli.main(["--json", "rotate", "--profile", "/synthetic/profile.json"])
    captured = capsys.readouterr()
    assert caught.value.code == 2
    assert '"central_restart_required": true' in captured.out
    assert "capability rotated" in captured.err
    assert "Traceback" not in captured.err


def test_setup_refuses_running_host_before_profile_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_profile = False

    def profile_api():
        nonlocal reached_profile
        reached_profile = True
        raise AssertionError("profile must not be created")

    monkeypatch.setattr(cli, "safe_component_summary", lambda: {})
    monkeypatch.setattr(cli, "_host_is_running", lambda _host: True)
    monkeypatch.setattr(cli, "_profile_api", profile_api)
    args = argparse.Namespace(apply=True, host_id="claude-desktop")
    with pytest.raises(IntegrationError, match="quit Claude Desktop"):
        cli.command_setup(args)
    assert not reached_profile


def test_setup_port_is_random_on_first_use_and_stable_after_profile(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    profiles = tmp_path / "profiles"
    profile_path = profiles / "derived" / "profile.json"

    class API:
        PersonalProfileError = RuntimeError
        ProfileSecurityError = RuntimeError

        @staticmethod
        def default_profiles_root() -> Path:
            return profiles

        @staticmethod
        def profile_path_for_project(_project: Path, _root: Path) -> Path:
            return profile_path

        @staticmethod
        def load_personal_profile(_path: Path):
            return SimpleNamespace(central_port=54321)

    args = argparse.Namespace(project=project, profiles_root=profiles, port=None)
    selected = cli._setup_port(API, args)
    assert 1024 <= selected <= 65535
    assert selected != 8766

    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("{}\n", encoding="utf-8")
    profile_path.chmod(0o600)
    assert cli._setup_port(API, args) == 54321


def test_explicit_setup_initialization_precedes_read_only_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SimpleNamespace(
        central_url="http://127.0.0.1:54321/mcp",
        capability_token="pp4-setup-secret",
        board_id="board-synthetic",
        authenticated_principal_id="PR-synthetic",
        agent_name="claude-desktop-primary",
    )
    order: list[str] = []

    class PersonalProfileError(RuntimeError):
        pass

    class ProfileSecurityError(PersonalProfileError):
        pass

    class API:
        PERSONAL_REVIEW_POLICY = "workflow"

        @staticmethod
        def resolve_personal_context(_profile, *, host: str, session: str):
            assert (host, session) == ("claude-desktop", "primary")
            return context

        @staticmethod
        async def bootstrap_personal_review_policy(_client):
            order.append("policy")
            return {"changed": True}

    API.PersonalProfileError = PersonalProfileError
    API.ProfileSecurityError = ProfileSecurityError

    class Client:
        identity = None

        def __init__(self, url, token, board, *, agent_name):
            assert (url, token, board, agent_name) == (
                context.central_url,
                context.capability_token,
                context.board_id,
                context.agent_name,
            )

        async def __aenter__(self):
            order.append("join")
            self.identity = SimpleNamespace(
                principal_id=context.authenticated_principal_id,
                agent_name=context.agent_name,
            )
            return self

        async def __aexit__(self, *_args):
            return None

        async def board_status(self):
            order.append("status")
            return {"board_id": context.board_id, "review_policy": "workflow"}

    monkeypatch.setattr(cli, "_profile_api", lambda: API)
    monkeypatch.setattr(
        cli,
        "import_verified_component",
        lambda *_args, **_kwargs: SimpleNamespace(BoardClient=Client),
    )
    result = cli._initialize_personal_board(
        object(), host_id="claude-desktop", session="primary"
    )
    assert order == ["join", "policy", "status"]
    assert result == {
        "status": "ready",
        "board_id": context.board_id,
        "principal_id": context.authenticated_principal_id,
        "agent_name": context.agent_name,
        "review_policy": "workflow",
        "policy_changed": True,
        "mutating": True,
    }
    assert "pp4-setup-secret" not in repr(result)


@pytest.mark.parametrize("returncode, expected", [(0, True), (1, False)])
def test_host_running_probe_is_exact_and_sanitized(
    monkeypatch: pytest.MonkeyPatch, returncode: int, expected: bool
) -> None:
    seen: list[list[str]] = []

    def run(command, **_kwargs):
        seen.append(command)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_system_binary", lambda _path, _label: None)
    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli._host_is_running("claude-desktop") is expected
    assert seen[0] == [
        cli.PGREP_PATH,
        "-x",
        "-u",
        str(cli.os.getuid()),
        "Claude",
    ]
    assert len(seen) == (1 if expected else 2)
    if not expected:
        assert seen[1] == [
            cli.PGREP_PATH,
            "-f",
            "-u",
            str(cli.os.getuid()),
            "/Applications/Claude.app/Contents/",
        ]


@pytest.mark.parametrize("security", [False, True])
def test_profile_failures_exit_without_traceback_or_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    security: bool,
) -> None:
    class PersonalProfileError(RuntimeError):
        pass

    class ProfileSecurityError(PersonalProfileError):
        pass

    class API:
        @staticmethod
        def select_personal_profile(**_kwargs):
            error = ProfileSecurityError if security else PersonalProfileError
            raise error("Bearer pp4-unique-secret-token")

    API.PersonalProfileError = PersonalProfileError
    API.ProfileSecurityError = ProfileSecurityError

    monkeypatch.setattr(cli, "_profile_api", lambda: API)
    with pytest.raises(SystemExit) as caught:
        cli.main(["doctor", "--profile", str(tmp_path / "missing.json")])
    stderr = capsys.readouterr().err
    assert caught.value.code == 2
    assert "Personal profile" in stderr
    assert "pp4-unique-secret-token" not in stderr
    assert "Bearer" not in stderr
    assert "Traceback" not in stderr


def test_launchctl_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli, "_system_binary", lambda _path, _label: None)

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["launchctl"], stderr=b"secret")

    monkeypatch.setattr(cli.subprocess, "run", fail)
    with pytest.raises(IntegrationError, match="owned Personal service") as caught:
        cli._run_launchctl([cli.LAUNCHCTL_PATH, "print", "safe-label"])
    assert "secret" not in str(caught.value)


def test_restart_and_remove_hold_one_integration_lifecycle_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile_path = tmp_path / "profile" / "profile.json"
    profile_path.parent.mkdir()
    profile_path.write_text("{}\n", encoding="utf-8")
    service = tmp_path / "service.plist"
    held = False
    lifecycle: list[str] = []

    @contextmanager
    def lock(path: Path):
        nonlocal held
        assert path == profile_path
        assert not held
        held = True
        try:
            yield
        finally:
            held = False

    status = {
        "state": "applied",
        "label": "com.onboard.personal.0123456789abcdef0123456789abcdef",
        "host_id": "claude-desktop",
        "session": "primary",
        "targets": [{"kind": "service", "path": str(service)}],
    }
    monkeypatch.setattr(cli, "_integration_lock", lock)
    monkeypatch.setattr(
        cli, "_selected_profile", lambda _args: (SimpleNamespace(profile_path=profile_path), "test")
    )
    monkeypatch.setattr(cli, "_maintenance_profile_path", lambda _args: profile_path)
    monkeypatch.setattr(cli, "integration_status", lambda _path: status)
    monkeypatch.setattr(
        cli,
        "launchctl_commands",
        lambda _label, _service: {"restart": [cli.LAUNCHCTL_PATH, "synthetic"]},
    )

    def activate(_label: str, _service: Path) -> str:
        assert held
        lifecycle.append("activate")
        return "restarted"

    def initialize(_profile, **_kwargs):
        assert held
        lifecycle.append("initialize")
        return {"status": "ready"}

    def host_closed(_host: str) -> None:
        assert held
        lifecycle.append("host-closed")

    def deactivate(_label: str, _service: Path) -> str:
        assert held
        lifecycle.append("deactivate")
        return "stopped"

    def rollback(_path: Path, *, terminal_state: str):
        assert held
        lifecycle.append(terminal_state)
        return {"status": "complete", "state": terminal_state}

    monkeypatch.setattr(cli, "_activate_service", activate)
    monkeypatch.setattr(cli, "_initialize_personal_board", initialize)
    monkeypatch.setattr(cli, "_require_host_closed", host_closed)
    monkeypatch.setattr(cli, "_deactivate_service", deactivate)
    monkeypatch.setattr(cli, "_rollback_integration_locked", rollback)

    restarted = cli.command_restart(
        argparse.Namespace(profile=profile_path, activate=True)
    )
    removed = cli.command_remove(
        argparse.Namespace(profile=profile_path), uninstall=True
    )
    assert restarted["status"] == "restarted"
    assert removed["state"] == "uninstalled"
    assert lifecycle == [
        "activate",
        "initialize",
        "host-closed",
        "deactivate",
        "uninstalled",
    ]
    assert not held


def test_remove_before_apply_is_idempotent_and_does_not_require_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile_path = tmp_path / "profile" / "profile.json"
    profile_path.parent.mkdir()
    monkeypatch.setattr(cli, "_maintenance_profile_path", lambda _args: profile_path)
    monkeypatch.setattr(
        cli,
        "integration_status",
        lambda _path: {"state": "not-installed", "targets": []},
    )
    monkeypatch.setattr(
        cli,
        "_rollback_integration_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("not-installed removal must not enter rollback")
        ),
    )
    result = cli.command_remove(argparse.Namespace(profile=profile_path), uninstall=True)
    assert result == {
        "status": "existing",
        "state": "not-installed",
        "service_stop": "already-stopped",
        "host_restart_required": False,
        "profile_retained": False,
    }
