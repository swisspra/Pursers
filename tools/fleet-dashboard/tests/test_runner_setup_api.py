from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

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
