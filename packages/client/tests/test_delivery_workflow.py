import pytest
from pursers_client.delivery_workflow import (
    activate_delivery_policy,
    compile_delivery_workflow,
    delivery_policy_revision,
    delivery_runtime_readiness,
    delivery_target,
    delivery_stage,
    parse_delivery_policy,
    parse_delivery_policy_activation,
    parse_delivery_workflow,
    resolve_delivery_policy,
)


def test_integration_defaults_and_human_promotions():
    p = parse_delivery_workflow({'mode': 'integration', 'base_branch': 'dev'})
    assert p['integration_branch'] == 'pursers-integration'
    assert p['base_branch'] == 'dev'
    assert 'production_branch' not in p
    assert delivery_target({'integration_ref': 'dev', 'delivery_workflow': p}) == 'pursers-integration'


@pytest.mark.parametrize('branch', ['', '-x', 'refs/heads/dev', 'a..b', 'a b', 'a~b', 'a.lock', 'a//b', '@', 'a@{b', '.hidden', 'a.', 'a\\b', 'a\n'])
def test_invalid_branch_rejected(branch):
    with pytest.raises(ValueError):
        parse_delivery_workflow({'mode': 'integration', 'base_branch': 'dev', 'integration_branch': branch})


@pytest.mark.parametrize('extra', [{'base_branch': 'pursers-integration'}, {'production_branch': 'dev'}, {'promotion_mode': 'automatic'}, {'auto_integrate': 'yes'}, {'unexpected': True}])
def test_unsafe_workflow_rejected(extra):
    with pytest.raises(ValueError):
        parse_delivery_workflow({'mode': 'integration', 'base_branch': 'dev', **extra})


def test_legacy_routing_stays_explicit():
    assert delivery_target({'integration_ref': 'release'}) == 'release'
    assert parse_delivery_workflow(None) is None


def test_delivery_stage_requires_explicit_evidence():
    assert delivery_stage('pr_created') == 'PR opened'
    assert delivery_stage('integration_merged') == 'Ready for your team'
    assert delivery_stage('unknown') == 'Delivery unconfirmed'


@pytest.mark.parametrize('value', [42, None, '', ' dev '])
def test_invalid_legacy_ref_is_not_coerced_to_a_delivery_branch(value):
    with pytest.raises(ValueError):
        delivery_target({'integration_ref':value})


def policy_registry():
    return {
        'delivery_policy_defaults': {
            'validation': {'test_commands': ['pytest -q'], 'required_reviewers': 2},
            'conflict_policy': 'pause',
        },
        'delivery_policy_groups': {
            'backend': {'mode': 'branch_only', 'final_pr_target': None,
                        'integration_branch': 'Pursers-Integration'},
        },
        'projects': {
            'api': {'integration_ref': 'Dev', 'delivery_policy_group': 'backend',
                    'delivery_policy': {'auto_integrate': False,
                                        'validation': {'test_commands': []}}},
            'web': {'integration_ref': 'main'},
        },
    }


def test_delivery_policy_resolves_global_group_repository_with_field_provenance():
    resolved = resolve_delivery_policy(policy_registry(), 'api')
    assert resolved['policy']['mode'] == 'branch_only'
    assert resolved['policy']['mapped_base'] == 'Dev'
    assert resolved['policy']['integration_branch'] == 'Pursers-Integration'
    assert resolved['policy']['validation']['test_commands'] == ['pytest -q']
    assert resolved['policy']['validation']['required_reviewers'] == 2
    assert resolved['policy']['auto_integrate'] is False
    assert resolved['provenance']['mode'] == 'group:backend'
    assert resolved['provenance']['auto_integrate'] == 'repository:api'
    assert resolve_delivery_policy(policy_registry(), 'web')['policy']['mode'] == 'per_ticket_pr'


@pytest.mark.parametrize('policy,match', [
    ({'release_trigger': {'kind': 'scheduled', 'timezone': 'Not/AZone', 'schedule': '0 9 * * 1'}}, 'timezone'),
    ({'release_trigger': {'kind': 'scheduled', 'timezone': 'UTC', 'schedule': 'daily'}}, 'five-field'),
    ({'validation': {'require_upstream_policies': False}}, 'cannot weaken'),
    ({'validation': {'independent_review': False}}, 'cannot weaken'),
    ({'mode': 'branch_only', 'final_pr_target': ''}, 'valid short'),
])
def test_delivery_policy_invalid_or_weakening_values_fail_closed(policy, match):
    with pytest.raises(ValueError, match=match):
        parse_delivery_policy(policy)


def test_delivery_policy_false_empty_and_null_have_deliberate_meanings():
    parsed = parse_delivery_policy({'auto_integrate': False, 'validation': {'test_commands': []},
                                    'final_pr_target': None})
    assert parsed == {'auto_integrate': False, 'validation': {'test_commands': []},
                      'final_pr_target': None}


def test_runtime_gating_compiles_only_deployed_modes_without_fallback():
    per_ticket = resolve_delivery_policy({'projects': {'api': {'integration_ref': 'main'}}}, 'api')['policy']
    assert delivery_runtime_readiness(per_ticket)['ready']
    assert compile_delivery_workflow(per_ticket) == ({'mode': 'direct'}, 'main')
    batch = {**per_ticket, 'mode': 'batch_pr', 'release_trigger': {'kind': 'manual'}}
    readiness = delivery_runtime_readiness(batch)
    assert not readiness['ready']
    assert any('manual resident release path' in item for item in readiness['blockers'])
    with pytest.raises(ValueError, match='configuration-only'):
        compile_delivery_workflow(batch)
    branch_only = {**per_ticket, 'mode': 'branch_only', 'final_pr_target': None,
                   'pr_update': 'freeze_on_ready'}
    readiness = delivery_runtime_readiness(branch_only)
    assert readiness['ready']
    with pytest.raises(ValueError, match='delivery_policy_activation'):
        compile_delivery_workflow(branch_only)


def test_delivery_policy_activation_is_deterministic_bounded_and_explicit():
    policy = resolve_delivery_policy(
        {'projects': {'api': {'integration_ref': 'main'}}}, 'api'
    )['policy']
    record = activate_delivery_policy(policy)
    assert record == parse_delivery_policy_activation(record)
    assert record['policy_revision'] == delivery_policy_revision(policy)
    assert activate_delivery_policy(policy) == record
    with pytest.raises(ValueError, match='policy_revision'):
        parse_delivery_policy_activation({**record, 'policy_revision': 'ABC'})
    with pytest.raises(ValueError, match='unsupported or missing'):
        parse_delivery_policy_activation({**record, 'extra': True})


def test_existing_integration_workflow_projects_as_branch_only_until_migrated():
    resolved = resolve_delivery_policy({'projects': {'api': {
        'integration_ref': 'Pursers-Integration',
        'delivery_workflow': {'mode': 'integration', 'base_branch': 'Dev',
                              'integration_branch': 'Pursers-Integration',
                              'auto_integrate': True, 'collection_paused': False},
    }}}, 'api')
    assert resolved['policy']['mode'] == 'branch_only'
    assert resolved['policy']['mapped_base'] == 'Dev'
    assert resolved['policy']['final_pr_target'] is None
    assert resolved['policy']['auto_integrate'] is True
    assert resolved['runtime']['ready'] is False
