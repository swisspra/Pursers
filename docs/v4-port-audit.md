# v4 Port Audit

## Purpose and audit boundary

This document inventories the useful surface area in the v4 predecessor repo
and compares it with the current v5 architecture. It is a port audit, not a
request to restore the v4 architecture.

The sweep covered:

- `dashboard_live.py` and `run-dashboard.sh`;
- all public tools in `server.py`, plus `a2a_wait.py`, `ticket_roles.py`, and
  `thrift_compress.py`;
- `setup-project.sh`, `update.sh`, `doctor.sh`, launchers, hooks, templates,
  configs, the skill, and setup/tool/A2A documentation;
- the root offline/live suites and the workflow, dashboard, setup, and protocol
  suites under `tests/`; and
- the changelog and release notes, including features whose significance is
  easier to see from the regression that introduced them.

The comparison uses these v5 destinations:

- **Standalone dashboard**: the main, multi-board operator webapp.
- **Inline view**: the read-only, per-project MCP App.
- **Coordinator**: detection, reporting, dispatch, and intake loops. Phase 1 is
  read-only; later writes require narrow authorization and operator policy.
- **Core/CLI**: board tools, wait bridge, registry administration, setup,
  diagnostics, and test infrastructure.

Ratings mean:

- **HIGH**: clear daily value and a relatively cheap fit on existing v5 data.
- **MEDIUM**: useful, but needs policy, schema, security, or product work first.
- **LOW/skip**: obsolete, misleading, or contrary to the v5 architecture.
- **Equivalent**: already present; retain and test rather than port again.

## Executive result

Most of v4's durable workflow is already present in stronger form: tickets,
independent review, leases, journal catchup, wait/retry, registry-based pools,
memories, retractions, links, checkpoints, handoffs, bounded reads, archive
access, board state, and transactional storage.

The unmined value is primarily presentation and operations:

1. richer standalone dashboard views (timeline, changes, ticket flow, and
   agent-to-agent routes);
2. explainable health findings (orphan/stale work, why it was flagged, and the
   safe next action);
3. a visible ticket-memory-file-tag linkage explorer;
4. one fleet-wide doctor over registry boards; and
5. small UX conveniences such as command search, copy actions, density/theme
   preferences, and explicit reconnect state.

The coordinator should own detection and policy; dashboards should render the
coordinator's evidence. Neither dashboard should silently become an
orchestrator.

## Dashboard and user-facing UX inventory

