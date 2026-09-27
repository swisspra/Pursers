# Fleet display-ready acceptance after the visual merge

Ticket: `TK-c76070ab763d86141374`

Outcome: **corrections required**. The combined visual build preserves the
routes, source-backed data, responsive layout, refresh state, and guarded
action boundaries that were exercised here. It is not display-ready yet
because two families of links miss the 44×44 CSS-pixel target requirement and
two keyboard flows do not leave focus/UI state in the expected place.

## Exact sources and method

- Before source: `5d3f1118d718d5e11f4b8c219cdf7d82b5f358ac` (`v5.0.5`).
- After UI source: `3aac6b6e7f034814ae83f973afdd29ee14162075`, the coordinator-authorized
  combined checkpoint.
- Browser: Ego Lite's Chromium surface. Browser evidence used one TaskSpace
  for the whole acceptance run.
- Fixture: `display-ready-v1`, served on loopback by
  `tools/fleet-dashboard/tests/display_acceptance_server.py`.
- Data: public-safe synthetic identifiers, 35 synthetic seats, five visible
  tickets, three journal events, one human request, one stale seat, and an
  explicitly bounded `5 of 8` ticket snapshot. No production board, token,
  door, profile, operator path, or personal identifier was read or captured.
- Viewports: 1440×900 in light/comfortable mode and 390×844 in dark/compact
  mode. A separate 200% page-scale pass covered the same eight routes with
  reduced motion enabled.

Each screenshot contains a visible evidence badge naming the source side,
short SHA, route, viewport, and fixture revision. The browser consumed real
responses from the product HTTP handler; expected values were not synthesized
from the predicates being checked. The browser run was interactive-no-send:
client-local navigation, filters, search, theme, density, disclosure, and
keyboard actions were exercised, while no production mutation endpoint was
called.

## Actual before / after / why

| Surface | Before observation | After observation | Why the change helps |
| --- | --- | --- | --- |
| Home | “Good to see your Team” led into several similarly weighted panels. | “Your team, at a glance” leads with one human decision, health counts, and the source-backed attention rail. | The next safe action and its reason are visible before supporting evidence. |
| Projects | “Connected projects” rendered a roomy project card and state pills. | “Your project map” aligns coordinator, health, ready/in-progress/review counts, routes, and bounded scope. | Ownership and project condition scan as one ledger record. |
| Work | Lifecycle groups were visually separated and the ticket identity moved between sections. | One filtered ledger keeps state, ticket, owner/lease, next gate, and evidence links aligned. | A lead can follow the same item while its lifecycle changes. |
| Team | Five filter controls occupied the top of the narrow layout. | A status strip precedes the roster and the mobile filter group is collapsed under `Filters`. | Held work and ownership arrive in the first mobile viewport. |
| Approvals | Human requests, review evidence, and intake had weaker ordering. | “Decisions, in safe order” puts the human request first, then independent review and guarded intake. | Decision authority and consequence are explicit without adding a new write path. |
| Activity | “Bounded activity” emphasized retained event volume. | “What changed, with its source” presents chronological outcomes with actor, role, board, journal sequence, and secondary provenance links. | The event remains primary while provenance stays available and source-backed. |
| Settings | Connections and advanced operations opened as a broad control surface. | A task/risk index leads to guarded editors, with project/seat/release intent separated. | The operator chooses an outcome before reaching a mutation boundary. |
| Board detail | The desktop table model compressed on narrow screens. | The selected ticket is retained and the 390px route uses readable stacked records while keeping Tickets, Timeline, Changes, Flow, and Routes. | Every recorded field remains available without page-level horizontal overflow. |

## Browser acceptance result

