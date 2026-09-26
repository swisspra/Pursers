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
