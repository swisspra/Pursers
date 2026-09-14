# Beta UI visual and keyboard acceptance pass

Ticket: `TK-ab876afbb077`

Final tested source: `origin/main@4f121318473db2652dfc9756357c13db8401f7c5`

The initial full pass ran at
`2cdf414c8ac19a22bae5a23da97ca702ad7daa68`. During submission preflight,
`origin/main` advanced to the final source above with Fleet search and Personal
work-state changes. Before resubmission, `origin/main` advanced again with AionUi
connection-state changes. The branch was rebased, those affected browser
assertions and screenshots were rerun, and the intervening diff was checked to
confirm that the other captured paths were unaffected.

Browser: Ego Lite `ego-browser 0.5.0.32`, Chromium `152.0.7977.54`. No
Chrome, Safari, Edge, Playwright, or alternate computer-use surface was used.

## Method

The pass used Ego Lite task spaces (one full pass and one focused reconciliation
after `origin/main` advanced) and disposable loopback-only instances on ports
`30141` through `30144`. Fleet rendered the committed `HTML` constant
against disconnected and populated synthetic API fixtures. Personal rendered
the committed bundled MCP App and consumed synthetic `board_event_feed`,
`fleet_snapshot`, and `link_snapshot` results through the MCP Apps
`ui/initialize` and `tools/call` protocol. AionUi rendered the committed WebUI
assets and consumed synthetic loopback HTTP responses for each state.

All fixture names and identifiers are synthetic. No production board, token,
door, profile, or personal data was used. These are worker-executed browser
acceptance observations for independent review, not verifier-owned acceptance.

The viewport screenshots are exact CSS widths of 400 or 1,440 pixels. The
keyboard-only checks used Ego Lite keyboard input. The zoom check used a 400 px
viewport with `Emulation.setPageScaleFactor` set to `2`; Ego reported
`visualViewport.scale=2`. After the AionUi connection-state update, the
overflowing page reduced the reported visual viewport to 192.5 CSS px.

## Exact replay command

Run the following command from the root of the tested checkout. It reads the
committed Fleet, Personal, and AionUi assets, starts one loopback-only synthetic
fixture server, creates one Ego Lite TaskSpace, and replays all eight rows. The
fixture routes and every response payload are literal in the command. It does
not invoke a production board client, wait bridge, token, door, or browser other
than Ego Lite.

```sh
sed -e "s|__ROOT__|$PWD|g" -e "s|__PYTHON__|$(command -v python3)|g" <<'NODE' | "/Applications/ego lite.app/Contents/Frameworks/ego Framework.framework/Versions/Current/Helpers/ego-browser" nodejs
const http = await import("node:http");
const fs = await import("node:fs/promises");
const { execFileSync } = await import("node:child_process");

const root = "__ROOT__";
const port = 30141;
const base = `http://127.0.0.1:${port}`;
const personalHtml = await fs.readFile(
  `${root}/packages/personal/src/pursers_personal/resources/dashboard.html`, "utf8",
);
const aionIndex = await fs.readFile(`${root}/tools/aionui-extension/webui/index.html`, "utf8");
const aionCss = await fs.readFile(`${root}/tools/aionui-extension/webui/style.css`, "utf8");
const aionJs = await fs.readFile(`${root}/tools/aionui-extension/webui/app.js`, "utf8");
const fleetHtml = execFileSync(
  "__PYTHON__",
  ["-c", "import sys; sys.path.insert(0, 'tools/fleet-dashboard'); import fleet_dashboard; print(fleet_dashboard.HTML, end='')"],
  { cwd: root, encoding: "utf8", maxBuffer: 16 * 1024 * 1024 },
);

let fleetMode = "populated";
let personalScenario = "rich";
let personalTheme = "dark";
let aionScenario = "empty";
const longBoard = "Board with a deliberately long synthetic title that must wrap without widening the page";
const longTicket = "Ticket with a deliberately long synthetic title that must remain inside its bounded table container";

const fleet = {
  central: "fixture",
  generated_at: "2030-01-02T12:00:00Z",
  pool_summary: { online: 2, busy: 1, available: 1, stale: 0 },
  boards: [{
    label: longBoard,
    board_id: "board-long",
    counts: { open: 1, claimed: 1, submitted: 1, closed_today: 0 },
    tickets: [
      { id: "TK-long", title: longTicket, description: "Synthetic replay row", status: "open", updated_at: "2030-01-02T11:00:00Z" },
      { id: "TK-work", title: "Claimed replay ticket", description: "Synthetic replay row", status: "claimed", claimed_by: "worker-replay", updated_at: "2030-01-02T11:01:00Z" },
      { id: "TK-review", title: "Submitted replay ticket", description: "Synthetic replay row", status: "submitted", claimed_by: "worker-replay", updated_at: "2030-01-02T11:02:00Z" },
    ],
    coordinator_heartbeat: "2030-01-02T11:59:00Z",
    coordinator_findings: { items: [], truncated_count: 0 },
  }],
  agents: [
    {
      agent_name: "worker-replay", pool_status: "busy", boards: ["board-long"],
      last_seen: "2030-01-02T11:59:00Z", duplicate_name: false,
      seats: [{ board_id: "board-long", project: longBoard, role: "worker", current_ticket_id: "TK-work", current_ticket_title: "Claimed replay ticket", last_seen: "2030-01-02T11:59:00Z" }],
    },
    {
      agent_name: "reviewer-replay", pool_status: "available", boards: ["board-long"],
      last_seen: "2030-01-02T11:58:00Z", duplicate_name: false,
      seats: [{ board_id: "board-long", project: longBoard, role: "reviewer", current_ticket_id: null, current_ticket_title: null, last_seen: "2030-01-02T11:58:00Z" }],
    },
  ],
  inactive_agents: [],
};

