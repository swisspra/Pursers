from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest


MODULE_PATH = Path(__file__).parents[1] / "public_projection.py"
SPEC = importlib.util.spec_from_file_location("public_projection", MODULE_PATH)
assert SPEC and SPEC.loader
public = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = public
SPEC.loader.exec_module(public)

DASHBOARD_PATH = MODULE_PATH.with_name("fleet_dashboard.py")
DASHBOARD_SPEC = importlib.util.spec_from_file_location(
    "fleet_dashboard_public_test", DASHBOARD_PATH
)
assert DASHBOARD_SPEC and DASHBOARD_SPEC.loader
dashboard = importlib.util.module_from_spec(DASHBOARD_SPEC)
sys.modules[DASHBOARD_SPEC.name] = dashboard
DASHBOARD_SPEC.loader.exec_module(dashboard)

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
CANARIES = {
    "person@example.invalid",
    "/PATH/TO/private/secret",
    "private-host.internal",
    "TK-secret-ticket",
    "AI-secret-agent",
    "PR-secret-principal",
    "sk-live-secret-token",
    "https://central.invalid/private",
    "feature/private-branch",
    "0123456789abcdef0123456789abcdef01234567",
    "sensitive-project-name",
    "provider-private",
    "model-private",
}


def source_with_tickets(count: int, *, state: str = "open") -> dict[str, Any]:
    tickets = []
    for index in range(count):
        tickets.append(
            {
                "id": f"TK-secret-ticket-{index}",
                "title": "sensitive-project-name",
                "status": state,
                "updated_at": (NOW - timedelta(hours=2)).isoformat(),
                "unknown": {
                    "email": "person@example.invalid",
                    "path": "/PATH/TO/private/secret",
                    "token": "sk-live-secret-token",
                },
            }
        )
    return {
        "generated_at": (NOW - timedelta(minutes=10)).isoformat(),
        "unknown_root": {"url": "https://central.invalid/private"},
        "boards": [
            {
                "board_id": "sensitive-project-name",
                "label": "private-host.internal",
                "status": "ready",
                "tickets": tickets,
                "events": [],
                "human_requests": [],
                "unknown_board": "feature/private-branch",
            }
        ],
        "agents": [],
    }


def rich_source() -> dict[str, Any]:
    source = source_with_tickets(0)
    board = source["boards"][0]
    statuses = ["open"] * 5 + ["claimed"] * 5 + ["submitted"] * 5 + ["closed"] * 5
    board["tickets"] = [
        {
            "id": f"TK-secret-ticket-{index}",
            "title": "sensitive-project-name",
            "status": status,
            "updated_at": (NOW - timedelta(hours=2 + index)).isoformat(),
            "description": "person@example.invalid /PATH/TO/private/secret",
            "principal_id": "PR-secret-principal",
            "agent_id": "AI-secret-agent",
            "branch": "feature/private-branch",
            "commit": "0123456789abcdef0123456789abcdef01234567",
        }
        for index, status in enumerate(statuses)
    ]
    board["events"] = [
        {
            "kind": "ticket_closed",
            "ticket_id": f"TK-secret-ticket-{index}",
            "occurred_at": (NOW - timedelta(hours=2)).isoformat(),
            "actor": "person@example.invalid",
        }
        for index in range(5)
    ]
    source["agents"] = [
        {
            "agent_id": f"AI-secret-agent-{index}",
            "principal_id": "PR-secret-principal",
            "agent_name": "person@example.invalid",
            "pool_status": "available",
            "provider": "provider-private",
            "model": "model-private",
            "seats": [{"role": "worker", "board_id": "sensitive-project-name"}],
        }
        for index in range(5)
    ]
    return source


def project(source: dict[str, Any]) -> dict[str, Any]:
    return public.project_public_snapshot(source, b"k" * 32, now=NOW)


