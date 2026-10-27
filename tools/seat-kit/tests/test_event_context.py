import subprocess
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "client" / "src"))
from pursers_client.project_workflow import (  # noqa: E402
    final_validation_packet, final_validation_ticket_id, parse_project_workflow,
    parse_workflow_activation, policy_digest,
)

from test_event_seat import api


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def repository_fixture(tmp_path):
    root = tmp_path / 'fleet' / 'clones'
    clone = root / 'sonar'
    clone.mkdir(parents=True)
    git(clone, 'init', '-q')
    git(clone, 'config', 'user.name', 'Test User')
    git(clone, 'config', 'user.email', 'test@example.invalid')
    (clone / 'tracked.txt').write_text('base\n')
    git(clone, 'add', 'tracked.txt')
    git(clone, 'commit', '-m', 'base')
    base = git(clone, 'rev-parse', 'HEAD')
    git(clone, 'update-ref', 'refs/remotes/origin/main', base)
    git(clone, 'remote', 'add', 'origin', 'https://example.invalid/team/sonar.git')
    project = {'status': 'active', 'fleet': True, 'board_id': 'pursers',
               'work_dir': str(clone), 'fleet_clone_dir': str(clone),
               'repository_url': 'https://example.invalid/team/sonar.git',
               'integration_ref': 'main'}
    registry = {'schema_version': 1, 'projects': {'Sonar': project}}
    return root, clone, registry, base


def code_ticket(ticket_id='TK-context-1234'):
    return {'ticket_id': ticket_id, 'target_url': 'https://example.invalid/team/sonar.git',
            'required_fields': ['branch_and_commit'], 'tags': ['production-code']}


def custom_evidence_ticket(ticket_id='TK-custom-evidence-1234'):
    return {'ticket_id': ticket_id, 'target_url': 'https://example.invalid/team/sonar.git',
            'required_fields': ['base_and_head_sha', 'accepted_core_revision',
                                'independent_review_evidence'], 'tags': ['interactive']}


def final_qa_policy(project):
    return {
        "schema_version": 1, "workflow_id": "workflow:sonar", "project_id": project,
        "board_id": "pursers", "cohort_id": "cohort:sonar", "sealed": True,
        "nodes": [{"node_id": "foundation", "ticket_id": "TK-foundation", "requires": [],
                   "acceptance_checks": ["exact candidate reviewed"]}],
        "final_validation": {"title": "Final QA", "acceptance_checks": ["exact combined checks"]},
    }


def final_qa_fixture(registry, project, combined_sha):
    normalized = parse_project_workflow(final_qa_policy(project), project_id=project, board_id="pursers")
    activation = {"schema_version": 1, "state": "active",
                  "policy_sha256": policy_digest(normalized), "activation_id": "activation:one"}
    registry["projects"][project]["butler_workflow"] = normalized
    registry["projects"][project]["butler_workflow_activation"] = activation
    ticket_id = final_validation_ticket_id(normalized)
    description, pages = final_validation_packet(normalized, {
        "ticket_id": ticket_id, "combined_revision": combined_sha,
        "members": {"TK-foundation": "a" * 40}, "acceptance_checks": ["exact combined checks"],
    }, delivery={"cohort_id": normalized["cohort_id"], "combined_revision": combined_sha})
    return {"ticket_id": ticket_id, "target_url": registry["projects"][project]["repository_url"],
            "required_fields": ["branch_and_commit", "test_output"], "description": description,
            "annotations": [{"kind": "evidence", "text": page} for page in pages],
            "annotations_omitted_count": 0, "annotation_count": len(pages),
            "tags": ["final-validation"]}


def test_ordinary_worker_detached_workspace_stays_rejected(tmp_path):
    module = api()
    root, clone, registry, base = repository_fixture(tmp_path)
    ticket = code_ticket("TK-detached-1234")
    workspace = tmp_path / "fleet" / "worktrees" / "sonar" / ticket["ticket_id"]
    git(clone, "worktree", "add", "--detach", str(workspace), base)
    with pytest.raises(module["EventContextError"]) as error:
        module["resolve_event_context"](registry, "pursers", ticket, str(root), "worker-a")
    assert error.value.code == "workspace_branch_mismatch"
    assert git(workspace, "branch", "--show-current") == ""


