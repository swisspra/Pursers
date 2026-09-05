from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
sys.path.insert(0, str(REPOSITORY / "packages" / "client" / "src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

import pursers_wait_server as wait_server  # noqa: E402


class RawClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.agent_name = "keepalive-seat"
        self.role = "worker"
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if self.fail:
            raise RuntimeError("claim was lost")
        return {
            "ok": True,
            "ticket_id": arguments["ticket_id"],
            "ttl_s": 1,
            "lease_expires_at": "later",
        }


class Connection:
    def __init__(self, client: RawClient) -> None:
        self.value = client

    async def client(self) -> RawClient:
        return self.value


class NoDiscoveryKeepalive(wait_server.LeaseKeepalive):
    async def _discover(self) -> None:
        return None


class LeaseKeepaliveTests(unittest.IsolatedAsyncioTestCase):
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
        await asyncio.sleep(0.5)
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
        client = RawClient(fail=True)
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


if __name__ == "__main__":
    unittest.main()
