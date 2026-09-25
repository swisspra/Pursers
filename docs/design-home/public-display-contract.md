# Fleet Dashboard public-display contract

Status: design only for the 5.0.5 source baseline. This document does not enable
network publication or change product behavior.

## Decision

Public display is a separate, read-only server projection rendered by a separate
route family. It is not a query flag on an operator response, a CSS theme, DOM
redaction, blur, crop, or screenshot post-process.

The proposed boundary is:

```text
Central/admin credential -> private fetch/cache -> public projector -> public cache
                                                        |
                                             /public and /api/public/v1/*

Private operator browser -> / and /api/* (unchanged, loopback-only)
Display browser         -> /public and /api/public/v1/* (read-only allowlist)
```

Only the public projector may copy data across the boundary. Its output schema is
closed: unknown source keys are dropped, not passed through. Public HTML bootstraps
only public endpoints. A public request can never select the private renderer or
private cache object.

This is necessary because the current dashboard intentionally carries operator
data in API objects, visible text, `href`, `title`, input values, and `data-*`
attributes. Hiding those nodes would leave the values in HTML, accessibility trees,
browser developer tools, clipboard paths, search indexes, and network responses.

## Source-backed baseline

The following facts are true in 5.0.5:

- The launcher and argument parser bind/refuse anything except `127.0.0.1`
  (`tools/fleet-dashboard/launch.sh:26`,
  `tools/fleet-dashboard/fleet_dashboard.py:10182-10235`). A deployable public
  listener does not exist.
- Every current response sets `Cache-Control: no-store`, `nosniff`, and a
  self-only CSP, but GET endpoints do not have viewer authentication
  (`fleet_dashboard.py:9058-9075`). POST protection checks loopback Host,
  same-origin Origin, and JSON only (`fleet_dashboard.py:9077-9104`). Those are
  operator-local CSRF controls, not public-view authorization.
- `/api/fleet` is already bounded, but it exposes real board, ticket, agent,
  principal, lease, dispatch, human-request, and finding data
  (`fleet_dashboard.py:3768-4254`). `/api/board/<id>` adds descriptions,
  annotations, submission summaries, handoffs, actors, timestamps, lifecycle,
  coordination, and provenance (`fleet_dashboard.py:3100-3767`). Bounded does
  not mean public-safe.
- Home, Projects, Work, Approvals, Activity, and Settings render from these
  private objects (`tools/fleet-dashboard/warm_home.py:27-59`). Global search
  indexes real board labels/IDs, ticket IDs/titles/status/owners/descriptions,
  and agent names/boards (`fleet_dashboard.py:8074-8090`).
- Settings embeds local paths and commands in inputs and output, exposes
  provider/connector and dispatch details, and can perform mutations. Examples
  include the seat form and registry clone panel
  (`fleet_dashboard.py:8499-8571`) and door operations
  (`fleet_dashboard.py:8600-8755`).

## Current exposure inventory

This inventory describes the current private operator surface. “Public” below
means the proposed replacement, not an assertion that the current value is safe.

