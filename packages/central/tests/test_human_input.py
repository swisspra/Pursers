from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = PACKAGE_ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(PACKAGE_ROOT / "src" / "pursers_central"))

import central  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from pursers_client import (  # noqa: E402
    HUMAN_INPUT_REQUESTED,
    HUMAN_INPUT_RESOLVED,
    KNOWN_EVENT_KINDS,
)


class HumanInputTests(unittest.IsolatedAsyncioTestCase):
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
            "PR-admin", "admin", frozenset({"board:read", "board:write", "board:review"})
        )
        self.worker = central.Principal(
            "PR-worker", "worker", frozenset({"board:read", "board:write"})
        )
        self.other = central.Principal(
            "PR-other", "other", frozenset({"board:read", "board:write"})
        )
        self.principal = self.admin
        self.original_current_principal = central.current_principal
        central.current_principal = lambda: self.principal
        await self.call("board_join", agent_name="admin-agent")
        for principal in (self.worker, self.other):
            await self.call(
                "board_member_add", agent_name="admin-agent",
                principal_id=principal.principal_id, role="member",
            )
            self.principal = principal
            await self.call("board_join", agent_name=principal.canonical)
            self.principal = self.admin

    async def asyncTearDown(self) -> None:
        central.current_principal = self.original_current_principal
        self.environment.stop()
        self.temp_dir.cleanup()

    async def call(self, name: str, **arguments: object):
        return await self.mcp.call_tool(name, {"board_id": "pursers", **arguments})

    async def claimed_ticket(self, ticket_id: str = "TK-human") -> None:
        self.principal = self.admin
        await self.call(
            "ticket_create", ticket_id=ticket_id, agent_name="admin-agent",
            title="Needs operator input", description="Pause safely",
            scope="interactive-no-send", required_fields=["test_output"],
            unassigned=True,
        )
        self.principal = self.worker
        await self.call("ticket_claim", ticket_id=ticket_id, agent_name="worker")

    async def request(
        self, ticket_id: str = "TK-human",
        message: str = "Choose the safe rollout option", **extra: object,
    ):
        self.principal = self.worker
        return await self.call(
            "ticket_request_human", ticket_id=ticket_id, agent_name="worker",
            message=message, kind="decision", **extra,
        )

    async def test_request_releases_lease_and_excludes_claim_and_dispatch(self) -> None:
        await self.claimed_ticket()
        result = (await self.request()).structured_content
        ticket = result["ticket"]
        self.assertEqual(ticket["status"], "needs_human")
        self.assertNotIn("lease_expires_at", ticket)
        self.assertNotIn("claimed_by_agent_id", ticket)
        self.assertEqual(result["event"]["kind"], HUMAN_INPUT_REQUESTED)
        self.assertEqual(ticket["human_request"]["request_id"], result["request_id"])
        self.assertIsNone(ticket["human_request"]["resolution"])

        with self.assertRaisesRegex(ToolError, "ticket is waiting for a human answer"):
            await self.call("ticket_claim", ticket_id="TK-human", agent_name="worker")
        document = self.service.load("pursers")
        self.assertNotIn("work_offer", document["tickets"]["TK-human"])
        self.assertEqual(document["tickets"]["TK-human"]["dispatch_state"]["state"], "needs_human")

    async def test_accept_records_answer_and_immediately_reopens(self) -> None:
        await self.claimed_ticket()
        requested = (
            await self.request(
                requested_schema={
                    "type": "object",
                    "properties": {
                        "choice": {
                            "type": "string",
                            "oneOf": [
                                {"const": "blue", "title": "Blue"},
                                {"const": "green", "title": "Green"},
                            ],
                        }
                    },
                    "required": ["choice"],
                    "additionalProperties": False,
                }
            )
        ).structured_content
        self.principal = self.admin
        resolved = (
            await self.call(
                "ticket_human_resolve", ticket_id="TK-human",
                agent_name="admin-agent", request_id=requested["request_id"],
                action="accept", content={"choice": "green"},
            )
        ).structured_content
        self.assertEqual(resolved["ticket"]["status"], "open")
        self.assertIn('HUMAN ANSWER: {"choice":"green"}', resolved["ticket"]["notes"])
        self.assertEqual(resolved["event"]["kind"], HUMAN_INPUT_RESOLVED)
        self.assertEqual(
            resolved["ticket"]["human_request"]["resolution"]["action"], "accept"
        )

    async def test_reopen_dispatches_to_an_alternative_worker(self) -> None:
        for principal in (self.worker, self.other):
            self.principal = principal
            joined = await self.call(
                "board_join", agent_name=principal.canonical,
                capabilities={"can_work": True, "can_review": False},
            )
            self.service.register_listener(
                "pursers", joined.structured_content["agent_id"]
            )
        self.principal = self.admin
        await self.call(
            "ticket_create", ticket_id="TK-redispatch", agent_name="admin-agent",
            title="Needs a decision", description="Redispatch after answer",
            scope="interactive-no-send", required_fields=["test_output"],
            assigned_to="worker",
        )
        self.principal = self.worker
        await self.call(
            "ticket_claim", ticket_id="TK-redispatch", agent_name="worker"
        )
        requested = (await self.request("TK-redispatch")).structured_content
        self.principal = self.admin
        resolved = (
            await self.call(
                "ticket_human_resolve", ticket_id="TK-redispatch",
                agent_name="admin-agent", request_id=requested["request_id"],
                action="accept", content="continue",
            )
        ).structured_content
        self.assertEqual(resolved["ticket"]["status"], "open")
        self.assertIsNotNone(resolved["dispatch_event"])
        self.assertEqual(resolved["dispatch_event"]["offered_agent_name"], "other")

    async def test_decline_can_park_or_cancel_and_cancel_action_is_reaskable(self) -> None:
        await self.claimed_ticket("TK-park")
        parked_request = (await self.request("TK-park")).structured_content
        self.principal = self.admin
        parked = (
            await self.call(
                "ticket_human_resolve", ticket_id="TK-park", agent_name="admin-agent",
                request_id=parked_request["request_id"], action="decline",
                disposition="park", note="Wait for procurement",
            )
        ).structured_content
        self.assertEqual(parked["ticket"]["status"], "needs_human")

        reasked = (
            await self.call(
                "ticket_request_human", ticket_id="TK-park", agent_name="admin-agent",
                message="Provide the revised delivery date", kind="information",
            )
        ).structured_content
        dismissed = (
            await self.call(
                "ticket_human_resolve", ticket_id="TK-park", agent_name="admin-agent",
                request_id=reasked["request_id"], action="cancel",
            )
        ).structured_content
        self.assertEqual(dismissed["ticket"]["status"], "needs_human")
        self.assertEqual(
            dismissed["ticket"]["human_request"]["resolution"]["action"], "cancel"
        )
        reasked_again = await self.call(
            "ticket_request_human", ticket_id="TK-park", agent_name="admin-agent",
            message="Choose whether to continue", kind="approval",
        )
        self.assertNotEqual(
            reasked_again.structured_content["request_id"], reasked["request_id"]
        )

        await self.claimed_ticket("TK-cancel")
        cancel_request = (await self.request("TK-cancel")).structured_content
        self.principal = self.admin
        canceled = (
            await self.call(
                "ticket_human_resolve", ticket_id="TK-cancel", agent_name="admin-agent",
                request_id=cancel_request["request_id"], action="decline",
                disposition="cancel",
            )
        ).structured_content
        self.assertEqual(canceled["ticket"]["status"], "canceled")

    async def test_schema_security_and_authorization(self) -> None:
        await self.claimed_ticket()
        with self.assertRaisesRegex(ToolError, "use url mode for sensitive input"):
            await self.request(
                requested_schema={
                    "type": "object",
                    "properties": {"api_key": {"type": "string"}},
                }
            )
        with self.assertRaisesRegex(ToolError, "primitive or array-of-enum"):
            await self.request(
                requested_schema={
                    "type": "object",
                    "properties": {"nested": {"type": "object", "properties": {}}},
                }
            )
        with self.assertRaises(ToolError):
            await self.request(message="Read /Users/synthetic-user/approval.txt")
        requested = (await self.request()).structured_content
        self.principal = self.other
        with self.assertRaisesRegex(ToolError, "board admin or board:coordinate"):
            await self.call(
                "ticket_human_resolve", ticket_id="TK-human", agent_name="other",
                request_id=requested["request_id"], action="accept",
            )

    async def test_malformed_form_schema_keywords_are_rejected(self) -> None:
        await self.claimed_ticket()
        invalid_schemas = (
            {
                "type": "object",
                "properties": {
                    "choice": {
                        "type": "string",
                        "oneOf": [{"const": "a", "title": ""}],
                    }
                },
            },
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": "not-an-integer"}
                },
            },
            {
                "type": "object",
                "title": {"nested": "not-a-string"},
                "properties": {},
            },
            {
                "type": "object",
                "properties": {
                    "choices": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["a"]},
                        "minItems": "bad",
                        "uniqueItems": "bad",
                    }
                },
            },
        )
        for requested_schema in invalid_schemas:
            with self.subTest(requested_schema=requested_schema):
                with self.assertRaises(ToolError):
                    await self.request(requested_schema=requested_schema)

    async def test_schema_constraints_and_defaults_are_typed_and_enforced(self) -> None:
        await self.claimed_ticket()
        invalid_properties = (
            {"name": {"type": "string", "title": {"bad": "title"}}},
            {"name": {"type": "string", "description": ["bad"]}},
            {"name": {"type": "string", "maxLength": True}},
            {"name": {"type": "string", "minLength": 3, "maxLength": 2}},
            {"count": {"type": "number", "minimum": "zero"}},
            {"count": {"type": "number", "maximum": False}},
            {"count": {"type": "number", "minimum": 2, "maximum": 1}},
            {"count": {"type": "integer", "default": 1.5}},
            {"enabled": {"type": "boolean", "default": "false"}},
            {"name": {"type": "string", "minLength": 2, "default": "x"}},
            {
                "choices": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["a", "b"]},
                    "minItems": "bad",
                }
            },
            {
                "choices": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["a", "b"]},
                    "maxItems": True,
                }
            },
            {
                "choices": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["a", "b"]},
                    "uniqueItems": True,
                }
            },
            {
                "choices": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["a", "b"]},
                    "default": ["other"],
                }
            },
            {"choice": {"type": "string", "enum": ["a"], "minimum": 0}},
            {"choice": {"type": "number", "enum": [1, 2]}},
            {"choice": {"enum": ["a", "b"]}},
            {
                "choices": {
                    "type": "array",
                    "items": {"type": "integer", "enum": [1, 2]},
                }
            },
            {
                "choice": {
                    "type": "string",
                    "enum": ["a"],
                    "oneOf": [{"const": "a", "title": "A"}],
                }
            },
        )
        for properties in invalid_properties:
            with self.subTest(properties=properties):
                with self.assertRaises(ToolError):
                    await self.request(
                        requested_schema={"type": "object", "properties": properties}
                    )
        with self.assertRaises(ToolError):
            await self.request(
                requested_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": 0,
                }
            )

        requested = (
            await self.request(
                requested_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 2,
                            "maxLength": 4,
                            "default": "ok",
                        },
                        "count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 3,
                            "default": 2,
                        },
                        "choices": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["a", "b"]},
                            "minItems": 1,
                            "maxItems": 1,
                            "default": ["a"],
                        },
                    },
                    "required": ["name", "count", "choices"],
                }
            )
        ).structured_content
        self.principal = self.admin
        for content in (
            {"name": "x", "count": 2, "choices": ["a"]},
            {"name": "okay", "count": 4, "choices": ["a"]},
            {"name": "okay", "count": 2, "choices": []},
            {"name": "okay", "count": 2, "choices": ["a", "b"]},
        ):
            with self.subTest(content=content), self.assertRaises(ToolError):
                await self.call(
                    "ticket_human_resolve",
                    ticket_id="TK-human",
                    agent_name="admin-agent",
                    request_id=requested["request_id"],
                    action="accept",
                    content=content,
                )
        resolved = await self.call(
            "ticket_human_resolve",
            ticket_id="TK-human",
            agent_name="admin-agent",
            request_id=requested["request_id"],
            action="accept",
            content={"name": "okay", "count": 2, "choices": ["a"]},
        )
        self.assertEqual(resolved.structured_content["ticket"]["status"], "open")

    async def test_list_snapshot_briefing_and_event_vocabulary(self) -> None:
        await self.claimed_ticket()
        requested = (
            await self.request(message="x" * 800)
        ).structured_content
        self.principal = self.admin
        listed = await self.call("ticket_list", status="needs_human")
        self.assertEqual([item["ticket_id"] for item in listed.structured_content["tickets"]], ["TK-human"])
        snapshot = await self.call("board_snapshot")
        request = snapshot.structured_content["tickets"][0]["human_request"]
        self.assertGreater(request["omitted_counts"]["message_chars"], 0)
        self.assertLessEqual(len(request["message"]), 500)
        onboard = await self.call("board_onboard", agent_name="admin-agent")
        pending = onboard.structured_content["briefing"]["pending_human_requests"]
        self.assertEqual(pending[0]["request_id"], requested["request_id"])
        self.assertIn(HUMAN_INPUT_REQUESTED, KNOWN_EVENT_KINDS)
        self.assertIn(HUMAN_INPUT_RESOLVED, KNOWN_EVENT_KINDS)


if __name__ == "__main__":
    unittest.main()
