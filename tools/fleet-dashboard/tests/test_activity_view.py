from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_activity_case_study_renders_source_timeline_and_unavailable_metrics() -> None:
    registry = (ROOT / "ui/view-registry.js").read_text()
    activity = (ROOT / "ui/views/activity.js").read_text()
    program = f"""
global.document = {{querySelector: () => ({{}})}};
eval({json.dumps(registry)});
eval({json.dumps(activity)});
const fleetData = {{local: {{case_studies: [{{
  study_id:'private-study', status:'comparable',
  comparisons:[{{pair_id:'pair-1',status:'comparable',reasons:[],sequential_run_id:'run-s',parallel_run_id:'run-p',speedup:{{status:'available',ratio:2}}}}],
  runs:[
    {{run_id:'run-s',arm:'sequential',complete:true,wall_time:{{status:'available',seconds:1200}},observed_peak_concurrency:1,accepted_count:1,missing:[],tickets:[{{board_id:'board-a',ticket_id:'TK-SOURCE',accepted:true,metrics:{{queue_time:{{status:'available',seconds:60}},ownership_time:{{status:'available',seconds:300}},review_queue_time:{{status:'unavailable'}},review_work_time:{{status:'unavailable'}}}},timeline:[{{phase:'started',occurred_at:'2026-01-01T00:01:00Z',quality:'observed',source:'board_journal'}}]}}]}},
    {{run_id:'run-p',arm:'parallel',complete:true,wall_time:{{status:'available',seconds:600}},observed_peak_concurrency:2,accepted_count:1,missing:[],tickets:[]}}
  ]
}}]}}}};
const html = globalThis.FleetViewModules.render('activity', {{
  esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;'), fmt:value=>value,
  pageHead:()=>'', warmTruthStrip:()=>'', warmTickets:()=>[], warmBoards:()=>[],
  ticketHref:(central,board,ticket)=>`#/${{central}}/${{board}}/${{ticket}}`, boardHref:()=> '#/board',
  autonomousRows:()=>[], autonomousStateLabel:value=>value, fleetData,
}});
console.log(html);
"""
    html = subprocess.run(
        ["node", "-e", program], check=True, capture_output=True, text=True
    ).stdout

    assert "Sequential and parallel evidence" in html
    assert "2.00× speedup" in html
    assert "#/local/board-a/TK-SOURCE" in html
    assert "review queue</dt><dd>unavailable" in html.lower()
    assert "Cost unavailable unless supplied by source telemetry" in html


def test_activity_case_study_styles_keep_two_lanes_responsive() -> None:
    css = (ROOT / "ui/views/activity.css").read_text()
    assert ".case-study-lanes { display: grid; grid-template-columns: 1fr 1fr" in css
    assert ".case-study-lanes { grid-template-columns: 1fr; }" in css
    assert '.case-study[data-comparison-state="incomparable"]' in css