def serialized(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def assert_no_canaries(value: Any) -> None:
    text = serialized(value)
    for canary in CANARIES:
        assert canary not in text


def test_projection_is_closed_bucketed_and_canary_free() -> None:
    projection = project(rich_source())
    summary = projection["summary"]

    assert set(summary) == {
        "schema_version",
        "mode",
        "release",
        "freshness",
        "health",
        "projects",
        "fleet",
        "approvals",
        "activity",
        "suppressed",
    }
    assert summary["projects"] == [
        {
            "alias": public.public_alias(
                b"k" * 32, "project", "sensitive-project-name"
            ),
            "health": "active",
            "workload": "several",
            "work": {"queued": "few", "active": "few", "review": "few"},
        }
    ]
    assert summary["fleet"] == {
        "active_agents": "few",
        "roles": [{"role": "worker", "count": "few"}],
    }
    assert summary["approvals"] == {
        "awaiting_review": "few",
        "awaiting_human": "none",
    }
    assert summary["activity"] == [
        {"window": "today", "transition": "completed", "count": "few"}
    ]
    public.validate_public_document(summary)
    assert_no_canaries(projection)


def test_projection_consumes_product_aggregate_without_leaking_source_fields() -> None:
    tickets = [
        {
            "ticket_id": f"TK-secret-ticket-{index}",
            "title": "sensitive-project-name",
            "status": "open",
            "updated_at": (NOW - timedelta(hours=2)).isoformat(),
            "description": "person@example.invalid /PATH/TO/private/secret",
        }
        for index in range(5)
    ]
    agents = [
        {
            "agent_id": f"AI-secret-agent-{index}",
            "principal_id": f"PR-secret-principal-{index}",
            "agent_name": f"person-{index}@example.invalid",
            "role": "worker",
            "status": "available",
            "last_activity_at": (NOW - timedelta(minutes=5)).isoformat(),
            "lifecycle_status": "active",
        }
        for index in range(5)
    ]
    fleet = dashboard.aggregate_fleet(
        [
            {
                "board_id": "sensitive-project-name",
                "label": "private-host.internal",
                "snapshot": {"tickets": tickets, "agents": agents},
                "events": [],
                "human_requests": [],
            }
        ],
        stale_seconds=300,
        now=NOW,
    )

    projection = project(fleet)

    assert projection["summary"]["projects"][0]["work"]["queued"] == "few"
    assert projection["summary"]["fleet"] == {
        "active_agents": "few",
        "roles": [{"role": "worker", "count": "few"}],
    }
    assert_no_canaries({"fleet_projection": projection})


@pytest.mark.parametrize(
    ("count", "expected_project", "expected_bucket"),
    [
        (0, False, None),
        (1, False, None),
        (4, False, None),
        (5, True, "few"),
        (9, True, "few"),
        (10, True, "several"),
        (24, True, "several"),
        (25, True, "many"),
    ],
)
def test_suppression_and_bucket_boundaries(
    count: int, expected_project: bool, expected_bucket: str | None
) -> None:
    summary = project(source_with_tickets(count))["summary"]
    assert bool(summary["projects"]) is expected_project
    if expected_project:
        assert summary["projects"][0]["work"]["queued"] == expected_bucket
        assert summary["projects"][0]["workload"] == expected_bucket
    assert summary["suppressed"] is (0 < count < 5)


def test_aliases_are_stable_typed_and_key_scoped() -> None:
    first = public.public_alias(b"a" * 32, "project", "private-id")
    assert first == public.public_alias(b"a" * 32, "project", "private-id")
    assert first != public.public_alias(b"b" * 32, "project", "private-id")
    assert first != public.public_alias(b"a" * 32, "work", "private-id")
    assert "private-id" not in first
    assert public.ALIAS_RE.fullmatch(first)


def test_activity_delay_uses_stable_zero_to_five_minute_jitter() -> None:
    source = rich_source()
    event = {"kind": "ticket_closed"}
    jitter = public._activity_jitter_minutes(b"k" * 32, "completed", event)
    assert 0 <= jitter <= 5
    source["boards"][0]["events"] = [
        {
            **event,
            "occurred_at": (
                NOW - timedelta(minutes=public.ACTIVITY_DELAY_MINUTES + jitter - 1)
            ).isoformat(),
        }
        for _index in range(5)
    ]
    assert project(source)["summary"]["activity"] == []
    for row in source["boards"][0]["events"]:
        row["occurred_at"] = (
            NOW - timedelta(minutes=public.ACTIVITY_DELAY_MINUTES + jitter)
        ).isoformat()
    assert project(source)["summary"]["activity"] == [
        {"window": "recent", "transition": "completed", "count": "few"}
    ]


def test_collision_suppresses_both_project_rows() -> None:
    source = rich_source()
    second = dict(source["boards"][0])
    second["board_id"] = "another-private-board"
    source["boards"].append(second)

    def collision(_key: bytes, entity: str, _identifier: str) -> str:
        return f"{entity}-aaaaaaaaaaaaaaaa"

    projection = public.project_public_snapshot(
        source, b"k" * 32, now=NOW, aliaser=collision
    )
    assert projection["summary"]["projects"] == []
    assert projection["summary"]["suppressed"] is True


def test_duplicate_active_agent_rows_do_not_satisfy_privacy_cohort() -> None:
    source = rich_source()
    duplicate = dict(source["agents"][0])
    source["agents"] = [dict(duplicate) for _index in range(5)]

    projection = project(source)

    assert projection["summary"]["fleet"] == {
        "active_agents": "none",
        "roles": [],
    }
    assert projection["summary"]["suppressed"] is True


def test_active_agent_without_immutable_id_is_suppressed() -> None:
    source = rich_source()
    source["agents"][0].pop("agent_id")

    projection = project(source)

    assert projection["summary"]["fleet"] == {
        "active_agents": "none",
        "roles": [],
    }
    assert projection["summary"]["suppressed"] is True


def test_work_alias_collision_across_projects_suppresses_both_rows() -> None:
    source = source_with_tickets(5)
    second = json.loads(json.dumps(source["boards"][0]))
    second["board_id"] = "another-private-board"
    for index, ticket in enumerate(second["tickets"]):
        ticket["id"] = f"TK-another-secret-{index}"
    source["boards"].append(second)
    colliding_ids = {"TK-secret-ticket-0", "TK-another-secret-0"}
    collision_alias = "work-aaaaaaaaaaaaaaaa"

    def cross_project_collision(
        key: bytes, entity: str, identifier: str
    ) -> str:
        if entity == "work" and identifier in colliding_ids:
            return collision_alias
        return public.public_alias(key, entity, identifier)

    projection = public.project_public_snapshot(
        source, b"k" * 32, now=NOW, aliaser=cross_project_collision
    )

    work_items = [
        item
        for detail in projection["projects"].values()
        for item in detail["work_items"]
    ]
    assert len(projection["projects"]) == 2
    assert len(work_items) == 8
    assert all(item["alias"] != collision_alias for item in work_items)
    assert projection["summary"]["suppressed"] is True


def test_duplicate_source_project_is_suppressed() -> None:
    source = rich_source()
    source["boards"].append(dict(source["boards"][0]))

    projection = project(source)

    assert projection["summary"]["projects"] == []
    assert projection["summary"]["suppressed"] is True


def test_alias_key_is_private_and_rejects_wrong_permissions(tmp_path: Path) -> None:
    key_path = tmp_path / "state" / "alias.key"
    assert len(public.load_or_create_alias_key(key_path)) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    key_path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        public.load_or_create_alias_key(key_path)


def request(
    root: str, path: str, *, method: str = "GET", body: bytes | None = None
) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(root + path, data=body, method=method)
    try:
        response = urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)
    with response:
        return response.status, response.read(), dict(response.headers)


