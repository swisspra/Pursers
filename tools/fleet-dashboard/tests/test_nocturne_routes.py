from __future__ import annotations

import importlib.util
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
