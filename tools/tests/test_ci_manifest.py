from __future__ import annotations

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
