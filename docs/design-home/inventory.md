# Dashboard surface inventory

Source baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (origin/main, 5.0.0a25).

## Surface summary

| # | Surface | Source path | Lines | Build | Live entrypoint |
| --- | --- | --- | --- | --- | --- |
| 1 | Dashboard-UI SPA | `tools/dashboard-ui/src/dashboard.ts` | 1528 | Vite single-file | `packages/personal/src/pursers_personal/resources/dashboard.html` |
| 1 | Dashboard-UI CSS | `tools/dashboard-ui/src/dashboard.css` | 619 | Vite single-file | (bundled into dashboard.html) |
| 1 | Dashboard-UI entry | `tools/dashboard-ui/dashboard-entry.html` | 145 | Vite single-file | (bundled into dashboard.html) |
| 2 | Fleet Dashboard | `tools/fleet-dashboard/fleet_dashboard.py` | 7507 | None (inline) | Served by HTTP handler |
| 3 | Extension Join/Settings | `tools/aionui-extension/webui/` | 360 | None (static) | `webui/index.html` |
| 4 | Personal MCP Server | `packages/personal/src/pursers_personal/apps_server.py` | 2401 | None | MCP tools + HTML resource |

### Extension file breakdown

| File | Lines |
| --- | --- |
| `webui/index.html` | 33 |
| `webui/app.js` | 60 |
| `webui/routes.js` | 201 |
| `webui/style.css` | 66 |
| **Total** | **360** |

## States per surface

### Surface 1: Dashboard-UI

| State | How rendered | Line range |
| --- | --- | --- |
| Empty (no tickets) | `renderToday()` with empty highlights | 750–774 |
| Loading | `main.innerHTML = '<p class="empty">Loading…</p>'` in `render()` | 1194–1214 |
| Error | `renderConnection()` shows disconnected banner | 648–676 |
| Permission denied | `decodeSnapshot()` returns null; `render()` shows error | 367–427, 1194–1214 |
| Stale data | `renderConnection()` shows `data.stale` flag | 648–676 |
| Search empty | `renderSearch()` shows "No results" | 1231–1316 |

### Surface 2: Fleet Dashboard

| State | How rendered | Function |
| --- | --- | --- |
| Empty (no centrals) | `renderFleet()` shows skeleton | line 5863 |
| Loading board | `<p class="empty">Loading board detail…</p>` | `syncRoute()` line 5888 |
| Error | `<p class="error">Board detail unavailable</p>` | `refreshDetail()` line 5887 |
| Disconnected | `markConnectionFailure()` shows banner | connection state module |
| Bounded data | `<span class="status">bounded view</span>` | `renderCentral()` line 5860 |
| Truncated tickets | `<span class="status">N of M tickets shown</span>` | `renderDetail()` line 5879 |
| Edit-paused refresh | `#refresh-paused` indicator | `refreshPaused()` line 5861 |
| Empty workers | `<tr><td colspan="6" class="empty">No API workers configured.</td></tr>` | `renderWorkers()` line 6001 |
| Empty agents | `<p class="empty">No active agents available.</p>` | `renderAgentsHub()` line 6113 |
| Bounded routes | `<span class="warning">Routes source unavailable.</span>` | `routesView()` line 5877 |

### Surface 3: Extension

| State | How rendered | Location |
| --- | --- | --- |
| Initial (no door) | Join form with intro text | `index.html` |
| Joining | `message.textContent = 'Joining…'` | `app.js` line 30 |
| Joined | `message.textContent = 'Joined and registered…'` + status card | `app.js` line ~35 |
| Join failed | `message.textContent = result.install_hint \|\| 'Join failed…'` | `app.js` line ~32 |
| Bridge missing | `install_hint: INSTALL_HINT` from routes.js | `routes.js` status() |
| Status loaded | `showStatus()` fills status card fields | `app.js` `showStatus()` |
| Status empty | Status card hidden (`card.hidden = true`) | `app.js` initial state |

