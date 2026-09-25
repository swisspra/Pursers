from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import socket
import shutil
import sys
import threading
import time
from argparse import Namespace
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pursers_client import (
    BoardClient,
    CentralInstanceMismatchError,
    fork_central_instance_identity,
)
import pursers_acp.agent as agent_module
from pursers_acp.agent import ACP_VERSION, AuthRequired, PursersACPAgent
from pursers_acp.agent import PersonalBoardSurface, StdioWaitBridge
from pursers_central import central
from pursers_central.runtime_health import create_streamable_http_app

JSON = dict[str, Any]
TEST_TIMEOUT_S = float(os.environ.get("PURSERS_TEST_TIMEOUT_S", "30"))
INSTANCE_TEST_ISSUER = "https://instance-test.example"


def _instance_test_principal_id(name: str) -> str:
    canonical = json.dumps(
        [name, INSTANCE_TEST_ISSUER, name], separators=(",", ":")
    )
    return "PR-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _instance_test_token_fixture(root: Path, audience: str):
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    public = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public.update({"kid": "acp-instance", "alg": "RS256", "use": "sig"})
    jwks = root / "jwks.json"
    jwks.write_text(json.dumps({"keys": [public]}), encoding="utf-8")

    def issue(name: str, scopes: str) -> str:
        now = datetime.now(timezone.utc)
        return jwt.encode(
            {
                "iss": INSTANCE_TEST_ISSUER,
                "sub": name,
                "aud": audience,
                "resource": audience,
                "scope": scopes,
                "client_id": name,
                "iat": now,
                "nbf": now - timedelta(seconds=5),
                "exp": now + timedelta(minutes=10),
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "acp-instance"},
        )

    return jwks, issue


class FakeBoard:
    board_id = "pursers"

    def __init__(self) -> None:
        self.closed = False
        self.mutations: list[JSON] = []
        self.watch_started = asyncio.Event()
        self.readiness_calls: list[JSON] = []

    async def close(self) -> None:
        self.closed = True

    async def my_tickets(self) -> list[JSON]:
        return [
            {
                "ticket_id": "TK-owned",
                "status": "open",
                "title": "Owned",
                "project": "Atlas",
            }
        ]

    async def my_offers(self) -> list[JSON]:
        return [{"ticket_id": "TK-offer", "agent_name": "personal", "kind": "work"}]

    async def board_status(self) -> JSON:
        return {
            "board_id": self.board_id,
            "status_counts": {"open": 1},
            "latest_seq": 4,
            "projects": [
                {
                    "name": "Atlas",
                    "status": "active",
                    "ticket_counts": {"open": 1},
                }
            ],
        }

    async def set_readiness(
        self,
        session_id: str,
        sequence: int,
        **state: object,
    ) -> None:
        self.readiness_calls.append(
            {"session_id": session_id, "sequence": sequence, **state}
        )

    async def ticket_evidence(self, ticket_id: str) -> JSON:
        return {
            "ticket": {
                "ticket_id": ticket_id,
                "status": "submitted",
                "summary": "Ready for review",
                "files_changed": ["src/example.py"],
                "review_verdict": None,
                "notes": "branch_and_commit: branch@abc\ntest_output: 2 passed",
            }
        }

    def create_action(self, title: str, description: str) -> JSON:
        return {
            "operation": "ticket_create",
            "board_id": self.board_id,
            "params": {"title": title, "description": description},
        }

    def annotate_action(self, ticket_id: str, text: str) -> JSON:
        return {
            "operation": "ticket_annotate",
            "board_id": self.board_id,
            "params": {"ticket_id": ticket_id, "text": text},
        }

    async def answer_action(self, ticket_id: str, text: str) -> JSON:
        return {
            "operation": "ticket_human_resolve",
            "board_id": self.board_id,
            "params": {"ticket_id": ticket_id, "content": text},
        }

    def question_answer_action(
        self, ticket_id: str, question_id: str, text: str
    ) -> JSON:
        return {
            "operation": "ticket_question_answer",
            "board_id": self.board_id,
            "params": {
                "ticket_id": ticket_id,
                "question_id": question_id,
                "action": "answer",
                "message": text,
            },
        }

    async def mutate(self, action: JSON) -> JSON:
        self.mutations.append(action)
        if action["operation"] == "ticket_create":
            return {"ticket": {"ticket_id": "TK-created"}}
        if action["operation"] == "ticket_human_resolve":
            return {"ticket": {"ticket_id": action["params"]["ticket_id"]}}
        if action["operation"] == "ticket_question_answer":
            return {
                "question": {
                    "question_id": action["params"]["question_id"],
                    "state": "answered",
                }
            }
        return {"annotation": {"annotation_id": "AN-created"}}

    async def watch(
        self, cursor: int | None, cancel: asyncio.Event
    ) -> AsyncIterator[tuple[int, JSON]]:
        self.watch_started.set()
        yield 4, {
            "kind": "watch_snapshot",
            "tickets": [
                {
                    "ticket_id": "TK-work",
                    "title": "Build runtime",
                    "status": "claimed",
                    "project": "Atlas",
                    "claimed_by": "worker-build",
                },
                {
                    "ticket_id": "TK-submitted",
                    "title": "Await review",
                    "status": "submitted",
                    "project": "Atlas",
                    "claimed_by": "worker-submit",
                },
                {
                    "ticket_id": "TK-review",
                    "title": "Review active",
                    "status": "submitted",
                    "project": "Beacon",
                    "dispatch_state": {"state": "reviewing"},
                    "review_lease": {"reviewer_agent_name": "reviewer-one"},
                },
                {
                    "ticket_id": "TK-human",
                    "title": "Choose rollout",
                    "status": "needs_human",
                    "project": "Beacon",
                    "human_request": {
                        "asked_by": {"agent_name": "worker-question"}
                    },
                },
            ],
            "truncated": False,
        }
        yield 5, {
            "seq": 5,
            "kind": "ticket_status_changed",
            "ticket_id": "TK-work",
            "status_to": "submitted",
            "claimed_by": "worker-build",
        }
        await cancel.wait()


class BlockingBoard(FakeBoard):
    def __init__(self) -> None:
        super().__init__()
        self.read_started = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.mutate_started = asyncio.Event()
        self.mutate_cancelled = asyncio.Event()

    async def my_tickets(self) -> list[JSON]:
        self.read_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.read_cancelled.set()
            raise
        raise AssertionError("unreachable")

    async def mutate(self, action: JSON) -> JSON:
        self.mutate_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.mutate_cancelled.set()
            raise
        raise AssertionError("unreachable")


class FakeACPClient:
    """Small fake IDE client derived from the P0 ACP client harness."""

    def __init__(
        self,
        agent: PursersACPAgent,
        *,
        allow: bool | list[bool] = True,
        elicit: JSON | list[JSON] | None = None,
    ) -> None:
        self.agent = agent
        self.allow = allow
        self.elicit = elicit
        self.incoming: asyncio.Queue[bytes] = asyncio.Queue()
        self.outgoing: asyncio.Queue[JSON] = asyncio.Queue()
        self.next_id = 0
        self.updates: list[JSON] = []
        self.permissions: list[JSON] = []
        self.elicitations: list[JSON] = []
        self.elicitation_responses: list[JSON] = []
        self.wire: list[JSON] = []
        self.task = asyncio.create_task(agent.run(self.incoming.get, self.outgoing.put))

    async def close(self) -> None:
        await self.incoming.put(b"")
        await self.task

    async def notify(self, method: str, params: JSON) -> None:
        await self.incoming.put(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params}).encode()
        )

    async def request(self, method: str, params: JSON) -> Any:
        self.next_id += 1
        request_id = self.next_id
        await self.incoming.put(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            ).encode()
        )
        while True:
            message = await asyncio.wait_for(
                self.outgoing.get(), TEST_TIMEOUT_S
            )
            self.wire.append(message)
            if message.get("method") == "session/update":
                self.updates.append(message["params"])
                continue
            if message.get("method") == "session/request_permission":
                self.permissions.append(message["params"])
                if isinstance(self.allow, list):
                    allow = self.allow.pop(0)
                else:
                    allow = self.allow
                outcome = {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": "allow-once" if allow else "reject-once",
                    }
                }
                await self.incoming.put(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": message["id"], "result": outcome}
                    ).encode()
                )
                continue
            if message.get("method") == "elicitation/create":
                self.elicitations.append(message["params"])
                if isinstance(self.elicit, list):
                    result = self.elicit.pop(0)
                elif isinstance(self.elicit, dict):
                    result = self.elicit
                else:
                    raise AssertionError("unexpected elicitation/create request")
                response = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": result,
                }
                self.elicitation_responses.append(response)
                await self.incoming.put(
                    json.dumps(response).encode()
                )
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message["result"]

    async def initialize(self) -> JSON:
        return await self.request(
            "initialize",
            {
                "protocolVersion": ACP_VERSION,
                "clientCapabilities": {
                    "auth": {"terminal": True},
                    "elicitation": {"form": {}},
                },
                "clientInfo": {"name": "fake-ide", "version": "1"},
            },
        )

    async def new_session(self, cwd: Path, mcp_servers: list[JSON] | None = None) -> str:
        result = await self.request(
            "session/new",
            {
                "cwd": str(cwd.resolve()),
                "mcpServers": [] if mcp_servers is None else mcp_servers,
            },
        )
        return result["sessionId"]

    async def prompt(self, session_id: str, text: str) -> JSON:
        return await self.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
        )


def test_conformance_honest_capabilities_and_read_intents(tmp_path: Path) -> None:
    asyncio.run(_conformance_honest_capabilities_and_read_intents(tmp_path))


async def _conformance_honest_capabilities_and_read_intents(tmp_path: Path) -> None:
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        initialized = await client.initialize()
        assert initialized["protocolVersion"] == 1
        assert initialized["agentCapabilities"] == {"promptCapabilities": {}}
        assert "loadSession" not in initialized["agentCapabilities"]
        assert initialized["authMethods"][0]["id"] == "pursers-personal-profile"
        assert initialized["authMethods"][0]["type"] == "agent"
        session = await client.new_session(tmp_path)
        commands_update = client.updates[-1]["update"]
        assert commands_update["sessionUpdate"] == "available_commands_update"
        assert [row["name"] for row in commands_update["availableCommands"]] == [
            "board",
            "create",
            "watch",
            "evidence",
            "answer",
        ]
        assert (await client.prompt(session, "my tickets"))["stopReason"] == "end_turn"
        assert [row["dispatch_ready"] for row in board.readiness_calls] == [
            True,
            False,
        ]
        assert board.readiness_calls[0]["foreground_running"] is True
        assert board.readiness_calls[1]["session_idle"] is True
        assert board.readiness_calls[1]["sequence"] == 2
        text = client.updates[-1]["update"]["content"]["text"]
        assert "TK-owned" in text
        assert str(tmp_path) not in text
    finally:
        await client.close()
    assert board.closed
    assert board.readiness_calls[-1]["transport_connected"] is False


def test_board_transport_is_opened_and_closed_by_run_loop_task() -> None:
    asyncio.run(_board_transport_is_opened_and_closed_by_run_loop_task())


async def _board_transport_is_opened_and_closed_by_run_loop_task() -> None:
    opened_by: asyncio.Task[object] | None = None

    class TaskBoundBoard(FakeBoard):
        async def close(self) -> None:
            assert asyncio.current_task() is opened_by
            await super().close()

    bound_board = TaskBoundBoard()

    def bound_factory() -> TaskBoundBoard:
        nonlocal opened_by
        opened_by = asyncio.current_task()
        return bound_board

    client = FakeACPClient(PursersACPAgent(bound_factory))
    await client.initialize()
    await client.close()
    assert bound_board.closed


