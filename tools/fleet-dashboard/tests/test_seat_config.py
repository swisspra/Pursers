from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "seat_config.py"
SPEC = importlib.util.spec_from_file_location("seat_config", MODULE_PATH)
assert SPEC and SPEC.loader
seat_config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = seat_config
SPEC.loader.exec_module(seat_config)


def desired(tmp_path: Path, host: str, **overrides):
    values = {
        "host": host,
        "role": "worker",
        "name": f"{host}-worker",
        "central_url": "https://central.example/mcp",
        "home_board": "pursers",
        "token_file": str(tmp_path / "seat.jwt"),
        "ca_file": str(tmp_path / "ca.pem"),
        "bridge_command": str(tmp_path / "bin/pursers-wait-bridge"),
        "config_path": str(tmp_path / f"{host}.config"),
    }
    values.update(overrides)
    return seat_config.DesiredSeat(**values)


def test_profiles_match_wait_bridge_and_keep_host_margins() -> None:
    actual = seat_config.wait_bridge_host_timeouts()
    assert actual == {
        host: profile.host_timeout_s
        for host, profile in seat_config.HOST_PROFILES.items()
    }
    assert {
        host: profile.block_s for host, profile in seat_config.HOST_PROFILES.items()
    } == {
        "codex": 560,
        "codex-cli": 560,
        "goose": 270,
        "claude-code": 21_540,
        "claude-desktop": 200,
        "headless": 21_540,
    }


def test_codex_plan_apply_inspect_backup_and_idempotency(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("# keep this comment\n[features]\nweb_search = true\n")
    adapter = seat_config.CodexAdapter(config)
    target = desired(tmp_path, "codex", config_path=str(config))

    plan = adapter.plan(target)
    assert len(plan) == 1
    result = adapter.apply(plan)
    document = tomllib.loads(config.read_text())

    assert document["features"]["web_search"] is True
    assert document["mcp_servers"][target.connector_name]["tool_timeout_sec"] == 620
    assert (
        document["mcp_servers"]["pursers-dev"]["bearer_token_env_var"]
        == "ONBOARD_CENTRAL_TOKEN"
    )
    assert (
        document["mcp_servers"][target.connector_name]["env"]
        ["ONBOARD_CENTRAL_TOKEN_FILE"]
        == target.token_file
    )
    assert "# keep this comment" in config.read_text()
    assert len(result.backups) == 1
    assert Path(result.backups[0]).read_text().startswith("# keep this comment")
    assert adapter.inspect()["error"] is None
    assert adapter.plan(target) == []


def test_codex_replaces_uvx_and_wrong_timeout_under_private_ca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.codex-worker]\n"
        'command = "uvx"\n'
        "tool_timeout_sec = 30\n"
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "private-ca.pem"))
    target = desired(
        tmp_path,
        "codex",
        config_path=str(config),
        bridge_name="codex-worker",
    )
    adapter = seat_config.CodexAdapter(config)

    result = adapter.apply(adapter.plan(target))
    document = tomllib.loads(config.read_text())

    assert document["mcp_servers"]["codex-worker"]["command"] == "/bin/sh"
    assert document["mcp_servers"]["codex-worker"]["tool_timeout_sec"] == 620
    assert 'command = "uvx"' not in config.read_text()
    assert result.backups


