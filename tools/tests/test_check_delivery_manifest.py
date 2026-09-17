from __future__ import annotations

from pathlib import Path

import pytest

from tools import check_delivery_manifest


ROOT = Path(__file__).resolve().parents[2]
RED_FIXTURE = Path(__file__).parent / "fixtures/delivery_manifest_red"


def _base_repository(tmp_path: Path) -> Path:
    versions = tmp_path / "tools/release_versions.toml"
    versions.parent.mkdir(parents=True)
    versions.write_text(
        'schema_version = 1\nproduct = "5.0.0b2"\n[packages]\n',
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("main: <code>5.0.0b2\n", encoding="utf-8")
    (tmp_path / "delivery-manifest.toml").write_text(
        """schema_version = 1
artifacts = []

[[version_surfaces]]
path = "README.md"
version_key = "product"
prefix = "main: <code>"
""",
        encoding="utf-8",
    )
    return tmp_path


def test_repository_delivery_manifest_is_current() -> None:
    assert check_delivery_manifest.validate(ROOT) == []


def test_red_fixture_proves_unregistered_artifact_and_b1_version_fail() -> None:
    failures = check_delivery_manifest.validate(RED_FIXTURE)

    assert any("python-distribution:smuggled-artifact" in item for item in failures)
    assert any("self-described version '5.0.0b1'" in item for item in failures)


@pytest.mark.parametrize(
    "kind", ["distribution", "page", "tool", "console-script"]
)
def test_unregistered_artifact_fails(tmp_path: Path, kind: str) -> None:
    root = _base_repository(tmp_path)
    if kind == "distribution":
        artifact = root / "packages/new/pyproject.toml"
        artifact.parent.mkdir(parents=True)
        artifact.write_text(
            '[project]\nname = "new-artifact"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
    elif kind == "page":
        artifact = root / "website/new.html"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("<!doctype html><title>new</title>\n", encoding="utf-8")
    elif kind == "tool":
        artifact = root / "tools/new_tool.py"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            "def run() -> None:\n    pass\n",
            encoding="utf-8",
        )
    else:
        artifact = root / "tools/example/audit_export.py"
        artifact.parent.mkdir(parents=True)
        artifact.write_text(
            "def main() -> None:\n    pass\n",
            encoding="utf-8",
        )
        (artifact.parent / "pyproject.toml").write_text(
            """[project]
name = "example-tools"
version = "1.0.0"

[project.scripts]
pursers-audit-export = "audit_export:main"
""",
            encoding="utf-8",
        )

    failures = check_delivery_manifest.validate(root)

    assert any("unregistered artifact:" in failure for failure in failures)
    if kind == "console-script":
        assert any(
            "python-console-script:example-tools:pursers-audit-export" in failure
            and "tools/example/audit_export.py" in failure
            for failure in failures
        )


def test_console_script_target_must_resolve_to_source(tmp_path: Path) -> None:
    root = _base_repository(tmp_path)
    project = root / "tools/example/pyproject.toml"
    project.parent.mkdir(parents=True)
    project.write_text(
        """[project]
name = "example-tools"
version = "1.0.0"

[project.scripts]
pursers-missing = "missing_module:main"
""",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match="cannot resolve project.scripts.pursers-missing"
    ):
        check_delivery_manifest.validate(root)


def test_distribution_outside_conventional_roots_cannot_be_smuggled(
    tmp_path: Path,
) -> None:
    root = _base_repository(tmp_path)
    project = root / "services/audit-export/pyproject.toml"
    project.parent.mkdir(parents=True)
    project.write_text(
        """[project]
name = "pursers-audit-export"
version = "1.0.0"

[project.scripts]
pursers-audit-export = "audit_export:main"
""",
        encoding="utf-8",
    )
    (project.parent / "audit_export.py").write_text(
        "def main() -> None:\n    pass\n",
        encoding="utf-8",
    )

    failures = check_delivery_manifest.validate(root)

    assert any(
        "unregistered artifact: python-distribution:pursers-audit-export "
        "(services/audit-export/pyproject.toml)" in failure
        for failure in failures
    )
    assert any(
        "unregistered artifact: "
        "python-console-script:pursers-audit-export:pursers-audit-export "
        "(services/audit-export/audit_export.py)" in failure
        for failure in failures
    )


def test_web_surface_outside_conventional_roots_cannot_be_smuggled(
    tmp_path: Path,
) -> None:
    root = _base_repository(tmp_path)
    page = root / "services/ops-console/status.html"
    page.parent.mkdir(parents=True)
    page.write_text("<!doctype html><title>Operations</title>\n", encoding="utf-8")

    failures = check_delivery_manifest.validate(root)

    assert any(
        "unregistered artifact: web-surface:services/ops-console/status.html "
        "(services/ops-console/status.html)" in failure
        for failure in failures
    )


def test_operator_tool_outside_conventional_roots_cannot_be_smuggled(
    tmp_path: Path,
) -> None:
    root = _base_repository(tmp_path)
    tool = root / "services/ops-console/reconcile.py"
    tool.parent.mkdir(parents=True)
    tool.write_text(
        "def main() -> None:\n"
        "    pass\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)

    failures = check_delivery_manifest.validate(root)

    assert any(
        "unregistered artifact: operator-tool:services/ops-console/reconcile.py "
        "(services/ops-console/reconcile.py)" in failure
        for failure in failures
    )


def test_shell_operator_tool_outside_conventional_roots_cannot_be_smuggled(
    tmp_path: Path,
) -> None:
    root = _base_repository(tmp_path)
    tool = root / "services/ops-console/reconcile.sh"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)

    failures = check_delivery_manifest.validate(root)

    assert any(
        "unregistered artifact: operator-tool:services/ops-console/reconcile.sh "
        "(services/ops-console/reconcile.sh)" in failure
        for failure in failures
    )


@pytest.mark.parametrize(
    ("filename", "body", "artifact_id"),
    [
        (
            "package.json",
            '{"name": "ops-console"}\n',
            "node-application:ops-console",
        ),
        (
            "aion-extension.json",
            '{"name": "ops-console-extension"}\n',
            "host-extension:ops-console-extension",
        ),
    ],
)
def test_application_metadata_outside_conventional_roots_cannot_be_smuggled(
    tmp_path: Path,
    filename: str,
    body: str,
    artifact_id: str,
) -> None:
    root = _base_repository(tmp_path)
    metadata = root / "services/ops-console" / filename
    metadata.parent.mkdir(parents=True)
    metadata.write_text(body, encoding="utf-8")

    failures = check_delivery_manifest.validate(root)

    assert any(
        f"unregistered artifact: {artifact_id} "
        f"(services/ops-console/{filename})" in failure
        for failure in failures
    )


@pytest.mark.parametrize("ignored_root", ["build", "tests", "vendor"])
def test_generated_test_and_vendor_project_trees_stay_ignored(
    tmp_path: Path, ignored_root: str
) -> None:
    root = _base_repository(tmp_path)
    project = root / ignored_root / "nested/pyproject.toml"
    project.parent.mkdir(parents=True)
    project.write_text(
        '[project]\nname = "third-party-fixture"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )

    assert check_delivery_manifest.validate(root) == []


def test_stale_delivery_channel_fails(tmp_path: Path) -> None:
    root = _base_repository(tmp_path)
    project = root / "packages/alpha/pyproject.toml"
    project.parent.mkdir(parents=True)
    project.write_text(
        '[project]\nname = "alpha"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    (root / "delivery-manifest.toml").write_text(
        """schema_version = 1

[[artifacts]]
id = "python-distribution:alpha"
kind = "python-distribution"
source = "packages/alpha/pyproject.toml"
state = "delivered"
channel = "missing workflow"
evidence = [".github/workflows/missing.yml"]

[[version_surfaces]]
path = "README.md"
version_key = "product"
prefix = "main: <code>"
""",
        encoding="utf-8",
    )

    failures = check_delivery_manifest.validate(root)

    assert any("delivery channel is stale" in failure for failure in failures)


def test_draft_only_workflow_is_not_delivery_evidence(tmp_path: Path) -> None:
    root = _base_repository(tmp_path)
    page = root / "website/index.html"
    page.parent.mkdir()
    page.write_text("<!doctype html><title>page</title>\n", encoding="utf-8")
    draft = root / "website/deploy/pages.yml"
    draft.parent.mkdir()
    draft.write_text("# DRAFT ONLY. Copy this file manually.\n", encoding="utf-8")
    (root / "delivery-manifest.toml").write_text(
        """schema_version = 1

[[artifacts]]
id = "web-surface:website/index.html"
kind = "web-surface"
source = "website/index.html"
state = "delivered"
channel = "deployment workflow"
evidence = ["website/deploy/pages.yml"]

[[version_surfaces]]
path = "README.md"
version_key = "product"
prefix = "main: <code>"
""",
        encoding="utf-8",
    )

    failures = check_delivery_manifest.validate(root)

    assert any("draft-only evidence" in failure for failure in failures)


def test_current_b1_landing_page_version_fails(tmp_path: Path) -> None:
    root = _base_repository(tmp_path)
    website = root / "website/index.html"
    website.parent.mkdir()
    website.write_text(
        '<span class="ribbon">Beta v5.0.0b1</span>\n', encoding="utf-8"
    )
    (root / "delivery-manifest.toml").write_text(
        """schema_version = 1

[[artifacts]]
id = "web-surface:website/index.html"
kind = "web-surface"
source = "website/index.html"
state = "exempt"
reason = "The fixture intentionally has no delivery channel for this page."

[[version_surfaces]]
path = "website/index.html"
version_key = "product"
prefix = "Beta v"
reject_other_product_versions = true
""",
        encoding="utf-8",
    )

    failures = check_delivery_manifest.validate(root)

    assert (
        "website/index.html: self-described version '5.0.0b1' != "
        "product '5.0.0b2'"
    ) in failures
