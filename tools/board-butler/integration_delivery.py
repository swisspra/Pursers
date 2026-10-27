"""Deterministic delivery runtimes. Human promotion branches are never completed."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pursers_client.delivery_workflow import parse_delivery_workflow

SHA = re.compile(r'^[0-9a-f]{40}$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')


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
    base, integration, target = (result['base_branch'], result['integration_branch'],
                                  result['target_branch'])
    folded = [base.casefold(), integration.casefold(), target.casefold()]
    if (folded[0] == folded[1] or folded[1] == folded[2]
            or (folded[0] == folded[2]
                and not (mode == 'branch_only' or (mode == 'batch_pr' and base == target)))):
        raise ValueError('base and integration must be distinct; only an exact batch PR base may equal its customer target')
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


def runtime_policy_from_resolved(resolved: Mapping[str, Any], project: Mapping[str, Any]) -> dict[str, Any]:
    """Compile the shared resolved-policy shape into the installed runtime contract."""
    policy = resolved.get('policy') if isinstance(resolved, Mapping) else None
    if not isinstance(policy, Mapping):
        raise ValueError('resolved delivery policy is unavailable')
    repository = project.get('repository_url')
    if not isinstance(repository, str) or not repository:
        raise ValueError('mapped repository identity is unavailable')
    mode = policy.get('mode')
    if mode == 'per_ticket_pr':
        return {'mode': 'per_ticket_pr'}
    trigger = policy.get('release_trigger')
    if not isinstance(trigger, Mapping):
        raise ValueError('resolved release trigger is unavailable')
    trigger_kind = trigger.get('kind')
    result = {
        'mode': mode, 'repository': repository,
        'base_branch': policy.get('mapped_base'),
        'integration_branch': policy.get('integration_branch'),
        'snapshot_prefix': policy.get('snapshot_branch_prefix'),
        'target_branch': policy.get('final_pr_target') or policy.get('mapped_base'),
        'trigger': trigger_kind,
        'snapshot_strategy': 'rolling' if policy.get('pr_update') == 'rolling' else 'frozen',
        'policy_revision': _digest(policy)[:40],
    }
    if trigger_kind == 'scheduled':
        fields = str(trigger.get('schedule', '')).split()
        if (len(fields) != 5 or not fields[0].isdigit() or not fields[1].isdigit()
                or fields[2:] != ['*', '*', '*']):
            raise ValueError('installed scheduler requires one exact daily minute')
        minute, hour = int(fields[0]), int(fields[1])
        if not 0 <= minute <= 59 or not 0 <= hour <= 23:
            raise ValueError('scheduled release time is invalid')
        result.update(schedule=f'{hour:02d}:{minute:02d}', timezone=trigger.get('timezone'))
    if policy.get('conflict_policy') == 'repair_then_review':
        raise ValueError('conflict repair runner is not configured')
    return parse_batch_policy(result) or {}


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


class VerifiedGitConnectorAdapter:
    """Production adapter: verified fleet clone for Git, injected connector for PRs."""

    def __init__(self, repo_path: Path, repository_url: str, connector: Any,
                 *, validation_commands: list[str] | None = None) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.repository_url = repository_url.rstrip('/').removesuffix('.git').casefold()
        self.connector = connector
        self.validation_commands = list(validation_commands or [])
        if not (self.repo_path / '.git').exists():
            raise ValueError('batch delivery requires a verified fleet Git clone')
        observed = self._git('config', '--get', 'remote.origin.url').strip()
        if observed.rstrip('/').removesuffix('.git').casefold() != self.repository_url:
            raise ValueError('fleet clone origin does not match mapped repository')

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0',
               'GIT_AUTHOR_NAME': 'Pursers Batch Delivery',
               'GIT_AUTHOR_EMAIL': 'batch-delivery@pursers.invalid',
               'GIT_COMMITTER_NAME': 'Pursers Batch Delivery',
               'GIT_COMMITTER_EMAIL': 'batch-delivery@pursers.invalid'}
        result = subprocess.run(['git', *args], cwd=cwd or self.repo_path, env=env,
                                text=True, capture_output=True, check=False)
        if check and result.returncode:
            raise RuntimeError('verified Git operation failed')
        return result.stdout.strip()

    async def read_ref(self, repository, branch):
        if repository.rstrip('/').removesuffix('.git').casefold() != self.repository_url:
            raise ValueError('repository identity changed')
        output = await asyncio.to_thread(
            self._git, 'ls-remote', '--heads', 'origin', f'refs/heads/{_branch(branch, "branch")}')
        rows = [line.split() for line in output.splitlines() if line.strip()]
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 2 or not SHA.fullmatch(rows[0][0]):
            raise RuntimeError('remote ref response is ambiguous')
        await asyncio.to_thread(
            self._git, 'fetch', '--no-tags', 'origin', f'refs/heads/{branch}')
        return rows[0][0]

    async def ensure_branch(self, repository, branch, sha, expected, operation_id):
        del operation_id
        observed = await self.read_ref(repository, branch)
        if observed != expected:
            return {'status': 'conflict', 'head_sha': observed}
        _sha(sha, 'sha')
        if expected is not None:
            base = await asyncio.to_thread(self._git, 'merge-base', expected, sha)
            if base != expected:
                return {'status': 'conflict', 'head_sha': observed}
        try:
            await asyncio.to_thread(self._git, 'push', 'origin', f'{sha}:refs/heads/{branch}')
        except RuntimeError:
            return {'status': 'unknown'}
        confirmed = await self.read_ref(repository, branch)
        return {'status': 'confirmed' if confirmed == sha else 'unknown', 'head_sha': confirmed}

    async def integrate_reviewed(self, repository, source_sha, branch, expected, operation_id):
        del operation_id
        if await self.read_ref(repository, branch) != expected:
            return {'status': 'conflict', 'head_sha': await self.read_ref(repository, branch)}
        _sha(source_sha, 'source_sha'); _sha(expected, 'expected_head')
        with tempfile.TemporaryDirectory(prefix='pursers-batch-worktree-') as raw:
            worktree = Path(raw)
            await asyncio.to_thread(self._git, 'worktree', 'add', '--detach', str(worktree), expected)
            try:
                merged = subprocess.run(
                    ['git', 'merge', '--no-ff', '--no-edit', source_sha], cwd=worktree,
                    env={**os.environ, 'GIT_AUTHOR_NAME': 'Pursers Batch Delivery',
                         'GIT_AUTHOR_EMAIL': 'batch-delivery@pursers.invalid',
                         'GIT_COMMITTER_NAME': 'Pursers Batch Delivery',
                         'GIT_COMMITTER_EMAIL': 'batch-delivery@pursers.invalid'},
                    text=True, capture_output=True, check=False)
                if merged.returncode:
                    return {'status': 'conflict', 'source_sha': source_sha}
                head = await asyncio.to_thread(self._git, 'rev-parse', '--verify', 'HEAD^{commit}', cwd=worktree)
                try:
                    await asyncio.to_thread(self._git, 'push', 'origin', f'{head}:refs/heads/{branch}', cwd=worktree)
                except RuntimeError:
                    return {'status': 'unknown'}
            finally:
                await asyncio.to_thread(self._git, 'worktree', 'remove', '--force', str(worktree))
        confirmed = await self.read_ref(repository, branch)
        return {'status': 'confirmed' if confirmed == head else 'unknown', 'head_sha': confirmed}

    async def validate_cumulative(self, repository, base_branch, source_sha, members):
        target_sha = await self.read_ref(repository, base_branch)
        _sha(target_sha, 'target_sha'); _sha(source_sha, 'source_sha')
        for member in members:
            source = _sha(member.get('source_sha'), 'member.source_sha')
            base = await asyncio.to_thread(self._git, 'merge-base', source, source_sha)
            if base != source:
                return {'passed': False, 'source_sha': source_sha, 'target_sha': target_sha}
        with tempfile.TemporaryDirectory(prefix='pursers-batch-validation-') as raw:
            worktree = Path(raw)
            await asyncio.to_thread(self._git, 'worktree', 'add', '--detach', str(worktree), source_sha)
            try:
                for command in self.validation_commands:
                    args = shlex.split(command)
                    if not args:
                        raise ValueError('validation command is empty')
                    result = await asyncio.to_thread(
                        subprocess.run, args, cwd=worktree, text=True,
                        capture_output=True, check=False)
                    if result.returncode:
                        return {'passed': False, 'source_sha': source_sha,
                                'target_sha': target_sha, 'failed_command': command}
            finally:
                await asyncio.to_thread(self._git, 'worktree', 'remove', '--force', str(worktree))
        return {'passed': True, 'source_sha': source_sha, 'target_sha': target_sha,
                'commands': list(self.validation_commands)}

    async def list_customer_prs(self, repository, target_branch, status):
        return await self.connector.list_customer_prs(repository, target_branch, status)

    async def create_customer_pr(self, repository, payload, body, operation_id):
        return await self.connector.create_customer_pr(repository, payload, body, operation_id)

    async def update_customer_pr(self, repository, payload, body, operation_id):
        return await self.connector.update_customer_pr(repository, payload, body, operation_id)

    async def get_customer_pr(self, repository, pr_id):
        return await self.connector.get_customer_pr(repository, pr_id)

    async def confirm_customer_merge(self, repository, target_branch, snapshot_sha, pr):
        del snapshot_sha
        target_sha = await self.read_ref(repository, target_branch)
        return {'confirmed': pr.get('status') == 'completed' and target_sha is not None,
                'target_sha': target_sha}

    async def request_conflict_repair(self, repository, runner, ticket_id, source_sha, working_head):
        if not hasattr(self.connector, 'request_conflict_repair'):
            return {'status': 'unavailable'}
        return await self.connector.request_conflict_repair(
            repository, runner, ticket_id, source_sha, working_head)

    async def reconcile_operation(self, repository, reservation):
        payload = reservation.get('payload', {})
        kind = reservation.get('kind')
        if kind in {'create_integration_branch', 'snapshot_ref'}:
            head = await self.read_ref(repository, payload['branch'])
            return {'status': 'confirmed' if head == payload['sha'] else 'unknown', 'head_sha': head}
        if kind in {'integrate_member', 'integrate_repaired_member'}:
            head = await self.read_ref(repository, payload['integration_branch'])
            source = payload['source_sha']
            if head and await asyncio.to_thread(self._git, 'merge-base', source, head) == source:
                return {'status': 'confirmed', 'head_sha': head}
            return {'status': 'unknown'}
        return await self.connector.reconcile_operation(repository, reservation)


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
                      and (b.get('state') == 'collecting'
                           or (b.get('state') == 'delivery_open'
                               and policy['snapshot_strategy'] == 'rolling'
                               and b.get('snapshot_frozen') is not True))]
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
            # The durable ledger is the evidence record; never truncate it to fit
            # a provider's PR-description limit. The PR receives only a manifest.
            'summary': str(member['summary']), 'tests': str(member['tests']),
            'blockers': str(member.get('blockers', 'none')),
            'baseline_failures': str(member.get('baseline_failures', 'none')),
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
                pending = [r for r in existing_batch['reservations'].values()
                           if r.get('status') in {'reserved', 'unknown'}]
                draft_only = (existing_batch.get('state') == 'blocked_unknown' and pending
                              and all(r.get('kind') == 'create_draft_customer_pr'
                                      for r in pending))
                if draft_only:
                    # Read-only reconciliation only; never retry a provider create here.
                    await self.reconcile_unknown(indexed['batch_key'])
                    existing_batch = self.ledger.document['batches'][indexed['batch_key']]
                    existing = existing_batch['members'][member['ticket_id']]
                elif pending:
                    return blocked('remote_outcome_unknown_reconcile_before_retry')
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
            if (not isinstance(request, Mapping)
                    or not isinstance(request.get('cohort_id'), str)
                    or not request['cohort_id']
                    or sorted(request.get('members', [])) != sorted(batch['members'])):
                return False, 'explicit_ready_cohort_required'
            prior = batch.get('ready_cohort_id')
            if (prior is not None and prior != request['cohort_id']
                    and policy['snapshot_strategy'] == 'frozen'):
                return False, 'ready_cohort_already_frozen'
            batch['ready_cohort_id'] = request['cohort_id']
            history = batch.setdefault('ready_cohort_ids', [])
            if request['cohort_id'] not in history:
                history.append(request['cohort_id'])
            self.ledger.save()
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

    async def _recover_confirmed_delivery(self, policy, batch, request):
        """Return only an existing, still-correlated frozen delivery receipt.

        This path is deliberately separate from a new release: it never runs
        the ready/schedule/manual trigger or initiates a delivery mutation.
        The workflow adapter has already revalidated immutable QA and member
        evidence before calling release; this method checks that the ledger
        and remote state still describe that exact previously released cohort.
        """
        batch_key = batch.get('batch_key')
        head = batch.get('working_head')
        snapshot_branch = batch.get('snapshot_branch')
        snapshot_sha = batch.get('snapshot_sha')
        binding = batch.get('workflow_delivery')
        members = batch.get('members')
        if (not isinstance(batch_key, str) or not batch_key
                or batch.get('effective_policy_revision') != policy['policy_revision']
                or batch.get('repository') != policy['repository']
                or batch.get('target_branch') != policy['target_branch']
                or batch.get('base_branch') != policy['base_branch']
                or batch.get('integration_branch') != policy['integration_branch']
                or batch.get('mode') != policy['mode']
                or batch.get('trigger') != policy['trigger']
                or batch.get('snapshot_strategy') != 'frozen'
                or batch.get('snapshot_frozen') is not True
                or not isinstance(head, str) or not SHA.fullmatch(head)
                or snapshot_sha != head
                or not isinstance(snapshot_branch, str)
                or not snapshot_branch.startswith(policy['snapshot_prefix'].rstrip('/') + '/')
                or not isinstance(members, Mapping) or not members
                or set(members) != {row.get('ticket_id') for row in members.values()
                                    if isinstance(row, Mapping)}
                or any(not isinstance(row, Mapping) or row.get('status') != 'in_delivery'
                       or not SHA.fullmatch(str(row.get('source_sha', '')))
                       or not isinstance(row.get('evidence_digest'), str)
                       for row in members.values())):
            return blocked('confirmed_delivery_ledger_binding_mismatch')
        cohort = request.get('cohort_id') if isinstance(request, Mapping) else None
        requested_members = request.get('members') if isinstance(request, Mapping) else None
        if (not isinstance(cohort, str) or not cohort
                or batch.get('ready_cohort_id') != cohort
                or not isinstance(requested_members, list)
                or sorted(requested_members) != sorted(members)
                or not isinstance(binding, Mapping) or binding.get('cohort_id') != cohort
                or binding.get('combined_revision') != head):
            return blocked('confirmed_delivery_cohort_binding_mismatch')
        validation = batch.get('validation')
        if (not isinstance(validation, Mapping) or validation.get('passed') is not True
                or validation.get('source_sha') != head
                or not SHA.fullmatch(str(validation.get('target_sha', '')))):
            return blocked('confirmed_delivery_validation_binding_mismatch')
        try:
            observed_snapshot = await self.adapter.read_ref(policy['repository'], snapshot_branch)
        except Exception:
            return blocked('confirmed_delivery_snapshot_unavailable')
        if observed_snapshot != head:
            return blocked('confirmed_delivery_snapshot_mismatch')

        if policy['mode'] == 'branch_only':
            if batch.get('state') != 'in_delivery' or batch.get('customer_pr') is not None:
                return blocked('confirmed_branch_receipt_mismatch')
            return {'state': 'in_delivery', 'batch_key': batch_key,
                    'branch': snapshot_branch, 'released': True}

        if policy['mode'] != 'batch_pr' or batch.get('state') != 'delivery_open':
            return blocked('confirmed_pr_receipt_state_mismatch')
        recorded = batch.get('customer_pr')
        if (not isinstance(recorded, Mapping)
                or isinstance(recorded.get('id'), bool)
                or not isinstance(recorded.get('id'), (str, int))
                or (isinstance(recorded.get('id'), str) and not recorded['id'].strip())
                or (isinstance(recorded.get('id'), int) and recorded['id'] < 1)
                or recorded.get('correlation') != batch_key
                or recorded.get('source_branch') != snapshot_branch
                or recorded.get('source_sha') != head
                or recorded.get('target_branch') != policy['target_branch']):
            return blocked('confirmed_pr_ledger_binding_mismatch')
        try:
            active = await self.adapter.list_customer_prs(
                policy['repository'], policy['target_branch'], status='active')
            remote = await self.adapter.get_customer_pr(policy['repository'], recorded['id'])
        except Exception:
            return blocked('confirmed_pr_remote_state_unavailable')
        def exact_pr(pr):
            return (isinstance(pr, Mapping)
                    and pr.get('id') == recorded['id']
                    and pr.get('status') == 'active'
                    and pr.get('correlation') == batch_key
                    and pr.get('source_branch') == snapshot_branch
                    and pr.get('source_sha') == head
                    and pr.get('target_branch') == policy['target_branch'])
        if (not isinstance(active, list) or len(active) != 1
                or not exact_pr(active[0]) or not exact_pr(remote)):
            return blocked('confirmed_pr_remote_binding_mismatch')
        return {'state': 'delivery_open', 'batch_key': batch_key,
                'pr_id': recorded['id'], 'released': True}

    async def release_report_held_draft(self, raw_policy, batch_key, *, request=None,
                                        report=None):
        """Publish one correlated draft without changing QA or acceptance state."""
        policy = parse_batch_policy(raw_policy)
        if policy is None or policy['mode'] != 'batch_pr' or policy['snapshot_strategy'] != 'frozen':
            return blocked('report_held_draft_requires_frozen_batch_pr')
        batch = self.ledger.document['batches'].get(batch_key)
        if not batch or batch.get('effective_policy_revision') != policy['policy_revision']:
            return blocked('batch_not_found_or_policy_changed')
        if batch.get('state') == 'blocked_unknown':
            await self.reconcile_unknown(batch_key)
            batch = self.ledger.document['batches'].get(batch_key)
            if batch.get('state') == 'blocked_unknown':
                return blocked('draft_pr_create_outcome_unknown')
        if batch.get('state') == 'draft_open':
            if (batch.get('publication_state') != 'report_held_draft'
                    or batch.get('draft_report_source_digest') != _digest(dict(report or {}))):
                return blocked('draft_publication_ledger_mismatch')
            pr = batch.get('customer_pr')
            if not isinstance(pr, Mapping) or pr.get('is_draft') is not True:
                return blocked('draft_pr_ledger_binding_mismatch')
            try:
                active = await self.adapter.list_customer_prs(
                    policy['repository'], policy['target_branch'], status='active')
                remote = await self.adapter.get_customer_pr(policy['repository'], pr['id'])
            except Exception:
                return blocked('draft_pr_remote_state_unavailable')
            if (not isinstance(active, list) or len(active) != 1
                    or not self._matches_draft_pr(active[0], batch)
                    or not self._matches_draft_pr(remote, batch)
                    or active[0].get('id') != pr.get('id')
                    or remote.get('id') != pr.get('id')):
                return blocked('draft_pr_remote_binding_mismatch')
            return {'state': 'report_held_draft', 'batch_key': batch_key,
                    'pr_id': pr['id'], 'released': True, 'is_draft': True}
        if batch.get('state') != 'collecting':
            return blocked('batch_not_collecting_for_draft')
        if (not isinstance(report, Mapping)
                or report.get('quality_status') not in {'approved', 'rejected'}
                or report.get('combined_revision') != batch.get('working_head')
                or not isinstance(report.get('submission'), Mapping)
                or not isinstance(report.get('review'), Mapping)):
            return blocked('exact_completed_qa_report_required')
        due, reason = self._trigger_due(policy, batch, request)
        if not due:
            return {'state': batch['state'], 'batch_key': batch_key,
                    'released': False, 'reason': reason}
        try:
            validation = await self.adapter.validate_cumulative(
                policy['repository'], policy['base_branch'], batch['working_head'],
                list(batch['members'].values()))
        except Exception as exc:
            batch['state'] = 'blocked_validation'
            batch['validation_error_class'] = type(exc).__name__
            self.ledger.save()
            return blocked('exact_validation_capability_unavailable')
        if (not isinstance(validation, Mapping)
                or validation.get('source_sha') != batch['working_head']
                or not SHA.fullmatch(str(validation.get('target_sha', '')))
                or type(validation.get('passed')) is not bool):
            batch['state'] = 'blocked_validation'; self.ledger.save()
            return blocked('exact_cumulative_validation_disposition_required')
        request = dict(request or {})
        request['cohort_id'] = request.get('cohort_id') or batch.get('ready_cohort_id')
        request['members'] = request.get('members') or list(batch.get('members', {}))
        if not request['cohort_id']:
            return blocked('exact_ready_cohort_required')
        # Check the final exact body before snapshot or PR reservations/mutations.
        render_batch = {**batch, 'snapshot_sha': batch.get('snapshot_sha') or batch['working_head']}
        try:
            body = self._pr_body(render_batch, draft_report=report)
        except (TypeError, ValueError, UnicodeError):
            return blocked('customer_pr_body_preflight_failed')
        batch['validation'] = dict(validation)
        batch['validation_disposition'] = 'passed' if validation['passed'] else 'failed_or_baseline'
        self.ledger.save()
        snapshot = await self._snapshot(policy, batch)
        if snapshot.get('state') == 'integration_blocked':
            return snapshot
        return await self._open_or_reuse_report_held_draft(policy, batch, report, body=body)

    @staticmethod
    def _matches_draft_pr(pr, batch):
        return (isinstance(pr, Mapping) and pr.get('status') == 'active'
                and pr.get('is_draft') is True
                and pr.get('correlation') == batch.get('batch_key')
                and pr.get('source_branch') == batch.get('snapshot_branch')
                and pr.get('source_sha') == batch.get('snapshot_sha')
                and pr.get('target_branch') == batch.get('target_branch'))

    async def _open_or_reuse_report_held_draft(self, policy, batch, report, *, body):
        """Create/reconcile an exactly-once draft without using ready publication."""
        target = await self.adapter.read_ref(policy['repository'], policy['target_branch'])
        validation = batch.get('validation')
        if (not isinstance(validation, Mapping) or type(validation.get('passed')) is not bool
                or validation.get('source_sha') != batch.get('working_head')
                or validation.get('target_sha') != target):
            batch['state'] = 'blocked_validation'
            self.ledger.save()
            return blocked('draft_validation_binding_mismatch')
        active = await self.adapter.list_customer_prs(
            policy['repository'], policy['target_branch'], status='active')
        if not isinstance(active, list):
            return blocked('customer_pr_inventory_unavailable')
        digest = _digest(dict(report))
        exact = [row for row in active if self._matches_draft_pr(row, batch)]
        if len(exact) > 1 or len(active) != len(exact):
            batch['state'] = 'blocked_existing_pr'
            self.ledger.save()
            return blocked('one_active_customer_pr_limit')
        reservation = None
        if exact:
            pr = exact[0]
            reservation = next((row for row in batch.get('reservations', {}).values()
                                if row.get('kind') == 'create_draft_customer_pr'
                                and row.get('status') == 'confirmed'
                                and isinstance(row.get('result'), Mapping)
                                and isinstance(row['result'].get('pr'), Mapping)
                                and row['result']['pr'].get('id') == pr.get('id')), None)
            if reservation is None:
                batch['state'] = 'blocked_existing_pr'
                self.ledger.save()
                return blocked('draft_pr_has_no_confirmed_publication_intent')
        else:
            payload = {'source_branch': batch['snapshot_branch'], 'target_branch': policy['target_branch'],
                       'snapshot_sha': batch['snapshot_sha'], 'correlation': batch['batch_key'],
                       'mutable': False, 'is_draft': True, 'report_digest': digest,
                       'publication_state': 'report_held_draft'}
            batch['draft_report'] = dict(report)
            batch['draft_report_source_digest'] = digest
            self.ledger.save()
            operation_id = _digest([batch['batch_key'], 'create_draft_customer_pr', payload])
            prior_intent = batch.get('reservations', {}).get(operation_id)
            is_new_intent = prior_intent is None
            reservation = self._reserve(batch, 'create_draft_customer_pr', payload)
            status = reservation.get('status')
            if status == 'confirmed':
                result = reservation.get('result')
                pr = result.get('pr') if isinstance(result, Mapping) else None
            elif status in {'reserved', 'unknown'} and not is_new_intent:
                # A persisted intent might have crossed the provider boundary
                # before a crash. Reconcile only; never repeat its create.
                try:
                    result = await self.adapter.reconcile_operation(policy['repository'], reservation)
                except Exception:
                    result = {'status': 'unknown'}
                if result.get('status') != 'confirmed':
                    reservation['status'] = 'unknown'
                    self.ledger.save()
                    batch['state'] = 'blocked_unknown'
                    self.ledger.save()
                    return blocked('draft_pr_create_outcome_unknown')
                self._finish(reservation, result)
                pr = result.get('pr')
            elif status in {'reserved', 'new'} and is_new_intent:
                try:
                    result = await self.adapter.create_customer_pr(
                        policy['repository'], payload, body, reservation['operation_id'])
                except Exception:
                    reservation['status'] = 'unknown'
                    self.ledger.save()
                    batch['state'] = 'blocked_unknown'
                    self.ledger.save()
                    return blocked('draft_pr_create_outcome_unknown')
                self._finish(reservation, result)
                if result.get('status') != 'confirmed':
                    batch['state'] = 'blocked_unknown'
                    self.ledger.save()
                    return blocked('draft_pr_create_outcome_unknown')
                pr = result.get('pr')
            else:
                return blocked('draft_pr_reservation_not_creatable')
        if reservation.get('status') != 'confirmed':
            self._finish(reservation, {'status': 'confirmed', 'pr': pr})
        if (not isinstance(pr, Mapping) or pr.get('status') != 'active'
                or pr.get('id') != (reservation.get('result', {}).get('pr', {}).get('id')
                                     if isinstance(reservation, Mapping)
                                     and isinstance(reservation.get('result'), Mapping)
                                     and isinstance(reservation['result'].get('pr'), Mapping) else pr.get('id'))
                or pr.get('is_draft') is not True
                or pr.get('source_branch') != batch['snapshot_branch']
                or pr.get('source_sha') != batch['snapshot_sha']
                or pr.get('target_branch') != batch['target_branch']
                or pr.get('correlation') != batch['batch_key']):
            batch['state'] = 'blocked_existing_pr'
            self.ledger.save()
            return blocked('draft_pr_correlation_mismatch')
        batch['customer_pr'] = dict(pr)
        batch['state'] = 'draft_open'
        batch['publication_state'] = 'report_held_draft'
        batch['draft_report_source_digest'] = digest
        batch['draft_report'] = dict(report)
        batch['released_at'] = self.clock().astimezone(timezone.utc).isoformat()
        batch['delivery_ready'] = False
        batch['qa_status'] = 'report_held'
        batch['security_status'] = 'pending'
        self.ledger.save()
        return {'state': 'report_held_draft', 'batch_key': batch['batch_key'],
                'pr_id': pr['id'], 'released': True, 'is_draft': True}

    async def release(self, raw_policy, batch_key, *, request=None):
        policy = parse_batch_policy(raw_policy)
        if policy is None or policy['mode'] == 'per_ticket_pr':
            return blocked('batch_delivery_not_configured')
        batch = self.ledger.document['batches'].get(batch_key)
        if not batch:
            return blocked('batch_not_found')
        if batch['effective_policy_revision'] != policy['policy_revision']:
            return blocked('active_batch_policy_is_frozen')
        if batch.get('state') == 'draft_open':
            return blocked('report_held_draft_is_not_ready_for_promotion')
        if (batch['state'] in {'delivery_open', 'in_delivery'}
                and batch.get('snapshot_strategy') == 'frozen'
                and isinstance(batch.get('workflow_delivery'), Mapping)):
            return await self._recover_confirmed_delivery(policy, batch, request)
        releasable = batch['state'] in {'collecting', 'waiting_customer_slot'}
        rolling_update = (batch['state'] == 'delivery_open'
                          and policy['snapshot_strategy'] == 'rolling')
        if not (releasable or rolling_update):
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
        # Validate the exact concise description before snapshot or PR mutations.
        # The full evidence stays in the private ledger and canonical QA packet.
        body = None
        if policy['mode'] == 'batch_pr':
            try:
                body = self._pr_body({**batch, 'snapshot_sha': batch['working_head']})
            except (TypeError, ValueError, UnicodeError):
                return blocked('customer_pr_body_preflight_failed')
        if batch['state'] == 'delivery_open' and policy['snapshot_strategy'] == 'rolling':
            return await self._roll_snapshot(policy, batch, body=body)
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
        return await self._open_or_reuse_pr(policy, batch, body=body)

    async def _snapshot(self, policy, batch):
        observed = await self.adapter.read_ref(policy['repository'], batch['snapshot_branch'])
        if batch.get('snapshot_frozen'):
            if observed != batch.get('snapshot_sha'):
                batch['state'] = 'blocked_external_edit'; self.ledger.save()
                return blocked('frozen_snapshot_changed_externally')
            if batch['working_head'] != batch.get('snapshot_sha'):
                batch['state'] = 'blocked_external_edit'; self.ledger.save()
                return blocked('frozen_snapshot_is_immutable')
            return {'state': 'snapshot_ready'}
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

    async def _open_or_reuse_pr(self, policy, batch, *, body=None):
        current_target = await self.adapter.read_ref(policy['repository'], policy['target_branch'])
        if current_target != batch.get('validation', {}).get('target_sha'):
            batch['state'] = 'blocked_validation'; self.ledger.save()
            return blocked('customer_target_changed_revalidation_required')
        active = await self.adapter.list_customer_prs(policy['repository'], policy['target_branch'], status='active')
        if not isinstance(active, list):
            return blocked('customer_pr_inventory_unavailable')
        exact = [p for p in active if p.get('correlation') == batch['batch_key']
                 and p.get('source_branch') == batch['snapshot_branch']
                 and p.get('source_sha') == batch['snapshot_sha']
                 and p.get('target_branch') == policy['target_branch']
                 and p.get('is_draft') is not True]
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
            # Render and validate before persisting a reservation. Local size or
            # rendering failures must never create an ambiguous remote intent.
            if body is None:
                try:
                    body = self._pr_body(batch)
                except (TypeError, ValueError, UnicodeError):
                    return blocked('customer_pr_body_preflight_failed')
            payload = {'source_branch': batch['snapshot_branch'], 'target_branch': policy['target_branch'],
                       'snapshot_sha': batch['snapshot_sha'], 'correlation': batch['batch_key'],
                       'mutable': policy['snapshot_strategy'] == 'rolling', 'is_draft': False,
                       'body_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest()}
            reservation = self._reserve(batch, 'create_customer_pr', payload)
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
                or pr.get('target_branch') != policy['target_branch']
                or pr.get('correlation') != batch['batch_key']):
            batch['state'] = 'blocked_existing_pr'; self.ledger.save()
            return blocked('customer_pr_correlation_mismatch')
        batch['customer_pr'] = dict(pr); batch['state'] = 'delivery_open'
        batch['released_at'] = self.clock().astimezone(timezone.utc).isoformat()
        for member in batch['members'].values():
            if member['status'] == 'integrated': member['status'] = 'in_delivery'
        self.ledger.save()
        return {'state': 'in_delivery', 'batch_key': batch['batch_key'], 'pr_id': pr['id'], 'released': True}

    async def _roll_snapshot(self, policy, batch, *, body=None):
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
                   'correlation': batch['batch_key'],
                   'body_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest()}
        if body is None:
            try:
                body = self._pr_body(batch)
            except (TypeError, ValueError, UnicodeError):
                return blocked('customer_pr_body_preflight_failed')
        reservation = self._reserve(batch, 'update_customer_pr', payload)
        try:
            result = await self.adapter.update_customer_pr(
                policy['repository'], payload, body, reservation['operation_id'])
        except Exception:
            reservation['status'] = 'unknown'
            batch['state'] = 'blocked_unknown'
            self.ledger.save()
            return blocked('rolling_customer_pr_update_outcome_unknown')
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
    def _pr_body(batch, *, max_code_units=3700, draft_report=None):
        """Render a deterministic bounded PR summary; evidence remains canonical."""
        if (not isinstance(max_code_units, int) or isinstance(max_code_units, bool)
                or max_code_units < 1 or max_code_units > 3700
                or not isinstance(batch, Mapping) or not isinstance(batch.get('members'), Mapping)
                or not isinstance(batch.get('batch_key'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', batch['batch_key'])
                or not isinstance(batch.get('snapshot_sha'), str)
                or not SHA.fullmatch(batch['snapshot_sha'])):
            raise ValueError('PR summary identity is incomplete')
        members = sorted(batch['members'].values(), key=lambda m: m.get('ticket_id', '')
                         if isinstance(m, Mapping) else '')
        if any(not isinstance(member, Mapping)
               or not isinstance(member.get('ticket_id'), str) or not member['ticket_id']
               or not isinstance(member.get('source_sha'), str)
               or not SHA.fullmatch(member['source_sha']) for member in members):
            raise ValueError('PR summary member manifest is incomplete')
        try:
            for member in members:
                member['ticket_id'].encode('utf-16-le')
        except UnicodeEncodeError as exc:
            raise ValueError('PR summary member identity is invalid Unicode') from exc
        manifest = {member['ticket_id']: member['source_sha'] for member in members}
        if len(manifest) != len(members):
            raise ValueError('PR summary member manifest has duplicate ticket IDs')
        workflow = batch.get('workflow_delivery')
        qa_reference = 'unavailable'
        qa_report_reference = 'unavailable'
        if isinstance(workflow, Mapping):
            ticket_id = workflow.get('final_validation_ticket_id')
            policy_sha = workflow.get('workflow_policy_sha256')
            report_sha = workflow.get('final_validation_policy_sha256')
            if (isinstance(ticket_id, str) and ticket_id
                    and SHA256.fullmatch(str(policy_sha or ''))
                    and SHA256.fullmatch(str(report_sha or ''))):
                qa_reference = ticket_id
                qa_report_reference = f'workflow-delivery:{policy_sha}; final-report-policy:{report_sha}'
        lines = [f"Pursers-Batch-Correlation: {batch['batch_key']}",
                 f"Pursers-Snapshot-Mutable: {str(batch.get('snapshot_strategy') == 'rolling').lower()}",
                 f"Immutable snapshot: {batch['snapshot_sha']}",
                 f"Reviewed members: {len(members)}",
                 f"Member manifest SHA-256: {_digest(manifest)}"]
        common = [f'Full QA ticket/report reference: {qa_reference} / {qa_report_reference}',
                  'QA/security acceptance: pending; this summary is not a security approval.',
                  'Customer merge is manual. No claim is made that Sonar issues are resolved.',
                  'Full tests, blockers, baseline findings, report fields, and approval evidence remain in the private batch ledger and canonical QA packet.']
        member_ids = ', '.join(member['ticket_id'] for member in members)
        all_members = f'All member tickets: {member_ids or "none"}'
        if draft_report is None:
            body = '\n'.join([*lines, all_members, *common])
        else:
            if not isinstance(draft_report, Mapping):
                raise ValueError('draft report is malformed')
            lines.extend(['', 'Pursers-Publication-State: report-held-draft',
                          'Status: DRAFT ONLY. QA/security review remains pending; no promotion or merge.',
                          'Quality status: ' + str(draft_report.get('quality_status', 'unknown')).upper(),
                          'Combined revision: ' + str(draft_report.get('combined_revision', 'unknown')),
                          f'Full QA report reference: {qa_reference} (report SHA-256: {_digest(dict(draft_report))})',
                          'VA status: UNKNOWN. RA status: NOT APPROVED. Security hold remains in force.',
                          'Manual boundary: no ready promotion, autocomplete, merge, or deployment is authorized.'])
            body = '\n'.join([*lines, all_members, *common])
        units = len(body.encode('utf-16-le')) // 2
        if units > max_code_units:
            # Never slice prose or an arbitrary prefix of the manifest.
            manifest_pointer = 'All member tickets: see complete canonical manifest in the private delivery ledger.'
            body = '\n'.join([*lines, manifest_pointer, *common])
            units = len(body.encode('utf-16-le')) // 2
        if units > max_code_units:
            raise ValueError('PR summary exceeds the supported description limit')
        return body

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
            elif reservation['kind'] in {'create_customer_pr', 'create_draft_customer_pr'}:
                pr = result.get('pr')
                draft = reservation['kind'] == 'create_draft_customer_pr'
                if (not isinstance(pr, Mapping) or pr.get('correlation') != batch['batch_key']
                        or pr.get('source_sha') != batch.get('snapshot_sha')
                        or pr.get('source_branch') != batch['snapshot_branch']
                        or pr.get('target_branch') != batch['target_branch']
                        or pr.get('is_draft') is not draft):
                    return blocked('reconciled_customer_pr_mismatch')
                batch['customer_pr'] = dict(pr)
                if draft:
                    batch['state'] = 'draft_open'
                    batch['publication_state'] = 'report_held_draft'
                    batch['draft_report_source_digest'] = reservation.get('payload', {}).get('report_digest')
                    batch['delivery_ready'] = False
                    batch['qa_status'] = 'report_held'
                    batch['security_status'] = 'pending'
                else:
                    batch['state'] = 'delivery_open'
                    for member in batch['members'].values():
                        if member['status'] == 'integrated':
                            member['status'] = 'in_delivery'
            elif reservation['kind'] == 'update_customer_pr':
                pr = result.get('pr')
                recorded = batch.get('customer_pr', {})
                if (not isinstance(pr, Mapping)
                        or pr.get('id') != payload.get('pr_id')
                        or pr.get('id') != recorded.get('id')
                        or pr.get('correlation') != batch['batch_key']
                        or pr.get('source_branch') != batch['snapshot_branch']
                        or pr.get('source_sha') != payload.get('source_sha')
                        or pr.get('target_branch') != batch['target_branch']):
                    return blocked('reconciled_customer_pr_update_mismatch')
                batch['customer_pr'] = dict(pr)
                batch['state'] = 'delivery_open'
                batch['validation_state'] = 'stale'
                for member in batch['members'].values():
                    if member['status'] == 'integrated':
                        member['status'] = 'in_delivery'
        if unknown and batch['state'] == 'blocked_unknown':
            batch['state'] = 'collecting'
        # Persist derived state (not just the confirmed reservation) before return;
        # a restart must observe draft_open/delivery_open after exact reconciliation.
        self.ledger.save()
        return {'state': batch['state'], 'batch_key': batch_key, 'reconciled': len(unknown)}

    async def observe_customer_completion(self, batch_key):
        batch = self.ledger.document['batches'].get(batch_key)
        if not batch or not batch.get('customer_pr'):
            return blocked('customer_pr_not_recorded')
        pr = await self.adapter.get_customer_pr(batch['repository'], batch['customer_pr']['id'])
        if not isinstance(pr, Mapping) or pr.get('status') not in {'active', 'completed', 'abandoned'}:
            return blocked('customer_pr_state_unavailable')
        recorded = batch['customer_pr']
        if (pr.get('id') != recorded.get('id')
                or pr.get('correlation') != batch['batch_key']
                or pr.get('source_branch') != batch['snapshot_branch']
                or pr.get('target_branch') != batch['target_branch']
                or pr.get('source_sha') != batch.get('snapshot_sha')):
            batch['state'] = 'blocked_external_edit'; self.ledger.save()
            return blocked('customer_pr_correlation_mismatch')
        if pr.get('status') == 'active':
            return {'state': 'in_delivery', 'batch_key': batch_key}
        if pr.get('status') == 'abandoned':
            batch['state'] = 'abandoned'; self.ledger.save()
            return {'state': 'abandoned', 'batch_key': batch_key}
        if not SHA.fullmatch(str(pr.get('merge_sha', ''))):
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
