#!/usr/bin/env python3
"""Print reviewed GitHub release flags for one manifest-bound tag."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

from release_versions import github_release_flags, release_version_from_tag


def _git_revision(repository: Path, revision: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", revision],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def verify_tag_checkout(tag: str, repository: Path) -> str:
    """Require HEAD to be the commit selected by the qualified release tag."""
    release_version_from_tag(tag)
    tag_commit = _git_revision(repository, f"refs/tags/{tag}^{{commit}}")
    head_commit = _git_revision(repository, "HEAD^{commit}")
    if head_commit != tag_commit:
        raise ValueError(
            f"release checkout mismatch: HEAD {head_commit} != tag {tag_commit}"
        )
    return head_commit


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_existing_asset(local: Path, existing: Path) -> str:
    """Refuse to reuse a same-name GitHub asset with different bytes."""
    if local.name != existing.name:
        raise ValueError("release asset names differ")
    if (
        not local.is_file()
        or local.is_symlink()
        or not existing.is_file()
        or existing.is_symlink()
    ):
        raise ValueError("release assets must be regular files")
    local_digest = _sha256(local)
    existing_digest = _sha256(existing)
    if local_digest != existing_digest:
        raise ValueError(
            f"existing release asset mismatch: {local.name} "
            f"({existing_digest} != {local_digest})"
        )
    return local_digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument(
        "mode", choices=("create", "edit", "verify-checkout", "verify-asset")
    )
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--local", type=Path)
    parser.add_argument("--existing", type=Path)
    args = parser.parse_args()
    if args.mode in {"create", "edit"}:
        for flag in github_release_flags(args.tag, existing=args.mode == "edit"):
            print(flag)
    elif args.mode == "verify-checkout":
        print(verify_tag_checkout(args.tag, args.repository))
    else:
        release_version_from_tag(args.tag)
        if args.local is None or args.existing is None:
            parser.error("verify-asset requires --local and --existing")
        print(verify_existing_asset(args.local, args.existing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
