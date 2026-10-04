import json
import runpy
from pathlib import Path

import pytest


async def no_owned_tickets():
    pass


def api():
    path=Path(__file__).resolve().parents[1]/'event_seat.py'
    assert path.exists(), 'native event seat is missing'
    return runpy.run_path(str(path))


def config(tmp_path):
    return {'seat_id':'worker-a','role':'worker','provider':'test-provider','model':'test-model',
            'seat_dir':str(tmp_path),'board_script':str(tmp_path/'board.sh'),'state_file':str(tmp_path/'state.json'),
            'token_file':str(tmp_path/'token'),'goose':'/example/goose','mcp':'/example/mcp',
            'central_url':'http://localhost:8766/mcp','home_board':'home','repository_root':str(tmp_path/'clones'),
            'max_runs_per_hour':1,'max_turns':30,'turn_timeout_s':1800}


def codex_config(tmp_path):
    value = config(tmp_path)
    value.pop('goose')
    value.pop('mcp')
    value.update(client='codex', codex='/example/codex',
        effort='high', service_tier='fast', codex_sandbox='danger-full-access',
        last_message_file=str(tmp_path/'last.txt'))
    return value


def test_empty_irrelevant_and_repeated_events_do_not_run_model(tmp_path):
    runner=api()['EventSeatRunner'](config(tmp_path))
    calls=[]
    runner.run_command=lambda argv,**kwargs: calls.append(argv)
    response={'new_seq':{'home':42},'events':[]}
    runner.process(response,100)
    response['events']=[{'kind':'offer_expired','board_id':'home','ticket_id':'TK-one'}]
    runner.process(response,101)
    assert calls == []
    response['events']=[{'kind':'ticket_offered','board_id':'home','ticket_id':'TK-one','updated_at':'one'}]
    runner.process(response,102)
    assert len(calls)==1 and '--model' in calls[0] and 'test-model' in calls[0]
    resumed=api()['EventSeatRunner'](config(tmp_path))
    resumed.run_command=lambda *a,**k: pytest.fail('duplicate model call after restart')
    resumed.process(response,103)
    assert resumed.state['cursor']=={'home':42}
    response['events'][0]['ticket_id']='TK-two'
    assert resumed.process(response,104) == 3598
    assert len(resumed.state['pending']) == 1


def test_codex_event_uses_model_and_only_runs_after_offer(tmp_path,monkeypatch):
    monkeypatch.setenv('CODEX_HOME','/example/codex-home')
    runner=api()['EventSeatRunner'](codex_config(tmp_path));calls=[]
    assert runner.environment()['CODEX_HOME']=='/example/codex-home'
    runner.run_command=lambda argv,**kwargs:calls.append(argv)
    runner.process({'new_seq':{'home':42},'events':[]},100)
    assert calls==[]
    runner.process({'new_seq':{'home':43},'events':[
        {'kind':'ticket_offered','board_id':'home','ticket_id':'TK-one','id':'offer-one'}]},101)
    assert len(calls)==1
    command=calls[0]
    assert command[:4]==['/example/codex','exec','-m','test-model']
    assert '--profile' not in command
    assert '--dangerously-bypass-approvals-and-sandbox' in command
    assert command[command.index('-C')+1]==str(tmp_path)
    assert command[command.index('-o')+1]==str(tmp_path/'last.txt')
    assert '--with-extension' not in command
    assert 'model_reasoning_effort="high"' in command
    assert 'service_tier="fast"' in command
    assert 'Read AGENTS.md and START.md.' in command[-1]
    assert 'board_id home' in command[-1]


def test_event_environment_preserves_configured_identity_capabilities(tmp_path,monkeypatch):
    monkeypatch.setenv('PURSERS_HOST','zed')
    worker_config=codex_config(tmp_path);worker_config['skills']=['python','docs','python']
    worker=api()['EventSeatRunner'](worker_config).environment()
    assert worker['PURSERS_HOST']=='codex'
    assert worker['PURSERS_CAN_WORK']=='true'
    assert worker['PURSERS_CAN_REVIEW']=='false'
    assert worker['PURSERS_TIER_MAX']=='2'
    assert worker['PURSERS_SKILLS']=='docs,python'
    reviewer_config=codex_config(tmp_path);reviewer_config['role']='reviewer'
    reviewer=api()['EventSeatRunner'](reviewer_config).environment()
    assert reviewer['PURSERS_CAN_WORK']=='false'
    assert reviewer['PURSERS_CAN_REVIEW']=='true'


