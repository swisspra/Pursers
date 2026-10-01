"""Local evidence must protect leases across all selected boards."""
import copy
import runpy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fleet_reconciler import NOW, fleet_executor


def observer_api():
    path = Path(__file__).resolve().parents[1] / 'fleet_observation.py'
    assert path.exists(), 'native fleet observer is missing'
    return runpy.run_path(str(path))


def fixture():
    template = SimpleNamespace(seat_root=Path('/example/seat'), principal_id='p', role='worker',
        template_id='t', digest_sha256='a'*64,
        capabilities={'can_work': True, 'can_review': False, 'tier_max': 2, 'max_parallel': 1})
    agent = {'agent_name': 'seat', 'agent_id': 'agent', 'principal_id': 'p', 'role': 'worker',
        'status': 'idle', 'lifecycle_status': 'active', 'last_activity_at': NOW.isoformat(),
        'capabilities': template.capabilities}
    boards = {b: {'agents': [copy.deepcopy(agent)], 'tickets': []} for b in ('a', 'b')}
    members = {b: {'members': [{'principal_id': 'p', 'role': 'member'}]} for b in boards}
    service = SimpleNamespace(inspect=lambda *_: fleet_executor.ServiceObservation(True, True, True, True))
    return template, boards, members, service


@pytest.mark.parametrize('fault', ['work', 'review', 'truncated', 'missing', 'stale', 'wrong_principal', 'retired'])
def test_cross_board_busy_or_unknown_never_becomes_idle(fault):
    api = observer_api()
    template, boards, members, service = fixture()
    if fault in ('work', 'review'):
        boards['b']['agents'][0].update(status='busy', lease_expires_at=(NOW+timedelta(minutes=3)).isoformat())
    elif fault == 'truncated': boards['b']['truncated'] = True
    elif fault == 'missing': del boards['b']
    elif fault == 'stale': boards['b']['agents'][0]['last_activity_at'] = (NOW-timedelta(hours=1)).isoformat()
    elif fault == 'wrong_principal': boards['b']['agents'][0]['principal_id'] = 'other'
    elif fault == 'retired': boards['b']['agents'][0]['lifecycle_status'] = 'retired'
    observer = api['LocalFleetObserver']({'t': template}, service, {}, {'t': {'board_id': 'a', 'provider': 'p1'}})
    observation, readiness, leases = observer.collect(['a', 'b'], boards, members, NOW,
        {'p1': {'status': 'healthy', 'latency_ms': 1}}, {'load_ratio': 0.1, 'capacity_available': True, 'executor_status': 'healthy'})
    assert observation['executor_seats'][0]['lifecycle'] in {'busy', 'unhealthy'}
    if fault in ('work', 'review'):
        assert all(leases['boards'][b]['seats']['seat']['work'] for b in ('a', 'b'))
    else:
        assert all('seat' not in leases['boards'][b]['seats'] for b in ('a', 'b'))


def test_unbound_provider_unknown_and_private_publication(tmp_path):
    api = observer_api()
    template, boards, members, service = fixture()
    observer = api['LocalFleetObserver']({'t': template}, service, {}, {})
    observation, readiness, leases = observer.collect(['a', 'b'], boards, members, NOW, {}, {})
    assert observation['executor_seats'][0]['lifecycle'] == 'unhealthy'
    path = tmp_path/'observation.json'
    api['publish'](path, observation)
    assert path.stat().st_mode & 0o777 == 0o600


def test_executor_snapshot_returns_copies(tmp_path):
    store = fleet_executor.ExecutorStore(tmp_path/'executor.sqlite3')
    assert hasattr(store, 'observation_snapshot'), 'executor snapshot interface is missing'
    assert store.observation_snapshot() == {}
    store.connection.close()


def test_native_cli_accepts_local_observation_paths(tmp_path):
    from test_fleet_reconciler import butler
    args = butler.parse_args(['--token-path', str(tmp_path/'token'), '--repo', str(tmp_path),
                              '--pid-file', str(tmp_path/'pid'), '--cursor-file', str(tmp_path/'cursor'),
                              '--fleet-observation-mode', 'local',
                              '--fleet-local-config', str(tmp_path/'local.json'),
                              '--fleet-executor-state', str(tmp_path/'executor')])
    assert args.fleet_observation_mode == 'local'