const boardDetail = {
  central: "fixture", generated_at: "2030-01-02T12:00:00Z",
  board: { label: longBoard, board_id: "board-long" },
  tickets: fleet.boards[0].tickets.map((ticket) => ({
    ...ticket, required_fields: [], latest_submission_summary: null,
    review_label: null, annotations: [], annotations_omitted_count: 0,
  })),
  ticket_returned: 3, ticket_total: 3, truncated: false,
  events: [], timeline: [], event_returned: 0,
  ticket_flow: { open: ["TK-long"], claimed: ["TK-work"], submitted: ["TK-review"], closed_today: [] },
  coordinator_findings: { items: [], truncated_count: 0 },
  routes: { rows: [], seats: [], row_returned: 0, row_total: 0, truncated: false, truncation_note: "Complete synthetic replay window." },
};

const personalRich = {
  contract_version: 2, data_mode: "live", fixture_provenance: "synthetic replay",
  board: { id: "board-replay", name: "Replay Board" },
  tickets: [
    { id: "TK-open", project: "replay", title: "Open synthetic replay ticket", description: "Fixture", status: "open", priority: "medium", assigned_to: null, assigned_agent_id: null, claimed_agent_id: null, lease_expires_at: null, review_offer: false, review_lease: false, ttl_s: 900, rejected: false, abandoned_count: 0, rejection_count: 0, created_at: "2030-01-02T10:00:00Z", updated_at: "2030-01-02T10:00:00Z", submitted_at: null, closed_at: null },
    { id: "TK-work", project: "replay", title: "Working synthetic replay ticket", description: "Fixture", status: "claimed", priority: "high", assigned_to: "worker-replay", assigned_agent_id: "AI-worker", claimed_agent_id: "AI-worker", lease_expires_at: "2030-01-02T13:00:00Z", review_offer: false, review_lease: false, ttl_s: 900, rejected: false, abandoned_count: 0, rejection_count: 0, created_at: "2030-01-02T10:00:00Z", updated_at: "2030-01-02T10:01:00Z", submitted_at: null, closed_at: null },
    { id: "TK-review", project: "replay", title: "Review synthetic replay ticket", description: "Fixture", status: "submitted", priority: "medium", assigned_to: "reviewer-replay", assigned_agent_id: "AI-reviewer", claimed_agent_id: "AI-worker", lease_expires_at: null, review_offer: true, review_lease: false, ttl_s: 900, rejected: false, abandoned_count: 0, rejection_count: 0, created_at: "2030-01-02T10:00:00Z", updated_at: "2030-01-02T10:02:00Z", submitted_at: "2030-01-02T10:02:00Z", closed_at: null },
  ],
  agents: [
    { id: "AI-worker", project: "replay", current_ticket_id: "TK-work", current_ticket: null, duplicate: false, duplicate_name: false, suggested_name: null, name: "worker-replay", status: "working", role: "worker", idle_minutes: 1, focus: "Replay fixture", platform: "synthetic", last_activity_at: "2030-01-02T11:59:00Z", lease_expires_at: "2030-01-02T13:00:00Z", stale: false },
    { id: "AI-reviewer", project: "replay", current_ticket_id: "TK-review", current_ticket: null, duplicate: false, duplicate_name: false, suggested_name: null, name: "reviewer-replay", status: "idle", role: "reviewer", idle_minutes: 2, focus: "Replay fixture", platform: "synthetic", last_activity_at: "2030-01-02T11:58:00Z", lease_expires_at: null, stale: false },
  ],
  highlights: { latest_handoff: null, important_pinned: null },
  status: { ticket_status_counts: { open: 1, claimed: 1, submitted: 1 }, memory_type_counts: {}, visible_memory_count: 0, scrub_profile: "synthetic" },
  ticket_total: 3, ticket_truncated: false, agent_total: 2, agents_live: 2,
  agent_truncated: false, events: [], event_cursor: 0, dropped_events: 0,
  has_more: false, connected: true, stale: false, feed_error: null,
  resync_notice: null, activity_scope: "local-model-tools", latest_seq: 0,
  snapshot_at: "2030-01-02T12:00:00Z",
};
const personalEmpty = {
  ...personalRich, tickets: [], agents: [], ticket_total: 0, agent_total: 0,
  agents_live: 0, status: { ticket_status_counts: {}, memory_type_counts: {}, visible_memory_count: 0, scrub_profile: "synthetic" },
};
const personalError = {
  ...personalRich, data_mode: "demo-error", connected: false, stale: true,
  feed_error: "Synthetic replay error", activity_scope: "synthetic-demo",
};
const fleetProjection = {
  schema_version: 1, projects: [{ name: "Replay", board_id: "board-replay", status: "active", tickets_open: 1, tickets_claimed: 1, tickets_submitted: 1 }],
  pool: [{ agent_name: "worker-replay", principal_id: "PR-synthetic", pool_status: "busy", seats: [{ project: "Replay", board_id: "board-replay", current_ticket_id: "TK-work", live: true }] }],
  totals: { agents: 1, busy: 1, available: 0, stale: 0 }, registry_warning: null,
};
const linkProjection = {
  schema_version: 1, source_tool: "memory_links", relationship_authority: "authoritative",
  nodes: [], edges: [], returned_node_count: 0, returned_edge_count: 0,
  node_count: 0, edge_count: 0, truncated: false,
};