def test_five_slash_commands_are_executable_and_writes_request_permission(
    tmp_path: Path,
) -> None:
    asyncio.run(_five_slash_commands_are_executable_and_writes_request_permission(tmp_path))


async def _five_slash_commands_are_executable_and_writes_request_permission(
    tmp_path: Path,
) -> None:
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        await client.prompt(session, "/board")
        await client.prompt(session, "/evidence TK-owned")
        await client.prompt(session, "/create Title :: Description")
        await client.prompt(session, "/answer TK-owned approved")

        messages = [
            row["update"].get("content", {}).get("text", "")
            for row in client.updates
            if row["update"].get("sessionUpdate") == "agent_message_chunk"
        ]
        assert any(text.startswith("Pursers board overview") for text in messages)
        assert any("branch_and_commit: branch@abc" in text for text in messages)
        assert [row["toolCall"]["rawInput"]["operation"] for row in client.permissions] == [
            "ticket_create",
            "ticket_human_resolve",
        ]
    finally:
        await client.close()


def test_board_failure_is_rendered_as_agent_message(tmp_path: Path) -> None:
    asyncio.run(_board_failure_is_rendered_as_agent_message(tmp_path))


def test_board_and_watch_make_multiple_projects_legible(tmp_path: Path) -> None:
    asyncio.run(_board_and_watch_make_multiple_projects_legible(tmp_path))


async def _board_and_watch_make_multiple_projects_legible(tmp_path: Path) -> None:
    class MultiProjectBoard(FakeBoard):
        async def my_tickets(self) -> list[JSON]:
            return [
                {
                    "ticket_id": "TK-a",
                    "status": "open",
                    "title": "API",
                    "project": "Atlas",
                },
                {
                    "ticket_id": "TK-b",
                    "status": "claimed",
                    "title": "UI",
                    "project": "Beacon",
                    "claimed_by": "worker-beacon",
                },
                {
                    "ticket_id": "TK-c",
                    "status": "submitted",
                    "title": "Docs",
                    "project": "Comet",
                },
            ]

        async def board_status(self) -> JSON:
            return {
                "board_id": self.board_id,
                "status_counts": {"open": 1, "claimed": 1, "submitted": 1},
                "latest_seq": 9,
                "projects": [
                    {
                        "name": "Atlas",
                        "status": "active",
                        "ticket_counts": {"needs_human": 1, "open": 1},
                    },
                    {
                        "name": "Beacon",
                        "status": "active",
                        "ticket_counts": {"claimed": 1},
                    },
                    {
                        "name": "Comet",
                        "status": "paused",
                        "ticket_counts": {"submitted": 1},
                    },
                ],
            }

    board = MultiProjectBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        assert await client.prompt(session, "/board") == {"stopReason": "end_turn"}
        text = client.updates[-1]["update"]["content"]["text"]
        assert "Pursers projects (showing 3 of 3)" in text
        assert "- Atlas [active] — 1 needs human, 1 open" in text
        assert "- Beacon [active] — 1 claimed" in text
        assert "- Comet [paused] — 1 submitted" in text
        assert "Needs your answer: 0" in text
        assert "Atlas\n- TK-a · Open · Unassigned — API" in text
        assert "Beacon\n- TK-b · Claimed · Held by worker-beacon — UI" in text
        assert "Comet\n- TK-c · Submitted · Awaiting review — Docs" in text

        prompt = asyncio.create_task(client.prompt(session, "watch"))
        await asyncio.wait_for(board.watch_started.wait(), TEST_TIMEOUT_S)
        while not any(
            row["update"].get("sessionUpdate") == "plan"
            and any(
                entry.get("content", "").startswith("Atlas ·")
                for entry in row["update"].get("entries", [])
            )
            for row in client.updates
        ):
            await asyncio.sleep(0)
        plan = next(
            row["update"]
            for row in reversed(client.updates)
            if row["update"].get("sessionUpdate") == "plan"
        )
        assert [entry["content"] for entry in plan["entries"]] == [
            "Beacon · TK-human — Choose rollout · Needs you · asked by worker-question",
            "Beacon · TK-review — Review active · In review · reviewer-one",
            (
                "Atlas · TK-submitted — Await review · Awaiting reviewer · "
                "submitted by worker-submit"
            ),
            (
                "Atlas · TK-work — Build runtime · Awaiting reviewer · "
                "submitted by worker-build"
            ),
        ]
        assert [entry["status"] for entry in plan["entries"]] == [
            "pending",
            "in_progress",
            "pending",
            "pending",
        ]
        await client.notify("session/cancel", {"sessionId": session})
        assert await prompt == {"stopReason": "cancelled"}
    finally:
        await client.close()


async def _board_failure_is_rendered_as_agent_message(tmp_path: Path) -> None:
    class BrokenBoard(FakeBoard):
        async def board_status(self) -> JSON:
            raise RuntimeError("profile points at an unavailable Central")

    client = FakeACPClient(PursersACPAgent(lambda: BrokenBoard()))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        assert await client.prompt(session, "/board") == {"stopReason": "end_turn"}
        text = client.updates[-1]["update"]["content"]["text"]
        assert "Check the selected Personal profile and board access" in text
        assert "unavailable Central" in text
    finally:
        await client.close()


def test_every_write_requires_exact_allow_once_payload(tmp_path: Path) -> None:
    asyncio.run(_every_write_requires_exact_allow_once_payload(tmp_path))


async def _every_write_requires_exact_allow_once_payload(tmp_path: Path) -> None:
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        result = await client.prompt(session, "create ticket Title :: Description")
        assert result == {"stopReason": "end_turn"}
        approved = client.permissions[0]["toolCall"]["rawInput"]
        assert approved == board.mutations[0]
        assert [row["kind"] for row in client.permissions[0]["options"]] == [
            "allow_once",
            "reject_once",
        ]
    finally:
        await client.close()


def test_rejected_write_makes_no_board_call(tmp_path: Path) -> None:
    asyncio.run(_rejected_write_makes_no_board_call(tmp_path))


async def _rejected_write_makes_no_board_call(tmp_path: Path) -> None:
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board), allow=False)
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        assert (await client.prompt(session, "annotate TK-one note"))["stopReason"] == "end_turn"
        assert board.mutations == []
    finally:
        await client.close()


def test_mutation_cards_use_human_titles_edit_kind_and_real_locations(
    tmp_path: Path,
) -> None:
    asyncio.run(_mutation_cards_use_human_titles_edit_kind_and_real_locations(tmp_path))


async def _mutation_cards_use_human_titles_edit_kind_and_real_locations(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "evidence.md"
    artifact.write_text("evidence", encoding="utf-8")

    class ArtifactBoard(FakeBoard):
        async def mutate(self, action: JSON) -> JSON:
            self.mutations.append(action)
            return {
                "ticket": {
                    "ticket_id": "TK-created",
                    "related_files": ["evidence.md", "../outside.txt"],
                }
            }

    board = ArtifactBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        await client.prompt(session, "/create Human title :: Human description")
        calls = [
            row["update"]
            for row in client.updates
            if row["update"].get("sessionUpdate") in {"tool_call", "tool_call_update"}
        ]
        assert calls[0]["title"] == "Create ticket on pursers"
        assert calls[0]["kind"] == "edit"
        assert client.permissions[0]["toolCall"]["title"] == "Create ticket on pursers"
        assert client.permissions[0]["toolCall"]["kind"] == "edit"
        assert calls[-1]["locations"] == [{"path": str(artifact), "line": 1}]
        assert "ticket_create on pursers" not in json.dumps(calls)
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("operation", "params", "title"),
    [
        ("ticket_create", {}, "Create ticket on pursers"),
        ("ticket_annotate", {"ticket_id": "TK-one"}, "Add note to TK-one"),
        (
            "ticket_human_resolve",
            {"ticket_id": "TK-one"},
            "Answer request on TK-one",
        ),
    ],
)
def test_mutation_titles_hide_internal_operation_names(
    operation: str, params: JSON, title: str
) -> None:
    assert agent_module._mutation_title(
        {"operation": operation, "board_id": "pursers", "params": params}
    ) == title


def test_watch_streams_then_cancel_stops_prompt(tmp_path: Path) -> None:
    asyncio.run(_watch_streams_then_cancel_stops_prompt(tmp_path))


async def _watch_streams_then_cancel_stops_prompt(tmp_path: Path) -> None:
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        prompt = asyncio.create_task(client.prompt(session, "watch pursers"))
        await asyncio.wait_for(board.watch_started.wait(), TEST_TIMEOUT_S)
        while not any(
            row["update"]
            .get("content", {})
            .get("text", "")
            .startswith("Pursers board update")
            for row in client.updates
        ):
            await asyncio.sleep(0)
        update = next(
            row["update"]["content"]["text"]
            for row in client.updates
            if row["update"]
            .get("content", {})
            .get("text", "")
            .startswith("Pursers board update")
        )
        assert update == (
            "Pursers board update\n\nTicket status changed\n"
            "Ticket: TK-work\nNow: Submitted\nBoard event: 5"
        )
        assert "{" not in update
        await client.notify("session/cancel", {"sessionId": session})
        assert await prompt == {"stopReason": "cancelled"}
        plans = [
            row["update"]
            for row in client.updates
            if row["update"].get("sessionUpdate") == "plan"
        ]
        assert [row["entries"] for row in plans] == [
            [
                {
                    "content": "pursers is quiet — no active work needs attention",
                    "priority": "low",
                    "status": "in_progress",
                }
            ],
            [
                {
                    "content": (
                        "Beacon · TK-human — Choose rollout · Needs you · asked by "
                        "worker-question"
                    ),
                    "priority": "high",
                    "status": "pending",
                },
                {
                    "content": (
                        "Beacon · TK-review — Review active · In review · "
                        "reviewer-one"
                    ),
                    "priority": "medium",
                    "status": "in_progress",
                },
                {
                    "content": (
                        "Atlas · TK-work — Build runtime · Working · worker-build"
                    ),
                    "priority": "medium",
                    "status": "in_progress",
                },
                {
                    "content": (
                        "Atlas · TK-submitted — Await review · Awaiting reviewer · "
                        "submitted by worker-submit"
                    ),
                    "priority": "low",
                    "status": "pending",
                },
            ],
            [
                {
                    "content": (
                        "Beacon · TK-human — Choose rollout · Needs you · asked by "
                        "worker-question"
                    ),
                    "priority": "high",
                    "status": "pending",
                },
                {
                    "content": (
                        "Beacon · TK-review — Review active · In review · "
                        "reviewer-one"
                    ),
                    "priority": "medium",
                    "status": "in_progress",
                },
                {
                    "content": (
                        "Atlas · TK-submitted — Await review · Awaiting reviewer · "
                        "submitted by worker-submit"
                    ),
                    "priority": "low",
                    "status": "pending",
                },
                {
                    "content": (
                        "Atlas · TK-work — Build runtime · Awaiting reviewer · "
                        "submitted by worker-build"
                    ),
                    "priority": "low",
                    "status": "pending",
                },
            ],
            [
                {
                    "content": "pursers is quiet — no active work needs attention",
                    "priority": "low",
                    "status": "completed",
                }
            ],
        ]
    finally:
        await client.close()


def test_live_plan_is_bounded_and_represents_overflow() -> None:
    tickets = {
        f"TK-{index:02d}": {
            "ticket_id": f"TK-{index:02d}",
            "title": f"Parallel item {index}",
            "phase": "work",
        }
        for index in range(25)
    }

    entries = agent_module._plan_entries(tickets)

    assert len(entries) == 20
    assert entries[-1] == {
        "content": "6 more in-flight tickets (20-entry plan cap)",
        "priority": "low",
        "status": "in_progress",
    }
    rendered = json.dumps(entries, sort_keys=True)
    assert "claimed_by" not in rendered
    assert "/Users/" not in rendered


