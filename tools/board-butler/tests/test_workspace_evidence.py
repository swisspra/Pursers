from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess


MODULE = Path(__file__).resolve().parents[1] / "workspace_evidence.py"
spec = importlib.util.spec_from_file_location("workspace_evidence_under_test", MODULE)
assert spec and spec.loader
workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def fixture(tmp_path: Path, clone_parent: str = "clones"):
    root = tmp_path / "fleet" / clone_parent
    clone = root / "sonar"
    clone.mkdir(parents=True)
    git(clone, "init", "-q")
    git(clone, "config", "user.name", "Test User")
    git(clone, "config", "user.email", "test@example.invalid")
    (clone / "tracked.txt").write_text("base\n")
    git(clone, "add", "tracked.txt")
    git(clone, "commit", "-m", "base")
    base = git(clone, "rev-parse", "HEAD")
    git(clone, "update-ref", "refs/remotes/origin/main", base)
    git(clone, "remote", "add", "origin", "https://example.invalid/team/sonar.git")
    branch = "pursers/TK-evidence-1234"
    worktree = root.parent / "worktrees" / "sonar" / "TK-evidence-1234"
    git(clone, "worktree", "add", "-b", branch, str(worktree), base)
    registry = {"schema_version": 1, "projects": {"Sonar": {
        "board_id": "pursers", "status": "active", "fleet": True,
        "work_dir_owner": "fleet", "work_dir": str(clone),
        "fleet_clone_dir": str(clone),
        "repository_url": "https://example.invalid/team/sonar.git",
    }}}
    ticket = {"ticket_id": "TK-evidence-1234", "target_url": "https://example.invalid/team/sonar.git",
              "required_fields": ["branch_and_commit"], "tags": ["production-code"]}
    return root, clone, worktree, registry, ticket, base


def test_projection_uses_exact_existing_workspace_and_hashes_owned_progress(tmp_path):
    _root, clone, worktree, registry, ticket, base = fixture(tmp_path)
    (worktree / "tracked.txt").write_text("owned edit\n")
    (worktree / "untracked.txt").write_text("preserve me\n")
    before = git(clone, "worktree", "list", "--porcelain")

    evidence = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)

    assert evidence["status"] == "verified"
    assert evidence["branch"] == "pursers/TK-evidence-1234"
    assert evidence["head"] == base
    assert evidence["preserved_dirty_entries"] == 2
    assert evidence["source_progress"]
    assert evidence["runner_checkpoint"]["status"] == "unavailable"
    assert git(clone, "worktree", "list", "--porcelain") == before
    assert (worktree / "tracked.txt").read_text() == "owned edit\n"
    assert (worktree / "untracked.txt").read_text() == "preserve me\n"

    first = evidence["source_progress"]
    (worktree / "tracked.txt").write_text("different owned progress\n")
    changed = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)
    assert changed["source_progress"] != first


def test_projection_missing_worktree_fails_closed_without_provision(tmp_path):
    root, clone, worktree, registry, ticket, _base = fixture(tmp_path)
    git(clone, "worktree", "remove", "--force", str(worktree))
    evidence = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)
    assert evidence["status"] == "unverified"
    assert evidence["error_class"] == "EventContextError"
    assert not worktree.exists()
    assert not (root.parent / "worktrees" / "sonar").exists() or not list(
        (root.parent / "worktrees" / "sonar").iterdir())


def test_projection_rejects_foreign_route_and_symlink_worktree(tmp_path):
    root, _clone, worktree, registry, ticket, _base = fixture(tmp_path)
    foreign = {**registry, "projects": {"Sonar": {**registry["projects"]["Sonar"],
               "board_id": "other"}}}
    assert workspace.project_workspace_evidence(
        registry=foreign, board_id="pursers", project_id="Sonar", ticket=ticket)["status"] == "unverified"
    external = tmp_path / "outside"
    external.mkdir()
    worktree.rename(external / "actual")
    worktree.symlink_to(external / "actual", target_is_directory=True)
    evidence = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)
    assert evidence["status"] == "unverified"
    assert "symlink" in str(evidence["error_class"]).lower() or evidence["error_class"]
    assert (external / "actual").is_dir()


def test_registered_coordinator_directory_does_not_replace_fleet_clone(tmp_path):
    _root, clone, worktree, registry, ticket, _base = fixture(tmp_path)
    registry["projects"]["Sonar"]["work_dir"] = str(tmp_path / "project-metadata")
    before = git(clone, "worktree", "list", "--porcelain")
    evidence = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)
    assert evidence["status"] == "verified"
    assert evidence["workspace_path"] == str(worktree.resolve())
    assert evidence["diagnostics_root"] == str(worktree / ".pursers-diagnostics")
    assert "prepare_owned_diagnostics" in evidence["allowed_actions"]
    assert git(clone, "worktree", "list", "--porcelain") == before


def test_registered_clone_parent_need_not_have_a_magic_name(tmp_path):
    _root, _clone, _worktree, registry, ticket, _base = fixture(tmp_path, "repositories")
    evidence = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)
    assert evidence["status"] == "verified"


