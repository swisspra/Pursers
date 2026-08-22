from __future__ import annotations

import importlib
import importlib.util
import os
import py_compile
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import onboard_personal.artifacts as artifacts


def test_hashless_local_wheel_receipt_uses_exact_member_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_path = tmp_path / "fixture_pkg" / "__init__.py"
    metadata_path = tmp_path / "fixture-1.dist-info" / "METADATA"
    package_path.parent.mkdir()
    metadata_path.parent.mkdir()
    package_path.write_text("VALUE = 1\n", encoding="utf-8")
    metadata_path.write_text("Name: fixture\nVersion: 1\n", encoding="utf-8")
    members = {
        "fixture_pkg/__init__.py": artifacts._digest(package_path),
        "fixture-1.dist-info/METADATA": artifacts._digest(metadata_path),
    }

    class Distribution:
        version = "1"
        files = [
            *members,
            "fixture-1.dist-info/RECORD",
            "fixture-1.dist-info/INSTALLER",
            "fixture-1.dist-info/REQUESTED",
            "fixture-1.dist-info/direct_url.json",
            "fixture-1.dist-info/uv_cache.json",
            "fixture_pkg/__pycache__/__init__.cpython-314.pyc",
        ]

        @staticmethod
        def read_text(name: str) -> str | None:
            if name == "direct_url.json":
                return '{"archive_info": {}, "url": "file:///fixture.whl"}'
            return None

        @staticmethod
        def locate_file(relative: str) -> Path:
            return tmp_path / relative

    monkeypatch.setattr(
        artifacts,
        "_lock_document",
        lambda: {
            "schema_version": 1,
            "components": {
                "fixture": {
                    "version": "1",
                    "wheel_sha256": "f" * 64,
                    "members": members,
                }
            },
        },
    )
    monkeypatch.setattr(
        artifacts.importlib.metadata,
        "distribution",
        lambda _name: Distribution(),
    )
    result = artifacts.verify_component_artifacts({"fixture"})
    assert result["fixture"]["wheel_provenance"] == "locked-members"

    Distribution.files = [*Distribution.files, "fixture_pkg/unapproved.py"]
    with pytest.raises(
        artifacts.ArtifactVerificationError, match="unapproved distribution members"
    ):
        artifacts.verify_component_artifacts({"fixture"})


def test_verified_import_ignores_timestamp_valid_malicious_pyc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_name = "pp4_verified_fixture"
    package = tmp_path / package_name
    package.mkdir()
    package_init = package / "__init__.py"
    module_path = package / "payload.py"
    package_init.write_text("\n", encoding="utf-8")
    fixed_time = 2_000_000_000
    module_path.write_text("VALUE = 'evil!'\n", encoding="utf-8")
    os.utime(module_path, (fixed_time, fixed_time))
    py_compile.compile(str(module_path), doraise=True)
    module_path.write_text("VALUE = 'clean'\n", encoding="utf-8")
    os.utime(module_path, (fixed_time, fixed_time))
    cache = Path(importlib.util.cache_from_source(str(module_path)))
    assert cache.is_file()

    monkeypatch.setattr(
        artifacts,
        "verify_component_artifacts",
        lambda _names: {
            "fixture-distribution": {
                "version": "1",
                "members": {
                    f"{package_name}/__init__.py": str(package_init),
                    f"{package_name}/payload.py": str(module_path),
                },
            }
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    try:
        module = artifacts.import_verified_component(
            "fixture-distribution",
            package_name,
            f"{package_name}.payload",
            package_member=f"{package_name}/__init__.py",
            module_member=f"{package_name}/payload.py",
        )
        assert module.VALUE == "clean"
    finally:
        sys.modules.pop(f"{package_name}.payload", None)
        sys.modules.pop(package_name, None)
