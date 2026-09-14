from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from pursers_client import BoardClient
from pursers_acp.agent import ACP_VERSION, PursersACPAgent
from pursers_acp.agent import PersonalBoardSurface
from pursers_central import central

JSON = dict[str, Any]


class FakeBoard:
    board_id = "pursers"

    def __init__(self) -> None:
        self.closed = False
        self.mutations: list[JSON] = []
        self.watch_started = asyncio.Event()

    async def close(self) -> None:
        self.closed = True

    async def my_tickets(self) -> list[JSON]:
        return [{"ticket_id": "TK-owned", "status": "open", "title": "Owned"}]

    async def my_offers(self) -> list[JSON]:
        return [{"ticket_id": "TK-offer", "agent_name": "personal", "kind": "work"}]

    async def board_status(self) -> JSON:
        return {"board_id": self.board_id, "status_counts": {"open": 1}, "latest_seq": 4}

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

    async def mutate(self, action: JSON) -> JSON:
        self.mutations.append(action)
        if action["operation"] == "ticket_create":
            return {"ticket": {"ticket_id": "TK-created"}}
        return {"annotation": {"annotation_id": "AN-created"}}

    async def watch(
        self, cursor: int | None, cancel: asyncio.Event
    ) -> AsyncIterator[tuple[int, JSON]]:
        self.watch_started.set()
        yield 5, {"seq": 5, "kind": "ticket_created", "ticket_id": "TK-live"}
        await cancel.wait()


class FakeACPClient:
    """Small fake IDE client derived from the P0 ACP client harness."""

    def __init__(self, agent: PursersACPAgent, *, allow: bool = True) -> None:
        self.agent = agent
        self.allow = allow
        self.incoming: asyncio.Queue[bytes] = asyncio.Queue()
        self.outgoing: asyncio.Queue[JSON] = asyncio.Queue()
        self.next_id = 0
        self.updates: list[JSON] = []
        self.permissions: list[JSON] = []
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
            message = await asyncio.wait_for(self.outgoing.get(), 2)
            if message.get("method") == "session/update":
                self.updates.append(message["params"])
                continue
            if message.get("method") == "session/request_permission":
                self.permissions.append(message["params"])
                outcome = {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": "allow-once" if self.allow else "reject-once",
                    }
                }
                await self.incoming.put(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": message["id"], "result": outcome}
                    ).encode()
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
                "clientCapabilities": {"auth": {"terminal": True}},
                "clientInfo": {"name": "fake-ide", "version": "1"},
            },
        )

    async def new_session(self, cwd: Path) -> str:
        result = await self.request(
            "session/new", {"cwd": str(cwd.resolve()), "mcpServers": []}
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
        assert (await client.prompt(session, "my tickets"))["stopReason"] == "end_turn"
        text = client.updates[-1]["update"]["content"]["text"]
        assert "TK-owned" in text
        assert str(tmp_path) not in text
    finally:
        await client.close()
    assert board.closed


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


def test_watch_streams_then_cancel_stops_prompt(tmp_path: Path) -> None:
    asyncio.run(_watch_streams_then_cancel_stops_prompt(tmp_path))


async def _watch_streams_then_cancel_stops_prompt(tmp_path: Path) -> None:
    board = FakeBoard()
    client = FakeACPClient(PursersACPAgent(lambda: board))
    try:
        await client.initialize()
        session = await client.new_session(tmp_path)
        prompt = asyncio.create_task(client.prompt(session, "watch pursers"))
        await asyncio.wait_for(board.watch_started.wait(), 1)
        while not any(
            row["update"].get("content", {}).get("text", "").startswith("Board event")
            for row in client.updates
        ):
            await asyncio.sleep(0)
        await client.notify("session/cancel", {"sessionId": session})
        assert await prompt == {"stopReason": "cancelled"}
    finally:
        await client.close()


class InProcessPersonalBoard(PersonalBoardSurface):
    def __init__(self, raw: Client, principal_id: str) -> None:
        self.profile = None
        self.board_id = "acp-e2e"
        self.principal_id = principal_id
        self.agent_name = "human-personal"
        self._client = raw

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
    mcp, _service = central.build_server("localhost", 8765, tmp_path / "central")
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
        board = InProcessPersonalBoard(raw, principal.principal_id)
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
