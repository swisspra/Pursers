import asyncio
from contextlib import asynccontextmanager
import json
import time

import pytest

from test_event_seat import api, config
from test_seat_new import build_local_central


@pytest.mark.parametrize('role', ['worker', 'reviewer'])
@pytest.mark.parametrize('completed', [False, True])
def test_owned_turn_recovery_survives_restart_and_checks_central_outcome(tmp_path, monkeypatch, role, completed):
    async def exercise():
        import pursers_client
        from pursers_client import BoardClient
        import pursers_client.client as client_module

        central, mcp, _, principals, active, ids, call, original = await build_local_central(tmp_path/'central', monkeypatch)
        @asynccontextmanager
        async def http_context():
            yield object()
        class LocalClient(BoardClient):
            def _http(self):
                return http_context()
        monkeypatch.setattr(client_module, 'streamable_http_client', lambda *a, **k: mcp)
        monkeypatch.setattr(pursers_client, 'BoardClient', LocalClient)
        try:
            created = await call('ticket_create', ticket_id='TK-owned', agent_name='admin-agent',
                title='Owned turn recovery', description='Keep partial evidence across a bounded continuation',
                scope='interactive-no-send', required_fields=[], assigned_to=ids['worker'])
            tid = created.structured_content['ticket']['ticket_id']
            active['principal'] = principals['worker']
            claimed = await call('ticket_claim', agent_name='worker-agent', ticket_id=tid)
            if role == 'reviewer':
                await call('ticket_submit', agent_name='worker-agent', ticket_id=tid,
                    summary='Review fixture', notes='test_output: fixture passed', files_changed=['example.py'], stay_active=True)
                active['principal'] = principals['reviewer']
                claimed = await call('ticket_review_claim', agent_name='reviewer-agent', ticket_id=tid)
                await call('board_join', agent_name='unrelated-reviewer', role='reviewer', allow_takeover=True)
                with pytest.raises(Exception, match='work or review lease'):
                    await call('ticket_request_human', agent_name='unrelated-reviewer',
                        ticket_id=tid, message='Cannot pause another seat', kind='decision')
                from dataclasses import replace
                active['principal'] = replace(principals['reviewer'], scopes=frozenset({'board:read','board:review'}))
            before = claimed.structured_content['ticket']
            prefix = '' if role == 'reviewer' else 'lease_'
            lease_before = before.get('review_lease', before)
            model_stamp = lease_before.get(prefix+'last_model_renewed_at')
            cfg = config(tmp_path)
            cfg.update(seat_id=role+'-agent', role=role, home_board='pursers', max_runs_per_hour=None)
            token = tmp_path/'token'; token.write_text('fixture'); token.chmod(0o600)
            runner = api()['EventSeatRunner'](cfg)
            runner.active_boards = ['pursers']; runner.state['cursor'] = {'pursers': 1}
            await runner.reconcile_owned()
            after = (await call('ticket_get', ticket_id=tid)).structured_content['ticket']
            lease_after = after.get('review_lease', after)
            assert lease_after[prefix+'renewal_source'] == 'keepalive'
            assert lease_after.get(prefix+'last_model_renewed_at') == model_stamp
            assert len(runner.state['pending']) == 1
            assert runner.state['pending'][0]['ticket'] == tid

            # A restarted runner must perform the reserved continuation exactly once.
            resumed = api()['EventSeatRunner'](cfg); resumed.active_boards = ['pursers']
            calls = []; resumed.run_command = lambda argv, **kw: calls.append(argv)
            await resumed.reconcile_owned()
            resumed.process({'new_seq': {'pursers': 1}, 'events': []}, time.time())
            assert len(calls) == 1
            assert tid in calls[0][-1]
            if completed:
                if role == 'worker':
                    await call('ticket_submit', agent_name='worker-agent', ticket_id=tid,
                        summary='Recovered work', notes='test_output: fixture passed',
                        files_changed=['example.py'], stay_active=True)
                else:
                    await call('ticket_review', agent_name='reviewer-agent', ticket_id=tid,
                        verdict='approve', review_notes='Independent fixture verification passed.')
            # Goose exits successfully without submit/review: Central remains authoritative.
            final = api()['EventSeatRunner'](cfg); final.active_boards = ['pursers']
            await final.reconcile_owned()
            ticket = (await call('ticket_get', ticket_id=tid)).structured_content['ticket']
            assert ticket['status'] == (('submitted' if role == 'worker' else 'closed') if completed else 'needs_human')
            assert not ticket.get('review_lease')
            assert not ticket.get('lease_expires_at_epoch')
            await final.reconcile_owned()
            assert final.state['pending'] == []
        finally:
            central.current_principal = original
    asyncio.run(exercise())