def test_long_turn_refreshes_three_boards_and_stops_after_clean_exit(tmp_path,monkeypatch):
    module=api();runner=module['EventSeatRunner'](codex_config(tmp_path))
    runner.active_boards=['home','project-a','project-b']
    refreshes=[]
    async def refresh():refreshes.append(tuple(runner.active_boards))
    runner.refresh_presence=refresh
    clock=iter([0,0,120,120,240,240,240,240])
    runner.monotonic=lambda:next(clock)
    class Process:
        def __init__(self):self.waits=0;self.terminated=False
        def wait(self,timeout=None):
            self.waits+=1
            if self.waits<3:raise module['subprocess'].TimeoutExpired(['model'],timeout)
            return 0
        def poll(self):return 0
        def terminate(self):self.terminated=True
        def kill(self):pytest.fail('clean model exit must not be killed')
    process=Process()
    monkeypatch.setattr(module['subprocess'],'Popen',lambda *a,**k:process)
    runner._run(['model'])
    assert refreshes==[('home','project-a','project-b')]*3
    assert process.terminated is False


def test_presence_failure_terminates_live_model_and_never_renews_forever(tmp_path,monkeypatch):
    module=api();cfg=codex_config(tmp_path);cfg['turn_timeout_s']=300
    runner=module['EventSeatRunner'](cfg);runner.active_boards=['home','project-a','project-b']
    attempts=0
    async def refresh():
        nonlocal attempts
        attempts+=1
        if attempts==2:raise RuntimeError('registry readiness failed')
    runner.refresh_presence=refresh
    clock=iter([0,0,120])
    runner.monotonic=lambda:next(clock)
    class Process:
        terminated=False
        def wait(self,timeout=None):
            if self.terminated:return 0
            raise module['subprocess'].TimeoutExpired(['model'],timeout)
        def poll(self):return None if not self.terminated else 0
        def terminate(self):self.terminated=True
        def kill(self):pytest.fail('cooperative process should terminate')
    process=Process()
    monkeypatch.setattr(module['subprocess'],'Popen',lambda *a,**k:process)
    with pytest.raises(RuntimeError,match='readiness'):
        runner._run(['model'])
    assert attempts==2
    assert process.terminated is True


def test_hung_model_timeout_stops_process_and_presence(tmp_path,monkeypatch):
    module=api();cfg=codex_config(tmp_path);cfg['turn_timeout_s']=240
    runner=module['EventSeatRunner'](cfg);runner.active_boards=['home','project-a','project-b']
    refreshes=[]
    async def refresh():refreshes.append('refresh')
    runner.refresh_presence=refresh
    clock=iter([0,0,120,120,120,240])
    runner.monotonic=lambda:next(clock)
    class Process:
        terminated=False
        def wait(self,timeout=None):
            if self.terminated:return 0
            raise module['subprocess'].TimeoutExpired(['model'],timeout)
        def poll(self):return None if not self.terminated else 0
        def terminate(self):self.terminated=True
        def kill(self):pytest.fail('cooperative process should terminate')
    process=Process()
    monkeypatch.setattr(module['subprocess'],'Popen',lambda *a,**k:process)
    with pytest.raises(module['subprocess'].TimeoutExpired):
        runner._run(['model'])
    assert refreshes==['refresh','refresh']
    assert process.terminated is True


def test_model_crash_stops_presence_loop(tmp_path,monkeypatch):
    module=api();runner=module['EventSeatRunner'](codex_config(tmp_path))
    runner.active_boards=['home','project-a','project-b'];refreshes=[]
    async def refresh():refreshes.append('refresh')
    runner.refresh_presence=refresh
    runner.monotonic=lambda:0
    class Process:
        def wait(self,timeout=None):return 17
        def poll(self):return 17
        def terminate(self):pytest.fail('exited process must not be terminated')
        def kill(self):pytest.fail('exited process must not be killed')
    monkeypatch.setattr(module['subprocess'],'Popen',lambda *a,**k:Process())
    with pytest.raises(module['subprocess'].CalledProcessError) as caught:
        runner._run(['model'])
    assert caught.value.returncode==17
    assert refreshes==['refresh']


