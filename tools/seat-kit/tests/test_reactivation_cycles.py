"""Exercise failed joins across real observation, signing and reconciliation."""
import copy
import importlib.util
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_fleet_executor import runtime, signed_request, executor, NOW

BUTLER_TESTS = Path(__file__).resolve().parents[2]/'board-butler/tests'
sys.path.insert(0, str(BUTLER_TESTS))
from test_fleet_reconciler import butler, board_policy, FINGERPRINT

SPEC = importlib.util.spec_from_file_location('reactivation_observer', BUTLER_TESTS.parent/'fleet_observation.py')
observer_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer_api)


class ReactivationCycle:
    def __init__(self, runtime, partial):
        self.rt = runtime
        self.service, self.adapter, self.template = (runtime[k] for k in ('service', 'adapter', 'template'))
        self.clock = NOW
        self.root = runtime['tmp_path']
        self.service.clock = lambda: self.clock
        self.service.readiness = executor.FileRegistryReadinessProvider(self.root/'ready.json', clock=lambda: self.clock)
        self.service.leases = executor.FileLeaseProvider(self.root/'leases.json', clock=lambda: self.clock)
        agent = {'agent_id': 'original', 'agent_name': 'worker-a', 'principal_id': self.template.principal_id,
            'role': self.template.role, 'capabilities': dict(self.template.capabilities), 'status': 'idle',
            'lifecycle_status': 'stale', 'lifecycle_reason': 'inactivity_threshold',
            'last_activity_at': (datetime.fromtimestamp(NOW, timezone.utc)-timedelta(days=4)).isoformat()}
        self.boards = {b: {'agents': [copy.deepcopy(agent)], 'tickets': []} for b in ('pursers', 'b')}
        self.boards['pursers']['tickets'] = [{'ticket_id': 'TK-synthetic-demand', 'tier': 1, 'status': 'open',
            'created_at': datetime.fromtimestamp(NOW, timezone.utc).isoformat()}]
        self.members = {b: {'members': [{'principal_id': self.template.principal_id, 'role': 'member'}]} for b in self.boards}
        start = self.adapter.start
        def join(seat, template):
            start(seat, template)
            if partial:
                self.activate('pursers')
        self.adapter.start = join
        policy = replace(board_policy('pursers'), approved_template_ids=frozenset({self.template.template_id}))
        self.engine = butler.FleetReconciler(butler.FleetHostPolicy(3, 0, 3), {'pursers': policy},
            config_revision=7, authorization_fingerprint_sha256=FINGERPRINT, max_operation_attempts=3)
        self.state = butler.MemoryFleetStateStore()
        self.operations = []

    def activate(self, board):
        self.boards[board]['agents'][0].update(lifecycle_status='active',
            last_activity_at=datetime.fromtimestamp(self.clock, timezone.utc).isoformat())

    def execute(self, operation):
        self.operations.append(operation)
        return self.service.handle(signed_request(self.rt, operation.action, operation.operation_id,
            generation=operation.expected_seat_generation, now=self.clock))

    def collect(self):
        store = self.service.store
        observer = observer_api.LocalFleetObserver({self.template.template_id: self.template}, self.adapter,
            store.observation_snapshot(), {self.template.template_id: {'board_id': 'pursers',
                'provider': 'direct', 'seat_id': 'worker-a'}}, recover=store.reconcile_unexpected_stop,
            confirm_ready=store.confirm_registry_ready)
        now = datetime.fromtimestamp(self.clock, timezone.utc)
        ob, ready, leases = observer.collect(['pursers', 'b'], self.boards, self.members, now,
            {'direct': {'status': 'healthy', 'latency_ms': 1}},
            {'load_ratio': .1, 'capacity_available': True, 'executor_status': 'healthy'})
        observer_api.publish(self.root/'ready.json', ready)
        observer_api.publish(self.root/'leases.json', leases)
        snapshot = butler.fleet_snapshot_from_products({'pursers': self.boards['pursers']},
            ob['executor_seats'], ob['provider_observations'], ob['host_observation'], now)
        return ob, self.engine.reconcile(snapshot, self.state, self)

    def fail_process(self):
        self.clock += 1
        self.collect()
        self.adapter.observations['worker-a'] = executor.ServiceObservation(True, False, False, True)
        self.clock += 1
        self.collect()
        self.clock += 6

    def reopen_store(self):
        rows = self.service.store.connection.execute('PRAGMA database_list').fetchall()
        path = Path(next(row[2] for row in rows if row[1] == 'main'))
        self.service.store.connection.close()
        self.service.store = executor.ExecutorStore(path)


