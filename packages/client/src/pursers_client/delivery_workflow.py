"""Shared, fail-closed repository delivery policy and evidence stage vocabulary."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DELIVERY_STAGES = {
    'pr_pending': 'PR pending', 'pr_blocked': 'Delivery blocked',
    'pr_uncertain': 'Delivery unconfirmed', 'pr_created': 'PR opened',
    'integration_pending': 'Awaiting integration',
    'integration_blocked': 'Integration blocked',
    'integration_merged': 'Ready for your team',
    'delivery_recorded': 'Delivery recorded',
}

DELIVERY_POLICY_MODES = frozenset({'per_ticket_pr', 'batch_pr', 'branch_only'})
DELIVERY_RELEASE_TRIGGERS = frozenset({'ready', 'manual', 'scheduled'})
DELIVERY_POLICY_FIELDS = frozenset({
    'mode', 'mapped_base', 'integration_branch', 'snapshot_branch_prefix',
    'final_pr_target', 'release_trigger', 'pr_update', 'auto_integrate',
    'final_merge', 'validation', 'conflict_policy', 'collection_paused',
})
DELIVERY_POLICY_PRESETS: dict[str, dict[str, Any]] = {
    'review-each-ticket': {'mode': 'per_ticket_pr', 'release_trigger': {'kind': 'ready'}},
    'receive-batches': {'mode': 'batch_pr', 'release_trigger': {'kind': 'manual'}},
    'branch-only': {'mode': 'branch_only', 'final_pr_target': None,
                    'release_trigger': {'kind': 'ready'}},
}
_CRON_FIELD_RE = re.compile(r'[0-9*/?,\-]+')
_GROUP_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}')


def _cron_field(value: str, minimum: int, maximum: int) -> bool:
    """Validate one numeric cron field, including lists, ranges and steps."""
    if _CRON_FIELD_RE.fullmatch(value) is None or '?' in value:
        return False
    for item in value.split(','):
        if not item:
            return False
        base, separator, step_text = item.partition('/')
        if separator:
            if not step_text.isdigit() or not 1 <= int(step_text) <= maximum - minimum + 1:
                return False
        if base == '*':
            continue
        if '-' in base:
            start_text, dash, end_text = base.partition('-')
            if not dash or '-' in end_text or not start_text.isdigit() or not end_text.isdigit():
                return False
            start, end = int(start_text), int(end_text)
            if not minimum <= start <= end <= maximum:
                return False
            continue
        if not base.isdigit() or not minimum <= int(base) <= maximum:
            return False
    return True


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


def delivery_group_name(value: Any, field: str = 'delivery_policy_group') -> str:
    if not isinstance(value, str) or _GROUP_RE.fullmatch(value) is None:
        raise ValueError(f'{field} must use 1-80 letters, numbers, dot, underscore or dash')
    return value


def _optional_branch(value: Any, field: str) -> str | None:
    return None if value is None else branch_name(value, field)


def _branch_prefix(value: Any) -> str:
    if not isinstance(value, str) or not value or value.endswith('/'):
        raise ValueError('delivery_policy.snapshot_branch_prefix must be a non-empty branch prefix')
    branch_name(value + '/example', 'delivery_policy.snapshot_branch_prefix')
    return value


def _release_trigger(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {'kind', 'timezone', 'schedule'}:
        raise ValueError('delivery_policy.release_trigger contains unsupported fields')
    kind = value.get('kind')
    if kind not in DELIVERY_RELEASE_TRIGGERS:
        raise ValueError('delivery_policy.release_trigger.kind must be ready, manual or scheduled')
    timezone_name = value.get('timezone')
    schedule = value.get('schedule')
    if kind != 'scheduled':
        if timezone_name is not None or schedule is not None:
            raise ValueError('only scheduled release triggers accept timezone and schedule')
        return {'kind': kind}
    if not isinstance(timezone_name, str) or not timezone_name or timezone_name != timezone_name.strip():
        raise ValueError('scheduled release trigger requires a trimmed IANA timezone')
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('scheduled release trigger timezone is not recognized') from exc
    fields = schedule.split() if isinstance(schedule, str) else []
    limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    if (not isinstance(schedule, str) or schedule != schedule.strip()
            or len(fields) != 5
            or any(not _cron_field(field, *limit) for field, limit in zip(fields, limits))):
        raise ValueError('scheduled release trigger requires a five-field numeric cron schedule')
    return {'kind': kind, 'timezone': timezone_name, 'schedule': schedule}


def _validation_policy(value: Any, *, partial: bool) -> dict[str, Any]:
    allowed = {'test_commands', 'required_reviewers', 'independent_review', 'require_upstream_policies'}
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ValueError('delivery_policy.validation contains unsupported fields')
    result: dict[str, Any] = {}
    if 'test_commands' in value:
        commands = value['test_commands']
        if (not isinstance(commands, list) or len(commands) > 20
                or any(not isinstance(command, str) or not command or command != command.strip()
                       or len(command) > 500 or any(ord(char) < 0x20 for char in command)
                       for command in commands)):
            raise ValueError('delivery_policy.validation.test_commands must be up to 20 trimmed commands')
        result['test_commands'] = list(dict.fromkeys(commands))
    if 'required_reviewers' in value:
        reviewers = value['required_reviewers']
        if type(reviewers) is not int or not 1 <= reviewers <= 10:
            raise ValueError('delivery_policy.validation.required_reviewers must be 1-10')
        result['required_reviewers'] = reviewers
    for key in ('independent_review', 'require_upstream_policies'):
        if key in value:
            if value[key] is not True:
                raise ValueError(f'delivery_policy.validation.{key} cannot weaken required policy')
            result[key] = True
    if not partial:
        result.setdefault('test_commands', [])
        result.setdefault('required_reviewers', 1)
        result.setdefault('independent_review', True)
        result.setdefault('require_upstream_policies', True)
    return result


def parse_delivery_policy(value: Any, *, partial: bool = True) -> dict[str, Any] | None:
    """Validate one policy layer. Missing keys inherit; explicit false/list emptiness survive."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) - DELIVERY_POLICY_FIELDS:
        raise ValueError('delivery_policy contains unsupported fields')
    result: dict[str, Any] = {}
    if 'mode' in value:
        if value['mode'] not in DELIVERY_POLICY_MODES:
            raise ValueError('delivery_policy.mode must be per_ticket_pr, batch_pr or branch_only')
        result['mode'] = value['mode']
    for key in ('mapped_base', 'integration_branch', 'final_pr_target'):
        if key in value:
            result[key] = _optional_branch(value[key], f'delivery_policy.{key}')
            if key != 'final_pr_target' and result[key] is None:
                raise ValueError(f'delivery_policy.{key} cannot be null')
    if 'snapshot_branch_prefix' in value:
        result['snapshot_branch_prefix'] = _branch_prefix(value['snapshot_branch_prefix'])
    if 'release_trigger' in value:
        result['release_trigger'] = _release_trigger(value['release_trigger'])
    if 'pr_update' in value:
        if value['pr_update'] not in {'rolling', 'freeze_on_ready'}:
            raise ValueError('delivery_policy.pr_update must be rolling or freeze_on_ready')
        result['pr_update'] = value['pr_update']
    for key in ('auto_integrate', 'collection_paused'):
        if key in value:
            if type(value[key]) is not bool:
                raise ValueError(f'delivery_policy.{key} must be boolean')
            result[key] = value[key]
    if 'final_merge' in value:
        if value['final_merge'] != 'manual':
            raise ValueError('delivery_policy.final_merge must remain manual')
        result['final_merge'] = 'manual'
    if 'validation' in value:
        result['validation'] = _validation_policy(value['validation'], partial=partial)
    if 'conflict_policy' in value:
        if value['conflict_policy'] not in {'repair_then_review', 'pause'}:
            raise ValueError('delivery_policy.conflict_policy must be repair_then_review or pause')
        result['conflict_policy'] = value['conflict_policy']
    if not partial:
        missing = DELIVERY_POLICY_FIELDS - set(result)
        if missing:
            raise ValueError(f'delivery_policy is missing required fields: {", ".join(sorted(missing))}')
        _validate_effective_policy(result)
    return result


