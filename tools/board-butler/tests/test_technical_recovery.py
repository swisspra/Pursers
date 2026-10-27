from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "technical_recovery.py"
_spec = importlib.util.spec_from_file_location("technical_recovery", MODULE)
assert _spec and _spec.loader
recovery = importlib.util.module_from_spec(_spec)
import sys
sys.path.insert(0, str(MODULE.parent))
sys.modules.setdefault(_spec.name, recovery)
_spec.loader.exec_module(recovery)

BOARD = "board-demo"
PROJECT = "Demo"
POLICY = {
    "schema_version": 1,
    "mode": "active",
    "activation_id": "operator:fixture-activation",
    "project_id": PROJECT,
    "board_id": BOARD,
    "operations": ["technical_hold_resume", "dispatch_reconcile"],
    "sweep_interval_s": 300,
    "project_timeout_s": 30,
    "max_projects_per_sweep": 4,
    "page_size": 50,
    "max_no_progress_attempts": 3,
    "retry_backoff_s": 300,
}


def test_policy_is_absent_by_default_and_opt_in_is_exactly_project_scoped():
    assert recovery.parse_recovery_policy(None, project_id=PROJECT, board_id=BOARD, active=True) is None
    parsed = recovery.parse_recovery_policy(POLICY, project_id=PROJECT, board_id=BOARD, active=True)
    assert parsed and parsed.operations == ("dispatch_reconcile", "technical_hold_resume")
    for kwargs in (
        {"project_id": "Other", "board_id": BOARD, "active": True},
        {"project_id": PROJECT, "board_id": "Other", "active": True},
        {"project_id": PROJECT, "board_id": BOARD, "active": False},
    ):
        with pytest.raises(recovery.RecoveryPolicyError):
            recovery.parse_recovery_policy(POLICY, **kwargs)


def test_autonomous_project_timeout_defaults_to_120_and_preserves_overrides():
    minimal = {key: value for key, value in POLICY.items() if key != "project_timeout_s"}
    parsed = recovery.parse_recovery_policy(
        minimal, project_id=PROJECT, board_id=BOARD, active=True)
    assert parsed.project_timeout_s == 120
    overridden = recovery.parse_recovery_policy(
        POLICY, project_id=PROJECT, board_id=BOARD, active=True)
    assert overridden.project_timeout_s == 30
    for value in (None, True, "120", 0, 301):
        with pytest.raises(recovery.RecoveryPolicyError):
            recovery.parse_recovery_policy(
                {**POLICY, "project_timeout_s": value},
                project_id=PROJECT, board_id=BOARD, active=True)


def test_policy_rejects_unknown_authority_and_unbounded_values():
    for change in (
        {"extra_admin": True},
        {"operations": ["approve_final_qa"]},
        {"activation_id": ""},
        {"page_size": 101},
        {"project_timeout_s": True},
    ):
        invalid = {**POLICY, **change}
        with pytest.raises(recovery.RecoveryPolicyError):
            recovery.parse_recovery_policy(invalid, project_id=PROJECT, board_id=BOARD, active=True)


def test_fair_scheduler_rotates_even_when_batch_is_bounded():
    scheduler = recovery.FairProjectScheduler()
    assert scheduler.select(["a", "b", "c", "d", "e"], 2) == ["a", "b"]
    assert scheduler.select(["a", "b", "c", "d", "e"], 2) == ["c", "d"]
    assert scheduler.select(["a", "b", "c", "d", "e"], 2) == ["e", "a"]


def test_incomplete_projection_is_degraded_not_healthy_empty():
    status = recovery.classify_backlog(
        board_id=BOARD, tickets=[], complete=False,
        observed_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )
    assert status["status"] == "degraded"
    assert status["nonterminal_count"] is None
    assert status["tickets"] == []


def test_complete_backlog_records_current_nonterminal_wait_reasons():
    tickets = [
        {"ticket_id": "TK-human", "status": "needs_human",
         "human_request": {"request_id": "HR-1", "resolution": None}},
        {"ticket_id": "TK-claimed", "status": "claimed",
         "dispatch_state": {"state": "claimed"}},
        {"ticket_id": "TK-review", "status": "submitted"},
        {"ticket_id": "TK-open", "status": "open"},
        {"ticket_id": "TK-closed", "status": "closed"},
    ]
    status = recovery.classify_backlog(
        board_id=BOARD, tickets=tickets, complete=True,
        observed_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )
    assert status["status"] == "observed"
    assert status["nonterminal_count"] == 4
    assert [row["reason"] for row in status["tickets"]] == [
        "human_request_pending", "current_lease_or_assignment",
        "awaiting_independent_review", "open_dispatch_or_dependency",
    ]

# Resident-engine tests use the production binding (not an unused callback).
import asyncio
import copy
import hashlib
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace


def registry_for(*names):
    return {'schema_version': 1, 'projects': {name: {
        'board_id': BOARD, 'status': 'active', 'fleet': True,
        'work_dir': '/fixture/' + name,
        'butler_recovery_policy': {**POLICY, 'project_id': name, 'page_size': 25},
    } for name in names}}


FE03_SCHEMA = {
    'type': 'object', 'properties': {'decision': {'type': 'string', 'enum': [
        'authorize_candidate_publication_and_submission',
        'park_or_release_pending_combined_validation',
    ]}, 'rationale': {'type': 'string', 'maxLength': 500}},
    'required': ['decision'], 'additionalProperties': False,
}

FE04_SCHEMA = {
    'type': 'object', 'properties': {
        'authorized_worktree_path': {'type': 'string'},
        'authorized_branch': {'type': 'string'},
        'accepted_base_sha': {'type': 'string'},
        'preserve_other_worktree': {'type': 'boolean'},
    },
    'required': ['authorized_worktree_path', 'authorized_branch', 'accepted_base_sha',
                 'preserve_other_worktree'],
    'additionalProperties': False,
}


def held(ticket='TK-held', project=PROJECT, request='HR-1', message='Repair scoped test failure', schema=None):
    return {'ticket_id': ticket, 'target_url': project + '/src', 'title': 'Scoped repair',
            'description': 'Repair the assigned behavior and submit exact evidence.',
            'status': 'needs_human', 'claimed_by_agent_id': 'historical-worker',
            'claim_expires_at': None, 'review_lease': None,
            'human_request': {'request_id': request, 'kind': 'decision', 'message': message,
                              'requested_schema': copy.deepcopy(schema), 'resolution': None}}