def test_model_timeout_is_durable_and_driver_can_continue(tmp_path):
    module=api();cfg=config(tmp_path);cfg['max_runs_per_hour']=None
    runner=module['EventSeatRunner'](cfg)
    runner.run_command=lambda *_a,**_k: (_ for _ in ()).throw(
        module['subprocess'].TimeoutExpired(['model'],240))
    runner.process({'new_seq':{'home':42},'events':[{
        'kind':'ticket_offered','board_id':'home','ticket_id':'TK-one','id':'one'}]},100)
    assert runner.state['pending']==[]
    assert runner.state['last_turn']['outcome']=='interrupted'
    assert runner.state['last_turn']['exit_cause']=='model_timeout'
    resumed=module['EventSeatRunner'](cfg)
    calls=[];resumed.run_command=lambda *args,**kwargs:calls.append(args)
    resumed.process({'new_seq':{'home':43},'events':[{
        'kind':'ticket_offered','board_id':'home','ticket_id':'TK-two','id':'two'}]},101)
    assert len(calls)==1


def test_stale_event_preflight_skips_model_and_preserves_cursor(tmp_path):
    cfg=config(tmp_path);cfg['max_runs_per_hour']=None
    runner=api()['EventSeatRunner'](cfg);runner.active_boards=['home']
    runner.preflight_enabled=True
    async def stale(event, now): return False, 'stale_or_foreign_event'
    runner.event_authorized=stale
    runner.run_command=lambda *_a,**_k:pytest.fail('stale event must not launch model')
    runner.process({'new_seq':{'home':42},'events':[{
        'kind':'ticket_offered','board_id':'home','ticket_id':'TK-closed','id':'old'}]},100)
    assert runner.state['cursor']=={'home':42}
    assert runner.state['pending']==[]
    assert runner.state['last_skip']['reason']=='stale_or_foreign_event'


@pytest.mark.parametrize('role',["worker","reviewer"])
def test_presence_refresh_preserves_role_capabilities_on_every_board(tmp_path,monkeypatch,role):
    import asyncio
    import pursers_client
    from types import SimpleNamespace
    cfg=codex_config(tmp_path);cfg['role']=role
    runner=api()['EventSeatRunner'](cfg);runner.active_boards=['home','project-a','project-b']
    calls=[]
    class Client:
        def __init__(self,_url,_token,board,**kwargs):
            calls.append((board,kwargs));self.board=board
            self.identity=SimpleNamespace(board_id=board,agent_name='worker-a',role=role,
                principal_id='PR-exact')
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(pursers_client,'BoardClient',Client)
    token=tmp_path/'token';token.write_text('fixture');token.chmod(0o600)
    asyncio.run(runner.refresh_presence())
    assert [board for board,_ in calls]==runner.active_boards
    for _,kwargs in calls:
        assert kwargs['agent_platform']=='codex'
        assert kwargs['renewal_source']=='keepalive'
        assert kwargs['capabilities']=={
            'can_work':role=='worker','can_review':role=='reviewer','tier_max':2,
            'max_parallel':1,'skills':[],'host':'codex','model':'test-model',
            'provider':'test-provider'}


def test_presence_refresh_rejects_cross_board_principal_mismatch(tmp_path,monkeypatch):
    import asyncio
    import pursers_client
    from types import SimpleNamespace
    runner=api()['EventSeatRunner'](codex_config(tmp_path))
    runner.active_boards=['home','project-a','project-b']
    class Client:
        def __init__(self,_url,_token,board,**kwargs):
            principal='PR-other' if board=='project-b' else 'PR-exact'
            self.identity=SimpleNamespace(board_id=board,agent_name='worker-a',role='worker',
                principal_id=principal)
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(pursers_client,'BoardClient',Client)
    token=tmp_path/'token';token.write_text('fixture');token.chmod(0o600)
    with pytest.raises(ValueError,match='principal mismatch'):
        asyncio.run(runner.refresh_presence())


def test_codex_named_profile_is_only_used_when_explicitly_configured(tmp_path):
    value=codex_config(tmp_path);value['codex_profile']='configured-profile'
    command=api()['EventSeatRunner'](value).model_command('prompt','unused')
    assert command[command.index('--profile')+1]=='configured-profile'


@pytest.mark.parametrize('change,match', [
    ({'client':'codex'}, 'Codex'),
    ({'client':'other'}, 'unsupported'),
    ({'client':'codex','codex':'/example/codex','codex_profile':'bad profile'}, 'profile'),
    ({'client':'codex','codex':'/example/codex','codex_sandbox':'read-only'}, 'sandbox'),
])
def test_invalid_codex_configuration_fails_closed(tmp_path,change,match):
    value=config(tmp_path)
    value.pop('goose',None);value.pop('mcp',None);value.update(change)
    with pytest.raises(ValueError,match=match):api()['validate_config'](value)


