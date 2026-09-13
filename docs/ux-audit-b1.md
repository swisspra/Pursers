# Beta.1 UX/UI audit

Audit target: Fleet Dashboard, Personal dashboard, and the AionUi extension on
`origin/main` at `3556d37438e93f52a746dfb8f745491ba0e2bcb0`.

## Method and limits

The three surfaces were started from this checkout with disposable state on
loopback ports 28741, 28742, and 28745. HTTP responses, source templates, CSS,
JavaScript, fixture-driven tests, and built artifacts were inspected. The
Fleet API returned its expected disconnected error response, the Personal
bundle was served directly, and an AionUi route harness exercised empty status
and invalid-door responses.

The seat could not connect to the Ego Lite bootstrap. Per coordinator decision,
no other browser was substituted. Visual checks at 400 px and 1440 px and
interactive keyboard checks are explicitly pending below; this report makes no
claim that those checks passed. Static contrast checks covered the committed
fallback color pairs, not host-injected themes.

No P0 issue was found. The audit found 3 P1, 10 P2, and 4 P3 issues. The most
important beta.1 work is the AionUi join state machine: the current screen skips
the documented confirmation, cannot distinguish partial success, and does not
recover visibly from a rejected network request.

## Findings

| Severity | Screen | Issue | Evidence | Proposed fix | Effort |
|---|---|---|---|---|---|
| P1 | AionUi / Join | The form posts directly to the legacy join route, so connecting and MCP registration happen before the documented redacted metadata confirmation. | `tools/aionui-extension/webui/app.js:27`; `tools/aionui-extension/README.md:21`; the typed validation route exists at `tools/aionui-extension/webui/routes.js:141`. | Make Join a two-step `validate` then confirm flow; call `connect` only after confirmation and show board, role, seat, tier, key ID, expiry, and transport. | M |
| P1 | AionUi / Join result | A partial MCP-registration failure can return `joined: true`, but the UI ignores that state and presents a generic failure. Retrying can look like a fresh join even though credentials were stored. | `tools/aionui-extension/webui/routes.js:106`; `tools/aionui-extension/webui/app.js:38`. | Render a distinct “Connected; registration incomplete” state with a Recover action and no door replay. | M |
| P1 | AionUi / Join loading | The submit handler has no `catch` or `finally`, never disables the button, and leaves “Joining…” in place if `fetch` rejects. | `tools/aionui-extension/webui/app.js:27`. | Disable the form, set `aria-busy`, catch transport failures, restore controls in `finally`, and keep a retryable error visible. | S |
| P2 | Fleet / disconnected home | The API returns a structured 503 body, but `fetchJson` discards it and the home UI can only show `HTTP 503`; the first-time user gets no cause or next action. | Runtime: `GET /api/fleet?central=default` returned `503 {"error":"ExceptionGroup","central":"default"}`; `tools/fleet-dashboard/fleet_dashboard.py:5994`. | Preserve a scrubbed server message/code and add a short action such as “Check Central URL and token, then Retry.” | S |
| P2 | Fleet / global search | The input controls a `listbox` and arrow keys change `aria-selected`, but the input is not a combobox and has no `aria-activedescendant`; focus stays on the input while the selected option changes. | `tools/fleet-dashboard/fleet_dashboard.py:5945`, `:5964`, `:6002`. | Implement the ARIA combobox/listbox pattern with stable option IDs, `aria-autocomplete`, and `aria-activedescendant`. | M |
| P2 | Fleet / tables | Every first table row becomes a roving tab stop, but rows have no activation behavior. This adds focus stops that do not perform the link action users may expect. | `tools/fleet-dashboard/fleet_dashboard.py:5966`. | Keep focus on real links and buttons, or give actionable rows an accessible name plus Enter/Space activation. | M |
| P2 | Fleet / loading and refresh state | The visible `#state` changes from Loading to Updated, Startup failed, or paused, but it is not a live region. | `tools/fleet-dashboard/fleet_dashboard.py:5945`, `:5974`, `:6002`. | Use a polite status region for meaningful state transitions; avoid announcing the five-second timestamps. | S |
| P2 | Fleet / keyboard help | The modal dialog has a heading but no programmatic accessible name. | `tools/fleet-dashboard/fleet_dashboard.py:5945`. | Add `aria-labelledby` pointing to a stable heading ID and verify focus return to the opener. | S |
| P2 | Personal / Work | `reviewing` and `in_review` are grouped and counted as “Submitted,” hiding the review stage described in the architecture and weakening the offer → claim → lease → review mental model. | `tools/dashboard-ui/src/dashboard.ts:595`, `:602`, `:796`; `docs/ARCHITECTURE.md:82`. | Add a distinct “In review” group/count while retaining the raw status pill. | M |
| P3 | Personal / search | Search results are rendered in a separate section, but the input only has `aria-describedby`; it does not expose `aria-controls` or expanded/collapsed state. | `tools/dashboard-ui/dashboard-entry.html:41`; `tools/dashboard-ui/src/dashboard.ts:1231`. | Add `aria-controls` and update `aria-expanded`; announce when results open or close. | S |
| P2 | AionUi / initial status | Bridge-not-installed and status transport failures are silently discarded on initial load, including the backend's install hint. The screen is indistinguishable from a valid empty state. | `tools/aionui-extension/webui/routes.js:94`; `tools/aionui-extension/webui/app.js:46`. | Show explicit empty, unavailable, and bridge-missing states; include the bounded install hint. | S |
| P2 | AionUi / status | The status response supports multiple seats, but the UI renders only `seats[0]`. | `tools/aionui-extension/webui/app.js:46`; response shape at `tools/aionui-extension/webui/routes.js:94`. | Render a card per seat or clearly label and select the active seat. | M |
| P2 | AionUi / success | Success ends with a status card but omits the documented next action: start a conversation and choose the matching preset. | `tools/aionui-extension/webui/index.html:19`; `tools/aionui-extension/webui/app.js:42`; `tools/aionui-extension/README.md:23`. | Add a concise next-step panel and, where the host permits, a direct navigation action. | S |
| P3 | Fleet / Agents | Empty work is rendered as mixed Thai/English text (`ว่าง/idle`) inside an otherwise English interface. | `tools/fleet-dashboard/fleet_dashboard.py:5978`, `:6223`. | Use one term, preferably “Idle — no active claim,” everywhere. | S |
| P3 | AionUi / responsive layout | The status definition list has fixed 7 rem and 12 rem minimum columns and the stylesheet has no responsive rule. It fits 400 px with little spare width and has no large-text fallback. | `tools/aionui-extension/webui/style.css:12`, `:53`. | Stack `dt`/`dd` below a narrow breakpoint and test with 200% text zoom. | S |
| P2 | Fleet / mobile navigation | Mobile navigation and common action controls use roughly 9 px vertical padding without a 44 px minimum target. | `tools/fleet-dashboard/fleet_dashboard.py:6189`, `:6190`. | Set a shared minimum target size and preserve spacing at 400 px. | S |
| P3 | Personal / Fleet table | The 620 px table intentionally scrolls at 400 px, but the scroll container has no accessible label or keyboard-focus affordance. | `tools/dashboard-ui/src/dashboard.css:376`. | Give the scroll region a label and focus style, or replace rows with stacked cards below 620 px. | S |

