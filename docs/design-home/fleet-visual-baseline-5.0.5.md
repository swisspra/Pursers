# Fleet 5.0.5 visual baseline and display-ready brief

Status: design/report only. This document does not authorize an API, route,
command, permission, or runtime change.

## Audience and job

The primary user is a human team lead beginning or resuming a shift. Within ten
seconds they should know: what needs attention, who owns it, what is blocked,
and the next safe action. The visual direction is **Shift Ledger**: an
operational handoff surface with one strong attention rail, compact ledger rows,
and quiet evidence panels. It should feel like a calm control room, not a card
gallery or a developer console.

## Source-bound baseline

The baseline was captured from exact source `5d3f1118d718d5e11f4b8c219cdf7d82b5f358ac`
(`v5.0.5`) with synthetic public-safe data. Chromium rendered every route below
at 1440×900 and 390×844. Sixteen full-page captures were inspected; both
viewports finished with `scrollWidth == clientWidth`. No screenshot, fixture,
name, path, ticket, or credential from a private environment is part of this
report.

| Route | Current visual evidence | Design implication |
| --- | --- | --- |
| Home | Strong lead action and useful counts, followed by large empty/evidence panels | Keep one lead action; turn the rest into a compact shift summary |
| Projects | One roomy project card carries status chips and two actions | Use ledger rows so ownership, health, and next action scan together |
| Work | Each lifecycle state becomes a separate large card | Make state a column/filter and tickets the stable rows |
| Team | Desktop is readable; mobile spends nearly its first viewport on five filter controls before seat data | Collapse filters into one summary control on narrow screens; show owner/work first |
| Approvals | Empty evidence panels dominate while the one submitted item sits lower | Put actionable decisions first; collapse empty evidence groups |
| Activity | Chronology is legible but Ticket/Timeline/Changes links compete equally | Make the event the target and keep provenance as secondary disclosure |
| Settings | A long provider form precedes the settings index and diagnostics | Lead with a settings index and risk class; open one bounded editor at a time |
| Board detail | Desktop table is strong; at 390px the table compresses headings, lifecycle, evidence, status, and timestamps into narrow columns | Switch ticket rows to stacked records below 720px while preserving every field and disclosure |

Existing strengths to retain: restrained green, clear guarded-write language,
visible connection/update state, explicit empty states, keyboard-visible native
controls, stable route names, and zero horizontal page overflow in the capture.

## Visual system

### Color tokens

Use six semantic tokens. Status is never communicated by color alone.

| Token | Hex | Use |
| --- | --- | --- |
| `--ledger-paper` | `#F6F4EE` | Page ground; warm enough for long reading without becoming decorative cream |
| `--ledger-ink` | `#1F2925` | Primary text and high-value numbers |
| `--ledger-rule` | `#CFD6D0` | Structure, row boundaries, and input outlines |
| `--ledger-signal` | `#2F6F61` | Current location, verified/ready state, and primary safe action |
| `--ledger-watch` | `#A96016` | Needs-attention and expiring state, paired with text/icon |
| `--ledger-stop` | `#A33A32` | Failed, disconnected, or destructive state, paired with text/icon |

Avoid decorative gradients. Use white only for the active reading surface, not
for every container. This deliberately replaces the current repeated white-card
kit with rules, rows, and one highlighted attention surface.

### Type scale

Use self-hosted **IBM Plex Sans** for the whole interface; tabular numerals for
counts and timestamps. IDs remain in the same family at medium weight rather
than defaulting all metadata to monospace.

| Role | Size / line height | Weight |
| --- | --- | --- |
| Page title | 40 / 44 desktop; 30 / 34 mobile | 600 |
| Section title | 22 / 28 | 600 |
| Row title | 16 / 22 | 600 |
| Body/control | 15 / 22 | 400 or 500 |
| Metadata | 13 / 18 | 400; labels use sentence case |
| Metric | 28 / 32 | 600 with tabular numerals |

Keep prose measures under 72 characters. Do not use tracked all-caps eyebrows;
the route title and navigation already establish context.

## Layout sketches

Desktop uses a 208px navigation rail and a fluid reading pane capped at 1280px.
Alignment is left and row-based. The single memorable element is the attention
rail directly under the header.

```text
┌ navigation ┐ ┌ shift header ─ search ─ freshness ────────┐
│ Home       │ │ ATTENTION  owner · reason · next action   │
│ Projects   │ ├────────────────────────────────────────────┤
│ Work       │ │ health counts │ capacity │ review queue   │
│ Team       │ ├────────────────────────────────────────────┤
│ Approvals  │ │ ledger rows: state | item | owner | next  │
│ Activity   │ │ evidence/provenance expands in place      │
│ Settings   │ └────────────────────────────────────────────┘
└────────────┘
```