def test_truncated_snapshot_is_disclosed_in_plan() -> None:
    entries = agent_module._plan_entries(
        {
            "TK-known": {
                "ticket_id": "TK-known",
                "title": "Known item",
                "phase": "work",
            }
        },
        snapshot_truncated=True,
    )

    assert entries[-1]["content"] == (
        "0 more known in-flight; additional active tickets are outside the "
        "500-ticket snapshot"
    )


def test_empty_truncated_snapshot_does_not_claim_the_board_is_idle() -> None:
    entries = agent_module._plan_entries({}, snapshot_truncated=True)

    assert entries == [
        {
            "content": (
                "0 more known in-flight; additional active tickets are outside the "
                "500-ticket snapshot"
            ),
            "priority": "low",
            "status": "in_progress",
        }
    ]
def test_watch_review_verdict_completes_turn_for_notification(tmp_path: Path) -> None:
    asyncio.run(_watch_review_verdict_completes_turn_for_notification(tmp_path))


async def _watch_review_verdict_completes_turn_for_notification(
    tmp_path: Path,
) -> None:
    class ReviewBoard(FakeBoard):
        async def watch(
            self, cursor: int | None, cancel: asyncio.Event
        ) -> AsyncIterator[tuple[int, JSON]]:
            yield 7, {
                "seq": 7,
                "kind": "ticket_status_changed",
                "ticket_id": "TK-reviewed",
                "status_to": "open",
                "review_verdict": "reject",
            }
            await cancel.wait()

    client = FakeACPClient(PursersACPAgent(lambda: ReviewBoard()))
    try:
        await client.initialize()
        session_id = await client.new_session(tmp_path)
        assert await client.prompt(session_id, "/watch") == {
            "stopReason": "end_turn"
        }
        plans = [
            row["update"]
            for row in client.updates
            if row["update"].get("sessionUpdate") == "plan"
        ]
        assert [row["entries"][0]["status"] for row in plans] == [
            "in_progress",
            "in_progress",
            "completed",
        ]
    finally:
        await client.close()


def test_watch_questions_use_stable_references_and_refusal_preserves_pending(
    tmp_path: Path,
) -> None:
    asyncio.run(_watch_questions_use_stable_references_and_refusal_preserves_pending(tmp_path))


async def _watch_questions_use_stable_references_and_refusal_preserves_pending(
    tmp_path: Path,
) -> None:
    class QuestionBoard(FakeBoard):
        def __init__(self) -> None:
            super().__init__()
            self.next_event = 0

        async def ticket_evidence(self, ticket_id: str) -> JSON:
            number = "one" if ticket_id == "TK-one" else "two"
            return {
                "ticket": {
                    "ticket_id": ticket_id,
                    "required_fields": ["observations"],
                    "coordinator_questions": [
                        {
                            "question_id": f"CQ-{number}",
                            "state": "open",
                            "message": f"Question from seat {number}",
                            "asked_at": "2026-09-20T14:00:00+00:00",
                            "asked_by": {"agent_name": f"worker-{number}"},
                        }
                    ],
                }
            }

        async def watch(
            self, cursor: int | None, cancel: asyncio.Event
        ) -> AsyncIterator[tuple[int, JSON]]:
            self.watch_started.set()
            seq, number = ((5, "one"), (6, "two"))[self.next_event]
            self.next_event += 1
            yield seq, {
                "seq": seq,
                "kind": "coordinator_question_asked",
                "ticket_id": f"TK-{number}",
                "question_id": f"CQ-{number}",
            }
            await cancel.wait()

    board = QuestionBoard()
    client = FakeACPClient(
        PursersACPAgent(lambda: board),
        allow=[True, False],
        elicit=[
            {
                "action": "accept",
                "content": {
                    "answer": "Ship the first change",
                    "observations": "First is unblocked",
                },
            },
            {
                "action": "accept",
                "content": {
                    "answer": "Hold the second change",
                    "observations": "Second needs more review",
                },
            },
        ],
    )
    try:
        await client.initialize()
        session_id = await client.new_session(tmp_path)
        assert await client.prompt(session_id, "/watch") == {
            "stopReason": "end_turn"
        }
        assert await client.prompt(session_id, "/watch") == {
            "stopReason": "end_turn"
        }
        plans = [
            row["update"]
            for row in client.updates
            if row["update"].get("sessionUpdate") == "plan"
        ]
        assert [row["entries"][0]["status"] for row in plans] == [
            "in_progress",
            "completed",
            "in_progress",
            "completed",
        ]

        assert await client.prompt(session_id, "ambiguous bare reply") == {
            "stopReason": "end_turn"
        }
        assert client.permissions == []
        assert "Pursers will not guess" in client.updates[-1]["update"]["content"]["text"]

        await client.prompt(session_id, "/answer #1")
        await client.prompt(session_id, "/answer #2")

        assert [row["toolCall"]["rawInput"]["operation"] for row in client.permissions] == [
            "ticket_question_answer",
            "ticket_question_answer",
        ]
        assert "host_binding" not in json.dumps(client.permissions)
        assert len(client.elicitations) == 2
        first_schema = client.elicitations[0]["requestedSchema"]
        assert first_schema["required"] == ["answer", "observations"]
        assert set(first_schema["properties"]) == {"answer", "observations"}
        first_elicitation_index = next(
            index
            for index, message in enumerate(client.wire)
            if message.get("method") == "elicitation/create"
        )
        first_permission_index = next(
            index
            for index, message in enumerate(client.wire)
            if message.get("method") == "session/request_permission"
        )
        assert first_elicitation_index < first_permission_index
        assert [row["params"]["question_id"] for row in board.mutations] == ["CQ-one"]
        pending = client.agent.sessions[session_id].pending_questions
        assert list(pending) == ["CQ-two"]
    finally:
        await client.close()


def test_question_elicitation_decline_and_cancel_leave_board_unanswered(
    tmp_path: Path,
) -> None:
    asyncio.run(_question_elicitation_decline_and_cancel_leave_board_unanswered(tmp_path))


async def _question_elicitation_decline_and_cancel_leave_board_unanswered(
    tmp_path: Path,
) -> None:
    class OneQuestionBoard(FakeBoard):
        async def ticket_evidence(self, ticket_id: str) -> JSON:
            return {
                "ticket": {
                    "ticket_id": ticket_id,
                    "required_fields": ["observations"],
                    "coordinator_questions": [
                        {
                            "question_id": "CQ-one",
                            "state": "open",
                            "message": "Choose safely",
                            "asked_at": "2026-09-20T14:00:00+00:00",
                            "asked_by": {"agent_name": "worker-one"},
                        }
                    ],
                }
            }

        async def watch(
            self, cursor: int | None, cancel: asyncio.Event
        ) -> AsyncIterator[tuple[int, JSON]]:
            yield 5, {
                "seq": 5,
                "kind": "coordinator_question_asked",
                "ticket_id": "TK-one",
                "question_id": "CQ-one",
            }
            await cancel.wait()

    board = OneQuestionBoard()
    client = FakeACPClient(
        PursersACPAgent(lambda: board),
        elicit=[{"action": "decline"}, {"action": "cancel"}],
    )
    try:
        await client.initialize()
        session_id = await client.new_session(tmp_path)
        assert await client.prompt(session_id, "/watch") == {
            "stopReason": "end_turn"
        }
        assert await client.prompt(session_id, "/answer") == {
            "stopReason": "end_turn"
        }
        assert await client.prompt(session_id, "/answer") == {
            "stopReason": "end_turn"
        }
        assert board.mutations == []
        assert client.permissions == []
        assert list(client.agent.sessions[session_id].pending_questions) == ["CQ-one"]
    finally:
        await client.close()


def _stdio_server_script(path: Path, source: str) -> None:
    path.write_text(
        "from mcp.server.mcpserver import MCPServer\n"
        + source
        + "\nserver.run(transport='stdio')\n",
        encoding="utf-8",
    )


def test_personal_watch_uses_wait_bridge_cursor_and_cancel_teardown(
    tmp_path: Path,
) -> None:
    asyncio.run(_personal_watch_uses_wait_bridge_cursor_and_cancel_teardown(tmp_path))


def test_personal_watch_projects_digest_holder_and_human_request() -> None:
    asyncio.run(_personal_watch_projects_digest_holder_and_human_request())


def test_personal_watch_retains_reviewer_name_across_live_digest_claim() -> None:
    asyncio.run(_personal_watch_retains_reviewer_name_across_live_digest_claim())


async def _personal_watch_retains_reviewer_name_across_live_digest_claim() -> None:
    profile = SimpleNamespace(board_id="pursers", principal_id="PR-human")
    board = PersonalBoardSurface(profile)
    board.agent_name = "human-personal"

    class SnapshotCentral:
        async def call_tool(self, name: str, arguments: JSON) -> Any:
            assert name == "ticket_list"
            return SimpleNamespace(
                is_error=False,
                structured_content={
                    "result": {
                        "tickets": [
                            {
                                "ticket_id": "TK-review",
                                "title": "Check the release",
                                "status": "submitted",
                                "project": "Atlas",
                                "claimed_by": "worker-submit",
                                "dispatch_state": {
                                    "state": "review_offered",
                                    "agent_id": "AI-reviewer",
                                },
                                "review_offer": {
                                    "agent_id": "AI-reviewer",
                                    "agent_name": "reviewer-one",
                                },
                            }
                        ],
                        "latest_seq": 6,
                        "total_matching": 1,
                    }
                },
                content=[],
            )

    class ReviewClaimDigest:
        closed = False

        async def close(self) -> None:
            self.closed = True

        async def digests(
            self, board_id: str, cursor: int | None, cancel: asyncio.Event
        ) -> AsyncIterator[JSON]:
            assert board_id == "pursers"
            assert cursor == 6
            # This is the real board_digest ticket shape: active review claims
            # carry only dispatch_state.agent_id, not review_lease or a name.
            yield {
                "cursor_map": {"pursers": 7},
                "tickets": [
                    {
                        "ticket_id": "TK-review",
                        "board_id": "pursers",
                        "title": "Check the release",
                        "transitions": [
                            {
                                "from": "submitted",
                                "to": "submitted",
                                "actor": "AI-reviewer",
                                "at": "2030-01-01T00:00:00+00:00",
                            }
                        ],
                        "transitions_omitted_count": 0,
                        "status_now": "submitted",
                        "review": {
                            "verdict": None,
                            "rejection_count": 0,
                            "reviewer": None,
                        },
                        "claimed_by": "worker-submit",
                        "dispatch_state": {
                            "state": "review_claimed",
                            "kind": "review",
                            "agent_id": "AI-reviewer",
                        },
                        "offers": {},
                    }
                ],
                "human_requests": [],
            }

    bridge = ReviewClaimDigest()
    board._client = SnapshotCentral()  # type: ignore[assignment]
    board._wait_bridge_factory = lambda: bridge
    rows = [row async for row in board.watch(None, asyncio.Event())]

    prior = agent_module._plan_ticket(rows[0][1]["tickets"][0])
    assert prior is not None
    current = agent_module._plan_ticket(rows[1][1], prior=prior)
    assert current is not None
    assert current["holder"] == "reviewer-one"
    assert agent_module._plan_content(current) == (
        "Atlas · TK-review — Check the release · In review · reviewer-one"
    )
    assert "AI-reviewer" not in agent_module._plan_content(current)
    assert bridge.closed


