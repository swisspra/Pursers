# Pursers Dashboard Surface Inventory

Complete inventory of every rendered surface across the Pursers project, including hidden/admin/detail states, duplicate frontends, and actual live entrypoints.

## Surface 1: Dashboard-UI (Personal Board)

**Location:** `tools/dashboard-ui/`

| File | Lines | Purpose |
|------|-------|---------|
| `dashboard-entry.html` | ~290 lines | Entry HTML with semantic structure for all 6 views |
| `src/dashboard.ts` | ~820 lines | Full TypeScript SPA logic: decoding, rendering, refresh, search, host integration |
| `src/dashboard.css` | ~290 lines | Complete CSS with dark/light themes, responsive, accessibility |
| `package.json` | — | Vite + vite-plugin-singlefile build config |
| `vite.config.ts` | — | Vite single-file build → `dist/dashboard-entry.html` |

**Build output:** `packages/personal/src/pursers_personal/resources/dashboard.html` (single-file, ~350+ lines, inlined JS+CSS+HTML)

**Live entrypoint:** Served by Personal MCP server as `apps.add_html_resource(UI_URI, ...)` with `title="On Board Personal"`, `csp=ResourceCsp()`, `prefers_border=True`.

**Views:**

| View | Elements | States |
|------|----------|--------|
| Today | health-card, metrics-grid (4), today-work (4 tickets), today-agents (4), latest-handoff, important-pinned, recent-activity (5 events) | loading, demo, demo-error, stale, live |
| Work | work-total badge, work-notice (truncation warning), 6 status groups (Open/Working/Submitted/Needs attention/Done/Ended + Other) | empty (no tickets), truncated (500 cap) |
| Agents | agents-total badge, agents-notice (truncation), agents-grid (auto-fit cards) | empty (no agents), stale (grayscale filter), duplicate-name warning |
| Fleet | fleet-warning, fleet-metrics (4), projects table (6 cols), pool table (3 cols) | unavailable (tool not exposed), no projects, no pool entries |
| Links | links-total badge, links-notice (truncation), link-groups (by ticket) | unavailable (tool not exposed), no links, no files/tags, suggested authority |
| Activity | activity-total badge, activity-notice (resync/dropped/has_more), timeline | empty (no activity), resync_notice, dropped_events, has_more |

**State machine:**
- `data-scenario`: `demo` (synthetic), `live` (connected)
- `data-render-state`: `loading`, `demo`, `demo-error`, `error`, `stale`
- `data_mode`: `live`, `demo`, `demo-error`, `stale`
- `connected`: boolean (host bridge connection)
- `stale`: boolean (cached view)
- `feed_error`: string|null

**Empty/loading/error/permission states:**

| State | Desktop | Mobile | Implementation |
|-------|---------|--------|----------------|
| Loading | `main[aria-busy="true"]` → opacity 0.72 | same | `setLoading(true)` sets aria-busy, connection-title="Loading authorized board state" |
| Demo data | connection-banner tone="demo" (warning dot), health-card tone="demo" | same | `renderConnection()` + `renderHealth()` with `data_mode === "demo"` |
| Demo error | connection-banner tone="error" (danger dot), health-card tone="stale" | same | `data_mode === "demo-error"` — synthetic connection-error simulation |
| Stale/error | connection-banner tone="stale" or "error", health-card tone="stale" | same | `data.stale` or `data.feed_error` — last-known state remains visible |
| Live | connection-banner tone="live" (success dot) | same | `data_mode === "live"` && `!data.stale` |
| Empty (per view) | `emptyState()` — bold title + muted detail text | same | Each view checks for empty data arrays and renders emptyState |
| Truncation notice | `.notice` with tone="warning" | same | `ticket_truncated`, `agent_truncated`, `data.truncated` |
| Search no results | `emptyState("No matching loaded data", ...)` | same | `renderSearch()` renders emptyState when no matches |
| Fleet unavailable | `emptyState("Fleet data unavailable", ...)` | same | `fleetUnavailable` flag when `fleet_snapshot` tool not exposed |
| Links unavailable | `emptyState("Links unavailable", ...)` | same | `linksUnavailable` flag when `link_snapshot` tool not exposed |
| Host bridge error | `render(failureSnapshot("Host bridge error"))` | same | `app.onerror` handler |
| Not connected (pre-connect) | fallback snapshot with demo data | same | `render(fallback)` at module init |

