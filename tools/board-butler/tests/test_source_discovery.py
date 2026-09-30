"""Live inventories, explicit exceptions, and no guessing across repositories."""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


def load():
    path = Path(__file__).parents[1] / 'source_discovery.py'
    spec = importlib.util.spec_from_file_location('discovery_under_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def repo(name='api', project='Team', **extra):
    return {'id': project + '-' + name, 'name': name, 'project': {'name': project},
            'remoteUrl': f'https://dev.azure.com/org/{project}/_git/{name}',
            'defaultBranch': 'refs/heads/dev', **extra}


def test_unique_names_and_explicit_aliases_require_accessible_enabled_repository():
    m = load()
    projects = [{'key': 'org_api', 'name': 'API'}, {'key': 'org_admin-backend', 'name': 'Admin Backend'}]
    repos = [repo(), repo('admin')]
    aliases = {'org_admin-backend': {'repository_url': repos[1]['remoteUrl'], 'integration_ref': 'dev'}}
    result = m.match_projects(projects, repos, aliases)
    assert set(result.repositories) == {'org_api', 'org_admin-backend'}
    assert result.repositories['org_api']['integration_ref'] == 'dev'
    assert result.repositories['org_admin-backend']['repository_url'] == repos[1]['remoteUrl']
    denied = m.match_projects(projects, [repo(isDisabled=True)], aliases)
    assert denied.repositories == {}
    assert len(denied.findings) == 2


def test_ambiguous_repo_names_and_unknown_aliases_are_not_guessed():
    m = load()
    result = m.match_projects([{'key': 'org_api', 'name': 'API'}], [repo(project='One'), repo(project='Two')], {})
    assert result.repositories == {}
    assert result.findings[0]['reason_code'] == 'ambiguous_repository'


def test_inventory_discovers_new_project_on_next_refresh_and_pages_sonar():
    m = load()
    calls = []
    async def read(tool, args):
        calls.append((tool, args))
        if tool == m.SONAR_PROJECTS:
            page = args['pageIndex']
            return {'components': [{'key': 'org_api', 'name': 'API'}] if page == 1 else [{'key': 'org_new', 'name': 'new'}], 'paging': {'total': 2}}
        if tool == m.ADO_PROJECTS:
            return {'value': [{'name': 'Team'}]}
        return {'value': [repo(), repo('new')]}
    result = asyncio.run(m.discover(read, {}, page_size=1))
    assert set(result.repositories) == {'org_api', 'org_new'}
    assert len([x for x in calls if x[0] == m.SONAR_PROJECTS]) == 2


def test_incomplete_inventory_fails_instead_of_publishing_partial_scope():
    m = load()
    async def read(tool, args):
        if tool == m.SONAR_PROJECTS:
            return {'components': [{'key': 'org_api', 'name': 'API'}], 'paging': {'total': 99}}
        return {'value': []}
    with pytest.raises(ValueError, match='incomplete|repeated'):
        asyncio.run(m.discover(read, {}, max_pages=2, page_size=1))


def test_onboarding_bootstraps_nonworking_identity_before_coordinator_join(monkeypatch):
    from types import SimpleNamespace
    from test_source_intake import butler
    import pursers_client
    entered = []
    calls = []
    class Client:
        def __init__(self, url, token, board, **kwargs):
            self.kwargs = kwargs
            self.agent_name = kwargs['agent_name']
        async def __aenter__(self):
            entered.append(self.kwargs)
            if self.kwargs['role'] == 'coordinator':
                raise PermissionError('principal is not a member')
            return self
        async def __aexit__(self, *args): pass
        async def board_onboard(self, **kwargs):
            calls.append(('board_onboard', kwargs))
        async def _call(self, name, args):
            calls.append((name, args))
            return {'dispatch_policy': {'offer_ttl_s': 240}}
    monkeypatch.setattr(pursers_client, 'BoardClient', Client)
    backend = butler.CentralBackend(SimpleNamespace(url='http://example.invalid/mcp',home_board='home',agent_name='butler'), 'unused')
    adapter = butler.CentralProjectRegistry(backend)
    asyncio.run(adapter.ensure_board('new-board', 'work', 2))
    assert entered[0]['capabilities']['can_work'] is False
    assert entered[0]['capabilities']['can_review'] is False
    assert calls[0][1]['role'] == 'coordinator'
    assert calls[-1][1]['default_ticket_tier'] == 2
    assert calls[-1][1]['offer_ttl_s'] == 240


def test_real_central_bootstrap_and_member_admission(tmp_path, monkeypatch):
    """Exercise Central authorization, not only a mock's accepted call sequence."""
    from types import SimpleNamespace
    import contextlib
    from test_source_intake import butler
    central_path=Path(__file__).resolve().parents[3]/'packages/central/src/pursers_central'
    monkeypatch.syspath_prepend(str(central_path))
    import central
    jwks=tmp_path/'jwks.json';jwks.write_text('{"keys":[]}')
    for k,v in {'CENTRAL_AUTH_MODE':'jwt','CENTRAL_JWT_ISSUER':'https://issuer.invalid',
                'CENTRAL_JWT_AUDIENCE':'http://localhost:8765/mcp','CENTRAL_JWKS_PATH':str(jwks),
                'CENTRAL_ADMISSION':'invite','STORE_BACKEND':'sqlite'}.items():monkeypatch.setenv(k,v)
    server,service=central.build_server('localhost',8765,tmp_path/'data')
    principal=central.Principal('PR-butler','butler',frozenset({'board:read','board:write','board:coordinate'}))
    monkeypatch.setattr(central,'current_principal',lambda:principal)
    class Client:
        agent_name='butler'
        async def _call(self,name,args):
            result=await server.call_tool(name,{'board_id':'new-board',**args})
            assert not result.is_error
            return result.structured_content
        async def board_onboard(self,**args):return await self._call('board_onboard',{'agent_name':self.agent_name,**args})
    client=Client()
    @contextlib.asynccontextmanager
    async def factory(board,*,onboarding=False):
        await client._call('board_join',{'agent_name':'butler','role':'worker' if onboarding else 'coordinator',
                                        'capabilities':dict(butler.BOARD_BUTLER_CAPABILITIES),'allow_takeover':True})
        yield client
    adapter=butler.CentralProjectRegistry(SimpleNamespace(_client_for_board=factory))
    async def run():
        await adapter.ensure_board('new-board','work',2)
        await adapter.ensure_members('new-board',{'PR-worker':'member','PR-reviewer':'reviewer','PR-intake':'member'})
        result=await client._call('board_members',{})
        assert {m['principal_id']:m['role'] for m in result['members']}=={
            'PR-butler':'admin','PR-worker':'member','PR-reviewer':'reviewer','PR-intake':'member'}
        assert (await client._call('board_status',{}))['dispatch_policy']['default_ticket_tier']==2
    asyncio.run(run())
