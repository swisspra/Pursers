#!/usr/bin/env python3
"""Run bounded agent work only after relevant registry events; persist cursors.

The persistent process only waits on Central.  It starts Goose or Codex CLI for
an authorized event, so an idle fleet performs no paid model or provider call.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import stat
import subprocess
import sys
import time
import threading
import uuid

import anyio
import httpcore2
import httpx2

HELPERS = runpy.run_path(str(Path(__file__).resolve().parents[1]/'ado-connector/git_credential.py'))
DIAGNOSTICS = runpy.run_path(str(Path(__file__).with_name('event_diagnostics.py')))
PUBLISH = runpy.run_path(str(Path(__file__).resolve().parents[1]/'board-butler/fleet_observation.py'))['publish']
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$')
PRESENCE_INTERVAL_S = 120
PROCESS_STOP_GRACE_S = 5
TRANSPORT_RECOVERY_ATTEMPTS = 4
TRANSPORT_RECOVERY_BASE_DELAY_S = 5


class TransportRecoveryExhausted(RuntimeError):
    """A bounded transport recovery window ended without reconnecting."""


def validate_config(config):
    required = {'seat_id','role','provider','model','seat_dir','board_script','state_file','token_file',
                'central_url','home_board','repository_root'}
    if not required <= config.keys() or config['role'] not in ('worker','reviewer'):
        raise ValueError('event seat configuration is incomplete')
    client = config.get('client', 'goose')
    if client not in ('goose', 'codex'):
        raise ValueError('unsupported event seat client')
    if client == 'goose' and not {'goose', 'mcp'} <= config.keys():
        raise ValueError('Goose event seat configuration is incomplete')
    if client == 'codex' and 'codex' not in config:
        raise ValueError('Codex event seat configuration is incomplete')
    if not SAFE_ID.fullmatch(config['seat_id']) or not SAFE_ID.fullmatch(config['home_board']):
        raise ValueError('invalid seat or board identity')
    path_keys = {'seat_dir','board_script','state_file','token_file','repository_root'}
    path_keys.update(('goose', 'mcp') if client == 'goose' else ('codex',))
    if config.get('last_message_file') is not None:
        path_keys.add('last_message_file')
    if config.get('drain_file') is not None:
        path_keys.add('drain_file')
    for key in path_keys:
        if not Path(config[key]).is_absolute(): raise ValueError('runtime paths must be absolute')
    profile = config.get('codex_profile')
    if profile is not None and (
        client != 'codex' or not isinstance(profile, str)
        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', profile)
    ):
        raise ValueError('invalid Codex profile')
    if config.get('codex_sandbox', 'workspace-write') not in (
        'workspace-write', 'danger-full-access'
    ):
        raise ValueError('invalid Codex sandbox')
    effort = config.get('effort', 'high')
    if not isinstance(effort, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', effort):
        raise ValueError('invalid model effort')
    service_tier = config.get('service_tier')
    if service_tier is not None and (
        client != 'codex' or not isinstance(service_tier, str)
        or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', service_tier)
    ):
        raise ValueError('invalid Codex service tier')
    hourly_limit=config.get('max_runs_per_hour')
    if hourly_limit is not None and (type(hourly_limit) is not int or not 1 <= hourly_limit <= 100):
        raise ValueError('invalid hourly run limit')
    for key, default, ceiling in (('max_turns',30,100),('turn_timeout_s',1800,3600)):
        value=config.get(key,default)
        if type(value) is not int or not 1 <= value <= ceiling: raise ValueError('invalid turn limit')
    tier = config.get('tier_max',2)
    if type(tier) is not int or tier not in (1,2,3): raise ValueError('invalid seat tier')
    skills=config.get('skills',[])
    if (not isinstance(skills,list) or any(not isinstance(skill,str) or not SAFE_ID.fullmatch(skill)
                                           for skill in skills)):
        raise ValueError('invalid seat skills')
    config['skills']=sorted(set(skills))
    recoveries = config.get('max_owned_recoveries', 1)
    if type(recoveries) is not int or not 0 <= recoveries <= 10:
        raise ValueError('invalid owned recovery limit')
    return config


class EventContextError(ValueError):
    """A typed, fail-closed routing or preserved-workspace problem."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _git(repository, *arguments):
    environment = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_PAGER': 'cat',
                   'GIT_TERMINAL_PROMPT': '0'}
    return subprocess.run(['git', '-C', str(repository), *arguments], env=environment,
                          check=True, capture_output=True, text=True).stdout.strip()


def _verified_final_qa_branch_tip(clone, branch, combined_revision, listings, workspace):
    """Return an unused ticket ref's safe tip, or fail closed without changing it."""
    ref = f'refs/heads/{branch}'
    exists = subprocess.run(
        ['git', '-C', str(clone), 'show-ref', '--verify', '--quiet', ref],
        env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'},
        check=False, capture_output=True, timeout=5).returncode
    if exists == 1:
        return None
    if exists != 0:
        raise EventContextError('workspace_candidate_mismatch',
                                'final-QA ticket branch ref could not be verified')
    for block in listings.split('\n\n'):
        fields = block.splitlines()
        path_line = next((line for line in fields if line.startswith('worktree ')), None)
        branch_line = next((line for line in fields if line.startswith('branch ')), None)
        if branch_line is None or branch_line.removeprefix('branch ') != ref:
            continue
        attached = Path(path_line.removeprefix('worktree ')) if path_line else None
        if (workspace is None or attached is None
                or attached.absolute() != workspace.absolute()):
            raise EventContextError('workspace_branch_mismatch',
                                    'final-QA ticket branch is already attached to another worktree')
    try:
        tip = _git(clone, 'rev-parse', '--verify', f'{ref}^{{commit}}')
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EventContextError('workspace_candidate_mismatch',
                                'existing final-QA ticket branch has no verifiable commit') from exc
    if tip != combined_revision:
        ancestor = subprocess.run(
            ['git', '-C', str(clone), 'merge-base', '--is-ancestor', tip, combined_revision],
            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'},
            check=False, capture_output=True, timeout=5)
        if ancestor.returncode != 0:
            raise EventContextError('workspace_candidate_mismatch',
                                    'existing final-QA ticket branch is not an ancestor of the exact combined revision')
    return tip


def _canonical_git_url(value):
    from urllib.parse import urlsplit, urlunsplit
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
        return None
    path = parsed.path.rstrip('/')
    if path.endswith('.git'):
        path = path[:-4]
    return urlunsplit(('https', parsed.netloc.casefold(), path, '', ''))


def validate_registry_roots(registry, repository_root):
    """Reject an unusable relay boundary before invoking any model."""
    root = Path(repository_root).resolve()
    for project in registry['projects'].values():
        if project['status'] != 'active' or not project.get('fleet', True):
            continue
        work = Path(project.get('fleet_clone_dir') or project['work_dir']).resolve()
        if not work.is_relative_to(root):
            raise ValueError('repository_root excludes an active registry project clone')


def _has_source_submission_metadata(ticket):
    """Recognize claimed source metadata without depending on tags or schema."""
    submissions = ticket.get('submission_history')
    candidates = [ticket]
    if isinstance(submissions, list):
        candidates.extend(item for item in submissions if isinstance(item, dict))
    for item in candidates:
        if any(key in item and item[key] is not None for key in ('branch', 'commit_hash')):
            return True
        proof = item.get('submission_preflight')
        if isinstance(proof, dict) and any(key in proof and proof[key] is not None
                                           for key in ('branch', 'commit')):
            return True
    return False