def test_goose_upgrade_preserves_clone_and_extra_files(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("provider: synthetic\nextensions:\n  keep:\n    enabled: true\n")
    seat = tmp_path / "seat"
    clone = seat / "Pursers"
    clone.mkdir(parents=True)
    (clone / "keep.txt").write_text("clone")
    (seat / "operator-note.txt").write_text("keep")
    target = desired(
        tmp_path,
        "goose",
        config_path=str(config),
        seat_dir=str(seat),
    )
    adapter = seat_config.GooseAdapter(config)

    result = adapter.apply(adapter.plan(target))

    text = config.read_text()
    assert "provider: synthetic" in text
    assert "  keep:" in text
    assert f"  {target.connector_name}:" in text
    assert "    timeout: 300" in text
    assert (clone / "keep.txt").read_text() == "clone"
    assert (seat / "operator-note.txt").read_text() == "keep"
    assert sys.executable in (seat / "bin/board.sh").read_text()
    assert stat.S_IMODE((seat / "bin/board.sh").stat().st_mode) == 0o755
    assert result.backups
    assert set(Path(path).name for path in result.changed) >= {
        "config.yaml",
        "board.sh",
        "board.py",
        "AGENTS.md",
        ".goosehints",
    }
    assert adapter.plan(target) == []


def test_claude_desktop_round_trip_preserves_unrelated_json(tmp_path: Path) -> None:
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"theme": "dark", "mcpServers": {"keep": {"command": "x"}}}))
    target = desired(tmp_path, "claude-desktop", config_path=str(config))
    adapter = seat_config.ClaudeDesktopAdapter(config)

    first = adapter.apply(adapter.plan(target))
    document = json.loads(config.read_text())

    assert document["theme"] == "dark"
    assert document["mcpServers"]["keep"] == {"command": "x"}
    assert document["mcpServers"][target.connector_name]["env"]["PURSERS_HOST"] == "claude-desktop"
    assert document["mcpServers"]["pursers-personal"]["args"][-1] == target.name
    assert len(first.backups) == 1
    assert adapter.plan(target) == []


def test_claude_desktop_plans_pinned_personal_venv_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = tmp_path / "personal-venv/bin/pursers-personal"
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    target = desired(
        tmp_path,
        "claude-desktop",
        personal_command=str(command),
    )
    monkeypatch.setattr(
        seat_config.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "5.0.0a1\n", ""
        ),
    )

    plan = seat_config.ClaudeDesktopAdapter(target.config_path).plan(target)

    repair = next(change for change in plan if change.action == "personal-install")
    assert repair.path == command
    assert repair.after == seat_config._package_version("personal")


def test_claude_code_emits_command_and_writes_only_with_path(tmp_path: Path) -> None:
    no_file = desired(tmp_path, "claude-code", config_path="")
    adapter = seat_config.ClaudeCodeAdapter("")
    assert adapter.plan(no_file) == []
    assert adapter.inspect()["write_enabled"] is False
    assert adapter.command(no_file).startswith("claude mcp add-json ")

    target = desired(tmp_path, "claude-code", config_path=str(tmp_path / ".mcp.json"))
    writer = seat_config.ClaudeCodeAdapter(target.config_path)
    writer.apply(writer.plan(target))
    assert target.connector_name in json.loads(Path(target.config_path).read_text())["mcpServers"]


def test_bridge_installer_unsets_private_ca_and_never_uses_uvx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def which(name: str):
        if name == "uv":
            return "/tool/uv"
        if name == "pursers-wait-bridge" and calls:
            return "/tool/pursers-wait-bridge"
        return None

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(seat_config.shutil, "which", which)
    monkeypatch.setenv("SSL_CERT_FILE", "/private/ca.pem")
    command = seat_config.BridgeInstaller("0.1.0a6", runner=run).install()

    assert command == "/tool/pursers-wait-bridge"
    assert calls[0][0] == [
        "/tool/uv",
        "tool",
        "install",
        "--force",
        "pursers-wait-bridge==0.1.0a6",
    ]
    assert "SSL_CERT_FILE" not in calls[0][1]["env"]
    assert "uvx" not in calls[0][0]


