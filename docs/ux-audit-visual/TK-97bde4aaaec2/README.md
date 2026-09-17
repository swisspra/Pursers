# TK-97bde4aaaec2 — operator Fleet Browser201 capture

Operator-owned runtime capture for HR-0c237dccae079304. No fleet seat has a browser.

- Product candidate: `afaf26ad317df0e305107fef7150fae71324b633` (`origin/main`)
- Served by that candidate's own `tools/fleet-dashboard/fleet_dashboard.py` from a clean
  detached worktree, on loopback, against the live Central, under its own session identity
  (`fleet-dashboard-session-b201`).
- The served page was hashed before capture: sha256
  `d4c289ed89adcae41c995ed5140871ba2145713fd2f49f8aa807eb225e9e335a`, which is exactly the
  candidate HTML hash worker-13 named in the request. The already-running operator dashboard
  (`1e1abe5a…`) was not used.
- Driven by real Chrome over the DevTools Protocol at 1600x1200. Every match is read out of
  the rendered DOM of the running product. No fixture, no expected-value file, no adapter.

## Result

| outcome | rows |
|---|---|
| SUCCESS | 60 |
| FAILURE | 33 |
| BLOCKED | 0 |
| total | 93 |

Sixteen routes were surveyed: `#/`, `#/home`, `#/boards`, `#/projects`, `#/agents`, `#/team`,
`#/work`, `#/approvals`, `#/activity`, `#/operations`, `#/config`, `#/seats`, `#/settings`,
`#/central/default/config`, `#/central/default/workers`, `#/central/default/overhead`.

Twelve of the thirteen `data-pursers-*` names in the approved selector contract were found in
the live DOM. All 33 failures have the same single cause, and it is one defect, not 33.

## The one defect: `data-pursers-seat` is never rendered

`data-pursers-seat` is set only by the warm-home script, which walks
`.seat-layout table tbody tr` and stamps each row. `.seat-layout` is produced only by
`renderSeats()`. `renderSeats()` is reached only through
`renderHub = function(){ if (navKind() !== 'seats') return seatRenderHubV1(); … }`.

`tools/fleet-dashboard/warm_home.py` then replaces `navKind` with the warm-home vocabulary —
`home`, `projects`, `work`, `team`, `approvals`, `activity`, `settings` — which has no
`seats` member. Measured in the running product at `#/seats`: `route()` returns
`{"kind":"seats"}` while `navKind()` returns `"settings"`. The guard can therefore never pass,
`renderSeats()` never runs, `.seat-layout` never mounts (0 nodes on all 16 routes), and no
element ever carries `data-pursers-seat`.

So the seat-inventory surface is unreachable in the shipped UI at this candidate, and every
acceptance row whose binding names a seat row fails at runtime. The static precheck could not
see this: the attribute is present in the sources, which is what a static scan measures.

## Files

- `per-row-results.json` — all 93 rows with outcome, the attributes each binding requires,
  the occurrences and routes for each attribute that was found, and the reason.
- `evidence-manifest.json` — per-route survey (URL, DOM node count, `navKind`, `route().kind`,
  `.seat-layout` count, seat row count, connection-banner state, every `data-pursers-*`
  attribute with counts and the distinct values the product rendered) plus the union.

A connection banner was visible on one route (`#/work`) during the survey; it affects vertical
layout only, not attribute presence, and that route contributed no unique attribute.
