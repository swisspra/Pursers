# Routes — Pursers Dashboard Surfaces

Full route/feature coverage matrix across all dashboard surfaces.

## Surface 1: Dashboard-UI (Personal Board)

Source: `tools/dashboard-ui/src/dashboard.ts` — tab-based SPA, no URL routing. Views switched via `selectView()`.

| View | Tab ID | Panel ID | Description | Key Functions |
|------|--------|----------|-------------|---------------|
| Today | `tab-today` | `view-today` | Project health, metrics, current work, agents, handoff, pinned note, recent activity | `renderToday()`, `renderHealth()`, `ticketCounts()` |
| Work | `tab-work` | `view-work` | All tickets grouped by status (Open, Working, Submitted, Needs attention, Done, Ended) | `renderWork()` |
| Agents | `tab-agents` | `view-agents` | Agent roster with status, role, platform, project, current ticket | `renderAgents()` |
| Fleet | `tab-fleet` | `view-fleet` | Organization-wide fleet snapshot: projects table + agent pool table | `renderFleet()` |
| Links | `tab-links` | `view-links` | Ticket/memory/file/tag relationships from memory_links | `renderLinks()` |
| Activity | `tab-activity` | `view-activity` | Bounded event feed timeline | `renderActivity()`, `renderTimeline()` |

### Search (overlay)

`renderSearch()` — global search across tickets, agents, projects, pool entries, links, events, highlights. Activated by `/` key. Results shown in `#search-results` panel.

### Data Refresh Cycle

- `refreshSnapshot()` — calls MCP `board_snapshot` tool
- `refreshFeed()` — calls MCP `board_event_feed` tool (scheduled timer, exponential backoff on failure)
- `refreshFleet()` — calls MCP `fleet_snapshot` tool (on demand when Fleet tab selected)
- `refreshLinks()` — calls MCP `link_snapshot` tool (on demand when Links tab selected)

## Surface 2: Fleet Dashboard

Source: `tools/fleet-dashboard/fleet_dashboard.py` — hash-based SPA routing.

### Hash Routes

| Route Pattern | Kind | Description |
|---------------|------|-------------|
| `#/` | overview | Fleet overview: health cards per central, needs attention, waiting-for-you |
| `#/boards` | boards-hub | Board workspace cards with quick links |
| `#/agents` | agents-hub | Unified agent pool: live workers, API agents, controls, logs |
| `#/operations` | operations-hub | Operations: config, overhead, routes per central |
| `#/central/{central}/board/{id}` | board-detail | Board workspace with tabs |
| `#/central/{central}/board/{id}/tickets` | board-view | Ticket list with expandable detail rows |
| `#/central/{central}/board/{id}/timeline` | board-view | Event timeline grouped by day and ticket |
| `#/central/{central}/board/{id}/changes` | board-view | Change summary (created/claimed/submitted/closed/rejected) |
| `#/central/{central}/board/{id}/flow` | board-view | 4-column flow board (Open → Claimed → Submitted → Closed) |
| `#/central/{central}/board/{id}/routes` | board-view | Ticket provenance routes + per-seat load |
| `#/central/{central}/config` | config | Coordinator configuration form |
| `#/central/{central}/overhead` | overhead | Session context pressure + bridge diagnostics |
| `#/central/{central}/workers` | workers | API worker management (add, test, start, stop) |
| `#/central/{central}/seats` | seats | Seat configuration, dispatch policy, doctor, registry |
| `#/board/{id}` | board-detail | Board detail (default central) |
| `#/config` | config | Config (default central) |

### API Routes (GET)

| Path | Description |
|------|-------------|
| `/` | HTML page (SPA) |
| `/api/centrals` | Central label list |
| `/api/fleet?central={label}` | Fleet data per central |
| `/api/board/{board_id}?central={label}` | Board detail data |
| `/api/config/seats` | Seat inventory |
| `/api/config/bridge` | Wait bridge status |
| `/api/config/release` | Release status |
| `/api/config/registry` | Registry data |
| `/api/config/jobs/{hash}` | Job status |
| `/api/attention` | Attention state |
| `/api/overhead?central={label}` | Session context pressure |
| `/api/dispatch?central={label}` | Dispatch policy data |

### API Routes (POST, loopback-only for config)