def test_goose_clean_clone_plans_and_applies_fast_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seat = tmp_path / "seat"
    clone = seat / "Pursers"
    clone.mkdir(parents=True)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs.get("cwd")))
        if command[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["git", "rev-list", "--count"]:
            return subprocess.CompletedProcess(command, 0, "1\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(seat_config.subprocess, "run", run)
    target = desired(
        tmp_path,
        "goose",
        seat_dir=str(seat),
        repository="https://example.test/Pursers.git",
    )
    adapter = seat_config.GooseAdapter(target.config_path)
    plan = adapter.plan(target)

    assert any(change.action == "git-ff" and change.path == clone for change in plan)
    adapter.apply(plan)
    assert (["git", "pull", "--ff-only"], clone) in calls


def test_prompt_renderer_has_exact_registry_rearm_and_role_rules(tmp_path: Path) -> None:
    renderer = seat_config.PromptRenderer()
    worker = renderer.render(desired(tmp_path, "codex"))
    reviewer = renderer.render(desired(tmp_path, "claude-desktop", role="reviewer"))

    assert 'boards="registry"' in worker
    assert "timeout_s=560" in worker
    assert "whole new_seq map" in worker
    assert "bound to this Codex window" in worker
    assert "never claim, edit, commit, or push" in reviewer
    assert "200s bridge block" in reviewer
    assert "Never use another name" in reviewer


def test_inventory_and_doctor_redact_token_and_report_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = desired(tmp_path, "codex")
    Path(target.token_file).write_text("eyJhbGciOi.TOKEN_MUST_NOT_APPEAR.signature")
    Path(target.ca_file).write_text("synthetic ca")
    command = Path(target.bridge_command)
    command.parent.mkdir()
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    adapter = seat_config.CodexAdapter(target.config_path)
    adapter.apply(adapter.plan(target))

    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "mock.valid.token")

    def run(command, **_kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "0.1.0a6\n", "")

    doctor = seat_config.Doctor(
        runner=run,
        pypi_fetcher=lambda: "0.1.0a6",
        live_probe=lambda _desired, timeout: {
            "mode": "push",
            "registry_boards": ["pursers", "project-a"],
            "skipped_boards": {},
            "timeout_s": timeout,
        },
    )
    report = seat_config._doctor_document(doctor.run(target))
    serialized = json.dumps(report)

    assert report["overall"] == "PASS"
    assert "mode=push; boards=2; skipped=0" in serialized
    assert "TOKEN_MUST_NOT_APPEAR" not in serialized

    inventory = seat_config.SeatInventory(tmp_path / "state/seats.json")
    inventory.upsert(target, bridge_version="0.1.0a6", doctor=report)
    loaded = inventory.load()
    assert loaded["seats"][0]["host"] == "codex"
    assert loaded["seats"][0]["last_doctor"]["overall"] == "PASS"
    assert stat.S_IMODE(inventory.path.stat().st_mode) == 0o600


def test_default_live_probe_checks_status_subscription_and_registry_boards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = desired(tmp_path, "codex")
    Path(target.token_file).write_text("NOT_RETURNED")
    calls = []

    class FakeBoardClient:
        def __init__(self, _url, _token, board, *, agent_name):
            self.board = board
            self.agent_name = agent_name
            self.identity = SimpleNamespace(agent_id=f"AI-{board}")

        async def __aenter__(self):
            calls.append(("join", self.board, self.agent_name))
            return self

        async def __aexit__(self, *_args):
            return None

        async def board_status(self):
            calls.append(("status", self.board))
            return {"latest_seq": 7}

        async def board_state_get(self, key):
            assert key == "project_registry"
            return {
                "state": {
                    "value": json.dumps(
                        {
                            "projects": {
                                "project": {
                                    "board_id": "project-board",
                                    "status": "active",
                                }
                            }
                        }
                    )
                }
            }

        async def events(self, **arguments):
            calls.append(("listen", self.board, arguments["from_cursor"]))
            arguments["subscription_callback"]()
            if False:
                yield {}

    monkeypatch.setitem(
        sys.modules,
        "pursers_client",
        SimpleNamespace(BoardClient=FakeBoardClient),
    )

    result = seat_config._default_live_probe(target, 0.5)

    assert result == {
        "mode": "push",
        "registry_boards": ["pursers", "project-board"],
        "skipped_boards": {},
    }
    assert ("listen", "pursers", 7) in calls
    assert ("status", "project-board") in calls


def test_doctor_poll_is_explicit_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = desired(tmp_path, "codex")
    Path(target.token_file).write_text("header.redacted.signature")
    Path(target.ca_file).write_text("ca")
    command = Path(target.bridge_command)
    command.parent.mkdir()
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "mock.jwt.token")
    seat_config.CodexAdapter(target.config_path).apply(
        seat_config.CodexAdapter(target.config_path).plan(target)
    )

    def run(command, **_kwargs):
        stdout = "" if command[0] == "ps" else "0.1.0a6\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    rows = seat_config.Doctor(
        runner=run,
        pypi_fetcher=lambda: "0.1.0a6",
        live_probe=lambda _desired, _timeout: {
            "mode": "poll",
            "registry_boards": ["pursers"],
            "skipped_boards": {},
        },
    ).run(target)
    assert next(row for row in rows if row.check == "live-smoke").status == "WARN"


