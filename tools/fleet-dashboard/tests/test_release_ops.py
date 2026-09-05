"""Tests for release and operations management in fleet dashboard."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from release_ops import (
    ReleaseOpsManager,
    _clean_text,
    is_loopback_url,
    parse_elapsed_seconds,
)


def test_clean_text_redacts_tokens_and_jwts() -> None:
    fake_jwt = ".".join(["ey" + "JhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9", "eyJzdWIiOiIxMjM0In0", "signature123"])
    text = (
        f"raw_token = {fake_jwt}\n"
        "token = secret-token-value-1234\n"
        "api_key: secret-api-key\n"
        "PURSERS_TOKEN_FILE=/path/to/token.jwt\n"
    )
    cleaned = _clean_text(text)
    assert "[REDACTED]" in cleaned
    assert "token = [REDACTED]" in cleaned
    assert "api_key: [REDACTED]" in cleaned
    assert "/path/to/token.jwt" in cleaned  # Path not redacted


def test_is_loopback_url() -> None:
    assert is_loopback_url("http://127.0.0.1:8766/mcp") is True
    assert is_loopback_url("https://localhost:8899/api") is True
    assert is_loopback_url("https://pypi.org/pypi/pursers/json") is False
    assert is_loopback_url("https://api.github.com/repos") is False


def test_parse_elapsed_seconds() -> None:
    assert parse_elapsed_seconds("10") == 10
    assert parse_elapsed_seconds("01:23") == 83
    assert parse_elapsed_seconds("02:01:23") == 7283
    assert parse_elapsed_seconds("03-02:01:23") == 3 * 86400 + 7283
    assert parse_elapsed_seconds("") == 0
    assert parse_elapsed_seconds("invalid") == 0


def test_load_manifest_versions(tmp_path: Path) -> None:
    manifest = tmp_path / "release_versions.toml"
    manifest.write_text(
        'product = "5.0.0a21"\n[packages]\npursers = "5.0.0a21"\ncentral = "0.1.0a25"\n',
        encoding="utf-8",
    )
    ops = ReleaseOpsManager(manifest_path=manifest)
    versions = ops.load_manifest_versions()
    assert versions["product"] == "5.0.0a21"
    assert versions["packages"]["central"] == "0.1.0a25"


def test_origin_tag_resolution_and_local_tag_divergence(tmp_path: Path) -> None:
    def mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "ls-remote" in cmd:
            stdout = (
                "aaaaaaaa11111111\trefs/tags/v5.0.0a21\n"
                "bbbbbbbb22222222\trefs/tags/v5.0.0a22\n"
                "cccccccc33333333\trefs/tags/v5.0.0a22^{}\n"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout)
        if "tag" in cmd and "-l" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="v5.0.0a19\n")
        return subprocess.CompletedProcess(cmd, 1, stderr="error")

    ops = ReleaseOpsManager(root=tmp_path, runner=mock_runner)
    latest = ops.get_latest_tag()
    assert latest == "v5.0.0a22"
    assert ops.validate_publish_tag("v5.0.0a22") is True
    assert ops.validate_publish_tag("v9.9.9") is False


def test_ci_status_queries_intended_workflow(tmp_path: Path) -> None:
    gh_calls: list[list[str]] = []

    def mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "run" in cmd and "list" in cmd:
            gh_calls.append(cmd)
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps([{"status": "completed", "conclusion": "success", "url": "https://ci/1"}]),
            )
        if "ls-remote" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="sha123\trefs/tags/v5.0.0a20\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    ops = ReleaseOpsManager(root=tmp_path, runner=mock_runner)
    status = ops.get_ci_status("v5.0.0a20")
    assert status["main"]["conclusion"] == "success"
    assert status["tag"]["conclusion"] == "success"
    for call in gh_calls:
        assert "--workflow" in call
        idx = call.index("--workflow")
        assert call[idx + 1] == "ci.yml"


def test_release_card_status_and_pypi_checks(tmp_path: Path) -> None:
    manifest = tmp_path / "release_versions.toml"
    manifest.write_text(
        'product = "5.0.0a20"\n[packages]\npursers = "5.0.0a20"\ncentral = "0.1.0a24"\n',
        encoding="utf-8",
    )
    profile = tmp_path / "profile.env"
    profile.write_text(
        "CENTRAL_WHEEL=/dist/pursers_central-0.1.0a24-py3-none-any.whl\n"
        "CENTRAL_WHEEL_SHA256=abcdef123456\n",
        encoding="utf-8",
    )

    def mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "ls-remote" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="sha1\trefs/tags/v5.0.0a20\n")
        if "run" in cmd and "list" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps([{"status": "completed", "conclusion": "success", "url": "https://ci/1"}]),
            )
        if "release" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"tagName": "v5.0.0a20", "url": "https://github.com/rel/1"}),
            )
        if "ps" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="  00:10   12345 /bin/pursers-wait-bridge\n")
        return subprocess.CompletedProcess(cmd, 1, stderr="not mocked")

    def mock_http(url: str, timeout: float = 3.0) -> tuple[int, bytes]:
        if "healthz" in url:
            return 200, json.dumps({
                "status": "ok",
                "version": "0.1.0a24",
                "build": {"wheel_sha256": "abcdef123456"},
            }).encode("utf-8")
        if "pypi.org" in url:
            if "pursers/5.0.0a20" in url or "pursers-central/0.1.0a24" in url:
                return 200, b'{"info":{"version":"match"}}'
            return 404, b'{"error":"not found"}'
        return 500, b'{"error":"unknown"}'

    ops = ReleaseOpsManager(
        root=tmp_path,
        manifest_path=manifest,
        profile_env_path=profile,
        central_url="https://127.0.0.1:8766/mcp",
        runner=mock_runner,
        http_get=mock_http,
        state_dir=tmp_path,
    )

    status = ops.release_card_status()
    assert status["latest_tag"] == "v5.0.0a20"
    assert status["ci_status"]["main"]["conclusion"] == "success"
    assert status["ci_status"]["tag"]["conclusion"] == "success"
    assert status["pypi"]["pursers"]["present"] is True
    assert status["pypi"]["pursers"]["status_code"] == 200
    assert status["github_release"]["present"] is True
    assert status["central_version"]["live_version"] == "0.1.0a24"
    assert status["central_version"]["staged_version"] == "0.1.0a24"
    assert status["central_version"]["status"] == "matches"
    assert "commands" in status
    assert "publish_from_tag" in status["commands"]
    assert "stage_central" in status["commands"]
    assert status["commands"]["stage_central"] is None


def test_restart_checklist_flags_older_bridge_processes(tmp_path: Path) -> None:
    bridge_installer = MagicMock()
    bridge_installer.inspect.return_value = {"installed_version": "0.1.0a10"}
    shim_file = tmp_path / "pursers-wait-bridge"
    shim_file.write_text("#!/bin/sh\n", encoding="utf-8")
    bridge_installer._resolve.return_value = (shim_file, "path", [])

    current_time = 100000.0
    os.utime(shim_file, (99900, 99900))

    inventory = MagicMock()
    inventory.load.return_value = {"seats": [{"host": "codex", "name": "worker-1"}]}

    def mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "ps" in cmd:
            stdout = (
                "  00:50   101 python -m pursers_wait_server\n"
                "  05:00   102 python /bin/pursers-wait-bridge\n"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout)
        return subprocess.CompletedProcess(cmd, 1, stderr="error")

    ops = ReleaseOpsManager(
        root=tmp_path,
        runner=mock_runner,
        clock=lambda: current_time,
        bridge_installer=bridge_installer,
        inventory=inventory,
        state_dir=tmp_path,
    )

    checklist = ops.get_restart_checklist()
    codex_check = next((c for c in checklist if c["host"] == "codex"), None)
    assert codex_check is not None
    assert codex_check["needs_restart"] is True
    assert 102 in codex_check["running_pids"]
    assert "PID 102 started before shim update" in codex_check["reason"]


def test_publish_from_tag_fails_on_unavailable_origin(tmp_path: Path) -> None:
    def mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "ls-remote" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stderr="fatal: remote error\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="Workflow triggered\n")

    ops = ReleaseOpsManager(root=tmp_path, runner=mock_runner, state_dir=tmp_path)
    with pytest.raises(RuntimeError, match="could not be retrieved from origin"):
        ops.publish_from_tag("v5.0.0a20")


def test_publish_from_tag_validates_origin_tag(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "ls-remote" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="sha1\trefs/tags/v5.0.0a20\n")
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Workflow triggered\n")

    ops = ReleaseOpsManager(root=tmp_path, runner=mock_runner, state_dir=tmp_path)

    with pytest.raises(ValueError, match="does not exist on origin"):
        ops.publish_from_tag("v9.9.9")

    result = ops.publish_from_tag("v5.0.0a20")
    assert result["ok"] is True
    assert "gh workflow run publish-pypi.yml --ref v5.0.0a20" in result["command"]
    assert result["output"] == "Workflow triggered"
    assert calls[0] == ["gh", "workflow", "run", "publish-pypi.yml", "--ref", "v5.0.0a20"]


def test_stage_central_transaction_wheel_copy_pin_update_and_mode_preservation(tmp_path: Path) -> None:
    profile = tmp_path / "profile.env"
    profile.write_text(
        "CENTRAL_WHEEL=/old/path.whl\nCENTRAL_WHEEL_SHA256=oldsha\n",
        encoding="utf-8",
    )
    profile.chmod(0o600)
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)

    source_wheel = tmp_path / "pursers_central-0.1.0a25-py3-none-any.whl"
    wheel_content = b"fake-wheel-binary-data-12345"
    source_wheel.write_bytes(wheel_content)
    expected_sha = hashlib.sha256(wheel_content).hexdigest()

    manifest = tmp_path / "release_versions.toml"
    manifest.write_text(
        'product = "5.0.0a21"\n[packages]\npursers = "5.0.0a21"\ncentral = "0.1.0a25"\n',
        encoding="utf-8",
    )
    component_lock = tmp_path / "component-lock.json"
    component_lock.write_text(
        json.dumps({
            "components": {
                "pursers-central": {
                    "version": "0.1.0a25",
                    "wheel_sha256": expected_sha,
                }
            }
        }),
        encoding="utf-8",
    )

    pip_calls: list[list[str]] = []

    def mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        pip_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Successfully installed pursers_central\n")

    ops = ReleaseOpsManager(
        root=tmp_path,
        manifest_path=manifest,
        component_lock_path=component_lock,
        staging_root=tmp_path,
        profile_env_path=profile,
        central_venv_python=python,
        runner=mock_runner,
        state_dir=tmp_path,
    )

    preview = ops.get_preview_commands()["stage_central"]
    assert isinstance(preview, str)
    assert str(source_wheel) in preview
    assert str(tmp_path / "wheels" / source_wheel.name) in preview
    assert str(profile) in preview
    assert str(python) in preview
    assert expected_sha in preview
    assert "mode=0600" in preview
    assert "/path/to" not in preview
    assert "<wheel>" not in preview

    logs: list[str] = []
    result = ops.stage_central(log_callback=logs.append)
    assert result["ok"] is True
    assert result["sha256"] == expected_sha

    # Verify wheel was copied to staging directory
    dest_wheel = tmp_path / "wheels" / source_wheel.name
    assert dest_wheel.is_file()
    assert dest_wheel.read_bytes() == wheel_content

    # Verify profile.env pins were updated and mode 0600 preserved
    assert profile.stat().st_mode & 0o777 == 0o600
    assert profile.stat().st_uid == os.getuid()
    assert profile.stat().st_gid == os.getgid()
    updated_profile = profile.read_text(encoding="utf-8")
    assert f"CENTRAL_WHEEL={dest_wheel}" in updated_profile
    assert f"CENTRAL_WHEEL_SHA256={expected_sha}" in updated_profile

    # Verify pip install --no-deps was called with destination wheel
    assert pip_calls[-1] == [str(python), "-m", "pip", "install", "--no-deps", str(dest_wheel)]

    # Callers cannot redirect Stage to an arbitrary path, even with an exact name.
    arbitrary = tmp_path / "outside" / source_wheel.name
    arbitrary.parent.mkdir()
    arbitrary.write_bytes(wheel_content)
    with pytest.raises(TypeError, match="wheel_path"):
        ops.stage_central(wheel_path=arbitrary)  # type: ignore[call-arg]


def test_stage_central_refuses_mismatched_wheel_and_hash_mismatch(tmp_path: Path) -> None:
    profile = tmp_path / "profile.env"
    original_profile = "CENTRAL_WHEEL=/old.whl\nCENTRAL_WHEEL_SHA256=oldsha\n"
    profile.write_text(original_profile, encoding="utf-8")
    profile.chmod(0o600)
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)

    manifest = tmp_path / "release_versions.toml"
    manifest.write_text(
        'product = "5.0.0a21"\n[packages]\npursers = "5.0.0a21"\ncentral = "0.1.0a25"\n',
        encoding="utf-8",
    )
    component_lock = tmp_path / "component-lock.json"
    component_lock.write_text(
        json.dumps({
            "components": {
                "pursers-central": {
                    "version": "0.1.0a25",
                    "wheel_sha256": "a" * 64,
                }
            }
        }),
        encoding="utf-8",
    )

    ops = ReleaseOpsManager(
        root=tmp_path,
        manifest_path=manifest,
        component_lock_path=component_lock,
        staging_root=tmp_path,
        profile_env_path=profile,
        central_venv_python=python,
        state_dir=tmp_path,
    )

    # 1. Refuse mismatched version wheel: no exact manifest artifact resolves.
    wrong_ver = tmp_path / "pursers_central-0.1.0a24-py3-none-any.whl"
    wrong_ver.write_bytes(b"content")
    with pytest.raises(RuntimeError, match="Could not find exact wheel"):
        ops.stage_central()
    assert profile.read_text(encoding="utf-8") == original_profile

    # 2. Refuse hash mismatch before mutation
    correct_name = tmp_path / "pursers_central-0.1.0a25-py3-none-any.whl"
    correct_name.write_bytes(b"bad-content")
    with pytest.raises(ValueError, match="Preflight digest mismatch"):
        ops.stage_central()
    assert profile.read_text(encoding="utf-8") == original_profile
    assert not (tmp_path / "wheels" / correct_name.name).exists()


def test_stage_central_rollback_on_pip_install_failure(tmp_path: Path) -> None:
    profile = tmp_path / "profile.env"
    original_profile = "CENTRAL_WHEEL=/old.whl\nCENTRAL_WHEEL_SHA256=oldsha\n"
    profile.write_text(original_profile, encoding="utf-8")
    profile.chmod(0o600)
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)

    source_wheel = tmp_path / "pursers_central-0.1.0a25-py3-none-any.whl"
    wheel_content = b"fake-wheel-binary"
    source_wheel.write_bytes(wheel_content)
    expected_sha = hashlib.sha256(wheel_content).hexdigest()

    manifest = tmp_path / "release_versions.toml"
    manifest.write_text(
        'product = "5.0.0a21"\n[packages]\npursers = "5.0.0a21"\ncentral = "0.1.0a25"\n',
        encoding="utf-8",
    )
    component_lock = tmp_path / "component-lock.json"
    component_lock.write_text(
        json.dumps({
            "components": {
                "pursers-central": {
                    "version": "0.1.0a25",
                    "wheel_sha256": expected_sha,
                }
            }
        }),
        encoding="utf-8",
    )

    def mock_failing_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="pip failed", stderr="error")

    ops = ReleaseOpsManager(
        root=tmp_path,
        manifest_path=manifest,
        component_lock_path=component_lock,
        staging_root=tmp_path,
        profile_env_path=profile,
        central_venv_python=python,
        runner=mock_failing_runner,
        state_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="Pip install failed"):
        ops.stage_central()

    # Assert profile.env rolled back to original content and mode 0600
    assert profile.read_text(encoding="utf-8") == original_profile
    assert profile.stat().st_mode & 0o777 == 0o600
    # Destination wheel removed
    dest_wheel = tmp_path / "wheels" / source_wheel.name
    assert not dest_wheel.exists()


def test_kickstart_central_and_restart_dashboard(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Kickstarted\n")

    ops = ReleaseOpsManager(
        root=tmp_path,
        runner=mock_runner,
        state_dir=tmp_path,
        central_job_label="test.central",
        dashboard_job_label="test.dashboard",
    )

    res_central = ops.kickstart_central()
    assert res_central["ok"] is True
    assert "launchctl kickstart -k" in res_central["command"]
    assert "test.central" in res_central["command"]

    res_dash = ops.restart_dashboard()
    assert res_dash["ok"] is True
    assert "launchctl kickstart -k" in res_dash["command"]
    assert "test.dashboard" in res_dash["command"]
