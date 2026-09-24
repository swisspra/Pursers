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


def test_autonomous_butler_browser_accessibility_and_conflict(
    tmp_path: Path,
) -> None:
    """Exercise the real rendered form and 409 state in verifier-owned Chromium."""
    task_space_id = os.environ.get("PURSERS_EGO_TASK_SPACE_ID")
    ego_browser = shutil.which("ego-browser")
    if not task_space_id or not ego_browser:
        pytest.skip("requires PURSERS_EGO_TASK_SPACE_ID and ego-browser")

    config = {
        "schema": "autonomous_butler_config_v1",
        "schema_version": 1,
        "board_id": "pursers",
        "revision": 4,
        "enabled": False,
        "host_runtime": {
            "agent_process_ceiling": 8,
            "total_process_ceiling": 10,
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
                    "secret_configured": True,
                    "tools": [{"name": "issue_get"}],
                    "resources": ["repo:issues"],
                }
            ],
        },
        "envelope": {
            "approved_template_ids": ["worker-template"],
            "max_capacity": {"worker": 4, "reviewer": 4, "acp_worker": 4},
            "max_host_concurrency": 8,
            "max_board_concurrency": 8,
            "max_budget": {
                "period": "day",
                "max_tokens": 100_000,
                "max_cost_microunits": 10_000_000,
                "max_external_calls": 1_000,
            },
        },
    }

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
                "boards": [
                    {
                        "board_id": "pursers",
                        "label": "Pursers",
                        "status": "ready",
                        "counts": {},
                        "tickets": [],
                    }
                ],
                "agents": [],
                "pool_summary": {},
            }

        @staticmethod
        def get_config(_central: str | None = None) -> dict:
            return {"config": {}, "expected_sha256": None}

        @staticmethod
        def get_project_registry(_central: str | None = None) -> dict:
            return {
                "registry": {"schema_version": 1, "projects": {}},
                "expected_sha256": "a" * 64,
            }

        @staticmethod
        def get_autonomous_butler(
            board_id: str, _central: str | None = None
        ) -> dict:
            assert board_id == "pursers"
            return {
                "schema_version": 1,
                "board_id": board_id,
                "revision": 4,
                "effective_state": "shadow",
                "config": config,
                "actual_state": {
                    "config_revision": 3,
                    "observed_at": "2030-01-01T00:00:00Z",
                    "capacity": {},
                    "connectors": [
                        {
                            "connector_id": "github",
                            "status": "degraded",
                        }
                    ],
                },
                "actual_state_available": False,
                "actual_state_stale": False,
                "actual_state_revision_mismatch": True,
                "actual_state_status": "revision_mismatch",
                "commands": [],
            }

        @staticmethod
        def save_autonomous_butler(
            board_id: str, _request: dict, _central: str | None = None
        ) -> dict:
            assert board_id == "pursers"
            raise dashboard.ConfigConflictError(
                "configuration changed; reload before saving"
            )

    class Seats:
        @staticmethod
        def registry(_fleet: dict, _registry: dict) -> dict:
            return {"boards": [], "projects": [], "seats": {}}

        @staticmethod
        def seats() -> dict:
            return {"schema_version": 1, "seats": [], "discovered_configs": []}

        @staticmethod
        def bridge() -> dict:
            return {}

        @staticmethod
        def release_status() -> dict:
            return {}

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache(), seat_manager=Seats())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/#/settings"
    try:
        script = f"""
const task = await taskSpace({int(task_space_id)});
const page = task.page("p1");
await page.cdp("Emulation.setDeviceMetricsOverride", {{
  width: 320, height: 900, deviceScaleFactor: 1, mobile: false,
}});
await page.goto({json.dumps(url)});
await page.waitForSelector(".autonomous-config-form", {{state: "visible", timeout: 10000}});
const save = ".autonomous-config-form button[type=submit]";
await page.evaluate(() => document.activeElement?.blur());
for (let index = 0; index < 80; index += 1) {{
  await page.keyboard.press("Tab");
  if (await page.evaluate(() => document.activeElement?.matches(".autonomous-config-form button[type=submit]"))) break;
}}
const before = await page.evaluate(() => {{
  const form = document.querySelector(".autonomous-config-form");
  const rgb = value => value.match(/[\\d.]+/g).slice(0, 3).map(Number);
  const luminance = value => {{
    const channels = rgb(value).map(channel => {{
      const normalized = channel / 255;
      return normalized <= .03928 ? normalized / 12.92 : ((normalized + .055) / 1.055) ** 2.4;
    }});
    return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
  }};
  const contrast = element => {{
    const style = getComputedStyle(element), foreground = luminance(style.color), background = luminance(style.backgroundColor);
    return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05);
  }};
  const controls = [...form.querySelectorAll("button")];
  const fields = [...form.querySelectorAll("input:not([type=hidden]),select")];
  const danger = form.querySelector(".danger-action");
  const focused = form.querySelector(":focus");
  return {{
    state: form.closest("[data-pursers-autonomous-board]").dataset.pursersState,
    observation: form.closest("[data-pursers-autonomous-board]").querySelector("[data-autonomous-observation]").textContent,
    fieldCount: fields.length,
    allFieldsLabeled: fields.every(field => field.labels?.length === 1),
    allButtons44: controls.every(button => button.getBoundingClientRect().height >= 44),
    checkboxTarget: form.querySelector("input[type=checkbox]").closest("label").getBoundingClientRect().height,
    autonomousDisabled: form.querySelector('option[value="autonomous"]').disabled,
    statusLive: form.querySelector('[role="status"]').getAttribute("aria-live"),
    focusOutline: focused ? parseFloat(getComputedStyle(focused).outlineWidth) : 0,
    dangerContrast: contrast(danger),
    noHorizontalOverflow: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    leakedSecret: document.body.textContent.includes("browser-secret-sentinel"),
  }};
}});
await page.press(save, "Enter");
await page.waitForFunction(
  () => document.querySelector(".autonomous-result")?.textContent.includes("reload before saving"),
  undefined,
  {{timeout: 10000}},
);
const after = await page.evaluate(() => ({{
  message: document.querySelector(".autonomous-result")?.textContent,
  role: document.querySelector(".autonomous-result")?.getAttribute("role"),
  buttonEnabled: !document.querySelector(".autonomous-config-form button[type=submit]").disabled,
}}));
await page.evaluate(() => {{ location.hash = "#/team"; }});
await page.waitForSelector('[data-pursers-autonomous-team="pursers"] [data-autonomous-observation="revision_mismatch"]', {{state: "visible", timeout: 10000}});
const team = await page.evaluate(() => ({{
  state: document.querySelector('[data-pursers-autonomous-team="pursers"] .status')?.textContent,
  observation: document.querySelector('[data-pursers-autonomous-team="pursers"] [data-autonomous-observation]')?.textContent,
  capacity: [...document.querySelectorAll('[data-pursers-autonomous-team="pursers"] .autonomous-capacity .meta')].map(node => node.textContent),
}}));
console.log(JSON.stringify({{before, after, team}}));
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
    assert evidence["before"]["state"] == "shadow"
    assert evidence["before"]["observation"] == (
        "Actual observation revision mismatch (observed 3, config 4)"
    )
    assert evidence["before"]["fieldCount"] == 21
    assert evidence["before"]["allFieldsLabeled"] is True
    assert evidence["before"]["allButtons44"] is True
    assert evidence["before"]["checkboxTarget"] >= 44
    assert evidence["before"]["autonomousDisabled"] is True
    assert evidence["before"]["statusLive"] == "polite"
    assert evidence["before"]["focusOutline"] >= 3
    assert evidence["before"]["dangerContrast"] >= 4.5
    assert evidence["before"]["noHorizontalOverflow"] is True
    assert evidence["before"]["leakedSecret"] is False
    assert evidence["after"] == {
        "message": "Save failed: configuration changed; reload before saving",
        "role": "status",
        "buttonEnabled": True,
    }
    assert evidence["team"] == {
        "state": "shadow",
        "observation": "Actual observation revision mismatch (observed 3, config 4)",
        "capacity": [
            "Actual revision mismatch (observed 3, config 4)",
            "Actual revision mismatch (observed 3, config 4)",
            "Actual revision mismatch (observed 3, config 4)",
        ],
    }
