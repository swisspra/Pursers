# Beta UI visual and keyboard acceptance pass

Ticket: `TK-ab876afbb077`

Final tested source: `origin/main@d95cc6ebe9ac962e895947d000fe20c0a682ae0a`

The initial full pass ran at
`2cdf414c8ac19a22bae5a23da97ca702ad7daa68`. During submission preflight,
`origin/main` advanced to the final source above with Fleet search and Personal
work-state changes. The branch was fast-forwarded, those affected browser
assertions and screenshots were rerun, and the intervening diff was checked to
confirm that the other captured paths were unaffected.

Browser: Ego Lite `ego-browser 0.5.0.32`, Chromium `152.0.7977.54`. No
Chrome, Safari, Edge, Playwright, or alternate computer-use surface was used.

## Method

The pass used Ego Lite task spaces (one full pass and one focused reconciliation
after `origin/main` advanced) and disposable loopback-only instances on ports
`30141` through `30144`. Fleet rendered the committed `HTML` constant
against disconnected and populated synthetic API fixtures. Personal rendered
the committed bundled MCP App and consumed synthetic `board_event_feed`,
`fleet_snapshot`, and `link_snapshot` results through the MCP Apps
`ui/initialize` and `tools/call` protocol. AionUi rendered the committed WebUI
assets and consumed synthetic loopback HTTP responses for each state.

All fixture names and identifiers are synthetic. No production board, token,
door, profile, or personal data was used. These are worker-executed browser
acceptance observations for independent review, not verifier-owned acceptance.

The viewport screenshots are exact CSS widths of 400 or 1,440 pixels. The
keyboard-only checks used Ego Lite keyboard input. The zoom check used a 400 px
viewport with `Emulation.setPageScaleFactor` set to `2`; Ego reported
`visualViewport.scale=2` and a 200 px visual viewport.

## Row-by-row results

