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
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence, TextIO


@dataclass(frozen=True)
class Suite:
    name: str
    path: str
    cwd: str = "."
    covers: tuple[str, ...] = ()
    # Relative wall-time hint used only to start expensive parallel work first.
    # Output and validation always retain manifest order.
    parallel_weight: int = 0


@dataclass(frozen=True)
class SuiteResult:
    suite: Suite
    returncode: int
    output: str
    duration_s: float


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
    Suite(
        "central",
        "packages/central/tests",
        covers=("packages/central",),
        parallel_weight=90,
    ),
    Suite(
        "client",
        "packages/client/tests",
        covers=("packages/client",),
        parallel_weight=9,
    ),
    Suite(
        "import",
        "packages/import/tests",
        cwd="packages/import",
        covers=("packages/import",),
        parallel_weight=24,
    ),
    Suite(
        "personal",
        "packages/personal/tests",
        covers=("packages/personal",),
        parallel_weight=3,
    ),
    Suite(
        "wait-bridge",
        "tools/wait-bridge/tests",
        covers=("tools/wait-bridge",),
        parallel_weight=47,
    ),
    Suite(
        "fleet-dashboard",
        "tools/fleet-dashboard/tests",
        covers=("tools/fleet-dashboard",),
        parallel_weight=85,
    ),
    Suite(
        "coordinator",
        "tools/coordinator/tests",
        covers=("tools/coordinator",),
        parallel_weight=3,
    ),
    Suite(
        "board-butler",
        "tools/board-butler/tests",
        covers=("tools/board-butler",),
        parallel_weight=4,
    ),
    Suite(
        "worker-runtime",
        "tools/worker-runtime/tests",
        covers=("tools/worker-runtime",),
        parallel_weight=15,
    ),
    Suite(
        "acp-seat",
        "tools/acp-seat/tests",
        covers=("tools/acp-seat",),
        parallel_weight=8,
    ),
    Suite(
        "acp-agent",
        "tools/acp-agent/tests",
        covers=("tools/acp-agent",),
        parallel_weight=2,
    ),
    Suite(
        "seat-kit",
        "tools/seat-kit/tests",
        covers=("tools/seat-kit",),
        parallel_weight=93,
    ),
    Suite(
        "aionui-extension",
        "tools/aionui-extension/tests",
        covers=("tools/aionui-extension", "packages/client", "packages/personal"),
        parallel_weight=143,
    ),
    Suite(
        "release-tools",
        "tools/tests",
        covers=("tools",),
        parallel_weight=126,
    ),
)

INTEGRATION_FILES_MANIFEST = Path(
    "tools/aionui-extension/INTEGRATION_FILES.sha256"
)
SHA256_MANIFEST_LINE = re.compile(r"([0-9a-f]{64})  (.+)")
# One observed abandoned seat root exceeded 7.7 GB. Keep enough headroom for a
# similarly sized run plus normal filesystem churn.
DEFAULT_MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024
MIN_FREE_BYTES_ENV = "PURSERS_CI_MIN_FREE_BYTES"
DEFAULT_JOB_CAP = 4
SCRATCH_PREFIX = "pursers-ci-manifest-"
SCRATCH_OWNER_FILE = ".owner.json"
SCRATCH_OWNER_SCHEMA = 1


@contextlib.contextmanager
def _locked_scratch_parent(parent: Path) -> Iterator[None]:
    """Serialize stale-root sweeping and new-root registration."""
    descriptor = os.open(parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _valid_scratch_owner(handle: TextIO) -> bool:
    try:
        handle.seek(0)
        payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("schema") == SCRATCH_OWNER_SCHEMA
        and isinstance(payload.get("pid"), int)
        and not isinstance(payload.get("pid"), bool)
        and payload["pid"] > 0
    )


def _sweep_abandoned_scratch(parent: Path) -> None:
    """Remove only roots whose valid owner lock is no longer held.

    The PID is diagnostic metadata. The advisory lock is the liveness proof: it
    is released by the kernel on normal exit, exceptions, and SIGKILL, and it
    cannot mistake an unrelated process that later reuses the same PID for the
    original owner.
    """
    for candidate in sorted(parent.glob(f"{SCRATCH_PREFIX}*")):
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        owner_path = candidate / SCRATCH_OWNER_FILE
        try:
            owner = owner_path.open("r+", encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError, OSError):
            # Legacy or malformed roots have no trustworthy liveness signal.
            continue
        try:
            try:
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            if _valid_scratch_owner(owner):
                shutil.rmtree(candidate)
        finally:
            owner.close()


