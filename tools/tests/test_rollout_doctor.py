from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "rollout_doctor.py"
SPEC = importlib.util.spec_from_file_location("rollout_doctor", MODULE_PATH)
assert SPEC and SPEC.loader
doctor: ModuleType = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = doctor
SPEC.loader.exec_module(doctor)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(command: list[str], cwd: Path) -> str:
    return subprocess.check_output(["git", *command], cwd=cwd, text=True).strip()


def _seed_repo(path: Path, marker: str) -> str:
    path.mkdir(parents=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.name", "Fixture"], path)
    _git(["config", "user.email", "fixture@example.invalid"], path)
    _write(
        path / "tools/release_versions.toml",
        """schema_version = 1
product = "5.0.6"
[packages]
client = "0.1.5"
wait_bridge = "0.1.3"
""",
    )
    _write(path / "marker.txt", marker)
    _write(path / "tools/example.py", "print('fixture')\n")
    _git(["add", "."], path)
    env = {
        "GIT_AUTHOR_DATE": "2026-09-27T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-09-27T00:00:00+00:00",
    }
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=path,
        env={**__import__("os").environ, **env},
        check=True,
        capture_output=True,
        text=True,
    )
    return _git(["rev-parse", "HEAD"], path)


def _clone(seed: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", str(seed), str(target)],
        check=True,
    )


def _fixture(tmp_path: Path) -> tuple[SimpleNamespace, Path, Path, str]:
    home = tmp_path / "home"
    pursers_home = home / ".pursers"
    snapshot = tmp_path / "snapshot"
    proof_dir = snapshot / "proofs"
    seed = tmp_path / "seed"
    release_sha = _seed_repo(seed, "release")

    active = pursers_home / "runtimes/registry-main-release"
    _clone(seed, active / "src")
    (active / ".venv").mkdir()
    stale = pursers_home / "runtimes/review-stale"
    stale_sha = _seed_repo(stale / "src", "stale")
    assert stale_sha != release_sha
    (stale / ".venv").mkdir()
    _clone(seed, pursers_home / "runtimes/fleet-dashboard/repo")
    _clone(seed, pursers_home / "coordinator/src")

    tool = home / ".local/share/uv/tools/pursers-wait-bridge"
    _write(
        tool / "uv-receipt.toml",
        """[tool]
requirements = [{ name = "pursers-wait-bridge", specifier = "==0.1.3" }]
[tool.options]
find-links = ["file:///PATH/TO/wheels/v5.0.6"]
""",
    )
    for distribution, version in (
        ("pursers_wait_bridge", "0.1.3"),
        ("pursers_client", "0.1.5"),
    ):
        _write(
            tool / f"lib/python3.12/site-packages/{distribution}-{version}.dist-info/METADATA",
            f"Name: {distribution.replace('_', '-')}\nVersion: {version}\n",
        )

    command = str(home / ".local/bin/pursers-wait-bridge")
    _write(
        home / "Library/Application Support/Claude/claude_desktop_config.json",
        json.dumps({"mcpServers": {"pursers": {"command": command}}}),
    )
    _write(
        home / ".config/zed/settings.json",
        json.dumps({"context_servers": {"pursers": {"command": command}}}),
    )
    for label in doctor.LAUNCHD_LABELS:
        if label in {"com.pursers.fleet-dashboard", "com.pursers.board-butler"}:
            launch_source = pursers_home / "runtimes/fleet-dashboard/repo"
        elif label == "com.pursers.coordinator":
            launch_source = pursers_home / "coordinator/src"
        else:
            launch_source = active / "src"
        _write(
            snapshot / "launchd" / f"{label}.txt",
            f"state = running\npid = 123\nprogram = {launch_source}/tools/example.py\n",
        )
    _write(snapshot / "processes.txt", f"python {active}/src/tools/example.py\n")
    now = datetime(2026, 9, 27, 19, 1, tzinfo=timezone.utc)
    proofs = {
        "coordinator-digest": {
            "connected": True,
            "last_event_at": "2026-09-27T19:00:30+00:00",
        },
        "wait-worker": {"mode_by_board": {"pursers": "push"}},
        "wait-reviewer": {"mode": "push"},
        "board-butler": {
            "effective_state": "autonomous",
            "capabilities": ["approved_merge", "TK-ee3d61fd"],
        },
        "dashboard": {
            "release_sha": release_sha,
            "visual_shell": "warm-guided-home-v1",
        },
        "release-checks": {
            "release_sha": release_sha,
            "ci_manifest": "pass",
            "release_train": "pass",
        },
    }
    for name, value in proofs.items():
        _write(proof_dir / f"{name}.json", json.dumps(value))
    args = SimpleNamespace(
        home=home,
        pursers_home=pursers_home,
        snapshot_root=snapshot,
        proof_dir=proof_dir,
        release_sha=release_sha,
        now=now,
        fresh_seconds=300,
        inventory_only=False,
    )
    return args, active, stale, release_sha


