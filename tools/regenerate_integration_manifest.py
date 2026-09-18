#!/usr/bin/env python3
"""Regenerate the existing integration-file digest list without changing its scope."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path("tools/aionui-extension/INTEGRATION_FILES.sha256")
MANIFEST_ROW = re.compile(r"[0-9a-f]{64}  (.+)")


def listed_paths(manifest: Path) -> tuple[str, ...]:
    """Return the manifest's exact path set, rejecting ambiguous input."""
    paths: list[str] = []
    seen: set[str] = set()
    for line_number, row in enumerate(
        manifest.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = MANIFEST_ROW.fullmatch(row)
        if match is None:
            raise ValueError(f"{manifest}: malformed line {line_number}")
        relative = match.group(1)
        path = Path(relative)
        if path.is_absolute() or not relative or ".." in path.parts:
            raise ValueError(f"{manifest}: unsafe path on line {line_number}")
        if relative in seen:
            raise ValueError(f"{manifest}: duplicate path {relative!r}")
        seen.add(relative)
        paths.append(relative)
    if not paths:
        raise ValueError(f"{manifest}: no paths listed")
    return tuple(paths)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regenerate(
    root: Path = ROOT,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> tuple[int, bool]:
    """Refresh listed digests, sort rows, and return ``(count, changed)``."""
    root = root.resolve()
    manifest = root / manifest_path
    paths = listed_paths(manifest)
    rows: list[str] = []
    for relative in sorted(paths):
        candidate = root / relative
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"listed path is missing: {relative}") from exc
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f"listed path is not a repository file: {relative}")
        rows.append(f"{_sha256(resolved)}  {relative}")

    rendered = ("\n".join(rows) + "\n").encode("utf-8")
    if manifest.read_bytes() == rendered:
        return len(rows), False

    mode = manifest.stat().st_mode & 0o777
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=manifest.parent, delete=False) as stream:
            temporary_name = stream.name
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, manifest)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return len(rows), True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        count, changed = regenerate(args.repository, args.manifest)
    except (OSError, ValueError) as exc:
        print(f"integration manifest regeneration failed: {exc}")
        return 1
    state = "regenerated" if changed else "already current"
    print(f"integration manifest {state}: {count} paths")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