def test_bridge_installer_stale_shim_and_pypi(tmp_path: Path) -> None:
    shim = tmp_path / "bin/pursers-wait-bridge"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)

    # 1. Stale shim: returns 0.1.0a5 while pinned is 0.1.0a6
    def run_stale(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, "0.1.0a5\n", "")

    installer = seat_config.BridgeInstaller(
        "0.1.0a6",
        runner=run_stale,
        command=shim,
        pypi_fetcher=lambda: "0.1.0a6",
    )
    info = installer.inspect()
    assert info["installed"] is True
    assert info["installed_version"] == "0.1.0a5"
    assert info["pinned_version"] == "0.1.0a6"
    assert info["latest_pypi_version"] == "0.1.0a6"
    assert info["status"] == "FAIL"
    assert "installed=0.1.0a5; pinned=0.1.0a6" in info["message"]

    # In Doctor, stale bridge causes FAIL
    target = desired(tmp_path, "codex", bridge_command=str(shim))
    Path(target.token_file).write_text("part1.part2.part3")
    Path(target.ca_file).write_text("ca")
    doc = seat_config.Doctor(
        runner=run_stale,
        pypi_fetcher=lambda: "0.1.0a6",
        live_probe=lambda _d, _t: {"mode": "push", "registry_boards": ["pursers"], "skipped_boards": {}},
    )
    rows = doc.run(target)
    bridge_check = next(r for r in rows if r.check == "bridge")
    assert bridge_check.status == "FAIL"

    # 2. Installed matches pinned, but PyPI is unreachable: WARN
    def run_current(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, "0.1.0a6\n", "")

    installer_warn = seat_config.BridgeInstaller(
        "0.1.0a6",
        runner=run_current,
        command=shim,
        pypi_fetcher=lambda: None,
    )
    info_warn = installer_warn.inspect()
    assert info_warn["installed_version"] == "0.1.0a6"
    assert info_warn["latest_pypi_version"] is None
    assert info_warn["status"] == "WARN"
    assert "PyPI unreachable" in info_warn["message"]

    doc_warn = seat_config.Doctor(
        runner=run_current,
        pypi_fetcher=lambda: None,
        live_probe=lambda _d, _t: {"mode": "push", "registry_boards": ["pursers"], "skipped_boards": {}},
    )
    rows_warn = doc_warn.run(target)
    bridge_warn = next(r for r in rows_warn if r.check == "bridge")
    assert bridge_warn.status == "WARN"


