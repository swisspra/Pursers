"""Read-only bounded evidence from an exact registered fleet ticket workspace."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import runpy
import subprocess
from typing import Any, Mapping

MAX_DIFF_BYTES = 256 * 1024
_CONTEXT_API: Mapping[str, Any] | None = None
_RUNTIME_DIRECTORIES = frozenset({
    ".operator-context", ".pursers-diagnostics", ".ticket-runtime-diagnostic",
    ".pursers", ".pytest_cache", "__pycache__",
})


def _git_read(worktree: Path, *arguments: str) -> bytes:
    return subprocess.run([
        "git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
        "-C", str(worktree), *arguments,
    ], env=_git_environment(), check=True, capture_output=True, timeout=5).stdout


def _runtime_path(path: Path) -> bool:
    return any(part in _RUNTIME_DIRECTORIES or part.startswith(".ticket-runtime-")
               for part in path.parts)


def _event_context_api() -> Mapping[str, Any]:
    global _CONTEXT_API
    if _CONTEXT_API is None:
        seat_module = Path(__file__).resolve().parents[1] / "seat-kit" / "event_seat.py"
        _CONTEXT_API = runpy.run_path(str(seat_module))
    return _CONTEXT_API


def _git_environment() -> dict[str, str]:
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1", "GIT_EXTERNAL_DIFF": ""}


def _source_diff(worktree: Path) -> dict[str, Any]:
    """Hash tracked changes and bounded untracked regular files without exposing content."""
    tracked = _git_read(worktree, "diff", "--no-ext-diff", "--no-textconv",
                        "--no-renames", "--binary", "HEAD", "--")
    status = _git_read(worktree, "status", "--porcelain=v1",
                       "--untracked-files=no", "--ignore-submodules=all")
    untracked = _git_read(worktree, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    if len(tracked) > MAX_DIFF_BYTES or len(status) > MAX_DIFF_BYTES:
        raise ValueError("owned workspace diff exceeds bounded evidence limit")
    digest = hashlib.sha256(tracked + b"\0" + status)
    file_bytes = len(tracked) + len(status)
    names = [line[3:].decode("utf-8", "replace")[:240]
             for line in status.splitlines() if len(line) >= 4]
    for raw_path in untracked:
        if not raw_path:
            continue
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Git returned an unsafe untracked path")
        if _runtime_path(relative):
            continue
        target = worktree / relative
        names.append(str(relative)[:240])
        if target.is_symlink() or not target.is_file():
            digest.update(b"unsafe-untracked\0" + raw_path)
            continue
        info = target.stat(follow_symlinks=False)
        if info.st_size > MAX_DIFF_BYTES or file_bytes + info.st_size > MAX_DIFF_BYTES:
            raise ValueError("owned untracked evidence exceeds bounded evidence limit")
        if not target.resolve(strict=True).is_relative_to(worktree.resolve(strict=True)):
            raise ValueError("Untracked source leaves the registered workspace")
        content = target.read_bytes()
        file_bytes += len(content)
        digest.update(b"untracked\0" + raw_path + b"\0" + content)
    return {"digest_sha256": digest.hexdigest(), "entry_count": len(names),
            "changed_paths": names[:32], "paths_omitted": max(0, len(names) - 32)}


def _runner_checkpoint(_context: Mapping[str, Any], _ticket_id: str) -> dict[str, Any]:
    # The runner's private state path is not present in the project registry;
    # never infer it from process layout, ticket prose, or an arbitrary path.
    return {"status": "unavailable", "failure_class": None, "checkpoint_ref": None}


def context_facts(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Map trusted facts to the existing flat, primitive recovery context contract."""
    checkpoint = evidence.get("runner_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
    result = {
        "workspace_status": evidence.get("status", "unverified"),
        "workspace_ref": evidence.get("workspace_ref"),
        "workspace_path": evidence.get("workspace_path"),
        "workspace_branch": evidence.get("branch"),
        "workspace_expected_branch": evidence.get("expected_branch"),
        "workspace_final_validation_status": evidence.get("final_validation_status"),
        "workspace_head": evidence.get("head"),
        "workspace_source_progress": evidence.get("source_progress"),
        "workspace_source_progress_status": evidence.get("source_progress_status", "unavailable"),
        "workspace_dirty_entries": evidence.get("preserved_dirty_entries"),
        "workspace_diagnostics_boundary": evidence.get("diagnostics_boundary"),
        "workspace_diagnostics_root": evidence.get("diagnostics_root"),
        "runner_checkpoint_status": checkpoint.get("status", "unavailable"),
        "runner_failure_class": checkpoint.get("failure_class"),
        "runner_checkpoint_ref": checkpoint.get("checkpoint_ref"),
        "workspace_error_class": evidence.get("error_class"),
    }
    paths = evidence.get("changed_paths")
    if isinstance(paths, list) and len(paths) <= 32 and all(isinstance(p, str) for p in paths):
        result["workspace_changed_paths"] = paths
    actions = evidence.get("allowed_actions")
    if isinstance(actions, list):
        result["workspace_allowed_actions"] = actions
    return result


def project_workspace_evidence(*, registry: Mapping[str, Any], board_id: str,
                               project_id: str, ticket: Mapping[str, Any]) -> dict[str, Any]:
    """Return bounded authoritative facts; any route/worktree ambiguity fails closed."""
    ticket_id = str(ticket.get("ticket_id", ""))[:160]
    evidence: dict[str, Any] = {
        "status": "unverified", "board_id": board_id, "project_id": project_id,
        "ticket_id": ticket_id, "workspace_ref": None, "branch": None, "head": None,
        "workspace_path": None, "diagnostics_root": None, "allowed_actions": [],
        "source_progress": None, "source_progress_status": "unavailable", "preserved_dirty_entries": None,
        "changed_paths": [], "paths_omitted": 0,
        "diagnostics_boundary": None, "runner_checkpoint": None, "error_class": None,
    }
    try:
        project = registry.get("projects", {}).get(project_id)
        if (not isinstance(project, Mapping) or project.get("board_id") != board_id
                or project.get("status") != "active" or project.get("fleet", True) is not True
                or project.get("work_dir_owner") != "fleet"):
            raise ValueError("project is not registered as active fleet-owned work")
        clone_value = project.get("fleet_clone_dir")
        if not isinstance(clone_value, str) or not Path(clone_value).is_absolute():
            raise ValueError("registered protected clone is missing")
        clone = Path(clone_value)
        if clone.is_symlink() or clone.resolve(strict=True) != clone.absolute():
            raise ValueError("registered protected clone path is unsafe")
        api = _event_context_api()
        context = api["resolve_event_context"](
            registry, board_id, ticket, str(clone.parent), "butler", create_if_missing=False)
        if not context or context.get("project") != project_id or context.get("board_id") != board_id:
            raise ValueError("ticket has no exact registered workspace")
        worktree = Path(context["worktree"])
        if context.get("workspace_state") not in {"clean_preserved", "dirty_preserved"}:
            raise ValueError("ticket workspace is not verified")
        diagnostics = worktree / ".pursers-diagnostics"
        if diagnostics.is_symlink() or not diagnostics.resolve(strict=False).is_relative_to(worktree):
            raise ValueError("Diagnostics boundary leaves the registered workspace")
        try:
            diff = _source_diff(worktree)
        except ValueError:
            # A bounded content-hash limit does not invalidate independent
            # route/common-dir/branch ownership. Keep authority and expose the
            # missing progress evidence explicitly; never invent progress or
            # replenish retry budgets from a large diagnostics artifact.
            diff = {"digest_sha256": None, "entry_count": None,
                    "changed_paths": [], "paths_omitted": 0}
        evidence.update(status="verified", workspace_ref="registered_ticket_worktree",
                        workspace_path=str(worktree), diagnostics_root=str(diagnostics),
                        expected_branch=context.get("expected_branch"),
                        final_validation_status=("recoverable_detached_exact_combined_sha"
                            if context.get("final_validation") and not context.get("branch")
                            and context.get("final_qa_branch_recoverable") is True
                            and context.get("head") == context["final_validation"]["combined_revision"]
                            else "attached_exact_combined_sha" if context.get("final_validation")
                            else None),
                        allowed_actions=["prepare_owned_diagnostics",
                            "inspect_owned_runner_checkpoint",
                            "include_independently_accepted_dependency_in_owned_branch"],
                        branch=context.get("branch"),
                        head=_git_read(worktree, "rev-parse", "HEAD").decode().strip(),
                        source_progress=diff["digest_sha256"],
                        source_progress_status="available" if diff["digest_sha256"] else "unavailable",
                        preserved_dirty_entries=diff["entry_count"],
                        changed_paths=diff["changed_paths"], paths_omitted=diff["paths_omitted"],
                        diagnostics_boundary="registered_ticket_workspace_children_only",
                        runner_checkpoint=_runner_checkpoint(context, ticket_id))
    except Exception as exc:
        evidence["error_class"] = type(exc).__name__
    return evidence