def test_custom_source_contract_provisions_worker_workspace_without_code_tags(tmp_path):
    module = api()
    root, clone, registry, base = repository_fixture(tmp_path)
    ticket = custom_evidence_ticket()

    context = module['resolve_event_context'](registry, 'pursers', ticket, str(root), 'worker-a')

    workspace = Path(context['worktree'])
    assert workspace == tmp_path / 'fleet' / 'worktrees' / 'sonar' / ticket['ticket_id']
    assert context['branch'] == f"pursers/{ticket['ticket_id']}"
    assert context['workspace_state'] == 'clean_preserved'
    assert git(workspace, 'rev-parse', 'HEAD') == base
    assert git(clone, 'status', '--porcelain') == ''


def test_final_qa_workspace_provisions_from_combined_sha_not_origin_main(tmp_path):
    module = api()
    root, clone, registry, base = repository_fixture(tmp_path)
    seed = tmp_path / "seed"
    git(clone, "worktree", "add", "-b", "pursers/accepted-foundation", str(seed), base)
    (seed / "combined.txt").write_text("accepted combined source\n")
    git(seed, "add", "combined.txt")
    git(seed, "commit", "-m", "combined accepted candidate")
    combined = git(seed, "rev-parse", "HEAD")
    ticket = final_qa_fixture(registry, "Sonar", combined)
    ticket_id = ticket["ticket_id"]

    context = module["resolve_event_context"](registry, "pursers", ticket, str(root), "worker-a")

    assert context["branch"] == f"pursers/{ticket_id}"
    assert git(context["worktree"], "rev-parse", "HEAD") == combined
    assert git(context["worktree"], "rev-parse", "HEAD") != base
    assert context["final_validation"] == {"project_id": "Sonar", "combined_revision": combined}


def test_final_qa_clean_detached_legacy_attaches_only_for_authorized_exact_sha(tmp_path):
    module = api()
    root, clone, registry, base = repository_fixture(tmp_path)
    seed = tmp_path / "seed"
    git(clone, "worktree", "add", "-b", "pursers/accepted-foundation", str(seed), base)
    (seed / "combined.txt").write_text("accepted combined source\n")
    git(seed, "add", "combined.txt")
    git(seed, "commit", "-m", "combined accepted candidate")
    combined = git(seed, "rev-parse", "HEAD")
    ticket = final_qa_fixture(registry, "Sonar", combined)
    ticket_id = ticket["ticket_id"]
    legacy = root.parent / "worktrees" / "sonar" / ticket_id
    git(clone, "worktree", "add", "--detach", str(legacy), combined)
    before = git(clone, "worktree", "list", "--porcelain")

    readonly = module["resolve_event_context"](
        registry, "pursers", ticket, str(root), "butler", create_if_missing=False)
    assert readonly["final_validation"]["combined_revision"] == combined
    assert readonly["branch"] == ""
    assert readonly["expected_branch"] == f"pursers/{ticket_id}"
    assert git(legacy, "branch", "--show-current") == ""
    assert git(clone, "worktree", "list", "--porcelain") == before

    resumed = module["resolve_event_context"](registry, "pursers", ticket, str(root), "worker-a")
    assert resumed["branch"] == f"pursers/{ticket_id}"
    assert git(legacy, "branch", "--show-current") == f"pursers/{ticket_id}"
    assert git(legacy, "rev-parse", "HEAD") == combined


@pytest.mark.parametrize('branch_at_combined', [False, True])
def test_final_qa_detached_workspace_fast_forwards_only_safe_unused_ticket_branch(
        tmp_path, branch_at_combined):
    module = api()
    root, clone, registry, base = repository_fixture(tmp_path)
    seed = tmp_path / 'seed'
    git(clone, 'worktree', 'add', '-b', 'pursers/accepted-foundation', str(seed), base)
    (seed / 'combined.txt').write_text('combined source\n')
    git(seed, 'add', 'combined.txt')
    git(seed, 'commit', '-m', 'combined')
    combined = git(seed, 'rev-parse', 'HEAD')
    ticket = final_qa_fixture(registry, 'Sonar', combined)
    ticket_id = ticket['ticket_id']
    branch = f'pursers/{ticket_id}'
    branch_base = combined if branch_at_combined else base
    git(clone, 'branch', branch, branch_base)
    legacy = root.parent / 'worktrees' / 'sonar' / ticket_id
    git(clone, 'worktree', 'add', '--detach', str(legacy), combined)
    before = git(clone, 'worktree', 'list', '--porcelain')
    main_before = git(clone, 'rev-parse', 'refs/remotes/origin/main')

    readonly = module['resolve_event_context'](
        registry, 'pursers', ticket, str(root), 'butler', create_if_missing=False)
    assert readonly['branch'] == ''
    assert git(clone, 'worktree', 'list', '--porcelain') == before
    assert git(clone, 'rev-parse', branch) == branch_base
    assert git(clone, 'rev-parse', 'refs/remotes/origin/main') == main_before
    assert git(legacy, 'rev-parse', 'HEAD') == combined

    resumed = module['resolve_event_context'](registry, 'pursers', ticket, str(root), 'worker-a')
    assert resumed['branch'] == branch
    assert git(legacy, 'branch', '--show-current') == branch
    assert git(legacy, 'rev-parse', 'HEAD') == combined
    assert git(clone, 'rev-parse', branch) == combined
    assert git(clone, 'rev-parse', 'refs/remotes/origin/main') == main_before


