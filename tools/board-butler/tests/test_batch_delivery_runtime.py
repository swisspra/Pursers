import asyncio
import importlib.util
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    'batch_delivery_runtime', Path(__file__).parents[1] / 'integration_delivery.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

A, B, C, D, E = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40, 'e' * 40)


def policy(**changes):
    value = {
        'mode': 'batch_pr', 'repository': 'mapped/repo', 'base_branch': 'main',
        'integration_branch': 'pursers/integration', 'snapshot_prefix': 'pursers/delivery',
        'target_branch': 'customer/review', 'trigger': 'ready',
        'snapshot_strategy': 'frozen', 'policy_revision': E,
    }
    value.update(changes)
    return value


def member(ticket='TK-one', sha=A):
    return {
        'ticket_id': ticket, 'source_ref': f'pursers/{ticket}', 'source_sha': sha,
        'issue_ids': ['ISSUE-1'], 'summary': 'Reviewed change', 'tests': 'pytest: passed',
        'evidence': {'approved_sha': sha, 'independent_review': True, 'validation_passed': True},
    }


def cohort(*ticket_ids, cohort_id='ready-1'):
    return {'cohort_id': cohort_id, 'members': list(ticket_ids)}


class FakeAdapter:
    def __init__(self):
        self.refs = {'main': B, 'pursers/integration': B, 'pursers/TK-one': A,
                     'pursers/TK-two': C, 'pursers/TK-three': D, 'customer/review': B}
        self.prs = []
        self.calls = []
        self.merge_results = []
        self.create_error = None
        self.update_error = None
        self.validation_passed = True
        self.validation_error = None
        self.target_after_validation = None
        self.repair_result = None
        self.delay = {}

    async def read_ref(self, repository, branch):
        self.calls.append(('read_ref', repository, branch))
        return self.refs.get(branch)

    async def ensure_branch(self, repository, branch, sha, expected, operation_id):
        self.calls.append(('ensure_branch', repository, branch, sha, expected, operation_id))
        observed = self.refs.get(branch)
        if observed != expected:
            return {'status': 'conflict', 'head_sha': observed}
        self.refs[branch] = sha
        return {'status': 'confirmed', 'head_sha': sha}

    async def integrate_reviewed(self, repository, source_sha, branch, expected, operation_id):
        self.calls.append(('integrate', repository, source_sha, branch, expected, operation_id))
        if repository in self.delay:
            await self.delay[repository].wait()
        if self.merge_results:
            result = self.merge_results.pop(0)
        else:
            result = {'status': 'confirmed', 'head_sha': D if source_sha == A else E}
        if result.get('status') == 'confirmed':
            self.refs[branch] = result['head_sha']
        return result

    async def validate_cumulative(self, repository, base_branch, source_sha, members):
        self.calls.append(('validate', repository, base_branch, source_sha, len(members)))
        if self.validation_error:
            raise self.validation_error
        result = {'passed': self.validation_passed, 'source_sha': source_sha,
                  'target_sha': self.refs[base_branch]}
        if self.target_after_validation:
            self.refs['customer/review'] = self.target_after_validation
        return result

    async def list_customer_prs(self, repository, target_branch, status):
        self.calls.append(('list_prs', repository, target_branch, status))
        return [p for p in self.prs if p['target_branch'] == target_branch and p['status'] == status]

    async def create_customer_pr(self, repository, payload, body, operation_id):
        self.calls.append(('create_pr', repository, payload, body, operation_id))
        if self.create_error:
            raise self.create_error
        pr = {'id': len(self.prs) + 1, 'status': 'active', 'mutable': True,
              'source_sha': payload['snapshot_sha'], 'source_branch': payload['source_branch'],
              'target_branch': payload['target_branch'], 'correlation': payload['correlation']}
        self.prs.append(pr)
        return {'status': 'confirmed', 'pr': pr}

    async def update_customer_pr(self, repository, payload, body, operation_id):
        self.calls.append(('update_pr', repository, payload, body, operation_id))
        if self.update_error:
            raise self.update_error
        pr = next(p for p in self.prs if p['id'] == payload['pr_id'])
        pr['source_sha'] = payload['source_sha']
        return {'status': 'confirmed', 'source_sha': payload['source_sha']}

    async def reconcile_operation(self, repository, reservation):
        self.calls.append(('reconcile', repository, reservation['operation_id']))
        if reservation['kind'] in {'integrate_member', 'integrate_repaired_member'}:
            head = D if reservation['payload']['source_sha'] == A else E
            self.refs[reservation['payload']['integration_branch']] = head
            return {'status': 'confirmed', 'head_sha': head}
        if reservation['kind'] == 'snapshot_ref':
            return {'status': 'confirmed', 'head_sha': reservation['payload']['sha']}
        if reservation['kind'] == 'create_customer_pr':
            payload = reservation['payload']
            pr = {'id': len(self.prs) + 1, 'status': 'active', 'mutable': True,
                  'source_sha': payload['snapshot_sha'], 'source_branch': payload['source_branch'],
                  'target_branch': payload['target_branch'], 'correlation': payload['correlation']}
            return {'status': 'confirmed', 'pr': pr}
        if reservation['kind'] == 'update_customer_pr':
            payload = reservation['payload']
            pr = next(p for p in self.prs if p['id'] == payload['pr_id'])
            pr['source_sha'] = payload['source_sha']
            return {'status': 'confirmed', 'pr': dict(pr)}
        return {'status': 'confirmed'}

    async def get_customer_pr(self, repository, pr_id):
        return next((p for p in self.prs if p['id'] == pr_id), self.prs[0])

    async def confirm_customer_merge(self, repository, target_branch, snapshot_sha, pr):
        return {'confirmed': True, 'target_sha': self.refs[target_branch]}

    async def request_conflict_repair(self, repository, runner, ticket_id, source_sha, working_head):
        self.calls.append(('repair', repository, runner, ticket_id, source_sha, working_head))
        if self.repair_result and self.repair_result.get('source_sha'):
            self.refs[f'pursers/{ticket_id}'] = self.repair_result['source_sha']
        return self.repair_result or {'status': 'unavailable'}


