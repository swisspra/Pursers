#!/usr/bin/env python3
"""Write a byte-reproducible ``.tar.gz`` of one directory tree.

The release workflow packs the Home runtime wheelhouse twice and requires the
two archives to be identical. Shelling out to ``tar`` is not portable: the
ownership flags differ between GNU tar and bsdtar, and GNU tar recurses into
directories named in a member list. This writes a USTAR archive with members
in byte-sorted path order, root ownership, one fixed mtime, and a gzip header
without a filename or timestamp.
"""

from __future__ import annotations

import gzip
import os
import sys
import tarfile
from pathlib import Path


def _entries(root: Path, name: str) -> list[Path]:
    top = root / name
    if not top.is_dir():
        raise SystemExit(f"not a directory: {top}")
    return [top, *top.rglob("*")]


def write_archive(root: Path, name: str, output: Path, mtime: int) -> None:
    entries = sorted(
        _entries(root, name),
        key=lambda path: path.relative_to(root).as_posix().encode("utf-8"),
    )
    with output.open("wb") as raw, gzip.GzipFile(
        filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9
    ) as compressed, tarfile.open(
        fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT
    ) as archive:
        for path in entries:
            if path.is_symlink():
                raise SystemExit(f"symlinks are not allowed in the archive: {path}")
            info = archive.gettarinfo(str(path), arcname=path.relative_to(root).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = mtime
            info.mode = 0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644
            if info.isfile():
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: deterministic_tar.py ROOT NAME OUTPUT.tar.gz", file=sys.stderr)
        return 64
    mtime = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    write_archive(Path(argv[0]), argv[1], Path(argv[2]), mtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
