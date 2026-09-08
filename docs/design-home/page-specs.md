# Warm Guided Home Page Specifications

## Shared Shell

- Direction: operator-approved Direction A, Warm Guided Home.
- Navigation: Home, Projects, Work, Team, Approvals, Activity, Settings.
- Context: WORK and PERSONAL stay visibly distinct.
- Truth labels: Demo, Local, Read-only, Bounded, Stale, Truncated, Permission denied, and partial states remain explicit.
- Team contract: one folder per teammate, monitor-only lead, and board-driven dispatch. No ACP, forks, or automatic scaling.
- Accessibility: visible focus, semantic headings, 44px minimum interactive targets, reduced-motion support, and no horizontal page overflow at 390px.
- Tokens: `docs/design-home/design-tokens.css`. AionUI host typography and color variables take precedence when embedded.

## Pages

| Destination | Draft | Preview | Implementation contract |
| --- | --- | --- | --- |
| Home | `3c7bf43a-9ae1-451d-8fbc-97a9468c2fab` v3 | https://p.superdesign.dev/draft/3c7bf43a-9ae1-451d-8fbc-97a9468c2fab | Lead with the next safe human action, then project health, current work, Team, approval, and reviewed result. First-run onboarding must not appear beside populated returning-user data. |
| Projects | `69b3340a-b9fd-47ab-9882-79fda80ac431` v2 | https://p.superdesign.dev/draft/69b3340a-b9fd-47ab-9882-79fda80ac431 | Project selection, registry/connection health, stale last-known data, empty project guidance, and supported connect/add-project entry. |
| Work | `338327db-9531-43a3-ab58-63f20617d7f3` v1 | https://p.superdesign.dev/draft/338327db-9531-43a3-ab58-63f20617d7f3 | Preserve Open, Working, Submitted, Needs attention, Done, Ended, and unknown states; ticket detail progressively exposes timeline, changes, flow, routes, lease, and reviewed handoff. |
| Team | `274d5b8a-c348-4193-8ffc-6166a899927a` v1 | https://p.superdesign.dev/draft/274d5b8a-c348-4193-8ffc-6166a899927a | Roster and readiness first; show role, tier, project, assignment, idle/stale/retired/duplicate/partial-start states. Worker and seat lifecycle operations stay in advanced Settings. |
| Approvals | `f928178a-b5e2-4de9-a1ba-c6880499efa4` v1 | https://p.superdesign.dev/draft/f928178a-b5e2-4de9-a1ba-c6880499efa4 | Keep intake, human requests, submitted review, and guarded destructive confirmations distinct. Show scope, evidence, age, waiting party, conflicts, and only supported actions. |
| Activity | `7c2fd485-4cb0-409c-8fb4-f76275edb548` v2 | https://p.superdesign.dev/draft/7c2fd485-4cb0-409c-8fb4-f76275edb548 | Bounded chronological history with actor, ticket/decision/result provenance, filters, relationships, has-more, dropped, stale, cursor, and resync states. Never imply a complete audit graph. |
| Settings | `44799faf-b589-43b1-85db-b1ab705e9fb1` v2 | https://p.superdesign.dev/draft/44799faf-b589-43b1-85db-b1ab705e9fb1 | Group Connections, Team & seats, Dispatch, Workers, Doors, Diagnostics, and Release operations. Separate view-only summaries, plans, explicit confirmation, and applied mutation state. Secrets stay redacted. |

## Source-Backed Boundary

- Live dashboard route includes `#/seats`.
- Live APIs include `/api/config`, `/api/intake`, `/api/dispatch`, `/api/workers`, and `/api/doors`, plus their existing guarded subroutes.
- Bare HTTP `/central/{central}/workers` and `/seats` are not source routes. The source-backed hash routes are `#/central/<central>/workers` and `#/seats`.
- Draft controls illustrate information architecture. They do not prove a host API, connector mutation, Team lifecycle mutation, or release operation succeeded.

## Export Map

The implementation-reference HTML is under `docs/design-home/exports/`:

- `home.html`
- `projects.html`
- `work.html`
- `team.html`
- `approvals.html`
- `activity.html`
- `settings.html`

These exports are design references, not production templates. Implementers must preserve existing authorization, origin, loopback, redaction, idempotency, and confirmation guards.
