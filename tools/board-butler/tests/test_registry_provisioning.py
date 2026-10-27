from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / 'registry_provisioning.py'


def api():
    spec = importlib.util.spec_from_file_location('registry_provisioning_test', PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path):
    work = tmp_path / 'clones' / 'frontend'
    work.mkdir(parents=True)
    request_dir = tmp_path / 'state' / 'board-provisioning' / 'requests'
    request_dir.mkdir(parents=True)
    request = {'schema_version': 1, 'project': 'frontend', 'board_id': 'frontend-board'}
    request_path = request_dir / 'frontend.json'
    request_path.write_text(json.dumps(request))
    request_path.chmod(0o600)
    registry = {'schema_version': 1, 'projects': {'frontend': {
        'board_id': 'frontend-board', 'status': 'active', 'fleet': True,
        'domain': 'work', 'work_dir_owner': 'fleet',
        'work_dir': str(work), 'fleet_clone_dir': str(work)}}}
    return request_dir, registry


class FakeClient:
    def __init__(self):
        self.values = {'seat_registry': {'schema_version': 1, 'seats': {
            'worker': {'board_mode': 'registry', 'principal_id': 'PR-worker', 'role': 'worker'},
            'reviewer': {'board_mode': 'registry', 'principal_id': 'PR-reviewer', 'role': 'reviewer'}}}}

    async def board_state_get(self, key):
        if key not in self.values:
            raise RuntimeError('state key not found')
        return {'state': {'value': json.dumps(self.values[key])}}

    async def board_snapshot(self, **kwargs):
        return {'agents': [
            {'agent_name': 'vm-coordinator', 'principal_id': 'PR-coordinator',
             'role': 'coordinator', 'lifecycle_status': 'active',
             'capabilities': {'can_work': False, 'can_review': False}},
            {'agent_name': 'retired', 'principal_id': 'PR-retired',
             'role': 'worker', 'lifecycle_status': 'retired'}], 'omitted_counts': {}}


class FakeAdapter:
    def __init__(self, registry):
        self.registry = registry
        self.operations = []

    async def snapshot(self):
        return self.registry, 'a' * 64

    async def ensure_board(self, board, domain, tier=None):
        self.operations.append(('board', board))

    async def ensure_members(self, board, roles):
        self.operations.append(('members', board, roles))


class FakeBackend:
    def __init__(self, registry):
        self.client = FakeClient()
        self.adapter = FakeAdapter(registry)
        self._project_onboarding_policies = {
            'configured': SimpleNamespace(member_roles={'PR-extra-reviewer': 'reviewer'})}
        self.bindings = []

    async def bind_project_coordinator(self, board, project, principal, name):
        self.bindings.append((board, project, principal, name))


def test_rejects_invalid_registered_request_before_bootstrap(tmp_path, mutation=None):
    if mutation is None:
        for case in ('paused', 'wrong_board', 'operator_root', 'outside_root'):
            case_path = tmp_path / case
            case_path.mkdir()
            test_rejects_invalid_registered_request_before_bootstrap(case_path, case)
        return
    module = api()
    directory, registry = fixture(tmp_path)
    entry = registry['projects']['frontend']
    if mutation == 'paused': entry['status'] = 'paused'
    elif mutation == 'wrong_board': entry['board_id'] = 'other'
    elif mutation == 'operator_root': entry['work_dir_owner'] = 'operator'
    else: entry['work_dir'] = entry['fleet_clone_dir'] = '/outside/clones'
    backend = FakeBackend(registry)
    result = asyncio.run(module.process_requests(backend, directory.parent, tmp_path / 'clones'))
    assert result[0]['status'] == 'failed'
    assert not backend.adapter.operations


    assert not backend.bindings


def test_provisions_all_registry_roles_and_binds_only_verified_coordinator(tmp_path):
    module = api()
    directory, registry = fixture(tmp_path)
    backend = FakeBackend(registry)
    result = asyncio.run(module.process_requests(backend, directory.parent, tmp_path / 'clones'))
    assert result[0]['status'] == 'succeeded', result
    roles = backend.adapter.operations[1][2]
    assert roles == {'PR-worker': 'member', 'PR-reviewer': 'reviewer',
                     'PR-extra-reviewer': 'reviewer', 'PR-coordinator': 'member'}
    assert backend.bindings == [('frontend-board', 'frontend', 'PR-coordinator', 'vm-coordinator')]
    count = len(backend.adapter.operations)
    assert asyncio.run(module.process_requests(backend, directory.parent, tmp_path / 'clones')) == []
    assert len(backend.adapter.operations) == count


def test_role_conflict_and_symlink_requests_fail_closed(tmp_path):
    module = api()
    directory, registry = fixture(tmp_path)
    backend = FakeBackend(registry)
    backend._project_onboarding_policies['configured'].member_roles['PR-reviewer'] = 'member'
    result = asyncio.run(module.process_requests(backend, directory.parent, tmp_path / 'clones'))
    assert result[0]['status'] == 'failed'
    assert not backend.adapter.operations


    (directory / 'frontend.json').unlink()
    target = tmp_path / 'target.json'
    target.write_text('{}')
    (directory / 'symlink.json').symlink_to(target)
    result = asyncio.run(module.process_requests(backend, directory.parent, tmp_path / 'clones'))
    assert result[0]['request_file'] == 'symlink.json'
    assert result[0]['status'] == 'failed'


def test_duplicate_coordinator_names_and_incomplete_roster_rejected(tmp_path):
    module = api()
    directory, registry = fixture(tmp_path)
    backend = FakeBackend(registry)
    async def snapshot(**kwargs):
        return {'agents': [], 'omitted_counts': {'agents': 1}}
    backend.client.board_snapshot = snapshot
    result = asyncio.run(module.process_requests(backend, directory.parent, tmp_path / 'clones'))
    assert result[0]['status'] == 'failed'
    assert not backend.adapter.operations