def test_cursor_validation_never_accepts_zero(tmp_path):
    runner=api()['EventSeatRunner'](config(tmp_path))
    with pytest.raises(ValueError,match='cursor'):runner.process({'new_seq':{'home':0},'events':[]},100)
    with pytest.raises(ValueError,match='skipped'):runner.process({'new_seq':{'home':8},'skipped_boards':['x']},100)


@pytest.mark.parametrize("tier_max", [1,2,3])
def test_generation_uses_event_config_for_model_and_provider(tmp_path,tier_max):
    from test_seat_new import args, seat_new
    cfg=config(tmp_path)
    cfg['seat_id']='seat-test'
    cfg['tier_max']=tier_max
    path=tmp_path/'event.json';path.write_text(json.dumps(cfg));path.chmod(0o600)
    parsed=args(tmp_path)
    parsed.name='seat-test'
    parsed.event_config=path
    # Resolve identity before generation; the same validated source feeds runtime.
    assert hasattr(seat_new,'apply_event_config'), 'seat metadata integration is missing'
    seat_new.apply_event_config(parsed)
    assert (parsed.model,parsed.provider)==('test-model','test-provider')
    assert parsed.tier_max == tier_max
    parsed.role='reviewer'
    with pytest.raises(ValueError,match='identity'):seat_new.apply_event_config(parsed)


def test_bootstrap_reads_authoritative_watermarks_only_for_new_boards(tmp_path,monkeypatch):
    import asyncio
    import pursers_client
    cfg=config(tmp_path)
    token=tmp_path/'token';token.write_text('fixture-token');token.chmod(0o600)
    snapshots=[]
    class Client:
        def __init__(self,_url,_token,board,**kwargs):self.board=board
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def board_state_get(self,key):
            return {'state':{'value':json.dumps({'schema_version':1,'projects':{'Example':{
                'board_id':'project','work_dir':str(tmp_path/'clones'/'project'),'status':'active','domain':'work'}}})}}
        async def board_snapshot(self,**kwargs):snapshots.append(self.board);return {'latest_seq':97}
    monkeypatch.setattr(pursers_client,'BoardClient',Client)
    runner=api()['EventSeatRunner'](cfg)
    asyncio.run(runner.bootstrap())
    assert runner.state['cursor']=={'home':97,'project':97}
    resumed=api()['EventSeatRunner'](cfg)
    asyncio.run(resumed.bootstrap())
    assert snapshots==['home','project']


@pytest.mark.parametrize("tier", [True,0,4,"2"])
def test_invalid_event_tier_is_rejected(tmp_path,tier):
    cfg=config(tmp_path);cfg["tier_max"]=tier
    with pytest.raises(ValueError,match="tier"):
        api()["validate_config"](cfg)


def test_transport_reconnect_preserves_cursor_and_only_runs_after_an_offer(tmp_path, monkeypatch):
    import subprocess
    module=api();runner=module['EventSeatRunner'](config(tmp_path))
    runner.state['cursor']={'home':42}
    calls=[];delays=[];waits=[]
    async def bootstrap(): runner.active_boards=['home']
    runner.bootstrap=bootstrap
    runner.reconcile_owned=no_owned_tickets
    runner.run_command=lambda *args,**kwargs:calls.append(args)
    def wait(command,**kwargs):
        waits.append(json.loads(command[command.index('--since')+1]))
        if len(waits)<=2:
            raise subprocess.CalledProcessError(1,command,stderr='ConnectError: All connection attempts failed')
        if len(waits)==3:
            return subprocess.CompletedProcess(command,0,stdout=json.dumps({'new_seq':{'home':43},'events':[
                {'kind':'ticket_offered','board_id':'home','ticket_id':'TK-one','updated_at':'one'}]}))
        raise KeyboardInterrupt
    monkeypatch.setattr(module['subprocess'],'run',wait)
    monkeypatch.setattr(module['time'],'sleep',delays.append)
    with pytest.raises(KeyboardInterrupt): runner.run()
    assert delays==[5,10]
    assert waits==[{'home':42},{'home':42},{'home':42},{'home':43}]
    assert len(calls)==1
    assert runner.state['cursor']=={'home':43}


def test_wait_authentication_failure_stops_without_retry(tmp_path, monkeypatch):
    import subprocess
    module=api();runner=module['EventSeatRunner'](config(tmp_path))
    runner.state['cursor']={'home':42}
    async def bootstrap(): runner.active_boards=['home']
    runner.bootstrap=bootstrap
    runner.reconcile_owned=no_owned_tickets
    def wait(*args,**kwargs):
        raise subprocess.CalledProcessError(1,['wait'],stderr='HTTP 401 Unauthorized')
    monkeypatch.setattr(module['subprocess'],'run',wait)
    monkeypatch.setattr(module['time'],'sleep',lambda _:pytest.fail('auth must not retry'))
    with pytest.raises(subprocess.CalledProcessError): runner.run()
    assert runner.state['cursor']=={'home':42}


