# QoderWake UX research: coordination flow

Research date: 2026-09-15

Scope: public product pages and assets only; no sign-up, download, install, or contact flow.

Pursers baseline: `origin/main` at `02e2dad2f9b339137fa8c7d947ef480408362446`.

## Executive read

QoderWake's strongest first-impression move is not a novel workflow control. It is a
single, polished workspace image that makes an AI team legible at a glance: three
setup steps, a named roster with roles and online state, connected chat spaces, and a
horizontal recent-activity trail. Its copy then repeats one simple coordination
promise: work in parallel, hand off, review, and remain traceable.

Pursers already exposes more concrete lifecycle evidence than the QoderWake public
page: bounded journal events, ticket states, dispatch history, ticket flow, route
provenance, review readiness, leases, and handoff memories. The opportunity is to
compose those existing facts into a clear ticket story. For beta.2, the best return is
an inline lifecycle rail plus a ticket-scoped activity/handoff summary in Fleet and
Personal. A drag-and-drop kanban or invented “agent thinking” display would add risk
without improving truthfulness.

## Evidence and limits

Primary source: the official [QoderWake product page](https://qoder.com/en/qoderwake).
The page was fetched as public HTML and its linked official image assets were
inspected at their published resolution. The official
[QoderWake blog URL](https://qoder.com/en/blog/qoderwake) was also fetched, but its
publicly extracted content exposed only shell/footer material, so it is not used for
product-detail claims.

The preferred Ego Lite browser binary was attempted but could not connect to its
bootstrap from this sandbox. The in-app browser surface was also unavailable. This
report therefore distinguishes HTML copy, static product artwork, and an animated
GIF asset; it does not claim hover, scroll, transition timing, or live-product
behavior.

## What the public page actually shows

### Page hierarchy and message

The page opens with the exact heading **“QoderWake”** and hero line
**“Autonomous AI Employees, On the Job.”** The supporting sentence says it is
embedded where teammates already work and carries a task through delivery. The next
major section, **“Meet AI Employees That Truly Own the Work,”** uses three feature
cards:

1. **“A Teammate in the IM Chat”** — a chat request, `@ Waker Summarize issues in
   this iteration`, followed by a structured release-summary response.
2. **“Autonomously Start Work on Its Own”** — an “Automation” card with a green
   `Active` dot, a daily `09:00` schedule, run limits, and an instruction field.
3. **“Multiple Wakers, Working Together”** — a workspace membership card with six
   illustrated avatars, a leader badge, an Add control, `Group Skills`, and
   `Group Coordinate SOP`. The adjacent copy says: “One task, split across roles:
   they work in parallel, hand off as needed, and review each other — fully
   traceable.”

The **“How QoderWake Works”** section follows with three system explanations. The
coordination-relevant heading is **“Harness Engineering for Stable Delivery.”** Its
illustration is a left-to-right dashed flow: agent glyph → “Answer questions or hand
off tasks” → checklist → observation/verification glyph → “Automate Execution,”
with `Plan` looping back into the flow. The text claims persistent task state,
recovery, verification, and retry for work lasting hours or days.

Further exact headings—**“Your First AI Employee,” “Job skills continuously
expand,” “Acts Inside Your Existing Workflows,” “Built for Real Work, Not for
Demos,” “Customize for Each Profession,” “Knowledge Retention and Continuous
Evolution,” “Secure, Governable, Accountable,”** and **“An AI Team for the Whole
Organization”**—reinforce identity, continuity, and teamwork. They are marketing
sections, not screenshots of a lifecycle surface.

### Hero product screenshot

The hero artwork shows a dark QoderWake dashboard inside a pale/moss-green landing
composition:

- a warm-black left sidebar and near-black content cards;
- a neon lime-green mark and `@Waker` title, **“Wake up your AI employee team”**;
- a three-step setup strip connected by arrows: `Choose an IM connection` →
  `Connect an IM conversation` → `Multiple Wakers collaborate`, with overlapping
  hand-drawn avatar cards;
- a `Product Spec Review Workspace` with tabs `Overview`, `Memory`, `Task`, and
  `Expert Configuration`;
- an `Assigned Waker` table with columns `Waker`, `Role`, `Status`, `Model`, and
  `Workspace`; the visible rows name Alexander, Kevin, and Vivienne, show distinct
  roles, and mark each `Online`;
- IM connection cards; and
- `Recent Activity` as a horizontal line with colored nodes and labels including
  `Memory Added`, `Automation Run`, `Task Created`, and `Skills Self-Evolution`.

The hierarchy is cinematic rather than dense: oversized setup guidance first,
roster second, then activity. Lime status accents and the illustrated portraits do
most of the identity work; white/gray sans-serif labels and tabular dark cards keep
the product shell quiet.

### Status language, empty states, and motion

Visible status language is human-readable and affirmative: `Online`, `Active`,
`Multiple Wakers collaborate`, `Recent Activity`, and event names. The page's
staff-badge GIF begins as a physical black ID badge with an illustrated portrait,
`Backend Engineer`, `ID: waker001`, and `Onboarded: 2026-03-24`. The asset is an
animated GIF, but its temporal sequence could not be verified in the available
static inspection path.

The public assets do **not** show:

- a real task board or draggable kanban;
- per-task status columns, state timestamps, lease/expiry state, or retry count;
- a visible source-to-destination handoff edge tied to one task;
- an approval request, reviewer decision, rejected/rework state, or approval queue;
- a completion receipt with files, tests, or reviewer evidence;
- empty, error, stale, offline, partial-data, or permission-denied states; or
- enough captured interaction to establish hover, focus, animation curves, reduced
  motion, keyboard behavior, or responsive behavior.

Those absences matter: “fully traceable” is page copy, while the screenshot proves
only a roster and a recent-activity summary.

## Side-by-side with Pursers today

| Concern | QoderWake public evidence | Pursers Fleet | Pursers Personal | AionUi / showcase |
|---|---|---|---|---|
| First frame | One branded workspace: setup, Wakers, IM cards, activity | Operational overview starts with fleet health, counts, “Waiting for you,” then findings | “Today” combines health, current work, agents, handoff/pinned context, and recent activity | Warm, guided shell; Home explains connection and Tickets presents lifecycle actions |
| Team identity | Illustrated named Wakers, role, model, workspace, `Online` | Agent pool shows role, current claim, project, freshness, and duplicate/stale state | Agent cards show status, role, focus, platform, idle time, and linked/current work | Seat/Team navigation exists; recent-ticket cards name the claimed identity |
| Task progression | Claimed in copy and harness illustration; no task-level UI shown | Real `Tickets`, `Timeline`, `Changes`, `Ticket Flow`, and `Routes` views exist, but the story is split across tabs | `Work` groups tickets by lifecycle; `Activity` is a separate bounded feed | `Ticket lifecycle` and `Recent tickets` show status and available actions, not event history |
| Handoff/review | Copy says Wakers hand off and review; no concrete per-task handoff control shown | Route provenance names created/executed/submitted/reviewed seats; ticket detail shows review label and annotations | Today can show `latest_handoff`; tickets expose review readiness, while events carry state transitions | Status chips such as `open`, `claimed`, `submitted`; no handoff rail in the captured screen |
| Traceability | Recent Activity nodes imply a trail | Journal-backed timeline has sequence, kind, transition, and timestamp; dispatch view has offer history | Bounded event feed explicitly labels its scope and truncation/staleness | Captured ticket screen is a current-state list |
| Visual tone | Near-black product cards, lime accents, moss/aqua editorial backdrop, illustrated people | Compact system UI, blue accent, dense tables/cards, light/dark and density controls | Airy blue/white cards, strong type hierarchy, semantic pills, light/dark themes | Cream/forest palette, large rounded cards, calm onboarding language |
| Failure/empty truth | Not shown | Explicit empty/loading/unavailable copy and bounded-response notes | Explicit demo, stale, error, truncated, empty, and reconnecting states | Connection and authentication states are prominent |

Relevant current implementation and visual evidence:

- Fleet composition and board views:
  `tools/fleet-dashboard/fleet_dashboard.py`.
- Personal rendering and responsive/theme behavior:
  `tools/dashboard-ui/src/dashboard.ts`, `tools/dashboard-ui/src/dashboard.css`, and
  `packages/personal/src/pursers_personal/resources/dashboard.html`.
- AionUi shell and ticket routes:
  `tools/aionui-extension/webui/app.js`,
  `tools/aionui-extension/webui/routes.js`, and
  `tools/aionui-extension/webui/style.css`.
- Captured baseline: `docs/showcase/01-fleet-overview.png`,
  `docs/showcase/02-personal-today.png`,
  `docs/showcase/03-personal-work.png`,
  `docs/showcase/04-personal-agents.png`,
  `docs/showcase/06-aionui-offer-claim.png`, and
  `docs/showcase/README.md`.

## Ranked proposals

Ranking balances first-impression impact against delivery effort. All displays must
derive labels and timestamps from current ticket, journal, memory, and dispatch
projections; no UI-only status may be invented.

### 1. Ticket lifecycle rail — high impact / S / beta.2

Put the ticket's current state and completed transitions directly inside its detail
instead of requiring a jump to the board-wide Timeline or Flow view.

```text
TK-123  Build release guide                         claimed · lease 8m
● Created ─── ● Offered ─── ● Claimed ─── ○ Submitted ─── ○ Reviewed
12:04          12:06         worker-7
                              ↑ current
```

- Components/files: Fleet `ticketView()` and event projection in
  `tools/fleet-dashboard/fleet_dashboard.py`; Personal ticket model/rendering in
  `tools/dashboard-ui/src/dashboard.ts` and styles in `dashboard.css`.
- Data: ticket state plus ticket-scoped journal transitions; dispatch supplies offer
  stage only when present. Missing stages render `Not observed`, never a guessed time.
- Acceptance: at 1440 px the rail is one readable row; at 400 px it becomes an
  ordered vertical list without horizontal page overflow. Status is conveyed by
  text/icon as well as color in light and dark themes. Tab focus reaches the ticket
  disclosure, and the rail itself adds no redundant focus stops.

### 2. “Now / Next / Blocked” ticket summary — high impact / S / beta.2

Translate protocol state into a compact, truthful sentence block at the top of an
expanded ticket.

```text
NOW      mong1-worker-7 is working · lease 8m
NEXT     Submit for independent review
BLOCKED  None recorded
```

- Components/files: Fleet `ticketView()` in
  `tools/fleet-dashboard/fleet_dashboard.py`; Personal `ticketRow()`/work-card
  rendering in `tools/dashboard-ui/src/dashboard.ts`.
- Data: claim/review lease, latest coordinator decision/blocker annotation, and
  lifecycle state. “Next” comes from an explicit state-to-label map, not generated
  prose.
- Acceptance: 1440 px uses three equal cells; 400 px stacks in source order. Empty
  blocker copy reads `None recorded`, stale data remains visibly stale, theme
  contrast passes existing tokens, and keyboard reading order matches visual order.

### 3. Ticket-scoped activity drawer — high impact / M / beta.2

Reuse the journal evidence already present in Fleet and Personal, filtered to the
selected ticket and summarized by meaningful event type.

```text
Recent activity                                      View all
#29240  claimed              worker-7       12:08
#29251  lease renewed        worker-7       12:18
#29277  decision added       coordinator    12:24
        Older events omitted from this bounded view
```

- Components/files: Fleet `timelineView()` plus `ticketView()` in
  `tools/fleet-dashboard/fleet_dashboard.py`; Personal `renderTimeline()` and
  `renderActivity()` in `tools/dashboard-ui/src/dashboard.ts`.
- Data: bounded journal events and their authoritative sequence numbers. Preserve
  truncation/resync notices and source-scope wording.
- Acceptance: newest three events inline at 1440 px and a compact disclosure at
  400 px; no event disappears solely because its label wraps. Disclosure works with
  Enter/Space, focus remains visible, event order is announced in DOM order, and
  light/dark use the existing semantic tokens.

### 4. Handoff card with accountable endpoints — high impact / M / beta.2

Make the latest explicit handoff actionable as a reading surface, while avoiding a
fictional graph when only one handoff memory exists.

```text
HANDOFF
worker-7  ───────────────▶  reviewer-unassigned
“UI shell ready for review”
Next: check 400 px layout · verify keyboard order       #MEM-…
```

- Components/files: Fleet ticket annotation/detail composition in
  `tools/fleet-dashboard/fleet_dashboard.py`; Personal `renderHighlight()` and link
  data in `tools/dashboard-ui/src/dashboard.ts`.
- Data: handoff memory author, explicit ticket link, next steps, and current review
  assignment. If the destination is absent, say `reviewer unassigned`.
- Acceptance: endpoint names wrap safely at 400 px; arrow also has accessible text;
  memory ID is copyable by keyboard where copy controls already exist; no arrow is
  shown without an explicit source and destination; both themes retain visible
  boundaries and focus rings.

### 5. Compact four-lane flow with attention lane — medium-high impact / M / beta.2

Keep Fleet's existing four-stage flow, but promote review/rework attention and add a
non-draggable compact form to Personal. This is a status board, not a task editor.

```text
OPEN (3)       WORKING (2)      REVIEW (1)       DONE (8)
[TK-1]         [TK-4 · w7]      [TK-6 · 14m]     [TK-8]
[TK-2]         [TK-5 · w2]      ↳ rejected ×1    [TK-9]
```

- Components/files: Fleet `flowView()` in
  `tools/fleet-dashboard/fleet_dashboard.py`; Personal work groups in
  `tools/dashboard-ui/src/dashboard.ts` and `dashboard.css`.
- Data: ticket status, review lease/offer, rejection count, and claimed identity.
- Acceptance: 1440 px shows four lanes; 400 px shows a select/tab and one lane at a
  time, with counts retained. Arrow keys operate the tab pattern, cards are links or
  disclosures rather than draggable controls, status is not color-only, and theme
  tokens cover rejected/overdue states.

### 6. Agent-to-work roster links — medium impact / S / beta.2

Borrow QoderWake's immediate team legibility while retaining Pursers' stronger
freshness and lease truth.

```text
AGENTS
[W7] mong1-worker-7   working   TK-123  lease 8m
[R1] reviewer-1       ready     —       seen 1m
[W2] worker-2         stale     TK-099  seen 42m
```

- Components/files: Fleet agent cards/table in
  `tools/fleet-dashboard/fleet_dashboard.py`; Personal `renderAgents()` in
  `tools/dashboard-ui/src/dashboard.ts`.
- Data: agent status, current ticket, lease, and last-seen/stale projection.
- Acceptance: ticket IDs navigate to the associated ticket at 1440 and 400 px; stale
  is stated in text rather than opacity alone; table collapses to cards without
  clipping; links and visibility controls have visible keyboard focus in both
  themes.

### 7. Completion receipt — medium impact / L / later

Give a closed ticket a compact, evidence-backed ending. Do not attempt this for
beta.2 until the bounded projection can supply every displayed field consistently.

```text
✓ CLOSED 12:42
Built by worker-7  •  Reviewed by reviewer-2
Commit 0a580e9     •  4 checks passed
Rework 1           •  Open submission evidence
```

- Components/files: Fleet route/ticket views in
  `tools/fleet-dashboard/fleet_dashboard.py`; Personal ticket and activity models in
  `tools/dashboard-ui/src/dashboard.ts`; Central/client projection work may be
  required before UI implementation.
- Data: submission metadata, exact branch/SHA, review verdict, test observations,
  and route/rework events. Never parse prose as the sole source of a “passed” claim.
- Acceptance: absent evidence becomes `Not supplied`; 400 px stacks fields; 1440 px
  remains compact; commit/review links are keyboard reachable; success is expressed
  with icon and label, and both themes meet the established contrast treatment.

## What not to copy

- **Do not copy illustrated portraits as operational identity.** They improve warmth
  but can hide duplicate names, principals, stale seats, and exact current claims.
  Pursers should keep initials or optional avatars secondary to identity facts.
- **Do not copy “fully traceable” as an unsupported blanket label.** Pursers has
  bounded views and must keep truncation, source, staleness, and “not observed” copy
  visible.
- **Do not turn status cards into draggable kanban by default.** Claim, submit,
  review, and close are guarded protocol transitions; drag implies an unsafe direct
  mutation and weakens keyboard/accessibility behavior.
- **Do not use neon green as the only state signal.** Preserve textual statuses,
  icons, and existing semantic theme tokens.
- **Do not animate live activity continuously.** Motion can imply progress where no
  new journal event exists, disrupt reading, and make a bounded feed look exhaustive.
  Update only on authoritative data arrival and honor `prefers-reduced-motion`.
- **Do not hide empty, stale, rejected, or partial-data states for a cleaner hero.**
  QoderWake's public art omits them; Pursers' operational value depends on making
  those states explicit.
- **Do not collapse Fleet, Personal, and AionUi into one visual density.** Fleet is an
  operator console, Personal is a read-only work home, and AionUi is guided local
  onboarding/action. Share lifecycle language and components where practical, not
  every layout decision.

## Recommended beta.2 cut

Ship proposals 1–4 and the small link/status refinements in proposal 6. Proposal 5
is worthwhile if Personal can reuse its existing lifecycle grouping without adding
mutation semantics. Defer proposal 7 until completion evidence is a stable bounded
projection. The result should make one ticket understandable in five seconds while
remaining honest about what the current response did—and did not—observe.
