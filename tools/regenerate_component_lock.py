#!/usr/bin/env python3
"""Build the four Pursers wheels and regenerate the Personal component lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


SOURCE_DATE_EPOCH = "315532800"
PRODUCT_VERSION = "5.0.0a7"
EXPECTED_VIEW_SHA256 = (
    "46e4eb01e18fa7e285c7ed4dc7600c81086d30fb83e168874d61abaafdc6c243"
)
EXPECTED_VIEW_SIZE = 396499
PROJECTS = (
    ("central", "pursers-central", "0.1.0a13"),
    ("client", "pursers-client", "0.1.0a11"),
    ("import", "pursers-personal-import", "5.0.0a2"),
    ("personal", "pursers-personal", PRODUCT_VERSION),
)
LOCKED_COMPONENTS = ("pursers-central", "pursers-client")
BUILD_TOOLCHAIN = (
    ("build", "1.3.0"),
    ("setuptools", "80.9.0"),
    ("wheel", "0.45.1"),
    ("packaging", "25.0"),
    ("pyproject-hooks", "1.2.0"),
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _clean_build_state(project_dir: Path) -> None:
    build_dir = project_dir / "build"
    if build_dir.is_dir():
        shutil.rmtree(build_dir)
    for parent in (project_dir, project_dir / "src"):
        if not parent.is_dir():
            continue
        for path in parent.glob("*.egg-info"):
            if path.is_dir():
                shutil.rmtree(path)


def _create_build_environment(root: Path) -> Path:
    """Create and verify the private, fully pinned wheel-build environment."""
    venv_dir = root / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
    )
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    requirements = [f"{name}=={version}" for name, version in BUILD_TOOLCHAIN]
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            *requirements,
        ],
        check=True,
    )
    verification = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import importlib.metadata as m, json; "
                f"names={json.dumps([name for name, _ in BUILD_TOOLCHAIN])}; "
                "print(json.dumps({name: m.version(name) for name in names}, "
                "sort_keys=True))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    installed = json.loads(verification.stdout)
    expected = {name: version for name, version in BUILD_TOOLCHAIN}
    if installed != expected:
        raise RuntimeError(
            f"build toolchain verification failed: {installed!r} != {expected!r}"
        )
    return python


def _build_wheel(
    build_python: Path,
    repository: Path,
    wheel_dir: Path,
    project: str,
    distribution: str,
    version: str,
) -> Path:
    project_dir = repository / "packages" / project
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
        }
    )
    _clean_build_state(project_dir)
    try:
        subprocess.run(
            [
                str(build_python),
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(wheel_dir),
                str(project_dir),
            ],
            cwd=repository,
            env=environment,
            check=True,
        )
    finally:
        _clean_build_state(project_dir)

    normalized = distribution.replace("-", "_")
    matches = sorted(wheel_dir.glob(f"{normalized}-{version}-*.whl"))
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {distribution}=={version} wheel, found {len(matches)}"
        )
    return matches[0]


def _locked_members(wheel: Path) -> dict[str, str]:
    members: dict[str, str] = {}
    with zipfile.ZipFile(wheel) as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if info.is_dir() or info.filename.endswith(".dist-info/RECORD"):
                continue
            if info.filename in members:
                raise RuntimeError(f"duplicate wheel member: {info.filename}")
            members[info.filename] = _sha256(archive.read(info))
    if not members:
        raise RuntimeError(f"wheel has no lockable members: {wheel.name}")
    return members


def _write_lock(repository: Path, wheels: dict[str, Path]) -> Path:
    view_path = (
        repository
        / "packages/personal/src/pursers_personal/resources/dashboard.html"
    )
    view = view_path.read_bytes()
    view_sha256 = _sha256(view)
    if view_sha256 != EXPECTED_VIEW_SHA256 or len(view) != EXPECTED_VIEW_SIZE:
        raise RuntimeError("dashboard bundle changed; refusing to regenerate the lock")

    versions = {distribution: version for _, distribution, version in PROJECTS}
    components = {
        distribution: {
            "version": versions[distribution],
            "wheel_sha256": _sha256(wheels[distribution].read_bytes()),
            "members": _locked_members(wheels[distribution]),
        }
        for distribution in LOCKED_COMPONENTS
    }
    document = {
        "schema_version": 1,
        "product_version": PRODUCT_VERSION,
        "build_toolchain": {
            name: version for name, version in BUILD_TOOLCHAIN
        },
        "components": components,
        "view": {
            "resource": "pursers_personal/resources/dashboard.html",
            "size_bytes": len(view),
            "sha256": view_sha256,
        },
    }
    lock_path = (
        repository
        / "packages/personal/src/pursers_personal/resources/component-lock.json"
    )
    temporary = lock_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(lock_path)
    return lock_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build Pursers wheels and regenerate component-lock.json"
    )
    parser.add_argument(
        "--wheel-dir",
        required=True,
        type=Path,
        help="empty directory that will receive the four rebuilt wheels",
    )
    args = parser.parse_args()

    repository = Path(__file__).resolve().parents[1]
    wheel_dir = args.wheel_dir.resolve()
    wheel_dir.mkdir(parents=True, exist_ok=True)
    if any(wheel_dir.iterdir()):
        parser.error(f"wheel directory must be empty: {wheel_dir}")

    wheels: dict[str, Path] = {}
    with tempfile.TemporaryDirectory(
        prefix="pursers-build-toolchain-", dir=wheel_dir.parent
    ) as toolchain_root:
        build_python = _create_build_environment(Path(toolchain_root))
        for project, distribution, version in PROJECTS[:2]:
            wheels[distribution] = _build_wheel(
                build_python,
                repository,
                wheel_dir,
                project,
                distribution,
                version,
            )
        lock_path = _write_lock(repository, wheels)
        for project, distribution, version in PROJECTS[2:]:
            wheels[distribution] = _build_wheel(
                build_python,
                repository,
                wheel_dir,
                project,
                distribution,
                version,
            )

    summary = {
        "lock": str(lock_path.relative_to(repository)),
        "build_toolchain": {
            name: version for name, version in BUILD_TOOLCHAIN
        },
        "wheels": {
            distribution: {
                "file": wheel.name,
                "sha256": _sha256(wheel.read_bytes()),
            }
            for distribution, wheel in wheels.items()
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
