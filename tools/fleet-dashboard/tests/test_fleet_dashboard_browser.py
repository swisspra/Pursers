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


def test_delivery_shared_layer_editor_preserves_scope_and_does_not_copy_repository_values() -> None:
    """Exercise the real UI when a shared policy preview rerenders the editor."""
    task_space_id = os.environ.get("PURSERS_EGO_TASK_SPACE_ID")
    ego_browser = shutil.which("ego-browser")
    if not task_space_id or not ego_browser:
        pytest.skip("requires PURSERS_EGO_TASK_SPACE_ID and ego-browser")

    requests: list[dict] = []

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
            return {"generated_at": "2030-01-01T00:00:00Z", "boards": [], "agents": []}

        @staticmethod
        def get_project_registry(_central: str | None = None) -> dict:
            return {"registry": {"schema_version": 1, "projects": {}}, "expected_sha256": "a" * 64}

        @staticmethod
        def get_project_delivery_settings(_central: str | None = None) -> dict:
            effective = {
                "mode": "branch_only", "mapped_base": "dev", "integration_branch": "alpha-delivery",
                "snapshot_branch_prefix": "alpha-snap", "final_pr_target": None,
                "release_trigger": {"kind": "ready"}, "pr_update": "rolling",
                "auto_integrate": False, "collection_paused": False, "final_merge": "manual",
                "validation": {"test_commands": ["pytest -q alpha"], "required_reviewers": 1,
                               "independent_review": True, "require_upstream_policies": True},
                "conflict_policy": "pause",
            }
            return {"central": "fixture", "projects": [{
                "name": "alpha", "board_id": "alpha", "status": "active",
                "integration_ref": "dev", "repository_configured": True,
                "delivery_policy": effective,
                "delivery_policy_overrides": {
                    "mode": "branch_only", "integration_branch": "alpha-delivery",
                    "snapshot_branch_prefix": "alpha-snap", "final_pr_target": None,
                    "validation": {"test_commands": ["pytest -q alpha"]},
                },
                "delivery_policy_group": "backend",
                "delivery_policy_provenance": {"mode": "repository:alpha"},
                "delivery_runtime": {"ready": False, "blockers": ["branch_only runtime is not installed"]},
                "delivery_policy_groups": ["backend"],
                "delivery_policy_layers": {
                    "global": {"snapshot_branch_prefix": "global-snap"},
                    "groups": {"backend": {"conflict_policy": "repair_then_review"}},
                    "repository": {
                        "mode": "branch_only", "integration_branch": "alpha-delivery",
                        "snapshot_branch_prefix": "alpha-snap", "final_pr_target": None,
                        "validation": {"test_commands": ["pytest -q alpha"]},
                    },
                },
            }]}

        @staticmethod
        def plan_project_lifecycle(request: dict, central: str | None = None) -> dict:
            requests.append(json.loads(json.dumps(request)))
            label = "global defaults" if request["scope"] == "global" else request["name"]
            return {
                "schema_version": 1, "kind": "project-delivery-policy", "central": central or "fixture",
                "project": label, "scope": request["scope"], "blocked": False,
                "blockers": [], "warnings": [], "preserved": [], "rollback": [],
                "operations": [], "affected_projects": [{"project": "alpha"}],
                "delivery_policy": {**request["delivery_policy"], "mode": "per_ticket_pr",
                                    "mapped_base": "dev", "release_trigger": {"kind": "ready"}},
                "expires_at": "2030-01-01T00:10:00Z", "confirmation": f"CONFIGURE {label}",
                "plan_id": "plan-1", "plan_digest": "b" * 64,
            }

    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(Cache()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/#/projects"
    try:
        script = f"""
const task = await taskSpace({int(task_space_id)});
const page = task.page("p1");
let stage = 'load';
try {{
await page.goto({json.dumps(url)});
await page.waitForFunction(() => document.querySelector('#project-delivery-form select[name="name"]')?.value === 'alpha', undefined, {{timeout: 10000}});
stage = 'global-layer';
await page.selectOption('#project-delivery-form select[name="scope"]', 'global');
await page.waitForFunction(() => document.querySelector('#project-delivery-form select[name="scope"]')?.value === 'global', undefined, {{timeout: 10000}});
const globalBefore = await page.evaluate(() => {{
  const form = document.querySelector('#project-delivery-form');
  return {{scope: form.elements.scope.value, snapshot: form.elements.snapshot_branch_prefix.value,
    mode: form.elements.mode.value, integration: form.elements.integration_branch.value,
    commands: form.elements.test_commands.value, activateDisabled: form.elements.activate.disabled}};
}});
await page.fill('#project-delivery-form input[name="snapshot_branch_prefix"]', 'global-edited');
await page.click('#project-delivery-form button[type="submit"]');
await page.waitForSelector('.projects-lifecycle-preview', {{state: 'visible', timeout: 10000}});
stage = 'group-layer';
const globalAfter = await page.evaluate(() => {{
  const form = document.querySelector('#project-delivery-form');
  return {{scope: form.elements.scope.value, snapshot: form.elements.snapshot_branch_prefix.value,
    mode: form.elements.mode.value, integration: form.elements.integration_branch.value,
    commands: form.elements.test_commands.value}};
}});
await page.selectOption('#project-delivery-form select[name="scope"]', 'group');
await page.waitForFunction(() => document.querySelector('#project-delivery-form select[name="scope"]')?.value === 'group', undefined, {{timeout: 10000}});
const groupLayer = await page.evaluate(() => {{
  const form = document.querySelector('#project-delivery-form');
  return {{scope: form.elements.scope.value, group: form.elements.delivery_policy_group.value,
    conflict: form.elements.conflict_policy.value, snapshot: form.elements.snapshot_branch_prefix.value,
    mode: form.elements.mode.value, integration: form.elements.integration_branch.value,
    commands: form.elements.test_commands.value}};
}});
await page.selectOption('#project-delivery-form select[name="scope"]', 'repository');
await page.waitForFunction(() => document.querySelector('#project-delivery-form select[name="scope"]')?.value === 'repository' && document.querySelector('#project-delivery-form select[name="mode"]')?.value === 'branch_only', undefined, {{timeout: 10000}});
stage = 'repository-draft';
await page.selectOption('#project-delivery-form select[name="preset"]', 'receive-batches');
await page.fill('#project-delivery-form input[name="snapshot_branch_prefix"]', 'repo-draft');
await page.evaluate(() => {{
  const field = document.querySelector('#project-delivery-form textarea[name="test_commands"]');
  field.value = 'pytest -q edited';
  field.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText', data: 'pytest -q edited'}}));
}});
await page.click('#project-delivery-form input[name="activate"]');
await page.click('#project-delivery-form button[type="submit"]');
await page.waitForFunction(() => document.querySelector('.projects-lifecycle-preview h2')?.textContent.includes('alpha') && document.querySelector('#project-delivery-form input[name="snapshot_branch_prefix"]')?.value === 'repo-draft', undefined, {{timeout: 10000}});
const repositoryAfter = await page.evaluate(() => {{
  const form = document.querySelector('#project-delivery-form');
  return {{scope: form.elements.scope.value, preset: form.elements.preset.value,
    activate: form.elements.activate.checked, snapshot: form.elements.snapshot_branch_prefix.value,
    mode: form.elements.mode.value, integration: form.elements.integration_branch.value,
    commands: form.elements.test_commands.value}};
}});
console.log(JSON.stringify({{globalBefore, globalAfter, groupLayer, repositoryAfter}}));
}} catch (error) {{
  const state = await page.evaluate(() => {{ const form = document.querySelector('#project-delivery-form'); return form ? {{scope: form.elements.scope.value, mode: form.elements.mode.value, preset: form.elements.preset.value, snapshot: form.elements.snapshot_branch_prefix.value, activate: form.elements.activate.checked, preview: document.querySelector('.projects-lifecycle-preview h2')?.textContent || null}} : {{form: false}}; }});
  console.log(JSON.stringify({{stage, state, error: String(error)}}));
  throw error;
}}
"""
        completed = subprocess.run(
            [ego_browser, "nodejs", "-e", script], check=False, capture_output=True,
            text=True, timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stderr.strip().splitlines()[-1])
    assert evidence == {
        "globalBefore": {"scope": "global", "snapshot": "global-snap", "mode": "",
                         "integration": "", "commands": "", "activateDisabled": True},
        "globalAfter": {"scope": "global", "snapshot": "global-edited", "mode": "",
                        "integration": "", "commands": ""},
        "groupLayer": {"scope": "group", "group": "backend",
                       "conflict": "repair_then_review", "snapshot": "", "mode": "",
                       "integration": "", "commands": ""},
        "repositoryAfter": {"scope": "repository", "preset": "receive-batches",
                            "activate": True, "snapshot": "repo-draft", "mode": "batch_pr",
                            "integration": "alpha-delivery", "commands": "pytest -q edited"},
    }
    assert requests == [
        {"action": "delivery", "scope": "global", "name": "alpha",
         "delivery_policy_group": "backend", "activate": False,
         "delivery_policy": {"snapshot_branch_prefix": "global-edited"}},
        {"action": "delivery", "scope": "repository", "name": "alpha",
         "delivery_policy_group": "backend", "activate": True,
         "delivery_policy": {
             "mode": "batch_pr", "integration_branch": "alpha-delivery",
             "snapshot_branch_prefix": "repo-draft", "release_trigger": {"kind": "ready"},
             "pr_update": "freeze_on_ready",
             "validation": {"test_commands": ["pytest -q edited"]},
         }},
    ]


def test_projects_route_loads_owned_css_in_real_browser() -> None:
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
                "boards": [
                    {
                        "board_id": "sample-board",
                        "label": "Sample project",
                        "status": "ready",
                        "counts": {"open": 2, "in_progress": 1},
                        "tickets": [],
                    }
                ],
                "agents": [],
                "pool_summary": {},
            }

    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.make_handler(Cache())
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/#/projects"
    try:
        script = f"""
const task = await taskSpace({int(task_space_id)});
const page = task.page("p1");
await page.goto({json.dumps(url)});
await page.waitForFunction(
  () => document.querySelectorAll("[data-projects-card]").length === 1 &&
    document.styleSheets.length > 1,
  undefined,
  {{timeout: 10000}},
);
const evidence = await page.evaluate(() => {{
  const card = document.querySelector("[data-projects-card]");
  const style = document.querySelector('link[data-fleet-view-style="projects"]');
  return {{
    className: card.className,
    board: card.getAttribute("data-board-id"),
    display: getComputedStyle(card).display,
    stylePath: new URL(style.href).pathname,
  }};
}});
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
    assert evidence == {
        "className": "board-card",
        "board": "sample-board",
        "display": "grid",
        "stylePath": "/ui/views/projects.css",
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


def test_refresh_cycles_preserve_reader_state_at_desktop_and_mobile(
    tmp_path: Path,
) -> None:
    """The released innerHTML refresh loses this state after the first cycle."""
    task_space_id = os.environ.get("PURSERS_EGO_TASK_SPACE_ID")
    ego_browser = shutil.which("ego-browser")
    if not task_space_id or not ego_browser:
        pytest.skip("requires PURSERS_EGO_TASK_SPACE_ID and ego-browser")
    evidence_dir = Path(
        os.environ.get("PURSERS_BROWSER_EVIDENCE_DIR", str(tmp_path / "screenshots"))
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)

    class Cache:
        def __init__(self) -> None:
            self.fleet_revision = 0
            self.detail_revision = 0
            self.config_revision = 0
            self.lock = threading.Lock()

        @staticmethod
        def labels() -> list[str]:
            return ["fixture"]

        @staticmethod
        def resolve_central(value: str | None) -> str:
            if value not in {None, "fixture"}:
                raise KeyError(value)
            return "fixture"

        def get(self, central: str | None = None) -> dict:
            self.resolve_central(central)
            with self.lock:
                self.fleet_revision += 1
                revision = self.fleet_revision
            agents = []
            for index in range(35):
                name = f"browser-seat-{index:02d}"
                agents.append(
                    {
                        "agent_name": name,
                        "agent_id": f"AI-browser-{index:02d}",
                        "principal_id": f"PR-browser-{index:02d}",
                        "pool_status": "available",
                        "boards": ["pursers"],
                        "board_scope": ["pursers"],
                        "duplicate_name": False,
                        "last_seen": "2030-01-01T00:00:00Z",
                        "seats": [
                            {
                                "board_id": "pursers",
                                "project": "Fixture",
                                "role": "worker",
                                "capabilities": {
                                    "tier_max": 2,
                                    "host": "browser",
                                    "can_work": True,
                                    "can_review": False,
                                },
                            }
                        ],
                    }
                )
            return {
                "central": "fixture",
                "generated_at": f"2030-01-01T00:00:{revision % 60:02d}Z",
                "boards": [
                    {
                        "board_id": "pursers",
                        "label": "Fixture Board",
                        "status": "ready",
                        "counts": {"open": revision},
                        "tickets": [
                            {
                                "id": "TK-live",
                                "title": f"Live change {revision}",
                                "status": "open",
                                "claimed_by": None,
                                "description": "Refresh state regression fixture",
                            }
                        ],
                    }
                ],
                "agents": agents,
                "inactive_agents": [],
                "pool_summary": {
                    "online": 35,
                    "busy": 0,
                    "available": 35,
                    "connected": 0,
                    "stale": 0,
                    "unknown_model": 0,
                },
            }

        def get_board(self, board_id: str, central: str | None = None) -> dict:
            self.resolve_central(central)
            if board_id != "pursers":
                raise KeyError(board_id)
            with self.lock:
                self.detail_revision += 1
                revision = self.detail_revision
            result = dashboard.project_board_detail(
                {
                    "board_id": "pursers",
                    "label": "Fixture Board",
                    "snapshot": {
                        "tickets": [
                            {
                                "ticket_id": "TK-live",
                                "title": f"Live ticket {revision}",
                                "description": "Long reader state " * 80,
                                "status": "open",
                                "required_fields": ["test_output"],
                                "updated_at": f"2030-01-01T00:00:{revision % 60:02d}Z",
                            }
                        ],
                        "total_counts": {"tickets": 1},
                    },
                    "events": [
                        {
                            "seq": revision,
                            "kind": "ticket_status_changed",
                            "ticket_id": "TK-live",
                            "occurred_at": f"2030-01-01T00:00:{revision % 60:02d}Z",
                        }
                    ],
                }
            )
            result.update(
                {
                    "central": "fixture",
                    "generated_at": f"2030-01-01T00:00:{revision % 60:02d}Z",
                }
            )
            return result

        def get_config(self, central: str | None = None) -> dict:
            self.resolve_central(central)
            with self.lock:
                self.config_revision += 1
                revision = self.config_revision
            return {
                "config": {
                    "board_butler": {
                        "schema_version": 1,
                        "global": {
                            "drafting": {
                                "endpoint_ref": "https://provider.example.invalid/v1",
                                "model": f"server-model-{revision}",
                            }
                        },
                        "projects": {},
                        "boards": {},
                    }
                },
                "expected_sha256": "a" * 64,
            }

        @staticmethod
        def get_project_registry(central: str | None = None) -> dict:
            Cache.resolve_central(central)
            return {
                "registry": {"schema_version": 1, "projects": {}},
                "expected_sha256": "b" * 64,
            }

    cache = Cache()
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        dashboard.make_handler(
            cache,
            worker_manager=dashboard.WorkerManager(tmp_path / "workers"),
            butler_manager=dashboard.ButlerSettingsManager(tmp_path / "butler"),
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        script = f"""
const task = await taskSpace({int(task_space_id)});
const page = task.page("p1");
const results = [];
for (const viewport of [{{width:1440,height:900}},{{width:390,height:844}}]) {{
  await page.cdp("Emulation.setDeviceMetricsOverride", {{...viewport,deviceScaleFactor:1,mobile:false}});
  await page.goto({json.dumps(url)} + "#/home");
  await page.waitForSelector("#central-sections .page-head", {{state:"visible",timeout:10000}});
  const home = await page.evaluate(async () => {{
    const before = document.querySelector("#state").textContent;
    const action = document.querySelector("#central-sections a.primary-action");
    action?.focus();
    for (let index=0;index<3;index+=1) await refreshFleet(2000);
    return {{route:location.hash,before,after:document.querySelector("#state").textContent,focused:document.activeElement?.getAttribute("href")||null}};
  }});
  home.screenshot = await page.screenshot({{
    path: {json.dumps(str(evidence_dir))} + `/fleet-home-${{viewport.width}}x${{viewport.height}}.png`
  }});
  const workflowRoutes = [];
  for (const theme of ["light", "dark"]) {{
    await page.evaluate(value => {{ document.documentElement.dataset.theme=value; }}, theme);
    await page.evaluate(() => {{ location.hash="#/work"; }});
    await page.waitForSelector("#central-sections [data-work-layout]", {{state:"attached",timeout:10000}});
    const work = await page.evaluate(() => ({{
      route: location.hash,
      kanban: getComputedStyle(document.querySelector('[data-work-layout="kanban"]')).display,
      list: getComputedStyle(document.querySelector('[data-work-layout="list"]')).display,
      lanes: document.querySelectorAll('[data-work-lane]').length,
      tickets: document.querySelectorAll('[data-pursers-ticket="TK-live"]').length,
    }}));
    work.screenshot = await page.screenshot({{path:{json.dumps(str(evidence_dir))}+`/fleet-work-${{theme}}-${{viewport.width}}x${{viewport.height}}.png`}});
    await page.evaluate(() => {{ location.hash="#/inbox"; }});
    await page.waitForSelector("#central-sections .inbox-master-detail", {{state:"visible",timeout:10000}});
    const inbox = await page.evaluate(() => {{
      const root=document.querySelector('.inbox-master-detail'), first=document.querySelector('[data-inbox-select]');
      const before=root.dataset.inboxMobileView;
      if (innerWidth<=800 && first) first.click();
      const selected=document.querySelector('.inbox-master-detail').dataset.inboxMobileView;
      if (innerWidth<=800) document.querySelector('[data-inbox-back]')?.click();
      return {{route:location.hash,before,selected,after:document.querySelector('.inbox-master-detail').dataset.inboxMobileView,source:first?.dataset.inboxSource||null}};
    }});
    inbox.screenshot = await page.screenshot({{path:{json.dumps(str(evidence_dir))}+`/fleet-inbox-${{theme}}-${{viewport.width}}x${{viewport.height}}.png`}});
    workflowRoutes.push({{theme,work,inbox}});
  }}
  await page.evaluate(() => {{ location.hash="#/team"; }});
  await page.waitForFunction(() => document.querySelectorAll(".agent-card").length===35, undefined, {{timeout:10000}});
  const team = await page.evaluate(async () => {{
    const disclosure = document.querySelector(".agent-card:nth-of-type(18) details.agent-ops");
    disclosure.open = true;
    const summary = disclosure.querySelector("summary");
    const focusTarget = getComputedStyle(disclosure).display==='none' ? document.querySelector('[data-agent-filter]') : summary;
    focusTarget.focus();
    window.scrollTo(0, Math.min(900, document.documentElement.scrollHeight-innerHeight));
    const beforeY = window.scrollY;
    const focusTrace = [document.activeElement?.tagName||null];
    for (let index=0;index<3;index+=1) {{ await refreshFleet(2000); focusTrace.push(document.activeElement?.tagName||null); }}
    const current = [...document.querySelectorAll("details.agent-ops")].find(node => node.open);
    return {{route:location.hash,beforeY,afterY:window.scrollY,open:!!current,focused:document.activeElement?.tagName||null,focusExpected:focusTarget.tagName,focusTrace,count:document.querySelectorAll(".agent-card").length}};
  }});
  await page.evaluate(() => {{ location.hash="#/settings"; }});
  await page.waitForSelector("#central-sections .settings-groups", {{state:"visible",timeout:10000}});
  await page.waitForFunction(() => document.querySelector("#butler-settings-form") || document.querySelector(".butler-settings.error"), undefined, {{timeout:10000}});
  const settings = await page.evaluate(async () => {{
    let input = document.querySelector("#butler-settings-form input[name=model]");
    const cleanBefore = input?.value||null;
    for (let index=0;index<3;index+=1) {{ await refreshFleet(2000); await refreshHubExtras(); }}
    input = document.querySelector("#butler-settings-form input[name=model]");
    const cleanAfter = input?.value||null, cleanServer = butlerData?.model||null, cleanDirty = input?.form?.dataset.dirty||null;
    if (input) {{ input.value="unsaved-browser-model";input.dispatchEvent(new Event("input",{{bubbles:true}}));input.focus(); }}
    for (let index=0;index<3;index+=1) {{ await refreshFleet(2000); await refreshHubExtras(); }}
    const restored = document.querySelector("#butler-settings-form input[name=model]");
    return {{route:location.hash,cleanBefore,cleanAfter,cleanServer,cleanDirty,value:restored?.value||null,latestServer:butlerData?.model||null,focused:document.activeElement===restored,dirty:restored?.form?.dataset.dirty||null}};
  }});
  if (settings.cleanAfter===settings.cleanBefore || settings.cleanAfter!==settings.cleanServer) throw new Error(`clean Butler control masked server refresh: ${{JSON.stringify(settings)}}`);
  await page.evaluate(() => {{ location.hash="#/central/fixture/board/pursers/tickets?ticket=TK-live"; }});
  await page.waitForFunction(() => document.querySelector('[data-ticket="TK-live"]') || document.querySelector("#detail-view .error"), undefined, {{timeout:10000}});
  const detailReady = await page.evaluate(() => ({{ready:!!document.querySelector('[data-ticket="TK-live"]'),hash:location.hash,parsed:route(),html:document.querySelector("#detail-view").innerHTML.slice(0,800)}}));
  if (!detailReady.ready) throw new Error(`detail fixture failed: ${{JSON.stringify(detailReady)}}`);
  const detail = await page.evaluate(async () => {{
    let ticket = document.querySelector('[data-ticket="TK-live"]');
    ticket.open = true;
    ticket.querySelector("summary").focus();
    let overflowStyle = document.querySelector("#refresh-overflow-fixture");
    if (!overflowStyle) {{ overflowStyle=document.createElement("style");overflowStyle.id="refresh-overflow-fixture";overflowStyle.textContent="#detail-view .table-scroll table{{min-width:1800px}}";document.head.appendChild(overflowStyle); }}
    const scroller = document.querySelector("#detail-view .table-scroll");
    if (scroller) scroller.scrollLeft = 35;
    window.scrollTo(0, Math.min(420, document.documentElement.scrollHeight-innerHeight));
    const beforeY = window.scrollY, beforeX = scroller?.scrollLeft||0;
    for (let index=0;index<3;index+=1) await refreshDetail();
    ticket = document.querySelector('[data-ticket="TK-live"]');
    const reading = {{route:location.hash,open:ticket.open,focused:document.activeElement===ticket.querySelector("summary"),beforeY,afterY:window.scrollY,beforeX,afterX:document.querySelector("#detail-view .table-scroll")?.scrollLeft||0,revision:detailData.generated_at}};
    const draft = document.querySelector("#intake-form textarea");
    draft.value = "unsaved form survives three refreshes";
    draft.dispatchEvent(new Event("input",{{bubbles:true}}));
    draft.focus();
    const revisionBefore = detailData.generated_at;
    for (let index=0;index<3;index+=1) await refreshDetail();
    const restored = document.querySelector("#intake-form textarea");
    return {{...reading,draft:restored.value,draftFocused:document.activeElement===restored,dirty:restored.form.dataset.dirty||null,networkAdvanced:detailData.generated_at!==revisionBefore}};
  }});
  results.push({{viewport,home,workflowRoutes,team,settings,detail}});
}}
console.log(JSON.stringify(results));
"""
        completed = subprocess.run(
            [ego_browser, "nodejs", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stderr.strip().splitlines()[-1])
    print(json.dumps(evidence, sort_keys=True))
    assert [row["viewport"] for row in evidence] == [
        {"width": 1440, "height": 900},
        {"width": 390, "height": 844},
    ]
    for row in evidence:
        assert row["home"]["route"] == "#/home"
        assert row["home"]["after"] != row["home"]["before"]
        assert row["home"]["focused"] is not None
        screenshot = Path(row["home"]["screenshot"])
        assert screenshot.parent == evidence_dir
        assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        for workflow in row["workflowRoutes"]:
            assert workflow["theme"] in {"light", "dark"}
            assert workflow["work"]["route"] == "#/work"
            assert workflow["work"]["lanes"] >= 1
            assert workflow["work"]["tickets"] >= 1
            if row["viewport"]["width"] <= 800:
                assert workflow["work"]["kanban"] == "none"
                assert workflow["work"]["list"] == "block"
                assert workflow["inbox"]["before"] == "list"
                assert workflow["inbox"]["selected"] == "detail"
                assert workflow["inbox"]["after"] == "list"
            else:
                assert workflow["work"]["kanban"] == "grid"
                assert workflow["work"]["list"] == "none"
                assert workflow["inbox"]["before"] == "detail"
            for surface in (workflow["work"], workflow["inbox"]):
                image = Path(surface["screenshot"])
                assert image.parent == evidence_dir
                assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert row["team"]["route"] == "#/team"
        assert row["team"]["count"] == 35
        assert row["team"]["open"] is True
        assert row["team"]["focused"] == row["team"]["focusExpected"]
        assert abs(row["team"]["afterY"] - row["team"]["beforeY"]) <= 1
        assert row["settings"]["route"] == "#/settings"
        assert row["settings"]["cleanBefore"].startswith("server-model-")
        assert row["settings"]["cleanAfter"].startswith("server-model-")
        assert row["settings"]["cleanAfter"] != row["settings"]["cleanBefore"]
        assert row["settings"]["cleanAfter"] == row["settings"]["cleanServer"]
        assert row["settings"]["cleanDirty"] is None
        assert row["settings"]["value"] == "unsaved-browser-model"
        assert row["settings"]["latestServer"] != row["settings"]["value"]
        assert row["settings"]["focused"] is True
        assert row["settings"]["dirty"] == "1"
        assert row["detail"]["route"].endswith("?ticket=TK-live")
        assert row["detail"]["open"] is True
        assert row["detail"]["focused"] is True
        assert abs(row["detail"]["afterY"] - row["detail"]["beforeY"]) <= 1
        assert row["detail"]["beforeX"] == 35
        assert row["detail"]["afterX"] == row["detail"]["beforeX"]
        assert row["detail"]["draft"] == "unsaved form survives three refreshes"
        assert row["detail"]["draftFocused"] is True
        assert row["detail"]["dirty"] == "1"
        assert row["detail"]["networkAdvanced"] is True