| v4 feature | What it does | v5 equivalent | Port decision and destination |
| --- | --- | --- | --- |
| Live local dashboard shell | Generates one self-refreshing HTML file and can serve/open it | Standalone fleet dashboard and an inline MCP App exist | **Equivalent architecture**. Do not port raw-file serving; port selected views through bounded APIs |
| Overview KPIs | Shows memory, agent, open-ticket, token, and digest counts | Inline Today/Work/Agents/Activity and standalone fleet counts cover much of it | **MEDIUM**: add only reconciled, server-backed KPIs to the standalone dashboard |
| 24-hour memory activity | Sparkline of recent writes | Activity feed exists, but no trend chart | **HIGH**: standalone dashboard; use bounded event or aggregate data, not browser reads of storage files |
| Memory-type breakdown | Donut/bar distribution by type | Board status exposes memory counts | **MEDIUM**: inline per-project view; cheap once counts are stable |
| Timeline view | Groups memories by hour, day, or week | Activity view is recent and bounded, not a historical grouped timeline | **HIGH**: standalone dashboard with explicit lower-bound/truncation labels |
| Changes since checkpoint | Shows new memories, ticket changes, and agent status changes since the last checkpoint | No dedicated view | **HIGH**: coordinator computes a deterministic comparison; standalone dashboard renders it |
| Agent roster | Sorts active first and shows platform, role, writes, join time, and last activity | Inline Agents and standalone pool views exist with live/stale and current work | **Equivalent**, with small field-parity cleanup only |
| Platform inference fallback | Guesses a platform from agent names when metadata is missing | v5 stores explicit stable identities and host/session context | **LOW/skip**: do not guess identity metadata; show unknown and a diagnostic |
| Agent grouping and idle indicators | Groups repeated names, warns about idle/duplicate identities | Inline view flags duplicate names; fleet view groups seats and stale state | **Equivalent** |
| Agent-to-agent route graph | Derives directed handoff/review routes from ticket actors; folds sub-workers into the accountable seat | Journal and ticket provenance provide stronger source data, but no route view | **HIGH**: standalone dashboard; render provenance, never infer authorization from the graph |
| Working/all sub-worker toggle | Hides idle helper nodes in the route graph | v5 accounts at seat/principal level | **LOW/skip** unless explicit child-run provenance is later added |
| Ticket list | Sorts by priority and shows creator, assignee, claimer, related files | Inline Work and standalone ticket rows exist | **Equivalent** |
| Ticket flow view | Visual pipeline for open, in progress, review, closed, rejected, and canceled | No pipeline visualization | **HIGH**: standalone dashboard; map exactly to current statuses |
| Orphan ticket detector | Flags claimed work whose agent is offline, explains why, and suggests release/reassign | Leases, stale classification, reap, and unclaim exist; no unified explanation panel | **HIGH**: coordinator owns detection; both dashboards render evidence and safe next action |
| Ticket-memory links | Scores explicit ticket links, text references, shared files, tags, and pin status | `memory_links` exposes authoritative linkage | **HIGH**: inline view should render explicit links first; heuristic links must be labeled suggestions |
| File reference rollup | Counts memories and tickets mentioning each file, with copy-path action | Link data exists, no dedicated UI | **HIGH** as part of the inline linkage explorer |
| Tag rollup | Shows tag frequencies | Tags are stored and searchable | **MEDIUM** as part of inline search/filtering |
| Data-health panel | Surfaces orphan/linkage warnings | Personal doctor and bounded snapshot warnings cover different layers | **HIGH**: coordinator produces board findings; standalone dashboard aggregates them |
| Token inventory | Estimates hot memory, digests, archive, and per-session load | v5 bounds every response and archives oversized content byte-exact | **LOW/skip** for estimates; expose measured response sizes only if operationally needed |
| Quota-saved view | Models tokens and time saved, including a multiplier | No equivalent | **LOW/skip**: the model is promotional and assumption-heavy, not an operational control |
| Global filter | Filters the current view | Inline dashboard already searches tickets, agents, and activity | **Equivalent**; add to standalone views consistently |
| Command palette | Keyboard navigation/search across views, memories, tickets, agents, and toggles | No equivalent | **HIGH**: reusable dashboard UX, with bounded server-side search where needed |
| Copy ID/path actions | Copies durable identifiers and file paths with feedback | No consistent equivalent | **HIGH**: cheap, useful, and safe on already-visible values |
| Theme, chart, and density toggles | Persists display preferences in browser local storage | Current views have fixed presentation | **HIGH** for theme/density; **LOW** for carrying both chart engines before real demand |
| List/flow and time-window toggles | Switches ticket representation and report windows | No direct equivalent | **HIGH** for ticket list/flow; **MEDIUM** for historical windows pending reliable aggregates |
| Live/stale/offline state | Shows heartbeat, reconnecting, and offline states without discarding last data | Inline view has bounded backoff/failure data; standalone polls | **Equivalent**, but standardize wording and last-success time |
| Responsive/mobile layout | Collapses grids and navigation for narrow screens | Current inline UI is responsive; standalone is utilitarian | **MEDIUM** for the standalone product surface |
| HTML escaping and no-store reads | Escapes board text and avoids stale browser caching | Current UI decoders, caps, and inert rendering are stronger | **Equivalent**; retain hostile-input negative controls |
| Raw `.agent-mem` data discovery | Tries several relative paths and a query override | v5 data is behind authenticated Central APIs | **LOW/skip**: raw browser access violates the new boundary |