def runtime(tmp_path, adapter=None, clock=None):
    adapter = adapter or FakeAdapter()
    return m.BatchDeliveryRuntime(m.BatchLedger(tmp_path / 'ledger.json'), adapter, clock=clock), adapter


def run(awaitable):
    return asyncio.run(awaitable)


def test_policy_modes_fail_closed_and_keep_legacy_unchanged(tmp_path):
    rt, adapter = runtime(tmp_path)
    assert run(rt.collect({'mode': 'future_mode'}, member())) == {
        'state': 'integration_blocked', 'reason': 'delivery_mode_unavailable'}
    assert run(rt.collect({'mode': 'per_ticket_pr'}, member())) == {
        'state': 'legacy_per_ticket_pr', 'changed': False}
    assert adapter.calls == []


def test_multi_ticket_one_customer_pr_and_frozen_batch_starts_next(tmp_path):
    rt, adapter = runtime(tmp_path)
    first = run(rt.collect(policy(), member()))
    second = run(rt.collect(policy(), member('TK-two', C)))
    assert first['state'] == 'integrated' and second['state'] == 'integrated'
    opened = run(rt.release(policy(), second['batch_key'], request=cohort('TK-one', 'TK-two')))
    assert opened['state'] == 'in_delivery' and opened['pr_id'] == 1
    third = run(rt.collect(policy(), member('TK-three', D)))
    assert third['state'] == 'integrated'
    waiting = run(rt.release(policy(), third['batch_key'], request=cohort('TK-three', cohort_id='ready-2')))
    assert waiting['state'] == 'integration_pending' and waiting['reason'] == 'customer_pr_slot_busy'
    # The first immutable customer snapshot remains untouched; the next batch waits
    # because one customer PR is already active.
    assert len(adapter.prs) == 1
    batches = list(rt.ledger.document['batches'].values())
    assert len(batches) == 2
    assert set(batches[0]['members']) == {'TK-one', 'TK-two'}
    assert all(row['status'] == 'in_delivery' for row in batches[0]['members'].values())
    assert set(batches[1]['members']) == {'TK-three'}
    assert batches[1]['members']['TK-three']['status'] == 'integrated'


def test_branch_only_never_calls_pr_connector(tmp_path):
    rt, adapter = runtime(tmp_path)
    result = run(rt.collect(policy(mode='branch_only'), member(), release=cohort('TK-one')))
    assert result['state'] == 'in_delivery' and 'pr_id' not in result
    assert not any(call[0] in {'list_prs', 'create_pr', 'update_pr'} for call in adapter.calls)


