"""Only inactivity-stale, stopped original seats may re-enter startup."""
import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_fleet_observation import fixture, observer_api
from test_fleet_reconciler import NOW, fleet_executor, butler


def collect(fault=None):
    template, boards, members, _ = fixture()
    services = SimpleNamespace(inspect=lambda *_: fleet_executor.ServiceObservation(
        True, False, False, True))
    for board in boards.values():
        board['agents'][0].update(lifecycle_status='stale',
            lifecycle_reason='inactivity_threshold',
            last_activity_at=(NOW-timedelta(days=4)).isoformat())
    if fault == 'running':
        services.inspect = lambda *_: fleet_executor.ServiceObservation(True, True, True, True)
    elif fault == 'unverified':
        services.inspect = lambda *_: fleet_executor.ServiceObservation(True, False, False, False)
    elif fault == 'missing': members['b']['members'] = []
    elif fault == 'principal': boards['b']['agents'][0]['principal_id'] = 'other'
    elif fault == 'capability': boards['b']['agents'][0]['capabilities'] = {}
    elif fault == 'retired': boards['b']['agents'][0]['lifecycle_status'] = 'retired'
    elif fault == 'reason': boards['b']['agents'][0]['lifecycle_reason'] = 'operator_disabled'
    elif fault == 'truncated': boards['b']['truncated'] = True
    elif fault == 'unready': boards['b']['agents'][0]['readiness'] = {'reported': True, 'dispatch_ready': False}
    elif fault == 'work': boards['b']['tickets'] = [{'status': 'claimed', 'claimed_by_agent_id': 'agent'}]
    elif fault == 'review': boards['b']['tickets'] = [{'status': 'submitted', 'review_lease': {'reviewer_agent_id': 'agent'}}]
    elif fault == 'unknown_lease': boards['b']['tickets'] = [{'status': 'claimed'}]
    elif fault == 'malformed_expiry': boards['b']['agents'][0]['lease_expires_at'] = 'invalid'
    elif fault == 'live_expiry': boards['b']['agents'][0]['lease_expires_at'] = (NOW+timedelta(minutes=5)).isoformat()
    observer = observer_api()['LocalFleetObserver']({'t': template}, services, {},
        {'t': {'board_id': 'a', 'provider': 'worker'}})
    return observer.collect(['a', 'b'], boards, members, NOW, {}, {})


def test_inactivity_stale_original_stopped_seat_can_restart_without_forging_active():
    observation, readiness, leases = collect()
    assert observation['executor_seats'][0]['lifecycle'] == 'stopped'
    for board in ('a', 'b'):
        record = readiness['boards'][board]['seats']['seat']
        assert record['lifecycle_status'] == 'stale'
        assert record['restart_eligible'] is True
        assert record['lifecycle_reason'] == 'inactivity_threshold'
        assert leases['boards'][board]['seats']['seat']['work'] is False


@pytest.mark.parametrize('fault', ['running', 'unverified', 'missing', 'principal',
    'capability', 'retired', 'reason', 'truncated', 'unready', 'work', 'review',
    'unknown_lease', 'malformed_expiry', 'live_expiry'])
def test_stale_restart_fails_closed(fault):
    observation, readiness, _ = collect(fault)
    assert observation['executor_seats'][0]['lifecycle'] == 'unhealthy'
    assert not any(row.get('restart_eligible') for board in readiness['boards'].values()
                   for row in board['seats'].values())


def test_local_fleet_reads_stale_inventory_on_every_board_without_live_dispatch():
    import asyncio
    from contextlib import asynccontextmanager
    template, boards, members, services = fixture()
    services.inspect = lambda *_: fleet_executor.ServiceObservation(True, False, False, True)
    for board in boards.values():
        board['agents'][0].update(lifecycle_status='stale',
            lifecycle_reason='inactivity_threshold')
    class Client:
        def __init__(self, board): self.board = board
        async def board_snapshot(self, **kwargs):
            return copy.deepcopy(boards[self.board] if kwargs.get('include_retired') else
                {'agents': [], 'tickets': []})
        async def _call(self, name, args):
            assert name == 'board_members' and args == {}
            return members[self.board]
    backend = butler.CentralBackend(SimpleNamespace(), 'unused')
    @asynccontextmanager
    async def client_for_board(board): yield Client(board)
    backend._client_for_board = client_for_board
    inventory, memberships = asyncio.run(backend._fleet_registry_inventory(['a', 'b'], boards))
    observer = observer_api()['LocalFleetObserver']({'t': template}, services, {},
        {'t': {'board_id': 'a', 'provider': 'worker'}})
    observation, readiness, _ = observer.collect(['a', 'b'], inventory, memberships, NOW, {}, {})
    assert observation['executor_seats'][0]['lifecycle'] == 'stopped'
    assert all(readiness['boards'][b]['seats']['seat']['restart_eligible'] for b in ('a', 'b'))
    assert not butler._capable_live_worker(inventory['a']['agents'][0], NOW)


def test_running_stale_process_is_not_ready_during_self_join():
    template, boards, members, _ = fixture()
    for board in boards.values():
        board['agents'][0].update(lifecycle_status='stale',
            lifecycle_reason='inactivity_threshold')
    services = SimpleNamespace(inspect=lambda *_: fleet_executor.ServiceObservation(
        True, True, True, True))
    observer = observer_api()['LocalFleetObserver']({'t': template}, services,
        {'seat': {'lifecycle': 'starting'}}, {'t': {'board_id': 'a', 'provider': 'worker'}})
    observation, readiness, _ = observer.collect(['a', 'b'], boards, members, NOW, {}, {})
    assert observation['executor_seats'][0]['lifecycle'] != 'ready'
    assert not any(row.get('restart_eligible') for board in readiness['boards'].values()
                   for row in board['seats'].values())


def test_complete_ticket_scan_does_not_hide_truncated_agent_inventory():
    template, boards, members, services = fixture()
    boards['b'].update(truncated=True, omitted_counts={'agents': 1},
        coordination_tickets=[], coordination_tickets_complete=True)
    observer = observer_api()['LocalFleetObserver']({'t': template}, services, {},
        {'t': {'board_id': 'a', 'provider': 'worker'}})
    observation, _, _ = observer.collect(['a', 'b'], boards, members, NOW, {}, {})
    assert observation['executor_seats'][0]['lifecycle'] == 'unhealthy'