| Surface | Current source fields rendered or derived | Current browser-side carriers | Public disposition |
|---|---|---|---|
| Shared chrome | Central labels; deployed full SHA in `title`; generated time; connection error class/detail; route/hash state; theme/density; selected board | text, `title`, hash `href`, `data-board-id`, `data-pursers-selected-board`, live-region text, search DOM | Replace Central names with a single generic service label; expose release label only, never SHA; coarse freshness only; generic errors; public-local routes only |
| Home | project count; working/review-ready/attention counts; next human request message and ticket link; next submitted/open ticket title/ID; Central label and connection state; busy/available/stale; human requests (`ticket_id`, `request_id`, `message`, `kind`, `asked_by`, `asked_at`, schema, URL); Butler holds, evidence, question IDs, release times, precedents and human marks; coordinator findings; ticket age/title/ID; lease lapse count; expired-offer identities; context pressure with agent/board/token rates; push errors | text, links, form fields, buttons, `data-central`, `data-board`, `data-ticket`, `data-question`, `data-attention-key` | Counts after suppression only. No messages, IDs, names, URLs, evidence, token rates, failures, actions, or hidden metadata |
| Projects | Central label; board label and ID; status; ticket counts; snapshot returned/total; routes link | text, hash `href`, `data-board-id`, `data-central`, `data-pursers-board`, `data-pursers-status` | Stable project alias, health bucket, suppressed/bucketed workload, public alias route. No real label/ID, exact counts, or source cardinality |
| Work | ticket status; ID; title; board label; Central label; claimed owner; detail and flow links; unknown statuses | text and hash `href` | Status bucket plus anonymous ticket alias only when its cohort passes suppression. No title, real ID, owner, project name, or deep private route |
| Team | agent/principal IDs; display name; duplicate-name state; role; status; boards; current ticket ID/title/status; exact lease/last-seen; capability names and values; tier; model; provider/client/host; readiness; board scope; managed-worker state/log tail/commands; inactive identities; autonomous desired/actual capacity, ceilings, connector IDs/tools/resources, health and audit IDs | text, `title`, links, inputs, clipboard, dialogs, `data-agent*`, `data-central`, `data-name`, `data-board`, `data-state-key`, `data-pursers-*` | Aggregate role/status counts after suppression. Optional stable agent alias for active cohorts only; no principals, capabilities, models, clients, tickets, logs, commands, connector inventory, or exact timing |
| Approvals | submitted ticket ID/title/project; human-request data from Home; intake links; Butler holds/marks | text, links, forms, action buttons, `data-*` identifiers | Suppressed counts by broad state only (`awaiting_review`, `awaiting_human`). No item rows or actions |
| Activity | ticket title/ID, exact `updated_at`, status, board label; timeline/changes/routes links; autonomous command intent/ID/revision/time/audit/reason/status | text, links, `data-pursers-autonomous-team`, `data-autonomous-observation` | Coarse time-bucketed transition counts only. No item rows, actor/project/ticket identifiers, command or audit data |
| Settings | Central names; Butler runtime, endpoint, model, credential header/prefix, paths, extra headers, key presence/location and validation; autonomous policy/revision/budgets/connectors; local seat host/name/principal/capabilities/model/provider; Central/token/CA/config/repository paths; bridge versions/commands; Doctor results; offers and dispatch policy/history; operator and clone paths/status; release SHA/tags/commands/logs; doors, key IDs and one-time credential strings; provider worker configuration | text, password/text inputs, `<pre>`, `title`, clipboard, confirm dialogs, `data-*`, POST bodies | Route absent. Return 404 for all public Settings, config, worker, door, Butler, dispatch, attention, human-resolution, project-add, release, and job resources |
| Search | Boards: Central, label, ID. Tickets: ID, title, status, owner, description, board. Agents: Central, name, status, boards | text, `href`, `aria-activedescendant`, `data-search-index`, in-memory search items | Index only already-projected aliases/status buckets. Query stays client-side; no server logging, suggestions, descriptions, owners, names, or private links |
| Clipboard | seat provisioning/admin command; generated prompt; worker command; door string; rotated door string; project evidence values indirectly through controls | `navigator.clipboard.writeText`, password inputs, `data-hub-copy`, `data-copy-command`, `data-copy-input` | Clipboard actions do not exist in public HTML or JavaScript |
| HTML metadata | real Central/board/ticket/agent/seat names and IDs; status; paths; action payloads; titles; hidden form revisions; selector-contract attributes | `href`, `title`, `value`, `id`, `name`, `data-*`, ARIA labels/status, hidden inputs, inline script state | Permit only enumerated public aliases, route keys, presentation state, and accessibility labels. Run the same leak assertions over serialized DOM, not only visible text |

### Current HTTP endpoint inventory

The handler exposes the following routes in the private trust domain
(`fleet_dashboard.py:9106-9970`). None is inherited implicitly by public mode.

