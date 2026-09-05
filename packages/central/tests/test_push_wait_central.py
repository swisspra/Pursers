from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402


class PushWaitCentralTests(unittest.IsolatedAsyncioTestCase):
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
            "admin-canonical",
            frozenset({"board:read", "board:write", "board:review"}),
        )
        self.worker = central.Principal(
            "PR-worker",
            "worker-canonical",
            frozenset({"board:read", "board:write"}),
        )
        self.worker_read_only = central.Principal(
            self.worker.principal_id,
            self.worker.canonical,
            frozenset({"board:read"}),
        )
        self.other = central.Principal(
            "PR-other",
            "other-canonical",
            frozenset({"board:read", "board:write"}),
        )
        self.coordinator = central.Principal(
            "PR-coordinator",
            "coordinator-canonical",
            frozenset({"board:read", "board:coordinate"}),
        )
        self.stranger = central.Principal(
            "PR-stranger", "stranger-canonical", frozenset({"board:read"})
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        joined = await self.call("board_join", agent_name="admin-agent")
        self.admin_id = joined.structured_content["agent_id"]
        for principal in (self.worker, self.other, self.coordinator):
            await self.call(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principal.principal_id,
                role="member",
            )
        self.worker_id = await self.join(self.worker, "worker-agent")
        self.other_id = await self.join(self.other, "other-agent")
        self.coordinator_id = await self.join(
            self.coordinator, "coordinator-agent"
        )

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(
            name, {"board_id": "pursers", **arguments}
        )

    async def join(self, principal: central.Principal, name: str) -> str:
        self.principal = principal
        role = "coordinator" if "board:coordinate" in principal.scopes else "worker"
        joined = await self.call("board_join", agent_name=name, role=role)
        self.assertFalse(joined.is_error)
        return joined.structured_content["agent_id"]

    async def create_ticket(self, *, assigned_to: str | None = None) -> str:
        self.principal = self.admin
        arguments: dict[str, object] = {
            "agent_name": "admin-agent",
            "title": "push wait target",
            "description": "exercise pure refetch and targeted cues",
            "target_url": "pursers/packages/central",
            "scope": "interactive-no-send",
            "required_fields": ["test_output"],
            "unassigned": assigned_to is None,
        }
        if assigned_to is not None:
            arguments["assigned_to"] = assigned_to
        created = await self.call("ticket_create", **arguments)
        self.assertFalse(created.is_error)
        return created.structured_content["ticket"]["ticket_id"]

    def persisted_documents(self) -> list[tuple[str, str, int]]:
        connection = sqlite3.connect(self.service.store.db_path)
        try:
            return connection.execute(
                "SELECT path, doc, version FROM documents ORDER BY path"
            ).fetchall()
        finally:
            connection.close()

    async def authorize_subscription(
        self, principal: central.Principal, uri: str
    ) -> object:
        self.principal = principal
        request = SimpleNamespace(
            method="subscriptions/listen",
            params={
                "notifications": {"resourceSubscriptions": [uri]}
            },
            meta={},
        )

        async def accepted(_ctx: object) -> object:
            return {"accepted": True}

        return await central.SubscriptionAuthorization(self.service)(
            request, accepted
        )

    async def assert_target_only_cue(
        self,
        action,
    ) -> object:
        target_uri = f"board://pursers/agent/{self.worker_id}"
        other_uri = f"board://pursers/agent/{self.other_id}"
        async with (
            Client(self.mcp, mode="2026-07-28", cache=None) as target_client,
            Client(self.mcp, mode="2026-07-28", cache=None) as other_client,
        ):
            self.principal = self.worker
            async with target_client.listen(
                resource_subscriptions=[target_uri]
            ) as target_subscription:
                self.principal = self.other
                async with other_client.listen(
                    resource_subscriptions=[other_uri]
                ) as other_subscription:
                    result = await action()
                    await asyncio.wait_for(anext(target_subscription), timeout=1)
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(
                            anext(other_subscription), timeout=0.05
                        )
                    return result

    async def test_touch_false_leaves_all_persisted_documents_byte_identical(
        self,
    ) -> None:
        ticket_id = await self.create_ticket(assigned_to=self.worker_id)
        self.principal = self.worker
        claimed = await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        self.assertFalse(claimed.is_error)
        self.service.cursors.ack(
            self.worker.principal_id, "worker-agent", "pursers", 1
        )

        def expire(document: dict[str, Any]) -> dict[str, Any]:
            ticket = document["tickets"][ticket_id]
            ticket["lease_expires_at_epoch"] = 0
            ticket["lease_expires_at"] = "1970-01-01T00:00:00+00:00"
            return {}

        self.service.mutate("pursers", expire, require_generation=False)
        before = self.persisted_documents()

        for principal in (self.worker, self.worker_read_only):
            with self.subTest(scopes=sorted(principal.scopes)):
                self.principal = principal
                result = await self.call(
                    "board_catchup",
                    agent_name="worker-agent",
                    cursor=0,
                    ack=True,
                    touch=False,
                    expected_generation="GEN-stale-reader-token",
                )
                self.assertFalse(result.is_error)
                self.assertFalse(result.structured_content["touched"])
                self.assertEqual(
                    result.structured_content["acknowledged_cursor"], 1
                )
                self.assertEqual(self.persisted_documents(), before)

        ticket = self.service.load("pursers")["tickets"][ticket_id]
        self.assertEqual(ticket["status"], "claimed")
        self.assertEqual(ticket["lease_expires_at_epoch"], 0)
        self.assertEqual(
            self.service.cursors.get(
                self.worker.principal_id, "worker-agent", "pursers"
            ),
            1,
        )

    async def test_touch_true_keeps_compatibility_side_effects(self) -> None:
        await self.create_ticket()
        self.principal = self.worker
        before = self.service.load("pursers")["members"][self.worker_id][
            "last_activity_at"
        ]
        call_time = datetime.fromisoformat(before).timestamp() + 60
        with patch.object(central.time, "time", return_value=call_time):
            result = await self.call(
                "board_catchup",
                agent_name="worker-agent",
                cursor=0,
                ack=True,
            )
        self.assertFalse(result.is_error)
        self.assertTrue(result.structured_content["touched"])
        self.assertNotEqual(
            self.service.load("pursers")["members"][self.worker_id][
                "last_activity_at"
            ],
            before,
        )
        self.assertGreater(
            self.service.cursors.get(
                self.worker.principal_id, "worker-agent", "pursers"
            ),
            0,
        )

    async def test_per_seat_subscription_authorization_matrix(self) -> None:
        self.assertEqual(
            await self.authorize_subscription(
                self.worker, f"board://pursers/agent/{self.worker_id}"
            ),
            {"accepted": True},
        )
        self.assertEqual(
            await self.authorize_subscription(
                self.worker, "board://pursers/journal"
            ),
            {"accepted": True},
        )
        denied = (
            (self.worker, f"board://pursers/agent/{self.other_id}"),
            (self.stranger, f"board://pursers/agent/{self.worker_id}"),
            (self.stranger, "board://pursers/journal"),
            (self.worker, f"board://missing/agent/{self.worker_id}"),
            (self.worker, "board://pursers/agent"),
            (self.worker, "board://pursers/agent/"),
            (self.worker, f"board://pursers/agent/{self.worker_id}/extra"),
            (self.worker, "board://pursers/agent/%2F"),
            (self.worker, f"board://pursers/agent/{self.worker_id}?extra=1"),
            (self.worker, "https://pursers/agent/AI-invalid"),
        )
        for principal, uri in denied:
            with self.subTest(principal=principal.principal_id, uri=uri):
                with self.assertRaises(MCPError):
                    await self.authorize_subscription(principal, uri)

    async def test_assignment_and_nudge_publish_only_target_seat_cue(self) -> None:
        ticket_id = await self.create_ticket()

        async def assign() -> object:
            self.principal = self.coordinator
            return await self.call(
                "ticket_assign",
                agent_name="coordinator-agent",
                ticket_id=ticket_id,
                assigned_to_agent_id=self.worker_id,
                expected_status="open",
                coordinator_op_key="push-wait-assignment",
                reason="targeted subscription test",
            )

        assigned = await self.assert_target_only_cue(assign)
        self.assertEqual(
            assigned.structured_content["event"]["recipient_identities"],
            [self.worker_id],
        )
        self.assertEqual(
            self.service.load("pursers")["tickets"][ticket_id][
                "assigned_to_agent_id"
            ],
            self.worker_id,
        )

        async def nudge() -> object:
            self.principal = self.coordinator
            return await self.call(
                "agent_nudge",
                agent_name="coordinator-agent",
                ticket_id=ticket_id,
                target_agent_id=self.worker_id,
                coordinator_op_key="push-wait-nudge",
                reason="targeted subscription test",
                expires_at=(
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            )

        nudged = await self.assert_target_only_cue(nudge)
        self.assertEqual(
            nudged.structured_content["event"]["recipient_identities"],
            [self.worker_id],
        )

    async def test_assigned_creation_publishes_only_assignee_seat_cue(self) -> None:
        async def create() -> object:
            self.principal = self.admin
            return await self.call(
                "ticket_create",
                agent_name="admin-agent",
                title="assigned push wait target",
                description="only the exact assignee receives the seat cue",
                target_url="pursers/packages/central",
                scope="interactive-no-send",
                required_fields=["test_output"],
                assigned_to=self.worker_id,
            )

        created = await self.assert_target_only_cue(create)
        self.assertEqual(
            created.structured_content["event"]["recipient_identities"],
            [self.worker_id],
        )

    async def test_review_result_publishes_only_participant_seat_cue(self) -> None:
        ticket_id = await self.create_ticket(assigned_to=self.worker_id)
        self.principal = self.worker
        await self.call(
            "ticket_claim", agent_name="worker-agent", ticket_id=ticket_id
        )
        submitted = await self.call(
            "ticket_submit",
            agent_name="worker-agent",
            ticket_id=ticket_id,
            summary="ready for independent review",
            files_changed=["packages/central/example.py"],
            notes="test_output: passed",
            stay_active=True,
        )
        self.assertFalse(submitted.is_error)

        async def review() -> object:
            self.principal = self.admin
            return await self.call(
                "ticket_review",
                agent_name="admin-agent",
                ticket_id=ticket_id,
                verdict="reject",
                review_notes="independent test rejection",
                fix_instructions="exercise the retry cue",
            )

        reviewed = await self.assert_target_only_cue(review)
        self.assertEqual(
            reviewed.structured_content["event"]["recipient_identities"],
            [self.worker_id],
        )
        self.assertEqual(
            reviewed.structured_content["event"]["status_to"], "open"
        )


if __name__ == "__main__":
    unittest.main()
