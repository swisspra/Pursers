from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


class ClaimTtlTests(unittest.IsolatedAsyncioTestCase):
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
            "PR-admin", "admin", frozenset({"board:read", "board:write"})
        )
        self.worker = central.Principal(
            "PR-worker", "worker", frozenset({"board:read", "board:write"})
        )
        self.coordinator = central.Principal(
            "PR-coordinator", "coordinator",
            frozenset({"board:read", "board:coordinate"}),
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call("board_join", agent_name="admin-agent")
        for principal in (self.worker, self.coordinator):
            await self.call(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principal.principal_id,
                role="member",
            )
        self.principal = self.worker
        await self.call("board_join", agent_name="worker-agent")
        self.principal = self.coordinator
        await self.call(
            "board_join", agent_name="coordinator-agent", role="coordinator"
        )

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name, {"board_id": "pursers", **arguments}
        )

    async def create_ticket(self) -> str:
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="admin-agent",
            title="lease target",
            description="exercise live claim TTL",
            target_url="pursers/packages/central",
            scope="interactive-no-send",
            required_fields=["test_output"],
        )
        return created.structured_content["ticket"]["ticket_id"]

    async def test_admin_and_coordinator_can_change_live_ttl(self) -> None:
        self.principal = self.admin
        changed = await self.call(
            "board_claim_ttl_set", agent_name="admin-agent", claim_ttl_s=120
        )
        self.assertEqual(changed.structured_content["claim_ttl_s"], 120)
        self.assertEqual(
            changed.structured_content["event"]["kind"],
            "board_claim_ttl_changed",
        )
        self.assertEqual(changed.structured_content["event"]["claim_ttl_to"], 120)

        ticket_id = await self.create_ticket()
        self.principal = self.worker
        claimed = await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        self.assertEqual(claimed.structured_content["ttl_s"], 120)

        self.principal = self.coordinator
        changed = await self.call(
            "board_claim_ttl_set",
            agent_name="coordinator-agent",
            claim_ttl_s=240,
        )
        self.assertEqual(changed.structured_content["previous_claim_ttl_s"], 120)

        self.principal = self.worker
        renewed = await self.call(
            "lease_renew", agent_name="worker-agent", ticket_id=ticket_id
        )
        self.assertEqual(renewed.structured_content["ttl_s"], 240)

    async def test_worker_is_denied_and_bounds_are_enforced(self) -> None:
        self.principal = self.worker
        with self.assertRaisesRegex(ToolError, "board role not authorized"):
            await self.call(
                "board_claim_ttl_set",
                agent_name="worker-agent",
                claim_ttl_s=60,
            )
        self.principal = self.admin
        for invalid in (0, 86_401):
            with self.assertRaisesRegex(ToolError, "between 1 and 86400"):
                await self.call(
                    "board_claim_ttl_set",
                    agent_name="admin-agent",
                    claim_ttl_s=invalid,
                )

    async def test_claim_returns_successor_continuation_hint(self) -> None:
        ticket_id = await self.create_ticket()
        self.principal = self.worker
        await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        await self.call(
            "ticket_unclaim", agent_name="worker-agent", ticket_id=ticket_id
        )

        def add_prior_submission(document):
            document["tickets"][ticket_id]["submission_history"] = [
                {
                    "notes": (
                        "test_output: pass\n"
                        "branch_and_commit: codex/TK-old @ " + "a" * 40
                    )
                }
            ]

        self.service.mutate("pursers", add_prior_submission)
        claimed = await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        hint = claimed.structured_content["continuation"]
        self.assertEqual(hint["prior_holder"]["agent_name"], "worker-agent")
        self.assertEqual(
            hint["branch_and_commit"], "codex/TK-old @ " + "a" * 40
        )


if __name__ == "__main__":
    unittest.main()
