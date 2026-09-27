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
        ".home-briefing{display:grid",
        '.home-command[data-pressure="high"]',
        ".home-command .card-actions a{min-height:44px}",
        '.home-health-row[data-tone="danger"]',
        ':root[data-density="compact"] .home-command',
        "@media(max-width:980px){.home-briefing",
        "@media(max-width:560px){.home-command",
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
  north: {{pool_summary:{{busy:2,available:1,stale:1}},boards:[{{
    board_id:'alpha',label:'Alpha',human_requests:[{{ticket_id:'TK-HUMAN'}}],
  }}]}},
}};
const attention = [{{
  key:'finding|north|alpha',level:'critical',title:'Evidence stale',
  text:'Refresh the bounded snapshot.',central:'north',board:{{board_id:'alpha',label:'Alpha'}},
  ticket_id:'TK-ATTN',first_seen:'2030-01-01T00:00:00Z',
}}];
const base = {{
  esc,fmt:value=>value,centralHref:central=>`#/${{central}}`,
  ticketHref:(central,board,ticket)=>`#/${{central}}/${{board}}/${{ticket}}`,
  pageHead:(kicker,title,copy)=>`<header><b>${{kicker}}</b><h2>${{title}}</h2><p>${{copy}}</p></header>`,
  warmTruthStrip:()=>'<div data-truth></div>',
  renderWaitingForYou:()=>'<div data-human-queue></div>',
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
  warmBoards:()=>[{{}}],warmTickets:()=>[],
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
    assert 'class="home-command" data-pressure="high"' in high
    assert "Choose a safe window" in high
    assert "Needs attention now" in high
    assert "Evidence stale" in high
    assert "In progress</dt><dd>1" in high
    assert "Blocked</dt><dd>1" in high
    assert "Review ready</dt><dd>1" in high
    assert "Open queue</dt><dd>1" in high
    assert "data-human-queue" in high

    calm = result["calm"]
    assert 'class="home-command" data-pressure="calm"' in calm
    assert "No work needs attention" in calm
    assert 'data-state="calm"' in calm
    assert "No operational blockers" in calm
    assert "data-human-queue" not in calm