def test_duplicate_member_is_idempotent_across_restart_and_modes(tmp_path):
    rt, adapter = runtime(tmp_path)
    result = run(rt.collect(policy(mode='branch_only'), member(), release=cohort('TK-one')))
    rt2 = m.BatchDeliveryRuntime(m.BatchLedger(tmp_path / 'ledger.json'), adapter)
    duplicate = run(rt2.collect(policy(mode='batch_pr'), member()))
    assert duplicate['deduplicated'] is True
    assert duplicate['batch_key'] == result['batch_key']
    assert sum(call[0] == 'integrate' for call in adapter.calls) == 1


def test_source_change_and_exact_validation_fail_closed(tmp_path):
    rt, adapter = runtime(tmp_path)
    adapter.refs['pursers/TK-one'] = C
    assert run(rt.collect(policy(), member()))['reason'] == 'source_ref_changed_fresh_independent_review_required'
    adapter.refs['pursers/TK-one'] = A
    adapter.validation_passed = False
    assert run(rt.collect(policy(), member(), release=cohort('TK-one')))['reason'] == 'exact_cumulative_validation_required'
    assert not adapter.prs


def test_unavailable_external_checks_are_explicit_and_never_fabricated(tmp_path):
    rt, adapter = runtime(tmp_path)
    adapter.validation_error = RuntimeError('upstream checks route unavailable')
    result = run(rt.collect(policy(), member(), release=cohort('TK-one')))
    assert result['reason'] == 'exact_validation_capability_unavailable'
    batch = next(iter(rt.ledger.document['batches'].values()))
    assert batch['state'] == 'blocked_validation'
    assert batch['validation_error_class'] == 'RuntimeError'
    assert 'validation' not in batch and not adapter.prs


def test_target_change_after_validation_blocks_pr_creation(tmp_path):
    rt, adapter = runtime(tmp_path)
    adapter.target_after_validation = C
    result = run(rt.collect(policy(), member(), release=cohort('TK-one')))
    assert result['reason'] == 'customer_target_changed_revalidation_required'
    assert not adapter.prs


def test_conflict_requires_configured_runner_and_fresh_independent_review(tmp_path):
    rt, adapter = runtime(tmp_path)
    adapter.merge_results = [{'status': 'conflict', 'source_sha': A}]
    result = run(rt.collect(policy(mode='branch_only', trigger='manual'), member()))
    assert result['reason'] == 'conflict_runner_unavailable'
    assert not any(call[0] == 'repair' for call in adapter.calls)

    rt, adapter = runtime(tmp_path / 'repaired')
    adapter.merge_results = [{'status': 'conflict', 'source_sha': A},
                             {'status': 'confirmed', 'head_sha': E}]
    adapter.repair_result = {
        'status': 'confirmed', 'source_sha': C,
        'evidence': {'approved_sha': C, 'independent_review': True, 'validation_passed': True},
    }
    result = run(rt.collect(policy(mode='branch_only', trigger='manual', conflict_runner='repair-1'), member()))
    assert result['state'] == 'integrated'
    batch = rt.ledger.document['batches'][result['batch_key']]
    assert batch['members']['TK-one']['source_sha'] == C
    assert batch['members']['TK-one']['evidence']['independent_review'] is True
    assert any(call[0] == 'repair' for call in adapter.calls)


def test_unknown_merge_is_reserved_before_mutation_and_reconciled_after_restart(tmp_path):
    rt, adapter = runtime(tmp_path)
    adapter.merge_results = [{'status': 'unknown'}]
    result = run(rt.collect(policy(mode='branch_only'), member(), release=cohort('TK-one')))
    assert result['reason'] == 'integration_outcome_unknown_reconcile_before_retry'
    disk = m.BatchLedger(tmp_path / 'ledger.json')
    batch = next(iter(disk.document['batches'].values()))
    reservation = next(r for r in batch['reservations'].values() if r['kind'] == 'integrate_member')
    assert reservation['status'] == 'unknown'
    assert run(m.BatchDeliveryRuntime(disk, adapter).reconcile_unknown(batch['batch_key']))['reconciled'] == 1
    assert sum(call[0] == 'integrate' for call in adapter.calls) == 1


