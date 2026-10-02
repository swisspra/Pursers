from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("fleet_work_view", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


def test_work_view_renders_product_projected_progress_without_lease_inference() -> None:
    fresh_progress = {
        "low_percent": 35,
        "high_percent": 55,
        "confidence": "medium",
        "assessed_at": "2030-01-02T11:58:00Z",
        "evidence": "Focused product tests pass.",
    }
    stale_progress = {
        "low_percent": 70,
        "high_percent": 70,
        "confidence": "high",
        "assessed_at": "2030-01-02T10:00:00Z",
        "evidence": "Checkpoint is older than its freshness window.",
    }
    snapshot_tickets = [
        {
            "ticket_id": "TK-fresh",
            "title": "Fresh progress",
            "status": "in_progress",
            "claimed_by": "worker-1",
            "lease_expires_at": "2030-01-02T12:15:00Z",
            "progress": fresh_progress,
            "progress_freshness": "fresh",
            "updated_at": "2030-01-02T11:58:00Z",
        },
        {
            "ticket_id": "TK-stale",
            "title": "Stale progress",
            "status": "claimed",
            "claimed_by": "worker-2",
            "lease_expires_at": "2030-01-02T11:55:00Z",
            "progress": stale_progress,
            "progress_freshness": "stale",
            "updated_at": "2030-01-02T11:59:00Z",
        },
        {
            "ticket_id": "TK-unknown",
            "title": "No estimate",
            "status": "claimed",
            "claimed_by": "worker-3",
            "updated_at": "2030-01-02T11:57:00Z",
        },
    ]
    fleet = dashboard.aggregate_fleet(
        [
            {
                "board_id": "private-board",
                "label": "Private board",
                "snapshot": {"agents": [], "tickets": snapshot_tickets},
                "events": [],
            }
        ],
        stale_seconds=300,
        now=datetime(2030, 1, 2, 12, tzinfo=timezone.utc),
    )
    projected = fleet["boards"][0]["tickets"]
    reopened = dashboard._detail_ticket(
        {
            "ticket_id": "TK-reopened",
            "title": "Reviewer requested rework",
            "status": "rejected",
            "progress": {
                "low_percent": 88,
                "high_percent": 88,
                "confidence": "high",
                "assessed_at": "2030-01-02T11:00:00Z",
                "evidence": "OLD-ATTEMPT-EVIDENCE",
            },
            "progress_freshness": "fresh",
            "updated_at": "2030-01-02T11:59:30Z",
        }
    )
    registry = dashboard.UI_ASSETS["/ui/view-registry.js"][1].decode("utf-8")
    work = dashboard.UI_ASSETS["/ui/views/work.js"][1].decode("utf-8")
    program = f"""
global.document = {{
  querySelector: () => null,
  createElement: () => ({{dataset: {{}}}}),
  head: {{append: () => {{}}}},
  addEventListener: () => {{}},
}};
global.matchMedia = () => ({{matches: true}});
Date.now = () => Date.parse('2030-01-02T12:00:00Z');
eval({json.dumps(registry)});
eval({json.dumps(work)});
const tickets = {json.dumps(projected + [reopened])};
const html = globalThis.FleetViewModules.render('work', {{
  esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  fmt: value => value,
  relativeAge: value => value === '2030-01-02T11:58:00Z' ? '2 min ago' : '2 hr ago',
  ticketHref: (central, board, ticket) => `#/${{central}}/${{board}}/${{ticket}}`,
  boardHref: (central, board, route) => `#/${{central}}/${{board}}/${{route}}`,
  pageHead: () => '',
  warmTruthStrip: () => '',
  warmTickets: () => tickets.map(ticket => ({{
    central: 'private', board: {{board_id: 'private-board', label: 'Private board'}}, ticket,
  }})),
}});
console.log(JSON.stringify({{html, tracks: (html.match(/work-progress-track/g) || []).length}}));
"""

    rendered = json.loads(
        subprocess.run(
            ["node", "-e", program], check=True, capture_output=True, text=True
        ).stdout
    )
    html = rendered["html"]

    assert "35–55% · medium confidence" in html
    assert "Current · assessed 2 min ago" in html
    assert "Focused product tests pass." in html
    assert "About 70% · high confidence" in html
    assert "Stale · assessed 2 hr ago" in html
    assert "Lease expired" in html
    assert "Lease · 15 min remaining" in html
    assert "Progress not assessed" in html
    assert "Rework not assessed" in html
    assert "OLD-ATTEMPT-EVIDENCE" not in html
    assert "88%" not in html
    assert "About 0%" not in html
    assert rendered["tracks"] == 2


def test_work_progress_styles_are_static_responsive_and_high_contrast_safe() -> None:
    css = dashboard.UI_ASSETS["/ui/views/work.css"][1].decode("utf-8")

    for contract in (
        ".work-progress-range",
        "min-width: 4px",
        '.work-progress-cell[data-progress-state="stale"]',
        ".work-progress-evidence > summary",
        "min-height: 44px",
        "@media (max-width: 800px)",
        "@media (max-width: 430px)",
        "@media (prefers-reduced-motion: reduce)",
        "@media (forced-colors: active)",
    ):
        assert contract in css


def test_work_view_pages_deduplicated_rows_and_advances_without_reload() -> None:
    registry = dashboard.UI_ASSETS["/ui/view-registry.js"][1].decode("utf-8")
    work = dashboard.UI_ASSETS["/ui/views/work.js"][1].decode("utf-8")
    tickets = [
        {"id": f"TK-{index:03d}", "title": f"Ticket {index}", "status": "open"}
        for index in range(120)
    ]
    tickets.append(dict(tickets[0]))
    program = f"""
const listeners = [];
const host = {{innerHTML: ''}};
global.document = {{
  querySelector: selector => selector === '#central-sections' ? host : selector === '.work-view' ? {{}} : null,
  createElement: () => ({{dataset: {{}}}}),
  head: {{append: () => {{}}}},
  addEventListener: (kind, callback) => listeners.push([kind, callback]),
}};
global.matchMedia = () => ({{matches: true}});
eval({json.dumps(registry)});
eval({json.dumps(work)});
const tickets = {json.dumps(tickets)};
const context = {{
  esc: value => String(value), fmt: value => value, relativeAge: value => value,
  ticketHref: () => '#ticket', boardHref: () => '#board', pageHead: () => '',
  warmTruthStrip: () => '',
  warmTickets: () => tickets.map(ticket => ({{
    central: 'private', board: {{board_id: 'board', label: 'Board'}}, ticket,
  }})),
}};
const first = globalThis.FleetViewModules.render('work', context);
const next = {{disabled: false, textContent: '', dataset: {{}}}};
for (const [kind, callback] of listeners) {{
  if (kind === 'click') callback({{target: {{closest: selector => selector === '[data-work-next-page]' ? next : null}}}});
}}
console.log(JSON.stringify({{
  firstCount: (first.match(/work-ledger-row/g) || []).length,
  firstSummary: first.includes('Showing 50 of 120 matching tickets (120 loaded)'),
  firstButton: first.includes('Show next 50'),
  secondCount: (host.innerHTML.match(/work-ledger-row/g) || []).length,
  secondSummary: host.innerHTML.includes('Showing 100 of 120 matching tickets (120 loaded)'),
}}));
"""
    rendered = json.loads(
        subprocess.run(
            ["node", "-e", program], check=True, capture_output=True, text=True
        ).stdout
    )
    assert rendered == {
        "firstCount": 50,
        "firstSummary": True,
        "firstButton": True,
        "secondCount": 100,
        "secondSummary": True,
    }
