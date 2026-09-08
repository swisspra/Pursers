# Routes and feature matrix

Source baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (origin/main, 5.0.0a25).

## Surface 1: Dashboard-UI (Vite + TypeScript SPA)

**Source:** `tools/dashboard-ui/src/dashboard.ts` (1528 lines); exact selected ranges 537–636, 648–1214, and 1231–1400 are preserved in `excerpts/dashboard-ts.txt`.
**Entry:** `tools/dashboard-ui/dashboard-entry.html`
**Build output:** `packages/personal/resources/dashboard.html` (Vite single-file)

### Tab views (data-driven, no hash router)

The dashboard-ui SPA uses a tab-based view switcher, not hash routing. Views are
selected by clicking tab buttons that call `switchPanel(name)`:

| View | Tab label | Render function | Line range |
| --- | --- | --- | --- |
| Today | Today | `renderToday()` | 750–774 |
| Work | Work | `renderWork()` | 805–844 |
| Agents | Agents | `renderAgents()` | 845–922 |
| Fleet | Fleet | `renderFleet()` | 923–1040 |
| Links | Links | `renderLinks()` | 1041–1134 |
| Activity | Activity | `renderActivity()` | 1151–1180 |

Additional render functions:

| Function | Purpose | Line range |
| --- | --- | --- |
| `renderConnection()` | Connection health banner | 648–676 |
| `renderActivityScope()` | Activity scope toggle | 677–696 |
| `renderHealth()` | Health metrics strip | 697–749 |
| `renderHighlight()` | Highlight card | 775–804 |
| `renderTimeline()` | Timeline event list | 1135–1150 |
| `renderActivePanel()` | Active panel dispatcher | 1181–1193 |
| `render()` | Top-level render dispatcher | 1194–1214 |
| `renderSearch()` | Global search results | 1231–1316 |

### Data refresh functions

| Function | Purpose | Line range |
| --- | --- | --- |
| `refreshSnapshot()` | Fetch board snapshot via MCP | 1317–1338 |
| `refreshFleet()` | Fetch fleet snapshot | 1339–1366 |
| `refreshLinks()` | Fetch link snapshot | 1367–1401 |
| `refreshFeed()` | Fetch event feed with backoff | 1402–1528 |

### Decoders (type-safe parsers)

| Function | Purpose | Line range |
| --- | --- | --- |
| `decodeAgent()` | Parse agent from raw | 280–301 |
| `decodeTicket()` | Parse ticket from raw | 302–319 |
| `decodeEvent()` | Parse board event | 320–335 |
| `decodeHighlight()` | Parse highlight card | 336–349 |
| `decodeSnapshot()` | Parse full board snapshot | 367–427 |
| `decodeFleetProject()` | Parse fleet project | 428–439 |
| `decodeFleetSeat()` | Parse fleet seat | 440–449 |
| `decodeFleetPoolEntry()` | Parse pool entry | 450–462 |
| `decodeFleetSnapshot()` | Parse fleet snapshot | 463–485 |
| `decodeLinkNode()` | Parse link node | 492–502 |
| `decodeLinkEdge()` | Parse link edge | 503–514 |
| `decodeLinkSnapshot()` | Parse link snapshot | 515–532 |

### PostMessageTransport MCP Apps bridge

The dashboard communicates with the host through `PostMessageTransport` (lines
545–636), which wraps `window.postMessage` / `window.addEventListener` for
MCP tool calls. The `structured()` helper (line 545) extracts structured content
from MCP results. `serializedWithinLimit()` (line 537) validates response size.

## Surface 2: Fleet Dashboard (inline Python HTML SPA)

**Source:** `tools/fleet-dashboard/fleet_dashboard.py` (7507 lines)
**HTML definition:** Inline string and patches at lines 5828–6427
**HTTP handlers:** `do_GET` at lines 6580–6883; `do_POST` at lines 6885–7284

### Hash routes (from patched `route()` function)

The `route()` function (line 5859) is progressively patched by hub extensions:

**Original routes (legacyRouteV1, line 5859):**

| Hash pattern | Kind | View |
| --- | --- | --- |
| `#/` or no hash | (null) → overview/home | Fleet overview |
| `#/central/<central>/board/<board>` | board | tickets (default) |
| `#/central/<central>/board/<board>/tickets` | board | tickets |
| `#/central/<central>/board/<board>/timeline` | board | timeline |
| `#/central/<central>/board/<board>/changes` | board | changes |
| `#/central/<central>/board/<board>/flow` | board | flow |
| `#/central/<central>/board/<board>/routes` | board | routes |
| `#/central/<central>/overhead` | overhead | overhead detail |
| `#/central/<central>/config` | config | coordinator config |
| `#/board/<board>` | board | tickets (uses defaultCentral) |
| `#/board/<board>/tickets` | board | tickets |
| `#/board/<board>/timeline` | board | timeline |
| `#/board/<board>/changes` | board | changes |
| `#/board/<board>/flow` | board | flow |
| `#/board/<board>/routes` | board | routes |
| `#/config` | config | config (uses defaultCentral) |

