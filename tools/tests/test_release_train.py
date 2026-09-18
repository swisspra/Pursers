from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tools import check_delivery_manifest, release_train
from tools.release_versions import load_versions


ROOT = Path(__file__).resolve().parents[2]


def _fixture_repository(tmp_path: Path) -> Path:
    paths = {
        "CHANGELOG.md",
        "tools/release_versions.toml",
        "tools/regenerate_component_lock.py",
        "packages/personal/src/pursers_personal/resources/component-lock.json",
        *release_train.COHORT_VERSION_FILES,
        *(
            path
            for paths in release_train.VERSION_FILES.values()
            for path in paths
        ),
    }
    for relative in paths:
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return tmp_path


def test_explicit_bump_rewrites_fixture_consumers_without_touching_disk(
    tmp_path: Path,
) -> None:
    root = _fixture_repository(tmp_path)
    current = load_versions(root / "tools/release_versions.toml")
    target = release_train.bumped_versions(
        current,
        (
            "product=5.0.0a27",
            "central=0.1.0a32",
            "client=0.1.0a25",
            "import=5.0.0a4",
            "wait_bridge=0.1.0a18",
            "acp=0.1.1",
        ),
        None,
    )

    planned = release_train.plan_bump(root, current, target)

    assert "5.0.0a27" in planned[root / "packages/pursers/pyproject.toml"]
    assert "pursers-central==0.1.0a32" in planned[
        root / "packages/personal/pyproject.toml"
    ]
    assert "pursers-client==0.1.0a25" in planned[
        root / "tools/wait-bridge/pyproject.toml"
    ]
    assert "pursers-client==0.1.0a25" in planned[
        root / "packages/central/pyproject.toml"
    ]
    assert 'SOURCE_VERSION = "0.1.0a18"' in planned[
        root / "tools/wait-bridge/pursers_wait_server.py"
    ]
    assert 'version = "0.1.1"' in planned[root / "tools/acp-agent/pyproject.toml"]
    assert '"pursers-client==0.1.0a25"' in planned[
        root / "tools/acp-agent/pyproject.toml"
    ]
    assert '"pursers-personal==5.0.0a27"' in planned[
        root / "tools/acp-agent/pyproject.toml"
    ]
    assert '"pursers-wait-bridge==0.1.0a18"' in planned[
        root / "tools/acp-agent/pyproject.toml"
    ]
    assert "pursers-acp==0.1.1" in planned[root / "tools/acp-agent/README.md"]
    assert 'IMPLEMENTATION_VERSION = "0.1.1"' in planned[
        root / "tools/acp-agent/src/pursers_acp/agent.py"
    ]
    assert "## [5.0.0a27] - " in planned[root / "CHANGELOG.md"]
    assert "5.0.0a27" not in (root / "packages/pursers/pyproject.toml").read_text()


def test_component_only_bump_rewrites_and_checks_every_cohort_document(
    tmp_path: Path,
) -> None:
    root = _fixture_repository(tmp_path)
    current = load_versions(root / "tools/release_versions.toml")
    next_central = release_train._alpha_next(current.packages["central"])
    target = release_train.bumped_versions(
        current,
        (f"central={next_central}",),
        None,
    )

    before = release_train.check(root, target)
    assert {
        f"{relative}: missing bound central version {next_central}"
        for relative in release_train.COHORT_VERSION_FILES
    } <= set(before)

    planned = release_train.plan_bump(root, current, target)
    for relative in release_train.COHORT_VERSION_FILES:
        path = root / relative
        assert next_central in planned[path]
        assert current.product in planned[path]
    for path, content in planned.items():
        path.write_text(content, encoding="utf-8")

    after = release_train.check(root, target)
    assert not {
        error
        for error in after
        if any(
            error.startswith(f"{relative}:")
            for relative in release_train.COHORT_VERSION_FILES
        )
    }