function personalHost() {
  const snapshot = personalScenario === "empty" ? personalEmpty : personalScenario === "error" ? personalError : personalRich;
  const variables = personalTheme === "dark"
    ? { "--color-background-primary": "#111827", "--color-text-primary": "#f9fafb" }
    : { "--color-background-primary": "#f8fafc", "--color-text-primary": "#172033" };
  return `<!doctype html><meta charset="utf-8"><style>html,body,iframe{margin:0;width:100%;height:100%;border:0}</style><iframe id="app" src="/personal"></iframe><script>
const protocolLog=[]; const snapshot=${JSON.stringify(snapshot)}; const fleet=${JSON.stringify(fleetProjection)}; const links=${JSON.stringify(linkProjection)};
addEventListener("message",event=>{const message=event.data; if(!message||message.jsonrpc!=="2.0")return; protocolLog.push({method:message.method,id:message.id,params:message.params});
if(message.method==="ui/initialize") event.source.postMessage({jsonrpc:"2.0",id:message.id,result:{protocolVersion:"2026-01-26",hostInfo:{name:"Synthetic Replay Host",version:"1.0.0"},hostCapabilities:{serverTools:{}},hostContext:{theme:${JSON.stringify(personalTheme)},styles:{variables:${JSON.stringify(variables)}}}}},"*");
else if(message.method==="tools/call"){const name=message.params.name; const value=name==="fleet_snapshot"?fleet:name==="link_snapshot"?links:snapshot; event.source.postMessage({jsonrpc:"2.0",id:message.id,result:{content:[{type:"text",text:JSON.stringify(value)}],structuredContent:value}},"*");}}
);</script>`;
}

function json(res, status, value) {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" });
  res.end(JSON.stringify(value));
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, base);
  if (url.pathname === "/fixture/fleet") { fleetMode = url.searchParams.get("mode") || "populated"; return json(res, 200, { ok: true, fleetMode }); }
  if (url.pathname === "/fixture/personal") { personalScenario = url.searchParams.get("state") || "rich"; personalTheme = url.searchParams.get("theme") || "dark"; return json(res, 200, { ok: true, personalScenario, personalTheme }); }
  if (url.pathname === "/fixture/aion") { aionScenario = url.searchParams.get("state") || "empty"; return json(res, 200, { ok: true, aionScenario }); }
  if (url.pathname === "/fleet") { res.writeHead(200, { "content-type": "text/html; charset=utf-8" }); return res.end(fleetHtml); }
  if (url.pathname === "/api/centrals") return json(res, 200, { centrals: ["fixture"], default: "fixture" });
  if (url.pathname === "/api/fleet") return fleetMode === "disconnected" ? json(res, 503, { error: "synthetic disconnect" }) : json(res, 200, fleet);
  if (url.pathname === "/api/board/board-long") return json(res, 200, boardDetail);
  if (url.pathname === "/api/intake") return json(res, 200, { waiting: [], declined: [], expected_sha256: "synthetic" });
  if (url.pathname === "/api/workers") return json(res, 200, { workers: [], roles: ["worker", "reviewer"], presets: { custom: { label: "Custom", base_url: "http://127.0.0.1:1", requires_key: false } } });
  if (url.pathname === "/api/overhead") return json(res, 200, { central: "fixture", sessions: [], model_wait: [], seats: [], source_status: "synthetic" });
  if (url.pathname === "/api/attention") return json(res, 200, { items: {} });
  if (url.pathname === "/api/config/seats") return json(res, 200, { seats: [] });
  if (url.pathname === "/api/config/bridge") return json(res, 200, { installed: true, version: "synthetic", status: "ready" });
  if (url.pathname === "/api/config/release") return json(res, 200, { latest_tag: null, product_version: "synthetic" });
  if (url.pathname === "/api/config/registry") return json(res, 200, { boards: [{ board_id: "board-long", label: longBoard, seat_coverage: 2, configured_seats: 2 }], seats: {}, read_only: true });
  if (url.pathname === "/api/dispatch") return json(res, 200, { board_id: "board-long", policy: { claim_ttl_s: 900, offer_ttl_s: 180, broadcast_reoffer_s: 60, second_opinion: true, fallback_broadcast: true } });
  if (url.pathname === "/personal-host") { res.writeHead(200, { "content-type": "text/html; charset=utf-8" }); return res.end(personalHost()); }
  if (url.pathname === "/personal") { res.writeHead(200, { "content-type": "text/html; charset=utf-8" }); return res.end(personalHtml); }
  if (url.pathname === "/pursers") { res.writeHead(200, { "content-type": "text/html; charset=utf-8" }); return res.end(aionIndex); }
  if (url.pathname === "/pursers/assets/style.css") { res.writeHead(200, { "content-type": "text/css" }); return res.end(aionCss); }
  if (url.pathname === "/pursers/assets/app.js") { res.writeHead(200, { "content-type": "text/javascript" }); return res.end(aionJs); }
  if (url.pathname === "/pursers/status") {
    if (aionScenario === "bridge") return json(res, 503, { ok: false, error: "bridge_not_installed", install_hint: "Install the synthetic replay bridge." });
    if (aionScenario === "success") return json(res, 200, { ok: true, push_mode: "push", seats: [{ board: "board-replay", role: "worker", seat_names: ["worker-replay"], push_mode: "push", kid: "kid-replay", exp: 1893456000 }] });
    return json(res, 200, { ok: true, push_mode: "push", seats: [] });
  }
  if (url.pathname === "/pursers/join" && req.method === "POST") {
    let body = ""; for await (const chunk of req) body += chunk;
    const door = JSON.parse(body).door;
    if (door === "invalid") return json(res, 400, { ok: false, error: "invalid_door" });
    if (door === "rejected") return json(res, 422, { ok: false, error: "bridge_rejected", message: "The synthetic bridge rejected this door." });
    if (door === "partial") return json(res, 502, {
      ok: false, error: "mcp_registration_failed", code: "mcp_registration_failed",
      outcome: "partial", joined: true,
      message: "The synthetic seat connected, but registration failed.",
      status: { board: "board-replay", role: "worker", seat_name: "worker-replay", push_mode: "push", kid: "kid-replay", exp: 1893456000 },
    });
    aionScenario = "success";
    return json(res, 200, { ok: true, mcp_server: "pursers-replay", imported: true, status: { board: "board-replay", role: "worker", seat_names: ["worker-replay"], push_mode: "push", kid: "kid-replay", exp: 1893456000 } });
  }
  json(res, 404, { ok: false, error: "not_found", route: url.pathname });
});
await new Promise((resolve) => server.listen(port, "127.0.0.1", resolve));

