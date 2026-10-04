from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("runner_setup_api_dashboard", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


class Cache:
    @staticmethod
    def resolve_central(value: str | None) -> str:
        if value not in {None, "default"}:
            raise KeyError(value)
        return "default"

    def get_project_registry(self, central: str | None = None) -> dict[str, object]:
        del central
        return self.registry


class Runners:
    def __init__(self) -> None:
        self.plans: list[object] = []

    def catalog(self, *, target: str) -> dict[str, object]:
        return {"target": target, "agents": [], "compatibility": []}

    def refresh(self, *, target: str) -> dict[str, object]:
        return {"target": target, "agents": [], "compatibility": []}

    def plan(self, request: object) -> dict[str, object]:
        self.plans.append(request)
        return {"plan_id": "a" * 32, "digest": "b" * 64}

    def apply(self, **request: str) -> dict[str, object]:
        return {"ok": True, **request}


def request_json(url: str, path: str, payload: object, *, origin: str | None = None):
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    return urllib.request.urlopen(
        urllib.request.Request(
            url + path,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        ),
        timeout=5,
    )


def test_runner_routes_keep_loopback_same_origin_guard() -> None:
    runners = Runners()
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(Cache(), runner_manager=runners),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(
            base + "/api/config/runners", timeout=5
        ) as response:
            assert response.status == 200
            assert json.loads(response.read())["compatibility"]
        with request_json(
            base,
            "/api/config/runners/plan",
            {"preset": {}, "runtime": {}},
            origin=base,
        ) as response:
            assert response.status == 200
            assert json.loads(response.read())["plan_id"] == "a" * 32
        try:
            request_json(
                base,
                "/api/config/runners/refresh",
                {"target": "darwin-aarch64"},
                origin="https://attacker.invalid",
            )
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("cross-origin runner mutation was accepted")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_config_ui_exposes_guarded_native_and_acp_runner_flow() -> None:
    source = (MODULE_PATH.parent / "ui" / "assets" / "app.js").read_text(
        encoding="utf-8"
    )
    assert "Managed native/ACP runner setup" in source
    assert 'value="native-codex"' in source
    assert 'value="native-goose"' in source
    assert "/api/config/runners/refresh" in source
    assert "/api/config/runners/plan" in source
    assert "/api/config/runners/apply" in source
    assert source.rfind("finalRenderSeatsBeforeRunners") > source.rfind(
        "seatFormWithHostMode"
    )


def _configured_seat_manager(
    tmp_path: Path,
) -> tuple[dashboard.SeatConfigManager, Path, Path]:
    state = tmp_path / "state"
    seat_root = state / "seats" / "worker-13"
    repository = state / "clones" / "project"
    seat_root.mkdir(parents=True)
    repository.mkdir(parents=True)
    token = seat_root / "seat.jwt"
    token.write_text("header.payload.signature", encoding="utf-8")
    os.chmod(token, 0o600)
    manager = dashboard.SeatConfigManager(
        state / "seats.json",
        state_dir=state,
        latest_version=lambda: None,
    )
    manager.inventory.save(
        {
            "schema_version": 1,
            "seats": [
                {
                    "host": "codex",
                    "role": "worker",
                    "name": "worker-13",
                    "central_url": "https://central.example.invalid/mcp",
                    "home_board": "",
                    "token_file": str(token),
                    "ca_file": "",
                    "bridge_command": "pursers-wait-bridge",
                    "config_path": str(state / "config.toml"),
                    "seat_dir": str(seat_root),
                    "boards": "registry",
                    "registry_board": "pursers",
                }
            ],
        }
    )
    return manager, repository, token


def test_runner_plan_api_passes_only_server_owned_path_bindings(
    tmp_path: Path,
) -> None:
    seats, repository, token = _configured_seat_manager(tmp_path)
    cache = Cache()
    cache.registry = {
        "registry": {
            "projects": {
                "project": {
                    "status": "active",
                    "fleet": True,
                    "board_id": "project",
                    "fleet_clone_dir": str(repository),
                }
            }
        }
    }

    class BoundRunners(Runners):
        uses_runtime_path_bindings = True

        def plan(self, request: object, *, runtime_bindings=None) -> dict[str, object]:
            assert runtime_bindings.select("repository", str(repository)) == repository
            assert runtime_bindings.select("token_file", str(token)) == token
            with pytest.raises(ValueError, match="not authorized"):
                runtime_bindings.select("repository", str(tmp_path / "outside"))
            return super().plan(request)

    runners = BoundRunners()
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            cache,
            seat_manager=seats,
            runner_manager=runners,
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    payload = {
        "preset": {
            "seat": {
                "agent_name": "worker-13",
                "role": "worker",
                "board_id": "pursers",
            },
            "runner": {"kind": "acp"},
        },
        "runtime": {},
    }
    try:
        with request_json(
            base,
            "/api/config/runners/plan",
            payload,
            origin=base,
        ) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