**Responsive breakpoints:**
- `≤900px`: header stacks vertical, hero-grid → 1 col, today-grid → 2 cols, page padding → 18px
- `≤620px`: all grids → 1 col, metrics → 2 cols, command-bar kbd hidden, search-count wraps, footer stacks, page padding → 14px

**Accessibility:**
- Skip link (visible on focus)
- `aria-busy` on main during loading
- `role="tablist"` / `role="tab"` / `role="tabpanel"` for view tabs
- `aria-live="polite"` on connection banner, search count, source text
- `aria-selected` on active tab
- Keyboard navigation: Arrow Left/Right, Home, End for tabs; `/` for search focus; Escape to clear search
- `@media (prefers-reduced-motion: reduce)` disables animations
- `@media (forced-colors: active)` forces visible borders
- Min button height 44px

## Surface 2: Fleet Dashboard

**Location:** `tools/fleet-dashboard/fleet_dashboard.py` (453 KB, single Python file)

**Type:** Python HTTP server (`BaseHTTPRequestHandler`) serving an inline JavaScript SPA.

**HTML constant:** `HTML = r"""..."""` starting at line ~5828, patched via `HTML.replace()` calls through line ~6173+.

**Layout:** Sidebar (brand, nav: Overview/Boards/Agents/Operations) + main content area

**Key JS functions:**

| Function | Lines (approx) | Purpose |
|----------|----------------|---------|
| `route()` | ~5950 | Hash route parser |
| `renderFleet()` | ~5960 | Home view: per-central sections |
| `renderCentral()` | ~5953 | Per-central: board cards, agent pool, retire drawer |
| `renderDetail()` | ~5930 | Board detail: tabs + intake + findings + view |
| `ticketView()` | ~5900 | Sortable ticket table with expandable detail |
| `timelineView()` | ~5905 | Event timeline grouped by day/ticket |
| `changesView()` | ~5874 | Change summary metrics (created/claimed/submitted/closed/rejected) |
| `flowView()` | ~5940 | 4-column flow board |
| `routesView()` | ~5944 | Ticket provenance + per-seat load |
| `renderConfig()` | ~5942 | Coordinator config form |
| `renderOverhead()` | ~5908 | Session context pressure + bridge diagnostics |
| `renderOverview()` | ~6106 | Fleet overview (hub route) |
| `renderBoardsHub()` | ~6107 | Board workspace cards (hub route) |
| `renderAgentsHub()` | ~6113 | Unified agent pool (hub route) |
| `renderOperationsHub()` | ~6114 | Operations cards (hub route) |
| `renderSeats()` | ~6213 | Seat config, dispatch, doctor, registry |
| `renderReleaseOps()` | ~6204 | Release & operations card |
| `intakePanel()` | ~5934 | New-ask intake form + pending asks |
| `fleetTable()` (fleet dashboard) | inline | Table builder helper |
| `pressureBadge()` | inline | Context pressure status badge |
| `pageHead()` | ~6104 | Page header with kicker/title/copy/action |

**API endpoints:** See `routes.md` for complete list (20+ GET, 20+ POST routes).

**Empty/loading/error/permission states:**

