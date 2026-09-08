"""Dashboard tests for the needs_human "Waiting for you" surface (a22)."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
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
    assert row["form_safe"] is True
    assert row["safety_reason"] is None


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
        url="http://127.0.0.1:8766/mcp",
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


def test_aggregate_marks_sensitive_form_for_safe_fallback() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    sensitive = dict(
        HUMAN_RECORD,
        message="Paste the credential file",
        requested_schema={
            "type": "object",
            "properties": {"accessToken": {"type": "string"}},
        },
    )
    result = dashboard.aggregate_fleet(
        [_board_row([sensitive])], stale_seconds=300, now=now
    )
    row = result["boards"][0]["human_requests"][0]
    assert row["form_safe"] is False
    assert "trusted URL" in row["safety_reason"]


def test_aggregate_allows_file_deliverable_form() -> None:
    now = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)
    deliverable = dict(
        HUMAN_RECORD,
        message="Export the dataset file",
        requested_schema={
            "type": "object",
            "properties": {"file": {"type": "string", "title": "Dataset file"}},
        },
    )
    result = dashboard.aggregate_fleet(
        [_board_row([deliverable])], stale_seconds=300, now=now
    )
    assert result["boards"][0]["human_requests"][0]["form_safe"] is True


def _run_human_renderer(program_tail: str) -> str:
    scripts = "\n".join(
        re.findall(r"<script>(.*?)</script>", dashboard.HTML, flags=re.DOTALL | re.IGNORECASE)
    )
    lines = scripts.splitlines()

    def last_source(prefix: str) -> str:
        return [line for line in lines if line.startswith(prefix)][-1]

    program = "\n".join(
        [
            "const esc=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('\\\"','&quot;');",
            "const fmt=value=>String(value);",
            "const ticketHref=()=>'/ticket';",
            last_source("function humanUrlHost("),
            last_source("function humanEnumOptions("),
            last_source("function humanFormField("),
            last_source("function humanFormContent("),
            last_source("function humanRequestCard("),
            program_tail,
        ]
    )
    completed = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_renderer_executes_required_defaults_titles_and_array_enum() -> None:
    output = _run_human_renderer(
        "console.log(JSON.stringify({"
        "primitive:humanFormField('priority',{type:'string',oneOf:[{const:'low',title:'Low title'},{const:'high',title:'High title'}],default:'high'},true),"
        "multi:humanFormField('regions',{type:'array',items:{enum:['eu','us']},default:['eu'],minItems:2},true)"
        "}));"
    )
    rendered = json.loads(output)
    assert "priority *" in rendered["primitive"]
    assert "required" in rendered["primitive"]
    assert 'value="high" selected' in rendered["primitive"]
    assert "High title" in rendered["primitive"]
    assert 'data-human-min-items="2"' in rendered["multi"]
    assert 'value="eu" checked' in rendered["multi"]
    assert 'value="us"' in rendered["multi"]


def test_renderer_executes_sensitive_fallback_without_form_fields() -> None:
    output = _run_human_renderer(
        "console.log(humanRequestCard({central:'default',board:{board_id:'one',label:'One'},h:{ticket_id:'TK-1',request_id:'HR-1',message:'Paste credential',kind:'decision',form_safe:false,safety_reason:'A trusted URL is required.',requested_schema:{properties:{accessToken:{type:'string'}}}}}));"
    )
    assert "trusted URL is required" in output
    assert "human-form" not in output
    assert 'data-human-field="accessToken"' not in output


def test_renderer_executes_required_and_min_items_validation() -> None:
    output = _run_human_renderer(
        "const multi={dataset:{humanField:'regions',humanType:'multi-enum'},value:'eu'};"
        "const multiForm={querySelectorAll:s=>s==='[data-human-field]'?[multi]:[multi],querySelector:s=>({dataset:{humanMinItems:'2'}})};"
        "const required={dataset:{humanField:'region',humanType:'string'},value:'',required:true};"
        "const requiredForm={querySelectorAll:()=>[required],querySelector:()=>null};"
        "for(const [name,form] of [['minItems',multiForm],['required',requiredForm]]){try{humanFormContent(form);console.log(name+':missing-error')}catch(error){console.log(name+':'+error.message)}}"
    )
    assert "minItems:regions requires at least 2 selections" in output
    assert "required:region is required" in output


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
        ("127.0.0.1", 0),
        dashboard.make_handler(cache, worker_manager=SimpleNamespace()),
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
