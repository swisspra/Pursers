from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tools import regenerate_integration_manifest as regenerator


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_regenerate_sorts_and_rewrites_only_existing_path_set(
    tmp_path: Path,
) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "nested" / "b.txt"
    second.parent.mkdir()
    first.write_bytes(b"first\n")
    second.write_bytes(b"second\n")
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(
        f"{'0' * 64}  nested/b.txt\n{'f' * 64}  a.txt\n",
        encoding="utf-8",
    )

    count, changed = regenerator.regenerate(
        tmp_path, Path("manifest.sha256")
    )

    assert (count, changed) == (2, True)
    assert manifest.read_text(encoding="utf-8") == (
        f"{_digest(first.read_bytes())}  a.txt\n"
        f"{_digest(second.read_bytes())}  nested/b.txt\n"
    )
    assert set(regenerator.listed_paths(manifest)) == {"a.txt", "nested/b.txt"}


def test_second_regeneration_is_byte_and_write_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracked = tmp_path / "tracked.txt"
    tracked.write_bytes(b"current\n")
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(f"{'0' * 64}  tracked.txt\n", encoding="utf-8")
    replacements = 0
    real_replace = regenerator.os.replace

    def replace(source: str, destination: Path) -> None:
        nonlocal replacements
        replacements += 1
        real_replace(source, destination)

    monkeypatch.setattr(regenerator.os, "replace", replace)

    assert regenerator.regenerate(tmp_path, Path("manifest.sha256")) == (1, True)
    first = manifest.read_bytes()
    assert regenerator.regenerate(tmp_path, Path("manifest.sha256")) == (1, False)
    assert manifest.read_bytes() == first
    assert replacements == 1


@pytest.mark.parametrize(
    "row, message",
    [
        (f"{'0' * 64} tracked.txt\n", "malformed line 1"),
        (
            f"{'0' * 64}  tracked.txt\n{'1' * 64}  tracked.txt\n",
            "duplicate path 'tracked.txt'",
        ),
        (f"{'0' * 64}  ../outside.txt\n", "unsafe path on line 1"),
    ],
)
def test_regenerate_rejects_ambiguous_manifest(
    tmp_path: Path, row: str, message: str
) -> None:
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(row, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        regenerator.regenerate(tmp_path, Path("manifest.sha256"))


def test_regenerate_rejects_missing_listed_file(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.sha256"
    manifest.write_text(f"{'0' * 64}  missing.txt\n", encoding="utf-8")

    with pytest.raises(ValueError, match="listed path is missing: missing.txt"):
        regenerator.regenerate(tmp_path, Path("manifest.sha256"))