def test_final_qa_ticket_branch_attached_elsewhere_is_preserved_and_refused(tmp_path):
    module = api()
    root, clone, registry, base = repository_fixture(tmp_path)
    seed = tmp_path / 'seed'
    git(clone, 'worktree', 'add', '-b', 'pursers/accepted-foundation', str(seed), base)
    (seed / 'combined.txt').write_text('combined source\n')
    git(seed, 'add', 'combined.txt')
    git(seed, 'commit', '-m', 'combined')
    combined = git(seed, 'rev-parse', 'HEAD')
    ticket = final_qa_fixture(registry, 'Sonar', combined)
    ticket_id = ticket['ticket_id']
    branch = f'pursers/{ticket_id}'
    attached = tmp_path / 'other-worktree'
    git(clone, 'worktree', 'add', '-b', branch, str(attached), base)
    legacy = root.parent / 'worktrees' / 'sonar' / ticket_id
    git(clone, 'worktree', 'add', '--detach', str(legacy), combined)
    before = git(clone, 'worktree', 'list', '--porcelain')

    with pytest.raises(module['EventContextError']) as error:
        module['resolve_event_context'](registry, 'pursers', ticket, str(root), 'worker-a')

    assert error.value.code == 'workspace_branch_mismatch'
    assert git(clone, 'worktree', 'list', '--porcelain') == before
    assert git(attached, 'rev-parse', 'HEAD') == base
    assert git(clone, 'rev-parse', branch) == base
    assert git(legacy, 'branch', '--show-current') == ''
    assert git(legacy, 'rev-parse', 'HEAD') == combined


def test_final_qa_fast_forward_compare_and_swap_rejects_changed_ticket_branch(tmp_path, monkeypatch):
    module = api()
    root, clone, registry, base = repository_fixture(tmp_path)
    seed = tmp_path / 'seed'
    git(clone, 'worktree', 'add', '-b', 'pursers/accepted-foundation', str(seed), base)
    (seed / 'combined.txt').write_text('combined source\n')
    git(seed, 'add', 'combined.txt')
    git(seed, 'commit', '-m', 'combined')
    combined = git(seed, 'rev-parse', 'HEAD')
    ticket = final_qa_fixture(registry, 'Sonar', combined)
    branch = f"pursers/{ticket['ticket_id']}"
    git(clone, 'branch', branch, base)
    raced = tmp_path / 'raced'
    git(clone, 'worktree', 'add', '-b', 'pursers/race', str(raced), base)
    (raced / 'tracked.txt').write_text('concurrent divergent update\n')
    git(raced, 'commit', '-am', 'concurrent ref update')
    raced_tip = git(raced, 'rev-parse', 'HEAD')
    git(clone, 'worktree', 'remove', str(raced))
    legacy = root.parent / 'worktrees' / 'sonar' / ticket['ticket_id']
    git(clone, 'worktree', 'add', '--detach', str(legacy), combined)
    original_git = module['_git']
    injected = False

    def race_ref_update(repository, *arguments):
        nonlocal injected
        if not injected and arguments[:1] == ('update-ref',):
            injected = True
            git(clone, 'update-ref', f'refs/heads/{branch}', raced_tip, base)
        return original_git(repository, *arguments)

    monkeypatch.setitem(module['resolve_event_context'].__globals__, '_git', race_ref_update)
    with pytest.raises(module['EventContextError']) as error:
        module['resolve_event_context'](registry, 'pursers', ticket, str(root), 'worker-a')

    assert injected
    assert error.value.code == 'workspace_candidate_mismatch'
    assert git(clone, 'rev-parse', branch) == raced_tip
    assert git(clone, 'rev-parse', 'refs/remotes/origin/main') == base
    assert git(legacy, 'branch', '--show-current') == ''
    assert git(legacy, 'rev-parse', 'HEAD') == combined


