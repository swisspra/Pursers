"""Guarded project add/remove planning for the Fleet dashboard.

Discovery in this module is read-only.  The caller owns the actual registry,
board, clone, and credential operations and may execute them only after the
stored plan has passed the digest, expiry, actor, and confirmation checks.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PLAN_TTL_SECONDS = 600
PLAN_LIMIT = 50
GIT_TIMEOUT_SECONDS = 30
PROJECT_NAME_RE = re.compile(r"^[^\x00-\x1f/\\]{1,120}$")
BOARD_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
GIT_MODES = frozenset({"none", "existing", "clone"})


class ProjectLifecycleError(ValueError):
    """A bounded lifecycle request or plan is invalid."""


class ProjectLifecycleConflictError(RuntimeError):
    """Observed product state changed after the plan was created."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _safe_repository_url(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 2_048:
        raise ProjectLifecycleError("repository_url must be a bounded string")
    selected = value.strip()
    if not selected or any(ord(char) < 32 for char in selected):
        raise ProjectLifecycleError("repository_url is invalid")
    parsed = urlsplit(selected)
    if parsed.username is not None or parsed.password is not None:
        raise ProjectLifecycleError("repository_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ProjectLifecycleError("repository_url must not contain query or fragment data")
    if selected.startswith("-"):
        raise ProjectLifecycleError("repository_url is invalid")
    return selected


def _run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        env=env,
    )


