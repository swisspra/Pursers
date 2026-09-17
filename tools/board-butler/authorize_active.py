#!/usr/bin/env python3
"""Create the separate operator authorization required for active mode."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Sequence


CONFIRMATION = "ENABLE-BOARD-BUTLER-ACTIVE"


def authorize(path: Path, confirmation: str) -> None:
    if confirmation != CONFIRMATION:
        raise ValueError(f"confirmation must be exactly {CONFIRMATION}")
    if not path.is_absolute():
        raise ValueError("--output must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if path.exists() or path.is_symlink():
        raise ValueError("refusing to replace an existing authorization path")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {"schema_version": 1, "mode": "active", "authorized": True},
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        path.unlink(missing_ok=True)
        raise ValueError("authorization file did not retain mode 0600")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--confirm", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        authorize(args.output, args.confirm)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"active authorization created at {args.output}")


if __name__ == "__main__":
    main()