**Hub patch (line 6101, replaces route function):**

| Hash pattern | Kind |
| --- | --- |
| `#/boards` | boards (hub overview) |
| `#/agents` | agents (unified agent pool) |
| `#/operations` | operations (policy, overhead, routes) |

**Workers patch (lines 5963–6006, patches route + adds syncWorkersRoute):**

| Hash pattern | Kind |
| --- | --- |
| `#/central/<central>/workers` | workers (API worker management) |

**Seats patch (line 6149–6152, replaces route function again):**

| Hash pattern | Kind |
| --- | --- |
| `#/seats` | seats (seat inventory) |

**Keyboard shortcuts (line 5889, patched at 5905, 5975–5976):**

| Key | Action |
| --- | --- |
| `g` then `f` | Go home (`#/`) |
| `g` then `o` | Go to overhead |
| `g` then `c` | Go to config |
| `g` then `w` | Go to workers |
| `g` then `r` | Go to routes (if on a board) |
| `/` | Focus search/filter |
| `?` | Show keyboard help dialog |
| `Escape` | Close search/help |

### GET API endpoints

| Route | Purpose | Handler line |
| --- | --- | --- |
| `/` | Serve HTML page | 6582 |
| `/api/centrals` | List configured centrals | 6585 |
| `/api/fleet` | Fleet snapshot (all centrals) | 6639 |
| `/api/overhead` | Context pressure per agent | 6730 |
| `/api/config` | Coordinator config read | 6765 |
| `/api/config/seats` | Seat inventory | 6603 |
| `/api/config/bridge` | Bridge install/upgrade status | 6605 |
| `/api/config/release` | Release operations status | 6607 |
| `/api/attention` | Attention state | 6609 |
| `/api/config/registry` | Project registry | 6648 |
| `/api/doors` | Door inventory | 6665 |
| `/api/intake` | Intake queues | 6774 |
| `/api/dispatch` | Dispatch history/timing | 6796 |
| `/api/workers` | API workers list | 6818 |
| `/api/config/jobs/<hash>` | Poll async config job | 6591 |

### POST API endpoints

| Route | Purpose | Handler line |
| --- | --- | --- |
| `/api/config/plan` | Preview config changes | 6965 |
| `/api/config/suggestions` | Config suggestions | 6967 |
| `/api/config/apply` | Apply config changes | 6969 |
| `/api/config/prompt` | Render prompt preview | 6973 |
| `/api/config/doctor` | Run Doctor check | 6975 |
| `/api/config/import` | Import discovered seats | 6984 |
| `/api/config/bridge/install` | Install wait bridge | 6988 |
| `/api/config/bridge/upgrade-all` | Upgrade all bridges | 6992 |
| `/api/config/ops/plan` | Release ops plan | 6996 |
| `/api/config/ops` | Release operations execute | 7000 |
| `/api/config/registry/clone` | Create/fetch registry clone | 7009 |
| `/api/dispatch` | Dispatch info write | 7032 |
| `/api/agents/retire` | Retire one agent | 7046 |
| `/api/agents/retire-inert` | Retire inert agents | 7059 |
| `/api/attention` | Acknowledge/snooze attention | 7067 |
| `/api/human/resolve` | Resolve human-input request | 7069 |
| `/api/intake` | Submit/decide intake | 7264 |
| `/api/doors/copy` | Copy-once door | 7093 |
| `/api/doors/rotate` | Rotate door | 7104 |
| `/api/projects/add` | Add project to registry | 7115 |
| `/api/workers` | Create/update API worker | 7137 |
| `/api/config` | Update coordinator config | 7161 |
| `/api/workers/<name>/<action>` | Worker test/start/stop/restart | 6909 |

**Config routes set** (line 6920): routes that accept POST with config body size
limit. Includes `/api/config`, `/api/config/plan`, `/api/config/apply`,
`/api/config/suggestions`, `/api/config/prompt`, `/api/config/doctor`,
`/api/config/import`, `/api/config/bridge/install`,
`/api/config/bridge/upgrade-all`, `/api/config/ops/plan`, `/api/config/ops`,
`/api/config/registry/clone`.

**Worker action regex** (line 6909):
`/api/workers/([a-z0-9-]{2,32})/(test|start|stop|restart)`

## Surface 3: Extension (AionUI settings tab)

**Source:** `tools/aionui-extension/` (4 webui files, 360 lines total, plus the 99-line extension manifest)

