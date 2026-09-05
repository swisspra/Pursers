"""Pure relevance and synthetic-cue helpers for ticket backlog scans."""

from __future__ import annotations

import re
from typing import Any


CLAIMABLE_STATES = frozenset({"open"})
WAIT_FOR_CLAIMABLE = "claimable"
WAIT_FOR_SUBMITTED = "submitted"
BRANCH_AND_COMMIT_RE = re.compile(
    r"(?im)^\s*branch_and_commit\s*:\s*(.+?)\s*$"
)


def continuation_hint(ticket: dict[str, Any]) -> dict[str, Any] | None:
    branch_and_commit = None
    for submission in reversed(ticket.get("submission_history", [])):
        if not isinstance(submission, dict):
            continue
        notes = submission.get("notes")
        if isinstance(notes, str):
            match = BRANCH_AND_COMMIT_RE.search(notes)
            if match:
                branch_and_commit = match.group(1).strip()
                break
    prior_name = ticket.get("last_claimed_by")
    prior_agent_id = ticket.get("last_claimed_by_agent_id")
    if not prior_name and not prior_agent_id and not branch_and_commit:
        return None
    return {
        "prior_holder": (
            {
                "agent_name": prior_name,
                "agent_id": prior_agent_id,
                "principal_id": ticket.get("last_claimed_by_principal_id"),
                "claimed_at": ticket.get("last_claimed_at"),
                "release_reason": ticket.get("last_release_reason"),
            }
            if prior_name or prior_agent_id
            else None
        ),
        "branch_and_commit": branch_and_commit,
        "abandoned_count": int(ticket.get("abandoned_count", 0) or 0),
    }


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
    dispatch_state = ticket.get("dispatch_state")
    dispatch_enabled = isinstance(dispatch_state, dict)
    if dispatch_enabled:
        state = str(dispatch_state.get("state") or "")
        if wait_for == WAIT_FOR_SUBMITTED:
            offer = ticket.get("review_offer")
            lease = ticket.get("review_lease")
            return bool(
                ticket.get("status") == "submitted"
                and (
                    (isinstance(offer, dict) and offer.get("agent_id") == my_agent_id)
                    or (
                        isinstance(lease, dict)
                        and lease.get("reviewer_agent_id") == my_agent_id
                    )
                    or (
                        state == "broadcast"
                        and _review_is_available(ticket, my_agent_id)
                    )
                )
            )
        offer = ticket.get("work_offer")
        return bool(
            ticket.get("claimed_by_agent_id") == my_agent_id
            or (
                ticket.get("status") == "open"
                and (
                    (isinstance(offer, dict) and offer.get("agent_id") == my_agent_id)
                    or state == "broadcast"
                )
            )
        )
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
    board_id: str | None = None,
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
        offer_key = (
            "review_offer" if wait_for == WAIT_FOR_SUBMITTED else "work_offer"
        )
        offer = ticket.get(offer_key)
        offered_to_me = (
            isinstance(offer, dict) and offer.get("agent_id") == my_agent_id
        )
        event: dict[str, Any] = {
            "kind": (
                "review_offered"
                if offered_to_me and wait_for == WAIT_FOR_SUBMITTED
                else "ticket_offered" if offered_to_me else "ticket_backlog"
            ),
            "source": "backlog_scan",
            "ticket_id": ticket_id,
            "status": wanted_status,
        }
        if offered_to_me:
            event["offer"] = {
                "ticket_id": ticket_id,
                "board_id": board_id,
                "expires_at": offer.get("expires_at"),
                "tier": ticket.get("tier", 2),
                "skills_required": list(ticket.get("skills_required") or []),
            }
        updated_at = ticket.get("updated_at")
        if isinstance(updated_at, str) and updated_at:
            event["updated_at"] = updated_at
        payload_ref = ticket.get("payload_ref")
        if payload_ref:
            event["payload_ref"] = payload_ref
        continuation = continuation_hint(ticket)
        if continuation is not None:
            event["continuation"] = continuation
        events.append(event)
    return events