class MemoryClient:
    def __init__(self, registry, tickets=()):
        self.state = {'project_registry': json.dumps(registry)}
        self.tickets = {row['ticket_id']: copy.deepcopy(row) for row in tickets}
        self.calls, self.resolutions = [], []
        self.identity = {'role': 'coordinator', 'principal_id': 'PR-butler'}
        self.agent_name = 'fixture-butler'

    async def board_state_get(self, key):
        if key not in self.state:
            raise ValueError('state key not found')
        return {'state': {'value': self.state[key]}}

    async def _call(self, tool, args):
        assert tool == 'board_state_update'
        key, value = args['key'], args['value']
        if args.get('expected_absent') and key in self.state:
            raise ValueError('state precondition failed')
        old = self.state.get(key)
        if args.get('expected_sha256') and (old is None or hashlib.sha256(old.encode()).hexdigest() != args['expected_sha256']):
            raise ValueError('state precondition failed')
        self.state[key] = value
        return {'ok': True}

    async def ticket_list(self, **kwargs):
        rows = [x for x in self.tickets.values() if x['status'] not in {'closed', 'canceled'}]
        start = int(kwargs.get('cursor') or 0)
        end = start + kwargs['limit']
        self.calls.append((start, kwargs['limit']))
        return {'tickets': copy.deepcopy(rows[start:end]), 'total_matching': len(rows),
                'next_cursor': str(end) if end < len(rows) else None}

    async def ticket_get(self, ticket_id, **kwargs):
        return {'ticket': copy.deepcopy(self.tickets[ticket_id])}

    async def ticket_human_resolve(self, ticket_id, request_id, action, **kwargs):
        t = self.tickets[ticket_id]
        assert t['status'] == 'needs_human'
        assert t['human_request']['request_id'] == request_id
        recovery.validate_typed_answer(kwargs['content'], t['human_request']['requested_schema']) if t['human_request']['requested_schema'] else None
        self.resolutions.append((ticket_id, kwargs))
        t['human_request']['resolution'] = {'content': kwargs['content'], 'resolved_by_principal_id': 'PR-butler'}
        t['status'] = 'open'
        return {'ticket': copy.deepcopy(t)}


class Resident:
    def __init__(self, client):
        self.client = client
        self.args = SimpleNamespace(runtime_mode='active', act_on_registry=True, home_board=BOARD,
                                    dry_run=False, kill_switch=False, veto_question=False)
        self.model_calls = []
        self.answer = {'decision': 'resume', 'category': 'scoped_repair',
                       'reason': 'Existing owned repair is authorized.',
                       'note': 'Fix only assigned defects, rerun scoped tests and submit for independent review.'}

    @asynccontextmanager
    async def _client_for_board(self, board):
        assert board == BOARD
        yield self.client

    async def _technical_recovery_choose(self, context):
        self.model_calls.append(copy.deepcopy(context))
        if isinstance(self.answer, Exception):
            raise self.answer
        request = context['ticket']['human_request']
        schema = request.get('requested_schema')
        if schema and self.answer.get('decision') == 'resume':
            typed = {}
            grounded_strings = {
                'authorized_worktree_path': '/fixture/worktrees/project/TK-123',
                'authorized_branch': 'project/TK-123', 'accepted_base_sha': 'a' * 40,
            }
            for name, definition in schema['properties'].items():
                if definition['type'] == 'boolean':
                    typed[name] = True
                elif name in schema.get('required', []):
                    typed[name] = definition.get('enum', [grounded_strings.get(
                        name, 'Grounded technical continuation; preserve final QA and independent review.')])[0]
            return {**self.answer, 'answer': typed}
        return dict(self.answer)


def test_resident_executes_own_typed_resolution_and_preserves_other_project():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT, 'Other'), [held(), held('TK-other', 'Other')])
        registry = json.loads(c.state['project_registry'])
        registry['projects']['Other'].pop('butler_recovery_policy')
        c.state['project_registry'] = json.dumps(registry)
        b = Resident(c)
        engine = recovery.TechnicalRecoverySupervisor(b)
        await engine.tick()
        assert [x[0] for x in c.resolutions] == ['TK-held']
        assert c.tickets['TK-other']['status'] == 'needs_human'
        assert len(b.model_calls) == 1
        state = [json.loads(v) for k, v in c.state.items() if k.startswith('technical_recovery_supervision_')][0]
        assert state['last_successful_observation_at']
        assert state['last_progress_at']
        assert 'projection_rows' not in state
    asyncio.run(scenario())


def test_empty_unchanged_human_and_provider_failure_do_not_burn_tokens():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [])
        b = Resident(c)
        now = [1000.0]
        engine = recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0])
        await engine.tick()
        assert b.model_calls == []
        c.tickets['TK-held'] = held()
        b.answer = {'decision': 'escalate', 'category': 'human', 'reason': 'Unknown registered SSO contract.', 'note': 'Keep human hold.'}
        now[0] += 301
        await engine.tick()
        assert len(b.model_calls) == 1 and not c.resolutions
        # Restart must read persistent decision; heartbeats must not change input.
        engine = recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0])
        await engine.tick()
        assert len(b.model_calls) == 1
        c.tickets['TK-held']['human_request']['message'] = 'New scoped evidence'
        b.answer = RuntimeError('provider unavailable')
        now[0] += 301
        await engine.tick()
        assert len(b.model_calls) == 2
        await recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0]).tick()
        assert len(b.model_calls) == 2
    asyncio.run(scenario())


def test_pagination_reaches_late_holds_and_invalid_policy_is_isolated():
    async def scenario():
        registry = registry_for(PROJECT, 'Bad')
        registry['projects']['Bad']['butler_recovery_policy']['page_size'] = 999
        rows = [{'ticket_id': f'TK-{i:03}', 'target_url': PROJECT + '/src', 'status': 'open'} for i in range(120)]
        rows.append(held('TK-late'))
        c, now = MemoryClient(registry, rows), [1000.0]
        b = Resident(c)
        engine = recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0])
        for _ in range(8):
            await engine.tick()
            now[0] += 301
        assert [r[0] for r in c.resolutions] == ['TK-late']
        assert max(start for start, _ in c.calls) >= 100
        assert len(b.model_calls) == 1
        assert engine.status['projects']['Bad']['status'] == 'degraded'
    asyncio.run(scenario())


