import asyncio
import time
from types import SimpleNamespace

import pytest

from test_event_seat import api, config
from test_event_context import code_ticket, custom_evidence_ticket, git, repository_fixture


def _runner_client(tmp_path, ticket):
    module = api()
    root, clone, registry, _base = repository_fixture(tmp_path)
    runner = module['EventSeatRunner'](config(tmp_path))
    runner.config['repository_root'] = str(root)
    runner.registry = registry
    identity = SimpleNamespace(board_id='pursers', agent_id='AI-worker',
                               principal_id='PR-worker', agent_name='worker-a', role='worker')
    ticket.setdefault('ticket_id', 'TK-authorize-1234')
    calls = []

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def ticket_get(self, *_args, **_kwargs): return {'ticket': dict(ticket)}

        async def ticket_claim(self, _ticket_id):
            calls.append('claim')
            ticket.update(status='claimed', claimed_by_agent_id=identity.agent_id,
                          claimed_by_principal_id=identity.principal_id,
                          lease_expires_at_epoch=time.time() + 1000,
                          claimed_at='claim-epoch')

    Client.identity = identity
    runner.client = lambda _board: Client()
    return runner, clone, root, ticket, calls


@pytest.mark.parametrize('offer', [
    {'kind': 'work', 'agent_id': 'AI-worker', 'agent_name': 'worker-a',
     'expires_at_epoch': 1},
    {'kind': 'work', 'agent_id': 'AI-other', 'agent_name': 'other',
     'expires_at_epoch': time.time() + 1000},
])
def test_stale_or_foreign_code_event_does_not_provision_workspace(tmp_path, offer):
    ticket = {**code_ticket('TK-stale-1234'), 'status': 'open', 'work_offer': offer}
    runner, clone, root, _ticket, calls = _runner_client(tmp_path, ticket)
    before_worktrees = git(clone, 'worktree', 'list', '--porcelain')
    before_branches = git(clone, 'for-each-ref', '--format=%(refname)', 'refs/heads')

    authorized, reason = asyncio.run(runner.event_authorized(
        {'board': 'pursers', 'ticket': ticket['ticket_id']}, time.time()))

    assert (authorized, reason) == (False, 'stale_or_foreign_event')
    assert calls == []
    assert not (root.parent / 'worktrees' / 'sonar' / ticket['ticket_id']).exists()
    assert git(clone, 'worktree', 'list', '--porcelain') == before_worktrees
    assert git(clone, 'for-each-ref', '--format=%(refname)', 'refs/heads') == before_branches


@pytest.mark.parametrize('mode', ['fresh', 'held', 'restart'])
def test_fresh_held_and_restarted_custom_evidence_events_resolve_workspace(tmp_path, mode):
    expiry = time.time() + 1000
    ticket = {**custom_evidence_ticket('TK-custom-authorized-1234'), 'status': 'open',
              'work_offer': {'kind': 'work', 'agent_id': 'AI-worker',
                             'agent_name': 'worker-a', 'expires_at_epoch': expiry}}
    original_runner, clone, root, ticket, calls = _runner_client(tmp_path, ticket)
    identity = SimpleNamespace(agent_id='AI-worker', principal_id='PR-worker')
    event = {'board': 'pursers', 'ticket': ticket['ticket_id']}
    if mode != 'fresh':
        ticket.update(status='claimed', claimed_by_agent_id='AI-worker',
                      claimed_by_principal_id='PR-worker', lease_expires_at_epoch=expiry,
                      claimed_at='claim-epoch')
        event['recovery_key'] = original_runner.owned_key(
            'pursers', ticket, identity, time.time())
    runner = original_runner
    if mode == 'restart':
        runner = api()['EventSeatRunner'](original_runner.config)
        runner.registry = original_runner.registry
        runner.client = original_runner.client

    authorized, reason = asyncio.run(runner.event_authorized(event, time.time()))

    workspace = root.parent / 'worktrees' / 'sonar' / ticket['ticket_id']
    expected = 'claim_acquired' if mode == 'fresh' else 'owned_lease'
    assert (authorized, reason) == (True, expected)
    assert calls == (['claim'] if mode == 'fresh' else [])
    assert runner._event_context['worktree'] == str(workspace)
    assert runner._event_context['branch'] == f"pursers/{ticket['ticket_id']}"
    assert workspace.is_dir()
    assert git(clone, 'status', '--porcelain') == ''


