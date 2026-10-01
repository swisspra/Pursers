"""Guided delivery policy planning; branch changes are explicit and CAS guarded."""
from __future__ import annotations
import copy
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from pursers_client.delivery_workflow import (
    activate_delivery_policy,
    compile_delivery_workflow,
    delivery_policy_revision,
    delivery_group_name,
    parse_delivery_policy_activation,
    parse_delivery_policy,
    parse_delivery_workflow,
    resolve_delivery_policy,
)


def delivery_route_changed(existing, proposed):
    before = parse_delivery_workflow(existing.get('delivery_workflow')) or {}
    after = parse_delivery_workflow(proposed.get('delivery_workflow')) or {}
    return (existing.get('integration_ref', 'main') != proposed.get('integration_ref', 'main')
            or any(before.get(k) != after.get(k) for k in ('mode', 'base_branch', 'integration_branch')))


def public_delivery_settings(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    groups = sorted((registry.get('delivery_policy_groups') or {}).keys())
    for name, row in sorted(registry['projects'].items()):
        resolved = resolve_delivery_policy(registry, name)
        activation = parse_delivery_policy_activation(row.get('delivery_policy_activation'))
        policy = resolved['policy']
        legacy = parse_delivery_workflow(row.get('delivery_workflow'))
        active = (
            policy['mode'] == 'per_ticket_pr'
            and (legacy is None or legacy['mode'] == 'direct')
            and row.get('integration_ref', 'main') == policy['mapped_base']
        ) or bool(
            activation
            and activation['policy_revision'] == delivery_policy_revision(policy)
        )
        rows.append({'name': name, 'board_id': row['board_id'], 'status': row['status'],
                     'integration_ref': row.get('integration_ref', 'main'),
                     'repository_configured': bool(row.get('repository_url')),
                     'delivery_workflow': parse_delivery_workflow(row.get('delivery_workflow')) or {'mode': 'direct'},
                     'delivery_policy': policy,
                     'delivery_policy_overrides': resolved['overrides'],
                     'delivery_policy_group': resolved['group'],
                     'delivery_policy_provenance': resolved['provenance'],
                     'delivery_runtime': resolved['runtime'],
                     'delivery_policy_activation': activation,
                     'delivery_policy_active': active,
                     'delivery_policy_groups': groups})
    return rows


def remote_branches(entry: Mapping[str, Any], runner=subprocess.run) -> dict[str, str]:
    repo = entry.get('fleet_clone_dir') or entry['work_dir']
    # Read the configured identity, not Git's insteadOf-expanded transport URL.
    result = runner(['git', '-C', repo, 'config', '--get', 'remote.origin.url'], capture_output=True, text=True, timeout=30)
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


def _build_legacy_delivery_plan(*, request, registry, registry_expected_sha256, actor, central, observation, refs):
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


def _policy_branch_plan(policy, existing, refs):
    blockers = []
    create = None
    for field in ('mapped_base', 'final_pr_target'):
        selected = policy.get(field)
        if selected is not None and selected not in refs:
            blockers.append(f'{field.replace("_", " ").title()} was not found on the registered remote')
    if policy['mode'] in {'batch_pr', 'branch_only'}:
        branch = policy['integration_branch']
        activation = parse_delivery_policy_activation(existing.get('delivery_policy_activation'))
        branch_is_owned = bool(
            activation
            and activation['policy_revision'] == delivery_policy_revision(policy)
        )
        if branch in refs and not branch_is_owned:
            blockers.append('Delivery branch already exists outside this project policy; choose a new branch name')
        folded = branch.casefold()
        if any(name != branch and (name.casefold() == folded
                or name.casefold().startswith(folded + '/')
                or folded.startswith(name.casefold() + '/')) for name in refs):
            blockers.append('Delivery branch conflicts with an existing branch namespace, including case differences')
        if branch not in refs and policy['mapped_base'] in refs:
            create = {'name': branch, 'base_sha': refs[policy['mapped_base']]}
    prefix = policy['snapshot_branch_prefix'].casefold()
    if any(name.casefold() == prefix or prefix.startswith(name.casefold() + '/') for name in refs):
        blockers.append('Snapshot branch prefix conflicts with an existing branch namespace')
    return blockers, create


def _build_policy_plan(*, request, registry, registry_expected_sha256, actor, central, observation, refs):
    allowed = {'action', 'name', 'scope', 'delivery_policy', 'delivery_policy_group',
               'activate', 'reset_to_inherit'}
    if set(request) - allowed:
        raise ValueError('Delivery policy request contains unsupported fields')
    scope = request.get('scope', 'repository')
    if scope not in {'repository', 'global', 'group'}:
        raise ValueError('delivery policy scope must be repository, group or global')
    activate = request.get('activate', False)
    reset = request.get('reset_to_inherit', False)
    if type(activate) is not bool or type(reset) is not bool:
        raise ValueError('activate and reset_to_inherit must be boolean')
    if scope != 'repository' and activate:
        raise ValueError('shared policy edits are configuration-only; activate repositories separately')
    registry_after = copy.deepcopy(registry)
    projects = registry_after.get('projects', {})
    name = request.get('name')
    affected: list[str]
    existing = projects.get(name) if scope == 'repository' else None
    if scope == 'repository':
        if not isinstance(existing, Mapping) or not existing.get('repository_url'):
            raise ValueError('Select a registered Git repository before configuring delivery')
        if reset:
            existing.pop('delivery_policy', None)
        else:
            existing['delivery_policy'] = parse_delivery_policy(request.get('delivery_policy', {})) or {}
        if 'delivery_policy_group' in request:
            group = request['delivery_policy_group']
            if group is None:
                existing.pop('delivery_policy_group', None)
            else:
                existing['delivery_policy_group'] = delivery_group_name(group)
        affected = [str(name)]
    elif scope == 'global':
        if reset:
            registry_after.pop('delivery_policy_defaults', None)
        else:
            registry_after['delivery_policy_defaults'] = parse_delivery_policy(request.get('delivery_policy', {})) or {}
        affected = sorted(projects)
    else:
        group = delivery_group_name(request.get('delivery_policy_group'), 'delivery_policy_group')
        groups = registry_after.setdefault('delivery_policy_groups', {})
        if reset:
            groups[group] = {}
        else:
            groups[group] = parse_delivery_policy(request.get('delivery_policy', {})) or {}
        affected = sorted(project for project, row in projects.items()
                          if row.get('delivery_policy_group') == group)

    effects = []
    for project in affected:
        before = resolve_delivery_policy(registry, project)
        after = resolve_delivery_policy(registry_after, project)
        effects.append({'project': project, 'before': before['policy'], 'after': after['policy'],
                        'provenance': after['provenance'], 'runtime': after['runtime']})
    blockers: list[str] = []
    create = None
    proposed = copy.deepcopy(dict(existing)) if isinstance(existing, Mapping) else None
    policy = effects[0]['after'] if len(effects) == 1 else None
    routing_changed = False
    if scope == 'repository' and activate:
        runtime = effects[0]['runtime']
        blockers.extend(runtime['blockers'])
        if runtime['ready']:
            proposed = copy.deepcopy(dict(existing))
            if policy['mode'] == 'per_ticket_pr':
                workflow, integration_ref = compile_delivery_workflow(policy)
                proposed['delivery_workflow'] = workflow
                proposed['integration_ref'] = integration_ref
                proposed.pop('delivery_policy_activation', None)
            else:
                proposed['delivery_policy_activation'] = activate_delivery_policy(policy)
            registry_after['projects'][str(name)] = proposed
            routing_changed = (
                delivery_route_changed(registry['projects'][str(name)], proposed)
                or registry['projects'][str(name)].get('delivery_policy_activation')
                != proposed.get('delivery_policy_activation')
            )
            branch_blockers, create = _policy_branch_plan(policy, registry['projects'][str(name)], refs)
            blockers.extend(branch_blockers)
    if scope == 'repository':
        if activate and not observation.get('complete'):
            blockers.append('Active work observation is incomplete; refresh before changing delivery')
        if routing_changed and (observation.get('active_tickets') or observation.get('pending_offers')):
            blockers.append('Finish or pause active work and pending offers before activating delivery settings')
    now = datetime.now(timezone.utc)
    label = str(name) if scope == 'repository' else ('global defaults' if scope == 'global' else request['delivery_policy_group'])
    return {'schema_version': 1, 'kind': 'project-delivery-policy', 'created_at': now.isoformat(),
            'expires_at': (now + timedelta(minutes=10)).isoformat(), 'actor': actor,
            'central': central, 'project': label, 'board_id': existing.get('board_id') if isinstance(existing, Mapping) else None,
            'scope': scope, 'activate': activate, 'registry_expected_sha256': registry_expected_sha256,
            'routing_changed': routing_changed,
            'existing_entry': copy.deepcopy(dict(registry['projects'][str(name)])) if scope == 'repository' else None,
            'proposed_entry': proposed, 'proposed_registry': registry_after,
            'delivery_policy': policy, 'affected_projects': effects, 'create_branch': create,
            'observed_refs': refs, 'board_observation': dict(observation),
            'blocked': bool(blockers), 'blockers': blockers, 'confirmation': f'CONFIGURE {label}',
            'operations': ([{'operation_id': 'integration-branch', 'effect': 'Create delivery branch from mapped base', 'target': create['name']}] if create else []) +
                          [{'operation_id': 'registry', 'effect': 'Save delivery policy' + (' and activate supported runtime' if activate else ' as configuration'),
                            'target': 'project_registry', 'affected_projects': affected}],
            'warnings': ['Existing PRs and in-flight batches keep their targets.',
                         'Unavailable runtime modes remain drafts and never fall back to another mode.'],
            'preserved': ['ticket history and live leases', 'source analysis mapping',
                          'credentials and unrelated project settings'],
            'rollback': ['Restore the previous policy through a new preview. A newly created branch is retained for inspection.']}


def build_delivery_plan(*, request, registry, registry_expected_sha256, actor, central, observation, refs):
    if 'delivery_policy' in request or request.get('reset_to_inherit'):
        return _build_policy_plan(request=request, registry=registry,
                                  registry_expected_sha256=registry_expected_sha256,
                                  actor=actor, central=central, observation=observation, refs=refs)
    return _build_legacy_delivery_plan(request=request, registry=registry,
                                       registry_expected_sha256=registry_expected_sha256,
                                       actor=actor, central=central, observation=observation, refs=refs)


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