def test_final_qa_existing_unregistered_divergent_ticket_branch_is_not_reused(tmp_path):
    module = api()
    root, clone, registry, base = repository_fixture(tmp_path)
    seed = tmp_path / "seed"
    git(clone, "worktree", "add", "-b", "pursers/accepted-foundation", str(seed), base)
    (seed / "combined.txt").write_text("combined source\n")
    git(seed, "add", "combined.txt")
    git(seed, "commit", "-m", "combined")
    combined = git(seed, "rev-parse", "HEAD")
    ticket = final_qa_fixture(registry, "Sonar", combined)
    ticket_id = ticket["ticket_id"]
    branch = f"pursers/{ticket_id}"
    divergent_worktree = tmp_path / "divergent"
    git(clone, "worktree", "add", "-b", branch, str(divergent_worktree), base)
    (divergent_worktree / "tracked.txt").write_text("divergent history\n")
    git(divergent_worktree, "commit", "-am", "divergent ticket branch")
    divergent = git(divergent_worktree, "rev-parse", "HEAD")
    git(clone, "worktree", "remove", str(divergent_worktree))
    worktree = tmp_path / "fleet" / "worktrees" / "sonar" / ticket_id
    with pytest.raises(module["EventContextError"]) as error:
        module["resolve_event_context"](registry, "pursers", ticket, str(root), "worker-a")
    assert error.value.code == "workspace_candidate_mismatch"
    assert git(clone, "rev-parse", branch) == divergent
    assert not worktree.exists()


def test_final_qa_wrong_sha_dirty_and_unactivated_detached_workspaces_fail_closed(tmp_path):
    module = api()
    for failure in ("wrong-sha", "dirty", "unactivated"):
        case = tmp_path / failure
        case.mkdir()
        root, clone, registry, base = repository_fixture(case)
        seed = case / "seed"
        git(clone, "worktree", "add", "-b", f"pursers/accepted-{failure}", str(seed), base)
        (seed / "combined.txt").write_text("combined\n")
        git(seed, "add", "combined.txt")
        git(seed, "commit", "-m", "combined")
        combined = git(seed, "rev-parse", "HEAD")
        ticket = final_qa_fixture(registry, "Sonar", combined)
        ticket_id = ticket["ticket_id"]
        if failure == "unactivated":
            registry["projects"]["Sonar"]["butler_workflow_activation"] = None
        actual = base if failure == "wrong-sha" else combined
        legacy = root.parent / "worktrees" / "sonar" / ticket_id
        git(clone, "worktree", "add", "--detach", str(legacy), actual)
        if failure == "dirty":
            (legacy / "tracked.txt").write_text("dirty and preserved\n")
        before = git(clone, "worktree", "list", "--porcelain")
        with pytest.raises(module["EventContextError"]):
            module["resolve_event_context"](registry, "pursers", ticket, str(root), "worker-a")
        assert git(clone, "worktree", "list", "--porcelain") == before
        if failure == "dirty":
            assert (legacy / "tracked.txt").read_text() == "dirty and preserved\n"


def test_final_qa_foreign_and_ambiguous_worktrees_refused_without_cleanup(tmp_path):
    module = api()
    root, clone, registry, base = repository_fixture(tmp_path)
    seed = tmp_path / "seed"
    git(clone, "worktree", "add", "-b", "pursers/accepted-foundation", str(seed), base)
    (seed / "combined.txt").write_text("combined\n")
    git(seed, "add", "combined.txt")
    git(seed, "commit", "-m", "combined")
    combined = git(seed, "rev-parse", "HEAD")
    ticket = final_qa_fixture(registry, "Sonar", combined)
    ticket_id = ticket["ticket_id"]
    second = root.parent / "worktrees" / "sonar" / ticket_id
    git(clone, "worktree", "add", "--detach", str(second), combined)
    legacy = clone / ".worktrees" / ticket_id
    legacy.parent.mkdir()
    git(clone, "worktree", "add", "--detach", str(legacy), combined)
    before = git(clone, "worktree", "list", "--porcelain")
    with pytest.raises(module["EventContextError"]) as error:
        module["resolve_event_context"](registry, "pursers", ticket, str(root), "worker-a")
    assert error.value.code == "workspace_ambiguous"
    assert git(clone, "worktree", "list", "--porcelain") == before


