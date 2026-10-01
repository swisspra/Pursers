"""Guided delivery policy planning; branch changes are explicit and CAS guarded."""
from __future__ import annotations
import copy
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from pursers_client.delivery_workflow import parse_delivery_workflow


def delivery_route_changed(existing, proposed):
    before = parse_delivery_workflow(existing.get('delivery_workflow')) or {}
    after = parse_delivery_workflow(proposed.get('delivery_workflow')) or {}
    return (existing.get('integration_ref', 'main') != proposed.get('integration_ref', 'main')
            or any(before.get(k) != after.get(k) for k in ('mode', 'base_branch', 'integration_branch')))


def public_delivery_settings(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{'name': name, 'board_id': row['board_id'], 'status': row['status'],
             'integration_ref': row.get('integration_ref', 'main'),
             'repository_configured': bool(row.get('repository_url')),
             'delivery_workflow': parse_delivery_workflow(row.get('delivery_workflow')) or {'mode': 'direct'}}
            for name, row in sorted(registry['projects'].items())]


def remote_branches(entry: Mapping[str, Any], runner=subprocess.run) -> dict[str, str]:
    repo = entry.get('fleet_clone_dir') or entry['work_dir']
    result = runner(['git', '-C', repo, 'remote', 'get-url', 'origin'], capture_output=True, text=True, timeout=30)
    if result.returncode or result.stdout.strip() != entry.get('repository_url'):
        raise ValueError('Repository checkout origin does not match the registered repository')
    result = runner(['git', '-C', repo, 'ls-remote', '--heads', 'origin'], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('Remote branches unavailable; check repository access on the dashboard host')
    refs = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith('refs/heads/'):
            refs[parts[1][11:]] = parts[0]
    return refs


def build_delivery_plan(*, request, registry, registry_expected_sha256, actor, central, observation, refs):
    if not {'action', 'name', 'delivery_workflow'} <= set(request) or set(request) - {'action', 'name', 'delivery_workflow', 'use_as_default'}:
        raise ValueError('Delivery settings require action, name and delivery_workflow')
    if type(request.get('use_as_default', False)) is not bool:
        raise ValueError('use_as_default must be boolean')
    name = request['name']
    existing = registry.get('projects', {}).get(name)
    if not isinstance(existing, Mapping) or not existing.get('repository_url'):
        raise ValueError('Select a registered Git repository before configuring delivery')
    policy = parse_delivery_workflow(request['delivery_workflow'])
    if policy is None or policy['mode'] != 'integration':
        raise ValueError('Use integration delivery; legacy projects remain unchanged until configured')
    proposed = copy.deepcopy(dict(existing))
    proposed.update(delivery_workflow=policy, integration_ref=policy['integration_branch'])
    blockers = []
    if not observation.get('complete'):
        blockers.append('Active work observation is incomplete; refresh before changing delivery')
    routing_changed = delivery_route_changed(existing, proposed)
    if routing_changed and (observation.get('active_tickets') or observation.get('pending_offers')):
        blockers.append('Finish or pause active work and pending offers before changing delivery settings')
    if policy['base_branch'] not in refs:
        blockers.append('Mapped base branch was not found on the registered remote')
    previous = existing.get('delivery_workflow') or {}
    if policy['integration_branch'] in refs and previous.get('integration_branch') != policy['integration_branch']:
        blockers.append('Delivery branch already exists outside this project policy; choose a new branch name')
    folded = policy['integration_branch'].casefold()
    if any(name != policy['integration_branch'] and (name.casefold() == folded or name.casefold().startswith(folded + '/') or folded.startswith(name.casefold() + '/')) for name in refs):
        blockers.append('Delivery branch conflicts with an existing branch namespace, including case differences')
    create = None
    if policy['integration_branch'] not in refs and policy['base_branch'] in refs:
        create = {'name': policy['integration_branch'], 'base_sha': refs[policy['base_branch']]}
    now = datetime.now(timezone.utc)
    return {'schema_version': 1, 'kind': 'project-delivery', 'created_at': now.isoformat(),
            'expires_at': (now + timedelta(minutes=10)).isoformat(), 'actor': actor, 'central': central,
            'project': name, 'board_id': existing['board_id'], 'registry_expected_sha256': registry_expected_sha256,
            'existing_entry': copy.deepcopy(dict(existing)), 'proposed_entry': proposed,
            'delivery_workflow': policy, 'use_as_default': request.get('use_as_default', False), 'observed_refs': refs, 'create_branch': create,
            'board_observation': dict(observation), 'blocked': bool(blockers), 'blockers': blockers,
            'confirmation': f'CONFIGURE {name}',
            'operations': ([{'operation_id': 'integration-branch', 'effect': 'Create delivery branch from mapped base', 'target': policy['integration_branch']}] if create else []) +
                          [{'operation_id': 'registry', 'effect': 'Update delivery route', 'target': 'project_registry', 'before': dict(existing), 'after': proposed, 'changed_fields': ['integration_ref', 'delivery_workflow']}],
            'warnings': ['Existing PRs keep their targets. Pursers never merges into the mapped base or environment branches.',
                         'Automatic integration requires the configured connector and successful validation policies.'],
            'preserved': ['ticket history and live leases', 'source analysis mapping', 'credentials and unrelated project settings'],
            'rollback': ['Restore the previous policy through a new preview. A newly created branch is retained for inspection.']}


def prepare_delivery_branch(plan, runner=subprocess.run):
    """Create only an absent branch; never overwrite an existing ref."""
    entry = plan['existing_entry']
    current = remote_branches(entry, runner=runner)
    if current != plan['observed_refs']:
        raise ValueError('Remote branches changed after preview; refresh the plan')
    create = plan.get('create_branch')
    if not create:
        return
    repo = entry.get('fleet_clone_dir') or entry['work_dir']
    fetch = runner(['git', '-C', repo, 'fetch', 'origin', create['base_sha']], capture_output=True, text=True, timeout=60)
    if fetch.returncode:
        raise ValueError('Could not fetch the verified base commit')
    ref = 'refs/heads/' + create['name']
    # The empty lease requires ABSENCE. This cannot rewrite an existing branch.
    result = runner(['git', '-C', repo, 'push', '--force-with-lease=' + ref + ':', 'origin', create['base_sha'] + ':' + ref], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('Branch creation was not confirmed; inspect the remote before retrying')
    if remote_branches(entry, runner=runner).get(create['name']) != create['base_sha']:
        raise ValueError('Created branch SHA could not be confirmed; inspect the remote')
