import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


def module():
    spec=importlib.util.spec_from_file_location('grouping_test_module',Path(__file__).parents[1]/'source_grouping.py')
    m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m);return m


def issue(i, **extra):
    return {'key':f'I-{i}','project':'org_api','rule':'typescript:S1128','component':f'org_api:src/module/file.ts','line':i,'message':'Remove unused import','updateDate':'r1',**extra}


def test_model_groups_all_ids_once_and_cache_survives_restart(tmp_path):
    m=module();calls=[]
    async def choose(context):
        calls.append(context)
        return {'groups':[{'title':'Remove unused imports','objective':'Clean unused imports','validation':'Run typecheck','issues':[0,1,2]}]}
    path=tmp_path/'groups.json'
    rows=[issue(i) for i in range(3)]
    first=asyncio.run(m.plan_groups(rows,{'project':'org_api','branch':'dev'},choose,path))
    second=asyncio.run(m.plan_groups(list(reversed(rows)),{'project':'org_api','branch':'dev'},choose,path))
    assert len(calls)==1
    assert first==second and len(first)==1
    assert first[0]['member_ids']==['I-0','I-1','I-2']
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('indices',[[0,0,1],[0],[0,1,9]])
def test_invalid_model_partition_never_becomes_work(tmp_path,indices):
    m=module()
    async def choose(_):return {'groups':[{'title':'Fix','objective':'Fix','validation':'Test','issues':indices}]}
    with pytest.raises(ValueError):asyncio.run(m.plan_groups([issue(0),issue(1)],{'project':'org_api'},choose,tmp_path/'cache'))


def test_group_size_and_text_are_bounded_without_losing_members(tmp_path):
    m=module()
    async def choose(_):return {'groups':[{'title':'Cleanup','objective':'Remove unused imports','validation':'Run typecheck','issues':list(range(30))}]}
    groups=asyncio.run(m.plan_groups([issue(i) for i in range(30)],{'project':'org_api'},choose,tmp_path/'cache'))
    assert len(groups)==3
    assert sum(len(g['member_ids']) for g in groups)==30
    assert all(len(g['body'])<=1700 for g in groups)
    assert len({i for g in groups for i in g['member_ids']})==30


from test_managed_intake import (Board, PagedClient, Model, _declaration, _runtime, _source, _poller, butler, NOW)
from datetime import timedelta


class SnapshotClient(PagedClient):
    async def call_tool(self, name, arguments, **kwargs):
        result = await super().call_tool(name, arguments, **kwargs)
        if name == 'fetch':
            return Model(structured_content={'issues':self.pages.get(arguments.get('page',1),[]),
                                             'paging':{'total':sum(map(len,self.pages.values()))}})
        return result


def grouped_poller(tmp_path, board, rows, groups, *, limit=15, planner_calls=None):
    calls=[]
    runtime=_runtime(_declaration(),SnapshotClient({1:rows[:2],2:rows[2:]},calls))
    poller=_poller(board,runtime,[_source(grouping={'kind':'sonar','max_in_flight':limit})],
                   index=butler.SourceIntakeIndex(tmp_path/'index.json'),active=True)
    async def choose(_):
        if planner_calls is not None:planner_calls.append(1)
        return {'groups':[{'title':'Repair imports','objective':'Remove unused imports','validation':'Run typecheck','issues':g} for g in groups]}
    async def scope(*_):return {'project':'alpha','branch':'dev','repository_url':'https://example.invalid/repo'}
    poller.group_choose=choose;poller.group_scope=scope
    return poller,calls


def test_complete_snapshot_groups_before_admission_and_reuses_plan(tmp_path):
    board=Board();planner=[];rows=[issue(i,project='alpha') for i in range(3)]
    poller,calls=grouped_poller(tmp_path,board,rows,[[0,1,2]],planner_calls=planner)
    result=asyncio.run(poller.run_cycle(NOW))
    assert result['new_asks']==1 and len(board.asks())==1
    assert [args['page'] for name,args in calls if name=='fetch']==[1,2]
    assert sum(poller.index.in_flight().values())==1
    assert all(f'I-{i}' in board.asks()[0]['text'] for i in range(3))
    restarted,_=grouped_poller(tmp_path,board,rows,[[0,1,2]],planner_calls=planner)
    assert asyncio.run(restarted.run_cycle(NOW+timedelta(minutes=1)))['new_asks']==0
    assert len(planner)==1