def test_custom_source_contract_resumes_exact_legacy_workspace_and_ignores_decoys(tmp_path):
    module = api()
    root, clone, registry, _base = repository_fixture(tmp_path)
    ticket = custom_evidence_ticket()
    legacy = clone / '.worktrees' / ticket['ticket_id']
    legacy.parent.mkdir()
    branch = f"platform/custom-{ticket['ticket_id']}"
    git(clone, 'worktree', 'add', '-b', branch, str(legacy), 'origin/main')
    (legacy / 'tracked.txt').write_text('preserve tracked edit\\n')
    (legacy / 'custom-untracked.txt').write_text('preserve untracked\\n')
    suffix = root.parent / 'worktrees' / 'sonar' / f"{ticket['ticket_id']}-diagnostic"
    detached = root.parent / 'worktrees' / 'sonar' / f"{ticket['ticket_id']}-detached"
    git(clone, 'worktree', 'add', '--detach', str(suffix), 'origin/main')
    git(clone, 'worktree', 'add', '--detach', str(detached), 'origin/main')

    context = module['resolve_event_context'](registry, 'pursers', ticket, str(root), 'worker-a')

    assert Path(context['worktree']) == legacy
    assert context['branch'] == branch
    assert context['workspace_state'] == 'dirty_preserved'
    assert context['dirty_entries'] == 2
    assert (legacy / 'tracked.txt').read_text() == 'preserve tracked edit\\n'
    assert (legacy / 'custom-untracked.txt').read_text() == 'preserve untracked\\n'
    assert not (root.parent / 'worktrees' / 'sonar' / ticket['ticket_id']).exists()


def test_non_code_context_does_not_need_a_registered_git_clone(tmp_path):
    module = api()
    root, clone, registry, _base = repository_fixture(tmp_path)
    ticket = {'ticket_id': 'TK-interactive-1234',
              'target_url': 'https://example.invalid/team/sonar.git',
              'required_fields': ['interactive_detail'], 'tags': ['interactive']}
    clone.rename(clone.with_name('clone-unavailable'))

    context = module['resolve_event_context'](registry, 'pursers', ticket, str(root), 'worker-a')

    assert context['worktree'] is None
    assert context['workspace_state'] == 'not_required'


def test_registered_ticket_workspace_is_source_context_without_field_or_tag_hints(tmp_path):
    module = api()
    root, clone, registry, _base = repository_fixture(tmp_path)
    ticket = {'ticket_id': 'TK-existing-source-1234',
              'target_url': 'https://example.invalid/team/sonar.git',
              'required_fields': [], 'tags': ['interactive']}
    legacy = clone / '.worktrees' / ticket['ticket_id']
    legacy.parent.mkdir()
    branch = f"platform/source-{ticket['ticket_id']}"
    git(clone, 'worktree', 'add', '-b', branch, str(legacy), 'origin/main')

    context = module['resolve_event_context'](registry, 'pursers', ticket, str(root), 'worker-a')

    assert Path(context['worktree']) == legacy
    assert context['branch'] == branch


def test_worker_context_provisions_registered_fleet_worktree_and_preserves_dirty_state(tmp_path):
    module = api()
    root, clone, registry, base = repository_fixture(tmp_path)
    fresh = module['resolve_event_context'](registry, 'pursers', code_ticket(), str(root), 'worker-a')
    workspace = Path(fresh['worktree'])
    assert workspace == tmp_path / 'fleet' / 'worktrees' / 'sonar' / 'TK-context-1234'
    assert fresh['board_id'] == 'pursers' and fresh['project'] == 'Sonar'
    assert fresh['branch'] == 'pursers/TK-context-1234'
    assert fresh['workspace_state'] == 'clean_preserved'
    assert git(workspace, 'rev-parse', 'HEAD') == base
    (workspace / 'tracked.txt').write_text('uncommitted worker edit\n')
    (workspace / 'untracked.txt').write_text('retain me\n')
    resumed = module['resolve_event_context'](registry, 'pursers', code_ticket(), str(root), 'worker-a')
    assert resumed['workspace_state'] == 'dirty_preserved'
    assert resumed['dirty_entries'] == 2
    assert (workspace / 'tracked.txt').read_text() == 'uncommitted worker edit\n'
    assert (workspace / 'untracked.txt').read_text() == 'retain me\n'
    assert git(clone, 'status', '--porcelain') == ''


