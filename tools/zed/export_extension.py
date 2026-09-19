#!/usr/bin/env python3
"""Export the Pursers Zed extension as a standalone Git repository."""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path
from typing import Sequence


DEFAULT_PREFIX = Path("integrations/zed/pursers-mcp")


class ExportError(RuntimeError):
    """Raised when an extension export cannot be produced safely."""


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(args),
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except subprocess.CalledProcessError as exc:
        command = shlex.join(str(arg) for arg in args)
        detail = (exc.stderr or exc.stdout or "").strip()
        raise ExportError(f"command failed ({command}): {detail}") from exc


def validate_relative_path(raw: str, *, name: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ExportError(f"{name} must be a non-empty repository-relative path")
    return path


def resolve_commit(source_repo: Path, revision: str) -> str:
    if not revision or revision.startswith("-"):
        raise ExportError("revision must name a commit and must not start with '-'")
    result = _run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ],
        cwd=source_repo,
    )
    return result.stdout.strip()


def require_empty_output(output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ExportError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)


def export_extension(
    source_repo: Path,
    revision: str,
    output_dir: Path,
    *,
    prefix: Path = DEFAULT_PREFIX,
) -> dict[str, str | list[str]]:
    """Create a standalone repository and return its stable Git identifiers."""

    source_repo = source_repo.resolve()
    output_dir = output_dir.resolve()
    prefix = validate_relative_path(prefix.as_posix(), name="prefix")
    if not (source_repo / ".git").exists():
        raise ExportError(f"source is not a Git checkout: {source_repo}")

    source_commit = resolve_commit(source_repo, revision)
    tree_entry = _run(
        ["git", "ls-tree", "-d", "--name-only", source_commit, prefix.as_posix()],
        cwd=source_repo,
    ).stdout.strip()
    if tree_entry != prefix.as_posix():
        raise ExportError(
            f"extension directory does not exist at {source_commit}: {prefix}"
        )
    license_entry = _run(
        [
            "git",
            "cat-file",
            "-e",
            f"{source_commit}:{prefix.as_posix()}/LICENSE",
        ],
        cwd=source_repo,
    )
    del license_entry

    split = _run(
        [
            "git",
            "subtree",
            "split",
            f"--prefix={prefix.as_posix()}",
            source_commit,
        ],
        cwd=source_repo,
    ).stdout.strip().splitlines()[-1]

    require_empty_output(output_dir)
    _run(["git", "init", "--initial-branch=main"], cwd=output_dir)
    _run(
        [
            "git",
            "fetch",
            "--no-tags",
            source_repo.as_posix(),
            split,
        ],
        cwd=output_dir,
    )
    _run(["git", "reset", "--hard", split], cwd=output_dir)

    export_commit = resolve_commit(output_dir, "HEAD")
    export_tree = _run(
        ["git", "rev-parse", "--verify", "HEAD^{tree}"], cwd=output_dir
    ).stdout.strip()
    files = _run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=output_dir
    ).stdout.splitlines()
    if "LICENSE" not in files:
        raise ExportError("exported extension does not contain LICENSE at repository root")

    return {
        "source_commit": source_commit,
        "export_commit": export_commit,
        "export_tree": export_tree,
        "files": files,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Pursers checkout")
    parser.add_argument("--commit", required=True, help="Pursers commit or ref")
    parser.add_argument("--output", type=Path, required=True, help="empty output dir")
    parser.add_argument(
        "--prefix",
        type=Path,
        default=DEFAULT_PREFIX,
        help=f"extension directory (default: {DEFAULT_PREFIX})",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = export_extension(
            args.source,
            args.commit,
            args.output,
            prefix=args.prefix,
        )
    except ExportError as exc:
        print(f"EXPORT FAIL: {exc}")
        return 1

    print(f"SOURCE_COMMIT={result['source_commit']}")
    print(f"EXPORT_COMMIT={result['export_commit']}")
    print(f"EXPORT_TREE={result['export_tree']}")
    print("TREE_LISTING:")
    for path in result["files"]:
        print(path)
    print("EXPORT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
