from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SPEC = importlib.util.spec_from_file_location(
    'decline_ui_dashboard', Path(__file__).parents[1] / 'fleet_dashboard.py'
)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


def javascript(*prefixes: str) -> str:
    lines = dashboard.UI_ASSETS['/ui/assets/app.js'][1].decode().splitlines()
    return '\n'.join(next(line for line in lines if line.startswith(prefix)) for prefix in prefixes)


def run_node(program: str):
    result = subprocess.run(['node', '-e', program], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_approved_intake_keeps_decline_without_second_approval():
    source = javascript('function intakePanel(')
    result = run_node(source + '''
const esc = value => String(value ?? ''); const fmt = esc;
const intakeKey = () => 'queue'; const intakeDraft = () => ({title:'Draft',category:'docs'});
const recentIntake = new Map(); const intakeQueues = new Map();
const ask = {id:'ask-1',text:'Obsolete request',approved:true};
intakeQueues.set('queue',{waiting:[ask]});
const approved = intakePanel({board:{label:'Lab'}},{central:'one',board:'lab'});
intakeQueues.set('queue',{waiting:[{...ask,approved:false}]});
const pending = intakePanel({board:{label:'Lab'}},{central:'one',board:'lab'});
intakeQueues.set('queue',{waiting:[],declined:[ask]});
const declined = intakePanel({board:{label:'Lab'}},{central:'one',board:'lab'});
console.log(JSON.stringify({approved,pending,declined}));
''')
    assert 'data-intake-action="decline"' in result['approved']
    assert 'data-intake-action="approve"' not in result['approved']
    assert 'data-intake-action="approve"' in result['pending']
    assert 'data-intake-action="decline"' not in result['declined']


@pytest.mark.parametrize('queued, conflict', [(True, False), (False, False), (True, True)])
def test_attention_decline_reads_fresh_queue_and_never_posts_for_consumed_ask(queued, conflict):
    source = javascript('async function declineAttentionIntake(')
    result = run_node(source + f'''
const queued = {json.dumps(queued)}; const conflict = {json.dumps(conflict)};
const calls=[];const status={{textContent:'',className:''}};
const button={{disabled:false,dataset:{{central:'one',board:'lab',askId:'ask-1'}},closest:()=>({{querySelector:()=>status}})}};
const apiCentral = central => 'central='+encodeURIComponent(central);
const fetchJson = async url => {{calls.push({{method:'GET',url}});return {{waiting:queued?[{{id:'ask-1',approved:true}}]:[],expected_sha256:'fresh-digest'}}}};
const fetch = async (url,options) => {{calls.push({{method:options.method,url,body:JSON.parse(options.body)}});return {{ok:!conflict,json:async()=>({{error:'Queue changed; refresh before retrying.'}})}}}};
const refreshFleet = async () => {{calls.push({{method:'refresh'}})}};
const renderHub = () => {{}};
(async()=>{{await declineAttentionIntake(button);console.log(JSON.stringify({{calls,status,disabled:button.disabled}}));}})();
''')
    assert result['calls'][0] == {'method': 'GET', 'url': '/api/intake?central=one&board_id=lab'}
    posts = [c for c in result['calls'] if c['method'] == 'POST']
    if queued:
        assert posts[0]['body'] == {'board_id': 'lab', 'ask_id': 'ask-1', 'action': 'decline', 'expected_sha256': 'fresh-digest'}
        if conflict:
            assert 'Queue changed' in result['status']['textContent']
            assert not result['disabled']
            assert not any(c['method'] == 'refresh' for c in result['calls'])
    else:
        assert not posts
        assert 'no longer queued' in result['status']['textContent']
        assert not result['disabled']


def test_attention_candidates_keep_separate_intake_ids_and_exclude_system_actions():
    result = run_node(javascript('function attentionCandidates(') + """
const hubOverhead = {};
const fleetData = {one:{boards:[{board_id:'lab',coordinator_findings:{items:[
 {kind:'intake-pending',ask_id:'ask-1'},
 {kind:'intake-pending',ask_id:'ask-2'},
 {kind:'intake-created',ask_id:'ask-3'},
 {kind:'butler_observation',ask_id:'not-intake'},
]}}]}};
console.log(JSON.stringify(attentionCandidates()));
""")
    assert [item.get('ask_id') for item in result] == ['ask-1', 'ask-2', None, None]
    assert len({item['key'] for item in result}) == 4
