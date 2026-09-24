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
import hmac
import json
import os
import re
import secrets
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
class ChangeRecord:
    status: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class AffectedSelection:
    base: str
    candidate: str
    changes: tuple[ChangeRecord, ...]
    selected_suites: tuple[str, ...]
    escalation_reasons: tuple[str, ...]
    skipped_suite_reasons: dict[str, str]

    @property
    def full_gate(self) -> bool:
        return bool(self.escalation_reasons)


@dataclass(frozen=True)
class FullGateAdmissionTiming:
    admission_class: str
    budget: int
    queue_wait_s: float
    execution_s: float | None = None


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
FULL_SHA = re.compile(r"[0-9a-f]{40}")
MAX_AFFECTED_PATHS = 512
MAX_EVIDENCE_BYTES = 1_000_000
FULL_GATE_BUDGET_ENV = "PURSERS_FULL_GATE_CONCURRENCY"
FULL_GATE_STATE_DIR_ENV = "PURSERS_FULL_GATE_STATE_DIR"
FULL_GATE_CLASS_ENV = "PURSERS_FULL_GATE_CLASS"
FULL_GATE_LEASE_ENV = "PURSERS_TICKET_LEASE_ID"
FULL_GATE_PRIORITIES = {
    "critical-reviewer": 40,
    "active-reviewer": 30,
    "active-worker": 20,
    "background-validation": 10,
    # Main and release gates are mandatory and must not sit behind background work.
    "main": 50,
    "release": 50,
}
LEASE_REQUIRED_CLASSES = frozenset(
    {"critical-reviewer", "active-reviewer", "active-worker", "background-validation"}
)
POLICY_PATH_TERMS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "authority",
        "credential",
        "dispatch",
        "jwt",
        "lease",
        "permission",
        "policy",
        "principal",
        "protocol",
        "review",
        "schema",
        "secret",
        "security",
        "token",
    }
)
GLOBAL_EXACT_PATHS = frozenset(
    {
        ".github/workflows/ci.yml",
        "tools/ci_manifest.py",
        "tools/release_versions.toml",
        "tools/aionui-extension/INTEGRATION_FILES.sha256",
        "packages/personal/src/pursers_personal/resources/component-lock.json",
    }
)


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


def _scratch_parent() -> Path:
    configured_tmp = os.environ.get("TMPDIR")
    parent = (
        Path(configured_tmp).expanduser().resolve()
        if configured_tmp
        else Path(tempfile.gettempdir()).resolve()
    )
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def _sweep_scratch_parent() -> None:
    """Sweep unlocked managed roots before disk-space admission checks."""
    parent = _scratch_parent()
    with _locked_scratch_parent(parent):
        _sweep_abandoned_scratch(parent)


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


def _git(
    root: Path,
    arguments: Sequence[str],
    *,
    text: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[Any]:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=text,
        env=env,
    )
    if completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", "replace")
        stdout = completed.stdout if text else completed.stdout.decode("utf-8", "replace")
        raise RuntimeError(
            f"git {' '.join(arguments)} failed: {(stderr or stdout).strip()}"
        )
    return completed


def resolve_exact_commit(root: Path, value: str, label: str) -> str:
    """Resolve a caller-provided full SHA and reject names or abbreviated hashes."""
    if FULL_SHA.fullmatch(value) is None:
        raise ValueError(f"{label} must be an exact 40-character lowercase commit SHA")
    resolved = _git(root, ["rev-parse", "--verify", f"{value}^{{commit}}"])
    exact = resolved.stdout.strip()
    if exact != value:
        raise ValueError(f"{label} SHA drift: requested={value}, resolved={exact}")
    return exact


def diff_change_records(root: Path, base: str, candidate: str) -> tuple[ChangeRecord, ...]:
    """Read rename-aware paths from Git without accepting caller-owned path claims."""
    completed = _git(
        root,
        ["diff", "--name-status", "--find-renames", "-z", base, candidate, "--"],
        text=False,
    )
    fields = completed.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    records: list[ChangeRecord] = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii", "strict")
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(fields):
            raise RuntimeError("git emitted a malformed name-status record")
        paths = tuple(
            fields[index + offset].decode("utf-8", "surrogateescape")
            for offset in range(path_count)
        )
        index += path_count
        records.append(ChangeRecord(status=status, paths=paths))
    return tuple(records)


def _component_owner(path: str) -> str | None:
    parts = Path(path).parts
    if len(parts) >= 2 and parts[0] in {"packages", "tools"}:
        return "/".join(parts[:2])
    return None