def test_unknown_pr_create_does_not_duplicate_after_restart(tmp_path):
    rt, adapter = runtime(tmp_path)
    adapter.create_error = TimeoutError()
    result = run(rt.collect(policy(), member(), release=cohort('TK-one')))
    assert result['reason'] == 'customer_pr_create_outcome_unknown'
    batch = next(iter(rt.ledger.document['batches'].values()))
    assert next(r for r in batch['reservations'].values() if r['kind'] == 'create_customer_pr')['status'] == 'unknown'
    reloaded = m.BatchDeliveryRuntime(m.BatchLedger(tmp_path / 'ledger.json'), adapter)
    duplicate = run(reloaded.collect(policy(), member()))
    assert duplicate['reason'] == 'remote_outcome_unknown_reconcile_before_retry'
    assert sum(call[0] == 'create_pr' for call in adapter.calls) == 1


def test_policy_revision_is_frozen_for_active_batch(tmp_path):
    rt, adapter = runtime(tmp_path)
    result = run(rt.collect(policy(mode='branch_only', trigger='manual'), member()))
    changed = policy(mode='branch_only', trigger='manual', policy_revision=D)
    released = run(rt.release(changed, result['batch_key'], request={'authorized': True, 'request_id': 'r1'}))
    assert released['reason'] == 'active_batch_policy_is_frozen'


def test_manual_trigger_authorization_and_idempotency(tmp_path):
    rt, adapter = runtime(tmp_path)
    waiting = run(rt.collect(policy(mode='branch_only', trigger='manual'), member()))
    assert waiting['state'] == 'integrated'
    assert run(rt.release(policy(mode='branch_only', trigger='manual'), waiting['batch_key']))['released'] is False
    released = run(rt.release(policy(mode='branch_only', trigger='manual'), waiting['batch_key'],
                              request={'authorized': True, 'request_id': 'manual-1'}))
    assert released['state'] == 'in_delivery'
    assert run(rt.release(policy(mode='branch_only', trigger='manual'), waiting['batch_key'],
                          request={'authorized': True, 'request_id': 'manual-1'}))['released'] is False


def test_scheduled_trigger_has_timezone_slot_and_no_catchup(tmp_path):
    now = [datetime(2026, 1, 1, 8, 59, tzinfo=timezone.utc)]
    rt, adapter = runtime(tmp_path, clock=lambda: now[0])
    scheduled = policy(mode='branch_only', trigger='scheduled', schedule='16:00', timezone='Asia/Bangkok')
    waiting = run(rt.collect(scheduled, member()))
    assert waiting['state'] == 'integrated'
    now[0] = datetime(2026, 1, 1, 9, 1, tzinfo=timezone.utc)
    assert run(rt.release(scheduled, waiting['batch_key']))['reason'] == 'scheduled_slot_not_current'
    now[0] = datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc)
    assert run(rt.release(scheduled, waiting['batch_key']))['state'] == 'in_delivery'


def test_duplicate_existing_pr_and_external_correlation_block(tmp_path):
    rt, adapter = runtime(tmp_path)
    adapter.prs.append({'id': 99, 'status': 'active', 'mutable': False, 'source_sha': C,
                        'source_branch': 'foreign', 'target_branch': 'customer/review',
                        'correlation': 'foreign'})
    result = run(rt.collect(policy(), member(), release=cohort('TK-one')))
    assert result['reason'] == 'one_active_customer_pr_limit'
    assert sum(call[0] == 'create_pr' for call in adapter.calls) == 0


def test_rolling_pr_updates_only_authorized_snapshot_and_invalidates_validation(tmp_path):
    rt, adapter = runtime(tmp_path)
    rolling = policy(snapshot_strategy='rolling')
    first = run(rt.collect(rolling, member(), release=cohort('TK-one')))
    assert first['state'] == 'in_delivery'
    second = run(rt.collect(rolling, member('TK-two', C),
                            release=cohort('TK-one', 'TK-two', cohort_id='ready-2')))
    assert second['state'] == 'in_delivery'
    batch = rt.ledger.document['batches'][first['batch_key']]
    assert batch['validation_state'] == 'stale'
    assert len(adapter.prs) == 1 and adapter.prs[0]['source_sha'] == E
    assert sum(call[0] == 'update_pr' for call in adapter.calls) == 1


def test_rolling_update_timeout_stays_reserved_until_exact_pr_readback(tmp_path):
    rt, adapter = runtime(tmp_path)
    rolling = policy(snapshot_strategy='rolling')
    opened = run(rt.collect(rolling, member(), release=cohort('TK-one')))
    adapter.update_error = TimeoutError()
    result = run(rt.collect(rolling, member('TK-two', C),
                            release=cohort('TK-one', 'TK-two', cohort_id='ready-2')))
    assert result['reason'] == 'rolling_customer_pr_update_outcome_unknown'
    batch = rt.ledger.document['batches'][opened['batch_key']]
    reserved = next(row for row in batch['reservations'].values()
                    if row['kind'] == 'update_customer_pr')
    assert reserved['status'] == 'unknown'
    reconciled = run(rt.reconcile_unknown(opened['batch_key']))
    assert reconciled['state'] == 'delivery_open'
    assert reserved['status'] == 'confirmed'
    assert batch['customer_pr']['source_sha'] == E
    assert all(row['status'] == 'in_delivery' for row in batch['members'].values())