@pytest.mark.parametrize('recovery', [False, True])
def test_authorized_fresh_and_recovery_events_provision_only_after_claim_check(tmp_path, recovery):
    expiry = time.time() + 1000
    ticket = {**code_ticket('TK-authorized-1234'), 'status': 'open',
              'work_offer': {'kind': 'work', 'agent_id': 'AI-worker',
                             'agent_name': 'worker-a', 'expires_at_epoch': expiry}}
    runner, clone, root, ticket, calls = _runner_client(tmp_path, ticket)
    event = {'board': 'pursers', 'ticket': ticket['ticket_id']}
    if recovery:
        ticket.update(status='claimed', claimed_by_agent_id='AI-worker',
                      claimed_by_principal_id='PR-worker', lease_expires_at_epoch=expiry,
                      claimed_at='claim-epoch')
        identity = SimpleNamespace(agent_id='AI-worker', principal_id='PR-worker')
        event['recovery_key'] = runner.owned_key('pursers', ticket, identity, time.time())

    authorized, reason = asyncio.run(runner.event_authorized(event, time.time()))

    assert authorized is True
    assert reason == ('owned_lease' if recovery else 'claim_acquired')
    assert calls == ([] if recovery else ['claim'])
    workspace = root.parent / 'worktrees' / 'sonar' / ticket['ticket_id']
    assert workspace.is_dir()
    assert git(workspace, 'branch', '--show-current') == f"pursers/{ticket['ticket_id']}"
    assert runner._event_context['worktree'] == str(workspace)


def _review_fixture(tmp_path, ticket_id):
    module = api()
    root, clone, registry, _base = repository_fixture(tmp_path)
    branch = f'platform/candidate-{ticket_id}'
    git(clone, 'checkout', '-b', branch)
    (clone / 'tracked.txt').write_text('candidate code\\n')
    git(clone, 'add', 'tracked.txt')
    git(clone, 'commit', '-m', 'candidate')
    sha = git(clone, 'rev-parse', 'HEAD')
    ticket = {**code_ticket(ticket_id), 'status': 'submitted',
              'submission_history': [{'branch': branch, 'commit_hash': sha,
                  'notes': f'branch_and_commit: {branch} @ {sha}'}]}
    cfg = config(tmp_path)
    cfg['role'] = 'reviewer'
    cfg['seat_id'] = 'reviewer-a'
    cfg['repository_root'] = str(root)
    runner = module['EventSeatRunner'](cfg)
    runner.registry = registry
    identity = SimpleNamespace(board_id='pursers', agent_id='AI-reviewer',
                               principal_id='PR-reviewer', agent_name='reviewer-a', role='reviewer')
    ticket['review_offer'] = {'kind': 'review', 'agent_id': 'AI-reviewer',
                              'agent_name': 'reviewer-a', 'expires_at_epoch': time.time() + 1000}
    calls = []

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def ticket_get(self, *_args, **_kwargs): return {'ticket': dict(ticket)}
        async def ticket_review_claim(self, _ticket_id):
            calls.append('review_claim')
            ticket['review_lease'] = {'reviewer_agent_id': identity.agent_id,
                'reviewer_principal_id': identity.principal_id,
                'expires_at_epoch': time.time() + 1000, 'claimed_at': 'review-claim-epoch'}

    Client.identity = identity
    runner.client = lambda _board: Client()
    return runner, clone, root, ticket, branch, sha, calls


