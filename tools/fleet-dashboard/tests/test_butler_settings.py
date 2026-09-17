from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import signal
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

MODULE_DIR = Path(__file__).parents[1]
REPO_ROOT = MODULE_DIR.parents[1]
sys.path.insert(0, str(MODULE_DIR))

import butler_settings  # noqa: E402

DASHBOARD_SPEC = importlib.util.spec_from_file_location(
    "butler_settings_dashboard", MODULE_DIR / "fleet_dashboard.py"
)
assert DASHBOARD_SPEC and DASHBOARD_SPEC.loader
dashboard = importlib.util.module_from_spec(DASHBOARD_SPEC)
sys.modules[DASHBOARD_SPEC.name] = dashboard
DASHBOARD_SPEC.loader.exec_module(dashboard)

BUTLER_SPEC = importlib.util.spec_from_file_location(
    "butler_settings_runtime", REPO_ROOT / "tools/board-butler/board_butler.py"
)
assert BUTLER_SPEC and BUTLER_SPEC.loader
board_butler = importlib.util.module_from_spec(BUTLER_SPEC)
sys.modules[BUTLER_SPEC.name] = board_butler
BUTLER_SPEC.loader.exec_module(board_butler)


class Response:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.status = status
        self.payload = json.dumps(payload).encode()

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


def provider_request(**overrides: Any) -> dict[str, Any]:
    request = {
        "endpoint": "https://provider.example.invalid/v1",
        "model": "Model/Exact-1",
        "api_key": "",
        "extra_headers": {"X-Tenant": "sandbox"},
        "key_header": "Authorization",
        "key_prefix": "Bearer",
        "validation_path": "models",
        "draft_path": "draft",
        "draft_protocol": "pursers_json_v1",
        "expected_sha256": "a" * 64,
    }
    request.update(overrides)
    return request


def coordinator_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "thresholds": {
            "stale_seconds": 330,
            "grace_seconds": 600,
            "starved_seconds": 1800,
            "critical_starved_seconds": 600,
            "review_backlog_seconds": 1800,
            "lease_warning_ratio": 0.8,
            "abandoner_drops": 3,
            "abandoner_window_days": 7,
        },
        "integration_watch_since": None,
        "intake": {
            "enabled": True,
            "auto_categories": ["docs", "tests", "audit-analysis", "bug"],
            "always_ask_categories": [
                "production-code",
                "release-ci",
                "membership-roles",
                "board-registry",
            ],
            "work_domain_always_ask": True,
            "rate_per_hour": 5,
        },
        "updated_at": "2026-09-17T00:00:00+00:00",
        "updated_by": "fleet-dashboard-session-test",
    }


def configured_payload() -> dict[str, Any]:
    config = coordinator_config()
    config["board_butler"] = {
        "schema_version": 1,
        "global": {
            "drafting": {
                "endpoint_ref": "https://provider.example.invalid/v1",
                "model": "Model/Exact-1",
            }
        },
        "projects": {},
        "boards": {},
    }
    return {"config": config, "expected_sha256": "a" * 64}


def write_runtime(path: Path, *, mode: str, pid: int = 4321) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pid": pid,
                "mode": mode,
                "running": True,
                "started_at": "2026-09-17T00:00:00+00:00",
                "last_activity_at": "2026-09-17T00:01:00+00:00",
                "last_activity": "registry_refresh",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


@pytest.mark.parametrize(
    ("configured", "runtime_mode", "alive", "expected"),
    [
        (False, None, False, "not_configured"),
        (True, None, False, "configured_not_running"),
        (True, "shadow", True, "running_shadow"),
        (True, "active", True, "running_active"),
    ],
)
def test_runtime_indicator_distinguishes_four_states(
    tmp_path: Path,
    configured: bool,
    runtime_mode: str | None,
    alive: bool,
    expected: str,
) -> None:
    runtime = tmp_path / "runtime.json"
    if runtime_mode is not None:
        write_runtime(runtime, mode=runtime_mode)
    manager = butler_settings.ButlerSettingsManager(
        tmp_path / "secrets",
        runtime_path=runtime,
        process_probe=lambda pid: alive and pid == 4321,
    )
    payload = configured_payload() if configured else {
        "config": coordinator_config(),
        "expected_sha256": "a" * 64,
    }

    status = manager.view(payload, "sandbox")["runtime"]

    assert status["state"] == expected
    assert status["running"] is expected.startswith("running_")
    if status["running"]:
        assert status["last_activity"] == "registry_refresh"


