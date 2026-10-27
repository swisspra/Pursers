"""Stale readback is startup-only, never live registry readiness."""
import json
from datetime import datetime, timezone

import pytest
from test_fleet_executor import runtime, signed_request, executor, NOW


def provider(runtime):
    template = runtime['template']
    seat = {'principal_id': template.principal_id, 'role': template.role,
        'membership_role': 'member', 'capabilities': dict(template.capabilities),
        'lifecycle_status': 'stale', 'lifecycle_reason': 'inactivity_threshold',
        'restart_eligible': True}
    document = {'schema': 'pursers_registry_readiness_v1',
        'stale_after': datetime.fromtimestamp(NOW+60, timezone.utc).isoformat(),
        'selected_active_boards': ['pursers', 'b'],
        'boards': {b: {'seats': {'worker-a': dict(seat)}} for b in ('pursers', 'b')}}
    path = runtime['tmp_path']/'restart-readiness.json'
    path.write_text(json.dumps(document))
    path.chmod(0o600)
    result = executor.FileRegistryReadinessProvider(path, clock=lambda: NOW)
    runtime['service'].readiness = result
    return result, path, document


def test_stale_readback_is_restart_eligible_but_not_ready(runtime):
    result, _, _ = provider(runtime)
    observed = result.observe('pursers', 'worker-a', runtime['template'])
    assert observed.known and not observed.ready
    assert observed.restart_eligible
    assert observed.selected_active_boards == ('pursers', 'b')


@pytest.mark.parametrize('fault', ['marker', 'reason', 'retired', 'principal', 'capability'])
def test_stale_readback_needs_exact_all_board_restart_evidence(runtime, fault):
    result, path, document = provider(runtime)
    seat = document['boards']['b']['seats']['worker-a']
    if fault == 'marker': seat.pop('restart_eligible')
    elif fault == 'reason': seat['lifecycle_reason'] = 'operator_disabled'
    elif fault == 'retired': seat['lifecycle_status'] = 'retired'
    elif fault == 'principal': seat['principal_id'] = 'other'
    else: seat['capabilities']['can_work'] = False
    path.write_text(json.dumps(document))
    observed = result.observe('pursers', 'worker-a', runtime['template'])
    assert not observed.ready and not getattr(observed, 'restart_eligible', False)


def test_stale_restart_self_join_must_become_active_before_success(runtime):
    result, path, document = provider(runtime)
    adapter = runtime['adapter']
    start = adapter.start
    def join_on_start(seat_id, template):
        start(seat_id, template)
        for board in document['boards'].values():
            board['seats']['worker-a']['lifecycle_status'] = 'active'
        path.write_text(json.dumps(document))
    adapter.start = join_on_start
    receipt = runtime['service'].handle(signed_request(runtime, 'start', 'stale-start'))
    assert receipt['outcome'] == 'succeeded'
    assert result.observe('pursers', 'worker-a', runtime['template']).ready


@pytest.mark.parametrize('lease', [executor.LeaseObservation(False),
    executor.LeaseObservation(True, live_work=True),
    executor.LeaseObservation(True, live_review=True)])
def test_stale_restart_requires_known_lease_free_state_before_mutation(runtime, lease):
    provider(runtime)
    runtime['leases'].observation = lease
    receipt = runtime['service'].handle(signed_request(runtime, 'start', 'stale-lease'))
    assert receipt['outcome'] == 'rejected'
    assert receipt['reason_code'] in ('live_lease', 'lease_state_unknown')
    assert not any(call[0] in ('start', 'instantiate') for call in runtime['adapter'].calls)


def test_stale_start_cannot_authorize_running_process_or_live_inspection(runtime):
    provider(runtime)
    runtime['adapter'].observations['worker-a'] = executor.ServiceObservation(True, True, True, True, 'pid:1')
    for action in ('start', 'inspect'):
        receipt = runtime['service'].handle(signed_request(runtime, action, 'stale-'+action))
        assert receipt['outcome'] == 'rejected'


def test_stale_start_waits_for_next_product_observation_without_reporting_ready(runtime):
    result, _, _ = provider(runtime)
    receipt = runtime['service'].handle(signed_request(runtime, 'start', 'stale-pending'))
    assert receipt['outcome'] == 'succeeded'
    assert runtime['service'].store.seat('worker-a')['lifecycle'] == 'starting'
    assert not result.observe('pursers', 'worker-a', runtime['template']).ready
    inspected = runtime['service'].handle(signed_request(runtime, 'inspect', 'pending-inspect'))
    assert inspected['outcome'] == 'rejected'


def test_stale_restart_checks_lease_on_every_selected_board(runtime):
    provider(runtime)
    runtime['leases'].observe = lambda board, seat: executor.LeaseObservation(
        True, live_work=board == 'b')
    receipt = runtime['service'].handle(signed_request(runtime, 'start', 'cross-board-lease'))
    assert receipt['outcome'] == 'rejected' and receipt['reason_code'] == 'live_lease'
    assert not any(call[0] in ('start', 'instantiate') for call in runtime['adapter'].calls)


def test_stale_restart_rolls_back_when_post_start_evidence_is_revoked(runtime):
    _, path, document = provider(runtime)
    adapter = runtime['adapter']
    start = adapter.start
    def revoke_on_start(seat_id, template):
        start(seat_id, template)
        document['boards']['b']['seats'].clear()
        path.write_text(json.dumps(document))
    adapter.start = revoke_on_start
    receipt = runtime['service'].handle(signed_request(runtime, 'start', 'revoked-start'))
    assert receipt['outcome'] == 'failed'
    assert ('stop', 'worker-a') in adapter.calls
    assert runtime['service'].store.seat('worker-a')['lifecycle'] == 'stopped'
