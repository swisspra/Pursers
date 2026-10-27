from __future__ import annotations

import json
import subprocess
from pathlib import Path


UI_ROOT = Path(__file__).parents[1] / "ui"


def _node(script: str) -> str:
    return subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    ).stdout


def test_ag_ui_client_parses_finite_scoped_observation_and_honors_abort() -> None:
    client = (UI_ROOT / "ag-ui-client.js").read_text()
    snapshot = {
        "scope": {"central": "fixture", "board_id": "demo-board"},
        "tickets": [{"id": "TK-draft", "delivery": {"state": "integration_merged"}}],
        "coverage": {"ticket_total": 1, "ticket_returned": 1, "ticket_omitted": 0, "truncated": False},
    }
    program = f"""
const requests = [];
globalThis.fetch = async (url, options) => {{
  const request = JSON.parse(options.body);
  requests.push({{url, signal: options.signal, request}});
  const snapshot = {json.dumps(snapshot)};
  const events = [
    {{type: 'RUN_STARTED', threadId: request.threadId, runId: request.runId, protocolVersion: '1.0'}},
    {{type: 'STATE_SNAPSHOT', snapshot}},
    {{type: 'RUN_FINISHED', threadId: request.threadId, runId: request.runId}},
  ];
  return new Response(events.map(event => 'data: ' + JSON.stringify(event)).join('\\n\\n') + '\\n\\n', {{status: 200, headers: {{'Content-Type': 'text/event-stream'}}}});
}};
eval({json.dumps(client)});
const controller = new AbortController();
const result = await PursersAgUi.observe({{central: 'fixture', board_id: 'demo-board'}}, {{signal: controller.signal}});
controller.abort();
console.log(JSON.stringify({{url: requests[0].url, signalForwarded: requests[0].signal === controller.signal, request: requests[0].request, result}}));
"""
    result = json.loads(_node(program))
    assert result["url"] == "/api/ag-ui/observe?central=fixture&board_id=demo-board"
    assert result["signalForwarded"] is True
    assert result["request"]["threadId"] == "pursers-projects"
    assert result["request"]["messages"] == [] and result["request"]["tools"] == []
    assert result["result"]["scope"] == snapshot["scope"]
    assert result["result"]["tickets"][0]["delivery"]["state"] == "integration_merged"


def test_projects_distinguishes_unavailable_source_and_never_infers_delivery_readiness() -> None:
    registry = (UI_ROOT / "view-registry.js").read_text()
    projects = (UI_ROOT / "views/projects.js").read_text()
    program = f"""
const dashboardState = {{textContent: 'Source unavailable · 1 Central'}};
globalThis.document = {{querySelector: selector => selector === '#state' ? dashboardState : null, createElement: () => ({{dataset: {{}}}}), head: {{append: () => {{}}}}}};
eval({json.dumps(registry)});
eval({json.dumps(projects)});
const escape = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const render = (fleetData, tickets = []) => FleetViewModules.render('projects', {{
  esc: escape, pageHead: () => '', warmTruthStrip: () => '', numberCount: Number,
  boardHref: () => '#/board', centralLabels: ['fixture'], fleetData,
  warmBoards: () => tickets.length ? [{{central: 'fixture', board: {{
    label: 'Demo', board_id: 'demo-board', status: 'ready', counts: {{}}, tickets,
  }}}}] : [],
}});
const unavailable = render({{}});
dashboardState.textContent = 'Connecting to centrals…';
const loading = render({{}});
dashboardState.textContent = 'Updated 2030-01-02';
const confirmedEmpty = render({{fixture: {{boards: []}}}});
const draft = render({{fixture: {{boards: []}}}}, [{{
  id: 'TK-draft', title: 'Draft', status: 'closed', status_label: 'Approved · delivery pending',
  activity: {{stage: 'delivery', state: 'blocked', next_action: 'Complete final QA', blocking_reason: 'QA held; security pending', evidence_refs: ['ticket://demo/evidence']}},
  result: {{state: 'approved', review: {{verdict: 'approve', independent: true}}}},
  delivery: {{state: 'integration_merged'}},
}}, {{id: 'TK-unknown', title: 'Unknown evidence', status: 'closed', delivery: {{state: 'integration_merged'}}}}]);
console.log(JSON.stringify({{unavailable, loading, confirmedEmpty, draft}}));
"""
    rendered = json.loads(_node(program))
    assert "Central connection unavailable" in rendered["unavailable"]
    assert "Connect your first project" not in rendered["unavailable"]
    assert "Checking Central connection" in rendered["loading"]
    assert "Connect your first project" not in rendered["loading"]
    assert "Connect your first project" in rendered["confirmedEmpty"]
    assert "Current stage" in rendered["draft"]
    assert "Complete final QA" in rendered["draft"]
    assert "Evidence references" in rendered["draft"] and "ticket://demo/evidence" in rendered["draft"]
    assert "Final QA" in rendered["draft"] and "Held · held" in rendered["draft"]
    assert "Security" in rendered["draft"] and "Held · pending" in rendered["draft"]
    assert "Unknown evidence" in rendered["draft"] and rendered["draft"].count("<dd>Unknown</dd>") >= 2
    assert "Ticket coverage unknown" in rendered["draft"]
    assert "Ready for team merge" not in rendered["draft"]


