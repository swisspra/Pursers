from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest


DASHBOARD_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(DASHBOARD_DIR))
import fleet_dashboard as dashboard  # noqa: E402


def test_seats_route_renders_inventory_and_selector_contract(
    tmp_path: Path,
) -> None:
    """Run only with a verifier-owned Ego task space for real Chromium proof."""
    task_space_id = os.environ.get("PURSERS_EGO_TASK_SPACE_ID")
    ego_browser = shutil.which("ego-browser")
    if not task_space_id or not ego_browser:
        pytest.skip("requires PURSERS_EGO_TASK_SPACE_ID and ego-browser")

    class Cache:
        @staticmethod
        def labels() -> list[str]:
            return ["fixture"]

        @staticmethod
        def resolve_central(value: str | None) -> str:
            if value not in {None, "fixture"}:
                raise KeyError(value)
            return "fixture"

        @staticmethod
        def get(_central: str | None = None) -> dict:
            return {
                "generated_at": "2030-01-01T00:00:00Z",
                "boards": [],
                "agents": [],
                "pool_summary": {},
            }

        @staticmethod
        def get_project_registry(_central: str | None = None) -> dict:
            return {
                "registry": {"schema_version": 1, "projects": {}},
                "expected_sha256": "a" * 64,
            }

    class Seats:
        @staticmethod
        def seats() -> dict:
            return {
                "schema_version": 1,
                "seats": [
                    {
                        "host": "codex",
                        "role": "worker",
                        "name": "browser-seat-one",
                        "principal_label": "worker",
                        "tier_max": 2,
                        "can_review": False,
                        "can_work": True,
                        "skills": [],
                        "bridge_version": "1.0.0",
                        "doctor_summary": {},
                    },
                    {
                        "host": "codex",
                        "role": "reviewer",
                        "name": "browser-seat-two",
                        "principal_label": "review",
                        "tier_max": 2,
                        "can_review": True,
                        "can_work": False,
                        "skills": [],
                        "bridge_version": "1.0.0",
                        "doctor_summary": {},
                    },
                ],
                "discovered_configs": [],
                "import_review": {},
            }

        @staticmethod
        def bridge() -> dict:
            return {
                "installed_version": "1.0.0",
                "pinned_version": "1.0.0",
                "latest_pypi_version": "1.0.0",
                "status": "PASS",
            }

        @staticmethod
        def release_status() -> dict:
            return {}

        @staticmethod
        def registry(_fleet: dict, _registry: dict) -> dict:
            return {
                "boards": [],
                "projects": [],
                "seats": {
                    "browser-seat-one": {"status": "available"},
                    "browser-seat-two": {"status": "available"},
                },
            }

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache(), seat_manager=Seats())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/#/seats"
    try:
        script = f"""
const task = await taskSpace({int(task_space_id)});
const page = task.page("p1");
await page.goto({json.dumps(url)});
await page.waitForFunction(
  () => typeof window.route === "function" &&
    typeof window.navKind === "function" &&
    (navKind() !== "seats" ||
      document.querySelectorAll("[data-pursers-seat]").length === 2),
  undefined,
  {{timeout: 10000}},
);
const evidence = await page.evaluate(() => ({{
  route: route(),
  navKind: navKind(),
  seatLayouts: document.querySelectorAll(".seat-layout").length,
  seatRows: document.querySelectorAll(".seat-layout table tbody tr").length,
  taggedSeats: [...document.querySelectorAll("[data-pursers-seat]")].map(node => ({{
    seat: node.getAttribute("data-pursers-seat"),
    status: node.getAttribute("data-pursers-status"),
  }})),
  heading: document.querySelector("#central-sections h2")?.textContent || null,
  navByRoute: [
    "home", "projects", "work", "team", "approvals", "activity",
    "settings", "seats", "boards", "agents", "operations",
  ].map(kind => {{
    location.hash = `#/${{kind}}`;
    return [kind, navKind()];
  }}),
}}));
console.log(JSON.stringify(evidence));
"""
        completed = subprocess.run(
            [ego_browser, "nodejs", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    evidence = json.loads(completed.stderr.strip().splitlines()[-1])
    print(json.dumps(evidence, sort_keys=True))
    assert evidence == {
        "route": {"kind": "seats"},
        "navKind": "seats",
        "seatLayouts": 1,
        "seatRows": 2,
        "taggedSeats": [
            {"seat": "browser-seat-one", "status": "available"},
            {"seat": "browser-seat-two", "status": "available"},
        ],
        "heading": "Seats and dispatch",
        "navByRoute": [
            ["home", "home"],
            ["projects", "projects"],
            ["work", "work"],
            ["team", "team"],
            ["approvals", "approvals"],
            ["activity", "activity"],
            ["settings", "settings"],
            ["seats", "seats"],
            ["boards", "projects"],
            ["agents", "team"],
            ["operations", "settings"],
        ],
    }