| State | Desktop | Mobile | Implementation |
|-------|---------|--------|----------------|
| Loading | `<p class="empty">Loading board detail…</p>` | same | `syncRoute()` sets loading placeholder |
| Central unavailable | `<p class="error">Unavailable: ${error}</p>` | same | `fleetErrors[label]` rendered in central section |
| Board unavailable | `<p class="error">Board detail unavailable...</p>` | same | `refreshDetail()` catch block |
| Overhead unavailable | `<p class="error">Overhead unavailable...</p>` | same | `refreshOverhead()` catch block |
| No tickets match | `<td colspan="4" class="empty">No tickets match the filter.</td>` | same | `ticketView()` empty tbody |
| No boards match | `<p class="empty">No boards match the filter.</p>` | same | `renderCentral()` empty grid |
| No agents match | `<p class="empty">No active agents match the filter.</p>` | same | `renderCentral()` empty pool |
| No timeline events | `<p class="empty">No timeline events match the filter.</p>` | same | `timelineView()` empty groups |
| Nothing needs attention | `<p class="empty">Nothing needs attention. The fleet is calm.</p>` | same | `renderOverview()` empty attention |
| No current findings | `<p class="empty">No current findings</p>` | same | `findings()` empty list |
| Connection error | `#connection-banner` (sticky, warning border) | same | `connectionFailures` set + `updateConnectionState()` |
| Refresh paused | `#refresh-paused` fixed pill + `#state[data-paused]` | same | `refreshPaused()` — paused when editing forms |
| Loopback required (POST) | `{"error":"loopback required"}` (403) | same | `do_POST()` config route guard |
| Central not found | `{"error":"central not found"}` (404) | same | `do_GET()` / `do_POST()` guard |
| Search no results | `<p class="empty">No results</p>` | same | `renderSearchResults()` empty |

**Responsive:** `@media(max-width:800px)` — strips → 2 cols, grids/flow → 1 col, `.hide-small` hidden, agent summary → 2 cols, search → full width

## Surface 3: Extension Join/Settings

**Location:** `tools/aionui-extension/webui/`

| File | Lines | Purpose |
|------|-------|---------|
| `index.html` | ~40 lines | Join form + status card |
| `app.js` | ~50 lines | Form handler, status fetcher |
| `routes.js` | ~170 lines | Server-side Node.js route handler |
| `style.css` | ~50 lines | Fixed dark theme CSS |
| `aion-extension.json` | ~80 lines | Extension manifest with settingsTab, apiRoutes, staticAssets |
| `contexts/worker.md` | — | Worker context preset |
| `contexts/reviewer.md` | — | Reviewer context preset |

**Live entrypoint:** Registered as AionUI settings tab "Pursers" at `webui/index.html`. Served via extension static assets at `/pursers/assets/`.

**States:**

| State | Implementation |
|-------|----------------|
| No seats | Status card hidden, shows join form only |
| Joining | `message.textContent = 'Joining…'` |
| Join success | `message.textContent = 'Joined and registered...'`, status card shown |
| Join failure | `message.textContent = result.install_hint \|\| 'Join failed...'` |
| Bridge not installed | API returns `{"ok":false,"error":"bridge_not_installed","install_hint":...}` → 503 |
| Join failed | API returns `{"ok":false,"error":"join_failed"}` → 422 |
| Invalid door | API returns `{"ok":false,"error":"invalid_door"}` → 400 |
| Invalid JSON | API returns `{"ok":false,"error":"invalid_json"}` → 400 |

## Surface 4: Personal MCP Server

**Location:** `packages/personal/src/pursers_personal/apps_server.py` (~800+ lines)

**Type:** Python MCP server with 24 tools and 1 HTML resource.

**Serves:** `dashboard.html` (built dashboard-ui) via `apps.add_html_resource(UI_URI, load_dashboard_html(), ...)`.

**Backend classes:**

| Class/Module | Purpose |
|--------------|---------|
| `LiveDashboard` | Stateful dashboard: projection cache, event subscription, feed, fleet/links |
| `build_dashboard_server()` | Factory: creates MCPServer + LiveDashboard with all MCP tools |
| `build_personal_server()` | Factory: loads profile, builds server with review policy |
| `load_dashboard_html()` | Loads `resources/dashboard.html` |

**Additional source files in `packages/personal/`:**

| File | Purpose |
|------|---------|
| `src/pursers_personal/cli.py` | CLI entry point |
| `src/pursers_personal/profile.py` | Profile loading and context resolution |
| `src/pursers_personal/integration.py` | Integration helpers |
| `src/pursers_personal/artifacts.py` | Artifact management |
| `src/pursers_personal/resources/component-lock.json` | Component lock for build verification |

## Logo / Assets Inventory