@pytest.mark.parametrize('change', [
    {'claimed_by_agent_id': 'AI-other'}, {'claimed_by_principal_id': 'PR-other'},
    {'lease_expires_at_epoch': 9}, {'status': 'needs_human'}, {'status': 'closed'},
])
def test_recovery_never_takes_foreign_expired_or_completed_work(tmp_path, change):
    from types import SimpleNamespace
    runner = api()['EventSeatRunner'](config(tmp_path))
    ticket = {'ticket_id':'TK-one','status':'claimed','claimed_at':'2030-01-01T00:00:00Z',
              'claimed_by_agent_id':'AI-self','claimed_by_principal_id':'PR-self',
              'lease_expires_at_epoch':20}
    identity = SimpleNamespace(agent_id='AI-self',principal_id='PR-self')
    assert runner.owned_key('home',ticket,identity,10) is not None
    assert runner.owned_key('home',{**ticket,**change},identity,10) is None


def test_runner_checks_owned_result_before_waiting_again(tmp_path, monkeypatch):
    import subprocess
    module = api(); runner = module['EventSeatRunner'](config(tmp_path))
    runner.state['cursor']={'home':42}
    trace=[]
    async def bootstrap():runner.active_boards=['home']
    async def reconcile():
        trace.append('check')
        if trace == ['check']:
            runner.state['pending'].append({'board':'home','ticket':'TK-one',
                'recovery_key':'claim-one','marker':['home','TK-one','claim-one','owned_recovery']})
    def wait(*args,**kwargs):
        trace.append('wait')
        raise KeyboardInterrupt
    runner.bootstrap=bootstrap; runner.reconcile_owned=reconcile
    runner.run_command=lambda *a,**k:trace.append('model')
    monkeypatch.setattr(subprocess,'run',wait)
    with pytest.raises(KeyboardInterrupt):runner.run()
    assert trace == ['check','model','check','wait']


def test_interrupted_recovery_is_reserved_before_model_execution(tmp_path):
    cfg=config(tmp_path);runner=api()['EventSeatRunner'](cfg)
    runner.state['pending']=[{'board':'home','ticket':'TK-one','recovery_key':'claim-one',
        'marker':['home','TK-one','claim-one','owned_recovery']}]
    def interrupted(*a,**kw):raise KeyboardInterrupt
    runner.run_command=interrupted
    with pytest.raises(KeyboardInterrupt):runner.process({'new_seq':{'home':42},'events':[]},100)
    resumed=api()['EventSeatRunner'](cfg)
    assert resumed.state['owned_recoveries']=={'claim-one':1}
    assert resumed.state['last_turn']['outcome']=='interrupted'


def test_wait_join_never_claims_model_progress(tmp_path, monkeypatch):
    async def exercise():
        from pursers_client import BoardClient, wait_for_boards
        import pursers_client.client as client_module
        import pursers_client.project_registry as registry_module
        central, mcp, _, principals, active, ids, call, original = await build_local_central(tmp_path/'central', monkeypatch)
        @asynccontextmanager
        async def http_context():
            yield object()
        class LocalClient(BoardClient):
            def _http(self):return http_context()
        monkeypatch.setattr(client_module, 'streamable_http_client', lambda *a, **k: mcp)
        monkeypatch.setattr(registry_module, 'streamable_http_client', lambda *a, **k: mcp)
        try:
            created = await call('ticket_create', ticket_id='TK-owned', agent_name='admin-agent', title='Idle lease truth',
                description='Transport activity is not model progress', scope='interactive-no-send',
                required_fields=[], assigned_to=ids['worker'])
            tid = created.structured_content['ticket']['ticket_id']
            active['principal'] = principals['worker']
            before = (await call('ticket_claim', agent_name='worker-agent', ticket_id=tid)).structured_content['ticket']
            await asyncio.sleep(0.01)
            async with LocalClient('http://test/mcp', 'fixture', 'pursers',
                agent_name='worker-agent', renewal_source='keepalive', allow_takeover=True) as client:
                await wait_for_boards(client, ['pursers'], {'pursers': 1}, 0, kinds=[], submitted=False)
            after = (await call('ticket_get', ticket_id=tid)).structured_content['ticket']
            assert after['lease_renewal_source'] == 'keepalive'
            assert after.get('lease_last_model_renewed_at') == before.get('lease_last_model_renewed_at')
        finally:
            central.current_principal = original
    asyncio.run(exercise())
