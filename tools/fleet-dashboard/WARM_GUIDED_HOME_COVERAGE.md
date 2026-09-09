# Fleet Warm Guided Home Coverage

The Fleet dashboard implements the approved Direction A shell without changing
its loopback API, authorization, redaction, confirmation, or idempotency guards.

| Destination | Source-backed capability | Evidence route or endpoint |
| --- | --- | --- |
| Home | Fleet health, attention, human requests, next safe action | `#/home`, `GET /api/fleet`, `GET /api/attention` |
| Projects | Central and board selection, registry health, add project | `#/projects`, `#/central/<central>/board/<board>/tickets`, `POST /api/projects/add` |
| Work | Ticket states, detail, timeline, changes, flow, routes, intake | `#/work`, `#/central/<central>/board/<board>/{tickets,timeline,changes,flow,routes}`, `GET/POST /api/intake` |
| Team | Agent pool, roles, current work, worker controls | `#/team`, `GET/POST /api/workers`, guarded worker actions |
| Approvals | Human requests, submitted review, intake decisions | `#/approvals`, `POST /api/human/resolve`, `GET/POST /api/intake` |
| Activity | Bounded state, per-board actor/event provenance | `#/activity`, board `timeline`, `changes`, and `routes` |
| Settings | Connections, seats, dispatch, workers, doors, diagnostics, release operations | `#/settings`, `#/seats`, `GET/POST /api/config`, `GET/POST /api/dispatch`, `GET/POST /api/workers`, `GET/POST /api/doors` |

Legacy hash routes remain valid and map to the nearest new destination. The
dashboard continues to label local, bounded, stale, truncated, and guarded
states rather than presenting them as complete or remotely verified.
