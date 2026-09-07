from __future__ import annotations

from typing import Any

import pytest

from pursers_client import (
    BoardClient,
    HUMAN_INPUT_REQUESTED,
    HUMAN_INPUT_RESOLVED,
    KNOWN_EVENT_KINDS,
    TICKET_ARCHIVED,
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
    assert TICKET_ARCHIVED in KNOWN_EVENT_KINDS


@pytest.mark.parametrize(
    ("property_name", "title"),
    [
        ("password", "Password"),
        ("api_key", "API key"),
        ("apiKey", "API key"),
        ("APIKey", "API key"),
        ("accessToken", "Access token"),
        ("clientSecret", "Client secret"),
        ("paymentCredentials", "Payment credentials"),
        ("token", "Access token"),
        ("payment", "Payment credentials"),
    ],
)
def test_human_form_safety_rejects_secret_or_credential_fields(
    property_name: str, title: str
) -> None:
    safe, reason = human_form_safety(
        "Ordinary request text",
        {"properties": {property_name: {"title": title, "type": "string"}}},
    )
    assert safe is False
    assert "trusted URL" in str(reason)


@pytest.mark.parametrize("field", ["username", "email", "name", "file"])
def test_human_form_safety_allows_profile_and_file_fields(field: str) -> None:
    safe, reason = human_form_safety(
        "Export the dataset file; do not paste an API key here",
        {
            "type": "object",
            "properties": {
                field: {
                    "type": "string",
                    "title": field.title(),
                    "description": "Ordinary profile or deliverable field",
                }
            },
        },
    )
    assert safe is True
    assert reason is None