def test_inspect_reports_complete_passing_inventory_and_stale_cleanup(
    tmp_path: Path,
) -> None:
    args, active, stale, release_sha = _fixture(tmp_path)

    result = doctor.inspect(args)

    assert result["schema"] == "pursers_rollout_doctor_v1"
    assert result["read_only"] is True
    assert result["release"]["sha"] == release_sha
    assert result["summary"]["ok"] is True
    assert all(item["status"] == "PASS" for item in result["checks"].values())
    launchd = {
        item["consumer"]: item
        for item in result["inventory"]
        if item["consumer"].startswith("launchd:")
    }
    assert all(
        {source["sha"] for source in item["sources"]} == {release_sha}
        for item in launchd.values()
    )
    assert all(item["entrypoint_sha256"] for item in launchd.values())
    hosts = {
        item["consumer"]: item
        for item in result["inventory"]
        if item["consumer"].startswith("host-mcp:")
    }
    assert hosts["host-mcp:Claude Desktop"]["wait_bridge_version"] == "0.1.3"
    cleanup = {item["runtime"]: item for item in result["stale_runtime_cleanup"]}
    assert cleanup["<PURSERS_HOME>/runtimes/registry-main-release"]["safe_to_delete"] is False
    assert cleanup["<PURSERS_HOME>/runtimes/review-stale"]["safe_to_delete"] is True
    assert active.exists() and stale.exists()
    assert str(args.home) not in json.dumps(result)


def test_inspect_fails_closed_for_stale_or_missing_post_rollout_proofs(
    tmp_path: Path,
) -> None:
    args, _active, stale, _release_sha = _fixture(tmp_path)
    (args.proof_dir / "wait-reviewer.json").unlink()
    _write(
        args.proof_dir / "coordinator-digest.json",
        json.dumps(
            {"connected": True, "last_event_at": "2026-09-27T18:00:00+00:00"}
        ),
    )

    result = doctor.inspect(args)

    assert result["summary"]["ok"] is False
    assert result["checks"]["wait_bridge_push_reviewer"]["status"] == "FAIL"
    assert result["checks"]["coordinator_digest_subscription"]["status"] == "FAIL"
    assert stale.exists()


def test_main_writes_only_when_output_is_explicit(tmp_path: Path, capsys) -> None:
    args, _active, stale, release_sha = _fixture(tmp_path)
    argv = [
        "--release-sha",
        release_sha,
        "--home",
        str(args.home),
        "--pursers-home",
        str(args.pursers_home),
        "--snapshot-root",
        str(args.snapshot_root),
        "--proof-dir",
        str(args.proof_dir),
        "--now",
        "2026-09-27T19:01:00+00:00",
    ]

    assert doctor.main(argv) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["summary"]["ok"] is True
    assert stale.exists()

    output = tmp_path / "doctor.json"
    assert doctor.main([*argv, "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["summary"]["ok"] is True


def test_inventory_only_reports_without_rollout_proofs(tmp_path: Path, capsys) -> None:
    args, _active, stale, release_sha = _fixture(tmp_path)
    for proof in args.proof_dir.glob("*.json"):
        proof.unlink()

    exit_code = doctor.main(
        [
            "--inventory-only",
            "--release-sha",
            release_sha,
            "--home",
            str(args.home),
            "--pursers-home",
            str(args.pursers_home),
            "--snapshot-root",
            str(args.snapshot_root),
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["mode"] == "inventory"
    assert result["checks"] == {}
    assert result["summary"] == {"fail": 0, "ok": True, "pass": 0, "warn": 0}
    assert stale.exists()


def test_short_release_sha_is_rejected() -> None:
    with pytest.raises(SystemExit):
        doctor.parse_args(["--release-sha", "abc123"])
