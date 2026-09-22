from __future__ import annotations

import fcntl
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOLS = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import ci_manifest  # noqa: E402
from ci_manifest import (  # noqa: E402
    SUITES,
    Suite,
    changed_paths_since,
    covering_suites,
    default_job_count,
    inspect_integration_files,
    parse_collected_count,
    print_seat_digest_report,
    pytest_target,
    require_free_space,
    run_suites,
    run_seat_suites,
    suite_environment,
    validate_integration_files,
    validate_manifest,
    verify_counts,
)


def test_manifest_covers_every_test_directory() -> None:
    validate_manifest(REPOSITORY_ROOT)


def test_low_space_guard_fires_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ci_manifest.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10_000, used=9_001, free=999),
    )

    with pytest.raises(RuntimeError, match=r"LOW DISK SPACE.*free_bytes=999"):
        require_free_space(tmp_path, minimum_free_bytes=1_000)

    monkeypatch.setattr(
        ci_manifest.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=10_000, used=9_000, free=1_000),
    )
    require_free_space(tmp_path, minimum_free_bytes=1_000)


def test_integration_files_manifest_matches_the_tree() -> None:
    validate_integration_files(REPOSITORY_ROOT)


def test_integration_files_manifest_rejects_a_stale_entry(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("current\n", encoding="utf-8")
    manifest = tmp_path / "INTEGRATION_FILES.sha256"
    manifest.write_text(
        "48aa6cae8c70abdb28631d22b316e6d9f9d0768ec2911de7090e248b2afe6ca1"
        "  tracked.txt\n",
        encoding="utf-8",
    )

    validate_integration_files(tmp_path, Path("INTEGRATION_FILES.sha256"))
    manifest.write_text(f"{'0' * 64}  tracked.txt\n", encoding="utf-8")

    state = inspect_integration_files(tmp_path, Path("INTEGRATION_FILES.sha256"))
    assert state.valid is False
    assert state.stale_files == ("tracked.txt",)

    with pytest.raises(ValueError, match=r"stale_files=\['tracked.txt'\]"):
        validate_integration_files(tmp_path, Path("INTEGRATION_FILES.sha256"))


def test_seat_digest_report_lists_changed_manifest_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("current\n", encoding="utf-8")
    manifest = tmp_path / "tools/aionui-extension/INTEGRATION_FILES.sha256"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "48aa6cae8c70abdb28631d22b316e6d9f9d0768ec2911de7090e248b2afe6ca1"
        "  tracked.txt\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )
    tracked.write_text("changed\n", encoding="utf-8")

    assert changed_paths_since(tmp_path, "HEAD") == ("tracked.txt",)
    print_seat_digest_report(tmp_path, "HEAD")

    output = capsys.readouterr().out
    assert "SEAT SUITE REPORT (NOT A RELEASE/CI GATE)" in output
    assert "integration_digest_status=stale" in output
    assert "integration_digest_stale_files=['tracked.txt']" in output
    assert "integration_base_ref=HEAD" in output
    assert "integration_listed_files_changed=['tracked.txt']" in output
    assert "release_gate_command=python3 tools/ci_manifest.py run" in output


def test_seat_suite_report_runs_every_suite_and_keeps_digest_test_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suites = (
        Suite("release-tools", "tools/tests"),
        Suite("next", "packages/next/tests"),
    )
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0)

    monkeypatch.setattr(ci_manifest.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="release-tools .* exit=1"):
        run_seat_suites(tmp_path, suites=suites)

    assert len(calls) == 2
    assert calls[0][-2:] == [
        "-k",
        "not test_integration_files_manifest_matches_the_tree "
        "and not test_real_tree_is_clean_and_current_bump_has_zero_diff",
    ]
    assert "-k" not in calls[1]


def test_seat_suite_report_cleans_tmpdir_and_avoids_default_pytest_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout"
    suite_path = root / "packages/example/tests"
    suite_path.mkdir(parents=True)
    (suite_path / "test_pass.py").write_text(
        "def test_pass():\n    assert True\n", encoding="utf-8"
    )
    configured_tmp = tmp_path / "seat-tmp"
    configured_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(configured_tmp))

    run_seat_suites(
        root,
        suites=(Suite("example", "packages/example/tests"),),
    )

    assert list(configured_tmp.iterdir()) == []
    assert not any(tmp_path.rglob("pytest-of-*"))


def test_central_suite_covers_board_move_regression() -> None:
    central = next(suite for suite in SUITES if suite.name == "central")
    assert central.path == "packages/central/tests"
    assert (REPOSITORY_ROOT / central.path / "test_board_move.py").is_file()