def test_plan_review_transition_does_not_reuse_a_different_offer_holder() -> None:
    prior = agent_module._plan_ticket(
        {
            "ticket_id": "TK-review",
            "title": "Check the release",
            "status": "submitted",
            "claimed_by": "worker-submit",
            "review_offer": {
                "agent_id": "AI-old-reviewer",
                "agent_name": "old-reviewer",
            },
        }
    )
    assert prior is not None

    current = agent_module._plan_ticket(
        {
            "ticket_id": "TK-review",
            "title": "Check the release",
            "status_to": "submitted",
            "dispatch_state": {
                "state": "review_claimed",
                "agent_id": "AI-new-reviewer",
            },
        },
        prior=prior,
    )

    assert current is not None
    assert current["holder"] is None
    assert agent_module._plan_content(current).endswith(
        "In review · reviewer not reported"
    )
    assert "old-reviewer" not in agent_module._plan_content(current)
    assert "AI-new-reviewer" not in agent_module._plan_content(current)


async def _personal_watch_projects_digest_holder_and_human_request() -> None:
    profile = SimpleNamespace(board_id="pursers", principal_id="PR-human")
    board = PersonalBoardSurface(profile)
    board.agent_name = "human-personal"

    class SnapshotCentral:
        async def call_tool(self, name: str, arguments: JSON) -> Any:
            assert name == "ticket_list"
            return SimpleNamespace(
                is_error=False,
                structured_content={
                    "result": {
                        "tickets": [],
                        "latest_seq": 6,
                        "total_matching": 0,
                    }
                },
                content=[],
            )

    class OneDigest:
        closed = False

        async def close(self) -> None:
            self.closed = True

        async def digests(
            self, board_id: str, cursor: int | None, cancel: asyncio.Event
        ) -> AsyncIterator[JSON]:
            assert board_id == "pursers"
            assert cursor == 6
            yield {
                "cursor_map": {"pursers": 7},
                "tickets": [
                    {
                        "ticket_id": "TK-human",
                        "title": "Choose rollout",
                        "status_now": "needs_human",
                        "claimed_by": None,
                        "dispatch_state": {"state": "needs_human"},
                    }
                ],
                "human_requests": [
                    {
                        "ticket_id": "TK-human",
                        "asked_by": "worker-question",
                    }
                ],
            }

    central = SnapshotCentral()
    bridge = OneDigest()
    board._client = central  # type: ignore[assignment]
    board._wait_bridge_factory = lambda: bridge
    rows = [row async for row in board.watch(None, asyncio.Event())]

    assert rows[1] == (
        7,
        {
            "kind": "ticket_status_changed",
            "ticket_id": "TK-human",
            "title": "Choose rollout",
            "project": None,
            "status_to": "needs_human",
            "dispatch_state": {"state": "needs_human"},
            "claimed_by": None,
            "review_state": None,
            "review_claimed_by": None,
            "human_request": {
                "asked_by": {"agent_name": "worker-question"}
            },
        },
    )
    assert bridge.closed


def test_cancel_stops_blocked_watch_snapshot(tmp_path: Path) -> None:
    asyncio.run(_cancel_stops_blocked_watch_snapshot(tmp_path))


@pytest.mark.parametrize(
    "authority_error",
    [
        "question inbox requires role coordinator",
        (
            "board_question_inbox Central error: coordinator inbox requires "
            "registered project coordinator ownership on this board"
        ),
    ],
    ids=["worker-role", "unbound-coordinator"],
)
def test_question_inbox_authority_failure_preserves_ordinary_digest(
    authority_error: str,
) -> None:
    asyncio.run(
        _question_inbox_authority_failure_preserves_ordinary_digest(authority_error)
    )


async def _question_inbox_authority_failure_preserves_ordinary_digest(
    authority_error: str,
) -> None:
    server = MCPServer("question-inbox-authority-stub")

    @server.tool()
    async def board_digest(
        since: dict[str, int],
        boards: list[str],
        include_notes_keys: list[str],
        max_transitions_per_ticket: int,
    ) -> JSON:
        assert since == {"pursers": 11}
        assert boards == ["pursers"]
        assert include_notes_keys == []
        assert max_transitions_per_ticket == 2
        return {
            "cursor_map": {"pursers": 12},
            "tickets": [
                {
                    "ticket_id": "TK-ordinary",
                    "title": "Ordinary update",
                    "status_now": "submitted",
                }
            ],
        }

    @server.tool()
    async def board_question_inbox(state: str, limit: int) -> JSON:
        assert state == "open"
        assert limit == 100
        raise ToolError(authority_error)

    async with Client(server, mode="2026-07-28", cache=None) as raw:
        bridge = StdioWaitBridge("stub", [], {})
        bridge._client = raw
        page = await bridge._digest_with_questions("pursers", 11)

    assert page["cursor_map"] == {"pursers": 12}
    assert page["tickets"] == [
        {
            "ticket_id": "TK-ordinary",
            "title": "Ordinary update",
            "status_now": "submitted",
        }
    ]
    assert page["questions"] == []
    assert page["question_inbox_unavailable"] == agent_module.QUESTION_INBOX_UNAVAILABLE


def test_personal_watch_reports_question_limit_once_and_streams_ticket() -> None:
    asyncio.run(_personal_watch_reports_question_limit_once_and_streams_ticket())


async def _personal_watch_reports_question_limit_once_and_streams_ticket() -> None:
    profile = SimpleNamespace(board_id="pursers", principal_id="PR-human")
    board = PersonalBoardSurface(profile)

    class SnapshotCentral:
        async def call_tool(self, name: str, arguments: JSON) -> Any:
            assert name == "ticket_list"
            return SimpleNamespace(
                is_error=False,
                structured_content={
                    "result": {
                        "tickets": [],
                        "latest_seq": 11,
                        "total_matching": 0,
                    }
                },
                content=[],
            )

    class UnauthorizedQuestionBridge:
        closed = False

        async def close(self) -> None:
            self.closed = True

        async def digests(
            self, board_id: str, cursor: int | None, cancel: asyncio.Event
        ) -> AsyncIterator[JSON]:
            assert board_id == "pursers"
            assert cursor == 11
            for status in ("claimed", "submitted"):
                yield {
                    "cursor_map": {"pursers": 12},
                    "question_inbox_unavailable": (
                        agent_module.QUESTION_INBOX_UNAVAILABLE
                    ),
                    "questions": [],
                    "tickets": [
                        {
                            "ticket_id": "TK-ordinary",
                            "title": "Ordinary update",
                            "status_now": status,
                        }
                    ],
                }

    central = SnapshotCentral()
    bridge = UnauthorizedQuestionBridge()
    board._client = central  # type: ignore[assignment]
    board._wait_bridge_factory = lambda: bridge  # type: ignore[assignment]
    events = board.watch(None, asyncio.Event())
    try:
        observed = [await anext(events) for _ in range(4)]
    finally:
        await events.aclose()

    assert [event[1]["kind"] for event in observed] == [
        "watch_snapshot",
        "question_inbox_unavailable",
        "ticket_status_changed",
        "ticket_status_changed",
    ]
    assert observed[1][1]["message"] == agent_module.QUESTION_INBOX_UNAVAILABLE
    assert observed[2][1]["ticket_id"] == "TK-ordinary"
    assert observed[3][1]["status_to"] == "submitted"
    assert bridge.closed is True


async def _cancel_stops_blocked_watch_snapshot(tmp_path: Path) -> None:
    profile = SimpleNamespace(board_id="pursers", principal_id="PR-human")
    board = PersonalBoardSurface(profile)
    board.agent_name = "human-personal"

    class BlockingCentral:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def call_tool(self, name: str, arguments: JSON) -> Any:
            assert name == "ticket_list"
            assert arguments["board_id"] == "pursers"
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            raise AssertionError("unreachable")

    central = BlockingCentral()
    board._client = central  # type: ignore[assignment]
    bridge_opened = False

    def bridge_factory() -> Any:
        nonlocal bridge_opened
        bridge_opened = True
        raise AssertionError("wait bridge opened before the snapshot completed")

    board._wait_bridge_factory = bridge_factory
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        prompt = asyncio.create_task(client.prompt(session, "watch pursers"))
        await asyncio.wait_for(central.started.wait(), TEST_TIMEOUT_S)
        await client.notify("session/cancel", {"sessionId": session})
        assert await asyncio.wait_for(prompt, TEST_TIMEOUT_S) == {
            "stopReason": "cancelled"
        }
        assert central.cancelled.is_set()
        assert not bridge_opened
        assert client.updates[-1]["update"] == {
            "sessionUpdate": "plan",
            "entries": [
                {
                    "content": "pursers is quiet — no active work needs attention",
                    "priority": "low",
                    "status": "completed",
                }
            ],
        }
    finally:
        await client.close()


def test_cancel_stops_blocked_wait_bridge_digest_and_closes_bridge(
    tmp_path: Path,
) -> None:
    asyncio.run(_cancel_stops_blocked_wait_bridge_digest_and_closes_bridge(tmp_path))


async def _cancel_stops_blocked_wait_bridge_digest_and_closes_bridge(
    tmp_path: Path,
) -> None:
    profile = SimpleNamespace(board_id="pursers", principal_id="PR-human")
    board = PersonalBoardSurface(profile)
    board.agent_name = "human-personal"

    class SnapshotCentral:
        async def call_tool(self, name: str, arguments: JSON) -> Any:
            assert name == "ticket_list"
            assert arguments["board_id"] == "pursers"
            return SimpleNamespace(
                is_error=False,
                structured_content={
                    "result": {
                        "tickets": [],
                        "latest_seq": 6,
                        "total_matching": 0,
                    }
                },
                content=[],
            )

    class BlockingDigestClient:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def call_tool(self, name: str, arguments: JSON) -> Any:
            assert name == "board_digest"
            assert arguments["since"] == {"pursers": 6}
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            raise AssertionError("unreachable")

    central = SnapshotCentral()
    digest_client = BlockingDigestClient()
    bridge = StdioWaitBridge("stub", [], {})
    bridge._client = digest_client  # type: ignore[assignment]
    board._client = central  # type: ignore[assignment]
    board._wait_bridge_factory = lambda: bridge
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        prompt = asyncio.create_task(client.prompt(session, "watch pursers"))
        await asyncio.wait_for(digest_client.started.wait(), TEST_TIMEOUT_S)
        await client.notify("session/cancel", {"sessionId": session})
        assert await asyncio.wait_for(prompt, TEST_TIMEOUT_S) == {
            "stopReason": "cancelled"
        }
        assert digest_client.cancelled.is_set()
        assert bridge._client is None
        assert client.updates[-1]["update"] == {
            "sessionUpdate": "plan",
            "entries": [
                {
                    "content": "pursers is quiet — no active work needs attention",
                    "priority": "low",
                    "status": "completed",
                }
            ],
        }
    finally:
        await client.close()