def test_stale_runtime_pid_is_never_reported_running(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    write_runtime(runtime, mode="shadow", pid=999_999)
    manager = butler_settings.ButlerSettingsManager(
        tmp_path / "secrets",
        runtime_path=runtime,
        process_probe=lambda _pid: False,
    )

    status = manager.view(configured_payload(), "sandbox")["runtime"]

    assert status["state"] == "configured_not_running"
    assert status["running"] is False
    assert status["pid"] is None


def test_indicator_turns_not_running_after_real_process_exits(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    runtime = tmp_path / "runtime.json"
    write_runtime(runtime, mode="shadow", pid=process.pid)
    manager = butler_settings.ButlerSettingsManager(
        tmp_path / "secrets", runtime_path=runtime
    )
    try:
        assert manager.view(configured_payload(), "sandbox")["runtime"]["state"] == (
            "running_shadow"
        )
    finally:
        process.terminate()
        process.wait(timeout=5)

    status = manager.view(configured_payload(), "sandbox")["runtime"]
    assert status["state"] == "configured_not_running"
    assert status["running"] is False


def test_runtime_projection_drops_unrecognized_text(tmp_path: Path) -> None:
    secret = "sentinel-runtime-secret-3197"
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pid": 4321,
                "mode": "shadow",
                "running": True,
                "started_at": secret,
                "last_activity_at": secret,
                "last_activity": secret,
            }
        ),
        encoding="utf-8",
    )
    runtime.chmod(0o600)
    manager = butler_settings.ButlerSettingsManager(
        tmp_path / "secrets",
        runtime_path=runtime,
        process_probe=lambda _pid: True,
    )

    projected = manager.view(configured_payload(), "sandbox")

    assert projected["runtime"]["state"] == "running_shadow"
    assert secret not in json.dumps(projected)


def test_kill_switch_marks_then_signals_live_resident(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    marker = tmp_path / "KILLED"
    write_runtime(runtime, mode="shadow")
    signals: list[tuple[int, int]] = []
    manager = butler_settings.ButlerSettingsManager(
        tmp_path / "secrets",
        runtime_path=runtime,
        kill_path=marker,
        process_probe=lambda pid: pid == 4321,
        signaler=lambda pid, selected: signals.append((pid, selected)),
        now=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc),
    )

    result = manager.kill(configured_payload(), "sandbox")

    assert result["kill_switch_engaged"] is True
    assert result["signal_sent"] is True
    assert signals == [(4321, signal.SIGTERM)]
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert json.loads(marker.read_text(encoding="utf-8"))["engaged"] is True


@pytest.mark.parametrize(
    ("opener", "outcome"),
    [
        (lambda *_args, **_kwargs: Response({"data": [{"id": "Model/Exact-1"}]}), "reachable"),
        (
            lambda request, **_kwargs: (_ for _ in ()).throw(
                urllib.error.HTTPError(request.full_url, 401, "denied", {}, None)
            ),
            "rejected_credential",
        ),
        (lambda *_args, **_kwargs: Response({"data": [{"id": "other"}]}), "wrong_model"),
        (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                urllib.error.URLError("offline")
            ),
            "unreachable",
        ),
    ],
)
def test_provider_validation_has_four_fixed_outcomes(opener: Any, outcome: str) -> None:
    result = butler_settings.validate_provider(
        provider_request(), "not-returned", opener=opener
    )

    assert result.outcome == outcome
    assert "not-returned" not in json.dumps(result.as_dict())


