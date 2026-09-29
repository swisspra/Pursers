import json
import runpy
from pathlib import Path

import pytest


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
    with pytest.raises(ValueError,match='budget'): resumed.process(response,104)


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
                'board_id':'project','work_dir':str(tmp_path),'status':'active','domain':'work'}}})}}
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