def test_aggregate_supervision_status_degrades_without_starving_valid_projects():
    async def scenario():
        registry = registry_for('Good', 'Bad')
        registry['projects']['Bad']['butler_recovery_policy']['page_size'] = 999
        c = MemoryClient(registry, [])
        b = Resident(c)
        engine = recovery.TechnicalRecoverySupervisor(b)

        await engine.tick()

        assert engine.status['projects']['Good']['status'] == 'observed'
        assert engine.status['projects']['Bad']['status'] == 'degraded'
        assert engine.status['status'] == 'degraded'
        assert b.model_calls == []

        # A configured but wholly invalid set is not indistinguishable from
        # disabled supervision: no tenant was observed successfully.
        registry = registry_for('BadA', 'BadB')
        for project in registry['projects'].values():
            project['butler_recovery_policy']['page_size'] = 999
        c.state['project_registry'] = json.dumps(registry)
        all_invalid = recovery.TechnicalRecoverySupervisor(b)
        await all_invalid.tick()
        assert all_invalid.status['status'] == 'degraded'
        assert all(all_invalid.status['projects'][name]['status'] == 'degraded'
                   for name in ('BadA', 'BadB'))

    asyncio.run(scenario())


def test_typed_schema_is_strictly_bounded_and_malformed_answers_are_rejected():
    with pytest.raises(recovery.RecoveryPolicyError):
        recovery.validate_typed_schema({'type': 'object', 'properties': {'next_step': {'type': 'object'}},
                                        'required': ['next_step'], 'additionalProperties': False})
    with pytest.raises(recovery.RecoveryPolicyError):
        recovery.validate_typed_schema({'type': 'object', 'properties': {'decision': {'type': 'string',
            'enum': [True]}}, 'required': ['decision'], 'additionalProperties': False})
    with pytest.raises(recovery.RecoveryPolicyError):
        recovery.validate_typed_schema({'type': 'object', 'properties': {'flag': {'type': 'number'}},
                                        'required': ['flag']})
    with pytest.raises(recovery.RecoveryPolicyError):
        recovery.validate_typed_schema({'type': 'object', 'properties': {'nested': {
            'type': 'object', 'properties': {'flag': {'type': 'boolean'}}}}, 'required': ['nested']})
    assert recovery.validate_typed_schema(FE04_SCHEMA) == FE04_SCHEMA
    assert recovery.validate_typed_schema({'type': 'object', 'properties': {
        'flag': {'type': 'boolean', 'enum': [True, False]}}})
    for value in (1, 0, 'true', 'false', None):
        with pytest.raises(recovery.RecoveryPolicyError):
            recovery.validate_typed_answer({
                'authorized_worktree_path': '/verified/worktree',
                'authorized_branch': 'main', 'accepted_base_sha': 'a' * 40,
                'preserve_other_worktree': value,
            }, FE04_SCHEMA)
    for value in (True, False):
        answer = {'authorized_worktree_path': '/verified/worktree',
                  'authorized_branch': 'main', 'accepted_base_sha': 'a' * 40,
                  'preserve_other_worktree': value}
        assert recovery.validate_typed_answer(answer, FE04_SCHEMA) == answer
    boolean_enum = {'type': 'object', 'properties': {
        'enabled': {'type': 'boolean', 'enum': [True]}}, 'required': ['enabled']}
    assert recovery.validate_typed_answer({'enabled': True}, boolean_enum) == {'enabled': True}
    with pytest.raises(recovery.RecoveryPolicyError):
        recovery.validate_typed_answer({'enabled': False}, boolean_enum)
    with pytest.raises(recovery.RecoveryPolicyError):
        recovery.validate_typed_schema({'type': 'object', 'properties': {
            'enabled': {'type': 'boolean', 'enum': [True, 1]}}})
    bounded_strings = {'type': 'object', 'properties': {
        f'field_{i}': {'type': 'string', 'maxLength': 1000} for i in range(4)},
        'required': [f'field_{i}' for i in range(4)]}
    with pytest.raises(recovery.RecoveryPolicyError, match='oversized'):
        recovery.validate_typed_answer({f'field_{i}': 'x' * 1000 for i in range(4)}, bounded_strings)
    with pytest.raises(recovery.RecoveryPolicyError):
        recovery.validate_typed_answer({'decision': 'invented'}, FE03_SCHEMA)
    with pytest.raises(recovery.RecoveryPolicyError):
        recovery.validate_typed_answer({'decision': 'authorize_candidate_publication_and_submission',
                                        'extra': 'not allowed'}, FE03_SCHEMA)


def test_typed_technical_answer_is_cached_after_ambiguous_resolution_without_second_provider_call():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [held(schema=FE03_SCHEMA)])
        b = Resident(c)
        original = c.ticket_human_resolve
        calls = [0]
        async def uncertain(*args, **kwargs):
            calls[0] += 1
            await original(*args, **kwargs)
            raise TimeoutError('response lost after commit')
        c.ticket_human_resolve = uncertain
        await recovery.TechnicalRecoverySupervisor(b).tick()
        assert len(b.model_calls) == 1
        assert c.tickets['TK-held']['human_request']['resolution']['content'] == {
            'decision': 'authorize_candidate_publication_and_submission'}
        # Exact resolved request is terminal; a restarted sweep neither repeats
        # resolution nor invokes the paid provider again.
        await recovery.TechnicalRecoverySupervisor(b).tick()
        assert len(b.model_calls) == 1 and calls == [1]
        record = [json.loads(v) for k, v in c.state.items() if k.startswith('technical_recovery_request_')][0]
        assert record['status'] == 'completed'
        assert record['resolution_content'] == {'decision': 'authorize_candidate_publication_and_submission'}
        assert record['resolved_by_principal_id'] == 'PR-butler'
    asyncio.run(scenario())


def test_fe04_typed_recovery_resolves_through_butler_and_preserves_other_workspace():
    async def scenario():
        requested = held(ticket='TK-FE04', schema=FE04_SCHEMA,
                         message='Resume verified workspace recovery while preserving the unrelated workspace.')
        other = held(ticket='TK-other', project='Other', schema=FE04_SCHEMA,
                     message='This ticket is outside the selected project.')
        c = MemoryClient(registry_for(PROJECT, 'Other'), [requested, other])
        registry = json.loads(c.state['project_registry'])
        registry['projects']['Other'].pop('butler_recovery_policy')
        c.state['project_registry'] = json.dumps(registry)
        b = Resident(c)
        await recovery.TechnicalRecoverySupervisor(b).tick()

        expected = {
            'authorized_worktree_path': '/fixture/worktrees/project/TK-123',
            'authorized_branch': 'project/TK-123',
            'accepted_base_sha': 'a' * 40,
            'preserve_other_worktree': True,
        }
        assert len(b.model_calls) == 1
        assert b.model_calls[0]['ticket']['human_request']['requested_schema'] == FE04_SCHEMA
        assert c.resolutions == [('TK-FE04', {
            'content': expected, 'note': 'Existing owned repair is authorized.'})]
        resolved = c.tickets['TK-FE04']
        assert resolved['status'] == 'open'
        assert resolved['human_request']['resolution']['resolved_by_principal_id'] == 'PR-butler'
        assert c.tickets['TK-other']['status'] == 'needs_human'
    asyncio.run(scenario())