def test_doctor_bearer_env_var_missing_vs_defined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = desired(tmp_path, "codex")
    Path(target.token_file).write_text("part1.part2.part3")
    Path(target.ca_file).write_text("ca")
    command = Path(target.bridge_command)
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    seat_config.CodexAdapter(target.config_path).apply(
        seat_config.CodexAdapter(target.config_path).plan(target)
    )

    # 1. Variable is unset in current environment and login shell returns empty
    monkeypatch.delenv("ONBOARD_CENTRAL_TOKEN", raising=False)

    def run_missing(command, **_kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "zsh":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "0.1.0a6\n", "")

    doc = seat_config.Doctor(
        runner=run_missing,
        pypi_fetcher=lambda: "0.1.0a6",
        live_probe=lambda _d, _t: {"mode": "push", "registry_boards": ["pursers"], "skipped_boards": {}},
    )
    rows = doc.run(target)
    env_check = next(r for r in rows if r.check == "token-env")
    assert env_check.status == "FAIL"
    assert "'ONBOARD_CENTRAL_TOKEN' is not defined" in env_check.message
    assert "~/.zshrc" in env_check.message
    assert "launchctl setenv" in env_check.message

    # 2. Variable is defined in process env -> PASS
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "valid-token")
    rows_pass = doc.run(target)
    env_pass = next(r for r in rows_pass if r.check == "token-env")
    assert env_pass.status == "PASS"

    # 3. Variable unset in process env, but defined in login shell -> PASS
    monkeypatch.delenv("ONBOARD_CENTRAL_TOKEN", raising=False)

    def run_shell_defined(command, **_kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "zsh":
            return subprocess.CompletedProcess(command, 0, "set", "")
        return subprocess.CompletedProcess(command, 0, "0.1.0a6\n", "")

    doc_shell = seat_config.Doctor(
        runner=run_shell_defined,
        pypi_fetcher=lambda: "0.1.0a6",
        live_probe=lambda _d, _t: {"mode": "push", "registry_boards": ["pursers"], "skipped_boards": {}},
    )
    rows_shell = doc_shell.run(target)
    env_shell = next(r for r in rows_shell if r.check == "token-env")
    assert env_shell.status == "PASS"


def test_doctor_token_file_validation_and_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = desired(tmp_path, "codex")
    Path(target.ca_file).write_text("ca")
    command = Path(target.bridge_command)
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "valid.mock.token")

    def run_ok(command, **_kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "0.1.0a6\n", "")

    doc = seat_config.Doctor(
        runner=run_ok,
        pypi_fetcher=lambda: "0.1.0a6",
        live_probe=lambda _d, _t: {"mode": "push", "registry_boards": ["pursers"], "skipped_boards": {}},
    )

    # 1. Missing file
    if Path(target.token_file).exists():
        Path(target.token_file).unlink()
    row = next(r for r in doc.run(target) if r.check == "token-file")
    assert row.status == "FAIL"
    assert row.message == "missing or unreadable"

    # 2. Empty file
    Path(target.token_file).write_text("   \n")
    row = next(r for r in doc.run(target) if r.check == "token-file")
    assert row.status == "FAIL"
    assert row.message == "token file is empty"

    # 3. Not JWT shape (no dots)
    secret_bad = "SUPER_SECRET_TOKEN_NOT_JWT"
    Path(target.token_file).write_text(secret_bad)
    report = seat_config._doctor_document(doc.run(target))
    row = next(r for r in report["checks"] if r["check"] == "token-file")
    assert row["status"] == "FAIL"
    assert "invalid JWT format" in row["message"]
    # Ensure sensitive content was NEVER printed or logged
    assert secret_bad not in json.dumps(report)

    # 4. Not JWT shape (two dots but invalid base64url characters)
    Path(target.token_file).write_text("part1.part2.part3!@#")
    row = next(r for r in doc.run(target) if r.check == "token-file")
    assert row.status == "FAIL"
    assert "invalid JWT format" in row.message

    # 5. Valid JWT shape
    secret_good = "SECRET_PAYLOAD_CONTENT"
    Path(target.token_file).write_text(f"eyJhbGciOi.{secret_good}.signature_abc123")
    report_good = seat_config._doctor_document(doc.run(target))
    row_good = next(r for r in report_good["checks"] if r["check"] == "token-file")
    assert row_good["status"] == "PASS"
    assert "valid JWT" in row_good["message"]
    assert secret_good not in json.dumps(report_good)


