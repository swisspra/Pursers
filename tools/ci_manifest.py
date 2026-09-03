#!/usr/bin/env python3
"""Required Python test-suite manifest and CI runner.

The workflow deliberately delegates suite discovery, collection, execution, and
verification to this module so the suite list has one source of truth.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class Suite:
    name: str
    path: str
    cwd: str = "."


SUITES: tuple[Suite, ...] = (
    Suite("central", "packages/central/tests"),
    Suite("client", "packages/client/tests"),
    Suite("import", "packages/import/tests", cwd="packages/import"),
    Suite("personal", "packages/personal/tests"),
    Suite("wait-bridge", "tools/wait-bridge/tests"),
    Suite("fleet-dashboard", "tools/fleet-dashboard/tests"),
    Suite("coordinator", "tools/coordinator/tests"),
    Suite("worker-runtime", "tools/worker-runtime/tests"),
    Suite("seat-kit", "tools/seat-kit/tests"),
    Suite("release-tools", "tools/tests"),
)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def discovered_test_directories(root: Path) -> set[str]:
    paths = {
        path.relative_to(root).as_posix()
        for pattern in ("packages/*/tests", "tools/*/tests")
        for path in root.glob(pattern)
        if path.is_dir()
    }
    tools_tests = root / "tools" / "tests"
    if tools_tests.is_dir():
        paths.add(tools_tests.relative_to(root).as_posix())
    return paths


def validate_manifest(root: Path, suites: Sequence[Suite] = SUITES) -> None:
    names = [suite.name for suite in suites]
    paths = [suite.path for suite in suites]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    duplicate_paths = sorted({path for path in paths if paths.count(path) > 1})
    if duplicate_names or duplicate_paths:
        raise ValueError(
            "duplicate manifest entries: "
            f"names={duplicate_names}, paths={duplicate_paths}"
        )

    expected = set(paths)
    discovered = discovered_test_directories(root)
    missing = sorted(discovered - expected)
    stale = sorted(expected - discovered)
    if missing or stale:
        raise ValueError(
            "test-suite manifest mismatch: "
            f"unlisted_test_directories={missing}, missing_directories={stale}"
        )


def parse_collected_count(output: str, suite_path: str) -> int:
    summaries = re.findall(
        r"(?m)^(\d+) tests? collected(?: in [^\n]+)?$",
        output,
    )
    if summaries:
        return int(summaries[-1])

    # Retain a node-id fallback for pytest format changes and custom reporters.
    prefix = f"{suite_path}/"
    return sum(
        1
        for raw_line in output.splitlines()
        if (line := raw_line.strip()).startswith(prefix) and "::" in line
    )


def pytest_target(suite: Suite) -> str:
    if suite.cwd == ".":
        return suite.path
    return Path(suite.path).relative_to(suite.cwd).as_posix()


def collect_counts(root: Path, suites: Sequence[Suite] = SUITES) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for suite in suites:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                pytest_target(suite),
            ],
            cwd=root / suite.cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        output = completed.stdout + completed.stderr
        if completed.returncode != 0:
            raise RuntimeError(
                f"collection failed for {suite.name} ({suite.path})\n{output}"
            )
        count = parse_collected_count(output, suite.path)
        if count == 0:
            raise RuntimeError(
                f"suite collected zero tests: {suite.name} ({suite.path})\n{output}"
            )
        print(f"collected {count:4d}  {suite.name} ({suite.path})")
        results.append(
            {"name": suite.name, "path": suite.path, "collected": count}
        )
    return {"schema": 1, "suites": results}


def run_suites(root: Path, suites: Sequence[Suite] = SUITES) -> None:
    for suite in suites:
        print(f"::group::pytest {suite.name} ({suite.path})", flush=True)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", pytest_target(suite)],
            cwd=root / suite.cwd,
            check=False,
        )
        print("::endgroup::", flush=True)
        if completed.returncode != 0:
            raise RuntimeError(
                f"pytest failed for {suite.name} ({suite.path}) "
                f"with exit code {completed.returncode}"
            )


def verify_counts(payload: Any, suites: Sequence[Suite] = SUITES) -> None:
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("collection report must be a schema 1 object")
    rows = payload.get("suites")
    if not isinstance(rows, list):
        raise ValueError("collection report suites must be a list")

    expected = {(suite.name, suite.path) for suite in suites}
    actual: set[tuple[str, str]] = set()
    invalid: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("collection report suite rows must be objects")
        name = row.get("name")
        path = row.get("path")
        count = row.get("collected")
        if not isinstance(name, str) or not isinstance(path, str):
            raise ValueError("collection report suite name/path must be strings")
        key = (name, path)
        if key in actual:
            raise ValueError(f"duplicate collection report suite: {name} ({path})")
        actual.add(key)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            invalid.append(f"{name} ({path})={count!r}")

    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected or invalid:
        raise ValueError(
            "collection report mismatch: "
            f"missing={missing}, unexpected={unexpected}, non_positive={invalid}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="compare test directories with the manifest")
    collect = subparsers.add_parser("collect", help="collect every required suite")
    collect.add_argument("--output", type=Path, required=True)
    subparsers.add_parser("run", help="run every required suite")
    verify = subparsers.add_parser("verify", help="verify a collection report")
    verify.add_argument("--input", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = repository_root()
    try:
        validate_manifest(root)
        if args.command == "check":
            print(f"manifest covers all {len(SUITES)} test directories")
        elif args.command == "collect":
            payload = collect_counts(root)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        elif args.command == "run":
            run_suites(root)
        elif args.command == "verify":
            payload = json.loads(args.input.read_text(encoding="utf-8"))
            verify_counts(payload)
            print(f"verified non-zero collection for all {len(SUITES)} suites")
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ci manifest error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
