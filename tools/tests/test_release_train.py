from __future__ import annotations

import shutil
from pathlib import Path

from tools import release_train
from tools.release_versions import load_versions


ROOT = Path(__file__).resolve().parents[2]


def _fixture_repository(tmp_path: Path) -> Path:
    paths = {
        "CHANGELOG.md",
        "tools/release_versions.toml",
        "tools/regenerate_component_lock.py",
        "packages/personal/src/pursers_personal/resources/component-lock.json",
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
            "product=5.0.0a17",
            "central=0.1.0a21",
            "client=0.1.0a15",
            "import=5.0.0a4",
            "wait_bridge=0.1.0a7",
        ),
        None,
    )

    planned = release_train.plan_bump(root, current, target)

    assert "5.0.0a17" in planned[root / "packages/pursers/pyproject.toml"]
    assert "pursers-central==0.1.0a21" in planned[
        root / "packages/personal/pyproject.toml"
    ]
    assert "pursers-client==0.1.0a15" in planned[
        root / "tools/wait-bridge/pyproject.toml"
    ]
    assert "pursers-client==0.1.0a15" in planned[
        root / "packages/central/pyproject.toml"
    ]
    assert 'SOURCE_VERSION = "0.1.0a7"' in planned[
        root / "tools/wait-bridge/pursers_wait_server.py"
    ]
    assert "## [5.0.0a17] - " in planned[root / "CHANGELOG.md"]
    assert "5.0.0a17" not in (root / "packages/pursers/pyproject.toml").read_text()


def test_next_patch_alpha_advances_every_component() -> None:
    current = load_versions(ROOT / "tools/release_versions.toml")
    target = release_train.bumped_versions(current, (), "patch-alpha")
    assert target.product == "5.0.0a17"
    assert target.packages == {
        "pursers": "5.0.0a17",
        "central": "0.1.0a21",
        "client": "0.1.0a16",
        "personal": "5.0.0a17",
        "import": "5.0.0a4",
        "wait_bridge": "0.1.0a7",
    }


def test_check_detects_fixture_dependency_drift(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    manifest = load_versions(root / "tools/release_versions.toml")
    pyproject = root / "tools/wait-bridge/pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            "pursers-client==0.1.0a15",
            "pursers-client==0.1.0a14",
        )
    )

    errors = release_train.check(root, manifest)

    assert any("missing pursers-client==0.1.0a15" in error for error in errors)


def test_check_detects_central_client_dependency_drift(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    manifest = load_versions(root / "tools/release_versions.toml")
    pyproject = root / "packages/central/pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            "pursers-client==0.1.0a15",
            "pursers-client==0.1.0a14",
        )
    )

    errors = release_train.check(root, manifest)

    assert any(
        "packages/central/pyproject.toml: missing pursers-client==0.1.0a15" in error
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
            'SOURCE_VERSION = "0.1.0a6"', 'SOURCE_VERSION = "0.1.0a5"'
        )
    )

    errors = release_train.check(root, manifest)

    assert any("SOURCE_VERSION '0.1.0a5' != '0.1.0a6'" in error for error in errors)