@pytest.mark.parametrize('partial', [False, True])
def test_failed_join_episode_is_bounded_across_generations_and_store_restart(runtime, partial):
    cycle = ReactivationCycle(runtime, partial)
    for attempt in range(8):
        cycle.collect()
        cycle.fail_process()
        if attempt == 1:
            cycle.reopen_store()
    starts = [op for op in cycle.operations if op.action == 'start']
    assert len(starts) == 3
    assert [op.expected_seat_generation for op in starts] == [1, 2, 3]
    assert cycle.service.store.seat('worker-a')['generation'] == 4
    ob, report = cycle.collect()
    assert ob['executor_seats'][0]['reason_code'] == 'registry_reactivation_exhausted'
    assert report['operations'] == []
    # A caller cannot bypass the bound using a new authenticated operation ID.
    receipt = cycle.service.handle(signed_request(runtime, 'start', 'bypass-episode', generation=4, now=cycle.clock))
    assert receipt['outcome'] == 'rejected'
    assert not cycle.adapter.observations['worker-a'].running


def test_actual_all_board_ready_observation_resets_reactivation_episode(runtime):
    cycle = ReactivationCycle(runtime, False)
    for _ in range(2):
        cycle.collect()
        cycle.fail_process()
    cycle.collect()  # third start is running but has not joined yet
    assert cycle.service.store.seat('worker-a')['reactivation_attempts'] == 3
    for board in cycle.boards:
        cycle.activate(board)
    ob, _ = cycle.collect()
    assert ob['executor_seats'][0]['lifecycle'] == 'ready'
    assert cycle.service.store.seat('worker-a')['reactivation_attempts'] == 0
    # A subsequent independent inactivity episode gets its own bounded budget.
    cycle.adapter.observations['worker-a'] = executor.ServiceObservation(True, False, False, True)
    cycle.clock += 1
    for board in cycle.boards.values():
        board['agents'][0]['lifecycle_status'] = 'stale'
    cycle.collect()
    cycle.clock += 6
    _, report = cycle.collect()
    assert report['receipts'][0]['outcome'] == 'succeeded'
    assert cycle.service.store.seat('worker-a')['reactivation_attempts'] == 1


@pytest.mark.parametrize('lease', [executor.LeaseObservation(False),
    executor.LeaseObservation(True, live_work=True), executor.LeaseObservation(True, live_review=True)])
def test_active_readback_pending_episode_requires_all_board_lease_evidence(runtime, lease):
    cycle = ReactivationCycle(runtime, False)
    cycle.collect()
    for board in cycle.boards:
        cycle.activate(board)
    cycle.adapter.observations['worker-a'] = executor.ServiceObservation(True, False, False, True)
    cycle.clock += 1
    cycle.collect()  # recover generation without any live ready confirmation
    cycle.clock += 6
    cycle.service.leases.observe = lambda board, seat: lease if board == 'b' else executor.LeaseObservation(True)
    receipt = cycle.service.handle(signed_request(runtime, 'start', 'active-pending-retry',
        generation=2, now=cycle.clock))
    assert receipt['outcome'] == 'rejected'
    assert receipt['reason_code'] in {'live_lease', 'lease_state_unknown'}
    assert cycle.service.store.seat('worker-a')['reactivation_attempts'] == 1
    assert not cycle.adapter.observations['worker-a'].running


def test_stopped_active_pending_episode_with_unknown_holder_is_not_planned(runtime):
    cycle = ReactivationCycle(runtime, False)
    cycle.collect()
    for board in cycle.boards:
        cycle.activate(board)
    cycle.adapter.observations['worker-a'] = executor.ServiceObservation(True, False, False, True)
    cycle.clock += 1
    cycle.collect()
    cycle.clock += 6
    cycle.boards['b']['tickets'] = [{'ticket_id': 'TK-unknown', 'status': 'claimed'}]
    observation, report = cycle.collect()
    assert observation['executor_seats'][0]['lifecycle'] == 'unhealthy'
    assert report['operations'] == []
    assert cycle.service.store.seat('worker-a')['reactivation_attempts'] == 1
