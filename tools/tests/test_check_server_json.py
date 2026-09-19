from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from tools import check_server_json


ROOT = Path(__file__).resolve().parents[2]


def _fixture_repository(tmp_path: Path) -> Path:
    for relative in (
        "server.json",
        "tools/release_versions.toml",
        check_server_json.CENTRAL_PYPROJECT,
        check_server_json.CLIENT_PYPROJECT,
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
    client = next(
        package
        for package in document["packages"]
        if package["identifier"] == "pursers-client"
    )
    client["version"] = "7.7.7"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    failures = check_server_json.check(root)

    assert "does not match product" in "\n".join(failures)
    assert "does not match central" in "\n".join(failures)
    assert "does not match client" in "\n".join(failures)


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


def test_check_rejects_unpublished_central_readme(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    pyproject = root / check_server_json.CENTRAL_PYPROJECT
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('readme = "README.md"\n', ""),
        encoding="utf-8",
    )

    failures = check_server_json.check(root)

    assert any(
        "project.readme must equal 'README.md'" in failure for failure in failures
    )


def test_central_wheel_publishes_exactly_one_marker() -> None:
    assert check_server_json.build_and_check_central_wheel(ROOT) == []


def test_client_wheel_publishes_exactly_one_marker() -> None:
    assert check_server_json.build_and_check_client_wheel(ROOT) == []


def test_wheel_check_rejects_wrong_name_missing_and_duplicate_markers(
    tmp_path: Path,
) -> None:
    marker = f"<!-- mcp-name: {check_server_json.SERVER_NAME} -->"
    results: dict[int, list[str]] = {}
    for count in (0, 2):
        wheel = tmp_path / f"pursers_central-{count}-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                f"pursers_central_{count}.dist-info/METADATA",
                "Metadata-Version: 2.4\nName: wrong-central\n\n"
                + "\n".join([marker] * count),
            )
        results[count] = check_server_json.check_central_wheel_archive(wheel)

    assert "does not match server.json identifier 'pursers-central'" in "\n".join(
        results[0]
    )
    assert "found 0" in "\n".join(results[0])
    assert "found 2" in "\n".join(results[2])


def test_check_rejects_transport_and_environment_drift(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    path = root / "server.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    central = document["packages"][0]
    central["transport"] = {"type": "sse", "url": "http://127.0.0.1:9999/mcp"}
    central["environmentVariables"] = central["environmentVariables"][:-1]
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    failures = check_server_json.check(root)

    rendered = "\n".join(failures)
    assert "transport must be streamable-http" in rendered
    assert "transport URL must equal http://127.0.0.1:8766/mcp" in rendered
    assert "environment names must equal" in rendered


def test_check_rejects_boolean_legacy_tools_input(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    path = root / "server.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    environment = document["packages"][0]["environmentVariables"]
    legacy = next(
        item for item in environment if item["name"] == "PURSERS_LEGACY_TOOLS"
    )
    legacy["format"] = "boolean"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    failures = check_server_json.check(root)

    assert any(
        "PURSERS_LEGACY_TOOLS must be a string" in failure
        for failure in failures
    )


def test_check_rejects_https_transport_for_http_packaged_runtime(
    tmp_path: Path,
) -> None:
    root = _fixture_repository(tmp_path)
    path = root / "server.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["packages"][0]["transport"]["url"] = (
        "https://127.0.0.1:8766/mcp"
    )
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    failures = check_server_json.check(root)

    assert any("transport URL must equal" in failure for failure in failures)


def test_check_rejects_client_launch_contract_drift(tmp_path: Path) -> None:
    root = _fixture_repository(tmp_path)
    path = root / "server.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    client = next(
        package
        for package in document["packages"]
        if package["identifier"] == "pursers-client"
    )
    client["transport"] = {"type": "streamable-http", "url": "http://localhost"}
    client["runtimeArguments"][0]["value"] = "pursers-client==9.9.9"
    client["packageArguments"] = [
        item
        for item in client["packageArguments"]
        if item.get("name") != "--token-file"
    ]
    client["environmentVariables"] = [
        {"name": "PURSERS_TOKEN", "isRequired": True, "isSecret": True}
    ]
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    rendered = "\n".join(check_server_json.check(root))

    assert "transport must be exactly stdio" in rendered
    assert "runtimeArguments must pin --from" in rendered
    assert "named package arguments must equal" in rendered
    assert "credentials are supplied only through --token-file" in rendered