## Wait, wake, ticket, and role inventory

| v4 feature | What it does | v5 equivalent | Port decision |
| --- | --- | --- | --- |
| Check before blocking | Drains backlog before parking | `board_catchup` plus the wait bridge's open-ticket scan | **Equivalent** |
| Drain on wake | Returns every pending relevant event | Bounded journal catchup returns multiple events | **Equivalent** |
| Self-event loop guard | Suppresses events authored by the waiting agent | Wait bridge filters by exact agent/principal provenance | **Equivalent**, stronger identity basis |
| Relevant-work filtering | Supports `only_mine` and open-queue work | Wait bridge filters assignment and backlog cues | **Equivalent** |
| Verdict payloads | Carries rejection notes, fix instructions, and count deltas | Journal review events and ticket refetch provide the verdict | **Equivalent**; workers should still refetch the full ticket |
| Rejection detection by count delta | Detects the transient reject-to-open path safely | Durable review history and rejection count exist | **Equivalent** |
| Per-agent watch cursor | Prevents listeners from sharing mutable cursor state | Caller-owned, per-board journal cursors | **Equivalent**, better suited to multiple boards |
| Heartbeat while parked | Prevents a listening worker from being marked dead | Wait bridge renews the exact held lease while blocked | **Equivalent** |
| Desktop-safe timeout clamp | Keeps a wait below common host cancellation limits | Wait bridge clamps its wait and remains stdio-first | **Equivalent** |
| Long-wait mode | Lets transports without the short limit wait longer | v5 standardizes a bounded re-arm loop | **LOW/skip** until host-specific evidence justifies it |
| Idle budget and `STAND-DOWN` | Stops an unattended listener after repeated empty parks | No direct v5 result state | **MEDIUM**: optional operator policy, not the default pool-worker behavior |
| `stay_active` after submit | Keeps a worker present for review/retry | v5 submit supports `stay_active` | **Equivalent** |
| Submitter-owes-review safeguard | Avoids handing off a seat that must review another submission | Independent-principal review and coordinator backlog detection supersede the exact rule | **MEDIUM**: implement as coordinator finding, not submit side effect |
| Strict executor/reviewer separation | Executor cannot close its own work without an explicit marked escape | Board roles and strict review policy enforce independent review | **Equivalent**, stronger principal boundary |
| Assignment claim gate | Prevents another worker from taking assigned work | Assignment-aware claim rules exist | **Equivalent** |
| Transition actor stamps | Records creator, assignee, claimer, submitter, and reviewer | Journal and records carry agent and principal IDs | **Equivalent**, stronger provenance |
| Cross-process file lock | Serializes v4 JSON mutations | Transactional SQLite and locked stores | **Equivalent architecture**; do not port `flock` |
| Mtime-gated snapshot cache | Avoids reparsing unchanged JSON on every wait tick | Central snapshots and standalone fleet cache | **Equivalent architecture** |
| Auto-KIA after idle | Marks agents dead after a local inactivity threshold | Lease expiry and explicit stale/lifecycle projection | **LOW/skip**: leases are safer than inferred death |
| Human-readable ticket Markdown | Mirrors ticket records for manual inspection | Central ticket records and dashboards are authoritative | **LOW/skip**: avoid a second stale projection |

## Public tool inventory

Every v4 public tool is accounted for below.

