from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

FLEET_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(FLEET_DIR))
AG_UI_SPEC = importlib.util.spec_from_file_location(
    "fleet_dashboard_ag_ui", FLEET_DIR / "ag_ui.py"
)
assert AG_UI_SPEC and AG_UI_SPEC.loader
ag_ui = importlib.util.module_from_spec(AG_UI_SPEC)
sys.modules[AG_UI_SPEC.name] = ag_ui
AG_UI_SPEC.loader.exec_module(ag_ui)
DASHBOARD_SPEC = importlib.util.spec_from_file_location(
    "fleet_dashboard_ag_ui_http", FLEET_DIR / "fleet_dashboard.py"
)
assert DASHBOARD_SPEC and DASHBOARD_SPEC.loader

dashboard = importlib.util.module_from_spec(DASHBOARD_SPEC)
sys.modules[DASHBOARD_SPEC.name] = dashboard
DASHBOARD_SPEC.loader.exec_module(dashboard)


HELD_DRAFT = {
    "id": "TK-held-draft",
    "title": "<script>alert('draft')</script>",
    "status": "closed",
    "status_label": "Approved · delivery pending",
    "priority": "medium",
    "progress_freshness": "stale",
    "activity": {
        "schema_version": 1,
        "stage": "delivery",
        "state": "waiting",
        "attempt_id": 1,
        "actor_id": "reviewer",
        "updated_at": "2026-10-27T00:00:00Z",
        "freshness": "stale",
        "evidence_refs": ["draft"],
        "next_action": "Complete final QA",
        "blocking_reason": "QA held; security pending",
        "completion_boundary": "delivery",
    },
    "result": {
        "state": "approved",
        "review": {"verdict": "approve", "independent": True},
    },
    "delivery": {"state": "pr_pending", "pr_id": None, "url": None},
    # Deliberate non-allow-listed data must never cross the observation boundary.
    "submission_evidence": {"test_output": "DUMMY_SECRET_TOKEN"},
    "annotations": [{"text": "DUMMY_SECRET_TOKEN"}],
}


def detail(*tickets: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "board": {"board_id": "demo-board", "label": "Demo board"},
        "tickets": list(tickets),
        "ticket_total": len(tickets),
        "ticket_returned": len(tickets),
        "ticket_omitted": 0,
        "truncated": False,
        "snapshot_truncation": {"snapshot": False},
        "event_window_truncated": False,
        "event_resync_required": False,
        "coordinator_findings_stale": True,
    }
    result.update(overrides)
    return result


class Cache:
    default_central = "local"

    def __init__(self, boards: dict[tuple[str, str], dict[str, Any]] | None = None) -> None:
        self.boards = boards or {("local", "demo-board"): detail(HELD_DRAFT)}
        self.calls: list[tuple[str, str]] = []

    def labels(self) -> list[str]:
        return ["local"]

    def resolve_central(self, central: str | None = None) -> str:
        value = central or "local"
        if value != "local":
            raise KeyError(value)
        return value

    def get_board(self, board_id: str, central: str | None = None) -> dict[str, Any]:
        key = (central or "local", board_id)
        self.calls.append(key)
        if key not in self.boards:
            raise KeyError(board_id)
        return self.boards[key]

    def get_cached_board(
        self, board_id: str, central: str | None = None
    ) -> dict[str, Any] | None:
        key = (central or "local", board_id)
        self.calls.append(key)
        value = self.boards.get(key)
        if value is None:
            return None
        return {"value": value, "age_seconds": 0.0, "fresh": True}


