from __future__ import annotations

import ast
import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "refresh_generated_verifier", ROOT / "refresh_generated_verifier.py"
)
assert SPEC and SPEC.loader
refresh_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh_module)


def make_seat(tmp_path: Path, *, role: str = "reviewer") -> tuple[Path, bytes]:
    from test_seat_new import args, seat_new, synthetic_jwt

    seat = seat_new.generate(args(tmp_path / "generated", role=role, client="goose"))
    token = tmp_path / "generated" / "seat.jwt"
    token.write_text(synthetic_jwt("PR-synthetic-refresh"), encoding="utf-8")
    token.chmod(0o600)
    return seat, token.read_bytes()


def test_dry_run_then_apply_updates_only_generated_cli_and_is_idempotent(tmp_path: Path) -> None:
    seat, token_bytes = make_seat(tmp_path)
    managed = [seat / "AGENTS.md", seat / ".goosehints", seat / "bin" / "board.sh"]
    before = {path: path.read_bytes() for path in managed}
    cli = seat / "bin" / "board.py"
    original = cli.read_bytes()
    stale = cli.read_text(encoding="utf-8").replace("DEFAULT_WAIT_S = 270", "DEFAULT_WAIT_S = 271", 1)
    cli.write_text(stale, encoding="utf-8")
    cli.chmod(0o640)

    preview = refresh_module.refresh(seat, apply=False, idle_confirmed=False)
    assert preview["changed"] is True
    assert preview["applied"] is False
    assert preview["after_sha256"] == preview["before_sha256"]
    assert not (seat / ".pursers" / "verifier-refresh").exists()

    with pytest.raises(refresh_module.RefreshError, match="idle-confirmed"):
        refresh_module.refresh(seat, apply=True, idle_confirmed=False)
    result = refresh_module.refresh(seat, apply=True, idle_confirmed=True)
    assert result["applied"] is True
    assert result["after_sha256"] == result["candidate_sha256"]
    assert stat.S_IMODE(cli.stat().st_mode) == 0o640
    assert {path: path.read_bytes() for path in managed} == before
    assert (seat / "bin" / "board.sh").read_bytes() == before[managed[-1]]
    assert (seat / "AGENTS.md").read_bytes() == before[managed[0]]
    assert (seat / ".goosehints").read_bytes() == before[managed[1]]
    backup = Path(result["backups"][0])
    assert backup.read_bytes() == stale.encode("utf-8")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert token_bytes == (tmp_path / "generated" / "seat.jwt").read_bytes()
    assert "PR-synthetic-refresh" not in json.dumps(result)
    again = refresh_module.refresh(seat, apply=True, idle_confirmed=True)
    assert again["changed"] is False and again["applied"] is False


@pytest.mark.parametrize("role", ["worker", "reviewer"])
def test_refresh_regenerates_canonical_cli_for_each_role(tmp_path: Path, role: str) -> None:
    from test_seat_new import seat_new

    seat, _ = make_seat(tmp_path, role=role)
    result = refresh_module.refresh(seat, apply=True, idle_confirmed=True)
    cli = (seat / "bin" / "board.py").read_text(encoding="utf-8")
    assert result["role"] == role
    assert f"ROLE = '{role}'" in cli
    assert result["role"] == role
    cli_path = seat / "bin" / "board.py"
    permitted = ("claim", "submit") if role == "worker" else ("verify", "review-claim")
    forbidden = ("verify", "review-claim") if role == "worker" else ("claim", "submit")
    for command in permitted:
        assert subprocess.run(
            [sys.executable, str(cli_path), command, "--help"], capture_output=True,
        ).returncode == 0
    for command in forbidden:
        assert subprocess.run(
            [sys.executable, str(cli_path), command, "--help"], capture_output=True,
        ).returncode != 0
    parser = next(
        node for node in ast.parse(cli).body
        if isinstance(node, ast.FunctionDef) and node.name == "_parser"
    )
    role_branch = next(
        node for node in ast.walk(parser)
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name) and node.test.left.id == "ROLE"
    )
    exposed = {
        node.args[0].value for statement in (role_branch.body if role == "worker" else role_branch.orelse)
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser" and node.args
        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
    }
    assert set(permitted).issubset(exposed)
    assert not set(forbidden).intersection(exposed)
    shell = (seat / "bin" / "board.sh").read_text(encoding="utf-8")
    expected_agent = f"export ONBOARD_AGENT_NAME={role}-a"
    expected_work = f"export PURSERS_CAN_WORK={'true' if role == 'worker' else 'false'}"
    expected_review = f"export PURSERS_CAN_REVIEW={'true' if role == 'reviewer' else 'false'}"
    assert expected_agent in shell
    assert expected_work in shell
    assert expected_review in shell


