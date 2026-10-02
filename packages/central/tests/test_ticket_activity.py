from __future__ import annotations

from datetime import datetime, timezone

import activity


NOW = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc).timestamp()


def project(ticket: dict) -> dict:
    return activity.project_ticket_activity(
        {"ticket_id": "TK-activity", "created_at": "2030-01-02T10:00:00Z", **ticket},
        board_id="pursers",
        now=NOW,
    )


def test_activity_schema_is_additive_bounded_and_truthful_for_legacy_ticket() -> None:
    result = project({"status": "open"})

    assert result == {
        "schema_version": 1,
        "stage": "intake",
        "state": "waiting",
        "attempt_id": None,
        "actor_id": None,
        "updated_at": "2030-01-02T10:00:00Z",
        "freshness": "unknown",
        "evidence_refs": ["board://pursers/ticket/TK-activity#created"],
        "next_action": "Queue the ticket for an eligible worker.",
        "completion_boundary": "unknown",
    }


def test_activity_tracks_work_validation_and_only_real_estimates() -> None:
    working = project(
        {
            "status": "in_progress",
            "work_attempt": 2,
            "claimed_by_agent_id": "AI-worker",
            "claimed_at": "2030-01-02T11:00:00Z",
            "lease_expires_at": "2030-01-02T12:15:00Z",
            "progress_freshness": "fresh",
            "progress": {
                "attempt": 2,
                "revision": 3,
                "low_percent": 35,
                "high_percent": 55,
                "confidence": "medium",
                "evidence": "Focused tests pass; integration remains.",
                "assessed_at": "2030-01-02T11:58:00Z",
                "fresh_until": "2030-01-02T12:20:00Z",
            },
        }
    )
    validating = project(
        {
            "status": "creating_report",
            "work_attempt": 1,
            "claimed_by_agent_id": "AI-worker",
            "claimed_at": "2030-01-02T11:00:00Z",
            "lease_expires_at": "2030-01-02T12:15:00Z",
        }
    )

    assert (working["stage"], working["state"], working["attempt_id"]) == (
        "work",
        "running",
        2,
    )
    assert working["actor_id"] == "AI-worker"
    assert working["freshness"] == "fresh"
    assert working["estimate"] == {
        "low_percent": 35,
        "high_percent": 55,
        "confidence": "medium",
        "evidence": "Focused tests pass; integration remains.",
        "assessed_at": "2030-01-02T11:58:00Z",
    }
    assert working["evidence_refs"][-1].endswith("#progress-2-3")
    assert validating["stage"] == "validation"
    assert validating["state"] == "running"
    assert validating["freshness"] == "unknown"
    assert "estimate" not in validating


def test_activity_distinguishes_retry_stale_expired_failed_and_canceled() -> None:
    retry = project(
        {
            "status": "claimed",
            "work_attempt": 3,
            "rejection_count": 1,
            "claimed_by_agent_id": "AI-worker",
            "claimed_at": "2030-01-02T11:30:00Z",
            "lease_expires_at": "2030-01-02T12:15:00Z",
            "review_history": [
                {
                    "verdict": "reject",
                    "reviewed_at": "2030-01-02T11:20:00Z",
                    "reviewed_by_agent_id": "AI-reviewer",
                }
            ],
        }
    )
    stale = project(
        {
            "status": "in_progress",
            "work_attempt": 1,
            "claimed_by_agent_id": "AI-worker",
            "lease_expires_at": "2030-01-02T12:15:00Z",
            "progress_freshness": "stale",
            "progress": {
                "attempt": 1,
                "revision": 1,
                "low_percent": 10,
                "high_percent": 20,
                "confidence": "low",
                "evidence": "Old checkpoint.",
                "assessed_at": "2030-01-02T10:00:00Z",
                "fresh_until": "2030-01-02T11:00:00Z",
            },
        }
    )
    expired = project(
        {
            "status": "claimed",
            "work_attempt": 1,
            "claimed_by_agent_id": "AI-worker",
            "lease_expires_at": "2030-01-02T11:59:59Z",
        }
    )
    failed = project(
        {
            "status": "rejected",
            "work_attempt": 1,
            "review_verdict": "reject",
            "reviewed_at": "2030-01-02T11:50:00Z",
            "reviewed_by_agent_id": "AI-reviewer",
        }
    )
    canceled = project(
        {
            "status": "canceled",
            "work_attempt": 1,
            "canceled_at": "2030-01-02T11:55:00Z",
            "last_claimed_by_agent_id": "AI-worker",
        }
    )

    assert (retry["state"], retry["attempt_id"], retry["actor_id"]) == (
        "retrying",
        3,
        "AI-worker",
    )
    assert stale["state"] == "stale"
    assert stale["freshness"] == "stale"
    assert stale["estimate"]["low_percent"] == 10
    assert expired["state"] == "stale"
    assert expired["blocking_reason"] == "The work lease expired."
    assert failed["stage"] == "review"
    assert failed["state"] == "failed"
    assert canceled["state"] == "canceled"
    assert canceled["actor_id"] == "AI-worker"


