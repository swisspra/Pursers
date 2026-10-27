"""Registry-first provisioning requests executed only by the resident Butler.

The coordinator-side wrapper holds no service credentials. The resident uses its
existing onboarding authority and refuses unregistered routes or role conflicts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}')


def private_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o022
                or info.st_size > 65536):
            raise ValueError('provisioning file is not private regular bounded data')
        raw = os.read(fd, 65537)
    finally:
        os.close(fd)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('provisioning document must be an object')
    return value


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.provision-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_request(request: dict, registry: dict, root: Path) -> dict:
    if set(request) != {'schema_version', 'project', 'board_id'} or request['schema_version'] != 1:
        raise ValueError('unsupported provisioning request')
    for key in ('project', 'board_id'):
        if not isinstance(request[key], str) or SAFE_ID.fullmatch(request[key]) is None:
            raise ValueError('invalid provisioning identifier')
    entry = registry.get('projects', {}).get(request['project'])
    if (not isinstance(entry, dict) or entry.get('board_id') != request['board_id']
            or entry.get('status') != 'active' or entry.get('fleet') is not True
            or entry.get('domain') != 'work' or entry.get('work_dir_owner') != 'fleet'):
        raise ValueError('request must match an active registered fleet-owned work project')
    work = Path(entry['work_dir']).resolve(strict=True)
    clone = Path(entry['fleet_clone_dir']).resolve(strict=True)
    if work != clone or work == root.resolve() or not work.is_relative_to(root.resolve()):
        raise ValueError('registered clone is outside the approved fleet root')
    return entry


async def registry_roles(backend) -> tuple[dict[str, str], dict]:
    roles: dict[str, str] = {}

    def add(principal, role):
        if not isinstance(principal, str) or not principal.startswith('PR-'):
            raise ValueError('invalid registry principal')
        if role not in {'member', 'reviewer'}:
            raise ValueError('unsupported registry membership role')
        if principal in roles and roles[principal] != role:
            raise ValueError('registry principal role conflict')
        roles[principal] = role

    raw = await backend.client.board_state_get('seat_registry')
    seats = json.loads(raw['state']['value'])
    if seats.get('schema_version') != 1 or not isinstance(seats.get('seats'), dict):
        raise ValueError('seat registry is malformed')
    for seat in seats['seats'].values():
        if seat.get('board_mode') != 'registry':
            continue
        if seat.get('role') not in {'worker', 'reviewer'}:
            raise ValueError('invalid registry seat role')
        add(seat['principal_id'], 'reviewer' if seat['role'] == 'reviewer' else 'member')
    for policy in backend._project_onboarding_policies.values():
        for principal, role in policy.member_roles.items():
            add(principal, role)
    roster = await backend.client.board_snapshot(limit=1000, max_bytes=750000)
    if roster.get('omitted_counts', {}).get('agents', 0):
        raise ValueError('home identity roster is incomplete')
    coordinators = []
    for actor in roster.get('agents', []):
        if actor.get('lifecycle_status') != 'active':
            continue
        if actor.get('agent_name') == 'vm-coordinator':
            caps = actor.get('capabilities', {})
            if (actor.get('role') != 'coordinator' or caps.get('can_work') is not False
                    or caps.get('can_review') is not False):
                raise ValueError('coordinator identity must remain nonworking and nonreviewing')
            coordinators.append(actor)
        if actor.get('role') in {'worker', 'reviewer'}:
            caps = actor.get('capabilities', {})
            if caps.get('can_work') is True or caps.get('can_review') is True:
                add(actor['principal_id'], 'reviewer' if actor['role'] == 'reviewer' else 'member')
    if len(coordinators) != 1:
        raise ValueError('exactly one active home coordinator is required')
    coordinator = coordinators[0]
    add(coordinator['principal_id'], 'member')
    return roles, coordinator


async def bind_coordinator(backend, board: str, project: str, principal: str, name: str) -> None:
    """Reserve the admitted coordinator's exact seat ID without impersonating them."""
    logical = json.dumps([board, principal, name], separators=(',', ':'))
    expected_owner = 'AI-' + hashlib.sha256(logical.encode()).hexdigest()
    async with backend._client_for_board(board) as client:
        members = await client._call('board_members', {})
        member = next((row for row in members.get('members', [])
                       if row.get('principal_id') == principal), None)
        if member is None or member.get('role') not in {'member', 'admin'}:
            raise ValueError('coordinator principal is not admitted on the target board')
        try:
            raw = await client.board_state_get('project_coordinators')
        except Exception as exc:
            if 'state key not found' not in str(exc).casefold():
                raise
            previous = None
            bindings = {}
        else:
            previous = raw['state']['value']
            bindings = json.loads(previous)
            if not isinstance(bindings, dict):
                raise ValueError('project coordinator binding is malformed')
        existing = bindings.get(project)
        if existing is not None and existing != [expected_owner]:
            raise ValueError('project coordinator binding conflicts with existing ownership')
        bindings[project] = [expected_owner]
        encoded = json.dumps(bindings, sort_keys=True, separators=(',', ':'))
        if encoded != previous:
            arguments = {'agent_name': client.agent_name,
                         'key': 'project_coordinators', 'value': encoded}
            if previous is None:
                arguments['expected_absent'] = True
            else:
                arguments['expected_sha256'] = hashlib.sha256(previous.encode()).hexdigest()
            await client._call('board_state_update', arguments)
        raw = await client.board_state_get('project_coordinators')
        if json.loads(raw['state']['value']).get(project) != [expected_owner]:
            raise RuntimeError('project coordinator binding read-back mismatch')


async def process_requests(backend, directory: Path, clone_root: Path) -> list[dict]:
    requests = directory / 'requests'
    if not requests.exists():
        return []
    results = []
    adapter = getattr(backend, 'adapter', None)
    if adapter is None:
        adapter = backend._project_registry_adapter
        if adapter is None:
            raise RuntimeError('resident registry adapter is not initialized')
    pending = [path for path in sorted(requests.glob('*.json'))
               if not (directory / 'results' / path.name).exists()]
    for path in pending[:20]:
        receipt = directory / 'results' / path.name
        result = {'schema_version': 1, 'request_file': path.name, 'status': 'failed'}
        try:
            request = private_json(path)
            registry, digest = await adapter.snapshot()
            entry = validate_request(request, registry, clone_root)
            roles, coordinator = await registry_roles(backend)
            # Recheck the registry after roster discovery, before creator admission.
            registry_again, digest_again = await adapter.snapshot()
            if digest_again != digest:
                raise ValueError('project registry changed before provisioning')
            validate_request(request, registry_again, clone_root)
            await adapter.ensure_board(request['board_id'], entry['domain'], 2)
            await adapter.ensure_members(request['board_id'], roles)
            binder = getattr(backend, 'bind_project_coordinator', None)
            if binder is None:
                await bind_coordinator(backend, request['board_id'], request['project'],
                                       coordinator['principal_id'], coordinator['agent_name'])
            else:
                await binder(request['board_id'], request['project'],
                             coordinator['principal_id'], coordinator['agent_name'])
            result.update(status='succeeded', project=request['project'],
                          board_id=request['board_id'], principals_provisioned=len(roles),
                          coordinator_principal_id=coordinator['principal_id'])
        except Exception as exc:
            # Never serialize transport errors that could include credential material.
            result['error_class'] = type(exc).__name__
            if isinstance(exc, ValueError):
                result['reason'] = str(exc)[:240]
            else:
                result['reason'] = 'provisioning failed; inspect resident service diagnostics'
        save(receipt, result)
        results.append(result)
    return results
