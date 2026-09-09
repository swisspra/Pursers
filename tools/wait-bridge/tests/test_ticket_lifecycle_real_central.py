from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
sys.path.insert(0, str(REPOSITORY / "packages" / "central" / "src" / "pursers_central"))
sys.path.insert(0, str(REPOSITORY / "packages" / "client" / "src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

import central
import pursers_client.client as client_module
from pursers_client import BoardClient, BoardClientError
from ticket_lifecycle import CAPABILITIES, TicketLifecycleService, create_sidecar_client


class TicketLifecycleRealCentralTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        jwks = self.root / "jwks.json"
        jwks.write_text('{"keys": []}', encoding="utf-8")
        self.environment = patch.dict(os.environ, {
            "CENTRAL_AUTH_MODE": "jwt",
            "CENTRAL_JWT_ISSUER": "https://issuer.example",
            "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
            "CENTRAL_JWKS_PATH": str(jwks),
            "CENTRAL_ADMISSION": "invite",
            "STORE_BACKEND": "sqlite",
        })
        self.environment.start()
        self.mcp, self.store = central.build_server("localhost", 8765, self.root / "data")
        scopes = frozenset({"board:read", "board:write", "board:review"})
        self.owner = central.Principal("PR-home-owner", "home-owner", scopes)
        self.member = central.Principal("PR-home-member", "home-member", scopes)
        self.stranger = central.Principal("PR-home-stranger", "home-stranger", scopes)
        self.principal = self.owner
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temporary.cleanup()

    @asynccontextmanager
    async def _http(self):
        yield object()

    def _sidecar(self) -> BoardClient:
        client = create_sidecar_client(
            {"u": "http://central.invalid/mcp", "t": "test-token", "r": "worker"},
            "home-board",
        )
        client._http = self._http  # type: ignore[method-assign]
        return client

    def _client(self, name: str, *, capabilities=None, allow_takeover=False) -> BoardClient:
        client = BoardClient(
            "http://central.invalid/mcp", "test-token", "home-board",
            agent_name=name, role="worker", capabilities=capabilities,
            allow_takeover=allow_takeover,
        )
        client._http = self._http  # type: ignore[method-assign]
        return client

    async def test_join_persisted_lifecycle_authority_and_restart(self) -> None:
        payload = {
            "board": "home-board",
            "title": "Persisted Home ticket",
            "description": "Exercise the real Central and BoardClient path.",
            "target_url": "home/ticket-lifecycle",
            "scope": "interactive-no-send",
            "required_fields": ["branch_and_commit"],
        }
        with patch.object(client_module, "streamable_http_client", return_value=self.mcp):
            # This is the exact old shape: a real Central rejects it before join.
            invalid = self._client("old-invalid-sidecar", capabilities={
                "can_work": False,
                "can_review": False,
                "capabilities_explicit": True,
                "platform": "aionui-home",
            })
            with self.assertRaisesRegex(BoardClientError, "unsupported capability fields"):
                async with invalid:
                    pass

            async with self._sidecar() as client:
                lifecycle = TicketLifecycleService(client, "home-board")
                created = await lifecycle.dispatch("create", payload)
                self.assertTrue(created["ok"])
                ticket_id = created["ticket"]["ticket_id"]
                self.assertIsNone(created["ticket"]["assigned_to"])
                listed = await lifecycle.dispatch("list", {"board": "home-board"})
                fetched = await lifecycle.dispatch("get", {
                    "board": "home-board", "ticket_id": ticket_id,
                })
                mismatch = await lifecycle.dispatch("get", {
                    "board": "other-board", "ticket_id": ticket_id,
                })
                self.assertIn(ticket_id, {ticket["ticket_id"] for ticket in listed["tickets"]})
                self.assertEqual(fetched["ticket"]["status"], "open")
                self.assertEqual(mismatch["error"]["code"], "board_mismatch")

                added = await self.mcp.call_tool("board_member_add", {
                    "board_id": "home-board",
                    "agent_name": client.agent_name,
                    "principal_id": self.member.principal_id,
                    "role": "member",
                })
                self.assertFalse(added.is_error)

            self.principal = self.stranger
            with self.assertRaises(BoardClientError):
                async with self._client("unauthorized-home-actor", capabilities=CAPABILITIES):
                    pass

            self.principal = self.member
            async with self._client("other-home-actor", capabilities=CAPABILITIES) as other:
                denied = await TicketLifecycleService(other, "home-board").dispatch(
                    "cancel", {"board": "home-board", "ticket_id": ticket_id},
                )
                self.assertEqual(denied["error"]["code"], "permission_denied")
                self.assertEqual(
                    self.store.load("home-board")["tickets"][ticket_id]["status"],
                    "open",
                )

            self.principal = self.owner
            async with self._sidecar() as restarted:
                lifecycle = TicketLifecycleService(restarted, "home-board")
                self.assertEqual((await lifecycle.dispatch("get", {
                    "board": "home-board", "ticket_id": ticket_id,
                }))["ticket"]["status"], "open")
                canceled = await lifecycle.dispatch("cancel", {
                    "board": "home-board", "ticket_id": ticket_id,
                    "reason": "real Central regression complete",
                })
                self.assertEqual(canceled["ticket"]["status"], "canceled")

        document = self.store.load("home-board")
        member = document["members"][central.agent_id(
            "home-board", self.owner.principal_id,
            "pursers-home-ticket-lifecycle-worker",
        )]
        self.assertTrue(member["capabilities_explicit"])
        self.assertFalse(member["capabilities"]["can_work"])
        self.assertFalse(member["capabilities"]["can_review"])
        self.assertEqual(document["tickets"][ticket_id]["status"], "canceled")


if __name__ == "__main__":
    unittest.main()