def test_stale_review_event_does_not_provision_candidate_worktree(tmp_path):
    runner, clone, root, ticket, _branch, _sha, calls = _review_fixture(tmp_path, 'TK-review-stale-1234')
    ticket['review_offer']['expires_at_epoch'] = 1
    candidate_dir = root.parent / 'worktrees' / 'sonar' / f"{ticket['ticket_id']}-review-{ticket['submission_history'][0]['commit_hash'][:12]}"
    before_worktrees = git(clone, 'worktree', 'list', '--porcelain')

    authorized, reason = asyncio.run(runner.event_authorized(
        {'board': 'pursers', 'ticket': ticket['ticket_id']}, time.time()))

    assert (authorized, reason) == (False, 'stale_or_foreign_event')
    assert calls == []
    assert not candidate_dir.exists()
    assert git(clone, 'worktree', 'list', '--porcelain') == before_worktrees


def test_authorized_review_claim_precedes_candidate_worktree_provisioning(tmp_path):
    runner, clone, root, ticket, branch, sha, calls = _review_fixture(tmp_path, 'TK-review-live-1234')
    candidate_dir = root.parent / 'worktrees' / 'sonar' / f"{ticket['ticket_id']}-review-{sha[:12]}"
    assert not candidate_dir.exists()

    authorized, reason = asyncio.run(runner.event_authorized(
        {'board': 'pursers', 'ticket': ticket['ticket_id']}, time.time()))

    assert (authorized, reason) == (True, 'claim_acquired')
    assert calls == ['review_claim']
    assert candidate_dir.is_dir()
    assert git(candidate_dir, 'rev-parse', 'HEAD') == sha
    assert git(candidate_dir, 'branch', '--show-current') == ''
    assert runner._event_context['branch'] == branch


def test_owned_recovery_resumes_legacy_custom_evidence_workspace(tmp_path):
    ticket = {**custom_evidence_ticket('TK-custom-recovery-9876'), 'status': 'open'}
    runner, clone, root, ticket, _calls = _runner_client(tmp_path, ticket)
    legacy = clone / '.worktrees' / ticket['ticket_id']
    legacy.parent.mkdir()
    branch = f"platform/event-recovery-{ticket['ticket_id']}"
    git(clone, 'worktree', 'add', '-b', branch, str(legacy), 'origin/main')
    (legacy / 'tracked.txt').write_text('preserve local edit\\n')
    (legacy / 'untracked.txt').write_text('preserve this too\\n')
    canonical = root.parent / 'worktrees' / 'sonar' / f"{ticket['ticket_id']}-diagnostic"
    git(clone, 'worktree', 'add', '--detach', str(canonical), 'origin/main')
    ticket.update(status='claimed', claimed_by_agent_id='AI-worker',
                  claimed_by_principal_id='PR-worker',
                  lease_expires_at_epoch=time.time() + 1000, claimed_at='claim-epoch')

    authorized, reason = asyncio.run(runner.event_authorized(
        {'board': 'pursers', 'ticket': ticket['ticket_id']}, time.time()))

    assert (authorized, reason) == (True, 'owned_lease')
    assert runner._event_context['worktree'] == str(legacy)
    assert runner._event_context['branch'] == branch
    assert runner._event_context['workspace_state'] == 'dirty_preserved'
    assert (legacy / 'tracked.txt').read_text() == 'preserve local edit\\n'
    assert (legacy / 'untracked.txt').read_text() == 'preserve this too\\n'
    assert not (root.parent / 'worktrees' / 'sonar' / ticket['ticket_id']).exists()


