#!/usr/bin/env python3
"""Run the Personal feed_error DOM/AX acceptance test in Ego Lite."""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import shutil
import subprocess
import threading
from pathlib import Path


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ego-browser", default=os.environ.get("EGO_BROWSER", "ego-browser"))
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    dashboard_root = Path(__file__).resolve().parents[1]
    dashboard = dashboard_root / "dashboard.html"
    script = Path(__file__).with_name("feed-error.browser.mjs")
    if not dashboard.is_file():
        parser.error("dashboard.html is missing; run `NODE_ENV= npm run build` first")
    ego_browser = shutil.which(args.ego_browser) if not Path(args.ego_browser).is_absolute() else args.ego_browser
    if not ego_browser or not Path(ego_browser).is_file():
        parser.error("ego-browser executable is unavailable")

    handler = functools.partial(QuietHandler, directory=str(dashboard_root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        environment = os.environ.copy()
        environment["PURSERS_DASHBOARD_TEST_URL"] = (
            f"http://127.0.0.1:{server.server_port}/tests/feed-error-host.html"
        )
        completed = subprocess.run(
            [str(ego_browser), "nodejs"],
            input=script.read_text(encoding="utf-8"),
            text=True,
            capture_output=True,
            timeout=args.timeout,
            env=environment,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=__import__("sys").stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