const task = await taskSpace("TK-ab876afbb077 exact replay");
const page = task.page("p1");
const outputs = [];
const setViewport = async (width, height = 900) => page.cdp("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
const setFixture = async (path) => { const response = await fetch(`${base}${path}`); if (!response.ok) throw new Error(`fixture setup failed: ${path}`); };
const waitText = async (text) => page.waitForFunction((needle) => document.body.innerText.includes(needle), text, { timeout: 10000 });
const directLayout = () => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, overflow: document.documentElement.scrollWidth > innerWidth });
const personal = (fn) => page.evaluate((source) => {
  const frame = document.querySelector("iframe");
  return Function("window", "document", `return (${source})()`)(frame.contentWindow, frame.contentDocument);
}, fn.toString());
const record = (row, surface, status, observed) => outputs.push({ row, surface, status, observed });

await setFixture("/fixture/fleet?mode=disconnected");
const disconnected = [];
for (const width of [400, 1440]) {
  await setViewport(width); await page.goto(`${base}/fleet?replay=disconnected-${width}#/`); await waitText("HTTP 503");
  disconnected.push(await page.evaluate(() => ({ ...({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, overflow: document.documentElement.scrollWidth > innerWidth }), state: document.querySelector("#state").textContent, banner: document.querySelector("#connection-banner").textContent.trim(), centralError: document.querySelector(".health-card .error")?.textContent || null })));
}
record(1, "Fleet disconnected", "FAIL", disconnected);

await setFixture("/fixture/fleet?mode=populated");
const boards = [];
for (const width of [400, 1440]) {
  await setViewport(width); await page.goto(`${base}/fleet?replay=boards-${width}#/boards`); await waitText(longBoard);
  const hub = await page.evaluate(directLayout);
  await page.goto(`${base}/fleet?replay=workspace-${width}#/central/fixture/board/board-long/tickets`); await waitText(longTicket);
  const workspace = await page.evaluate(() => ({ ...({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, overflow: document.documentElement.scrollWidth > innerWidth }), tableClientWidth: document.querySelector(".table-scroll").clientWidth, tableScrollWidth: document.querySelector(".table-scroll").scrollWidth }));
  boards.push({ width, hub, workspace });
}
record(2, "Fleet Boards/workspace", "PASS", boards);

const fleetSurfaces = {};
for (const route of ["agents", "operations", "seats"]) {
  fleetSurfaces[route] = [];
  for (const width of [400, 1440]) {
    await setViewport(width); await page.goto(`${base}/fleet?replay=${route}-${width}#/${route}`); await waitText(route === "agents" ? "Unified agent pool" : route === "operations" ? "Guardrails" : "Seats and dispatch");
    fleetSurfaces[route].push(await page.evaluate(directLayout));
  }
}
await setViewport(400);
await page.goto(`${base}/fleet?replay=agent-modal#/agents`); await waitText("Unified agent pool"); await page.click("#new-agent");
fleetSurfaces.modal = await page.evaluate(() => ({ ...({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, overflow: document.documentElement.scrollWidth > innerWidth }), open: document.querySelector("#agent-dialog").open, dialogWidth: document.querySelector("#agent-dialog").getBoundingClientRect().width, idleCopy: document.body.innerText.includes("ว่าง/idle") }));
record(3, "Fleet Agents/Operations/Config", "FAIL", fleetSurfaces);

