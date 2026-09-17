# Fleet DOM selector contract

This contract gives browser observers a stable way to locate Fleet state without depending on CSS classes, element order, colour, or icons. Attribute names are public compatibility points. Values are escaped product values, not verifier-authored expectations.

The contract uses only the `data-pursers-*` namespace:

- `data-pursers-surface="fleet|personal"` identifies the rendered product surface.
- `data-pursers-panel="connection|hub|boards|tickets|ticket-detail|agents|seats|board-detail|config|butler-settings|search"` identifies a state-owning panel.
- `data-pursers-state="loading|ready|empty|error"` exposes the panel's current render state.
- `data-pursers-board`, `data-pursers-ticket`, `data-pursers-agent`, and `data-pursers-seat` contain stable product identifiers on rows or cards.
- `data-pursers-selected-board` and `data-pursers-selected-ticket` contain the current selection, including the empty string when nothing is selected.
- `data-pursers-selected="true|false"` exposes selection state on addressable board and ticket rows.
- `data-pursers-status` contains the source-backed status for board, ticket, agent, or seat rows. Fleet board rows use `ready` after a successful bounded snapshot and `error` when that board read failed. Fleet seat cards normalize the board's live pool values to `working`, `available`, `stale`, or `offline`: `busy` becomes the user-facing `working`, and a configured local seat that is not running becomes `offline`.
- `data-pursers-connection="loading|connected|reconnecting|error|demo|stale"` and `data-pursers-health="connected|reconnecting"` expose connection and health state as text values.
- `data-pursers-field`, `data-pursers-action`, and `data-pursers-validation="not_run|reachable|rejected_credential|wrong_model|unreachable|invalid"` bind the write-only Butler form without exposing its key value.

## Catalogue bindings

Repeated catalogue entries use the same binding. The attribute locates the real product element; the catalogue predicate still evaluates the visible or source-backed fact on that element.

| Catalogue row | Row or element | Attribute binding | Meaning |
|---|---|---|---|
| `fleet-dashboard.surface`; `fleet.theme`; `fleet.density`; `fleet.keyboard-help`; `fleet.refresh-pause-resume`; `fleet.updated-state`; `fleet.unknown-route-recovery` | Fleet root | `[data-pursers-surface="fleet"]` and `data-pursers-state` | Stable surface root and its current render state. |
| `fleet-dashboard.state.empty-centrals`; `fleet-dashboard.state.loading-board`; `fleet-dashboard.state.error-board`; `fleet-dashboard.state.bounded`; `fleet-dashboard.state.truncated-tickets`; `fleet-dashboard.state.edit-paused`; `fleet-dashboard.state.empty-workers`; `fleet-dashboard.state.empty-agents`; `fleet-dashboard.state.routes-unavailable` | Owning hub, board-detail, agents, seats, or config panel | `[data-pursers-panel]` plus `data-pursers-state` | The observer reads `loading`, `ready`, `empty`, or `error` from the panel rather than inferring it from placeholder styling. |
| `fleet-dashboard.state.offline`; `fleet.central-availability-isolation`; `fleet.default-central-aliases`; `fleet.hub-overview` | Connection banner or Central health card | `[data-pursers-panel="connection"][data-pursers-connection]` or `[data-pursers-health]` | Connection and Central health are explicit values rather than coloured dots. |
| `fleet.board-cards`; `fleet.hub-boards`; `fleet.add-project-board`; `fleet.add-project-registry` | Board list and board card | `[data-pursers-panel="boards"] [data-pursers-board][data-pursers-status]` | The panel exposes its state and each card exposes its exact board identifier plus source-backed `ready` or `error` status. |
| `fleet.board-detail-activity`; `fleet.board-detail-metadata`; `fleet.board-detail-truncation`; `fleet.findings`; `fleet.intake`; `fleet.tab-tickets`; `fleet.tab-timeline`; `fleet.tab-changes`; `fleet.tab-flow`; `fleet.tab-routes` | Selected board marker and board-detail panel | `[data-pursers-selected-board]` and `[data-pursers-panel="board-detail"]` | The selected board is readable directly; the detail panel owns the loaded, empty, and error states. |
| `fleet.active-ticket-rows`; `fleet.ticket-counts` | Ticket panel and ticket row | `[data-pursers-panel="tickets"] [data-pursers-ticket][data-pursers-status]` | Every ticket row exposes its identifier and source-backed status. |
| `fleet.agent-current-claims`; `fleet.agent-duplicate-names`; `fleet.agent-pool`; `fleet.agent-retired-stale-drawer`; `fleet.hub-agents`; `fleet.pool-online`; `fleet.pool-busy`; `fleet.pool-available`; `fleet.pool-stale` | Agent count strip, filter bar, agent card, and board chip | `[data-pursers-panel="agents"][data-pursers-state]`, `[data-pursers-agent][data-pursers-seat][data-pursers-status]`, and `[data-pursers-board][data-pursers-status]` | The count strip and filtered grid expose their render state. Every dense card exposes the stable agent/seat identity and one of `working`, `available`, `stale`, or `offline`; each board chip keeps its exact board identifier. Role, status, board, and client/platform controls filter only these product-produced values. |
| `fleet.config-add-update-preview`; `fleet.config-bridge-versions`; `fleet.config-current-offers`; `fleet.config-discovery-import-conflicts`; `fleet.config-dispatch-policy-gaps-history`; `fleet.config-doctor`; `fleet.config-exact-diff-confirmation`; `fleet.config-registry-worktrees`; `fleet.config-seat-inventory`; `fleet.config-tier-skill-role-capabilities`; `fleet.coordinator-configuration`; `fleet.protocol-overhead`; `fleet.worker-management`; `fleet.hub-operations` | Config or seats panel and seat row | `[data-pursers-panel="config"]`, `[data-pursers-panel="seats"]`, and `[data-pursers-seat][data-pursers-status]` | Configuration state is panel-addressable and each configured seat has readable identity and status. |
| Butler provider configuration | Butler settings panel, fields, save action, and validation result | `[data-pursers-panel="butler-settings"][data-pursers-state]`, `[data-pursers-field]`, `[data-pursers-action="save-butler"]`, and `[data-pursers-validation]` | Endpoint, model, validation path, draft path, explicit draft protocol, optional non-secret headers, read-only mode, write-only key input, key-presence status, and the fixed validation outcome are directly addressable. The stored key value is never rendered. |
| `fleet.add-project-authorization-error`; `fleet.add-project-clone-steps`; `fleet.add-project-idempotent-rerun`; `fleet.add-project-one-time-doors`; `fleet.add-project-partial-failure`; `fleet.add-project-policy`; `fleet.add-project-principals` | Config panel | `[data-pursers-panel="config"][data-pursers-state]` | Add-project progress and failures remain attached to the stable config panel state. |
| `fleet.doors-connected-seats`; `fleet.doors-copy`; `fleet.doors-disabled`; `fleet.doors-error`; `fleet.doors-expiry`; `fleet.doors-key-id`; `fleet.doors-project-rows`; `fleet.doors-rotation-warning`; `fleet.doors-secret-free-output`; `fleet.doors-unconfigured` | Config panel, board row, and seat row | `[data-pursers-panel="config"]`, `[data-pursers-board]`, and `[data-pursers-seat]` | Door assertions bind to the exact project and connected-seat rows without exposing a credential. |
| `fleet.operations-disabled-controls`; `fleet.operations-job-progress`; `fleet.operations-job-result`; `fleet.operations-rollback-failure`; `fleet.operations-unavailable-services`; `fleet.release-central`; `fleet.release-ci`; `fleet.release-github`; `fleet.release-immutable-confirmation-plan`; `fleet.release-manifest`; `fleet.release-pypi`; `fleet.release-restart-checklist`; `fleet.release-tag` | Config or operations panel | `[data-pursers-panel="config"][data-pursers-state]` | Release and operation states have a stable panel anchor while their visible values remain authoritative. |
| `fleet.search-no-results`; `fleet.search-results` | Search result panel | `[data-pursers-panel="search"][data-pursers-state]` | Search results expose `ready` or `empty` independently of result styling. |