def resolve_event_context(registry, board_id, ticket, repository_root, seat_id, role='worker',
                          create_if_missing=True):
    """Resolve one exact board/project and its preserved canonical ticket worktree."""
    from pursers_client.project_registry import resolve_registry_target
    # Scope the extra Git protections to the new read-only caller. Existing
    # worker provisioning keeps its original command interface and behavior.
    def git(repository, *arguments):
        options = () if create_if_missing else (
            '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false')
        return _git(repository, *options, *arguments)

    target = ticket.get('target_url')
    required = ticket.get('required_fields') or []
    is_code = ('branch_and_commit' in required or 'commit_hash' in required
               or 'base_and_head_sha' in required or _has_source_submission_metadata(ticket)
               or any(isinstance(tag, str) and tag in {'production-code', 'code'}
                      for tag in ticket.get('tags', [])))
    if not isinstance(target, str) or not target.strip():
        if is_code:
            raise EventContextError('project_route_missing',
                                    'code ticket has no target_url for registered project routing')
        return None
    try:
        route = resolve_registry_target(registry, board_id, target)
    except ValueError as exc:
        raise EventContextError(getattr(exc, 'code', 'project_route_invalid'), str(exc)) from exc
    project = registry['projects'].get(route['project'])
    if (not project or project.get('status') != 'active' or not project.get('fleet', True)
            or project.get('board_id') != board_id):
        raise EventContextError('project_route_invalid', 'resolved project is not active on this board')
    clone_value = project.get('fleet_clone_dir')
    if not clone_value:
        if is_code:
            raise EventContextError('fleet_clone_missing',
                                    'registered project has no protected fleet_clone_dir')
        return {'board_id': board_id, 'project': route['project'],
                'fleet_clone_dir': None, 'repository_url': project.get('repository_url'),
                'worktree': None, 'branch': None, 'workspace_state': 'not_required'}
    try:
        boundary = Path(repository_root).resolve(strict=True)
        clone = Path(clone_value).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        if not is_code:
            return {'board_id': board_id, 'project': route['project'],
                    'fleet_clone_dir': clone_value, 'repository_url': project.get('repository_url'),
                    'worktree': None, 'branch': None, 'workspace_state': 'not_required'}
        raise EventContextError('registered_repository_unavailable',
                                'configured clone root or registered project clone is missing or unsafe') from exc
    if not clone.is_relative_to(boundary):
        if not is_code:
            return {'board_id': board_id, 'project': route['project'],
                    'fleet_clone_dir': str(clone), 'repository_url': project.get('repository_url'),
                    'worktree': None, 'branch': None, 'workspace_state': 'not_required'}
        raise EventContextError('repository_root_mismatch',
                                'registered fleet clone is outside the configured repository root')
    expected_origin = project.get('repository_url')
    if not expected_origin:
        if is_code:
            raise EventContextError('repository_identity_missing',
                                    'code project has no registered repository_url')
        return {'board_id': board_id, 'project': route['project'],
                'fleet_clone_dir': str(clone), 'repository_url': None,
                'worktree': None, 'branch': None, 'workspace_state': 'repository_unverified'}
    try:
        clone_top = Path(git(clone, 'rev-parse', '--show-toplevel')).resolve(strict=True)
        common_value = Path(git(clone, 'rev-parse', '--git-common-dir'))
        common = (common_value if common_value.is_absolute() else clone_top / common_value).resolve(strict=True)
        origin = git(clone, 'remote', 'get-url', 'origin')
    except (OSError, subprocess.CalledProcessError) as exc:
        if not is_code:
            return {'board_id': board_id, 'project': route['project'],
                    'fleet_clone_dir': str(clone), 'repository_url': expected_origin,
                    'worktree': None, 'branch': None, 'workspace_state': 'repository_unverified'}
        raise EventContextError('registered_repository_unavailable',
                                'registered fleet clone is not a usable Git origin') from exc
    if clone_top != clone or _canonical_git_url(origin) != _canonical_git_url(expected_origin):
        if not is_code:
            return {'board_id': board_id, 'project': route['project'],
                    'fleet_clone_dir': str(clone), 'repository_url': expected_origin,
                    'worktree': None, 'branch': None, 'workspace_state': 'repository_unverified'}
        raise EventContextError('repository_identity_mismatch',
                                'fleet clone top-level or origin does not match registered project identity')
    from pursers_client.project_workflow import final_validation_workspace_identity
    final_qa = final_validation_workspace_identity(registry, board_id, ticket)
    if final_qa is not None:
        try:
            git(clone, 'cat-file', '-e', f"{final_qa['combined_revision']}^{{commit}}")
        except subprocess.CalledProcessError as exc:
            raise EventContextError('workspace_candidate_mismatch',
                                    'final-QA combined revision is not present in the verified protected clone') from exc
        if role == 'reviewer':
            final_qa = None
    ticket_id = ticket.get('ticket_id')
    valid_ticket_id = isinstance(ticket_id, str) and re.fullmatch(r'TK-[A-Za-z0-9-]+', ticket_id)
    if not valid_ticket_id and is_code:
        raise EventContextError('ticket_identity_invalid', 'ticket has no valid stable identity')
    if not valid_ticket_id:
        return {'board_id': board_id, 'project': route['project'],
                'fleet_clone_dir': str(clone), 'repository_url': expected_origin,
                'worktree': None, 'branch': None, 'workspace_state': 'not_required'}
    if final_qa is not None and role != 'reviewer':
        # The immutable final-QA packet and active registry contract bind the
        # workspace to the combined source, independently of ticket prose/tags.
        is_code = True
    if not is_code and role != 'reviewer':
        # Existing exact ticket worktrees are source evidence even when an older
        # Coordinator ticket omitted every conventional code tag/field. Only
        # protected common-dir entries with a ticket-bound branch qualify;
        # detached and suffixed diagnostics remain context, not candidates.
        project_worktrees = boundary.parent / 'worktrees' / clone.relative_to(boundary)
        legacy_worktrees = clone / '.worktrees'
        try:
            listings = git(clone, 'worktree', 'list', '--porcelain')
        except (OSError, subprocess.CalledProcessError) as exc:
            raise EventContextError('workspace_invalid',
                                    'protected Git common dir worktree list is unavailable') from exc
        for block in listings.split('\n\n'):
            fields = block.splitlines()
            path_line = next((line for line in fields if line.startswith('worktree ')), None)
            branch_line = next((line for line in fields if line.startswith('branch ')), None)
            if path_line is None or branch_line is None:
                continue
            candidate = Path(path_line.removeprefix('worktree '))
            branch_ref = branch_line.removeprefix('branch ')
            if (candidate.name == ticket_id and candidate.parent.absolute() in
                    {project_worktrees.absolute(), legacy_worktrees.absolute()}
                    and branch_ref.startswith('refs/heads/')
                    and ticket_id in branch_ref.removeprefix('refs/heads/')):
                is_code = True
                break
    if not is_code:
        return {'board_id': board_id, 'project': route['project'],
                'fleet_clone_dir': str(clone), 'repository_url': expected_origin,
                'worktree': None, 'branch': None, 'workspace_state': 'not_required'}
    submission = None
    history = ticket.get('submission_history')
    if isinstance(history, list) and history and isinstance(history[-1], dict):
        submission = history[-1]
    if submission is None:
        submission = ticket
    submitted_branch = submitted_sha = None
    if is_code:
        from pursers_client.submission_evidence import submission_identity
        submitted_branch, submitted_sha = submission_identity(submission)
        if submitted_branch:
            valid_branch = subprocess.run(
                ['git', '-C', str(clone), 'check-ref-format', '--branch', submitted_branch],
                check=False, capture_output=True).returncode == 0
            if not valid_branch:
                raise EventContextError('submitted_branch_invalid',
                                        'ticket submission branch is not a valid Git branch name')
        if role == 'reviewer' and (not submitted_branch or not submitted_sha):
            raise EventContextError('submitted_candidate_missing',
                                    'code review has no exact submitted branch and commit')
    # Prefer a previously registered exact ticket workspace before provisioning.
    # Fleet upgrades may have legacy <protected-clone>/.worktrees/<ticket-id>
    # trees; never adopt suffixed diagnostics or infer paths from ticket prose.
    try:
        clone_relative = clone.relative_to(boundary)
    except ValueError as exc:
        raise EventContextError('repository_root_mismatch',
                                'registered clone is not beneath the configured clone root') from exc
    fleet_root = boundary.parent
    worktrees_root = fleet_root / 'worktrees'
    project_worktrees = worktrees_root / clone_relative
    expected_branch = submitted_branch if role == 'reviewer' else f'pursers/{ticket_id}'
    workspace_name = (f'{ticket_id}-review-{submitted_sha[:12]}'
                      if role == 'reviewer' else ticket_id)
    workspace = project_worktrees / workspace_name
    allowed_roots = [project_worktrees]
    if role != 'reviewer':
        allowed_roots.append(clone / '.worktrees')
    for guarded in (worktrees_root, project_worktrees, workspace, *allowed_roots[1:]):
        if guarded.is_symlink() or guarded.resolve() != guarded.absolute():
            raise EventContextError('workspace_path_escape',
                                    'registered fleet worktree path contains a symlink or escape')

    readonly_detached_final_qa = False
    readonly_final_qa_branch_recoverable = False
    if role != 'reviewer':
        try:
            listings = git(clone, 'worktree', 'list', '--porcelain')
        except (OSError, subprocess.CalledProcessError) as exc:
            raise EventContextError('workspace_invalid',
                                    'protected Git common dir worktree list is unavailable') from exc
        registered = []
        for block in listings.split('\n\n'):
            fields = block.splitlines()
            path_line = next((line for line in fields if line.startswith('worktree ')), None)
            branch_line = next((line for line in fields if line.startswith('branch ')), None)
            if path_line is None:
                continue
            candidate = Path(path_line.removeprefix('worktree '))
            if candidate.name != ticket_id:
                continue
            for allowed_root in allowed_roots:
                if candidate.parent.absolute() == allowed_root.absolute():
                    registered.append((candidate, branch_line.removeprefix('branch ')
                                       if branch_line else None))
                    break
        if len(registered) > 1:
            raise EventContextError('workspace_ambiguous',
                                    'multiple protected Git worktrees have the exact ticket workspace identity')
        if registered:
            legacy_path, branch_ref = registered[0]
            if (not branch_ref or not branch_ref.startswith('refs/heads/')) and final_qa is not None:
                if role == 'reviewer':
                    raise EventContextError('workspace_branch_mismatch',
                                            'final-QA workspace cannot be adopted by a reviewer')
                if not create_if_missing:
                    readonly_detached_final_qa = True
                if legacy_path.is_symlink() or legacy_path.resolve(strict=True).parent != legacy_path.parent.resolve(strict=True):
                    raise EventContextError('workspace_path_escape',
                                            'existing final-QA worktree path contains a symlink or escape')
                try:
                    detached_head = git(legacy_path, 'rev-parse', 'HEAD')
                    detached_status = git(legacy_path, 'status', '--porcelain')
                except (OSError, subprocess.CalledProcessError) as exc:
                    raise EventContextError('workspace_invalid',
                                            'detached final-QA worktree cannot be verified') from exc
                if detached_head != final_qa['combined_revision']:
                    raise EventContextError('workspace_candidate_mismatch',
                                            'detached final-QA worktree is not at the exact combined revision')
                if detached_status:
                    raise EventContextError('workspace_candidate_dirty',
                                            'detached final-QA worktree has changes and cannot be attached safely')
                legacy_branch = f'pursers/{ticket_id}'
                branch_tip = _verified_final_qa_branch_tip(
                    clone, legacy_branch, final_qa['combined_revision'], listings,
                    legacy_path if not readonly_detached_final_qa else None)
                if readonly_detached_final_qa:
                    # A missing ref can be created with no-clobber update-ref;
                    # an existing ref was just checked for ownership and ancestry.
                    readonly_final_qa_branch_recoverable = True
                if not readonly_detached_final_qa:
                    ref = f'refs/heads/{legacy_branch}'
                    if branch_tip is None:
                        try:
                            git(legacy_path, 'switch', '-c', legacy_branch)
                        except subprocess.CalledProcessError as exc:
                            raise EventContextError('workspace_provision_failed',
                                                    'verified final-QA workspace could not be attached without source changes') from exc
                    else:
                        if branch_tip != final_qa['combined_revision']:
                            try:
                                git(clone, 'update-ref', ref,
                                    final_qa['combined_revision'], branch_tip)
                            except subprocess.CalledProcessError as exc:
                                raise EventContextError('workspace_candidate_mismatch',
                                                        'final-QA ticket branch changed during safe fast-forward') from exc
                        try:
                            git(legacy_path, 'switch', legacy_branch)
                        except subprocess.CalledProcessError as exc:
                            raise EventContextError('workspace_provision_failed',
                                                    'verified final-QA workspace could not attach its fast-forwarded ticket branch') from exc
                    if (git(legacy_path, 'rev-parse', 'HEAD') != final_qa['combined_revision']
                            or git(clone, 'rev-parse', f'{ref}^{{commit}}') != final_qa['combined_revision']):
                        raise EventContextError('workspace_candidate_mismatch',
                                                'final-QA ticket branch changed while attaching the verified combined revision')
                    branch_ref = ref
                else:
                    legacy_branch = None
            elif not branch_ref or not branch_ref.startswith('refs/heads/'):
                raise EventContextError('workspace_branch_mismatch',
                                        'existing ticket workspace is detached or has no local branch')
            legacy_branch = (branch_ref.removeprefix('refs/heads/')
                             if branch_ref and branch_ref.startswith('refs/heads/') else None)
            if (not readonly_detached_final_qa and final_qa is not None
                    and legacy_branch != f'pursers/{ticket_id}'):
                raise EventContextError('workspace_branch_mismatch',
                                        'existing final-QA workspace branch is not the deterministic ticket branch')
            if (not readonly_detached_final_qa and final_qa is None
                    and (not legacy_branch or ticket_id not in legacy_branch)):
                raise EventContextError('workspace_branch_mismatch',
                                        'existing ticket workspace branch is not bound to this ticket identity')
            if legacy_path.is_symlink() or legacy_path.resolve(strict=True).parent != legacy_path.parent.resolve(strict=True):
                raise EventContextError('workspace_path_escape',
                                        'existing ticket worktree path contains a symlink or escape')
            workspace = legacy_path
            expected_branch = legacy_branch if legacy_branch else f'pursers/{ticket_id}'

    if not workspace.exists():
        readonly_final_qa_branch_recoverable = False
        if final_qa is not None and not create_if_missing:
            readonly_final_qa_branch_recoverable = (
                _verified_final_qa_branch_tip(
                    clone, f'pursers/{ticket_id}', final_qa['combined_revision'], listings, None)
                is not None)
        if not create_if_missing:
            raise EventContextError('workspace_missing',
                                    'registered ticket workspace is absent; read-only resolution will not create it')
        try:
            project_worktrees.mkdir(parents=True, exist_ok=True)
            if (worktrees_root.is_symlink() or project_worktrees.is_symlink()
                    or project_worktrees.resolve() != project_worktrees.absolute()):
                raise EventContextError('workspace_path_escape',
                                        'registered fleet worktree root became a symlink or escaped')
            if role == 'reviewer':
                try:
                    git(clone, 'cat-file', '-e', f'{submitted_sha}^{{commit}}')
                except subprocess.CalledProcessError as exc:
                    raise EventContextError('submitted_commit_unavailable',
                                            'exact submitted commit is not present in the registered clone') from exc
                git(clone, 'worktree', 'add', '--detach', str(workspace), submitted_sha)
            else:
                branch_tip = None
                if final_qa is not None:
                    branch_tip = _verified_final_qa_branch_tip(
                        clone, expected_branch, final_qa['combined_revision'], listings, None)
                else:
                    branch_exists = subprocess.run(
                        ['git', '-C', str(clone), 'show-ref', '--verify', '--quiet',
                         f'refs/heads/{expected_branch}'], check=False).returncode == 0
                    if branch_exists:
                        git(clone, 'worktree', 'add', str(workspace), expected_branch)
                if final_qa is not None:
                    git(clone, 'cat-file', '-e', f"{final_qa['combined_revision']}^{{commit}}")
                    git(clone, 'worktree', 'add', '--detach', str(workspace),
                        final_qa['combined_revision'])
                    ref = f'refs/heads/{expected_branch}'
                    if branch_tip is None:
                        zero = '0' * 40
                        try:
                            git(clone, 'update-ref', ref, final_qa['combined_revision'], zero)
                        except subprocess.CalledProcessError as exc:
                            raise EventContextError('workspace_candidate_mismatch',
                                                    'final-QA ticket branch appeared during safe creation') from exc
                    else:
                        if branch_tip != final_qa['combined_revision']:
                            try:
                                git(clone, 'update-ref', ref,
                                    final_qa['combined_revision'], branch_tip)
                            except subprocess.CalledProcessError as exc:
                                raise EventContextError('workspace_candidate_mismatch',
                                                        'final-QA ticket branch changed during safe fast-forward') from exc
                    try:
                        git(workspace, 'switch', expected_branch)
                    except subprocess.CalledProcessError as exc:
                        raise EventContextError('workspace_provision_failed',
                                                'verified final-QA worktree could not attach its exact ticket branch') from exc
                elif not branch_exists:
                    integration_ref = project.get('integration_ref', 'main')
                    if not isinstance(integration_ref, str) or not integration_ref:
                        raise EventContextError('integration_ref_missing',
                                                'registered project has no safe integration ref')
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', integration_ref):
                        raise EventContextError('integration_ref_invalid',
                                                'registered project integration ref is malformed')
                    base_ref = f'origin/{integration_ref}'
                    git(clone, 'rev-parse', '--verify', f'{base_ref}^{{commit}}')
                    git(clone, 'worktree', 'add', '-b', expected_branch,
                         str(workspace), base_ref)
        except EventContextError:
            raise
        except (OSError, subprocess.CalledProcessError) as exc:
            raise EventContextError('workspace_provision_failed',
                                    'canonical isolated fleet ticket worktree could not be safely provisioned; '
                                    'existing branches and workspace state were preserved') from exc
        if final_qa is not None:
            try:
                actual_head = git(workspace, 'rev-parse', 'HEAD')
            except (OSError, subprocess.CalledProcessError) as exc:
                raise EventContextError('workspace_candidate_mismatch',
                                        'final-QA workspace HEAD is unavailable') from exc
            if actual_head != final_qa['combined_revision']:
                raise EventContextError('workspace_candidate_mismatch',
                                        'final-QA workspace must start at the exact combined revision')
    try:
        listings = git(clone, 'worktree', 'list', '--porcelain')
        paths = {str(Path(line.removeprefix('worktree ')).resolve())
                 for line in listings.splitlines() if line.startswith('worktree ')}
        worktree = workspace.resolve(strict=True)
        if str(worktree) not in paths:
            raise EventContextError('workspace_unregistered',
                                    'canonical workspace is not registered with the protected Git common dir')
        worktree_top = Path(git(worktree, 'rev-parse', '--show-toplevel')).resolve(strict=True)
        worktree_common_value = Path(git(worktree, 'rev-parse', '--git-common-dir'))
        worktree_common = (worktree_common_value if worktree_common_value.is_absolute()
                           else worktree_top / worktree_common_value).resolve(strict=True)
        branch = git(worktree, 'branch', '--show-current')
        status = git(worktree, 'status', '--porcelain')
    except EventContextError:
        raise
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EventContextError('workspace_invalid',
                                'canonical ticket workspace is not a readable Git worktree') from exc
    if worktree_top != worktree or worktree_common != common:
        raise EventContextError('workspace_repository_mismatch',
                                'ticket workspace does not share the protected repository common dir')
    head = git(worktree, 'rev-parse', 'HEAD')
    if role == 'reviewer':
        if branch not in {'', expected_branch}:
            raise EventContextError('workspace_branch_mismatch',
                                    f'review workspace must be detached or on submitted branch {expected_branch!r}')
        if head != submitted_sha:
            raise EventContextError('workspace_candidate_mismatch',
                                    'review workspace HEAD does not equal the exact submitted commit')
        if status:
            raise EventContextError('workspace_candidate_dirty',
                                    'review worktree has uncommitted changes; exact candidate cannot be verified')
    elif final_qa is not None:
        if head != final_qa['combined_revision']:
            raise EventContextError('workspace_candidate_mismatch',
                                    'final-QA workspace HEAD must remain at the exact combined revision')
        if readonly_detached_final_qa:
            if branch:
                raise EventContextError('workspace_branch_mismatch',
                                        'read-only legacy final-QA workspace is not detached')
        elif branch != expected_branch:
            raise EventContextError('workspace_branch_mismatch',
                                    f'final-QA ticket branch must be {expected_branch!r}, found {branch!r}')
    elif branch != expected_branch:
        raise EventContextError('workspace_branch_mismatch',
                                f'ticket workspace branch must be {expected_branch!r}, found {branch!r}')
    return {'board_id': board_id, 'project': route['project'], 'fleet_clone_dir': str(clone),
            'repository_url': expected_origin, 'worktree': str(worktree),
            'branch': expected_branch if role == 'reviewer' else branch,
            'workspace_state': 'dirty_preserved' if status else 'clean_preserved',
            'dirty_entries': len(status.splitlines()),
            'head': head,
            'final_validation': final_qa,
            'final_qa_branch_recoverable': readonly_final_qa_branch_recoverable,
            'expected_branch': expected_branch}


