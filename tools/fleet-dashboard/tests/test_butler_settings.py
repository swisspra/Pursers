from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
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
from html.parser import HTMLParser

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


def autonomous_config(board_id: str = "pursers") -> dict[str, Any]:
    return {
        "schema": "autonomous_butler_config_v1",
        "schema_version": 1,
        "board_id": board_id,
        "revision": 3,
        "enabled": False,
        "host_runtime": {
            "host_ref": "host-a",
            "revision": 1,
            "agent_process_ceiling": 12,
            "control_plane_processes": 2,
            "total_process_ceiling": 14,
            "configured_by": "human-admin",
            "configured_at": "2026-09-24T00:00:00+00:00",
        },
        "desired": {
            "mode": "shadow",
            "runner": "direct_api",
            "capacity": {
                role: {"min": 0, "target": 1, "max": 2}
                for role in ("worker", "reviewer", "acp_worker")
            },
            "host_concurrency": 4,
            "board_concurrency": 3,
            "cooldowns": {
                "scale_up_s": 30,
                "scale_down_s": 60,
                "failure_backoff_s": 10,
            },
            "budget": {
                "period": "day",
                "max_tokens": 10_000,
                "max_cost_microunits": 1_000_000,
                "max_external_calls": 100,
            },
            "connectors": [
                {
                    "connector_id": "github",
                    "enabled": True,
                    "transport": "streamable_http",
                    "protocol_revision": "2026-07-28",
                    "endpoint_ref": "github-endpoint",
                    "secret_ref": "github-secret",
                    "tools": [
                        {
                            "name": "issue_get",
                            "effect": "read_only",
                            "replay": "safe_with_stable_call_id",
                            "stable_call_id_field": "call_id",
                        }
                    ],
                    "resources": ["repo:issues"],
                    "limits": {
                        "timeout_ms": 1_000,
                        "max_input_bytes": 1_024,
                        "max_output_bytes": 4_096,
                        "max_concurrency": 2,
                        "calls_per_minute": 10,
                    },
                }
            ],
        },
        "envelope": {
            "fingerprint_sha256": "f" * 64,
            "approved_template_ids": ["worker-template"],
            "approved_connector_ids": ["github"],
            "max_capacity": {"worker": 4, "reviewer": 4, "acp_worker": 4},
            "max_host_concurrency": 8,
            "max_board_concurrency": 8,
            "max_budget": {
                "period": "day",
                "max_tokens": 100_000,
                "max_cost_microunits": 10_000_000,
                "max_external_calls": 1_000,
            },
            "created_by": "human-admin",
            "created_at": "2026-09-24T00:00:00+00:00",
        },
    }


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