def test_acp_bump_updates_its_surfaces_without_rewriting_dependency_versions(
    tmp_path: Path,
) -> None:
    root = _fixture_repository(tmp_path)
    current = load_versions(root / "tools/release_versions.toml")
    target = release_train.bumped_versions(current, ("acp=0.1.1",), None)

    planned = release_train.plan_bump(root, current, target)

    pyproject = planned[root / "tools/acp-agent/pyproject.toml"]
    assert 'version = "0.1.1"' in pyproject
    assert '"pursers-client==0.1.0a24"' in pyproject
    assert '"pursers-wait-bridge==0.1.0a17"' in pyproject
    assert 'IMPLEMENTATION_VERSION = "0.1.1"' in planned[
        root / "tools/acp-agent/src/pursers_acp/agent.py"
    ]
    assert '"version": "0.1.1"' in planned[root / "tools/acp-agent/pursers/agent.json"]
    assert "pursers-acp==0.1.1" in planned[root / "tools/acp-agent/README.md"]


def test_acp_only_bump_preserves_delivery_manifest_validation(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    shutil.copytree(
        ROOT,
        root,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            "__pycache__",
            "*.pyc",
        ),
    )
    current = load_versions(root / "tools/release_versions.toml")
    target = release_train.bumped_versions(current, ("acp=0.1.1",), None)

    planned = release_train.plan_bump(root, current, target)
    for path, content in planned.items():
        path.write_text(content, encoding="utf-8")

    assert check_delivery_manifest.validate(root) == []


@pytest.mark.parametrize("relative", release_train.COHORT_VERSION_FILES)
def test_check_rejects_swapped_component_versions_even_when_both_remain_present(
    tmp_path: Path,
    relative: str,
) -> None:
    root = _fixture_repository(tmp_path)
    manifest = load_versions(root / "tools/release_versions.toml")
    path = root / relative
    current, history = release_train._version_reference_regions(
        relative, path.read_text(encoding="utf-8")
    )
    central = manifest.packages["central"]
    client = manifest.packages["client"]
    swapped = current.replace(central, "CENTRAL_VERSION_PLACEHOLDER")
    swapped = swapped.replace(client, central)
    swapped = swapped.replace("CENTRAL_VERSION_PLACEHOLDER", client)
    path.write_text(swapped + history, encoding="utf-8")

    errors = release_train.check(root, manifest)

    assert any(
        error.startswith(f"{relative}:")
        and "pursers-central reference" in error
        and f"!= central version {central}" in error
        for error in errors
    )
    assert any(
        error.startswith(f"{relative}:")
        and "pursers-client reference" in error
        and f"!= client version {client}" in error
        for error in errors
    )


def test_component_only_bump_preserves_whats_new_release_history(
    tmp_path: Path,
) -> None:
    root = _fixture_repository(tmp_path)
    current = load_versions(root / "tools/release_versions.toml")
    current_central = current.packages["central"]
    next_central = release_train._alpha_next(current_central)
    target = release_train.bumped_versions(
        current,
        (f"central={next_central}",),
        None,
    )
    path = root / "docs-local/whats-new.html"
    original_current, original_history = release_train._version_reference_regions(
        "docs-local/whats-new.html",
        path.read_text(encoding="utf-8"),
    )

    planned = release_train.plan_bump(root, current, target)[path]
    planned_current, planned_history = release_train._version_reference_regions(
        "docs-local/whats-new.html",
        planned,
    )

    assert current_central in original_current
    assert next_central in planned_current
    assert current_central not in planned_current
    assert planned_history == original_history
    assert current.product in planned_history