def test_projects_observation_ignores_stale_scope_response() -> None:
    registry = (UI_ROOT / "view-registry.js").read_text()
    projects = (UI_ROOT / "views/projects.js").read_text()
    client = (UI_ROOT / "ag-ui-client.js").read_text()
    program = f"""
const pending = [];
globalThis.fetch = (url, options) => new Promise(resolve => pending.push({{url, options, resolve}}));
globalThis.document = {{
  querySelector: () => null,
  createElement: () => ({{dataset: {{}}}}),
  head: {{append: () => {{}}}},
}};
eval({json.dumps(registry)});
eval({json.dumps(client)});
eval({json.dumps(projects)});
const context = {{
  esc: value => String(value ?? ''), pageHead: () => '', warmTruthStrip: () => '', numberCount: Number,
  boardHref: () => '#/board', centralLabels: ['fixture'], fleetData: {{fixture: {{}}}},
  warmBoards: () => [{{central: 'fixture', board: {{label: 'Demo', board_id: 'old-board', status: 'ready', counts: {{}}}}}}],
}};
const observation = {{innerHTML: 'placeholder'}};
const card = {{isConnected: true, dataset: {{central: 'fixture', boardId: 'old-board'}}}};
card.querySelector = selector => selector === '[data-ag-ui-observation]' ? observation : null;
const root = {{querySelector: () => null, querySelectorAll: selector => selector === '[data-projects-card]' ? [card] : []}};
FleetViewModules.bind('projects', context, root);
await Promise.resolve();
context.warmBoards = () => [{{central: 'fixture', board: {{label: 'New', board_id: 'new-board', status: 'ready', counts: {{}}}}}}];
const currentCard = {{isConnected: true, dataset: {{central: 'fixture', boardId: 'new-board'}}}};
const currentObservation = {{innerHTML: 'current'}};
currentCard.querySelector = selector => selector === '[data-ag-ui-observation]' ? currentObservation : null;
root.querySelectorAll = selector => selector === '[data-projects-card]' ? [currentCard] : [];
FleetViewModules.bind('projects', context, root);
const oldBody = 'data: ' + JSON.stringify({{type: 'RUN_STARTED', threadId: 'fixture/old-board', runId: 'run-old', protocolVersion: '1.0'}}) + '\\n\\ndata: ' + JSON.stringify({{type: 'STATE_SNAPSHOT', snapshot: {{scope: {{central: 'fixture', board_id: 'old-board'}}, tickets: [], coverage: {{}}}}}}) + '\\n\\ndata: ' + JSON.stringify({{type: 'RUN_FINISHED', threadId: 'fixture/old-board', runId: 'run-old'}}) + '\\n\\n';
pending[0].resolve(new Response(oldBody, {{status: 200, headers: {{'Content-Type': 'text/event-stream'}}}}));
await new Promise(resolve => setTimeout(resolve, 0));
console.log(JSON.stringify({{oldSignalAborted: pending[0].options.signal.aborted, oldObservation: observation.innerHTML, currentObservation: currentObservation.innerHTML}}));
"""
    rendered = json.loads(_node(program))
    assert rendered["oldSignalAborted"] is True
    assert rendered["oldObservation"] == "placeholder"
    assert rendered["currentObservation"] == "current"


def test_ag_ui_client_rejects_wrong_project_scope_and_non_sse_errors() -> None:
    client = (UI_ROOT / "ag-ui-client.js").read_text()
    program = f"""
eval({json.dumps(client)});
const failures = [];
globalThis.fetch = async () => new Response('{{"error":"observation cache unavailable"}}', {{status: 503, headers: {{'Content-Type': 'application/json'}}}});
try {{ await PursersAgUi.observe({{central: 'fixture', board_id: 'demo-board'}}); }} catch (error) {{ failures.push(error.message); }}
globalThis.fetch = async (_url, options) => {{
  const request = JSON.parse(options.body);
  const events = [
    {{type: 'RUN_STARTED', threadId: request.threadId, runId: request.runId, protocolVersion: '1.0'}},
    {{type: 'STATE_SNAPSHOT', snapshot: {{scope: {{central: 'fixture', board_id: 'other-board'}}, tickets: [], coverage: {{}}}}}},
    {{type: 'RUN_FINISHED', threadId: request.threadId, runId: request.runId}},
  ];
  return new Response(events.map(event => 'data: ' + JSON.stringify(event)).join('\\n\\n') + '\\n\\n', {{status: 200, headers: {{'Content-Type': 'text/event-stream'}}}});
}};
try {{ await PursersAgUi.observe({{central: 'fixture', board_id: 'demo-board'}}); }} catch (error) {{ failures.push(error.message); }}
console.log(JSON.stringify(failures));
"""
    errors = json.loads(_node(program))
    assert "Observation unavailable (HTTP 503)." in errors
    assert "Observation snapshot scope did not match the requested project." in errors


def test_ag_ui_client_asset_is_registered_and_script_is_loaded() -> None:
    import importlib.util
    import sys

    dashboard_path = Path(__file__).parents[1] / "fleet_dashboard.py"
    spec = importlib.util.spec_from_file_location("fleet_dashboard_ag_ui_asset", dashboard_path)
    assert spec and spec.loader
    dashboard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dashboard
    spec.loader.exec_module(dashboard)
    route, (content_type, path) = "/ui/ag-ui-client.js", dashboard.UI_ASSET_PATHS["/ui/ag-ui-client.js"]
    assert content_type == "text/javascript; charset=utf-8"
    assert path == UI_ROOT / "ag-ui-client.js"
    assert dashboard.UI_ASSETS[route][1] == path.read_bytes()
    assert '<script src="/ui/ag-ui-client.js"></script>' in dashboard.HTML_SHELL