@contextlib.contextmanager
def locked_helper_process(tmp_path: Path) -> Any:
    script = tmp_path / "board_butler.py"
    pid_path = tmp_path / "board-butler.pid"
    script.write_text(
        """import fcntl
import os
import sys
import time

with open(sys.argv[1], "a+", encoding="utf-8") as handle:
    os.chmod(sys.argv[1], 0o600)
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\\n")
    handle.flush()
    time.sleep(30)
""",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(script), str(pid_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(200):
            if pid_path.exists() and pid_path.read_text(encoding="utf-8").strip():
                break
            if process.poll() is not None:
                pytest.fail("locked helper exited before publishing its PID")
            time.sleep(0.01)
        else:
            pytest.fail("locked helper did not publish its PID")
        yield process, script, pid_path
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_indicator_binds_to_locked_expected_process_and_rejects_zombie(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime.json"
    with locked_helper_process(tmp_path) as (process, script, pid_path):
        write_runtime(runtime, mode="shadow", pid=process.pid)
        manager = butler_settings.ButlerSettingsManager(
            tmp_path / "secrets",
            runtime_path=runtime,
            pid_path=pid_path,
            expected_process_path=script,
        )
        assert manager.view(configured_payload(), "sandbox")["runtime"]["state"] == (
            "running_shadow"
        )
        process.terminate()
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        state = subprocess.run(
            ["/bin/ps", "-p", str(process.pid), "-o", "state="],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert state.upper().startswith("Z")

        status = manager.view(configured_payload(), "sandbox")["runtime"]
        assert status["state"] == "configured_not_running"
        assert status["running"] is False


def test_live_unrelated_process_is_not_reported_or_signaled(tmp_path: Path) -> None:
    process = subprocess.Popen(
        ["/bin/sleep", "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    runtime = tmp_path / "runtime.json"
    marker = tmp_path / "KILLED"
    write_runtime(runtime, mode="shadow", pid=process.pid)
    manager = butler_settings.ButlerSettingsManager(
        tmp_path / "secrets", runtime_path=runtime, kill_path=marker
    )
    try:
        status = manager.view(configured_payload(), "sandbox")["runtime"]
        assert status["state"] == "configured_not_running"
        assert status["running"] is False

        result = manager.kill(configured_payload(), "sandbox")
        assert result["signal_sent"] is False
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


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


def test_kill_switch_revalidates_process_identity_before_signal(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    marker = tmp_path / "KILLED"
    write_runtime(runtime, mode="shadow")
    inspections = iter((True, False, False))
    signals: list[tuple[int, int]] = []
    manager = butler_settings.ButlerSettingsManager(
        tmp_path / "secrets",
        runtime_path=runtime,
        kill_path=marker,
        process_inspector=lambda _pid: next(inspections),
        signaler=lambda pid, selected: signals.append((pid, selected)),
    )

    result = manager.kill(configured_payload(), "sandbox")

    assert result["kill_switch_engaged"] is True
    assert result["signal_sent"] is False
    assert signals == []


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


def test_openai_chat_protocol_requires_explicit_selection_and_preserves_legacy_default(
    tmp_path: Path,
) -> None:
    legacy = butler_settings.ButlerSettingsManager(tmp_path / "legacy").view(
        configured_payload(), "sandbox"
    )
    assert legacy["draft_path"] == "draft"
    assert legacy["draft_protocol"] == "pursers_json_v1"

    clean = butler_settings.validate_request(
        provider_request(
            draft_path="chat/completions",
            draft_protocol="openai_chat_completions_v1",
        )
    )
    assert clean["draft_path"] == "chat/completions"
    assert clean["draft_protocol"] == "openai_chat_completions_v1"

    with pytest.raises(
        butler_settings.ButlerSettingsError, match="draft_protocol is invalid"
    ):
        butler_settings.validate_request(
            provider_request(draft_protocol="implicit-or-unknown")
        )


def test_save_persists_openai_chat_protocol_for_next_resident_cycle(
    tmp_path: Path,
) -> None:
    saved: dict[str, Any] = {}
    manager = butler_settings.ButlerSettingsManager(
        tmp_path / "private-keys",
        opener=lambda *_args, **_kwargs: Response(
            {"data": [{"id": "Model/Exact-1"}]}
        ),
    )
    result = manager.save(
        {"config": coordinator_config(), "expected_sha256": "a" * 64},
        provider_request(
            draft_path="chat/completions",
            draft_protocol="openai_chat_completions_v1",
        ),
        "sandbox",
        lambda value, _expected: saved.update(config=value)
        or {"config": value, "expected_sha256": "b" * 64},
    )

    provider = saved["config"]["board_butler"]["global"]["drafting"]
    assert result["saved"] is True
    assert result["draft_path"] == "chat/completions"
    assert result["draft_protocol"] == "openai_chat_completions_v1"
    assert provider["draft_path"] == "chat/completions"
    assert provider["draft_protocol"] == "openai_chat_completions_v1"

    effective = board_butler.resolve_config(
        saved["config"],
        SimpleNamespace(
            drafts_per_hour=5,
            drafts_per_ticket=2,
            drafts_per_board=20,
            home_board="sandbox",
            project=None,
        ),
        {},
        datetime(2026, 9, 17, tzinfo=timezone.utc),
    )
    assert effective.drafting_draft_path == "chat/completions"
    assert effective.drafting_draft_protocol == "openai_chat_completions_v1"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://provider.example.invalid/v1",
        "https://169.254.169.254/v1",
        "https://2851995905/v1",
        "https://0xA9FE0101/v1",
        "https://0251.0376.0001.0001/v1",
        "https://[fe80::1]/v1",
        "https://[::ffff:169.254.169.254]/v1",
    ],
)
def test_provider_validation_refuses_unsafe_endpoint_without_request(
    endpoint: str,
) -> None:
    secret = "sentinel-policy-refusal-6471"
    requests = 0

    def opener(*_args: object, **_kwargs: object) -> Response:
        nonlocal requests
        requests += 1
        return Response({"data": [{"id": "Model/Exact-1"}]})

    settings = provider_request(endpoint="https://provider.example.invalid/v1")
    settings["endpoint"] = endpoint
    result = butler_settings.validate_provider(settings, secret, opener=opener)

    assert result.as_dict() == {
        "outcome": "unreachable",
        "message": "The endpoint is not permitted.",
        "http_status": None,
    }
    assert requests == 0
    assert secret not in json.dumps(result.as_dict())


@pytest.mark.parametrize("resolved_host", ["169.254.1.1", "fe80::1"])
def test_provider_validation_refuses_dns_link_local_without_request_or_key(
    resolved_host: str,
) -> None:
    secret = "sentinel-dns-refusal-6471"
    requests: list[urllib.request.Request] = []
    family = socket.AF_INET6 if ":" in resolved_host else socket.AF_INET

    def resolver(
        _host: str, port: int, _family: int, _socktype: int
    ) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        sockaddr: tuple[Any, ...]
        if family == socket.AF_INET6:
            sockaddr = (resolved_host, port, 0, 0)
        else:
            sockaddr = (resolved_host, port)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    def opener(request: urllib.request.Request, **_kwargs: object) -> Response:
        requests.append(request)
        return Response({"data": [{"id": "Model/Exact-1"}]})

    result = butler_settings.validate_provider(
        provider_request(endpoint="https://metadata.example/v1"),
        secret,
        opener=opener,
        resolver=resolver,
    )

    assert result.as_dict() == {
        "outcome": "unreachable",
        "message": "The endpoint is not permitted.",
        "http_status": None,
    }
    assert requests == []
    assert secret not in json.dumps(result.as_dict())


def test_provider_validation_fails_closed_when_dns_resolution_fails() -> None:
    secret = "sentinel-resolution-refusal-6471"
    requests: list[urllib.request.Request] = []

    def resolver(*_args: object) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        raise socket.gaierror("unavailable")

    def opener(request: urllib.request.Request, **_kwargs: object) -> Response:
        requests.append(request)
        return Response({"data": [{"id": "Model/Exact-1"}]})

    result = butler_settings.validate_provider(
        provider_request(), secret, opener=opener, resolver=resolver
    )

    assert result.as_dict() == {
        "outcome": "unreachable",
        "message": "The endpoint is not permitted.",
        "http_status": None,
    }
    assert requests == []
    assert secret not in json.dumps(result.as_dict())


def test_provider_validation_refuses_http_localhost_resolving_non_loopback() -> None:
    requests: list[urllib.request.Request] = []

    def resolver(
        _host: str, port: int, _family: int, _socktype: int
    ) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", port))
        ]

    def opener(request: urllib.request.Request, **_kwargs: object) -> Response:
        requests.append(request)
        return Response({"data": [{"id": "Model/Exact-1"}]})

    result = butler_settings.validate_provider(
        provider_request(endpoint="http://localhost/v1"),
        "sentinel-http-resolution-6471",
        opener=opener,
        resolver=resolver,
    )

    assert result.as_dict() == {
        "outcome": "unreachable",
        "message": "The endpoint is not permitted.",
        "http_status": None,
    }
    assert requests == []


def test_provider_validation_pins_request_to_validated_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("203.0.113.7", 443),
        )
    ]
    pinned: list[tuple[str, tuple[Any, ...]]] = []

    def resolver(
        _host: str, _port: int, _family: int, _socktype: int
    ) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        return resolved

    def pinned_opener(
        validation_url: str, addresses: tuple[Any, ...]
    ) -> Any:
        pinned.append((validation_url, addresses))
        return lambda *_args, **_kwargs: Response(
            {"data": [{"id": "Model/Exact-1"}]}
        )

    monkeypatch.setattr(butler_settings, "_pinned_opener", pinned_opener)

    result = butler_settings.validate_provider(
        provider_request(endpoint="https://provider.example/v1"),
        "sentinel-pinned-6471",
        resolver=resolver,
    )

    assert result.outcome == "reachable"
    assert pinned == [
        (
            "https://provider.example/v1/models",
            tuple(resolved),
        )
    ]


@pytest.mark.parametrize(
    ("connection_class", "port", "use_tls"),
    [
        (butler_settings._PinnedHTTPConnection, 80, False),
        (butler_settings._PinnedHTTPSConnection, 443, True),
    ],
)
def test_pinned_connection_uses_validated_address_without_second_resolution(
    monkeypatch: pytest.MonkeyPatch,
    connection_class: type[Any],
    port: int,
    use_tls: bool,
) -> None:
    connected: list[tuple[Any, ...]] = []
    resolution_attempts: list[tuple[str, int]] = []
    server_names: list[str] = []

    class FakeSocket:
        def settimeout(self, _timeout: object) -> None:
            return

        def bind(self, _source_address: tuple[str, int]) -> None:
            return

        def connect(self, sockaddr: tuple[Any, ...]) -> None:
            connected.append(sockaddr)

        def setsockopt(self, *_args: object) -> None:
            return

        def close(self) -> None:
            return

    class FakeContext:
        def wrap_socket(
            self, sock: FakeSocket, *, server_hostname: str
        ) -> FakeSocket:
            server_names.append(server_hostname)
            return sock

    def rebound_resolver(
        host: str,
        requested_port: int,
        *_args: object,
        **_kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        resolution_attempts.append((host, requested_port))
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("169.254.1.1", requested_port),
            )
        ]

    monkeypatch.setattr(butler_settings.socket, "getaddrinfo", rebound_resolver)
    monkeypatch.setattr(
        butler_settings.socket,
        "socket",
        lambda *_args, **_kwargs: FakeSocket(),
    )
    resolved = (
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("203.0.113.7", port),
        ),
    )
    kwargs: dict[str, Any] = {"resolved": resolved}
    if use_tls:
        kwargs["context"] = FakeContext()
    connection = connection_class("provider.example", port=port, **kwargs)

    connection.connect()

    assert resolution_attempts == []
    assert connected == [("203.0.113.7", port)]
    assert server_names == (["provider.example"] if use_tls else [])


