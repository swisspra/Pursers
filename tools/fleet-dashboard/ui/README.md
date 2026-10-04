# Fleet UI asset contract

The Fleet dashboard is a loopback-only, dependency-free web application. The Python
server owns API behavior and serves this directory as packaged, same-origin assets.

`index.html` is the static application shell. `assets/fleet.css` owns shared tokens,
layout, responsive behavior, and component styles. `assets/app.js` owns shared state,
API calls, refresh preservation, authentication-safe requests, and DOM utilities.

Each primary route has one entry module in `views/`. A module registers exactly one
route through `FleetViewModules` and declares the UI surfaces it owns:

- `home.js`: fleet health, attention, and guided start
- `projects.js`: projects, boards, and workspace entry points
- `work.js`: ticket queues and filters
- `team.js`: agents, seats, and autonomous-butler status
- `approvals.js`: approval and human-request queues
- `activity.js`: recent and autonomous-butler activity
- `settings.js`: seat, dispatch, release, project, and door controls

Route modules own their renderer and route-private markup. `app.js` passes an explicit,
read-only render context containing shared state snapshots and bounded utilities;
cross-route state, API/refresh machinery, and event binding stay in `app.js`. A route
module must not delegate to a route renderer on `globalThis` or in `app.js`.

Assets use fixed same-origin URLs with strong ETags and mandatory revalidation, so
source checkouts cannot serve stale UI bytes after an update.

The compact navigation uses the supplied horizontal Pursers wordmark at
`assets/brand/pursers-wordmark.png`. It is an opaque-white PNG, so the shell keeps
an explicit white frame in both themes instead of recoloring the artwork. See the
[brand asset record](../../../docs/media/brand/README.md) for provenance, intended
placements, and the byte-preserving update procedure.

## Nocturne phase 1

The shared shell defines the Nocturne light/dark tokens and keeps the existing
route modules, same-origin assets, search, shortcuts, density control, Central
context, and selector contracts. At 800 CSS pixels and below, navigation becomes
an off-canvas drawer with a visible current-route label, focus containment,
Escape dismissal, and focus restoration. The Personal context remains visibly
unavailable instead of acting like a workspace switch.

Home renders bounded in-progress, review-ready, blocked, and open-queue totals;
source status; operational attention; human decisions; and observed seats. Counts
remain marked partial while optional sources enrich. A new intent opens the only
project directly or asks the operator to choose a project before entering the
existing scoped intake flow.

This slice does not redesign Work, Team, Projects, Activity, Approvals/Inbox, or
Settings/onboarding content. Those routes stay usable in the new shell and retain
their existing owners and guarded handlers until their planned migration phases.
