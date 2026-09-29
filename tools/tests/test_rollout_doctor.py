from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from importlib.metadata import version
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


def _seed_environment(root: Path, packages: dict[str, str]) -> Path:
    interpreter = root / "bin/python"
    _write(interpreter, "#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    for distribution, version in packages.items():
        normalized = distribution.replace("-", "_")
        _write(
            root
            / f"lib/python3.12/site-packages/{normalized}-{version}.dist-info/METADATA",
            f"Name: {distribution}\nVersion: {version}\n",
        )
    return interpreter


def _fixture(tmp_path: Path) -> tuple[SimpleNamespace, Path, Path, str]:
    home = tmp_path / "home"
    pursers_home = home / ".pursers"
    snapshot = tmp_path / "snapshot"
    proof_dir = snapshot / "proofs"
    seed = tmp_path / "seed"
    release_sha = _seed_repo(seed, "release")

    active = pursers_home / "runtimes/registry-main-release"
    _clone(seed, active / "src")
    active_python = _seed_environment(
        active / ".venv",
        {
            "pursers-central": "0.1.4",
            "pursers-client": "0.1.5",
            "pursers-wait-bridge": "0.1.3",
        },
    )
    stale = pursers_home / "runtimes/review-stale"
    stale_sha = _seed_repo(stale / "src", "stale")
    assert stale_sha != release_sha
    (stale / ".venv").mkdir()
    _clone(seed, pursers_home / "runtimes/fleet-dashboard/repo")
    _clone(seed, pursers_home / "coordinator/src")
    fleet_python = _seed_environment(
        pursers_home / "runtimes/fleet-dashboard/.venv",
        {"pursers-client": "0.1.5"},
    )
    coordinator_python = _seed_environment(
        pursers_home / "coordinator/.venv",
        {"pursers-client": "0.1.5"},
    )

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
        if label == "com.pursers.central":
            launch_source = active / "src"
        elif label in {"com.pursers.fleet-dashboard", "com.pursers.board-butler"}:
            launch_source = pursers_home / "runtimes/fleet-dashboard/repo"
        elif label == "com.pursers.coordinator":
            launch_source = pursers_home / "coordinator/src"
        else:
            launch_source = active / "src"
        python_line = {
            "com.pursers.central": f"CENTRAL_VENV => {active / '.venv'}",
            "com.pursers.fleet-dashboard": f"PURSERS_FLEET_PYTHON => {fleet_python}",
            "com.pursers.board-butler": f"PURSERS_BUTLER_PYTHON => {active_python}",
            "com.pursers.coordinator": (
                f"PURSERS_COORDINATOR_PYTHON => {coordinator_python}"
            ),
            "com.pursers.mong1-supervisor": "",
        }[label]
        contract_lines = {
            "com.pursers.central": "",
            "com.pursers.fleet-dashboard": "\n".join(
                (
                    f"PURSERS_BUTLER_STATE_DIR => {pursers_home}/private/butler-state",
                    "PURSERS_BUTLER_ENTRYPOINT => "
                    f"{launch_source}/tools/board-butler/board_butler.py",
                    f"PURSERS_BUTLER_PROVIDER_SECRETS_DIR => {pursers_home}/private/butler-secrets",
                )
            ),
            "com.pursers.board-butler": "\n".join(
                (
                    "PURSERS_BUTLER_FLEET_EXECUTOR_SOCKET => "
                    f"{pursers_home}/private/fleet-executor/executor.sock",
                    "PURSERS_BUTLER_FLEET_EXECUTOR_KEY_ID => board-butler-local",
                    "PURSERS_BUTLER_FLEET_EXECUTOR_PRIVATE_KEY => "
                    f"{pursers_home}/private/fleet-executor/board-butler-local.key",
                )
            ),
            "com.pursers.coordinator": "",
            "com.pursers.mong1-supervisor": "",
        }[label]
        _write(
            snapshot / "launchd" / f"{label}.txt",
            f"state = running\npid = 123\n{python_line}\n"
            f"{contract_lines}\n"
            f"program = {launch_source}/tools/example.py\n",
        )
    _write(snapshot / "processes.txt", f"python {active}/src/tools/example.py\n")
    now = datetime(2026, 9, 27, 19, 1, tzinfo=timezone.utc)
    wheel_dir = tmp_path / "wheels"
    wheel = wheel_dir / "fixture-5.0.6-py3-none-any.whl"
    _write(wheel, "fixture wheel\n")
    digest = __import__("hashlib").sha256(wheel.read_bytes()).hexdigest()
    assets = []
    for name in (
        "pursers-aionui-0.1.0.zip",
        "pursers-home-runtime-wheelhouse-5.0.6-linux-x86_64.tar.gz",
        "pursers-home-runtime-wheelhouse-5.0.6-linux-x86_64.json",
    ):
        path = wheel_dir / name
        _write(path, f"fixture {name}\n")
        assets.append(
            (
                __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
                name,
            )
        )
    sums = wheel_dir / "SHA256SUMS.txt"
    _write(
        sums,
        "\n".join(
            [f"{digest}  ./{wheel.name}", *(f"{value}  ./{name}" for value, name in assets)]
        )
        + "\n",
    )
    proofs = {
        "coordinator-digest": {
            "connected": True,
            "observed_at": "2026-09-27T19:00:30+00:00",
            "last_event_at": "2026-09-27T12:00:00+00:00",
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


def _add_systemd_snapshot(args: SimpleNamespace, active: Path) -> None:
    fleet_repo = args.pursers_home / "runtimes/fleet-dashboard/repo"
    coordinator_repo = args.pursers_home / "coordinator/src"
    service_inputs = {
        "central": (
            active / "src",
            f"CENTRAL_VENV={active}/.venv",
        ),
        "fleet-dashboard": (
            fleet_repo,
            (
                "PURSERS_FLEET_PYTHON="
                f"{args.pursers_home}/runtimes/fleet-dashboard/.venv/bin/python "
                f"PURSERS_BUTLER_STATE_DIR={args.pursers_home}/private/butler-state "
                "PURSERS_BUTLER_ENTRYPOINT="
                f"{fleet_repo}/tools/board-butler/board_butler.py "
                "PURSERS_BUTLER_PROVIDER_SECRETS_DIR="
                f"{args.pursers_home}/private/butler-secrets"
            ),
        ),
        "coordinator": (
            coordinator_repo,
            "PURSERS_COORDINATOR_PYTHON="
            f"{args.pursers_home}/coordinator/.venv/bin/python",
        ),
        "board-butler": (
            fleet_repo,
            (
                f"PURSERS_BUTLER_PYTHON={active}/.venv/bin/python "
                "PURSERS_BUTLER_FLEET_EXECUTOR_SOCKET="
                f"{args.pursers_home}/private/fleet-executor/executor.sock "
                "PURSERS_BUTLER_FLEET_EXECUTOR_KEY_ID=board-butler-local "
                "PURSERS_BUTLER_FLEET_EXECUTOR_PRIVATE_KEY="
                f"{args.pursers_home}/private/fleet-executor/board-butler-local.key"
            ),
        ),
        "mong1-supervisor": (active / "src", ""),
    }
    for role, (source, environment) in service_inputs.items():
        unit = doctor.SYSTEMD_UNITS[role]
        entrypoint = source / "tools/example.py"
        _write(
            args.snapshot_root / "systemd" / f"{unit}.txt",
            "LoadState=loaded\n"
            "ActiveState=active\n"
            "MainPID=123\n"
            f"ExecStart={{ path={entrypoint} ; argv[]={entrypoint} ; }}\n"
            f"Environment={environment}\n",
        )
    for path in (
        args.home / "Library/Application Support/Claude/claude_desktop_config.json",
        args.home / ".config/zed/settings.json",
    ):
        path.unlink()


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
    assert result["checks"]["dashboard_release_sha"]["status"] == "PASS"
    assert "dashboard_visual_shell" not in result["checks"]
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
    assert all(
        item["python_environment"]["ok"] is True
        for name, item in launchd.items()
        if name != "launchd:com.pursers.mong1-supervisor"
    )
    assert launchd["launchd:com.pursers.fleet-dashboard"]["fleet_environment"][
        "ok"
    ] is True
    assert launchd["launchd:com.pursers.board-butler"]["fleet_executor"][
        "state"
    ] == "provisioned"
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


def test_inspect_supports_systemd_service_inventory(tmp_path: Path) -> None:
    args, active, _stale, release_sha = _fixture(tmp_path)
    _add_systemd_snapshot(args, active)

    result = doctor.inspect(args)

    services = {
        item["service_role"]: item
        for item in result["inventory"]
        if item["consumer"].startswith("systemd:")
    }
    assert set(services) == set(doctor.SERVICE_ROLES)
    assert services["fleet-dashboard"]["fleet_environment"]["ok"] is True
    assert services["board-butler"]["fleet_executor"]["state"] == "provisioned"
    assert all(
        {source["sha"] for source in item["sources"]} == {release_sha}
        for item in services.values()
    )
    assert result["checks"]["host-mcp:Claude Desktop"]["status"] == "WARN"
    assert result["checks"]["host-mcp:Zed"]["status"] == "WARN"
    assert result["summary"]["ok"] is True
    assert result["summary"]["warn"] == 2


def test_inspect_warns_when_optional_ide_is_not_installed(tmp_path: Path) -> None:
    args, _active, _stale, _release_sha = _fixture(tmp_path)
    (args.home / ".config/zed/settings.json").unlink()

    result = doctor.inspect(args)

    assert result["checks"]["host-mcp:Claude Desktop"]["status"] == "PASS"
    assert result["checks"]["host-mcp:Zed"]["status"] == "WARN"
    assert result["summary"]["ok"] is True


def test_wheel_checksums_accept_real_release_manifest_shape(tmp_path: Path) -> None:
    args, _active, _stale, _release_sha = _fixture(tmp_path)

    assert doctor._wheel_checksums(args.wheel_dir, args.sha256s) is True

    second = args.wheel_dir / "second-5.0.6-py3-none-any.whl"
    _write(second, "unlisted wheel\n")
    assert doctor._wheel_checksums(args.wheel_dir, args.sha256s) is False


def test_inspect_fails_closed_for_stale_or_missing_post_rollout_proofs(
    tmp_path: Path,
) -> None:
    args, _active, stale, _release_sha = _fixture(tmp_path)
    (args.proof_dir / "wait-reviewer.json").unlink()
    _write(
        args.proof_dir / "coordinator-digest.json",
        json.dumps(
            {
                "connected": True,
                "observed_at": "2026-09-27T18:00:00+00:00",
                "last_event_at": "2026-09-27T19:00:30+00:00",
            }
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
                "deviations": {
                    "fleet_executor": {
                        "status": "not_provisioned",
                        "recorded": True,
                    }
                },
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


def test_inspect_warns_for_recorded_approved_merge_deviation(
    tmp_path: Path,
) -> None:
    args, _active, _stale, _release_sha = _fixture(tmp_path)
    proof_path = args.proof_dir / "board-butler.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof["capabilities"] = []
    proof["deviations"] = {
        "approved_merge": {"status": "not_granted", "recorded": True}
    }
    _write(proof_path, json.dumps(proof))

    result = doctor.inspect(args)

    assert result["checks"]["board_butler_autonomous_merge"]["status"] == "WARN"
    assert result["summary"]["ok"] is True


def test_inspect_accepts_recorded_executor_not_provisioned_deviation(
    tmp_path: Path,
) -> None:
    args, _active, _stale, release_sha = _fixture(tmp_path)
    butler_launchd = (
        args.snapshot_root / "launchd/com.pursers.board-butler.txt"
    )
    launchd = butler_launchd.read_text(encoding="utf-8")
    launchd = "\n".join(
        line
        for line in launchd.splitlines()
        if not line.strip().startswith("PURSERS_BUTLER_FLEET_EXECUTOR_")
    )
    _write(butler_launchd, launchd + "\n")
    proof_path = args.proof_dir / "board-butler.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    proof["fleet"] = {"status": "disabled"}
    proof["deviations"] = {
        "fleet_executor": {"status": "not_provisioned", "recorded": True}
    }
    proof["state_precondition_conflict"]["release_sha"] = release_sha
    _write(proof_path, json.dumps(proof))

    result = doctor.inspect(args)

    row = next(
        item
        for item in result["inventory"]
        if item["consumer"] == "launchd:com.pursers.board-butler"
    )
    assert row["fleet_executor"]["state"] == "not_provisioned"
    assert result["checks"]["launchd:com.pursers.board-butler:fleet_executor"][
        "status"
    ] == "WARN"
    assert result["checks"]["board_butler_fleet_reconciled"]["status"] == "WARN"
    assert result["summary"]["ok"] is True
    assert result["summary"]["warn"] == 2


def test_inspect_fails_closed_for_missing_fleet_environment(
    tmp_path: Path,
) -> None:
    args, _active, _stale, _release_sha = _fixture(tmp_path)
    fleet_launchd = (
        args.snapshot_root / "launchd/com.pursers.fleet-dashboard.txt"
    )
    launchd = fleet_launchd.read_text(encoding="utf-8").replace(
        "PURSERS_BUTLER_ENTRYPOINT => ", "REMOVED_ENTRYPOINT => "
    )
    _write(fleet_launchd, launchd)

    result = doctor.inspect(args)

    row = next(
        item
        for item in result["inventory"]
        if item["consumer"] == "launchd:com.pursers.fleet-dashboard"
    )
    assert row["fleet_environment"]["missing"] == ["PURSERS_BUTLER_ENTRYPOINT"]
    assert result["checks"]["launchd:com.pursers.fleet-dashboard:fleet_environment"][
        "status"
    ] == "FAIL"
    assert result["summary"]["ok"] is False


def test_inspect_fails_closed_for_inconsistent_service_pursers_pins(
    tmp_path: Path,
) -> None:
    args, _active, _stale, _release_sha = _fixture(tmp_path)
    fleet_environment = args.pursers_home / "runtimes/fleet-dashboard/.venv"
    _write(
        fleet_environment
        / "lib/python3.12/site-packages/pursers_personal-5.0.5.dist-info/METADATA",
        "Name: pursers-personal\nVersion: 5.0.5\n",
    )

    result = doctor.inspect(args)

    row = next(
        item
        for item in result["inventory"]
        if item["consumer"] == "launchd:com.pursers.fleet-dashboard"
    )
    environment = row["python_environment"]
    assert environment["inconsistent_pursers_distributions"] == {
        "pursers-personal": {"installed": "5.0.5", "expected": "5.0.6"}
    }
    assert result["checks"][f'{row["consumer"]}:installed_packages']["status"] == (
        "FAIL"
    )
    assert result["summary"]["ok"] is False


def test_inspect_fails_closed_for_partial_fleet_executor_configuration(
    tmp_path: Path,
) -> None:
    args, _active, _stale, _release_sha = _fixture(tmp_path)
    butler_launchd = (
        args.snapshot_root / "launchd/com.pursers.board-butler.txt"
    )
    launchd = "\n".join(
        line
        for line in butler_launchd.read_text(encoding="utf-8").splitlines()
        if "PURSERS_BUTLER_FLEET_EXECUTOR_PRIVATE_KEY" not in line
    )
    _write(butler_launchd, launchd + "\n")

    result = doctor.inspect(args)

    row = next(
        item
        for item in result["inventory"]
        if item["consumer"] == "launchd:com.pursers.board-butler"
    )
    assert row["fleet_executor"]["state"] == "partial"
    assert result["checks"]["launchd:com.pursers.board-butler:fleet_executor"][
        "status"
    ] == "FAIL"
    assert result["summary"]["ok"] is False


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


def test_inspect_accepts_permanent_butler_conflict_retry_with_fix_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _active, _stale, _release_sha = _fixture(tmp_path)
    repo = args.pursers_home / "runtimes/fleet-dashboard/repo"
    _write(
        repo / "tools/board-butler/board_butler.py",
        """STATE_WRITE_MAX_ATTEMPTS = 3
class StateWriteConflict(RuntimeError): pass
for attempt in range(STATE_WRITE_MAX_ATTEMPTS):
    if \"state precondition failed\" in str(exc).casefold():
        raise StateWriteConflict
""",
    )
    monkeypatch.setattr(
        doctor,
        "_git_is_ancestor",
        lambda candidate, ancestor: candidate == repo
        and ancestor == doctor.BUTLER_FIX_SHA,
    )

    result = doctor.inspect(args)

    assert result["checks"]["board_butler_conflict_guard_source"]["status"] == (
        "PASS"
    )


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


@pytest.mark.parametrize("installed_client", [None, "0.1.4"])
def test_inspect_fails_closed_for_empty_or_old_active_service_environment(
    tmp_path: Path, installed_client: str | None,
) -> None:
    args, active, _stale, _release_sha = _fixture(tmp_path)
    environment = active / ".venv"
    for metadata in environment.glob("lib/python*/site-packages/*.dist-info/METADATA"):
        metadata.unlink()
    if installed_client is not None:
        _write(
            environment
            / f"lib/python3.12/site-packages/pursers_client-{installed_client}.dist-info/METADATA",
            f"Name: pursers-client\nVersion: {installed_client}\n",
        )

    result = doctor.inspect(args)

    runtime_check = result["checks"][
        "runtime:registry-main-release:installed_packages"
    ]
    butler_check = result["checks"][
        "launchd:com.pursers.board-butler:installed_packages"
    ]
    assert result["summary"]["ok"] is False
    assert runtime_check["status"] == "FAIL"
    assert butler_check["status"] == "FAIL"
    runtime = next(
        row
        for row in result["inventory"]
        if row["consumer"] == "runtime:registry-main-release"
    )
    assert runtime["python_environment"]["installed_versions"]["pursers-client"] == (
        installed_client
    )


def test_release_inputs_are_required() -> None:
    with pytest.raises(SystemExit):
        doctor.parse_args(["--release-sha", "a" * 40])


def test_runbook_pins_reviewed_butler_fix_and_authoritative_suite() -> None:
    docs = MODULE_PATH.parents[1] / "docs/releases"
    runbook = (docs / "RUNBOOK-v5.0.6.md").read_text(encoding="utf-8")
    template = (docs / "RUNBOOK-template.md").read_text(encoding="utf-8")

    assert "ee5c9e436ce35fd906c0ac943559482046e9186a" in runbook
    assert "if git -C \"$FLEET_REPO\" merge-base --is-ancestor" in runbook
    assert "tools/board-butler/tests/test_board_butler.py" in runbook
    assert "PURSERS_BUTLER_ENTRYPOINT" in runbook
    assert "PURSERS_BUTLER_PROVIDER_SECRETS_DIR must be outside" in runbook
    assert "RUNBOOK-template.md" in runbook
    assert "serve_tls.build_app(data_dir_override=...)" in runbook
    assert '--find-links "$RELEASE_WHEELS"' in template
    assert "  --no-index" not in template
    assert '"pursers-central==$CENTRAL_VERSION"' in template
    assert '"pursers-client==$CLIENT_VERSION"' in template
    assert "grep -E '^[0-9a-fA-F]{64}  (\\./)?[^/]+\\.whl$'" in template
    assert "listed != expected" in template
    assert '(cd "$RELEASE_WHEELS" && shasum -a 256 -c SHA256SUMS.txt)' not in template
    assert "python3 -m venv \"$TARGET_VENV\"" in template
    assert "old-third-party-constraints.txt" in template
    assert "canonical_name(line: str)" in template
    assert '"pursers-personal==$PRODUCT_VERSION"' in template
    assert '"pursers-acp==$ACP_VERSION"' in template
    assert '"pursers-personal-import==$IMPORT_VERSION"' in template
    assert "CENTRAL_SOURCE_SHA256" in template
    assert "CLIENT_SOURCE_SHA256" in template
    assert "sqlite3 \"$CENTRAL_DB\" \".backup" in template
    assert "module.build_app(data_dir_override=data_dir)" in template
    assert '"$CENTRAL_VENV/bin/pursers-central" run "$CENTRAL_SMOKE_PROFILE"' in template
    assert "ONBOARD_CENTRAL_PORT" in template
    assert "CENTRAL_JWT_AUDIENCE" in template
    assert "ONBOARD_CENTRAL_DATA_DIR" in template
    assert "systemctl --user daemon-reload" in template
    assert "StartLimitBurst=1" in template
    central_reset = template.index('systemctl --user reset-failed "$CENTRAL_SERVICE_NAME"')
    executor_reset = template.index(
        'systemctl --user reset-failed "$FLEET_EXECUTOR_SERVICE_NAME"'
    )
    butler_reset = template.index('systemctl --user reset-failed "$BUTLER_SERVICE_NAME"')
    assert central_reset < executor_reset < butler_reset
    assert 'systemctl --user restart "$CENTRAL_SERVICE_NAME"' not in template
    assert "PURSERS_BUTLER_STATE_DIR" in template
    assert "PURSERS_BUTLER_ENTRYPOINT" in template
    assert "PURSERS_BUTLER_PROVIDER_SECRETS_DIR" in template
    assert '"status":"not_provisioned","recorded":true' in template
    assert "Bad request" in template
    assert (
        "/PATH/TO/services/pursers/{central,config,credentials,state,bin,backups}"
        in runbook
    )
    assert "neither `uv` nor `gh`" in runbook
    assert "python3 -m venv" in runbook
    assert "ONBOARD_CENTRAL_PORT" in runbook
    assert "CENTRAL_JWT_AUDIENCE" in runbook
    assert "ONBOARD_CENTRAL_DATA_DIR" in runbook
    central_start = runbook.index("systemctl --user start pursers-central")
    executor_start = runbook.index("systemctl --user start pursers-fleet-executor")
    butler_start = runbook.index("systemctl --user start pursers-butler")
    assert central_start < executor_start < butler_start


def test_runbook_constraint_filter_handles_real_pip_freeze_forms(
    tmp_path: Path,
) -> None:
    template = (
        MODULE_PATH.parents[1] / "docs/releases/RUNBOOK-template.md"
    ).read_text(encoding="utf-8")
    marker = '"$OLD_SERVICE_PYTHON" - "$OLD_FREEZE" "$THIRD_PARTY_CONSTRAINTS" <<\'PY\'\n'
    script = template.split(marker, 1)[1].split("\nPY\n", 1)[0]
    source = tmp_path / "freeze.txt"
    destination = tmp_path / "constraints.txt"
    _write(
        source,
        f"""pip=={version("pip")}
 pursers @ file:///PATH/TO/pursers
MCP[cli] @ file:///PATH/TO/mcp
-e git+https://example.invalid/repo#egg=pursers-client
pursers_wait_bridge===0.1.2
# Editable install with no version control (pursers-demo==0.0.1)
-e /PATH/TO/pursers-demo
# Editable install with no version control (mcp==2.2.0)
-e /PATH/TO/mcp
""",
    )

    subprocess.run(
        [sys.executable, "-c", script, str(source), str(destination)],
        check=True,
    )

    assert destination.read_text(encoding="utf-8") == f"pip=={version('pip')}\n"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--no-index",
            "--break-system-packages",
            "--constraint",
            str(destination),
            f"pip=={version('pip')}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    _write(
        source,
        """# Editable install with no version control (customer-addon==1.0.0)
-e /PATH/TO/customer-addon
""",
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(source), str(destination)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "operator-approved immutable pin" in result.stderr


def test_runbook_smoke_adapters_are_mutually_exclusive_and_share_cleanup() -> None:
    template = (
        MODULE_PATH.parents[1] / "docs/releases/RUNBOOK-template.md"
    ).read_text(encoding="utf-8")
    smoke = template.split('case "$CENTRAL_SMOKE_ADAPTER" in', 1)[1].split(
        "Take a second SQLite", 1
    )[0]
    profile_branch, launcher_branch = smoke.split("  launcher)", 1)
    launcher_branch = launcher_branch.split("esac", 1)[0]

    assert (
        '"$CENTRAL_VENV/bin/pursers-central" run "$CENTRAL_SMOKE_PROFILE" &'
        in profile_branch
    )
    assert "module.build_app(data_dir_override=data_dir)" not in profile_branch
    assert "module.build_app(data_dir_override=data_dir)" in launcher_branch
    assert "pursers-central\" run" not in launcher_branch
    assert smoke.count("SMOKE_PID=$!") == 1
    assert smoke.index("SMOKE_PID=$!") < smoke.index("trap 'kill \"$SMOKE_PID\"")
    assert smoke.index("trap 'kill \"$SMOKE_PID\"") < smoke.index(
        "for attempt in 1 2 3 4 5 6 7 8 9 10"
    )


def test_runbook_systemd_central_install_is_uv_free() -> None:
    template = (
        MODULE_PATH.parents[1] / "docs/releases/RUNBOOK-template.md"
    ).read_text(encoding="utf-8")
    install_case = template.split('case "$CENTRAL_SERVICE_KIND" in', 1)[1].split(
        '"$CENTRAL_VENV/bin/python" -m pip check', 1
    )[0]
    systemd_branch, launchd_branch = install_case.split("  launchd)", 1)

    assert "CENTRAL_VENV=$TARGET_VENV" in systemd_branch
    assert '"$CENTRAL_VENV/bin/python" -m pip install' in systemd_branch
    assert "\n    uv " not in systemd_branch
    assert "uv venv" in launchd_branch


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