| Path | Description |
|------|-------------|
| `/api/config` | Save coordinator config |
| `/api/intake` | Intake approve/decline |
| `/api/workers` | Add/update worker |
| `/api/workers/{name}/{test\|start\|stop\|restart}` | Worker actions |
| `/api/config/plan` | Config plan preview |
| `/api/config/suggestions` | Config suggestions |
| `/api/config/apply` | Apply config changes |
| `/api/config/prompt` | Generate seat prompt |
| `/api/config/doctor` | Run doctor checks |
| `/api/config/import` | Import discovered seats |
| `/api/config/bridge/install` | Install/upgrade bridge |
| `/api/config/bridge/upgrade-all` | Upgrade all seats |
| `/api/config/ops/plan` | Ops action plan |
| `/api/config/ops` | Execute ops action |
| `/api/config/registry/clone` | Clone registry project |
| `/api/dispatch` | Save dispatch policy |
| `/api/agents/retire` | Retire specific agent |
| `/api/agents/retire-inert` | Retire inert agents |
| `/api/attention` | Attention action (ack/snooze) |
| `/api/human/resolve` | Resolve human request |
| `/api/doors/copy` | Copy doors |
| `/api/doors/rotate` | Rotate doors |
| `/api/projects/add` | Add project |

## Surface 3: Extension Join/Settings

Source: `tools/aionui-extension/aion-extension.json` + `webui/routes.js`

| Route | Method | Description |
|-------|--------|-------------|
| `/pursers/join` | POST | Store a door and register the wait bridge as MCP server |
| `/pursers/status` | GET | Read redacted wait-bridge status |
| Settings tab: "Pursers" | — | Entry point: `webui/index.html`, positioned after "tools" |

## Surface 4: Personal MCP Server Tools

Source: `packages/personal/src/pursers_personal/apps_server.py`

| MCP Tool | Visibility | Description |
|----------|-----------|-------------|
| `board_snapshot` | MODEL_AND_APP | Current board projection |
| `fleet_snapshot` | MODEL_AND_APP | Fleet projection across boards |
| `link_snapshot` | APP_ONLY | Ticket/memory/file/tag links |
| `board_event_feed` | APP_ONLY | Bounded observed events |
| `board_onboard` | MODEL_ONLY | Join board + working context |
| `board_status` | MODEL_ONLY | Board health + workload counts |
| `board_catchup` | MODEL_ONLY | Bounded Central journal page |
| `ticket_get` | MODEL_ONLY | Read one ticket by ID |
| `ticket_list` | MODEL_ONLY | List visible tickets |
| `ticket_create` | MODEL_ONLY | Create a ticket |
| `ticket_claim` | MODEL_ONLY | Claim a ticket |
| `ticket_submit` | MODEL_ONLY | Submit completed work |
| `ticket_review` | MODEL_ONLY | Record review verdict |
| `lease_renew` | MODEL_ONLY | Renew ticket lease |
| `ticket_cancel` | MODEL_ONLY | Cancel a ticket |
| `memory_write` | MODEL_ONLY | Write project memory |
| `memory_read` | MODEL_ONLY | Read project memory |
| `memory_search` | MODEL_ONLY | Search project memory |
| `memory_links` | MODEL_ONLY | Traverse memory relationships |
| `memory_checkpoint` | MODEL_ONLY | Record continuation checkpoint |
| `memory_handoff` | MODEL_ONLY | Record handoff with next steps |
| `memory_unpin` | MODEL_ONLY | Unpin a memory entry |
| `board_state_get` | MODEL_ONLY | Read board-state values |
| `board_state_update` | MODEL_ONLY | Update board-state value |

## Duplicate Frontend Analysis

| Source | Built Output | Relationship |
|--------|-------------|--------------|
| `tools/dashboard-ui/dashboard-entry.html` + `src/dashboard.ts` + `src/dashboard.css` | `packages/personal/src/pursers_personal/resources/dashboard.html` | Vite single-file build of dashboard-ui source. The built HTML is the **actual live entrypoint** served by the Personal MCP server via `load_dashboard_html()` + `apps.add_html_resource()`. The source files are the development version. |
| `tools/fleet-dashboard/fleet_dashboard.py` (inline HTML/CSS/JS) | same file | Self-contained; the HTML is generated as a Python string constant and served by `BaseHTTPRequestHandler.do_GET("/")`. No separate build step. |
| `tools/aionui-extension/webui/*` | same files | Served directly by the AionUI extension host. No build step. |

**Actual live entrypoints:**
1. Personal dashboard: `packages/personal/src/pursers_personal/resources/dashboard.html` (served as MCP App resource `UI_URI`)
2. Fleet dashboard: `tools/fleet-dashboard/fleet_dashboard.py` line ~5828 (served at HTTP `/`)
3. Extension: `tools/aionui-extension/webui/index.html` (served by AionUI at settings tab)