async def _personal_watch_uses_wait_bridge_cursor_and_cancel_teardown(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "digest-calls.jsonl"
    script = tmp_path / "wait_bridge.py"
    _stdio_server_script(
        script,
        """import asyncio, json, os
from pathlib import Path
from mcp.server.mcpserver import Context
server = MCPServer('wait-bridge-stub')
calls = 0
@server.resource('board://pursers/digest')
async def digest_resource():
    return '{}'
@server.tool()
async def board_question_inbox(ctx: Context, state: str, limit: int):
    assert state == 'open'
    assert limit == 100
    return {'questions': []}
@server.tool()
async def board_digest(ctx: Context, since: dict[str, int], boards: list[str], include_notes_keys: list[str], max_transitions_per_ticket: int):
    global calls
    calls += 1
    marker = Path(os.environ['WAIT_MARKER'])
    with marker.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'boards': boards, 'include_notes_keys': include_notes_keys, 'max_transitions_per_ticket': max_transitions_per_ticket, 'since': since}, sort_keys=True) + '\\n')
    if calls == 1:
        return {'cursor_map': {'pursers': 7}, 'tickets': [{'ticket_id': 'TK-live', 'title': 'Live item', 'status_now': 'claimed', 'dispatch_state': {'state': 'claimed'}}]}
    if calls == 2:
        async def notify():
            await asyncio.sleep(0.05)
            await ctx.notify_resource_updated('board://pursers/digest')
        asyncio.create_task(notify())
        return {'cursor_map': {'pursers': 7}, 'tickets': []}
    return {'cursor_map': {'pursers': 8}, 'tickets': [{'ticket_id': 'TK-live', 'title': 'Live item', 'status_now': 'submitted', 'dispatch_state': {'state': 'broadcast'}}]}
""",
    )
    profile = SimpleNamespace(board_id="pursers", principal_id="PR-human")
    board = PersonalBoardSurface(profile)
    board.agent_name = "human-personal"
    bridges: list[StdioWaitBridge] = []

    class RejectDirectWatchClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call_tool(self, name: str, arguments: JSON) -> Any:
            self.calls.append(name)
            if name != "ticket_list":
                raise AssertionError("watch bypassed the configured wait bridge")
            assert arguments == {
                "board_id": "pursers",
                "include_closed": False,
                "include_archived": False,
                "limit": 500,
            }
            return SimpleNamespace(
                is_error=False,
                structured_content={
                    "result": {
                        "tickets": [
                            {
                                "ticket_id": "TK-snapshot",
                                "title": "Snapshot item",
                                "status": "claimed",
                            }
                        ],
                        "latest_seq": 6,
                        "total_matching": 1,
                    }
                },
                content=[],
            )

        def listen(self, **_kwargs: object) -> None:
            self.calls.append("listen")
            raise AssertionError("watch opened Central's journal directly")

    central = RejectDirectWatchClient()
    board._client = central  # type: ignore[assignment]

    async def factory() -> StdioWaitBridge:
        bridge = await StdioWaitBridge.connect(
            sys.executable, [str(script)], {"WAIT_MARKER": str(marker)}
        )
        bridges.append(bridge)
        return bridge

    board._wait_bridge_factory = factory
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        prompt = asyncio.create_task(client.prompt(session, "watch pursers"))
        async with asyncio.timeout(TEST_TIMEOUT_S):
            while not marker.exists() or len(marker.read_text().splitlines()) < 3:
                await asyncio.sleep(0.01)
        await client.notify("session/cancel", {"sessionId": session})
        assert await prompt == {"stopReason": "cancelled"}
    finally:
        await client.close()

    calls = [json.loads(line) for line in marker.read_text().splitlines()]
    assert calls[0] == {
        "boards": ["pursers"],
        "include_notes_keys": [],
        "max_transitions_per_ticket": 2,
        "since": {"pursers": 6},
    }
    assert calls[1]["since"] == {"pursers": 7}
    assert calls[2]["since"] == {"pursers": 7}
    assert bridges and bridges[0]._client is None
    assert central.calls == ["ticket_list"]


def test_nonempty_stdio_mcp_servers_initialize_and_close(tmp_path: Path) -> None:
    asyncio.run(_nonempty_stdio_mcp_servers_initialize_and_close(tmp_path))


async def _nonempty_stdio_mcp_servers_initialize_and_close(tmp_path: Path) -> None:
    marker = tmp_path / "mcp-marker.txt"
    script = tmp_path / "mcp_server.py"
    _stdio_server_script(
        script,
        """import os
from pathlib import Path
Path(os.environ['MCP_MARKER']).write_text(os.environ['PRIVATE_VALUE'], encoding='utf-8')
server = MCPServer('acp-session-mcp-stub')
""",
    )
    secret = "mcp-private-value"
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(
            tmp_path,
            [
                {
                    "name": "fixture",
                    "command": sys.executable,
                    "args": [str(script)],
                    "env": [
                        {"name": "MCP_MARKER", "value": str(marker)},
                        {"name": "PRIVATE_VALUE", "value": secret},
                    ],
                }
            ],
        )
        assert session.startswith("pursers-")
        assert marker.read_text(encoding="utf-8") == secret
        rendered = json.dumps(
            {"updates": client.updates, "permissions": client.permissions},
            sort_keys=True,
        )
        assert secret not in rendered
    finally:
        await client.close()
    assert board.closed


def test_cancel_stops_in_flight_read_and_returns_cancelled(tmp_path: Path) -> None:
    asyncio.run(_cancel_stops_in_flight_read_and_returns_cancelled(tmp_path))


def test_cancel_before_prompt_task_starts_is_bound_to_that_turn(
    tmp_path: Path,
) -> None:
    asyncio.run(_cancel_before_prompt_task_starts_is_bound_to_that_turn(tmp_path))


async def _cancel_before_prompt_task_starts_is_bound_to_that_turn(
    tmp_path: Path,
) -> None:
    board = BlockingBoard()
    agent = PursersACPAgent(lambda: board)
    agent.initialized = True
    agent.board = board
    agent.sessions["session"] = agent_module.Session(cwd=str(tmp_path))
    sent: list[JSON] = []
    response_sent = asyncio.Event()

    async def send(message: JSON) -> None:
        sent.append(message)
        if message.get("id") in {1, 2}:
            response_sent.set()

    agent.send = send
    await agent.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/prompt",
            "params": {
                "sessionId": "session",
                "prompt": [{"type": "text", "text": "my tickets"}],
            },
        }
    )
    await agent.handle(
        {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": "session"},
        }
    )
    await asyncio.wait_for(response_sent.wait(), TEST_TIMEOUT_S)

    response = next(message for message in sent if message.get("id") == 1)
    assert response["result"] == {"stopReason": "cancelled"}
    assert not board.read_started.is_set()
    assert not agent.sessions["session"].active

    # Cancellation belongs only to request 1; the next turn gets a fresh event.
    agent.board = FakeBoard()
    response_sent.clear()
    await agent.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {
                "sessionId": "session",
                "prompt": [{"type": "text", "text": "my tickets"}],
            },
        }
    )
    await asyncio.wait_for(response_sent.wait(), TEST_TIMEOUT_S)
    second = next(message for message in sent if message.get("id") == 2)
    assert second["result"] == {"stopReason": "end_turn"}
    await agent.close()


async def _cancel_stops_in_flight_read_and_returns_cancelled(
    tmp_path: Path,
) -> None:
    board = BlockingBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        prompt = asyncio.create_task(client.prompt(session, "my tickets"))
        await asyncio.wait_for(board.read_started.wait(), TEST_TIMEOUT_S)
        await client.notify("session/cancel", {"sessionId": session})
        assert await asyncio.wait_for(prompt, TEST_TIMEOUT_S) == {
            "stopReason": "cancelled"
        }
        assert board.read_cancelled.is_set()
    finally:
        await client.close()


def test_cancel_stops_approved_in_flight_write(tmp_path: Path) -> None:
    asyncio.run(_cancel_stops_approved_in_flight_write(tmp_path))


async def _cancel_stops_approved_in_flight_write(tmp_path: Path) -> None:
    board = BlockingBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        prompt = asyncio.create_task(
            client.prompt(session, "create ticket Title :: Description")
        )
        await asyncio.wait_for(board.mutate_started.wait(), TEST_TIMEOUT_S)
        await client.notify("session/cancel", {"sessionId": session})
        assert await asyncio.wait_for(prompt, TEST_TIMEOUT_S) == {
            "stopReason": "cancelled"
        }
        assert board.mutate_cancelled.is_set()
        updates = [row["update"] for row in client.updates]
        assert any(
            row.get("sessionUpdate") == "tool_call_update"
            and row.get("status") == "failed"
            and row.get("content", [{}])[0]
            .get("content", {})
            .get("text")
            == "Board write was cancelled."
            for row in updates
        )
    finally:
        await client.close()


def test_authenticate_runs_setup_without_exposing_credentials(tmp_path: Path) -> None:
    asyncio.run(_authenticate_runs_setup_without_exposing_credentials(tmp_path))


async def _authenticate_runs_setup_without_exposing_credentials(tmp_path: Path) -> None:
    secret = "seat-token-must-not-escape"
    board = FakeBoard()
    configured = False
    settings: JSON = {}

    def board_factory() -> FakeBoard:
        if not configured:
            raise AuthRequired("profile missing")
        return board

    async def setup() -> None:
        nonlocal configured
        configured = True
        settings["profile"] = "/PATH/TO/profile.json"

    client = FakeACPClient(PursersACPAgent(board_factory, auth_setup=setup))
    try:
        initialized = await client.initialize()
        assert [row["id"] for row in initialized["authMethods"]] == [
            "pursers-personal-profile",
            "pursers-personal-login",
        ]
        await client.request(
            "authenticate", {"methodId": "pursers-personal-profile"}
        )
        await client.new_session(tmp_path)
        rendered = json.dumps(
            {
                "initialized": initialized,
                "updates": client.updates,
                "permissions": client.permissions,
                "settings": settings,
            },
            sort_keys=True,
        )
        assert secret not in rendered
        assert "TOKEN" not in settings
    finally:
        await client.close()


def test_login_invokes_personal_setup_when_profile_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = Namespace(profile=None, project=tmp_path, login=True)
    calls: list[Path | None] = []
    selections = 0

    class Parser:
        def parse_args(self) -> Namespace:
            return args

    def select(**_kwargs: object) -> object:
        nonlocal selections
        selections += 1
        if selections == 1:
            raise agent_module.PersonalProfileError("missing")
        return object()

    monkeypatch.setattr(agent_module, "_parser", lambda: Parser())
    monkeypatch.setattr(agent_module, "select_personal_profile", select)
    monkeypatch.setattr(
        agent_module, "_run_personal_setup", lambda project: calls.append(project)
    )
    agent_module.main()
    assert calls == [tmp_path]
    assert selections == 2


def test_setup_environment_excludes_inherited_seat_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "seat-secret")
    monkeypatch.setenv("ONBOARD_TOKEN_FILE", "/secret/token")
    monkeypatch.setenv("PATH", "/usr/bin")
    environment = agent_module._setup_environment()
    assert environment["PATH"] == "/usr/bin"
    assert "ONBOARD_CENTRAL_TOKEN" not in environment
    assert "ONBOARD_TOKEN_FILE" not in environment


def test_personal_setup_command_has_no_inherited_board_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "seat-secret")
    monkeypatch.setenv("ONBOARD_TOKEN_FILE", "/secret/token")
    monkeypatch.setattr(agent_module.shutil, "which", lambda _name: "/bin/setup")
    monkeypatch.setattr(
        agent_module, "default_profiles_root", lambda: tmp_path / "profiles"
    )
    monkeypatch.setattr(
        agent_module,
        "profile_path_for_project",
        lambda _project, root: root / "project-fixture" / "profile.json",
    )

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs["env"]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(agent_module.subprocess, "run", run)
    agent_module._run_personal_setup(tmp_path)
    command, environment = calls[0]
    assert command == [
        "/bin/setup",
        "setup",
        "--project",
        str(tmp_path.resolve()),
        "--apply",
        "--activate",
        "--host-id",
        "pursers-acp",
        "--session",
        "ide",
        "--host-config",
        str(tmp_path / "profiles" / "project-fixture" / "acp-host.json"),
    ]
    assert "seat-secret" not in json.dumps(command)
    assert "ONBOARD_CENTRAL_TOKEN" not in environment
    assert "ONBOARD_TOKEN_FILE" not in environment