def test_public_http_surface_is_read_only_and_private_routes_are_constant_404() -> None:
    projection = project(rich_source())
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), public.make_public_handler(projection, clock=lambda: NOW)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    private_routes = [
        "/",
        "/api/fleet",
        "/api/board/private-board",
        "/api/config",
        "/api/workers",
        "/api/doors",
        "/api/butler",
        "/api/intake",
        "/api/dispatch",
        "/api/projects/add",
        "/settings",
    ]
    try:
        status, body, headers = request(root, "/api/public/v1/summary")
        assert status == 200
        assert json.loads(body) == projection["summary"]
        assert headers["Cache-Control"] == "private, max-age=15, stale-if-error=60"
        assert headers["Vary"] == "Authorization"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert "'unsafe-inline'" not in headers["Content-Security-Policy"]
        assert_no_canaries({"body": body.decode(), "headers": headers})

        statuses = {request(root, route)[0:2] for route in private_routes}
        assert statuses == {(404, public.PUBLIC_NOT_FOUND)}
        for route in private_routes:
            status, body, _headers = request(root, route, method="HEAD")
            assert status == 404
            assert body == b""

        intake_payloads = [
            b'{"board_id":"private","text":"secret"}',
            b'{"board_id":"private","ask_id":"a","action":"approve",'
            b'"expected_sha256":"secret"}',
            b"not-json",
        ]
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            payloads = intake_payloads if method == "POST" else [b"private"]
            for payload in payloads:
                status, body, _headers = request(
                    root, "/api/intake", method=method, body=payload
                )
                assert (status, body) == (405, public.PUBLIC_METHOD_NOT_ALLOWED)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_public_handler_freezes_preview_bytes_and_fails_closed_after_ttl() -> None:
    projection = project(rich_source())
    handler = public.make_public_handler(projection, clock=lambda: NOW)
    alias = projection["summary"]["projects"][0]["alias"]
    projection["projects"][alias]["project"]["health"] = "degraded"
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    try:
        status, body, _headers = request(
            root, f"/api/public/v1/projects/{alias}"
        )
        assert status == 200
        assert json.loads(body)["project"]["health"] == "active"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    expired = public.make_public_handler(
        project(rich_source()),
        clock=lambda: NOW + timedelta(hours=2),
    )
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), expired)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    try:
        status, body, _headers = request(root, "/api/public/v1/summary")
        assert (status, body) == (503, public.PUBLIC_UNAVAILABLE)
        status, body, _headers = request(root, "/api/config")
        assert (status, body) == (404, public.PUBLIC_NOT_FOUND)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_project_detail_schema_is_closed() -> None:
    projection = project(rich_source())
    alias = projection["summary"]["projects"][0]["alias"]
    projection["projects"][alias]["work_items"][0]["private"] = "secret"
    with pytest.raises(ValueError, match="closed schema"):
        public.make_public_handler(projection, clock=lambda: NOW)