def test_save_writes_0600_key_and_restart_resolves_new_provider(tmp_path: Path) -> None:
    secret = "".join(("sk", "-restart-only-", "91f25d"))
    seen: dict[str, Any] = {}

    def opener(request: urllib.request.Request, **_kwargs: object) -> Response:
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        return Response({"models": [{"name": "Model/Exact-1"}]})

    payload = {
        "config": coordinator_config(),
        "expected_sha256": "a" * 64,
    }

    def save_config(value: dict[str, Any], expected: str | None) -> dict[str, Any]:
        seen["board_state"] = value
        seen["expected"] = expected
        return {"config": value, "expected_sha256": "b" * 64}

    manager = butler_settings.ButlerSettingsManager(
        tmp_path / "private-keys",
        opener=opener,
        now=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc),
    )
    result = manager.save(
        payload,
        provider_request(api_key=secret),
        "sandbox",
        save_config,
    )

    assert result["saved"] is True
    assert result["reload"] == "next_cycle"
    assert seen["url"] == "https://provider.example.invalid/v1/models"
    assert seen["authorization"] == f"Bearer {secret}"
    assert seen["expected"] == "a" * 64
    assert "updated_at" not in seen["board_state"]
    assert "updated_by" not in seen["board_state"]
    assert secret not in json.dumps(seen["board_state"])
    assert secret not in json.dumps(result)

    assert result["key_location"].startswith("file:")
    key_path = tmp_path / "private-keys" / result["key_location"].removeprefix("file:")
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert key_path.read_text(encoding="utf-8") == secret

    restarted = butler_settings.ButlerSettingsManager(tmp_path / "private-keys")
    restarted_payload = {
        "config": seen["board_state"],
        "expected_sha256": "b" * 64,
    }
    view = restarted.view(restarted_payload, "sandbox")
    effective = board_butler.resolve_config(
        seen["board_state"],
        SimpleNamespace(
            drafts_per_hour=5,
            drafts_per_ticket=2,
            drafts_per_board=20,
            home_board="sandbox-board",
            project=None,
        ),
        {},
        datetime(2026, 9, 17, tzinfo=timezone.utc),
    )

    assert view["endpoint"] == "https://provider.example.invalid/v1"
    assert view["model"] == "Model/Exact-1"
    assert view["key_present"] is True
    assert "api_key" not in view
    assert secret not in json.dumps(view)
    assert effective.drafting_endpoint_ref == view["endpoint"]
    assert effective.drafting_model == view["model"]
    assert effective.drafting_key_ref == view["key_location"]
    assert effective.drafting_draft_path == "draft"
    assert effective.drafting_draft_protocol == "pursers_json_v1"
    runtime = board_butler.resolve_provider_runtime(
        effective, "drafting", tmp_path / "private-keys"
    )
    assert runtime is not None
    assert runtime.endpoint == view["endpoint"]
    assert runtime.model == view["model"]
    assert runtime.request_headers()["Authorization"] == f"Bearer {secret}"
    assert secret not in repr(runtime)
    assert secret not in json.dumps(effective.as_finding())