def test_owned_recovery_resumes_legacy_ticket_worktree_without_reset(tmp_path):
    ticket = {**code_ticket('TK-legacy-9876'), 'status': 'open'}
    runner, clone, root, ticket, _calls = _runner_client(tmp_path, ticket)
    legacy = clone / '.worktrees' / ticket['ticket_id']
    legacy.parent.mkdir()
    branch = f"platform/event-recovery-{ticket['ticket_id']}"
    git(clone, 'worktree', 'add', '-b', branch, str(legacy), 'origin/main')
    (legacy / 'tracked.txt').write_text('preserve local edit\n')
    (legacy / 'untracked.txt').write_text('preserve this too\n')
    canonical = root.parent / 'worktrees' / 'sonar' / f"{ticket['ticket_id']}-diagnostic"
    git(clone, 'worktree', 'add', '--detach', str(canonical), 'origin/main')
    ticket.update(status='claimed', claimed_by_agent_id='AI-worker',
                  claimed_by_principal_id='PR-worker',
                  lease_expires_at_epoch=time.time() + 1000, claimed_at='claim-epoch')
    original_branch = git(legacy, 'branch', '--show-current')

    authorized, reason = asyncio.run(runner.event_authorized(
        {'board': 'pursers', 'ticket': ticket['ticket_id']}, time.time()))

    assert (authorized, reason) == (True, 'owned_lease')
    assert runner._event_context['worktree'] == str(legacy)
    assert runner._event_context['branch'] == original_branch
    assert runner._event_context['workspace_state'] == 'dirty_preserved'
    assert (legacy / 'tracked.txt').read_text() == 'preserve local edit\n'
    assert (legacy / 'untracked.txt').read_text() == 'preserve this too\n'
    assert not (root.parent / 'worktrees' / 'sonar' / ticket['ticket_id']).exists()


def test_actual_event_prompt_includes_verified_context_and_worker_cq_instructions(tmp_path):
    module = api()
    cfg = config(tmp_path)
    cfg['max_runs_per_hour'] = None
    runner = module['EventSeatRunner'](cfg)
    prompts = []
    runner.run_command = lambda argv, **_kwargs: prompts.append(argv[-1])
    context = {'board_id': 'pursers', 'project': 'Sonar',
               'fleet_clone_dir': '/fleet/clones/sonar',
               'repository_url': 'https://example.invalid/team/sonar.git',
               'worktree': '/fleet/worktrees/sonar/TK-prompt',
               'branch': 'pursers/TK-prompt', 'workspace_state': 'dirty_preserved'}

    async def authorized(_event, _now):
        runner._event_context = context
        runner._current_claim_ref = 'claim'
        return True, 'claim_acquired'

    runner.preflight_enabled = True
    runner.event_authorized = authorized
    runner.process({'new_seq': {'pursers': 1}, 'events': [
        {'kind': 'ticket_offered', 'board_id': 'pursers', 'ticket_id': 'TK-prompt', 'id': 'fresh'}
    ]}, 10)
    prompt = prompts[0]
    assert 'Trusted project/workspace context (Central registry and Git-verified)' in prompt
    assert '"worktree":"/fleet/worktrees/sonar/TK-prompt"' in prompt
    assert 'Keep Goose process CWD at seat identity directory' in prompt
    assert 'ask the authorized Butler a concrete technical question' in prompt
    assert 'Request a human hold only for missing domain/authority facts or exhausted no-progress recovery' in prompt
    assert 'Confirm the resulting Central ticket state' in prompt