def _path_escalation_reasons(path: str) -> set[str]:
    lowered = path.lower()
    parts = tuple(part.lower() for part in Path(path).parts)
    reasons: set[str] = set()
    if path in GLOBAL_EXACT_PATHS:
        reasons.add(f"global-or-generated:{path}")
    if path.startswith(".github/") or len(parts) == 1:
        reasons.add(f"repository-wide:{path}")
    if "tests" in parts or Path(path).name.startswith(("test_", "conftest.")):
        reasons.add(f"test-or-runner:{path}")
    name = Path(path).name.lower()
    if (
        path.startswith(("docs/release/", "dist/", "build/"))
        or "generated" in parts
        or name.endswith((".lock", ".sha256"))
    ):
        reasons.add(f"release-or-generated:{path}")
    tokens = set(filter(None, re.split(r"[^a-z0-9]+", lowered)))
    if tokens.intersection(POLICY_PATH_TERMS):
        reasons.add(f"policy-sensitive:{path}")
    return reasons


def select_affected_suites(
    root: Path,
    base: str,
    candidate: str,
    *,
    high_risk: bool = False,
    suites: Sequence[Suite] = SUITES,
) -> AffectedSelection:
    """Select suites from an exact Git diff; every uncertain case escalates."""
    exact_base = resolve_exact_commit(root, base, "base")
    exact_candidate = resolve_exact_commit(root, candidate, "candidate")
    records = diff_change_records(root, exact_base, exact_candidate)
    paths = tuple(sorted({path for record in records for path in record.paths}))
    if not paths:
        raise ValueError("base and candidate have no changed paths")
    if len(paths) > MAX_AFFECTED_PATHS:
        raise ValueError(
            f"affected diff exceeds bounded path limit: {len(paths)} > {MAX_AFFECTED_PATHS}"
        )

    coverage = covering_suites(paths, suites)
    escalation: set[str] = set()
    if high_risk:
        escalation.add("ticket-marked-high-risk")
    for path in paths:
        if not coverage[path]:
            escalation.add(f"unmapped:{path}")
        escalation.update(_path_escalation_reasons(path))
    owners = sorted(filter(None, {_component_owner(path) for path in paths}))
    if len(owners) > 1:
        escalation.add("cross-component:" + ",".join(owners))

    if escalation:
        selected = tuple(suite.name for suite in suites)
    else:
        covered = {name for names in coverage.values() for name in names}
        selected = tuple(suite.name for suite in suites if suite.name in covered)
        if not selected:
            # Defensive redundancy: coverage should already have emitted unmapped.
            escalation.add("no-reviewed-narrow-coverage-rule")
            selected = tuple(suite.name for suite in suites)

    skipped = {
        suite.name: "no changed path maps to this suite"
        for suite in suites
        if suite.name not in selected
    }
    return AffectedSelection(
        base=exact_base,
        candidate=exact_candidate,
        changes=records,
        selected_suites=selected,
        escalation_reasons=tuple(sorted(escalation)),
        skipped_suite_reasons=skipped,
    )


def selection_payload(selection: AffectedSelection) -> dict[str, Any]:
    return {
        "schema": 1,
        "base": selection.base,
        "candidate": selection.candidate,
        "changed_paths": sorted(
            {path for record in selection.changes for path in record.paths}
        ),
        "changes": [
            {"status": record.status, "paths": list(record.paths)}
            for record in selection.changes
        ],
        "selected_suites": list(selection.selected_suites),
        "full_gate": selection.full_gate,
        "full_gate_escalation_reasons": list(selection.escalation_reasons),
        "skipped_suite_reasons": selection.skipped_suite_reasons,
    }


