from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_ticket_lifecycle_ui_is_labeled_live_and_keyboard_reachable() -> None:
    html = read("webui/index.html")
    app = read("webui/app.js")
    assert 'id="tickets" aria-labelledby="tickets-title"' in html
    assert 'id="ticket-message" role="status" aria-live="polite"' in html
    assert 'id="ticket-list" aria-live="polite"' in html
    assert 'id="ticket-form"' in html
    assert "row.tabIndex = 0" in app
    assert "row.setAttribute('aria-label'" in app


def test_home_exposes_no_worker_submission_or_reviewer_approval_route() -> None:
    routes = read("webui/routes.js")
    adapter = read("ticket_lifecycle/adapter.cjs")
    for forbidden in ("/pursers/tickets/claim", "/pursers/tickets/submit", "/pursers/tickets/review"):
        assert forbidden not in routes
    assert "create: (input) => call('create', input)" in adapter
    assert "cancel: (input) => call('cancel', input)" in adapter