@pytest.mark.parametrize('mode', ['fresh', 'held_recovery', 'restart_recovery'])
def test_fresh_held_and_restarted_worker_prompts_retain_verified_workspace(tmp_path, mode):
    module = api()
    cfg = config(tmp_path)
    cfg['max_runs_per_hour'] = None
    runner = module['EventSeatRunner'](cfg)
    context = {'board_id': 'pursers', 'project': 'Sonar',
               'fleet_clone_dir': '/registered/clones/sonar',
               'repository_url': 'https://example.invalid/team/sonar.git',
               'worktree': '/registered/worktrees/sonar/TK-context',
               'branch': 'pursers/TK-context', 'workspace_state': 'dirty_preserved'}
    captured = []

    async def authorized(event, _now):
        runner._event_context = context
        runner._current_claim_ref = event.get('recovery_key', 'claim')
        return True, 'owned_lease'

    runner.preflight_enabled = True
    runner.event_authorized = authorized
    runner.run_command = lambda argv, **_kwargs: captured.append(argv[-1])
    event = {'board_id': 'pursers', 'ticket_id': 'TK-context',
             'kind': 'ticket_offered', 'id': mode}
    key = '["pursers","TK-context","worker","claim-epoch"]'
    if mode != 'fresh':
        event['recovery_key'] = key
        event['marker'] = ['pursers', 'TK-context', key, 'owned_recovery']
    runner.process({'new_seq': {'pursers': 1}, 'events': [event]}, 1)
    assert len(captured) == 1
    assert '"workspace_state":"dirty_preserved"' in captured[0]
    assert '/registered/worktrees/sonar/TK-context' in captured[0]
    if mode == 'restart_recovery':
        restart_config = config(tmp_path)
        restart_config['max_runs_per_hour'] = None
        resumed = module['EventSeatRunner'](restart_config)
        resumed.preflight_enabled = True

        async def resumed_authorized(event, _now):
            resumed._event_context = context
            resumed._current_claim_ref = event.get('recovery_key', 'claim')
            return True, 'owned_lease'

        resumed.event_authorized = resumed_authorized
        resumed.run_command = lambda argv, **_kwargs: captured.append(argv[-1])
        resumed.process({'new_seq': {'pursers': 2}, 'events': [{
            'board_id': 'pursers', 'ticket_id': 'TK-context',
            'kind': 'ticket_offered', 'id': 'restart-recovery',
            'recovery_key': key,
            'marker': ['pursers', 'TK-context', key, 'owned_recovery']}]}, 2)
        assert len(captured) == 2
        assert '"workspace_state":"dirty_preserved"' in captured[-1]


def test_invalid_workspace_preflight_holds_without_paid_model_launch(tmp_path):
    module = api()
    runner = module['EventSeatRunner'](config(tmp_path))
    runner.registry = {'schema_version': 1, 'projects': {}}
    runner.preflight_enabled = True
    lease_expiry = time.time() + 1000
    ticket = {'ticket_id': 'TK-no-route', 'status': 'open',
              'required_fields': ['branch_and_commit'],
              'work_offer': {'kind': 'work', 'agent_id': 'AI-worker',
                             'agent_name': 'worker-a', 'expires_at_epoch': lease_expiry}}
    calls = []

    class Client:
        identity = SimpleNamespace(board_id='pursers', agent_id='AI-worker',
                                   principal_id='PR-worker', agent_name='worker-a', role='worker')

        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def ticket_get(self, *_args, **_kwargs): return {'ticket': dict(ticket)}

        async def ticket_claim(self, _ticket_id):
            calls.append('claim')
            ticket.update(status='claimed', claimed_by_agent_id='AI-worker',
                          claimed_by_principal_id='PR-worker', lease_expires_at_epoch=lease_expiry,
                          claimed_at='claim-epoch')

        async def ticket_question_ask(self, _ticket_id, message, kind, *, message_id=None):
            calls.append(('question', kind, message, message_id))

        async def ticket_request_human(self, _ticket_id, message, kind):
            calls.append(('human', kind, message))
            ticket['status'] = 'needs_human'

    runner.client = lambda _board: Client()
    launched = []
    runner.run_command = lambda argv, **_kwargs: launched.append(argv)
    authorized, reason = asyncio.run(runner.event_authorized(
        {'board': 'pursers', 'ticket': 'TK-no-route'}, 100))
    assert (authorized, reason) == (False, 'project_route_missing')
    assert calls[0] == 'claim'
    assert calls[1][0:2] == ('question', 'information')
    assert calls[2][0:2] == ('human', 'decision')
    assert ticket['status'] == 'needs_human'
    assert launched == []