| v4 tool | v5 status | Notes |
| --- | --- | --- |
| `memory_init` | Partial equivalent | Board/profile setup replaces per-repo JSON initialization. No direct port |
| `memory_agent_join` | Equivalent: `board_join` | v5 adds principal and stable agent IDs |
| `memory_onboard` | Equivalent: `board_onboard` | Compact bounded snapshot and optional ticket focus |
| `memory_write` | Equivalent: `memory_write` | Includes scrub policy, retractions, pinning, and archived writes |
| `memory_unpin` | Equivalent: `memory_unpin` | Keeps provenance rather than deleting content |
| `memory_read` | Equivalent: `memory_read` | Explicit archive inclusion and bounded output |
| `memory_search` | Equivalent: `memory_search` | Explicit archive inclusion |
| `memory_search_vector` | Missing | **LOW/skip**: local bag-of-words vectors are weak; revisit only with a measured retrieval gap |
| `memory_links` | Equivalent: `memory_links` | Strong basis for the inline linkage explorer |
| `memory_checkpoint` | Equivalent: `memory_checkpoint` | Journaled and attributable |
| `memory_handoff` | Equivalent: `memory_handoff` | Journaled and attributable |
| `memory_get_briefing` | Equivalent: `board_get_briefing` | Bounded current context |
| `memory_status` | Equivalent: `board_status` | Standalone fleet projection adds cross-board status |
| `memory_doctor` | Partial equivalent | Personal CLI doctor checks installation/identity; board/fleet semantic doctor is a **HIGH** gap |
| `memory_update_state` | Equivalent: `board_state_update` | Board-state values are scrubbed and namespaced by policy |
| `memory_compact` | Superseded | Bounded reads, byte-exact archive records, and journal compaction separate concerns |
| `memory_token_usage` | Superseded for correctness | Response bounds are enforced; add measured telemetry only if needed |
| `memory_prepare_compaction` | Superseded | No agent-authored destructive memory compaction should be required |
| `memory_search_archive` | Equivalent | `memory_search(include_archived=true)` |
| `memory_bootstrap` | Missing as a direct workflow | **MEDIUM**: design a deterministic project-manifest preview plus operator-approved intake, not broad silent scanning |
| `memory_context_dirs` | Missing by design | **LOW/skip**: raw external path access conflicts with the central security boundary |
| `memory_context_read` | Missing by design | **LOW/skip**: use explicit attachments/imports with provenance if this need returns |
| `memory_create_ticket` | Equivalent: `ticket_create` | v5 uses server IDs, scope, required fields, and durable provenance |
| `memory_claim_ticket` | Equivalent: `ticket_claim` | Lease-backed and transactionally arbitrated |
| `memory_submit_ticket` | Equivalent: `ticket_submit` | Supports `stay_active` and strict review |
| `memory_review_ticket` | Equivalent: `ticket_review` | Independent-principal review policy |
| `memory_cancel_ticket` | Equivalent: `ticket_cancel` | Role-authorized |
| `memory_terminate_ticket` | Equivalent: `ticket_terminate` | Destructive and role-authorized |
| `memory_list_tickets` | Equivalent: `ticket_list` | Bounded; exact ticket retrieval is separate |
| `memory_wait_for_event` | Equivalent: `a2a_wait` + `board_catchup` | v5 adds multi-board registry pools, optional push cue, leases, and resync |

V5 also has useful primitives that v4 did not: board membership/invites/roles,
scrub and review policies, ticket unclaim, lease renew/reap, journal compaction,
multi-board registry selection, bounded snapshots, stable event resources, and
one-way audited v4 import. These should remain the foundation.

## Scripts, configuration, and documentation inventory

