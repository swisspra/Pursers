from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("fleet_nocturne_routes", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


def test_ticket_detail_projects_bounded_delivery_review_and_question_evidence() -> None:
    ticket = {
        "ticket_id": "TK-detail",
        "title": "Evidence detail",
        "status": "closed",
        "review_verdict": "approve",
        "submitted_by_principal_id": "PR-worker",
        "reviewed_by_principal_id": "PR-reviewer",
        "reviewed_by_agent_name": "reviewer-1",
        "reviewed_at": "2030-01-02T12:00:00Z",
        "submission_history": [{
            "summary": "Implemented the route",
            "branch": "codex/TK-detail",
            "commit_hash": "a" * 40,
            "files_changed": ["tools/fleet-dashboard/ui/views/work.js"],
            "test_output": "pytest: 4 passed",
            "submitted_at": "2030-01-02T11:00:00Z",
        }],
        "review_history": [{
            "verdict": "approve",
            "review_label": "independent-principal-review",
            "reviewed_by_agent_name": "reviewer-1",
            "reviewed_at": "2030-01-02T12:00:00Z",
            "submission_commit": "a" * 40,
            "review_notes": "Verified exact source and tests.",
        }],
        "coordinator_questions": [{
            "question_id": "CQ-one",
            "kind": "decision",
            "state": "answered",
            "message": "Which interface is approved?",
            "answer": "Use the bounded interface.",
            "asked_by": {"agent_name": "worker-1"},
            "asked_at": "2030-01-02T10:00:00Z",
            "answered_at": "2030-01-02T10:05:00Z",
        }],
    }

    detail = dashboard._detail_ticket(ticket)

    assert detail["submission_evidence"] == {
        "summary": "Implemented the route",
        "branch": "codex/TK-detail",
        "commit": "a" * 40,
        "files_changed": ["tools/fleet-dashboard/ui/views/work.js"],
        "files_omitted": 0,
        "test_output": "pytest: 4 passed",
        "submitted_at": "2030-01-02T11:00:00Z",
    }
    assert detail["review_rounds"][0]["verdict"] == "approve"
    assert detail["review_rounds"][0]["reviewer"] == "reviewer-1"
    assert detail["coordination_questions"][0]["state"] == "answered"
    assert detail["coordination_questions"][0]["answer"] == "Use the bounded interface."


def test_inbox_alias_and_focused_ticket_assets_are_packaged() -> None:
    app = dashboard.UI_ASSETS["/ui/assets/app.js"][1].decode("utf-8")
    index = dashboard.HTML_SHELL
    inbox = dashboard.UI_ASSETS["/ui/views/approvals.js"][1].decode("utf-8")
    css = dashboard.UI_ASSETS["/ui/assets/fleet.css"][1].decode("utf-8")

    assert "approvals|inbox|activity" in app
    assert "match[1]==='inbox'?'approvals':match[1]" in app
    assert 'href="#/inbox"' in index
    assert ">Inbox</a>" in index
    assert "pageHead('Inbox'" in inbox
    assert "function ticketFocusedView" in app
    assert "Submission evidence" in app
    assert "Independent review rounds" in app
    assert ".ticket-focus-grid" in css
    assert "@media(max-width:800px){.ticket-focus-head,.ticket-focus-grid" in css


def test_ticket_detail_falls_back_to_literal_seat_suite_report() -> None:
    ticket = {
        "ticket_id": "TK-suite",
        "status": "submitted",
        "submission_history": [{
            "summary": "Ready",
            "test_output": "",
            "notes": "branch_and_commit: codex/TK-suite@" + "b" * 40
            + "\nseat-suite-report: pytest -q: 19 passed; browser mobile/light passed\nnext-action: review",
        }],
    }

    detail = dashboard._detail_ticket(ticket)

    assert detail["submission_evidence"]["test_output"] == (
        "pytest -q: 19 passed; browser mobile/light passed"
    )


def test_typed_inbox_mobile_list_detail_back_and_independent_bounds() -> None:
    registry = dashboard.UI_ASSETS["/ui/view-registry.js"][1].decode("utf-8")
    inbox = dashboard.UI_ASSETS["/ui/views/approvals.js"][1].decode("utf-8")
    reviews = [
        {"central": "work", "board": {"board_id": "pursers", "label": "Pursers"},
         "ticket": {"id": f"TK-{index:03d}", "title": f"Review {index}", "status": "submitted"}}
        for index in range(55)
    ]
    program = f"""
const listeners=[];const host={{innerHTML:''}};
global.document={{querySelector:s=>s==='#central-sections'?host:s==='.inbox-master-detail'?{{}}:null,createElement:()=>({{dataset:{{}}}}),head:{{append:()=>{{}}}},addEventListener:(k,cb)=>listeners.push([k,cb])}};
global.matchMedia=()=>({{matches:true}});
eval({json.dumps(registry)});eval({json.dumps(inbox)});
const human={{central:'work',board:{{board_id:'pursers',label:'Pursers'}},h:{{request_id:'HR-1',kind:'decision',message:'Choose a safe option'}}}};
const context={{esc:v=>String(v),fmt:v=>v,pageHead:()=>'',warmTruthStrip:()=>'',centralLabels:['work'],fleetData:{{work:{{}}}},
 humanRequestRows:()=>[human,human],humanRequestCard:()=>'<form data-inbox-source-action="human"><button data-human-action="accept">Accept</button></form>',
 butlerHoldRows:()=>[],butlerHoldCard:()=>'',warmTickets:()=>{json.dumps(reviews)},warmBoards:()=>[],ticketHref:()=> '#ticket',boardHref:()=> '#board',
 butlerAgreementRows:()=>[],butlerTicketAgreementRows:()=>[],butlerRepeatedTicketRows:()=>[],butlerEvaluationTruncationRows:()=>[],butlerScoreCard:()=>'',butlerRepeatedTicketCard:()=>''}};
const first=globalThis.FleetViewModules.render('approvals',context);
for(const [kind,cb] of listeners)if(kind==='click')cb({{target:{{closest:s=>s==='[data-inbox-select]'?{{dataset:{{inboxSelect:'human:work:pursers:HR-1'}}}}:null}}}});
const selected=host.innerHTML;
for(const [kind,cb] of listeners)if(kind==='click')cb({{target:{{closest:s=>s==='[data-inbox-back]'?{{}}:null}}}});
console.log(JSON.stringify({{first,selected,back:host.innerHTML}}));
"""
    rendered = json.loads(subprocess.run(
        ["node", "-e", program], check=True, capture_output=True, text=True
    ).stdout)

    assert 'data-inbox-mobile-view="list"' in rendered["first"]
    assert "2 duplicate records collapsed" in rendered["first"]
    assert "50 shown" in rendered["first"]
    assert "5 history item(s) omitted independently" in rendered["first"]
    assert 'data-inbox-mobile-view="detail"' in rendered["selected"]
    assert 'data-human-action="accept"' in rendered["selected"]
    assert 'data-inbox-mobile-view="list"' in rendered["back"]
