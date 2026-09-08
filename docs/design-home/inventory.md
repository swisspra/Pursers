# Dashboard surface inventory

Source baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (origin/main, 5.0.0a25).

## Surface summary

| ID | # | Surface | Source path | Lines | Build | Live entrypoint |
| --- | --- | --- | --- | ---: | --- | --- |
| `dashboard-ui.logic` | 1 | Dashboard-UI SPA | `tools/dashboard-ui/src/dashboard.ts` | 1528 | Vite single-file | `packages/personal/src/pursers_personal/resources/dashboard.html` |
| `dashboard-ui.styles` | 1 | Dashboard-UI CSS | `tools/dashboard-ui/src/dashboard.css` | 619 | Vite single-file | (bundled into dashboard.html) |
| `dashboard-ui.shell` | 1 | Dashboard-UI entry | `tools/dashboard-ui/dashboard-entry.html` | 145 | Vite single-file | (bundled into dashboard.html) |
| `fleet-dashboard.surface` | 2 | Fleet Dashboard | `tools/fleet-dashboard/fleet_dashboard.py` | 7507 | None (inline) | Served by HTTP handler |
| `extension-join.surface` | 3 | Extension Join/Settings | `tools/aionui-extension/webui/` | 360 | None (static) | `webui/index.html` |
| `personal-mcp.surface` | 4 | Personal MCP Server | `packages/personal/src/pursers_personal/apps_server.py` | 2401 | None | MCP tools + HTML resource |

### Extension file breakdown

| File | Lines |
| --- | --- |
| `webui/index.html` | 33 |
| `webui/app.js` | 60 |
| `webui/routes.js` | 201 |
| `webui/style.css` | 66 |
| **Total** | **360** |

## Authoritative source counts

The checker requires each source path to exist, then derives every count from the exact Git object at `c2ebac5de803a0f7a00468ec4d3cdf06e4719096`. Later integration-parent changes cannot silently redefine this artifact baseline.

<!-- source-counts:start -->
| Source path | Lines |
| --- | ---: |
| `tools/dashboard-ui/src/dashboard.ts` | 1528 |
| `tools/dashboard-ui/src/dashboard.css` | 619 |
| `tools/dashboard-ui/dashboard-entry.html` | 145 |
| `tools/dashboard-ui/vite.config.ts` | 11 |
| `tools/dashboard-ui/package.json` | 21 |
| `tools/dashboard-ui/tsconfig.json` | 13 |
| `tools/fleet-dashboard/fleet_dashboard.py` | 7507 |
| `tools/aionui-extension/aion-extension.json` | 99 |
| `tools/aionui-extension/webui/index.html` | 33 |
| `tools/aionui-extension/webui/app.js` | 60 |
| `tools/aionui-extension/webui/routes.js` | 201 |
| `tools/aionui-extension/webui/style.css` | 66 |
| `packages/personal/src/pursers_personal/apps_server.py` | 2401 |
<!-- source-counts:end -->

## States per surface

### Surface 1: Dashboard-UI

| ID | State | How rendered | Line range |
| --- | --- | --- | --- |
| `dashboard-ui.state.empty-tickets` | Empty (no tickets) | `renderToday()` with empty highlights | 750–774 |
| `dashboard-ui.state.loading` | Loading | `main.innerHTML = '<p class="empty">Loading…</p>'` in `render()` | 1194–1214 |
| `dashboard-ui.state.error` | Error | `renderConnection()` shows disconnected banner | 648–676 |
| `dashboard-ui.state.permission-denied` | Permission denied | `decodeSnapshot()` returns null; `render()` shows error | 367–427, 1194–1214 |
| `dashboard-ui.state.stale` | Stale data | `renderConnection()` shows `data.stale` flag | 648–676 |
| `dashboard-ui.state.search-empty` | Search empty | `renderSearch()` shows "No results" | 1231–1316 |

### Surface 2: Fleet Dashboard

| ID | State | How rendered | Function |
| --- | --- | --- | --- |
| `fleet-dashboard.state.empty-centrals` | Empty (no centrals) | `renderFleet()` shows skeleton | line 5863 |
| `fleet-dashboard.state.loading-board` | Loading board | `<p class="empty">Loading board detail…</p>` | `syncRoute()` line 5888 |
| `fleet-dashboard.state.error-board` | Error | `<p class="error">Board detail unavailable</p>` | `refreshDetail()` line 5887 |
| `fleet-dashboard.state.offline` | Disconnected | `markConnectionFailure()` shows banner | connection state module |
| `fleet-dashboard.state.bounded` | Bounded data | `<span class="status">bounded view</span>` | `renderCentral()` line 5860 |
| `fleet-dashboard.state.truncated-tickets` | Truncated tickets | `<span class="status">N of M tickets shown</span>` | `renderDetail()` line 5879 |
| `fleet-dashboard.state.edit-paused` | Edit-paused refresh | `#refresh-paused` indicator | `refreshPaused()` line 5861 |
| `fleet-dashboard.state.empty-workers` | Empty workers | `<tr><td colspan="6" class="empty">No API workers configured.</td></tr>` | `renderWorkers()` line 6001 |
| `fleet-dashboard.state.empty-agents` | Empty agents | `<p class="empty">No active agents available.</p>` | `renderAgentsHub()` line 6113 |
| `fleet-dashboard.state.routes-unavailable` | Bounded routes | `<span class="warning">Routes source unavailable.</span>` | `routesView()` line 5877 |

