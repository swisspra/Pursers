#!/usr/bin/env python3
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSIONS = ROOT / "tools" / "release_versions.toml"
LOCK_PATH = ROOT / "tools" / "home_runtime_wheelhouse.lock"
CLIENT_PROJECT = ROOT / "packages" / "client"
BRIDGE_PROJECT = ROOT / "tools" / "wait-bridge"
LOCAL_PROJECTS = (CLIENT_PROJECT, BRIDGE_PROJECT)
LOCK_LINE = re.compile(
    r"^([A-Za-z0-9_.-]+)==([^\s\\]+)\s+--hash=sha256:([0-9a-f]{64})$"
)


class WheelhouseError(RuntimeError):
    pass


def _release_source_date_epoch() -> str:
    with RELEASE_VERSIONS.open("rb") as stream:
        value = tomllib.load(stream).get("source_date_epoch")
    if not isinstance(value, str) or not value.isdigit():
        raise WheelhouseError("release manifest source_date_epoch is invalid")
    return value


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            **kwargs,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "command failed").strip()
        raise WheelhouseError(f"{command[0]} failed: {detail}") from error


def _project(project: Path) -> tuple[str, str]:
    with (project / "pyproject.toml").open("rb") as stream:
        metadata = tomllib.load(stream)["project"]
    return str(metadata["name"]), str(metadata["version"])


def _source_requirements() -> tuple[str, ...]:
    rows: list[str] = []
    for project in LOCAL_PROJECTS:
        with (project / "pyproject.toml").open("rb") as stream:
            metadata = tomllib.load(stream)["project"]
        name = str(metadata["name"])
        dependencies = metadata.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise WheelhouseError(f"{name} dependencies are invalid")
        rows.extend(f"{name}:{item}" for item in dependencies)
    return tuple(sorted(rows))


def _source_requirements_sha256() -> str:
    payload = "\n".join(_source_requirements()) + "\n"
    return hashlib.sha256(payload.encode()).hexdigest()


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalized_wheel_prefix(name: str, version: str) -> str:
    return f"{name.replace('-', '_')}-{version}-"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _single_wheel(directory: Path, name: str, version: str) -> Path:
    matches = sorted(directory.glob(f"{_normalized_wheel_prefix(name, version)}*.whl"))
    if len(matches) != 1:
        raise WheelhouseError(
            f"expected one {name}=={version} wheel, found {len(matches)}"
        )
    return matches[0]


def _verify_wheel_members(wheel: Path, required: tuple[str, ...]) -> None:
    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())
    missing = sorted(set(required) - members)
    if missing:
        raise WheelhouseError(f"{wheel.name} is missing package members: {', '.join(missing)}")


def _wheel_identity(wheel: Path) -> tuple[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_paths = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise WheelhouseError(f"{wheel.name} has invalid distribution metadata")
        metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise WheelhouseError(f"{wheel.name} is missing Name or Version metadata")
    return _normalized_name(name), version


def _python_details(python: Path) -> dict[str, str]:
    result = _run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import json, platform, sys, sysconfig; "
                "print(json.dumps({'executable': sys.executable, "
                "'version': platform.python_version(), "
                "'platform': platform.platform(), "
                "'platform_tag': sysconfig.get_platform()}))"
            ),
        ]
    )
    details = json.loads(result.stdout)
    if not str(details["version"]).startswith("3.12."):
        raise WheelhouseError("--python must select Python 3.12")
    return {key: str(value) for key, value in details.items()}


def _source_commit(allow_dirty: bool) -> tuple[str, bool]:
    commit = _run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip()
    dirty = bool(_run(["git", "status", "--porcelain"], cwd=ROOT).stdout.strip())
    if dirty and not allow_dirty:
        raise WheelhouseError("source checkout is dirty; commit exact source before building")
    return commit, dirty


def _build_environment(python: Path) -> dict[str, str]:
    environment = {
        **os.environ,
        "PYTHONHASHSEED": "0",
        "SOURCE_DATE_EPOCH": _release_source_date_epoch(),
        "UV_PYTHON": str(python),
    }
    environment.pop("PYTHONPATH", None)
    environment.pop("PIP_FIND_LINKS", None)
    environment.pop("UV_FIND_LINKS", None)
    return environment