def add_evidence_integrity(
    payload: dict[str, Any], signing_key_file: Path | None = None
) -> dict[str, Any]:
    """Bind evidence bytes with SHA-256 and optional secret-file HMAC."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(canonical) > MAX_EVIDENCE_BYTES:
        raise ValueError(
            f"evidence exceeds bounded size: {len(canonical)} > {MAX_EVIDENCE_BYTES}"
        )
    result = dict(payload)
    result["evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    if signing_key_file is not None:
        key = signing_key_file.read_bytes()
        if not key:
            raise ValueError("signing key file is empty")
        result["signature"] = {
            "algorithm": "hmac-sha256",
            "key_id": hashlib.sha256(key).hexdigest()[:16],
            "value": hmac.new(key, canonical, hashlib.sha256).hexdigest(),
        }
    return result


def write_json_evidence(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if len(encoded.encode()) > MAX_EVIDENCE_BYTES:
        raise ValueError("rendered evidence exceeds bounded size")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8")


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


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _full_gate_state_dir() -> Path:
    configured = os.environ.get(FULL_GATE_STATE_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".cache" / "pursers" / "full-gate"


def _queue_order(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            -FULL_GATE_PRIORITIES[row["admission_class"]],
            row["requested_ns"],
            row["request_id"],
        ),
    )


@contextlib.contextmanager
def full_gate_admission(
    admission_class: str,
    lease_id: str | None,
    *,
    budget: int | None = None,
    state_dir: Path | None = None,
    poll_interval_s: float = 0.1,
) -> Iterator[FullGateAdmissionTiming]:
    """Admit one host-wide full gate using a priority-ordered file queue."""
    if admission_class not in FULL_GATE_PRIORITIES:
        raise ValueError(f"unknown full-gate admission class: {admission_class}")
    if admission_class in LEASE_REQUIRED_CLASSES and not (lease_id or "").strip():
        raise RuntimeError(
            f"{admission_class} full gate requires an active ticket lease; "
            f"set {FULL_GATE_LEASE_ENV} or pass --ticket-lease"
        )
    configured_budget = (
        int(os.environ.get(FULL_GATE_BUDGET_ENV, "1")) if budget is None else budget
    )
    if configured_budget < 1:
        raise ValueError(f"{FULL_GATE_BUDGET_ENV} must be at least 1")
    directory = _full_gate_state_dir() if state_dir is None else state_dir
    directory.mkdir(parents=True, exist_ok=True)
    queue_dir = directory / "queue"
    queue_dir.mkdir(exist_ok=True)
    request_id = f"{time.time_ns()}-{os.getpid()}-{secrets.token_hex(6)}"
    request_path = queue_dir / f"{request_id}.json"
    request = {
        "schema": 1,
        "request_id": request_id,
        "pid": os.getpid(),
        "requested_ns": time.time_ns(),
        "admission_class": admission_class,
        "lease_id": lease_id,
    }
    request_path.write_text(json.dumps(request, sort_keys=True) + "\n", encoding="utf-8")
    queue_lock = (directory / "queue.lock").open("a+")
    slot_handle: TextIO | None = None
    queued_at = time.monotonic()
    try:
        while slot_handle is None:
            fcntl.flock(queue_lock.fileno(), fcntl.LOCK_EX)
            try:
                rows: list[dict[str, Any]] = []
                for queued in sorted(queue_dir.glob("*.json")):
                    try:
                        row = json.loads(queued.read_text(encoding="utf-8"))
                        valid = (
                            isinstance(row, dict)
                            and row.get("schema") == 1
                            and row.get("admission_class") in FULL_GATE_PRIORITIES
                            and isinstance(row.get("pid"), int)
                            and isinstance(row.get("requested_ns"), int)
                            and isinstance(row.get("request_id"), str)
                        )
                    except (OSError, json.JSONDecodeError):
                        valid = False
                        row = {}
                    if not valid or not _process_exists(row["pid"]):
                        queued.unlink(missing_ok=True)
                        continue
                    rows.append(row)
                ordered = _queue_order(rows)
                own_position = next(
                    (
                        index
                        for index, row in enumerate(ordered)
                        if row["request_id"] == request_id
                    ),
                    None,
                )
                free_slots: list[TextIO] = []
                for index in range(configured_budget):
                    candidate = (directory / f"slot-{index}.lock").open("a+")
                    try:
                        fcntl.flock(
                            candidate.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                        )
                    except BlockingIOError:
                        candidate.close()
                    else:
                        free_slots.append(candidate)
                if own_position is not None and own_position < len(free_slots):
                    slot_handle = free_slots[own_position]
                    request_path.unlink(missing_ok=True)
                    for handle in free_slots:
                        if handle is not slot_handle:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                            handle.close()
                else:
                    for handle in free_slots:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                        handle.close()
            finally:
                fcntl.flock(queue_lock.fileno(), fcntl.LOCK_UN)
            if slot_handle is None:
                time.sleep(poll_interval_s)

        timing = FullGateAdmissionTiming(
            admission_class=admission_class,
            budget=configured_budget,
            queue_wait_s=time.monotonic() - queued_at,
        )
        yield timing
    finally:
        request_path.unlink(missing_ok=True)
        if slot_handle is not None:
            fcntl.flock(slot_handle.fileno(), fcntl.LOCK_UN)
            slot_handle.close()
        queue_lock.close()


def run_full_gate(
    root: Path,
    *,
    jobs: int | None,
    admission_class: str,
    lease_id: str | None,
) -> FullGateAdmissionTiming:
    with full_gate_admission(admission_class, lease_id) as admission:
        started = time.monotonic()
        run_suites(root, jobs=jobs)
        elapsed = time.monotonic() - started
    timing = FullGateAdmissionTiming(
        admission_class=admission.admission_class,
        budget=admission.budget,
        queue_wait_s=admission.queue_wait_s,
        execution_s=elapsed,
    )
    print(
        "ci-manifest full-gate timing "
        f"class={timing.admission_class} budget={timing.budget} "
        f"queue_wait={timing.queue_wait_s:.2f}s execution={elapsed:.2f}s"
    )
    return timing


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

    scratch_parent = _scratch_parent()
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


def run_affected(
    root: Path,
    selection: AffectedSelection,
    *,
    jobs: int | None,
    admission_class: str,
    lease_id: str | None,
) -> dict[str, Any]:
    head = _git(root, ["rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
    if head != selection.candidate:
        raise RuntimeError(
            "affected suites must execute from the exact candidate checkout: "
            f"HEAD={head}, candidate={selection.candidate}"
        )
    if _git(root, ["status", "--porcelain"]).stdout:
        raise RuntimeError("affected suites require a clean candidate checkout")
    by_name = {suite.name: suite for suite in SUITES}
    selected = tuple(by_name[name] for name in selection.selected_suites)
    started = time.monotonic()
    timing: FullGateAdmissionTiming | None = None
    if selection.full_gate:
        timing = run_full_gate(
            root,
            jobs=jobs,
            admission_class=admission_class,
            lease_id=lease_id,
        )
    else:
        run_suites(root, suites=selected, jobs=jobs)
    execution_s = time.monotonic() - started
    payload = selection_payload(selection)
    payload["execution"] = {
        "suite_invocations": len(selected),
        "result": "passed",
        "wall_time_s": round(execution_s, 3),
        "queue_wait_s": round(timing.queue_wait_s, 3) if timing else 0.0,
        "admission_class": timing.admission_class if timing else None,
    }
    return payload


def _load_batch_approvals(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid approvals JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise ValueError("approvals must be a schema 1 object")
    if not isinstance(payload.get("tickets"), list) or not payload["tickets"]:
        raise ValueError("approvals tickets must be a non-empty list")
    return payload


def validate_batch_approvals(
    root: Path,
    payload: dict[str, Any],
    base: str,
) -> tuple[dict[str, Any], ...]:
    if payload.get("frozen_base") != base:
        raise ValueError(
            "stale base: approvals frozen_base does not match requested exact base"
        )
    validated: list[dict[str, Any]] = []
    seen_tickets: set[str] = set()
    for raw in payload["tickets"]:
        if not isinstance(raw, dict):
            raise ValueError("approval rows must be objects")
        ticket_id = raw.get("ticket_id")
        candidate = raw.get("candidate_sha")
        review = raw.get("review")
        files_changed = raw.get("files_changed")
        if not isinstance(ticket_id, str) or not ticket_id.startswith("TK-"):
            raise ValueError("approval row has invalid ticket_id")
        if ticket_id in seen_tickets:
            raise ValueError(f"duplicate approved ticket: {ticket_id}")
        seen_tickets.add(ticket_id)
        if not isinstance(candidate, str):
            raise ValueError(f"{ticket_id}: missing candidate_sha")
        exact_candidate = resolve_exact_commit(root, candidate, f"{ticket_id} candidate")
        if not isinstance(review, dict) or review.get("status") != "approved":
            raise ValueError(f"{ticket_id}: missing approved review")
        reviewer = review.get("reviewer_principal_id")
        submitter = review.get("submitter_principal_id")
        if not isinstance(reviewer, str) or not reviewer.startswith("PR-"):
            raise ValueError(f"{ticket_id}: missing review identity")
        if not isinstance(submitter, str) or not submitter.startswith("PR-"):
            raise ValueError(f"{ticket_id}: missing submitter identity")
        if reviewer == submitter:
            raise ValueError(f"{ticket_id}: review is not independent")
        if review.get("candidate_sha") != exact_candidate:
            raise ValueError(f"{ticket_id}: review/SHA mismatch")
        merge_base = _git(root, ["merge-base", base, exact_candidate]).stdout.strip()
        if merge_base != base:
            raise ValueError(
                f"{ticket_id}: candidate is not based on frozen base {base}"
            )
        actual_files = sorted(
            {
                path
                for record in diff_change_records(root, base, exact_candidate)
                for path in record.paths
            }
        )
        if not isinstance(files_changed, list) or not all(
            isinstance(path, str) for path in files_changed
        ):
            raise ValueError(f"{ticket_id}: files_changed must be a string list")
        if files_changed != actual_files:
            raise ValueError(
                f"{ticket_id}: changed-file drift: claimed={files_changed}, "
                f"actual={actual_files}"
            )
        validated.append(
            {
                "ticket_id": ticket_id,
                "candidate_sha": exact_candidate,
                "files_changed": actual_files,
                "review": {
                    "status": "approved",
                    "candidate_sha": exact_candidate,
                    "reviewer_principal_id": reviewer,
                    "submitter_principal_id": submitter,
                },
            }
        )
    return tuple(validated)


def _require_clean_exact_base(root: Path, base: str, main_ref: str) -> None:
    if _git(root, ["status", "--porcelain"]).stdout:
        raise RuntimeError("approved-batch requires a clean worktree")
    head = _git(root, ["rev-parse", "--verify", "HEAD^{commit}"]).stdout.strip()
    if head != base:
        raise RuntimeError(f"approved-batch requires HEAD at frozen base: {head} != {base}")
    main = _git(root, ["rev-parse", "--verify", f"{main_ref}^{{commit}}"])
    if main.stdout.strip() != base:
        raise RuntimeError(
            f"stale base: {main_ref}={main.stdout.strip()}, frozen_base={base}"
        )


def run_approved_batch(
    root: Path,
    *,
    base: str,
    approvals_path: Path,
    output: Path,
    main_ref: str,
    jobs: int | None,
    admission_class: str,
    lease_id: str | None,
    signing_key_file: Path | None,
) -> dict[str, Any]:
    exact_base = resolve_exact_commit(root, base, "base")
    _require_clean_exact_base(root, exact_base, main_ref)
    approvals = validate_batch_approvals(
        root, _load_batch_approvals(approvals_path), exact_base
    )
    request_payload = {
        "schema": 1,
        "frozen_base": exact_base,
        "tickets": list(approvals),
    }
    batch_id = hashlib.sha256(
        json.dumps(request_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    scratch_parent = _scratch_parent()
    worktree = scratch_parent / f"pursers-fullgate-{batch_id}"
    if worktree.exists():
        raise RuntimeError(f"batch worktree already exists: {worktree}")
    _git(root, ["worktree", "add", "--detach", str(worktree), exact_base])
    aggregate_sha = exact_base
    aggregate_ref = f"refs/pursers/integration/{batch_id}"
    try:
        base_epoch = int(
            _git(root, ["show", "-s", "--format=%ct", exact_base]).stdout.strip()
        )
        for index, approval in enumerate(approvals, start=1):
            environment = os.environ.copy()
            deterministic_date = f"{base_epoch + index} +0000"
            environment.update(
                {
                    "GIT_AUTHOR_NAME": "Pursers integration gate",
                    "GIT_AUTHOR_EMAIL": "integration@pursers.invalid",
                    "GIT_COMMITTER_NAME": "Pursers integration gate",
                    "GIT_COMMITTER_EMAIL": "integration@pursers.invalid",
                    "GIT_AUTHOR_DATE": deterministic_date,
                    "GIT_COMMITTER_DATE": deterministic_date,
                }
            )
            _git(
                worktree,
                [
                    "merge",
                    "--no-ff",
                    "--no-edit",
                    approval["candidate_sha"],
                    "-m",
                    f"Integrate approved {approval['ticket_id']}",
                ],
                env=environment,
            )
        aggregate_sha = _git(
            worktree, ["rev-parse", "--verify", "HEAD^{commit}"]
        ).stdout.strip()
        validate_integration_files(worktree)
        timing = run_full_gate(
            worktree,
            jobs=jobs,
            admission_class=admission_class,
            lease_id=lease_id,
        )
        if _git(worktree, ["status", "--porcelain"]).stdout:
            raise RuntimeError("full gate changed the aggregate worktree")
        _git(root, ["update-ref", aggregate_ref, aggregate_sha])
        aggregate_files = sorted(
            {
                path
                for record in diff_change_records(root, exact_base, aggregate_sha)
                for path in record.paths
            }
        )
        result = {
            "schema": 1,
            "batch_id": batch_id,
            "frozen_base": exact_base,
            "aggregate_candidate": aggregate_sha,
            "aggregate_ref": aggregate_ref,
            "aggregate_files_changed": aggregate_files,
            "tickets": list(approvals),
            "full_gate": {
                "selected_suites": [suite.name for suite in SUITES],
                "suite_invocations": len(SUITES),
                "result": "passed",
                "admission_class": timing.admission_class,
                "queue_wait_s": round(timing.queue_wait_s, 3),
                "execution_s": round(timing.execution_s or 0.0, 3),
            },
        }
        signed = add_evidence_integrity(result, signing_key_file)
        write_json_evidence(output, signed)
        return signed
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )


def run_seat_suites(root: Path, suites: Sequence[Suite] = SUITES) -> None:
    """Run every suite for seat evidence, with release-owned gates reported apart."""
    scratch_parent = _scratch_parent()
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
    run.add_argument(
        "--admission-class",
        choices=tuple(FULL_GATE_PRIORITIES),
        default=os.environ.get(
            FULL_GATE_CLASS_ENV,
            "main" if os.environ.get("GITHUB_ACTIONS") == "true" else "background-validation",
        ),
    )
    run.add_argument(
        "--ticket-lease",
        default=os.environ.get(FULL_GATE_LEASE_ENV),
        help="active ticket lease identifier; required outside main/release gates",
    )
    affected = subparsers.add_parser(
        "affected",
        help="derive and run fail-closed suites from exact Git commit SHAs",
    )
    affected.add_argument("--base", required=True)
    affected.add_argument("--candidate", required=True)
    affected.add_argument("--output", type=Path, required=True)
    affected.add_argument("--high-risk", action="store_true")
    affected.add_argument("--jobs", type=int, default=None)
    affected.add_argument(
        "--admission-class",
        choices=tuple(FULL_GATE_PRIORITIES),
        default=os.environ.get(FULL_GATE_CLASS_ENV, "active-worker"),
    )
    affected.add_argument(
        "--ticket-lease", default=os.environ.get(FULL_GATE_LEASE_ENV)
    )
    affected.add_argument("--signing-key-file", type=Path)
    batch = subparsers.add_parser(
        "approved-batch",
        help="merge approved exact SHAs on one frozen base and run one full gate",
    )
    batch.add_argument("--base", required=True)
    batch.add_argument("--approvals", type=Path, required=True)
    batch.add_argument("--output", type=Path, required=True)
    batch.add_argument("--jobs", type=int, default=None)
    batch.add_argument(
        "--admission-class",
        choices=tuple(FULL_GATE_PRIORITIES),
        default=os.environ.get(FULL_GATE_CLASS_ENV, "release"),
    )
    batch.add_argument("--ticket-lease", default=os.environ.get(FULL_GATE_LEASE_ENV))
    batch.add_argument("--signing-key-file", type=Path)
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
        if args.command in {"run", "affected", "approved-batch", "seat-suite-report"}:
            _sweep_scratch_parent()
        require_free_space(root, configured_minimum)
        validate_manifest(root)
        if args.command not in {"affected", "approved-batch", "seat-suite-report"}:
            validate_integration_files(root)
        if args.command == "check":
            print(f"manifest covers all {len(SUITES)} test directories")
        elif args.command == "collect":
            payload = collect_counts(root)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        elif args.command == "run":
            run_full_gate(
                root,
                jobs=args.jobs,
                admission_class=args.admission_class,
                lease_id=args.ticket_lease,
            )
        elif args.command == "affected":
            selection = select_affected_suites(
                root,
                args.base,
                args.candidate,
                high_risk=args.high_risk,
            )
            payload = run_affected(
                root,
                selection,
                jobs=args.jobs,
                admission_class=args.admission_class,
                lease_id=args.ticket_lease,
            )
            write_json_evidence(
                args.output,
                add_evidence_integrity(payload, args.signing_key_file),
            )
        elif args.command == "approved-batch":
            payload = run_approved_batch(
                root,
                base=args.base,
                approvals_path=args.approvals,
                output=args.output,
                main_ref="origin/main",
                jobs=args.jobs,
                admission_class=args.admission_class,
                lease_id=args.ticket_lease,
                signing_key_file=args.signing_key_file,
            )
            print(
                "ci-manifest approved-batch passed "
                f"aggregate={payload['aggregate_candidate']} output={args.output}"
            )
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
