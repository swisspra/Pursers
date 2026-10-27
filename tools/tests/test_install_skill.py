from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import pytest

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_PATH = ROOT / "integrations/skills/pursers-install/scripts/bootstrap.py"
spec = importlib.util.spec_from_file_location("pursers_install_bootstrap", BOOTSTRAP_PATH)
assert spec and spec.loader
bootstrap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bootstrap
spec.loader.exec_module(bootstrap)


def run_cli(root: Path, command: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BOOTSTRAP_PATH), command, "--root", str(root), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class ControlledRunner:
    """Create a real venv and Central instance; intercept only package install."""

    def __init__(self, *, fail_init: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.fail_init = fail_init

    def __call__(self, command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        args = [str(part) for part in command]
        self.commands.append(args)
        if "pip" in args and "install" in args:
            assert "pursers==5.2.1" in args
            environment = Path(args[0]).parent.parent
            site_packages = environment / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
            site_packages.mkdir(parents=True, exist_ok=True)
            metadata = site_packages / "pursers-5.2.1.dist-info"
            metadata.mkdir(exist_ok=True)
            (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: pursers\nVersion: 5.2.1\n")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[-2:-1] == ["init"]:
            if self.fail_init:
                return subprocess.CompletedProcess(args, 2, "", "initialization failed")
            central_root = Path(args[-1])
            from pursers_central.quickstart import init_instance

            init_instance(central_root)
            return subprocess.CompletedProcess(args, 0, "initialized", "")
        return subprocess.run(command, **kwargs)


def test_plan_is_read_only_and_reports_deferred_readiness(tmp_path: Path) -> None:
    root = tmp_path / "private" / "pursers"
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    result = run_cli(root, "plan")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["root"] == str(root.resolve())
    assert payload["release"] == "5.2.1"
    assert payload["ready_for_work"] is False
    assert [step["id"] for step in payload["steps"]] == [
        "private-venv",
        "released-package",
        "central-init",
    ]
    assert "--apply" in payload["next_action"]
    assert "Fleet and autonomous harness activation" in payload["deferred"]
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_apply_uses_private_release_and_is_resumable_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "private" / "pursers"
    runner = ControlledRunner()
    result = bootstrap.apply(root, apply=True, runner=runner)
    assert result["ready_for_work"] is False
    assert result["installed"] is True
    assert result["initialized"] is True
    assert result["running"] == "unknown"
    assert result["service_started_by_helper"] is False
    assert (root / ".venv").is_dir()
    assert (root / "central" / "profile.env").is_file()
    receipt = json.loads((root / bootstrap.RECEIPT_NAME).read_text())
    assert receipt["release"] == "5.2.1"
    assert receipt["steps"]["central-init"] == "complete"
    worker_token = (root / "central" / "worker.jwt").read_text().strip()
    assert worker_token not in json.dumps(result)
    assert worker_token not in (root / bootstrap.RECEIPT_NAME).read_text()
    assert any("pursers==5.2.1" in command for command in runner.commands)
    before = len(runner.commands)
    again = bootstrap.apply(root, apply=True, runner=runner)
    assert again["initialized"] is True
    assert len(runner.commands) == before
    assert (root.stat().st_mode & 0o777) == 0o700
    doctor_cli = run_cli(root, "doctor")
    assert doctor_cli.returncode == 0, doctor_cli.stderr
    assert json.loads(doctor_cli.stdout)["initialized"] is True


def test_existing_foreign_root_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "private" / "existing"
    root.mkdir(parents=True)
    sentinel = root / "important.txt"
    sentinel.write_text("keep me")
    result = bootstrap.plan(root)
    assert result["blockers"]
    assert result["next_action"] == "Use $pursers-start to connect an existing installation."
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.apply(root, apply=True, runner=ControlledRunner())
    assert sentinel.read_text() == "keep me"
    assert not (root / bootstrap.RECEIPT_NAME).exists()


def test_symlink_root_is_refused_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "important.txt"
    sentinel.write_text("keep me")
    link = tmp_path / "private" / "link"
    link.parent.mkdir()
    link.symlink_to(target, target_is_directory=True)
    result = run_cli(link, "plan")
    assert result.returncode != 0
    assert "symlink" in json.loads(result.stdout)["error"].lower()
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.apply(link, apply=True, runner=ControlledRunner())
    assert sentinel.read_text() == "keep me"


def test_failed_initialization_is_recorded_without_secret_output(tmp_path: Path) -> None:
    root = tmp_path / "private" / "pursers"
    runner = ControlledRunner(fail_init=True)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.apply(root, apply=True, runner=runner)
    receipt = json.loads((root / bootstrap.RECEIPT_NAME).read_text())
    assert receipt["steps"]["released-package"] == "complete"
    assert receipt["steps"]["central-init"] == "failed"
    assert "initialization failed" not in (root / bootstrap.RECEIPT_NAME).read_text()
    doctor = bootstrap.doctor(root)
    assert doctor["initialized"] is False
    assert doctor["ready_for_work"] is False
    assert "secret" not in json.dumps(doctor).lower()
    resumed = bootstrap.apply(root, apply=True, runner=ControlledRunner())
    assert resumed["initialized"] is True


def test_apply_cli_requires_explicit_confirmation(tmp_path: Path) -> None:
    root = tmp_path / "private" / "pursers"
    result = run_cli(root, "apply")
    assert result.returncode != 0
    assert not root.exists()


def test_skill_bundle_install_check_remove_includes_install_skill(tmp_path: Path) -> None:
    manager = ROOT / "integrations/skills/manage.py"
    target = tmp_path / "project" / ".agents" / "skills"

    def manage(command: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(manager), command, "--host", "codex", "--target", str(target), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    preview = manage("plan")
    assert preview.returncode == 0, preview.stderr
    assert not target.exists()
    installed = manage("install", "--apply")
    assert installed.returncode == 0, installed.stderr
    assert (target / "pursers-install" / "SKILL.md").is_file()
    repeated = manage("install", "--apply")
    assert repeated.returncode == 0, repeated.stderr
    checked = manage("check")
    assert checked.returncode == 0, checked.stderr
    removed = manage("remove", "--apply")
    assert removed.returncode == 0, removed.stderr
    assert not (target / "pursers-install").exists()


def test_installed_skill_helper_runs_from_an_unrelated_working_directory(
    tmp_path: Path,
) -> None:
    manager = ROOT / "integrations/skills/manage.py"
    caller = tmp_path / "unrelated-cwd"
    caller.mkdir()
    scopes = {
        "project": tmp_path / "project" / ".agents" / "skills",
        "user": tmp_path / "user-home" / ".agents" / "skills",
    }
    for scope, target in scopes.items():
        installed = subprocess.run(
            [
                sys.executable,
                str(manager),
                "install",
                "--host",
                "codex",
                "--scope",
                scope,
                "--target",
                str(target),
                "--apply",
            ],
            cwd=caller,
            capture_output=True,
            text=True,
            check=False,
        )
        assert installed.returncode == 0, installed.stderr
        helper = target / "pursers-install" / "scripts" / "bootstrap.py"
        plan_root = tmp_path / f"{scope}-planned-root"
        result = subprocess.run(
            [sys.executable, str(helper), "plan", "--root", str(plan_root)],
            cwd=caller,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["root"] == str(plan_root)
        assert not plan_root.exists()