def _pip_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PIP_FIND_LINKS", None)
    environment.pop("UV_FIND_LINKS", None)
    return environment


def _lock_headers(lock: Path) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_line in lock.read_text().splitlines():
        if not raw_line.startswith("# ") or ": " not in raw_line:
            continue
        key, value = raw_line[2:].split(": ", 1)
        headers[key] = value
    return headers


def _validate_lock(lock: Path, python_details: dict[str, str]) -> tuple[tuple[str, str, str], ...]:
    if not lock.is_file():
        raise WheelhouseError(f"lock file does not exist: {lock}")
    headers = _lock_headers(lock)
    expected_requirements = _source_requirements_sha256()
    if headers.get("schema") != "1":
        raise WheelhouseError("wheelhouse lock schema is invalid; run --refresh-lock")
    if headers.get("source-requirements-sha256") != expected_requirements:
        raise WheelhouseError(
            "wheelhouse lock is stale for current source requirements; run --refresh-lock"
        )
    python_minor = ".".join(python_details["version"].split(".")[:2])
    if headers.get("python") != python_minor:
        raise WheelhouseError(
            f"wheelhouse lock targets Python {headers.get('python')}, not {python_minor}; "
            "run --refresh-lock"
        )
    if headers.get("platform") != python_details["platform_tag"]:
        raise WheelhouseError(
            f"wheelhouse lock targets {headers.get('platform')}, not "
            f"{python_details['platform_tag']}; run --refresh-lock"
        )

    entries: list[tuple[str, str, str]] = []
    names: set[str] = set()
    for raw_line in lock.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = LOCK_LINE.fullmatch(line)
        if match is None:
            raise WheelhouseError(
                "wheelhouse lock entries must use name==version --hash=sha256:<digest>"
            )
        name, version, digest = match.groups()
        normalized = _normalized_name(name)
        if normalized in names:
            raise WheelhouseError(f"wheelhouse lock contains duplicate package {normalized}")
        names.add(normalized)
        entries.append((normalized, version, digest))
    if not entries:
        raise WheelhouseError("wheelhouse lock contains no packages")
    return tuple(entries)


def _render_lock(wheels: list[Path], python_details: dict[str, str]) -> str:
    entries: dict[str, tuple[str, str]] = {}
    for wheel in wheels:
        name, version = _wheel_identity(wheel)
        if name in entries:
            raise WheelhouseError(f"resolver produced duplicate package {name}")
        entries[name] = (version, _sha256(wheel))
    if not entries:
        raise WheelhouseError("resolver produced no third-party wheels")
    python_minor = ".".join(python_details["version"].split(".")[:2])
    lines = [
        "# Pursers Home runtime wheelhouse lock. Regenerate deliberately; do not hand-edit.",
        "# schema: 1",
        f"# python: {python_minor}",
        f"# platform: {python_details['platform_tag']}",
        f"# source-requirements-sha256: {_source_requirements_sha256()}",
        "",
    ]
    lines.extend(
        f"{name}=={version} --hash=sha256:{digest}"
        for name, (version, digest) in sorted(entries.items())
    )
    return "\n".join(lines) + "\n"


def _build_source_wheels(
    temp: Path,
    python: Path,
    uv: str,
    build_environment: dict[str, str],
) -> tuple[Path, Path]:
    dist = temp / "source-wheels"
    projects = temp / "projects"
    dist.mkdir()
    projects.mkdir()
    staged_projects: list[Path] = []
    for project in LOCAL_PROJECTS:
        staged_project = projects / project.name
        shutil.copytree(
            project,
            staged_project,
            ignore=shutil.ignore_patterns(
                "*.egg-info", ".pytest_cache", "__pycache__", "build", "dist"
            ),
        )
        staged_projects.append(staged_project)
    for project in staged_projects:
        _run(
            [uv, "build", "--wheel", "--out-dir", str(dist), str(project)],
            cwd=ROOT,
            env=build_environment,
        )
    client_name, client_version = _project(CLIENT_PROJECT)
    bridge_name, bridge_version = _project(BRIDGE_PROJECT)
    source_wheels = (
        _single_wheel(dist, client_name, client_version),
        _single_wheel(dist, bridge_name, bridge_version),
    )
    _verify_wheel_members(source_wheels[0], ("pursers_client/__init__.py",))
    _verify_wheel_members(
        source_wheels[1],
        (
            "pursers_wait_server.py",
            "door_state.py",
        ),
    )
    return source_wheels


