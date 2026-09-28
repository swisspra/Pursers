#!/usr/bin/env python3
"""Read-only inventory and post-rollout checks for Pursers v5.0.6."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "pursers_rollout_doctor_v1"
RELEASE_TAG = "v5.0.6"
EXPECTED_PRODUCT = "5.0.6"
EXPECTED_WAIT_BRIDGE = "0.1.3"
EXPECTED_CLIENT = "0.1.5"
EXPECTED_PACKAGES = {
    "product": "5.0.6",
    "client": "0.1.5",
    "wait_bridge": "0.1.3",
    "central": "0.1.4",
    "acp": "0.1.4",
    "import": "5.0.0",
}
RUNTIME_REQUIRED_DISTRIBUTIONS = {
    "pursers-central": EXPECTED_PACKAGES["central"],
    "pursers-client": EXPECTED_CLIENT,
    "pursers-wait-bridge": EXPECTED_WAIT_BRIDGE,
}
SERVICE_REQUIRED_DISTRIBUTIONS = {"pursers-client": EXPECTED_CLIENT}
RUNTIME_NAMES = re.compile(r"^(?:registry-main|review(?:-main)?)-[A-Za-z0-9._-]+$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LAUNCHD_LABELS = (
    "com.pursers.fleet-dashboard",
    "com.pursers.coordinator",
    "com.pursers.board-butler",
    "com.pursers.mong1-supervisor",
)


def _json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _run(command: Sequence[str], *, cwd: Path | None = None) -> str | None:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _git_sha(repo: Path) -> str | None:
    value = _run(("git", "rev-parse", "--verify", "HEAD^{commit}"), cwd=repo)
    return value if value is not None and FULL_SHA.fullmatch(value) else None


def _git_clean(repo: Path) -> bool | None:
    status = _git_status(repo)
    return None if status is None else not status


def _git_status(repo: Path) -> list[str] | None:
    try:
        completed = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.splitlines() if completed.returncode == 0 else None


def _git_diff_sha256(repo: Path) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "diff", "--binary", "--no-ext-diff"),
            cwd=repo,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    return hashlib.sha256(completed.stdout).hexdigest()


def _release_versions(repo: Path) -> dict[str, str]:
    path = repo / "tools/release_versions.toml"
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    packages = document.get("packages")
    result = {"product": str(document.get("product", ""))}
    if isinstance(packages, Mapping):
        for name, value in packages.items():
            if isinstance(name, str) and isinstance(value, str):
                result[name] = value
    return result


def _safe_path(path: Path, home: Path, pursers_home: Path) -> str:
    rendered = str(path)
    for root, label in ((pursers_home, "<PURSERS_HOME>"), (home, "<HOME>")):
        base = str(root)
        if rendered == base:
            return label
        if rendered.startswith(base + os.sep):
            return label + rendered[len(base) :]
    return rendered


def _metadata_version(tool_root: Path, distribution: str) -> str | None:
    normalized = distribution.replace("-", "_").lower()
    for metadata in sorted(tool_root.glob("lib/python*/site-packages/*.dist-info/METADATA")):
        try:
            parsed = Parser().parsestr(metadata.read_text(encoding="utf-8"))
        except OSError:
            continue
        name = (parsed.get("Name") or "").replace("-", "_").lower()
        if name == normalized:
            return parsed.get("Version")
    return None


def _python_environment(
    interpreter: Path | None,
    required: Mapping[str, str],
    *,
    home: Path,
    pursers_home: Path,
) -> dict[str, Any]:
    environment = (
        interpreter.parent.parent
        if interpreter is not None and interpreter.parent.name == "bin"
        else None
    )
    installed = {
        distribution: (
            _metadata_version(environment, distribution)
            if environment is not None
            else None
        )
        for distribution in required
    }
    present = interpreter is not None and interpreter.is_file()
    return {
        "interpreter": (
            _safe_path(interpreter, home, pursers_home)
            if interpreter is not None
            else None
        ),
        "present": present,
        "required_versions": dict(required),
        "installed_versions": installed,
        "ok": present
        and all(installed[name] == version for name, version in required.items()),
    }


def _uv_tool(home: Path) -> dict[str, Any]:
    root = home / ".local/share/uv/tools/pursers-wait-bridge"
    receipt = root / "uv-receipt.toml"
    find_links: list[str] = []
    requirement = None
    try:
        value = tomllib.loads(receipt.read_text(encoding="utf-8"))
        tool = value.get("tool", {})
        requirements = tool.get("requirements", [])
        if isinstance(requirements, list) and requirements:
            first = requirements[0]
            if isinstance(first, Mapping):
                requirement = f"{first.get('name', '')}{first.get('specifier', '')}"
        options = tool.get("options", {})
        raw_links = options.get("find-links", []) if isinstance(options, Mapping) else []
        if isinstance(raw_links, list):
            find_links = [str(item) for item in raw_links]
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return {
        "consumer": "uv-tool:pursers-wait-bridge",
        "present": receipt.is_file(),
        "requirement": requirement,
        "wait_bridge_version": _metadata_version(root, "pursers-wait-bridge"),
        "client_version": _metadata_version(root, "pursers-client"),
        "find_links_release": next(
            (
                part
                for link in find_links
                for part in link.rstrip("/").split("/")
                if part.startswith("v5.")
            ),
            None,
        ),
    }


def _repo_record(
    consumer: str,
    repo: Path,
    *,
    home: Path,
    pursers_home: Path,
    referenced: bool,
    interpreter: Path | None = None,
    required_distributions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    status = _git_status(repo)
    record = {
        "consumer": consumer,
        "path": _safe_path(repo, home, pursers_home),
        "present": repo.is_dir(),
        "sha": _git_sha(repo),
        "clean": None if status is None else not status,
        "dirty_paths": status,
        "worktree_diff_sha256": _git_diff_sha256(repo),
        "versions": _release_versions(repo),
        "referenced": referenced,
    }
    if required_distributions is not None:
        record["python_environment"] = _python_environment(
            interpreter,
            required_distributions,
            home=home,
            pursers_home=pursers_home,
        )
    return record


def _recursive_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _recursive_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _recursive_strings(nested)


def _mcp_clients(
    home: Path, pursers_home: Path
) -> tuple[list[dict[str, Any]], list[str]]:
    candidates = (
        ("Claude Desktop", home / "Library/Application Support/Claude/claude_desktop_config.json"),
        (
            "Claude Desktop 3P",
            home / "Library/Application Support/Claude-3p/claude_desktop_config.json",
        ),
        ("Zed", home / ".config/zed/settings.json"),
        ("Zed macOS", home / "Library/Application Support/Zed/settings.json"),
    )
    rows: list[dict[str, Any]] = []
    references: list[str] = []
    for name, path in candidates:
        value = _json(path)
        strings = list(_recursive_strings(value)) if value is not None else []
        matches = [item for item in strings if "pursers-wait-bridge" in item]
        references.extend(strings)
        if path.exists() or matches:
            rows.append(
                {
                    "consumer": f"host-mcp:{name}",
                    "config": _safe_path(path, home, pursers_home),
                    "present": path.is_file(),
                    "launches_wait_bridge": bool(matches),
                    "commands": [
                        _safe_path(Path(item), home, pursers_home) for item in matches
                    ],
                }
            )
    return rows, references


def _launchd(
    labels: Sequence[str], *, snapshot_root: Path | None
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rows: list[dict[str, Any]] = []
    references: dict[str, str] = {}
    uid = os.getuid()
    for label in labels:
        if snapshot_root is None:
            text = _run(("/bin/launchctl", "print", f"gui/{uid}/{label}"))
        else:
            path = snapshot_root / "launchd" / f"{label}.txt"
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = None
        if text:
            references[label] = text
        pid_match = re.search(r"(?m)^\s*pid\s*=\s*(\d+)\s*$", text or "")
        state_match = re.search(r"(?m)^\s*state\s*=\s*([A-Za-z]+)\s*$", text or "")
        rows.append(
            {
                "consumer": f"launchd:{label}",
                "loaded": text is not None,
                "running": bool(pid_match) or (state_match and state_match.group(1) == "running"),
            }
        )
    return rows, references


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _wheel_checksums(wheel_dir: Path, sums_path: Path) -> bool:
    try:
        rows = sums_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    seen: set[str] = set()
    for row in rows:
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/]+\.whl)", row)
        if match is None or match.group(2) in seen:
            return False
        expected, name = match.groups()
        seen.add(name)
        if _file_sha256(wheel_dir / name) != expected:
            return False
    return bool(seen) and {path.name for path in wheel_dir.glob("*.whl")} == seen


def _butler_conflict_guard(repo: Path) -> bool:
    try:
        source = (repo / "tools/board-butler/board_butler.py").read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    return (
        source.count("_is_state_precondition_conflict") >= 3
        and source.count("await process_question(") >= 2
        and "deferred question after state precondition conflict" in source
        and "deferred pending question after state precondition conflict" in source
    )


def _entrypoint(text: str) -> Path | None:
    candidates: list[Path] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("program = "):
            line = line.removeprefix("program = ")
        if not line.startswith("/") or " => " in line:
            continue
        candidate = Path(line)
        if candidate.is_file() and candidate.suffix in {".py", ".sh"}:
            candidates.append(candidate)
    return candidates[0] if candidates else None


def _launchd_interpreter(label: str, text: str) -> Path | None:
    preferred_keys = {
        "com.pursers.fleet-dashboard": "PURSERS_FLEET_PYTHON",
        "com.pursers.board-butler": "PURSERS_BUTLER_PYTHON",
        "com.pursers.coordinator": "PURSERS_COORDINATOR_PYTHON",
    }
    preferred = preferred_keys.get(label)
    if preferred is not None:
        match = re.search(
            rf"(?m)^\s*{re.escape(preferred)}\s*(?:=>|=)\s*(/\S+)\s*$",
            text,
        )
        if match is not None:
            return Path(match.group(1))
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("program = "):
            candidate = Path(line.removeprefix("program = "))
            if candidate.name.startswith("python"):
                return candidate
    return None


def _attach_launchd_sources(
    rows: list[dict[str, Any]],
    references: Mapping[str, str],
    sources: Sequence[tuple[Path, Mapping[str, Any]]],
    *,
    home: Path,
    pursers_home: Path,
) -> None:
    for row in rows:
        label = row["consumer"].removeprefix("launchd:")
        text = references.get(label, "")
        matches = sorted(
            [
                (path, source)
                for path, source in sources
                if str(path.resolve()) in text
                or str(path.parent.resolve()) in text
            ],
            key=lambda item: str(item[0]),
        )
        row["sources"] = [
            {
                "path": source["path"],
                "sha": source["sha"],
                "version": source.get("versions", {}).get("product"),
            }
            for _path, source in matches
        ]
        entrypoint = _entrypoint(text)
        row["entrypoint"] = (
            _safe_path(entrypoint, home, pursers_home) if entrypoint else None
        )
        row["entrypoint_sha256"] = _file_sha256(entrypoint) if entrypoint else None
        if label != "com.pursers.mong1-supervisor":
            row["python_environment"] = _python_environment(
                _launchd_interpreter(label, text),
                SERVICE_REQUIRED_DISTRIBUTIONS,
                home=home,
                pursers_home=pursers_home,
            )


def _process_text(snapshot_root: Path | None) -> str:
    if snapshot_root is not None:
        try:
            return (snapshot_root / "processes.txt").read_text(encoding="utf-8")
        except OSError:
            return ""
    return _run(("/bin/ps", "-axo", "command=")) or ""


def _referenced(path: Path, references: Sequence[str]) -> bool:
    absolute = str(path.resolve())
    return any(absolute in item for item in references)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _check(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def _proof(proof_dir: Path, name: str) -> Mapping[str, Any] | None:
    return _json(proof_dir / f"{name}.json")


def _post_rollout_checks(
    proof_dir: Path,
    *,
    release_sha: str,
    now: datetime,
    fresh_seconds: int,
) -> dict[str, dict[str, str]]:
    checks: dict[str, dict[str, str]] = {}
    coordinator = _proof(proof_dir, "coordinator-digest")
    event_at = _parse_time(coordinator.get("last_event_at")) if coordinator else None
    fresh = (
        event_at is not None
        and 0
        <= (now - event_at.astimezone(timezone.utc)).total_seconds()
        <= fresh_seconds
    )
    checks["coordinator_digest_subscription"] = _check(
        "PASS" if coordinator and coordinator.get("connected") is True and fresh else "FAIL",
        "connected=true with fresh last_event_at"
        if fresh
        else "missing, disconnected, or stale coordinator digest proof",
    )
    for role in ("worker", "reviewer"):
        value = _proof(proof_dir, f"wait-{role}")
        modes = value.get("mode_by_board") if value else None
        push = value is not None and (
            value.get("mode") == "push"
            or (isinstance(modes, Mapping) and bool(modes) and set(modes.values()) == {"push"})
        )
        checks[f"wait_bridge_push_{role}"] = _check(
            "PASS" if push else "FAIL",
            "saved positive-cursor wait used push transport"
            if push
            else f"missing push proof for {role}",
        )
    butler = _proof(proof_dir, "board-butler")
    capabilities = butler.get("capabilities", []) if butler else []
    fleet = butler.get("fleet") if butler else None
    merged = isinstance(capabilities, list) and any(
        item in {"approved_merge", "pr-review-merge", "TK-ee3d61fd"}
        for item in capabilities
    )
    autonomous = butler is not None and butler.get("effective_state") == "autonomous"
    fleet_reconciled = isinstance(fleet, Mapping) and fleet.get("status") == "reconciled"
    checks["board_butler_autonomous_merge"] = _check(
        "PASS" if autonomous and merged else "FAIL",
        "autonomous mode includes approved merge"
        if autonomous and merged
        else "missing autonomous/approved-merge proof",
    )
    checks["board_butler_fleet_reconciled"] = _check(
        "PASS" if fleet_reconciled else "FAIL",
        "new post-restart refresh reports fleet status reconciled"
        if fleet_reconciled
        else "fleet status is not reconciled",
    )
    conflict = butler.get("state_precondition_conflict") if butler else None
    conflict_survived = (
        isinstance(conflict, Mapping)
        and conflict.get("ticket_id") == "TK-164fb22b"
        and conflict.get("release_sha") == release_sha
        and conflict.get("post_restart_refresh_seen") is True
        and conflict.get("state_precondition_traceback") is False
        and conflict.get("evidence_kind") == "live-runtime"
    )
    checks["board_butler_precondition_conflict_survival"] = _check(
        "PASS" if conflict_survived else "FAIL",
        "post-restart refreshes continued without a state-precondition traceback"
        if conflict_survived
        else "missing release-bound post-restart refresh/no-traceback proof",
    )
    dashboard = _proof(proof_dir, "dashboard")
    shell = dashboard.get("visual_shell") if dashboard else None
    dashboard_ok = (
        dashboard is not None
        and dashboard.get("release_sha") == release_sha
        and shell == "warm-guided-home-v1"
    )
    checks["dashboard_visual_shell"] = _check(
        "PASS" if dashboard_ok else "FAIL",
        "release SHA serves warm-guided-home-v1"
        if dashboard_ok
        else "dashboard proof does not bind the release SHA and visual shell",
    )
    release = _proof(proof_dir, "release-checks")
    release_ok = (
        release is not None
        and release.get("release_sha") == release_sha
        and release.get("ci_manifest") == "pass"
        and release.get("release_train") == "pass"
    )
    checks["release_tag_checks"] = _check(
        "PASS" if release_ok else "FAIL",
        "ci_manifest and release_train passed at the release SHA"
        if release_ok
        else "release-tag ci_manifest/release_train proof is missing or mismatched",
    )
    return checks


def inspect(args: argparse.Namespace) -> dict[str, Any]:
    home = args.home.resolve()
    pursers_home = args.pursers_home.resolve()
    snapshot_root = args.snapshot_root.resolve() if args.snapshot_root else None
    proof_dir = (
        args.proof_dir.resolve()
        if args.proof_dir
        else pursers_home / "rollout/v5.0.6/proofs"
    )
    mcp_rows, mcp_references = _mcp_clients(home, pursers_home)
    launchd_rows, launchd_references = _launchd(LAUNCHD_LABELS, snapshot_root=snapshot_root)
    process_text = _process_text(snapshot_root)
    references = [*mcp_references, *launchd_references.values(), process_text]

    repositories: list[dict[str, Any]] = []
    runtime_dirs: list[Path] = []
    root = pursers_home / "runtimes"
    if root.is_dir():
        runtime_dirs = sorted(
            path for path in root.iterdir() if path.is_dir() and RUNTIME_NAMES.fullmatch(path.name)
        )
    for runtime in runtime_dirs:
        repo = runtime / "src"
        repositories.append(
            _repo_record(
                f"runtime:{runtime.name}",
                repo,
                home=home,
                pursers_home=pursers_home,
                referenced=_referenced(runtime, references),
                interpreter=runtime / ".venv/bin/python",
                required_distributions=RUNTIME_REQUIRED_DISTRIBUTIONS,
            )
        )
    fixed_repositories = (
        ("runtime:fleet-dashboard+board-butler", pursers_home / "runtimes/fleet-dashboard/repo"),
        ("runtime:coordinator", pursers_home / "coordinator/src"),
    )
    for name, repo in fixed_repositories:
        repositories.append(
            _repo_record(
                name,
                repo,
                home=home,
                pursers_home=pursers_home,
                referenced=_referenced(repo.parent, references),
            )
        )

    repo_sources = [
        (runtime / "src", repositories[index])
        for index, runtime in enumerate(runtime_dirs)
    ]
    repo_sources.extend(
        (repo, repositories[len(runtime_dirs) + offset])
        for offset, (_name, repo) in enumerate(fixed_repositories)
    )
    _attach_launchd_sources(
        launchd_rows,
        launchd_references,
        repo_sources,
        home=home,
        pursers_home=pursers_home,
    )

    uv_tool = _uv_tool(home)
    for row in mcp_rows:
        row["wait_bridge_version"] = uv_tool["wait_bridge_version"]
        row["client_version"] = uv_tool["client_version"]
    checks: dict[str, dict[str, str]] = {}
    uv_ok = (
        uv_tool["wait_bridge_version"] == EXPECTED_WAIT_BRIDGE
        and uv_tool["client_version"] == EXPECTED_CLIENT
        and uv_tool["find_links_release"] == RELEASE_TAG
    )
    checks["uv_wait_bridge"] = _check(
        "PASS" if uv_ok else "FAIL",
        f"expected wait-bridge {EXPECTED_WAIT_BRIDGE}, client {EXPECTED_CLIENT}, "
        f"find-links {RELEASE_TAG}",
    )
    butler_proof = _proof(proof_dir, "board-butler")
    hotfix = butler_proof.get("hotfix") if butler_proof else None
    for row in repositories:
        required = row["consumer"] in {
            "runtime:fleet-dashboard+board-butler",
            "runtime:coordinator",
        } or row["referenced"]
        if not required:
            continue
        carried_hotfix = (
            row["consumer"] == "runtime:fleet-dashboard+board-butler"
            and isinstance(hotfix, Mapping)
            and hotfix.get("ticket_id") == "TK-164fb22b"
            and hotfix.get("carried_forward") is True
            and hotfix.get("release_sha") == args.release_sha
            and SHA256.fullmatch(str(hotfix.get("patch_sha256", ""))) is not None
            and hotfix.get("patch_sha256") == row["worktree_diff_sha256"]
            and row["dirty_paths"] == [" M tools/board-butler/board_butler.py"]
        )
        good = row["sha"] == args.release_sha and (
            row["clean"] is True or carried_hotfix
        )
        checks[row["consumer"]] = _check(
            "PASS" if good else "FAIL",
            "release SHA with exact TK-164fb22b carry-forward patch"
            if carried_hotfix
            else "clean checkout at release SHA"
            if good
            else "active checkout missing, dirty, or at the wrong SHA",
        )
        environment = row.get("python_environment")
        if isinstance(environment, Mapping):
            checks[f'{row["consumer"]}:installed_packages'] = _check(
                "PASS" if environment.get("ok") is True else "FAIL",
                "active interpreter has the required installed distributions"
                if environment.get("ok") is True
                else "active interpreter is missing or has mismatched installed distributions",
            )
    version_sources = [
        row["versions"] for row in repositories if row["sha"] == args.release_sha
    ]
    versions_ok = bool(version_sources) and all(
        all(versions.get(key) == value for key, value in EXPECTED_PACKAGES.items())
        for versions in version_sources
    )
    checks["release_version_manifest"] = _check(
        "PASS" if versions_ok else "FAIL",
        "release_versions.toml matches the approved package map"
        if versions_ok
        else "release package versions are missing or mismatched",
    )
    fleet_repo = pursers_home / "runtimes/fleet-dashboard/repo"
    checks["board_butler_conflict_guard_source"] = _check(
        "PASS" if _butler_conflict_guard(fleet_repo) else "FAIL",
        "running Butler source guards both process_question call sites"
        if _butler_conflict_guard(fleet_repo)
        else "running Butler source lacks the two conflict-deferral guards",
    )
    release_inputs_ok = (
        args.release_tag == RELEASE_TAG
        and args.sha256s.resolve().parent == args.wheel_dir.resolve()
        and _wheel_checksums(args.wheel_dir.resolve(), args.sha256s.resolve())
    )
    checks["release_wheel_inputs"] = _check(
        "PASS" if release_inputs_ok else "FAIL",
        "release tag and complete SHA256SUMS wheel cohort match"
        if release_inputs_ok
        else "release tag, wheel directory, or SHA256SUMS validation failed",
    )
    for row in launchd_rows:
        source_shas = {source["sha"] for source in row["sources"]}
        sources_ok = bool(source_shas) and source_shas == {args.release_sha}
        if row["consumer"] == "launchd:com.pursers.mong1-supervisor":
            sources_ok = sources_ok or row["entrypoint_sha256"] is not None
        checks[row["consumer"]] = _check(
            "PASS" if row["loaded"] and row["running"] and sources_ok else "FAIL",
            "loaded, running, and bound to verified source"
            if row["loaded"] and row["running"] and sources_ok
            else "not loaded/running or source SHA is unresolved/mismatched",
        )
        environment = row.get("python_environment")
        if isinstance(environment, Mapping):
            checks[f'{row["consumer"]}:installed_packages'] = _check(
                "PASS" if environment.get("ok") is True else "FAIL",
                "configured service interpreter has pursers-client "
                f"{EXPECTED_CLIENT}"
                if environment.get("ok") is True
                else "configured service interpreter is missing or pursers-client is mismatched",
            )
    configured_hosts = {row["consumer"] for row in mcp_rows if row["launches_wait_bridge"]}
    for name in ("host-mcp:Claude Desktop", "host-mcp:Zed"):
        alternatives = (
            {"host-mcp:Claude Desktop", "host-mcp:Claude Desktop 3P"}
            if name.endswith("Claude Desktop")
            else {"host-mcp:Zed", "host-mcp:Zed macOS"}
        )
        checks[name] = _check(
            "PASS" if configured_hosts.intersection(alternatives) else "FAIL",
            "launches pursers-wait-bridge"
            if configured_hosts.intersection(alternatives)
            else "wait bridge command not found",
        )
    checks.update(
        _post_rollout_checks(
            proof_dir,
            release_sha=args.release_sha,
            now=args.now,
            fresh_seconds=args.fresh_seconds,
        )
    )
    if args.inventory_only:
        checks = {}

    cleanup: list[dict[str, Any]] = []
    by_consumer = {row["consumer"]: row for row in repositories}
    for runtime in runtime_dirs:
        row = by_consumer[f"runtime:{runtime.name}"]
        shape_ok = (runtime / "src/.git").exists() and (runtime / ".venv").is_dir()
        safe = (
            shape_ok
            and not runtime.is_symlink()
            and row["clean"] is True
            and not row["referenced"]
            and row["sha"] != args.release_sha
        )
        cleanup.append(
            {
                "runtime": _safe_path(runtime, home, pursers_home),
                "sha": row["sha"],
                "referenced": row["referenced"],
                "clean": row["clean"],
                "safe_to_delete": safe,
                "action": "list only; deletion is never performed by this doctor",
            }
        )
    failures = sum(item["status"] == "FAIL" for item in checks.values())
    return {
        "schema": SCHEMA,
        "read_only": True,
        "mode": "inventory" if args.inventory_only else "verify",
        "release": {
            "tag": RELEASE_TAG,
            "sha": args.release_sha,
            "product": EXPECTED_PRODUCT,
            "wait_bridge": EXPECTED_WAIT_BRIDGE,
            "client": EXPECTED_CLIENT,
        },
        "inventory": [uv_tool, *repositories, *launchd_rows, *mcp_rows],
        "stale_runtime_cleanup": cleanup,
        "checks": checks,
        "summary": {
            "pass": sum(item["status"] == "PASS" for item in checks.values()),
            "warn": sum(item["status"] == "WARN" for item in checks.values()),
            "fail": failures,
            "ok": failures == 0,
        },
    }


def _utc(value: str) -> datetime:
    parsed = _parse_time(value)
    if parsed is None:
        raise argparse.ArgumentTypeError("--now must be an ISO-8601 timestamp with timezone")
    return parsed.astimezone(timezone.utc)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--sha256s", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--pursers-home", type=Path)
    parser.add_argument("--proof-dir", type=Path)
    parser.add_argument("--snapshot-root", type=Path)
    parser.add_argument("--fresh-seconds", type=int, default=300)
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="report consumers and stale-runtime candidates without enforcing rollout checks",
    )
    parser.add_argument("--now", type=_utc, default=datetime.now(timezone.utc))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not FULL_SHA.fullmatch(args.release_sha):
        parser.error("--release-sha must be a full lowercase 40-hex commit SHA")
    if args.fresh_seconds < 1:
        parser.error("--fresh-seconds must be positive")
    if args.pursers_home is None:
        args.pursers_home = args.home / ".pursers"
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = inspect(args)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if result["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