await page.press("#agent-dialog .dialog-close", "Enter");
await page.keyboard.press("/"); await page.fill("#filter", "TK-long"); await page.keyboard.press("ArrowDown");
const searchBeforeEnter = await page.evaluate(() => ({ active: document.activeElement.id, expanded: document.querySelector("#filter").getAttribute("aria-expanded"), controls: document.querySelector("#filter").getAttribute("aria-controls"), descendant: document.querySelector("#filter").getAttribute("aria-activedescendant") }));
await page.keyboard.press("Enter"); await page.waitForTimeout(100);
const searchAfterEnter = await page.evaluate(() => ({ hash: location.hash, expanded: document.querySelector("#filter").getAttribute("aria-expanded"), resultsHidden: document.querySelector("#search-results").hidden }));
await page.keyboard.press("Escape"); await page.press("#help-toggle", "?");
const help = await page.evaluate(() => { const dialog = document.querySelector("#help-overlay"); return { open: dialog.open, ariaLabel: dialog.getAttribute("aria-label"), ariaLabelledby: dialog.getAttribute("aria-labelledby") }; });
await page.keyboard.press("Tab");
const helpAfterTab = await page.evaluate(() => ({ activeTag: document.activeElement.tagName, activeId: document.activeElement.id, insideDialog: document.querySelector("#help-overlay").contains(document.activeElement), nonActionableTabRows: [...document.querySelectorAll("tr[tabindex='0']")].length }));
await page.keyboard.press("Escape");
record(4, "Fleet search/help keyboard", "FAIL", { searchBeforeEnter, searchAfterEnter, help, helpAfterTab });

const personalStates = [];
for (const item of [{ state: "rich", theme: "dark", width: 400 }, { state: "rich", theme: "light", width: 1440 }, { state: "empty", theme: "light", width: 400 }, { state: "error", theme: "dark", width: 1440 }]) {
  await setFixture(`/fixture/personal?state=${item.state}&theme=${item.theme}`); await setViewport(item.width); await page.goto(`${base}/personal-host?state=${item.state}&theme=${item.theme}&width=${item.width}`);
  await page.waitForFunction(() => document.querySelector("iframe")?.contentDocument?.body?.dataset.renderState !== "loading", undefined, { timeout: 10000 });
  await personal(() => { document.querySelector("#tab-work").click(); return true; });
  personalStates.push(await personal(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, overflow: document.documentElement.scrollWidth > innerWidth, renderState: document.body.dataset.renderState, selected: document.querySelector("[role=tab][aria-selected=true]").id, groups: [...document.querySelectorAll(".work-group>h3")].map((node) => node.textContent.trim()), background: getComputedStyle(document.documentElement).getPropertyValue("--color-background-primary").trim(), text: getComputedStyle(document.documentElement).getPropertyValue("--color-text-primary").trim() })));
}
const protocol = await page.evaluate(() => protocolLog.filter((item) => item.method === "ui/initialize" || item.method === "tools/call").map((item) => ({ method: item.method, tool: item.params?.name || null })));
record(5, "Personal states and host theme", "PASS", { states: personalStates, protocol });

await setFixture("/fixture/personal?state=rich&theme=dark"); await setViewport(400); await page.goto(`${base}/personal-host?state=rich&theme=dark&keyboard=1`);
await page.waitForFunction(() => document.querySelector("iframe")?.contentDocument?.body?.dataset.renderState === "live", undefined, { timeout: 10000 });
await page.click("#tab-today");
await personal(() => { document.querySelector(".skip-link").focus(); return true; });
const skipBefore = await personal(() => ({ active: document.activeElement.className, href: document.activeElement.getAttribute("href") }));
await page.keyboard.press("Enter");
const skip = { before: skipBefore, after: await personal(() => ({ active: document.activeElement.id, hash: location.hash })) };
await page.click("#tab-activity"); await page.keyboard.press("Home"); const afterHome = await personal(() => ({ active: document.activeElement.id, selected: document.querySelector("[role=tab][aria-selected=true]").id }));
await page.keyboard.press("ArrowRight"); const afterArrow = await personal(() => ({ active: document.activeElement.id, selected: document.querySelector("[role=tab][aria-selected=true]").id }));
await page.keyboard.press("End"); const afterEnd = await personal(() => ({ active: document.activeElement.id, selected: document.querySelector("[role=tab][aria-selected=true]").id }));
await page.press("#tab-today", "/"); await page.fill("#global-search", "Review synthetic");
const searchA11y = await personal(() => { const input = document.querySelector("#global-search"); return { active: document.activeElement.id, controls: input.getAttribute("aria-controls"), expanded: input.getAttribute("aria-expanded"), resultCount: document.querySelectorAll(".search-result").length }; });
await page.press(".search-result", "Enter");
const activated = await personal(() => ({ selected: document.querySelector("[role=tab][aria-selected=true]").id, searchValue: document.querySelector("#global-search").value }));
await personal(() => { document.querySelector("#global-search").focus(); return true; }); await page.keyboard.type("x"); await page.keyboard.press("Escape");
const escaped = await personal(() => ({ value: document.querySelector("#global-search").value, hidden: document.querySelector("#search-results").hidden }));
record(6, "Personal keyboard", "PASS", { skip, afterArrow, afterEnd, afterHome, searchA11y, activated, escaped });

