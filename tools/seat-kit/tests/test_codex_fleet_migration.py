from __future__ import annotations

import base64
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "codex_fleet_migration.py"
SPEC = importlib.util.spec_from_file_location("codex_fleet_migration", PATH)
assert SPEC and SPEC.loader
migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)


def fixture(tmp_path: Path):
    repositories=tmp_path/'repositories';repository=repositories/'project';repository.mkdir(parents=True)
    seats=tmp_path/'seats';worker=seats/'worker-a';reviewer=seats/'reviewer-a'
    worker.mkdir(parents=True);reviewer.mkdir()
    credential=tmp_path/'seat.env';credential.write_text('TOKEN=fixture\n');credential.chmod(0o600)
    template=lambda role,root:{'role':role,'principal_id':f'PR-{role}',
        'credential_ref':'credential.seat','repository_root':str(repository),'seat_root':str(root),
        'command':[sys.executable,str(PATH),'--help'],'boards':'registry','capabilities':{
            'can_work':role=='worker','can_review':role=='reviewer','tier_max':2,'max_parallel':1}}
    config={'schema':'pursers_fleet_executor_config_v1','authorization_fingerprint_sha256':'a'*64,
        'caller_keys':{'butler':base64.b64encode(b'x'*32).decode()},
        'templates':{'worker':template('worker',worker),'reviewer':template('reviewer',reviewer)},
        'credential_paths':{'credential.seat':str(credential)},'repository_roots':[str(repositories)],
        'seat_roots':[str(seats)],'board_caps':{'pursers':2},'host_cap':4}
    executor=tmp_path/'executor.json';executor.write_text(json.dumps(config));executor.chmod(0o600)
    local=tmp_path/'local.json';local.write_text(json.dumps({'templates':{
        'worker':{'board_id':'pursers','provider':'codex','seat_id':'mong1-worker-14','enabled':True},
        'reviewer':{'board_id':'pursers','provider':'codex','seat_id':'mong1-reviewer-14','enabled':False}},'providers':{}}));local.chmod(0o600)
    expiry=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
    lease=tmp_path/'leases.json';lease.write_text(json.dumps({'boards':{'pursers':{'seats':{
        'mong1-worker-14':{'stale_after':expiry,'work':False,'review':False},
        'mong1-reviewer-14':{'stale_after':expiry,'work':False,'review':False}}}}}));lease.chmod(0o600)
    supervisor=tmp_path/'supervise.sh';supervisor.write_text('#!/bin/sh\n');supervisor.chmod(0o700)
    pidfile=tmp_path/'supervisor.pid';pidfile.write_text('100\n')
    marker=tmp_path/'controller.json'
    spec={'schema':migration.SCHEMA,'executor_config':str(executor),'local_config':str(local),
        'lease_snapshot':str(lease),'legacy_supervisor_command':str(supervisor),
        'legacy_supervisor_pid_file':str(pidfile),'controller_marker':str(marker)}
    spec_path=tmp_path/'migration.json';spec_path.write_text(json.dumps(spec));spec_path.chmod(0o600)
    ps=(f'100 1 /bin/sh {supervisor}\n'
        f'200 100 /PATH/TO/node /PATH/TO/codex.js exec -m model -C {worker} prompt\n'
        f'201 200 /PATH/TO/codex-rust exec -m model -C {worker} prompt\n')
    return spec_path,lease,marker,ps,worker


def test_preview_confirm_is_non_mutating_idempotent_and_preserves_disabled_seat(tmp_path):
    spec,_,marker,ps,_=fixture(tmp_path);plan_path=tmp_path/'plan.json'
    plan=migration.preview(spec,plan_path,process_source=lambda:ps)
    assert plan['destructive_ready'] is True
    assert plan['inventory']['managed_process_count']==1
    assert next(row for row in plan['inventory']['seats'] if row['seat_id']=='mong1-worker-14')['child_process_count']==1
    assert next(row for row in plan['inventory']['seats'] if row['seat_id']=='mong1-reviewer-14')['cutover_action']=='remain_disabled'
    assert not marker.exists()
    result=migration.confirm(plan_path,plan['confirmation'],process_source=lambda:ps)
    assert result['live_actions']==[] and result['duplicate'] is False
    assert json.loads(marker.read_text())['disabled_seats']==['mong1-reviewer-14']
    assert migration.confirm(plan_path,plan['confirmation'],process_source=lambda:ps)['duplicate'] is True


