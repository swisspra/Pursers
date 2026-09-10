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

<!-- acceptance-facts:start -->
## Authoritative acceptance fact catalog

Each row is normative until a verifier records measured evidence. The machine predicate is exact and cannot be replaced by a generic title assertion.

| ID | Surface | Required observable fact | Predicate | Evidence source |
| --- | --- | --- | --- | --- |
| `dashboard-ui.logic` | personal | The accessibility tree exposes 'Today' for dashboard-ui.logic. | `ax_name_contains(Today)` | `tools/dashboard-ui/src/dashboard.ts` |
| `dashboard-ui.shell` | personal | The accessibility tree exposes 'On Board Personal' for dashboard-ui.shell. | `ax_name_contains(On Board Personal)` | `tools/dashboard-ui/src/dashboard.ts` |
| `dashboard-ui.state.empty-tickets` | personal | The accessibility tree exposes 'No current work' for dashboard-ui.state.empty-tickets. | `ax_name_contains(No current work)` | `tools/dashboard-ui/src/dashboard.ts` |
| `dashboard-ui.state.error` | personal | The accessibility tree exposes 'Disconnected' for dashboard-ui.state.error. | `ax_name_contains(Disconnected)` | `tools/dashboard-ui/src/dashboard.ts` |
| `dashboard-ui.state.loading` | personal | The accessibility tree exposes 'Loading' for dashboard-ui.state.loading. | `ax_name_contains(Loading)` | `tools/dashboard-ui/src/dashboard.ts` |
| `dashboard-ui.state.permission-denied` | personal | The accessibility tree exposes 'Permission denied' for dashboard-ui.state.permission-denied. | `ax_name_contains(Permission denied)` | `tools/dashboard-ui/src/dashboard.ts` |
| `dashboard-ui.state.search-empty` | personal | The accessibility tree exposes 'No results' for dashboard-ui.state.search-empty. | `ax_name_contains(No results)` | `tools/dashboard-ui/src/dashboard.ts` |
| `dashboard-ui.state.stale` | personal | The accessibility tree exposes 'Stale' for dashboard-ui.state.stale. | `ax_name_contains(Stale)` | `tools/dashboard-ui/src/dashboard.ts` |
| `dashboard-ui.styles` | personal | The accessibility tree exposes 'On Board Personal' for dashboard-ui.styles. | `ax_name_contains(On Board Personal)` | `tools/dashboard-ui/src/dashboard.ts` |
| `extension-join.state.bridge-missing` | aionui | The accessibility tree exposes 'Install the Pursers bridge' for extension-join.state.bridge-missing. | `ax_name_contains(Install the Pursers bridge)` | `tools/aionui-extension/webui/app.js` |
| `extension-join.state.error` | aionui | The accessibility tree exposes 'Join failed' for extension-join.state.error. | `ax_name_contains(Join failed)` | `tools/aionui-extension/webui/app.js` |
| `extension-join.state.initial` | aionui | The accessibility tree exposes 'Join Pursers' for extension-join.state.initial. | `ax_name_contains(Join Pursers)` | `tools/aionui-extension/webui/app.js` |
| `extension-join.state.joined` | aionui | The accessibility tree exposes 'Joined and registered' for extension-join.state.joined. | `ax_name_contains(Joined and registered)` | `tools/aionui-extension/webui/app.js` |
| `extension-join.state.joining` | aionui | The accessibility tree exposes 'Joining' for extension-join.state.joining. | `ax_name_contains(Joining)` | `tools/aionui-extension/webui/app.js` |
| `extension-join.state.status-empty` | aionui | The accessibility tree exposes 'Join Pursers' for extension-join.state.status-empty. | `ax_name_contains(Join Pursers)` | `tools/aionui-extension/webui/app.js` |
| `extension-join.state.status-loaded` | aionui | The accessibility tree exposes 'Board' for extension-join.state.status-loaded. | `ax_name_contains(Board)` | `tools/aionui-extension/webui/app.js` |
| `extension-join.surface` | aionui | The accessibility tree exposes 'Join Pursers' for extension-join.surface. | `ax_name_contains(Join Pursers)` | `tools/aionui-extension/webui/app.js` |
| `extension.bounded-errors` | aionui | The accessibility tree exposes 'Bounded Errors' for extension.bounded-errors. | `ax_name_contains(Bounded Errors)` | `tools/aionui-extension/webui/app.js` |
| `extension.environment-free-mcp-registration` | aionui | The accessibility tree exposes 'Environment Free MCP Registration' for extension.environment-free-mcp-registration. | `ax_name_contains(Environment Free MCP Registration)` | `tools/aionui-extension/webui/app.js` |
| `extension.idempotent-reconnect` | aionui | The accessibility tree exposes 'Idempotent Reconnect' for extension.idempotent-reconnect. | `ax_name_contains(Idempotent Reconnect)` | `tools/aionui-extension/webui/app.js` |
| `extension.join-form` | aionui | The accessibility tree exposes 'Join Form' for extension.join-form. | `ax_name_contains(Join Form)` | `tools/aionui-extension/webui/app.js` |
| `extension.join-progress` | aionui | The accessibility tree exposes 'Join Progress' for extension.join-progress. | `ax_name_contains(Join Progress)` | `tools/aionui-extension/webui/app.js` |
| `extension.redacted-status-card` | aionui | The accessibility tree exposes 'Redacted Status Card' for extension.redacted-status-card. | `ax_name_contains(Redacted Status Card)` | `tools/aionui-extension/webui/app.js` |
| `extension.reviewer-preset-claude` | aionui | The accessibility tree exposes 'Reviewer Preset Claude' for extension.reviewer-preset-claude. | `ax_name_contains(Reviewer Preset Claude)` | `tools/aionui-extension/webui/app.js` |
| `extension.reviewer-preset-codex` | aionui | The accessibility tree exposes 'Reviewer Preset Codex' for extension.reviewer-preset-codex. | `ax_name_contains(Reviewer Preset Codex)` | `tools/aionui-extension/webui/app.js` |
| `extension.settings-navigation` | aionui | The accessibility tree exposes 'Settings Navigation' for extension.settings-navigation. | `ax_name_contains(Settings Navigation)` | `tools/aionui-extension/webui/app.js` |
| `extension.worker-preset-claude` | aionui | The accessibility tree exposes 'Worker Preset Claude' for extension.worker-preset-claude. | `ax_name_contains(Worker Preset Claude)` | `tools/aionui-extension/webui/app.js` |
| `extension.worker-preset-codex` | aionui | The accessibility tree exposes 'Worker Preset Codex' for extension.worker-preset-codex. | `ax_name_contains(Worker Preset Codex)` | `tools/aionui-extension/webui/app.js` |
| `fleet-dashboard.state.bounded` | fleet | The accessibility tree exposes 'bounded view' for fleet-dashboard.state.bounded. | `ax_name_contains(bounded view)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet-dashboard.state.edit-paused` | fleet | The accessibility tree exposes 'Refresh paused' for fleet-dashboard.state.edit-paused. | `ax_name_contains(Refresh paused)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet-dashboard.state.empty-agents` | fleet | The accessibility tree exposes 'No active agents available' for fleet-dashboard.state.empty-agents. | `ax_name_contains(No active agents available)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet-dashboard.state.empty-centrals` | fleet | The accessibility tree exposes 'No centrals' for fleet-dashboard.state.empty-centrals. | `ax_name_contains(No centrals)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet-dashboard.state.empty-workers` | fleet | The accessibility tree exposes 'No API workers configured' for fleet-dashboard.state.empty-workers. | `ax_name_contains(No API workers configured)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet-dashboard.state.error-board` | fleet | The accessibility tree exposes 'Board detail unavailable' for fleet-dashboard.state.error-board. | `ax_name_contains(Board detail unavailable)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet-dashboard.state.loading-board` | fleet | The accessibility tree exposes 'Loading board detail' for fleet-dashboard.state.loading-board. | `ax_name_contains(Loading board detail)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet-dashboard.state.offline` | fleet | The accessibility tree exposes 'Offline' for fleet-dashboard.state.offline. | `ax_name_contains(Offline)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet-dashboard.state.routes-unavailable` | fleet | The accessibility tree exposes 'Routes source unavailable' for fleet-dashboard.state.routes-unavailable. | `ax_name_contains(Routes source unavailable)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet-dashboard.state.truncated-tickets` | fleet | The accessibility tree exposes 'tickets shown' for fleet-dashboard.state.truncated-tickets. | `ax_name_contains(tickets shown)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet-dashboard.surface` | fleet | The accessibility tree exposes 'Pursers Fleet' for fleet-dashboard.surface. | `ax_name_contains(Pursers Fleet)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.active-ticket-rows` | fleet | The accessibility tree exposes 'Active Ticket Rows' for fleet.active-ticket-rows. | `ax_name_contains(Active Ticket Rows)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.add-project-authorization-error` | fleet | The accessibility tree exposes 'Add Project Authorization Error' for fleet.add-project-authorization-error. | `ax_name_contains(Add Project Authorization Error)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.add-project-board` | fleet | The accessibility tree exposes 'Add Project Board' for fleet.add-project-board. | `ax_name_contains(Add Project Board)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.add-project-clone-steps` | fleet | The accessibility tree exposes 'Add Project Clone Steps' for fleet.add-project-clone-steps. | `ax_name_contains(Add Project Clone Steps)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.add-project-idempotent-rerun` | fleet | The accessibility tree exposes 'Add Project Idempotent Rerun' for fleet.add-project-idempotent-rerun. | `ax_name_contains(Add Project Idempotent Rerun)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.add-project-one-time-doors` | fleet | The accessibility tree exposes 'Add Project One Time Doors' for fleet.add-project-one-time-doors. | `ax_name_contains(Add Project One Time Doors)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.add-project-partial-failure` | fleet | The accessibility tree exposes 'Add Project Partial Failure' for fleet.add-project-partial-failure. | `ax_name_contains(Add Project Partial Failure)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.add-project-policy` | fleet | The accessibility tree exposes 'Add Project Policy' for fleet.add-project-policy. | `ax_name_contains(Add Project Policy)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.add-project-principals` | fleet | The accessibility tree exposes 'Add Project Principals' for fleet.add-project-principals. | `ax_name_contains(Add Project Principals)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.add-project-registry` | fleet | The accessibility tree exposes 'Add Project Registry' for fleet.add-project-registry. | `ax_name_contains(Add Project Registry)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.agent-current-claims` | fleet | The accessibility tree exposes 'Agent Current Claims' for fleet.agent-current-claims. | `ax_name_contains(Agent Current Claims)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.agent-duplicate-names` | fleet | The accessibility tree exposes 'Agent Duplicate Names' for fleet.agent-duplicate-names. | `ax_name_contains(Agent Duplicate Names)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.agent-pool` | fleet | The accessibility tree exposes 'Agent Pool' for fleet.agent-pool. | `ax_name_contains(Agent Pool)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.agent-retired-stale-drawer` | fleet | The accessibility tree exposes 'Agent Retired Stale Drawer' for fleet.agent-retired-stale-drawer. | `ax_name_contains(Agent Retired Stale Drawer)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.board-cards` | fleet | The accessibility tree exposes 'Board Cards' for fleet.board-cards. | `ax_name_contains(Board Cards)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.board-detail-activity` | fleet | The accessibility tree exposes 'Board Detail Activity' for fleet.board-detail-activity. | `ax_name_contains(Board Detail Activity)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.board-detail-metadata` | fleet | The accessibility tree exposes 'Board Detail Metadata' for fleet.board-detail-metadata. | `ax_name_contains(Board Detail Metadata)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.board-detail-truncation` | fleet | The accessibility tree exposes 'Board Detail Truncation' for fleet.board-detail-truncation. | `ax_name_contains(Board Detail Truncation)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.central-availability-isolation` | fleet | The accessibility tree exposes 'Central Availability Isolation' for fleet.central-availability-isolation. | `ax_name_contains(Central Availability Isolation)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.config-add-update-preview` | fleet | The accessibility tree exposes 'Config Add Update Preview' for fleet.config-add-update-preview. | `ax_name_contains(Config Add Update Preview)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.config-bridge-versions` | fleet | The accessibility tree exposes 'Config Bridge Versions' for fleet.config-bridge-versions. | `ax_name_contains(Config Bridge Versions)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.config-current-offers` | fleet | The accessibility tree exposes 'Config Current Offers' for fleet.config-current-offers. | `ax_name_contains(Config Current Offers)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.config-discovery-import-conflicts` | fleet | The accessibility tree exposes 'Config Discovery Import Conflicts' for fleet.config-discovery-import-conflicts. | `ax_name_contains(Config Discovery Import Conflicts)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.config-dispatch-policy-gaps-history` | fleet | The accessibility tree exposes 'Config Dispatch Policy Gaps History' for fleet.config-dispatch-policy-gaps-history. | `ax_name_contains(Config Dispatch Policy Gaps History)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.config-doctor` | fleet | The accessibility tree exposes 'Config Doctor' for fleet.config-doctor. | `ax_name_contains(Config Doctor)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.config-exact-diff-confirmation` | fleet | The accessibility tree exposes 'Config Exact Diff Confirmation' for fleet.config-exact-diff-confirmation. | `ax_name_contains(Config Exact Diff Confirmation)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.config-registry-worktrees` | fleet | The accessibility tree exposes 'Config Registry Worktrees' for fleet.config-registry-worktrees. | `ax_name_contains(Config Registry Worktrees)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.config-seat-inventory` | fleet | The accessibility tree exposes 'Config Seat Inventory' for fleet.config-seat-inventory. | `ax_name_contains(Config Seat Inventory)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.config-tier-skill-role-capabilities` | fleet | The accessibility tree exposes 'Config Tier Skill Role Capabilities' for fleet.config-tier-skill-role-capabilities. | `ax_name_contains(Config Tier Skill Role Capabilities)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.coordinator-configuration` | fleet | The accessibility tree exposes 'Coordinator Configuration' for fleet.coordinator-configuration. | `ax_name_contains(Coordinator Configuration)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.default-central-aliases` | fleet | The accessibility tree exposes 'Default Central Aliases' for fleet.default-central-aliases. | `ax_name_contains(Default Central Aliases)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.density` | fleet | The accessibility tree exposes 'Density' for fleet.density. | `ax_name_contains(Density)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.doors-connected-seats` | fleet | The accessibility tree exposes 'Doors Connected Seats' for fleet.doors-connected-seats. | `ax_name_contains(Doors Connected Seats)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.doors-copy` | fleet | The accessibility tree exposes 'Doors Copy' for fleet.doors-copy. | `ax_name_contains(Doors Copy)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.doors-disabled` | fleet | The accessibility tree exposes 'Doors Disabled' for fleet.doors-disabled. | `ax_name_contains(Doors Disabled)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.doors-error` | fleet | The accessibility tree exposes 'Doors Error' for fleet.doors-error. | `ax_name_contains(Doors Error)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.doors-expiry` | fleet | The accessibility tree exposes 'Doors Expiry' for fleet.doors-expiry. | `ax_name_contains(Doors Expiry)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.doors-key-id` | fleet | The accessibility tree exposes 'Doors Key Id' for fleet.doors-key-id. | `ax_name_contains(Doors Key Id)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.doors-project-rows` | fleet | The accessibility tree exposes 'Doors Project Rows' for fleet.doors-project-rows. | `ax_name_contains(Doors Project Rows)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.doors-rotation-warning` | fleet | The accessibility tree exposes 'Doors Rotation Warning' for fleet.doors-rotation-warning. | `ax_name_contains(Doors Rotation Warning)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.doors-secret-free-output` | fleet | The accessibility tree exposes 'Doors Secret Free Output' for fleet.doors-secret-free-output. | `ax_name_contains(Doors Secret Free Output)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.doors-unconfigured` | fleet | The accessibility tree exposes 'Doors Unconfigured' for fleet.doors-unconfigured. | `ax_name_contains(Doors Unconfigured)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.findings` | fleet | The accessibility tree exposes 'Findings' for fleet.findings. | `ax_name_contains(Findings)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.hub-agents` | fleet | The accessibility tree exposes 'Hub Agents' for fleet.hub-agents. | `ax_name_contains(Hub Agents)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.hub-boards` | fleet | The accessibility tree exposes 'Hub Boards' for fleet.hub-boards. | `ax_name_contains(Hub Boards)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.hub-operations` | fleet | The accessibility tree exposes 'Hub Operations' for fleet.hub-operations. | `ax_name_contains(Hub Operations)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.hub-overview` | fleet | The accessibility tree exposes 'Hub Overview' for fleet.hub-overview. | `ax_name_contains(Hub Overview)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.intake` | fleet | The accessibility tree exposes 'Intake' for fleet.intake. | `ax_name_contains(Intake)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.keyboard-help` | fleet | The accessibility tree exposes 'Keyboard Help' for fleet.keyboard-help. | `ax_name_contains(Keyboard Help)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.operations-disabled-controls` | fleet | The accessibility tree exposes 'Operations Disabled Controls' for fleet.operations-disabled-controls. | `ax_name_contains(Operations Disabled Controls)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.operations-job-progress` | fleet | The accessibility tree exposes 'Operations Job Progress' for fleet.operations-job-progress. | `ax_name_contains(Operations Job Progress)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.operations-job-result` | fleet | The accessibility tree exposes 'Operations Job Result' for fleet.operations-job-result. | `ax_name_contains(Operations Job Result)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.operations-rollback-failure` | fleet | The accessibility tree exposes 'Operations Rollback Failure' for fleet.operations-rollback-failure. | `ax_name_contains(Operations Rollback Failure)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.operations-unavailable-services` | fleet | The accessibility tree exposes 'Operations Unavailable Services' for fleet.operations-unavailable-services. | `ax_name_contains(Operations Unavailable Services)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.pool-available` | fleet | The accessibility tree exposes 'Pool Available' for fleet.pool-available. | `ax_name_contains(Pool Available)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.pool-busy` | fleet | The accessibility tree exposes 'Pool Busy' for fleet.pool-busy. | `ax_name_contains(Pool Busy)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.pool-online` | fleet | The accessibility tree exposes 'Pool Online' for fleet.pool-online. | `ax_name_contains(Pool Online)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.pool-stale` | fleet | The accessibility tree exposes 'Pool Stale' for fleet.pool-stale. | `ax_name_contains(Pool Stale)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.protocol-overhead` | fleet | The accessibility tree exposes 'Protocol Overhead' for fleet.protocol-overhead. | `ax_name_contains(Protocol Overhead)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.refresh-pause-resume` | fleet | The accessibility tree exposes 'Refresh Pause Resume' for fleet.refresh-pause-resume. | `ax_name_contains(Refresh Pause Resume)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.release-central` | fleet | The accessibility tree exposes 'Release Central' for fleet.release-central. | `ax_name_contains(Release Central)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.release-ci` | fleet | The accessibility tree exposes 'Release CI' for fleet.release-ci. | `ax_name_contains(Release CI)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.release-github` | fleet | The accessibility tree exposes 'Release Github' for fleet.release-github. | `ax_name_contains(Release Github)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.release-immutable-confirmation-plan` | fleet | The accessibility tree exposes 'Release Immutable Confirmation Plan' for fleet.release-immutable-confirmation-plan. | `ax_name_contains(Release Immutable Confirmation Plan)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.release-manifest` | fleet | The accessibility tree exposes 'Release Manifest' for fleet.release-manifest. | `ax_name_contains(Release Manifest)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.release-pypi` | fleet | The accessibility tree exposes 'Release PyPI' for fleet.release-pypi. | `ax_name_contains(Release PyPI)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.release-restart-checklist` | fleet | The accessibility tree exposes 'Release Restart Checklist' for fleet.release-restart-checklist. | `ax_name_contains(Release Restart Checklist)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.release-tag` | fleet | The accessibility tree exposes 'Release Tag' for fleet.release-tag. | `ax_name_contains(Release Tag)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.search-no-results` | fleet | The accessibility tree exposes 'Search No Results' for fleet.search-no-results. | `ax_name_contains(Search No Results)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.search-results` | fleet | The accessibility tree exposes 'Search Results' for fleet.search-results. | `ax_name_contains(Search Results)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.tab-changes` | fleet | The accessibility tree exposes 'Tab Changes' for fleet.tab-changes. | `ax_name_contains(Tab Changes)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.tab-flow` | fleet | The accessibility tree exposes 'Tab Flow' for fleet.tab-flow. | `ax_name_contains(Tab Flow)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.tab-routes` | fleet | The accessibility tree exposes 'Tab Routes' for fleet.tab-routes. | `ax_name_contains(Tab Routes)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.tab-tickets` | fleet | The accessibility tree exposes 'Tab Tickets' for fleet.tab-tickets. | `ax_name_contains(Tab Tickets)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.tab-timeline` | fleet | The accessibility tree exposes 'Tab Timeline' for fleet.tab-timeline. | `ax_name_contains(Tab Timeline)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.theme` | fleet | The accessibility tree exposes 'Theme' for fleet.theme. | `ax_name_contains(Theme)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.ticket-counts` | fleet | The accessibility tree exposes 'Ticket Counts' for fleet.ticket-counts. | `ax_name_contains(Ticket Counts)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.unknown-route-recovery` | fleet | The accessibility tree exposes 'Unknown Route Recovery' for fleet.unknown-route-recovery. | `ax_name_contains(Unknown Route Recovery)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.updated-state` | fleet | The accessibility tree exposes 'Updated State' for fleet.updated-state. | `ax_name_contains(Updated State)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `fleet.worker-management` | fleet | The accessibility tree exposes 'Worker Management' for fleet.worker-management. | `ax_name_contains(Worker Management)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `personal-mcp.state.board-empty` | personal | The accessibility tree exposes 'No current work' for personal-mcp.state.board-empty. | `ax_name_contains(No current work)` | `packages/personal/src/pursers_personal/apps_server.py` |
| `personal-mcp.state.fleet-unavailable` | personal | The accessibility tree exposes 'Fleet unavailable' for personal-mcp.state.fleet-unavailable. | `ax_name_contains(Fleet unavailable)` | `packages/personal/src/pursers_personal/apps_server.py` |
| `personal-mcp.state.links-empty` | personal | The accessibility tree exposes 'No links' for personal-mcp.state.links-empty. | `ax_name_contains(No links)` | `packages/personal/src/pursers_personal/apps_server.py` |
| `personal-mcp.state.memory-empty` | personal | The accessibility tree exposes 'No pinned memory' for personal-mcp.state.memory-empty. | `ax_name_contains(No pinned memory)` | `packages/personal/src/pursers_personal/apps_server.py` |
| `personal-mcp.state.not-onboarded` | personal | The accessibility tree exposes 'Not onboarded' for personal-mcp.state.not-onboarded. | `ax_name_contains(Not onboarded)` | `packages/personal/src/pursers_personal/apps_server.py` |
| `personal-mcp.state.ticket-not-found` | personal | The accessibility tree exposes 'Ticket not found' for personal-mcp.state.ticket-not-found. | `ax_name_contains(Ticket not found)` | `packages/personal/src/pursers_personal/apps_server.py` |
| `personal-mcp.surface` | personal | The accessibility tree exposes 'On Board Personal' for personal-mcp.surface. | `ax_name_contains(On Board Personal)` | `packages/personal/src/pursers_personal/apps_server.py` |
| `personal.activity-bounded-feed` | personal | The accessibility tree exposes 'Activity Bounded Feed' for personal.activity-bounded-feed. | `ax_name_contains(Activity Bounded Feed)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.activity-cursor` | personal | The accessibility tree exposes 'Activity Cursor' for personal.activity-cursor. | `ax_name_contains(Activity Cursor)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.activity-dropped-events` | personal | The accessibility tree exposes 'Activity Dropped Events' for personal.activity-dropped-events. | `ax_name_contains(Activity Dropped Events)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.activity-empty` | personal | The accessibility tree exposes 'Activity Empty' for personal.activity-empty. | `ax_name_contains(Activity Empty)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.activity-error` | personal | The accessibility tree exposes 'Activity Error' for personal.activity-error. | `ax_name_contains(Activity Error)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.activity-has-more-resync` | personal | The accessibility tree exposes 'Activity Has More Resync' for personal.activity-has-more-resync. | `ax_name_contains(Activity Has More Resync)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.activity-offline` | personal | The accessibility tree exposes 'Activity Offline' for personal.activity-offline. | `ax_name_contains(Activity Offline)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.activity-scope` | personal | The accessibility tree exposes 'Activity Scope' for personal.activity-scope. | `ax_name_contains(Activity Scope)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.activity-stale` | personal | The accessibility tree exposes 'Activity Stale' for personal.activity-stale. | `ax_name_contains(Activity Stale)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.agents-current-ticket` | personal | The accessibility tree exposes 'Agents Current Ticket' for personal.agents-current-ticket. | `ax_name_contains(Agents Current Ticket)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.agents-duplicate-identity` | personal | The accessibility tree exposes 'Agents Duplicate Identity' for personal.agents-duplicate-identity. | `ax_name_contains(Agents Duplicate Identity)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.agents-duplicate-name` | personal | The accessibility tree exposes 'Agents Duplicate Name' for personal.agents-duplicate-name. | `ax_name_contains(Agents Duplicate Name)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.agents-empty` | personal | The accessibility tree exposes 'Agents Empty' for personal.agents-empty. | `ax_name_contains(Agents Empty)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.agents-focus` | personal | The accessibility tree exposes 'Agents Focus' for personal.agents-focus. | `ax_name_contains(Agents Focus)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.agents-idle-lease` | personal | The accessibility tree exposes 'Agents Idle Lease' for personal.agents-idle-lease. | `ax_name_contains(Agents Idle Lease)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.agents-platform` | personal | The accessibility tree exposes 'Agents Platform' for personal.agents-platform. | `ax_name_contains(Agents Platform)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.agents-role` | personal | The accessibility tree exposes 'Agents Role' for personal.agents-role. | `ax_name_contains(Agents Role)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.agents-stale` | personal | The accessibility tree exposes 'Agents Stale' for personal.agents-stale. | `ax_name_contains(Agents Stale)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.agents-total-live` | personal | The accessibility tree exposes 'Agents Total Live' for personal.agents-total-live. | `ax_name_contains(Agents Total Live)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.board-identity` | personal | The accessibility tree exposes 'Board Identity' for personal.board-identity. | `ax_name_contains(Board Identity)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.connection-banner` | personal | The accessibility tree exposes 'Connection Banner' for personal.connection-banner. | `ax_name_contains(Connection Banner)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.data-provenance` | personal | The accessibility tree exposes 'Data Provenance' for personal.data-provenance. | `ax_name_contains(Data Provenance)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.fleet-empty` | personal | The accessibility tree exposes 'Fleet Empty' for personal.fleet-empty. | `ax_name_contains(Fleet Empty)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.fleet-online-busy-available-stale` | personal | The accessibility tree exposes 'Fleet Online Busy Available Stale' for personal.fleet-online-busy-available-stale. | `ax_name_contains(Fleet Online Busy Available Stale)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.fleet-project-seats` | personal | The accessibility tree exposes 'Fleet Project Seats' for personal.fleet-project-seats. | `ax_name_contains(Fleet Project Seats)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.fleet-project-ticket-counts` | personal | The accessibility tree exposes 'Fleet Project Ticket Counts' for personal.fleet-project-ticket-counts. | `ax_name_contains(Fleet Project Ticket Counts)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.fleet-projects` | personal | The accessibility tree exposes 'Fleet Projects' for personal.fleet-projects. | `ax_name_contains(Fleet Projects)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.fleet-registry-warning` | personal | The accessibility tree exposes 'Fleet Registry Warning' for personal.fleet-registry-warning. | `ax_name_contains(Fleet Registry Warning)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.fleet-shared-pool` | personal | The accessibility tree exposes 'Fleet Shared Pool' for personal.fleet-shared-pool. | `ax_name_contains(Fleet Shared Pool)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.fleet-truncation` | personal | The accessibility tree exposes 'Fleet Truncation' for personal.fleet-truncation. | `ax_name_contains(Fleet Truncation)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.fleet-unavailable` | personal | The accessibility tree exposes 'Fleet Unavailable' for personal.fleet-unavailable. | `ax_name_contains(Fleet Unavailable)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.health` | personal | The accessibility tree exposes 'Health' for personal.health. | `ax_name_contains(Health)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.keyboard-tabs` | personal | The accessibility tree exposes 'Keyboard Tabs' for personal.keyboard-tabs. | `ax_name_contains(Keyboard Tabs)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.links-edge-types` | personal | The accessibility tree exposes 'Links Edge Types' for personal.links-edge-types. | `ax_name_contains(Links Edge Types)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.links-empty` | personal | The accessibility tree exposes 'Links Empty' for personal.links-empty. | `ax_name_contains(Links Empty)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.links-node-edge-totals` | personal | The accessibility tree exposes 'Links Node Edge Totals' for personal.links-node-edge-totals. | `ax_name_contains(Links Node Edge Totals)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.links-pinned` | personal | The accessibility tree exposes 'Links Pinned' for personal.links-pinned. | `ax_name_contains(Links Pinned)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.links-source-label` | personal | The accessibility tree exposes 'Links Source Label' for personal.links-source-label. | `ax_name_contains(Links Source Label)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.links-truncation` | personal | The accessibility tree exposes 'Links Truncation' for personal.links-truncation. | `ax_name_contains(Links Truncation)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.links-unavailable` | personal | The accessibility tree exposes 'Links Unavailable' for personal.links-unavailable. | `ax_name_contains(Links Unavailable)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.refresh` | personal | The accessibility tree exposes 'Refresh' for personal.refresh. | `ax_name_contains(Refresh)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.search-no-results` | personal | The accessibility tree exposes 'Search No Results' for personal.search-no-results. | `ax_name_contains(Search No Results)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.search-results` | personal | The accessibility tree exposes 'Search Results' for personal.search-results. | `ax_name_contains(Search Results)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.source-board-event-feed` | personal | The accessibility tree exposes 'Source Board Event Feed' for personal.source-board-event-feed. | `ax_name_contains(Source Board Event Feed)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.source-board-snapshot` | personal | The accessibility tree exposes 'Source Board Snapshot' for personal.source-board-snapshot. | `ax_name_contains(Source Board Snapshot)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.source-fleet-snapshot` | personal | The accessibility tree exposes 'Source Fleet Snapshot' for personal.source-fleet-snapshot. | `ax_name_contains(Source Fleet Snapshot)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.source-link-snapshot` | personal | The accessibility tree exposes 'Source Link Snapshot' for personal.source-link-snapshot. | `ax_name_contains(Source Link Snapshot)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.theme` | personal | The accessibility tree exposes 'Theme' for personal.theme. | `ax_name_contains(Theme)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.today-active-agents` | personal | The accessibility tree exposes 'Today Active Agents' for personal.today-active-agents. | `ax_name_contains(Today Active Agents)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.today-current-work` | personal | The accessibility tree exposes 'Today Current Work' for personal.today-current-work. | `ax_name_contains(Today Current Work)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.today-important-pinned-note` | personal | The accessibility tree exposes 'Today Important Pinned Note' for personal.today-important-pinned-note. | `ax_name_contains(Today Important Pinned Note)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.today-latest-handoff` | personal | The accessibility tree exposes 'Today Latest Handoff' for personal.today-latest-handoff. | `ax_name_contains(Today Latest Handoff)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.today-recent-activity` | personal | The accessibility tree exposes 'Today Recent Activity' for personal.today-recent-activity. | `ax_name_contains(Today Recent Activity)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.today-status-metrics` | personal | The accessibility tree exposes 'Today Status Metrics' for personal.today-status-metrics. | `ax_name_contains(Today Status Metrics)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.work-abandonment` | personal | The accessibility tree exposes 'Work Abandonment' for personal.work-abandonment. | `ax_name_contains(Work Abandonment)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.work-lease` | personal | The accessibility tree exposes 'Work Lease' for personal.work-lease. | `ax_name_contains(Work Lease)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.work-no-ticket` | personal | The accessibility tree exposes 'Work No Ticket' for personal.work-no-ticket. | `ax_name_contains(Work No Ticket)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.work-ownership` | personal | The accessibility tree exposes 'Work Ownership' for personal.work-ownership. | `ax_name_contains(Work Ownership)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.work-priority` | personal | The accessibility tree exposes 'Work Priority' for personal.work-priority. | `ax_name_contains(Work Priority)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.work-rejection` | personal | The accessibility tree exposes 'Work Rejection' for personal.work-rejection. | `ax_name_contains(Work Rejection)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.work-review-readiness` | personal | The accessibility tree exposes 'Work Review Readiness' for personal.work-review-readiness. | `ax_name_contains(Work Review Readiness)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.work-status-groups` | personal | The accessibility tree exposes 'Work Status Groups' for personal.work-status-groups. | `ax_name_contains(Work Status Groups)` | `tools/dashboard-ui/src/dashboard.ts` |
| `personal.work-total` | personal | The accessibility tree exposes 'Work Total' for personal.work-total. | `ax_name_contains(Work Total)` | `tools/dashboard-ui/src/dashboard.ts` |

### Additional final gates

These are mandatory in addition to the nine sequence steps and 189 inventory observations.

| ID | Surface | Required observable fact | Predicate | Evidence source |
| --- | --- | --- | --- | --- |
| `final.quickstart-candidate-flow` | aionui | The final Quickstart flow visibly reaches the exact installed candidate Home. | `ax_name_contains(Pursers)` | `docs/design-home/quickstart.md` |
| `final.fleet-503-recovery` | fleet | The live Fleet surface visibly reports a 503 dependency failure and a subsequent recovered state. | `ax_name_contains(Recovered)` | `tools/fleet-dashboard/fleet_dashboard.py` |
| `final.o1-readiness-rollback` | fleet | The O1 readiness check and rollback outcome are both visible on the real operations surface. | `ax_name_contains(Rollback)` | `docs/design-home/acceptance.md` |
<!-- acceptance-facts:end -->

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