def test_customer_pr_body_reports_blockers_and_baseline_without_sonar_claim(tmp_path):
    rt, adapter = runtime(tmp_path)
    work = member()
    work.update(blockers='waiting on customer window', baseline_failures='one known flaky check')
    run(rt.collect(policy(), work, release=cohort('TK-one')))
    body = next(call[3] for call in adapter.calls if call[0] == 'create_pr')
    assert 'Blockers: waiting on customer window' in body
    assert 'Baseline failures: one known flaky check' in body
    assert 'does not claim Sonar issues are resolved' in body


def test_customer_squash_completion_is_reconciled_without_downstream_pr(tmp_path):
    rt, adapter = runtime(tmp_path)
    opened = run(rt.collect(policy(), member(), release=cohort('TK-one')))
    pr = adapter.prs[0]
    pr.update(status='completed', merge_sha=C)
    adapter.refs['customer/review'] = E  # target may advance beyond a squash merge SHA
    completed = run(rt.observe_customer_completion(opened['batch_key']))
    assert completed == {'state': 'customer_merged', 'batch_key': opened['batch_key'], 'merge_sha': C}
    batch = rt.ledger.document['batches'][opened['batch_key']]
    assert batch['mapped_target_sha'] == E
    assert all(v['status'] == 'customer_merged' for v in batch['members'].values())
    assert sum(call[0] == 'create_pr' for call in adapter.calls) == 1


@pytest.mark.parametrize('field,value', [
    ('id', 99), ('source_branch', 'externally/edited'),
    ('target_branch', 'wrong/target'), ('correlation', 'wrong-batch'),
])
def test_customer_completion_requires_exact_immutable_pr_identity(tmp_path, field, value):
    rt, adapter = runtime(tmp_path)
    opened = run(rt.collect(policy(), member(), release=cohort('TK-one')))
    adapter.prs[0].update(status='completed', merge_sha=C)
    adapter.prs[0][field] = value
    result = run(rt.observe_customer_completion(opened['batch_key']))
    assert result['reason'] == 'customer_pr_correlation_mismatch'


def test_blocked_member_does_not_block_independent_repository(tmp_path):
    async def scenario():
        adapter = FakeAdapter()
        blocked_gate = asyncio.Event()
        adapter.delay['repo/blocked'] = blocked_gate
        rt = m.BatchDeliveryRuntime(m.BatchLedger(tmp_path / 'ledger.json'), adapter)
        blocked_policy = policy(repository='repo/blocked', mode='branch_only')
        independent = policy(repository='repo/free', target_branch='customer/free',
                             integration_branch='pursers/free', mode='branch_only')
        adapter.refs['pursers/free'] = B
        task = asyncio.create_task(rt.collect(blocked_policy, member()))
        await asyncio.sleep(0)
        result = await rt.collect(independent, member('TK-two', C), release=cohort('TK-two'))
        assert result['state'] == 'in_delivery'
        blocked_gate.set()
        await task
    run(scenario())


def test_repo_mutation_is_single_writer(tmp_path):
    async def scenario():
        adapter = FakeAdapter()
        gate = asyncio.Event()
        adapter.delay['mapped/repo'] = gate
        rt = m.BatchDeliveryRuntime(m.BatchLedger(tmp_path / 'ledger.json'), adapter)
        one = asyncio.create_task(rt.collect(policy(mode='branch_only', trigger='manual'), member()))
        await asyncio.sleep(0)
        two = asyncio.create_task(rt.collect(policy(mode='branch_only', trigger='manual'), member('TK-two', C)))
        await asyncio.sleep(0)
        assert sum(call[0] == 'integrate' for call in adapter.calls) == 1
        gate.set()
        await one
        await two
        assert sum(call[0] == 'integrate' for call in adapter.calls) == 2
    run(scenario())