class InProcessPersonalBoard(PersonalBoardSurface):
    def __init__(
        self,
        raw: Client,
        principal_id: str,
        *,
        agent_name: str = "human-personal",
        agent_id: str = "",
        coordinator_binding: str = "",
        central_instance_id: str | None = None,
    ) -> None:
        self.profile = None
        self.board_id = "acp-e2e"
        self.principal_id = principal_id
        self.agent_name = agent_name
        self.agent_id = agent_id
        self._coordinator_binding = coordinator_binding
        self.central_instance_id = central_instance_id
        self._wait_bridge_factory = None
        self._client = raw
        self._readiness_enabled = True

    async def close(self) -> None:
        self._client = None


def test_end_to_end_create_against_in_process_central(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_end_to_end_create_against_in_process_central(tmp_path, monkeypatch))


async def _end_to_end_create_against_in_process_central(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jwks = tmp_path / "jwks.json"
    jwks.write_text('{"keys": []}', encoding="utf-8")
    monkeypatch.setenv("CENTRAL_AUTH_MODE", "jwt")
    monkeypatch.setenv("CENTRAL_JWT_ISSUER", "https://issuer.invalid")
    monkeypatch.setenv("CENTRAL_JWT_AUDIENCE", "http://localhost:8765/mcp")
    monkeypatch.setenv("CENTRAL_JWKS_PATH", str(jwks))
    monkeypatch.setenv("CENTRAL_ADMISSION", "invite")
    monkeypatch.setenv("STORE_BACKEND", "sqlite")
    mcp, service = central.build_server("localhost", 8765, tmp_path / "central")
    principal = central.Principal(
        "PR-acp-human",
        "acp-human",
        frozenset({"board:read", "board:write", "board:review"}),
    )
    monkeypatch.setattr(central, "current_principal", lambda: principal)
    async with Client(mcp, mode="2026-07-28", cache=None) as raw:
        joined = BoardClient._decode(
            await raw.call_tool(
                "board_join",
                {
                    "board_id": "acp-e2e",
                    "agent_name": "human-personal",
                    "role": "worker",
                    "capabilities": {"can_work": False, "can_review": False},
                },
            )
        )
        assert joined["principal_id"] == principal.principal_id
        board = InProcessPersonalBoard(
            raw,
            principal.principal_id,
            central_instance_id=service.instance_id,
        )
        client = FakeACPClient(PursersACPAgent(lambda: board))
        try:
            await client.initialize()
            session = await client.new_session(tmp_path)
            assert (
                await client.prompt(session, "create ticket ACP E2E :: Created through ACP")
            ) == {"stopReason": "end_turn"}
            created_text = client.updates[-1]["update"]["content"]["text"]
            ticket_id = created_text.removeprefix("Created ticket ").removesuffix(".")
            fetched = BoardClient._decode(
                await raw.call_tool(
                    "ticket_get", {"board_id": "acp-e2e", "ticket_id": ticket_id}
                )
            )
            assert fetched["ticket"]["title"] == "ACP E2E"
            assert fetched["ticket"]["created_by_principal_id"] == principal.principal_id
        finally:
            await client.close()


def test_real_acp_turn_controls_dispatch_readiness_and_preserves_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _real_acp_turn_controls_dispatch_readiness_and_preserves_claim(
            tmp_path, monkeypatch
        )
    )


async def _real_acp_turn_controls_dispatch_readiness_and_preserves_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jwks = tmp_path / "readiness-jwks.json"
    jwks.write_text('{"keys": []}', encoding="utf-8")
    monkeypatch.setenv("CENTRAL_AUTH_MODE", "jwt")
    monkeypatch.setenv("CENTRAL_JWT_ISSUER", "https://issuer.invalid")
    monkeypatch.setenv("CENTRAL_JWT_AUDIENCE", "http://localhost:8765/mcp")
    monkeypatch.setenv("CENTRAL_JWKS_PATH", str(jwks))
    monkeypatch.setenv("CENTRAL_ADMISSION", "invite")
    monkeypatch.setenv("STORE_BACKEND", "sqlite")
    mcp, _service = central.build_server(
        "localhost", 8765, tmp_path / "readiness-central"
    )
    principal = central.Principal(
        "PR-acp-worker",
        "acp-worker",
        frozenset({"board:read", "board:write", "board:review"}),
    )
    monkeypatch.setattr(central, "current_principal", lambda: principal)

    class BlockingBridge:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def digests(
            self, board_id: str, cursor: int | None, cancel: asyncio.Event
        ) -> AsyncIterator[JSON]:
            assert board_id == "acp-e2e"
            self.started.set()
            await cancel.wait()
            if False:
                yield {"latest_seq": cursor or 0}

        async def close(self) -> None:
            return None

    async with Client(mcp, mode="2026-07-28", cache=None) as raw:
        joined = BoardClient._decode(
            await raw.call_tool(
                "board_join",
                {
                    "board_id": "acp-e2e",
                    "agent_name": "interactive-worker",
                    "role": "worker",
                    "capabilities": {
                        "can_work": True,
                        "can_review": False,
                        "tier_max": 2,
                        "max_parallel": 1,
                    },
                },
            )
        )
        agent_id = joined["agent_id"]
        bridge = BlockingBridge()
        board = InProcessPersonalBoard(
            raw,
            principal.principal_id,
            agent_name="interactive-worker",
            agent_id=agent_id,
        )
        board._wait_bridge_factory = lambda: bridge
        client = FakeACPClient(PursersACPAgent(lambda: board))

        async def call(name: str, **params: object) -> JSON:
            return BoardClient._decode(
                await raw.call_tool(name, {"board_id": "acp-e2e", **params})
            )

        try:
            await client.initialize()
            session = await client.new_session(tmp_path)
            assert await client.prompt(session, "/board") == {
                "stopReason": "end_turn"
            }

            created = await call(
                "ticket_create",
                agent_name="interactive-worker",
                title="Later ACP work",
                description="Must wait for a consuming model turn",
                target_url="acp-e2e",
                scope="interactive-no-send",
                required_fields=["test_output"],
                prefer_agents=[agent_id],
            )
            ticket_id = created["ticket"]["ticket_id"]
            assert "work_offer" not in created["ticket"]
            assert created["ticket"].get("work_offer_expirations", 0) == 0
            assert not client.task.done(), "ACP transport stays connected after end_turn"

            prompt = asyncio.create_task(client.prompt(session, "watch acp-e2e"))
            await asyncio.wait_for(bridge.started.wait(), TEST_TIMEOUT_S)
            offered = (await call("ticket_get", ticket_id=ticket_id))["ticket"]
            assert offered["work_offer"]["agent_id"] == agent_id
            await call(
                "ticket_claim",
                agent_name="interactive-worker",
                ticket_id=ticket_id,
            )
            claimed = (await call("ticket_get", ticket_id=ticket_id))["ticket"]
            lease_expires_at = claimed["lease_expires_at"]

            await client.notify("session/cancel", {"sessionId": session})
            assert await prompt == {"stopReason": "cancelled"}
            held = (await call("ticket_get", ticket_id=ticket_id))["ticket"]
            assert held["status"] == "claimed"
            assert held["claimed_by_agent_id"] == agent_id
            assert held["lease_expires_at"] >= lease_expires_at
            status = await call("board_status")
            worker = next(
                row for row in status["agents"] if row["agent_id"] == agent_id
            )
            assert worker["readiness"]["transport_connected"] is True
            assert worker["readiness"]["dispatch_ready"] is False
        finally:
            await client.close()


def test_real_acp_reviewer_turn_uses_review_scope_and_controls_offers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_real_acp_reviewer_turn_uses_review_scope_and_controls_offers(
        tmp_path, monkeypatch
    ))


async def _real_acp_reviewer_turn_uses_review_scope_and_controls_offers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jwks = tmp_path / "reviewer-readiness-jwks.json"
    jwks.write_text('{"keys": []}', encoding="utf-8")
    monkeypatch.setenv("CENTRAL_AUTH_MODE", "jwt")
    monkeypatch.setenv("CENTRAL_JWT_ISSUER", "https://issuer.invalid")
    monkeypatch.setenv("CENTRAL_JWT_AUDIENCE", "http://localhost:8765/mcp")
    monkeypatch.setenv("CENTRAL_JWKS_PATH", str(jwks))
    monkeypatch.setenv("CENTRAL_ADMISSION", "invite")
    monkeypatch.setenv("STORE_BACKEND", "sqlite")
    mcp, _service = central.build_server(
        "localhost", 8765, tmp_path / "reviewer-readiness-central"
    )
    admin = central.Principal(
        "PR-admin-worker",
        "admin-worker",
        frozenset({"board:read", "board:write", "board:review"}),
    )
    reviewer_principal = central.Principal(
        "PR-acp-reviewer",
        "acp-reviewer",
        frozenset({"board:read", "board:review"}),
    )
    active = {"principal": admin}
    monkeypatch.setattr(central, "current_principal", lambda: active["principal"])

    class BlockingBridge:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def digests(
            self, board_id: str, cursor: int | None, cancel: asyncio.Event
        ) -> AsyncIterator[JSON]:
            self.started.set()
            await cancel.wait()
            if False:
                yield {"latest_seq": cursor or 0}

        async def close(self) -> None:
            return None

    async with Client(mcp, mode="2026-07-28", cache=None) as raw:
        async def call(name: str, **params: object) -> JSON:
            return BoardClient._decode(
                await raw.call_tool(name, {"board_id": "review-e2e", **params})
            )

        worker = await call(
            "board_join",
            agent_name="worker-admin",
            role="worker",
            capabilities={"can_work": True, "can_review": False},
        )
        await call(
            "board_member_add",
            agent_name="worker-admin",
            principal_id=reviewer_principal.principal_id,
            role="reviewer",
        )
        active["principal"] = reviewer_principal
        reviewer = await call(
            "board_join",
            agent_name="interactive-reviewer",
            role="reviewer",
            capabilities={"can_work": False, "can_review": True},
            readiness={
                "transport_connected": True,
                "session_idle": True,
                "foreground_running": False,
                "dispatch_ready": False,
                "managed_autonomous": False,
                "session_id": "reviewer-bootstrap",
                "sequence": 0,
                "ttl_s": 360,
            },
        )
        active["principal"] = admin
        created = await call(
            "ticket_create",
            agent_name="worker-admin",
            title="ACP reviewer target",
            description="Review only during a consuming ACP prompt",
            target_url="review-e2e",
            scope="interactive-no-send",
            required_fields=["test_output"],
            prefer_agents=[worker["agent_id"]],
        )
        ticket_id = created["ticket"]["ticket_id"]
        await call("ticket_claim", agent_name="worker-admin", ticket_id=ticket_id)
        submitted = await call(
            "ticket_submit",
            agent_name="worker-admin",
            ticket_id=ticket_id,
            summary="ready for ACP review",
        )
        assert "review_offer" not in submitted

        active["principal"] = reviewer_principal
        bridge = BlockingBridge()
        board = InProcessPersonalBoard(
            raw,
            reviewer_principal.principal_id,
            agent_name="interactive-reviewer",
            agent_id=reviewer["agent_id"],
        )
        board.board_id = "review-e2e"
        board._wait_bridge_factory = lambda: bridge
        client = FakeACPClient(PursersACPAgent(lambda: board))
        try:
            await client.initialize()
            session = await client.new_session(tmp_path)
            assert await client.prompt(session, "/board") == {"stopReason": "end_turn"}
            idle = (await call("ticket_get", ticket_id=ticket_id))["ticket"]
            assert "review_offer" not in idle

            prompt = asyncio.create_task(client.prompt(session, "watch review-e2e"))
            await asyncio.wait_for(bridge.started.wait(), TEST_TIMEOUT_S)
            offered = (await call("ticket_get", ticket_id=ticket_id))["ticket"]
            assert offered["review_offer"]["agent_id"] == reviewer["agent_id"]
            await call(
                "ticket_review_claim",
                agent_name="interactive-reviewer",
                ticket_id=ticket_id,
            )
            await client.notify("session/cancel", {"sessionId": session})
            assert await prompt == {"stopReason": "cancelled"}
            held = (await call("ticket_get", ticket_id=ticket_id))["ticket"]
            assert held["review_lease"]["reviewer_agent_id"] == reviewer["agent_id"]
            status = await call("board_status")
            projected = next(
                row for row in status["agents"]
                if row["agent_id"] == reviewer["agent_id"]
            )
            assert projected["readiness"]["dispatch_ready"] is False
        finally:
            await client.close()


