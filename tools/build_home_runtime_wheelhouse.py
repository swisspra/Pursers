#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]
CLIENT_PROJECT = ROOT / "packages" / "client"
BRIDGE_PROJECT = ROOT / "tools" / "wait-bridge"
LIFECYCLE_COMMANDS = ("ticket-lifecycle", "seat-lifecycle", "team-lifecycle")


class WheelhouseError(RuntimeError):
    pass


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


def _python_details(python: Path) -> dict[str, str]:
    result = _run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import json, platform, sys; "
                "print(json.dumps({'executable': sys.executable, "
                "'version': platform.python_version(), 'platform': platform.platform()}))"
            ),
        ]
    )
    details = json.loads(result.stdout)
    if not str(details["version"]).startswith("3.12."):
        raise WheelhouseError("--python must select Python 3.12")
    return details


def _source_commit(allow_dirty: bool) -> tuple[str, bool]:
    commit = _run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip()
    dirty = bool(_run(["git", "status", "--porcelain"], cwd=ROOT).stdout.strip())
    if dirty and not allow_dirty:
        raise WheelhouseError("source checkout is dirty; commit exact source before building")
    return commit, dirty


def build(output: Path, python: Path, allow_dirty: bool = False) -> dict[str, object]:
    if not output.is_absolute():
        raise WheelhouseError("--output must be an absolute path")
    if output.exists() or output.is_symlink():
        raise WheelhouseError("--output must not already exist")
    if not output.parent.is_dir():
        raise WheelhouseError("--output parent must be an existing directory")
    python = python.resolve(strict=True)
    python_details = _python_details(python)
    commit, dirty = _source_commit(allow_dirty)
    uv = shutil.which("uv")
    if uv is None:
        raise WheelhouseError("uv is required")
    client_name, client_version = _project(CLIENT_PROJECT)
    bridge_name, bridge_version = _project(BRIDGE_PROJECT)

    with tempfile.TemporaryDirectory(prefix=".home-wheelhouse-", dir=output.parent) as raw_temp:
        temp = Path(raw_temp)
        dist = temp / "source-wheels"
        wheelhouse = temp / "wheelhouse"
        resolver = temp / "resolver"
        verifier = temp / "verifier"
        dist.mkdir()
        wheelhouse.mkdir()
        wheelhouse.chmod(0o700)
        build_environment = {**os.environ, "UV_PYTHON": str(python)}
        for project in (CLIENT_PROJECT, BRIDGE_PROJECT):
            _run(
                [uv, "build", "--wheel", "--out-dir", str(dist), str(project)],
                cwd=ROOT,
                env=build_environment,
            )
        source_wheels = (
            _single_wheel(dist, client_name, client_version),
            _single_wheel(dist, bridge_name, bridge_version),
        )

        _run([str(python), "-m", "venv", str(resolver)])
        resolver_python = resolver / "bin" / "python"
        _run(
            [
                str(resolver_python),
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--dest",
                str(wheelhouse),
                "--only-binary=:all:",
                *(str(wheel) for wheel in source_wheels),
            ]
        )
        for source_wheel in source_wheels:
            copied_wheel = wheelhouse / source_wheel.name
            if not copied_wheel.is_file() or _sha256(copied_wheel) != _sha256(source_wheel):
                raise WheelhouseError(f"resolver did not preserve exact source wheel {source_wheel.name}")

        _run([str(python), "-m", "venv", str(verifier)])
        verifier_python = verifier / "bin" / "python"
        _run(
            [
                str(verifier_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                f"{bridge_name}=={bridge_version}",
            ]
        )
        bridge = verifier / "bin" / "pursers-wait-bridge"
        clean_environment = os.environ.copy()
        clean_environment.pop("PYTHONHOME", None)
        clean_environment.pop("PYTHONPATH", None)
        command_tails: dict[str, str] = {}
        for command in LIFECYCLE_COMMANDS:
            result = _run(
                [str(bridge), command, "--help"],
                cwd=temp,
                env=clean_environment,
            )
            command_tails[command] = result.stdout.strip().splitlines()[-1]

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
        manifest: dict[str, object] = {
            "schema": 1,
            "source": {"commit": commit, "dirty": dirty},
            "python": python_details,
            "builder_platform": platform.platform(),
            "requirements": {
                client_name: client_version,
                bridge_name: bridge_version,
            },
            "verification": {
                "install": "pip install --no-index --find-links WHEELHOUSE "
                f"{bridge_name}=={bridge_version}",
                "lifecycle_command_tails": command_tails,
            },
            "artifacts": artifacts,
        }
        manifest_path = wheelhouse / "wheelhouse.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        checksums.chmod(0o600)
        manifest_path.chmod(0o600)
        os.replace(wheelhouse, output)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and verify a complete offline Pursers home runtime wheelhouse."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        manifest = build(args.output, args.python, args.allow_dirty)
    except (OSError, WheelhouseError) as error:
        parser.error(str(error))
    print(json.dumps({"ok": True, "output": str(args.output), "manifest": manifest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