def test_real_git_fixture_can_form_cumulative_reviewed_history(tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-b', 'main', repo], check=True, capture_output=True)
    subprocess.run(['git', '-C', repo, 'config', 'user.email', 'test@example.invalid'], check=True)
    subprocess.run(['git', '-C', repo, 'config', 'user.name', 'Batch Test'], check=True)
    (repo / 'base').write_text('base\n')
    subprocess.run(['git', '-C', repo, 'add', 'base'], check=True)
    subprocess.run(['git', '-C', repo, 'commit', '-m', 'base'], check=True, capture_output=True)
    base = subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['git', '-C', repo, 'switch', '-c', 'ticket-one'], check=True, capture_output=True)
    (repo / 'one').write_text('one\n')
    subprocess.run(['git', '-C', repo, 'add', 'one'], check=True)
    subprocess.run(['git', '-C', repo, 'commit', '-m', 'one'], check=True, capture_output=True)
    reviewed = subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['git', '-C', repo, 'switch', '-c', 'pursers-integration', base], check=True, capture_output=True)
    subprocess.run(['git', '-C', repo, 'merge', '--no-ff', '--no-edit', reviewed], check=True, capture_output=True)
    cumulative = subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()
    assert len({base, reviewed, cumulative}) == 3
    assert subprocess.run(['git', '-C', repo, 'merge-base', '--is-ancestor', reviewed, cumulative]).returncode == 0


def test_verified_git_adapter_drives_branch_only_without_pr_calls(tmp_path):
    origin = tmp_path / 'origin.git'
    repo = tmp_path / 'fleet-clone'
    subprocess.run(['git', 'init', '--bare', origin], check=True, capture_output=True)
    subprocess.run(['git', 'clone', origin, repo], check=True, capture_output=True)
    subprocess.run(['git', '-C', repo, 'config', 'user.email', 'test@example.invalid'], check=True)
    subprocess.run(['git', '-C', repo, 'config', 'user.name', 'Batch Test'], check=True)
    (repo / 'base').write_text('base\n')
    subprocess.run(['git', '-C', repo, 'add', 'base'], check=True)
    subprocess.run(['git', '-C', repo, 'commit', '-m', 'base'], check=True, capture_output=True)
    base = subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['git', '-C', repo, 'branch', '-M', 'main'], check=True)
    subprocess.run(['git', '-C', repo, 'push', 'origin', 'main'], check=True, capture_output=True)
    subprocess.run(['git', '-C', repo, 'switch', '-c', 'ticket-one'], check=True, capture_output=True)
    (repo / 'reviewed').write_text('reviewed\n')
    subprocess.run(['git', '-C', repo, 'add', 'reviewed'], check=True)
    subprocess.run(['git', '-C', repo, 'commit', '-m', 'reviewed'], check=True, capture_output=True)
    reviewed = subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['git', '-C', repo, 'push', 'origin', 'ticket-one'], check=True, capture_output=True)
    subprocess.run(['git', '-C', repo, 'push', 'origin', f'{base}:refs/heads/pursers-integration'],
                   check=True, capture_output=True)

    class NoPrConnector:
        def __getattr__(self, name):
            raise AssertionError(f'branch_only called connector method {name}')

    adapter = m.VerifiedGitConnectorAdapter(
        repo, str(origin), NoPrConnector(),
        validation_commands=['python3 -c "from pathlib import Path; assert Path(\'reviewed\').is_file()"'])
    rt = m.BatchDeliveryRuntime(m.BatchLedger(tmp_path / 'ledger.json'), adapter)
    actual_policy = policy(mode='branch_only', repository=str(origin), base_branch='main',
                           integration_branch='pursers-integration', target_branch='main')
    actual_member = member(sha=reviewed)
    actual_member['source_ref'] = 'ticket-one'
    result = run(rt.collect(actual_policy, actual_member, release=cohort('TK-one')))
    assert result['state'] == 'in_delivery'
    snapshot = next(iter(rt.ledger.document['batches'].values()))['snapshot_branch']
    assert run(adapter.read_ref(str(origin), snapshot)) is not None


@pytest.mark.parametrize('bad', [
    {'trigger': 'count'}, {'snapshot_strategy': 'mutable'},
    {'mode': 'branch_only', 'snapshot_strategy': 'rolling'},
    {'trigger': 'scheduled', 'schedule': '25:90', 'timezone': 'UTC'},
])
def test_nonworking_switches_are_rejected(bad):
    with pytest.raises(ValueError):
        m.parse_batch_policy(policy(**bad))