def test_diagnostic_artifacts_are_not_owned_source_progress(tmp_path):
    _root, _clone, worktree, registry, ticket, _base = fixture(tmp_path)
    first = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)
    diagnostics = worktree / ".pursers-diagnostics"
    diagnostics.mkdir()
    (diagnostics / "run.log").write_text("heartbeat 1\n")
    second = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)
    assert second["source_progress"] == first["source_progress"]
    (worktree / "new_source.py").write_text("answer = 42\n")
    third = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)
    assert third["source_progress"] != first["source_progress"]


def test_diagnostics_boundary_rejects_existing_escape_link(tmp_path):
    _root, _clone, worktree, registry, ticket, _base = fixture(tmp_path)
    outside = tmp_path / "private-files"
    outside.mkdir()
    (worktree / ".pursers-diagnostics").symlink_to(outside, target_is_directory=True)
    evidence = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)
    assert evidence["status"] == "unverified"
    assert not evidence.get("allowed_actions")


def test_readonly_projection_never_executes_git_fsmonitor(tmp_path):
    _root, clone, _worktree, registry, ticket, _base = fixture(tmp_path)
    marker = tmp_path / "hook-executed"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text("#!/bin/sh\nprintf executed > '" + str(marker) + "'\n")
    hook.chmod(0o755)
    git(clone, "config", "core.fsmonitor", str(hook))
    evidence = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)
    assert evidence["status"] == "verified"
    assert not marker.exists()


def test_large_untracked_artifact_does_not_erase_verified_workspace_authority(tmp_path):
    _root, _clone, worktree, registry, ticket, _base = fixture(tmp_path)
    (worktree / "large-artifact.log").write_bytes(b"x" * (workspace.MAX_DIFF_BYTES + 1))
    evidence = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id="Sonar", ticket=ticket)
    assert evidence["status"] == "verified"
    assert evidence["workspace_path"] == str(worktree)
    assert evidence["source_progress"] is None
    assert evidence["source_progress_status"] == "unavailable"
    assert "prepare_owned_diagnostics" in evidence["allowed_actions"]


def test_readonly_recovery_identifies_clean_detached_final_qa_without_mutation(tmp_path):
    import sys
    root_repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root_repo / "packages" / "client" / "src"))
    from pursers_client.project_workflow import (
        final_validation_packet, final_validation_ticket_id, parse_project_workflow,
        policy_digest,
    )

    _root, clone, _unused, registry, _ordinary, base = fixture(tmp_path)
    seed = tmp_path / "seed"
    git(clone, "worktree", "add", "-b", "pursers/member", str(seed), base)
    (seed / "combined.txt").write_text("combined source\n")
    git(seed, "add", "combined.txt")
    git(seed, "commit", "-m", "combined")
    combined = git(seed, "rev-parse", "HEAD")
    project = "Sonar"
    policy = parse_project_workflow({
        "schema_version": 1, "workflow_id": "workflow:sonar", "project_id": project,
        "board_id": "pursers", "cohort_id": "cohort:sonar", "sealed": True,
        "nodes": [{"node_id": "foundation", "ticket_id": "TK-foundation", "requires": [],
                   "acceptance_checks": ["review exact source"]}],
        "final_validation": {"title": "Final QA", "acceptance_checks": ["combined checks"]},
    }, project_id=project, board_id="pursers")
    activation = {"schema_version": 1, "state": "active",
                  "policy_sha256": policy_digest(policy), "activation_id": "active:one"}
    row = registry["projects"][project]
    row.update(butler_workflow=policy, butler_workflow_activation=activation)
    ticket_id = final_validation_ticket_id(policy)
    deterministic_branch = f"pursers/{ticket_id}"
    git(clone, "branch", deterministic_branch, base)
    description, pages = final_validation_packet(policy, {
        "ticket_id": ticket_id, "combined_revision": combined,
        "members": {"TK-foundation": "a" * 40}, "acceptance_checks": ["combined checks"],
    }, delivery={"cohort_id": policy["cohort_id"], "combined_revision": combined})
    ticket = {"ticket_id": ticket_id, "target_url": "Sonar", "description": description,
        "required_fields": ["branch_and_commit", "test_output"],
        "annotations": [{"kind": "evidence", "text": page} for page in pages],
        "annotations_omitted_count": 0, "annotation_count": len(pages)}
    worktree = tmp_path / "fleet" / "worktrees" / "sonar" / ticket_id
    git(clone, "worktree", "add", "--detach", str(worktree), combined)
    before = git(clone, "worktree", "list", "--porcelain")

    evidence = workspace.project_workspace_evidence(
        registry=registry, board_id="pursers", project_id=project, ticket=ticket)

    assert evidence["status"] == "verified"
    assert evidence["branch"] == ""
    assert evidence["expected_branch"] == f"pursers/{ticket_id}"
    assert evidence["final_validation_status"] == "recoverable_detached_exact_combined_sha"
    assert evidence["head"] == combined and combined != base
    assert git(worktree, "branch", "--show-current") == ""
    assert git(clone, "worktree", "list", "--porcelain") == before
    assert git(clone, "rev-parse", deterministic_branch) == base
    assert git(clone, "rev-parse", "refs/remotes/origin/main") == base
