from __future__ import annotations

import json
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
from team_lifecycle import CAPABILITIES, STATE_KEY, TeamLifecycleService, create_sidecar_client


class TeamLifecycleRealCentralTests(unittest.IsolatedAsyncioTestCase):
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
        self.owner = central.Principal("PR-group-owner", "group-owner", scopes)
        self.stranger = central.Principal("PR-group-stranger", "group-stranger", scopes)
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

    def _client(self, name: str, *, capabilities=None) -> BoardClient:
        client = BoardClient(
            "http://central.invalid/mcp", "test-token", "home-board",
            agent_name=name, role="worker", capabilities=capabilities,
        )
        client._http = self._http  # type: ignore[method-assign]
        return client

    async def test_real_join_persistence_isolation_and_restart(self) -> None:
        with patch.object(client_module, "streamable_http_client", return_value=self.mcp):
            async with self._sidecar() as client:
                worker_a = await client.board_join(
                    agent_name="standalone-worker-a", role="worker",
                    capabilities={"can_work": True, "can_review": False},
                )
                worker_b = await client.board_join(
                    agent_name="standalone-worker-b", role="worker",
                    capabilities={"can_work": True, "can_review": False},
                )
                service = TeamLifecycleService(client, "home-board")
                created = await service.dispatch("create", {
                    "board": "home-board", "expected_revision": 0,
                    "name": "Delivery", "member_agent_ids": [worker_a["agent_id"], worker_b["agent_id"]],
                })
                self.assertTrue(created["ok"])
                group_id = created["groups"][0]["group_id"]
                mismatch = await service.dispatch("remove", {
                    "board": "other-board", "expected_revision": 1, "group_id": group_id,
                })
                unknown = await service.dispatch("update", {
                    "board": "home-board", "expected_revision": 1, "group_id": group_id,
                    "name": "Delivery", "member_agent_ids": ["AI-other-board"],
                })
                stale = await service.dispatch("remove", {
                    "board": "home-board", "expected_revision": 0, "group_id": group_id,
                })
                self.assertEqual(mismatch["error"]["code"], "board_mismatch")
                self.assertEqual(unknown["error"]["code"], "member_not_found")
                self.assertEqual(stale["error"]["code"], "conflict")

            self.principal = self.stranger
            with self.assertRaises(BoardClientError):
                async with self._client("unauthorized-group-actor", capabilities=CAPABILITIES):
                    pass

            self.principal = self.owner
            async with self._sidecar() as restarted:
                service = TeamLifecycleService(restarted, "home-board")
                viewed = await service.dispatch("list", {"board": "home-board"})
                self.assertEqual(viewed["groups"][0]["group_id"], group_id)
                updated = await service.dispatch("update", {
                    "board": "home-board", "expected_revision": 1, "group_id": group_id,
                    "name": "Delivery 2", "member_agent_ids": [worker_a["agent_id"]],
                })
                self.assertEqual(updated["revision"], 2)
                removed = await service.dispatch("remove", {
                    "board": "home-board", "expected_revision": 2, "group_id": group_id,
                })
                self.assertEqual(removed["groups"], [])

        document = self.store.load("home-board")
        saved = json.loads(document["state"][STATE_KEY]["value"])
        self.assertEqual(saved, {"groups": [], "revision": 3, "schema_version": 1})
        actor = document["members"][central.agent_id(
            "home-board", self.owner.principal_id, "pursers-home-team-lifecycle-worker",
        )]
        self.assertTrue(actor["capabilities_explicit"])
        self.assertFalse(actor["capabilities"]["can_work"])
        self.assertFalse(actor["capabilities"]["can_review"])


if __name__ == "__main__":
    unittest.main()