def test_manifest_declares_cross_package_acceptance_coverage() -> None:
    coverage = covering_suites(
        [
            "packages/client/src/pursers_client/__init__.py",
            "docs/design-home/example.md",
        ]
    )

    assert coverage["packages/client/src/pursers_client/__init__.py"] == (
        "client",
        "aionui-extension",
    )
    assert coverage["docs/design-home/example.md"] == ()


def test_manifest_rejects_an_unlisted_test_directory(tmp_path: Path) -> None:
    listed = tmp_path / "packages" / "known" / "tests"
    listed.mkdir(parents=True)
    (listed / "test_known.py").write_text("def test_known(): pass\n")
    unlisted = tmp_path / "tools" / "new-tool" / "tests"
    unlisted.mkdir(parents=True)
    (unlisted / "test_new.py").write_text("def test_new(): pass\n")

    with pytest.raises(ValueError, match="tools/new-tool/tests"):
        validate_manifest(
            tmp_path,
            suites=(Suite("known", "packages/known/tests"),),
        )


def test_manifest_ignores_non_pytest_test_directories(tmp_path: Path) -> None:
    listed = tmp_path / "packages" / "known" / "tests"
    listed.mkdir(parents=True)
    (listed / "test_known.py").write_text("def test_known(): pass\n")
    node_tests = tmp_path / "tools" / "dashboard-ui" / "tests"
    node_tests.mkdir(parents=True)
    (node_tests / "render.test.mjs").write_text("// covered by the Node runner\n")

    validate_manifest(
        tmp_path,
        suites=(Suite("known", "packages/known/tests"),),
    )


def test_manifest_rejects_duplicate_paths(tmp_path: Path) -> None:
    path = tmp_path / "tools" / "same" / "tests"
    path.mkdir(parents=True)
    (path / "test_same.py").write_text("def test_same(): pass\n")

    with pytest.raises(ValueError, match="duplicate manifest entries"):
        validate_manifest(
            tmp_path,
            suites=(
                Suite("first", "tools/same/tests"),
                Suite("second", "tools/same/tests"),
            ),
        )


def test_parse_collected_count_ignores_summary_and_other_suites() -> None:
    output = """\
packages/central/tests/test_one.py::test_first
packages/central/tests/test_one.py::test_parameterized[value]
packages/client/tests/test_other.py::test_other
2 tests collected in 0.02s
"""
    assert parse_collected_count(output, "packages/central/tests") == 2


def test_parse_collected_count_accepts_pytest_rootdir_summary() -> None:
    output = """\
tests/test_one.py::test_first
tests/test_one.py::test_second

2 tests collected in 0.02s
"""
    assert parse_collected_count(output, "packages/central/tests") == 2


def test_pytest_target_is_relative_to_suite_working_directory() -> None:
    suite = Suite("import", "packages/import/tests", cwd="packages/import")
    assert pytest_target(suite) == "tests"


def test_suite_environment_prepends_checkout_package_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/existing/source")

    environment = suite_environment(tmp_path)

    assert environment["PYTHONPATH"].split(os.pathsep) == ["/existing/source"]

    for package in ("central", "client", "personal"):
        (tmp_path / "packages" / package / "src").mkdir(parents=True)
    environment = suite_environment(tmp_path)

    assert environment["PYTHONPATH"].split(os.pathsep) == [
        str(tmp_path / "packages/central/src"),
        str(tmp_path / "packages/client/src"),
        str(tmp_path / "packages/personal/src"),
        "/existing/source",
    ]


def test_default_job_count_uses_idle_cpu_capacity_with_a_sensible_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert default_job_count(0, 0.0) == 1
    assert default_job_count(1, 0.0) == 1
    assert default_job_count(3, 0.0) == 3
    assert default_job_count(128, 0.0) == 4
    assert default_job_count(10, 7.2) == 2
    assert default_job_count(10, 12.0) == 1
    monkeypatch.setattr(ci_manifest.os, "cpu_count", lambda: 10)
    monkeypatch.setattr(ci_manifest.os, "getloadavg", lambda: (8.1, 0.0, 0.0))
    assert default_job_count() == 1


def test_parallel_runner_buffers_output_in_manifest_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suites = (
        Suite("slow-first", "packages/first/tests"),
        Suite("fast-second", "packages/second/tests"),
    )

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        target = command[-1]
        if target == "packages/first/tests":
            time.sleep(0.04)
            return SimpleNamespace(returncode=0, stdout="first output\n")
        return SimpleNamespace(returncode=0, stdout="second output\n")

    monkeypatch.setattr(ci_manifest.subprocess, "run", run)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "runner-tmp"))

    run_suites(tmp_path, suites=suites, jobs=2)

    output = capsys.readouterr().out
    assert output.index("pytest slow-first") < output.index("first output")
    assert output.index("first output") < output.index("pytest fast-second")
    assert output.index("pytest fast-second") < output.index("second output")


