"""Pure, additive projection of authoritative ticket lifecycle evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any


ACTIVITY_SCHEMA_VERSION = 1
MAX_EVIDENCE_REFS = 8
STAGES = frozenset(
    {"intake", "queued", "work", "validation", "review", "integration", "delivery", "completed"}
)
STATES = frozenset(
    {"waiting", "running", "blocked", "retrying", "failed", "canceled", "stale", "unknown", "completed"}
)
COMPLETION_BOUNDARIES = frozenset(
    {"review", "pull_request", "integration", "delivery", "unknown"}
)
ACTIVE_WORK_STATES = frozenset({"claimed", "in_progress", "creating_report"})
REVIEW_STATES = frozenset({"submitted", "reviewing", "in_review"})
DELIVERY_STATES = frozenset(
    {
        "pr_pending",
        "pr_blocked",
        "pr_uncertain",
        "pr_created",
        "integration_pending",
        "integration_blocked",
        "integration_merged",
        "delivery_recorded",
        "release_pending",
        "released",
    }
)


def _text(value: Any, limit: int = 280) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split())
    return compact[:limit] or None


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def _latest(ticket: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    values = ticket.get(field)
    if not isinstance(values, list):
        return {}
    return next((item for item in reversed(values) if isinstance(item, Mapping)), {})


def _positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def _count(value: Any) -> int:
    return value if type(value) is int and value > 0 else 0


def _actor(value: Any) -> str | None:
    return _text(value, 96)


def _delivery_fact(ticket: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    annotations = ticket.get("annotations")
    if not isinstance(annotations, list):
        return {}, {}
    for annotation in reversed(annotations):
        if not isinstance(annotation, Mapping):
            continue
        raw = annotation.get("text")
        if not isinstance(raw, str) or not raw.startswith("pursers-delivery: "):
            continue
        try:
            value = json.loads(raw.removeprefix("pursers-delivery: "))
        except (TypeError, ValueError):
            continue
        if isinstance(value, Mapping) and value.get("state") in DELIVERY_STATES:
            return value, annotation
    for annotation in reversed(annotations):
        if not isinstance(annotation, Mapping):
            continue
        raw = annotation.get("text")
        if isinstance(raw, str) and raw.startswith("source-writeback-sha256:"):
            return {"state": "delivery_recorded"}, annotation
    return {}, {}


def _valid_estimate(ticket: Mapping[str, Any], attempt_id: int | None) -> dict[str, Any] | None:
    progress = ticket.get("progress")
    if not isinstance(progress, Mapping):
        return None
    low = progress.get("low_percent")
    high = progress.get("high_percent")
    confidence = progress.get("confidence")
    if (
        type(low) is not int
        or type(high) is not int
        or not 0 <= low <= high <= 99
        or confidence not in {"low", "medium", "high"}
        or (
            attempt_id is not None
            and _positive_int(progress.get("attempt")) not in {None, attempt_id}
        )
    ):
        return None
    result: dict[str, Any] = {
        "low_percent": low,
        "high_percent": high,
        "confidence": confidence,
    }
    evidence = _text(progress.get("evidence"))
    assessed_at = _text(progress.get("assessed_at"), 40)
    if evidence is not None:
        result["evidence"] = evidence
    if assessed_at is not None:
        result["assessed_at"] = assessed_at
    return result


def _completion_boundary(
    ticket: Mapping[str, Any], delivery: Mapping[str, Any]
) -> str:
    for value in (
        ticket.get("activity_completion_boundary"),
        delivery.get("completion_boundary"),
    ):
        if value in COMPLETION_BOUNDARIES - {"unknown"}:
            return str(value)
    return "unknown"


def project_ticket_activity(
    ticket: Mapping[str, Any],
    *,
    board_id: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Return current lifecycle evidence without mutating workflow or leases."""
    current_time = datetime.now().astimezone().timestamp() if now is None else now
    ticket_id = _text(ticket.get("ticket_id"), 96) or "unknown"
    prefix = (
        f"board://{board_id}/ticket/{ticket_id}"
        if isinstance(board_id, str) and board_id
        else f"ticket:{ticket_id}"
    )
    evidence_refs: list[str] = []

    def evidence(fragment: str) -> None:
        ref = f"{prefix}#{fragment}"
        if ref not in evidence_refs and len(evidence_refs) < MAX_EVIDENCE_REFS:
            evidence_refs.append(ref)

    created_at = _text(ticket.get("created_at"), 40)
    if created_at:
        evidence("created")

    dispatch = _latest(ticket, "dispatch_history")
    dispatch_state = ticket.get("dispatch_state")
    if not isinstance(dispatch_state, Mapping):
        dispatch_state = {}
    if dispatch or dispatch_state:
        evidence("dispatch")

    attempt_id = _positive_int(ticket.get("work_attempt"))
    claimed_at = _text(ticket.get("claimed_at"), 40)
    if claimed_at or ticket.get("claimed_by_agent_id"):
        evidence(f"claim-{attempt_id or 'unknown'}")

    progress = ticket.get("progress")
    if isinstance(progress, Mapping):
        revision = _positive_int(progress.get("revision"))
        evidence(
            f"progress-{_positive_int(progress.get('attempt')) or attempt_id or 'unknown'}-"
            f"{revision or 'unknown'}"
        )

    submission = _latest(ticket, "submission_history")
    submitted_at = _text(submission.get("submitted_at") or ticket.get("submitted_at"), 40)
    if submitted_at:
        evidence("submission")

    review = _latest(ticket, "review_history")
    reviewed_at = _text(review.get("reviewed_at") or ticket.get("reviewed_at"), 40)
    if reviewed_at or ticket.get("review_verdict"):
        evidence("review")

    delivery, delivery_annotation = _delivery_fact(ticket)
    annotation_id = _text(delivery_annotation.get("annotation_id"), 96)
    if delivery:
        evidence(f"annotation-{annotation_id}" if annotation_id else "delivery")

    blocker = ticket.get("workflow_blocker")
    if ticket.get("parked") or isinstance(blocker, Mapping) or isinstance(ticket.get("human_request"), Mapping):
        evidence("blocker")

    boundary = _completion_boundary(ticket, delivery)
    status = str(ticket.get("status") or "unknown")
    result: dict[str, Any] = {
        "schema_version": ACTIVITY_SCHEMA_VERSION,
        "stage": "intake",
        "state": "waiting",
        "attempt_id": attempt_id,
        "actor_id": None,
        "updated_at": created_at or _text(ticket.get("updated_at"), 40),
        "freshness": "unknown",
        "evidence_refs": evidence_refs,
        "next_action": "Queue the ticket for an eligible worker.",
        "completion_boundary": boundary,
    }

    if status == "open":
        rejected = (
            review.get("verdict") == "reject"
            or ticket.get("review_verdict") == "reject"
            or _count(ticket.get("rejection_count")) > 0
        )
        offer = ticket.get("work_offer")
        if not isinstance(offer, Mapping):
            offer = {}
        if ticket.get("parked") or isinstance(blocker, Mapping):
            reason = blocker.get("reason") if isinstance(blocker, Mapping) else None
            result.update(
                stage="work" if attempt_id else "queued",
                state="blocked",
                updated_at=_text(
                    blocker.get("detected_at") if isinstance(blocker, Mapping) else None,
                    40,
                )
                or _text(ticket.get("updated_at"), 40)
                or created_at,
                blocking_reason=(_text(reason.replace("_", " ")) if isinstance(reason, str) else None)
                or "The workflow is parked.",
                next_action="Resolve the workflow blocker before resuming.",
            )
        elif rejected:
            result.update(
                stage="work",
                state="retrying",
                actor_id=_actor(offer.get("agent_id") or ticket.get("assigned_to_agent_id")),
                updated_at=reviewed_at or _text(ticket.get("updated_at"), 40) or created_at,
                next_action="An eligible worker must claim the next attempt.",
            )
        elif dispatch_state or dispatch or offer or ticket.get("assigned_to_agent_id"):
            result.update(
                stage="queued",
                actor_id=_actor(offer.get("agent_id") or ticket.get("assigned_to_agent_id")),
                updated_at=_text(
                    offer.get("offered_at")
                    or dispatch_state.get("at")
                    or dispatch.get("at")
                    or ticket.get("updated_at"),
                    40,
                )
                or created_at,
                next_action="An eligible worker must accept the queued work.",
            )

    elif status in ACTIVE_WORK_STATES:
        result.update(
            stage="validation" if status == "creating_report" else "work",
            state="running",
            actor_id=_actor(ticket.get("claimed_by_agent_id")),
            updated_at=_text(
                progress.get("assessed_at") if isinstance(progress, Mapping) else None,
                40,
            )
            or claimed_at
            or _text(ticket.get("updated_at"), 40),
            next_action=(
                "Finish validation and submit its recorded evidence."
                if status == "creating_report"
                else "Continue the current work attempt and record evidence."
            ),
        )
        estimate = _valid_estimate(ticket, attempt_id)
        if estimate is not None:
            result["estimate"] = estimate
        expiry = _epoch(ticket.get("lease_expires_at"))
        if expiry is not None and expiry <= current_time:
            result.update(
                state="stale",
                freshness="stale",
                blocking_reason="The work lease expired.",
                next_action="Reconcile the expired lease before continuing.",
            )
        elif ticket.get("progress_freshness") == "stale":
            result.update(
                state="stale",
                freshness="stale",
                next_action="Refresh meaningful progress evidence or reconcile the work attempt.",
            )
        elif ticket.get("progress_freshness") == "fresh" and estimate is not None:
            result["freshness"] = "fresh"
        elif (
            (review.get("verdict") == "reject" or _count(ticket.get("rejection_count")) > 0)
            and estimate is None
        ):
            result.update(
                state="retrying",
                next_action="Address the latest review feedback in this work attempt.",
            )

    elif status in REVIEW_STATES:
        lease = ticket.get("review_lease")
        if not isinstance(lease, Mapping):
            lease = {}
        actor_id = _actor(lease.get("reviewer_agent_id") or ticket.get("reviewed_by_agent_id"))
        lease_expiry = _epoch(lease.get("expires_at"))
        result.update(
            stage="review",
            state="running" if actor_id else "waiting",
            actor_id=actor_id,
            updated_at=_text(lease.get("claimed_at"), 40) or submitted_at or _text(ticket.get("updated_at"), 40),
            next_action=(
                "The independent reviewer records an approve or reject verdict."
                if actor_id
                else "An independent reviewer must claim the submission."
            ),
        )
        if lease_expiry is not None and lease_expiry <= current_time:
            result.update(
                state="stale",
                freshness="stale",
                blocking_reason="The review lease expired.",
                next_action="Reconcile the expired review lease before review continues.",
            )

    elif status == "needs_human":
        request = ticket.get("human_request")
        request = request if isinstance(request, Mapping) else {}
        result.update(
            stage="review" if submitted_at else "validation" if attempt_id else "intake",
            state="blocked",
            actor_id=_actor(request.get("requested_by_agent_id") or ticket.get("claimed_by_agent_id")),
            updated_at=_text(request.get("requested_at"), 40) or _text(ticket.get("updated_at"), 40),
            blocking_reason=_text(request.get("message")) or "A human decision is required.",
            next_action="Record the requested human decision before resuming.",
        )

    elif status == "rejected":
        result.update(
            stage="review",
            state="failed",
            actor_id=_actor(review.get("reviewed_by_agent_id") or ticket.get("reviewed_by_agent_id")),
            updated_at=reviewed_at or _text(ticket.get("updated_at"), 40),
            freshness="fresh",
            next_action="Inspect the recorded review failure; no retry is currently queued.",
        )

    elif status in {"canceled", "terminated"}:
        stage = "review" if submitted_at else "work" if attempt_id else "queued"
        result.update(
            stage=stage,
            state="canceled",
            actor_id=_actor(
                ticket.get("canceled_by_agent_id")
                or ticket.get("last_claimed_by_agent_id")
                or ticket.get("claimed_by_agent_id")
            ),
            updated_at=_text(ticket.get("canceled_at") or ticket.get("updated_at"), 40),
            freshness="fresh",
            next_action="No further action is scheduled for this canceled ticket.",
        )

    elif status == "closed":
        result.update(
            stage="review",
            state="completed" if ticket.get("review_verdict") == "approve" else "unknown",
            actor_id=_actor(review.get("reviewed_by_agent_id") or ticket.get("reviewed_by_agent_id")),
            updated_at=reviewed_at or _text(ticket.get("closed_at") or ticket.get("updated_at"), 40),
            freshness="fresh",
            next_action=(
                "Determine the configured delivery boundary; review approval is recorded."
                if ticket.get("review_verdict") == "approve"
                else "Inspect the terminal review evidence."
            ),
        )
        delivery_state = delivery.get("state")
        if delivery_state:
            result["actor_id"] = _actor(
                (delivery_annotation.get("by") or {}).get("agent_id")
                if isinstance(delivery_annotation.get("by"), Mapping)
                else None
            ) or result["actor_id"]
            result["updated_at"] = _text(delivery_annotation.get("at"), 40) or result["updated_at"]
            result["freshness"] = "fresh"
            if delivery_state in {"pr_pending", "pr_created", "pr_blocked", "pr_uncertain"}:
                result["stage"] = "delivery"
                result["state"] = {
                    "pr_pending": "waiting",
                    "pr_created": "running",
                    "pr_blocked": "blocked",
                    "pr_uncertain": "unknown",
                }[str(delivery_state)]
                result["next_action"] = {
                    "pr_pending": "Create or reconcile the configured pull request.",
                    "pr_created": "Confirm whether the pull request is the configured completion boundary.",
                    "pr_blocked": "Resolve the recorded pull-request delivery blocker.",
                    "pr_uncertain": "Reconcile the unconfirmed pull-request outcome.",
                }[str(delivery_state)]
                if delivery_state in {"pr_blocked", "pr_uncertain"}:
                    result["blocking_reason"] = (
                        _text(delivery.get("reason")).replace("_", " ")
                        if _text(delivery.get("reason"))
                        else "Pull-request delivery needs reconciliation."
                    )
            elif delivery_state in {"integration_pending", "integration_blocked", "integration_merged"}:
                result["stage"] = "integration"
                result["state"] = {
                    "integration_pending": "waiting",
                    "integration_blocked": "blocked",
                    "integration_merged": "completed",
                }[str(delivery_state)]
                result["next_action"] = {
                    "integration_pending": "Wait for authoritative integration evidence.",
                    "integration_blocked": "Resolve the recorded integration blocker.",
                    "integration_merged": "Confirm whether integration is the configured completion boundary.",
                }[str(delivery_state)]
                if delivery_state == "integration_blocked":
                    result["blocking_reason"] = (
                        _text(delivery.get("reason")).replace("_", " ")
                        if _text(delivery.get("reason"))
                        else "Integration is blocked."
                    )
            elif delivery_state in {"delivery_recorded", "release_pending", "released"}:
                result["stage"] = "delivery"
                result["state"] = "waiting" if delivery_state == "release_pending" else "completed"
                result["next_action"] = (
                    "Wait for authoritative release evidence."
                    if delivery_state == "release_pending"
                    else "No further lifecycle action is required."
                )

        boundary_reached = (
            boundary == "review" and ticket.get("review_verdict") == "approve"
            or boundary == "pull_request" and delivery.get("state") == "pr_created"
            or boundary == "integration" and delivery.get("state") == "integration_merged"
            or boundary == "delivery" and delivery.get("state") in {"delivery_recorded", "released"}
        )
        if boundary_reached:
            result.update(
                stage="completed",
                state="completed",
                freshness="fresh",
                next_action="No further lifecycle action is required at the configured boundary.",
            )

    else:
        result.update(
            stage="intake",
            state="unknown",
            updated_at=_text(ticket.get("updated_at"), 40) or created_at,
            next_action="Inspect the authoritative ticket state before acting.",
        )

    assert result["stage"] in STAGES
    assert result["state"] in STATES
    return result