@pytest.mark.parametrize('fault,reason', [('duplicate','duplicate_process:worker'),('lease','live_lease:worker')])
def test_preview_blocks_duplicate_process_or_live_holder(tmp_path,fault,reason):
    spec,lease,_,ps,worker=fixture(tmp_path)
    if fault=='duplicate':ps+=f'300 100 /PATH/TO/codex exec -C {worker} prompt\n'
    else:
        value=json.loads(lease.read_text());value['boards']['pursers']['seats']['mong1-worker-14']['work']=True
        lease.write_text(json.dumps(value));lease.chmod(0o600)
    plan=migration.preview(spec,tmp_path/'plan.json',process_source=lambda:ps)
    assert plan['destructive_ready'] is False
    assert reason in plan['inventory']['blockers']
    with pytest.raises(migration.MigrationError,match='migration_blocked'):
        migration.confirm(tmp_path/'plan.json',plan['confirmation'],process_source=lambda:ps)


def test_confirmation_refuses_changed_process_inventory(tmp_path):
    spec,_,_,ps,worker=fixture(tmp_path);path=tmp_path/'plan.json'
    plan=migration.preview(spec,path,process_source=lambda:ps)
    changed=ps+f'300 100 /PATH/TO/codex exec -C {worker} prompt\n'
    with pytest.raises(migration.MigrationError,match='inventory_changed'):
        migration.confirm(path,plan['confirmation'],process_source=lambda:changed)


def test_preview_uses_each_seats_bound_board_and_ignores_non_codex_exec(tmp_path):
    spec,lease,_,ps,worker=fixture(tmp_path)
    value=json.loads(lease.read_text())
    value['boards']['another-work-board']={'seats':{}}
    lease.write_text(json.dumps(value));lease.chmod(0o600)
    ps+=f'300 100 /PATH/TO/python exec -C {worker} prompt\n'
    plan=migration.preview(spec,tmp_path/'plan.json',process_source=lambda:ps)
    assert plan['destructive_ready'] is True
    assert plan['inventory']['managed_process_count']==1


def test_preview_counts_node_and_rust_chains_without_parsing_quoted_prompts(tmp_path):
    spec,_,_,ps,worker=fixture(tmp_path)
    reviewer=tmp_path/'seats/reviewer-a'
    ps=ps.replace(' prompt\n', " worker's prompt\n")
    ps+=(f'300 100 /PATH/TO/node /PATH/TO/codex.js exec -m model -C {reviewer} '
        "reviewer's prompt isn't launch flags -C /unrelated\n"
        f'301 300 /PATH/TO/codex-rust exec -m model -C {reviewer} '
        "reviewer's prompt isn't launch flags -C /unrelated\n")
    plan=migration.preview(spec,tmp_path/'plan.json',process_source=lambda:ps)
    seats={row['seat_id']:row for row in plan['inventory']['seats']}
    assert plan['inventory']['managed_process_count']==2
    assert seats['mong1-worker-14']['process_count']==1
    assert seats['mong1-worker-14']['child_process_count']==1
    assert seats['mong1-reviewer-14']['process_count']==1
    assert seats['mong1-reviewer-14']['child_process_count']==1


def test_preview_fails_closed_when_codex_prefix_is_ambiguous(tmp_path):
    spec,_,_,ps,_=fixture(tmp_path)
    ps+='300 100 /PATH/TO/codex exec -m "unterminated\n'
    plan=migration.preview(spec,tmp_path/'plan.json',process_source=lambda:ps)
    assert plan['destructive_ready'] is False
    assert 'codex_process_inventory_ambiguous' in plan['inventory']['blockers']