| Check | Actual observation | Result |
| --- | --- | --- |
| Primary routes | `home`, `projects`, `work`, `team`, `approvals`, `activity`, and `settings` all parsed and rendered from direct deep links. | PASS |
| Compatible aliases | `boards → projects`, `agents → team`, `operations/config/overhead → settings`; board `timeline`, `changes`, `flow`, and `routes` kept their deep-link parser contracts. | PASS |
| Responsive layout | All eight routes reported `scrollWidth == clientWidth` at 1440×900 and 390×844. | PASS |
| 200% zoom | All eight routes reported `visualViewport.scale == 2` and no page-level horizontal overflow. | PASS |
| Theme and density | Desktop evidence used light/comfortable; mobile evidence used dark/compact. Route, selected ticket, and source freshness stayed visible. | PASS |
| Reduced motion | With `prefers-reduced-motion: reduce`, the visible route matrix reported no non-zero animation or transition durations. | PASS |
| Large roster | Team rendered exactly 35 source-backed synthetic seats: 1 working, 33 available, and 1 stale. | PASS |
| Status semantics | Every visible `.status`, `[data-tone]`, and agent state in the matrix had non-empty text; color was not the only state carrier. | PASS |
| Empty states | Product responses with no boards or seats rendered explicit empty guidance on Home, Projects, Work, Team, Approvals, and Activity. | PASS |
| Error state | A product-generated `/api/fleet` failure kept the page bounded and exposed a visible reconnecting banner with error class and next checks. | PASS |
| Stale and bounded states | The same product response rendered one stale seat and `5 of 8` bounded ticket scope without inferring missing records. | PASS |
| Search keyboard flow | `/`, ArrowDown, and Enter selected `TK-working` and navigated to its direct link; the results popup reopened on the destination instead of closing. | FAIL |
| Help keyboard flow | `?` opened the native dialog and Escape returned focus to `#help-toggle`; the first Tab did not move focus inside the open dialog. | FAIL |
| Focus visibility | The Home primary action showed a 3px solid outline with measured 5.07:1 outline/background contrast. | PASS |
| Touch targets | The shell `Coordinator config` link measured 99.2×16px on every route. Five board-detail `View all` links measured 32.6×44px. | FAIL |
| Refresh preservation | Across three live refreshes, Home action focus, Team disclosure/focus/scroll, clean Settings server updates, dirty Settings input/focus, selected ticket, detail disclosure, scroll Y, dirty intake text, and network advancement were preserved. | PASS with test-fixture correction noted below |

## Accessibility findings

Standard: WCAG 2.1 AA.

| # | Finding | Criterion | Severity | Required correction |
| --- | --- | --- | --- | --- |
| A1 | `Coordinator config` has a 16px-high interactive box. | 2.5.5 Target Size | Major | Give the shell metadata link a minimum 44×44px hit area without changing its visible text hierarchy. |
| A2 | Board-detail `View all` links are only 32.6px wide. | 2.5.5 Target Size | Major | Add horizontal hit-area padding or a minimum inline size while preserving the 44px height. |
| A3 | The first Tab after opening keyboard help does not enter the modal. | 2.4.3 Focus Order; 2.1.1 Keyboard | Major | Move initial focus into the dialog and contain sequential focus until Escape/Close restores the trigger. |
| A4 | Enter activates a search result, but rerendering the destination opens the results popup again. | 2.4.3 Focus Order; 3.2.1 On Focus | Moderate | Clear or close the search state atomically before route render and keep the result list closed on the destination. |

No contrast failure was observed in the focused primary action. This browser
audit does not replace a manual VoiceOver/NVDA pass; it covers semantic DOM,
keyboard behavior, visible focus, target geometry, responsive layout, and zoom.

## Route, action, endpoint, and confirmation preservation

| Surface | Exercised or source-verified actions | Existing contract retained |
| --- | --- | --- |
| Global shell | Work/Personal context, seven routes, search, theme, density, help | `/api/version`, `/api/centrals`, `/api/fleet`; client-local preferences add no production action |
| Home | Review human request, view work, open attention/ticket | Fleet, workers, overhead, attention reads; attention acknowledgement remains `POST /api/attention` |
| Projects | Open project, open routes, reach guarded Add project | Fleet read; project creation remains `POST /api/projects/add` behind existing guards |
| Work | Filter lifecycle, open details, Timeline, Changes, Flow, Routes | Fleet read and `/api/board/{board}` |
| Team | Filter/reset, inspect held owner, reach worker/retirement controls | `/api/workers`, worker action endpoints, `/api/agents/retire`, `/api/agents/retire-inert` |
| Approvals | Inspect human request, submitted evidence, and guarded intake | `/api/human/resolve`, `/api/butler/mark`, board/intake reads; no review authority added |
| Activity | Open Ticket, Timeline, Changes, Flow, Routes | Fleet and board reads only |
| Settings | Open task/risk index and Butler editor; inspect autonomous command controls | `/api/butler`, `/api/butler/kill`, `/api/butler/autonomous`, `/api/butler/autonomous/command`, plus existing config/doors/release plan boundaries |
| Board detail | Select ticket; retain Tickets, Timeline, Changes, Flow, Routes and intake controls | `/api/board/{board}` and `/api/intake`; plan/authorization/confirmation boundaries unchanged |

The focused Fleet suite exercises the actual handler guards, allowed methods,
request shapes, same-origin checks, redaction, idempotency, and confirmation
paths. This acceptance did not send any production action.

## Combined-shell test-fixture correction