| v4 item | What it provides | v5 status | Port decision |
| --- | --- | --- | --- |
| `setup-project.sh` | One central install, per-project config, safe/off startup hooks, rules, skill placement, dashboard launcher, `.gitignore`, and migration-preservation checks | Personal setup creates protected profiles/integration; registry maps boards to work dirs | **MEDIUM**: retain the obvious one-command experience, but generate secure profile/registry plans rather than copying runtime files |
| Linked-project registry | Lists and refreshes all projects from one central checkout | Project registry is a board-state document with validated admin CLI | **Equivalent concept**, stronger source of truth |
| `update.sh --refresh-linked` | Pulls code, backs up managed files, refreshes known projects | Versioned packages and manifests replace checkout mutation | **LOW/skip**: package upgrade tooling should own this |
| `doctor.sh` | Read-only checks for runtime, generated config, hooks, ignored data, migration proof, and source hygiene | Personal `doctor` covers profile/integration; registry CLI validates registry | **HIGH gap**: add a fleet mode that checks every active board, work dir, access, stale claims, and integration metadata without writes |
| `run-dashboard.sh` | Start/stop/open a loopback dashboard with a custom port | Standalone dashboard exists and refuses non-loopback binds | **Equivalent**; add friendly lifecycle commands if the webapp lacks them |
| Runtime launchers | Prefer the existing virtualenv and repair only when absent | Installed package entry points and managed service | **Equivalent architecture** |
| Safe/off startup hooks | Inject a tiny read-only hint; deliberately avoid noisy stop hooks | Explicit host/session profiles and `board_onboard` are authoritative | **MEDIUM**: offer host-specific setup guidance only where runtime pickup is verifiable |
| Mini briefing hook | Reads a capped local hint and tells the agent to onboard | No generic cross-host bootstrap hook | **MEDIUM**: useful convenience, but must never create identity or write state implicitly |
| Stop-hook removal | Prevents a memory write on every turn | v5 does not require turn-end writes | **Equivalent policy** |
| Shared agent-rule template and skill | Teaches onboard, ticket, memory, checkpoint, handoff, and wait discipline | Worker directive and product docs cover current loop | **Equivalent**; keep one canonical directive and generate host-specific placement only when needed |
| Client config templates | Examples for binary, local virtualenv, and multiple hosts | Personal profiles keep credentials out of shared configs | **LOW/skip** for token-bearing templates; retain placeholder-only examples |
| `AGENT_MEM_CONTEXT_DIRS` config | Adds read-only external context paths | No direct equivalent | **LOW/skip** pending an explicit provenance/security design |
| `AGENT_MEM_IDLE_KIA_MIN` | Tunes inferred agent death | Leases/stale projection | **LOW/skip** |
| Optional local vector backend | Enables local vector-style search | No direct equivalent | **LOW/skip** until benchmarked |
| Optional thrift compaction | Compresses prose behind an opt-in flag | Bounded responses and archive separation | **LOW/skip** in runtime; retain fidelity test ideas |
| `tools/measure_compaction.py` | Measures savings and contains negative controls for title/ID/number fidelity | No direct equivalent | **MEDIUM** as a reusable test pattern, not a product feature |
| Binary/package install paths | Documents package-manager, isolated-tool, and source-checkout installation | V5 ships versioned packages and hash-locked release assets | **Equivalent architecture**; keep one recommended path and clearly label development-only paths |
| Plugin manifest | Publishes server metadata, tools, prompts, and a launch template | V5 packages have separate component and host integration metadata | **MEDIUM**: generate any marketplace/catalog metadata from authoritative tool schemas; do not copy legacy launch or secret patterns |
| `listen` prompt | Teaches the cheap wait/re-arm/act loop | Wait-bridge worker directive and examples | **Equivalent** |
| `on-board` prompt | Teaches first-call onboarding | `board_onboard` plus worker instructions | **Equivalent** |
| Setup, tool, and A2A manuals | Give one obvious start path, field examples, role guidance, retry flow, and troubleshooting | Current docs are split across package, dashboard, and wait-bridge READMEs | **MEDIUM**: consolidate an operator quickstart and worker/reviewer loop without weakening secret handling |
| Planned guard/lifeline hook modes | Named but explicitly unimplemented | No equivalent | **LOW/skip**: inventory only; there is no behavior to port |

## Memory and workflow niceties