### API routes (from `aion-extension.json` webui.apiRoutes)

| Route | Method | Handler | Description |
| --- | --- | --- | --- |
| `/pursers/join` | POST | `webui/routes.js:join()` | Store door, register wait bridge |
| `/pursers/status` | GET | `webui/routes.js:status()` | Read redacted wait-bridge status |

### Static assets

| URL prefix | Directory | Description |
| --- | --- | --- |
| `/pursers/assets` | `webui/` | Join tab HTML, CSS, JS |

### Extension contribution surface

| Contribution | Entry | Details |
| --- | --- | --- |
| Settings tab | `webui/index.html` | "Pursers" tab, order=80, after "tools" |
| Assistant presets | 4 presets | worker-codex, worker-claude, reviewer-codex, reviewer-claude |
| API routes | 2 routes | join (POST), status (GET) |

### Bridge commands (from `routes.js`)

The extension calls `pursers-wait-bridge` (BRIDGE_COMMAND, line 6) with:
- `join <door>` — validates and stores door, onboards seat
- `status` — reads board, role, key ID, expiry, seat names, push mode

After join, the extension registers the bridge as a stdio MCP server through
AionUI's `POST /api/mcp/servers/import` endpoint (loopback-only).

## Surface 4: Personal MCP Server

**Source:** `packages/personal/src/pursers_personal/apps_server.py` (2401 lines)

### MCP tools (24 total, `@apps.tool` decorated)

| # | Tool name | Line | Description |
| --- | --- | --- | --- |
| 1 | board_snapshot | 1974 | Live board projection |
| 2 | fleet_snapshot | 1982 | Fleet-wide snapshot |
| 3 | link_snapshot | 1993 | Memory link graph |
| 4 | board_event_feed | 2004 | Bounded event feed |
| 5 | board_onboard | 2015 | Join board, get context |
| 6 | board_status | 2036 | Board health and counts |
| 7 | board_catchup | 2044 | Read journal page (touch/ack) |
| 8 | ticket_get | 2064 | Read one ticket |
| 9 | ticket_list | 2072 | List tickets with filters |
| 10 | ticket_create | 2091 | Create a ticket |
| 11 | ticket_claim | 2126 | Claim a ticket |
| 12 | ticket_submit | 2134 | Submit completed work |
| 13 | ticket_review | 2155 | Record review verdict |
| 14 | lease_renew | 2174 | Renew ticket lease |
| 15 | ticket_cancel | 2182 | Cancel a ticket |
| 16 | memory_write | 2192 | Write project memory |
| 17 | memory_read | 2223 | Read project memory |
| 18 | memory_search | 2248 | Search project memory |
| 19 | memory_links | 2263 | Traverse memory relationships |
| 20 | memory_checkpoint | 2286 | Record continuation checkpoint |
| 21 | memory_handoff | 2311 | Record handoff with next steps |
| 22 | memory_unpin | 2330 | Unpin memory entry |
| 23 | board_state_get | 2340 | Read board-state values |
| 24 | board_state_update | 2348 | Update board-state value |

### Tool visibility classification

- **PRIMARY_UI** (4): board_snapshot, board_event_feed, fleet_snapshot, link_snapshot
- **MODEL/CHAT** (16): board_onboard, board_status, board_catchup, ticket_get,
  ticket_list, ticket_create, ticket_claim, ticket_submit, ticket_review,
  lease_renew, ticket_cancel, memory_write, memory_read, memory_search,
  memory_links, board_state_get
- **MODEL_ONLY** (4): board_catchup, memory_checkpoint, memory_handoff,
  memory_unpin (visibility=MODEL_ONLY on decorator)
- **Public** (2): board_state_get, board_state_update

### HTML resource serving

The server also serves the built dashboard HTML as a resource at `UI_URI`,
providing the MCP App UI surface for compatible hosts.

## Duplicate frontend analysis

| Surface | Build step | Live entrypoint | Duplicates? |
| --- | --- | --- | --- |
| Dashboard-UI | Vite single-file build | `packages/personal/resources/dashboard.html` | Source of truth |
| Fleet Dashboard | None (inline HTML in Python) | Served by `fleet_dashboard.py` | Self-contained, no overlap |
| Extension | None (static files) | `webui/index.html` | Independent |
| Personal MCP | Serves built dashboard.html | MCP App resource | Consumes dashboard-ui build output |

The dashboard-ui source (`dashboard-entry.html` + `dashboard.ts` + `dashboard.css`)
builds via Vite single-file to `packages/personal/resources/dashboard.html`.
The Personal MCP server serves this built file as an HTML resource. The fleet
dashboard is self-contained inline HTML with no dependency on dashboard-ui.
The extension has no build step and no overlap with either.