def test_typed_provider_malformed_answer_does_not_mutate():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [held(schema=FE03_SCHEMA)])
        b = Resident(c)
        b._technical_recovery_choose = lambda context: {
            'decision': 'resume', 'category': 'scoped_submission',
            'reason': 'Continue.', 'note': 'Submit for review.',
            'answer': {'decision': 'approve_final_qa'}}
        await recovery.TechnicalRecoverySupervisor(b).tick()
        assert not c.resolutions
        assert c.tickets['TK-held']['status'] == 'needs_human'
    asyncio.run(scenario())


def test_lost_both_resolution_and_receipt_reads_reconciles_durable_typed_content():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [held(schema=FE03_SCHEMA)])
        # Production BoardClient.identity is a dataclass, not a dict; receipt
        # recovery must authenticate its principal without assuming .get().
        c.identity = SimpleNamespace(principal_id='PR-butler', role='coordinator')
        b = Resident(c)
        original = c.ticket_human_resolve
        async def committed_then_lost(*args, **kwargs):
            await original(*args, **kwargs)
            c.ticket_human_resolve = original
            c.ticket_get = lambda *a, **kw: (_ for _ in ()).throw(TimeoutError('temporary receipt read failure'))
            raise TimeoutError('resolve response lost')
        c.ticket_human_resolve = committed_then_lost
        await recovery.TechnicalRecoverySupervisor(b).tick()
        assert len(b.model_calls) == 1
        c.ticket_get = lambda ticket_id, **kw: asyncio.sleep(0, result={'ticket': copy.deepcopy(c.tickets[ticket_id])})
        # Next page includes this resolved record; reconcile exact own receipt,
        # without invoking the provider or attempting a second mutation.
        await recovery.TechnicalRecoverySupervisor(b).tick()
        record = [json.loads(v) for k, v in c.state.items() if k.startswith('technical_recovery_request_')][0]
        assert record['status'] == 'completed'
        assert record['resolution_content'] == {'decision': 'authorize_candidate_publication_and_submission'}
        assert len(b.model_calls) == 1
    asyncio.run(scenario())


def test_supported_fe05_and_fe08_typed_schemas_and_unknown_sso_hold():
    schemas = [
        {'type': 'object', 'properties': {name: {'type': 'string'} for name in (
            'accepted_core_revision', 'support_evidence_or_owner', 'review_action',
            'lint_disposition', 'next_step')}, 'required': ['accepted_core_revision',
            'support_evidence_or_owner', 'review_action', 'lint_disposition', 'next_step'],
         'additionalProperties': False},
        {'type': 'object', 'properties': {'next_step': {'type': 'string'}},
         'required': ['next_step'], 'additionalProperties': False},
    ]
    for schema in schemas:
        assert recovery.validate_typed_schema(schema) == schema
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [held(message='Unknown registered production SSO contract.', schema=FE03_SCHEMA)])
        b = Resident(c)
        b.answer = {'decision': 'escalate', 'category': 'human', 'reason': 'External SSO contract is unknown.', 'note': 'Keep domain decision with the operator.'}
        result = await recovery.TechnicalRecoverySupervisor(b).tick()
        assert len(b.model_calls) == 1 and c.resolutions == []
        assert c.tickets['TK-held']['status'] == 'needs_human'
        assert result['projects'][PROJECT]['actions'][0]['status'] == 'escalated'
    asyncio.run(scenario())


def test_reread_fences_request_changes_and_ambiguous_leases():
    async def scenario():
        t = held()
        t['claim_expires_at'] = '2999-01-01T00:00:00+00:00'
        c, b = MemoryClient(registry_for(PROJECT), [t]), None
        b = Resident(c)
        await recovery.TechnicalRecoverySupervisor(b).tick()
        assert b.model_calls == []
        c.tickets['TK-held']['claim_expires_at'] = None
        async def choose(context):
            c.tickets['TK-held']['human_request']['request_id'] = 'HR-changed'
            return b.answer
        b._technical_recovery_choose = choose
        await recovery.TechnicalRecoverySupervisor(b).tick()
        assert not c.resolutions
    asyncio.run(scenario())