| Method | Private route(s) | Data or effect | Public contract |
|---|---|---|---|
| GET | `/`, `/api/version`, `/api/centrals` | operator HTML, commit identity, Central labels | Separate `/public`; public build metadata is a non-unique release label only |
| GET | `/api/fleet`, `/api/board/<board_id>` | private fleet and board projections | Replaced by `/api/public/v1/summary` and alias-scoped `/api/public/v1/projects/<alias>` |
| GET | `/api/overhead`, `/api/config`, `/api/dispatch` | protocol usage, coordinator configuration, offers and policy | 404 |
| GET | `/api/config/seats`, `/api/config/bridge`, `/api/config/release`, `/api/config/registry`, `/api/config/jobs/<id>` | local inventory, paths, versions, clones, job logs/results | 404 |
| GET | `/api/workers` | managed workers, providers, local state and commands | 404 |
| GET | `/api/doors` | protected-door metadata | 404 |
| GET | `/api/butler`, `/api/butler/autonomous`, `/api/intake`, `/api/attention` | provider/runtime settings, autonomous policy, drafts/intake, local acknowledgement state | 404 |
| POST | `/api/config/plan`, `/api/config/suggestions`, `/api/config/apply`, `/api/config/prompt`, `/api/config/doctor`, `/api/config/import`, `/api/config/bridge/install`, `/api/config/bridge/upgrade-all`, `/api/config/ops/plan`, `/api/config/ops`, `/api/config/registry/clone`, `/api/config` | local configuration, upgrades and operator actions | Method not allowed; route not registered |
| POST | `/api/butler`, `/api/butler/autonomous`, `/api/butler/autonomous/command`, `/api/butler/kill`, `/api/butler/mark` | provider configuration and Butler control/evaluation | Method not allowed; route not registered |
| POST | `/api/dispatch`, `/api/agents/retire`, `/api/agents/retire-inert`, `/api/attention`, `/api/human/resolve` | policy, lifecycle, local state and human decisions | Method not allowed; route not registered |
| POST | `/api/doors/copy`, `/api/doors/rotate`, `/api/projects/add` | credential return/rotation and project creation | Method not allowed; route not registered |
| POST | `/api/workers`, `/api/workers/<name>/<action>` | provider worker save/test/start/stop/restart | Method not allowed; route not registered |

The public listener must answer every unregistered `/api/*` path with the same
small 404 body. It must not reveal whether the private route exists. All non-GET
and non-HEAD methods return 405 before parsing a body.

## Public projection v1

### Closed schema

`GET /api/public/v1/summary` returns exactly:

```json
{
  "schema_version": 1,
  "mode": "public",
  "release": "5.0",
  "freshness": "recent",
  "health": "operational",
  "projects": [
    {
      "alias": "project-amber",
      "health": "active",
      "workload": "several",
      "work": {"queued": "several", "active": "few", "review": "few"}
    }
  ],
  "fleet": {
    "active_agents": "several",
    "roles": [{"role": "worker", "count": "several"}]
  },
  "approvals": {"awaiting_review": "few", "awaiting_human": "none"},
  "activity": [
    {"window": "recent", "transition": "completed", "count": "several"}
  ],
  "suppressed": true
}
```

`GET /api/public/v1/projects/<alias>` returns only `schema_version`, `mode`,
`release`, `freshness`, one project row with the same fields, and an optional
`work_items` array. Each work item has only `alias`, `state`, and `age_bucket`.
It is omitted unless both the project cohort and state cohort satisfy the
suppression rules below. There is no ticket detail endpoint in v1.

Allowed enum values are fixed:

- `freshness`: `recent`, `aging`, `stale`, `unavailable`;
- `health`: `operational`, `degraded`, `unavailable`;
- project `health`: `active`, `quiet`, `degraded`;
- count bucket: `none`, `few`, `several`, `many`;
- work `state`: `queued`, `active`, `review`, `completed`, `ended`;
- `age_bucket`/activity `window`: `recent`, `today`, `older`;
- activity `transition`: `created`, `started`, `submitted`, `completed`,
  `reworked`, `ended`;