| v4 nicety | v5 status | Decision |
| --- | --- | --- |
| Compact onboarding instead of dumping full history | Bounded onboard/briefing responses | **Equivalent** |
| Priority-3 auto-pin with readable pinned summary | Pinned compact summaries exist | **Equivalent** |
| Exact recent duplicate suppression | Server-side idempotency/provenance differs by operation | **MEDIUM**: retain only where retries can otherwise duplicate a record |
| Retraction links with target demotion | Retractions and unpinning exist | **Equivalent** |
| Rejection-warning auto-demotion after resolution | Superseded: v5 keeps rejection evidence in durable ticket `review_history` and `fix_instructions`, so it normally creates no separate warning memory to demote | **No direct port**. A manually created pinned warning remains until explicit unpin or retraction |
| Ticket-scoped briefing and linked memories | Ticket focus and memory links exist | **Equivalent** |
| Brief/normal/deep/handoff-only modes | V5 favors bounded typed reads over mode-dependent prose | **LOW/skip**: explicit tools are more predictable |
| Ranked lexical search | Search exists | **Equivalent** |
| Local vector-style search | Missing | **LOW/skip** without retrieval evidence |
| Archive search | `include_archived` reads/search | **Equivalent** |
| Checkpoint every 10–15 minutes guidance | Tools exist; cadence is agent policy | **MEDIUM**: keep as guidance, never an automatic noisy timer |
| Handoff before leaving | Tool exists | **Equivalent** |
| Stable agent-name validation | Stable agent and principal identities exist; wait bridge validates instance suffixes | **Equivalent**, stronger server identity |
| Server identity and icon declaration | Current packaged App/service owns its identity metadata | **Equivalent** |

## Test-harness techniques worth keeping

| v4 technique | v5 coverage | Decision |
| --- | --- | --- |
| Pure wait diff/relevance module with injected clock, sleep, snapshot, and heartbeat | Wait bridge has focused async unit tests | **Equivalent** |
| Pure role-transition table tests | Central role/review tests cover server behavior | **Equivalent**; preserve table-driven negative cases |
| Real multi-process contention stress | SQLite/import/profile tests exercise concurrency and crash recovery | **Equivalent architecture**; keep at least one release-gate contention smoke |
| Full live create → claim → submit → reject → reclaim → resubmit → approve | Cross-project wait tests cover wake paths; workflow should stay a release smoke | **MEDIUM**: add one packaged-candidate end-to-end scenario if absent |
| Backlog, drain, self-loop, attribution, and rejection-delta negative cases | Wait bridge covers backlog, identities, multi-board isolation, push fallback | **Equivalent** |
| Idle-budget boundary tests | No idle-budget feature | Port only if the **MEDIUM** feature is accepted |
| Snapshot cache identity/invalidation tests | Fleet cache and bounded snapshot tests exist | **Equivalent** |
| Setup integration in temporary projects | Personal CLI integration/rollback tests are substantially stronger | **Equivalent** |
| Source hygiene and docs protocol tests | Artifact locks, packaging, and release-note tests exist | **Equivalent**; add doc invariants only for promises that must not drift |
| Dashboard hostile-input, mobile, and local-first assertions | Inline App has contract/lock tests and typed decoders | **Equivalent**; extend to every new view |
| Compaction fidelity negative controls | Runtime compaction is obsolete | **MEDIUM** test pattern: any future summarizer must prove its gate can fail |
| CI explicitly enumerates suites that discovery missed | Current suite layout is broader | **HIGH hygiene**: keep a manifest/test that fails when a required suite is omitted |

## Ranked gaps

### HIGH

1. **Standalone Timeline + Changes + Ticket Flow.** Add three read-only views
   backed by bounded Central data: grouped activity, deterministic changes
   since a selected watermark/checkpoint, and an exact ticket-status pipeline.
2. **Explainable fleet health findings.** Detect stale/expired claims,
   submitted-review backlog, starvation, unavailable/truncated boards, and
   closed-but-unmerged work. The coordinator owns the rules; dashboards show
   evidence, threshold, and safe next action.
3. **Per-project Link Explorer.** Render explicit ticket-memory-file-tag links
   from `memory_links`, with copy ID/path actions. Label any heuristic
   suggestions and never merge them into authoritative links.
4. **Registry-wide doctor.** One read-only command checks all active registry
   boards, access, work directories, snapshot bounds, stale claims, review
   backlog, and integration metadata, returning actionable failures without
   exposing credentials.
