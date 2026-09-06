"""Unit tests for board_human_requests (a22 elicitation + fallback)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
CLIENT_SRC = REPOSITORY / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ONBOARD_CENTRAL_TOKEN", "TOKEN_PLACEHOLDER")

from mcp.types import ElicitResult, InputRequiredResult  # noqa: E402
import pursers_wait_server as wait_server  # noqa: E402


def _tool_result(payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        is_error=False, structured_content={"result": payload}, content=[]
    )


def _human_record(
    request_id: str = "REQ-1",
    message: str = "Please approve the dataset export",
    kind: str = "approval",
    schema: dict[str, Any] | None = None,
    url: str | None = None,
    resolution: Any = None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "message": message,
        "kind": kind,
        "requested_schema": schema
        if schema is not None
        else {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "title": "Answer"},
            },
            "required": ["answer"],
        },
        "url": url,
        "asked_by": {"agent_name": "worker-x", "agent_id": "AI-x", "principal_id": "PR-x"},
        "asked_at": "2026-09-06T09:00:00+00:00",
        "expires_at": None,
        "resolution": resolution,
    }


class FakeBoardClient:
    """BoardClient stand-in returning canned ticket_list/ticket_get payloads."""

    def __init__(
        self,
        tickets_by_board: dict[str, list[dict[str, Any]]] | None = None,
        registry_boards: list[str] | None = None,
    ) -> None:
        self.agent_name = "orchestrator-test"
        self.board_id = "pursers"
        self.tickets_by_board = tickets_by_board or {}
        self.registry_boards = registry_boards or []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.resolved: list[dict[str, Any]] = []
        self.list_include_record = True

    async def board_state_get(self, key: str | None = None) -> dict[str, Any]:
        registry = {
            "schema_version": 1,
            "projects": {
                board: {
                    "board_id": board,
                    "status": "active",
                    "work_dir": f"/tmp/{board}",
                }
                for board in self.registry_boards
            },
        }
        return {"state": {"value": json.dumps(registry)}}

    async def call_tool(self, name: str, payload: dict[str, Any], meta: Any = None):
        self.calls.append((name, dict(payload)))
        if name == "ticket_list":
            board = payload["board_id"]
            tickets = []
            for ticket in self.tickets_by_board.get(board, []):
                item = {
                    "ticket_id": ticket["ticket_id"],
                    "status": ticket.get("status", "needs_human"),
                }
                if self.list_include_record:
                    item["human_request"] = ticket.get("human_request")
                tickets.append(item)
            return _tool_result({"tickets": tickets, "total_matching": len(tickets)})
        if name == "ticket_get":
            board = payload["board_id"]
            for ticket in self.tickets_by_board.get(board, []):
                if ticket["ticket_id"] == payload["ticket_id"]:
                    return _tool_result(
                        {
                            "ticket": {
                                "ticket_id": ticket["ticket_id"],
                                "status": ticket.get("status", "needs_human"),
                                "human_request": ticket.get("human_request"),
                            }
                        }
                    )
            return _tool_result({"ticket": {}})
        if name == "ticket_human_resolve":
            self.resolved.append(dict(payload))
            return _tool_result({"ok": True, "ticket_id": payload["ticket_id"]})
        raise AssertionError(f"unexpected tool call {name}")


def caps(form: bool = False, url: bool = False) -> SimpleNamespace | None:
    if not form and not url:
        return None
    elicitation = SimpleNamespace(
        form=SimpleNamespace() if form else None,
        url=SimpleNamespace() if url else None,
    )
    return SimpleNamespace(elicitation=elicitation)


def run(coro):
    return asyncio.run(coro)


class HumanRequestsCoreTests(unittest.TestCase):
    def _client_with_form_request(self, url=None, schema=None):
        return FakeBoardClient(
            tickets_by_board={
                "proj-a": [
                    {
                        "ticket_id": "TK-1",
                        "human_request": _human_record(schema=schema, url=url),
                    }
                ]
            }
        )

    def test_no_pending_requests(self) -> None:
        client = FakeBoardClient()
        result = run(
            wait_server.board_human_requests_core(
                client, boards=["proj-a"], capabilities=caps(form=True)
            )
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(result["pending"], [])
        self.assertNotIn("inputRequests", str(type(result)))

    def test_fallback_when_client_declares_no_elicitation(self) -> None:
        client = self._client_with_form_request()
        result = run(
            wait_server.board_human_requests_core(
                client, boards=["proj-a"], capabilities=None
            )
        )
        self.assertIsInstance(result, dict)
        self.assertFalse(result["elicitation_declared"])
        self.assertEqual(len(result["pending"]), 1)
        item = result["pending"][0]
        self.assertEqual(item["ticket_id"], "TK-1")
        self.assertEqual(item["request_id"], "REQ-1")
        self.assertIn("answer=", result["instructions"])
        self.assertIn("dashboard", result["instructions"])
        self.assertEqual(client.resolved, [])

    def test_form_elicitation_schema_passthrough_and_disposition(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "title": "Dataset"},
                "approve": {"type": "boolean"},
            },
            "required": ["dataset"],
        }
        client = self._client_with_form_request(schema=schema)
        result = run(
            wait_server.board_human_requests_core(
                client, boards=["proj-a"], capabilities=caps(form=True)
            )
        )
        self.assertIsInstance(result, InputRequiredResult)
        requests = result.input_requests
        self.assertEqual(list(requests.keys()), ["r0"])
        params = requests["r0"].params
        self.assertEqual(params.mode, "form")
        self.assertIn("[proj-a/TK-1]", params.message)
        rendered = params.requested_schema
        # requested_schema verbatim plus mandatory disposition enum
        self.assertEqual(
            rendered["properties"]["dataset"],
            {"type": "string", "title": "Dataset"},
        )
        self.assertEqual(rendered["properties"]["approve"], {"type": "boolean"})
        disposition = rendered["properties"]["disposition"]
        self.assertEqual(disposition["enum"], ["reopen", "park", "cancel"])
        self.assertIn("title", disposition)
        self.assertEqual(sorted(rendered["required"]), ["dataset", "disposition"])
        state = json.loads(result.request_state)
        self.assertEqual(
            state["targets"]["r0"],
            {"board": "proj-a", "ticket_id": "TK-1", "request_id": "REQ-1"},
        )
        self.assertEqual(client.resolved, [])

    def test_never_sends_url_mode_to_form_only_client(self) -> None:
        client = self._client_with_form_request(url="https://vault.example/handoff")
        result = run(
            wait_server.board_human_requests_core(
                client, boards=["proj-a"], capabilities=caps(form=True)
            )
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result["unasked"]), 1)
        self.assertIn("url mode", result["unasked"][0]["reason"])
        self.assertIn("instructions", result)

    def test_url_mode_sent_when_declared(self) -> None:
        client = self._client_with_form_request(url="https://vault.example/handoff")
        result = run(
            wait_server.board_human_requests_core(
                client, boards=["proj-a"], capabilities=caps(form=True, url=True)
            )
        )
        self.assertIsInstance(result, InputRequiredResult)
        params = result.input_requests["r0"].params
        self.assertEqual(params.mode, "url")
        self.assertEqual(params.url, "https://vault.example/handoff")

    def test_request_state_roundtrip_accept(self) -> None:
        client = self._client_with_form_request()
        first = run(
            wait_server.board_human_requests_core(
                client, boards=["proj-a"], capabilities=caps(form=True)
            )
        )
        self.assertIsInstance(first, InputRequiredResult)
        result = run(
            wait_server.board_human_requests_core(
                client,
                boards=["proj-a"],
                capabilities=caps(form=True),
                input_responses={
                    "r0": ElicitResult(
                        action="accept",
                        content={"answer": "yes", "disposition": "park"},
                    )
                },
                request_state=first.request_state,
            )
        )
        self.assertIsInstance(result, dict)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["resolved"]), 1)
        self.assertEqual(result["deferred"], [])
        self.assertEqual(len(client.resolved), 1)
        resolve_call = client.resolved[0]
        self.assertEqual(resolve_call["action"], "accept")
        self.assertEqual(resolve_call["disposition"], "park")
        self.assertEqual(resolve_call["content"], {"answer": "yes"})
        self.assertEqual(resolve_call["ticket_id"], "TK-1")
        self.assertEqual(resolve_call["request_id"], "REQ-1")
        self.assertEqual(resolve_call["board_id"], "proj-a")

    def test_decline_defaults_disposition_to_park(self) -> None:
        client = self._client_with_form_request()
        first = run(
            wait_server.board_human_requests_core(
                client, boards=["proj-a"], capabilities=caps(form=True)
            )
        )
        result = run(
            wait_server.board_human_requests_core(
                client,
                boards=["proj-a"],
                capabilities=caps(form=True),
                input_responses={"r0": ElicitResult(action="decline", content=None)},
                request_state=first.request_state,
            )
        )
        self.assertEqual(result["resolved"][0]["disposition"], "park")
        self.assertEqual(client.resolved[0]["action"], "decline")

    def test_cancel_leaves_request_pending(self) -> None:
        client = self._client_with_form_request()
        first = run(
            wait_server.board_human_requests_core(
                client, boards=["proj-a"], capabilities=caps(form=True)
            )
        )
        result = run(
            wait_server.board_human_requests_core(
                client,
                boards=["proj-a"],
                capabilities=caps(form=True),
                input_responses={"r0": ElicitResult(action="cancel")},
                request_state=first.request_state,
            )
        )
        self.assertEqual(result["resolved"], [])
        self.assertEqual(len(result["deferred"]), 1)
        self.assertEqual(result["deferred"][0]["reason"], "asked later")
        self.assertEqual(client.resolved, [])

    def test_missing_response_is_asked_later(self) -> None:
        client = self._client_with_form_request()
        first = run(
            wait_server.board_human_requests_core(
                client, boards=["proj-a"], capabilities=caps(form=True)
            )
        )
        result = run(
            wait_server.board_human_requests_core(
                client,
                boards=["proj-a"],
                capabilities=caps(form=True),
                input_responses={"other-key": ElicitResult(action="accept")},
                request_state=first.request_state,
            )
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(result["resolved"], [])
        self.assertEqual(len(result["deferred"]), 1)
        self.assertEqual(result["deferred"][0]["reason"], "asked later")

    def test_answer_path_accept(self) -> None:
        client = self._client_with_form_request()
        result = run(
            wait_server.board_human_requests_core(
                client,
                boards=["proj-a"],
                answer={
                    "ticket_id": "TK-1",
                    "action": "accept",
                    "content": {"answer": "ok"},
                    "disposition": "reopen",
                },
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(client.resolved[0]["action"], "accept")
        self.assertEqual(client.resolved[0]["disposition"], "reopen")
        self.assertEqual(client.resolved[0]["content"], {"answer": "ok"})

    def test_answer_path_cancel_defers(self) -> None:
        client = self._client_with_form_request()
        result = run(
            wait_server.board_human_requests_core(
                client,
                boards=["proj-a"],
                answer={"ticket_id": "TK-1", "action": "cancel"},
            )
        )
        self.assertEqual(result["resolved"], [])
        self.assertEqual(result["deferred"][0]["reason"], "asked later")
        self.assertEqual(client.resolved, [])

    def test_answer_unknown_ticket(self) -> None:
        client = FakeBoardClient()
        result = run(
            wait_server.board_human_requests_core(
                client,
                boards=["proj-a"],
                answer={"ticket_id": "TK-missing", "action": "accept"},
            )
        )
        self.assertFalse(result["ok"])
        self.assertIn("no pending human request", result["error"])

    def test_registry_boards_selection(self) -> None:
        client = FakeBoardClient(
            tickets_by_board={
                "proj-b": [
                    {"ticket_id": "TK-9", "human_request": _human_record("REQ-9")}
                ]
            },
            registry_boards=["proj-b"],
        )
        result = run(
            wait_server.board_human_requests_core(
                client, boards="registry", capabilities=None
            )
        )
        self.assertEqual(result["pending"][0]["ticket_id"], "TK-9")
        self.assertEqual(result["pending"][0]["board_id"], "proj-b")

    def test_ticket_get_fallback_when_list_lacks_record(self) -> None:
        client = self._client_with_form_request()
        client.list_include_record = False
        result = run(
            wait_server.board_human_requests_core(
                client, boards=["proj-a"], capabilities=None
            )
        )
        self.assertEqual(len(result["pending"]), 1)
        self.assertTrue(any(name == "ticket_get" for name, _ in client.calls))

    def test_resolved_records_are_not_listed(self) -> None:
        client = FakeBoardClient(
            tickets_by_board={
                "proj-a": [
                    {
                        "ticket_id": "TK-2",
                        "human_request": _human_record(
                            "REQ-2", resolution={"action": "accept"}
                        ),
                    }
                ]
            }
        )
        result = run(
            wait_server.board_human_requests_core(
                client, boards=["proj-a"], capabilities=caps(form=True)
            )
        )
        self.assertEqual(result["pending"], [])


class DigestHumanRequestsTests(unittest.TestCase):
    def _engine(self) -> wait_server.OrchestratorEngine:
        engine = wait_server.OrchestratorEngine(
            None, None, Path(tempfile.mkdtemp()) / "state.json"
        )
        engine.active_boards = ["proj-a"]
        engine.cursor_map = {"proj-a": 5}
        return engine

    def test_digest_shows_pending_human_requests(self) -> None:
        engine = self._engine()
        engine.ticket_cache["proj-a:TK-1"] = {
            "ticket_id": "TK-1",
            "status": "needs_human",
            "human_request": _human_record(),
        }
        digest = run(engine.build_digest(since=0, boards=["proj-a"]))
        self.assertEqual(len(digest["human_requests"]), 1)
        row = digest["human_requests"][0]
        self.assertEqual(row["ticket_id"], "TK-1")
        self.assertEqual(row["request_id"], "REQ-1")
        self.assertEqual(row["kind"], "approval")
        self.assertEqual(row["asked_by"], "worker-x")

    def test_ack_clears_human_requests_until_new_one_arrives(self) -> None:
        engine = self._engine()
        engine.ticket_cache["proj-a:TK-1"] = {
            "ticket_id": "TK-1",
            "status": "needs_human",
            "human_request": _human_record(),
        }
        ack = run(engine.ack())
        self.assertEqual(ack["acknowledged_human_requests"], 1)
        digest = run(engine.build_digest(since=0, boards=["proj-a"]))
        self.assertEqual(digest["human_requests"], [])
        # a second ack of the same pending set acknowledges nothing new
        ack_again = run(engine.ack())
        self.assertEqual(ack_again["acknowledged_human_requests"], 0)
        # a new request surfaces again
        engine.ticket_cache["proj-a:TK-1"]["human_request"] = _human_record("REQ-2")
        digest = run(engine.build_digest(since=0, boards=["proj-a"]))
        self.assertEqual([r["request_id"] for r in digest["human_requests"]], ["REQ-2"])

    def test_reopened_ticket_request_visible_after_resolution_cleared(self) -> None:
        engine = self._engine()
        engine.ticket_cache["proj-a:TK-1"] = {
            "ticket_id": "TK-1",
            "status": "open",
            "human_request": _human_record(),
        }
        digest = run(engine.build_digest(since=0, boards=["proj-a"]))
        self.assertEqual(digest["human_requests"], [])


class SchemaHelperTests(unittest.TestCase):
    def test_form_schema_requires_disposition_without_mutation(self) -> None:
        original = {
            "type": "object",
            "properties": {"choice": {"type": "string", "enum": ["a", "b"]}},
            "required": ["choice"],
        }
        rendered = wait_server._form_schema_with_disposition(original)
        self.assertNotIn("disposition", original["properties"])
        self.assertEqual(
            original["properties"]["choice"]["enum"], ["a", "b"]
        )
        self.assertEqual(rendered["required"], ["choice", "disposition"])
        self.assertEqual(
            rendered["properties"]["disposition"]["enum"],
            ["reopen", "park", "cancel"],
        )

    def test_schema_summary_marks_required(self) -> None:
        summary = wait_server._human_schema_summary(
            {
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "level": {"enum": ["low", "high"]},
                },
                "required": ["answer"],
            }
        )
        self.assertIn("answer*:string", summary)
        self.assertIn("level:enum", summary)

    def test_elicitation_modes_dict_and_object(self) -> None:
        self.assertEqual(wait_server._elicitation_modes(None), (False, False))
        self.assertEqual(
            wait_server._elicitation_modes({"elicitation": {"form": {}}}),
            (True, False),
        )
        self.assertEqual(
            wait_server._elicitation_modes(caps(form=True, url=True)),
            (True, True),
        )
        self.assertEqual(
            wait_server._elicitation_modes(SimpleNamespace(elicitation=None)),
            (False, False),
        )


if __name__ == "__main__":
    unittest.main()
