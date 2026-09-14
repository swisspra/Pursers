"""Typed access to the release train's single version manifest."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from packaging.version import InvalidVersion, Version


MANIFEST_PATH = Path(__file__).with_name("release_versions.toml")
PACKAGE_KEYS = (
    "pursers",
    "central",
    "client",
    "personal",
    "import",
    "wait_bridge",
)
TOOLCHAIN_KEYS = ("build", "setuptools", "wheel", "packaging", "pyproject-hooks")
WHEEL_DISTRIBUTIONS = {
    "pursers": "pursers",
    "central": "pursers-central",
    "client": "pursers-client",
    "personal": "pursers-personal",
    "import": "pursers-personal-import",
    "wait_bridge": "pursers-wait-bridge",
}


@dataclass(frozen=True)
class ReleaseVersions:
    product: str
    packages: Mapping[str, str]
    build_toolchain: Mapping[str, str]
    source_date_epoch: str


def load_versions(path: Path = MANIFEST_PATH) -> ReleaseVersions:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("release version manifest must use schema_version = 1")
    packages = document.get("packages")
    toolchain = document.get("build_toolchain")
    if not isinstance(packages, dict) or set(packages) != set(PACKAGE_KEYS):
        raise ValueError(f"packages must contain exactly {PACKAGE_KEYS!r}")
    if not isinstance(toolchain, dict) or set(toolchain) != set(TOOLCHAIN_KEYS):
        raise ValueError(f"build_toolchain must contain exactly {TOOLCHAIN_KEYS!r}")
    values = [document.get("product"), document.get("source_date_epoch")]
    values.extend(packages.values())
    values.extend(toolchain.values())
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("all release manifest values must be non-empty strings")
    return ReleaseVersions(
        product=document["product"],
        packages=dict(packages),
        build_toolchain=dict(toolchain),
        source_date_epoch=document["source_date_epoch"],
    )


VERSIONS = load_versions()


def release_version_from_tag(
    tag: str, versions: ReleaseVersions = VERSIONS
) -> Version:
    if not tag.startswith("v"):
        raise ValueError("release tag must start with v")
    raw = tag[1:]
    try:
        parsed = Version(raw)
    except InvalidVersion as exc:
        raise ValueError(f"release tag has invalid version: {tag}") from exc
    if str(parsed) != raw:
        raise ValueError(f"release tag must use canonical PEP 440 spelling: {tag}")
    if raw != versions.product:
        raise ValueError(
            f"release tag {tag} does not match manifest product {versions.product}"
        )
    return parsed


def github_release_flags(
    tag: str, *, existing: bool, versions: ReleaseVersions = VERSIONS
) -> tuple[str, ...]:
    version = release_version_from_tag(tag, versions)
    if version.is_prerelease:
        return ("--prerelease", "--latest=false")
    if existing:
        return ("--prerelease=false", "--latest")
    return ("--latest",)


def expected_wheel_filenames(
    versions: ReleaseVersions = VERSIONS,
) -> tuple[str, ...]:
    return tuple(
        f"{distribution.replace('-', '_')}-{versions.packages[key]}-py3-none-any.whl"
        for key, distribution in WHEEL_DISTRIBUTIONS.items()
    )