def test_personal_board_surface_rejects_wrong_cloned_central_before_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _personal_board_surface_rejects_wrong_cloned_central_before_operations(
            tmp_path, monkeypatch
        )
    )


async def _personal_board_surface_rejects_wrong_cloned_central_before_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jwks = tmp_path / "instance-jwks.json"
    jwks.write_text('{"keys": []}', encoding="utf-8")
    monkeypatch.setenv("CENTRAL_AUTH_MODE", "jwt")
    monkeypatch.setenv("CENTRAL_JWT_ISSUER", "https://issuer.invalid")
    monkeypatch.setenv("CENTRAL_JWT_AUDIENCE", "http://localhost:8765/mcp")
    monkeypatch.setenv("CENTRAL_JWKS_PATH", str(jwks))
    monkeypatch.setenv("CENTRAL_ADMISSION", "invite")
    monkeypatch.setenv("STORE_BACKEND", "sqlite")
    source_root = tmp_path / "instance-source"
    source_mcp, source_service = central.build_server(
        "localhost", 8765, source_root
    )
    principal = central.Principal(
        "PR-acp-instance",
        "acp-instance",
        frozenset({"board:read", "board:write", "board:review"}),
    )
    monkeypatch.setattr(central, "current_principal", lambda: principal)
    async with Client(source_mcp, mode="2026-07-28", cache=None) as source:
        joined = BoardClient._decode(
            await source.call_tool(
                "board_join",
                {
                    "board_id": "acp-e2e",
                    "agent_name": "human-personal",
                    "role": "worker",
                    "capabilities": {"can_work": False, "can_review": False},
                },
            )
        )
        assert joined["principal_id"] == principal.principal_id
        created = BoardClient._decode(
            await source.call_tool(
                "ticket_create",
                {
                    "board_id": "acp-e2e",
                    "ticket_id": "TK-collision",
                    "agent_name": "human-personal",
                    "title": "Cloned ticket",
                    "description": "Must not be addressed through the source profile",
                    "scope": "interactive-no-send",
                    "required_fields": ["observations"],
                    "unassigned": True,
                },
            )
        )
        assert created["ticket"]["ticket_id"] == "TK-collision"

    clone_root = tmp_path / "instance-clone"
    shutil.copytree(source_root, clone_root)
    original_instance, clone_instance = fork_central_instance_identity(clone_root)
    assert original_instance == source_service.instance_id
    assert clone_instance != original_instance
    clone_mcp, clone_service = central.build_server(
        "localhost", 8765, clone_root
    )
    before_board = clone_service.load("acp-e2e")
    before_journal = clone_service.journal.read_after("acp-e2e", 0, 100)

    async with Client(clone_mcp, mode="2026-07-28", cache=None) as wrong_raw:
        wrong = InProcessPersonalBoard(
            wrong_raw,
            principal.principal_id,
            central_instance_id=source_service.instance_id,
        )
        calls = (
            ("board_status", {}),
            ("ticket_get", {"ticket_id": "TK-collision"}),
            (
                "ticket_create",
                {
                    "agent_name": "human-personal",
                    "title": "Wrong instance create",
                    "description": "Must fail before mutation",
                    "scope": "interactive-no-send",
                    "required_fields": ["observations"],
                    "unassigned": True,
                },
            ),
            (
                "ticket_annotate",
                {
                    "agent_name": "human-personal",
                    "ticket_id": "TK-collision",
                    "text": "must not be written",
                    "kind": "note",
                },
            ),
            (
                "ticket_human_resolve",
                {
                    "agent_name": "human-personal",
                    "ticket_id": "TK-collision",
                    "request_id": "HR-wrong-instance",
                    "action": "accept",
                    "content": {"answer": "must not be written"},
                    "disposition": "reopen",
                },
            ),
            (
                "ticket_question_answer",
                {
                    "agent_name": "human-personal",
                    "ticket_id": "TK-collision",
                    "question_id": "CQ-wrong-instance",
                    "host_binding": "wrong-instance-binding",
                    "action": "answer",
                    "message": "must not be written",
                },
            ),
        )
        for method, arguments in calls:
            with pytest.raises(
                CentralInstanceMismatchError, match="Central instance mismatch"
            ):
                await wrong._call(method, arguments)

    assert clone_service.load("acp-e2e") == before_board
    assert clone_service.journal.read_after("acp-e2e", 0, 100) == before_journal


def test_personal_watch_binds_real_wait_bridge_before_wrong_clone_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _personal_watch_binds_real_wait_bridge_before_wrong_clone_side_effects(
            tmp_path, monkeypatch
        )
    )


async def _personal_watch_binds_real_wait_bridge_before_wrong_clone_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    url = f"http://127.0.0.1:{port}/mcp"
    jwks, issue = _instance_test_token_fixture(tmp_path, url)
    identity_name = "acp-watch-personal"
    principal_id = _instance_test_principal_id(identity_name)
    token = issue(identity_name, "board:read board:write")
    monkeypatch.setenv("CENTRAL_AUTH_MODE", "jwt")
    monkeypatch.setenv("CENTRAL_JWT_ISSUER", INSTANCE_TEST_ISSUER)
    monkeypatch.setenv("CENTRAL_JWT_AUDIENCE", url)
    monkeypatch.setenv("CENTRAL_JWKS_PATH", str(jwks))
    monkeypatch.setenv("CENTRAL_ADMISSION", "invite")
    monkeypatch.setenv("STORE_BACKEND", "sqlite")
    principal = central.Principal(
        principal_id,
        identity_name,
        frozenset({"board:read", "board:write"}),
    )
    monkeypatch.setattr(central, "current_principal", lambda: principal)

    board_id = "acp-watch-instance"
    source_root = tmp_path / "watch-source"
    source_mcp, source_service = central.build_server("127.0.0.1", port, source_root)
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    async with Client(source_mcp, mode="2026-07-28", cache=None) as source:
        joined = BoardClient._decode(
            await source.call_tool(
                "board_join",
                {
                    "board_id": board_id,
                    "agent_name": identity_name,
                    "role": "worker",
                    "capabilities": {"can_work": False, "can_review": False},
                },
                meta={agent_module.INSTANCE_META_KEY: source_service.instance_id},
            )
        )
        BoardClient._decode(
            await source.call_tool(
                "ticket_create",
                {
                    "board_id": board_id,
                    "ticket_id": "TK-watch-source",
                    "agent_name": identity_name,
                    "title": "Source watch ticket",
                    "description": "The clone must not expose this through ACP watch",
                    "scope": "interactive-no-send",
                    "required_fields": ["observations"],
                    "unassigned": True,
                },
                meta={agent_module.INSTANCE_META_KEY: source_service.instance_id},
            )
        )

        clone_root = tmp_path / "watch-clone"
        shutil.copytree(source_root, clone_root)
        original_instance, clone_instance = fork_central_instance_identity(clone_root)
        assert original_instance == source_service.instance_id
        clone_mcp, clone_service = central.build_server("127.0.0.1", port, clone_root)
        app = create_streamable_http_app(
            clone_mcp, clone_service, host="127.0.0.1"
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="error",
                access_log=False,
            )
        )
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [listener]},
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert server.started

        repository = Path(__file__).resolve().parents[3]
        wait_bridge_root = repository / "tools" / "wait-bridge"
        client_src = repository / "packages" / "client" / "src"

        def bridge_environment(expected_instance: str) -> dict[str, str]:
            profile = SimpleNamespace(
                central_url=url,
                board_id=board_id,
                principal_id=principal_id,
                central_instance_id=expected_instance,
            )
            environment = agent_module._wait_bridge_environment(
                profile, token, joined
            )
            environment.update(
                {
                    "PURSERS_BOARDS": "home",
                    "PURSERS_WAIT_MODE": "push",
                    "PURSERS_BRIDGE_STATE_DIR": str(
                        tmp_path / f"bridge-state-{expected_instance}"
                    ),
                    "PURSERS_BRIDGE_STATS": str(
                        tmp_path / f"bridge-stats-{expected_instance}.json"
                    ),
                    "PYTHONPATH": os.pathsep.join(
                        (str(client_src), str(wait_bridge_root))
                    ),
                }
            )
            return environment

        async def bridge_factory(expected_instance: str) -> StdioWaitBridge:
            return await StdioWaitBridge.connect(
                sys.executable,
                [str(wait_bridge_root / "pursers_wait_server.py")],
                bridge_environment(expected_instance),
            )

        original_load = clone_service.load
        original_read_after = clone_service.journal.read_after
        before_board = copy.deepcopy(original_load(board_id))
        before_journal = copy.deepcopy(original_read_after(board_id, 0, 100))
        before_cursor = clone_service.cursors.get(
            principal_id, identity_name, board_id
        )
        calls = {
            "load": 0,
            "journal": 0,
            "subscription_allowed": 0,
            "register_listener": 0,
            "record_agent_activity": 0,
        }

        def tracked_load(selected_board: str) -> JSON:
            calls["load"] += 1
            return original_load(selected_board)

        def tracked_read_after(*args: Any, **kwargs: Any) -> Any:
            calls["journal"] += 1
            return original_read_after(*args, **kwargs)

        def wrap_method(name: str):
            original = getattr(clone_service, name)

            def tracked(*args: Any, **kwargs: Any) -> Any:
                calls[name] += 1
                return original(*args, **kwargs)

            return tracked

        monkeypatch.setattr(clone_service, "load", tracked_load)
        monkeypatch.setattr(clone_service.journal, "read_after", tracked_read_after)
        for method in (
            "subscription_allowed",
            "register_listener",
            "record_agent_activity",
        ):
            monkeypatch.setattr(clone_service, method, wrap_method(method))

        wrong_profile = SimpleNamespace(
            board_id=board_id,
            principal_id=principal_id,
            central_instance_id=source_service.instance_id,
        )
        wrong = PersonalBoardSurface(wrong_profile)
        wrong.agent_name = identity_name
        wrong._client = source  # type: ignore[assignment]
        wrong._wait_bridge_factory = lambda: bridge_factory(source_service.instance_id)
        wrong_watch = wrong.watch(None, asyncio.Event())
        snapshot_cursor, snapshot = await anext(wrong_watch)
        assert snapshot_cursor > 0
        assert snapshot["kind"] == "watch_snapshot"
        with pytest.raises(
            CentralInstanceMismatchError, match="Central instance mismatch"
        ) as mismatch:
            async with asyncio.timeout(TEST_TIMEOUT_S):
                await anext(wrong_watch)
        await wrong_watch.aclose()
        mismatch_detail = str(mismatch.value)
        assert token not in mismatch_detail
        assert str(tmp_path) not in mismatch_detail
        assert source_service.instance_id not in mismatch_detail
        assert clone_instance not in mismatch_detail

        assert calls == {
            "load": 0,
            "journal": 0,
            "subscription_allowed": 0,
            "register_listener": 0,
            "record_agent_activity": 0,
        }
        assert clone_service.active_stream_count == 0
        assert not clone_service.active_listeners.get(board_id)
        assert clone_service.last_seen_activity == {}
        assert original_load(board_id) == before_board
        assert original_read_after(board_id, 0, 100) == before_journal
        assert (
            clone_service.cursors.get(principal_id, identity_name, board_id)
            == before_cursor
        )

        async with Client(clone_mcp, mode="2026-07-28", cache=None) as clone:
            same_profile = SimpleNamespace(
                board_id=board_id,
                principal_id=principal_id,
                central_instance_id=clone_instance,
            )
            same = PersonalBoardSurface(same_profile)
            same.agent_name = identity_name
            same._client = clone  # type: ignore[assignment]
            same._wait_bridge_factory = lambda: bridge_factory(clone_instance)
            same_watch = same.watch(None, asyncio.Event())
            _same_cursor, same_snapshot = await anext(same_watch)
            assert same_snapshot["kind"] == "watch_snapshot"
            BoardClient._decode(
                await clone.call_tool(
                    "ticket_create",
                    {
                        "board_id": board_id,
                        "ticket_id": "TK-watch-same-instance",
                        "agent_name": identity_name,
                        "title": "Same instance watch ticket",
                        "description": "Prove the bound watch remains functional",
                        "scope": "interactive-no-send",
                        "required_fields": ["observations"],
                        "unassigned": True,
                    },
                    meta={agent_module.INSTANCE_META_KEY: clone_instance},
                )
            )
            try:
                async with asyncio.timeout(TEST_TIMEOUT_S):
                    while True:
                        same_event = await anext(same_watch)
                        if same_event[1].get("ticket_id") == "TK-watch-same-instance":
                            break
                assert same_event[1]["kind"] == "ticket_status_changed"
            finally:
                await same_watch.aclose()

    if server is not None:
        server.should_exit = True
    if thread is not None:
        await asyncio.to_thread(thread.join, 5)
    listener.close()