def test_provider_validation_refuses_cross_origin_redirect_before_key_leaves_origin(
) -> None:
    secret = "sentinel-redirect-refusal-6471"
    redirected_requests: list[str | None] = []

    class RedirectTarget(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            redirected_requests.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), RedirectTarget)

    class RedirectSource(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target.server_port}/models"
            )
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectSource)
    threads = [
        threading.Thread(target=target.serve_forever, daemon=True),
        threading.Thread(target=source.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        result = butler_settings.validate_provider(
            provider_request(
                endpoint=f"http://127.0.0.1:{source.server_port}",
                validation_path="redirect",
            ),
            secret,
        )
    finally:
        source.shutdown()
        target.shutdown()
        source.server_close()
        target.server_close()
        for thread in threads:
            thread.join(timeout=2)

    assert result.outcome == "unreachable"
    assert redirected_requests == []
    assert secret not in json.dumps(result.as_dict())


def test_extra_headers_cannot_set_host_or_override_custom_key_header() -> None:
    with pytest.raises(
        butler_settings.ButlerSettingsError, match="extra_headers must not set Host"
    ):
        butler_settings.validate_request(
            provider_request(extra_headers={"hOsT": "provider.example.invalid"})
        )

    with pytest.raises(
        butler_settings.ButlerSettingsError, match="key_header must not be Host"
    ):
        butler_settings.validate_request(provider_request(key_header="HOST"))

    with pytest.raises(
        butler_settings.ButlerSettingsError,
        match="key_header must not duplicate extra_headers",
    ):
        butler_settings.validate_request(
            provider_request(
                key_header="X-Credential",
                extra_headers={"x-credential": "replacement"},
            )
        )


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


def test_endpoint_change_never_reuses_stored_key(tmp_path: Path) -> None:
    secret = "sentinel-endpoint-binding-6471"
    authorizations: list[str | None] = []
    state = {
        "config": coordinator_config(),
        "expected_sha256": "a" * 64,
    }

    def opener(request: urllib.request.Request, **_kwargs: object) -> Response:
        authorizations.append(request.get_header("Authorization"))
        return Response({"data": [{"id": "Model/Exact-1"}]})

    def save_config(value: dict[str, Any], _expected: str | None) -> dict[str, Any]:
        state["config"] = value
        state["expected_sha256"] = "b" * 64
        return dict(state)

    secrets_dir = tmp_path / "private-keys"
    manager = butler_settings.ButlerSettingsManager(secrets_dir, opener=opener)
    first = manager.save(
        state,
        provider_request(api_key=secret),
        "sandbox",
        save_config,
    )
    old_key = secrets_dir / first["key_location"].removeprefix("file:")
    changed = manager.save(
        state,
        provider_request(
            endpoint="https://other-provider.example.invalid/v1",
            api_key="",
            expected_sha256="b" * 64,
        ),
        "sandbox",
        save_config,
    )

    assert authorizations == [f"Bearer {secret}", None]
    assert changed["saved"] is True
    assert changed["key_present"] is False
    assert changed["key_location"] is None
    assert old_key.exists() is False
    provider = state["config"]["board_butler"]["global"]["drafting"]
    assert provider["endpoint_ref"] == "https://other-provider.example.invalid/v1"
    assert provider["key_ref"] is None
    assert secret not in json.dumps(changed)


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


def test_butler_save_route_rejects_cross_origin_before_validation(
    tmp_path: Path,
) -> None:
    class Cache:
        def resolve_central(self, value: str | None) -> str:
            if value not in {None, "default"}:
                raise KeyError(value)
            return "default"

        def get_config(self, _central: str | None = None) -> dict[str, Any]:
            return {
                "config": coordinator_config(),
                "expected_sha256": "a" * 64,
                "central": "default",
            }

        def save_config(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
            pytest.fail("cross-origin request must not save settings")

    validation_calls = 0

    def opener(*_args: object, **_kwargs: object) -> Response:
        nonlocal validation_calls
        validation_calls += 1
        return Response({"data": [{"id": "Model/Exact-1"}]})

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            Cache(),
            butler_manager=butler_settings.ButlerSettingsManager(
                tmp_path / "secrets", opener=opener
            ),
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = urllib.request.Request(
            base + "/api/butler?central=default",
            data=json.dumps(provider_request()).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://attacker.example.invalid",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        response = caught.value.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert caught.value.code == 403
    assert json.loads(response) == {"error": "same-origin request required"}
    assert validation_calls == 0


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
    assert '<select name="draft_protocol"' in html
    assert 'value="openai_chat_completions_v1"' in html
    assert "body.saved===false" in html
    assert "form.elements.api_key.value=''" in html
    assert "Saved changes apply on the butler's next question cycle." in html
    assert "Not configured" in html
    assert "Configured · not running" in html
    assert "Running · shadow" in html
    assert "Running · active" in html
    assert 'data-pursers-action="kill-butler"' in html
    assert "/api/butler/kill" in html
    assert "/api/butler/autonomous" in html
    assert "Desired versus actual" in html
    assert "MCP v2 connector allowlists" in html
    assert "Kill immediately" in html
    assert "Autonomous · separate authorization required" in html
    assert "--bad:#984d3d" in html
    assert ".intake-actions button,.autonomous-card button{min-height:44px" in html
    assert ".connector-row label{display:flex;align-items:center;gap:8px;min-height:44px}" in html

    scripts = _inline_scripts(html)
    assert scripts
    for source in scripts:
        subprocess.run(
            ["node", "--check"],
            input=source,
            text=True,
            check=True,
            capture_output=True,
        )


def test_autonomous_view_redacts_references_and_projects_truthful_state() -> None:
    config = autonomous_config()
    payload = {
        "board_id": "pursers",
        "revision": 3,
        "effective_mode": "shadow",
        "config_digest_sha256": "a" * 64,
        "config": config,
    }
    commands = {
        "commands": [
            {
                "command_id": "cmd-1",
                "intent": "kill",
                "status": "succeeded",
                "revision": 2,
                "created_at": "2026-09-24T00:00:00+00:00",
                "updated_at": "2026-09-24T00:00:01+00:00",
                "sender": {"principal_id": "private-principal"},
                "transition": {
                    "reason_code": "kill_completed",
                    "audit_id": "audit-1",
                },
            }
        ],
        "truncated": True,
    }
    actual = {
        "schema": "autonomous_butler_state_v1",
        "effective_state": "killed",
        "stale_after": "2026-09-25T00:00:00+00:00",
        "connectors": [],
    }

    view = butler_settings.autonomous_butler_view(payload, commands, actual)

    connector = view["config"]["desired"]["connectors"][0]
    assert connector["secret_configured"] is True
    assert "secret_ref" not in connector
    assert "endpoint_ref" not in connector
    assert "github-secret" not in json.dumps(view)
    assert "github-endpoint" not in json.dumps(view)
    assert "private-principal" not in json.dumps(view)
    assert view["effective_state"] == "killed"
    assert view["actual_state_available"] is True
    assert view["commands"][0]["audit_id"] == "audit-1"
    assert view["history_truncated"] is True


def test_autonomous_view_marks_stale_health_unknown() -> None:
    state = {
        "schema": "autonomous_butler_state_v1",
        "effective_state": "degraded",
        "stale_after": "2026-09-24T00:00:00+00:00",
        "executor": {
            "status": "healthy",
            "observed_at": "2026-09-23T23:59:00+00:00",
        },
        "connectors": [
            {
                "connector_id": "github",
                "status": "healthy",
                "observed_at": "2026-09-23T23:59:00+00:00",
            }
        ],
    }

    view = butler_settings.autonomous_butler_view(
        {"board_id": "pursers", "revision": 3, "config": autonomous_config()},
        {"commands": []},
        state,
        now=datetime(2026, 9, 24, 1, tzinfo=timezone.utc),
    )

    assert view["actual_state_stale"] is True
    assert view["actual_state"]["executor"]["status"] == "unknown"
    assert view["actual_state"]["connectors"][0]["status"] == "unknown"
    assert state["executor"]["status"] == "healthy"


def test_prepare_autonomous_config_is_shadow_only_cas_and_preserves_envelope() -> None:
    config = autonomous_config()
    request = {
        "board_id": "pursers",
        "mutation_id": "mutation-1",
        "expected_revision": 3,
        "mode": "shadow",
        "runner": "acp",
        "capacity": {
            "worker": {"min": 1, "target": 2, "max": 3},
            "reviewer": {"min": 1, "target": 1, "max": 2},
            "acp_worker": {"min": 0, "target": 1, "max": 2},
        },
        "host_concurrency": 6,
        "board_concurrency": 5,
        "cooldowns": {
            "scale_up_s": 5,
            "scale_down_s": 10,
            "failure_backoff_s": 3,
        },
        "budget": {
            "period": "day",
            "max_tokens": 2_000,
            "max_cost_microunits": 3_000,
            "max_external_calls": 4,
        },
        "connectors": [{"connector_id": "github", "enabled": False}],
    }

    updated, expected, mutation_id = butler_settings.prepare_autonomous_butler_config(
        {"board_id": "pursers", "revision": 3, "config": config}, request
    )

    assert expected == 3
    assert mutation_id == "mutation-1"
    assert updated["revision"] == 4
    assert updated["enabled"] is False
    assert updated["desired"]["mode"] == "shadow"
    assert updated["desired"]["runner"] == "acp"
    assert updated["desired"]["connectors"][0]["enabled"] is False
    assert updated["desired"]["connectors"][0]["secret_ref"] == "github-secret"
    assert updated["envelope"] == config["envelope"]
    assert "authorization" not in updated

    with pytest.raises(
        butler_settings.ButlerSettingsError, match="separate active authorization"
    ):
        butler_settings.prepare_autonomous_butler_config(
            {"board_id": "pursers", "revision": 3, "config": config},
            {**request, "mode": "autonomous"},
        )
    with pytest.raises(
        butler_settings.ButlerSettingsError, match="reload before saving"
    ):
        butler_settings.prepare_autonomous_butler_config(
            {"board_id": "pursers", "revision": 4, "config": config}, request
        )


@pytest.mark.parametrize(
    ("request_data", "parameters"),
    [
        (
            {
                "board_id": "pursers",
                "request_id": "request-1",
                "intent": "reconcile_now",
                "expected_config_revision": 3,
            },
            {},
        ),
        (
            {
                "board_id": "pursers",
                "request_id": "request-2",
                "intent": "kill",
                "expected_config_revision": 3,
                "reason_code": "human_kill_switch",
            },
            {"reason_code": "human_kill_switch"},
        ),
        (
            {
                "board_id": "pursers",
                "request_id": "request-3",
                "intent": "disable_connector",
                "expected_config_revision": 3,
                "connector_id": "github",
            },
            {"connector_id": "github"},
        ),
    ],
)
def test_autonomous_commands_are_typed_and_bounded(
    request_data: dict[str, Any], parameters: dict[str, Any]
) -> None:
    assert butler_settings.validate_autonomous_command_request(request_data)[
        "parameters"
    ] == parameters

    with pytest.raises(butler_settings.ButlerSettingsError):
        butler_settings.validate_autonomous_command_request(
            {**request_data, "shell": "rm -rf /"}
        )


def test_autonomous_http_api_routes_typed_reads_writes_and_commands() -> None:
    class Cache:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def resolve_central(self, value: str | None) -> str:
            if value not in {None, "default"}:
                raise KeyError(value)
            return "default"

        def get_autonomous_butler(
            self, board_id: str, _central: str | None = None
        ) -> dict[str, Any]:
            self.calls.append(("get", board_id))
            return {"board_id": board_id, "revision": 3, "config": None}

        def save_autonomous_butler(
            self, board_id: str, request: dict[str, Any], _central: str | None = None
        ) -> dict[str, Any]:
            self.calls.append(("save", board_id))
            return {"board_id": board_id, "revision": request["expected_revision"] + 1}

        def submit_autonomous_butler_command(
            self, board_id: str, request: dict[str, Any], _central: str | None = None
        ) -> dict[str, Any]:
            self.calls.append((request["intent"], board_id))
            return {"board_id": board_id, "submitted_command": {"status": "accepted"}}

    cache = Cache()
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(cache)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        fetched = json.loads(
            urllib.request.urlopen(
                base + "/api/butler/autonomous?central=default&board_id=pursers"
            ).read()
        )
        save_body = {
            "board_id": "pursers",
            "mutation_id": "mutation-1",
            "expected_revision": 3,
        }
        saved = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(
                    base + "/api/butler/autonomous?central=default",
                    data=json.dumps(save_body).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json", "Origin": base},
                )
            ).read()
        )
        command_body = {
            "board_id": "pursers",
            "request_id": "request-1",
            "intent": "kill",
            "expected_config_revision": 4,
            "reason_code": "human_kill_switch",
        }
        commanded = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(
                    base + "/api/butler/autonomous/command?central=default",
                    data=json.dumps(command_body).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json", "Origin": base},
                )
            ).read()
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert fetched["revision"] == 3
    assert saved["revision"] == 4
    assert commanded["submitted_command"]["status"] == "accepted"
    assert cache.calls == [
        ("get", "pursers"),
        ("save", "pursers"),
        ("kill", "pursers"),
    ]


def test_fetcher_projects_product_config_and_command_responses() -> None:
    calls: list[tuple[str, int | None]] = []

    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def butler_config_get(self) -> dict[str, Any]:
            calls.append(("config", None))
            return {
                "board_id": "pursers",
                "revision": 3,
                "effective_mode": "shadow",
                "config_digest_sha256": "a" * 64,
                "config": autonomous_config(),
            }

        async def butler_command_inspect(self, *, limit: int) -> dict[str, Any]:
            calls.append(("commands", limit))
            return {"commands": [], "truncated": False}

    class Fetcher(dashboard.FleetFetcher):
        async def _boards(self) -> list[tuple[str, str]]:
            return [("Project", "pursers")]

    config = dashboard.Config(
        url="http://127.0.0.1:8766/mcp",
        token="test-token",
        home_board="pursers",
        agent_name="fleet-dashboard-session-default",
        stale_seconds=300,
        cache_seconds=5.0,
    )
    fetcher = Fetcher(config, client_factory=lambda *_args, **_kwargs: Client())
    try:
        view = asyncio.run(fetcher.fetch_autonomous_butler("pursers"))
    finally:
        fetcher.close()

    assert calls == [("config", None), ("commands", 50)]
    assert view["revision"] == 3
    assert view["effective_state"] == "shadow"
    assert view["config"]["desired"]["connectors"][0][
        "secret_configured"
    ] is True


class _InlineScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self._current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and not dict(attrs).get("src"):
            self._current = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._current is not None:
            self.scripts.append("".join(self._current))
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.append(data)


def _inline_scripts(html: str) -> list[str]:
    """Inline <script> bodies, found by a real HTML parser rather than a regex."""
    parser = _InlineScripts()
    parser.feed(html)
    parser.close()
    return parser.scripts
