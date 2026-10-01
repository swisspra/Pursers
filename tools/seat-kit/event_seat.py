#!/usr/bin/env python3
"""Run bounded Goose work only after relevant registry events; persist cursors."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import subprocess
import sys
import time

HELPERS = runpy.run_path(str(Path(__file__).resolve().parents[1]/'ado-connector/git_credential.py'))
PUBLISH = runpy.run_path(str(Path(__file__).resolve().parents[1]/'board-butler/fleet_observation.py'))['publish']
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$')


def validate_config(config):
    required = {'seat_id','role','provider','model','seat_dir','board_script','state_file','token_file',
                'goose','mcp','central_url','home_board','repository_root'}
    if not required <= config.keys() or config['role'] not in ('worker','reviewer'):
        raise ValueError('event seat configuration is incomplete')
    if not SAFE_ID.fullmatch(config['seat_id']) or not SAFE_ID.fullmatch(config['home_board']):
        raise ValueError('invalid seat or board identity')
    for key in ('seat_dir','board_script','state_file','token_file','goose','mcp','repository_root'):
        if not Path(config[key]).is_absolute(): raise ValueError('runtime paths must be absolute')
    hourly_limit=config.get('max_runs_per_hour',5)
    if hourly_limit is not None and (type(hourly_limit) is not int or not 1 <= hourly_limit <= 100):
        raise ValueError('invalid hourly run limit')
    for key, default, ceiling in (('max_turns',30,100),('turn_timeout_s',1800,3600)):
        value=config.get(key,default)
        if type(value) is not int or not 1 <= value <= ceiling: raise ValueError('invalid turn limit')
    tier = config.get('tier_max',2)
    if type(tier) is not int or tier not in (1,2,3): raise ValueError('invalid seat tier')
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


def transient_wait_failure(exc):
    """Only retry transport failures; never repeat a model execution here."""
    if isinstance(exc, (ConnectionError, TimeoutError, subprocess.TimeoutExpired)):
        return True
    message = str(exc).lower()
    if isinstance(exc, subprocess.CalledProcessError):
        message = str(exc.stderr or "").lower() + " " + str(exc.stdout or "").lower()
    if any(text in message for text in (
        "unauthorized", "forbidden", "authentication", "permission denied", "401", "403",
    )):
        return False
    return any(text in message for text in (
        "connection refused", "all connection attempts failed", "connection reset",
        "server disconnected", "connection closed", "stream closed", "read timed out",
        "connect timeout", "502 bad gateway", "503 service unavailable", "504 gateway timeout",
    ))


class EventSeatRunner:
    def __init__(self, config):
        self.config=validate_config(config)
        self.path=Path(config['state_file'])
        self.state={'cursor':{},'seen':[],'runs':[],'pending':[], 'owned_recoveries':{}}
        if self.path.exists(): self.state.update(json.loads(HELPERS['private_read'](self.path,1048576)))
        self.run_command=self._run

    def environment(self):
        c=self.config
        env={**os.environ,'PURSERS_WAIT_MODE':'push','PURSERS_BOARDS':'registry','PURSERS_PROJECT_BOARD':'',
             'PURSERS_MODEL':c['model'],'PURSERS_PROVIDER':c['provider'],
             'GOOSE_MODEL':c['model'],'GOOSE_PROVIDER':c['provider'],'GOOSE_MODE':'auto'}
        env.pop('GOOSE_THINKING_EFFORT',None)
        if c.get('effort'): env['GOOSE_THINKING_EFFORT']=c['effort']
        if c.get('git_credentials_config'):
            helper='!'+shlex.join([sys.executable,str(Path(__file__).resolve().parents[1]/'ado-connector/git_credential.py'),
                                   '--config',c['git_credentials_config']])
            env.update(GIT_CONFIG_COUNT='3',GIT_CONFIG_KEY_0='credential.helper',GIT_CONFIG_VALUE_0='',
                GIT_CONFIG_KEY_1='credential.helper',GIT_CONFIG_VALUE_1=helper,
                GIT_CONFIG_KEY_2='credential.useHttpPath',GIT_CONFIG_VALUE_2='true',GIT_TERMINAL_PROMPT='0')
        return env

    def _run(self, argv, **kwargs):
        subprocess.run(argv,env=self.environment(),cwd=self.config['seat_dir'],check=True,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=self.config.get('turn_timeout_s',1800))

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
            runs=[r for r in self.state['runs'] if now-r<3600]
            limit=self.config.get('max_runs_per_hour',5)
            if limit is not None and len(runs)>=limit:
                self.state['runs']=runs
                self.state['rate_limited_until']=min(runs)+3600
                PUBLISH(self.path,self.state)
                return max(0,self.state['rate_limited_until']-now)
            self.state.pop('rate_limited_until',None)
            event=pending.pop(0)
            if event.get('recovery_key'):
                key = event['recovery_key']
                self.state['owned_recoveries'][key] = self.state['owned_recoveries'].get(key, 0) + 1
            self.state['runs']=(runs+[now])[-200:]
            self.state['seen']=(self.state['seen']+[event['marker']])[-200:]
            PUBLISH(self.path,self.state)  # reserve before a potentially uncertain model execution
            c=self.config
            extension='pursers: '+shlex.join([c['mcp'],'--central-url',c['central_url'],'--board',event['board'],
                '--token-file',c['token_file'],'--tools',role,'--repository-root',c['repository_root']])
            prompt=('$token-thrift. Process exactly one authorized Pursers event, then exit. '
                    f"Seat {c['seat_id']}; role {role}; board_id {event['board']}; ticket_id {event['ticket']}. "
                    'Read AGENTS.md and .goosehints. Use this exact board for every operation. '
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
                self.run_command([c['goose'],'run','--no-session','--no-profile','--with-builtin','developer',
                    '--with-extension',extension,'--provider',c['provider'],'--model',c['model'],
                    '--max-turns',str(c.get('max_turns',30)),'--text',prompt])
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
        return BoardClient(c['central_url'],token,board,agent_name=c['seat_id'],role=c['role'],
            capabilities={'can_work':c['role']=='worker','can_review':c['role']=='reviewer',
                          'tier_max':c.get('tier_max',2),'max_parallel':1},
            allow_takeover=True, renewal_source='keepalive')

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
        failures=0
        while True:
            try:
                asyncio.run(self.bootstrap())
                asyncio.run(self.reconcile_owned())
            except Exception as exc:
                if not transient_wait_failure(exc): raise
                failures+=1
                delay=min(60,5*2**min(failures-1,4))
                print(f"event-seat: transport unavailable; reconnect in {delay}s",file=sys.stderr)
                time.sleep(delay)
                continue
            if self.state['pending']:
                delay=self.process({'new_seq':self.state['cursor'],'events':[]},time.time())
                if delay is not None and delay>0:
                    time.sleep(min(60,delay))
                continue
            command=[self.config['board_script'],'wait','--since',json.dumps(self.state['cursor']),
                     '--timeout','270','--boards',','.join(self.active_boards)]
            if self.config['role']=='reviewer':command.insert(2,'--submitted')
            try:
                result=subprocess.run(command,env=self.environment(),cwd=self.config['seat_dir'],
                                      capture_output=True,text=True,check=True,timeout=300)
            except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
                if not transient_wait_failure(exc): raise
                failures+=1
                delay=min(60,5*2**min(failures-1,4))
                print(f"event-seat: transport unavailable; reconnect in {delay}s",file=sys.stderr)
                time.sleep(delay)
                continue
            failures=0
            self.process(json.loads(result.stdout),time.time())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args()
    EventSeatRunner(json.loads(HELPERS['private_read'](args.config))).run()


if __name__=='__main__': main()
