#!/usr/bin/env python3
"""Run bounded agent work only after relevant registry events; persist cursors.

The persistent process only waits on Central.  It starts Goose or Codex CLI for
an authorized event, so an idle fleet performs no paid model or provider call.
"""
import argparse
import asyncio
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

import anyio
import httpx2

HELPERS = runpy.run_path(str(Path(__file__).resolve().parents[1]/'ado-connector/git_credential.py'))
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


def validate_registry_roots(registry, repository_root):
    """Reject an unusable relay boundary before invoking any model."""
    root = Path(repository_root).resolve()
    for project in registry['projects'].values():
        if project['status'] != 'active' or not project.get('fleet', True):
            continue
        work = Path(project.get('fleet_clone_dir') or project['work_dir']).resolve()
        if not work.is_relative_to(root):
            raise ValueError('repository_root excludes an active registry project clone')


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
        """Atomically acquire current work before spending a model turn."""
        async with self.client(event['board']) as client:
            identity = client.identity
            if (identity is None or identity.agent_name != self.config['seat_id']
                    or identity.role != self.config['role']):
                raise ValueError('event preflight identity mismatch')
            ticket = (await client.ticket_get(event['ticket'], view='full'))['ticket']
            if self.owned_key(event['board'], ticket, identity, now) is not None:
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
            try:
                if expected_kind == 'review':
                    await client.ticket_review_claim(event['ticket'])
                else:
                    await client.ticket_claim(event['ticket'])
            except Exception:
                # A simultaneous broadcast contender is expected to lose the
                # server-side admission race. Refetch once: only a changed
                # ownership/offer is a safe skip; persistent eligibility means
                # the original error (including authorization) must stop us.
                current = (await client.ticket_get(event['ticket'], view='full'))['ticket']
                if self.owned_key(event['board'], current, identity, time.time()) is not None:
                    return True, 'owned_lease'
                if not offered(current):
                    return False, 'claim_race_lost'
                raise
            current = (await client.ticket_get(event['ticket'], view='full'))['ticket']
            if self.owned_key(event['board'], current, identity, time.time()) is None:
                raise ValueError('event preflight claim was not retained')
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

    def _run(self, argv, **kwargs):
        timeout=self.config.get('turn_timeout_s',1800)
        # Validate and refresh every selected membership immediately before the
        # paid turn. This also makes a long turn visible without another model
        # call and fails closed before launch when registry readiness is broken.
        asyncio.run(self.recover_transport(
            'presence_prelaunch',self.refresh_presence,
            'model_not_started; retry the preserved event after transport recovery'))
        process=subprocess.Popen(argv,env=self.environment(),cwd=self.config['seat_dir'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        started=self.monotonic()
        next_presence=started+PRESENCE_INTERVAL_S
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
                    asyncio.run(self.recover_transport(
                        'presence_active_model',self.refresh_presence,
                        'stop_model_after_grace; preserve ticket lease and partial work'))
                    next_presence=self.monotonic()+PRESENCE_INTERVAL_S
                    continue
                if code:
                    raise subprocess.CalledProcessError(code,argv)
                return
        except BaseException:
            self._stop_process(process)
            raise

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
            if self.preflight_enabled and not event.get('recovery_key'):
                authorized, reason = asyncio.run(self.recover_transport(
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
            extension=(
                'pursers: '+shlex.join([c['mcp'],'--central-url',c['central_url'],'--board',event['board'],
                    '--token-file',c['token_file'],'--tools',role,'--repository-root',c['repository_root']])
                if c.get('client', 'goose') == 'goose'
                else 'the seat profile configured Pursers MCP tools'
            )
            instructions = 'Read AGENTS.md and .goosehints.' if c.get('client', 'goose') == 'goose' else 'Read AGENTS.md and START.md.'
            prompt=('$token-thrift. Process exactly one authorized Pursers event, then exit. '
                    f"Seat {c['seat_id']}; role {role}; board_id {event['board']}; ticket_id {event['ticket']}. "
                    f'{instructions} Use this exact board for every operation. '
                    'GET the ticket first and verify exact current ownership. If already held by this seat, resume it; '
                    'otherwise claim or review-claim only an authorized offer. Preserve existing worktrees, changes, '
                    'commits and evidence. Follow its scope, perform work or independent verification, '
                    'record required evidence and submit or review on the exact board_id above. '
                    'Confirm the resulting Central ticket state before exiting. If blocked, request human input '
                    'on that ticket instead of returning silently with a held lease. '
                    'Do not poll. Stop on authentication failure.')
            self.state['last_turn'] = {'board':event['board'], 'ticket':event['ticket'],
                'started_at':now, 'outcome':'started'}
            PUBLISH(self.path,self.state)
            try:
                self.run_command(self.model_command(prompt, extension))
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                self.state['last_turn'].update(outcome='interrupted', error_class=type(exc).__name__)
                self.state['last_turn']['exit_cause'] = (
                    'model_timeout' if isinstance(exc, subprocess.TimeoutExpired)
                    else 'model_process_failed'
                )
            except BaseException as exc:
                self.state['last_turn'].update(outcome='interrupted', error_class=type(exc).__name__)
                raise
            else:
                self.state['last_turn']['outcome'] = 'exited_pending_verification'
            finally:
                self.state['last_turn']['finished_at'] = time.time()
                PUBLISH(self.path,self.state)

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