def test_activity_retry_offer_does_not_mix_rejected_attempt_with_next_actor() -> None:
    rejected_review = [
        {
            "verdict": "reject",
            "reviewed_at": "2030-01-02T11:20:00Z",
            "reviewed_by_agent_id": "AI-reviewer",
        }
    ]
    offered = project(
        {
            "status": "open",
            "work_attempt": 1,
            "rejection_count": 1,
            "review_history": rejected_review,
            "work_offer": {
                "agent_id": "AI-next",
                "offered_at": "2030-01-02T11:25:00Z",
            },
        }
    )
    reclaimed = project(
        {
            "status": "claimed",
            "work_attempt": 2,
            "rejection_count": 1,
            "review_history": rejected_review,
            "claimed_by_agent_id": "AI-next",
            "claimed_at": "2030-01-02T11:30:00Z",
            "lease_expires_at": "2030-01-02T12:15:00Z",
        }
    )

    assert (offered["stage"], offered["state"]) == ("work", "retrying")
    assert (offered["attempt_id"], offered["actor_id"]) == (None, None)
    assert (reclaimed["stage"], reclaimed["state"]) == ("work", "retrying")
    assert (reclaimed["attempt_id"], reclaimed["actor_id"]) == (2, "AI-next")


def test_activity_keeps_review_pr_integration_and_delivery_distinct() -> None:
    submitted = project(
        {
            "status": "submitted",
            "work_attempt": 1,
            "submitted_at": "2030-01-02T11:00:00Z",
            "submitted_by_agent_id": "AI-worker",
            "review_lease": {
                "reviewer_agent_id": "AI-reviewer",
                "claimed_at": "2030-01-02T11:05:00Z",
                "expires_at": "2030-01-02T12:15:00Z",
            },
        }
    )
    approved_unknown = project(
        {
            "status": "closed",
            "review_verdict": "approve",
            "reviewed_at": "2030-01-02T11:10:00Z",
            "reviewed_by_agent_id": "AI-reviewer",
        }
    )
    pr_created = project(
        {
            "status": "closed",
            "review_verdict": "approve",
            "reviewed_at": "2030-01-02T11:10:00Z",
            "annotations": [
                {
                    "annotation_id": "AN-pr",
                    "at": "2030-01-02T11:20:00Z",
                    "by": {"agent_id": "AI-butler"},
                    "text": 'pursers-delivery: {"state":"pr_created","pr_id":42}',
                }
            ],
        }
    )
    integrated = project(
        {
            "status": "closed",
            "review_verdict": "approve",
            "activity_completion_boundary": "integration",
            "annotations": [
                {
                    "annotation_id": "AN-merge",
                    "at": "2030-01-02T11:30:00Z",
                    "by": {"agent_id": "AI-butler"},
                    "text": 'pursers-delivery: {"state":"integration_merged","merge_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
                }
            ],
        }
    )
    delivered = project(
        {
            "status": "closed",
            "review_verdict": "approve",
            "activity_completion_boundary": "delivery",
            "annotations": [
                {
                    "annotation_id": "AN-delivery",
                    "at": "2030-01-02T11:40:00Z",
                    "by": {"agent_id": "AI-butler"},
                    "text": 'pursers-delivery: {"state":"delivery_recorded"}',
                }
            ],
        }
    )

    assert (submitted["stage"], submitted["state"], submitted["actor_id"]) == (
        "review",
        "running",
        "AI-reviewer",
    )
    assert approved_unknown["stage"] == "review"
    assert approved_unknown["state"] == "completed"
    assert approved_unknown["completion_boundary"] == "unknown"
    assert "delivery" in approved_unknown["next_action"].lower()
    assert pr_created["stage"] == "delivery"
    assert pr_created["state"] == "running"
    assert pr_created["completion_boundary"] == "unknown"
    assert integrated["stage"] == "completed"
    assert integrated["state"] == "completed"
    assert integrated["completion_boundary"] == "integration"
    assert delivered["stage"] == "completed"
    assert delivered["completion_boundary"] == "delivery"
    assert pr_created["evidence_refs"][-1].endswith("#annotation-AN-pr")


