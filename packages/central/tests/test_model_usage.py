from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import central
from mcp.server.mcpserver.exceptions import ToolError


class TicketModelUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1]
        )
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
        self.mcp, _service = central.build_server(
            "localhost", 8765, self.root / "data"
        )
        self.admin = central.Principal(
            "PR-admin",
            "admin-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.worker = central.Principal(
            "PR-worker",
            "worker-canonical",
            frozenset({"board:read", "board:write"}),
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call(
            "board_join",
            agent_name="orchestrator-seat",
            capabilities={
                "host": "codex",
                "provider": "openai",
                "model": "gpt-test",
                "can_work": True,
                "can_review": False,
            },
        )
        await self.call(
            "board_join",
            agent_name="reviewer-seat",
            role="reviewer",
            capabilities={
                "host": "codex",
                "provider": "openai",
                "model": "gpt-review",
                "can_work": False,
                "can_review": True,
            },
        )
        await self.call(
            "board_member_add",
            agent_name="orchestrator-seat",
            principal_id=self.worker.principal_id,
            role="member",
        )
        self.principal = self.worker
        await self.call(
            "board_join",
            agent_name="worker-seat",
            capabilities={
                "host": "headless",
                "provider": "openai-compatible",
                "model": "worker-model",
                "can_work": True,
                "can_review": False,
            },
        )

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name, {"board_id": "pursers", **arguments}
        )

    @staticmethod
    def usage(turns: int, input_tokens: int, output_tokens: int) -> dict[str, int]:
        return {
            "schema_version": 1,
            "turns": turns,
            "reported_turns": turns,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    async def test_full_ticket_split_is_queryable_and_contains_no_model_content(self) -> None:
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="orchestrator-seat",
            title="measure a normal ticket",
            description="usage counters only",
            target_url="pursers/packages/central",
            scope="interactive",
            required_fields=["test_output"],
            assigned_to="worker-seat",
            model_usage=self.usage(3, 120, 30),
        )
        ticket_id = created.structured_content["ticket"]["ticket_id"]

        self.principal = self.worker
        await self.call("ticket_claim", agent_name="worker-seat", ticket_id=ticket_id)
        with self.assertRaisesRegex(ToolError, "unsupported model_usage fields"):
            await self.call(
                "ticket_submit",
                agent_name="worker-seat",
                ticket_id=ticket_id,
                summary="must be rejected",
                model_usage={
                    **self.usage(1, 10, 2),
                    "prompt_text": "forbidden prompt copy",
                },
            )
        submitted = await self.call(
            "ticket_submit",
            agent_name="worker-seat",
            ticket_id=ticket_id,
            summary="implemented",
            model_usage=self.usage(5, 500, 80),
        )
        self.assertFalse(submitted.is_error)

        self.principal = self.admin
        reviewed = await self.call(
            "ticket_review",
            agent_name="reviewer-seat",
            ticket_id=ticket_id,
            verdict="approve",
            review_notes="verified",
            model_usage=self.usage(2, 210, 25),
        )
        self.assertFalse(reviewed.is_error)
        fetched = await self.call("ticket_get", ticket_id=ticket_id, view="full")
        ticket = fetched.structured_content["ticket"]
        roles = ticket["model_usage"]["roles"]
        self.assertEqual(
            {role: (row["turns"], row["input_tokens"], row["output_tokens"])
             for role, row in roles.items()},
            {
                "orchestrator": (3, 120, 30),
                "worker": (5, 500, 80),
                "reviewer": (2, 210, 25),
            },
        )
        self.assertEqual(roles["orchestrator"]["hosts"], ["codex"])
        self.assertEqual(roles["worker"]["hosts"], ["headless"])
        self.assertEqual(roles["reviewer"]["hosts"], ["codex"])
        rendered = json.dumps(ticket["model_usage"], sort_keys=True)
        self.assertNotIn("prompt", rendered)
        self.assertNotIn("completion", rendered)

    async def test_missing_host_usage_is_explicitly_null(self) -> None:
        self.principal = self.admin
        created = await self.call(
            "ticket_create",
            agent_name="orchestrator-seat",
            title="host reports no counters",
            description="retain null rather than estimate",
            target_url="pursers/packages/central",
            scope="interactive",
            required_fields=["test_output"],
        )
        usage = created.structured_content["ticket"]["creation_model_usage"]
        self.assertEqual(usage["host"], "codex")
        self.assertIsNone(usage["turns"])
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])


if __name__ == "__main__":
    unittest.main()
