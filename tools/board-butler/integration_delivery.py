"""Deterministic delivery runtimes. Human promotion branches are never completed."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pursers_client.delivery_workflow import parse_delivery_workflow

SHA = re.compile(r'^[0-9a-f]{40}$')


def blocked(reason):
    return {'state': 'integration_blocked', 'reason': reason}


async def integrate(policy, *, approved_sha, source_ref, target_sha, remote, checks,
                    prior_attempt, reserve, complete):
    p = parse_delivery_workflow(policy)
    if not p or p['mode'] != 'integration':
        return blocked('integration_not_configured')
    if (remote.get('targetRefName') != 'refs/heads/' + p['integration_branch']
            or remote.get('sourceRefName') != source_ref
            or source_ref in {'refs/heads/'+p[k] for k in ('integration_branch','base_branch')}):
        return blocked('PR_target_or_source_changed_existing_PR_preserved')
    if (not SHA.fullmatch(approved_sha or '')
            or remote.get('lastMergeSourceCommit', {}).get('commitId') != approved_sha):
        return blocked('source_changed_fresh_independent_review_required')
    if remote.get('status') == 'completed':
        if prior_attempt and prior_attempt != approved_sha + ':' + str(remote.get('lastMergeTargetCommit', {}).get('commitId', '')):
            return blocked('completed_target_differs_from_validated_attempt')
        merged = remote.get('lastMergeCommit', {}).get('commitId', '')
        if not SHA.fullmatch(merged):
            return blocked('merge_commit_unconfirmed')
        return {'state': 'integration_merged', 'merge_sha': merged}
    if remote.get('status') != 'active' or remote.get('isDraft'):
        return blocked('PR_not_active_or_is_draft')
    if prior_attempt:
        return {'state': 'pr_uncertain', 'reason': 'completion_attempt_exists_reconcile_only'}
    if p['collection_paused'] or not p['auto_integrate']:
        return {'state': 'integration_pending', 'reason': 'collection_paused' if p['collection_paused'] else 'automatic_integration_disabled'}
    if (not SHA.fullmatch(target_sha or '')
            or remote.get('lastMergeTargetCommit', {}).get('commitId') != target_sha):
        return blocked('target_changed_revalidation_required')
    if remote.get('mergeStatus') != 'succeeded':
        return blocked('conflict_or_merge_calculation_pending')
    if any(r.get('vote', 0) < 0 for r in remote.get('reviewers', [])):
        return blocked('reviewer_has_unresolved_negative_vote')
    if not isinstance(checks, Mapping):
        return blocked('validation_unavailable')
    validation = checks.get('validation', {})
    if (not isinstance(validation, Mapping) or validation.get('source_sha') != approved_sha
            or validation.get('target_sha') != target_sha or validation.get('passed') is not True):
        return blocked('exact_source_target_validation_required')
    policies = checks.get('policies')
    if not isinstance(policies, list) or not policies:
        return blocked('required_branch_policies_unavailable')
    required = [v for v in policies if isinstance(v, Mapping) and v.get('enabled') is True and v.get('blocking') is True]
    if not required or any(v.get('status') not in {'approved', 'notApplicable'} for v in required):
        return blocked('required_policy_not_satisfied')
    await reserve(approved_sha + ':' + target_sha)
    await complete()
    return {'state': 'integration_pending', 'reason': 'completion_requested_awaiting_confirmation'}


BATCH_MODES = frozenset({'per_ticket_pr', 'batch_pr', 'branch_only'})
BATCH_TRIGGERS = frozenset({'ready', 'manual', 'scheduled'})
BATCH_SNAPSHOTS = frozenset({'frozen', 'rolling'})
MEMBER_STATES = frozenset({'approved', 'integrated', 'in_delivery', 'customer_merged', 'blocked'})


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError(f'{field} must be a full lowercase commit SHA')
    return value


def _branch(value: Any, field: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 200
            or value.startswith(('-', '/', 'refs/')) or value.endswith(('/', '.'))
            or value == '@' or '..' in value or '@{' in value
            or re.search(r'[\x00-\x20\x7f~^:?*\[\\]', value)
            or any(not part or part.startswith('.') or part.endswith('.lock')
                   for part in value.split('/'))):
        raise ValueError(f'{field} must be a valid short Git branch name')
    return value


def parse_batch_policy(value: Any) -> dict[str, Any] | None:
    """Parse only the batch-runtime fields; absent/unknown modes stay unavailable."""
    if not isinstance(value, Mapping) or value.get('mode') not in BATCH_MODES:
        return None
    mode = value['mode']
    if mode == 'per_ticket_pr':
        return {'mode': mode}
    allowed = {
        'mode', 'repository', 'base_branch', 'integration_branch', 'snapshot_prefix',
        'target_branch', 'trigger', 'snapshot_strategy', 'timezone', 'schedule',
        'policy_revision', 'conflict_runner',
    }
    if set(value) - allowed:
        raise ValueError('delivery_workflow contains unsupported batch runtime fields')
    repository = value.get('repository')
    if not isinstance(repository, str) or not repository.strip() or repository != repository.strip():
        raise ValueError('delivery_workflow.repository must be a stable mapped identity')
    result = {
        'mode': mode,
        'repository': repository,
        'base_branch': _branch(value.get('base_branch'), 'base_branch'),
        'integration_branch': _branch(value.get('integration_branch'), 'integration_branch'),
        'snapshot_prefix': _branch(value.get('snapshot_prefix', 'pursers/delivery'), 'snapshot_prefix'),
        'target_branch': _branch(value.get('target_branch'), 'target_branch'),
        'trigger': value.get('trigger', 'ready'),
        'snapshot_strategy': value.get('snapshot_strategy', 'frozen'),
    }
    if result['trigger'] not in BATCH_TRIGGERS:
        raise ValueError('delivery_workflow.trigger is unsupported')
    if result['snapshot_strategy'] not in BATCH_SNAPSHOTS:
        raise ValueError('delivery_workflow.snapshot_strategy is unsupported')
    if len({result['base_branch'].casefold(), result['integration_branch'].casefold(),
            result['target_branch'].casefold()}) != 3:
        raise ValueError('base, integration and customer target branches must be distinct')
    if mode == 'branch_only' and result['snapshot_strategy'] == 'rolling':
        raise ValueError('branch_only uses immutable delivery snapshots')
    revision = value.get('policy_revision')
    result['policy_revision'] = (
        _sha(revision, 'policy_revision') if revision is not None
        else _digest({k: result[k] for k in sorted(result)})
    )
    result['conflict_runner'] = value.get('conflict_runner')
    if result['conflict_runner'] is not None and (
            not isinstance(result['conflict_runner'], str) or not result['conflict_runner'].strip()):
        raise ValueError('delivery_workflow.conflict_runner must be a configured runner name')
    if result['trigger'] == 'scheduled':
        schedule = value.get('schedule')
        zone = value.get('timezone')
        if not isinstance(schedule, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', schedule):
            raise ValueError('scheduled delivery requires HH:MM schedule')
        try:
            ZoneInfo(zone)
        except (TypeError, ZoneInfoNotFoundError):
            raise ValueError('scheduled delivery requires an IANA timezone') from None
        result.update(schedule=schedule, timezone=zone)
    elif 'schedule' in value or 'timezone' in value:
        raise ValueError('schedule and timezone require the scheduled trigger')
    return result


class BatchLedger:
    """Atomic private ledger for batch membership, reservations and remote IDs."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.document: dict[str, Any] = {
            'schema_version': 1, 'batches': {}, 'member_index': {},
            'route_counters': {}, 'manual_requests': {}, 'schedule_slots': {},
        }
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise ValueError('batch delivery ledger is invalid') from None
            required = {'schema_version', 'batches', 'member_index', 'route_counters',
                        'manual_requests', 'schedule_slots'}
            if (not isinstance(loaded, Mapping) or loaded.get('schema_version') != 1
                    or not required <= set(loaded)
                    or any(not isinstance(loaded[k], Mapping) for k in required - {'schema_version'})):
                raise ValueError('batch delivery ledger is invalid')
            self.document = dict(loaded)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = _canonical(self.document)
        fd, tmp = tempfile.mkstemp(prefix='.batch-delivery-', dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


class BatchDeliveryRuntime:
    """Event-driven single-PR/branch delivery state machine.

    The adapter owns connector and verified-local-Git details. Every non-idempotent
    adapter call is preceded by a durable reservation and unknown outcomes are read
    back before another mutation is allowed.
    """

    def __init__(self, ledger: BatchLedger, adapter: Any, *, clock=None) -> None:
        self.ledger = ledger
        self.adapter = adapter
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _route(policy: Mapping[str, Any]) -> str:
        return _digest([policy['repository'], policy['target_branch']])

    def _new_batch(self, policy: Mapping[str, Any], working_head: str) -> dict[str, Any]:
        route = self._route(policy)
        counters = self.ledger.document['route_counters']
        ordinal = int(counters.get(route, 0)) + 1
        counters[route] = ordinal
        key = _digest([route, policy['policy_revision'], ordinal])
        snapshot_branch = f"{policy['snapshot_prefix'].rstrip('/')}/{key[:16]}"
        batch = {
            'batch_key': key, 'route_key': route, 'ordinal': ordinal,
            'repository': policy['repository'], 'target_branch': policy['target_branch'],
            'base_branch': policy['base_branch'], 'integration_branch': policy['integration_branch'],
            'snapshot_branch': snapshot_branch, 'mode': policy['mode'],
            'trigger': policy['trigger'], 'snapshot_strategy': policy['snapshot_strategy'],
            'effective_policy_revision': policy['policy_revision'], 'state': 'collecting',
            'working_head': working_head, 'members': {}, 'reservations': {},
            'created_at': self.clock().astimezone(timezone.utc).isoformat(),
        }
        self.ledger.document['batches'][key] = batch
        self.ledger.save()
        return batch

    def _batch_for_collection(self, policy: Mapping[str, Any], working_head: str) -> dict[str, Any]:
        route = self._route(policy)
        candidates = [b for b in self.ledger.document['batches'].values()
                      if b.get('route_key') == route
                      and b.get('effective_policy_revision') == policy['policy_revision']
                      and (b.get('state') in {'collecting', 'waiting_customer_slot'}
                           or (b.get('state') == 'delivery_open'
                               and policy['snapshot_strategy'] == 'rolling'))]
        if candidates:
            return max(candidates, key=lambda b: int(b['ordinal']))
        return self._new_batch(policy, working_head)

    @staticmethod
    def _validate_member(member: Mapping[str, Any]) -> dict[str, Any]:
        required = {'ticket_id', 'source_ref', 'source_sha', 'issue_ids', 'summary', 'tests', 'evidence'}
        if not required <= set(member):
            raise ValueError('approved member evidence is incomplete')
        source_sha = _sha(member['source_sha'], 'source_sha')
        _branch(member['source_ref'], 'source_ref')
        if not isinstance(member['ticket_id'], str) or not member['ticket_id']:
            raise ValueError('ticket_id is required')
        evidence = member['evidence']
        if (not isinstance(evidence, Mapping) or evidence.get('approved_sha') != source_sha
                or evidence.get('independent_review') is not True
                or evidence.get('validation_passed') is not True):
            raise ValueError('exact independent validation evidence is required')
        if not isinstance(member['issue_ids'], list) or any(not isinstance(v, str) for v in member['issue_ids']):
            raise ValueError('issue_ids must be strings')
        return {
            'ticket_id': member['ticket_id'], 'source_ref': member['source_ref'],
            'source_sha': source_sha, 'issue_ids': list(member['issue_ids']),
            'summary': str(member['summary'])[:4000], 'tests': str(member['tests'])[:8000],
            'blockers': str(member.get('blockers', 'none'))[:4000],
            'baseline_failures': str(member.get('baseline_failures', 'none'))[:4000],
            'evidence': dict(evidence), 'evidence_digest': _digest(evidence), 'status': 'approved',
        }

    def _reserve(self, batch: dict[str, Any], kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation_id = _digest([batch['batch_key'], kind, payload])
        reservations = batch['reservations']
        existing = reservations.get(operation_id)
        if existing:
            return existing
        reservation = {
            'operation_id': operation_id, 'kind': kind, 'status': 'reserved',
            'input_digest': _digest(payload), 'payload': dict(payload),
            'created_at': self.clock().astimezone(timezone.utc).isoformat(),
        }
        reservations[operation_id] = reservation
        self.ledger.save()
        return reservation

    def _finish(self, reservation: dict[str, Any], result: Mapping[str, Any]) -> None:
        status = result.get('status')
        reservation['status'] = 'confirmed' if status in {'confirmed', 'conflict'} else 'unknown'
        reservation['result'] = dict(result)
        self.ledger.save()

    async def collect(self, raw_policy: Mapping[str, Any], raw_member: Mapping[str, Any], *, release=None) -> dict[str, Any]:
        policy = parse_batch_policy(raw_policy)
        if policy is None:
            return blocked('delivery_mode_unavailable')
        if policy['mode'] == 'per_ticket_pr':
            return {'state': 'legacy_per_ticket_pr', 'changed': False}
        member = self._validate_member(raw_member)
        route = self._route(policy)
        async with self._locks.setdefault(route, asyncio.Lock()):
            indexed = self.ledger.document['member_index'].get(member['ticket_id'])
            if indexed:
                existing_batch = self.ledger.document['batches'][indexed['batch_key']]
                existing = existing_batch['members'][member['ticket_id']]
                if (existing['source_sha'] != member['source_sha']
                        or existing['evidence_digest'] != member['evidence_digest']):
                    return blocked('ticket_membership_changed_requires_new_approval')
                if any(r.get('status') in {'reserved', 'unknown'}
                       for r in existing_batch['reservations'].values()):
                    return blocked('remote_outcome_unknown_reconcile_before_retry')
                return {'state': existing['status'], 'batch_key': indexed['batch_key'], 'deduplicated': True}
            source_head = await self.adapter.read_ref(policy['repository'], member['source_ref'])
            if source_head != member['source_sha']:
                return blocked('source_ref_changed_fresh_independent_review_required')
            working_head = await self.adapter.read_ref(policy['repository'], policy['integration_branch'])
            if working_head is None:
                base_head = await self.adapter.read_ref(policy['repository'], policy['base_branch'])
                _sha(base_head, 'base_head')
                seed = {'branch': policy['integration_branch'], 'sha': base_head, 'expected': None}
                transient = self._new_batch(policy, base_head)
                reservation = self._reserve(transient, 'create_integration_branch', seed)
                result = await self.adapter.ensure_branch(policy['repository'], policy['integration_branch'], base_head,
                                                          None, reservation['operation_id'])
                self._finish(reservation, result)
                if result.get('status') != 'confirmed' or result.get('head_sha') != base_head:
                    transient['state'] = 'blocked_unknown'
                    self.ledger.save()
                    return blocked('integration_branch_creation_unconfirmed')
                working_head = base_head
                batch = transient
            else:
                _sha(working_head, 'working_head')
                batch = self._batch_for_collection(policy, working_head)
            if batch['working_head'] != working_head:
                batch['state'] = 'blocked_external_edit'
                self.ledger.save()
                return blocked('integration_ref_changed_outside_runtime')
            batch['members'][member['ticket_id']] = member
            self.ledger.document['member_index'][member['ticket_id']] = {
                'batch_key': batch['batch_key'], 'source_sha': member['source_sha'],
            }
            self.ledger.save()
            merge_payload = {'source_sha': member['source_sha'], 'expected_head': working_head,
                             'integration_branch': policy['integration_branch']}
            reservation = self._reserve(batch, 'integrate_member', merge_payload)
            result = await self.adapter.integrate_reviewed(
                policy['repository'], member['source_sha'], policy['integration_branch'], working_head,
                reservation['operation_id'])
            self._finish(reservation, result)
            if result.get('status') == 'conflict':
                return await self._conflict(policy, batch, member, result)
            if result.get('status') != 'confirmed' or not SHA.fullmatch(str(result.get('head_sha', ''))):
                member['status'] = 'blocked'; member['reason'] = 'integration_outcome_unknown'
                batch['state'] = 'blocked_unknown'; self.ledger.save()
                return blocked('integration_outcome_unknown_reconcile_before_retry')
            member['status'] = 'integrated'
            batch['working_head'] = result['head_sha']
            batch['state'] = 'collecting' if batch['state'] != 'delivery_open' else batch['state']
            self.ledger.save()
            if batch['state'] == 'delivery_open' and policy['snapshot_strategy'] == 'rolling':
                rolled = await self._roll_snapshot(policy, batch)
                if rolled['state'] == 'integration_blocked':
                    return rolled
            released = await self.release(policy, batch['batch_key'], request=release)
            return released if released.get('released') or released.get('state') in {
                'integration_blocked', 'integration_pending'} else {
                'state': member['status'], 'batch_key': batch['batch_key'], 'released': False,
            }

    async def _conflict(self, policy, batch, member, result):
        runner = policy.get('conflict_runner')
        if not runner or not hasattr(self.adapter, 'request_conflict_repair'):
            member['status'] = 'blocked'; member['reason'] = 'conflict_runner_unavailable'
            batch['state'] = 'blocked_conflict'; self.ledger.save()
            return blocked('conflict_runner_unavailable')
        repair = await self.adapter.request_conflict_repair(
            policy['repository'], runner, member['ticket_id'], member['source_sha'], batch['working_head'])
        evidence = repair.get('evidence', {}) if isinstance(repair, Mapping) else {}
        repaired_sha = repair.get('source_sha') if isinstance(repair, Mapping) else None
        if (repair.get('status') != 'confirmed' or not SHA.fullmatch(str(repaired_sha or ''))
                or evidence.get('approved_sha') != repaired_sha
                or evidence.get('independent_review') is not True
                or evidence.get('validation_passed') is not True):
            member['status'] = 'blocked'; member['reason'] = 'fresh_conflict_repair_review_required'
            batch['state'] = 'blocked_conflict'; self.ledger.save()
            return blocked('fresh_conflict_repair_review_required')
        member.update(source_sha=repaired_sha, evidence=dict(evidence), evidence_digest=_digest(evidence),
                      status='approved', repaired_from=result.get('source_sha', member['source_sha']))
        self.ledger.document['member_index'][member['ticket_id']]['source_sha'] = repaired_sha
        self.ledger.save()
        source_head = await self.adapter.read_ref(policy['repository'], member['source_ref'])
        if source_head != repaired_sha:
            return blocked('repaired_source_ref_not_confirmed')
        payload = {'source_sha': repaired_sha, 'expected_head': batch['working_head'],
                   'integration_branch': policy['integration_branch'], 'repair': True}
        reservation = self._reserve(batch, 'integrate_repaired_member', payload)
        merged = await self.adapter.integrate_reviewed(policy['repository'], repaired_sha,
                                                       policy['integration_branch'], batch['working_head'],
                                                       reservation['operation_id'])
        self._finish(reservation, merged)
        if merged.get('status') != 'confirmed' or not SHA.fullmatch(str(merged.get('head_sha', ''))):
            member['status'] = 'blocked'; member['reason'] = 'repaired_integration_unconfirmed'
            batch['state'] = 'blocked_unknown'; self.ledger.save()
            return blocked('repaired_integration_unconfirmed')
        member['status'] = 'integrated'; batch['working_head'] = merged['head_sha']; batch['state'] = 'collecting'
        self.ledger.save()
        return {'state': 'integrated', 'batch_key': batch['batch_key'], 'released': False}

    def _trigger_due(self, policy, batch, request) -> tuple[bool, str | None]:
        if not any(m['status'] == 'integrated' for m in batch['members'].values()):
            return False, 'no_integrated_members'
        trigger = policy['trigger']
        if trigger == 'ready':
            return True, None
        if trigger == 'manual':
            if (not isinstance(request, Mapping) or request.get('authorized') is not True
                    or not isinstance(request.get('request_id'), str) or not request['request_id']):
                return False, 'authorized_manual_request_required'
            prior = self.ledger.document['manual_requests'].get(request['request_id'])
            if prior and prior != batch['batch_key']:
                return False, 'manual_request_already_used'
            self.ledger.document['manual_requests'][request['request_id']] = batch['batch_key']
            self.ledger.save()
            return True, None
        local = self.clock().astimezone(ZoneInfo(policy['timezone']))
        slot = f"{local.date().isoformat()}T{policy['schedule']}@{policy['timezone']}"
        if local.strftime('%H:%M') != policy['schedule']:
            return False, 'scheduled_slot_not_current'
        if self.ledger.document['schedule_slots'].get(batch['route_key']) == slot:
            return False, 'scheduled_slot_already_used'
        self.ledger.document['schedule_slots'][batch['route_key']] = slot
        self.ledger.save()
        return True, None

    async def release(self, raw_policy, batch_key, *, request=None):
        policy = parse_batch_policy(raw_policy)
        if policy is None or policy['mode'] == 'per_ticket_pr':
            return blocked('batch_delivery_not_configured')
        batch = self.ledger.document['batches'].get(batch_key)
        if not batch:
            return blocked('batch_not_found')
        if batch['effective_policy_revision'] != policy['policy_revision']:
            return blocked('active_batch_policy_is_frozen')
        if batch['state'] not in {'collecting', 'delivery_open', 'waiting_customer_slot'}:
            return {'state': batch['state'], 'batch_key': batch_key, 'released': False}
        due, reason = self._trigger_due(policy, batch, request)
        if not due:
            return {'state': batch['state'], 'batch_key': batch_key, 'released': False, 'reason': reason}
        try:
            validation = await self.adapter.validate_cumulative(
                policy['repository'], policy['base_branch'], batch['working_head'],
                list(batch['members'].values()))
        except Exception as exc:
            batch['state'] = 'blocked_validation'
            batch['validation_error_class'] = type(exc).__name__
            self.ledger.save()
            return blocked('exact_validation_capability_unavailable')
        if (not isinstance(validation, Mapping) or validation.get('passed') is not True
                or validation.get('source_sha') != batch['working_head']
                or not SHA.fullmatch(str(validation.get('target_sha', '')))):
            batch['state'] = 'blocked_validation'; self.ledger.save()
            return blocked('exact_cumulative_validation_required')
        batch['validation'] = dict(validation)
        snapshot = await self._snapshot(policy, batch)
        if snapshot.get('state') == 'integration_blocked':
            return snapshot
        if policy['mode'] == 'branch_only':
            batch['state'] = 'in_delivery'; batch['released_at'] = self.clock().astimezone(timezone.utc).isoformat()
            for member in batch['members'].values():
                if member['status'] == 'integrated': member['status'] = 'in_delivery'
            self.ledger.save()
            return {'state': 'in_delivery', 'batch_key': batch_key, 'branch': batch['snapshot_branch'],
                    'released': True}
        return await self._open_or_reuse_pr(policy, batch)

    async def _snapshot(self, policy, batch):
        observed = await self.adapter.read_ref(policy['repository'], batch['snapshot_branch'])
        if batch.get('snapshot_frozen') and observed != batch.get('snapshot_sha'):
            batch['state'] = 'blocked_external_edit'; self.ledger.save()
            return blocked('frozen_snapshot_changed_externally')
        if observed == batch['working_head']:
            batch['snapshot_sha'] = observed; batch['snapshot_frozen'] = policy['snapshot_strategy'] == 'frozen'
            self.ledger.save(); return {'state': 'snapshot_ready'}
        payload = {'branch': batch['snapshot_branch'], 'sha': batch['working_head'], 'expected': observed}
        reservation = self._reserve(batch, 'snapshot_ref', payload)
        result = await self.adapter.ensure_branch(policy['repository'], batch['snapshot_branch'],
                                                  batch['working_head'], observed, reservation['operation_id'])
        self._finish(reservation, result)
        if result.get('status') != 'confirmed' or result.get('head_sha') != batch['working_head']:
            batch['state'] = 'blocked_unknown'; self.ledger.save()
            return blocked('snapshot_mutation_unconfirmed')
        batch['snapshot_sha'] = batch['working_head']
        batch['snapshot_frozen'] = policy['snapshot_strategy'] == 'frozen'
        self.ledger.save()
        return {'state': 'snapshot_ready'}

    async def _open_or_reuse_pr(self, policy, batch):
        current_target = await self.adapter.read_ref(policy['repository'], policy['target_branch'])
        if current_target != batch.get('validation', {}).get('target_sha'):
            batch['state'] = 'blocked_validation'; self.ledger.save()
            return blocked('customer_target_changed_revalidation_required')
        active = await self.adapter.list_customer_prs(policy['repository'], policy['target_branch'], status='active')
        if not isinstance(active, list):
            return blocked('customer_pr_inventory_unavailable')
        exact = [p for p in active if p.get('correlation') == batch['batch_key']
                 and p.get('source_branch') == batch['snapshot_branch']
                 and p.get('target_branch') == policy['target_branch']]
        if len(active) > 1 or (active and not exact):
            known = all(any(other.get('batch_key') == row.get('correlation')
                            and other.get('state') == 'delivery_open'
                            for other in self.ledger.document['batches'].values())
                        for row in active)
            batch['state'] = 'waiting_customer_slot' if known else 'blocked_existing_pr'
            self.ledger.save()
            return ({'state': 'integration_pending', 'reason': 'customer_pr_slot_busy',
                     'batch_key': batch['batch_key'], 'released': False}
                    if known else blocked('one_active_customer_pr_limit'))
        if exact:
            pr = exact[0]
        else:
            payload = {'source_branch': batch['snapshot_branch'], 'target_branch': policy['target_branch'],
                       'snapshot_sha': batch['snapshot_sha'], 'correlation': batch['batch_key']}
            reservation = self._reserve(batch, 'create_customer_pr', payload)
            body = self._pr_body(batch)
            try:
                result = await self.adapter.create_customer_pr(policy['repository'], payload, body,
                                                               reservation['operation_id'])
            except Exception:
                reservation['status'] = 'unknown'; self.ledger.save()
                batch['state'] = 'blocked_unknown'; self.ledger.save()
                return blocked('customer_pr_create_outcome_unknown')
            self._finish(reservation, result)
            if result.get('status') != 'confirmed':
                batch['state'] = 'blocked_unknown'; self.ledger.save()
                return blocked('customer_pr_create_outcome_unknown')
            pr = result.get('pr')
        if (not isinstance(pr, Mapping) or not isinstance(pr.get('id'), (str, int))
                or pr.get('source_sha') != batch['snapshot_sha']
                or pr.get('source_branch') != batch['snapshot_branch']
                or pr.get('target_branch') != policy['target_branch']):
            batch['state'] = 'blocked_existing_pr'; self.ledger.save()
            return blocked('customer_pr_correlation_mismatch')
        batch['customer_pr'] = dict(pr); batch['state'] = 'delivery_open'
        batch['released_at'] = self.clock().astimezone(timezone.utc).isoformat()
        for member in batch['members'].values():
            if member['status'] == 'integrated': member['status'] = 'in_delivery'
        self.ledger.save()
        return {'state': 'in_delivery', 'batch_key': batch['batch_key'], 'pr_id': pr['id'], 'released': True}

    async def _roll_snapshot(self, policy, batch):
        pr = batch.get('customer_pr', {})
        if pr.get('mutable') is not True:
            batch['state'] = 'blocked_external_edit'; self.ledger.save()
            return blocked('rolling_snapshot_not_authorized_mutable')
        batch.pop('validation', None)
        batch['snapshot_frozen'] = False
        snapshot = await self._snapshot(policy, batch)
        if snapshot.get('state') == 'integration_blocked':
            return snapshot
        payload = {'pr_id': pr['id'], 'source_sha': batch['snapshot_sha'],
                   'correlation': batch['batch_key']}
        reservation = self._reserve(batch, 'update_customer_pr', payload)
        result = await self.adapter.update_customer_pr(policy['repository'], payload,
                                                       self._pr_body(batch), reservation['operation_id'])
        self._finish(reservation, result)
        if result.get('status') != 'confirmed' or result.get('source_sha') != batch['snapshot_sha']:
            batch['state'] = 'blocked_unknown'; self.ledger.save()
            return blocked('rolling_customer_pr_update_unconfirmed')
        batch['validation_state'] = 'stale'
        for member in batch['members'].values():
            if member['status'] == 'integrated': member['status'] = 'in_delivery'
        self.ledger.save()
        return {'state': 'in_delivery', 'batch_key': batch['batch_key'], 'released': True}

    @staticmethod
    def _pr_body(batch):
        members = sorted(batch['members'].values(), key=lambda m: m['ticket_id'])
        lines = [f"Batch: {batch['batch_key']}", '', 'Reviewed members:']
        for member in members:
            issues = ', '.join(member['issue_ids']) or 'none'
            lines.extend([f"- {member['ticket_id']} ({member['source_sha']}), issues: {issues}",
                          f"  Summary: {member['summary']}", f"  Tests: {member['tests']}",
                          f"  Blockers: {member['blockers']}",
                          f"  Baseline failures: {member['baseline_failures']}"])
        lines.extend(['', 'Validation:', '- Exact cumulative validation is recorded in the private batch ledger.',
                      '- Customer merge is manual. This body does not claim Sonar issues are resolved.'])
        return '\n'.join(lines)

    async def reconcile_unknown(self, batch_key):
        batch = self.ledger.document['batches'].get(batch_key)
        if not batch:
            return blocked('batch_not_found')
        unknown = [r for r in batch['reservations'].values()
                   if r.get('status') in {'reserved', 'unknown'}]
        for reservation in sorted(unknown, key=lambda row: row['operation_id']):
            result = await self.adapter.reconcile_operation(batch['repository'], reservation)
            if result.get('status') != 'confirmed':
                return blocked('remote_outcome_still_unknown')
            reservation['status'] = 'confirmed'; reservation['result'] = dict(result); self.ledger.save()
            payload = reservation.get('payload', {})
            if reservation['kind'] in {'integrate_member', 'integrate_repaired_member'}:
                if not SHA.fullmatch(str(result.get('head_sha', ''))):
                    return blocked('reconciled_integration_head_invalid')
                batch['working_head'] = result['head_sha']
                match = next((row for row in batch['members'].values()
                              if row.get('source_sha') == payload.get('source_sha')), None)
                if match is None:
                    return blocked('reconciled_member_not_found')
                match['status'] = 'integrated'
            elif reservation['kind'] == 'snapshot_ref':
                if result.get('head_sha') != payload.get('sha'):
                    return blocked('reconciled_snapshot_head_mismatch')
                batch['snapshot_sha'] = result['head_sha']
            elif reservation['kind'] == 'create_customer_pr':
                pr = result.get('pr')
                if (not isinstance(pr, Mapping) or pr.get('correlation') != batch['batch_key']
                        or pr.get('source_sha') != batch.get('snapshot_sha')):
                    return blocked('reconciled_customer_pr_mismatch')
                batch['customer_pr'] = dict(pr); batch['state'] = 'delivery_open'
                for member in batch['members'].values():
                    if member['status'] == 'integrated':
                        member['status'] = 'in_delivery'
        if unknown and batch['state'] == 'blocked_unknown':
            batch['state'] = 'collecting'; self.ledger.save()
        return {'state': batch['state'], 'batch_key': batch_key, 'reconciled': len(unknown)}

    async def observe_customer_completion(self, batch_key):
        batch = self.ledger.document['batches'].get(batch_key)
        if not batch or not batch.get('customer_pr'):
            return blocked('customer_pr_not_recorded')
        pr = await self.adapter.get_customer_pr(batch['repository'], batch['customer_pr']['id'])
        if not isinstance(pr, Mapping) or pr.get('status') not in {'active', 'completed', 'abandoned'}:
            return blocked('customer_pr_state_unavailable')
        if pr.get('status') == 'active':
            return {'state': 'in_delivery', 'batch_key': batch_key}
        if pr.get('status') == 'abandoned':
            batch['state'] = 'abandoned'; self.ledger.save()
            return {'state': 'abandoned', 'batch_key': batch_key}
        if (pr.get('source_sha') != batch.get('snapshot_sha')
                or not SHA.fullmatch(str(pr.get('merge_sha', '')))):
            batch['state'] = 'blocked_external_edit'; self.ledger.save()
            return blocked('customer_merge_correlation_mismatch')
        confirmed = await self.adapter.confirm_customer_merge(
            batch['repository'], batch['target_branch'], batch['snapshot_sha'], pr)
        if confirmed.get('confirmed') is not True or not SHA.fullmatch(str(confirmed.get('target_sha', ''))):
            return blocked('customer_merge_not_reconciled')
        batch['customer_merge_sha'] = pr['merge_sha']
        batch['mapped_target_sha'] = confirmed['target_sha']
        batch['state'] = 'customer_merged'
        for member in batch['members'].values():
            member['status'] = 'customer_merged'
        self.ledger.save()
        return {'state': 'customer_merged', 'batch_key': batch_key, 'merge_sha': pr['merge_sha']}
