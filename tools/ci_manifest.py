#!/usr/bin/env python3
"""Required Python test-suite manifest and CI runner.

The workflow deliberately delegates suite discovery, collection, execution, and
verification to this module so the suite list has one source of truth.

All required suites must pass from a workspace-write seat sandbox where the user home
is read-only and process inspection may be unavailable. Tests must inject local
state and process providers rather than depend on operator-machine access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    covers: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegrationFilesState:
    paths: tuple[str, ...]
    malformed_lines: tuple[int, ...]
    duplicate_paths: tuple[str, ...]
    missing_files: tuple[str, ...]
    stale_files: tuple[str, ...]
    unsorted: bool

    @property
    def valid(self) -> bool:
        return not (
            self.malformed_lines
            or self.duplicate_paths
            or self.missing_files
            or self.stale_files
            or self.unsorted
        )

    def mismatch_message(self) -> str:
        return (
            "integration files manifest mismatch: "
            f"malformed_lines={list(self.malformed_lines)}, "
            f"duplicate_paths={list(self.duplicate_paths)}, "
            f"missing_files={list(self.missing_files)}, "
            f"stale_files={list(self.stale_files)}, unsorted={self.unsorted}"
        )


SUITES: tuple[Suite, ...] = (
    Suite("central", "packages/central/tests", covers=("packages/central",)),
    Suite("client", "packages/client/tests", covers=("packages/client",)),
    Suite(
        "import",
        "packages/import/tests",
        cwd="packages/import",
        covers=("packages/import",),
    ),
    Suite("personal", "packages/personal/tests", covers=("packages/personal",)),
    Suite("wait-bridge", "tools/wait-bridge/tests", covers=("tools/wait-bridge",)),
    Suite(
        "fleet-dashboard",
        "tools/fleet-dashboard/tests",
        covers=("tools/fleet-dashboard",),
    ),
    Suite("coordinator", "tools/coordinator/tests", covers=("tools/coordinator",)),
    Suite("board-butler", "tools/board-butler/tests", covers=("tools/board-butler",)),
    Suite(
        "worker-runtime",
        "tools/worker-runtime/tests",
        covers=("tools/worker-runtime",),
    ),
    Suite("acp-seat", "tools/acp-seat/tests", covers=("tools/acp-seat",)),
    Suite("acp-agent", "tools/acp-agent/tests", covers=("tools/acp-agent",)),
    Suite("seat-kit", "tools/seat-kit/tests", covers=("tools/seat-kit",)),
    Suite(
        "aionui-extension",
        "tools/aionui-extension/tests",
        covers=("tools/aionui-extension", "packages/client", "packages/personal"),
    ),
    Suite("release-tools", "tools/tests", covers=("tools",)),
)

INTEGRATION_FILES_MANIFEST = Path(
    "tools/aionui-extension/INTEGRATION_FILES.sha256"
)
SHA256_MANIFEST_LINE = re.compile(r"([0-9a-f]{64})  (.+)")


def covering_suites(
    changed_paths: Sequence[str], suites: Sequence[Suite] = SUITES
) -> dict[str, tuple[str, ...]]:
    """Map changed repository paths to every manifest suite that covers them."""
    result: dict[str, tuple[str, ...]] = {}
    for raw_path in changed_paths:
        path = raw_path.strip().strip("/")
        if not path or path.startswith("../"):
            raise ValueError(f"invalid repository path in coverage query: {raw_path!r}")
        names = tuple(
            suite.name
            for suite in suites
            if any(
                path == prefix.strip("/")
                or path.startswith(prefix.strip("/") + "/")
                for prefix in suite.covers
                if prefix.strip("/")
            )
        )
        result[path] = names
    return result


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def discovered_test_directories(root: Path) -> set[str]:
    paths = {
        path.relative_to(root).as_posix()
        for pattern in ("packages/*/tests", "tools/*/tests")
        for path in root.glob(pattern)
        if path.is_dir()
        and any(
            candidate.is_file()
            for test_pattern in ("test_*.py", "*_test.py")
            for candidate in path.rglob(test_pattern)
        )
    }
    tools_tests = root / "tools" / "tests"
    if tools_tests.is_dir() and any(tools_tests.glob("test_*.py")):
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


def inspect_integration_files(
    root: Path,
    manifest_path: Path = INTEGRATION_FILES_MANIFEST,
) -> IntegrationFilesState:
    """Inspect the cumulative integration manifest without deciding gate policy."""
    manifest = root / manifest_path
    rows = manifest.read_text(encoding="utf-8").splitlines()
    malformed: list[int] = []
    duplicate_paths: list[str] = []
    missing_files: list[str] = []
    stale_files: list[str] = []
    paths: list[str] = []
    seen: set[str] = set()

    for line_number, row in enumerate(rows, start=1):
        match = SHA256_MANIFEST_LINE.fullmatch(row)
        if match is None:
            malformed.append(line_number)
            continue
        expected, relative = match.groups()
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            malformed.append(line_number)
            continue
        if relative in seen:
            duplicate_paths.append(relative)
            continue
        seen.add(relative)
        paths.append(relative)
        candidate = root / relative_path
        if not candidate.is_file():
            missing_files.append(relative)
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected:
            stale_files.append(relative)

    unsorted = paths != sorted(paths)
    return IntegrationFilesState(
        paths=tuple(paths),
        malformed_lines=tuple(malformed),
        duplicate_paths=tuple(duplicate_paths),
        missing_files=tuple(missing_files),
        stale_files=tuple(stale_files),
        unsorted=unsorted,
    )


def validate_integration_files(
    root: Path,
    manifest_path: Path = INTEGRATION_FILES_MANIFEST,
) -> None:
    """Verify that the cumulative integration manifest describes this tree."""
    state = inspect_integration_files(root, manifest_path)
    if not state.valid:
        raise ValueError(state.mismatch_message())


def changed_paths_since(root: Path, base_ref: str) -> tuple[str, ...]:
    """Return committed and local paths changed from the supplied base ref."""
    commands = (
        ["git", "diff", "--name-only", "--diff-filter=ACDMRTUXB", f"{base_ref}...HEAD"],
        ["git", "diff", "--name-only", "--diff-filter=ACDMRTUXB", "HEAD"],
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACDMRTUXB", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    changed: set[str] = set()
    for command in commands:
        completed = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(
                f"cannot identify branch changes from {base_ref!r}: {detail}"
            )
        changed.update(line for line in completed.stdout.splitlines() if line)
    return tuple(sorted(changed))


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


def suite_environment(root: Path) -> dict[str, str]:
    """Prefer checkout package sources over operator installations."""
    environment = os.environ.copy()
    sources = [str(path) for path in sorted((root / "packages").glob("*/src"))]
    inherited = environment.get("PYTHONPATH", "").strip()
    if inherited:
        sources.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(sources)
    return environment


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
            env=suite_environment(root),
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
            env=suite_environment(root),
            check=False,
        )
        print("::endgroup::", flush=True)
        if completed.returncode != 0:
            raise RuntimeError(
                f"pytest failed for {suite.name} ({suite.path}) "
                f"with exit code {completed.returncode}"
            )


def run_seat_suites(root: Path, suites: Sequence[Suite] = SUITES) -> None:
    """Run every suite for seat evidence, with release-owned gates reported apart."""
    failures: list[str] = []
    for suite in suites:
        print(f"::group::pytest {suite.name} ({suite.path})", flush=True)
        command = [sys.executable, "-m", "pytest", "-q", pytest_target(suite)]
        if suite.path == "tools/tests":
            command.extend(
                ["-k", "not test_integration_files_manifest_matches_the_tree"]
            )
        completed = subprocess.run(
            command,
            cwd=root / suite.cwd,
            env=suite_environment(root),
            check=False,
        )
        print("::endgroup::", flush=True)
        if completed.returncode != 0:
            failures.append(
                f"{suite.name} ({suite.path}) exit={completed.returncode}"
            )
    if failures:
        raise RuntimeError("seat suite failures: " + ", ".join(failures))


def print_seat_digest_report(root: Path, base_ref: str) -> None:
    """Print explicit non-gate digest evidence for a worker branch."""
    state = inspect_integration_files(root)
    changed = changed_paths_since(root, base_ref)
    listed = set(state.paths)
    changed_listed = sorted(listed.intersection(changed))
    status = "current" if state.valid else "stale"
    print("SEAT SUITE REPORT (NOT A RELEASE/CI GATE)")
    print(f"integration_digest_status={status}")
    print(f"integration_base_ref={base_ref}")
    print(f"integration_digest_stale_files={list(state.stale_files)}")
    print(f"integration_listed_files_changed={changed_listed}")
    print("integration_digest_test=reported_separately_not_run_as_a_suite_test")
    print("release_gate_command=python3 tools/ci_manifest.py run")
    print(
        "release_gate_owner=operator_at_merge; worker seats must not regenerate "
        "INTEGRATION_FILES.sha256 or component-lock.json"
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
    seat_run = subparsers.add_parser(
        "seat-suite-report",
        help="run seat suites and report release-owned digest drift; not a CI gate",
    )
    seat_run.add_argument("--base-ref", default="origin/main")
    verify = subparsers.add_parser("verify", help="verify a collection report")
    verify.add_argument("--input", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = repository_root()
    try:
        validate_manifest(root)
        if args.command != "seat-suite-report":
            validate_integration_files(root)
        if args.command == "check":
            print(f"manifest covers all {len(SUITES)} test directories")
        elif args.command == "collect":
            payload = collect_counts(root)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        elif args.command == "run":
            run_suites(root)
        elif args.command == "seat-suite-report":
            print_seat_digest_report(root, args.base_ref)
            run_seat_suites(root)
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
