from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

from ci_manifest import (  # noqa: E402
    SUITES,
    Suite,
    parse_collected_count,
    pytest_target,
    suite_environment,
    validate_manifest,
    verify_counts,
)


def test_manifest_covers_every_test_directory() -> None:
    validate_manifest(REPOSITORY_ROOT)


def test_central_suite_covers_board_move_regression() -> None:
    central = next(suite for suite in SUITES if suite.name == "central")
    assert central.path == "packages/central/tests"
    assert (REPOSITORY_ROOT / central.path / "test_board_move.py").is_file()


def test_manifest_rejects_an_unlisted_test_directory(tmp_path: Path) -> None:
    listed = tmp_path / "packages" / "known" / "tests"
    listed.mkdir(parents=True)
    unlisted = tmp_path / "tools" / "new-tool" / "tests"
    unlisted.mkdir(parents=True)

    with pytest.raises(ValueError, match="tools/new-tool/tests"):
        validate_manifest(
            tmp_path,
            suites=(Suite("known", "packages/known/tests"),),
        )


def test_manifest_rejects_duplicate_paths(tmp_path: Path) -> None:
    path = tmp_path / "tools" / "same" / "tests"
    path.mkdir(parents=True)

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
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
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