def test_distinct_reoffers_without_updated_at_are_not_suppressed(tmp_path):
    cfg=config(tmp_path);cfg['max_runs_per_hour']=5
    runner=api()['EventSeatRunner'](cfg);calls=[]
    runner.run_command=lambda argv,**kwargs:calls.append(argv)
    event={'kind':'ticket_offered','board_id':'home','ticket_id':'TK-one','id':'event-one','seq':42}
    runner.process({'new_seq':{'home':42},'events':[event]},100)
    runner.process({'new_seq':{'home':43},'events':[dict(event,id='event-two',seq=43)]},101)
    assert len(calls)==2
    resumed=api()['EventSeatRunner'](cfg)
    resumed.run_command=lambda *_a,**_k:pytest.fail('replayed reoffer must not run again')
    resumed.process({'new_seq':{'home':43},'events':[dict(event,id='event-two',seq=43)]},102)


def test_disabled_hourly_limit_does_not_stop_after_twenty_runs(tmp_path):
    cfg=config(tmp_path);cfg['max_runs_per_hour']=None
    runner=api()['EventSeatRunner'](cfg);calls=[]
    runner.run_command=lambda *args,**kwargs:calls.append(args)
    for number in range(25):
        runner.process({'new_seq':{'home':42+number},'events':[
            {'kind':'ticket_offered','board_id':'home','ticket_id':f'TK-{number}',
             'id':f'event-{number}'}]},100+number)
    assert len(calls)==25
    assert runner.state['pending']==[]


def test_explicit_hourly_limit_waits_and_resumes_pending_without_replay(tmp_path,monkeypatch):
    module=api();cfg=config(tmp_path);runner=module['EventSeatRunner'](cfg)
    response={'new_seq':{'home':42},'events':[
        {'kind':'ticket_offered','board_id':'home','ticket_id':'TK-one','id':'one'},
        {'kind':'ticket_offered','board_id':'home','ticket_id':'TK-two','id':'two'}]}
    calls=[];runner.run_command=lambda *args,**kwargs:calls.append(args)
    assert runner.process(response,100)==3600
    assert len(calls)==1
    resumed=module['EventSeatRunner'](cfg)
    async def bootstrap():resumed.active_boards=['home']
    resumed.bootstrap=bootstrap
    resumed.reconcile_owned=no_owned_tickets
    resumed.run_command=lambda *args,**kwargs:pytest.fail('must wait for budget')
    monkeypatch.setattr(module['time'],'time',lambda:101)
    def sleep(seconds):
        assert seconds==60
        raise KeyboardInterrupt
    monkeypatch.setattr(module['time'],'sleep',sleep)
    with pytest.raises(KeyboardInterrupt):resumed.run()
    assert len(resumed.state['pending'])==1
    assert resumed.state['cursor']=={'home':42}
    resumed.run_command=lambda *args,**kwargs:calls.append(args)
    resumed.process({'new_seq':{'home':42},'events':[]},3700)
    assert len(calls)==2
    assert resumed.state['pending']==[]


@pytest.mark.parametrize('limit',[0,-1,True,101,'20'])
def test_invalid_hourly_limit_rejected(tmp_path,limit):
    cfg=config(tmp_path);cfg['max_runs_per_hour']=limit
    with pytest.raises(ValueError):api()['validate_config'](cfg)


def test_registry_root_guard_rejects_wrong_boundary_and_symlink_escape(tmp_path):
    module = api()
    root = tmp_path/'clones'; root.mkdir()
    project = root/'project'; project.mkdir()
    registry = {'projects': {'p': {'status': 'active', 'board_id': 'project', 'work_dir': str(project)}}}
    module['validate_registry_roots'](registry, str(root))
    with pytest.raises(ValueError, match='repository_root'):
        module['validate_registry_roots'](registry, str(root/'seat-only'))
    outside = tmp_path/'outside'; outside.mkdir()
    link = root/'escape'; link.symlink_to(outside, target_is_directory=True)
    registry['projects']['p']['work_dir'] = str(link)
    with pytest.raises(ValueError, match='repository_root'):
        module['validate_registry_roots'](registry, str(root))
