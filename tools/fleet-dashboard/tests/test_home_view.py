from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("fleet_home_view", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


def test_home_route_loads_owned_styles_and_keeps_shared_actions() -> None:
    source = dashboard.UI_ASSETS["/ui/views/home.js"][1].decode("utf-8")

    assert "const STYLE_URL = '/ui/views/home.css'" in source
    assert "link.dataset.fleetViewStyle = 'home'" in source
    assert 'data-attention-action="ack"' in source
    assert 'data-attention-action="snooze"' in source
    assert 'href="#/work"' in source
    assert "renderWaitingForYou()" in source


def test_home_styles_cover_hierarchy_responsiveness_and_touch_targets() -> None:
    css = dashboard.UI_ASSETS["/ui/views/home.css"][1].decode("utf-8")

    for contract in (
        ".home-status-grid{display:grid",
        ".home-operational-grid{display:grid",
        ".home-head-actions a{min-height:44px}",
        '.home-health-row[data-tone="danger"]',
        ':root[data-density="compact"] .home-status-grid',
        "@media(max-width:980px){.home-status-grid",
        "@media(max-width:560px){.home-head-actions",
        "@media(forced-colors:active)",
    ):
        assert contract in css


def test_home_render_distinguishes_high_pressure_and_calm_states() -> None:
    registry = dashboard.UI_ASSETS["/ui/view-registry.js"][1].decode("utf-8")
    home = dashboard.UI_ASSETS["/ui/views/home.js"][1].decode("utf-8")
    program = f"""
eval({json.dumps(registry)});
eval({json.dumps(home)});
const esc = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;');
const highData = {{
  north: {{pool_summary:{{busy:2,available:1,stale:1}},agents:[{{
    agent_id:'AI-1',agent_name:'worker-1',pool_status:'working',last_seen:'2030-01-01T00:00:00Z',
    seats:[{{role:'worker',board_id:'alpha',current_ticket_id:'TK-LIVE'}}],
  }}],boards:[{{
    board_id:'alpha',label:'Alpha',human_requests:[{{ticket_id:'TK-HUMAN'}}],
  }}]}},
}};
const attention = [{{
  key:'finding|north|alpha',level:'critical',title:'Evidence stale',
  text:'Refresh the bounded snapshot.',central:'north',board:{{board_id:'alpha',label:'Alpha'}},
  ticket_id:'TK-ATTN',first_seen:'2030-01-01T00:00:00Z',
}}, {{
  key:'intake|north|alpha',level:'warning',title:'Approved intake',text:'Waiting for dispatch.',
  central:'north',board:{{board_id:'alpha',label:'Alpha'}},ask_id:'ask-1',
  first_seen:'2030-01-01T00:00:00Z',
}}];
const base = {{
  esc,fmt:value=>value,centralHref:central=>`#/${{central}}`,
  boardHref:(central,board)=>`#/${{central}}/${{board}}`,
  ticketHref:(central,board,ticket)=>`#/${{central}}/${{board}}/${{ticket}}`,
  pageHead:(kicker,title,copy)=>`<header><b>${{kicker}}</b><h2>${{title}}</h2><p>${{copy}}</p></header>`,
  warmTruthStrip:()=>'<div data-truth></div>',
  renderWaitingForYou:()=>'<div data-human-queue></div>',
  agentIdentity:agent=>agent.agent_id,workerForAgent:()=>null,
  agentDisplayState:agent=>agent.pool_status,relativeAge:()=> 'just now',
}};
const high = globalThis.FleetViewModules.render('home', {{...base,
  warmBoards:()=>[{{}},{{}}],
  warmTickets:()=>[
    {{ticket:{{status:'in_progress'}}}},{{ticket:{{status:'needs_human',parked:true}}}},
    {{ticket:{{status:'submitted'}}}},{{ticket:{{status:'open'}}}},
  ],
  warmNextAction:()=>({{eyebrow:'Waiting for you',title:'Choose a safe window',copy:'Alpha is paused.',href:'#/review',label:'Review request'}}),
  reconcileAttention:()=>attention,centralLabels:['north'],fleetData:highData,
}});
const calmData = {{north:{{pool_summary:{{busy:0,available:3,stale:0}},boards:[{{board_id:'alpha',label:'Alpha',human_requests:[]}}]}}}};
const calm = globalThis.FleetViewModules.render('home', {{...base,
  warmBoards:()=>[{{central:'north',board:{{board_id:'alpha'}}}}],warmTickets:()=>[],
  warmNextAction:()=>({{eyebrow:'Everything is calm',title:'No work needs attention',copy:'Healthy.',href:'#/alpha',label:'Open a project'}}),
  reconcileAttention:()=>[],centralLabels:['north'],fleetData:calmData,
}});
console.log(JSON.stringify({{high,calm}}));
"""
    result = json.loads(
        subprocess.run(
            ["node", "-e", program], check=True, capture_output=True, text=True
        ).stdout
    )

    high = result["high"]
    assert 'class="home-status-grid"' in high
    assert "Needs you" in high
    assert "Evidence stale" in high
    assert high.count("data-intake-decline") == 1
    assert 'data-ask-id="ask-1"' in high
    assert "In progress</dt><dd>1" in high
    assert "Blocked</dt><dd>1" in high
    assert "Review ready</dt><dd>1" in high
    assert "Open queue</dt><dd>1" in high
    assert "data-human-queue" in high
    assert "On the floor" in high
    assert "worker-1" in high
    assert "TK-LIVE" in high

    calm = result["calm"]
    assert 'class="home-status-grid"' in calm
    assert 'data-state="calm"' in calm
    assert "No operational blockers" in calm
    assert "data-human-queue" not in calm


def test_home_reports_source_failure_without_inventing_empty_state() -> None:
    registry = dashboard.UI_ASSETS["/ui/view-registry.js"][1].decode("utf-8")
    home = dashboard.UI_ASSETS["/ui/views/home.js"][1].decode("utf-8")
    program = f"""
eval({json.dumps(registry)});
eval({json.dumps(home)});
const html = globalThis.FleetViewModules.render('home', {{
  esc:String,fmt:String,centralHref:()=>'',boardHref:()=>'',ticketHref:()=>'',
  pageHead:(a,b,c)=>`<header><h2>${{b}}</h2><p>${{c}}</p></header>`,
  warmTruthStrip:()=>'<div data-truth></div>',warmBoards:()=>[],warmTickets:()=>[],
  warmNextAction:()=>null,reconcileAttention:()=>[],renderWaitingForYou:()=>'',
  centralLabels:['north'],fleetData:{{}},fleetErrors:{{north:'http-503'}},
  agentIdentity:()=>'',agentDisplayState:()=>'',workerForAgent:()=>null,relativeAge:()=>'',
}});
console.log(html);
"""
    result = subprocess.run(
        ["node", "-e", program], check=True, capture_output=True, text=True
    ).stdout

    assert 'data-state="error"' in result
    assert 'role="alert"' in result
    assert "north (http-503)" in result
    assert "no zero or healthy state is inferred" in result
    assert "Connect your first project" not in result


def test_nocturne_shell_has_accessible_mobile_navigation_contract() -> None:
    html = dashboard.HTML
    script = dashboard.UI_ASSETS["/ui/assets/app.js"][1].decode("utf-8")
    css = dashboard.UI_ASSETS["/ui/assets/fleet.css"][1].decode("utf-8")

    assert 'id="nav-toggle"' in html
    assert 'aria-controls="fleet-sidebar"' in html
    assert 'id="mobile-route-label"' in html
    assert 'id="nav-scrim"' in html
    assert "function setMobileNavigation(open, returnFocus = false)" in script
    assert "event.key === 'Escape'" in script
    assert "mobileNavigation.setAttribute('inert', '')" in script
    assert "mobileNavigationToggle.focus()" in script
    assert '.mobile-shell-bar' in css
    assert 'height:calc(100dvh - 58px)' in css
    assert '@media(prefers-reduced-motion:reduce)' in css