def test_real_central_butler_resolution_reopens_worker_work_and_keeps_domain_hold(tmp_path, monkeypatch):
    import pursers_central.central as central
    jwks = tmp_path / 'jwks.json'
    jwks.write_text('{"keys":[]}')
    for key, value in {'CENTRAL_AUTH_MODE': 'jwt', 'CENTRAL_JWT_ISSUER': 'https://issuer.example',
                       'CENTRAL_JWT_AUDIENCE': 'http://localhost:8765/mcp', 'CENTRAL_JWKS_PATH': str(jwks),
                       'CENTRAL_ADMISSION': 'invite', 'STORE_BACKEND': 'sqlite'}.items():
        monkeypatch.setenv(key, value)
    mcp, service = central.build_server('localhost', 8765, tmp_path / 'central')
    admin = central.Principal('PR-admin', 'admin', frozenset({'board:read', 'board:write', 'board:coordinate'}))
    butler = central.Principal('PR-butler', 'butler', frozenset({'board:read', 'board:write', 'board:coordinate'}))
    worker = central.Principal('PR-worker', 'worker', frozenset({'board:read', 'board:write'}))
    actor = [admin]
    monkeypatch.setattr(central, 'current_principal', lambda: actor[0])
    monkeypatch.setattr(central, 'get_access_token', lambda: SimpleNamespace(token='disposable-test-only'))

    async def call(who, tool, **args):
        actor[0] = who
        result = await mcp.call_tool(tool, {'board_id': BOARD, **args})
        assert not result.is_error, (tool, result.content)
        return result.structured_content

    class Client:
        agent_name = 'fixture-butler'
        identity = {'principal_id': 'PR-butler', 'role': 'coordinator'}
        async def _call(self, tool, args):
            return await call(butler, tool, **args)
        async def board_state_get(self, key):
            result = await call(butler, 'board_state_get', key=key)
            # Real board_state_get returns state=None for an absent key.
            return result
        async def ticket_list(self, **kwargs):
            return await call(butler, 'ticket_list', agent_name=self.agent_name, **kwargs)
        async def ticket_get(self, ticket_id, **kwargs):
            return await call(butler, 'ticket_get', ticket_id=ticket_id, **kwargs)
        async def ticket_human_resolve(self, ticket_id, request_id, action, **kwargs):
            return await call(butler, 'ticket_human_resolve', agent_name=self.agent_name,
                              ticket_id=ticket_id, request_id=request_id, action=action, **kwargs)

    async def scenario():
        await call(admin, 'board_join', agent_name='admin')
        for who in (butler, worker):
            service.mutate(BOARD, lambda doc, who=who: doc['principal_memberships'].update({
                who.principal_id: {'principal_id': who.principal_id, 'role': 'member',
                                   'source': 'test', 'created_at': '2026-10-26T00:00:00+00:00'}}))
        await call(butler, 'board_join', agent_name='fixture-butler', role='coordinator',
                   capabilities={'can_work': False, 'can_review': False})
        await call(worker, 'board_join', agent_name='fixture-worker', role='worker',
                   capabilities={'can_work': True, 'can_review': False, 'tier_max': 3, 'max_parallel': 1})
        registry = registry_for(PROJECT)
        await call(admin, 'board_state_update', agent_name='admin', key='project_registry', value=json.dumps(registry))
        for ticket_id, message, schema in [
            ('TK-repair', 'Repair owned test failure then submit exact evidence.', FE03_SCHEMA),
            ('TK-domain', 'Unknown registered external SSO business contract.', FE03_SCHEMA),
            ('TK-boolean', 'Resume verified workspace recovery and preserve the other workspace.', FE04_SCHEMA),
        ]:
            await call(admin, 'ticket_create', agent_name='admin', ticket_id=ticket_id, title='Assigned repair',
                       target_url=PROJECT + '/src', scope='interactive', description='Repair assigned behavior.',
                       tier=1, assigned_to='fixture-worker')
            await call(worker, 'ticket_claim', agent_name='fixture-worker', ticket_id=ticket_id)
            await call(worker, 'ticket_request_human', agent_name='fixture-worker', ticket_id=ticket_id,
                       kind='decision', message=message, requested_schema=schema)
        # Exercise Central's actual strict-schema rejection before the Butler
        # submits a valid typed recommendation.
        actor[0] = butler
        with pytest.raises(Exception, match='human answer field decision'):
            await mcp.call_tool('ticket_human_resolve', {
                'board_id': BOARD, 'agent_name': 'fixture-butler', 'ticket_id': 'TK-repair',
                'request_id': (await call(butler, 'ticket_get', ticket_id='TK-repair', view='full'))['ticket']['human_request']['request_id'],
                'action': 'accept', 'content': {'decision': 'approve_final_qa'},
                'note': 'This invalid answer must be rejected.'})
        still_held = (await call(butler, 'ticket_get', ticket_id='TK-repair', view='full'))['ticket']
        assert still_held['status'] == 'needs_human' and still_held['human_request']['resolution'] is None

        client = Client()
        actual_resolve = client.ticket_human_resolve
        resolve_calls = []
        async def lose_response_after_commit(*args, **kwargs):
            resolve_calls.append((args, kwargs))
            await actual_resolve(*args, **kwargs)
            raise TimeoutError('disposable Central committed; response lost')
        client.ticket_human_resolve = lose_response_after_commit
        b = Resident(client)
        async def choose(context):
            if context['ticket']['ticket_id'] == 'TK-domain':
                return {'decision': 'escalate', 'category': 'human',
                        'reason': 'Registered SSO contract is unknown.',
                        'note': 'Keep the existing domain hold; do not invent registration facts.'}
            assert context['ticket']['human_request']['requested_schema'] in (FE03_SCHEMA, FE04_SCHEMA)
            return await Resident._technical_recovery_choose(b, context)
        b._technical_recovery_choose = choose
        await recovery.TechnicalRecoverySupervisor(b).tick()
        assert len(resolve_calls) == 2
        repair = (await call(butler, 'ticket_get', ticket_id='TK-repair', view='full'))['ticket']
        domain = (await call(butler, 'ticket_get', ticket_id='TK-domain', view='full'))['ticket']
        boolean_recovery = (await call(butler, 'ticket_get', ticket_id='TK-boolean', view='full'))['ticket']
        assert repair['status'] == 'open'
        assert repair['human_request']['resolution']['resolved_by_principal_id'] == 'PR-butler'
        assert repair['human_request']['resolution']['content'] == {
            'decision': 'authorize_candidate_publication_and_submission'}
        assert repair['human_request']['resolution']['resolved_by_principal_id'] == 'PR-butler'
        assert domain['status'] == 'needs_human'
        assert boolean_recovery['status'] == 'open'
        assert boolean_recovery['human_request']['resolution']['content'] == {
            'authorized_worktree_path': '/fixture/worktrees/project/TK-123',
            'authorized_branch': 'project/TK-123',
            'accepted_base_sha': 'a' * 40,
            'preserve_other_worktree': True,
        }
        assert boolean_recovery['human_request']['resolution']['resolved_by_principal_id'] == 'PR-butler'
        assert len(b.model_calls) == 2  # SSO request never reached the adviser
    asyncio.run(scenario())


def test_actual_backend_provider_binding_uses_configured_transport(monkeypatch):
    import board_butler as butler
    b = butler.CentralBackend(SimpleNamespace(), 'test-token')
    async def config():
        return {}
    b.coordinator_config = config
    runtime = SimpleNamespace(model='fixture-model', credential='fixture-secret', draft_protocol='openai_chat_completions_v1')
    monkeypatch.setattr(butler, 'resolve_config', lambda *a, **kw: SimpleNamespace(kill_switch=False))
    monkeypatch.setattr(butler, 'resolve_provider_runtime', lambda *a: runtime)
    seen = []
    async def post(bound_runtime, body, **kwargs):
        assert bound_runtime is runtime
        seen.append(json.loads(body))
        return {'choices': [{'message': {'content': json.dumps(Resident(None).answer)}}]}
    monkeypatch.setattr(butler, '_post_provider_json', post)
    result = asyncio.run(b._technical_recovery_choose({'project_id': PROJECT, 'ticket': {'ticket_id': 'TK-one'}}))
    assert result['decision'] == 'resume'
    assert len(seen) == 1 and seen[0]['model'] == 'fixture-model'
    assert seen[0]['max_tokens'] == 2048
    assert seen[0]['reasoning_effort'] == 'low'
    contract_prompt = seen[0]['messages'][0]['content']
    assert '"resume"' in contract_prompt and '"escalate"' in contract_prompt
    assert '"continue"' not in contract_prompt


