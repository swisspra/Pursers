from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
sys.path.insert(0, str(REPOSITORY / "packages" / "client" / "src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

import pursers_wait_server as wait_server  # noqa: E402


class RawClient:
    def __init__(
        self, *, fail: bool = False, ticket: dict[str, Any] | None = None
    ) -> None:
        self.agent_name = "keepalive-seat"
        self.role = "worker"
        self.fail = fail
        self.ticket = ticket or {
            "ticket_id": "TK-lost",
            "status": "open",
            "last_release_reason": "lease expired",
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "lease_renew" and self.fail:
            raise RuntimeError("claim was lost")
        if name == "ticket_get":
            payload = {"ticket": self.ticket}
        else:
            payload = {
                "ok": True,
                "ticket_id": arguments["ticket_id"],
                "ttl_s": 1,
                "lease_expires_at": "later",
            }
        return SimpleNamespace(
            is_error=False, structured_content=payload, content=[]
        )


class Connection:
    def __init__(self, client: RawClient) -> None:
        self.value = client

    async def client(self) -> RawClient:
        return self.value


class NoDiscoveryKeepalive(wait_server.LeaseKeepalive):
    async def _discover(self) -> None:
        return None

    async def _subscribe(self) -> None:
        await self.stopped.wait()


class ClaimOnDiscoveryKeepalive(NoDiscoveryKeepalive):
    async def _discover(self) -> None:
        self.discoveries = getattr(self, "discoveries", 0) + 1
        self.observe_join(
            "pursers",
            {
                "agent_name": "keepalive-seat",
                "claim_ttl_s": 1,
                "renewed_leases": [
                    {
                        "ticket_id": "TK-new",
                        "lease_kind": "work",
                        "ttl_s": 1,
                    }
                ],
            },
        )


class LeaseKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    def test_join_and_claim_results_populate_full_holder_identity(self) -> None:
        keepalive = NoDiscoveryKeepalive(Connection(RawClient()))
        keepalive.observe_join(
            "pursers",
            {
                "agent_name": "keepalive-seat",
                "agent_id": "AI-ours",
                "principal_id": "PR-ours",
                "claim_ttl_s": 30,
                "renewed_leases": [
                    {"ticket_id": "TK-joined", "lease_kind": "work", "ttl_s": 30}
                ],
            },
        )
        keepalive.observe_claim(
            "pursers",
            {
                "ok": True,
                "ticket": {
                    "ticket_id": "TK-work",
                    "claimed_by": "keepalive-seat",
                    "claimed_by_agent_id": "AI-ours",
                    "claimed_by_principal_id": "PR-ours",
                },
            },
        )
        keepalive.observe_claim(
            "pursers",
            {
                "ok": True,
                "ticket": {"ticket_id": "TK-review"},
                "review_lease": {
                    "reviewer_agent_name": "keepalive-seat",
                    "reviewer_agent_id": "AI-ours",
                    "reviewer_principal_id": "PR-ours",
                    "ttl_s": 30,
                },
            },
            lease_kind="review",
        )

        for ticket_id in ("TK-joined", "TK-work", "TK-review"):
            tracked = keepalive.leases[("pursers", ticket_id)]
            self.assertEqual(tracked["agent_id"], "AI-ours")
            self.assertEqual(tracked["principal_id"], "PR-ours")

    async def test_background_renews_outside_wait_and_stops_on_terminal(self) -> None:
        client = RawClient()
        keepalive = NoDiscoveryKeepalive(Connection(client))
        keepalive.board_ttls["pursers"] = 1
        keepalive.observe_claim(
            "pursers",
            {
                "ok": True,
                "ttl_s": 1,
                "ticket": {
                    "ticket_id": "TK-held",
                    "claimed_by": "keepalive-seat",
                },
            },
        )
        keepalive.start()
        await asyncio.sleep(0.7)
        self.assertFalse(
            keepalive.task.done(),
            repr(keepalive.task.exception()) if keepalive.task.done() else "",
        )
        renewals = [call for call in client.calls if call[0] == "lease_renew"]
        self.assertEqual(len(renewals), 1)

        keepalive.observe_event(
            "pursers",
            {
                "kind": "ticket_submitted",
                "ticket_id": "TK-held",
                "status_to": "submitted",
            },
        )
        await asyncio.sleep(0.5)
        self.assertEqual(
            len([call for call in client.calls if call[0] == "lease_renew"]), 1
        )
        await keepalive.stop()

    async def test_lost_claim_surfaces_once(self) -> None:
        client = RawClient(
            fail=True,
            ticket={
                "ticket_id": "TK-lost",
                "status": "submitted",
                "review_lease": {
                    "reviewer_agent_name": "another-reviewer"
                },
            },
        )
        keepalive = NoDiscoveryKeepalive(Connection(client))
        keepalive.board_ttls["pursers"] = 1
        keepalive.observe_lease(
            "pursers", "TK-lost", {"lease_kind": "review", "ttl_s": 1}
        )
        keepalive.start()
        await asyncio.sleep(0.5)

        cues = keepalive.drain_cues({"pursers"})
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["kind"], "lease_keepalive_failed")
        self.assertEqual(cues[0]["lease_kind"], "review")
        await asyncio.sleep(0.5)
        self.assertEqual(keepalive.drain_cues({"pursers"}), [])
        await keepalive.stop()

    async def test_same_name_different_work_identity_stops_retrying(self) -> None:
        client = RawClient(
            fail=True,
            ticket={
                "ticket_id": "TK-reclaimed-work",
                "status": "claimed",
                "claimed_by": "keepalive-seat",
                "claimed_by_agent_id": "AI-other",
                "claimed_by_principal_id": "PR-other",
            },
        )
        keepalive = NoDiscoveryKeepalive(Connection(client))
        keepalive.observe_lease(
            "pursers",
            "TK-reclaimed-work",
            {
                "lease_kind": "work",
                "ttl_s": 30,
                "agent_name": "keepalive-seat",
                "agent_id": "AI-ours",
                "principal_id": "PR-ours",
            },
        )

        await keepalive._renew("pursers", "TK-reclaimed-work")
        await keepalive._renew("pursers", "TK-reclaimed-work")

        self.assertNotIn(("pursers", "TK-reclaimed-work"), keepalive.leases)
        self.assertEqual(
            len([call for call in client.calls if call[0] == "lease_renew"]), 1
        )
        self.assertEqual(len(keepalive.drain_cues({"pursers"})), 1)

    async def test_same_name_different_review_identity_stops_retrying(self) -> None:
        client = RawClient(
            fail=True,
            ticket={
                "ticket_id": "TK-reclaimed-review",
                "status": "submitted",
                "review_lease": {
                    "reviewer_agent_name": "keepalive-seat",
                    "reviewer_agent_id": "AI-other",
                    "reviewer_principal_id": "PR-other",
                },
            },
        )
        keepalive = NoDiscoveryKeepalive(Connection(client))
        keepalive.observe_lease(
            "pursers",
            "TK-reclaimed-review",
            {
                "lease_kind": "review",
                "ttl_s": 30,
                "agent_name": "keepalive-seat",
                "agent_id": "AI-ours",
                "principal_id": "PR-ours",
            },
        )

        await keepalive._renew("pursers", "TK-reclaimed-review")
        await keepalive._renew("pursers", "TK-reclaimed-review")

        self.assertNotIn(("pursers", "TK-reclaimed-review"), keepalive.leases)
        self.assertEqual(
            len([call for call in client.calls if call[0] == "lease_renew"]), 1
        )
        self.assertEqual(len(keepalive.drain_cues({"pursers"})), 1)

    async def test_join_reconciles_released_lease_and_failure_classifies_submit(
        self,
    ) -> None:
        client = RawClient(
            fail=True,
            ticket={"ticket_id": "TK-done", "status": "submitted"},
        )
        keepalive = NoDiscoveryKeepalive(Connection(client))
        keepalive.observe_join(
            "pursers",
            {
                "agent_name": "keepalive-seat",
                "claim_ttl_s": 30,
                "renewed_leases": [
                    {"ticket_id": "TK-released", "lease_kind": "work", "ttl_s": 30}
                ],
            },
        )
        self.assertIn(("pursers", "TK-released"), keepalive.leases)
        keepalive.observe_join(
            "pursers",
            {
                "agent_name": "keepalive-seat",
                "claim_ttl_s": 30,
                "renewed_leases": [],
            },
        )
        self.assertNotIn(("pursers", "TK-released"), keepalive.leases)

        keepalive.observe_lease(
            "pursers",
            "TK-done",
            {"lease_kind": "work", "ttl_s": 30, "agent_name": "keepalive-seat"},
        )
        await keepalive._renew("pursers", "TK-done")
        self.assertNotIn(("pursers", "TK-done"), keepalive.leases)
        self.assertEqual(keepalive.drain_cues({"pursers"}), [])

    async def test_ttl_decrease_and_claim_signal_advance_discovery_and_renewal(
        self,
    ) -> None:
        client = RawClient()
        keepalive = ClaimOnDiscoveryKeepalive(Connection(client))
        old_due = asyncio.get_running_loop().time() + 360
        keepalive.next_discovery = old_due
        keepalive.observe_event(
            "pursers",
            {"kind": "board_claim_ttl_changed", "claim_ttl_to": 1},
        )
        self.assertLess(keepalive.next_discovery, old_due)

        # A journal resource cue for the subsequent claim forces immediate
        # authoritative join reconciliation, independent of a2a_wait.
        keepalive.signal_board_change()
        keepalive.start()
        await asyncio.sleep(0.7)
        self.assertFalse(
            keepalive.task.done(),
            repr(keepalive.task.exception()) if keepalive.task.done() else "",
        )
        self.assertGreaterEqual(keepalive.discoveries, 2)
        self.assertIn(("pursers", "TK-new"), keepalive.leases)
        await keepalive.stop()


if __name__ == "__main__":
    unittest.main()
