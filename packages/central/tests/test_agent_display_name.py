from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

import central


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class AgentDisplayNameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=PACKAGE_ROOT)
        self.root = Path(self.temp_dir.name)
        jwks_path = self.root / "jwks.json"
        jwks_path.write_text('{"keys": []}', encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "CENTRAL_AUTH_MODE": "jwt",
                "CENTRAL_JWT_ISSUER": "https://issuer.example",
                "CENTRAL_JWT_AUDIENCE": "http://localhost:8765/mcp",
                "CENTRAL_JWKS_PATH": str(jwks_path),
                "CENTRAL_ADMISSION": "invite",
                "STORE_BACKEND": "sqlite",
            },
        )
        self.environment.start()
        self.mcp, self.service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.admin = central.Principal(
            "PR-admin",
            "admin",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.worker = central.Principal(
            "PR-worker",
            "worker-token-1",
            frozenset({"board:read", "board:write"}),
        )
        self.other = central.Principal(
            "PR-other",
            "other",
            frozenset({"board:read", "board:write"}),
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        self.admin_ids: dict[str, str] = {}
        self.worker_ids: dict[str, str] = {}
        self.other_ids: dict[str, str] = {}
        for board_id in ("alpha", "beta"):
            joined = await self.call(board_id, "board_join", agent_name="admin-agent")
            self.admin_ids[board_id] = joined.structured_content["agent_id"]
            for principal in (self.worker, self.other):
                await self.call(
                    board_id,
                    "board_member_add",
                    agent_name="admin-agent",
                    principal_id=principal.principal_id,
                    role="member",
                )
            self.principal = self.worker
            joined = await self.call(
                board_id,
                "board_join",
                agent_name="worker-seat",
                capabilities={"can_work": True, "can_review": False},
            )
            self.worker_ids[board_id] = joined.structured_content["agent_id"]
            self.principal = self.other
            joined = await self.call(
                board_id,
                "board_join",
                agent_name="other-seat",
                capabilities={"can_work": True, "can_review": False},
            )
            self.other_ids[board_id] = joined.structured_content["agent_id"]
            self.principal = self.admin

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, board_id: str, name: str, **arguments: object):
        return await self.mcp.call_tool(name, {"board_id": board_id, **arguments})

    async def set_name(
        self,
        board_id: str,
        agent_name: str,
        display_name: str | None,
        expected_revision: int,
        target_agent_id: str | None = None,
    ):
        arguments: dict[str, object] = {
            "agent_name": agent_name,
            "display_name": display_name,
            "expected_revision": expected_revision,
        }
        if target_agent_id is not None:
            arguments["target_agent_id"] = target_agent_id
        return await self.call(board_id, "agent_display_name_set", **arguments)

    async def test_self_update_persists_without_changing_identity_or_lease(self) -> None:
        self.principal = self.admin
        await self.call(
            "alpha",
            "ticket_create",
            ticket_id="TK-profile-lease",
            agent_name="admin-agent",
            title="profile lease invariant",
            assigned_to_agent_id=self.worker_ids["alpha"],
        )
        now = time.time()

        def seed_claim(document: dict[str, object]) -> None:
            tickets = document["tickets"]
            assert isinstance(tickets, dict)
            ticket = tickets["TK-profile-lease"]
            assert isinstance(ticket, dict)
            ticket.pop("work_offer", None)
            ticket["status"] = "claimed"
            ticket["claimed_by_agent_id"] = self.worker_ids["alpha"]
            ticket["claimed_by_principal_id"] = self.worker.principal_id
            ticket["claimed_by"] = "worker-seat"
            ticket["claimed_at"] = central.iso_at(now)
            ticket["ttl_s"] = 900
            ticket["lease_expires_at_epoch"] = now + 900
            ticket["lease_expires_at"] = central.iso_at(now + 900)

        self.service.mutate("alpha", seed_claim, require_generation=False)
        self.principal = self.worker
        before = self.service.load("alpha")
        ticket_before = before["tickets"]["TK-profile-lease"]
        identity_before = dict(before["members"][self.worker_ids["alpha"]])

        changed = await self.set_name(
            "alpha", "worker-seat", "  อรุณ 🌤️  ", 0
        )
        profile = changed.structured_content["profile"]
        self.assertEqual(profile["display_name"], "อรุณ 🌤️")
        self.assertEqual(profile["revision"], 1)
        self.assertTrue(changed.structured_content["changed"])

        after = self.service.load("alpha")
        ticket_after = after["tickets"]["TK-profile-lease"]
        member_after = after["members"][self.worker_ids["alpha"]]
        for field in ("agent_id", "agent_name", "principal_id", "role", "capabilities"):
            self.assertEqual(member_after[field], identity_before[field])
        self.assertEqual(
            ticket_after["claimed_by_agent_id"], ticket_before["claimed_by_agent_id"]
        )
        self.assertEqual(
            ticket_after["lease_expires_at_epoch"],
            ticket_before["lease_expires_at_epoch"],
        )
        audit = after["agent_profile_audit"][-1]
        self.assertIsNone(audit["old_display_name"])
        self.assertEqual(audit["new_display_name"], "อรุณ 🌤️")

        snapshot = await self.call("alpha", "board_snapshot")
        projected = next(
            row
            for row in snapshot.structured_content["agents"]
            if row["agent_id"] == self.worker_ids["alpha"]
        )
        self.assertEqual(projected["display_name"], "อรุณ 🌤️")
        self.assertEqual(projected["profile"]["display_label"], "อรุณ 🌤️")

    async def test_revision_conflict_reset_and_old_record_fallback(self) -> None:
        self.principal = self.worker
        await self.set_name("alpha", "worker-seat", "First", 0)
        with self.assertRaisesRegex(ToolError, "display-name revision conflict"):
            await self.set_name("alpha", "worker-seat", "Stale", 0)
        reset = await self.set_name("alpha", "worker-seat", None, 1)
        self.assertIsNone(reset.structured_content["profile"]["display_name"])
        self.assertEqual(
            reset.structured_content["profile"]["display_label"], "worker-seat"
        )
        self.assertEqual(reset.structured_content["profile"]["revision"], 2)

        snapshot = await self.call("beta", "board_snapshot")
        old = next(
            row
            for row in snapshot.structured_content["agents"]
            if row["agent_id"] == self.worker_ids["beta"]
        )
        self.assertNotIn("display_name", old)
        self.assertEqual(old["profile"]["display_label"], "worker-seat")
        self.assertEqual(old["profile"]["revision"], 0)

    async def test_permissions_duplicates_cross_board_and_admin_edit(self) -> None:
        self.principal = self.other
        with self.assertRaisesRegex(
            ToolError, "requires identity ownership or board admin"
        ):
            await self.set_name(
                "alpha",
                "other-seat",
                "Not mine",
                0,
                self.worker_ids["alpha"],
            )

        self.principal = self.admin
        admin_changed = await self.set_name(
            "alpha",
            "admin-agent",
            "Shared label",
            0,
            self.worker_ids["alpha"],
        )
        self.assertEqual(
            admin_changed.structured_content["profile"]["agent_id"],
            self.worker_ids["alpha"],
        )
        await self.set_name(
            "alpha",
            "admin-agent",
            "Shared label",
            0,
            self.other_ids["alpha"],
        )
        alpha = self.service.load("alpha")
        self.assertEqual(
            alpha["members"][self.worker_ids["alpha"]]["display_name"],
            alpha["members"][self.other_ids["alpha"]]["display_name"],
        )
        beta = self.service.load("beta")
        self.assertNotIn("display_name", beta["members"][self.worker_ids["beta"]])

    async def test_validation_accepts_literal_markup_but_rejects_controls(self) -> None:
        self.principal = self.worker
        literal = '<img src=x onerror="alert(1)">'
        stored = await self.set_name("alpha", "worker-seat", literal, 0)
        self.assertEqual(stored.structured_content["profile"]["display_name"], literal)
        with self.assertRaisesRegex(ToolError, "control characters"):
            await self.set_name("beta", "worker-seat", "line\nbreak", 0)
        with self.assertRaisesRegex(ToolError, "bidirectional control"):
            await self.set_name("beta", "worker-seat", "safe\u202eevil", 0)
        with self.assertRaisesRegex(ToolError, "use null to reset"):
            await self.set_name("beta", "worker-seat", "   ", 0)

    async def test_revoked_deleted_and_retired_identities_cannot_update(self) -> None:
        self.principal = self.admin
        await self.call(
            "beta",
            "board_member_remove",
            agent_name="admin-agent",
            principal_id=self.worker.principal_id,
        )
        with self.assertRaisesRegex(ToolError, "target agent not found"):
            await self.set_name(
                "beta",
                "admin-agent",
                "Deleted",
                0,
                self.worker_ids["beta"],
            )
        self.principal = self.worker
        with self.assertRaisesRegex(ToolError, "board access denied"):
            await self.set_name("beta", "worker-seat", "Revoked", 0)

        self.principal = self.admin
        retired = await self.call(
            "alpha",
            "agent_retire",
            agent_name="admin-agent",
            target_agent_id=self.other_ids["alpha"],
        )
        self.assertEqual(
            retired.structured_content["agent"]["lifecycle_status"], "retired"
        )
        with self.assertRaisesRegex(ToolError, "target agent is not active"):
            await self.set_name(
                "alpha",
                "admin-agent",
                "Retired",
                0,
                self.other_ids["alpha"],
            )

    async def test_profile_survives_restart_and_refreshed_token_subject(self) -> None:
        self.principal = self.worker
        await self.set_name("alpha", "worker-seat", "Persistent", 0)
        refreshed = central.Principal(
            self.worker.principal_id,
            "worker-token-2",
            self.worker.scopes,
        )
        self.principal = refreshed
        restarted_mcp, _restarted_service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.mcp = restarted_mcp
        joined = await self.call(
            "alpha",
            "board_onboard",
            agent_name="worker-seat",
            role="worker",
            allow_takeover=True,
            capabilities={"can_work": True, "can_review": False},
        )
        self.assertEqual(joined.structured_content["agent_id"], self.worker_ids["alpha"])
        self.assertEqual(
            joined.structured_content["profile"]["display_name"], "Persistent"
        )
