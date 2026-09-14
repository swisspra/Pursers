"""Disposable loopback server for the beta-blocking browser dry-run.

It serves tracked candidate UI artifacts and synthetic JSON only.  It has no
Central client and cannot access production state.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from beta_blocking_fixtures import load_fixture


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
WEBUI = REPO / "tools" / "aionui-extension" / "webui"
PERSONAL_HTML = REPO / "packages" / "personal" / "src" / "pursers_personal" / "resources" / "dashboard.html"


def _fleet_html() -> str:
    for source_root in (
        REPO / "packages" / "client" / "src",
        REPO / "packages" / "central" / "src",
    ):
        sys.path.insert(0, str(source_root))
    path = REPO / "tools" / "fleet-dashboard" / "fleet_dashboard.py"
    spec = importlib.util.spec_from_file_location("beta_fixture_fleet_dashboard", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load tracked Fleet dashboard")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.HTML


FLEET_HTML = _fleet_html()
STATUS = load_fixture("extension-status-loaded")
FLEET = load_fixture("fleet-board")


class Handler(BaseHTTPRequestHandler):
    server_version = "PursersSyntheticFixture/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: object, status: int = 200) -> None:
        self._send(status, json.dumps(value, separators=(",", ":")).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        route = urlsplit(self.path).path
        if route in {"/extension", "/extension/"}:
            return self._send(200, (WEBUI / "index.html").read_bytes(), "text/html; charset=utf-8")
        if route == "/extension/style.css":
            return self._send(200, (WEBUI / "style.css").read_bytes(), "text/css; charset=utf-8")
        if route == "/extension/app.js":
            return self._send(200, (WEBUI / "app.js").read_bytes(), "text/javascript; charset=utf-8")
        if route in {"/fleet", "/fleet/"}:
            return self._send(200, FLEET_HTML.encode(), "text/html; charset=utf-8")
        if route in {"/personal", "/personal/"}:
            return self._send(200, PERSONAL_HTML.read_bytes(), "text/html; charset=utf-8")
        if route == "/api/centrals":
            return self._json({"centrals": ["synthetic-loopback"], "default": "synthetic-loopback"})
        if route == "/api/fleet":
            return self._json(FLEET)
        if route == "/pursers/helper/status":
            return self._json(STATUS["helper_status"])
        if route == "/pursers/onboarding/status":
            return self._json(STATUS["onboarding_status"])
        if route == "/pursers/seat-lifecycle/status":
            return self._json({"ok": True, "outcome": "not_joined", "message": "synthetic fixture"})
        if route == "/pursers/team/status":
            return self._json({"ok": True, "members": []})
        if route == "/pursers/groups":
            return self._json({"ok": True, "revision": 1, "groups": [], "agents": []})
        if route == "/pursers/tickets":
            return self._json({"ok": True, "tickets": []})
        if route == "/pursers/results":
            return self._json({"ok": True, "results": []})
        return self._json({"ok": False, "error": "synthetic_route_not_found"}, 404)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(json.dumps({"origin": f"http://127.0.0.1:{server.server_port}"}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