## What already works

- All three entry documents declare a language and viewport, and the inspected
  entry-form controls have labels. Each surface contains a polite status region.
- The Personal dashboard has a skip link, 44 px button minimums, strong focus
  treatment, tablist arrow/Home/End handling, reduced-motion rules, explicit
  demo/error copy, and concrete empty states (`tools/dashboard-ui/dashboard-entry.html:9`,
  `:33`, `:52`; `tools/dashboard-ui/src/dashboard.css:62`, `:67`, `:598`).
- Fleet has responsive rules at 800 px and narrower, horizontal table
  containment, escaped dynamic content, reconnect copy, and keyboard shortcuts
  covered by its test harness (`tools/fleet-dashboard/fleet_dashboard.py:5944`,
  `:5967`; `tools/fleet-dashboard/tests/test_fleet_dashboard.py:1604`, `:1635`,
  `:1737`, `:1775`).
- AionUi labels its secret input, clears the door before rendering results, uses
  a live status region, returns bounded errors, and does not echo the door
  (`tools/aionui-extension/webui/index.html:13`,
  `tools/aionui-extension/webui/app.js:27`,
  `tools/aionui-extension/tests/routes.test.cjs:84`).
- Static fallback contrast ratios for the tested text pairs were at least
  5.01:1. Host-supplied Personal theme variables still require the pending
  visual pass.