@contextlib.contextmanager
def _owned_scratch_directory(parent: Path) -> Iterator[Path]:
    """Create an auto-cleaned root that concurrent runners cannot sweep."""
    with _locked_scratch_parent(parent):
        _sweep_abandoned_scratch(parent)
        temporary = tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX, dir=parent)
        scratch = Path(temporary.name)
        owner = (scratch / SCRATCH_OWNER_FILE).open("w+", encoding="utf-8")
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX)
        json.dump(
            {"schema": SCRATCH_OWNER_SCHEMA, "pid": os.getpid()},
            owner,
            sort_keys=True,
        )
        owner.write("\n")
        owner.flush()
        os.fsync(owner.fileno())
    try:
        yield scratch
    finally:
        try:
            # Keep the owner lock held until the root itself is gone.
            temporary.cleanup()
        finally:
            fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
            owner.close()


def require_free_space(
    path: Path,
    minimum_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> None:
    """Stop before test execution when the checkout volume is too full."""
    if minimum_free_bytes < 0:
        raise ValueError(f"{MIN_FREE_BYTES_ENV} must not be negative")
    free = shutil.disk_usage(path).free
    if free < minimum_free_bytes:
        raise RuntimeError(
            "LOW DISK SPACE: refusing to start the CI manifest; "
            f"free_bytes={free}, required_bytes={minimum_free_bytes}. "
            "Reclaim abandoned temp roots, then rerun."
        )


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


def default_job_count(
    cpu_count: int | None = None,
    load_average: float | None = None,
) -> int:
    """Use idle CPU capacity without adding pressure to an overloaded host."""
    available = os.cpu_count() if cpu_count is None else cpu_count
    available = max(1, available or 1)
    if load_average is None:
        try:
            load_average = os.getloadavg()[0]
        except (AttributeError, OSError):
            load_average = 0.0
    idle_capacity = max(1, int(available - max(0.0, load_average)))
    return min(DEFAULT_JOB_CAP, available, idle_capacity)


def suite_environment(root: Path, scratch: Path | None = None) -> dict[str, str]:
    """Prefer checkout package sources over operator installations."""
    environment = os.environ.copy()
    sources = [str(path) for path in sorted((root / "packages").glob("*/src"))]
    inherited = environment.get("PYTHONPATH", "").strip()
    if inherited:
        sources.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(sources)
    if scratch is not None:
        isolated = {
            "TMPDIR": scratch / "tmp",
            "TEMP": scratch / "tmp",
            "TMP": scratch / "tmp",
            "XDG_CACHE_HOME": scratch / "xdg-cache",
            "PIP_CACHE_DIR": scratch / "pip-cache",
            "UV_CACHE_DIR": scratch / "uv-cache",
            "npm_config_cache": scratch / "npm-cache",
        }
        for path in set(isolated.values()):
            path.mkdir(parents=True, exist_ok=True)
        environment.update({name: str(path) for name, path in isolated.items()})
        # Do not let parallel interpreters race on checkout __pycache__ files.
        # Disabling writes avoids the cold I/O cost of a new tree per suite.
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
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


def _run_suite(root: Path, suite: Suite, scratch: Path) -> SuiteResult:
    pytest_tmp = scratch / "pytest-tmp"
    pytest_cache = scratch / "pytest-cache"
    pytest_tmp.mkdir(parents=True, exist_ok=True)
    pytest_cache.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--basetemp",
            str(pytest_tmp),
            "-o",
            f"cache_dir={pytest_cache}",
            pytest_target(suite),
        ],
        cwd=root / suite.cwd,
        env=suite_environment(root, scratch),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return SuiteResult(
        suite=suite,
        returncode=completed.returncode,
        output=completed.stdout or "",
        duration_s=time.monotonic() - started,
    )