def test_activity_blockers_and_evidence_are_bounded() -> None:
    result = project(
        {
            "status": "open",
            "parked": True,
            "work_attempt": 4,
            "workflow_blocker": {"reason": "repeated_same_commit_and_feedback"},
            "dispatch_history": [
                {"state": "offered", "at": f"2030-01-02T11:{minute:02d}:00Z"}
                for minute in range(12)
            ],
            "submission_history": [{"submitted_at": "2030-01-02T11:20:00Z"}],
            "review_history": [{"verdict": "reject", "reviewed_at": "2030-01-02T11:30:00Z"}],
        }
    )

    assert result["state"] == "blocked"
    assert result["blocking_reason"] == "repeated same commit and feedback"
    assert result["next_action"] == "Resolve the workflow blocker before resuming."
    assert 1 <= len(result["evidence_refs"]) <= activity.MAX_EVIDENCE_REFS
    assert len(result["evidence_refs"]) == len(set(result["evidence_refs"]))


def test_activity_tolerates_old_invalid_optional_fields_without_mutation() -> None:
    ticket = {
        "ticket_id": "TK-legacy",
        "status": "open",
        "created_at": "not-a-time",
        "work_attempt": True,
        "rejection_count": "unknown",
        "progress": {"low_percent": 80, "high_percent": 20},
        "annotations": [{"text": "pursers-delivery: not-json"}],
    }
    before = {**ticket, "progress": dict(ticket["progress"]), "annotations": list(ticket["annotations"])}

    result = activity.project_ticket_activity(ticket, board_id="pursers", now=NOW)

    assert result["stage"] == "intake"
    assert result["state"] == "waiting"
    assert result["attempt_id"] is None
    assert "estimate" not in result
    assert ticket == before


def test_activity_uses_actual_writeback_and_delivery_blocker_facts() -> None:
    recorded = project(
        {
            "status": "closed",
            "review_verdict": "approve",
            "annotations": [
                {
                    "annotation_id": "AN-writeback",
                    "at": "2030-01-02T11:30:00Z",
                    "text": "source-writeback-sha256:" + "a" * 64,
                }
            ],
        }
    )
    blocked = project(
        {
            "status": "closed",
            "review_verdict": "approve",
            "annotations": [
                {
                    "annotation_id": "AN-blocked",
                    "at": "2030-01-02T11:40:00Z",
                    "text": 'pursers-delivery: {"state":"integration_blocked","reason":"validation_failed","completion_boundary":"integration"}',
                }
            ],
        }
    )

    assert recorded["stage"] == "delivery"
    assert recorded["state"] == "completed"
    assert recorded["completion_boundary"] == "unknown"
    assert blocked["stage"] == "integration"
    assert blocked["state"] == "blocked"
    assert blocked["completion_boundary"] == "integration"
    assert blocked["blocking_reason"] == "validation failed"