- role: `worker`, `reviewer`, `coordinator`, `other`.

No free-form source string is permitted. In particular, public JSON contains no
real Central/board/project/ticket/agent/principal IDs or names; titles,
descriptions, annotations, findings, messages, URLs, branches, commits, files,
models, providers, clients, connector names, paths, commands, logs, errors,
headers, credential state, or source timestamps.

### Alias construction

Aliases must be stable within one installation but unlinkable across
installations:

1. Generate a 256-bit `public_alias_key` into the private state directory with
   mode `0600`; never return, log, back up with public assets, or accept it from
   a request.
2. Compute `HMAC-SHA256(key, "v1\0" + entity_type + "\0" + canonical_id)`.
3. Encode the first 80 bits with unpadded lowercase Base32 and prefix it with
   `project-`, `work-`, or `agent-`. A friendly color/word may be derived from
   the digest, but the digest suffix remains the collision-resistant key.
4. Keep aliases through rename because the input is the immutable internal ID.
   Key rotation deliberately changes all aliases. Never persist a reverse map
   in public storage.

The projector detects a collision before publishing and suppresses both rows.

### Suppression and granularity

Use `k = 5` as the default minimum cohort. The implementation may raise it but
must not lower it without a versioned contract change.

- Do not emit a project row until at least five distinct work items have existed
  in its rolling 30-day window. Do not emit a per-state count or item list until
  that state has at least five distinct items in the same window.
- Do not emit a role row until at least five distinct active agents share the
  role. Never emit an agent alias when fewer than five active agents exist in
  the project and role cohort.
- Replace exact counts with `none` (0), `few` (5-9), `several` (10-24), or
  `many` (25+). Counts 1-4 become `none` plus `suppressed: true`; never distinguish
  a zero from a suppressed low count in a narrower response.
- Coarsen source states: open/offered -> `queued`; claimed/in-progress/reporting
  -> `active`; submitted/reviewing -> `review`; closed -> `completed`; rejected,
  canceled, terminated and needs-human -> `ended`. Unknown states are suppressed,
  not copied.
- Round freshness and ages server-side: `recent` (<1 hour), `today` (1-24 hours),
  `older` (>24 hours). Public mode never exposes wall-clock time, timezone,
  lease expiry, heartbeat, event sequence, refresh interval, or exact ordering.
- Activity is delayed by at least 15 minutes and grouped into a rolling window.
  Random jitter of 0-5 minutes is fixed per bucket/HMAC input so refreshes do not
  become a timing oracle.
- Apply complementary suppression: when a total minus visible buckets could
  reveal a hidden 1-4 count, suppress the total or one additional bucket.

### Public HTML and browser behavior

The public page has Home, Projects, Work, Team, Approvals, and Activity. Settings,
board detail, ticket detail, routes, timelines, changes, intake, and operator
shortcuts are absent. The browser receives no private bundle or inline private
state.

- Home: overall health, coarse freshness, and suppressed aggregate cards.
- Projects: alias, coarse health, workload and state buckets.
- Work: suppressed alias/state/age rows only.
- Team: role/status count buckets; agent aliases only when the cohort permits.
- Approvals: two suppressed count buckets only.
- Activity: delayed transition buckets only.
- Search: local substring match over the already-projected alias and enum labels.
  Clear it on reload. Do not send queries, write analytics, or include source
  strings in an index, DOM comment, source map, or accessibility-only node.
- Clipboard: no copy controls and no clipboard writes.
- Attributes: allow only `id` from a static template, public-local `href`,
  `aria-*` with static/enumerated copy, and `data-public-alias`, `data-state`,
  `data-view`. Values must pass the alias/enum grammar. No `title`, source ID,
  action payload, hidden input, or private selector-contract attribute.

### Permission and deployment boundary

Public mode needs a distinct process identity and listener configuration. It
must not use the Central admin token used by the private dashboard. The preferred
design is a projector process with a read-only `board:read` credential for an
explicit allowlist of boards, writing signed projection snapshots to a private
handoff directory; a presentation process reads only those snapshots and has no
Central credential, worker manager, seat manager, door keys, or operator state.

