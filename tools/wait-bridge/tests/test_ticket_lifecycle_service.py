from __future__ import annotations

import os
import sys
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
sys.path.insert(0, str(REPOSITORY / "packages" / "client" / "src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from ticket_lifecycle import CAPABILITIES, TicketLifecycleService


class FakeClient:
    agent_name = "pursers-home-ticket-lifecycle-worker"

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def ticket_list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {"tickets": [{"ticket_id": "TK-1", "status": "submitted"}], "latest_seq": 9}

    async def ticket_get(self, ticket_id):
        self.calls.append(("get", ticket_id))
        return {"ticket": {"ticket_id": ticket_id, "status": "open"}, "latest_seq": 10}

    async def ticket_create(self, ticket_id, title, **kwargs):
        self.calls.append(("create", {"ticket_id": ticket_id, "title": title, **kwargs}))
        return {"ticket": {"ticket_id": "TK-new", "status": "open", "assigned_to": None}}

    async def ticket_cancel(self, ticket_id, *, reason=None):
        self.calls.append(("cancel", {"ticket_id": ticket_id, "reason": reason}))
        return {"ticket": {"ticket_id": ticket_id, "status": "canceled"}}


def test_real_operations_are_bounded_and_create_is_unassigned() -> None:
    client = FakeClient()
    service = TicketLifecycleService(client, "demo")
    created = asyncio.run(service.dispatch("create", {
        "board": "demo",
        "title": "Feature",
        "description": "Persist it",
        "target_url": "demo/feature",
        "scope": "interactive-no-send",
        "required_fields": ["branch_and_commit"],
    }))
    assert created["ticket"]["status"] == "open"
    create = dict(client.calls)["create"]
    assert create["ticket_id"] is None
    assert create["unassigned"] is True
    assert CAPABILITIES["can_work"] is False
    assert CAPABILITIES["can_review"] is False


def test_status_progression_is_read_from_board_and_no_submit_or_review_exists() -> None:
    client = FakeClient()
    service = TicketLifecycleService(client, "demo")
    listed = asyncio.run(service.dispatch("list", {"board": "demo"}))
    assert listed == {
        "ok": True,
        "board": "demo",
        "tickets": [{"ticket_id": "TK-1", "status": "submitted"}],
        "latest_seq": 9,
    }
    for operation in ("claim", "submit", "review"):
        result = asyncio.run(service.dispatch(operation, {"board": "demo"}))
        assert result["error"]["code"] == "unsupported_action"
    assert [call[0] for call in client.calls] == ["list"]


def test_board_isolation_and_cancel_use_central_authority() -> None:
    client = FakeClient()
    service = TicketLifecycleService(client, "demo")
    mismatch = asyncio.run(service.dispatch("cancel", {"board": "other", "ticket_id": "TK-1"}))
    assert mismatch["error"]["code"] == "board_mismatch"
    canceled = asyncio.run(service.dispatch("cancel", {"board": "demo", "ticket_id": "TK-1", "reason": "duplicate"}))
    assert canceled["ticket"]["status"] == "canceled"
    assert client.calls == [("cancel", {"ticket_id": "TK-1", "reason": "duplicate"})]