def inspect_project_source(
    *,
    work_dir: Any,
    git_mode: Any,
    repository_url: Any = None,
    integration_ref: Any = "main",
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run_git,
) -> dict[str, Any]:
    """Return a secret-free, read-only observation of a proposed project source."""
    if not isinstance(work_dir, str) or not os.path.isabs(work_dir):
        raise ProjectLifecycleError("work_dir must be an absolute path")
    if git_mode not in GIT_MODES:
        raise ProjectLifecycleError("git_mode must be none, existing, or clone")
    if (
        not isinstance(integration_ref, str)
        or not integration_ref.strip()
        or integration_ref.startswith("-")
        or len(integration_ref) > 255
    ):
        raise ProjectLifecycleError("integration_ref must be a valid git reference")
    ref = integration_ref.strip()
    repository = _safe_repository_url(repository_url)
    path = Path(work_dir).expanduser().resolve()
    exists = path.exists() or path.is_symlink()
    observation: dict[str, Any] = {
        "path": str(path),
        "git_mode": git_mode,
        "exists": exists,
        "kind": "missing",
        "clean": None,
        "repository_url": repository,
        "integration_ref": ref,
        "blocked": False,
        "blockers": [],
    }

    if exists:
        if path.is_symlink():
            observation["kind"] = "symlink"
            observation["blockers"].append("work_dir must not be a symbolic link")
        elif not path.is_dir():
            observation["kind"] = "not_directory"
            observation["blockers"].append("work_dir must be a directory")
        else:
            try:
                observation["kind"] = "empty_directory" if not any(path.iterdir()) else "directory"
            except OSError:
                observation["kind"] = "unreadable"
                observation["blockers"].append("work_dir could not be inspected")

    if git_mode == "none":
        if not exists:
            observation["blockers"].append("non-Git mode requires an existing folder")
    elif git_mode == "clone":
        if repository is None:
            observation["blockers"].append("clone mode requires repository_url")
        if exists and observation["kind"] != "empty_directory":
            observation["blockers"].append(
                "clone target must be absent or an existing empty directory"
            )
    elif not exists or observation["kind"] not in {"directory", "empty_directory"}:
        observation["blockers"].append("existing Git mode requires a readable folder")
    else:
        inside = runner(path, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            observation["blockers"].append("work_dir is not a Git checkout")
        else:
            status = runner(path, "status", "--porcelain")
            if status.returncode != 0:
                observation["blockers"].append("Git worktree status could not be read")
            else:
                observation["clean"] = not bool(status.stdout.strip())
                if not observation["clean"]:
                    observation["blockers"].append(
                        "Git worktree is dirty; inspect it before creating a new plan"
                    )
            origin = runner(path, "remote", "get-url", "origin")
            observed_origin = None
            if origin.returncode == 0:
                try:
                    observed_origin = _safe_repository_url(origin.stdout.strip())
                except ProjectLifecycleError:
                    observation["blockers"].append(
                        "checkout origin contains data that cannot enter a browser plan"
                    )
            observation["observed_origin"] = observed_origin
            if repository is not None and observed_origin != repository:
                observation["blockers"].append(
                    "repository_url differs from the checkout origin"
                )
            ref_check = runner(path, "rev-parse", "--verify", f"{ref}^{{commit}}")
            if ref_check.returncode != 0:
                observation["blockers"].append(
                    "integration_ref is not available in the existing checkout"
                )

    observation["blocked"] = bool(observation["blockers"])
    observation["digest"] = _digest(
        {key: value for key, value in observation.items() if key != "digest"}
    )
    return observation


def _validate_project_identity(name: Any, board_id: Any) -> tuple[str, str]:
    if not isinstance(name, str) or not PROJECT_NAME_RE.fullmatch(name.strip()):
        raise ProjectLifecycleError("name must be a safe non-empty project name")
    if not isinstance(board_id, str) or not BOARD_ID_RE.fullmatch(board_id):
        raise ProjectLifecycleError("board_id is invalid")
    return name.strip(), board_id


def _canonical_registry_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize persisted defaults before an idempotence comparison."""
    canonical = copy.deepcopy(dict(entry))
    if canonical.get("integration_ref", "main") == "main":
        canonical.pop("integration_ref", None)
    return canonical


def build_add_plan(
    *,
    request: Mapping[str, Any],
    registry: Mapping[str, Any],
    registry_expected_sha256: str,
    source: Mapping[str, Any],
    board_exists: bool,
    actor: str,
    central: str,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    name, board_id = _validate_project_identity(request.get("name"), request.get("board_id"))
    git_mode = source["git_mode"]
    prepare_clone = bool(request.get("prepare_fleet_clone", git_mode != "none"))
    blockers = list(source.get("blockers") or [])
    if git_mode == "none" and prepare_clone:
        blockers.append("a Fleet clone requires a Git-backed project")
    projects = registry.get("projects")
    if not isinstance(projects, Mapping):
        raise ProjectLifecycleError("project registry has no projects mapping")
    existing = projects.get(name)
    if name in projects and not isinstance(existing, Mapping):
        raise ProjectLifecycleError("existing project registry entry is invalid")
    proposed_entry: dict[str, Any] = {
        "board_id": board_id,
        "work_dir": source["path"],
        "status": "active",
    }
    repository = source.get("repository_url") or source.get("observed_origin")
    if repository:
        proposed_entry["repository_url"] = repository
    if source.get("integration_ref") != "main":
        proposed_entry["integration_ref"] = source["integration_ref"]
    if isinstance(existing, Mapping) and existing.get("fleet_clone_dir"):
        proposed_entry["fleet_clone_dir"] = existing["fleet_clone_dir"]

    existing_entry = copy.deepcopy(dict(existing)) if isinstance(existing, Mapping) else None
    exact_rerun = (
        existing_entry is not None
        and _canonical_registry_entry(existing_entry)
        == _canonical_registry_entry(proposed_entry)
    )
    changed_fields: list[str] = []
    if existing_entry is not None and not exact_rerun:
        missing = object()
        changed_fields = sorted(
            key
            for key in set(existing_entry) | set(proposed_entry)
            if existing_entry.get(key, missing) != proposed_entry.get(key, missing)
        )
        blockers.append(
            "project name is already registered with different settings; "
            "choose a unique name or remove the existing project through the guarded flow"
        )

    registry_operation = {
        "operation_id": "registry",
        "effect": (
            "already_present"
            if exact_rerun
            else "blocked_name_collision"
            if existing_entry is not None
            else "create"
        ),
        "target": "project_registry",
        "before": existing_entry,
        "after": copy.deepcopy(proposed_entry),
        "required_permission": f"admin on registry board and {board_id}",
    }
    if changed_fields:
        registry_operation["changed_fields"] = changed_fields

    operations = [
        registry_operation,
        {
            "operation_id": "board",
            "effect": "verify" if board_exists else "create",
            "target": board_id,
            "required_permission": "board administrator",
        },
        {
            "operation_id": "door-principals-and-policy",
            "effect": "reconcile",
            "target": board_id,
            "required_permission": "board administrator and local protected-door access",
        },
    ]
    if git_mode == "clone":
        operations.insert(
            0,
            {
                "operation_id": "source-clone",
                "effect": "create_new_checkout",
                "target": source["path"],
                "required_permission": "local folder write and Git source read",
            },
        )
    if prepare_clone:
        operations.append(
            {
                "operation_id": "fleet-clone",
                "effect": "prepare",
                "target": "isolated Fleet clone",
                "required_permission": "local Fleet state write and Git source read",
            }
        )

    timestamp = created_at or _now()
    return {
        "schema_version": 1,
        "kind": "project-add",
        "created_at": _iso(timestamp),
        "expires_at": _iso(timestamp + timedelta(seconds=PLAN_TTL_SECONDS)),
        "actor": actor,
        "central": central,
        "registry_expected_sha256": registry_expected_sha256,
        "project": name,
        "board_id": board_id,
        "source": copy.deepcopy(dict(source)),
        "prepare_fleet_clone": prepare_clone,
        "proposed_entry": proposed_entry,
        "operations": operations,
        "rollback": [
            "Re-add or restore the prior registry entry with a fresh CAS plan.",
            "Boards, histories, folders, repositories, clones, and credentials are preserved.",
        ],
        "warnings": [
            "Apply never pushes Git or rewrites an existing checkout.",
            "Protected door values remain outside this plan and are returned only by the compatibility flow when newly issued.",
        ],
        "confirmation": name,
        "blocked": bool(blockers),
        "blockers": blockers,
    }


def build_remove_plan(
    *,
    request: Mapping[str, Any],
    registry: Mapping[str, Any],
    registry_expected_sha256: str,
    board_observation: Mapping[str, Any],
    actor: str,
    central: str,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    name = request.get("name")
    if not isinstance(name, str) or not PROJECT_NAME_RE.fullmatch(name.strip()):
        raise ProjectLifecycleError("name must be a safe non-empty project name")
    name = name.strip()
    projects = registry.get("projects")
    if not isinstance(projects, Mapping) or not isinstance(projects.get(name), Mapping):
        raise ProjectLifecycleError("project is not registered")
    existing = copy.deepcopy(dict(projects[name]))
    board_id = existing.get("board_id")
    if not isinstance(board_id, str) or not BOARD_ID_RE.fullmatch(board_id):
        raise ProjectLifecycleError("project board_id is invalid")

    blockers: list[str] = []
    if existing.get("status") != "paused":
        blockers.append("project must be paused before removal")
    if not board_observation.get("complete"):
        blockers.append("complete board and registry observations are required")
    active = list(board_observation.get("active_tickets") or [])
    offers = list(board_observation.get("pending_offers") or [])
    if active:
        blockers.append("active work or review must finish before removal")
    if offers:
        blockers.append("pending work offers must clear before removal")

    timestamp = created_at or _now()
    return {
        "schema_version": 1,
        "kind": "project-remove",
        "created_at": _iso(timestamp),
        "expires_at": _iso(timestamp + timedelta(seconds=PLAN_TTL_SECONDS)),
        "actor": actor,
        "central": central,
        "registry_expected_sha256": registry_expected_sha256,
        "project": name,
        "board_id": board_id,
        "existing_entry": existing,
        "board_observation": copy.deepcopy(dict(board_observation)),
        "operations": [
            {
                "operation_id": "registry-remove",
                "effect": "remove_reference",
                "target": "project_registry",
                "before": existing,
                "after": None,
                "required_permission": "registry board administrator",
            }
        ],
        "rollback": [
            "Re-add the preserved registry entry with a fresh CAS plan.",
        ],
        "preserved": [
            "Central board and durable board history",
            "project folder, repository, worktrees, and Fleet clone",
            "seats, token files, keys, JWKS, and shared credentials",
            "tickets, journals, logs, and backups",
        ],
        "warnings": [
            "Removal only deletes the CAS-protected project_registry reference.",
            "Seat retirement and protected-door rotation are separate operations.",
        ],
        "confirmation": name,
        "blocked": bool(blockers),
        "blockers": blockers,
    }


class ProjectLifecycleStore:
    """Bounded in-memory store for immutable, actor-bound lifecycle plans."""

    def __init__(self, *, limit: int = PLAN_LIMIT) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._plans: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def add(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        stored = copy.deepcopy(dict(plan))
        stored["plan_id"] = secrets.token_urlsafe(24)
        stored["observed_digest"] = _digest(
            {
                "registry_expected_sha256": stored.get("registry_expected_sha256"),
                "source": stored.get("source"),
                "board_observation": stored.get("board_observation"),
            }
        )
        stored["plan_digest"] = _digest(
            {key: value for key, value in stored.items() if key != "plan_digest"}
        )
        stored["state"] = "planned"
        with self._lock:
            self._plans[stored["plan_id"]] = stored
            self._plans.move_to_end(stored["plan_id"])
            while len(self._plans) > self.limit:
                self._plans.popitem(last=False)
        return copy.deepcopy(stored)

    def get(self, plan_id: Any, *, actor: str, central: str) -> dict[str, Any]:
        if not isinstance(plan_id, str):
            raise ProjectLifecycleError("plan_id is required")
        with self._lock:
            plan = copy.deepcopy(self._plans.get(plan_id))
        if plan is None or plan.get("actor") != actor or plan.get("central") != central:
            raise KeyError(plan_id)
        return plan

    def reserve(
        self,
        plan_id: Any,
        *,
        actor: str,
        central: str,
        plan_digest: Any,
        confirmation: Any,
        now: datetime | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        current = now or _now()
        with self._lock:
            plan = self._plans.get(plan_id) if isinstance(plan_id, str) else None
            if plan is None or plan.get("actor") != actor or plan.get("central") != central:
                raise KeyError(str(plan_id))
            if plan.get("plan_digest") != plan_digest:
                raise ProjectLifecycleConflictError("plan digest does not match")
            if confirmation != plan.get("confirmation"):
                raise ProjectLifecycleError("typed confirmation does not match project name")
            if plan.get("blocked"):
                raise ProjectLifecycleConflictError("blocked plan cannot be applied")
            if plan.get("state") == "applied":
                return copy.deepcopy(plan), copy.deepcopy(plan.get("receipt"))
            expires = datetime.fromisoformat(str(plan["expires_at"]).replace("Z", "+00:00"))
            if current >= expires:
                raise ProjectLifecycleConflictError("plan expired; create a new preview")
            if plan.get("state") != "planned":
                raise ProjectLifecycleConflictError("plan is already being applied")
            plan["state"] = "applying"
            return copy.deepcopy(plan), None

    def complete(self, plan_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise KeyError(plan_id)
            plan["state"] = "applied"
            plan["receipt"] = copy.deepcopy(dict(receipt))
            return copy.deepcopy(plan["receipt"])

    def fail(self, plan_id: str, reason: str) -> None:
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is not None:
                plan["state"] = "failed"
                plan["failure"] = str(reason)[:240]


def clone_project_source(plan: Mapping[str, Any]) -> None:
    """Create the planned new checkout; never overwrites or cleans a path."""
    source = plan.get("source")
    if not isinstance(source, Mapping) or source.get("git_mode") != "clone":
        return
    path = Path(str(source["path"]))
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise ProjectLifecycleConflictError(
                "clone target changed after preview; create a new plan"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    command = [
        "git",
        "clone",
        "--branch",
        str(source["integration_ref"]),
        "--single-branch",
        "--origin",
        "origin",
        "--",
        str(source["repository_url"]),
        str(path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        raise ProjectLifecycleConflictError(
            "Git source clone failed; the target was preserved for inspection"
        )
