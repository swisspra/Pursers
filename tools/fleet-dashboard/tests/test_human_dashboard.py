"""Dashboard tests for the needs_human "Waiting for you" surface (a22)."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[2]
CLIENT_SRC = REPOSITORY / "packages" / "client" / "src"
sys.path.insert(0, str(CLIENT_SRC))
sys.path.insert(0, str(ROOT))

import fleet_dashboard as dashboard  # noqa: E402


HUMAN_RECORD = {
    "ticket_id": "TK-human",
    "request_id": "REQ-human",
    "message": "Approve the export",
    "kind": "approval",
    "asked_by": "worker-x",
    "asked_at": "2026-09-06T09:00:00+00:00",
    "requested_schema": {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    },
    "url": None,
}


def _board_row(human_requests: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "label": "One",
        "board_id": "board-one",
        "snapshot": {
            "board": {"stale_after_days": 7},
            "agents": [],
            "tickets": [],
        },
        "events": [],
        "human_requests": human_requests,
    }


def test_aggregate_fleet_projects_human_requests_bounded() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    rows = [_board_row([dict(HUMAN_RECORD)] * 15)]
    result = dashboard.aggregate_fleet(rows, stale_seconds=300, now=now)
    board = result["boards"][0]
    assert len(board["human_requests"]) == 10
    row = board["human_requests"][0]
    assert row["ticket_id"] == "TK-human"
    assert row["request_id"] == "REQ-human"
    assert row["kind"] == "approval"
    assert row["requested_schema"]["properties"]["answer"] == {"type": "string"}


def test_aggregate_fleet_board_without_human_requests_is_empty() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    rows = [_board_row([])]
    result = dashboard.aggregate_fleet(rows, stale_seconds=300, now=now)
    assert result["boards"][0]["human_requests"] == []


def test_pending_human_records_filters_resolved_and_invalid() -> None:
    resolved = dict(HUMAN_RECORD, resolution={"action": "accept"})
    missing_id = {k: v for k, v in HUMAN_RECORD.items() if k != "request_id"}
    assert dashboard._pending_human_records(resolved) == []
    assert dashboard._pending_human_records(missing_id) == []
    assert dashboard._pending_human_records(HUMAN_RECORD) == [HUMAN_RECORD]
    assert dashboard._pending_human_records([HUMAN_RECORD, resolved]) == [HUMAN_RECORD]


class FakeHumanClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "FakeHumanClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def ticket_human_resolve(
        self,
        ticket_id: str,
        *,
        request_id: str,
        action: str,
        content: dict[str, Any] | None = None,
        note: str | None = None,
        disposition: str = "reopen",
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "ticket_human_resolve",
                {
                    "ticket_id": ticket_id,
                    "request_id": request_id,
                    "action": action,
                    "content": content,
                    "note": note,
                    "disposition": disposition,
                },
            )
        )
        return {"ok": True, "ticket_id": ticket_id}


def _fetcher(client: FakeHumanClient) -> dashboard.FleetFetcher:
    config = SimpleNamespace(
        url="https://127.0.0.1:8766/mcp",
        token="TOKEN",
        home_board="pursers",
        agent_name="fleet-dashboard",
        label="default",
        overhead_path=None,
        stale_seconds=300,
    )
    return dashboard.FleetFetcher(config, client_factory=lambda *a, **k: client)


def test_resolve_human_request_success_defaults_disposition() -> None:
    client = FakeHumanClient()
    fetcher = _fetcher(client)
    result = asyncio.run(
        fetcher.resolve_human_request(
            "board-one",
            {
                "board_id": "board-one",
                "ticket_id": "TK-human",
                "request_id": "REQ-human",
                "action": "accept",
                "content": {"answer": "yes"},
            },
        )
    )
    assert result["ok"] is True
    call = client.calls[0][1]
    assert call["action"] == "accept"
    assert call["disposition"] == "reopen"
    assert call["content"] == {"answer": "yes"}


def test_resolve_human_request_decline_defaults_park_and_validates() -> None:
    client = FakeHumanClient()
    fetcher = _fetcher(client)
    result = asyncio.run(
        fetcher.resolve_human_request(
            "board-one",
            {
                "board_id": "board-one",
                "ticket_id": "TK-human",
                "request_id": "REQ-human",
                "action": "decline",
            },
        )
    )
    assert result["ok"] is True
    assert client.calls[0][1]["disposition"] == "park"

    with pytest.raises(ValueError):
        asyncio.run(
            fetcher.resolve_human_request(
                "board-one",
                {
                    "board_id": "board-one",
                    "ticket_id": "TK-human",
                    "request_id": "REQ-human",
                    "action": "explode",
                },
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            fetcher.resolve_human_request(
                "bad board!",
                {
                    "board_id": "bad board!",
                    "ticket_id": "TK-human",
                    "request_id": "REQ-human",
                    "action": "accept",
                },
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            fetcher.resolve_human_request(
                "board-one",
                {
                    "board_id": "board-one",
                    "ticket_id": "TK-human",
                    "request_id": "REQ-human",
                    "action": "accept",
                    "content": "not-an-object",
                },
            )
        )


def test_html_contains_waiting_for_you_surface() -> None:
    assert "Waiting for you" in dashboard.HTML
    assert "human_requests" in dashboard.HTML
    assert "/api/human/resolve" in dashboard.HTML
    assert "data-human-disposition" in dashboard.HTML
    assert 'target="_blank"' in dashboard.HTML


class Cache:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def labels(self) -> list[str]:
        return ["default"]

    def resolve_central(self, central: str | None = None) -> str:
        return "default"

    def resolve_human_request(
        self, board_id: str, payload: dict[str, Any], central: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("resolve_human_request", dict(payload)))
        if payload.get("action") not in dashboard.HUMAN_ACTION_VALUES:
            raise ValueError("action must be accept, decline, or cancel")
        return {"ok": True, "central": "default", "board_id": board_id}


def test_human_resolve_endpoint_guard_and_validation() -> None:
    cache = Cache()
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path: str, payload: object, headers: dict | None = None):
        req_headers = {"Content-Type": "application/json", "Origin": base}
        if headers:
            req_headers.update(headers)
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode(),
            headers=req_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, {}

    valid = {
        "board_id": "board-one",
        "ticket_id": "TK-human",
        "request_id": "REQ-human",
        "action": "accept",
        "content": {"answer": "yes"},
        "disposition": "reopen",
    }
    try:
        # same-origin guard: cross-origin rejected
        status, _ = post("/api/human/resolve", valid, {"Origin": "https://attacker.invalid"})
        assert status == 403
        # non-JSON rejected
        status, _ = post("/api/human/resolve", valid, {"Content-Type": "text/plain"})
        assert status == 415
        # valid same-origin request resolves
        status, body = post("/api/human/resolve", valid)
        assert status == 200
        assert body["ok"] is True
        assert cache.calls[-1][1]["ticket_id"] == "TK-human"
        # missing required field rejected
        missing = {k: v for k, v in valid.items() if k != "request_id"}
        status, _ = post("/api/human/resolve", missing)
        assert status == 400
        # unexpected field rejected
        status, _ = post("/api/human/resolve", {**valid, "evil": 1})
        assert status == 400
        # invalid action surfaces as a client error
        status, _ = post("/api/human/resolve", {**valid, "action": "explode"})
        assert status in (400, 503)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