def _builtin_policy(project: Mapping[str, Any]) -> dict[str, Any]:
    legacy = parse_delivery_workflow(project.get('delivery_workflow'))
    integrated = bool(legacy and legacy['mode'] == 'integration')
    mapped_base = (legacy.get('base_branch') if integrated else project.get('integration_ref', 'main'))
    mapped_base = branch_name(mapped_base, 'delivery_policy.mapped_base')
    return {
        'mode': 'branch_only' if integrated else 'per_ticket_pr', 'mapped_base': mapped_base,
        'integration_branch': legacy['integration_branch'] if integrated else 'pursers-integration',
        'snapshot_branch_prefix': 'codex',
        'final_pr_target': None if integrated else mapped_base, 'release_trigger': {'kind': 'ready'},
        'pr_update': 'rolling', 'auto_integrate': legacy.get('auto_integrate', False) if integrated else False,
        'final_merge': 'manual',
        'validation': {'test_commands': [], 'required_reviewers': 1,
                       'independent_review': True, 'require_upstream_policies': True},
        'conflict_policy': 'pause',
        'collection_paused': legacy.get('collection_paused', False) if integrated else False,
    }


def _merge_policy(target: dict[str, Any], provenance: dict[str, Any], layer: Mapping[str, Any], source: str) -> None:
    for key, value in layer.items():
        if key != 'validation':
            target[key] = value
            provenance[key] = source
            continue
        validation = target.setdefault('validation', {})
        for nested, selected in value.items():
            path = f'validation.{nested}'
            if nested == 'test_commands':
                before = list(validation.get(nested, []))
                validation[nested] = list(dict.fromkeys([*before, *selected]))
                if validation[nested] != before:
                    prior = provenance.get(path)
                    provenance[path] = [prior, source] if prior and prior != source else source
            elif nested == 'required_reviewers':
                before = validation.get(nested, 1)
                validation[nested] = max(before, selected)
                if validation[nested] != before:
                    provenance[path] = source
            else:
                before = bool(validation.get(nested, False))
                validation[nested] = bool(before or selected)
                if validation[nested] != before:
                    provenance[path] = source