| Surface and page | Result | Evidence and observation |
|---|---|---|
| Fleet `#/` — disconnected, reconnect banner, empty attention at 400/1440 | **FAIL** | Layout remained contained at both widths and the reconnect/empty-attention states were visible. The only central error was `HTTP 503`, with no cause or recovery action, and the header state remained `Connecting to centrals…` after the error rendered. Evidence: [400 px](01-fleet-disconnected-400.png), [1440 px](02-fleet-disconnected-1440.png). |
| Fleet `#/boards` and one workspace — populated long-title/table fixture at 400/1440 | **PASS** | Long board and ticket titles wrapped without page-level horizontal overflow. The workspace table stayed in its intended scroll container and status/actions remained reachable. Evidence: [boards 400 px](03-fleet-boards-400.png), [boards 1440 px](04-fleet-boards-1440.png), [workspace 400 px](05-fleet-workspace-400.png), [workspace 1440 px](06-fleet-workspace-1440.png). |
| Fleet `#/agents`, `#/operations`, `#/seats` — empty/populated cards and modal forms at 400/1440 | **FAIL** | Cards, controls, and the new-agent modal fit without page overflow, including the 400 px modal. The available reviewer card still renders mixed-language `ว่าง/idle` copy in an otherwise English interface. Evidence: [agents 400 px](07-fleet-agents-400.png), [agents 1440 px](08-fleet-agents-1440.png), [operations 400 px](09-fleet-operations-400.png), [seats 1440 px](10-fleet-seats-1440.png), [modal 400 px](11-fleet-agent-modal-400.png). |
| Fleet global search and help — `/`, arrows, Enter, Escape, `?`, Tab/Shift+Tab, focus return | **FAIL** | `/`, arrow selection, Enter routing, Escape clearing, the `combobox` role, `aria-expanded`, `aria-controls`, `aria-activedescendant=search-option-1`, and focus return to the help opener worked on the final source. Enter routed to the ticket but the results overlay reopened because the query remained. The help dialog had neither `aria-labelledby` nor `aria-label`, and Tab moved out to the document body. One non-actionable ticket table row also had `tabIndex=0`. Evidence: [keyboard help](12-fleet-keyboard-help.png). |
| Personal Today and Work — demo, demo-error, empty, rejected, active lease, in-review at 400/1440 | **PASS** | Demo, live empty, rejected, active lease, and error states remained contained at both widths. Light and dark host variables were applied exactly. On the final source, the `in_review` ticket rendered in a distinct `In review` group with its own count and explanatory copy. Evidence: [demo Today 400 px](13-personal-today-demo-400.png), [demo Work 1440 px](14-personal-work-demo-1440.png), [rich dark 400 px](15-personal-rich-dark-400.png), [rich light 1440 px](16-personal-rich-light-1440.png), [empty 400 px](17-personal-empty-400.png), [demo-error 1440 px](18-personal-demo-error-1440.png). |
| Personal tabs and search — Arrow/Home/End, `/`, Escape, result activation, skip link | **PASS** | Skip link focused first and moved focus to `#main-content`. ArrowRight selected Work, End selected Activity, and Home returned to Today. `/` focused search, a result was keyboard-activated into Work, and Escape cleared and hid results. Accessibility follow-up: the search input still lacks `aria-controls` and `aria-expanded`. Evidence: [keyboard search](19-personal-keyboard-search.png). |
| AionUi `/pursers` — empty, bridge missing, invalid door, rejected request, partial join, success at 400/1440 | **FAIL** | Empty, bridge-missing, and success states were distinct and contained. Invalid door, request rejection, and partial join all collapsed to `Join failed. Ask your coordinator to check the door.` The partial response (`joined: true`, MCP registration failed) left the card at `No connected seats`, hiding the durable partial connection. Evidence: [empty 400 px](20-aion-empty-400.png), [bridge missing 1440 px](21-aion-bridge-missing-1440.png), [invalid door 400 px](22-aion-invalid-door-400.png), [request rejected 1440 px](23-aion-request-rejected-1440.png), [partial join 400 px](24-aion-partial-join-400.png), [success 1440 px](25-aion-success-1440.png). |
| AionUi Join/status — tab order, Enter submit, focus after error/success, 200% zoom | **FAIL** | Tab order was door → Join → document → door. Enter submitted from the door field, cleared the secret, and returned focus to the door after both error and success. The form exposed no `aria-busy` state while joining. At 200% zoom, the document widened to 407 CSS px in a 400 px layout viewport, while the 336 px status card exceeded the 200 px visual viewport; its definition list was clipped horizontally. Evidence: [success at 200%](26-aion-success-400-zoom200.png). |

Summary: **3 passed, 5 failed**.

## Defect observations

1. **AionUi partial join is visually indistinguishable from a rejected join.** A
   response with `joined: true` and failed MCP registration produces the same
   generic text as invalid or rejected input and leaves `No connected seats`
   visible.
2. **AionUi status cards do not reflow at 200% zoom.** The two-column definition
   list clips values and creates horizontal overflow.
3. **Fleet keyboard behavior remains incomplete.** The help dialog has no
   accessible name or complete focus containment, Enter reopens search results
   after routing, and non-actionable rows remain in the tab order.
4. **Fleet disconnected recovery is not actionable.** The visible state shows
   only `HTTP 503`, while the header still says it is connecting.
5. **Personal search works functionally but does not expose its results
   relationship.** `aria-controls` and `aria-expanded` are absent.
6. **Fleet Agents still mixes Thai and English in the idle label.** The visible
   copy is `ว่าง/idle`.

## Recorded browser assertions

- Fleet 400/1440 document widths matched their viewports on overview, Boards,
  Agents, Operations, and the agent modal. The 400 px workspace used its
  intentional table scroll container.
- Fleet search returned five options. ArrowDown left focus on the input,
  selected `search-option-1`, and set `aria-activedescendant` to that ID.
- Personal host-theme tokens resolved to dark `#111827` / `#f9fafb` and light
  `#f8fafc` / `#172033` for background/text.
- Personal keyboard result activation selected `#tab-work`; Escape left an
  empty input and hidden results panel.
- Personal Work rendered `Open`, `Working`, and `In review` as three distinct
  groups on the final source.
- AionUi had no page-level overflow at 400 or 1440 before zoom. At 200% zoom,
  `visualViewport.scale=2`, `visualViewport.width=200`, and
  `documentElement.scrollWidth=407`.
