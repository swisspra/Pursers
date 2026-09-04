"""Shared journal event-kind contracts for Pursers consumers."""

from __future__ import annotations


TICKET_REVIEW_CLAIMED = "ticket_review_claimed"
REVIEW_LEASE_EXPIRED = "review_lease_expired"
REVIEW_LEASE_RELEASED = "review_lease_released"
TICKET_OFFERED = "ticket_offered"
OFFER_EXPIRED = "offer_expired"
OFFER_REVOKED = "offer_revoked"
REVIEW_OFFERED = "review_offered"

DISPATCH_KINDS = frozenset(
    {TICKET_OFFERED, OFFER_EXPIRED, OFFER_REVOKED, REVIEW_OFFERED}
)

REVIEW_LEASE_KINDS = frozenset(
    {TICKET_REVIEW_CLAIMED, REVIEW_LEASE_EXPIRED, REVIEW_LEASE_RELEASED}
)
SUBMISSION_KINDS = frozenset(
    {"ticket_status_changed", "ticket_submitted", "ticket_resubmitted"}
)
SUBMITTED_RELEVANT_KINDS = SUBMISSION_KINDS | REVIEW_LEASE_KINDS | DISPATCH_KINDS
