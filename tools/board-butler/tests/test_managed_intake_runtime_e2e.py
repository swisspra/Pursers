"""Real Central intake/independent approval, with external MCP/model boundaries faked."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

from test_managed_intake import _delivery_setup, _issue
from test_source_intake import NOW, butler


def test_native_resident_central_intake_approval_and_empty_restart(tmp_path, monkeypatch):
    root=Path(__file__).resolve().parents[3]
    sys.path.insert(0,str(root/'packages/central/src/pursers_central'))
    import central as central_server
    jwks=tmp_path/'jwks.json';jwks.write_text('{"keys":[]}')
    for key,value in {'CENTRAL_AUTH_MODE':'jwt','CENTRAL_JWT_ISSUER':'https://issuer.example',
        'CENTRAL_JWT_AUDIENCE':'http://localhost:8765/mcp','CENTRAL_JWKS_PATH':str(jwks),
        'CENTRAL_ADMISSION':'invite','STORE_BACKEND':'sqlite'}.items():monkeypatch.setenv(key,value)
    mcp,_=central_server.build_server('localhost',8765,tmp_path/'central')
    principal=lambda name:central_server.Principal('PR-'+name,name,frozenset({'board:read','board:write','board:review','board:coordinate','board:intake'}))
    admin,worker,reviewer=map(principal,('admin','worker','reviewer'))
    intake=central_server.Principal('PR-intake','intake',frozenset({'board:read','board:coordinate','board:intake'}))
    coordinator=runpy.run_path(str(root/'tools/coordinator/coordinator.py'))
    model_calls=[]; external_calls=[]; count=[1]

    async def call(who,name,**arguments):
        monkeypatch.setattr(central_server,'current_principal',lambda:who)
        result=await mcp.call_tool(name,{'board_id':'board-a',**arguments})
        assert not result.is_error
        return result.structured_content

    class Client:
        def __getattr__(self,name):
            async def invoke(*args,**kwargs):
                if name in {'board_state_get','board_state_update'}:
                    kwargs['key']=args[0]
                    if len(args)>1:kwargs['value']=args[1]
                if name=='ticket_get':kwargs['ticket_id']=args[0]
                if name=='ticket_annotate':kwargs.update(ticket_id=args[0],text=args[1])
                if name in {'board_state_update','ticket_annotate'}:kwargs['agent_name']='admin'
                try:
                    return await call(admin,name,**kwargs)
                except Exception as exc:
                    if name=='ticket_get' and str(exc).endswith('ticket not found'):
                        from pursers_client import BoardClientError
                        raise BoardClientError('ticket not found') from exc
                    raise
            return invoke
    @asynccontextmanager
    async def client_for_board(_board):yield Client()

    async def scenario():
        await call(admin,'board_join',agent_name='admin')
        for who,role in ((worker,'member'),(reviewer,'reviewer'),(intake,'member')):
            await call(admin,'board_member_add',agent_name='admin',principal_id=who.principal_id,role=role)
        await call(intake,'board_join',agent_name='intake',role='coordinator',capabilities={'can_work':False,'can_review':False})
        worker_join=await call(worker,'board_join',agent_name='worker',role='worker',capabilities={'can_work':True,'can_review':False,'tier_max':3,'max_parallel':1})
        await call(reviewer,'board_join',agent_name='reviewer',role='reviewer',capabilities={'can_work':False,'can_review':True,'tier_max':3,'max_parallel':1})
        await call(admin,'board_state_update',agent_name='admin',key='coordinator_intake',value=json.dumps({'schema_version':2,'asks':[],'tombstones':[]}))
        poller,_calls=_delivery_setup(tmp_path)
        poller.index.entries.clear();poller.index.dirty=True;poller.index.save()
        source=replace(poller.sources[0],observation=butler.SourceObservationPolicy('fetch',{},'paging.total'))
        runtime=poller.runtimes[source.connector_id]
        async def external(operation,tool,arguments):
            external_calls.append((tool,arguments))
            if tool=='fetch':document={'paging':{'total':count[0]},'issues':[_issue(1)] if count[0] else []}
            elif tool=='refs':document={'value':[{'name':'refs/'+arguments['filter'],'objectId':'b'*40}]}
            else:document={'pullRequestId':1}
            return SimpleNamespace(payload={'structured_content':document})
        monkeypatch.setattr(runtime,'call_tool',external)
        def backend():
            b=butler.CentralBackend(SimpleNamespace(_connector_runtimes=[runtime],_connector_sources=[source],
                runtime_mode='active',source_intake_index_file=poller.index.path),'unused')
            b._client_for_board=client_for_board
            b.source_intake_poller.registry_projects={'Alpha':'board-a'}
            async def ceiling(_):return 15
            async def project(_):return {'repository_url':'https://dev.azure.com/example-org/example-project/_git/example-repo','integration_ref':'main'}
            b.source_intake_poller.ceiling=ceiling;b.source_intake_poller.project_reader=project
            async def config():return {}
            b.coordinator_config=config
            return b
        async def model(_runtime,context):model_calls.append(context);return {'pull':1}
        monkeypatch.setattr(butler,'resolve_config',lambda *a,**k:None)
        monkeypatch.setattr(butler,'resolve_provider_runtime',lambda *a,**k:butler.ProviderRuntime('https://model.invalid','model','test'))
        monkeypatch.setattr(butler,'decide_intake_with_provider',model)
        b=backend()
        result=await b.source_intake_poller.run_cycle(NOW)
        assert result['new_asks']==1
        state=await call(admin,'board_state_get',key='coordinator_intake')
        created=[]
        async def create(_board,draft):
            result=await call(intake,'ticket_create',agent_name='intake',ticket_id=draft.ticket_id,title=draft.title,
                description=draft.description,scope=draft.scope,target_url=draft.target_url,
                required_fields=list(draft.required_fields),category=draft.category,unassigned=True,coordinator_op_key=draft.op_key)
            created.append(result['ticket']['ticket_id']);return created[-1]
        findings,updates=await coordinator['process_intakes']([coordinator['Project']('Alpha','board-a',tmp_path,domain='work')],
            {'board-a':{'tickets':[],'coordinator_intake_state':state}},NOW,coordinator['RuntimeState'].for_mode('active'),
            enabled=True,dry_run=False,create_ticket=create)
        assert created, findings
        ticket=created[0]
        await call(admin,'ticket_assign',agent_name='admin',ticket_id=ticket,assigned_to_agent_id=worker_join['agent_id'],
            expected_status='open',coordinator_op_key='assign-'+ticket,reason='Fixture assignment')
        await call(worker,'ticket_claim',agent_name='worker',ticket_id=ticket)
        await call(worker,'ticket_submit',agent_name='worker',ticket_id=ticket,summary='Fix verified',
            notes=f'branch_and_commit: pursers/{ticket}@'+ 'b'*40 +'\ncommit_hash: '+ 'b'*40 +'\ntest_output: 1 passed',
            files_changed=['src/example.py'],stay_active=False)
        await call(reviewer,'ticket_review',agent_name='reviewer',ticket_id=ticket,verdict='approve',review_notes='Independent fixture verification')
        approved=await call(admin,'ticket_get',ticket_id=ticket,view='full')
        assert butler._ticket_approved(approved['ticket']), approved
        assert butler._approved_submission(approved['ticket'])[1]=='b'*40, json.dumps({k:v for k,v in approved['ticket'].items() if 'submi' in k or k in {'notes','review_verdict'}})
        count[0]=0
        for restart in range(2):
            b=backend()
            for cycle in range(3):
                result=await b.source_intake_poller.run_cycle(NOW+timedelta(minutes=restart*3+cycle+1))
                assert not result['findings'], (result['findings'], await call(admin,'ticket_get',ticket_id=ticket,view='full'))
                assert result['decision']['reason']=='no_open_issues'
        assert len(model_calls)==1
        assert len([tool for tool,_ in external_calls if tool=='pr_create'])==1
    asyncio.run(scenario())