def exception_leaves(exc):
    """Flatten groups and wrapper causes so mixed failures cannot look transient."""
    pending=[exc]
    leaves=[]
    seen=set()
    while pending:
        current=pending.pop()
        if id(current) in seen: continue
        seen.add(id(current))
        nested=[]
        if isinstance(current,BaseExceptionGroup): nested.extend(current.exceptions)
        # Follow only explicit chaining. Implicit ``__context__`` can be the
        # model wait timeout whose handler happened to call a failing refresh;
        # treating that unrelated context as the refresh cause retries invalid
        # data and authorization failures.
        if current.__cause__ is not None: nested.append(current.__cause__)
        if nested: pending.extend(nested)
        else: leaves.append(current)
    return leaves


def exception_classes(exc):
    return sorted({type(item).__name__ for item in exception_leaves(exc)})


def run_async_blocking(coroutine):
    """Run an async boundary from the synchronous runner, including sync tests in a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _subprocess_transport_failure(exc):
    """Classify the board CLI boundary without treating arbitrary stderr as retryable."""
    message=(str(exc.stderr or "")+" "+str(exc.stdout or "")).casefold()
    if any(text in message for text in (
        "unauthorized", "forbidden", "authentication", "permission denied", "401", "403",
    )): return False
    return any(text in message for text in (
        "connection refused", "all connection attempts failed", "connection reset",
        "server disconnected", "connection closed", "stream closed", "read timed out",
        "connect timeout", "502 bad gateway", "503 service unavailable", "504 gateway timeout",
    ))


def transient_wait_failure(exc):
    """Retry only when every nested leaf is a recognized transport failure."""
    leaves=exception_leaves(exc)
    retryable=(ConnectionError,TimeoutError,subprocess.TimeoutExpired,httpx2.TransportError,
               httpcore2.TimeoutException,httpcore2.NetworkError,httpcore2.ProtocolError,
               anyio.BrokenResourceError,anyio.ClosedResourceError,anyio.EndOfStream)
    return bool(leaves) and all(
        isinstance(item,retryable)
        or (isinstance(item,subprocess.CalledProcessError)
            and _subprocess_transport_failure(item))
        for item in leaves
    )


class EventSeatRunner:
    def __init__(self, config):
        self.config=validate_config(config)
        self.path=Path(config['state_file'])
        self.state={'cursor':{},'seen':[],'runs':[],'pending':[], 'owned_recoveries':{}}
        if self.path.exists(): self.state.update(json.loads(HELPERS['private_read'](self.path,1048576)))
        self.run_command=self._run
        self.monotonic=time.monotonic
        self.preflight_enabled=False
        self.registry={'schema_version': 1, 'projects': {}}

    def record_transport_recovery(self, phase, status, attempts, exc, action):
        self.state['last_transport_failure']={
            'phase':phase,'status':status,'attempts':attempts,
            'reason_code':('transport_recovered' if status=='recovered'
                           else 'transport_recovery_exhausted' if status=='exhausted'
                           else 'transient_transport_failure'),
            'error_classes':exception_classes(exc),'action':action,'at':time.time(),
        }
        PUBLISH(self.path,self.state)

    async def recover_transport(self, phase, operation, exhausted_action):
        last=None
        for attempt in range(1,TRANSPORT_RECOVERY_ATTEMPTS+1):
            try:
                result=await operation()
            except Exception as exc:
                if not transient_wait_failure(exc): raise
                last=exc
                if attempt==TRANSPORT_RECOVERY_ATTEMPTS:
                    self.record_transport_recovery(
                        phase,'exhausted',attempt,exc,exhausted_action)
                    raise TransportRecoveryExhausted(
                        f'{phase} transport recovery exhausted after {attempt} attempts') from exc
                self.record_transport_recovery(
                    phase,'retrying',attempt,exc,'retry_with_preserved_state')
                await asyncio.sleep(TRANSPORT_RECOVERY_BASE_DELAY_S*2**(attempt-1))
                continue
            if last is not None:
                self.record_transport_recovery(
                    phase,'recovered',attempt-1,last,'resume_with_preserved_state')
            return result
        raise AssertionError('unreachable transport recovery')

    def drain_path(self):
        configured=self.config.get('drain_file')
        if configured is not None:return Path(configured)
        return Path(self.config['state_file']).parent.parent/'drain'/f"{self.config['seat_id']}.json"

    def drain_requested(self):
        """Read the executor's owner-only cooperative drain marker."""
        path=self.drain_path()
        try:info=path.lstat()
        except FileNotFoundError:return False
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>4096):
            raise ValueError('event seat drain marker is untrusted')
        try:document=json.loads(HELPERS['private_read'](path,4096))
        except (OSError,UnicodeError,json.JSONDecodeError) as exc:
            raise ValueError('event seat drain marker is invalid') from exc
        if document!={'schema':'pursers_seat_drain_v1','seat_id':self.config['seat_id']}:
            raise ValueError('event seat drain marker is invalid')
        return True

    async def event_authorized(self, event, now):
        """Atomically acquire and route one current event before model launch."""
        async with self.client(event['board']) as client:
            identity = client.identity
            if (identity is None or getattr(identity, 'board_id', event['board']) != event['board']
                    or identity.agent_name != self.config['seat_id']
                    or identity.role != self.config['role']):
                raise ValueError('event preflight identity mismatch')
            ticket = (await client.ticket_get(event['ticket'], view='full'))['ticket']
            if ticket.get('ticket_id') != event['ticket']:
                raise ValueError('event preflight ticket identity mismatch')
            # The journal cue can lag several thousand unrelated rows. Use
            # Central's current read-only ticket as authority, never the event's
            # possibly stale route hint; retain board and ticket identity exactly.
            if event.get('target_url') is not None and ticket.get('target_url') != event.get('target_url'):
                # Current ticket wins: do not route based on an obsolete payload.
                event['target_url'] = ticket.get('target_url')
            owned = self.owned_key(event['board'], ticket, identity, now)
            self._current_claim_ref = owned or 'unowned'

            def resolve_authorized_context():
                # This may provision a Git worktree/branch. Invoke only after
                # exact live ownership is verified or an offer is claimed and
                # refetched below; stale cues must be strictly read-only.
                try:
                    context = resolve_event_context(
                        self.registry, event['board'], ticket,
                        self.config['repository_root'], self.config['seat_id'],
                        role=self.config['role'])
                    context_error = None
                except EventContextError as exc:
                    context = None
                    context_error = exc
                self._event_context = context
                return context_error

            def bounded_context_failure(exc):
                """Persist a stable, safe diagnosis without echoing ticket-controlled text."""
                code = getattr(exc, 'code', 'workspace_invalid')
                if not isinstance(code, str) or not re.fullmatch(r'[a-z0-9_]{1,64}', code):
                    code = 'workspace_invalid'
                safe_details = {
                    'submitted_candidate_missing': (
                        'code review has no exact submitted branch and commit; do not infer one. '
                        'Ask the submission owner to republish the canonical candidate identity '
                        'through the supported workflow before retrying review'
                    ),
                    'submitted_commit_unavailable': 'exact submitted commit is not present in the registered clone',
                    'workspace_candidate_dirty': 'review worktree is dirty and the exact candidate cannot be verified',
                    'workspace_candidate_mismatch': 'review worktree does not match the exact submitted commit',
                    'workspace_missing': 'registered ticket worktree is missing and cannot be selected safely',
                    'workspace_ambiguous': 'multiple registered worktrees match the ticket identity',
                    'repository_identity_mismatch': 'registered clone identity does not match the project',
                }
                return code, safe_details.get(
                    code, 'event runner rejected an unsafe or unavailable ticket workspace')

            async def hold_context_failure(exc):
                code, detail = bounded_context_failure(exc)
                message = (f"Event runner cannot safely resolve this ticket workspace "
                           f"({code}): {detail}. No model work was launched; preserve "
                           "the submitted candidate and existing worktrees, then inspect "
                           "the exact registered clone/worktree evidence.")
                message_id = 'MSG-workspace-' + hashlib.sha256(
                    json.dumps([event['board'], event['ticket'], code, detail],
                               separators=(',', ':')).encode('utf-8')
                ).hexdigest()[:24]
                report_error = None
                try:
                    await client.ticket_question_ask(
                        event['ticket'], message, 'information', message_id=message_id)
                except Exception as exc:
                    report_error = exc
                    self.state['last_context_reporting_failure'] = {
                        'board': event['board'], 'ticket': event['ticket'],
                        'context_code': code, 'context_detail': detail,
                        'message_id': message_id,
                        'report_error_class': type(exc).__name__, 'at': time.time(),
                    }
                    PUBLISH(self.path, self.state)
                if self.config['role'] == 'reviewer':
                    # Human holds canonically release review leases and support
                    # exact board:review authority; never ask for board:write.
                    try:
                        await client.ticket_request_human(
                            event['ticket'], message, 'decision')
                    except Exception as exc:
                        self.state['last_context_hold_failure'] = {
                            'board': event['board'], 'ticket': event['ticket'],
                            'context_code': code, 'context_detail': detail,
                            'hold_error_class': type(exc).__name__, 'at': time.time(),
                        }
                        PUBLISH(self.path, self.state)
                        try:
                            await client.ticket_review_release(
                                event['ticket'], reason=f'{code}: {detail[:180]}')
                        except Exception as release_exc:
                            self.state['last_context_release_failure'] = {
                                'board': event['board'], 'ticket': event['ticket'],
                                'release_error_class': type(release_exc).__name__,
                                'at': time.time(),
                            }
                            PUBLISH(self.path, self.state)
                            raise ValueError(
                                'review context blocker reporting and canonical release failed') from release_exc
                        finally:
                            self._current_claim_ref = 'unowned'
                        return False, f'{code}:hold_failed:{type(exc).__name__}'
                    finally:
                        if self.config['role'] == 'reviewer':
                            self._current_claim_ref = 'unowned'
                else:
                    try:
                        await client.ticket_request_human(
                            event['ticket'], message, 'decision')
                    except Exception as exc:
                        self.state['last_context_hold_failure'] = {
                            'board': event['board'], 'ticket': event['ticket'],
                            'context_code': code, 'context_detail': detail,
                            'hold_error_class': type(exc).__name__, 'at': time.time(),
                        }
                        PUBLISH(self.path, self.state)
                        try:
                            await client.ticket_unclaim(event['ticket'])
                        except Exception as release_exc:
                            self.state['last_context_release_failure'] = {
                                'board': event['board'], 'ticket': event['ticket'],
                                'release_error_class': type(release_exc).__name__,
                                'at': time.time(),
                            }
                            PUBLISH(self.path, self.state)
                            raise ValueError(
                                'workspace blocker hold and canonical unclaim failed') from release_exc
                        finally:
                            self._current_claim_ref = 'unowned'
                        if report_error is not None:
                            return False, f'{code}:report_failed:{type(report_error).__name__}'
                        return False, f'{code}:hold_failed:{type(exc).__name__}'
                    finally:
                        self._current_claim_ref = 'unowned'
                held = (await client.ticket_get(event['ticket'], view='full'))['ticket']
                if held.get('status') != 'needs_human':
                    raise ValueError('workspace blocker did not create a Central human hold')
                if report_error is not None:
                    return False, f'{code}:report_failed:{type(report_error).__name__}'
                return False, code

            if event.get('recovery_key'):
                if owned != event['recovery_key']:
                    return False, 'stale_owned_recovery'
                context_error = resolve_authorized_context()
                if context_error is not None:
                    return await hold_context_failure(context_error)
                return True, 'owned_lease'
            if owned is not None:
                self._current_claim_ref=owned
                context_error = resolve_authorized_context()
                if context_error is not None:
                    return await hold_context_failure(context_error)
                return True, 'owned_lease'
            if self.drain_requested():
                return False, 'seat_draining'
            expected_kind = 'review' if self.config['role'] == 'reviewer' else 'work'

            def offered(current):
                eligible_status = current.get('status') in (
                    {'submitted'} if expected_kind == 'review' else {'open'})
                offer = current.get(
                    'review_offer' if expected_kind == 'review' else 'work_offer'
                ) or {}
                expiry = offer.get('expires_at_epoch')
                exact_offer = (
                    offer.get('kind') == expected_kind
                    and offer.get('agent_id') == identity.agent_id
                    and offer.get('agent_name') == identity.agent_name
                    and isinstance(expiry, (int, float))
                    and not isinstance(expiry, bool)
                    and expiry > now
                )
                dispatch = current.get('dispatch_state') or {}
                broadcast = (
                    dispatch.get('state') == 'broadcast'
                    and dispatch.get('kind') == expected_kind
                )
                return eligible_status and (exact_offer or broadcast)

            if not offered(ticket):
                return False, 'stale_or_foreign_event'
            claim_error = None
            try:
                if expected_kind == 'review':
                    await client.ticket_review_claim(event['ticket'])
                else:
                    await client.ticket_claim(event['ticket'])
            except Exception as exc:
                # A simultaneous broadcast contender is expected to lose the
                # server-side admission race. Refetch once: only ownership that
                # Central now attributes to this exact identity is admissible.
                claim_error = exc
            current = (await client.ticket_get(event['ticket'], view='full'))['ticket']
            if current.get('ticket_id') != event['ticket']:
                raise ValueError('event preflight ticket identity changed during claim')
            acquired = self.owned_key(event['board'], current, identity, time.time())
            if acquired is None:
                if claim_error is not None and not offered(current):
                    return False, 'claim_race_lost'
                if claim_error is not None:
                    raise claim_error
                raise ValueError('event preflight claim was not retained')
            self._current_claim_ref = acquired
            ticket = current
            context_error = resolve_authorized_context()
            if context_error is not None:
                return await hold_context_failure(context_error)
            return True, 'claim_acquired'

    def environment(self):
        c=self.config
        client=c.get('client', 'goose')
        host='codex' if client == 'codex' else 'goose'
        draining=self.drain_requested()
        env={**os.environ,'PURSERS_WAIT_MODE':'push','PURSERS_BOARDS':'registry','PURSERS_PROJECT_BOARD':'',
             'PURSERS_MODEL':c['model'],'PURSERS_PROVIDER':c['provider'],'PURSERS_HOST':host,
             'PURSERS_TIER_MAX':str(c.get('tier_max',2)),
             'PURSERS_SKILLS':','.join(c['skills']),
             'PURSERS_CAN_WORK':str(c['role']=='worker' and not draining).lower(),
             'PURSERS_CAN_REVIEW':str(c['role']=='reviewer' and not draining).lower()}
        if client == 'goose':
            env.update(GOOSE_MODEL=c['model'], GOOSE_PROVIDER=c['provider'], GOOSE_MODE='auto')
            env.pop('GOOSE_THINKING_EFFORT',None)
            if c.get('effort'): env['GOOSE_THINKING_EFFORT']=c['effort']
        if c.get('git_credentials_config'):
            helper='!'+shlex.join([sys.executable,str(Path(__file__).resolve().parents[1]/'ado-connector/git_credential.py'),
                                   '--config',c['git_credentials_config']])
            env.update(GIT_CONFIG_COUNT='3',GIT_CONFIG_KEY_0='credential.helper',GIT_CONFIG_VALUE_0='',
                GIT_CONFIG_KEY_1='credential.helper',GIT_CONFIG_VALUE_1=helper,
                GIT_CONFIG_KEY_2='credential.useHttpPath',GIT_CONFIG_VALUE_2='true',GIT_TERMINAL_PROMPT='0')
        return env

    def _stop_process(self, process):
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=PROCESS_STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _diagnostic_secrets(self):
        values=[]
        try:
            token=HELPERS['private_read'](Path(self.config['token_file']),16384).strip()
            if token: values.append(token)
        except (OSError, ValueError):
            pass
        for key, value in os.environ.items():
            if (value and re.search(r'(?:API_KEY|ACCESS_TOKEN|AUTHORIZATION|PASSWORD|SECRET|TOKEN)$', key, re.I)
                    and len(value) >= 6):
                values.append(value)
        return values

    async def verify_turn_outcome(self, turn):
        """Read Central after each child exit; never infer ticket completion from exit 0."""
        async with self.client(turn['board']) as client:
            if (client.identity is None or client.identity.board_id != turn['board']
                    or client.identity.agent_name != self.config['seat_id']
                    or client.identity.role != self.config['role']):
                raise ValueError('turn outcome identity mismatch')
            ticket=(await client.ticket_get(turn['ticket'],view='full'))['ticket']
            if ticket.get('ticket_id') != turn['ticket']:
                raise ValueError('turn outcome ticket mismatch')
            status=ticket.get('status')
            owned=self.owned_key(turn['board'],ticket,client.identity,time.time())
            # A submitted ticket is not complete for a reviewer while this
            # exact seat still holds its live review lease. The child may have
            # exited without recording a verdict; let bounded recovery handle
            # that unfinished owned turn instead of reporting false success.
            if (self.config['role'] == 'reviewer' and status == 'submitted'
                    and owned is not None and owned == turn.get('claim_ref')):
                return {'outcome':'unfinished_with_owned_lease','ticket_status':status,'owned':True}
            if status == 'submitted':
                return {'outcome':'ticket_submitted','ticket_status':status,'owned':False}
            if status == 'closed':
                return {'outcome':'ticket_closed','ticket_status':status,'owned':False}
            if status == 'needs_human':
                return {'outcome':'ticket_human_held','ticket_status':status,'owned':False}
            if owned is not None and owned == turn.get('claim_ref'):
                return {'outcome':'unfinished_with_owned_lease','ticket_status':status,'owned':True}
            return {'outcome':'lease_not_owned','ticket_status':status,'owned':False}

    def _record_child_diagnostic(self, outcome, error_class, returncode, stderr):
        turn=getattr(self,'_active_turn',None)
        if turn is None:
            return None
        capture=getattr(self,'_stderr_capture',None)
        text=capture.text() if capture is not None else ''
        truncated=bool(capture and capture.truncated)
        read_error=getattr(capture,'read_error',None) if capture is not None else None
        try:
            stderr_class=DIAGNOSTICS['classify_failure'](text)
            typed_outcome=(stderr_class if stderr_class and outcome in {
                'model_process_failed','child_exit_zero'} else outcome)
            ref=DIAGNOSTICS['write_evidence'](
                self.path, turn['attempt_id'], {
                    'operation_id':turn['attempt_id'], 'board_id':turn['board'],
                    'ticket_id':turn['ticket'], 'attempt_id':turn['attempt_id'],
                    'claim_ref':turn.get('claim_ref','unavailable'), 'outcome':typed_outcome,
                    'child_outcome':outcome, 'error_class':error_class, 'returncode':returncode,
                    'stderr_class':stderr_class, 'stderr_read_error':read_error,
                }, text, self._diagnostic_secrets(), stderr_truncated=truncated)
            return {'ref':str(ref), 'outcome':typed_outcome, 'omissions':{
                'stdout':'not_captured','private_prompt':'not_present',
                'stderr_truncated':truncated or len(text.encode('utf-8',errors='replace')) > DIAGNOSTICS['MAX_EVIDENCE_BYTES'],
            }}
        except (OSError, ValueError) as exc:
            return {'ref':None, 'outcome':outcome, 'omissions':{'diagnostic_write_error':type(exc).__name__,
                'stdout':'not_captured','private_prompt':'not_present','stderr':'not_retained'}}

    def _apply_child_result(self, last_turn, result):
        """Expose one bounded child result while keeping Central authoritative."""
        if not isinstance(result, dict):
            return
        diagnostic=result.get('diagnostic') or {}
        runtime_outcome=diagnostic.get('outcome') or result.get('outcome')
        last_turn.update(child_outcome=result.get('outcome'),
                         child_error_class=result.get('error_class'),
                         child_returncode=result.get('returncode'))
        if diagnostic:
            last_turn['diagnostic_ref']=diagnostic.get('ref')
            last_turn['omissions']=diagnostic.get('omissions')
            if diagnostic.get('ref') is None:
                last_turn['diagnostic_write_error']=(diagnostic.get('omissions') or {}).get('diagnostic_write_error')
        if runtime_outcome:
            last_turn['runtime_outcome']=runtime_outcome
            if runtime_outcome in {'provider_unavailable','transport_failure','model_timeout','authentication_failure'}:
                last_turn['outcome']=runtime_outcome
            elif result.get('outcome') == 'model_process_failed':
                last_turn['outcome']='model_nonzero_exit'
            elif result.get('outcome') == 'child_exit_zero':
                last_turn['outcome']='exited_pending_verification'

    def _run(self, argv, **kwargs):
        timeout=self.config.get('turn_timeout_s',1800)
        # Validate and refresh every selected membership immediately before the
        # paid turn. This also makes a long turn visible without another model
        # call and fails closed before launch when registry readiness is broken.
        run_async_blocking(self.recover_transport(
            'presence_prelaunch',self.refresh_presence,
            'model_not_started; retry the preserved event after transport recovery'))
        self._stderr_capture=DIAGNOSTICS['BoundedStderr']()
        try:
            process=subprocess.Popen(argv,env=self.environment(),cwd=self.config['seat_dir'],
                stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        except BaseException:
            self._stderr_capture=None
            raise
        stderr_stream=getattr(process,'stderr',None)
        drain_thread=None
        if stderr_stream is not None:
            drain_thread=threading.Thread(target=self._stderr_capture.drain,
                                          args=(stderr_stream,),daemon=True)
            drain_thread.start()
        started=self.monotonic()
        next_presence=started+PRESENCE_INTERVAL_S
        outcome='runner_interrupted'; error_class=None; returncode=None
        try:
            while True:
                now=self.monotonic()
                remaining=timeout-(now-started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv,timeout)
                try:
                    code=process.wait(timeout=min(remaining,max(0,next_presence-now)))
                except subprocess.TimeoutExpired:
                    now=self.monotonic()
                    if now-started >= timeout:
                        raise subprocess.TimeoutExpired(argv,timeout)
                    run_async_blocking(self.recover_transport(
                        'presence_active_model',self.refresh_presence,
                        'stop_model_after_grace; preserve ticket lease and partial work'))
                    next_presence=self.monotonic()+PRESENCE_INTERVAL_S
                    continue
                returncode=code
                if code:
                    outcome='model_process_failed'; error_class='CalledProcessError'
                    raise subprocess.CalledProcessError(code,argv)
                outcome='child_exit_zero'
                return
        except subprocess.TimeoutExpired:
            outcome='model_timeout'; error_class='TimeoutExpired'
            self._stop_process(process)
            raise
        except BaseException as exc:
            if error_class is None:
                error_class=type(exc).__name__
            if isinstance(exc,TransportRecoveryExhausted) or transient_wait_failure(exc):
                outcome='transport_failure'
            self._stop_process(process)
            raise
        finally:
            if drain_thread is not None:
                drain_thread.join(timeout=5)
            detail=self._record_child_diagnostic(outcome,error_class,returncode,None)
            self._child_result={'outcome':outcome,'error_class':error_class,
                                'returncode':returncode,'diagnostic':detail}
            self._stderr_capture=None

    def model_command(self, prompt, extension):
        """Return one explicit, profile-preserving model invocation."""
        c = self.config
        if c.get('client', 'goose') == 'goose':
            return [c['goose'],'run','--no-session','--no-profile','--with-builtin','developer',
                '--with-extension',extension,'--provider',c['provider'],'--model',c['model'],
                '--max-turns',str(c.get('max_turns',30)),'--text',prompt]
        command = [c['codex'], 'exec', '-m', c['model'],
            '-c', f'model_reasoning_effort="{c.get("effort", "high")}"']
        if c.get('service_tier'):
            command.extend(('-c', f'service_tier="{c["service_tier"]}"'))
        if c.get('codex_profile'):
            command.extend(('--profile', c['codex_profile']))
        if c.get('codex_sandbox', 'workspace-write') == 'danger-full-access':
            command.append('--dangerously-bypass-approvals-and-sandbox')
        else:
            command.extend(('-s', 'workspace-write', '-c',
                            'sandbox_workspace_write.network_access=true'))
        command.extend(('--skip-git-repo-check', '-C', c['seat_dir'], '--json'))
        if c.get('last_message_file'):
            command.extend(('-o', c['last_message_file']))
        command.append(prompt)
        return command

    def process(self,response,now):
        if response.get('skipped_boards'): raise ValueError('registry boards skipped')
        cursor=response.get('new_seq')
        if (not isinstance(cursor,dict) or not cursor or any(not SAFE_ID.fullmatch(k) or type(v) is not int or v<1 for k,v in cursor.items())):
            raise ValueError('invalid positive cursor map')
        pending=self.state['pending']
        role=self.config['role']
        for event in response.get('events',[]):
            kind=event.get('kind')
            relevant=(kind==('review_offered' if role=='reviewer' else 'ticket_offered')
                      or event.get('reason') in {'held_ticket_update','broadcast','offer_reconcile'})
            if kind in {'offer_expired','offer_revoked'} or not relevant: continue
            board,ticket=event.get('board_id'),event.get('ticket_id')
            if not isinstance(board,str) or not SAFE_ID.fullmatch(board) or not isinstance(ticket,str) or not re.fullmatch(r'TK-[A-Za-z0-9-]+',ticket):
                raise ValueError('event missing exact board and ticket')
            # Journal offers have event IDs/sequences, not ticket updated_at values.
            # A later reoffer must wake the seat while replaying the same event must not.
            stamp=event.get('updated_at')
            if stamp is None:
                stamp=event.get('id') or event.get('seq')
            marker=[board,ticket,stamp,kind]
            if marker not in self.state['seen'] and not any(p['marker']==marker for p in pending):
                pending.append({'marker':marker,'board':board,'ticket':ticket})
        if len(pending)>100: raise ValueError('event queue exceeds bound')
        self.state['cursor']=cursor
        PUBLISH(self.path,self.state)
        while pending:
            event=pending[0]
            runs=[r for r in self.state['runs'] if now-r<3600]
            limit=self.config.get('max_runs_per_hour')
            if limit is not None and len(runs)>=limit:
                self.state['runs']=runs
                self.state['rate_limited_until']=min(runs)+3600
                PUBLISH(self.path,self.state)
                return max(0,self.state['rate_limited_until']-now)
            self.state.pop('rate_limited_until',None)
            if self.preflight_enabled:
                authorized, reason = run_async_blocking(self.recover_transport(
                    'event_preflight',lambda:self.event_authorized(event,time.time()),
                    'keep_event_pending; do_not_launch_model'))
                if not authorized:
                    pending.pop(0)
                    self.state['seen']=(self.state['seen']+[event['marker']])[-200:]
                    self.state['last_skip']={'board':event['board'],'ticket':event['ticket'],
                        'reason':reason,'at':time.time()}
                    PUBLISH(self.path,self.state)
                    continue
            event=pending.pop(0)
            if event.get('recovery_key'):
                key = event['recovery_key']
                self.state['owned_recoveries'][key] = self.state['owned_recoveries'].get(key, 0) + 1
            self.state['runs']=(runs+[now])[-200:]
            self.state['seen']=(self.state['seen']+[event['marker']])[-200:]
            PUBLISH(self.path,self.state)  # reserve before a potentially uncertain model execution
            c=self.config
            event_context = getattr(self, '_event_context', None)
            context_block = (' Trusted project/workspace context (Central registry and Git-verified): '
                + json.dumps(event_context, sort_keys=True, separators=(',', ':'))
                + '. Keep Goose process CWD at seat identity directory; use only this worktree for code. '
                'Never reset, replace, or discard preserved changes. If route/workspace is ambiguous or unsafe, '
                'request a typed human hold before paid work.') if event_context else (
                ' No code workspace route applies to this contextless task; do not infer a project from ticket text.')
            extension=(
                'pursers: '+shlex.join([c['mcp'],'--central-url',c['central_url'],'--board',event['board'],
                    '--token-file',c['token_file'],'--tools',role,'--repository-root',c['repository_root']])
                if c.get('client', 'goose') == 'goose'
                else 'the seat profile configured Pursers MCP tools'
            )
            instructions = 'Read AGENTS.md; read .goosehints only if its contents differ.' if c.get('client', 'goose') == 'goose' else 'Read AGENTS.md and START.md.'
            review_scope = (' For independent review, investigate source/config and the ticket first; compare the '
                    'same commands and environment on the exact base and candidate. Report scoped ticket '
                    'acceptance separately from final whole-project QA: identify the target finding, unchanged '
                    'baseline findings, and any introduced or unfixed findings with truthful evidence. An unchanged '
                    'unrelated baseline failure is not a new regression, but do not call the project all-green or '
                    'waive its configured final gate. State unknowns explicitly, never fabricate evidence, and retain '
                    'the independent review decision.') if role == 'reviewer' else ''
            prompt=('$token-thrift. Process exactly one authorized Pursers event, then exit. '
                    f"Seat {c['seat_id']}; role {role}; board_id {event['board']}; ticket_id {event['ticket']}. "
                    f'{instructions} Use this exact board for every operation. '
                    + context_block + ' '
                    'GET the ticket first and verify exact current ownership. If already held by this seat, resume it; '
                    'otherwise claim or review-claim only an authorized offer. Preserve existing worktrees, changes, '
                    'commits and evidence. Follow its scope, perform work or independent verification, '
                    'record required evidence and submit or review on the exact board_id above. '
                    + review_scope + (' Confirm the resulting Central ticket state before exiting. If blocked, '
                    'ask the authorized Butler a concrete technical question with inspected references; continue '
                    'owned fixes, tests, format/lint and scoped submission when target checks pass. Request a human '
                    'hold only for missing domain/authority facts or exhausted no-progress recovery. '
                    if role == 'worker' else ' Confirm the resulting Central ticket state before exiting. If blocked, '
                    'record the independent scoped decision and evidence; do not alter candidate code.')
                    + (' If a genuine domain/authority blocker remains, request human input on this ticket; '
                       'never return silently while holding a lease. ' if role == 'worker' else
                       'If review is blocked, preserve the exact candidate and report the scoped blocker; '
                       'never alter candidate code. ')
                    + 'Do not poll. Stop on authentication failure.')
            attempt_id=uuid.uuid4().hex
            turn={'board':event['board'],'ticket':event['ticket'],'attempt_id':attempt_id,
                'claim_ref':getattr(self,'_current_claim_ref',None) or event.get('recovery_key','unavailable')}
            self._active_turn=turn
            self._child_result=None
            self.state['last_turn'] = {'board':event['board'], 'ticket':event['ticket'],
                'attempt_id':attempt_id, 'operation_id':attempt_id,
                'claim_ref':turn['claim_ref'], 'started_at':now, 'outcome':'started'}
            PUBLISH(self.path,self.state)
            try:
                self.run_command(self.model_command(prompt, extension))
            except subprocess.TimeoutExpired as exc:
                self.state['last_turn'].update(outcome='interrupted', error_class=type(exc).__name__,
                    exit_cause='model_timeout')
                self._apply_child_result(self.state['last_turn'],getattr(self,'_child_result',None))
            except subprocess.CalledProcessError as exc:
                self.state['last_turn'].update(outcome='interrupted', error_class=type(exc).__name__,
                    exit_cause='model_process_failed', returncode=exc.returncode)
                self._apply_child_result(self.state['last_turn'],getattr(self,'_child_result',None))
            except BaseException as exc:
                self.state['last_turn'].update(outcome='interrupted', error_class=type(exc).__name__,
                    exit_cause='runner_or_transport_failure')
                self._apply_child_result(self.state['last_turn'],getattr(self,'_child_result',None))
                raise
            else:
                child=getattr(self,'_child_result',None)
                if child is None:
                    child={'outcome':'child_exit_zero','error_class':None,'returncode':0,'diagnostic':None}
                self.state['last_turn'].update({'outcome':'child_exited', **child})
                self._apply_child_result(self.state['last_turn'],child)
            finally:
                self.state['last_turn']['finished_at'] = time.time()
                PUBLISH(self.path,self.state)
                if self.preflight_enabled:
                    try:
                        verified=run_async_blocking(self.recover_transport(
                            'turn_outcome',lambda:self.verify_turn_outcome(turn),
                            'outcome_unavailable; preserve event and live lease for bounded reconciliation'))
                    except TransportRecoveryExhausted as exc:
                        self.state['last_turn'].update(outcome='outcome_unavailable',
                            verification_error=type(exc).__name__)
                    except Exception as exc:
                        self.state['last_turn'].update(outcome='outcome_verification_failed',
                            verification_error=type(exc).__name__)
                    else:
                        self.state['last_turn']['central_outcome']=verified['outcome']
                        self.state['last_turn']['ticket_status']=verified['ticket_status']
                        self.state['last_turn']['owned']=verified['owned']
                        self.state['last_turn']['outcome']=verified['outcome']
                        if verified['outcome']=='unfinished_with_owned_lease':
                            self.state['last_turn']['next_action']='bounded_owned_recovery_or_human_escalation'
                        elif verified['outcome'] in {'ticket_submitted','ticket_closed','ticket_human_held'}:
                            self.state['owned_recoveries'].pop(turn['claim_ref'],None)
                else:
                    self.state['last_turn']['verification']='not_enabled_in_test_harness'
                PUBLISH(self.path,self.state)
                self._active_turn=None

    def client(self, board):
        from pursers_client import BoardClient
        c = self.config
        token = HELPERS['private_read'](Path(c['token_file']),16384).strip()
        client=c.get('client', 'goose')
        host='codex' if client == 'codex' else 'goose'
        draining=self.drain_requested()
        return BoardClient(c['central_url'],token,board,agent_name=c['seat_id'],role=c['role'],
            capabilities={'can_work':c['role']=='worker' and not draining,
                          'can_review':c['role']=='reviewer' and not draining,
                          'tier_max':c.get('tier_max',2),'max_parallel':1,
                          'skills':c['skills'],'host':host,'model':c['model'],'provider':c['provider']},
            allow_takeover=True, renewal_source='keepalive', agent_platform=host)

    async def refresh_presence(self):
        """Refresh every active board while proving one exact seat principal."""
        expected_principal=None
        if not getattr(self,'active_boards',None):
            raise ValueError('active registry boards unavailable')
        for board in self.active_boards:
            async with self.client(board) as client:
                identity=client.identity
                if (identity is None or identity.board_id != board
                        or identity.agent_name != self.config['seat_id']
                        or identity.role != self.config['role']):
                    raise ValueError('registry presence identity mismatch')
                if expected_principal is None:
                    expected_principal=identity.principal_id
                elif identity.principal_id != expected_principal:
                    raise ValueError('registry presence principal mismatch')

    def owned_key(self, board, ticket, identity, now):
        if self.config['role'] == 'reviewer':
            lease = ticket.get('review_lease') or {}
            owned = (ticket.get('status') == 'submitted'
                     and lease.get('reviewer_agent_id') == identity.agent_id
                     and lease.get('reviewer_principal_id') == identity.principal_id
                     and lease.get('expires_at_epoch',0) > now)
            epoch = lease.get('claimed_at')
        else:
            owned = (ticket.get('status') in {'claimed','in_progress','creating_report'}
                     and ticket.get('claimed_by_agent_id') == identity.agent_id
                     and ticket.get('claimed_by_principal_id') == identity.principal_id
                     and ticket.get('lease_expires_at_epoch',0) > now)
            epoch = ticket.get('claimed_at')
        if not owned: return None
        tid = ticket.get('ticket_id')
        if not isinstance(tid,str) or not re.fullmatch(r'TK-[A-Za-z0-9-]+',tid) or not isinstance(epoch,str):
            raise ValueError('owned lease lacks stable ticket or claim identity')
        return json.dumps([board,tid,self.config['role'],epoch],separators=(',',':'))

    async def reconcile_owned(self):
        """Recover unfinished owned turns once, then visibly pause; never infer progress from exit 0."""
        pending = [p for p in self.state['pending'] if not p.get('recovery_key')]
        live = set()
        for board in self.active_boards:
            async with self.client(board) as client:
                joined = await client.board_join(allow_takeover=True)
                if not isinstance(joined.get('renewed_leases'),list):
                    raise ValueError('Central did not report owned leases')
                for row in joined['renewed_leases']:
                    ticket = (await client.ticket_get(row['ticket_id'],view='full'))['ticket']
                    key = self.owned_key(board,ticket,client.identity,time.time())
                    if key is None: continue
                    live.add(key)
                    tid = ticket['ticket_id']
                    pending = [p for p in pending if (p['board'],p['ticket']) != (board,tid)]
                    attempts = self.state['owned_recoveries'].get(key,0)
                    if attempts >= self.config.get('max_owned_recoveries',1):
                        await client.ticket_request_human(tid,
                            'The event runner exited without completing this owned ticket. '
                            'Its bounded automatic continuation is exhausted. Partial work and evidence '
                            'are preserved; inspect the blocker before authorizing another attempt.', 'decision')
                    else:
                        pending.insert(0,{'board':board,'ticket':tid,'recovery_key':key,
                            'marker':[board,tid,f'{key}:{attempts+1}','owned_recovery']})
        self.state['pending'] = pending
        self.state['owned_recoveries'] = {k:v for k,v in self.state['owned_recoveries'].items() if k in live}
        PUBLISH(self.path,self.state)

    async def bootstrap(self):
        from pursers_client.project_registry import parse_project_registry,active_registry_boards
        c=self.config
        async with self.client(c['home_board']) as home:
            registry=parse_project_registry(await home.board_state_get('project_registry'))
        validate_registry_roots(registry,c['repository_root'])
        self.registry = registry
        boards=active_registry_boards(registry,c['home_board'])
        self.active_boards=boards
        self.preflight_enabled=True
        if any(type(v) is not int or v<1 for v in self.state['cursor'].values()):
            raise ValueError('invalid saved positive cursor')
        for board in boards:
            if board in self.state['cursor']: continue
            async with self.client(board) as joined:
                snapshot=await joined.board_snapshot(limit=1,max_bytes=100000)
            seq=snapshot.get('latest_seq')
            if type(seq) is not int or seq<1: raise ValueError('authoritative positive cursor unavailable')
            self.state['cursor'][board]=seq
        PUBLISH(self.path,self.state)

    def run(self):
        while True:
            asyncio.run(self.recover_transport(
                'bootstrap',self.bootstrap,
                'exit_with_saved_positive_cursors; operator checks Central transport'))
            asyncio.run(self.recover_transport(
                'owned_reconcile',self.reconcile_owned,
                'exit_with_owned_lease_state preserved; operator checks Central transport'))
            if self.state['pending']:
                delay=self.process({'new_seq':self.state['cursor'],'events':[]},time.time())
                if delay is not None and delay>0:
                    time.sleep(min(60,delay))
                continue
            if self.drain_requested():
                # Keep memberships visibly non-admitting while an already-held
                # lease finishes and the executor waits to perform the stop.
                asyncio.run(self.refresh_presence())
                time.sleep(30)
                continue
            command=[self.config['board_script'],'wait','--since',json.dumps(self.state['cursor']),
                     '--timeout','270','--boards',','.join(self.active_boards)]
            if self.config['role']=='reviewer':command.insert(2,'--submitted')
            last=None
            for attempt in range(1,TRANSPORT_RECOVERY_ATTEMPTS+1):
                try:
                    result=subprocess.run(command,env=self.environment(),cwd=self.config['seat_dir'],
                                          capture_output=True,text=True,check=True,timeout=300)
                except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
                    if not transient_wait_failure(exc): raise
                    last=exc
                    if attempt==TRANSPORT_RECOVERY_ATTEMPTS:
                        self.record_transport_recovery(
                            'wait','exhausted',attempt,exc,
                            'exit_with_saved_positive_cursors; do_not_reset_or_replay_model')
                        raise TransportRecoveryExhausted(
                            f'wait transport recovery exhausted after {attempt} attempts') from exc
                    delay=TRANSPORT_RECOVERY_BASE_DELAY_S*2**(attempt-1)
                    self.record_transport_recovery(
                        'wait','retrying',attempt,exc,'retry_from_saved_positive_cursors')
                    print(f"event-seat: transport unavailable; reconnect in {delay}s",file=sys.stderr)
                    time.sleep(delay)
                    continue
                break
            if last is not None:
                self.record_transport_recovery(
                    'wait','recovered',attempt-1,last,'resume_from_returned_cursor')
            self.process(json.loads(result.stdout),time.time())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args()
    EventSeatRunner(json.loads(HELPERS['private_read'](args.config))).run()


if __name__=='__main__': main()