@pytest.mark.parametrize('same_file,limit',[(True,15),(False,1)])
def test_file_overlap_and_group_cap_stop_parallel_admission(tmp_path,same_file,limit):
    board=Board()
    rows=[issue(i,project='alpha',component=f'alpha:src/{0 if same_file else i}.ts') for i in range(3)]
    poller,_=grouped_poller(tmp_path,board,rows,[[0],[1],[2]],limit=limit)
    assert asyncio.run(poller.run_cycle(NOW))['new_asks']==1
    assert asyncio.run(poller.run_cycle(NOW+timedelta(minutes=1)))['new_asks']==0


def test_cas_failure_replays_prepared_group_without_losing_members(tmp_path):
    board=Board();planner=[];rows=[issue(i,project='alpha') for i in range(3)]
    poller,_=grouped_poller(tmp_path,board,rows,[[0,1,2]],planner_calls=planner)
    async def fail(*_):raise RuntimeError('CAS conflict')
    poller.state_writer=fail
    with pytest.raises(RuntimeError):asyncio.run(poller.run_cycle(NOW))
    assert len(board.asks())==0
    assert {e['status'] for e in poller.index.entries.values()}=={'prepared'}
    restarted,_=grouped_poller(tmp_path,board,rows,[[0,1,2]],planner_calls=planner)
    assert asyncio.run(restarted.run_cycle(NOW+timedelta(minutes=1)))['new_asks']==1
    assert len(board.asks())==1 and len(planner)==1


def test_incomplete_snapshot_never_calls_planner_or_creates_ask(tmp_path):
    board=Board();planner=[];rows=[issue(i,project='alpha') for i in range(3)]
    poller,_=grouped_poller(tmp_path,board,rows,[[0,1,2]],planner_calls=planner)
    from dataclasses import replace
    poller.sources=(replace(poller.sources[0],max_pages=1),)
    result=asyncio.run(poller.run_cycle(NOW))
    assert result['new_asks']==0 and not board.asks() and not planner
    assert result['findings'][0]['kind']=='source-grouping-unavailable'


def test_total_canary_cap_survives_completion(tmp_path):
    from dataclasses import replace
    board=Board();rows=[issue(i,project='alpha',component=f'alpha:src/{i}.ts') for i in range(3)]
    poller,_=grouped_poller(tmp_path,board,rows,[[0],[1],[2]])
    poller.sources=(replace(poller.sources[0],grouping={'kind':'sonar','max_admitted_groups':1}),)
    assert asyncio.run(poller.run_cycle(NOW))['new_asks']==1
    for key in poller.index.entries:poller.index.set_status(key,'delivered')
    poller.index.save()
    assert asyncio.run(poller.run_cycle(NOW+timedelta(minutes=1)))['new_asks']==0
    assert len(board.asks())==1


def test_group_writeback_contains_every_issue_once(tmp_path):
    import json
    from dataclasses import replace
    from test_managed_intake import _delivery_setup
    poller,calls=_delivery_setup(tmp_path)
    source=poller.sources[0]
    poller.sources=(replace(source,writeback=replace(source.writeback,arg_template={**source.writeback.arg_template,'description':'Issues: {issue_ids}'})),)
    item=poller.index.get('sonar','one');item['member_ids']=json.dumps(['I-1','I-2','I-3'])
    poller.index.put('sonar','one',item);poller.index.save()
    assert asyncio.run(poller.run_cycle(NOW))['writebacks']==1
    assert [args['description'] for name,args in calls if name=='pr_create']==['Issues: I-1, I-2, I-3']
    assert asyncio.run(poller.run_cycle(NOW+timedelta(minutes=1)))['writebacks']==0