The first focused run returned `490 passed, 1 failed`. The only failure was
`test_refresh_cycles_preserve_reader_state_at_desktop_and_mobile`: it injects a
table `min-width:1100px` and then requires `scrollLeft == 35` at both
viewports. The combined desktop pane is wider than 1100px, so Chromium correctly
clamps `scrollLeft` to `0`; the 390px observation is `35`. Every actual
refresh-state assertion passed. This is a stale synthetic overflow assumption,
not observed product state loss. It is reported in coordinator question
`CQ-29a390ad63229610`; the fixture should force overflow above the desktop pane
width before the suite is rerun.

The touch-target findings are reported in coordinator question
`CQ-75b33ba3f9ee4b9a` for separate correction work. The keyboard findings are
reported in `CQ-4901dbc3646b09a5` for the same routing decision.

## Gallery

Desktop images use light theme and comfortable density.

| Route | Before (`5d3f1118d718`) | After (`3aac6b6e7f03`) |
| --- | --- | --- |
| Home | <img src="before-home-1440x900.png" alt="Fleet Home before at 1440 by 900" width="360"> | <img src="after-home-1440x900.png" alt="Fleet Home after at 1440 by 900" width="360"> |
| Projects | <img src="before-projects-1440x900.png" alt="Fleet Projects before at 1440 by 900" width="360"> | <img src="after-projects-1440x900.png" alt="Fleet Projects after at 1440 by 900" width="360"> |
| Work | <img src="before-work-1440x900.png" alt="Fleet Work before at 1440 by 900" width="360"> | <img src="after-work-1440x900.png" alt="Fleet Work after at 1440 by 900" width="360"> |
| Team | <img src="before-team-1440x900.png" alt="Fleet Team before at 1440 by 900" width="360"> | <img src="after-team-1440x900.png" alt="Fleet Team after at 1440 by 900" width="360"> |
| Approvals | <img src="before-approvals-1440x900.png" alt="Fleet Approvals before at 1440 by 900" width="360"> | <img src="after-approvals-1440x900.png" alt="Fleet Approvals after at 1440 by 900" width="360"> |
| Activity | <img src="before-activity-1440x900.png" alt="Fleet Activity before at 1440 by 900" width="360"> | <img src="after-activity-1440x900.png" alt="Fleet Activity after at 1440 by 900" width="360"> |
| Settings | <img src="before-settings-1440x900.png" alt="Fleet Settings before at 1440 by 900" width="360"> | <img src="after-settings-1440x900.png" alt="Fleet Settings after at 1440 by 900" width="360"> |
| Board detail | <img src="before-board-detail-1440x900.png" alt="Fleet board detail before at 1440 by 900" width="360"> | <img src="after-board-detail-1440x900.png" alt="Fleet board detail after at 1440 by 900" width="360"> |

Mobile images use dark theme and compact density.

| Route | Before (`5d3f1118d718`) | After (`3aac6b6e7f03`) |
| --- | --- | --- |
| Home | <img src="before-home-390x844.png" alt="Fleet Home before at 390 by 844" width="180"> | <img src="after-home-390x844.png" alt="Fleet Home after at 390 by 844" width="180"> |
| Projects | <img src="before-projects-390x844.png" alt="Fleet Projects before at 390 by 844" width="180"> | <img src="after-projects-390x844.png" alt="Fleet Projects after at 390 by 844" width="180"> |
| Work | <img src="before-work-390x844.png" alt="Fleet Work before at 390 by 844" width="180"> | <img src="after-work-390x844.png" alt="Fleet Work after at 390 by 844" width="180"> |
| Team | <img src="before-team-390x844.png" alt="Fleet Team before at 390 by 844" width="180"> | <img src="after-team-390x844.png" alt="Fleet Team after at 390 by 844" width="180"> |
| Approvals | <img src="before-approvals-390x844.png" alt="Fleet Approvals before at 390 by 844" width="180"> | <img src="after-approvals-390x844.png" alt="Fleet Approvals after at 390 by 844" width="180"> |
| Activity | <img src="before-activity-390x844.png" alt="Fleet Activity before at 390 by 844" width="180"> | <img src="after-activity-390x844.png" alt="Fleet Activity after at 390 by 844" width="180"> |
| Settings | <img src="before-settings-390x844.png" alt="Fleet Settings before at 390 by 844" width="180"> | <img src="after-settings-390x844.png" alt="Fleet Settings after at 390 by 844" width="180"> |
| Board detail | <img src="before-board-detail-390x844.png" alt="Fleet board detail before at 390 by 844" width="180"> | <img src="after-board-detail-390x844.png" alt="Fleet board detail after at 390 by 844" width="180"> |

These are worker-executed browser observations for independent review. They
are not represented as verifier-owned acceptance.