At 390px, keep the seven route destinations in a horizontally scrollable,
sticky top rail (the current mental model), then stack information in decision
order. Filters become one disclosure showing the active filter count.

```text
┌ Home Projects Work Team Approvals … ┐
├ title + freshness                    ┤
├ ATTENTION                            ┤
│ owner                                │
│ why now                              │
│ [next safe action]                   │
├ status strip (horizontal)            ┤
├ Filters (2 active) ▾                 ┤
├ item title                           ┤
│ state · owner · lease                │
│ next action                          │
└ evidence ▾                           ┘
```

## Component hierarchy

1. `AppShell`: context switcher, primary routes, theme/density/help, search, and
   source freshness.
2. `ShiftHeader`: plain route title plus one sentence describing the decision.
3. `AttentionRail`: highest-priority item, owner, reason, age, and one safe CTA.
4. `StatusStrip`: project, working, review, blocked, ready, and stale counts.
5. `Ledger`: stable rows shared by Projects, Work, Team, Approvals, and Activity.
6. `LedgerRow`: identity/title, state, owner/lease, next action, then evidence.
7. `EvidenceDisclosure`: timestamps, provenance, flow, changes, and routes.
8. `GuardedEditor`: risk statement, fields, validation state, and existing
   confirmation boundary for intake/configuration/operations.

Home composes 1–4 and only the first few ledger rows. Other routes start at the
ledger or guarded editor. Components share information order, not identical
rounded boxes.

## Route, action, and API preservation map

The source-backed route parser and renderers are in
`tools/fleet-dashboard/fleet_dashboard.py:8100-8243`; hub and settings extensions
continue through `tools/fleet-dashboard/fleet_dashboard.py:8315-8772`.

| Surface | Existing actions that remain | Current API contract |
| --- | --- | --- |
| Global shell | switch Work/Personal context, navigate, search, theme, density, help | `GET /api/version`, `/api/centrals`, `/api/fleet?central=…`; theme/density/search remain client-local |
| Home | open submitted work, view all work, open project/attention item | fleet reads plus `GET /api/workers`, `/api/overhead`, `/api/attention`; attention dismissal/state uses `POST /api/attention` |
| Projects | open project, open routes, add project from connections | fleet reads; `POST /api/projects/add?central=…` |
| Work | open Details or Flow for every visible ticket | fleet read, then `GET /api/board/{board}?central=…` |
| Team | filter/reset/show stale; add/update/test/start/stop/restart worker; copy seat command; retire agent or inert board seats | `GET/POST /api/workers?central=…`, `POST /api/workers/{name}/{action}`, `/api/agents/retire`, `/api/agents/retire-inert` |
| Approvals | inspect submitted evidence; resolve/reopen/cancel human request; mark Butler agreement; open guarded intake | `POST /api/human/resolve`, `/api/butler/mark`; board/intake reads |
| Activity | open Ticket, Timeline, Changes, Flow, or Routes | fleet read and `GET /api/board/{board}?central=…` |
| Settings | validate/save/stop Butler; edit shadow autonomous policy and issue reconcile/kill/resume; open connections, seats, readiness, coordinator config, workers, overhead, and guarded operations | `GET/POST /api/butler`, `POST /api/butler/kill`, `GET/POST /api/butler/autonomous`, `POST /api/butler/autonomous/command`, plus the admin contracts below |
| Board detail | Tickets/Timeline/Changes/Flow/Routes tabs; sort/select ticket; submit ask; approve/decline queued ask | `GET /api/board/{board}?central=…`, `GET/POST /api/intake?central=…` |

Advanced settings routes remain reachable, but visually subordinate to the
human lead workflow:

- coordinator policy: `GET/POST /api/config?central=…`;
- seats/bridge/release/registry/dispatch: `GET /api/config/seats`,
  `/api/config/bridge`, `/api/config/release`, `/api/config/registry`, and
  `/api/dispatch`; planning and writes use `/api/config/plan`, `/apply`,
  `/prompt`, `/suggestions`, `/doctor`, `/import`, `/bridge/install`,
  `/bridge/upgrade-all`, `/registry/clone`, and `POST /api/dispatch`;
- guarded operations: `POST /api/config/ops/plan`, then `/api/config/ops`, with
  bounded status from `GET /api/config/jobs/{job}`;