If served outside loopback, termination must be HTTPS behind an authenticated
viewer gateway. A share link is not authorization. Recommended viewer claims
are audience-bound, short-lived, read-only, and scoped to `public-display:v1`.
The application still verifies the proxy identity or a signed viewer token;
source IP and Host are not identities. Do not add CORS. Set a strict Host
allowlist and rate limits at both proxy and application.

The public process registers only GET/HEAD routes. It does not import the POST
handler, worker manager, seat manager, door manager, Butler settings, release
operations, or private HTML constant. Filesystem permissions prevent it from
reading credentials and operator configuration even if routing regresses.

### Cache and error contract

- Projection snapshots are immutable documents named by content digest. The
  latest pointer is updated atomically only after schema validation,
  suppression, leak checks, and signature creation succeed.
- API responses use `Cache-Control: private, max-age=15, stale-if-error=60`,
  `ETag` derived from the public document only, `Vary: Authorization`,
  `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, and a CSP
  without inline script. The authenticated gateway/CDN must not use a shared
  cache unless its cache key includes the verified viewer subject and scope.
- A refresh failure may serve the last valid public snapshot for at most 60
  minutes with `freshness: stale`. After that, return a constant-shape 503 page
  and JSON: `{"schema_version":1,"mode":"public","freshness":"unavailable","health":"unavailable"}`.
- Public errors never include exception type/message, Central label/URL, board
  existence, filesystem path, retry timestamp, trace ID tied to private logs,
  or a partial projection. Status and body are the same for unknown and denied
  project aliases.
- Never mix source rows from different successful refresh generations. A
  partially refreshed fleet is rejected as a unit.

## Exact local preview

The operator command should be conceptually:

```text
fleet-dashboard public-preview --input /PATH/TO/private/snapshot.json \
  --output /PATH/TO/private/public-preview
