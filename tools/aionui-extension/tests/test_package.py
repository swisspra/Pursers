from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def load_builder():
    spec = importlib.util.spec_from_file_location("aionui_extension_build", ROOT / "build.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_contains_only_allowlisted_runtime_files(tmp_path: Path) -> None:
    builder = load_builder()
    archive_path = builder.build(tmp_path / builder.ARCHIVE_NAME)
    with ZipFile(archive_path) as archive:
        assert archive.namelist() == list(builder.PACKAGE_FILES)
        assert len(archive.namelist()) == 17
        assert "IMPORT_PROVENANCE.md" in archive.namelist()
        assert "host/helper.cjs" in archive.namelist()
        assert "host/HELPER_CONTRACT.md" in archive.namelist()
        assert "team/adapter.cjs" in archive.namelist()
        assert "team/TEAM_ADAPTER_CONTRACT.md" in archive.namelist()


def test_package_build_is_byte_deterministic(tmp_path: Path) -> None:
    builder = load_builder()
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = builder.build(first_dir / builder.ARCHIVE_NAME)
    second = builder.build(second_dir / builder.ARCHIVE_NAME)
    assert first.read_bytes() == second.read_bytes()


def test_package_has_no_secrets_home_paths_or_private_identifiers(tmp_path: Path) -> None:
    builder = load_builder()
    archive_path = builder.build(tmp_path / builder.ARCHIVE_NAME)
    with ZipFile(archive_path) as archive:
        text = "\n".join(
            archive.read(name).decode("utf-8", errors="replace")
            for name in archive.namelist()
        )
    forbidden_literals = (
        "/Users/",
        "/home/",
        "C:\\Users\\",
        "swissp",
        "Claude-tech-default",
        ".pursers/fleet-dashboard",
        "BEGIN PRIVATE KEY",
        "Bearer ",
    )
    assert not any(value in text for value in forbidden_literals)
    assert re.search(r"prs1\.[A-Za-z0-9_-]{20,}", text) is None
    assert re.search(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", text) is None


def test_package_includes_every_relative_runtime_dependency(tmp_path: Path) -> None:
    builder = load_builder()
    archive_path = builder.build(tmp_path / builder.ARCHIVE_NAME)
    with ZipFile(archive_path) as archive:
        routes = archive.read("webui/routes.js").decode("utf-8")
        assert "../door/adapter.cjs" in routes
        assert "../security/loopback.cjs" in routes
        assert "../team/adapter.cjs" in routes
        helper = archive.read("host/helper.cjs").decode("utf-8")
        assert "../webui/routes.js" in helper
        assert "../security/loopback.cjs" in helper