- connections/doors: `GET /api/doors`, `POST /api/doors/copy`, and
  `/api/doors/rotate`;
- diagnostics: `GET /api/overhead?central=…`.

Route aliases remain compatible: `#/boards` maps to Projects, `#/agents` to
Team, `#/operations` to Settings, and `#/config` plus
`#/central/{central}/{config|overhead}` retain their deep-link behavior. Board
deep links retain `tickets`, `timeline`, `changes`, `flow`, `routes`, `ticket`,
and `since` parameters.

## Before / after / why

| Before | After | Why |
| --- | --- | --- |
| Six lifecycle cards on Work | One ticket ledger with state filter/grouping | Ticket identity and ownership stay aligned while state changes |
| Team mobile shows five full-width filters before useful rows | One `Filters (n active)` disclosure, then seat rows | A lead reaches ownership and held work in the first viewport |
| Approvals starts with empty analytical panels | Actionable human/review queue first; empty analytics collapsed | Empty evidence must not outrank a decision |
| Settings opens on a long provider form | Risk-labeled settings index, one editor opened at a time | Reduces accidental scanning burden without weakening guards |
| Board mobile compresses desktop table columns | Stacked ticket records below 720px | Preserves all fields and readable order without horizontal overflow |
| Similar rounded cards at every hierarchy level | Attention rail, ruled ledger rows, and evidence disclosures | Structure communicates urgency and evidence instead of decoration |
| “Good to see your Team” | “Your shift at a glance” | Clear sentence case and task-oriented language |

This direction was checked against generic dashboard defaults. The revision
removed a proposed grid of metric cards and decorative color wash; the ledger
and shift handoff metaphor are specific to coordinated agents, leases,
evidence, and guarded next actions.

## Motion restraint

- Route changes and keyboard-initiated actions do not animate.
- Buttons use only `transform 160ms cubic-bezier(0.23, 1, 0.32, 1)` with
  `scale(0.97)` while pressed.
- Occasional disclosure/popover entry may use opacity plus `scale(0.97)` for
  160ms with the same ease-out curve and an origin at its trigger.
- Live data changes do not slide or count upward. A 160ms background-color
  acknowledgement may mark a changed row, while text and focus stay stable.
- `prefers-reduced-motion: reduce` removes transforms and retains only a 160ms
  opacity/color acknowledgement. No transition targets `all`, layout size, or
  position.

## Acceptance screenshot matrix

Capture exactly the following against one fixed synthetic fixture and the exact
candidate SHA. Each image must show route name, source freshness, viewport, and
the same fixture revision in the evidence manifest.

| Viewport | Required screenshots |
| --- | --- |
| 1440×900 | Home attention rail; Projects ledger; Work with active state filter; Team with held owner; Approvals with one decision; Activity with provenance disclosure; Settings index plus one open guarded editor; board detail with one selected ticket |
| 390×844 | The same eight states, including sticky route rail, collapsed filters, stacked ticket detail, visible primary action, and no clipped text |

Automated acceptance for every capture: `scrollWidth == clientWidth`; no private
strings or credentials; visible focus at 3:1 or better; text contrast at least
4.5:1; tap targets at least 44×44; status has text/icon beyond color; route and
selected ticket survive three refresh cycles; dirty forms are not overwritten;
freshness/disconnect state remains visible.

## No-behavior-loss checklist

- [ ] All primary and alias routes above resolve from an existing deep link.
- [ ] Every read and mutation still calls the same endpoint, method, parameters,
  redaction, idempotency, plan, authorization, and confirmation boundary.
- [ ] Search, filters, sort, theme, density, clipboard actions, disclosures, and
  selected ticket remain keyboard operable.
- [ ] Unknown, stale, disconnected, empty, partial, truncated, conflict, and
  error states remain distinguishable and source-backed.
- [ ] Ticket fields, lifecycle steps, actor/provenance, lease state, timestamps,
  changes, routes, and handoffs remain available.
- [ ] Desktop and mobile preserve window/nested scroll, focus, open details,
  filters, route, selected ticket, and unsaved input across refreshes.
- [ ] Destructive and external effects never move into the attention rail
  without their existing confirmation step.
- [ ] Dark theme, compact density, reduced motion, zoom to 200%, and screen
  reader landmarks receive the same acceptance matrix.
- [ ] No version, API, action handler, cadence, command, deployment, or launchd
  change is bundled with the visual implementation.

Implementation should proceed route by route behind screenshot comparison,
starting with shared shell/ledger primitives, then Home, Work, Team, board
detail, Approvals, Activity, Projects, and Settings last.
