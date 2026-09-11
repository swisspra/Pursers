"""Bridge-level contracts for the coordinator question tools."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parents[1] / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from pursers_client import (  # noqa: E402
    COORDINATOR_QUESTION_ANSWERED,
    COORDINATOR_QUESTION_ASKED,
    JoinedIdentity,
)
import pursers_wait_server as wait_server  # noqa: E402


class FakeQuestionClient:
    def __init__(self, role: str, events: list[dict[str, Any]]) -> None:
        self.board_id = "pursers"
        self.identity = JoinedIdentity(
            "pursers", f"AI-{role}", f"PR-{role}", f"seat-{role}", role
        )
        self.asker_agent_id = self.identity.agent_id
        self.generation_token = "generation"
        self.events = events
        self.asked: list[dict[str, Any]] = []
        self.answered: list[dict[str, Any]] = []

    async def events_for_board(
        self,
        _board_id: str,
        _from_cursor: int,
        _identity: JoinedIdentity,
        cursor_callback,
        **_options: Any,
    ):
        for event in self.events:
            cursor_callback(event["seq"])
            yield event

    async def board_question_inbox(self, **_options: Any) -> dict[str, Any]:
        return {
            "questions": [
                {
                    "ticket_id": "TK-one",
                    "question_id": "CQ-one",
                    "state": "open",
                    "binding": "must-not-escape",
                }
            ]
        }

    async def ticket_get(self, _ticket_id: str) -> dict[str, Any]:
        return {
            "ticket": {
                "coordinator_questions": [
                    {
                        "question_id": "CQ-one",
                        "state": "answered",
                        "answer": "Ship canary first",
                        "asked_by": {"agent_id": self.asker_agent_id},
                        "binding": "must-not-escape",
                    }
                ]
            }
        }

    async def ticket_question_ask(
        self, ticket_id: str, message: str, kind: str, **options: Any
    ) -> dict[str, Any]:
        self.asked.append(
            {"ticket_id": ticket_id, "message": message, "kind": kind, **options}
        )
        return {"ok": True, "question_id": "CQ-one"}

    async def ticket_question_answer(
        self, ticket_id: str, question_id: str, **options: Any
    ) -> dict[str, Any]:
        self.answered.append(
            {"ticket_id": ticket_id, "question_id": question_id, **options}
        )
        return {
            "ok": True,
            "question": {
                "question_id": question_id,
                "state": options.get("action"),
                "binding": "must-not-escape",
            },
        }


class CoordinatorQuestionBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_tools_never_advertises_private_auth_inputs(self) -> None:
        tools = await wait_server.mcp.list_tools()
        selected = {
            tool.name: tool.input_schema
            for tool in tools
            if tool.name
            in {
                "ticket_question_ask",
                "board_question_inbox",
                "ticket_question_answer",
                "board_question_wait",
                "ticket_question_wait",
            }
        }
        self.assertEqual(len(selected), 5)
        rendered = str(selected).casefold()
        self.assertNotIn("host_binding", rendered)
        self.assertNotIn("bearer", rendered)
        self.assertNotIn("token", rendered)
        self.assertTrue(
            all(schema.get("additionalProperties") is False for schema in selected.values())
        )
        self.assertEqual(
            set(selected["ticket_question_answer"]["properties"]),
            {"ticket_id", "question_id", "action", "message"},
        )

    async def test_coordinator_wait_returns_only_correlated_public_question(self) -> None:
        client = FakeQuestionClient(
            "coordinator",
            [
                {
                    "seq": 12,
                    "kind": COORDINATOR_QUESTION_ASKED,
                    "ticket_id": "TK-one",
                    "question_id": "CQ-one",
                }
            ],
        )
        result = await wait_server._question_wait_core(
            client,
            since_seq=10,
            timeout_s=0.1,
            event_kind=COORDINATOR_QUESTION_ASKED,
            ticket_id="TK-one",
        )
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["new_seq"], 12)
        self.assertEqual(result["question"]["question_id"], "CQ-one")
        self.assertNotIn("binding", result["question"])
        self.assertEqual(result["model_continuation"], "host_managed")

    async def test_asker_wait_checks_identity_and_exact_question(self) -> None:
        client = FakeQuestionClient(
            "worker",
            [
                {
                    "seq": 14,
                    "kind": COORDINATOR_QUESTION_ANSWERED,
                    "ticket_id": "TK-one",
                    "question_id": "CQ-one",
                }
            ],
        )
        result = await wait_server._question_wait_core(
            client,
            since_seq=12,
            timeout_s=0.1,
            event_kind=COORDINATOR_QUESTION_ANSWERED,
            ticket_id="TK-one",
            question_id="CQ-one",
        )
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["question"]["answer"], "Ship canary first")
        self.assertNotIn("binding", result["question"])

        client.identity = JoinedIdentity(
            "pursers", "AI-other", "PR-other", "other", "worker"
        )
        with patch.object(wait_server, "_event_stream") as stream:
            async def one_event(*_args: Any, **_kwargs: Any):
                yield client.events[0]

            stream.side_effect = one_event
            timed_out = await wait_server._question_wait_core(
                client,
                since_seq=12,
                timeout_s=0.01,
                event_kind=COORDINATOR_QUESTION_ANSWERED,
                ticket_id="TK-one",
                question_id="CQ-one",
            )
        self.assertTrue(timed_out["timed_out"])

    async def test_role_guard_blocks_cross_channel_use(self) -> None:
        worker = FakeQuestionClient("worker", [])
        coordinator = FakeQuestionClient("coordinator", [])
        with self.assertRaisesRegex(wait_server.ToolError, "requires role coordinator"):
            wait_server._question_identity(
                worker, frozenset({"coordinator"}), "question inbox"
            )
        with self.assertRaisesRegex(wait_server.ToolError, "requires role reviewer or worker"):
            wait_server._question_identity(
                coordinator,
                frozenset({"worker", "reviewer"}),
                "question ask",
            )

    async def test_answer_wrapper_never_forwards_or_returns_binding(self) -> None:
        coordinator = FakeQuestionClient("coordinator", [])
        result = await coordinator.ticket_question_answer(
            "TK-one", "CQ-one", action="accept", message=None
        )
        projected = wait_server._public_question(result["question"])
        self.assertNotIn("binding", projected)
        self.assertEqual(
            coordinator.answered,
            [
                {
                    "ticket_id": "TK-one",
                    "question_id": "CQ-one",
                    "action": "accept",
                    "message": None,
                }
            ],
        )

    async def test_timeout_preserves_last_successful_cursor(self) -> None:
        client = FakeQuestionClient("coordinator", [])
        result = await wait_server._question_wait_core(
            client,
            since_seq=19,
            timeout_s=0.01,
            event_kind=COORDINATOR_QUESTION_ASKED,
        )
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["new_seq"], 19)


if __name__ == "__main__":
    unittest.main()