## Top 10 fixes for beta.1

1. Add the AionUi validate → confirm → connect flow.
2. Expose partial join success and a door-free Recover action.
3. Make AionUi join loading and transport errors deterministic and retryable.
4. Distinguish AionUi empty, bridge-missing, and unavailable startup states.
5. Implement a complete Fleet ARIA combobox pattern.
6. Preserve a scrubbed Fleet connection error and show a concrete recovery step.
7. Separate Personal “In review” from “Submitted.”
8. Render all AionUi seats and add the post-join conversation/preset step.
9. Remove non-actionable Fleet row tab stops; name the help dialog and live state.
10. Complete the 400 px/1440 px visual, zoom, and keyboard pass listed below.

## Visual and keyboard pass pending

| Surface and page | State | Width/input | Status |
|---|---|---|---|
| Fleet `#/` | Central disconnected; reconnect banner and empty attention | 400 px and 1440 px | not executed in this seat - visual pass pending |
| Fleet `#/boards` and one board workspace | Populated fixture, long titles, truncation, tables | 400 px and 1440 px | not executed in this seat - visual pass pending |
| Fleet `#/agents`, `#/operations`, and `#/seats` | Empty and populated cards; modal forms | 400 px and 1440 px | not executed in this seat - visual pass pending |
| Fleet global search and help | `/`, arrows, Enter, Escape, `?`, Tab/Shift+Tab, focus return | Keyboard only | not executed in this seat - visual pass pending |
| Personal Today and Work | Synthetic demo, demo-error, empty, rejected, active lease, in-review | 400 px and 1440 px | not executed in this seat - visual pass pending |
| Personal tabs and search | Arrow/Home/End, `/`, Escape, result activation, skip link | Keyboard only | not executed in this seat - visual pass pending |
| AionUi `/pursers` | Empty status, bridge missing, invalid door, request rejected, partial join, success | 400 px and 1440 px | not executed in this seat - visual pass pending |
| AionUi Join and status cards | Tab order, Enter submit, focus after error/success, 200% zoom | Keyboard and zoom | not executed in this seat - visual pass pending |

## Proposed follow-up ticket titles

- AionUi: add redacted door validation and explicit confirmation before connect
- AionUi: expose partial connection state and door-free recovery
- AionUi: harden join loading, retry, and transport-error handling
- AionUi: render startup health and all connected seats
- AionUi: add post-join conversation and preset guidance
- Fleet: make global search a conforming ARIA combobox
- Fleet: improve disconnected-state diagnostics and recovery copy
- Fleet: remove non-actionable row focus and label keyboard help
- Personal: separate submitted and in-review work
- Beta UI: execute 400 px, 1440 px, keyboard, zoom, and host-theme acceptance

## Verification evidence

- Fleet test suite: 264 passed.
- Personal Apps contract: 136 passed, 1 skipped.
- AionUi Python suite: 9 passed; Node route/door/team suites: 34 passed.
- Personal UI: TypeScript check passed; Vite built a 404.28 kB single-file
  dashboard (98.14 kB gzip).
- HTTP checks: Fleet root 200 (163,241 bytes), disconnected Fleet API 503;
  Personal bundle 200 (404,280 bytes); AionUi root 200, empty status 200 with
  `seats: []`, and invalid door 400.
- Static markup check: language, viewport, control labels, status regions,
  terminology presence, breakpoints, and fallback contrast tokens inspected for
  all three surfaces.