@pytest.fixture
def server() -> Any:
    cache = Cache()
    httpd = dashboard.FleetDashboardHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", httpd, cache
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def post_observe(
    base: str,
    payload: Any,
    *,
    path: str = "/api/ag-ui/observe?central=local&board_id=demo-board",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    request = urllib.request.Request(
        base + path,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def decode_sse(body: bytes) -> list[dict[str, Any]]:
    events = []
    for frame in body.decode("utf-8").strip().split("\n\n"):
        lines = [line for line in frame.splitlines() if line.startswith("data: ")]
        assert len(lines) == 1
        events.append(json.loads(lines[0][6:]))
    return events


def valid_input(**overrides: Any) -> dict[str, Any]:
    return {"threadId": "thread-1", "runId": "run-1", "messages": [], **overrides}


def test_events_have_canonical_shapes_order_and_preserve_held_delivery_state(server: Any) -> None:
    base, _httpd, cache = server
    status, headers, body = post_observe(base, valid_input())

    assert status == 200
    assert headers["Content-Type"].startswith("text/event-stream")
    assert headers["Content-Length"] == str(len(body))
    events = decode_sse(body)
    assert [event["type"] for event in events] == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "ACTIVITY_SNAPSHOT",
        "RUN_FINISHED",
    ]
    assert set(events[0]) == {"type", "threadId", "runId", "protocolVersion"}
    assert events[0] == {
        "type": "RUN_STARTED",
        "threadId": "thread-1",
        "runId": "run-1",
        "protocolVersion": "1.0",
    }
    assert set(events[1]) == {"type", "snapshot"}
    state = events[1]["snapshot"]
    assert state["scope"] == {"central": "local", "board_id": "demo-board"}
    ticket = state["tickets"][0]
    assert ticket["delivery"] == {"state": "pr_pending", "pr_id": None}
    assert ticket["result"] == {
        "state": "approved",
        "review": {"verdict": "approve", "independent": True},
    }
    assert ticket["activity"]["freshness"] == "stale"
    assert ticket["activity"]["blocking_reason"] == "QA held; security pending"
    assert state["coverage"]["coordinator_findings_stale"] is True
    assert set(events[2]) == {"type", "messageId", "activityType", "content"}
    assert events[2] == {
        "type": "ACTIVITY_SNAPSHOT",
        "messageId": "run-1:activity",
        "activityType": "pursers.board.activity",
        "content": {
            "scope": {"central": "local", "board_id": "demo-board"},
            "activities": [
                {"ticket_id": "TK-held-draft", "activity": ticket["activity"]}
            ],
        },
    }
    assert events[3] == {
        "type": "RUN_FINISHED",
        "threadId": "thread-1",
        "runId": "run-1",
    }
    assert cache.calls == [("local", "demo-board")]
    assert "DUMMY_SECRET_TOKEN" not in body.decode()
    assert "submission_evidence" not in body.decode()
    assert "<script>" not in body.decode()
    # JSON escaping preserves the untrusted text as text; it is not executable markup.
    assert "\\u003cscript\\u003e" in body.decode()


def test_zero_tickets_is_distinct_from_missing_coverage(server: Any) -> None:
    base, _httpd, cache = server
    cache.boards[("local", "demo-board")] = detail()
    status, _headers, body = post_observe(base, valid_input())
    assert status == 200
    state = decode_sse(body)[1]["snapshot"]
    assert state["coverage"]["ticket_total"] == 0
    assert state["tickets"] == []

    cache.boards[("local", "demo-board")] = {"tickets": []}
    status, _headers, body = post_observe(base, valid_input())
    assert status == 200
    state = decode_sse(body)[1]["snapshot"]
    assert state["coverage"]["ticket_total"] is None


def test_scope_selectors_unknown_board_and_capabilities(server: Any) -> None:
    base, _httpd, cache = server
    with urllib.request.urlopen(base + "/api/ag-ui/capabilities", timeout=3) as response:
        capabilities = json.loads(response.read())
        assert response.headers["Content-Type"].startswith("application/json")
    assert capabilities["protocolVersion"] == "1.0"
    assert capabilities["observe"] is True
    assert capabilities["mutations"] is False
    assert capabilities["run"] is False
    assert capabilities["resume"] is False

    status, _headers, body = post_observe(
        base, valid_input(), path="/api/ag-ui/observe?central=local&board_id=missing"
    )
    assert status == 503
    assert json.loads(body)["error"] == "observation cache unavailable"
    assert cache.calls == [("local", "missing")]

    status, _headers, body = post_observe(
        base, valid_input(), path="/api/ag-ui/observe?board_id=demo-board"
    )
    assert status == 200
    assert decode_sse(body)[1]["snapshot"]["scope"]["central"] == "local"
    assert (
        post_observe(
            base,
            valid_input(),
            path="/api/ag-ui/observe?central=local&board_id=bad%2Fid",
        )[0]
        == 400
    )

    status, _headers, body = post_observe(
        base,
        valid_input(),
        path="/api/ag-ui/observe?central=unknown&board_id=demo-board",
    )
    assert status == 404
    assert json.loads(body)["error"] == "central not found"
    assert cache.calls == [("local", "missing"), ("local", "demo-board")]


@pytest.mark.parametrize(
    "payload",
    [
        {"threadId": "", "runId": "run-1", "messages": []},
        {"threadId": "thread-1", "runId": "bad\nrun", "messages": []},
        {
            "threadId": "thread-1",
            "runId": "run-1",
            "messages": [{"role": "user", "content": "do work"}],
        },
        {"threadId": "thread-1", "runId": "run-1", "messages": [], "tools": [{"name": "execute"}]},
        {"threadId": "thread-1", "runId": "run-1", "messages": [], "state": {"action": "claim"}},
        {"threadId": "thread-1", "runId": "run-1", "messages": [], "resume": []},
        {"threadId": "thread-1", "runId": "run-1", "messages": [], "extra": "unsupported"},
    ],
)
def test_invalid_or_action_bearing_input_rejects_before_cache_and_sse(
    server: Any, payload: dict[str, Any]
) -> None:
    base, _httpd, cache = server
    status, headers, body = post_observe(base, payload)
    assert status == 400
    assert headers["Content-Type"].startswith("application/json")
    assert b"text/event-stream" not in headers["Content-Type"].encode()
    assert json.loads(body)["error"]
    assert cache.calls == []


def test_oversized_input_and_bad_origin_reject_before_cache(server: Any) -> None:
    base, _httpd, cache = server
    status, _headers, _body = post_observe(base, b"x" * (ag_ui.MAX_INPUT_BYTES + 1))
    assert status == 400
    status, _headers, body = post_observe(
        base, valid_input(), headers={"Origin": "http://example.invalid"}
    )
    assert status == 403
    assert json.loads(body)["error"] == "same-origin request required"
    assert cache.calls == []


def test_non_loopback_client_is_rejected_before_origin_or_cache(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, _httpd, cache = server
    monkeypatch.setattr(
        dashboard.ipaddress,
        "ip_address",
        lambda _value: type("NonLoopback", (), {"is_loopback": False})(),
    )
    status, _headers, body = post_observe(base, valid_input())
    assert status == 403
    assert json.loads(body)["error"] == "loopback required"
    assert cache.calls == []


def test_event_response_is_finite_and_listener_slot_is_released(server: Any) -> None:
    base, httpd, cache = server
    for _ in range(40):
        status, _headers, _body = post_observe(base, valid_input())
        assert status == 200
    assert httpd.max_active_requests == 32
    assert len(cache.calls) == 40
    # A fresh request after >32 finite SSE runs proves every handler returned.
    with urllib.request.urlopen(base + "/api/ag-ui/capabilities", timeout=2) as response:
        assert response.status == 200


def test_dashboard_cache_peek_never_runs_loader_or_creates_a_client() -> None:
    calls: list[str] = []

    class Fetcher:
        config = type("Config", (), {"label": "local"})()

        def enable_client_reuse(self) -> None:
            calls.append("reuse")

        async def fetch(self) -> dict[str, Any]:
            calls.append("fleet fetch")
            raise AssertionError("observation must not load Fleet")

        async def fetch_board(self, _board_id: str) -> dict[str, Any]:
            calls.append("board fetch / client join")
            raise AssertionError("observation must not construct a Central client")

    cache = dashboard.DashboardCache(Fetcher(), ttl_seconds=60)
    detail_cache = dashboard.TimedCache(
        60,
        lambda: calls.append("detail loader") or None,
        runner=lambda _awaitable: calls.append("runner") or None,
    )
    detail_cache.prime(detail(HELD_DRAFT), fresh_for=60)
    cache._details[("local", "demo-board")] = detail_cache
    httpd = dashboard.FleetDashboardHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, body = post_observe(
            f"http://127.0.0.1:{httpd.server_port}", valid_input()
        )
        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        state = decode_sse(body)[1]["snapshot"]
        assert state["tickets"][0]["id"] == "TK-held-draft"
        assert state["cache"]["fresh"] is True
        cache._details.pop(("local", "demo-board"))
        status, headers, body = post_observe(
            f"http://127.0.0.1:{httpd.server_port}", valid_input()
        )
        assert status == 503
        assert headers["Content-Type"].startswith("application/json")
        assert json.loads(body)["error"] == "observation cache unavailable"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
    assert calls == ["reuse"]


def test_warm_fleet_summary_is_fallback_with_unknown_total_and_source_coverage() -> None:
    calls: list[str] = []

    class Fetcher:
        config = type("Config", (), {"label": "local"})()

        def enable_client_reuse(self) -> None:
            calls.append("reuse")

        async def fetch(self) -> dict[str, Any]:
            calls.append("fleet fetch")
            raise AssertionError("observation must not load Fleet")

        async def fetch_board(self, _board_id: str) -> dict[str, Any]:
            calls.append("board fetch / client join")
            raise AssertionError("observation must not construct a Central client")

    cache = dashboard.DashboardCache(Fetcher(), ttl_seconds=60)
    summary_ticket = {
        key: HELD_DRAFT[key]
        for key in (
            "id",
            "title",
            "status",
            "status_label",
            "delivery",
            "progress_freshness",
            "activity",
        )
    }
    cache._fleets["local"].prime(
        {
            "boards": [
                {
                    "board_id": "demo-board",
                    "label": "Demo board",
                    "status": "ready",
                    "tickets": [summary_ticket],
                    "truncated": True,
                    "snapshot_truncation": {"tickets": 3},
                    "coverage": {"summary": "stale", "events": "unknown"},
                    "coordinator_findings_stale": True,
                }
            ]
        },
        fresh_for=60,
    )
    httpd = dashboard.FleetDashboardHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, body = post_observe(
            f"http://127.0.0.1:{httpd.server_port}", valid_input()
        )
        assert status == 200
        state = decode_sse(body)[1]["snapshot"]
        assert state["cache"]["source"] == "fleet_summary"
        assert state["coverage"]["ticket_total"] is None
        assert state["coverage"]["source"] == {
            "summary": "stale",
            "events": "unknown",
        }
        assert state["coverage"]["snapshot_truncation"] == {"tickets": 3}
        assert state["coverage"]["truncated"] is True
        assert state["tickets"][0]["delivery"]["state"] == "pr_pending"
        assert state["tickets"][0]["result"] is None
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
    assert calls == ["reuse"]


def test_cold_dashboard_cache_rejects_without_joining(server: Any) -> None:
    base, _httpd, cache = server
    del cache.boards[("local", "demo-board")]
    status, headers, body = post_observe(base, valid_input())
    assert status == 503
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body)["error"] == "observation cache unavailable"


def test_small_pure_projection_and_encoding_are_bounded() -> None:
    state = ag_ui.project_observation(
        detail(HELD_DRAFT), central="local", board_id="demo-board"
    )
    assert state["tickets"][0]["delivery"]["state"] == "pr_pending"
    events = ag_ui.observation_events(state, thread_id="thread-1", run_id="run-1")
    encoded = ag_ui.encode_sse(events)
    assert len(encoded) <= ag_ui.MAX_OUTPUT_BYTES
    assert [event["type"] for event in events] == [
        "RUN_STARTED", "STATE_SNAPSHOT", "ACTIVITY_SNAPSHOT", "RUN_FINISHED"
    ]