def test_source_findings_can_be_bounded_without_preserved_question():
    state={'findings':[{'kind':'unknown_project','item_id':str(i),'detail':'x'*400} for i in range(30)]}
    result=butler._bound_control_state(state)
    import json
    assert len(json.dumps(result,sort_keys=True,separators=(',',':')))<=butler.MAX_STATE_CHARS
    assert result['findings'] and result['truncation']['findings']>0


def test_reached_canary_cap_skips_reads_and_model_but_runs_delivery(tmp_path):
    from dataclasses import replace
    board=Board();rows=[issue(i,project='alpha') for i in range(2)]
    poller,calls=grouped_poller(tmp_path,board,rows,[[0,1]])
    poller.sources=(replace(poller.sources[0],grouping={'kind':'sonar','max_admitted_groups':1}),)
    asyncio.run(poller.run_cycle(NOW));before=len(calls);delivered=[]
    async def delivery(_):delivered.append(True);return 0
    poller._writeback_pass=delivery
    result=asyncio.run(poller.run_cycle(NOW+timedelta(minutes=1)))
    assert len(calls)==before and delivered==[True]
    assert result['decision']['model_called'] is False
    assert result['decision']['reason']=='group_capacity_exhausted'


def test_transport_only_changes_do_not_replan(tmp_path):
    m=module();calls=[]
    async def choose(_):
        calls.append(1)
        return {'groups':[{'title':'Clean imports','objective':'Remove unused import','validation':'Build','issues':[0]}]}
    asyncio.run(m.plan_groups([issue(0,impacts={'value':1})],{'project':'org_api','analysis_sha':'a'*40},choose,tmp_path/'cache'))
    asyncio.run(m.plan_groups([issue(0,impacts={'value':1.0})],{'project':'org_api','analysis_sha':'b'*40},choose,tmp_path/'cache'))
    assert calls==[1]


def test_delivered_group_releases_file_for_distinct_issues_after_restart(tmp_path):
    board=Board();planner=[]
    rows=[issue(i,project='alpha') for i in range(3)]
    poller,_=grouped_poller(tmp_path,board,rows,[[0],[1],[2]],planner_calls=planner)
    assert asyncio.run(poller.run_cycle(NOW))['new_asks']==1
    first=next(iter(poller.index.entries))
    poller.index.set_status(first,'delivering');poller.index.save()
    assert asyncio.run(poller.run_cycle(NOW+timedelta(minutes=1)))['new_asks']==0
    poller.index.set_status(first,'delivered');poller.index.save()
    restarted,_=grouped_poller(tmp_path,board,rows,[[0],[1],[2]],planner_calls=planner)
    assert asyncio.run(restarted.run_cycle(NOW+timedelta(minutes=2)))['new_asks']==1
    assert asyncio.run(restarted.run_cycle(NOW+timedelta(minutes=3)))['new_asks']==0
    import json
    admitted=[m for e in restarted.index.entries.values() for m in json.loads(e['member_ids'])]
    assert sorted(admitted)==['I-0','I-1']
    assert len(planner)==1


def test_group_policy_upgrade_replans_once_then_reuses_validated_plan(tmp_path):
    import json
    m=module();calls=[];path=tmp_path/'groups.json'
    async def choose(_):
        calls.append(1)
        return {'groups':[{'title':'Cleanup imports','objective':'Remove unused imports',
                           'validation':'Typecheck','issues':[0,1]}]}
    rows=[issue(0),issue(1)];scope={'project':'org_api','branch':'dev'}
    asyncio.run(m.plan_groups(rows,scope,choose,path))
    saved=json.loads(path.read_text());saved.pop('planner_version',None)
    m.save_private(path,saved)
    asyncio.run(m.plan_groups(rows,scope,choose,path))
    asyncio.run(m.plan_groups(rows,scope,choose,path))
    assert len(calls)==2