### Surface 4: Personal MCP Server

| State | How rendered | Tool |
| --- | --- | --- |
| Board empty | `board_snapshot` returns 0 tickets/agents | board_snapshot |
| Not onboarded | `board_onboard` required first | board_onboard |
| Ticket not found | `ticket_get` returns error | ticket_get |
| Memory empty | `memory_search` returns empty list | memory_search |
| Fleet unavailable | `fleet_snapshot` returns error | fleet_snapshot |
| No links | `link_snapshot` returns empty graph | link_snapshot |

## Logo and assets inventory

| Asset type | Found? | Details |
| --- | --- | --- |
| Logo image | No | None found in any surface |
| Favicon | No | None found |
| App icon | No | None found |
| CSS text marks | Yes | Dashboard-UI: `.product-mark` CSS-generated text "OB"; Fleet Dashboard: `.brand-block` text "Pursers"; Extension: `<h1>Pursers</h1>` text |
| Third-party images | No | None found |
| Font files | No | System font stack only (`ui-sans-serif, system-ui, -apple-system, sans-serif`) |

All visual identity is CSS-generated text, not image assets. No licenses
required for text marks.

## Third-party dependencies

| Package | Version | License | Used by |
| --- | --- | --- | --- |
| `@modelcontextprotocol/ext-apps` | 1.7.5 | MIT | Dashboard-UI (PostMessageTransport) |
| `@modelcontextprotocol/sdk` | 1.30.0 | MIT | Dashboard-UI (MCP client) |
| `vite` | 8.2.1 | MIT | Dashboard-UI (build) |
| `vite-plugin-singlefile` | 2.3.3 | MIT | Dashboard-UI (single-file output) |
| `mcp` (Python) | 2.1.1 | MIT | Wait bridge, Personal MCP server |

## Context line counts

| Artifact | Exact lines |
| --- | ---: |
| components.md | 223 |
| layouts.md | 152 |
| routes.md | 290 |
| theme.md | 200 |
| pages.md | 158 |
| extractable-components.md | 177 |
| inventory.md (this file) | 150 |
| **Total** | **1350** |

The total covers these seven Markdown artifacts only. Byte totals are intentionally omitted so this manifest does not depend on a self-referential byte count.

## Bounded context bundle for primary dashboard reproduction

To reproduce the Dashboard-UI primary surface, include:

1. `tools/dashboard-ui/src/dashboard.ts` (1528 lines) — full TypeScript SPA logic
2. `tools/dashboard-ui/src/dashboard.css` (619 lines) — full CSS with dark/light themes
3. `tools/dashboard-ui/dashboard-entry.html` (145 lines) — HTML shell
4. `tools/dashboard-ui/vite.config.ts` — Vite build config
5. `tools/dashboard-ui/package.json` — dependency manifest
6. `tools/dashboard-ui/tsconfig.json` — TypeScript config

For files >900 lines, the artifacts provide:
- `dashboard.ts` (1528 lines): bounded render/token ranges for each function
  (see components.md and pages.md for exact line ranges per function)
- `fleet_dashboard.py` (7507 lines): key HTML constant and HTTP handler ranges
  (see routes.md for exact route/handler line numbers)
- `apps_server.py` (2401 lines): tool name, line, and description table
  (see routes.md for complete tool inventory)

## Coordinator materialization notes

To materialize `.superdesign/init/` without repeating discovery:

1. Copy the 6 files from `docs/design-home/context/` into `.superdesign/init/`:
   - `components.md`
   - `layouts.md`
   - `routes.md`
   - `theme.md`
   - `pages.md`
   - `extractable-components.md`
2. Reference `docs/design-home/inventory.md` for the surface inventory and
   state matrix.
3. The exact branch and commit SHA is in the submission notes.
4. All file paths, line counts, and route inventories are verified against
   baseline `c2ebac5de803a0f7a00468ec4d3cdf06e4719096`.