def test_actual_configured_provider_rejects_continue_and_persists_only_safe_metadata(monkeypatch):
    import board_butler as butler
    c = MemoryClient(registry_for(PROJECT), [held()])
    resident = Resident(c)
    actual = butler.CentralBackend(SimpleNamespace(), 'test-token')
    async def config():
        return {}
    actual.coordinator_config = config
    runtime = SimpleNamespace(model='fixture-model', credential='fixture-secret',
                              draft_protocol='openai_chat_completions_v1')
    monkeypatch.setattr(butler, 'resolve_config', lambda *a, **kw: SimpleNamespace(kill_switch=False))
    monkeypatch.setattr(butler, 'resolve_provider_runtime', lambda *a: runtime)
    async def post(bound_runtime, body, **kwargs):
        payload = json.loads(body)
        assert payload['max_tokens'] == 2048
        assert payload['reasoning_effort'] == 'low'
        return {'id': 'resp-safe-1', 'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'decision': 'continue', 'category': 'scoped_repair',
                                  'reason': 'Continue.', 'note': 'Keep scoped.'})}}],
                'usage': {'prompt_tokens': 12, 'completion_tokens': 20,
                          'completion_tokens_details': {'reasoning_tokens': 3}}}
    monkeypatch.setattr(butler, '_post_provider_json', post)
    async def choose(context):
        return await butler.CentralBackend._technical_recovery_choose(actual, context)
    async def contract(context):
        return await butler.CentralBackend._technical_recovery_provider_contract(actual, context)
    resident._technical_recovery_choose = choose
    resident._technical_recovery_provider_contract = contract
    asyncio.run(recovery.TechnicalRecoverySupervisor(resident).tick())
    assert not c.resolutions and c.tickets['TK-held']['status'] == 'needs_human'
    record = [json.loads(v) for k, v in c.state.items() if k.startswith('technical_recovery_request_')][0]
    assert record['status'] == 'provider_failed'
    assert record['provider_diagnostic'] == {
        'response_id': 'resp-safe-1', 'finish_reason': 'stop', 'content_present': True,
        'response_chars': len(json.dumps({'decision': 'continue', 'category': 'scoped_repair',
                                          'reason': 'Continue.', 'note': 'Keep scoped.'})),
        'usage': {'prompt_tokens': 12, 'completion_tokens': 20, 'reasoning_tokens': 3},
        'outcome': 'contract_invalid'}
    assert 'Continue.' not in json.dumps(record) and 'fixture-secret' not in json.dumps(record)


def test_actual_configured_provider_length_reasoning_only_response_fails_closed(monkeypatch):
    import board_butler as butler
    c = MemoryClient(registry_for(PROJECT), [held()])
    resident = Resident(c)
    actual = butler.CentralBackend(SimpleNamespace(), 'test-token')
    async def config():
        return {}
    actual.coordinator_config = config
    runtime = SimpleNamespace(model='fixture-model', credential='fixture-secret',
                              draft_protocol='openai_chat_completions_v1')
    monkeypatch.setattr(butler, 'resolve_config', lambda *a, **kw: SimpleNamespace(kill_switch=False))
    monkeypatch.setattr(butler, 'resolve_provider_runtime', lambda *a: runtime)
    async def post(*args, **kwargs):
        return {'id': 'resp-length-2', 'choices': [{'finish_reason': 'length',
                'message': {'content': ''}}], 'usage': {'completion_tokens': 768,
                'completion_tokens_details': {'reasoning_tokens': 768}}}
    monkeypatch.setattr(butler, '_post_provider_json', post)
    async def choose(context):
        return await butler.CentralBackend._technical_recovery_choose(actual, context)
    async def contract(context):
        return await butler.CentralBackend._technical_recovery_provider_contract(actual, context)
    resident._technical_recovery_choose = choose
    resident._technical_recovery_provider_contract = contract
    asyncio.run(recovery.TechnicalRecoverySupervisor(resident).tick())
    assert not c.resolutions and c.tickets['TK-held']['status'] == 'needs_human'
    record = [json.loads(v) for k, v in c.state.items() if k.startswith('technical_recovery_request_')][0]
    assert record['provider_diagnostic'] == {
        'response_id': 'resp-length-2', 'finish_reason': 'length', 'content_present': False,
        'response_chars': 0, 'usage': {'completion_tokens': 768, 'reasoning_tokens': 768},
        'outcome': 'length'}


def test_actual_configured_provider_prompt_receives_schema_and_typed_answer_contract(monkeypatch):
    import board_butler as butler
    b = butler.CentralBackend(SimpleNamespace(), 'test-token')
    async def config():
        return {}
    b.coordinator_config = config
    runtime = SimpleNamespace(model='fixture-model', credential='fixture-secret', draft_protocol='openai_chat_completions_v1')
    monkeypatch.setattr(butler, 'resolve_config', lambda *a, **kw: SimpleNamespace(kill_switch=False))
    monkeypatch.setattr(butler, 'resolve_provider_runtime', lambda *a: runtime)
    seen = []
    answer = {'decision': 'authorize_candidate_publication_and_submission'}
    async def post(bound_runtime, body, **kwargs):
        payload = json.loads(body)
        seen.append(payload)
        response = {'decision': 'resume', 'category': 'scoped_submission',
                    'reason': 'Scoped checks pass.', 'note': 'Submit exact candidate for independent review.',
                    'answer': answer}
        return {'choices': [{'message': {'content': json.dumps(response)}}]}
    monkeypatch.setattr(butler, '_post_provider_json', post)
    context = {'project_id': PROJECT, 'board_id': BOARD, 'ticket': {
        'ticket_id': 'TK-typed', 'human_request': {'request_id': 'HR-typed', 'requested_schema': FE03_SCHEMA}}}
    result = asyncio.run(b._technical_recovery_choose(context))
    assert result['answer'] == answer
    assert len(seen) == 1
    assert 'requested_schema' in seen[0]['messages'][1]['content']
    assert 'requested_schema' in seen[0]['messages'][0]['content']
    assert 'schema' in seen[0]['messages'][0]['content'].casefold()


def test_resident_timer_progresses_without_board_event_and_stops_cleanly():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [])
        b = Resident(c)
        engine = recovery.TechnicalRecoverySupervisor(b)
        engine.next_delay = lambda: 0.001
        calls = []
        original = engine.tick
        async def tick():
            engine.next_due.clear()
            calls.append(await original())
        engine.tick = tick
        task = asyncio.create_task(engine.run())
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) >= 2
        assert b.model_calls == []
    asyncio.run(scenario())