5. **Agent-to-agent route view.** Use journal/ticket provenance to show who
   created, executed, submitted, and reviewed work across boards. This belongs
   in the standalone dashboard, not the inline per-project view.
6. **Dashboard interaction kit.** Standardize command search, current-view
   filtering, copy feedback, theme/density preferences, keyboard navigation,
   reconnect state, and last-success time across both dashboard surfaces.
7. **Required-suite CI manifest.** Make omission of offline, integration,
   concurrency, wait, and UI contract suites fail explicitly rather than
   relying on incidental test discovery.

### MEDIUM

1. Optional wait idle budget with a distinct stand-down outcome. It must be an
   operator-selected policy and must not change continuous pool-worker default.
2. Deterministic project-bootstrap preview feeding coordinator intake. Scan a
   bounded allowlist, show exactly what would be captured, scrub it, and require
   approval before writes.
3. Host-specific startup hint/setup helpers, but only for hosts where pickup is
   verified. They must remain read-only and must not synthesize identity.
4. Consolidated operator and worker-loop documentation with one quickstart,
   strict secret handling, registry pool examples, and reject/retry flow.
5. Historical aggregates for memory types, ticket latency, and throughput.
   Define retention and lower-bound behavior before drawing charts.
6. Packaged-candidate end-to-end workflow smoke, including reject/retry and
   reviewer independence.
7. Duplicate-suppression/idempotency for retry-prone memory or intake writes,
   based on explicit request keys rather than fuzzy content matching.
8. Reusable fidelity negative-control helpers for any future summarization or
   report compaction.

### LOW/skip

1. Browser reads of local JSON files and relative path probing.
2. Quota/time-saved marketing estimates and unverified token multipliers.
3. User-driven destructive memory compaction and the v4 hot/cold file layout.
4. Local bag-of-words vector search without a retrieval benchmark.
5. Raw external-context directory reads; use explicit provenance-bearing
   imports or attachments instead.
6. Git-pull update scripts that rewrite linked projects; use versioned package
   lifecycle tooling.
7. Copying server/runtime files into every project.
8. Name-based platform inference, auto-KIA, and file-lock semantics.
9. Human-readable ticket Markdown as a second source of truth.
10. Planned but unimplemented hook modes.

## Ownership map

| Capability | Source of truth | Natural owner | Dashboard responsibility |
| --- | --- | --- | --- |
| Ticket/memory/activity detail | Central bounded APIs | Core | Inline view renders one project |
| Cross-board counts and routes | Registry + fleet snapshots + journal | Core/coordinator | Standalone dashboard renders fleet |
| Stale/orphan/starvation findings | Coordinator thresholds over leases/snapshots | Coordinator | Both surfaces display evidence |
| Closed-but-unmerged finding | Submission metadata + read-only integration checks | Coordinator | Standalone dashboard surfaces queue |
| Timeline/changes/digests | Bounded journal/history or durable aggregates | Coordinator/core | Standalone dashboard renders trends |
| Dispatch, nudge, and intake | Narrow authorized coordinator tools | Coordinator | No direct dashboard mutation |
| Role, token, registry, and policy changes | Operator/admin tools | Operator | Dashboards remain read-only |

## Proposed next three tickets

1. **Standalone dashboard: add Timeline, Changes, and Ticket Flow views** —
   implement read-only bounded views with truncation labels, keyboard/filter
   behavior, and hostile-input/mobile tests; no workflow mutations.
2. **Coordinator phase 1: emit explainable fleet-health findings** — add
   deterministic read-only detectors for stale/expired claims, review backlog,
   starvation, unavailable/truncated boards, and closed-but-unmerged work,
   including evidence and safe next-action text.
3. **Inline MCP App: add the per-project Link Explorer** — render authoritative
   ticket-memory-file-tag links from `memory_links`, add copy ID/path actions,
   label heuristic suggestions, and preserve the read-only App boundary.