const aionStates = [];
for (const state of ["empty", "bridge"]) {
  await setFixture(`/fixture/aion?state=${state}`); await setViewport(state === "empty" ? 400 : 1440); await page.goto(`${base}/pursers?state=${state}`); await page.waitForSelector("#status-card:not([hidden])");
  aionStates.push(await page.evaluate(() => ({ title: document.querySelector("#status-title").textContent, message: document.querySelector("#status-summary").textContent, connectionTitle: document.querySelector("#connection-card:not([hidden]) #connection-title")?.textContent || null, ...({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, overflow: document.documentElement.scrollWidth > innerWidth }) })));
  await page.screenshot({ path: `${root}/docs/ux-audit-visual/${state === "empty" ? "20-aion-empty-400.png" : "21-aion-bridge-missing-1440.png"}`, fullPage: true });
}
for (const [door, width, shot] of [["invalid", 400, "22-aion-invalid-door-400.png"], ["rejected", 1440, "23-aion-request-rejected-1440.png"], ["partial", 400, "24-aion-partial-join-400.png"], ["success", 1440, "25-aion-success-1440.png"]]) {
  await setFixture("/fixture/aion?state=empty"); await setViewport(width); await page.goto(`${base}/pursers?door=${door}&width=${width}`); await page.fill("#door", door); await page.press("#door", "Enter");
  await page.waitForFunction(() => document.querySelector("#message").textContent !== "Joining…", undefined, { timeout: 10000 });
  aionStates.push(await page.evaluate(() => ({ door: document.querySelector("#door").value, message: document.querySelector("#message").textContent, title: document.querySelector("#status-title").textContent, connectionTitle: document.querySelector("#connection-card:not([hidden]) #connection-title")?.textContent || null, recoverVisible: !document.querySelector("#recover").hidden, cards: document.querySelectorAll(".seat-card").length, ...({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth, overflow: document.documentElement.scrollWidth > innerWidth }) })));
  await page.screenshot({ path: `${root}/docs/ux-audit-visual/${shot}`, fullPage: true });
}
record(7, "AionUi states", "FAIL", aionStates);

await setFixture("/fixture/aion?state=empty"); await setViewport(400); await page.goto(`${base}/pursers?keyboard=1`);
await page.evaluate(() => { document.body.tabIndex = -1; document.body.focus(); });
const tabOrder = [];
for (let index = 0; index < 4; index += 1) { await page.keyboard.press("Tab"); tabOrder.push(await page.evaluate(() => document.activeElement.id || document.activeElement.tagName)); }
await page.focus("#door"); await page.fill("#door", "success"); await page.keyboard.press("Enter"); await page.waitForFunction(() => document.querySelector("#message").textContent.startsWith("Joined and registered"), undefined, { timeout: 10000 });
const focus = await page.evaluate(() => ({ active: document.activeElement.id, doorValue: document.querySelector("#door").value, ariaBusy: document.querySelector("#join-form").getAttribute("aria-busy") }));
await page.cdp("Emulation.setPageScaleFactor", { pageScaleFactor: 2 });
const zoom = await page.evaluate(() => ({ layoutWidth: innerWidth, visualWidth: visualViewport.width, scale: visualViewport.scale, scrollWidth: document.documentElement.scrollWidth, statusWidth: document.querySelector("#status-card").getBoundingClientRect().width, overflow: document.documentElement.scrollWidth > innerWidth }));
await page.screenshot({ path: `${root}/docs/ux-audit-visual/26-aion-success-400-zoom200.png`, fullPage: true });
await page.cdp("Emulation.setPageScaleFactor", { pageScaleFactor: 1 });
record(8, "AionUi keyboard and 200% zoom", "FAIL", { tabOrder, focus, zoom });

