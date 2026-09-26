from __future__ import annotations

import importlib.util
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "fleet_dashboard.py"
SPEC = importlib.util.spec_from_file_location("fleet_route_css_assets", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


def test_route_css_assets_are_existing_allowlisted_views(tmp_path: Path) -> None:
    views = tmp_path / "views"
    views.mkdir()
    (views / "projects.css").write_text(".projects{}")
    (views / "unknown.css").write_text(".unknown{}")

    assets = dashboard._route_css_asset_paths(tmp_path)

    assert assets == {
        "/ui/views/projects.css": (
            "text/css; charset=utf-8",
            views / "projects.css",
        )
    }


def test_absent_allowlisted_route_css_keeps_404() -> None:
    class Cache:
        @staticmethod
        def labels() -> list[str]:
            return ["fixture"]

        @staticmethod
        def resolve_central(value: str | None) -> str:
            if value not in {None, "fixture"}:
                raise KeyError(value)
            return "fixture"

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/ui/views/home.css"
            )
        assert caught.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
