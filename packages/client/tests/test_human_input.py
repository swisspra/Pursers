from __future__ import annotations

from typing import Any

import pytest

from pursers_client import (
    BoardClient,
    HUMAN_INPUT_REQUESTED,
    HUMAN_INPUT_RESOLVED,
    KNOWN_EVENT_KINDS,
    human_form_safety,
)


def client() -> BoardClient:
    return BoardClient(
        "https://central.example/mcp", "TOKEN_PLACEHOLDER", "board-a",
        agent_name="worker-a",
    )


@pytest.mark.anyio
async def test_human_request_and_resolution_forward_exact_arguments(monkeypatch) -> None:
    board = client()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        return {
            "event": {
                "id": f"EV-{len(calls)}",
                "kind": (
                    HUMAN_INPUT_REQUESTED
                    if name == "ticket_request_human"
                    else HUMAN_INPUT_RESOLVED
                ),
                "payload_ref": "board://board-a/ticket/TK-1",
            }
        }

    monkeypatch.setattr(board, "_call", call)
    schema = {
        "type": "object",
        "properties": {"choice": {"type": "string", "enum": ["a", "b"]}},
    }
    await board.ticket_request_human(
        "TK-1", "Choose", "decision", requested_schema=schema,
        url="https://example.invalid/input",
    )
    await board.ticket_human_resolve(
        "TK-1", "HR-1", "accept", content={"choice": "a"}, note="approved",
    )

    assert calls == [
        (
            "ticket_request_human",
            {
                "agent_name": "worker-a", "ticket_id": "TK-1",
                "message": "Choose", "kind": "decision",
                "requested_schema": schema,
                "url": "https://example.invalid/input",
            },
        ),
        (
            "ticket_human_resolve",
            {
                "agent_name": "worker-a", "ticket_id": "TK-1",
                "request_id": "HR-1", "action": "accept",
                "disposition": "reopen", "content": {"choice": "a"},
                "note": "approved",
            },
        ),
    ]
    assert "board://board-a/ticket/TK-1" in board._watched_uris
    assert len(board._local_events) == 2
    assert {HUMAN_INPUT_REQUESTED, HUMAN_INPUT_RESOLVED} <= KNOWN_EVENT_KINDS


@pytest.mark.parametrize(
    ("message", "schema"),
    [
        ("Paste the credential and file path", {"type": "object"}),
        ("Continue", {"properties": {"api_key": {"type": "string"}}}),
        (
            "Continue",
            {"properties": {"value": {"title": "Access token", "type": "string"}}},
        ),
        (
            "Continue",
            {
                "properties": {
                    "value": {"description": "Upload the secret file", "type": "string"}
                }
            },
        ),
    ],
)
def test_human_form_safety_rejects_sensitive_message_and_schema_metadata(
    message: str, schema: dict[str, Any]
) -> None:
    safe, reason = human_form_safety(message, schema)
    assert safe is False
    assert "trusted URL" in str(reason)


def test_human_form_safety_allows_non_sensitive_decision() -> None:
    safe, reason = human_form_safety(
        "Choose a deployment region",
        {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "title": "Region",
                    "description": "Select one approved region",
                }
            },
        },
    )
    assert safe is True
    assert reason is None
