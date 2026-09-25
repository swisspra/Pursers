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


def test_refresh_cycles_preserve_reader_state_at_desktop_and_mobile(
    tmp_path: Path,
) -> None:
    """The released innerHTML refresh loses this state after the first cycle."""
    task_space_id = os.environ.get("PURSERS_EGO_TASK_SPACE_ID")
    ego_browser = shutil.which("ego-browser")
    if not task_space_id or not ego_browser:
        pytest.skip("requires PURSERS_EGO_TASK_SPACE_ID and ego-browser")

    class Cache:
        def __init__(self) -> None:
            self.fleet_revision = 0
            self.detail_revision = 0
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

        @staticmethod
        def get_config(central: str | None = None) -> dict:
            Cache.resolve_central(central)
            return {"config": {}, "expected_sha256": "a" * 64}

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
    const input = document.querySelector("#butler-settings-form input[name=model]");
    if (input) {{ input.value="unsaved-browser-model";input.dispatchEvent(new Event("input",{{bubbles:true}}));input.focus(); }}
    for (let index=0;index<3;index+=1) await refreshFleet(2000);
    const restored = document.querySelector("#butler-settings-form input[name=model]");
    return {{route:location.hash,value:restored?.value||null,focused:document.activeElement===restored,dirty:restored?.form?.dataset.dirty||null}};
  }});
  await page.evaluate(() => {{ location.hash="#/central/fixture/board/pursers/tickets?ticket=TK-live"; }});
  await page.waitForFunction(() => document.querySelector('[data-ticket="TK-live"]') || document.querySelector("#detail-view .error"), undefined, {{timeout:10000}});
  const detailReady = await page.evaluate(() => ({{ready:!!document.querySelector('[data-ticket="TK-live"]'),hash:location.hash,parsed:route(),html:document.querySelector("#detail-view").innerHTML.slice(0,800)}}));
  if (!detailReady.ready) throw new Error(`detail fixture failed: ${{JSON.stringify(detailReady)}}`);
  const detail = await page.evaluate(async () => {{
    let ticket = document.querySelector('[data-ticket="TK-live"]');
    ticket.open = true;
    ticket.querySelector("summary").focus();
    let overflowStyle = document.querySelector("#refresh-overflow-fixture");
    if (!overflowStyle) {{ overflowStyle=document.createElement("style");overflowStyle.id="refresh-overflow-fixture";overflowStyle.textContent="#detail-view .table-scroll table{{min-width:1100px}}";document.head.appendChild(overflowStyle); }}
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
  results.push({{viewport,home,team,settings,detail}});
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
        assert row["team"]["route"] == "#/team"
        assert row["team"]["count"] == 35
        assert row["team"]["open"] is True
        assert row["team"]["focused"] == row["team"]["focusExpected"]
        assert abs(row["team"]["afterY"] - row["team"]["beforeY"]) <= 1
        assert row["settings"] == {
            "route": "#/settings",
            "value": "unsaved-browser-model",
            "focused": True,
            "dirty": "1",
        }
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
