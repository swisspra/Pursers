#!/usr/bin/env python3
"""Extract one version's non-empty section from CHANGELOG.md."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Sequence


RELEASE_HEADING = re.compile(
    r"^## \[(?P<version>[^]]+)](?:\s+-\s+.*)?\s*$"
)


class ReleaseNotesError(ValueError):
    """Raised when a requested changelog section cannot be published."""


def extract_release_notes(changelog: str, version: str) -> str:
    """Return the exact body of *version*'s changelog section."""
    lines = changelog.splitlines()
    start: int | None = None
    end = len(lines)

    for index, line in enumerate(lines):
        match = RELEASE_HEADING.fullmatch(line)
        if match is None:
            continue
        if start is not None:
            end = index
            break
        if match.group("version") == version:
            start = index + 1

    if start is None:
        raise ReleaseNotesError(f"CHANGELOG.md has no [{version}] release section")

    notes = "\n".join(lines[start:end]).strip()
    if not notes:
        raise ReleaseNotesError(f"CHANGELOG.md [{version}] release notes are empty")
    return notes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="release version without a leading v")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path("CHANGELOG.md"),
        help="changelog path (default: CHANGELOG.md)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        notes = extract_release_notes(
            args.changelog.read_text(encoding="utf-8"),
            args.version,
        )
    except (OSError, ReleaseNotesError) as exc:
        raise SystemExit(f"release notes: FAIL: {exc}") from exc
    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
