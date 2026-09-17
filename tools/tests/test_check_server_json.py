from __future__ import annotations

import json
import shutil
from pathlib import Path

from tools import check_server_json


ROOT = Path(__file__).resolve().parents[2]


def _fixture_repository(tmp_path: Path) -> Path:
    for relative in (
        "server.json",
        "tools/release_versions.toml",
        *check_server_json.MARKER_READMES,
    ):
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return tmp_path


def test_repository_server_json_matches_release_versions() -> None:
    assert check_server_json.check(ROOT) == []


def test_check_rejects_top_level_and_package_version_drift(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    path = root / "server.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["version"] = "9.9.9"
    central = next(
        package
        for package in document["packages"]
        if package["identifier"] == "pursers-central"
    )
    central["version"] = "8.8.8"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    failures = check_server_json.check(root)

    assert "does not match product" in "\n".join(failures)
    assert "does not match central" in "\n".join(failures)


def test_check_rejects_missing_or_mismatched_markers(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    readme = root / "packages/central/README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(
            check_server_json.SERVER_NAME, "io.github.example/not-pursers"
        ),
        encoding="utf-8",
    )

    failures = check_server_json.check(root)

    assert any("packages/central/README.md" in failure for failure in failures)


def test_check_rejects_transport_and_environment_drift(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    path = root / "server.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    central = document["packages"][0]
    central["transport"] = {"type": "sse", "url": "http://127.0.0.1:8766/mcp"}
    central["environmentVariables"] = central["environmentVariables"][:-1]
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    failures = check_server_json.check(root)

    rendered = "\n".join(failures)
    assert "transport must be streamable-http" in rendered
    assert "transport URL must use https" in rendered
    assert "environment names must equal" in rendered
