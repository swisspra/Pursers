"""Tests for release and operations management in fleet dashboard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from release_ops import (
    ReleaseOpsManager,
    _clean_text,
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
        if "tag" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="v5.0.0a20\nv5.0.0a19\n")
        if "rev-list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="1122334455667788\n")
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


def test_restart_checklist_flags_older_bridge_processes(tmp_path: Path) -> None:
    bridge_installer = MagicMock()
    bridge_installer.inspect.return_value = {"installed_version": "0.1.0a10"}
    shim_file = tmp_path / "pursers-wait-bridge"
    shim_file.write_text("#!/bin/sh\n", encoding="utf-8")
    bridge_installer._resolve.return_value = (shim_file, "path", [])

    current_time = 100000.0
    # Shim updated at 99900 (age 100 seconds)
    os.utime(shim_file, (99900, 99900))

    inventory = MagicMock()
    inventory.load.return_value = {"seats": [{"host": "codex", "name": "worker-1"}]}

    def mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "ps" in cmd:
            # PID 101 elapsed 50s (< 100s, so started AFTER shim update)
            # PID 102 elapsed 300s (> 100s, so started BEFORE shim update -> STALE)
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


def test_publish_from_tag(tmp_path: Path) -> None:
    calls = []

    def mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Workflow triggered\n")

    ops = ReleaseOpsManager(root=tmp_path, runner=mock_runner, state_dir=tmp_path)
    result = ops.publish_from_tag("v5.0.0a20")
    assert result["ok"] is True
    assert "gh workflow run publish-pypi.yml --ref v5.0.0a20" in result["command"]
    assert result["output"] == "Workflow triggered"
    assert calls[0] == ["gh", "workflow", "run", "publish-pypi.yml", "--ref", "v5.0.0a20"]

    # Journal check
    journal = (tmp_path / "config-actions.jsonl").read_text(encoding="utf-8")
    assert "ops:publish_from_tag" in journal


def test_stage_central_preflight_and_execution(tmp_path: Path) -> None:
    profile = tmp_path / "profile.env"
    profile.write_text("CENTRAL_WHEEL=wheel.whl\nCENTRAL_WHEEL_SHA256=123\n", encoding="utf-8")
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)

    calls = []

    def mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Installed\n")

    ops = ReleaseOpsManager(
        root=tmp_path,
        profile_env_path=profile,
        central_venv_python=python,
        runner=mock_runner,
        state_dir=tmp_path,
    )
    result = ops.stage_central()
    assert result["ok"] is True
    assert "pip install --no-deps" in result["command"]
    assert calls[0][0] == "bash"
    assert calls[0][1] == "-c"


def test_kickstart_central_and_restart_dashboard(tmp_path: Path) -> None:
    calls = []

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