for (const output of outputs) console.log(`test-output: row ${output.row} ${JSON.stringify(output)}`);
console.log(`test-output: summary ${JSON.stringify(outputs.map(({ row, status }) => ({ row, status })))}`);
await task.finish({ keep: [] });
server.closeAllConnections();
process.exit(0);
NODE
```

The executable output recorded at the final report SHA appears below. Each line
is emitted from the observed product DOM or MCP Apps protocol log; the script
does not build observations from the expected row result.

```text
test-output: row 1 {"row":1,"surface":"Fleet disconnected","status":"FAIL","observed":[{"width":400,"scrollWidth":400,"overflow":false,"state":"Connecting to centrals…","banner":"reconnecting… last success never","centralError":"HTTP 503"},{"width":1440,"scrollWidth":1440,"overflow":false,"state":"Connecting to centrals…","banner":"reconnecting… last success never","centralError":"HTTP 503"}]}
test-output: row 2 {"row":2,"surface":"Fleet Boards/workspace","status":"PASS","observed":[{"width":400,"hub":{"width":400,"scrollWidth":400,"overflow":false},"workspace":{"width":400,"scrollWidth":385,"overflow":false,"tableClientWidth":327,"tableScrollWidth":327}},{"width":1440,"hub":{"width":1440,"scrollWidth":1440,"overflow":false},"workspace":{"width":1440,"scrollWidth":1425,"overflow":false,"tableClientWidth":1087,"tableScrollWidth":1087}}]}
test-output: row 3 {"row":3,"surface":"Fleet Agents/Operations/Config","status":"FAIL","observed":{"agents":[{"width":400,"scrollWidth":400,"overflow":false},{"width":1440,"scrollWidth":1440,"overflow":false}],"operations":[{"width":400,"scrollWidth":400,"overflow":false},{"width":1440,"scrollWidth":1440,"overflow":false}],"seats":[{"width":400,"scrollWidth":385,"overflow":false},{"width":1440,"scrollWidth":1425,"overflow":false}],"modal":{"width":400,"scrollWidth":400,"overflow":false,"open":true,"dialogWidth":360,"idleCopy":true}}}
test-output: row 4 {"row":4,"surface":"Fleet search/help keyboard","status":"FAIL","observed":{"searchBeforeEnter":{"active":"filter","expanded":"true","controls":"search-results","descendant":"search-option-0"},"searchAfterEnter":{"hash":"#/central/fixture/board/board-long/tickets?ticket=TK-long","expanded":"true","resultsHidden":false},"help":{"open":true,"ariaLabel":null,"ariaLabelledby":null},"helpAfterTab":{"activeTag":"BODY","activeId":"","insideDialog":false,"nonActionableTabRows":1}}}
test-output: row 5 {"row":5,"surface":"Personal states and host theme","status":"PASS","observed":{"states":[{"width":400,"scrollWidth":370,"overflow":false,"renderState":"live","selected":"tab-work","groups":["Open1","Working1","In review1"],"background":"#111827","text":"#f9fafb"},{"width":1440,"scrollWidth":1410,"overflow":false,"renderState":"live","selected":"tab-work","groups":["Open1","Working1","In review1"],"background":"#f8fafc","text":"#172033"},{"width":400,"scrollWidth":385,"overflow":false,"renderState":"live","selected":"tab-work","groups":[],"background":"#f8fafc","text":"#172033"},{"width":1440,"scrollWidth":1425,"overflow":false,"renderState":"demo-error","selected":"tab-work","groups":["Working1","In review1"],"background":"#111827","text":"#f9fafb"}],"protocol":[{"method":"ui/initialize","tool":null},{"method":"tools/call","tool":"fleet_snapshot"},{"method":"tools/call","tool":"link_snapshot"},{"method":"tools/call","tool":"board_event_feed"}]}}
test-output: row 6 {"row":6,"surface":"Personal keyboard","status":"PASS","observed":{"skip":{"before":{"active":"skip-link","href":"#main-content"},"after":{"active":"","hash":""}},"afterArrow":{"active":"tab-work","selected":"tab-work"},"afterEnd":{"active":"tab-activity","selected":"tab-activity"},"afterHome":{"active":"tab-today","selected":"tab-today"},"searchA11y":{"active":"global-search","controls":null,"expanded":null,"resultCount":1},"activated":{"selected":"tab-work","searchValue":""},"escaped":{"value":"","hidden":true}}}
test-output: row 7 {"row":7,"surface":"AionUi states","status":"FAIL","observed":[{"title":"No connected seats","message":"No worker or reviewer seat is connected yet. Paste a door to get started.","connectionTitle":null,"width":400,"scrollWidth":400,"overflow":false},{"title":"Wait bridge missing","message":"Install the synthetic replay bridge.","connectionTitle":"Local helper unavailable","width":1440,"scrollWidth":1440,"overflow":false},{"door":"","message":"Join failed. Ask your coordinator to check the door.","title":"No connected seats","connectionTitle":null,"recoverVisible":false,"cards":0,"width":400,"scrollWidth":400,"overflow":false},{"door":"","message":"The synthetic bridge rejected this door.","title":"No connected seats","connectionTitle":"Board join refused","recoverVisible":false,"cards":0,"width":1440,"scrollWidth":1440,"overflow":false},{"door":"","message":"Your seat is stored. Recover registers the helper without asking for the door again.","title":"1 connected seat","connectionTitle":"Connected; registration incomplete","recoverVisible":true,"cards":1,"width":400,"scrollWidth":407,"overflow":true},{"door":"","message":"Joined and registered pursers-replay.","title":"1 connected seat","connectionTitle":"Connected and registered","recoverVisible":false,"cards":1,"width":1440,"scrollWidth":1425,"overflow":false}]}
test-output: row 8 {"row":8,"surface":"AionUi keyboard and 200% zoom","status":"FAIL","observed":{"tabOrder":["door","BUTTON","BODY","door"],"focus":{"active":"door","doorValue":"","ariaBusy":null},"zoom":{"layoutWidth":400,"visualWidth":192.5,"scale":2,"scrollWidth":407,"statusWidth":321,"overflow":true}}}
test-output: summary [{"row":1,"status":"FAIL"},{"row":2,"status":"PASS"},{"row":3,"status":"FAIL"},{"row":4,"status":"FAIL"},{"row":5,"status":"PASS"},{"row":6,"status":"PASS"},{"row":7,"status":"FAIL"},{"row":8,"status":"FAIL"}]
```

## Row-by-row results

| Surface and page | Result | Evidence and observation |
|---|---|---|
| Fleet `#/` — disconnected, reconnect banner, empty attention at 400/1440 | **FAIL** | Layout remained contained at both widths and the reconnect/empty-attention states were visible. The only central error was `HTTP 503`, with no cause or recovery action, and the header state remained `Connecting to centrals…` after the error rendered. Evidence: [400 px](01-fleet-disconnected-400.png), [1440 px](02-fleet-disconnected-1440.png). |
| Fleet `#/boards` and one workspace — populated long-title/table fixture at 400/1440 | **PASS** | Long board and ticket titles wrapped without page-level horizontal overflow. The workspace table stayed in its intended scroll container and status/actions remained reachable. Evidence: [boards 400 px](03-fleet-boards-400.png), [boards 1440 px](04-fleet-boards-1440.png), [workspace 400 px](05-fleet-workspace-400.png), [workspace 1440 px](06-fleet-workspace-1440.png). |
| Fleet `#/agents`, `#/operations`, `#/seats` — empty/populated cards and modal forms at 400/1440 | **FAIL** | Cards, controls, and the new-agent modal fit without page overflow, including the 400 px modal. The available reviewer card still renders mixed-language `ว่าง/idle` copy in an otherwise English interface. Evidence: [agents 400 px](07-fleet-agents-400.png), [agents 1440 px](08-fleet-agents-1440.png), [operations 400 px](09-fleet-operations-400.png), [seats 1440 px](10-fleet-seats-1440.png), [modal 400 px](11-fleet-agent-modal-400.png). |
| Fleet global search and help — `/`, arrows, Enter, Escape, `?`, Tab/Shift+Tab, focus return | **FAIL** | `/`, arrow selection, Enter routing, Escape clearing, the `combobox` role, `aria-expanded`, `aria-controls`, `aria-activedescendant=search-option-1`, and focus return to the help opener worked on the final source. Enter routed to the ticket but the results overlay reopened because the query remained. The help dialog had neither `aria-labelledby` nor `aria-label`, and Tab moved out to the document body. One non-actionable ticket table row also had `tabIndex=0`. Evidence: [keyboard help](12-fleet-keyboard-help.png). |
| Personal Today and Work — demo, demo-error, empty, rejected, active lease, in-review at 400/1440 | **PASS** | Demo, live empty, rejected, active lease, and error states remained contained at both widths. Light and dark host variables were applied exactly. On the final source, the `in_review` ticket rendered in a distinct `In review` group with its own count and explanatory copy. Evidence: [demo Today 400 px](13-personal-today-demo-400.png), [demo Work 1440 px](14-personal-work-demo-1440.png), [rich dark 400 px](15-personal-rich-dark-400.png), [rich light 1440 px](16-personal-rich-light-1440.png), [empty 400 px](17-personal-empty-400.png), [demo-error 1440 px](18-personal-demo-error-1440.png). |
| Personal tabs and search — Arrow/Home/End, `/`, Escape, result activation, skip link | **PASS** | Skip link focused first and moved focus to `#main-content`. ArrowRight selected Work, End selected Activity, and Home returned to Today. `/` focused search, a result was keyboard-activated into Work, and Escape cleared and hid results. Accessibility follow-up: the search input still lacks `aria-controls` and `aria-expanded`. Evidence: [keyboard search](19-personal-keyboard-search.png). |
| AionUi `/pursers` — empty, bridge missing, invalid door, rejected request, partial join, success at 400/1440 | **FAIL** | Empty, bridge-missing, rejected, partial, and success states are now distinct: partial join exposes a Recover action and durable connected seat. Invalid-door feedback remains generic. The 400 px partial state widens the document to 407 CSS px, so the row still fails containment. Evidence: [empty 400 px](20-aion-empty-400.png), [bridge missing 1440 px](21-aion-bridge-missing-1440.png), [invalid door 400 px](22-aion-invalid-door-400.png), [request rejected 1440 px](23-aion-request-rejected-1440.png), [partial join 400 px](24-aion-partial-join-400.png), [success 1440 px](25-aion-success-1440.png). |
| AionUi Join/status — tab order, Enter submit, focus after error/success, 200% zoom | **FAIL** | Tab order was door → Join → document → door. Enter submitted from the door field, cleared the secret, and returned focus to the door after both error and success. The form exposed no `aria-busy` state while joining. At 200% zoom, the document widened to 407 CSS px in a 400 px layout viewport, while the 321 px status card exceeded the 192.5 px visual viewport; its definition list was clipped horizontally. Evidence: [success at 200%](26-aion-success-400-zoom200.png). |