def test_parallel_runner_starts_high_weight_suites_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suites = (
        Suite("low", "packages/low/tests", parallel_weight=1),
        Suite("high", "packages/high/tests", parallel_weight=100),
        Suite("medium", "packages/medium/tests", parallel_weight=50),
    )
    lock = threading.Lock()
    started: list[str] = []
    release = threading.Event()

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        with lock:
            started.append(command[-1])
            if len(started) == 2:
                release.set()
        assert release.wait(timeout=1)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(ci_manifest.subprocess, "run", run)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "runner-tmp"))

    run_suites(tmp_path, suites=suites, jobs=2)

    assert set(started[:2]) == {"packages/high/tests", "packages/medium/tests"}


def test_parallel_runner_aggregates_every_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suites = (
        Suite("first", "packages/first/tests"),
        Suite("second", "packages/second/tests"),
        Suite("third", "packages/third/tests"),
    )
    exits = {
        "packages/first/tests": 3,
        "packages/second/tests": 0,
        "packages/third/tests": 7,
    }

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=exits[command[-1]], stdout="")

    monkeypatch.setattr(ci_manifest.subprocess, "run", run)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "runner-tmp"))

    with pytest.raises(RuntimeError) as raised:
        run_suites(tmp_path, suites=suites, jobs=3)

    message = str(raised.value)
    assert "pytest failed for first (packages/first/tests) with exit code 3" in message
    assert "pytest failed for third (packages/third/tests) with exit code 7" in message
    assert "second" not in message


def test_jobs_one_runs_sequentially_in_manifest_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    suites = (
        Suite("first", "packages/first/tests"),
        Suite("second", "packages/second/tests"),
    )
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    targets: list[str] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            targets.append(command[-1])
        time.sleep(0.01)
        with lock:
            active -= 1
        return SimpleNamespace(returncode=0, stdout="ok\n")

    monkeypatch.setattr(ci_manifest.subprocess, "run", run)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "runner-tmp"))

    run_suites(tmp_path, suites=suites, jobs=1)

    assert targets == ["packages/first/tests", "packages/second/tests"]
    assert maximum_active == 1
    assert capsys.readouterr().out.endswith(
        "ci manifest: all 2 required pytest suites passed\n"
    )


def test_parallel_runner_isolates_each_suite_tmp_and_cache_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suites = (
        Suite("first", "packages/first/tests"),
        Suite("second", "packages/second/tests"),
    )
    environments: list[dict[str, str]] = []
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        environments.append(environment)
        commands.append(command)
        for name in (
            "TMPDIR",
            "XDG_CACHE_HOME",
            "PIP_CACHE_DIR",
            "UV_CACHE_DIR",
            "npm_config_cache",
        ):
            assert Path(environment[name]).is_dir()
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(ci_manifest.subprocess, "run", run)
    configured_tmp = tmp_path / "owned-tmp"
    monkeypatch.setenv("TMPDIR", str(configured_tmp))

    run_suites(tmp_path, suites=suites, jobs=2)

    assert len(environments) == 2
    for name in (
        "TMPDIR",
        "XDG_CACHE_HOME",
        "PIP_CACHE_DIR",
        "UV_CACHE_DIR",
        "npm_config_cache",
    ):
        assert environments[0][name] != environments[1][name]
        assert Path(environments[0][name]).is_relative_to(configured_tmp)
        assert Path(environments[1][name]).is_relative_to(configured_tmp)
    assert all("--basetemp" in command for command in commands)
    assert all("cache_dir=" in " ".join(command) for command in commands)


def test_runner_sweeps_dead_scratch_but_preserves_concurrent_live_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_tmp = tmp_path / "runner-tmp"
    configured_tmp.mkdir()
    live = configured_tmp / f"{ci_manifest.SCRATCH_PREFIX}live"
    dead = configured_tmp / f"{ci_manifest.SCRATCH_PREFIX}dead"
    live.mkdir()
    dead.mkdir()
    live_owner = (live / ci_manifest.SCRATCH_OWNER_FILE).open(
        "w+", encoding="utf-8"
    )
    live_owner.write('{"pid": 1, "schema": 1}\n')
    live_owner.flush()
    dead.joinpath(ci_manifest.SCRATCH_OWNER_FILE).write_text(
        '{"pid": 1, "schema": 1}\n', encoding="utf-8"
    )
    fcntl.flock(live_owner.fileno(), fcntl.LOCK_EX)

    def run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        assert live.is_dir()
        assert not dead.exists()
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(ci_manifest.subprocess, "run", run)
    monkeypatch.setenv("TMPDIR", str(configured_tmp))
    try:
        run_suites(
            tmp_path,
            suites=(Suite("only", "packages/only/tests"),),
            jobs=1,
        )
        assert live.is_dir()
        assert not dead.exists()
    finally:
        fcntl.flock(live_owner.fileno(), fcntl.LOCK_UN)
        live_owner.close()
        ci_manifest.shutil.rmtree(live)

    assert list(configured_tmp.iterdir()) == []


