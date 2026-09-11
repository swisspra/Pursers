#!/usr/bin/env python3
"""Print reviewed GitHub release flags for one manifest-bound tag."""

from __future__ import annotations

import argparse

from release_versions import github_release_flags


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("mode", choices=("create", "edit"))
    args = parser.parse_args()
    for flag in github_release_flags(args.tag, existing=args.mode == "edit"):
        print(flag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
