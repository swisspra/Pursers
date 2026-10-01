import asyncio
import importlib.util
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
                                     'status':'delivered','delivery_notice':notice,'approved_sha':S},
                               'z': {'board_id':'b','source_id':'s','ticket_id':'TK-two',
                                     'status':'delivered','delivery_notice':notice,'approved_sha':S}}
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
