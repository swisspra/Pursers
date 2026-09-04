"""Pure relevance and synthetic-cue helpers for ticket backlog scans."""

from __future__ import annotations

from typing import Any


CLAIMABLE_STATES = frozenset({"open"})
WAIT_FOR_CLAIMABLE = "claimable"
WAIT_FOR_SUBMITTED = "submitted"


def _review_is_available(
    ticket: dict[str, Any], _my_agent_id: str | None
) -> bool:
    """Accept unclaimed/expired state, inferring unclaimed from no live lease."""
    state = ticket.get("review_state")
    if state is None:
        return not isinstance(ticket.get("review_lease"), dict)
    if isinstance(state, str):
        return state.strip().lower() in {"unclaimed", "expired"}
    if not isinstance(state, dict):
        return False
    holder = (
        state.get("claimed_by_agent_id")
        or state.get("reviewer_agent_id")
        or state.get("holder_agent_id")
    )
    status = str(state.get("status") or "unclaimed").strip().lower()
    return status == "expired" or (holder is None and status == "unclaimed")


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
    wait_for: str = WAIT_FOR_CLAIMABLE,
) -> bool:
    """Apply the wait bridge's project and ownership relevance rules."""
    if project is not None and ticket_project(ticket) != project:
        return False
    if wait_for == WAIT_FOR_SUBMITTED:
        return (
            ticket.get("status") == "submitted"
            and _review_is_available(ticket, my_agent_id)
        )
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
    wait_for: str = WAIT_FOR_CLAIMABLE,
) -> list[dict[str, Any]]:
    """Project relevant tickets into bounded, sequence-free wake cues."""
    wanted_status = "submitted" if wait_for == WAIT_FOR_SUBMITTED else "open"
    events: list[dict[str, Any]] = []
    for ticket in tickets:
        ticket_id = ticket.get("ticket_id")
        if (
            not isinstance(ticket_id, str)
            or not ticket_id
            or ticket.get("status") != wanted_status
        ):
            continue
        if not ticket_is_relevant(
            ticket, my_agent_id, only_mine, project, wait_for
        ):
            continue
        event: dict[str, Any] = {
            "kind": "ticket_backlog",
            "source": "backlog_scan",
            "ticket_id": ticket_id,
            "status": wanted_status,
        }
        updated_at = ticket.get("updated_at")
        if isinstance(updated_at, str) and updated_at:
            event["updated_at"] = updated_at
        payload_ref = ticket.get("payload_ref")
        if payload_ref:
            event["payload_ref"] = payload_ref
        events.append(event)
    return events
