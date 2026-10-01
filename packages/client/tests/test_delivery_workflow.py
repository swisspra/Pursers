import pytest
from pursers_client.delivery_workflow import parse_delivery_workflow, delivery_target, delivery_stage


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