@pytest.mark.parametrize('running,ready,verified,prior,want', [
    (False,False,True,'stopped','stopped'), (True,False,True,'starting','starting'),
    (True,True,True,'draining','draining'), (True,True,False,'ready','unhealthy'),
])
def test_service_lifecycle_and_persisted_generation(running,ready,verified,prior,want):
    api = observer_api()
    template, boards, members, _ = fixture()
    service = SimpleNamespace(inspect=lambda *_: fleet_executor.ServiceObservation(True,running,ready,verified))
    stored = {'seat': {'generation': 7, 'lifecycle': prior, 'last_mutation': NOW.timestamp()-10}}
    observer = api['LocalFleetObserver']({'t': template}, service, stored, {'t': {'board_id': 'a', 'provider': 'worker'}})
    observation, _, _ = observer.collect(['a','b'],boards,members,NOW,
        {'drafting': {'status': 'healthy','latency_ms': 1}, 'worker': {'status': 'unavailable','latency_ms': 0}}, {})
    assert observation['executor_seats'][0]['lifecycle'] == want
    assert observation['executor_seats'][0]['generation'] == 7
    assert observation['provider_observations']['a']['worker']['status'] == 'unavailable'


def test_subscription_wait_heartbeat_uses_registry_freshness_window():
    template,boards,members,services=fixture()
    for board in boards.values():board['agents'][0]['last_activity_at']=(NOW-timedelta(seconds=240)).isoformat()
    observer=observer_api()['LocalFleetObserver']({'t':template},services,{}, {'t':{'board_id':'a','provider':'worker'}})
    observation,_,_=observer.collect(['a','b'],boards,members,NOW,{}, {})
    assert observation['executor_seats'][0]['lifecycle']=='ready'


def test_reported_unready_agent_cannot_authorize_fleet_readiness():
    template,boards,members,services=fixture()
    boards['b']['agents'][0]['readiness']={'reported':True,'dispatch_ready':False}
    observer=observer_api()['LocalFleetObserver']({'t':template},services,{}, {'t':{'board_id':'a','provider':'worker'}})
    observation,readiness,leases=observer.collect(['a','b'],boards,members,NOW,{}, {})
    assert observation['executor_seats'][0]['lifecycle']=='unhealthy'
    assert 'seat' not in readiness['boards']['b']['seats']


def test_stopped_seat_uses_current_membership_without_requiring_a_live_heartbeat():
    template,boards,members,_=fixture()
    services=SimpleNamespace(inspect=lambda *_: fleet_executor.ServiceObservation(True,False,False,True))
    for board in boards.values():
        board['agents'][0]['last_activity_at']=(NOW-timedelta(hours=1)).isoformat()
    observer=observer_api()['LocalFleetObserver']({'t':template},services,{}, {'t':{'board_id':'a','provider':'worker'}})
    observation,readiness,leases=observer.collect(['a','b'],boards,members,NOW,{}, {})
    assert observation['executor_seats'][0]['lifecycle']=='stopped'
    assert all('seat' in readiness['boards'][b]['seats'] for b in ('a','b'))
    boards['b']['agents'][0]['lease_expires_at']=(NOW+timedelta(minutes=2)).isoformat()
    _,_,leases=observer.collect(['a','b'],boards,members,NOW,{}, {})
    assert all(leases['boards'][b]['seats']['seat']['work'] for b in ('a','b'))
    members['b']['members']=[]
    _,readiness,leases=observer.collect(['a','b'],boards,members,NOW,{}, {})
    assert 'seat' not in readiness['boards']['b']['seats']
    assert all('seat' not in leases['boards'][b]['seats'] for b in ('a','b'))


def test_stopped_seat_missing_registry_membership_is_not_a_start_candidate():
    template, boards, members, _ = fixture()
    services = SimpleNamespace(inspect=lambda *_: fleet_executor.ServiceObservation(True, False, False, True))
    members['b']['members'] = []
    observer = observer_api()['LocalFleetObserver']({'t': template}, services, {},
        {'t': {'board_id': 'a', 'provider': 'worker'}})
    observation, _, leases = observer.collect(['a', 'b'], boards, members, NOW,
        {'worker': {'status': 'healthy', 'latency_ms': 1}}, {})
    assert observation['executor_seats'][0]['lifecycle'] == 'unhealthy'
    assert all('seat' not in leases['boards'][b]['seats'] for b in ('a', 'b'))


def test_complete_coordination_scan_allows_truncated_ticket_payloads():
    template, boards, members, services = fixture()
    boards['b'].update(truncated=True, coordination_tickets_complete=True,
        coordination_tickets=[{'status': 'claimed'}])
    observer = observer_api()['LocalFleetObserver']({'t': template}, services, {},
        {'t': {'board_id': 'a', 'provider': 'worker'}})
    observation, _, leases = observer.collect(['a', 'b'], boards, members, NOW, {}, {})
    assert observation['executor_seats'][0]['lifecycle'] == 'busy'
    assert all(leases['boards'][b]['seats']['seat']['work'] for b in ('a', 'b'))