def _populate_locked_wheels(
    wheelhouse: Path,
    resolver: Path,
    python: Path,
    lock: Path,
    source_wheels: tuple[Path, Path],
    environment: dict[str, str],
) -> None:
    _run([str(python), "-m", "venv", str(resolver)], env=environment)
    resolver_python = resolver / "bin" / "python"
    _run(
        [
            str(resolver_python),
            "-m",
            "pip",
            "--isolated",
            "download",
            "--disable-pip-version-check",
            "--retries",
            "10",
            "--timeout",
            "30",
            "--dest",
            str(wheelhouse),
            "--only-binary=:all:",
            "--no-deps",
            "--require-hashes",
            "--requirement",
            str(lock),
        ],
        env=environment,
    )
    for source_wheel in source_wheels:
        shutil.copy2(source_wheel, wheelhouse / source_wheel.name)


def _verify_install(
    temp: Path,
    wheelhouse: Path,
    python: Path,
    lock: Path,
    source_wheels: tuple[Path, Path],
    environment: dict[str, str],
) -> dict[str, object]:
    verifier = temp / "verifier"
    _run([str(python), "-m", "venv", str(verifier)], env=environment)
    verifier_python = verifier / "bin" / "python"
    _run(
        [
            str(verifier_python),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--no-deps",
            "--require-hashes",
            "--requirement",
            str(lock),
        ],
        env=environment,
    )
    exact_source_wheels = tuple(wheelhouse / wheel.name for wheel in source_wheels)
    _run(
        [
            str(verifier_python),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            *(str(wheel) for wheel in exact_source_wheels),
        ],
        env=environment,
    )
    _run([str(verifier_python), "-m", "pip", "check"], env=environment)
    _run(
        [str(verifier_python), "-I", "-c", "import pursers_client, pursers_wait_server"],
        cwd=temp,
        env=environment,
    )
    return {
        "install": [
            "pip --isolated install --no-index --find-links WHEELHOUSE "
            "--no-deps --require-hashes --requirement LOCK",
            "pip --isolated install --no-index --no-deps SOURCE_WHEELS",
            "pip check",
        ],
        "imports": ["pursers_client", "pursers_wait_server"],
    }


def _write_metadata(
    wheelhouse: Path,
    *,
    commit: str,
    dirty: bool,
    python_details: dict[str, str],
    lock: Path,
    verification: dict[str, object],
) -> dict[str, object]:
    wheels = sorted(wheelhouse.glob("*.whl"), key=lambda path: path.name)
    if not wheels:
        raise WheelhouseError("resolver produced no wheels")
    artifacts = [
        {"filename": wheel.name, "sha256": _sha256(wheel), "size": wheel.stat().st_size}
        for wheel in wheels
    ]
    for wheel in wheels:
        wheel.chmod(0o600)
    checksums = wheelhouse / "SHA256SUMS"
    checksums.write_text(
        "".join(f"{artifact['sha256']}  {artifact['filename']}\n" for artifact in artifacts)
    )
    client_name, client_version = _project(CLIENT_PROJECT)
    bridge_name, bridge_version = _project(BRIDGE_PROJECT)
    manifest: dict[str, object] = {
        "schema": 2,
        "source": {"commit": commit, "dirty": dirty},
        "python": python_details,
        "builder_platform": platform.platform(),
        "lock": {
            "path": lock.relative_to(ROOT).as_posix() if lock.is_relative_to(ROOT) else str(lock),
            "sha256": _sha256(lock),
            "source_requirements_sha256": _source_requirements_sha256(),
        },
        "requirements": {client_name: client_version, bridge_name: bridge_version},
        "verification": verification,
        "artifacts": artifacts,
    }
    manifest_path = wheelhouse / "wheelhouse.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    checksums.chmod(0o600)
    manifest_path.chmod(0o600)
    return manifest


