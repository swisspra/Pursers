from __future__ import annotations

import importlib.util
import json
import re
import stat
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
    runtime = board_butler.resolve_provider_runtime(
        effective, "drafting", tmp_path / "private-keys"
    )
    assert runtime is not None
    assert runtime.endpoint == view["endpoint"]
    assert runtime.model == view["model"]
    assert runtime.request_headers()["Authorization"] == f"Bearer {secret}"
    assert secret not in repr(runtime)
    assert secret not in json.dumps(effective.as_finding())


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
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    captured = capsys.readouterr()
    assert secret.encode() not in saved_raw
    assert secret.encode() not in fetched_raw
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
    assert "body.saved===false" in html
    assert "form.elements.api_key.value=''" in html
    assert "Saved changes apply on the butler's next question cycle." in html

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
