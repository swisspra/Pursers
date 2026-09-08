# Pages — Pursers Dashboard Surfaces

Page component dependency trees for each key route/view.

Baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (origin/main, 5.0.0a25).

## Surface 1: Dashboard-UI — Today View (primary page)

Entry: `tools/dashboard-ui/dashboard-entry.html`

```
dashboard-entry.html
├── src/dashboard.ts (module script)
│   └── (imports from @modelcontextprotocol/ext-apps SDK, bundled by Vite)
└── src/dashboard.css (bundled inline by vite-plugin-singlefile)

dashboard.ts dependency tree (internal):
├── Type definitions (Ticket, Agent, Snapshot, BoardEvent, FleetSnapshot, LinkSnapshot, etc.)
├── Constants (MAX_TICKETS=500, MAX_AGENTS=200, MAX_EVENTS=200, feed delay timers)
├── Fallback data (FALLBACK_AGENTS, FALLBACK_TICKETS, FALLBACK_HIGHLIGHTS, FALLBACK_EVENTS)
├── DOM helpers
│   ├── byId()
│   ├── element()
│   ├── emptyState()
│   ├── pill()
│   └── agentField()
├── Decoders
│   ├── decodeSnapshot()
│   ├── decodeFleetSnapshot()
│   ├── decodeLinkSnapshot()
│   └── structured()
├── Renderers
│   ├── renderConnection()
│   ├── renderHealth()
│   ├── renderToday()
│   ├── renderWork()
│   ├── renderAgents()
│   ├── renderFleet()
│   ├── renderLinks()
│   ├── renderActivity()
│   ├── renderSearch()
│   └── renderActivePanel()
├── Data refresh
│   ├── refreshSnapshot()
│   ├── refreshFeed()
│   ├── refreshFleet()
│   └── refreshLinks()
├── Event handlers (tab click, search input, refresh button, keyboard, visibility)
├── Host context integration (applyHostContext, applyDocumentTheme, applyHostStyleVariables)
└── App connect (PostMessageTransport, toolinput/toolresult/hostcontextchanged/onteardown/onerror)
```

Built output: `packages/personal/src/pursers_personal/resources/dashboard.html` (single-file, inlined JS+CSS)

## Surface 1: Dashboard-UI — Work View

Same entry, switches to Work tab via `selectView("work")`.

Renders `renderWork()` → `WORK_GROUPS` status groups → `ticketRow()` per ticket → `emptyState()` when empty.

Dependencies: same as Today View (all views share the single `dashboard.ts` module).

## Surface 1: Dashboard-UI — Agents View

`renderAgents()` → agent cards with avatar, status pills, focus text, meta-row (role, platform, project, ticket, idle), duplicate-name warning.

## Surface 1: Dashboard-UI — Fleet View

`renderFleet()` → fleet totals metrics → projects table + pool table via `fleetTable()`.

Calls MCP `fleet_snapshot` tool on first view activation.

## Surface 1: Dashboard-UI — Links View

`renderLinks()` → link groups grouped by ticket → memory rows with files/tags.

Calls MCP `link_snapshot` tool on first view activation.

## Surface 1: Dashboard-UI — Activity View

`renderActivity()` → notice banners (resync, dropped, has_more) → `renderTimeline()` event list.

## Surface 2: Fleet Dashboard — Overview (#/)

Entry: `tools/fleet-dashboard/fleet_dashboard.py` line 5828 (inline HTML constant `HTML`)

```
fleet_dashboard.py
├── HTML definition and patch sequence (lines 5828–6427)
│   ├── <style> — CSS custom properties, layout, components, responsive, print
│   ├── <body>
│   │   ├── .app-shell
│   │   │   ├── aside.sidebar (brand, primary-nav, sidebar-foot)
│   │   │   └── main
│   │   │       ├── #connection-banner
│   │   │       ├── .top (h1, view-controls, search, #state)
│   │   │       ├── #home-view
│   │   │       ├── #detail-view
│   │   │       └── dialog#help-overlay
│   │   └── <script> — SPA JavaScript
│   │       ├── Helpers (esc, fmt, href builders, matches, filters)
│   │       ├── State (fleetData, fleetErrors, centralLabels, detailData, etc.)
│   │       ├── Renderers (renderFleet, renderCentral, renderDetail, ticketView, etc.)
│   │       ├── API (fetchJson, fetchWithTimeout, loadCentrals, refreshCentral, etc.)
│   │       ├── Routing (route(), syncRoute())
│   │       └── Event listeners (hashchange, keyboard, search, theme, density)
│   └── HTML.replace() patches (lines 5892–6427) — legacy route overrides, new sections
│
├── Python HTTP handler methods (lines 6580–7284)
│   ├── do_GET — serves HTML, API endpoints
│   ├── do_POST — config, intake, worker, door, project APIs
│   └── _send — response helper
│
└── FleetCache, SeatManager, etc. — Python backend classes
```

### Fleet Dashboard: Board Detail (#/central/{central}/board/{id})

Renders via `renderDetail()` → tabs (Tickets, Timeline, Changes, Flow, Routes) → `intakePanel()` + `findings()` → view-specific renderer.

### Fleet Dashboard: Config (#/central/{central}/config)

Renders via `renderConfig()` → config form with thresholds, intake settings, category policies.

### Fleet Dashboard: Overhead (#/central/{central}/overhead)

Renders via `renderOverhead()` → model wait cost table + session pressure table + cumulative diagnostics.

### Fleet Dashboard: Agents Hub (#/agents)

Renders via `renderAgentsHub()` → agent cards with managed controls, pressure badges, log tails.

### Fleet Dashboard: Operations Hub (#/operations)

Renders via `renderOperationsHub()` → per-central operations cards with config/overhead/route links.

### Fleet Dashboard: Seats (#/seats)

Renders via `renderSeats()` → seat inventory table, add/update form, wait bridge status, doctor results, registry coverage, dispatch panels, release ops, import review.

## Surface 3: Extension Join/Settings

Entry: `tools/aionui-extension/webui/index.html`

```
webui/index.html
├── webui/style.css (link)
├── webui/app.js (script)
│   ├── form submit handler (POST /pursers/join)
│   ├── showStatus() (renders status card)
│   └── readJson() (fetch helper)
│
└── webui/routes.js (server-side, Node.js)
    ├── runBridge() (execFile pursers-wait-bridge)
    ├── parseJoin() / parseStatus()
    ├── status() (GET handler)
    └── join() (POST handler — calls bridge, imports MCP server)
```
