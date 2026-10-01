"""Shared, fail-closed repository delivery policy and evidence stage vocabulary."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import re

DELIVERY_STAGES = {
    'pr_pending': 'PR pending', 'pr_blocked': 'Delivery blocked',
    'pr_uncertain': 'Delivery unconfirmed', 'pr_created': 'PR opened',
    'integration_pending': 'Awaiting integration',
    'integration_blocked': 'Integration blocked',
    'integration_merged': 'Ready for your team',
    'delivery_recorded': 'Delivery recorded',
}


def branch_name(value: Any, field: str = 'branch') -> str:
    """Validate a short branch name without shell execution or ref ambiguity."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 200
            or value.startswith(('-', '/', 'refs/')) or value.endswith(('/', '.'))
            or value == '@' or '..' in value or '@{' in value
            or re.search(r'[\x00-\x20\x7f~^:?*\[\\]', value)
            or any(not part or part.startswith('.') or part.endswith('.lock')
                   for part in value.split('/'))):
        raise ValueError(f'{field} must be a valid short Git branch name')
    return value


def parse_delivery_workflow(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    allowed = {'mode', 'integration_branch', 'base_branch', 'auto_integrate', 'collection_paused'}
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ValueError('delivery_workflow contains unsupported fields')
    if value.get('mode') not in {'direct', 'integration'}:
        raise ValueError('delivery_workflow.mode must be direct or integration')
    if value['mode'] == 'direct':
        if set(value) != {'mode'}:
            raise ValueError('direct delivery does not accept integration settings')
        return {'mode': 'direct'}
    result = {'mode': 'integration'}
    for key, default in [('integration_branch', 'pursers-integration'), ('base_branch', None)]:
        result[key] = branch_name(value.get(key, default), key)
    if result['integration_branch'].casefold() == result['base_branch'].casefold():
        raise ValueError('Delivery branch must be distinct from the mapped base branch')
    for key, default in [('auto_integrate', False), ('collection_paused', False)]:
        selected = value.get(key, default)
        if type(selected) is not bool:
            raise ValueError(f'delivery_workflow.{key} must be boolean')
        result[key] = selected
    return result


def delivery_target(project: Mapping[str, Any] | None) -> str:
    project = project or {}
    policy = parse_delivery_workflow(project.get('delivery_workflow'))
    if policy and policy['mode'] == 'integration':
        return policy['integration_branch']
    ref = project.get('integration_ref', 'main')
    if not isinstance(ref, str) or not ref or ref != ref.strip():
        raise ValueError('integration_ref must be a non-empty, trimmed ref')
    return ref


def integration_policy(project: Mapping[str, Any] | None) -> dict[str, Any] | None:
    policy = parse_delivery_workflow((project or {}).get('delivery_workflow'))
    return policy if policy and policy['mode'] == 'integration' else None


def delivery_stage(state: str) -> str:
    return DELIVERY_STAGES.get(state, 'Delivery unconfirmed')
