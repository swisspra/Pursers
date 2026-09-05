"""Shared journal event-kind contracts for Pursers consumers."""

from __future__ import annotations


TICKET_REVIEW_CLAIMED = "ticket_review_claimed"
REVIEW_LEASE_EXPIRED = "review_lease_expired"
REVIEW_LEASE_RELEASED = "review_lease_released"
TICKET_OFFERED = "ticket_offered"
OFFER_EXPIRED = "offer_expired"
OFFER_REVOKED = "offer_revoked"
REVIEW_OFFERED = "review_offered"
DISPATCH_UNASSIGNABLE = "dispatch_unassignable"

DISPATCH_KINDS = frozenset(
    {TICKET_OFFERED, OFFER_EXPIRED, OFFER_REVOKED, REVIEW_OFFERED}
)
DISPATCH_EVENT_KINDS = DISPATCH_KINDS | frozenset({DISPATCH_UNASSIGNABLE})

REVIEW_LEASE_KINDS = frozenset(
    {TICKET_REVIEW_CLAIMED, REVIEW_LEASE_EXPIRED, REVIEW_LEASE_RELEASED}
)
SUBMISSION_KINDS = frozenset(
    {"ticket_status_changed", "ticket_submitted", "ticket_resubmitted"}
)
SUBMITTED_RELEVANT_KINDS = SUBMISSION_KINDS | REVIEW_LEASE_KINDS | DISPATCH_KINDS

CORE_EVENT_KINDS = frozenset(
    {
        "ticket_status_changed",
        "ticket_created",
        "memory_written",
        "coordinator_assignment",
        "coordinator_nudge",
    }
)
ADMISSION_EVENT_KINDS = frozenset(
    {"board_membership_changed", "board_invite_created"}
)
SCRUB_EVENT_KINDS = frozenset({"board_scrub_profile_changed"})
CLAIM_TTL_EVENT_KINDS = frozenset({"board_claim_ttl_changed"})
REVIEW_EVENT_KINDS = frozenset({"board_review_policy_changed"}) | REVIEW_LEASE_KINDS
DEPRECATION_EVENT_KINDS = frozenset({"deprecated_tool_warning"})

# Central imports this vocabulary rather than maintaining an independent set.
CENTRAL_EVENT_KINDS = (
    CORE_EVENT_KINDS
    | ADMISSION_EVENT_KINDS
    | SCRUB_EVENT_KINDS
    | CLAIM_TTL_EVENT_KINDS
    | REVIEW_EVENT_KINDS
    | DEPRECATION_EVENT_KINDS
    | DISPATCH_EVENT_KINDS
)

# Legacy client aliases remain accepted even though current Central emits the
# authoritative status-change cue instead.
LEGACY_EVENT_KINDS = frozenset(
    {"ticket_assigned", "ticket_submitted", "ticket_resubmitted"}
)
KNOWN_EVENT_KINDS = CENTRAL_EVENT_KINDS | LEGACY_EVENT_KINDS