@pytest.mark.parametrize("command", [["run", "--jobs", "1"], ["seat-suite-report"]])
def test_cli_sweeps_dead_scratch_before_low_space_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    configured_tmp = tmp_path / "runner-tmp"
    configured_tmp.mkdir()
    dead = configured_tmp / f"{ci_manifest.SCRATCH_PREFIX}dead"
    dead.mkdir()
    dead.joinpath(ci_manifest.SCRATCH_OWNER_FILE).write_text(
        '{"pid": 1, "schema": 1}\n', encoding="utf-8"
    )
    monkeypatch.setenv("TMPDIR", str(configured_tmp))
    monkeypatch.setenv(ci_manifest.MIN_FREE_BYTES_ENV, "1")
    monkeypatch.setattr(ci_manifest, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(
        ci_manifest.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=0),
    )

    assert ci_manifest.main(command) == 1
    assert not dead.exists()


def test_seat_suite_report_contains_nested_pytest_without_default_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout"
    suite_path = root / "packages/example/tests"
    nested_path = root / "nested-tests"
    suite_path.mkdir(parents=True)
    nested_path.mkdir()
    nested_path.joinpath("test_pass.py").write_text(
        "def test_pass():\n    assert True\n", encoding="utf-8"
    )
    suite_path.joinpath("test_nested.py").write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n\n"
        "def test_nested(tmp_path):\n"
        "    result = subprocess.run(\n"
        "        [sys.executable, '-m', 'pytest', '-q', 'nested-tests',\n"
        "         '--basetemp', str(tmp_path / 'nested-tmp'), '-o',\n"
        "         f\"cache_dir={tmp_path / 'nested-cache'}\"],\n"
        "        cwd=Path(__file__).resolve().parents[3], check=False)\n"
        "    assert result.returncode == 0\n",
        encoding="utf-8",
    )
    configured_tmp = tmp_path / "seat-tmp"
    configured_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(configured_tmp))

    run_seat_suites(root, suites=(Suite("example", "packages/example/tests"),))

    assert list(configured_tmp.iterdir()) == []
    assert not any(tmp_path.rglob("pytest-of-*"))


def test_verify_counts_requires_every_suite_to_be_positive() -> None:
    suites = (
        Suite("first", "packages/first/tests"),
        Suite("second", "tools/second/tests"),
    )
    payload = {
        "schema": 1,
        "suites": [
            {"name": "first", "path": "packages/first/tests", "collected": 1},
            {"name": "second", "path": "tools/second/tests", "collected": 0},
        ],
    }

    with pytest.raises(ValueError, match="non_positive"):
        verify_counts(payload, suites=suites)


def test_verify_counts_rejects_a_missing_suite() -> None:
    suites = (
        Suite("first", "packages/first/tests"),
        Suite("second", "tools/second/tests"),
    )
    payload = {
        "schema": 1,
        "suites": [
            {"name": "first", "path": "packages/first/tests", "collected": 1}
        ],
    }

    with pytest.raises(ValueError, match="second"):
        verify_counts(payload, suites=suites)


def test_fleet_dashboard_suite_passes_with_read_only_home(
    tmp_path: Path,
) -> None:
    read_only_home = tmp_path / "empty-home"
    read_only_home.mkdir()
    read_only_home.chmod(stat.S_IRUSR | stat.S_IXUSR)
    environment = os.environ.copy()
    environment["HOME"] = str(read_only_home)
    environment["PURSERS_STATE_DIR"] = str(tmp_path / "state")
    nested_basetemp = tmp_path / "fleet-pytest-tmp"
    nested_cache = tmp_path / "fleet-pytest-cache"
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--basetemp",
                str(nested_basetemp),
                "-o",
                f"cache_dir={nested_cache}",
                "tools/fleet-dashboard/tests",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        read_only_home.chmod(stat.S_IRWXU)

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output[-4_000:]
    # Count-agnostic green proof: a pytest exit of 0 already rejects failures
    # and empty collection, so require only a positive passed tally instead of
    # pinning the suite size.
    passed = re.search(r"(\d+) passed", output)
    assert passed is not None and int(passed.group(1)) > 0, output[-2_000:]
