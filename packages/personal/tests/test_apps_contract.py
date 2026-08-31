"""Isolated MCP Apps surface and profile-custody contract tests."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from mcp import Client
from mcp.client import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID

from pursers_personal import apps_server
from pursers_personal.apps_server import (
    APP_ONLY,
    CHAT_TOOL_NAMES,
    MODEL_AND_APP,
    MODEL_ONLY,
    PINNED_CLIENT_VERSION,
    PRODUCT_VERSION,
    PRIMARY_UI_TOOL_NAMES,
    UI_URI,
    DashboardConfig,
    LiveDashboard,
    build_dashboard_server,
    config_from_personal_context,
)


class FakeClientError(Exception):
    pass


class FakeClient:
    def __init__(self, *_args: Any, **_kwargs: Any):
        pass

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


def fake_config(token: str = "secret-not-for-the-view") -> DashboardConfig:
    return DashboardConfig(
        "http://127.0.0.1:8766/mcp",
        token,
        "board-personal-test",
        "codex-test-session",
    )


def test_exact_view_lock_and_embedded_external_attestation_boundary() -> None:
    root = Path(__file__).resolve().parents[1]
    view_path = root / "src/pursers_personal/resources/dashboard.html"
    lock_path = root / "src/pursers_personal/resources/component-lock.json"
    payload = view_path.read_bytes()
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    expected = "46e4eb01e18fa7e285c7ed4dc7600c81086d30fb83e168874d61abaafdc6c243"
    assert len(payload) == 396_499
    assert hashlib.sha256(payload).hexdigest() == expected
    assert lock["product_version"] == PRODUCT_VERSION == "5.0.0a7"
    assert lock["view"] == {
        "resource": "pursers_personal/resources/dashboard.html",
        "size_bytes": len(payload),
        "sha256": expected,
    }

    readme = (root / "README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())
    assert "Possessing this package does not authorize installing or activating it" in normalized_readme
    assert "operator test manifest may authorize `HOST_PROOF_ONLY`" in normalized_readme
    assert "dedicated isolated account or VM" in normalized_readme
    assert "`release_status=DO_NOT_PUBLISH`" in normalized_readme
    assert "`supported_hosts=[]`" in normalized_readme
    assert "never authorizes ordinary use, publication, or a supported-Host claim" in normalized_readme
    assert "official release manifest" in normalized_readme
    assert "identifies this exact wheel by filename and SHA-256" in normalized_readme
    assert "lists the exact Host product, version, and build" in normalized_readme
    assert "If neither matching external attestation exists, do not install or activate" in normalized_readme
    assert "never permits ordinary use" in normalized_readme
    assert "does not declare the current release status" in normalized_readme
    assert "claim support for any Host" in normalized_readme
    assert "This wheel is an isolated host-proof candidate" not in readme
    assert "The candidate remains" not in readme
    for forbidden in ("uv tool install", "--apply", "--activate"):
        assert forbidden not in readme


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_discovery_envelope_partitions_app_and_model_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified: list[object] = []
    monkeypatch.setattr(
        apps_server,
        "verify_component_artifacts",
        lambda names=None: verified.append(names) or {},
    )
    server, state = build_dashboard_server(
        fake_config(),
        client_class=FakeClient,
        client_error_class=FakeClientError,
    )
    assert state._post_join_hook is None
    extension = advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})
    try:
        async with Client(
            server, extensions=[extension], raise_exceptions=True
        ) as client:
            assert client.server_info is not None
            assert client.server_info.version == PRODUCT_VERSION
            response = await client.list_tools()
            tools = {tool.name: tool for tool in response.tools}
            assert set(tools) == PRIMARY_UI_TOOL_NAMES | CHAT_TOOL_NAMES

            def visibility(name: str) -> list[str]:
                return tools[name].meta["ui"]["visibility"]

            assert visibility("board_snapshot") == MODEL_AND_APP
            assert visibility("fleet_snapshot") == MODEL_AND_APP
            assert visibility("board_event_feed") == APP_ONLY
            assert all(visibility(name) == MODEL_ONLY for name in CHAT_TOOL_NAMES)
            catchup_description = tools["board_catchup"].description or ""
            assert "even when ack is false" in catchup_description
            assert "advances the durable cursor" in catchup_description
            assert all(
                tool.meta["ui"]["resourceUri"] == UI_URI
                for tool in tools.values()
            )

            # A hostile View is discoverably authorized for only these two tools.
            app_callable = {
                name for name in tools if "app" in visibility(name)
            }
            assert app_callable == PRIMARY_UI_TOOL_NAMES
            assert not app_callable & CHAT_TOOL_NAMES

            hidden = {
                "board_join",
                "board_list",
                "board_reap",
                "board_review_policy_set",
                "board_scrub_profile_set",
                "board_invite_issue",
                "board_capability_generate",
            }
            assert hidden.isdisjoint(tools)

            resource = await client.read_resource(UI_URI)
            html = resource.contents[0]
            assert html.mime_type == APP_MIME_TYPE
            assert html.meta["ui"]["csp"] == {}
            assert "On Board Personal Preview" in html.text
            assert fake_config().token not in html.text
            assert all(name not in html.text for name in CHAT_TOOL_NAMES)
            assert html.text.count("board_snapshot") == 1
            assert html.text.count("board_event_feed") == 1
            for fleet_marker in (
                "fleet_snapshot",
                "tab-fleet",
                "fleet-projects",
                "fleet-pool",
                "registry_warning",
                "tickets_open",
                "tickets_claimed",
                "tickets_submitted",
                "pool_status",
                "current_ticket_id",
            ):
                assert fleet_marker in html.text
            assert verified == [None]

            model_calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

            async def model_rpc(
                method: str, *args: Any, **kwargs: Any
            ) -> dict[str, Any]:
                model_calls.append((method, args, kwargs))
                return {"ok": True, "acknowledged_cursor": kwargs.get("cursor")}

            state._rpc = model_rpc  # type: ignore[method-assign]
            result = await client.call_tool(
                "board_catchup", {"cursor": 11, "limit": 25, "ack": False}
            )
            assert result.is_error is False
            assert model_calls == [
                ("board_catchup", (), {"cursor": 11, "limit": 25, "ack": False})
            ]
    finally:
        await state.stop()


def test_config_is_profile_derived_loopback_only_and_secret_safe() -> None:
    context = SimpleNamespace(
        central_url="http://localhost:8766/mcp",
        capability_token="profile-secret",
        board_id="board-profile",
        agent_name="codex-derived-agent",
    )
    config = config_from_personal_context(context)
    assert config.central_url == context.central_url
    assert config.token == context.capability_token
    assert config.board_id == context.board_id
    assert config.agent_name == context.agent_name
    assert context.capability_token not in repr(config)

    with pytest.raises(ValueError, match="loopback"):
        DashboardConfig(
            "https://central.example/mcp", "secret", "board", "agent"
        )
    with pytest.raises(ValueError, match="loopback"):
        DashboardConfig(
            "http://user:secret@127.0.0.1/mcp", "secret", "board", "agent"
        )


def test_client_is_verified_before_import_and_origin_checked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[Any] = []

    def verify(names: set[str]) -> dict[str, dict[str, Any]]:
        events.append("verify")
        assert names == {"pursers-client"}
        return {
            "pursers-client": {
                "version": PINNED_CLIENT_VERSION,
                "members": {},
            }
        }

    class ApprovedClient:
        pass

    class ApprovedError(Exception):
        pass

    def import_component(*args: Any, **kwargs: Any) -> Any:
        events.append((args, kwargs))
        return SimpleNamespace(
            BoardClient=ApprovedClient, BoardClientError=ApprovedError
        )

    monkeypatch.setattr(apps_server, "verify_component_artifacts", verify)
    monkeypatch.setattr(apps_server, "import_verified_component", import_component)

    assert apps_server._load_board_client() == (ApprovedClient, ApprovedError)
    assert events == [
        "verify",
        (
            ("pursers-client", "pursers_client", "pursers_client.client"),
            {
                "package_member": "pursers_client/__init__.py",
                "module_member": "pursers_client/client.py",
            },
        ),
    ]

    events.clear()
    monkeypatch.setattr(
        apps_server,
        "import_verified_component",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("source verification failed")
        ),
    )
    with pytest.raises(RuntimeError, match="source verification failed"):
        apps_server._load_board_client()
    assert events == ["verify"]


def test_personal_factory_uses_explicit_profile_host_and_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, Any] = {}
    profile_path = tmp_path / "profile.json"
    context = SimpleNamespace(
        central_url="http://127.0.0.1:8766/mcp",
        capability_token="profile-only-token",
        board_id="board-explicit",
        agent_name="host-session-derived",
    )
    profile_module = ModuleType("pursers_personal.profile")

    def load_personal_profile(value: Path) -> object:
        calls["profile_path"] = value
        return object()

    def resolve_personal_context(
        profile: object, *, host: str, session: str
    ) -> Any:
        calls.update(profile=profile, host=host, session=session)
        return context

    async def bootstrap_personal_review_policy(client: object) -> dict[str, Any]:
        calls["bootstrap_client"] = client
        return {"ok": True}

    profile_module.load_personal_profile = load_personal_profile  # type: ignore[attr-defined]
    profile_module.resolve_personal_context = resolve_personal_context  # type: ignore[attr-defined]
    profile_module.bootstrap_personal_review_policy = (  # type: ignore[attr-defined]
        bootstrap_personal_review_policy
    )
    monkeypatch.setitem(sys.modules, "pursers_personal.profile", profile_module)
    monkeypatch.setattr(
        apps_server, "_load_board_client", lambda: (FakeClient, FakeClientError)
    )
    monkeypatch.setattr(
        apps_server, "verify_component_artifacts", lambda names=None: {}
    )
    monkeypatch.setenv("ONBOARD_CENTRAL_TOKEN", "hostile-legacy-token")
    monkeypatch.setenv("ONBOARD_BOARD_ID", "hostile-legacy-board")

    _server, state = apps_server.build_personal_server(
        profile_path, "claude-desktop", "session-123"
    )
    assert calls["profile_path"] == profile_path
    assert calls["host"] == "claude-desktop"
    assert calls["session"] == "session-123"
    assert state.config.token == "profile-only-token"
    assert state.config.board_id == "board-explicit"
    assert state._post_join_hook is bootstrap_personal_review_policy


def test_run_entrypoint_builds_then_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, str, str] | str] = []

    class FakeServer:
        def run(self) -> None:
            calls.append("run")

    profile_path = Path("/tmp/synthetic-profile.json")

    def build(path: Path, host: str, session: str) -> tuple[FakeServer, object]:
        calls.append((path, host, session))
        return FakeServer(), object()

    monkeypatch.setattr(apps_server, "build_personal_server", build)
    result = apps_server.run_personal_mcp(profile_path, "codex", "session")
    assert result is None
    assert calls == [(profile_path, "codex", "session"), "run"]


@pytest.mark.anyio
async def test_rpc_envelope_preserves_kwargs() -> None:
    state = LiveDashboard(
        fake_config(),
        client_class=FakeClient,
        client_error_class=FakeClientError,
    )

    async def started() -> None:
        state._client = object()
        state._connected = True

    state.start = started  # type: ignore[method-assign]
    call = asyncio.create_task(
        state._rpc("ticket_submit", "TK-1", stay_active=False)
    )
    method, args, kwargs, future = await state._commands.get()
    assert method == "ticket_submit"
    assert args == ("TK-1",)
    assert kwargs == {"stay_active": False}
    future.set_result({"ok": True})
    assert await call == {"ok": True}


@pytest.mark.anyio
async def test_rpc_surfaces_allowlisted_central_validation_detail() -> None:
    detail = "generated-ID tickets require: description, target_url"
    wrapped = (
        "[TextContent(type='text', text='Error executing tool ticket_create: "
        f"{detail}', annotations=None, meta=None)]"
    )

    class ValidationClient(FakeClient):
        async def ticket_create(self) -> None:
            raise FakeClientError(wrapped)

    state = LiveDashboard(
        fake_config(),
        client_class=ValidationClient,
        client_error_class=FakeClientError,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError) as caught:
            await state._rpc("ticket_create")
        assert str(caught.value) == (
            f"Central request failed (FakeClientError): {detail}"
        )
    finally:
        await state.stop()


@pytest.mark.anyio
async def test_rpc_surfaces_fixed_central_ticket_status_detail() -> None:
    detail = "ticket is submitted"
    wrapped = (
        "[TextContent(type='text', text='Error executing tool ticket_claim: "
        f"{detail}', annotations=None, meta=None)]"
    )

    class FixedStatusClient(FakeClient):
        async def ticket_claim(self) -> None:
            raise FakeClientError(wrapped)

    state = LiveDashboard(
        fake_config(),
        client_class=FixedStatusClient,
        client_error_class=FakeClientError,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError) as caught:
            await state._rpc("ticket_claim")
        assert str(caught.value) == (
            f"Central request failed (FakeClientError): {detail}"
        )
    finally:
        await state.stop()


@pytest.mark.anyio
async def test_rpc_redacts_bare_hostname_in_wrapped_client_error() -> None:
    leaked_host = "sensitive.central.example"
    wrapped = (
        "[TextContent(type='text', text='Error executing tool ticket_create: "
        f"target must be {leaked_host}', annotations=None, meta=None)]"
    )

    class WrappedHostFailureClient(FakeClient):
        async def ticket_create(self) -> None:
            raise FakeClientError(wrapped)

    state = LiveDashboard(
        fake_config(),
        client_class=WrappedHostFailureClient,
        client_error_class=FakeClientError,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError) as caught:
            await state._rpc("ticket_create")
        message = str(caught.value)
        assert message == "Central request failed (FakeClientError)"
        assert leaked_host not in message
    finally:
        await state.stop()


@pytest.mark.anyio
async def test_rpc_redacts_unrecognized_wrapped_ticket_status() -> None:
    leaked_value = "syntheticprivatevalue"
    wrapped = (
        "[TextContent(type='text', text='Error executing tool ticket_claim: "
        f"ticket is {leaked_value}', annotations=None, meta=None)]"
    )

    class WrappedStatusFailureClient(FakeClient):
        async def ticket_claim(self) -> None:
            raise FakeClientError(wrapped)

    state = LiveDashboard(
        fake_config(),
        client_class=WrappedStatusFailureClient,
        client_error_class=FakeClientError,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError) as caught:
            await state._rpc("ticket_claim")
        message = str(caught.value)
        assert message == "Central request failed (FakeClientError)"
        assert leaked_value not in message
    finally:
        await state.stop()


@pytest.mark.parametrize("error_type", [ValueError, TypeError])
def test_request_error_surfaces_direct_validation_types(
    error_type: type[Exception],
) -> None:
    state = LiveDashboard(
        fake_config(),
        client_class=FakeClient,
        client_error_class=FakeClientError,
    )
    detail = "priority must be low, medium, high, or critical"
    assert state._safe_request_error(error_type(detail)) == (
        f"Central request failed ({error_type.__name__}): {detail}"
    )


@pytest.mark.parametrize("error_type", [ValueError, TypeError])
@pytest.mark.anyio
async def test_rpc_redacts_sensitive_direct_validation_types(
    error_type: type[Exception],
) -> None:
    leaked_url = "https://user:credential@central.example/mcp"
    detail = f"token must be valid at {leaked_url}"

    class DirectFailureClient(FakeClient):
        async def ticket_create(self) -> None:
            raise error_type(detail)

    state = LiveDashboard(
        fake_config(),
        client_class=DirectFailureClient,
        client_error_class=FakeClientError,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError) as caught:
            await state._rpc("ticket_create")
        message = str(caught.value)
        assert message == f"Central request failed ({error_type.__name__})"
        assert leaked_url not in message
        assert "credential" not in message
        assert "token" not in message
    finally:
        await state.stop()


@pytest.mark.parametrize("error_type", [ValueError, TypeError])
@pytest.mark.anyio
async def test_rpc_redacts_bare_hostname_in_direct_validation_types(
    error_type: type[Exception],
) -> None:
    leaked_host = "sensitive.central.example"
    detail = f"target must be {leaked_host}"

    class DirectHostFailureClient(FakeClient):
        async def ticket_create(self) -> None:
            raise error_type(detail)

    state = LiveDashboard(
        fake_config(),
        client_class=DirectHostFailureClient,
        client_error_class=FakeClientError,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError) as caught:
            await state._rpc("ticket_create")
        message = str(caught.value)
        assert message == f"Central request failed ({error_type.__name__})"
        assert leaked_host not in message
    finally:
        await state.stop()


@pytest.mark.parametrize("error_type", [ValueError, TypeError])
@pytest.mark.anyio
async def test_rpc_redacts_unrecognized_direct_ticket_status(
    error_type: type[Exception],
) -> None:
    leaked_value = "syntheticprivatevalue"

    class DirectStatusFailureClient(FakeClient):
        async def ticket_claim(self) -> None:
            raise error_type(f"ticket is {leaked_value}")

    state = LiveDashboard(
        fake_config(),
        client_class=DirectStatusFailureClient,
        client_error_class=FakeClientError,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError) as caught:
            await state._rpc("ticket_claim")
        message = str(caught.value)
        assert message == f"Central request failed ({error_type.__name__})"
        assert leaked_value not in message
    finally:
        await state.stop()


@pytest.mark.anyio
async def test_rpc_redacts_transport_and_auth_error_details() -> None:
    leaked_url = "https://user:credential@central.example/mcp"
    wrapped_auth = (
        "[TextContent(type='text', text='Error executing tool ticket_create: "
        f"unauthorized token at {leaked_url}', annotations=None, meta=None)]"
    )

    class AuthFailureClient(FakeClient):
        async def ticket_create(self) -> None:
            raise FakeClientError(wrapped_auth)

    state = LiveDashboard(
        fake_config(),
        client_class=AuthFailureClient,
        client_error_class=FakeClientError,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError) as caught:
            await state._rpc("ticket_create")
        message = str(caught.value)
        assert message == "Central request failed (FakeClientError)"
        assert leaked_url not in message
        assert "credential" not in message
        assert "token" not in message
    finally:
        await state.stop()

    transport = ConnectionError(f"lost connection to {leaked_url}")
    assert state._safe_connection_error(transport) == (
        "Central unavailable (ConnectionError)"
    )


@pytest.mark.anyio
async def test_app_reads_use_only_the_non_joining_pure_reader() -> None:
    calls: list[str] = []

    class ModelClientMustNotStart:
        def __init__(self, *_args: Any, **_kwargs: Any):
            calls.append("model-client-created")
            raise AssertionError("App read started the joined model client")

    class PureReader:
        def __init__(self, config: DashboardConfig):
            assert config.board_id == "board-personal-test"
            self.agent_name = config.agent_name

        async def __aenter__(self) -> "PureReader":
            calls.append("reader-enter")
            return self

        async def __aexit__(self, *_args: Any) -> None:
            calls.append("reader-exit")

        async def board_snapshot(self) -> dict[str, Any]:
            calls.append("board_snapshot")
            return {
                "board": {"board_id": "board-personal-test"},
                "agents": [],
                "latest_seq": 7,
                "snapshot_at": "2026-08-19T00:00:00Z",
            }

        async def board_status(self) -> dict[str, Any]:
            calls.append("board_status")
            return {
                "agents": [],
                "ticket_status_counts": {},
                "memory_type_counts": {},
                "visible_memory_count": 0,
                "latest_seq": 7,
            }

        async def ticket_list(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(f"ticket_list:{kwargs}")
            return {"tickets": [], "total_matching": 0, "latest_seq": 7}

        async def board_get_briefing(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(f"board_get_briefing:{kwargs}")
            return {"pinned_digest": [], "latest_handoff": None}

    bootstrap_calls: list[object] = []

    async def bootstrap(client: object) -> dict[str, Any]:
        bootstrap_calls.append(client)
        return {"ok": True}

    state = LiveDashboard(
        fake_config(),
        client_class=ModelClientMustNotStart,
        client_error_class=FakeClientError,
        post_join_hook=bootstrap,
        read_client_factory=PureReader,
    )

    async def healthy_probe() -> None:
        calls.append("probe")

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    first = await state.snapshot()
    second = await state.feed()

    assert first["data_mode"] == "live"
    assert first["event_cursor"] == 7
    assert first["events"] == []
    assert first["activity_scope"] == "local-model-tools"
    assert "does not read or acknowledge Central journal" in first["resync_notice"]
    assert second["count"] == 0
    assert state._worker_task is None
    assert bootstrap_calls == []
    assert "model-client-created" not in calls
    assert all("catchup" not in call for call in calls)
    assert calls.count("reader-enter") == 2
    assert calls.count("reader-exit") == 2


@pytest.mark.anyio
async def test_fleet_snapshot_groups_cross_board_seats_and_classifies_pool() -> None:
    registry = json.dumps(
        {
            "schema_version": 1,
            "projects": {
                "alpha": {
                    "board_id": "board-alpha",
                    "work_dir": "/tmp/alpha",
                    "status": "active",
                },
                "beta": {
                    "board_id": "board-beta",
                    "work_dir": "/tmp/beta",
                    "status": "active",
                },
                "paused": {
                    "board_id": "board-paused",
                    "work_dir": "/tmp/paused",
                    "status": "paused",
                },
            },
        }
    )
    snapshots = {
        "board-alpha": {
            "agents": [
                {
                    "agent_id": "AI-alpha-shared",
                    "principal_id": "PR-shared",
                    "agent_name": "shared-worker",
                    "last_activity_at": "2099-01-01T00:00:00Z",
                    "lifecycle_status": "active",
                },
                {
                    "agent_id": "AI-alpha-stale",
                    "principal_id": "PR-stale",
                    "agent_name": "stale-worker",
                    "last_activity_at": "2000-01-01T00:00:00Z",
                    "lifecycle_status": "active",
                },
            ],
            "tickets": [
                {
                    "ticket_id": "TK-alpha-claimed",
                    "status": "claimed",
                    "claimed_by_agent_id": "AI-alpha-shared",
                }
            ],
            "omitted_counts": {"agents": 0, "tickets": 0},
            "truncated": False,
        },
        "board-beta": {
            "agents": [
                {
                    "agent_id": "AI-beta-shared",
                    "principal_id": "PR-shared",
                    "agent_name": "shared-worker",
                    "last_activity_at": "2099-01-01T00:00:00Z",
                    "lifecycle_status": "active",
                },
                {
                    "agent_id": "AI-beta-available",
                    "principal_id": "PR-available",
                    "agent_name": "available-worker",
                    "last_activity_at": "2099-01-01T00:00:00Z",
                    "lifecycle_status": "active",
                },
            ],
            "tickets": [],
            "omitted_counts": {"agents": 0, "tickets": 0},
            "truncated": False,
        },
    }
    status_counts = {
        "board-alpha": {"open": 2, "claimed": 1, "submitted": 3},
        "board-beta": {"open": 4, "in_progress": 2, "submitted": 1},
    }
    calls: list[tuple[str, str, dict[str, Any]]] = []

    class FleetReader:
        def __init__(self, config: DashboardConfig):
            self.board_id = config.board_id

        async def __aenter__(self) -> "FleetReader":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def board_state_get(self, **kwargs: Any) -> dict[str, Any]:
            calls.append((self.board_id, "board_state_get", kwargs))
            return {"state": {"value": registry}}

        async def board_status(self) -> dict[str, Any]:
            calls.append((self.board_id, "board_status", {}))
            return {"ticket_status_counts": status_counts[self.board_id]}

        async def board_snapshot(self, **kwargs: Any) -> dict[str, Any]:
            calls.append((self.board_id, "board_snapshot", kwargs))
            return snapshots[self.board_id]

    state = LiveDashboard(
        fake_config(),
        client_class=FakeClient,
        client_error_class=FakeClientError,
        read_client_factory=FleetReader,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    payload = await state.fleet()

    assert payload["schema_version"] == 1
    assert payload["registry_warning"] is None
    assert payload["projects"] == [
        {
            "name": "alpha",
            "board_id": "board-alpha",
            "status": "active",
            "tickets_open": 2,
            "tickets_claimed": 1,
            "tickets_submitted": 3,
        },
        {
            "name": "beta",
            "board_id": "board-beta",
            "status": "active",
            "tickets_open": 4,
            "tickets_claimed": 2,
            "tickets_submitted": 1,
        },
    ]
    pool = {item["agent_name"]: item for item in payload["pool"]}
    assert pool["shared-worker"]["principal_id"] == "PR-shared"
    assert pool["shared-worker"]["pool_status"] == "busy"
    assert pool["shared-worker"]["seats"] == [
        {
            "board_id": "board-alpha",
            "project": "alpha",
            "live": True,
            "current_ticket_id": "TK-alpha-claimed",
        },
        {
            "board_id": "board-beta",
            "project": "beta",
            "live": True,
            "current_ticket_id": None,
        },
    ]
    assert pool["available-worker"]["pool_status"] == "available"
    assert pool["stale-worker"]["pool_status"] == "stale"
    assert payload["totals"] == {
        "agents": 3,
        "busy": 1,
        "available": 1,
        "stale": 1,
    }
    assert payload["truncation_counts"] == {
        "projects": 0,
        "boards": 0,
        "agents": 0,
        "tickets": 0,
        "pool": 0,
    }
    assert all(board != "board-paused" for board, _method, _args in calls)
    assert (
        "board-personal-test",
        "board_state_get",
        {"key": "project_registry"},
    ) in calls
    assert all(
        kwargs == {"limit": 500, "max_bytes": 250_000}
        for _board, method, kwargs in calls
        if method == "board_snapshot"
    )


@pytest.mark.anyio
async def test_fleet_snapshot_malformed_registry_degrades_to_profile_board() -> None:
    opened_boards: list[str] = []

    class FleetReader:
        def __init__(self, config: DashboardConfig):
            self.board_id = config.board_id

        async def __aenter__(self) -> "FleetReader":
            opened_boards.append(self.board_id)
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def board_state_get(self, **_kwargs: Any) -> dict[str, Any]:
            return {"state": {"value": "{"}}

        async def board_status(self) -> dict[str, Any]:
            return {"ticket_status_counts": {"open": 1}}

        async def board_snapshot(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "agents": [],
                "tickets": [],
                "omitted_counts": {"agents": 0, "tickets": 0},
                "truncated": False,
            }

    state = LiveDashboard(
        fake_config(),
        client_class=FakeClient,
        client_error_class=FakeClientError,
        read_client_factory=FleetReader,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    payload = await state.fleet()

    assert payload["registry_warning"] == (
        "project_registry unavailable or malformed; using the profile board only"
    )
    assert payload["projects"] == [
        {
            "name": "board-personal-test",
            "board_id": "board-personal-test",
            "status": "active",
            "tickets_open": 1,
            "tickets_claimed": 0,
            "tickets_submitted": 0,
        }
    ]
    assert payload["pool"] == []
    assert payload["totals"] == {
        "agents": 0,
        "busy": 0,
        "available": 0,
        "stale": 0,
    }
    assert opened_boards == ["board-personal-test", "board-personal-test"]


@pytest.mark.anyio
async def test_projection_flags_only_duplicate_active_agent_names() -> None:
    class ProjectionReader:
        async def board_status(self) -> dict[str, Any]:
            return {
                "agents": [
                    {
                        "agent_id": "AI-active-1",
                        "agent_name": "worker",
                        "status": "active",
                        "last_activity_at": "2099-01-01T00:00:00Z",
                    },
                    {
                        "agent_id": "AI-active-2",
                        "agent_name": "worker",
                        "status": "ACTIVE",
                        "last_activity_at": "2099-01-01T00:00:00Z",
                    },
                    {
                        "agent_id": "AI-idle",
                        "agent_name": "worker",
                        "status": "idle",
                        "last_activity_at": "2099-01-01T00:00:00Z",
                    },
                    {
                        "agent_id": "AI-solo",
                        "agent_name": "solo",
                        "status": "active",
                        "last_activity_at": "2099-01-01T00:00:00Z",
                    },
                    {
                        "agent_id": "AI-stale",
                        "agent_name": "worker",
                        "status": "active",
                        "last_activity_at": "2000-01-01T00:00:00Z",
                    },
                ],
                "latest_seq": 4,
            }

        async def ticket_list(self, **_kwargs: Any) -> dict[str, Any]:
            return {"tickets": [], "total_matching": 0, "latest_seq": 4}

        async def board_get_briefing(self, **_kwargs: Any) -> dict[str, Any]:
            return {"pinned_digest": [], "latest_handoff": None}

    state = LiveDashboard(
        fake_config(),
        client_class=FakeClient,
        client_error_class=FakeClientError,
    )
    await state._load_projection(
        ProjectionReader(),
        snapshot={
            "board": {"board_id": "board-personal-test"},
            "agents": [],
            "latest_seq": 4,
        },
    )

    assert state._projection is not None
    assert state._projection["agent_total"] == 5
    assert state._projection["agents_live"] == 4
    projected = {agent["id"]: agent for agent in state._projection["agents"]}
    first = projected["AI-active-1"]
    second = projected["AI-active-2"]
    assert first["duplicate"] is True
    assert second["duplicate"] is True
    assert first["duplicate_name"] is True
    assert second["duplicate_name"] is True
    assert first["suggested_name"] == (
        "worker-" + hashlib.sha256(b"AI-active-1").hexdigest()[:6]
    )
    assert second["suggested_name"] == (
        "worker-" + hashlib.sha256(b"AI-active-2").hexdigest()[:6]
    )
    assert first["suggested_name"] != second["suggested_name"]
    assert projected["AI-idle"]["duplicate"] is False
    assert projected["AI-idle"]["duplicate_name"] is True
    assert projected["AI-idle"]["suggested_name"] is None
    assert projected["AI-solo"]["duplicate"] is False
    assert projected["AI-solo"]["duplicate_name"] is False
    assert projected["AI-solo"]["suggested_name"] is None
    assert projected["AI-stale"]["stale"] is True
    assert projected["AI-stale"]["duplicate"] is False
    assert projected["AI-stale"]["duplicate_name"] is True
    assert projected["AI-stale"]["suggested_name"] is None


def test_duplicate_name_requires_distinct_agent_ids() -> None:
    agents = [
        {"id": "AI-same", "name": "reused", "status": "idle", "stale": False},
        {"id": "AI-same", "name": "reused", "status": "idle", "stale": False},
    ]

    LiveDashboard._flag_duplicate_agent_names(agents)

    assert all(agent["duplicate_name"] is False for agent in agents)


def test_agent_stale_threshold_is_sixty_minutes() -> None:
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(minutes=59)).isoformat()
    old = (now - timedelta(minutes=61)).isoformat()

    assert apps_server.AGENT_STALE_AFTER_MINUTES == 60
    assert LiveDashboard._agent_activity_age(fresh)[1] is False
    assert LiveDashboard._agent_activity_age(old)[1] is True


@pytest.mark.anyio
async def test_projection_adds_projects_without_extra_central_reads() -> None:
    calls: list[str] = []

    class ProjectionReader:
        async def board_status(self) -> dict[str, Any]:
            calls.append("board_status")
            return {
                "agents": [
                    {"agent_id": "AI-held", "agent_name": "held"},
                    {"agent_id": "AI-assigned", "agent_name": "assigned"},
                    {"agent_id": "AI-fallback", "agent_name": "fallback"},
                    {"agent_id": "AI-missing", "agent_name": "missing"},
                    {"agent_id": "AI-idle", "agent_name": "idle"},
                ],
                "latest_seq": 8,
            }

        async def ticket_list(self, **_kwargs: Any) -> dict[str, Any]:
            calls.append("ticket_list")
            tickets = [
                {
                    "ticket_id": "TK-held",
                    "status": "claimed",
                    "claimed_by_agent_id": "AI-held",
                    "claimed_at": "2026-08-24T10:00:00Z",
                    "target_url": "Pursers/packages/personal",
                },
                {
                    "ticket_id": "TK-assigned",
                    "status": "open",
                    "assigned_to_agent_id": "AI-assigned",
                    "updated_at": "2026-08-24T10:01:00Z",
                    "target_url": "Other-Project/docs",
                },
                {
                    "ticket_id": "TK-closed",
                    "status": "closed",
                    "claimed_by_agent_id": "AI-held",
                    "claimed_at": "2026-08-24T10:02:00Z",
                    "target_url": "stale/history",
                },
                {
                    "ticket_id": "TK-no-target",
                    "status": "claimed",
                    "claimed_by_agent_id": "AI-missing",
                    "target_url": None,
                },
                {
                    "ticket_id": "TK-fallback-history",
                    "status": "closed",
                    "claimed_by_agent_id": "AI-fallback",
                    "closed_at": "2026-08-24T10:02:00Z",
                    "target_url": "Historical/docs",
                },
            ]
            return {"tickets": tickets, "total_matching": 5, "latest_seq": 8}

        async def board_get_briefing(self, **_kwargs: Any) -> dict[str, Any]:
            calls.append("board_get_briefing")
            return {"pinned_digest": [], "latest_handoff": None}

    state = LiveDashboard(
        fake_config(),
        client_class=FakeClient,
        client_error_class=FakeClientError,
    )
    await state._load_projection(
        ProjectionReader(),
        snapshot={
            "board": {"board_id": "board-personal-test"},
            "agents": [],
            "latest_seq": 8,
        },
    )

    assert calls == ["board_status", "ticket_list", "board_get_briefing"]
    assert state._projection is not None
    tickets = {ticket["id"]: ticket for ticket in state._projection["tickets"]}
    assert tickets["TK-held"]["project"] == "pursers"
    assert tickets["TK-assigned"]["project"] == "other-project"
    assert tickets["TK-closed"]["project"] == "stale"
    assert tickets["TK-no-target"]["project"] is None

    agents = {agent["id"]: agent for agent in state._projection["agents"]}
    assert agents["AI-held"]["project"] == "pursers"
    assert agents["AI-assigned"]["project"] == "other-project"
    assert agents["AI-fallback"]["project"] == "historical"
    assert agents["AI-fallback"]["current_ticket_id"] is None
    assert agents["AI-missing"]["project"] is None
    assert agents["AI-missing"]["current_ticket_id"] == "TK-no-target"
    assert agents["AI-idle"]["project"] is None
    assert agents["AI-idle"]["current_ticket_id"] is None
    fallback = state._fallback_snapshot()
    assert all("project" in ticket for ticket in fallback["tickets"])
    assert all("project" in agent for agent in fallback["agents"])
    assert all("current_ticket_id" in agent for agent in fallback["agents"])
    assert all("duplicate_name" in agent for agent in fallback["agents"])


@pytest.mark.anyio
async def test_raw_reader_rejects_non_pure_tools_before_transport() -> None:
    transport_calls: list[tuple[str, dict[str, Any]]] = []

    class Transport:
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> object:
            transport_calls.append((name, arguments))
            return object()

    reader = object.__new__(apps_server.RawBoardReader)
    reader._client = Transport()
    reader.config = fake_config()
    reader._decode = lambda result: result

    allowed = await reader._call("board_state_get", {"key": "project_registry"})
    assert allowed is not None
    assert transport_calls == [
        (
            "board_state_get",
            {
                "board_id": "board-personal-test",
                "key": "project_registry",
            },
        )
    ]
    transport_calls.clear()
    with pytest.raises(RuntimeError, match="rejected a non-pure tool"):
        await reader._call("board_catchup", {"ack": False})
    assert transport_calls == []


@pytest.mark.anyio
async def test_hung_raw_view_entry_is_bounded_without_starting_model_worker() -> None:
    entered = asyncio.Event()

    class HangingReader:
        def __init__(self, _config: DashboardConfig):
            pass

        async def __aenter__(self) -> "HangingReader":
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def __aexit__(self, *_args: Any) -> None:
            return None

    config = DashboardConfig(
        "http://127.0.0.1:8766/mcp",
        "secret",
        "board-timeout",
        "agent-timeout",
        reconnect_min_s=1.0,
        reconnect_max_s=1.0,
        request_timeout_s=0.02,
    )
    state = LiveDashboard(
        config,
        client_class=FakeClient,
        client_error_class=FakeClientError,
        read_client_factory=HangingReader,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    payload = await asyncio.wait_for(state.snapshot(), timeout=0.2)
    assert entered.is_set()
    assert payload["data_mode"] == "demo"
    assert payload["feed_error"] == "Central unavailable (TimeoutError)"
    assert state._worker_task is None


@pytest.mark.anyio
async def test_hung_raw_view_exit_is_bounded_and_releases_read_lock() -> None:
    exited = asyncio.Event()

    class HangingExitReader:
        def __init__(self, config: DashboardConfig):
            self.agent_name = config.agent_name

        async def __aenter__(self) -> "HangingExitReader":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            exited.set()
            await asyncio.Event().wait()

        async def board_snapshot(self) -> dict[str, Any]:
            return {
                "board": {"board_id": "board-timeout"},
                "agents": [],
                "latest_seq": 1,
            }

        async def board_status(self) -> dict[str, Any]:
            return {"agents": [], "latest_seq": 1}

        async def ticket_list(self, **_kwargs: Any) -> dict[str, Any]:
            return {"tickets": [], "total_matching": 0, "latest_seq": 1}

        async def board_get_briefing(self, **_kwargs: Any) -> dict[str, Any]:
            return {"pinned_digest": [], "latest_handoff": None}

    class HealthyReader(HangingExitReader):
        async def __aexit__(self, *_args: Any) -> None:
            return None

    config = DashboardConfig(
        "http://127.0.0.1:8766/mcp",
        "secret",
        "board-timeout",
        "agent-timeout",
        reconnect_min_s=1.0,
        reconnect_max_s=1.0,
        request_timeout_s=0.02,
    )
    state = LiveDashboard(
        config,
        client_class=FakeClient,
        client_error_class=FakeClientError,
        read_client_factory=HangingExitReader,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    stale = await asyncio.wait_for(state.snapshot(), timeout=0.2)
    assert exited.is_set()
    assert stale["data_mode"] == "stale"
    assert stale["feed_error"] == "Central unavailable (TimeoutError)"

    state._read_client_factory = HealthyReader
    live = await asyncio.wait_for(state.snapshot(), timeout=0.2)
    assert live["data_mode"] == "live"
    assert state._worker_task is None


@pytest.mark.anyio
async def test_raw_reader_direct_close_is_bounded() -> None:
    class HangingStack:
        async def aclose(self) -> None:
            await asyncio.Event().wait()

    reader = object.__new__(apps_server.RawBoardReader)
    reader.config = DashboardConfig(
        "http://127.0.0.1:8766/mcp",
        "secret",
        "board-timeout",
        "agent-timeout",
        request_timeout_s=0.02,
    )
    reader._stack = HangingStack()
    reader._client = object()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(reader.__aexit__(None, None, None), timeout=0.2)
    assert reader._client is None
    assert reader._stack is None


@pytest.mark.anyio
async def test_model_join_bootstraps_without_loading_the_app_projection() -> None:
    order: list[tuple[str, object]] = []

    async def bootstrap(client: object) -> dict[str, Any]:
        order.append(("bootstrap", client))
        return {
            "ok": True,
            "changed": False,
            "event": {"id": "must-not-be-locally-injected"},
        }

    state = LiveDashboard(
        fake_config(),
        client_class=FakeClient,
        client_error_class=FakeClientError,
        post_join_hook=bootstrap,
    )

    async def project(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("model worker must not load the App projection")

    state._load_projection = project  # type: ignore[method-assign]
    first_client = object()
    second_client = object()
    await state._initialize_connection(first_client)
    await state._initialize_connection(second_client)

    assert order == [
        ("bootstrap", first_client),
        ("bootstrap", second_client),
    ]
    assert state._events == []


@pytest.mark.anyio
async def test_hung_model_client_join_is_bounded_and_unblocks_start() -> None:
    entered = asyncio.Event()

    class HangingJoinClient:
        def __init__(self, *_args: Any, **_kwargs: Any):
            pass

        async def __aenter__(self) -> "HangingJoinClient":
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def __aexit__(self, *_args: Any) -> None:
            return None

    config = DashboardConfig(
        "http://127.0.0.1:8766/mcp",
        "secret",
        "board-timeout",
        "agent-timeout",
        reconnect_min_s=1.0,
        reconnect_max_s=1.0,
        request_timeout_s=0.02,
    )
    state = LiveDashboard(
        config,
        client_class=HangingJoinClient,
        client_error_class=FakeClientError,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    try:
        await asyncio.wait_for(state.start(), timeout=0.2)
        assert entered.is_set()
        assert state._connected is False
        assert state._feed_error == "Central unavailable (TimeoutError)"
    finally:
        await state.stop()


@pytest.mark.anyio
async def test_hung_model_client_exit_does_not_block_dashboard_stop() -> None:
    exit_started = asyncio.Event()
    release_exit = asyncio.Event()

    class HangingExitClient:
        def __init__(self, *_args: Any, **_kwargs: Any):
            pass

        async def __aenter__(self) -> "HangingExitClient":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            exit_started.set()
            while not release_exit.is_set():
                try:
                    await release_exit.wait()
                except asyncio.CancelledError:
                    continue

    config = DashboardConfig(
        "http://127.0.0.1:8766/mcp",
        "secret",
        "board-timeout",
        "agent-timeout",
        reconnect_min_s=1.0,
        reconnect_max_s=1.0,
        request_timeout_s=0.02,
    )
    state = LiveDashboard(
        config,
        client_class=HangingExitClient,
        client_error_class=FakeClientError,
    )

    async def healthy_probe() -> None:
        return None

    state._probe_central = healthy_probe  # type: ignore[method-assign]
    await asyncio.wait_for(state.start(), timeout=0.2)
    await asyncio.wait_for(state.stop(), timeout=0.2)
    assert exit_started.is_set()
    assert state._feed_error == "Central unavailable (TimeoutError)"
    assert state._connected is False

    release_exit.set()
    assert state._worker_task is not None
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(state._worker_task, timeout=0.2)


@pytest.mark.anyio
async def test_app_reads_leave_sqlite_domain_journal_and_cursor_unchanged(
    tmp_path: Path,
) -> None:
    required = {"pursers-central": "0.1.0a13", "pursers-client": "0.1.0a11"}
    for distribution, version in required.items():
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            pytest.skip(f"{distribution} is not installed")
        if installed != version:
            pytest.skip(f"requires {distribution}=={version}, found {installed}")

    client_class, client_error_class = apps_server._load_board_client()
    package = sys.modules["pursers_client"]
    ensure_personal_profile = package.ensure_personal_profile
    resolve_personal_context = package.resolve_personal_context
    central_environment = package.central_environment

    project = tmp_path / "project"
    project.mkdir()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])
    profile = ensure_personal_profile(
        project,
        profiles_root=tmp_path / "profiles",
        port=port,
    )
    context = resolve_personal_context(profile, host="pytest", session="view-read")
    environment = os.environ.copy()
    environment.update(central_environment(profile))
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from pursers_central.pursers_central_runtime import main; main()",
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def wait_until_listening() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                _stdout, stderr = process.communicate()
                raise AssertionError(f"Central exited during startup: {stderr[-1000:]}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        raise AssertionError("Central did not start within 10 seconds")

    def sqlite_documents() -> tuple[tuple[str, str, int], ...]:
        connection = sqlite3.connect(profile.central_data_dir / "board.sqlite3")
        try:
            return tuple(
                connection.execute(
                    "SELECT path, doc, version FROM documents ORDER BY path"
                ).fetchall()
            )
        finally:
            connection.close()

    state: LiveDashboard | None = None
    try:
        wait_until_listening()
        async with client_class(
            context.central_url,
            context.capability_token,
            context.board_id,
            agent_name=context.agent_name,
        ) as provisioning_client:
            await provisioning_client.board_catchup(cursor=0, limit=100, ack=True)
        before = sqlite_documents()
        assert any(path.startswith("boards/") for path, _doc, _version in before)
        assert any(path.startswith("journals/") for path, _doc, _version in before)
        assert any(path.startswith("cursors/") for path, _doc, _version in before)

        state = LiveDashboard(
            config_from_personal_context(context),
            client_class=client_class,
            client_error_class=client_error_class,
        )
        await state.snapshot()
        await state.feed()
        snapshot = await state.snapshot()
        feed = await state.feed()
        after = sqlite_documents()

        assert snapshot["data_mode"] == "live"
        assert feed["data_mode"] == "live"
        assert state._worker_task is None
        assert before == after
    finally:
        if state is not None:
            await state.stop()
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


def test_source_has_no_legacy_connection_defaults_or_import_time_server() -> None:
    source = Path(apps_server.__file__).read_text(encoding="utf-8")
    assert "config_from_env" not in source
    assert "dev-principal-a" not in source
    assert "os.environ" not in source
    assert "mcp, dashboard =" not in source
    assert "importlib.resources.files" in source
    assert "import_verified_component(" in source
    assert ".board_catchup(" not in source
    assert source.count("await self.start()") == 1
    assert "version=PRODUCT_VERSION" in source


@pytest.mark.anyio
async def test_projection_joins_agents_to_their_current_ticket() -> None:
    agents = [
        {"agent_id": "AI-WORKING", "agent_name": "working-agent", "status": "active"},
        {"agent_id": "AI-ASSIGNED", "agent_name": "assigned-agent", "status": "active"},
        {"agent_id": "AI-IDLE", "agent_name": "idle-agent", "status": "active"},
    ]
    tickets = [
        {"ticket_id": "TK-OLDER", "title": "Older claim", "status": "claimed",
         "claimed_by_agent_id": "AI-WORKING", "claimed_at": "2026-08-24T01:00:00+00:00"},
        {"ticket_id": "TK-NEWER", "title": "Newer claim", "status": "submitted",
         "claimed_by_agent_id": "AI-WORKING", "claimed_at": "2026-08-24T02:00:00+00:00"},
        {"ticket_id": "TK-CLOSED", "title": "Closed newer claim", "status": "closed",
         "claimed_by_agent_id": "AI-WORKING", "claimed_at": "2026-08-24T03:00:00+00:00"},
        {"ticket_id": "TK-ASSIGNED", "title": "Assigned work", "status": "open",
         "assigned_to_agent_id": "AI-ASSIGNED", "updated_at": "2026-08-24T04:00:00+00:00"},
    ]

    class ProjectionReader:
        async def board_status(self) -> dict[str, Any]:
            return {"agents": agents, "latest_seq": 4}

        async def ticket_list(self, **_kwargs: Any) -> dict[str, Any]:
            return {"tickets": tickets, "total_matching": len(tickets), "latest_seq": 4}

        async def board_get_briefing(self, **_kwargs: Any) -> dict[str, Any]:
            return {"pinned_digest": [], "latest_handoff": None}

    state = LiveDashboard(
        fake_config(),
        client_class=FakeClient,
        client_error_class=FakeClientError,
    )
    await state._load_projection(
        ProjectionReader(),
        snapshot={
            "board": {"board_id": "board-personal-test"},
            "latest_seq": 4,
            "snapshot_at": "2026-08-24T04:00:00+00:00",
        },
    )

    assert state._projection is not None
    projected_agents = {item["id"]: item for item in state._projection["agents"]}
    assert set(projected_agents["AI-WORKING"]) == {
        "id", "name", "status", "role", "focus", "platform", "idle_minutes",
        "last_activity_at", "lease_expires_at", "stale", "duplicate", "suggested_name",
        "duplicate_name", "current_ticket_id", "current_ticket", "project",
    }
    assert projected_agents["AI-WORKING"]["current_ticket_id"] == "TK-NEWER"
    assert projected_agents["AI-WORKING"]["current_ticket"] == state._ticket_view(tickets[1])
    assert projected_agents["AI-ASSIGNED"]["current_ticket_id"] == "TK-ASSIGNED"
    assert projected_agents["AI-ASSIGNED"]["current_ticket"] == state._ticket_view(tickets[3])
    assert projected_agents["AI-IDLE"]["current_ticket_id"] is None
    assert projected_agents["AI-IDLE"]["current_ticket"] is None
