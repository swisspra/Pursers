import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('delivery_settings', Path(__file__).parents[1] / 'delivery_settings.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def registry():
    return {'schema_version': 1, 'projects': {'sample': {'board_id': 'sample', 'work_dir': '/PATH/TO/repo', 'repository_url': 'https://example.invalid/repo.git', 'integration_ref': 'dev', 'status': 'active', 'private_extra': 'preserve'}}}


def test_public_delivery_settings_exposes_exact_editable_layers_not_only_effective_policy():
    original = registry()
    original['delivery_policy_defaults'] = {'snapshot_branch_prefix': 'global-snap'}
    original['delivery_policy_groups'] = {'backend': {'conflict_policy': 'repair_then_review'}}
    original['projects']['sample'].update(
        delivery_policy_group='backend',
        delivery_policy={'mode': 'branch_only', 'final_pr_target': None},
    )
    row = m.public_delivery_settings(original)[0]
    assert row['delivery_policy']['mode'] == 'branch_only'
    assert row['delivery_policy_layers'] == {
        'global': {'snapshot_branch_prefix': 'global-snap'},
        'groups': {'backend': {'conflict_policy': 'repair_then_review'}},
        'repository': {'mode': 'branch_only', 'final_pr_target': None},
    }


def test_delivery_plan_preserves_unrelated_registry_and_requires_human_promotion():
    plan = m.build_delivery_plan(request={'action': 'delivery', 'name': 'sample', 'delivery_workflow': {'mode': 'integration', 'base_branch': 'dev'}}, registry=registry(), registry_expected_sha256='a'*64, actor='operator', central='default', observation={'complete': True, 'active_tickets': [], 'pending_offers': []}, refs={'dev': 'a'*40, 'prd': 'b'*40})
    assert not plan['blocked']
    assert plan['proposed_entry']['private_extra'] == 'preserve'
    assert plan['proposed_entry']['integration_ref'] == 'pursers-integration'
    assert plan['proposed_entry']['delivery_workflow']['base_branch'] == 'dev'
    assert plan['create_branch'] == {'name': 'pursers-integration', 'base_sha': 'a'*40}
    assert registry()['projects']['sample']['integration_ref'] == 'dev'


def test_routing_change_blocks_active_work():
    plan = m.build_delivery_plan(request={'action': 'delivery', 'name': 'sample', 'delivery_workflow': {'mode': 'integration', 'base_branch': 'dev'}}, registry=registry(), registry_expected_sha256='a'*64, actor='operator', central='default', observation={'complete': True, 'active_tickets': [{'id': 'TK-live'}], 'pending_offers': []}, refs={'dev': 'a'*40, 'prd': 'b'*40})
    assert plan['blocked']
    assert any('active' in b for b in plan['blockers'])


def test_missing_branch_blocks_without_guessing():
    plan = m.build_delivery_plan(request={'action': 'delivery', 'name': 'sample', 'delivery_workflow': {'mode': 'integration', 'base_branch': 'dev'}}, registry=registry(), registry_expected_sha256='a'*64, actor='operator', central='default', observation={'complete': True, 'active_tickets': [], 'pending_offers': []}, refs={})
    assert plan['blocked']
    assert plan['create_branch'] is None


@pytest.mark.parametrize('existing_ref', ['pursers/task-one', 'Pursers/task-one', 'pursers'])
def test_branch_namespace_collision_cannot_be_applied(existing_ref):
    plan = m.build_delivery_plan(request={'action':'delivery','name':'sample','delivery_workflow':{'mode':'integration','base_branch':'dev','integration_branch':'Pursers'}}, registry=registry(), registry_expected_sha256='a'*64, actor='operator', central='default', observation={'complete':True,'active_tickets':[],'pending_offers':[]}, refs={'dev':'a'*40, existing_ref:'b'*40})
    assert plan['blocked']
    assert any('namespace' in reason for reason in plan['blockers'])


def test_existing_unowned_branch_is_not_adopted():
    plan=m.build_delivery_plan(request={'action':'delivery','name':'sample','delivery_workflow':{'mode':'integration','base_branch':'dev'}}, registry=registry(), registry_expected_sha256='a'*64, actor='operator', central='default', observation={'complete':True,'active_tickets':[],'pending_offers':[]}, refs={'dev':'a'*40,'pursers-integration':'b'*40})
    assert plan['blocked']
    assert any('already exists' in reason for reason in plan['blockers'])


def test_pause_existing_route_is_allowed_while_workers_have_leases():
    original=registry()
    policy={'mode':'integration','base_branch':'dev'}
    original['projects']['sample'].update(integration_ref='pursers-integration', delivery_workflow=m.parse_delivery_workflow(policy))
    plan=m.build_delivery_plan(request={'action':'delivery','name':'sample','delivery_workflow':{**policy,'collection_paused':True}}, registry=original, registry_expected_sha256='a'*64, actor='operator', central='default', observation={'complete':True,'active_tickets':[{'id':'TK-live'}],'pending_offers':[]}, refs={'dev':'a'*40,'pursers-integration':'b'*40})
    assert not plan['blocked']
    assert not m.delivery_route_changed(plan['existing_entry'],plan['proposed_entry'])


def test_create_only_lease_cannot_overwrite_branch_created_after_preview(tmp_path):
    import subprocess
    def git(*args):
        return subprocess.run(['git',*map(str,args)],check=True,capture_output=True,text=True).stdout.strip()
    remote=tmp_path/'remote.git'; local=tmp_path/'local'
    git('init','--bare',remote); git('init','-b','dev',local)
    git('-C',local,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','--allow-empty','-m','base')
    git('-C',local,'remote','add','origin',remote);git('-C',local,'push','origin','dev')
    base=git('-C',local,'rev-parse','HEAD')
    git('-C',local,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','--allow-empty','-m','other')
    other=git('-C',local,'rev-parse','HEAD')
    entry={'work_dir':str(local),'repository_url':str(remote)}
    plan={'existing_entry':entry,'observed_refs':{'dev':base},'create_branch':{'name':'pursers-integration','base_sha':base}}
    def racing_runner(args,**kwargs):
        if 'push' in args:
            git('--git-dir',remote,'update-ref','refs/heads/pursers-integration',other)
        return subprocess.run(args,**kwargs)
    # Ensure the racing writer's object exists in the remote without creating a head.
    git('-C',local,'push','origin',other+':refs/tags/racing-object')
    with pytest.raises(ValueError,match='not confirmed'):
        m.prepare_delivery_branch(plan,runner=racing_runner)
    assert git('--git-dir',remote,'rev-parse','refs/heads/pursers-integration')==other
    assert git('--git-dir',remote,'rev-parse','refs/heads/dev')==base


@pytest.mark.parametrize('failure', ['permission','stale_registry','active_work','none'])
def test_delivery_apply_checks_permissions_and_cas_before_git(monkeypatch, failure):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import fleet_dashboard as dashboard
    original=registry()
    plan=m.build_delivery_plan(request={'action':'delivery','name':'sample','delivery_workflow':{'mode':'integration','base_branch':'dev'},'use_as_default':True}, registry=original, registry_expected_sha256='a'*64, actor='operator', central='default', observation={'complete':True,'active_tickets':[],'pending_offers':[]}, refs={'dev':'a'*40})
    fetcher=object.__new__(dashboard.FleetFetcher)
    fetcher.config=SimpleNamespace(home_board='home')
    fetcher._require_board_admin=AsyncMock(side_effect=PermissionError('denied') if failure=='permission' else None)
    fetcher.fetch_project_registry=AsyncMock(return_value={'registry':original,'expected_sha256':('b' if failure=='stale_registry' else 'a')*64})
    fetcher._delivery_observation=AsyncMock(return_value={'complete':True,'active_tickets':[{'id':'TK-live'}] if failure=='active_work' else [],'pending_offers':[]})
    fetcher.save_project_registry=AsyncMock()
    branch=Mock();monkeypatch.setattr(dashboard,'prepare_delivery_branch',branch)
    if failure!='none':
        with pytest.raises((PermissionError,dashboard.ProjectLifecycleConflictError)):
            asyncio.run(fetcher.apply_project_lifecycle_plan(plan,None))
        branch.assert_not_called();fetcher.save_project_registry.assert_not_called()
    else:
        asyncio.run(fetcher.apply_project_lifecycle_plan(plan,None))
        assert [call.args[0] for call in fetcher._require_board_admin.await_args_list]==['home','sample']
        saved,expected=fetcher.save_project_registry.await_args.args
        assert expected=='a'*64
        assert saved['delivery_defaults']['base_branch']=='dev'
        assert saved['projects']['sample']['private_extra']=='preserve'
        branch.assert_called_once()


@pytest.mark.parametrize('variant', ['empty','active','offer','truncated','missing_count','malformed'])
def test_delivery_observation_uses_complete_nonterminal_scan_not_truncated_history(variant):
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    import fleet_dashboard as dashboard
    rows=[]
    if variant=='active':rows=[{'ticket_id':'TK-active','status':'claimed'}]
    if variant=='offer':rows=[{'ticket_id':'TK-offer','status':'open','dispatch_state':{'state':'offered'}}]
    if variant=='malformed':rows=[{}]
    page={'tickets':rows,'count':len(rows),'total_matching':len(rows),'truncated':False}
    if variant=='truncated':page['total_matching']=501;page['truncated']=True
    if variant=='missing_count':page.pop('total_matching')
    client=SimpleNamespace(ticket_list=AsyncMock(return_value=page),board_snapshot=AsyncMock(return_value={'truncated':True,'omitted_counts':{'tickets':1000}}))
    @asynccontextmanager
    async def connection(board):yield client
    fetcher=object.__new__(dashboard.FleetFetcher);fetcher._client=connection
    result=asyncio.run(fetcher._delivery_observation('sample'))
    assert result['complete'] is (variant not in ('truncated','missing_count','malformed'))
    assert bool(result['active_tickets']) is (variant=='active')
    assert bool(result['pending_offers']) is (variant=='offer')
    client.board_snapshot.assert_not_called()
    client.ticket_list.assert_awaited_once_with(include_closed=False,limit=500,view='work')


def test_new_batch_policy_is_saved_as_draft_and_activation_fails_closed():
    request = {'action': 'delivery', 'scope': 'repository', 'name': 'sample',
               'delivery_policy': {'mode': 'batch_pr', 'mapped_base': 'dev',
                                   'integration_branch': 'pursers-integration',
                                   'snapshot_branch_prefix': 'codex',
                                   'final_pr_target': 'prd',
                                   'release_trigger': {'kind': 'manual'},
                                   'pr_update': 'rolling', 'auto_integrate': False,
                                   'final_merge': 'manual',
                                   'validation': {'test_commands': [], 'required_reviewers': 1,
                                                  'independent_review': True,
                                                  'require_upstream_policies': True},
                                   'conflict_policy': 'pause', 'collection_paused': False}}
    kwargs = dict(registry=registry(), registry_expected_sha256='a'*64, actor='operator',
                  central='default', observation={'complete': True, 'active_tickets': [], 'pending_offers': []},
                  refs={'dev': 'a'*40, 'prd': 'b'*40})
    draft = m.build_delivery_plan(request=request, **kwargs)
    assert not draft['blocked']
    assert draft['proposed_registry']['projects']['sample']['integration_ref'] == 'dev'
    assert draft['affected_projects'][0]['runtime']['ready'] is False
    activated = m.build_delivery_plan(request={**request, 'activate': True}, **kwargs)
    assert activated['blocked']
    assert any('batch_pr runtime' in blocker for blocker in activated['blockers'])
    assert activated['proposed_registry']['projects']['sample']['integration_ref'] == 'dev'


def test_repository_reset_to_inherit_removes_only_repository_override():
    original = registry()
    original['delivery_policy_defaults'] = {'conflict_policy': 'pause'}
    original['projects']['sample']['delivery_policy'] = {'mode': 'branch_only', 'final_pr_target': None}
    plan = m.build_delivery_plan(
        request={'action': 'delivery', 'scope': 'repository', 'name': 'sample', 'reset_to_inherit': True},
        registry=original, registry_expected_sha256='a'*64, actor='operator', central='default',
        observation={'complete': True, 'active_tickets': [], 'pending_offers': []}, refs={'dev': 'a'*40})
    assert not plan['blocked']
    assert 'delivery_policy' not in plan['proposed_registry']['projects']['sample']
    assert plan['proposed_registry']['delivery_policy_defaults'] == {'conflict_policy': 'pause'}


def test_group_policy_preview_lists_only_explicit_group_members():
    original = registry()
    original['projects']['other'] = {**original['projects']['sample'], 'board_id': 'other'}
    original['projects']['sample']['delivery_policy_group'] = 'backend'
    original['delivery_policy_groups'] = {'backend': {}}
    plan = m.build_delivery_plan(
        request={'action': 'delivery', 'scope': 'group', 'name': 'backend',
                 'delivery_policy_group': 'backend',
                 'delivery_policy': {'validation': {'test_commands': ['pytest -q']}}},
        registry=original, registry_expected_sha256='a'*64, actor='operator', central='default',
        observation={'complete': True, 'active_tickets': [], 'pending_offers': []}, refs={})
    assert [item['project'] for item in plan['affected_projects']] == ['sample']
    assert plan['affected_projects'][0]['after']['validation']['test_commands'] == ['pytest -q']


@pytest.mark.parametrize('active', [False, True])
def test_new_policy_apply_rechecks_active_work_before_runtime_activation(monkeypatch, active):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import fleet_dashboard as dashboard
    original = registry()
    original['projects']['sample'].update(
        integration_ref='pursers-integration',
        delivery_workflow=m.parse_delivery_workflow({
            'mode': 'integration', 'base_branch': 'dev',
            'integration_branch': 'pursers-integration'}),
    )
    request = {'action': 'delivery', 'scope': 'repository', 'name': 'sample', 'activate': True,
               'delivery_policy': {'mode': 'per_ticket_pr', 'mapped_base': 'dev',
                                   'integration_branch': 'pursers-integration',
                                   'snapshot_branch_prefix': 'codex', 'final_pr_target': 'dev',
                                   'release_trigger': {'kind': 'ready'}, 'pr_update': 'rolling',
                                   'auto_integrate': False, 'collection_paused': False,
                                   'final_merge': 'manual',
                                   'validation': {'test_commands': [], 'required_reviewers': 1,
                                                  'independent_review': True,
                                                  'require_upstream_policies': True},
                                   'conflict_policy': 'pause'}}
    plan = m.build_delivery_plan(
        request=request, registry=original, registry_expected_sha256='a'*64,
        actor='operator', central='default',
        observation={'complete': True, 'active_tickets': [], 'pending_offers': []},
        refs={'dev': 'a'*40, 'pursers-integration': 'b'*40})
    assert not plan['blocked']
    fetcher = object.__new__(dashboard.FleetFetcher)
    fetcher.config = SimpleNamespace(home_board='home')
    fetcher._require_board_admin = AsyncMock()
    fetcher.fetch_project_registry = AsyncMock(return_value={'registry': original, 'expected_sha256': 'a'*64})
    fetcher._delivery_observation = AsyncMock(return_value={
        'complete': True, 'active_tickets': [{'ticket_id': 'TK-live'}] if active else [],
        'pending_offers': []})
    fetcher.save_project_registry = AsyncMock()
    branch = Mock()
    monkeypatch.setattr(dashboard, 'prepare_delivery_branch', branch)
    if active:
        with pytest.raises(dashboard.ProjectLifecycleConflictError, match='Active work'):
            asyncio.run(fetcher.apply_project_lifecycle_plan(plan, None))
        branch.assert_not_called()
        fetcher.save_project_registry.assert_not_awaited()
    else:
        result = asyncio.run(fetcher.apply_project_lifecycle_plan(plan, None))
        assert result['kind'] == 'project-delivery-policy'
        branch.assert_called_once()
        saved = fetcher.save_project_registry.await_args.args[0]
        assert saved['projects']['sample']['integration_ref'] == 'dev'
        assert saved['projects']['sample']['delivery_workflow']['mode'] == 'direct'
