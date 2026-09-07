from __future__ import annotations

import asyncio

from pursers_client import (
    BoardClient,
    DISPATCH_KINDS,
    HELD_TICKET_KINDS,
    KNOWN_EVENT_KINDS,
    JoinedIdentity,
    REVIEWER_WAIT_KINDS,
    REVIEW_LEASE_EXPIRED,
    SUBMITTED_RELEVANT_KINDS,
    WORKER_WAIT_KINDS,
)


def test_holder_wait_contract_uses_only_central_emitted_kinds() -> None:
    assert HELD_TICKET_KINDS == {
        "ticket_annotated",
        "ticket_status_changed",
        "human_input_resolved",
        "ticket_parked",
        "ticket_unparked",
        REVIEW_LEASE_EXPIRED,
    }
    assert "ticket_cancelled" not in HELD_TICKET_KINDS
    assert "lease_expired" not in HELD_TICKET_KINDS
    assert HELD_TICKET_KINDS <= KNOWN_EVENT_KINDS


def test_role_wait_contracts_include_dispatch_submission_and_holder_updates() -> None:
    assert WORKER_WAIT_KINDS == DISPATCH_KINDS | HELD_TICKET_KINDS
    assert REVIEWER_WAIT_KINDS == SUBMITTED_RELEVANT_KINDS | HELD_TICKET_KINDS


def test_only_mine_retains_rejection_for_the_submitting_holder() -> None:
    async def exercise() -> None:
        board = BoardClient("http://central.invalid/mcp", "TOKEN_PLACEHOLDER", "pursers")
        board.identity = JoinedIdentity(
            "pursers", "AI-worker", "PR-worker", "worker-a", "worker"
        )

        async def call_with(_client, _name, _arguments):
            return {"ticket": {
                "ticket_id": "TK-rejected",
                "status": "in_progress",
                "claimed_by_agent_id": "AI-successor",
            }}

        board._call_with = call_with  # type: ignore[method-assign]
        event = {
            "kind": "ticket_status_changed",
            "ticket_id": "TK-rejected",
            "submitted_by_agent_id": "AI-worker",
            "status_from": "submitted",
            "status_to": "open",
        }
        assert await board._event_matches(
            object(), event, kinds=WORKER_WAIT_KINDS, only_mine=True
        )

    asyncio.run(exercise())