def test_public_html_and_assets_do_not_ship_private_ui_or_clipboard() -> None:
    assets = public.PUBLIC_HTML + public.PUBLIC_CSS + public.PUBLIC_JS
    lowered = assets.lower()
    assert b"settings" not in lowered
    assert b"clipboard" not in lowered
    assert b"/api/fleet" not in lowered
    assert b"/api/board/" not in lowered
    assert b"/api/config" not in lowered
    assert b"<script>" not in lowered
    for view in (b"home", b"projects", b"work", b"team", b"approvals", b"activity"):
        assert b"view==='" + view + b"'" in lowered or b'href="#' + view in lowered
    for canary in CANARIES:
        assert canary.encode() not in assets


def test_public_check_uses_projection_without_loading_central_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source_path = tmp_path / "snapshot.json"
    source_path.write_text(json.dumps(rich_source()), encoding="utf-8")
    source_path.chmod(0o600)
    key_path = tmp_path / "alias.key"
    monkeypatch.delenv("ONBOARD_CENTRAL_TOKEN", raising=False)

    dashboard.main(
        [
            "--mode",
            "public",
            "--public-input",
            str(source_path),
            "--public-alias-key",
            str(key_path),
            "--public-check",
        ]
    )

    output = capsys.readouterr().out.strip()
    digest = output.removeprefix("Fleet public projection: ")
    assert len(digest) == 64
    int(digest, 16)
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert dashboard.parse_args([]).mode == "private"
    assert hashlib.sha256(bytes.fromhex(digest)).hexdigest() != digest


def test_public_mode_refuses_central_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "private")
    with pytest.raises(SystemExit):
        dashboard.parse_args(
            [
                "--mode",
                "public",
                "--public-input",
                "/PATH/TO/private/snapshot.json",
                "--public-alias-key",
                "/PATH/TO/private/alias.key",
            ]
        )


def test_public_preview_allows_random_port_without_changing_private_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONBOARD_CENTRAL_TOKEN", raising=False)
    public_args = dashboard.parse_args(
        [
            "--mode",
            "public",
            "--port",
            "0",
            "--public-input",
            "/PATH/TO/private/snapshot.json",
            "--public-alias-key",
            "/PATH/TO/private/alias.key",
        ]
    )
    assert public_args.port == 0
    with pytest.raises(SystemExit):
        dashboard.parse_args(["--port", "0"])


@pytest.mark.parametrize(
    ("private_state", "public_state"),
    [
        ("assigned", "queued"),
        ("creating_report", "active"),
        ("in_review", "review"),
    ],
)
def test_current_private_states_are_coarsened(
    private_state: str, public_state: str
) -> None:
    projection = project(source_with_tickets(5, state=private_state))

    alias = projection["summary"]["projects"][0]["alias"]
    assert projection["projects"][alias]["work_items"][0]["state"] == public_state
