"""Typed access to the release train's single version manifest."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


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
