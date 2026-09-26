from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("fleet_work_view", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


def test_work_route_preserves_states_and_exposes_ownership_handoff_and_evidence() -> None:
    registry = dashboard.UI_ASSETS["/ui/view-registry.js"][1].decode("utf-8")
    work = dashboard.UI_ASSETS["/ui/views/work.js"][1].decode("utf-8")
    program = f"""
eval({json.dumps(registry)});
eval({json.dumps(work)});
const esc = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;');
const rows = [
  {{central:'central-a',board:{{board_id:'alpha',label:'Alpha'}},ticket:{{id:'TK-open',title:'A very long ready title',status:'open',status_label:'open',updated_at:'2030-01-01T10:00:00Z'}}}},
  {{central:'central-a',board:{{board_id:'alpha',label:'Alpha'}},ticket:{{id:'TK-work',title:'Build evidence',status:'claimed',status_label:'claimed',claimed_by:'worker-a',claim_age_s:740,lease_renewal_source:'model',updated_at:'2030-01-01T10:01:00Z'}}}},
  {{central:'central-b',board:{{board_id:'beta',label:'Beta'}},ticket:{{id:'TK-review',title:'Check evidence',status:'submitted',status_label:'in review by reviewer-a',updated_at:'2030-01-01T10:02:00Z'}}}},
  {{central:'central-b',board:{{board_id:'beta',label:'Beta'}},ticket:{{id:'TK-human',title:'Need an answer',status:'needs_human',status_label:'needs human',abandoned_count:2,updated_at:null}}}},
  {{central:'central-c',board:{{board_id:'gamma',label:'Gamma'}},ticket:{{id:'TK-new',title:'New protocol state',status:'future_state',status_label:'future_state'}}}},
];
const context = {{
  esc,
  fmt:value=>`DATE(${{value}})`,
  pageHead:(kicker,title,copy)=>`<header><b>${{esc(kicker)}}</b><h2>${{esc(title)}}</h2><p>${{esc(copy)}}</p></header>`,
  warmTruthStrip:()=>'<div class="truth-strip"></div>',
  warmTickets:()=>rows,
  ticketHref:(central,board,ticket)=>`#/${{central}}/${{board}}/ticket/${{ticket}}`,
  boardHref:(central,board,tab)=>`#/${{central}}/${{board}}/${{tab}}`,
}};
const html = globalThis.FleetViewModules.render('work', context);
rows.length = 0;
const empty = globalThis.FleetViewModules.render('work', context);
console.log(JSON.stringify({{html,empty}}));
"""
    result = json.loads(
        subprocess.run(
            ["node", "-e", program], check=True, capture_output=True, text=True
        ).stdout
    )
    html = result["html"]

    assert "Ready</span><b>1" in html
    assert "Working</span><b>1" in html
    assert "Review ready</span><b>1" in html
    assert "Needs attention</span><b>1" in html
    assert "worker-a" in html
    assert "Active · claimed 12m ago" in html
    assert "reviewer-a" in html
    assert "An independent reviewer checks the submitted evidence." in html
    assert "Updated DATE(2030-01-01T10:02:00Z)" in html
    assert "2 lease lapses recorded" in html
    assert "Other states" in html
    assert "future_state" in html
    assert 'href="#/central-b/beta/timeline">Timeline</a>' in html
    assert 'href="#/central-b/beta/changes">Changes</a>' in html
    assert 'href="#/projects">Choose a project</a>' in result["empty"]


def test_work_route_styles_cover_detail_responsive_density_and_reduced_motion() -> None:
    source = dashboard.UI_ASSETS["/ui/views/work.js"][1].decode("utf-8")
    css = dashboard.UI_ASSETS["/ui/views/work.css"][1].decode("utf-8")

    for contract in (
        ".work-summary{display:grid",
        ".work-ticket-list{display:grid",
        ".ticket-detail>summary",
        ".ticket-coordination{margin:0}",
        ':root[data-density="compact"] .work-ticket',
        "@media(max-width:800px){.work-summary",
        "@media(max-width:430px){.work-summary",
        "@media(prefers-reduced-motion:reduce)",
    ):
        assert contract in css
    assert "const STYLE_URL = '/ui/views/work.css'" in source
    assert "link.dataset.fleetViewStyle = 'work'" in source