@pytest.mark.parametrize('hold_succeeds', [True, False])
def test_legacy_unbound_review_failure_is_reported_without_stranding_lease(tmp_path, hold_succeeds):
    runner, _clone, _root, ticket, _branch, _sha, _calls = _review_fixture(
        tmp_path, 'TK-review-unbound-9876')
    ticket['submission_history'] = [{
        'summary': 'legacy incomplete candidate',
        'files_changed': ['tracked.txt'],
        'notes': 'legacy summary without canonical branch identity',
    }]
    lease = {
        'reviewer_agent_id': 'AI-reviewer',
        'reviewer_principal_id': 'PR-reviewer',
        'claimed_at': 'review-claim-epoch',
        'expires_at_epoch': time.time() + 1000,
    }
    ticket['review_lease'] = dict(lease)
    ticket['status'] = 'submitted'
    calls = []

    class Client:
        identity = SimpleNamespace(board_id='pursers', agent_id='AI-reviewer',
                                   principal_id='PR-reviewer', agent_name='reviewer-a', role='reviewer')

        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def ticket_get(self, *_args, **_kwargs): return {'ticket': dict(ticket)}

        async def ticket_question_ask(self, ticket_id, message, kind, *, message_id=None):
            calls.append(('question', ticket_id, kind, message_id, message))
            raise RuntimeError('question reporting RPC unavailable')

        async def ticket_request_human(self, ticket_id, message, kind):
            calls.append(('human', ticket_id, kind, message))
            if not hold_succeeds:
                raise RuntimeError('human hold RPC unavailable')
            ticket['status'] = 'needs_human'
            ticket.pop('review_lease', None)

        async def ticket_review_release(self, ticket_id, *, reason=None):
            calls.append(('release', ticket_id, reason))
            ticket.pop('review_lease', None)

    runner.client = lambda _board: Client()
    launched = []
    runner.run_command = lambda argv, **_kwargs: launched.append(argv)

    authorized, reason = asyncio.run(runner.event_authorized(
        {'board': 'pursers', 'ticket': ticket['ticket_id']}, time.time()))

    assert not authorized
    assert reason == ('submitted_candidate_missing:report_failed:RuntimeError'
                      if hold_succeeds else 'submitted_candidate_missing:hold_failed:RuntimeError')
    question = calls[0]
    assert question[:4] == ('question', ticket['ticket_id'], 'information',
                            question[3])
    assert question[3].startswith('MSG-workspace-')
    assert 'submitted_candidate_missing' in question[4]
    assert 'no exact submitted branch and commit' in question[4]
    assert 'do not infer one' in question[4]
    assert 'republish the canonical candidate identity' in question[4]
    assert runner.state['last_context_reporting_failure']['context_code'] == 'submitted_candidate_missing'
    assert runner.state['last_context_reporting_failure']['context_detail'] in question[4]
    assert runner.state['last_context_reporting_failure']['message_id'] == question[3]
    assert runner.state['last_context_reporting_failure']['report_error_class'] == 'RuntimeError'
    if hold_succeeds:
        assert calls[1][0:3] == ('human', ticket['ticket_id'], 'decision')
        assert ticket['status'] == 'needs_human'
    else:
        assert calls[1][0] == 'human' and calls[2][0] == 'release'
        assert ticket['status'] == 'submitted'
    assert 'review_lease' not in ticket
    assert runner._current_claim_ref == 'unowned'
    assert launched == []


def test_reviewer_prompt_has_independent_exact_baseline_guidance(tmp_path):
    module = api()
    cfg = config(tmp_path)
    cfg.update(role='reviewer', max_runs_per_hour=None)
    runner = module['EventSeatRunner'](cfg)
    prompts = []
    runner.run_command = lambda argv, **_kwargs: prompts.append(argv[-1])
    runner.process({'new_seq': {'pursers': 1}, 'events': [
        {'kind': 'review_offered', 'board_id': 'pursers',
         'ticket_id': 'TK-review-prompt', 'id': 'review'}]}, 10)
    prompt = prompts[0]
    assert 'Report scoped ticket acceptance separately from final whole-project QA' in prompt
    assert 'unchanged unrelated baseline failure is not a new regression' in prompt
    assert 'do not alter candidate code' in prompt
    assert 'ask the authorized Butler a concrete technical question' not in prompt
