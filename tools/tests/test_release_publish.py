from __future__ import annotations

import os
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
    assert "ref: refs/tags/${{ steps.release.outputs.tag }}" in workflow
    assert 'release_publish.py "$TAG" verify-checkout' in workflow
    assert 'release_publish.py "$TAG" verify-asset' in workflow


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_checkout_verifier_rejects_same_name_branch_tag_collision(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release-test@example.invalid")
    tracked = repository / "tracked.txt"
    tracked.write_text("tag commit\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-qm", "tag commit")
    tag_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "tag", "v5.0.0b1")
    _git(repository, "switch", "-qc", "v5.0.0b1")
    tracked.write_text("branch commit\n", encoding="utf-8")
    _git(repository, "commit", "-qam", "same-name branch commit")

    rejected = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/release_publish.py"),
            "v5.0.0b1",
            "verify-checkout",
            "--repository",
            str(repository),
        ],
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "release checkout mismatch" in rejected.stderr

    _git(repository, "checkout", "-q", "--detach", "refs/tags/v5.0.0b1")
    accepted = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/release_publish.py"),
            "v5.0.0b1",
            "verify-checkout",
            "--repository",
            str(repository),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert accepted.stdout.strip() == tag_commit


def test_existing_release_asset_must_match_rebuilt_bytes(tmp_path: Path) -> None:
    local = tmp_path / "local" / "asset.whl"
    existing = tmp_path / "existing" / "asset.whl"
    local.parent.mkdir()
    existing.parent.mkdir()
    local.write_bytes(b"rebuilt")
    existing.write_bytes(b"stale")
    command = [
        sys.executable,
        str(ROOT / "tools/release_publish.py"),
        "v5.0.0b1",
        "verify-asset",
        "--local",
        str(local),
        "--existing",
        str(existing),
    ]

    rejected = subprocess.run(command, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "existing release asset mismatch" in rejected.stderr

    existing.write_bytes(local.read_bytes())
    accepted = subprocess.run(command, check=True, capture_output=True, text=True)
    assert len(accepted.stdout.strip()) == 64


def test_release_handoff_uses_supported_run_and_latest_queries() -> None:
    handoff = (ROOT / "docs/release-train.md").read_text(encoding="utf-8")
    assert "gh run list --repo swisspra/Pursers --workflow release.yml" in handoff
    assert '--event push --branch "$TAG" --commit "$CANDIDATE"' in handoff
    assert 'gh run watch "$RUN_ID" --repo swisspra/Pursers --exit-status' in handoff
    assert "gh run watch --repo swisspra/Pursers --workflow" not in handoff
    assert "isLatest" not in handoff
    assert "--json isPrerelease --jq '.isPrerelease'" in handoff
    assert "gh api repos/swisspra/Pursers/releases/latest" in handoff
    assert "--jq '.tag_name'" in handoff

    section = handoff.split("## GitHub prerelease handoff", 1)[1]
    command_block = (
        section.split("```sh\n", 1)[1]
        .split("\n```", 1)[0]
        .replace("APPROVED_FULL_40_HEX_SHA", "a" * 40)
    )
    assert command_block.startswith("set -euo pipefail\n")
    assert 'test -n "$RUN_ID" && test "$RUN_ID" != null' in command_block
    subprocess.run(["bash", "-n"], input=command_block, text=True, check=True)


@pytest.mark.parametrize(
    ("failure", "forbidden_call"),
    [
        ("candidate", "git tag"),
        ("run-id", "gh run watch"),
        ("run-watch", "gh release view"),
        ("prerelease", "gh api"),
        ("latest", "gh release download"),
        ("checksum", None),
    ],
)
def test_release_handoff_stops_after_failed_gate(
    tmp_path: Path, failure: str, forbidden_call: str | None
) -> None:
    handoff = (ROOT / "docs/release-train.md").read_text(encoding="utf-8")
    command_block = (
        handoff.split("## GitHub prerelease handoff", 1)[1]
        .split("```sh\n", 1)[1]
        .split("\n```", 1)[0]
        .replace("APPROVED_FULL_40_HEX_SHA", "a" * 40)
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    driver = fake_bin / "driver"
    driver.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
failure = os.environ["RELEASE_TEST_FAILURE"]
candidate = "a" * 40
with Path(os.environ["RELEASE_TEST_LOG"]).open("a", encoding="utf-8") as stream:
    stream.write(" ".join((name, *args)) + "\\n")

if name == "git":
    if args[0] == "rev-parse":
        print("b" * 40 if failure == "candidate" else candidate)
elif name == "gh":
    if args[:2] == ["run", "list"]:
        print("null" if failure == "run-id" else "123")
    elif args[:2] == ["run", "watch"]:
        raise SystemExit(1 if failure == "run-watch" else 0)
    elif args[:2] == ["release", "view"]:
        if "--jq" in args:
            print("false" if failure == "prerelease" else "true")
        else:
            print("{}")
    elif args[0] == "api":
        print("v5.0.0b1" if failure == "latest" else "v5.0.0a26")
    elif args[:2] == ["release", "download"]:
        output = Path("dist-release")
        output.mkdir(exist_ok=True)
        (output / "SHA256SUMS.txt").write_text("fixture\\n", encoding="utf-8")
elif name == "shasum" and failure == "checksum":
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    driver.chmod(0o755)
    for name in ("git", "gh", "sleep", "shasum"):
        (fake_bin / name).symlink_to(driver)

    log = tmp_path / "calls.log"
    complete = tmp_path / "complete"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "RELEASE_TEST_FAILURE": failure,
            "RELEASE_TEST_LOG": str(log),
        }
    )
    result = subprocess.run(
        ["bash"],
        input=f'{command_block}\nprintf done > "{complete}"\n',
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not complete.exists()
    calls = log.read_text(encoding="utf-8")
    if forbidden_call is not None:
        assert forbidden_call not in calls


def test_current_beta_whats_new_versions_match_release_manifest() -> None:
    document = (ROOT / "docs-local/whats-new.html").read_text(encoding="utf-8")
    current_release = document.split('<section id="unreleased">', 1)[1].split(
        '<h3>5.0.0a24', 1
    )[0]
    for expected in ("0.1.0a30", "0.1.0a23", "0.1.0a16", "2026-09-11"):
        assert expected in current_release
    for stale in ("0.1.0a29", "0.1.0a22", "0.1.0a15", "2026-09-08"):
        assert stale not in current_release