## Selection rules

The Fleet root marker always carries `data-pursers-selected-board` and `data-pursers-selected-ticket`; neither attribute is removed when the selection is empty. Addressable board and ticket rows carry `data-pursers-selected="true|false"`. A verifier must not infer selection from an active CSS class, focus, location in the list, or the first returned row.

On `#/team`, the four status-count buttons carry `data-pursers-status` and the dense grid owns `data-pursers-panel="agents"`. The status text and glyph are both visible, so working, available, stale, and offline do not depend on colour. A card carries both `data-pursers-agent` and `data-pursers-seat` because the card is the unified live-seat record; duplicate display names remain disambiguated by that stable identity.

## Replay entrypoint

Start the loopback dashboard from the candidate checkout with a token file outside the repository:

```bash
python tools/fleet-dashboard/fleet_dashboard.py \
  --url http://127.0.0.1:8766/mcp \
  --token-file /PATH/TO/token \
  --port 8899
```

Open `http://127.0.0.1:8899/#/projects`. After the page settles, a verifier can read the contract without product discovery:

```js
document.querySelector('main').getAttribute('data-pursers-state');
document.querySelector('#board-id').getAttribute('data-pursers-selected-board');
document.querySelector('#board-id').getAttribute('data-pursers-selected-ticket');
[...document.querySelectorAll('[data-pursers-board]')].map((node) => ({
  board: node.getAttribute('data-pursers-board'),
  status: node.getAttribute('data-pursers-status'),
  selected: node.getAttribute('data-pursers-selected'),
}));
[...document.querySelectorAll('[data-pursers-ticket]')].map((node) => ({
  ticket: node.getAttribute('data-pursers-ticket'),
  status: node.getAttribute('data-pursers-status'),
  selected: node.getAttribute('data-pursers-selected'),
}));
[...document.querySelectorAll('[data-pursers-agent], [data-pursers-seat]')].map((node) => ({
  agent: node.getAttribute('data-pursers-agent'),
  seat: node.getAttribute('data-pursers-seat'),
  status: node.getAttribute('data-pursers-status'),
}));
[...document.querySelectorAll('[data-pursers-panel]')].map((node) => ({
  panel: node.getAttribute('data-pursers-panel'),
  state: node.getAttribute('data-pursers-state'),
}));
```

The Personal dashboard uses the same names for its board, ticket, agent, seat, connection, selection, and panel states so shared observer code does not need a second selector vocabulary.