def _print_suite_result(result: SuiteResult) -> None:
    suite = result.suite
    print(f"::group::pytest {suite.name} ({suite.path})", flush=True)
    if result.output:
        print(result.output, end="" if result.output.endswith("\n") else "\n")
    print(
        f"ci-manifest suite {suite.name} duration={result.duration_s:.2f}s "
        f"exit={result.returncode}",
        flush=True,
    )
    print("::endgroup::", flush=True)


def run_suites(
    root: Path,
    suites: Sequence[Suite] = SUITES,
    jobs: int | None = None,
) -> None:
    worker_count = default_job_count() if jobs is None else jobs
    if worker_count < 1:
        raise ValueError("--jobs must be at least 1")

    configured_tmp = os.environ.get("TMPDIR")
    scratch_parent = (
        Path(configured_tmp).expanduser().resolve()
        if configured_tmp
        else Path(tempfile.gettempdir()).resolve()
    )
    scratch_parent.mkdir(parents=True, exist_ok=True)
    failures: list[SuiteResult] = []
    with _owned_scratch_directory(scratch_parent) as scratch_root:
        indexed = tuple(
            (suite, scratch_root / f"{index:02d}-{suite.name}")
            for index, suite in enumerate(suites)
        )

        if worker_count == 1:
            for suite, scratch in indexed:
                result = _run_suite(root, suite, scratch)
                _print_suite_result(result)
                if result.returncode != 0:
                    failures.append(result)
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(worker_count, max(1, len(indexed)))
            ) as executor:
                prioritized = sorted(
                    enumerate(indexed),
                    key=lambda item: (-item[1][0].parallel_weight, item[0]),
                )
                futures = {
                    position: executor.submit(_run_suite, root, suite, scratch)
                    for position, (suite, scratch) in prioritized
                }
                # Await and print in manifest order, regardless of finish order.
                for position in range(len(indexed)):
                    result = futures[position].result()
                    _print_suite_result(result)
                    if result.returncode != 0:
                        failures.append(result)

    if failures:
        detail = "; ".join(
            f"pytest failed for {result.suite.name} ({result.suite.path}) "
            f"with exit code {result.returncode}"
            for result in failures
        )
        raise RuntimeError(f"pytest failures: {detail}")
    print(f"ci manifest: all {len(suites)} required pytest suites passed")


def run_seat_suites(root: Path, suites: Sequence[Suite] = SUITES) -> None:
    """Run every suite for seat evidence, with release-owned gates reported apart."""
    configured_tmp = os.environ.get("TMPDIR")
    scratch_parent = (
        Path(configured_tmp).expanduser().resolve()
        if configured_tmp
        else Path(tempfile.gettempdir()).resolve()
    )
    scratch_parent.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    with _owned_scratch_directory(scratch_parent) as scratch_root:
        for index, suite in enumerate(suites):
            scratch = scratch_root / f"{index:02d}-{suite.name}"
            pytest_tmp = scratch / "pytest-tmp"
            pytest_cache = scratch / "pytest-cache"
            pytest_tmp.mkdir(parents=True, exist_ok=True)
            pytest_cache.mkdir(parents=True, exist_ok=True)
            print(f"::group::pytest {suite.name} ({suite.path})", flush=True)
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--basetemp",
                str(pytest_tmp),
                "-o",
                f"cache_dir={pytest_cache}",
                pytest_target(suite),
            ]
            if suite.path == "tools/tests":
                command.extend(
                    [
                        "-k",
                        "not test_integration_files_manifest_matches_the_tree "
                        "and not test_real_tree_is_clean_and_current_bump_has_zero_diff",
                    ]
                )
            completed = subprocess.run(
                command,
                cwd=root / suite.cwd,
                env=suite_environment(root, scratch),
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
    print("component_lock_test=reported_separately_not_run_as_a_suite_test")
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
    run = subparsers.add_parser("run", help="run every required suite")
    run.add_argument(
        "--jobs",
        type=int,
        default=None,
        help=(
            "parallel pytest groups "
            f"(default: idle CPU capacity capped at {DEFAULT_JOB_CAP})"
        ),
    )
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
        configured_minimum = int(
            os.environ.get(MIN_FREE_BYTES_ENV, str(DEFAULT_MIN_FREE_BYTES))
        )
        require_free_space(root, configured_minimum)
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
            run_suites(root, jobs=args.jobs)
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