def test_new_generic_human_request_ids_do_not_reset_no_progress_budget():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [held()])
        b, now = Resident(c), [1000.0]
        engine = recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0])
        for i in range(5):
            c.tickets['TK-held']['status'] = 'needs_human'
            c.tickets['TK-held']['human_request'] = held(request=f'HR-{i}')['human_request']
            await engine.tick()
            now[0] += 301
        assert len(c.resolutions) == 3 and len(b.model_calls) == 3
        assert engine.status['projects'][PROJECT]['actions'][0]['status'] == 'no_progress_budget_exhausted'
        c.tickets['TK-held']['annotations'] = [{'kind': 'evidence', 'text': 'New exact-source scoped test results.'}]
        await recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0]).tick()
        assert len(c.resolutions) == 4
    asyncio.run(scenario())


def test_provider_model_change_invalidates_failed_attempt_but_restart_does_not():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [held()])
        b, now, models = Resident(c), [1000.0], ['fixture-model-a']
        b._technical_recovery_provider_contract = lambda context: asyncio.sleep(0, result={
            'contract': recovery.RECOVERY_DECISION_CONTRACT_VERSION,
            'protocol': 'openai_chat_completions_v1', 'model': models[0]})
        b.answer = RuntimeError('safe fake provider failure')
        engine = recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0])
        await engine.tick()
        assert len(b.model_calls) == 1
        models[0] = 'fixture-model-b'
        b.answer = {'decision': 'escalate', 'category': 'human',
                    'reason': 'The domain contract remains unknown.', 'note': 'Keep the human hold.'}
        now[0] += 301
        await engine.tick()
        assert len(b.model_calls) == 2
        await recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0]).tick()
        assert len(b.model_calls) == 2
    asyncio.run(scenario())


def test_repeated_provider_model_changes_do_not_reset_no_progress_budget():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [held()])
        b, now, models = Resident(c), [1000.0], ['fixture-model-0']
        b._technical_recovery_provider_contract = lambda context: asyncio.sleep(0, result={
            'contract': recovery.RECOVERY_DECISION_CONTRACT_VERSION,
            'protocol': 'openai_chat_completions_v1', 'model': models[0]})
        b.answer = RuntimeError('safe fake provider failure')
        engine = recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0])
        for i in range(5):
            models[0] = f'fixture-model-{i}'
            c.tickets['TK-held']['status'] = 'needs_human'
            c.tickets['TK-held']['human_request'] = held(request=f'HR-model-{i}')['human_request']
            await engine.tick()
            now[0] += 301
        assert len(b.model_calls) == 3
        assert engine.status['projects'][PROJECT]['actions'][0]['status'] == 'no_progress_budget_exhausted'
        await recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0]).tick()
        assert len(b.model_calls) == 3
    asyncio.run(scenario())


def test_timed_out_provider_calls_resume_after_last_attempt_without_starving_later_holds():
    async def scenario():
        registry = registry_for(PROJECT)
        registry['projects'][PROJECT]['butler_recovery_policy']['project_timeout_s'] = 1
        rows = [held(f'TK-slow-{i}') for i in range(1, 5)]
        c, now = MemoryClient(registry, rows), [1000.0]
        b = Resident(c)
        attempted = []
        async def slow_choose(context):
            ticket_id = context['ticket']['ticket_id']
            attempted.append(ticket_id)
            await asyncio.sleep(0.55)
            return {'decision': 'escalate', 'category': 'human',
                    'reason': 'Keep the exact human decision.', 'note': 'No autonomous action.'}
        b._technical_recovery_choose = slow_choose
        engine = recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0])
        await engine.tick()
        now[0] += 301
        await engine.tick()
        assert attempted == ['TK-slow-1', 'TK-slow-2', 'TK-slow-3', 'TK-slow-4']
        assert len(set(attempted)) == len(attempted)
        assert not c.resolutions
    asyncio.run(scenario())


def test_shared_durable_reservation_prevents_two_paid_decisions():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [held()])
        b = Resident(c)
        original = b._technical_recovery_choose
        async def choose(context):
            await asyncio.sleep(0.01)
            return await original(context)
        b._technical_recovery_choose = choose
        first, second = recovery.TechnicalRecoverySupervisor(b), recovery.TechnicalRecoverySupervisor(b)
        await asyncio.gather(first.tick(), second.tick())
        assert len(b.model_calls) == 1 and len(c.resolutions) == 1
    asyncio.run(scenario())


def test_unknown_mutation_outcome_does_not_pay_again_or_duplicate_resolution():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [held()])
        b, now = Resident(c), [1000.0]
        original = c.ticket_human_resolve
        async def resolve(*args, **kwargs):
            await original(*args, **kwargs)
            raise ConnectionError('response lost after commit')
        c.ticket_human_resolve = resolve
        await recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0]).tick()
        now[0] += 301
        await recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0]).tick()
        assert len(b.model_calls) == 1 and len(c.resolutions) == 1
    asyncio.run(scenario())


def test_fair_round_robin_and_slow_project_do_not_block_other_projects():
    async def scenario():
        names = ['A', 'B', 'C', 'D', 'E', 'F', 'G']
        registry = registry_for(*names)
        for row in registry['projects'].values():
            row['butler_recovery_policy']['max_projects_per_sweep'] = 2
            row['butler_recovery_policy']['project_timeout_s'] = 1
        c, now = MemoryClient(registry, []), [1000.0]
        b = Resident(c)
        engine = recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0])
        original = engine._observe
        async def observe(registry, policy):
            if policy.project_id == 'A':
                # Hold one selected tenant while the other publishes first.
                await asyncio.sleep(0.02)
                assert 'B' in engine.status['projects']
            await original(registry, policy)
        engine._observe = observe
        for _ in range(4):
            await engine.tick()
            now[0] += 0.1
        assert set(engine.status['projects']) == set(names)
        assert b.model_calls == []
    asyncio.run(scenario())


