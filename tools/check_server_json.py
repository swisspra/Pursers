"""Check that MCP Registry metadata follows the release version manifest."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Any, Sequence

try:
    from .release_versions import load_versions
except ImportError:  # Direct execution.
    from release_versions import load_versions


SERVER_NAME = "io.github.swisspra/pursers"
CENTRAL_PACKAGE_NAME = "pursers-central"
CLIENT_PACKAGE_NAME = "pursers-client"
SCHEMA_URL = (
    "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
)
PACKAGE_VERSION_KEYS = {
    CENTRAL_PACKAGE_NAME: "central",
    CLIENT_PACKAGE_NAME: "client",
}
MARKER_READMES = (
    "packages/pursers/README.md",
    "packages/central/README.md",
    "packages/client/README.md",
)
CENTRAL_PROJECT = "packages/central"
CENTRAL_PYPROJECT = f"{CENTRAL_PROJECT}/pyproject.toml"
CLIENT_PROJECT = "packages/client"
CLIENT_PYPROJECT = f"{CLIENT_PROJECT}/pyproject.toml"
CENTRAL_ENVIRONMENT = {
    "CENTRAL_ADMISSION",
    "CENTRAL_AUTH_MODE",
    "CENTRAL_JWKS_PATH",
    "CENTRAL_JWT_AUDIENCE",
    "CENTRAL_JWT_CLOCK_SKEW",
    "CENTRAL_JWT_ISSUER",
    "CENTRAL_PRINCIPAL_STREAM_CAP",
    "CENTRAL_REAPER_INTERVAL_S",
    "CENTRAL_REQUEST_STATE_KEY_FILE",
    "PURSERS_LEGACY_TOOLS",
    "STORE_BACKEND",
    "ONBOARD_CENTRAL_DATA_DIR",
    "ONBOARD_CENTRAL_ALLOWED_HOSTS",
    "ONBOARD_CENTRAL_HOST",
    "ONBOARD_CENTRAL_LOG_LEVEL",
    "ONBOARD_CENTRAL_PORT",
    "ONBOARD_CENTRAL_TLS_CERTFILE",
    "ONBOARD_CENTRAL_TLS_KEYFILE",
}
REQUIRED_CENTRAL_ENVIRONMENT = {
    "CENTRAL_JWKS_PATH",
    "CENTRAL_JWT_ISSUER",
}
CENTRAL_TRANSPORT_URL = "http://127.0.0.1:8766/mcp"
LEGACY_TOOLS_ENVIRONMENT = {
    "default": "0",
    "format": "string",
}
CLIENT_REQUIRED_ARGUMENTS = {
    "--central-url": {"format": "string", "isRequired": True},
    "--board": {"format": "string", "isRequired": True},
    "--token-file": {
        "format": "filepath",
        "isRequired": True,
        "isSecret": False,
    },
    "--ca-file": {
        "format": "filepath",
        "isRequired": False,
        "isSecret": False,
    },
    "--tools": {
        "choices": ["default", "worker", "reviewer", "all"],
        "default": "default",
        "isRequired": False,
    },
}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: cannot read server metadata: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{path}: server metadata must be a JSON object")
    return document


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: cannot read project metadata: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{path}: project metadata must be a TOML table")
    return document


def _ownership_marker() -> str:
    return f"<!-- mcp-name: {SERVER_NAME} -->"


def check_wheel_archive(wheel: Path, expected_package_name: str) -> list[str]:
    failures: list[str] = []
    try:
        with zipfile.ZipFile(wheel) as archive:
            metadata_members = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_members) != 1:
                return [
                    f"{wheel}: expected exactly one .dist-info/METADATA, "
                    f"found {len(metadata_members)}"
                ]
            metadata = archive.read(metadata_members[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        return [f"{wheel}: cannot inspect wheel metadata: {exc}"]

    package_name = Parser().parsestr(metadata).get("Name")
    if package_name != expected_package_name:
        failures.append(
            f"{wheel}: METADATA Name {package_name!r} does not match "
            f"server.json identifier {expected_package_name!r}"
        )

    marker = _ownership_marker()
    count = metadata.count(marker)
    if count != 1:
        failures.append(
            f"{wheel}: expected exactly one ownership marker {marker!r} "
            f"in wheel METADATA, found {count}"
        )
    return failures


def check_central_wheel_archive(wheel: Path) -> list[str]:
    return check_wheel_archive(wheel, CENTRAL_PACKAGE_NAME)


def check_client_wheel_archive(wheel: Path) -> list[str]:
    return check_wheel_archive(wheel, CLIENT_PACKAGE_NAME)


def build_and_check_wheel(
    repository: Path, project: str, package_name: str
) -> list[str]:
    uv = shutil.which("uv")
    if uv is None:
        return [f"{package_name} wheel: uv is required to build the artifact"]

    with tempfile.TemporaryDirectory(prefix=f"{package_name}-wheel-") as temporary:
        destination = Path(temporary)
        command = [
            uv,
            "build",
            "--wheel",
            "--out-dir",
            str(destination),
            str(repository / project),
        ]
        result = subprocess.run(
            command,
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            return [
                f"{package_name} wheel: build failed with exit code "
                f"{result.returncode}: {detail}"
            ]
        wheel_prefix = package_name.replace("-", "_")
        wheels = sorted(destination.glob(f"{wheel_prefix}-*.whl"))
        if len(wheels) != 1:
            return [
                f"{package_name} wheel: expected exactly one built artifact, "
                f"found {len(wheels)}"
            ]
        return check_wheel_archive(wheels[0], package_name)


def build_and_check_central_wheel(repository: Path) -> list[str]:
    return build_and_check_wheel(repository, CENTRAL_PROJECT, CENTRAL_PACKAGE_NAME)


def build_and_check_client_wheel(repository: Path) -> list[str]:
    return build_and_check_wheel(repository, CLIENT_PROJECT, CLIENT_PACKAGE_NAME)


def check(repository: Path) -> list[str]:
    repository = repository.resolve()
    document = _load_json(repository / "server.json")
    versions = load_versions(repository / "tools/release_versions.toml")
    failures: list[str] = []

    if document.get("$schema") != SCHEMA_URL:
        failures.append(f"server.json: $schema must equal {SCHEMA_URL}")
    if document.get("name") != SERVER_NAME:
        failures.append(f"server.json: name must equal {SERVER_NAME}")
    if document.get("version") != versions.product:
        failures.append(
            "server.json: version "
            f"{document.get('version')!r} does not match product {versions.product!r}"
        )

    packages = document.get("packages")
    if not isinstance(packages, list):
        failures.append("server.json: packages must be an array")
        packages = []
    by_identifier: dict[str, list[dict[str, Any]]] = {}
    for package in packages:
        if not isinstance(package, dict):
            failures.append("server.json: every package must be an object")
            continue
        identifier = package.get("identifier")
        if isinstance(identifier, str):
            by_identifier.setdefault(identifier, []).append(package)

    for identifier, release_key in PACKAGE_VERSION_KEYS.items():
        matches = by_identifier.get(identifier, [])
        if len(matches) != 1:
            failures.append(
                f"server.json: expected exactly one package {identifier!r}, "
                f"found {len(matches)}"
            )
            continue
        expected = versions.packages[release_key]
        actual = matches[0].get("version")
        if actual != expected:
            failures.append(
                f"server.json: package {identifier!r} version {actual!r} "
                f"does not match {release_key} {expected!r}"
            )

    central_matches = by_identifier.get(CENTRAL_PACKAGE_NAME, [])
    if len(central_matches) == 1:
        central = central_matches[0]
        if central.get("registryType") != "pypi":
            failures.append("server.json: pursers-central registryType must be pypi")
        transport = central.get("transport")
        if not isinstance(transport, dict):
            failures.append("server.json: pursers-central transport must be an object")
        else:
            if transport.get("type") != "streamable-http":
                failures.append(
                    "server.json: pursers-central transport must be streamable-http"
                )
            url = transport.get("url")
            if url != CENTRAL_TRANSPORT_URL:
                failures.append(
                    "server.json: pursers-central transport URL must equal "
                    f"{CENTRAL_TRANSPORT_URL}"
                )

        environment = central.get("environmentVariables")
        if not isinstance(environment, list):
            failures.append(
                "server.json: pursers-central environmentVariables must be an array"
            )
        else:
            names = [
                item.get("name")
                for item in environment
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
            if len(names) != len(environment) or set(names) != CENTRAL_ENVIRONMENT:
                failures.append(
                    "server.json: pursers-central environment names must equal "
                    f"{sorted(CENTRAL_ENVIRONMENT)!r}"
                )
            if len(names) != len(set(names)):
                failures.append(
                    "server.json: pursers-central environment names must be unique"
                )
            required = {
                item["name"]
                for item in environment
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item.get("isRequired") is True
            }
            if required != REQUIRED_CENTRAL_ENVIRONMENT:
                failures.append(
                    "server.json: required pursers-central environment names must "
                    f"equal {sorted(REQUIRED_CENTRAL_ENVIRONMENT)!r}"
                )
            legacy = next(
                (
                    item
                    for item in environment
                    if isinstance(item, dict)
                    and item.get("name") == "PURSERS_LEGACY_TOOLS"
                ),
                None,
            )
            if legacy is not None:
                actual = {
                    key: legacy.get(key) for key in LEGACY_TOOLS_ENVIRONMENT
                }
                if actual != LEGACY_TOOLS_ENVIRONMENT:
                    failures.append(
                        "server.json: PURSERS_LEGACY_TOOLS must be a string with "
                        "default '0'; Central enables it only for the exact string '1'"
                    )

    client_matches = by_identifier.get(CLIENT_PACKAGE_NAME, [])
    if len(client_matches) == 1:
        client = client_matches[0]
        if client.get("registryType") != "pypi":
            failures.append("server.json: pursers-client registryType must be pypi")
        if client.get("runtimeHint") != "uvx":
            failures.append("server.json: pursers-client runtimeHint must be uvx")
        transport = client.get("transport")
        if not isinstance(transport, dict) or transport != {"type": "stdio"}:
            failures.append(
                "server.json: pursers-client transport must be exactly stdio"
            )

        expected_from = f"{CLIENT_PACKAGE_NAME}=={versions.packages['client']}"
        runtime_arguments = client.get("runtimeArguments")
        expected_runtime_arguments = [
            {
                "type": "named",
                "name": "--from",
                "value": expected_from,
                "description": (
                    "Install the release-manifest-pinned client package before "
                    "running pursers-mcp"
                ),
            }
        ]
        if runtime_arguments != expected_runtime_arguments:
            failures.append(
                "server.json: pursers-client runtimeArguments must pin "
                f"--from {expected_from}"
            )

        package_arguments = client.get("packageArguments")
        if not isinstance(package_arguments, list):
            failures.append(
                "server.json: pursers-client packageArguments must be an array"
            )
        else:
            fixed_entrypoint = package_arguments[:1]
            if not fixed_entrypoint or not isinstance(fixed_entrypoint[0], dict) or {
                key: fixed_entrypoint[0].get(key)
                for key in ("type", "value")
            } != {"type": "positional", "value": "pursers-mcp"}:
                failures.append(
                    "server.json: pursers-client first package argument must be "
                    "the fixed pursers-mcp entrypoint"
                )
            named = {
                item.get("name"): item
                for item in package_arguments[1:]
                if isinstance(item, dict) and item.get("type") == "named"
            }
            if set(named) != set(CLIENT_REQUIRED_ARGUMENTS):
                failures.append(
                    "server.json: pursers-client named package arguments must equal "
                    f"{sorted(CLIENT_REQUIRED_ARGUMENTS)!r}"
                )
            else:
                for name, expected in CLIENT_REQUIRED_ARGUMENTS.items():
                    item = named[name]
                    actual = {
                        key: item.get(key, "string" if key == "format" else None)
                        for key in expected
                    }
                    if actual != expected:
                        failures.append(
                            f"server.json: pursers-client argument {name} must "
                            f"match {expected!r}"
                        )

        if client.get("environmentVariables") != []:
            failures.append(
                "server.json: pursers-client environmentVariables must be empty; "
                "credentials are supplied only through --token-file"
            )

    marker = _ownership_marker()
    for relative in MARKER_READMES:
        path = repository / relative
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            failures.append(f"{relative}: cannot read ownership marker: {exc}")
            continue
        if text.count(marker) != 1:
            failures.append(
                f"{relative}: expected exactly one ownership marker {marker!r}"
            )

    for pyproject, package_name in (
        (CENTRAL_PYPROJECT, CENTRAL_PACKAGE_NAME),
        (CLIENT_PYPROJECT, CLIENT_PACKAGE_NAME),
    ):
        project_metadata = _load_toml(repository / pyproject)
        project = project_metadata.get("project")
        readme = project.get("readme") if isinstance(project, dict) else None
        if readme != "README.md":
            failures.append(
                f"{pyproject}: project.readme must equal 'README.md' so the "
                f"ownership marker is published in {package_name} metadata"
            )

    return failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check server.json versions and PyPI ownership markers."
    )
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        failures = check(args.repository)
    except ValueError as exc:
        print(f"FAIL {exc}")
        return 1
    if not failures:
        failures.extend(build_and_check_central_wheel(args.repository.resolve()))
        failures.extend(build_and_check_client_wheel(args.repository.resolve()))
    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        return 1
    print(
        "PASS server.json versions, ownership markers, and pursers-central/"
        "pursers-client wheel metadata match release_versions.toml"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