def test_reviewer_context_uses_exact_submitted_candidate_and_rejects_dirty_candidate(tmp_path):
    module = api()
    root, clone, registry, _base = repository_fixture(tmp_path)
    branch = 'goose/TK-context-review'
    candidate = tmp_path / 'candidate'
    git(clone, 'worktree', 'add', '-b', branch, str(candidate), 'origin/main')
    (candidate / 'tracked.txt').write_text('candidate\n')
    git(candidate, 'add', 'tracked.txt')
    git(candidate, 'commit', '-m', 'candidate')
    sha = git(candidate, 'rev-parse', 'HEAD')
    ticket = {**code_ticket('TK-context-review'),
              'submission_history': [{'branch': branch, 'commit_hash': sha,
                 'notes': f'branch_and_commit: {branch} @ {sha}'}]}
    context = module['resolve_event_context'](registry, 'pursers', ticket, str(root),
                                               'reviewer-a', role='reviewer')
    review_tree = Path(context['worktree'])
    assert context['branch'] == branch
    assert git(review_tree, 'rev-parse', 'HEAD') == sha
    assert context['workspace_state'] == 'clean_preserved'
    (review_tree / 'tracked.txt').write_text('reviewer dirty edit\n')
    with pytest.raises(module['EventContextError'], match='uncommitted changes'):
        module['resolve_event_context'](registry, 'pursers', ticket, str(root),
                                       'reviewer-a', role='reviewer')
    assert (review_tree / 'tracked.txt').read_text() == 'reviewer dirty edit\n'


def test_custom_source_reviewer_uses_exact_submitted_sha(tmp_path):
    module = api()
    root, clone, registry, _base = repository_fixture(tmp_path)
    branch = 'platform/custom-TK-custom-review-1234'
    candidate = tmp_path / 'custom-candidate'
    git(clone, 'worktree', 'add', '-b', branch, str(candidate), 'origin/main')
    (candidate / 'tracked.txt').write_text('custom candidate\\n')
    git(candidate, 'add', 'tracked.txt')
    git(candidate, 'commit', '-m', 'custom source candidate')
    sha = git(candidate, 'rev-parse', 'HEAD')
    ticket = {**custom_evidence_ticket('TK-custom-review-1234'),
              'submission_history': [{'branch': branch, 'commit_hash': sha}]}

    context = module['resolve_event_context'](registry, 'pursers', ticket, str(root),
                                               'reviewer-a', role='reviewer')

    assert git(context['worktree'], 'rev-parse', 'HEAD') == sha
    assert context['branch'] == branch
    assert context['workspace_state'] == 'clean_preserved'


def test_event_context_rejects_wrong_board_repo_and_symlink_escape(tmp_path):
    module = api()
    root, _clone, registry, _base = repository_fixture(tmp_path)
    resolve = module['resolve_event_context']
    with pytest.raises(module['EventContextError'], match='not registered'):
        resolve(registry, 'wrong-board', code_ticket(), str(root), 'worker-a')
    wrong = {**registry, 'projects': {'Sonar': {**registry['projects']['Sonar'],
             'repository_url': 'https://example.invalid/other/repo.git'}}}
    ticket = {**code_ticket(), 'target_url': 'https://example.invalid/other/repo.git'}
    with pytest.raises(module['EventContextError'], match='does not match registered'):
        resolve(wrong, 'pursers', ticket, str(root), 'worker-a')
    outside = tmp_path / 'outside'
    outside.mkdir()
    project_tree = tmp_path / 'fleet' / 'worktrees' / 'sonar'
    project_tree.parent.mkdir(parents=True)
    project_tree.symlink_to(outside, target_is_directory=True)
    with pytest.raises(module['EventContextError'], match='symlink or escape'):
        resolve(registry, 'pursers', code_ticket(), str(root), 'worker-a')


def test_missing_code_route_typed_error_and_non_code_contextless(tmp_path):
    module = api()
    root, _clone, registry, _base = repository_fixture(tmp_path)
    with pytest.raises(module['EventContextError']) as error:
        module['resolve_event_context'](registry, 'pursers',
            {'ticket_id': 'TK-no-route', 'required_fields': ['branch_and_commit']},
            str(root), 'worker-a')
    assert error.value.code == 'project_route_missing'
    with pytest.raises(module['EventContextError']) as custom_error:
        module['resolve_event_context'](registry, 'pursers',
            {'ticket_id': 'TK-custom-no-route', 'required_fields': ['base_and_head_sha']},
            str(root), 'worker-a')
    assert custom_error.value.code == 'project_route_missing'
    assert module['resolve_event_context'](registry, 'pursers',
        {'ticket_id': 'TK-contextless', 'target_url': None, 'required_fields': []},
        str(root), 'worker-a') is None