| Surface | Logo/Mark | Type | License/Source |
|---------|-----------|------|----------------|
| Dashboard-UI | "OB" text in `.product-mark` (CSS-generated, no image) | CSS gradient box with border | Original, no external asset |
| Fleet Dashboard | "P" text in `.brand-mark` (CSS-generated) | CSS styled span | Original, no external asset |
| Extension | None | — | — |

**No image assets (SVG, PNG, ICO, JPG, WebP) found in the repository.**

## Third-Party Dependencies

| Package | Version | License | Used By |
|---------|---------|---------|---------|
| `@modelcontextprotocol/ext-apps` | 1.7.5 | Apache-2.0, MIT, CC-BY-4.0 | Dashboard-UI (MCP Apps SDK) |
| `@modelcontextprotocol/sdk` | 1.30.0 | MIT | Dashboard-UI (MCP TypeScript SDK) |
| `vite` | 8.2.1 | MIT | Dashboard-UI (build tool) |
| `vite-plugin-singlefile` | 2.3.3 | MIT | Dashboard-UI (build plugin) |
| `typescript` | 7.0.2 | Apache-2.0 | Dashboard-UI (type checking) |

License file: `packages/personal/LICENSE` (Apache 2.0)
Third-party notices: `packages/personal/THIRD_PARTY_NOTICES.md`
Additional licenses: `packages/personal/licenses/EXT_APPS_LICENSE`

## Total Context Size

| Artifact | Lines | Approx KB |
|----------|-------|-----------|
| `components.md` | ~221 | ~9 KB |
| `layouts.md` | ~150 | ~6 KB |
| `routes.md` | ~151 | ~7 KB |
| `theme.md` | ~200 | ~8 KB |
| `pages.md` | ~156 | ~6 KB |
| `extractable-components.md` | ~175 | ~8 KB |
| `inventory.md` (this file) | ~200 | ~9 KB |
| **Total** | ~1253 | **~53 KB** |

## Bounded Context Bundle for Primary Dashboard Reproduction

The primary dashboard is `dashboard-ui` (the personal board view). To reproduce it for Superdesign:

**Required files (all <900 lines, included in full):**
1. `tools/dashboard-ui/dashboard-entry.html` — 290 lines (full HTML structure)
2. `tools/dashboard-ui/src/dashboard.ts` — ~820 lines (full TS source)
3. `tools/dashboard-ui/src/dashboard.css` — ~290 lines (full CSS source)

**Context token range references (larger files, ranges only):**
- `tools/fleet-dashboard/fleet_dashboard.py` — 453 KB, ~6200+ lines. Key ranges:
  - Lines 5828-5892: HTML document + CSS
  - Lines 5892-6173: HTML.replace() patches (legacy route + hub extensions)
  - Lines 5950-5960: route() function
  - Lines 6104-6114: hub renderers (overview, boards, agents, operations)
  - Lines 6435-6950: Python HTTPHandler (do_GET, do_POST)
- `packages/personal/src/pursers_personal/apps_server.py` — ~800+ lines. Key ranges:
  - `LiveDashboard` class: projection, view subscription, event handling
  - `build_dashboard_server()`: MCP tool registration + HTML resource
  - `build_personal_server()` / `run_personal_mcp()`: profile-backed factory

**Git baseline:** origin/main at `c2ebac5` (release: consolidate 5.0.0a25 train)

## Coordinator Materialization Notes

To materialize `.superdesign/init/` without repeating discovery:
1. Copy `docs/design-home/context/components.md` → `.superdesign/init/components.md`
2. Copy `docs/design-home/context/layouts.md` → `.superdesign/init/layouts.md`
3. Copy `docs/design-home/context/routes.md` → `.superdesign/init/routes.md`
4. Copy `docs/design-home/context/theme.md` → `.superdesign/init/theme.md`
5. Copy `docs/design-home/context/pages.md` → `.superdesign/init/pages.md`
6. Copy `docs/design-home/context/extractable-components.md` → `.superdesign/init/extractable-components.md`
7. Reference `docs/design-home/inventory.md` for the full surface inventory

All six init artifacts are non-empty and contain actual code, route mappings, theme tokens, and dependency trees.
