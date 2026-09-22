#!/usr/bin/env python3
"""Verify publish wheels against the packaged component lock and toolchain."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Sequence

from packaging.utils import canonicalize_name, parse_wheel_filename

if __package__:
    from tools.regenerate_component_lock import BUILD_TOOLCHAIN
else:
    from regenerate_component_lock import BUILD_TOOLCHAIN


class VerificationError(RuntimeError):
    """Raised when a wheel cannot be proven publish-safe."""


PYPI_INDEX_URL = "https://pypi.org"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _one_member(archive: zipfile.ZipFile, suffix: str, label: str) -> str:
    matches = [name for name in archive.namelist() if name.endswith(suffix)]
    if len(matches) != 1:
        raise VerificationError(
            f"{label} must contain exactly one {suffix!r} member; found {matches}"
        )
    return matches[0]


def _verify_generator(wheel: Path, expected_version: str) -> None:
    with zipfile.ZipFile(wheel) as archive:
        member = _one_member(archive, ".dist-info/WHEEL", wheel.name)
        content = archive.read(member).decode("utf-8")
    expected = f"Generator: setuptools ({expected_version})"
    if expected not in content.splitlines():
        generator = next(
            (line for line in content.splitlines() if line.startswith("Generator:")),
            "Generator: <missing>",
        )
        raise VerificationError(
            f"{wheel.name} wheel generator mismatch: {generator!r} != {expected!r}"
        )


def _component_lock(personal_wheel: Path) -> dict[str, Any]:
    with zipfile.ZipFile(personal_wheel) as archive:
        member = _one_member(
            archive,
            "pursers_personal/resources/component-lock.json",
            personal_wheel.name,
        )
        try:
            document = json.loads(archive.read(member))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VerificationError("personal wheel component lock is invalid") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise VerificationError("personal wheel component lock schema is unsupported")
    return document


def _matching_wheel(
    wheels: Sequence[Path], component: str, version: object
) -> Path:
    if not isinstance(version, str) or not version:
        raise VerificationError(f"{component} component lock has invalid version")
    prefix = f"{component.replace('-', '_')}-{version}-"
    matches = [wheel for wheel in wheels if wheel.name.startswith(prefix)]
    if len(matches) != 1:
        raise VerificationError(
            f"{component} expected exactly one built wheel with prefix {prefix!r}; "
            f"found {[wheel.name for wheel in matches]}"
        )
    return matches[0]


def verify_publish_wheels(
    wheel_dir: Path, *, check_component_lock: bool = True
) -> list[Path]:
    wheels = sorted(wheel_dir.glob("*.whl"))
    if not wheels:
        raise VerificationError(f"no wheels found in {wheel_dir}")

    toolchain = dict(BUILD_TOOLCHAIN)
    expected_setuptools = toolchain.get("setuptools")
    if not expected_setuptools:
        raise VerificationError("BUILD_TOOLCHAIN has no setuptools pin")
    for wheel in wheels:
        _verify_generator(wheel, expected_setuptools)
        print(f"generator_ok={wheel.name}:setuptools=={expected_setuptools}")

    if not check_component_lock:
        return wheels

    personal = [wheel for wheel in wheels if wheel.name.startswith("pursers_personal-")]
    if len(personal) != 1:
        raise VerificationError(
            "expected exactly one freshly built pursers-personal wheel; "
            f"found {[wheel.name for wheel in personal]}"
        )
    document = _component_lock(personal[0])
    components = document.get("components")
    if not isinstance(components, dict) or not components:
        raise VerificationError("personal wheel component lock has no components")
    locked_toolchain = document.get("build_toolchain")
    if locked_toolchain != toolchain:
        raise VerificationError(
            f"personal wheel build toolchain mismatch: {locked_toolchain!r} "
            f"!= {toolchain!r}"
        )

    for component, pin in sorted(components.items()):
        if not isinstance(component, str) or not isinstance(pin, dict):
            raise VerificationError("personal wheel component lock is malformed")
        wheel = _matching_wheel(wheels, component, pin.get("version"))
        expected = pin.get("wheel_sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            raise VerificationError(f"{component} component lock has invalid wheel_sha256")
        actual = _sha256(wheel)
        if actual != expected:
            raise VerificationError(
                f"{component} wheel sha256 mismatch: {actual} != {expected}"
            )
        print(f"component_sha256_ok={component}:{wheel.name}:{actual}")
    return wheels


def _pypi_release(
    distribution: str, version: str, *, index_url: str
) -> dict[str, Any] | None:
    project = urllib.parse.quote(distribution, safe="")
    release = urllib.parse.quote(version, safe="")
    request = urllib.request.Request(
        f"{index_url.rstrip('/')}/pypi/{project}/{release}/json",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            document = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise VerificationError(
            f"PyPI lookup failed for {distribution}=={version}: HTTP {exc.code}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise VerificationError(
            f"PyPI lookup failed for {distribution}=={version}: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise VerificationError(
            f"PyPI returned an invalid release document for {distribution}=={version}"
        )
    return document


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except OSError as exc:
        raise VerificationError(f"published wheel download failed: {url}: {exc}") from exc


def verify_pypi_artifacts(
    wheels: Sequence[Path], *, index_url: str = PYPI_INDEX_URL
) -> list[Path]:
    """Reject changed wheel bytes at a version that PyPI already contains."""
    verified: list[Path] = []
    for wheel in wheels:
        try:
            distribution, version, _build, _tags = parse_wheel_filename(wheel.name)
        except ValueError as exc:
            raise VerificationError(f"invalid wheel filename: {wheel.name}") from exc
        project = canonicalize_name(distribution)
        release = _pypi_release(project, str(version), index_url=index_url)
        if release is None:
            print(f"pypi_version_new={project}=={version}:{wheel.name}")
            verified.append(wheel)
            continue
        urls = release.get("urls")
        if not isinstance(urls, list):
            raise VerificationError(
                f"PyPI release document has no artifact list for {project}=={version}"
            )
        matches = [
            entry
            for entry in urls
            if isinstance(entry, dict)
            and entry.get("packagetype") == "bdist_wheel"
            and entry.get("filename") == wheel.name
        ]
        if len(matches) != 1:
            raise VerificationError(
                f"PyPI {project}=={version} must contain exactly one {wheel.name}; "
                f"found {len(matches)}"
            )
        entry = matches[0]
        url = entry.get("url")
        digests = entry.get("digests")
        published_sha256 = digests.get("sha256") if isinstance(digests, dict) else None
        if not isinstance(url, str) or not url or not isinstance(published_sha256, str):
            raise VerificationError(
                f"PyPI artifact metadata is incomplete for {project}=={version}"
            )
        published = _download(url)
        downloaded_sha256 = hashlib.sha256(published).hexdigest()
        if downloaded_sha256 != published_sha256:
            raise VerificationError(
                f"PyPI artifact digest mismatch for {wheel.name}: "
                f"{downloaded_sha256} != {published_sha256}"
            )
        local_sha256 = _sha256(wheel)
        if local_sha256 != published_sha256:
            raise VerificationError(
                f"published wheel content mismatch for {project}=={version}: "
                f"built {local_sha256} != PyPI {published_sha256}; bump the version"
            )
        print(f"pypi_artifact_match={project}=={version}:{wheel.name}:{local_sha256}")
        verified.append(wheel)
    return verified


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify freshly built wheels before Trusted Publishing"
    )
    parser.add_argument("--wheel-dir", required=True, type=Path)
    parser.add_argument(
        "--generators-only",
        action="store_true",
        help="verify the pinned setuptools generator without a Personal lock",
    )
    parser.add_argument(
        "--verify-pypi",
        action="store_true",
        help="fail when a built wheel differs from the same version on PyPI",
    )
    parser.add_argument(
        "--pypi-index-url",
        default=PYPI_INDEX_URL,
        help="base URL providing the PyPI JSON API",
    )
    args = parser.parse_args(argv)
    try:
        wheels = verify_publish_wheels(
            args.wheel_dir, check_component_lock=not args.generators_only
        )
        if args.verify_pypi:
            verify_pypi_artifacts(wheels, index_url=args.pypi_index_url)
    except (OSError, UnicodeError, zipfile.BadZipFile, VerificationError) as exc:
        print(f"publish wheel verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"publish_wheel_verification=pass wheels={len(wheels)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
