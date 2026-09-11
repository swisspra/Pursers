from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tools.release_versions import (
    VERSIONS,
    expected_wheel_filenames,
    github_release_flags,
    release_version_from_tag,
)


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("existing", [False, True])
def test_beta_release_is_prerelease_and_never_latest(existing: bool) -> None:
    assert github_release_flags("v5.0.0b1", existing=existing) == (
        "--prerelease",
        "--latest=false",
    )


def test_stable_create_and_edit_preserve_stable_latest_behavior() -> None:
    stable = replace(VERSIONS, product="5.0.0")
    assert github_release_flags("v5.0.0", existing=False, versions=stable) == (
        "--latest",
    )
    assert github_release_flags("v5.0.0", existing=True, versions=stable) == (
        "--prerelease=false",
        "--latest",
    )


@pytest.mark.parametrize(
    "tag",
    ["5.0.0b1", "v5.0.0-beta.1", "v5.0.0b2", "vnot-a-version"],
)
def test_release_tag_must_be_canonical_and_match_manifest(tag: str) -> None:
    with pytest.raises(ValueError):
        release_version_from_tag(tag)


def test_six_wheel_cohort_matches_manifest_versions() -> None:
    assert set(expected_wheel_filenames()) == {
        "pursers-5.0.0b1-py3-none-any.whl",
        "pursers_central-0.1.0a30-py3-none-any.whl",
        "pursers_client-0.1.0a23-py3-none-any.whl",
        "pursers_personal-5.0.0b1-py3-none-any.whl",
        "pursers_personal_import-5.0.0a3-py3-none-any.whl",
        "pursers_wait_bridge-0.1.0a16-py3-none-any.whl",
    }


@pytest.mark.parametrize("mode", ["create", "edit"])
def test_release_publish_cli_emits_prerelease_flags_for_both_paths(mode: str) -> None:
    result = subprocess.run(
        [sys.executable, "tools/release_publish.py", "v5.0.0b1", mode],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == ["--prerelease", "--latest=false"]


def test_release_workflow_wires_both_paths_and_manifest_wheel_check() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert 'release_publish.py "$TAG" create' in workflow
    assert 'release_publish.py "$TAG" edit' in workflow
    assert "release_version_from_tag" in workflow
    assert "expected_wheel_filenames" in workflow
    assert "--prerelease=false" not in workflow