def test_run_services_omitted_project_before_interval_and_preserves_backoff():
    async def scenario():
        names = ['A', 'B']
        registry = registry_for(*names)
        for name, row in registry['projects'].items():
            policy = row['butler_recovery_policy']
            policy['max_projects_per_sweep'] = 1
            policy['retry_backoff_s'] = 900
        client, now = MemoryClient(registry, []), [1000.0]
        original_list = client.ticket_list
        list_calls = 0

        async def flaky_list(**kwargs):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 1:
                raise ConnectionError('disposable first-project read failure')
            return await original_list(**kwargs)

        client.ticket_list = flaky_list
        engine = recovery.TechnicalRecoverySupervisor(Resident(client), clock=lambda: now[0])
        observed = []
        second_observed = asyncio.Event()
        original_observe = engine._observe

        async def observe(registry, policy):
            observed.append(policy.project_id)
            await original_observe(registry, policy)
            if policy.project_id == 'B':
                second_observed.set()

        engine._observe = observe
        started = time.monotonic()
        task = asyncio.create_task(engine.run())
        try:
            await asyncio.wait_for(second_observed.wait(), timeout=1.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        elapsed = time.monotonic() - started
        assert observed == ['A', 'B']
        assert elapsed < POLICY['sweep_interval_s']
        # A's failed read retains its configured retry delay; B's successful
        # observation retains its sweep interval. Neither is hot-retried.
        assert engine.next_due == {'A': 1900.0, 'B': 1300.0}
        assert engine.next_delay() == 60.0

    asyncio.run(scenario())


def test_actual_backend_context_starts_and_cancels_periodic_supervisor(monkeypatch):
    import board_butler as butler
    import pursers_client
    lifecycle = []
    class Client:
        identity = {'role': 'coordinator'}
        async def __aenter__(self):
            lifecycle.append('join')
            return self
        async def __aexit__(self, *args):
            lifecycle.append('close')
        async def board_snapshot(self, **kwargs):
            return {'agents': [], 'latest_seq': 5}
    monkeypatch.setattr(pursers_client, 'BoardClient', lambda *args, **kwargs: Client())
    monkeypatch.setattr(butler, 'assert_complete_agent_view', lambda *args: None)
    monkeypatch.setattr(butler, 'assert_independent_identity', lambda *args: None)
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(butler, 'backfill_retrospective_evaluations', noop)
    async def scenario():
        args = SimpleNamespace(url='http://fixture.invalid/mcp', home_board=BOARD, agent_name='fixture-butler',
                               runtime_mode='active', dry_run=False, kill_switch=False, veto_question=False)
        b = butler.CentralBackend(args, 'disposable')
        b._project_name_from_registry = noop
        async def supervise():
            lifecycle.append('supervise')
            try:
                await asyncio.Event().wait()
            finally:
                lifecycle.append('cancel')
        b._technical_supervisor.run = supervise
        async with b:
            await asyncio.sleep(0)
            assert b._technical_supervision_task is not None
            assert 'supervise' in lifecycle
        assert lifecycle.index('cancel') < lifecycle.index('close')
    asyncio.run(scenario())


def test_kill_switch_prevents_paid_decision_and_rereads_before_resolution():
    async def scenario():
        c = MemoryClient(registry_for(PROJECT), [held()])
        b = Resident(c)
        permitted = [False]
        async def authorized(context):
            return permitted[0]
        b._technical_recovery_authorized = authorized
        await recovery.TechnicalRecoverySupervisor(b).tick()
        assert not b.model_calls
        permitted[0] = True
        original = b._technical_recovery_choose
        async def choose(context):
            result = await original(context)
            permitted[0] = False
            return result
        b._technical_recovery_choose = choose
        await recovery.TechnicalRecoverySupervisor(b).tick()
        assert len(b.model_calls) == 1 and not c.resolutions
        permitted[0] = True
        await recovery.TechnicalRecoverySupervisor(b).tick()
        assert len(b.model_calls) == 1 and len(c.resolutions) == 1
    asyncio.run(scenario())


def test_invalidated_cursor_restarts_bounded_traversal_instead_of_sticking():
    async def scenario():
        rows = [{'ticket_id': f'TK-{i}', 'target_url': PROJECT + '/src', 'status': 'open'} for i in range(60)]
        c, now = MemoryClient(registry_for(PROJECT), rows), [1000.0]
        b = Resident(c)
        engine = recovery.TechnicalRecoverySupervisor(b, clock=lambda: now[0])
        await engine.tick()
        original = c.ticket_list
        async def invalidated(**kwargs):
            if kwargs.get('cursor'):
                raise ValueError('cursor invalidated by concurrent board update')
            return await original(**kwargs)
        c.ticket_list = invalidated
        now[0] += 301
        await engine.tick()
        states = [json.loads(v) for k, v in c.state.items() if k.startswith('technical_recovery_supervision_')]
        assert states[0]['status'] == 'degraded'
        assert states[0]['page_cursor'] is None
        c.ticket_list = original
        for _ in range(3):
            now[0] += 301
            await engine.tick()
        assert engine.status['projects'][PROJECT]['traversal_complete']
    asyncio.run(scenario())


@pytest.mark.parametrize('schema', [FE03_SCHEMA, {'type': 'object', 'properties': {'next_step': {'type': 'string'}}},
    {'type': 'object', 'properties': {k: {'type': 'string'} for k in (
        'accepted_core_revision', 'support_evidence_or_owner', 'review_action', 'lint_disposition', 'next_step')},
     'required': ['accepted_core_revision', 'support_evidence_or_owner', 'review_action', 'lint_disposition', 'next_step'],
     'additionalProperties': False}])
def test_real_technical_context_shapes_allow_preserved_security_constraints(schema):
    ticket = held(schema=schema, message='Publish scoped candidate after owned fixes; disclose unchanged baseline failures.')
    ticket['title'] = 'Migrate shared SSO shell and preserve authorization behavior'
    ticket['description'] = 'Do not change security contracts or customer credentials. Repair only owned behavior.'
    policy = recovery.parse_recovery_policy(POLICY, project_id=PROJECT, board_id=BOARD, active=True)
    assert recovery._request_context(ticket, policy)['ticket']['human_request']['requested_schema'] == schema


def test_schema_transport_supports_generic_field_names_and_optional_properties():
    schema = {'type': 'object', 'properties': {'continuation': {'type': 'string', 'enum': ['repair', 'defer']}}}
    assert recovery.validate_typed_schema(schema) == schema
    assert recovery.validate_typed_answer({'continuation': 'repair'}, schema) == {'continuation': 'repair'}
    with pytest.raises(recovery.RecoveryPolicyError):
        recovery.validate_typed_answer({}, schema)
    with pytest.raises(recovery.RecoveryPolicyError):
        recovery.validate_typed_answer({'extra': 'not declared'}, schema)


def test_explicit_approval_kind_is_preserved_without_a_paid_decision():
    async def scenario():
        t = held(schema=FE03_SCHEMA)
        t['human_request']['kind'] = 'approval'
        c = MemoryClient(registry_for(PROJECT), [t])
        b = Resident(c)
        await recovery.TechnicalRecoverySupervisor(b).tick()
        assert not c.resolutions and not b.model_calls
    asyncio.run(scenario())