def test_end_to_end_two_seat_questions_answer_and_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_end_to_end_two_seat_questions_answer_and_refuse(tmp_path, monkeypatch))


async def _end_to_end_two_seat_questions_answer_and_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jwks = tmp_path / "questions-jwks.json"
    jwks.write_text('{"keys": []}', encoding="utf-8")
    monkeypatch.setenv("CENTRAL_AUTH_MODE", "jwt")
    monkeypatch.setenv("CENTRAL_JWT_ISSUER", "https://issuer.invalid")
    monkeypatch.setenv("CENTRAL_JWT_AUDIENCE", "http://localhost:8765/mcp")
    monkeypatch.setenv("CENTRAL_JWKS_PATH", str(jwks))
    monkeypatch.setenv("CENTRAL_ADMISSION", "invite")
    monkeypatch.setenv("STORE_BACKEND", "sqlite")
    mcp, service = central.build_server("localhost", 8765, tmp_path / "question-central")
    principals = {
        "admin": central.Principal(
            "PR-admin", "admin", frozenset({"board:read", "board:write"})
        ),
        "coord": central.Principal(
            "PR-coord",
            "coord",
            frozenset({"board:read", "board:write", "board:coordinate"}),
        ),
        "worker-one": central.Principal(
            "PR-worker-one",
            "worker-one",
            frozenset({"board:read", "board:write"}),
        ),
        "worker-two": central.Principal(
            "PR-worker-two",
            "worker-two",
            frozenset({"board:read", "board:write"}),
        ),
    }
    current = {"name": "admin"}
    binding_secret = "throwaway-question-binding"
    monkeypatch.setattr(
        central, "current_principal", lambda: principals[current["name"]]
    )
    monkeypatch.setattr(
        central,
        "current_host_binding",
        lambda agent_id: hashlib.sha256(
            json.dumps([binding_secret, agent_id], separators=(",", ":")).encode()
        ).hexdigest(),
    )

    async with Client(mcp, mode="2026-07-28", cache=None) as raw:
        async def call(name: str, **arguments: object) -> JSON:
            result = await raw.call_tool(name, {"board_id": "acp-e2e", **arguments})
            return BoardClient._decode(result)

        await call("board_join", agent_name="admin-agent")
        agent_ids: dict[str, str] = {}
        for name in ("coord", "worker-one", "worker-two"):
            await call(
                "board_member_add",
                agent_name="admin-agent",
                principal_id=principals[name].principal_id,
                role="member",
            )
            current["name"] = name
            joined = await call(
                "board_join",
                agent_name=name,
                role="coordinator" if name == "coord" else "worker",
            )
            agent_ids[name] = joined["agent_id"]
            current["name"] = "admin"
        await call(
            "board_state_update",
            agent_name="admin-agent",
            key="project_registry",
            value=json.dumps(
                {
                    "schema_version": 1,
                    "projects": {
                        "acp-e2e": {
                            "board_id": "acp-e2e",
                            "work_dir": "/PATH/TO/acp-e2e",
                            "status": "active",
                        }
                    },
                }
            ),
        )
        await call(
            "board_state_update",
            agent_name="admin-agent",
            key=central.PROJECT_COORDINATORS_STATE_KEY,
            value=json.dumps({"acp-e2e": [agent_ids["coord"]]}),
        )

        events: list[JSON] = []
        for number, worker in (("one", "worker-one"), ("two", "worker-two")):
            ticket_id = f"TK-{number}"
            await call(
                "ticket_create",
                ticket_id=ticket_id,
                agent_name="admin-agent",
                title=f"Question {number}",
                description="Throwaway ACP question transcript",
                scope="interactive-no-send",
                required_fields=["observations"],
                unassigned=True,
            )
            current["name"] = worker
            await call("ticket_claim", ticket_id=ticket_id, agent_name=worker)
            asked = await call(
                "ticket_question_ask",
                ticket_id=ticket_id,
                agent_name=worker,
                message=f"Question from {worker}",
                kind="decision",
            )
            events.append(asked["event"])
            current["name"] = "admin"

        class ProductQuestionBoard(InProcessPersonalBoard):
            next_event = 0

            async def watch(
                self, cursor: int | None, cancel: asyncio.Event
            ) -> AsyncIterator[tuple[int, JSON]]:
                event = events[self.next_event]
                self.next_event += 1
                yield int(event["seq"]), event
                await cancel.wait()

        current["name"] = "coord"
        coordinator_binding = central.current_host_binding(agent_ids["coord"])
        board = ProductQuestionBoard(
            raw,
            principals["coord"].principal_id,
            agent_name="coord",
            agent_id=agent_ids["coord"],
            coordinator_binding=coordinator_binding,
            central_instance_id=service.instance_id,
        )
        client = FakeACPClient(
            PursersACPAgent(lambda: board),
            allow=[True, False],
            elicit=[
                {
                    "action": "accept",
                    "content": {
                        "answer": "Ship one first",
                        "observations": "First seat can proceed",
                    },
                },
                {
                    "action": "decline",
                },
                {
                    "action": "cancel",
                },
                {
                    "action": "accept",
                    "content": {
                        "answer": "Hold two",
                        "observations": "Second seat stays blocked",
                    },
                },
            ],
        )
        try:
            await client.initialize()
            session_id = await client.new_session(tmp_path)
            assert await client.prompt(session_id, "/watch") == {
                "stopReason": "end_turn"
            }
            assert await client.prompt(session_id, "/watch") == {
                "stopReason": "end_turn"
            }
            assert await client.prompt(session_id, "/answer #1") == {
                "stopReason": "end_turn"
            }
            assert len(client.elicitations) == 1
            assert len(client.permissions) == 1
            permission_message = client.permissions[0]["toolCall"]["rawInput"][
                "params"
            ]["message"]
            assert isinstance(permission_message, str), repr(permission_message)
            answer_messages = [
                row["update"].get("content", {}).get("text", "")
                for row in client.updates
                if row["update"].get("sessionUpdate") == "agent_message_chunk"
            ]
            assert service.load("acp-e2e")["tickets"]["TK-one"][
                "coordinator_questions"
            ][0]["state"] == "answered", answer_messages
            path_states = {
                "accept_then_allow": {
                    "TK-one": "answered",
                    "TK-two": "open",
                }
            }
            assert await client.prompt(session_id, "/answer #2") == {
                "stopReason": "end_turn"
            }
            assert len(client.permissions) == 1
            assert service.load("acp-e2e")["tickets"]["TK-two"][
                "coordinator_questions"
            ][0]["state"] == "open"
            path_states["decline"] = {"TK-two": "open"}
            assert await client.prompt(session_id, "/answer #2") == {
                "stopReason": "end_turn"
            }
            assert len(client.permissions) == 1
            assert service.load("acp-e2e")["tickets"]["TK-two"][
                "coordinator_questions"
            ][0]["state"] == "open"
            path_states["cancel"] = {"TK-two": "open"}
            assert await client.prompt(session_id, "/answer #2") == {
                "stopReason": "end_turn"
            }
            path_states["accept_then_reject_permission"] = {"TK-two": "open"}

            state = {
                ticket_id: [
                    {
                        "question_id": question["question_id"],
                        "state": question["state"],
                        "answer": question.get("answer"),
                    }
                    for question in service.load("acp-e2e")["tickets"][ticket_id][
                        "coordinator_questions"
                    ]
                ]
                for ticket_id in ("TK-one", "TK-two")
            }
            assert state["TK-one"][0]["state"] == "answered"
            assert state["TK-one"][0]["answer"] == (
                "Ship one first\n\nobservations: First seat can proceed"
            )
            assert state["TK-two"][0]["state"] == "open"
            assert state["TK-two"][0]["answer"] is None
            assert len(client.permissions) == 2
            assert "host_binding" not in json.dumps(client.permissions)

            if os.environ.get("PURSERS_ACP_TRANSCRIPT") == "1":
                session_updates = [
                    message
                    for message in client.wire
                    if message.get("method") == "session/update"
                    and (
                        message.get("params", {})
                        .get("update", {})
                        .get("sessionUpdate")
                        in {"agent_message_chunk", "tool_call", "tool_call_update"}
                    )
                ]
                permission_requests = [
                    message
                    for message in client.wire
                    if message.get("method") == "session/request_permission"
                ]
                elicitation_requests = [
                    message
                    for message in client.wire
                    if message.get("method") == "elicitation/create"
                ]
                print(
                    "ACP_QUESTION_TRANSCRIPT="
                    + json.dumps(
                        {
                            "session_updates": session_updates,
                            "elicitation_requests": elicitation_requests,
                            "elicitation_responses": client.elicitation_responses,
                            "permission_requests": permission_requests,
                            "path_states": path_states,
                            "board_state": state,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
        finally:
            await client.close()


def _bridge_profile() -> SimpleNamespace:
    return SimpleNamespace(
        central_url="https://127.0.0.1:1/mcp",
        board_id="pursers",
        central_instance_id="CI-" + "a" * 64,
    )


def test_wait_bridge_environment_forwards_model_and_provider_from_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PURSERS_MODEL", "env-model")
    identity = {
        "agent_name": "ide-seat",
        "role": "worker",
        "capabilities": {"model": "joined-model", "provider": "joined-provider"},
    }

    environment = agent_module._wait_bridge_environment(
        _bridge_profile(), "not-a-real-token", identity
    )

    assert environment["PURSERS_MODEL"] == "joined-model"
    assert environment["PURSERS_PROVIDER"] == "joined-provider"
    assert environment["ONBOARD_CENTRAL_INSTANCE_ID"] == "CI-" + "a" * 64


def test_wait_bridge_environment_falls_back_to_agent_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PURSERS_MODEL", " env-model ")
    monkeypatch.delenv("PURSERS_PROVIDER", raising=False)
    identity = {"agent_name": "ide-seat", "role": "worker", "capabilities": {"model": None}}

    environment = agent_module._wait_bridge_environment(
        _bridge_profile(), "not-a-real-token", identity
    )

    assert environment["PURSERS_MODEL"] == "env-model"
    assert "PURSERS_PROVIDER" not in environment