def build(
    output: Path,
    python: Path,
    lock: Path = LOCK_PATH,
    allow_dirty: bool = False,
) -> dict[str, object]:
    if not output.is_absolute():
        raise WheelhouseError("--output must be an absolute path")
    if output.exists() or output.is_symlink():
        raise WheelhouseError("--output must not already exist")
    if not output.parent.is_dir():
        raise WheelhouseError("--output parent must be an existing directory")
    python = python.resolve(strict=True)
    lock = lock.resolve(strict=True)
    python_details = _python_details(python)
    _validate_lock(lock, python_details)
    commit, dirty = _source_commit(allow_dirty)
    uv = shutil.which("uv")
    if uv is None:
        raise WheelhouseError("uv is required")

    with tempfile.TemporaryDirectory(prefix=".home-wheelhouse-", dir=output.parent) as raw_temp:
        temp = Path(raw_temp)
        wheelhouse = temp / "wheelhouse"
        wheelhouse.mkdir()
        wheelhouse.chmod(0o700)
        source_wheels = _build_source_wheels(
            temp, python, uv, _build_environment(python)
        )
        environment = _pip_environment()
        _populate_locked_wheels(
            wheelhouse,
            temp / "resolver",
            python,
            lock,
            source_wheels,
            environment,
        )
        verification = _verify_install(
            temp, wheelhouse, python, lock, source_wheels, environment
        )
        manifest = _write_metadata(
            wheelhouse,
            commit=commit,
            dirty=dirty,
            python_details=python_details,
            lock=lock,
            verification=verification,
        )
        os.replace(wheelhouse, output)
    return manifest


def refresh_lock(lock: Path, python: Path) -> dict[str, object]:
    if not lock.is_absolute():
        raise WheelhouseError("--lock must be an absolute path")
    if not lock.parent.is_dir():
        raise WheelhouseError("--lock parent must be an existing directory")
    python = python.resolve(strict=True)
    python_details = _python_details(python)
    uv = shutil.which("uv")
    if uv is None:
        raise WheelhouseError("uv is required")
    with tempfile.TemporaryDirectory(prefix=".home-wheelhouse-lock-") as raw_temp:
        temp = Path(raw_temp)
        source_wheels = _build_source_wheels(
            temp, python, uv, _build_environment(python)
        )
        resolved = temp / "resolved"
        resolver = temp / "resolver"
        resolved.mkdir()
        environment = _pip_environment()
        _run([str(python), "-m", "venv", str(resolver)], env=environment)
        resolver_python = resolver / "bin" / "python"
        _run(
            [
                str(resolver_python),
                "-m",
                "pip",
                "--isolated",
                "download",
                "--disable-pip-version-check",
                "--retries",
                "10",
                "--timeout",
                "30",
                "--dest",
                str(resolved),
                "--only-binary=:all:",
                *(str(wheel) for wheel in source_wheels),
            ],
            env=environment,
        )
        source_names = {wheel.name for wheel in source_wheels}
        for source_wheel in source_wheels:
            copied = resolved / source_wheel.name
            if not copied.is_file() or _sha256(copied) != _sha256(source_wheel):
                raise WheelhouseError(
                    f"resolver did not preserve exact source wheel {source_wheel.name}"
                )
        dependency_wheels = [
            wheel for wheel in sorted(resolved.glob("*.whl")) if wheel.name not in source_names
        ]
        rendered = _render_lock(dependency_wheels, python_details)
        temporary_lock = lock.with_name(f".{lock.name}.tmp")
        temporary_lock.write_text(rendered)
        os.replace(temporary_lock, lock)
    entries = _validate_lock(lock, python_details)
    return {
        "path": str(lock),
        "sha256": _sha256(lock),
        "packages": len(entries),
        "source_requirements_sha256": _source_requirements_sha256(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and verify a locked offline Pursers home runtime wheelhouse."
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--refresh-lock", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    lock = args.lock.expanduser().absolute()
    try:
        if args.refresh_lock:
            if args.output is not None:
                raise WheelhouseError("--output cannot be used with --refresh-lock")
            result = {"ok": True, "lock": refresh_lock(lock, args.python)}
        else:
            if args.output is None:
                raise WheelhouseError("--output is required unless --refresh-lock is used")
            manifest = build(args.output, args.python, lock, args.allow_dirty)
            result = {"ok": True, "output": str(args.output), "manifest": manifest}
    except (OSError, WheelhouseError) as error:
        parser.error(str(error))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
