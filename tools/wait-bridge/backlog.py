"""Pure relevance and synthetic-cue helpers for open-ticket backlog scans."""

from __future__ import annotations

from typing import Any


CLAIMABLE_STATES = frozenset({"open"})


def ticket_project(ticket: dict[str, Any]) -> str | None:
    """Return the lowercase first path segment of a ticket target URL."""
    target = (ticket.get("target_url") or "").strip()
    if not target:
        return None
    first = target.replace(":", "/").split("/", 1)[0].strip().lower()
    return first or None


def ticket_is_relevant(
    ticket: dict[str, Any],
    my_agent_id: str | None,
    only_mine: bool,
    project: str | None,
) -> bool:
    """Apply the wait bridge's project and ownership relevance rules."""
    if project is not None and ticket_project(ticket) != project:
        return False
    if not only_mine:
        return True
    status = ticket.get("status")
    claimed_by = ticket.get("claimed_by_agent_id")
    assigned = ticket.get("assigned_to_agent_id")
    mine = (
        ticket.get("created_by_agent_id") == my_agent_id
        or claimed_by == my_agent_id
        or assigned == my_agent_id
    )
    claimable = (
        status in CLAIMABLE_STATES
        and claimed_by in (None, my_agent_id)
        and assigned in (None, my_agent_id)
    )
    return mine or claimable


def backlog_events(
    tickets: list[dict[str, Any]],
    my_agent_id: str | None,
    only_mine: bool,
    project: str | None,
) -> list[dict[str, Any]]:
    """Project relevant open tickets into bounded, sequence-free wake cues."""
    events: list[dict[str, Any]] = []
    for ticket in tickets:
        ticket_id = ticket.get("ticket_id")
        if (
            not isinstance(ticket_id, str)
            or not ticket_id
            or ticket.get("status") != "open"
        ):
            continue
        if not ticket_is_relevant(ticket, my_agent_id, only_mine, project):
            continue
        event: dict[str, Any] = {
            "kind": "ticket_backlog",
            "source": "backlog_scan",
            "ticket_id": ticket_id,
            "status": "open",
        }
        payload_ref = ticket.get("payload_ref")
        if payload_ref:
            event["payload_ref"] = payload_ref
        events.append(event)
    return events