def test_save_then_next_cycle_uses_custom_non_vendor_draft_contract(
    tmp_path: Path,
) -> None:
    secret = "custom-provider-secret-6471"
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append({"method": "GET", "path": self.path})
            self._reply({"models": [{"name": "vendor-model"}]})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            requests.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "tenant": self.headers.get("X-Tenant"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            self._reply({"draft": "custom provider shadow draft"})

        def _reply(self, document: dict[str, Any]) -> None:
            payload = json.dumps(document).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    saved: dict[str, Any] = {}
    secrets_dir = tmp_path / "secrets"
    endpoint = f"http://127.0.0.1:{server.server_port}/vendor/v2"
    try:
        manager = butler_settings.ButlerSettingsManager(secrets_dir)
        result = manager.save(
            {"config": coordinator_config(), "expected_sha256": "a" * 64},
            provider_request(
                endpoint=endpoint,
                model="vendor-model",
                api_key=secret,
                validation_path="validate",
                draft_path="generate",
                extra_headers={"X-Tenant": "sandbox"},
            ),
            "sandbox",
            lambda value, _expected: saved.update(config=value)
            or {"config": value, "expected_sha256": "b" * 64},
        )

        class Backend:
            project_name = "Pursers"
            identity = SimpleNamespace(
                agent_id="AI-butler",
                agent_name="board-butler-test",
                principal_id="PR-butler",
            )
            written: dict[str, Any] | None = None
            evaluation_written: dict[str, Any] | None = None

            async def findings(self) -> Mapping[str, Any]:
                return {}

            async def coordinator_config(self) -> Mapping[str, Any]:
                return saved["config"]

            async def evaluation(self, _question_id: str) -> Mapping[str, Any]:
                return {}

            async def write_findings(
                self, value: str, _expected: str | None
            ) -> None:
                self.written = json.loads(value)

            async def write_evaluation(
                self,
                _question_id: str,
                value: str,
                _expected: str | None,
            ) -> None:
                self.evaluation_written = json.loads(value)

        backend = Backend()
        finding = asyncio.run(
            board_butler.process_question(
                backend,
                {
                    "board_id": "sandbox",
                    "ticket_id": "TK-provider",
                    "question_id": "CQ-provider",
                    "kind": "information",
                    "message": "What should the coordinator answer?",
                },
                SimpleNamespace(
                    drafts_per_hour=5,
                    drafts_per_ticket=2,
                    drafts_per_board=20,
                    home_board="sandbox",
                    project="Pursers",
                    provider_secrets_dir=secrets_dir,
                    dry_run=False,
                    repo=REPO_ROOT,
                    integration_ref="origin/main",
                ),
                datetime(2026, 9, 17, tzinfo=timezone.utc),
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["saved"] is True
    assert [row["path"] for row in requests] == [
        "/vendor/v2/validate",
        "/vendor/v2/generate",
    ]
    draft_request = requests[1]
    assert draft_request["authorization"] == f"Bearer {secret}"
    assert draft_request["tenant"] == "sandbox"
    assert draft_request["body"]["protocol"] == "pursers_json_v1"
    assert draft_request["body"]["model"] == "vendor-model"
    assert draft_request["body"]["input"]["question"] == (
        "What should the coordinator answer?"
    )
    assert finding["message"] == "custom provider shadow draft"
    assert finding["draft_source"] == "configured_provider"
    assert backend.evaluation_written is not None
    assert backend.evaluation_written["evaluation"]["question_id"] == "CQ-provider"
    assert backend.evaluation_written["evaluation"]["ticket_id"] == "TK-provider"
    assert backend.evaluation_written["evaluation"]["draft_status"] == "produced"
    assert secret not in json.dumps(saved["config"])
    assert secret not in json.dumps(backend.written)
    assert secret not in json.dumps(backend.evaluation_written)
    assert secret not in json.dumps(finding)


@pytest.mark.parametrize(
    "secret",
    [
        "sentinel-space-6471 ",
        " sentinel-space-6471",
        "sentinel-space-6471\n",
        "sentinel-space-6471\x7f",
    ],
)
def test_save_rejects_noncanonical_key_before_any_side_effect(
    tmp_path: Path, secret: str
) -> None:
    opener_calls = 0
    save_calls = 0

    def opener(*_args: object, **_kwargs: object) -> Response:
        nonlocal opener_calls
        opener_calls += 1
        return Response({"data": [{"id": "Model/Exact-1"}]})

    def save_config(
        _value: dict[str, Any], _expected: str | None
    ) -> dict[str, Any]:
        nonlocal save_calls
        save_calls += 1
        return {}

    manager = butler_settings.ButlerSettingsManager(
        tmp_path / "private-keys", opener=opener
    )
    with pytest.raises(
        butler_settings.ButlerSettingsError,
        match="api_key must not contain surrounding whitespace or control characters",
    ) as caught:
        manager.save(
            {"config": coordinator_config(), "expected_sha256": "a" * 64},
            provider_request(api_key=secret),
            "sandbox",
            save_config,
        )

    assert secret not in str(caught.value)
    assert opener_calls == 0
    assert save_calls == 0
    assert not (tmp_path / "private-keys").exists()


def test_http_api_never_returns_key_or_persists_it_to_board_or_repo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "".join(("sentinel", "-browser-board-log-repo-", "6471"))

    class Cache:
        def __init__(self) -> None:
            self.config = coordinator_config()
            self.digest = "a" * 64

        def resolve_central(self, value: str | None) -> str:
            if value not in {None, "default"}:
                raise KeyError(value)
            return "default"

        def get_config(self, _central: str | None = None) -> dict[str, Any]:
            return {
                "config": self.config,
                "expected_sha256": self.digest,
                "central": "default",
            }

        def save_config(
            self, value: dict[str, Any], expected: str | None, _central: str | None = None
        ) -> dict[str, Any]:
            assert expected == self.digest
            self.config = dashboard.validate_coordinator_config(value)
            self.digest = "b" * 64
            return {"config": self.config, "expected_sha256": self.digest}

    cache = Cache()
    manager = butler_settings.ButlerSettingsManager(
        tmp_path / "secrets",
        opener=lambda *_args, **_kwargs: Response(
            {"data": [{"id": "Model/Exact-1"}]}
        ),
    )
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(cache, butler_manager=manager),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        body = json.dumps(provider_request(api_key=secret)).encode()
        request = urllib.request.Request(
            base + "/api/butler?central=default",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Origin": base},
        )
        saved_raw = urllib.request.urlopen(request).read()
        fetched_raw = urllib.request.urlopen(
            base + "/api/butler?central=default"
        ).read()
        kill_raw = urllib.request.urlopen(
            urllib.request.Request(
                base + "/api/butler/kill?central=default",
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json", "Origin": base},
            )
        ).read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    captured = capsys.readouterr()
    assert secret.encode() not in saved_raw
    assert secret.encode() not in fetched_raw
    assert secret.encode() not in kill_raw
    assert json.loads(kill_raw)["kill_switch_engaged"] is True
    assert stat.S_IMODE((tmp_path / "KILLED").stat().st_mode) == 0o600
    assert secret not in json.dumps(cache.config)
    assert secret not in captured.out
    assert secret not in captured.err
    assert all(secret not in record.getMessage() for record in caplog.records)

    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    assert all(
        secret.encode() not in (REPO_ROOT / raw.decode()).read_bytes()
        for raw in tracked
        if raw and (REPO_ROOT / raw.decode()).is_file()
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", "https://provider.example.invalid/sentinelleak6471"),
        ("model", "Model-sentinelleak6471"),
        ("extra_header_name", {"X-sentinelleak6471": "safe"}),
        ("extra_header_value", {"X-Auth": "sentinelleak6471"}),
        ("key_header", "X-sentinelleak6471"),
        ("key_prefix", "Bearer-sentinelleak6471"),
        ("validation_path", "models/sentinelleak6471"),
        ("draft_path", "generate/sentinelleak6471"),
    ],
)
def test_http_rejects_key_duplicated_into_readable_settings_before_side_effects(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: Any,
) -> None:
    secret = "sentinelleak6471"

    class Cache:
        def __init__(self) -> None:
            self.config = coordinator_config()
            self.digest = "a" * 64
            self.save_calls = 0

        def resolve_central(self, value: str | None) -> str:
            if value not in {None, "default"}:
                raise KeyError(value)
            return "default"

        def get_config(self, _central: str | None = None) -> dict[str, Any]:
            return {
                "config": self.config,
                "expected_sha256": self.digest,
                "central": "default",
            }

        def save_config(
            self, value: dict[str, Any], expected: str | None, _central: str | None = None
        ) -> dict[str, Any]:
            self.save_calls += 1
            self.config = value
            self.digest = "b" * 64
            return {"config": value, "expected_sha256": self.digest}

    opener_calls = 0

    def opener(*_args: object, **_kwargs: object) -> Response:
        nonlocal opener_calls
        opener_calls += 1
        return Response({"data": [{"id": "Model/Exact-1"}]})

    cache = Cache()
    secrets_dir = tmp_path / "secrets"
    manager = butler_settings.ButlerSettingsManager(secrets_dir, opener=opener)
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(cache, butler_manager=manager),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    request_body = provider_request(api_key=secret)
    if field == "extra_header_name":
        request_body["extra_headers"] = value
    elif field == "extra_header_value":
        request_body["extra_headers"] = value
    else:
        request_body[field] = value
    try:
        request = urllib.request.Request(
            base + "/api/butler?central=default",
            data=json.dumps(request_body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        response_raw = caught.value.read()
        view_raw = urllib.request.urlopen(base + "/api/butler?central=default").read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    captured = capsys.readouterr()
    assert caught.value.code == 400
    assert json.loads(response_raw) == {
        "error": "credential must not appear in persisted or readable settings",
        "central": "default",
    }
    assert len(response_raw) < 256
    assert secret.encode() not in response_raw
    assert secret.encode() not in view_raw
    assert opener_calls == 0
    assert cache.save_calls == 0
    assert secret not in json.dumps(cache.config)
    effective = board_butler.resolve_config(
        cache.config,
        SimpleNamespace(
            drafts_per_hour=5,
            drafts_per_ticket=2,
            drafts_per_board=20,
            home_board="sandbox-board",
            project=None,
        ),
        {},
        datetime(2026, 9, 17, tzinfo=timezone.utc),
    )
    assert secret not in json.dumps(effective.as_finding())
    assert not secrets_dir.exists()
    assert secret not in captured.out
    assert secret not in captured.err
    assert all(secret not in record.getMessage() for record in caplog.records)


def test_existing_key_cannot_be_duplicated_into_readable_settings(tmp_path: Path) -> None:
    secret = "existingcredential6471"
    saved: dict[str, Any] = {}
    opener_calls = 0

    def opener(*_args: object, **_kwargs: object) -> Response:
        nonlocal opener_calls
        opener_calls += 1
        return Response({"data": [{"id": "Model/Exact-1"}]})

    def save_config(value: dict[str, Any], _expected: str | None) -> dict[str, Any]:
        saved["config"] = value
        return {"config": value, "expected_sha256": "b" * 64}

    secrets_dir = tmp_path / "secrets"
    manager = butler_settings.ButlerSettingsManager(secrets_dir, opener=opener)
    manager.save(
        {"config": coordinator_config(), "expected_sha256": "a" * 64},
        provider_request(api_key=secret),
        "default",
        save_config,
    )
    key_files = list(secrets_dir.iterdir())
    assert len(key_files) == 1

    with pytest.raises(
        butler_settings.ButlerSettingsError,
        match="credential must not appear in persisted or readable settings",
    ) as caught:
        manager.save(
            {"config": saved["config"], "expected_sha256": "b" * 64},
            provider_request(
                api_key="",
                extra_headers={"X-Auth": secret},
                expected_sha256="b" * 64,
            ),
            "default",
            lambda *_args: pytest.fail("rejected settings must not be persisted"),
        )

    assert secret not in str(caught.value)
    assert opener_calls == 1
    assert list(secrets_dir.iterdir()) == key_files
    assert key_files[0].read_text(encoding="utf-8") == secret


def test_butler_panel_has_write_only_key_and_selector_contract() -> None:
    html = dashboard.HTML

    assert 'data-pursers-panel="butler-settings"' in html
    assert 'data-pursers-field="endpoint"' in html
    assert 'data-pursers-field="model"' in html
    assert 'data-pursers-field="api-key" type="password"' in html
    assert 'autocomplete="new-password"' in html
    assert 'data-pursers-action="save-butler"' in html
    assert 'data-pursers-field="draft-path"' in html
    assert 'data-pursers-field="draft-protocol"' in html
    assert "body.saved===false" in html
    assert "form.elements.api_key.value=''" in html
    assert "Saved changes apply on the butler's next question cycle." in html
    assert "Not configured" in html
    assert "Configured · not running" in html
    assert "Running · shadow" in html
    assert "Running · active" in html
    assert 'data-pursers-action="kill-butler"' in html
    assert "/api/butler/kill" in html

    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert scripts
    for source in scripts:
        subprocess.run(
            ["node", "--check"],
            input=source,
            text=True,
            check=True,
            capture_output=True,
        )