### Surface 3: Extension

| ID | State | How rendered | Location |
| --- | --- | --- | --- |
| `extension-join.state.initial` | Initial (no door) | Join form with intro text | `index.html` |
| `extension-join.state.joining` | Joining | `message.textContent = 'Joining…'` | `app.js` line 31 |
| `extension-join.state.joined` | Joined | `message.textContent = 'Joined and registered…'` + status card | `app.js` lines 42–43 |
| `extension-join.state.error` | Join failed | `message.textContent = result.install_hint \|\| 'Join failed…'` | `app.js` line 39 |
| `extension-join.state.bridge-missing` | Bridge missing | `install_hint: INSTALL_HINT` from routes.js | `routes.js` status() |
| `extension-join.state.status-loaded` | Status loaded | `showStatus()` fills status card fields | `app.js` `showStatus()` |
| `extension-join.state.status-empty` | Status empty | Status card hidden (`card.hidden = true`) | `app.js` initial state |

### Surface 4: Personal MCP Server

| ID | State | How rendered | Tool |
| --- | --- | --- | --- |
| `personal-mcp.state.board-empty` | Board empty | `board_snapshot` returns 0 tickets/agents | board_snapshot |
| `personal-mcp.state.not-onboarded` | Not onboarded | `board_onboard` required first | board_onboard |
| `personal-mcp.state.ticket-not-found` | Ticket not found | `ticket_get` returns error | ticket_get |
| `personal-mcp.state.memory-empty` | Memory empty | `memory_search` returns empty list | memory_search |
| `personal-mcp.state.fleet-unavailable` | Fleet unavailable | `fleet_snapshot` returns error | fleet_snapshot |
| `personal-mcp.state.links-empty` | No links | `link_snapshot` returns empty graph | link_snapshot |

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

## Deterministic artifact manifest

`context/source-manifest.json` declares every submitted documentation, literal-source, and bounded-excerpt artifact with exact line and byte counts. `check_artifacts.py` asserts every per-file value and the aggregate. The manifest file itself is the sole aggregate exclusion, so its expected values never count the file that stores them.

## Bounded context bundle for primary dashboard reproduction

To reproduce the Dashboard-UI primary surface, include the immutable documentation copies rather than rereading or uploading unbounded source:

1. `docs/design-home/context/excerpts/dashboard-ts.txt` — exact lines 537–636, 648–1214, and 1231–1400
2. `docs/design-home/context/raw/tools/dashboard-ui/src/dashboard.css` — literal 619-line stylesheet
3. `docs/design-home/context/raw/tools/dashboard-ui/dashboard-entry.html` — literal 145-line HTML shell
4. `docs/design-home/context/raw/tools/dashboard-ui/vite.config.ts` — literal build config
5. `docs/design-home/context/raw/tools/dashboard-ui/package.json` — literal dependency manifest
6. `docs/design-home/context/raw/tools/dashboard-ui/tsconfig.json` — literal TypeScript config

The extension context is also preserved literally under `docs/design-home/context/raw/tools/aionui-extension/`, including the 99-line manifest and all four HTML/CSS/JS inputs.

For files >900 lines, the artifacts provide:
- `dashboard.ts` (1528 lines): exact ranges 537–636, 648–1214, and 1231–1400 in `context/excerpts/dashboard-ts.txt`.
- `fleet_dashboard.py` (7507 lines): exact HTML definition and patch range 5828–6427 in `context/excerpts/fleet-dashboard-py.txt`; handler ranges 6580–6883 and 6885–7284 drive the checker-derived route matrices.
- `apps_server.py` (2401 lines): exact dashboard-server and MCP-tool range 1949–2401 in `context/excerpts/personal-apps-server-py.txt`.

## Coordinator materialization notes

To materialize `.superdesign/init/` without repeating discovery:

1. Copy the 6 files from `docs/design-home/context/` into `.superdesign/init/`:
   - `components.md`
   - `layouts.md`
   - `routes.md`
   - `theme.md`
   - `pages.md`
   - `extractable-components.md`
2. Use `context/source-manifest.json` to locate the exact literal-source and bounded-excerpt artifacts.
3. Reference `docs/design-home/inventory.md` for the surface inventory and state matrix.
4. The exact branch and commit SHA is in the submission notes.
5. `python3 docs/design-home/check_artifacts.py --negative-probes` verifies source identity, literal copies, excerpt ranges, complete route/method matrices, source counts, and the non-self-referential artifact total against baseline `c2ebac5de803a0f7a00468ec4d3cdf06e4719096`.