def _validate_effective_policy(policy: Mapping[str, Any]) -> None:
    branches = [policy['mapped_base']]
    if policy['mode'] in {'branch_only', 'batch_pr'}:
        branches.append(policy['integration_branch'])
    if (policy['final_pr_target'] is not None
            and policy['final_pr_target'].casefold() not in {branch.casefold() for branch in branches}):
        branches.append(policy['final_pr_target'])
    folded = [branch.casefold() for branch in branches]
    if len(folded) != len(set(folded)):
        raise ValueError('delivery policy branches must be distinct, including case differences')
    prefix = policy['snapshot_branch_prefix'].casefold()
    if any(branch == prefix or branch.startswith(prefix + '/') or prefix.startswith(branch + '/') for branch in folded):
        raise ValueError('snapshot branch prefix conflicts with a configured branch namespace')
    if policy['mode'] == 'branch_only' and policy['final_pr_target'] is not None:
        raise ValueError('branch_only delivery requires final_pr_target null')
    if policy['mode'] in {'per_ticket_pr', 'batch_pr'} and policy['final_pr_target'] is None:
        raise ValueError(f'{policy["mode"]} delivery requires final_pr_target')
    if policy['mode'] == 'per_ticket_pr' and policy['auto_integrate']:
        raise ValueError('per_ticket_pr cannot auto-integrate into a customer-owned target')
    if (policy['mode'] == 'per_ticket_pr'
            and policy['final_pr_target'].casefold() != policy['mapped_base'].casefold()):
        raise ValueError('per_ticket_pr final_pr_target must equal mapped_base')


def delivery_runtime_readiness(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Describe exact deployed support. Unsupported settings remain configuration drafts."""
    blockers: list[str] = []
    if policy['mode'] == 'batch_pr':
        blockers.append('batch_pr runtime is not installed')
    if policy['mode'] == 'branch_only':
        blockers.append('branch_only runtime is not installed; legacy integration still creates per-ticket PRs')
    if policy['release_trigger']['kind'] != 'ready':
        blockers.append(f'{policy["release_trigger"]["kind"]} release trigger is not installed')
    if policy['pr_update'] != 'rolling':
        blockers.append('freeze_on_ready PR updates are not installed')
    if policy['snapshot_branch_prefix'] != 'codex':
        blockers.append('custom snapshot branch prefixes are not installed')
    validation = policy['validation']
    if validation['test_commands']:
        blockers.append('custom validation test commands are not installed')
    if validation['required_reviewers'] != 1:
        blockers.append('multiple required reviewers are not installed')
    if policy['conflict_policy'] != 'pause':
        blockers.append('automatic conflict repair is not installed')
    return {'ready': not blockers, 'blockers': blockers,
            'supported_modes': ['per_ticket_pr'],
            'supported_release_triggers': ['ready']}


def resolve_delivery_policy(registry: Mapping[str, Any], project_name: str) -> dict[str, Any]:
    projects = registry.get('projects')
    if not isinstance(projects, Mapping) or not isinstance(projects.get(project_name), Mapping):
        raise ValueError(f'project {project_name!r} is not registered')
    project = projects[project_name]
    effective = _builtin_policy(project)
    provenance = {key: 'built-in' for key in DELIVERY_POLICY_FIELDS if key != 'validation'}
    provenance.update({f'validation.{key}': 'built-in' for key in effective['validation']})
    layers: list[tuple[str, Any]] = [('global', registry.get('delivery_policy_defaults'))]
    group = project.get('delivery_policy_group')
    if group is not None:
        group = delivery_group_name(group)
        groups = registry.get('delivery_policy_groups')
        if not isinstance(groups, Mapping) or group not in groups:
            raise ValueError(f'delivery policy group {group!r} is not defined')
        layers.append((f'group:{group}', groups[group]))
    layers.append((f'repository:{project_name}', project.get('delivery_policy')))
    for source, raw in layers:
        layer = parse_delivery_policy(raw, partial=True)
        if layer is not None:
            _merge_policy(effective, provenance, layer, source)
    effective = parse_delivery_policy(effective, partial=False) or {}
    return {'policy': effective, 'provenance': provenance, 'group': group,
            'overrides': parse_delivery_policy(project.get('delivery_policy'), partial=True) or {},
            'runtime': delivery_runtime_readiness(effective)}


def compile_delivery_workflow(policy: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    parsed = parse_delivery_policy(policy, partial=False)
    assert parsed is not None
    readiness = delivery_runtime_readiness(parsed)
    if not readiness['ready']:
        raise ValueError('delivery policy is configuration-only: ' + '; '.join(readiness['blockers']))
    return {'mode': 'direct'}, parsed['mapped_base']


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