```

It runs the production projector and production public renderer, with the same
schema version, alias key, suppression thresholds, timing delay, headers, and
assets. It starts a loopback-only, random-port, read-only preview server and
prints only its loopback URL and public document digest. It must not fall back to
private live endpoints.

The preview page includes an operator-only wrapper outside the rendered frame:
projection digest, source generation digest, schema version, projected/suppressed
row counts, and validation result. The iframe bytes are exactly the bytes that
would be published. “Publish” accepts that digest, re-runs validation, and copies
the same immutable public document/assets; it never reprojects different live
data between preview and publication.

## Threat model

| Threat | Required control |
|---|---|
| Accidental private-object pass-through | Closed typed schema; construct new primitives; reject unknown output keys; never serialize source objects |
| CSS/DOM recovery, accessibility tree, source map, clipboard | Separate HTML/bundle; serialized-DOM tests; no private strings, hidden nodes, source maps, comments, titles or clipboard code |
| Unique project/person/ticket inference | Per-install HMAC aliases, `k=5`, complementary suppression, coarse enums, no titles/names |
| Timing inference from refresh/event/lease data | 15-minute delay, deterministic jitter, age buckets, atomic fleet generations |
| Cross-install correlation | Random per-install alias key; no global salts or deterministic friendly names |
| Enumeration | Same 404 for unknown/denied aliases, authentication before lookup, rate limit, no index outside projected rows |
| Read-only bypass/CSRF | Public process registers GET/HEAD only and has no write-capable dependencies or credential; CSP and no CORS |
| Cache cross-user leak | Private/authenticated caching, `Vary: Authorization`, public-only ETags, no shared caching by default |
| Stale/partial/error disclosure | Last-known-good public snapshot only; fail closed after TTL; constant errors; generation atomicity |
| Operator preview differs from publication | Content-addressed artifact; production renderer/projector; publish the reviewed digest |

Residual risk remains: even bucketed public operational data can reveal business
cadence. Operators must explicitly select boards and accept that risk; default is
no public boards and no public listener.

## Acceptance and negative tests

Tests use synthetic source rows and product-produced projector/HTTP responses.
Expected-value fabrication is insufficient.

1. **Schema closure:** recursively assert every key/type/enum in actual public
   JSON; inject one unknown source key at every source nesting level and prove it
   never appears.
2. **Canary corpus:** seed synthetic values shaped like a credential, absolute
   path, hostname, email, personal/project name, Central/board/ticket/agent/
   principal ID, branch, commit, model, provider, URL, error, command and log.
   Assert none occurs in response bodies, headers, ETags, serialized DOM,
   attributes, accessibility text, JavaScript, CSS, source maps, clipboard calls,
   cache files or preview artifacts.
3. **Route/method matrix:** for every private endpoint in the inventory, send
   GET, HEAD, POST, PUT, PATCH, DELETE and OPTIONS to the public listener. Only
   documented public GET/HEAD routes succeed; private paths have the constant
   404 and mutation methods the constant 405. Assert no side effects.
4. **Authorization:** missing, expired, wrong-audience, wrong-scope and
   wrong-project viewer credentials fail before alias lookup with indistinguishable
   bodies. Verify the public process credential cannot call any Central write.
5. **Suppression boundaries:** product responses at cohort sizes 0, 1, 4, 5, 9,
   10, 24 and 25 verify the bucket boundaries and complementary suppression.
   Remove one row and confirm the new output cannot reveal its state by subtraction.
6. **Alias properties:** same key/entity is stable; rename does not change it;
   different entity types and installation keys differ; collision injection
   suppresses both rows; aliases have no reversible source substring.
7. **Timing:** events just before/after delay and age boundaries expose only the
   correct delayed bucket. Repeat refreshes cannot narrow occurrence time.
8. **Atomic/cache:** interrupt projection at every write step; readers see the
   previous complete digest. Validate ETag, `Vary`, stale TTL, 304 responses,
   constant 503, and that private/public caches share no object identity.
9. **Search/HTML:** drive the actual public UI. Search network requests remain
   zero and results contain only projected aliases/enums. DOM attributes match
   the allowlist. Clipboard API is absent. Settings and deep private routes are
   absent from navigation and keyboard shortcuts.
10. **Preview parity:** hash every iframe response and published response for one
    approved digest; bytes and security headers match. Mutating source data after
    preview cannot change the published artifact.
11. **Property/fuzz:** arbitrary Unicode, bidi controls, markup, oversized values,
    nested dictionaries and malformed timestamps cannot escape enums/alias grammar,
    cause partial output, or surface exception text.
12. **Leak regression:** run `python3 tools/leak_scan.py` over public artifacts in
    addition to semantic canaries. Passing the generic leak scan alone is not
    acceptance evidence.

## Migration and rollback

1. Land projector types, fixtures, and negative tests with no route and an empty
   board allowlist.
2. Land the isolated public renderer and local preview command. Verify byte parity
   and serialized-DOM leakage. Keep publication disabled.
3. Land the presentation process/listener behind authenticated HTTPS, still with
   no selected boards. Exercise route/method and credential tests.
4. Enable one synthetic board, then one explicitly approved real board whose
   projector output satisfies suppression. Monitor only public aggregate health;
   do not add private identifiers to telemetry.
5. Expand board selection only by explicit operator configuration and reviewed
   projection digest. Public mode never becomes the default dashboard mode.

Rollback is a single reversible configuration change: disable the public listener
and remove its board allowlist. Preserve the private dashboard unchanged. Revoke
viewer credentials, remove proxy routing, rotate the alias key if artifacts escaped,
and delete public cache objects by digest according to retention policy. Do not
delete private evidence needed for incident review. A failed rollout must not be
“rolled back” by serving the private dashboard at the public URL.

## Non-goals

This contract does not authorize a public deployment, a non-loopback binding for
the private dashboard, anonymous viewing, analytics, user-entered public labels,
ticket detail, actor attribution, screenshots of private mode, or any write action.
Implementation requires a separate scoped ticket and security review.