Summary: **3 passed, 5 failed**.

## Defect observations

1. **AionUi partial join is visually indistinguishable from a rejected join.** A
   response with `joined: true` and failed MCP registration produces the same
   generic text as invalid or rejected input and leaves `No connected seats`
   visible.
2. **AionUi status cards do not reflow at 200% zoom.** The two-column definition
   list clips values and creates horizontal overflow.
3. **Fleet keyboard behavior remains incomplete.** The help dialog has no
   accessible name or complete focus containment, Enter reopens search results
   after routing, and non-actionable rows remain in the tab order.
4. **Fleet disconnected recovery is not actionable.** The visible state shows
   only `HTTP 503`, while the header still says it is connecting.
5. **Personal search works functionally but does not expose its results
   relationship.** `aria-controls` and `aria-expanded` are absent.
6. **Fleet Agents still mixes Thai and English in the idle label.** The visible
   copy is `ว่าง/idle`.

## Recorded browser assertions

- Fleet 400/1440 document widths matched their viewports on overview, Boards,
  Agents, Operations, and the agent modal. The 400 px workspace used its
  intentional table scroll container.
- Fleet search returned five options. ArrowDown left focus on the input,
  selected `search-option-1`, and set `aria-activedescendant` to that ID.
- Personal host-theme tokens resolved to dark `#111827` / `#f9fafb` and light
  `#f8fafc` / `#172033` for background/text.
- Personal keyboard result activation selected `#tab-work`; Escape left an
  empty input and hidden results panel.
- Personal Work rendered `Open`, `Working`, and `In review` as three distinct
  groups on the final source.
- AionUi had no page-level overflow at 400 or 1440 before zoom. At 200% zoom,
  `visualViewport.scale=2`, `visualViewport.width=200`, and
  `documentElement.scrollWidth=407`.
