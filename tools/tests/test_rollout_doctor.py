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
central = "0.1.4"
acp = "0.1.4"
import = "5.0.0"
""",
    )
    _write(path / "marker.txt", marker)
    _write(path / "tools/example.py", "print('fixture')\n")
    _write(
        path / "tools/board-butler/board_butler.py",
        """await process_question(x)
await process_question(y)
_is_state_precondition_conflict(a)
_is_state_precondition_conflict(b)
def _is_state_precondition_conflict(exc): pass
deferred question after state precondition conflict
deferred pending question after state precondition conflict
""",
    )
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
    wheel_dir = tmp_path / "wheels"
    wheel = wheel_dir / "fixture-5.0.6-py3-none-any.whl"
    _write(wheel, "fixture wheel\n")
    digest = __import__("hashlib").sha256(wheel.read_bytes()).hexdigest()
    sums = wheel_dir / "SHA256SUMS.txt"
    _write(sums, f"{digest}  {wheel.name}\n")
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
            "fleet": {"status": "reconciled", "boards": ["pursers"]},
            "state_precondition_conflict": {
                "ticket_id": "TK-164fb22b",
                "release_sha": release_sha,
                "post_restart_refresh_seen": True,
                "state_precondition_traceback": False,
                "evidence_kind": "live-runtime",
            },
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
        release_tag="v5.0.6",
        wheel_dir=wheel_dir,
        sha256s=sums,
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


def test_inspect_fails_closed_when_butler_fleet_remains_disabled(
    tmp_path: Path,
) -> None:
    args, _active, _stale, _release_sha = _fixture(tmp_path)
    _write(
        args.proof_dir / "board-butler.json",
        json.dumps(
            {
                "effective_state": "autonomous",
                "capabilities": ["approved_merge", "TK-ee3d61fd"],
                "fleet": {"status": "disabled"},
                "state_precondition_conflict": {
                    "ticket_id": "TK-164fb22b",
                    "release_sha": _release_sha,
                    "post_restart_refresh_seen": True,
                    "state_precondition_traceback": False,
                    "evidence_kind": "live-runtime",
                },
            }
        ),
    )

    result = doctor.inspect(args)

    assert result["checks"]["board_butler_autonomous_merge"]["status"] == "PASS"
    check = result["checks"]["board_butler_fleet_reconciled"]
    assert check["status"] == "FAIL"
    assert "fleet status is not reconciled" in check["detail"]


def test_inspect_reports_and_accepts_exact_authorized_butler_hotfix(
    tmp_path: Path,
) -> None:
    args, _active, _stale, release_sha = _fixture(tmp_path)
    repo = args.pursers_home / "runtimes/fleet-dashboard/repo"
    _write(repo / "tools/board-butler/board_butler.py", "# TK-164fb22b\n")
    patch_sha256 = doctor._git_diff_sha256(repo)
    assert patch_sha256 is not None
    proof_path = args.proof_dir / "board-butler.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof["hotfix"] = {
        "ticket_id": "TK-164fb22b",
        "release_sha": release_sha,
        "carried_forward": True,
        "patch_sha256": patch_sha256,
    }
    _write(proof_path, json.dumps(proof))

    result = doctor.inspect(args)

    fleet = next(
        row
        for row in result["inventory"]
        if row["consumer"] == "runtime:fleet-dashboard+board-butler"
    )
    assert fleet["clean"] is False
    assert fleet["dirty_paths"] == [" M tools/board-butler/board_butler.py"]
    assert fleet["worktree_diff_sha256"] == patch_sha256
    assert result["checks"][fleet["consumer"]]["status"] == "PASS"


def test_inspect_fails_closed_without_precondition_conflict_survival_proof(
    tmp_path: Path,
) -> None:
    args, _active, _stale, _release_sha = _fixture(tmp_path)
    proof_path = args.proof_dir / "board-butler.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof.pop("state_precondition_conflict")
    _write(proof_path, json.dumps(proof))

    result = doctor.inspect(args)

    assert result["checks"]["board_butler_precondition_conflict_survival"]["status"] == "FAIL"


def test_inspect_fails_closed_for_release_input_and_version_mismatch(
    tmp_path: Path,
) -> None:
    args, _active, _stale, _release_sha = _fixture(tmp_path)
    args.release_tag = "v5.0.7"
    wheel = next(args.wheel_dir.glob("*.whl"))
    _write(wheel, "tampered\n")
    fleet = args.pursers_home / "runtimes/fleet-dashboard/repo"
    manifest = fleet / "tools/release_versions.toml"
    _write(manifest, manifest.read_text(encoding="utf-8").replace('central = "0.1.4"', 'central = "9.9.9"'))

    result = doctor.inspect(args)

    assert result["checks"]["release_wheel_inputs"]["status"] == "FAIL"
    assert result["checks"]["release_version_manifest"]["status"] == "FAIL"


def test_release_inputs_are_required() -> None:
    with pytest.raises(SystemExit):
        doctor.parse_args(["--release-sha", "a" * 40])


def test_main_writes_only_when_output_is_explicit(tmp_path: Path, capsys) -> None:
    args, _active, stale, release_sha = _fixture(tmp_path)
    argv = [
        "--release-sha",
        release_sha,
        "--release-tag",
        "v5.0.6",
        "--wheel-dir",
        str(args.wheel_dir),
        "--sha256s",
        str(args.sha256s),
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
            "--release-tag",
            "v5.0.6",
            "--wheel-dir",
            str(args.wheel_dir),
            "--sha256s",
            str(args.sha256s),
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
        doctor.parse_args(
            [
                "--release-sha",
                "abc123",
                "--release-tag",
                "v5.0.6",
                "--wheel-dir",
                "/wheels",
                "--sha256s",
                "/wheels/SHA256SUMS.txt",
            ]
        )