def test_wait_bridge_only_bump_uses_manifest_derived_cohort_keys(
    tmp_path: Path,
) -> None:
    root = _fixture_repository(tmp_path)
    current = load_versions(root / "tools/release_versions.toml")
    current_wait_bridge = current.packages["wait_bridge"]
    next_wait_bridge = release_train._alpha_next(current_wait_bridge)
    target = release_train.bumped_versions(
        current,
        (f"wait_bridge={next_wait_bridge}",),
        None,
    )

    planned = release_train.plan_bump(root, current, target)

    for relative in release_train.COHORT_VERSION_FILES:
        current_region, _history = release_train._version_reference_regions(
            relative,
            planned[root / relative],
        )
        assert next_wait_bridge in current_region
        assert current_wait_bridge not in current_region


def test_next_patch_alpha_advances_every_component() -> None:
    current = replace(
        load_versions(ROOT / "tools/release_versions.toml"),
        product="5.0.0a26",
        packages={
            "pursers": "5.0.0a26",
            "central": "0.1.0a30",
            "client": "0.1.0a23",
            "personal": "5.0.0a26",
            "import": "5.0.0a3",
            "wait_bridge": "0.1.0a16",
            "acp": "0.1.0a1",
        },
    )
    target = release_train.bumped_versions(current, (), "patch-alpha")
    assert target.product == "5.0.0a27"
    assert target.packages == {
        "pursers": "5.0.0a27",
        "central": "0.1.0a31",
        "client": "0.1.0a24",
        "personal": "5.0.0a27",
        "import": "5.0.0a4",
        "wait_bridge": "0.1.0a17",
        "acp": "0.1.0a2",
    }


def test_next_patch_alpha_rejects_beta_manifest() -> None:
    current = load_versions(ROOT / "tools/release_versions.toml")
    with pytest.raises(release_train.ReleaseTrainError, match="cannot apply patch-alpha"):
        release_train.bumped_versions(current, (), "patch-alpha")


def test_check_detects_fixture_dependency_drift(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    manifest = load_versions(root / "tools/release_versions.toml")
    pyproject = root / "tools/wait-bridge/pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            "pursers-client==0.1.0a24",
            "pursers-client==0.1.0a23",
        )
    )

    errors = release_train.check(root, manifest)

    assert any("missing pursers-client==0.1.0a24" in error for error in errors)


def test_check_detects_central_client_dependency_drift(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    manifest = load_versions(root / "tools/release_versions.toml")
    pyproject = root / "packages/central/pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            "pursers-client==0.1.0a24",
            "pursers-client==0.1.0a23",
        )
    )

    errors = release_train.check(root, manifest)

    assert any(
        "packages/central/pyproject.toml: missing pursers-client==0.1.0a24" in error
        for error in errors
    )


def test_real_tree_is_clean_and_current_bump_has_zero_diff() -> None:
    current = load_versions(ROOT / "tools/release_versions.toml")
    assert release_train.check(ROOT, current) == []
    assert release_train.plan_bump(ROOT, current, current) == {}


def test_check_detects_wait_bridge_source_constant_drift(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    manifest = load_versions(root / "tools/release_versions.toml")
    source = root / "tools/wait-bridge/pursers_wait_server.py"
    source.write_text(
        source.read_text().replace(
            'SOURCE_VERSION = "0.1.0a17"', 'SOURCE_VERSION = "0.1.0a16"'
        )
    )

    errors = release_train.check(root, manifest)

    assert any("SOURCE_VERSION '0.1.0a16' != '0.1.0a17'" in error for error in errors)


def test_component_source_lock_check_passes_fresh_and_fails_stale_lock(
    tmp_path: Path,
) -> None:
    source = tmp_path / "packages/client/src/pursers_client/client.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    member = "pursers_client/client.py"
    lock = {
        "components": {
            "pursers-client": {
                "members": {member: hashlib.sha256(source.read_bytes()).hexdigest()}
            }
        }
    }

    assert release_train._component_source_lock_errors(tmp_path, lock) == []

    source.write_text("VALUE = 2\n", encoding="utf-8")
    assert release_train._component_source_lock_errors(tmp_path, lock) == [
        "component-lock.json: pursers-client source digest mismatch: " + member
    ]