def test_doctor_uvx_under_private_ca_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Unset SSL_CERT_FILE + system CA: must NOT produce the private-CA FAIL
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "valid.mock.token")
    system_ca = "/etc/ssl/cert.pem"
    assert seat_config._is_private_ca(system_ca) is False

    target_sys = desired(
        tmp_path,
        "codex",
        bridge_command="uvx pursers-wait-bridge --from git+https://github.com/swisspra/Pursers.git",
        ca_file=system_ca,
    )
    Path(target_sys.token_file).write_text("part1.part2.part3")

    def run_bridge(command, **_kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "0.1.0a6\n", "")

    doc = seat_config.Doctor(
        runner=run_bridge,
        pypi_fetcher=lambda: "0.1.0a6",
        live_probe=lambda _d, _t: {"mode": "push", "registry_boards": ["pursers"], "skipped_boards": {}},
    )
    rows_sys = doc.run(target_sys)
    bridge_check_sys = next(r for r in rows_sys if r.check == "bridge")
    # Must NOT produce the private-CA FAIL
    assert "uvx --from fails under private CA" not in bridge_check_sys.message

    # 2. Custom private bundle under a system-like directory: must still be private
    system_like_dir = tmp_path / "etc/ssl"
    system_like_dir.mkdir(parents=True)
    custom_private_ca = system_like_dir / "custom-private-ca.pem"
    custom_private_ca.write_text("synthetic private CA")
    assert seat_config._is_private_ca(custom_private_ca) is True
    assert seat_config._is_private_ca("/etc/ssl/custom-private-bundle.pem") is True

    monkeypatch.setenv("SSL_CERT_FILE", str(custom_private_ca))
    target_priv = desired(
        tmp_path,
        "codex",
        bridge_command="uvx pursers-wait-bridge --from git+https://github.com/swisspra/Pursers.git",
        ca_file=str(custom_private_ca),
    )
    Path(target_priv.token_file).write_text("part1.part2.part3")

    rows_priv = doc.run(target_priv)
    bridge_check_priv = next(r for r in rows_priv if r.check == "bridge")
    assert bridge_check_priv.status == "FAIL"
    assert "uvx --from fails under private CA" in bridge_check_priv.message


