import asyncio
import contextlib
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('integration_delivery', Path(__file__).parents[1]/'integration_delivery.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
S, T = 'a'*40, 'b'*40
POLICY = {'mode': 'integration', 'integration_branch': 'Pursers', 'base_branch': 'dev', 'auto_integrate': True}


def pr(**changes):
    return {'status': 'active', 'sourceRefName': 'refs/heads/pursers/TK-one', 'targetRefName': 'refs/heads/Pursers', 'lastMergeSourceCommit': {'commitId': S}, 'lastMergeTargetCommit': {'commitId': T}, 'mergeStatus': 'succeeded', 'reviewers': [], **changes}


def run(remote=None, policy=None, checks=None, attempt=None):
    calls=[]
    async def reserve(key): calls.append(('reserve', key))
    async def complete(): calls.append(('complete',)); return {}
    result = asyncio.run(m.integrate(policy or POLICY, approved_sha=S, source_ref='refs/heads/pursers/TK-one', target_sha=T, remote=remote or pr(), checks=checks, prior_attempt=attempt, reserve=reserve, complete=complete))
    return result,calls


def test_never_completes_development_or_production():
    for branch in ('dev','prd'):
        result,calls=run(pr(targetRefName='refs/heads/'+branch))
        assert result['state']=='integration_blocked'
        assert calls==[]


def test_unavailable_validation_and_unknown_baseline_block():
    result,calls=run(checks={})
    assert result['state']=='integration_blocked' and calls==[]


def valid_checks():
    return {'validation': {'source_sha': S, 'target_sha': T, 'passed': True}, 'policies': [{'enabled': True, 'blocking': True, 'status': 'approved'}]}


def test_reserves_before_completion_then_waits_for_remote_confirmation():
    result,calls=run(checks=valid_checks())
    assert [x[0] for x in calls]==['reserve','complete']
    assert result['state']=='integration_pending'


def test_changed_sha_and_stale_validation_never_merge():
    for remote, checks in [(pr(lastMergeSourceCommit={'commitId': 'c'*40}), valid_checks()),(pr(), {**valid_checks(), 'validation': {'source_sha':S,'target_sha':'d'*40,'passed':True}})]:
        result,calls=run(remote,checks=checks)
        assert result['state']=='integration_blocked' and calls==[]


def test_paused_batch_prevents_new_merges():
    result,calls=run(policy={**POLICY,'collection_paused':True},checks=valid_checks())
    assert calls==[] and result['state']=='integration_pending'


def test_uncertain_attempt_is_not_repeated():
    result,calls=run(checks=valid_checks(),attempt=S+':'+T)
    assert calls==[] and result['state']=='pr_uncertain'


def test_completed_requires_confirmed_merge_commit():
    result,calls=run(pr(status='completed', lastMergeCommit={'commitId':'e'*40}))
    assert result['state']=='integration_merged' and result['merge_sha']=='e'*40 and calls==[]


def test_completed_attempt_with_different_target_is_not_reported_ready():
    result, calls = run(pr(status='completed', lastMergeCommit={'commitId':'e'*40},
                           lastMergeTargetCommit={'commitId':'f'*40}), attempt=S+':'+T)
    assert result['state'] == 'integration_blocked' and calls == []


@pytest.mark.parametrize('mode', ['success', 'timeout', 'source_changed', 'old_target'])
def test_resident_reconciler_persists_and_reconciles_without_repeating(tmp_path, mode):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from test_source_intake import butler

    async def scenario():
        poller = object.__new__(butler.SourceIntakePoller)
        poller.index = butler.SourceIntakeIndex(tmp_path / 'index.json')
        poller._integration_offset = 0
        poller.project_reader = AsyncMock(return_value={'delivery_workflow': POLICY})
        poller.sources = [SimpleNamespace(source_id='s', connector_id='c', writeback=SimpleNamespace(tool='ado_pull_request_create'))]
        poller.runtimes = {'c': object()}
        poller.ticket_reader = AsyncMock(return_value={'status':'closed', 'review_verdict':'approve'})
        poller.ticket_annotator = AsyncMock()
        fields = {'repository_project':'sample', 'repository_name':'repo',
                  'repository_url':'https://dev.azure.com/example/sample/_git/repo',
                  'source_branch':'pursers/TK-one', 'approved_sha':S}
        poller._writeback_fields = AsyncMock(return_value=fields)
        notice = 'pursers-delivery: ' + json.dumps({'state':'pr_created','pr_id':7})
        poller.index.entries = {'a': {'board_id':'b','source_id':'s','ticket_id':'TK-one',
                                     'status':'delivered','delivery_notice':notice,'approved_sha':S,'target_branch':'Pursers'},
                               'z': {'board_id':'b','source_id':'s','ticket_id':'TK-two',
                                     'status':'delivered','delivery_notice':notice,'approved_sha':S,'target_branch':'Pursers'}}
        remote = pr(repository={'name':'repo','project':{'name':'sample'}})
        if mode == 'old_target': remote['targetRefName'] = 'refs/heads/dev'
        calls = []
        reads = 0
        async def tool(_runtime, name, args, **kwargs):
            nonlocal reads
            calls.append(name)
            if name == 'ado_pull_request_get':
                reads += 1
                if mode == 'source_changed' and reads == 2:
                    return {**remote, 'lastMergeSourceCommit':{'commitId':'f'*40}}
                return remote
            if name == 'ado_repository_details_get':
                return {'repository': {'remoteUrl':fields['repository_url']}, 'refs':{'value':[{'name':'refs/heads/Pursers','objectId':T}]}}
            if name == 'ado_pull_request_checks_get': return valid_checks()
            if name == 'ado_pull_request_update':
                disk = json.loads(poller.index.path.read_text())
                assert disk['entries']['a']['integration_attempt'] == S + ':' + T
                assert args['additionalProperties']['completionOptions']['bypassPolicy'] is False
                if mode == 'timeout': raise TimeoutError()
                remote.update(status='completed',lastMergeCommit={'commitId':'e'*40})
                return remote
            raise AssertionError(name)
        poller._delivery_tool = tool
        findings = []
        await poller._integration_pass(findings)
        if mode == 'old_target':
            assert 'ado_pull_request_update' not in calls
            assert poller.index.entries['a']['delivery_state'] == 'integration_blocked'
            return
        # Simulated process restart, loading the actual durable index.
        poller.index = butler.SourceIntakeIndex(poller.index.path)
        await poller._integration_pass(findings)
        assert calls.count('ado_pull_request_update') == (0 if mode == 'source_changed' else 1)
        assert 'delivery_state' not in poller.index.entries['z']
        assert poller.index.entries['a']['delivery_state'] == ('integration_merged' if mode == 'success' else 'pr_uncertain')
    asyncio.run(scenario())


@pytest.mark.parametrize('effect,risky', [('read_only',[]),('mutating',[])])
def test_completion_tool_must_have_mutating_policy_gate(effect, risky):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from test_source_intake import butler
    poller=object.__new__(butler.SourceIntakePoller)
    runtime=SimpleNamespace(declaration=SimpleNamespace(tools=[SimpleNamespace(name='ado_pull_request_update',effect=effect)],risky_tools=risky),call_tool=AsyncMock())
    with pytest.raises(butler.ConnectorDenied):
        asyncio.run(poller._delivery_tool(runtime,'ado_pull_request_update',{},mutate=True))
    runtime.call_tool.assert_not_called()


def test_legacy_deliveries_are_not_adopted_by_a_later_policy(tmp_path):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from test_source_intake import butler
    poller=object.__new__(butler.SourceIntakePoller)
    poller.index=butler.SourceIntakeIndex(tmp_path/'index.json')
    poller.index.entries={'old':{'status':'delivered','board_id':'b','source_id':'s'}}
    poller.project_reader=AsyncMock(return_value={'delivery_workflow':POLICY})
    poller.sources=[SimpleNamespace(source_id='s')]
    poller._integration_offset=0
    poller._delivery_tool=AsyncMock()
    asyncio.run(poller._integration_pass([]))
    poller._delivery_tool.assert_not_called()
    poller.index.entries['old']['target_branch']='dev'
    asyncio.run(poller._integration_pass([]))
    poller._delivery_tool.assert_not_called()
    assert 'delivery_state' not in poller.index.entries['old']


def test_resident_branch_only_uses_resolved_policy_and_makes_zero_pr_calls(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from test_source_intake import butler

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

    effective = {
        'mode': 'branch_only', 'mapped_base': 'main',
        'integration_branch': 'pursers-integration',
        'snapshot_branch_prefix': 'pursers/delivery', 'final_pr_target': None,
        'release_trigger': {'kind': 'ready'}, 'pr_update': 'freeze_on_ready',
        'auto_integrate': False, 'final_merge': 'manual',
        'validation': {'test_commands': [], 'required_reviewers': 1,
                       'independent_review': True, 'require_upstream_policies': True},
        'conflict_policy': 'pause', 'collection_paused': False,
    }
    monkeypatch.setattr(butler._delivery_policy_api, 'resolve_delivery_policy',
                        lambda registry, project_name: {'policy': effective}, raising=False)
    registry_project = {
        'board_id': 'b', 'work_dir': str(repo), 'status': 'active',
        'repository_url': 'https://example.invalid/team/repo',
        'fleet_clone_dir': str(repo),
        'work_dir_owner': 'fleet',
    }
    registry = {'schema_version': 1, 'projects': {'P': registry_project}}

    class Client:
        async def board_state_get(self, key):
            assert key == 'project_registry'
            return {'state': {'value': json.dumps(registry)}}

    @contextlib.asynccontextmanager
    async def client_for_board(board_id):
        assert board_id == 'home'
        yield Client()

    backend = object.__new__(butler.CentralBackend)
    backend.args = SimpleNamespace(home_board='home')
    backend._client_for_board = client_for_board

    project = asyncio.run(backend._source_project_reader('b'))
    assert 'delivery_policy_activation' not in project
    ticket = {'status': 'closed', 'latest_verdict': {'verdict': 'approve'},
              'title': 'Reviewed work',
              'latest_submission': {'branch': 'ticket-one', 'commit_hash': reviewed,
                                    'notes': 'tests passed'}}
    poller = object.__new__(butler.SourceIntakePoller)
    poller.index = butler.SourceIntakeIndex(tmp_path / 'index.json')
    entry_key = poller.index.key('s', 'ISSUE-1')
    poller.index.entries = {entry_key: {'status': 'asked', 'source_id': 's', 'external_id': 'ISSUE-1',
                                        'board_id': 'b', 'ticket_id': 'TK-one'}}
    poller._writeback_offset = 0
    poller._batch_delivery_runtimes = {}
    poller.sources = [SimpleNamespace(source_id='s', connector_id='c',
                                      writeback=SimpleNamespace(tool='ado_pull_request_create'))]
    poller.runtimes = {'c': SimpleNamespace()}
    poller.project_reader = AsyncMock(return_value=project)
    poller.ticket_reader = AsyncMock(return_value=ticket)
    poller._writeback_fields = AsyncMock(return_value={
        'repository_project': 'sample', 'repository_name': 'repo',
        'repository_url': str(origin), 'source_branch': 'ticket-one',
        'approved_sha': reviewed, 'target_branch': 'main',
    })
    poller._maybe_writeback = AsyncMock(side_effect=AssertionError('legacy PR path called'))
    poller._delivery_tool = AsyncMock(side_effect=AssertionError('PR connector called'))
    draft_policy, _ = poller._resolved_batch_policy(project)
    assert draft_policy == {'mode': 'per_ticket_pr'}

    activation = {
        'schema_version': 1, 'state': 'active', 'activation_id': 'apply-1',
        'policy_revision': hashlib.sha256(json.dumps(
            effective, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False).encode()).hexdigest()[:40],
    }
    registry_project['delivery_policy_activation'] = {
        **activation, 'policy_revision': 'f' * 40,
    }
    mismatched = asyncio.run(backend._source_project_reader('b'))
    with pytest.raises(butler.ConnectorDenied, match='activation does not match'):
        poller._resolved_batch_policy(mismatched)

    registry_project['delivery_policy_activation'] = activation
    project = asyncio.run(backend._source_project_reader('b'))
    assert project['delivery_policy_activation'] == activation
    active_policy, _ = poller._resolved_batch_policy(project)
    assert active_policy['mode'] == 'branch_only'
    # The product-shaped registry requires HTTPS; use the temporary Git remote
    # only after proving the resident reader carried activation through.
    project = {**project, 'repository_url': str(origin)}
    poller.project_reader = AsyncMock(return_value=project)
    findings = []
    written = asyncio.run(poller._writeback_pass(findings))
    assert written == 1, findings[0].get('error_class') if findings else findings
    assert poller.index.entries[entry_key]['status'] == 'delivered'
    poller._maybe_writeback.assert_not_called()
    poller._delivery_tool.assert_not_called()