def test_refresh_refuses_identity_drift_unknown_layout_and_symlinks(tmp_path: Path) -> None:
    seat, _ = make_seat(tmp_path)
    (seat / ".goosehints").write_text("changed identity\n", encoding="utf-8")
    with pytest.raises(refresh_module.RefreshError, match="differ"):
        refresh_module.refresh(seat, apply=False, idle_confirmed=False)

    clean, _ = make_seat(tmp_path / "clean")
    launcher = clean / "bin" / "board.sh"
    payload = launcher.read_bytes()
    launcher.unlink()
    launcher.symlink_to(clean / "AGENTS.md")
    with pytest.raises(refresh_module.RefreshError, match="unsafe"):
        refresh_module.refresh(clean, apply=False, idle_confirmed=False)
    launcher.unlink()
    launcher.write_bytes(payload)
    launcher.chmod(0o755)

    cli = clean / "bin" / "board.py"
    content = cli.read_text(encoding="utf-8")
    cli.write_text(content.replace("ROLE = 'reviewer'", "ROLE = 'worker'", 1), encoding="utf-8")
    with pytest.raises(refresh_module.RefreshError, match="role/repository"):
        refresh_module.refresh(clean, apply=False, idle_confirmed=False)

    unsafe_parent, _ = make_seat(tmp_path / "unsafe-parent")
    outside = tmp_path / "outside"
    outside.mkdir()
    (unsafe_parent / ".pursers").symlink_to(outside, target_is_directory=True)
    cli = unsafe_parent / "bin" / "board.py"
    cli.write_text(cli.read_text(encoding="utf-8").replace(
        "DEFAULT_WAIT_S = 270", "DEFAULT_WAIT_S = 271", 1
    ), encoding="utf-8")
    with pytest.raises(refresh_module.RefreshError, match="snapshot parent"):
        refresh_module.refresh(unsafe_parent, apply=True, idle_confirmed=True)
    assert list(outside.iterdir()) == []


def test_refresh_migrates_effective_legacy_verifier_only_and_idempotently(tmp_path: Path) -> None:
    seat, _ = make_seat(tmp_path)
    current = seat / "bin" / "board-verifier-current.py"
    generated = (seat / "bin" / "board.py").read_text(encoding="utf-8")
    current.write_text(generated.replace("DEFAULT_WAIT_S = 270", "DEFAULT_WAIT_S = 271", 1), encoding="utf-8")
    current.chmod(0o750)
    launcher = seat / "bin" / "board.sh"
    launcher_text = launcher.read_text(encoding="utf-8")
    launcher.write_text(launcher_text.replace(
        '"$SCRIPT_DIR/board.py" "$@"', '"$SCRIPT_DIR/board-verifier-current.py" "$@"', 1
    ), encoding="utf-8")
    launcher.chmod(0o755)
    launcher_before = launcher.read_bytes()
    standard_before = (seat / "bin" / "board.py").read_bytes()

    preview = refresh_module.refresh(seat, apply=False, idle_confirmed=False)
    assert preview["migrates_effective_verifier"] is True
    assert preview["changed"] is True
    assert preview["after_sha256"] == preview["files"]["board-verifier-current.py"]
    assert launcher.read_bytes() == launcher_before
    assert (seat / "bin" / "board.py").read_bytes() == standard_before

    applied = refresh_module.refresh(seat, apply=True, idle_confirmed=True)
    assert applied["applied"] is True
    assert applied["after_sha256"] == applied["candidate_sha256"]
    assert launcher.read_bytes() == launcher_before
    assert (seat / "bin" / "board.py").read_bytes() == standard_before
    assert current.read_bytes() == standard_before
    assert stat.S_IMODE(current.stat().st_mode) == 0o750
    assert Path(applied["backups"][0]).read_bytes() != standard_before
    again = refresh_module.refresh(seat, apply=True, idle_confirmed=True)
    assert again["changed"] is False and again["applied"] is False


def test_refresh_preserves_explicit_seat_capabilities_and_does_not_open_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_seat_new import seat_new
    seat, token_bytes = make_seat(tmp_path, role="worker")
    shell = seat / "bin" / "board.sh"
    shell_text = shell.read_text(encoding="utf-8")
    assert "export ONBOARD_AGENT_NAME=worker-a" in shell_text
    assert "export PURSERS_CAN_WORK=true" in shell_text
    assert "export PURSERS_CAN_REVIEW=false" in shell_text
    shell.write_text(shell_text.replace("export PURSERS_CAN_REVIEW=false", "export PURSERS_CAN_REVIEW=true"), encoding="utf-8")
    shell.chmod(0o755)
    token = tmp_path / "generated" / "seat.jwt"
    token.write_text("unreadable-to-refresher", encoding="utf-8")
    token.chmod(0)

    result = refresh_module.refresh(seat, apply=False, idle_confirmed=False)

    assert result["identity"] == "worker-a"
    assert result["role"] == "worker"
    assert "exec " in shell.read_text(encoding="utf-8")
    assert "export PURSERS_CAN_REVIEW=true" in shell.read_text(encoding="utf-8")
    assert token.stat().st_mode & 0o777 == 0
    token.chmod(0o600)
    assert token_bytes != token.read_bytes()