def test_doctor_cat_token_file_reference_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_dir = tmp_path / "dir with spaces"
    token_dir.mkdir(parents=True)
    cat_token_file = token_dir / "referenced token.jwt"

    # Use unquoted escaped-space path in shell command: e.g. /dir\ with\ spaces/referenced\ token.jwt
    escaped_cat_path = str(cat_token_file).replace(" ", r"\ ")

    config = tmp_path / "config.toml"
    # Codex config referencing unquoted escaped-space $(cat ...) in args
    config.write_text(
        f'[mcp_servers.codex-worker]\n'
        f'command = "/bin/sh"\n'
        f'args = ["-c", \'token=$(cat {escaped_cat_path}); exec pursers-wait-bridge\']\n'
        f'tool_timeout_sec = 620\n'
    )
    target = desired(tmp_path, "codex", config_path=str(config))
    # Leave desired.token_file pointing to a valid JWT so failures isolate to cat_token_file
    Path(target.token_file).write_text("header.valid.sig")
    Path(target.ca_file).write_text("ca")
    command = Path(target.bridge_command)
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "valid.mock.token")

    def run_ok(command, **_kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "0.1.0a6\n", "")

    doc = seat_config.Doctor(
        runner=run_ok,
        pypi_fetcher=lambda: "0.1.0a6",
        live_probe=lambda _d, _t: {"mode": "push", "registry_boards": ["pursers"], "skipped_boards": {}},
    )

    # 1. Missing referenced file -> FAIL
    if cat_token_file.exists():
        cat_token_file.unlink()
    row_missing = next(r for r in doc.run(target) if r.check == "token-file")
    assert row_missing.status == "FAIL"
    assert row_missing.message == "missing or unreadable"

    # 2. Empty referenced file -> FAIL
    cat_token_file.write_text("   \n")
    row_empty = next(r for r in doc.run(target) if r.check == "token-file")
    assert row_empty.status == "FAIL"
    assert row_empty.message == "token file is empty"

    # 3. Malformed referenced file -> FAIL
    bad_secret = "TOP_SECRET_MALFORMED_JWT"
    cat_token_file.write_text(bad_secret)
    report_bad = seat_config._doctor_document(doc.run(target))
    row_bad = next(r for r in report_bad["checks"] if r["check"] == "token-file")
    assert row_bad["status"] == "FAIL"
    assert "invalid JWT format" in row_bad["message"]
    assert bad_secret not in json.dumps(report_bad)

    # 4. Valid JWT referenced file (unquoted escaped-space path) -> PASS
    good_secret = "TOP_SECRET_GOOD_JWT_PAYLOAD"
    cat_token_file.write_text(f"eyJhbGciOi.{good_secret}.signature_123")
    report_good = seat_config._doctor_document(doc.run(target))
    row_good = next(r for r in report_good["checks"] if r["check"] == "token-file")
    assert row_good["status"] == "PASS"
    assert "valid JWT" in row_good["message"]
    assert good_secret not in json.dumps(report_good)

    # 5. Also verify quoted path with spaces: $(cat "/path with spaces/token.jwt") -> PASS
    config.write_text(
        f'[mcp_servers.codex-worker]\n'
        f'command = "/bin/sh"\n'
        f'args = ["-c", \'token=$(cat "{cat_token_file}"); exec pursers-wait-bridge\']\n'
        f'tool_timeout_sec = 620\n'
    )
    report_quoted = seat_config._doctor_document(doc.run(target))
    row_quoted = next(r for r in report_quoted["checks"] if r["check"] == "token-file")
    assert row_quoted["status"] == "PASS"
    assert "valid JWT" in row_quoted["message"]
    assert good_secret not in json.dumps(report_quoted)


def test_doctor_dead_nvm_npx_path_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead_path = "/Users/synthetic-user/.nvm/versions/node/v24.18.0/bin/npx"
    config = tmp_path / "config.yaml"
    config.write_text(
        f"extensions:\n"
        f"  mcp-server:\n"
        f"    cmd: /bin/sh\n"
        f"    args:\n"
        f"      - -c\n"
        f"      - 'exec {dead_path} -y mcp-remote'\n"
    )
    target = desired(tmp_path, "goose", config_path=str(config))
    Path(target.token_file).write_text("part1.part2.part3")
    Path(target.ca_file).write_text("ca")
    command = Path(target.bridge_command)
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)

    def run_ok(command, **_kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "0.1.0a6\n", "")

    doc = seat_config.Doctor(
        runner=run_ok,
        pypi_fetcher=lambda: "0.1.0a6",
        live_probe=lambda _d, _t: {"mode": "push", "registry_boards": ["pursers"], "skipped_boards": {}},
    )
    rows = doc.run(target)
    npx_check = next(r for r in rows if r.check == "connector-npx")
    assert npx_check.status == "WARN"
    assert f"dead nvm npx path: {dead_path}" in npx_check.message

    # Create the file at that path if possible, or test with tmp path matching pattern
    fake_nvm = tmp_path / ".nvm/versions/node/v24.20.0/bin/npx"
    fake_nvm.parent.mkdir(parents=True)
    fake_nvm.write_text("#!/bin/sh\n")
    fake_nvm.chmod(0o755)

    config.write_text(
        f"extensions:\n"
        f"  mcp-server:\n"
        f"    cmd: /bin/sh\n"
        f"    args:\n"
        f"      - -c\n"
        f"      - 'exec {fake_nvm} -y mcp-remote'\n"
    )
    rows_resolved = doc.run(target)
    npx_resolved = next(r for r in rows_resolved if r.check == "connector-npx")
    assert npx_resolved.status == "PASS"
    assert "resolves" in npx_resolved.message
