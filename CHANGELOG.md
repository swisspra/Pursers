# Changelog

All notable changes to Pursers are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Wait bridge: add `pursers-door` to issue, inspect, rotate, and revoke
  per-board worker/reviewer door credentials with RSA-2048 keys, atomic JWKS
  replacement, and one secret-safe `prs1.…` setup string.

### Changed

- Seat-kit and wait-bridge waits now subscribe to holder-targeted ticket
  updates, label offer/holder/broadcast wakes explicitly, and permit workers
  and reviewers to claim only verified dispatch broadcasts when no live offer
  belongs to another seat.
- Wait-bridge Central traffic now reuses one bounded HTTP pool per process,
  caps concurrent Central connections at four by default, and logs before an
  excess board subscription falls back to polling. Coordinator subscription
  loss recovery now waits with jittered exponential backoff capped at 60
  seconds. Central rejects excess per-principal listen streams at a soft cap
  of 32, reports active stream counts through healthz, and attributes rejected
  coordinator joins by principal prefix, agent name, and requested role.
- Wait bridge and seat-kit setup now accept one `prs1` door: the bridge stores
  private per-board/per-role credentials, resolves missing runtime environment
  from them, assigns durable collision-safe seat-name suffixes, and provides
  redacted join/status/rotate/forget commands. Generated door seats omit
  credential and CA lines from their launcher.

## [5.0.0a23] - 2026-09-07

This release includes `pursers-central==0.1.0a27`,
`pursers-client==0.1.0a20`, `pursers-personal-import==5.0.0a3`,
`pursers-personal==5.0.0a23`, `pursers==5.0.0a23`, and
`pursers-wait-bridge==0.1.0a13`.

### Package summary

- **Central 0.1.0a27:** keys reviewer exclusion, claimed-ticket cancellation,
  and private-memory visibility to the exact seat identity, and rejects active
  seat-name collisions unless an intentional takeover is requested.
- **Client 0.1.0a20:** carries exact seat identity through the affected
  authorization and memory contracts and makes collision refusal the default
  while allowing stable-seat runtimes to opt in to takeover.
- **Wait Bridge 0.1.0a13:** uses plain HTTP for same-machine loopback Central
  connections, retains optional private-CA support for remote TLS, and opts in
  when resuming its stable seat identity.
- **Personal and meta 5.0.0a23:** ship the matching component pins, local HTTP
  defaults, documentation, dashboard assets, and regenerated component lock.
  Personal Import remains at 5.0.0a3.
- **Seat kit:** generates plain-HTTP loopback configuration without local CA
  environment lines, preserves optional remote CA configuration, and opts in
  when intentionally resuming a generated stable seat.
- **Fleet Dashboard tooling:** uses the loopback HTTP transport by default and
  keeps managed seat configuration aligned with the new local-versus-remote
  deployment boundary.
- **Coordinator:** uses loopback HTTP by default and explicitly resumes its
  stable identity without weakening collision refusal for fresh seats.
- **Worker runtime:** applies exact-seat identity rules, uses loopback HTTP by
  default, and opts in only when resuming a stable runtime seat.
- **Docs:** add the deployment transport decision: HTTP for same-machine
  loopback, port forwarding for remote seats, and public certificates only for
  a genuinely shared server.

### Changed

- Central identity checks now use the exact seat (`agent_id`) for reviewer
  self-exclusion, claimed-ticket cancellation, and private-memory visibility.
  Multiple worker or reviewer seats may share one bearer principal without
  inheriting each other's authority or private memories. Existing private
  memories that predate `author_agent_id` retain principal visibility until
  rewritten.
- `board_join` and `board_onboard` refuse a fresh active seat-name collision
  under the same principal and emit `seat_name_collision` to board admins.
  Intentional re-takeover requires `allow_takeover=true`; stale and retired
  seats remain reclaimable without it. `BoardClient` context startup defaults
  to collision refusal, while the wait bridge, worker runtime, coordinator,
  and generated seat CLI opt in when intentionally resuming a stable seat.
- Central, client, wait-bridge, coordinator, dashboard, simulation, and seat-kit
  defaults now use plain HTTP on loopback. Remote seats use port forwarding;
  optional private-CA configuration remains available only for explicit remote
  TLS deployments (TK-64e7bef5f4eb).

## [5.0.0a22] - 2026-09-06

This release includes `pursers-central==0.1.0a26`,
`pursers-client==0.1.0a19`, `pursers-personal-import==5.0.0a3`,
`pursers-personal==5.0.0a22`, `pursers==5.0.0a22`, and
`pursers-wait-bridge==0.1.0a12`.

### Package summary

- **Central 0.1.0a26:** adds attributed ticket annotations, structured
  `needs_human` requests and resolution, ticket park/unpark and claim gating,
  fleet-safe Registry Doctor and clone preflight hardening, backlog
  re-surfacing, and coordinator failure isolation.
- **Client 0.1.0a19:** carries the annotation, human-request, park, claim-gate,
  registry Doctor, and current event contracts while preserving exact Central
  claim-refusal messages for worker seats.
- **Wait Bridge 0.1.0a12:** delivers pending human requests through declared
  MCP elicitation capabilities, gates lease keepalive on model liveness, and
  preserves recoverable cursor clamping. Empty `elicitation: {}` remains
  form-only.
- **Personal and meta 5.0.0a22:** ship the human-request dashboard and guarded
  resolver, exact elicitation capability handling, credential-field safety,
  fleet-wide or dedicated seat semantics, and the regenerated component lock.
  Personal Import remains at 5.0.0a3.
- **Seat kit:** makes `--board` optional, adds `--registry-board`, persists the
  fleet selector, preserves virtual-environment interpreters, and repairs
  non-fast-forward upgrades without discarding seat work.
- **Fleet Dashboard tooling:** adds actionable Registry Doctor and clone
  preflight evidence, form-safe refresh and Resume controls, clone repair,
  ticket annotations, and schema-generated "Waiting for you" forms.
- **Coordinator:** isolates unreachable boards, retries with bounded backoff,
  reports stale findings, surfaces broadcast backlog, and routes human-request
  events without claiming work.
- **Docs:** reconciles the English and Thai manuals, architecture briefings,
  What's New timeline, release boundary, package versions, and operator
  guidance to the 5.0.0a22 train.

### Added
- Registry operations: `fleet: false` marks operator-only projects; Doctor
  reports per-check severity/scope and actionable duplicate-seat evidence, and
  `seat_admin.py dedupe` safely plans or commits duplicate-principal cleanup
  across active fleet boards (TK-f814e9bf3dde).
- Fleet dashboard: registry clone preparation now performs a non-interactive
  origin preflight, reports it in Doctor, supplies a launchd-safe git
  environment, and surfaces scrubbed subcommand/stderr diagnostics instead of
  bare exception names (TK-4c676bdba0ea).
- Central and client: attributed, bounded ticket annotations let administrators
  and coordinators attach notes, decisions, authorizations, or operator-run
  evidence without claiming or submitting; reviewer context, board digests, and
  the fleet dashboard expose the annotations (TK-1afa7f1ae8bb).
- Central and Client: workers can release a lease into the structured
  `needs_human` state with `ticket_request_human`; admins and coordinators can
  accept, decline, park, cancel, or dismiss the request with
  `ticket_human_resolve`. Pending questions appear in bounded briefings and
  remain excluded from claim and dispatch until reopened (TK-75275d51735f).
- Central/client: coordinators and board admins can park open or submitted tickets;
  parked tickets remain visible but are neither offered nor broadcast until unparked.
- Central: claim-gate refusals and ticket park/unpark transitions are journaled with
  seat attribution for operational attention views.
- Wait bridge + fleet dashboard: `needs_human` human requests are delivered to the
  host the human already sits in. New bridge tool `board_human_requests` lists pending
  requests and, when the MCP client declared elicitation (spec 2026-07-28), returns an
  `InputRequiredResult` with one `elicitation/create` per request — form mode carries
  the ticket's `requested_schema` verbatim plus a mandatory `disposition` enum
  (`reopen|park|cancel`), url mode is used when the request carries a URL, and a mode
  the client did not declare is never sent. Clients without elicitation receive the
  list plus `answer=`/dashboard fallback instructions. `human_input_requested` /
  `human_input_resolved` wake orchestrator seats, `board_digest` shows a
  `human_requests` section that `board_digest_ack` clears, and the dashboard hub gains
  a "Waiting for you" panel with schema-generated inline forms (string/number/boolean/
  enum/multi-enum, defaults, required), a disposition selector, and
  `POST /api/human/resolve` behind the same-origin loopback guard
  (TK-c5b7fef1ee2d). Required fields, primitive enum/oneOf defaults and titles,
  and array `items.enum` defaults/minimum selections are enforced by the
  renderer. The bridge treats empty `elicitation: {}` as form-only and returns
  and logs the SDK-parsed raw declaration. Its shared bridge/dashboard guard
  blocks only secret/credential fields (passwords, API keys, access tokens, and
  payment credentials); names, email addresses, usernames, ordinary prose, and
  file deliverables remain form-safe (TK-766ef9515d69).
- Dispatcher: unclaimed broadcast tickets are re-surfaced to idle identities on a
  cadence (`PURSERS_BACKLOG_RESURFACE_INTERVAL_S`, default 600 s) and re-offered after
  the board's `broadcast_reoffer_s`; coordinator identities are never offered work
  (TK-968b2d34c06c).
- Fleet dashboard: the fleet clone endpoint populates the working tree and repairs an
  empty clone instead of refusing it as dirty (TK-416e256f9c72).

### Changed
- Registry Doctor now segments bounded ticket scans by active status, reports
  exact omissions, and treats a missing fleet clone as fleet-critical only
  when recent fleet work exists; advisory WARN/INFO checks no longer make the
  overall fleet result fail (TK-f814e9bf3dde).
- Central: offer-based boards now enforce worker and reviewer offer ownership at
  claim time; eligible broadcast claims and admin/coordinator recovery bypasses remain.
- Client/seat-kit: claim-gate tool errors preserve Central's exact message so seats
  return to waiting without retrying an unoffered or parked ticket.
- Wait bridge: background lease keepalive renews only while the stdio session shows
  model liveness (`PURSERS_KEEPALIVE_IDLE_LIMIT_S`, default three claim TTLs); idle
  keepalive pauses with a `lease_keepalive_paused` cue and keepalive-only claims are
  flagged in Needs-attention (TK-b51fd3674ab7).
- seat-kit: `--python` keeps the virtual-environment interpreter instead of resolving
  the symlink to the bare CPython; `--upgrade` no longer aborts on a non-fast-forward
  seat clone (TK-35b6f1c9f373).
- Fleet dashboard / seat-kit: a seat's home board may now be blank, meaning the seat
  serves every active registry board (`boards=registry`); a named home board now
  means the seat is dedicated to that board only (`boards=home`). The bridge binds to
  the registry board (`pursers` by default, `--registry-board` in seat-kit) when the
  home board is blank. Managed host configs persist the selector as
  `PURSERS_BOARDS` / `PURSERS_HOME_BOARD` so discovery round-trips; legacy blocks
  without the marker keep their fleet-wide scope. Generated `bin/board.sh` exports
  `PURSERS_BOARDS`, and `board.py wait --boards` defaults to it.
- Fleet dashboard: dedicated seats' prompts now pass `boards=["<home board>"]` instead
  of the string `"home"`, which the wait bridge rejects.

### Fixed
- Coordinator: isolate registry-board join/read failures as `board_unreachable`
  home findings, preflight main/intake scopes into shadow mode, and retry an
  unreachable home board in-process with capped exponential backoff. The fleet
  dashboard now surfaces findings stale for more than 15 minutes
  (TK-758574bd4db1).
- Fleet dashboard: timer-driven panel refreshes (fleet, detail, overhead, config, hub,
  attention, seats) no longer wipe form input; refresh pauses while a form has focus
  or unsaved input and resumes from a fixed "Resume" pill or the status line.

## [5.0.0a21] - 2026-09-06

This release includes `pursers-central==0.1.0a25`,
`pursers-client==0.1.0a18`, `pursers-personal-import==5.0.0a3`,
`pursers-personal==5.0.0a21`, `pursers==5.0.0a21`, and
`pursers-wait-bridge==0.1.0a11`.

### Package summary

- **Central 0.1.0a25:** adds agent retirement and automatic stale-seat
  lifecycle handling; dispatches only to live seats with fair rotation,
  durable offer deadlines, and visible dispatch history; defaults joins from
  reviewer membership; and enforces the reduced tool surface.
- **Client 0.1.0a18:** bounds oversized submit notes with explicit truncation
  metadata and carries the current registry, lifecycle, and tool contracts.
- **Wait Bridge 0.1.0a11:** batches large catch-up windows with monotonic
  progress, compaction and cursor clamping, and returns deadline-bounded partial
  results without losing the next cursor.
- **Personal and meta 5.0.0a21:** keep the Personal `memory_*` API active for its
  shipped UI caller, remove `ticket_terminate`, and ship the regenerated
  component lock. Personal Import remains at 5.0.0a3.
- **Fleet Dashboard tooling:** adds Config-page seat import, Doctor for one or
  all seats, fleet-clone registry ownership, and a loopback Release & Operations
  panel for release status, immutable confirmed plans, Central staging,
  publish dispatch, kickstart, dashboard restart, and seat restart checks.
- **Seat kit:** generates fleet-owned clone workflows and refuses the operator
  checkout as a worker directory.
- **Coordinator:** reports live-seat dispatch rotation, rehydrated offer
  deadlines, and dispatch history in Needs attention.
- **Worker runtime:** follows the same live-seat, fleet-clone, reduced-tool, and
  bounded wait/submit contracts as interactive seats.

### Added

- Added the local Fleet operations runbook and the Config-page import/Doctor
  workflow for one seat or the full fleet.
- Added the Release & Operations panel with read-only tag, CI, PyPI, GitHub
  Release, Central-version, and restart telemetry plus guarded operator jobs.

### Changed

- Dispatcher offers now target live seats only, rotate fairly, preserve
  server-side offer deadlines across ASGI restarts, and expose dispatch history.
- `a2a_wait` catch-up is batched and compacted with guaranteed progress,
  deadline-honoring partial returns, and cursor clamping.
- `agent_retire` and automatic stale-seat retirement make retire-all-inert
  explicit; `board_join` derives its default role from reviewer membership.
- The Personal `memory_*` family remains supported because the Personal UI is a
  shipped caller. `ticket_assign` is retained only as an admin escape hatch.

### Removed

- `agent_nudge`; autonomous Dispatcher offers are the sole targeted wake path.
- `board_get_briefing`; use bounded `board_status` and `board_snapshot` views.
- `ticket_terminate`; use `ticket_cancel` for role-authorized cancellation.

### Migration

- Set `fleet_clone_dir` for each project in the Config page Registry card and
  run seats only from that fleet-owned clone, never the operator checkout.
- `PURSERS_LEGACY_TOOLS` no longer restores `agent_nudge`,
  `board_get_briefing`, or `ticket_terminate`; migrate callers to Dispatcher
  offers, `board_status` / `board_snapshot`, and `ticket_cancel` respectively.

## [5.0.0a20] - 2026-09-05

This release includes `pursers-central==0.1.0a24`,
`pursers-client==0.1.0a17`, `pursers-personal-import==5.0.0a3`,
`pursers-personal==5.0.0a20`, `pursers==5.0.0a20`, and
`pursers-wait-bridge==0.1.0a10`.

### Package summary

- **Central 0.1.0a24 and Client 0.1.0a17:** share one complete event-kind
  vocabulary, including claim-TTL, dispatch, deprecation, and review-lease
  events. Unknown requested kinds are dropped with one warning while known
  kinds remain subscribed, so vocabulary drift cannot disable push-wait.
- **Wait Bridge 0.1.0a10:** subscribes only to event kinds accepted by both the
  client and Central. Subscription failures are retained in bridge stats until
  a healthy push return clears them.
- **Personal and meta 5.0.0a20:** ship the updated component pins, dashboard,
  documentation, and regenerated component lock; Personal Import remains at
  5.0.0a3.
- **Fleet Dashboard tooling:** surfaces bridge subscription failures in Needs
  attention as `push unavailable: <reason>` instead of leaving them only in
  stderr.

### Migration

- The deprecated-tool compatibility path (`PURSERS_LEGACY_TOOLS` and
  `capabilities.legacy_tools`) remains available in a20. Its removal is tracked
  separately and is not part of this release.

### Fixed

- Restored subscription-first push-wait after claim-TTL cues introduced an
  event-kind mismatch that made deployed bridges fall back to polling.
- Made future event-kind skew fail open without discarding known subscriptions,
  and added cross-package plus real-Central coverage for the full bridge kind
  set.
- Exposed per-seat push failures through bounded bridge stats and Fleet Needs
  attention, with automatic clearing after a healthy push return.

## [5.0.0a19] - 2026-09-05

This release includes `pursers-central==0.1.0a23`,
`pursers-client==0.1.0a16`, `pursers-personal-import==5.0.0a3`,
`pursers-personal==5.0.0a19`, `pursers==5.0.0a19`, and
`pursers-wait-bridge==0.1.0a9`.

### Package summary

- **Central 0.1.0a23:** admits existing `admin`, `member`, and `reviewer`
  memberships at coordinator/orchestrator joins and applies the same membership
  rule to narrow `board:coordinate` and `board:intake` operations.
- **Wait Bridge 0.1.0a9:** distinguishes a connector token that is absent from
  one that resolves to a different principal, while preserving the existing
  split-identity fail-closed behavior.
- **Personal and meta 5.0.0a19:** ship the matching package pins, dashboard,
  documentation, and component lock; Client remains at 0.1.0a16 and Personal
  Import remains at 5.0.0a3.
- **Fleet Dashboard tooling:** writes the connector token-file value into managed
  Codex and Goose bridge environments, and Doctor verifies the literal, both
  Central-resolved principals, and the exact stdio launch environment.

### Migration

- The deprecated-tool compatibility path (`PURSERS_LEGACY_TOOLS` and
  `capabilities.legacy_tools`) is retained through a19; physical removal moves
  to a20.

### Fixed

- Coordinator and orchestrator joins now admit existing `admin`, `member`, and
  `reviewer` board memberships consistently. The same membership rule applies
  to narrow `board:coordinate` and `board:intake` operations; token scopes
  remain the action-authority boundary, and coordinate-only joins still cannot
  consume invites or change membership. Coordinate-only credentials may update
  ticket dispatch requirements under the existing creator/admin rule, while
  dispatch-policy changes remain restricted to admin memberships.
- Codex and Goose wait bridges no longer depend on GUI hosts forwarding the
  HTTP connector token into stdio subprocesses. Managed configs carry the
  token-file value in their private env block, startup distinguishes a missing
  connector token from a real mismatch, and Doctor probes the host launch plus
  Central-resolved principal identity without exposing token contents.

## [5.0.0a18] - 2026-09-05

This release includes `pursers-central==0.1.0a22`,
`pursers-client==0.1.0a16`, `pursers-personal-import==5.0.0a3`,
`pursers-personal==5.0.0a18`, `pursers==5.0.0a18`, and
`pursers-wait-bridge==0.1.0a8`.

### Package summary

- **Central 0.1.0a22:** adds dispatch-aware offers, explicit seat roles,
  orchestrator digests, and the audited legacy-tool visibility policy.
- **Client 0.1.0a16:** declares seat capabilities and roles, handles targeted
  offer waits, and keeps wait-bridge and connector token identity aligned.
- **Wait Bridge 0.1.0a8:** adds orchestrator mode, role-safe push waits, and
  background lease keepalive while hiding deprecated tools unless legacy
  compatibility is enabled.
- **Personal and meta 5.0.0a18:** ship the matching dashboard, setup, Doctor,
  packaging, and release-lock updates; Personal Import remains at 5.0.0a3.
- **Fleet Dashboard and seat-kit tooling:** add capability and dispatch controls,
  explicit role generation, split-identity diagnostics, and reviewer safeguards.

### Migration

- Set `PURSERS_ROLE` explicitly to `worker`, `reviewer`, or `orchestrator` for
  every seat.
- Use one token identity per seat for both its wait bridge and board connector.
- Set `PURSERS_LEGACY_TOOLS=1` only when deprecated tools are still required;
  this compatibility path remains available through a18 and is removed in a19.
- **a19 addendum:** compatibility is retained through a19; removal moves to a20.

### Added

- Background lease keepalive in the wait bridge: held work and review claims are
  renewed process-wide at about 40% of the lease TTL, independent of whether the
  seat is inside `a2a_wait`; a lost claim is surfaced once instead of failing at
  submit. Board claim TTL is now adjustable live by an admin or coordinator, and
  claims carry a successor continuation hint (previous holder and branch) so a
  new holder resumes instead of restarting.
- Fleet Dashboard seat capability controls (`tier_max`, skills, work/review gates,
  model, and provider), connector-derived skill suggestions, Central drift checks,
  and per-board dispatch policy, capability-gap, current-offer, and offer-timeline
  panels.
- Dispatch-aware waits and generated seats now declare tier, skills, work,
  review, host, model, and provider capabilities at join/reconnect. Workers and
  reviewers wake only for their own offers (plus held-ticket lease events),
  while boards without dispatch retain legacy broadcast behavior. Offer
  returns use `reason=offer` and carry expiry, tier, and required skills;
  orchestrator digests expose dispatch state, offers, and unassignable reasons.
- Hide-unless-legacy capability mechanism in Central and Wait-Bridge: seats can declare `capabilities={"legacy_tools": true}` in `board_join` or `board_onboard` to view deprecated tools. Operators can set `PURSERS_LEGACY_TOOLS=1` to force legacy tools to remain visible across all connections.
- Comprehensive tool surface audit document: `docs/tool-surface-audit.md` covering 7-day usage telemetry across all 5 caller roles, repository caller inventory, 2-view consolidation architecture (`board_status` + `board_snapshot`), and a ticket-ready a19 removal backlog.
- Orchestrator mode for the wait bridge (`PURSERS_ROLE=orchestrator`): runs a continuous background subscription across all active registry boards, buffering journal events in a bounded ring buffer (5000 events) and refetching changed tickets via side-effect-free (`touch=false`) catchup with zero board writes while idle.
- Instant non-blocking MCP tools for leaders/orchestrators (Claude Desktop, Claude Code): `board_digest` (returns tickets, new tickets, status transitions, review details, and `branch_and_commit` note on close), `board_digest_ack` (advances the acknowledged cursor), `board_watch`, and `board_unwatch`.
- Best-effort MCP resource updates (`board://<home_board_id>/digest`) emitted whenever new events arrive in the digest buffer.
- Persistent orchestrator state file (`~/.pursers/wait-bridge/orchestrator_state_<board>.json`) preserving cursors and event buffer across bridge restarts.
- Seat config and Fleet Dashboard UI support for role `orchestrator` with custom prompt renderer preventing `a2a_wait` and ticket claims.

### Deprecated

- **Tool Surface Audit & Deprecation (a18, TK-2ffa16368cbb):** 10 superseded, redundant, or unused Central tools are deprecated in a18 and hidden from `tools/list` by default, saving ~1,500 context tokens per turn across all seats:
  - `agent_nudge`: superseded by autonomous Dispatcher offers (TK-10da96af6455).
  - `board_get_briefing`: redundant with `board_snapshot` and `board_status`.
  - `ticket_terminate`: superseded by `ticket_cancel`.
  - `ticket_assign`: superseded by autonomous Dispatcher assignment; preserved for admin escape hatch.
  - `memory_checkpoint`, `memory_handoff`, `memory_links`, `memory_read`, `memory_search`, `memory_unpin`: specialized memory family with 0 model seat read calls in 7 days; moved behind capability negotiation. (Note: `memory_write` with 102 calls and `ticket_unclaim` with 138 calls remain visible core tools).
  - Deprecated tools remain callable for backward compatibility in a18. Invocations emit a `_deprecated: true` annotation and a one-time journal warning per caller (`deprecated_tool_warning`). Warning idempotency survives journal compaction in a journal-local 4,096-entry, oldest-sequence-first bounded summary. Physical removal is scheduled for a19.

### Fixed

- `board_join` and `board_onboard` now persist an explicit seat role instead of
  inferring reviewer behavior from token scopes. Review-scoped worker tokens
  therefore continue to wait for claimable work. Worker declarations require
  `board:write`, reviewer declarations require `board:review`, and coordinator
  or orchestrator declarations require `board:coordinate`. Default capabilities
  are role-safe; hybrid work/review seats are rejected.
- Codex seat configuration now uses one token identity for the wait bridge and
  HTTP board connector. The bridge fails closed and Doctor reports
  `split identity` when the two token sources differ. Worker and reviewer
  connector pairs now coexist without overwriting each other.

## [5.0.0a17] - 2026-09-04

This release includes `pursers-central==0.1.0a21`,
`pursers-client==0.1.0a15`, `pursers-personal-import==5.0.0a3`,
`pursers-personal==5.0.0a17`, `pursers==5.0.0a17`, and
`pursers-wait-bridge==0.1.0a7`.

### Added

- Atomic review leases use the board's configured work-lease TTL (900 seconds
  by default) and prevent duplicate verification across the
  fleet. Central now provides `ticket_review_claim` and optional
  `ticket_review_release`, renews review leases through `lease_renew`, exposes
  review state in ticket reads/lists, and emits push-wait cues for claim,
  expiry, and release.
- Client and generated reviewer seats support review claim/renew/release and
  unclaimed-only listing; reviewer approve/reject claims first and returns to
  wait on a structured `review_already_claimed` conflict.
- The loopback Fleet Dashboard now has a top-level Config page for seat
  inventory, diff-before-apply setup, timestamped backups, restart guidance,
  one/all Doctor jobs, wait-bridge install/upgrade, and read-only registry seat
  coverage. New `/api/config/*` writes are loopback-only, journaled locally,
  and never return JWT contents to the browser.

### Fixed

- Repository hygiene (`TK-2266327730b6`) now runs the generic leak scanner in
  CI and scrubs identifying local paths without exposing operator markers.
- Wait-bridge version and discovery repair (`TK-3b94ad0eedeb`) derives CLI and
  MCP server versions from package metadata, resolves configured and uv-tool
  shims outside `PATH`, and shows stale version strings as a Config-page WARN.
- Generated reviewer seats now include a shared HARD-verify checklist, an
  exact-SHA `board.sh verify` helper, evidence-gated approval, and mandatory
  non-empty rejection fixes. Shipped leak rules are generic; operator-specific
  regexes load from `~/.pursers/leak-markers.txt` (or
  `PURSERS_LEAK_MARKERS_FILE`) without printing their values.
- Review-lease journal kinds now come from one `pursers_client` contract used
  by Central, the wait bridge, and generated seats. Reviewer backlog cues now
  exclude submissions reserved by another live reviewer.
- The Config doctor distinguishes installed, pinned, and PyPI bridge versions,
  validates bearer-token and token-file setup, and reports private-CA and dead
  `nvm` connector hazards accurately.
- The wait bridge derives its CLI and MCP server version from installed package
  metadata, with a release-checked source-tree fallback.
- Headless worker and reviewer runtimes select claimable versus submitted waits
  from their configured runtime role, independent of broader token scopes.
- `a2a_wait(wait_for="auto")` now derives reviewer waits from the joined role,
  wakes reviewers only for submitted/review work, suppresses unchanged backlog
  cues after their first process-local return, and reports whether each return
  came from the journal, backlog, or timeout.
- `BoardClient.events()` now owns MCP listen scopes in a dedicated producer
  task, and generated seats close the event stream explicitly after one cue,
  preventing early-exit cancel-scope errors and false nonzero wait exits.

- Generated seat-kit CLI waits now subscribe to every active project-registry
  board with cursor maps and route follow-up verbs through `--board`.

## [5.0.0a16] - 2026-09-04

This release includes `pursers-central==0.1.0a20`,
`pursers-client==0.1.0a14`, `pursers-personal-import==5.0.0a3`,
`pursers-personal==5.0.0a16`, `pursers==5.0.0a16`, and
`pursers-wait-bridge==0.1.0a6`.

### Added

- Subscription-first wait design and host profiles
  (`TK-10cea5ba067a`): Codex/Codex CLI 620s/560s, Goose 300s/270s,
  Claude Desktop 240s/200s, and Claude Code/headless 21,600s/21,540s,
  with five-minute Claude Code progress notifications and immediate
  `timed_out=true` re-arm using the returned cursor.
- Central side-effect-free wake refetch and subscribed-seat liveness
  (`TK-fb29d1de526b`), plus the subscription-first wait bridge
  (`TK-75bc6cdc2405`) using `BoardClient.events()` reconnect/dedup,
  per-board degradation, host-aware ceilings, and separate model-visible
  wait-return metering.
- Generated CLI/Goose seats now provide `board.sh wait` on
  `subscriptions/listen` (`TK-d08152560570`). The default path is push;
  the current explicit poll-only compatibility flag is `--poll`.
- Local English and Thai manuals, the Thai architecture briefing, and a
  standalone `docs-local/whats-new.html` now describe the a16 candidate.

### Changed

- Idle waits no longer use a Central timer loop. After the subscription
  race-closing drain, a seat with no claims makes zero Central calls; a
  waiting seat with claims renews only those exact leases at the
  TTL-derived interval.
- Polling is an explicit fallback only:
  `PURSERS_WAIT_MODE=poll` for the wait bridge or
  `board.sh wait --poll` for a generated CLI seat. A bridge subscription
  failure degrades only that board for the current call, logs the failure,
  and retries push on the next re-arm.

### Follow-ups

- `TK-011d4336785a` corrects merged per-seat cue authorization.
- `TK-a6cd4fc8d082` reworks the headless worker and reviewer runtime around
  subscription cues and side-effect-free refetch.

## [5.0.0a15] - 2026-09-01

This release includes `pursers-personal==5.0.0a15`, `pursers==5.0.0a15`, and
`pursers-wait-bridge==0.1.0a5`.

### Fixed

- `setup` field bugs from terminal-host usage: the quit-Claude-Desktop gate now
  applies only when the target really is Desktop's config; unknown host ids no
  longer dead-end; a no-`--apply` run is a true plan that writes nothing; apply
  failures no longer leak orphan profiles, and `profiles list` / `profiles
  prune --orphaned` clean up existing ones; `--version` now reports package
  metadata.
- More central validation messages pass through the Personal facade allowlist
  (scope enum and the bounded max_bytes family).
- Wait-bridge completes the MCP initialize handshake unconditionally: board
  join is deferred, and auth/connectivity problems surface as classified
  per-call tool errors instead of a silent process exit.
- Coordinator: `board-degraded` now means real call failures only; persistent
  snapshot truncation became a daily `board-large` info finding pointing at
  journal compaction, and identical findings no longer stack.

## [5.0.0a14] - 2026-09-01

This release includes `pursers-personal==5.0.0a14`, `pursers==5.0.0a14`, and
`pursers-central==0.1.0a19`.

### Added

- Central runtime health hardening after a live healthz-500 incident
  (proximate cause: sqlite "unable to open database file" under file-descriptor
  pressure): a `runtime_health` module, full-detail healthz/tool error logging
  in machine-readable single lines, and a concurrency/disconnect stress
  regression that holds healthz at 200 under a 128-FD limit.
- Dashboard Workers tab: click-to-add API workers with OpenAI-compatible
  presets, API keys stored in the macOS Keychain (never on disk), Test button,
  and start/stop lifecycle; worker runtime gained the keychain key source and
  graceful SIGTERM during long waits.
- Worker tier filtering: `tier:light|standard|heavy` ticket tags,
  per-worker `max_tier` and assigned-first claiming, and tier-aware
  coordinator dispatch.
- Session context-pressure panel (overhead v2): per-poll estimated tokens per
  seat with trend and compact-recommendation badges.

## [5.0.0a13] - 2026-09-01

This release includes `pursers-personal==5.0.0a13`, `pursers==5.0.0a13`, and
`pursers-central==0.1.0a18`.

### Added

- Coordinator phase 3: structured intake — a deterministic classifier turns
  one-line asks (`coordinator_intake` board state) into well-formed tickets
  under the operator's approval matrix: auto-create only for docs/tests/
  read-only/reproduced-bug categories on personal-domain boards, drafts with
  next-action findings for everything else. Consume-once idempotency with
  collision hardening, client and server rate limits, opt-in via
  `--enable-intake`.
- Central: narrow `board:intake` capability — ticket creation journaled with
  intake origin plus state writes restricted to intake keys; every other
  mutation stays denied for the coordinator principal.

## [5.0.0a12] - 2026-08-31

This release includes `pursers-personal==5.0.0a12` and `pursers==5.0.0a12`.

### Added

- MCP App Link Explorer: per-project ticket-memory-file-tag links from
  `memory_links` with copy actions, served by a new bounded read-only
  projection.
- Fleet Dashboard Timeline, Changes, and Ticket Flow views with a keyboard
  filter — all read-only and bounded.

## [5.0.0a11] - 2026-08-31

This release includes `pursers-personal==5.0.0a11`, `pursers==5.0.0a11`, and
`pursers-central==0.1.0a17`.

### Added

- Coordinator phase 2: shadow-by-default dispatch writes — targeted nudges and
  atomic force-assign at the escalation thresholds, with idempotency op-keys,
  rate limits, a circuit breaker back to shadow, and full audit findings.
- Central: atomic `ticket_assign` (open/unclaimed/expected-assignee
  preconditions) and exact-recipient `coordinator_nudge` /
  `coordinator_assignment` journal kinds.
- Coordinator integration watch: `--integration-watch-since` watermark with
  visible suppression counts and an `unverifiable-commit` classification.

## [5.0.0a10] - 2026-08-31

This release includes `pursers-personal==5.0.0a10`, `pursers==5.0.0a10`,
`pursers-central==0.1.0a16`, and `pursers-wait-bridge==0.1.0a3`.

### Added

- Coordinator phase 1 (`tools/coordinator/`): read-only findings engine —
  stale/starved/abandoner detection with the approved thresholds, an
  escalation ladder that records the would-be force-assignee, integration
  watch (`integration_ref` ancestry + `no-merge-needed`), a privacy gate fed
  from a private terms file, findings in board state, and digest memories.
  Plus a dispatch simulation harness replaying real history.
- `tools/board_move/`: offline board export/import between central instances
  with principal mapping, scrub gate, and dry-run-by-default — the migration
  path for splitting trust domains. Central gained a shared instance lock.
- `tools/worker-runtime/pursers_worker.py`: headless API-driven worker for any
  OpenAI-compatible endpoint — config-file driven, work-dir-jailed tools, lease
  renewal, same seat/review governance as every other worker.
- `pursers-client` 0.1.0a13: version bump for artifact parity — the a12 wheel
  on PyPI predates the pinned-toolchain publish fix and can never match the
  component lock; a13 republishes identical source under the pinned toolchain.
- Wait-bridge per-seat overhead metering (bytes and estimated tokens per
  agent per day) surfaced on the Fleet Dashboard at `/api/overhead`, and a
  coordinator findings panel on board detail views.

### Fixed

- The coordinator's first live run surfaced a closed-but-unmerged commit from
  an earlier ticket; its content was already subsumed — the merge now records
  ancestry so the finding clears.

## [5.0.0a9] - 2026-08-31

This release includes `pursers-personal==5.0.0a9`, `pursers==5.0.0a9`,
`pursers-central==0.1.0a15`, and `pursers-wait-bridge==0.1.0a2`.

### Added

- `seat_admin.py`: one-command provisioning for worker and reviewer seats —
  duplicate-name guard against the live pool, membership plus reviewer-role
  runbook applied across all registry boards, `new-board` propagation, and a
  ready-to-paste config block that never prints credentials (`159c32c`).
- Fleet Dashboard board and agent drill-down: bounded per-board detail views
  with ticket expansion, per-agent seats and current claims, and a linked
  activity feed (`8b13673`).
- `docs/coordinator-design.md`: phase 0 design for the coordinator control
  seat (`93f9752`), and `docs/v4-port-audit.md`: ranked inventory of v4
  features worth porting (`22e05c1`).

### Changed

- Cache-friendly response layout: briefing, catchup, and journal envelopes now
  serialize stable-first with deterministic field ordering, so provider prompt
  caches see a stable prefix (`98094d9`, `docs/cache-friendly-prose.md`).

## [5.0.0a8] - 2026-08-31

This release includes `pursers-personal==5.0.0a8`, `pursers==5.0.0a8`,
`pursers-central==0.1.0a14`, and `pursers-client==0.1.0a12`.

### Changed

- Upgraded the MCP SDK across central, client, and personal from 2.0.0 to the
  current stable 2.1.1. Central converts intentional validation failures to
  `ToolError` at the tool boundary so safe client-visible validation details
  survive the 2.1.0 exception hardening; all suites pass with
  `MCPDeprecationWarning` promoted to an error (`51abb57`).

## [5.0.0a7] - 2026-08-31

This release includes `pursers-personal==5.0.0a7`, `pursers==5.0.0a7`,
`pursers-central==0.1.0a13`, and `pursers-client==0.1.0a11`.

### Fixed

- Personal no longer swallows central error detail: allowlisted central
  validation messages (e.g. the generated-ID required-field contract) pass
  through to the caller, while anything resembling auth, transport, hostname,
  or credential detail stays blanket-masked (`069b1e6`).
- `ticket_create` now documents the conditional contract: tickets created
  without an explicit `ticket_id` require `description`, `target_url`,
  `scope`, and `required_fields` (`e7357f0`).

### Changed

- `pursers-client` 0.1.0a11 exposes the bounded-response parameters:
  `board_snapshot(limit=, max_bytes=)` and
  `board_catchup(max_events=, max_bytes=)` (`101b3a9`).

## [5.0.0a6] - 2026-08-27

This release includes `pursers-personal==5.0.0a6`, `pursers==5.0.0a6`, and
`pursers-central==0.1.0a12`.

### Fixed

- Bounded the snapshot attached to `board_onboard` responses through the same
  `bounded_snapshot_payload` machinery as `board_snapshot`, with optional
  validated `snapshot_limit` / `snapshot_max_bytes` parameters and explicit
  truncation metadata. Previously a data-heavy board could push the onboard
  response past the ~1MB MCP frame cap even after the briefing fix (`2babe77`).

## [5.0.0a5] - 2026-08-26

This release includes `pursers-personal==5.0.0a5`, `pursers==5.0.0a5`, and
`pursers-central==0.1.0a11` (`36e0540`).

### Fixed

- Bounded briefing payloads: `open_tickets` became a compact projection with
  capped list length, and pinned digest / handoff entries carry per-entry
  content caps with explicit truncation flags (`7093565`).
- Paginated `board_catchup` with `max_events` / `max_bytes` and a monotonic
  cursor (`has_more` / `new_seq`), so a fresh cursor on a data-heavy board no
  longer streams the entire journal in one response (`7093565`).

## [5.0.0a4] - 2026-08-26

This fleet-era release includes `pursers-personal==5.0.0a4`,
`pursers==5.0.0a4`, and `pursers-central==0.1.0a10` (`36ef082`, `8572f18`).

### Added

- Added multi-board `a2a_wait` so one worker identity can wait across explicit
  board lists with per-board cursors, board-tagged events, isolated push/poll
  fallback, and lease renewal on the board holding the claim (`1044c3a`).
- Added the `project_registry` board-state format, parsed
  `project_registry_get`, and `boards="registry"` discovery for active project
  boards, plus a validating `registry_admin.py` CLI with verified readback for
  show, add, pause, activate, and remove operations (`07c2ff5`, `a4ba4d1`).
- Added the bounded, non-joining `fleet_snapshot` projection across active
  registry boards and a read-only Fleet dashboard tab for project ticket totals,
  pooled agent seats, current work, and unavailable-board warnings
  (`3a32643`, `160efb3`).

### Fixed

- Fixed Central `board_state_update` to honor each board's scrub profile, so an
  internal registry can retain machine-local absolute work directories while
  strict boards still reject them; released as `pursers-central==0.1.0a10`
  (`6b7a1cf`, `36ef082`).
- Corrected the Personal dependency pin from `pursers-central==0.1.0a9` to
  `pursers-central==0.1.0a10` and rebuilt Personal and the meta-package as a4
  after the incompatible a3 publication (`f18e380`, `8572f18`).

## [5.0.0a3] - 2026-08-26

### Withdrawn

- Withdrawn after publication: `pursers-personal==5.0.0a3` pinned
  `pursers-central==0.1.0a9`, while `pursers==5.0.0a3` pinned Central a10 and
  Personal a3, making that package set co-uninstallable. Use 5.0.0a4 instead
  (`36ef082`, `f18e380`, `8572f18`).

## [5.0.0a2] - 2026-08-25

### Added

- Added a stdio wait bridge with stable instance names, project-filtered backlog
  scans, lease heartbeats, and per-call `agent_name` identities so multiple host
  sessions can share one connector without mutating client identity state
  (`4759573`, `fe47967`, `341ca18`).
- Added MCP v2 journal-cue wakeups through `subscriptions/listen`, with refetching
  of authoritative events and automatic polling fallback when push is unavailable
  (`5b3c30d`).
- Added push/poll invariant coverage proving stable journal-only subscriptions,
  backlog-before-subscribe ordering, authoritative refetch after a cue, and
  byte-identical polling fallback when subscriptions fail (`cf71cfe`).
- Added `ticket_unclaim` so an authorized holder can return pre-submission work to
  the open queue with an auditable journal transition (`ea45542`).
- Added bounded board snapshots with explicit truncation counts and a journal
  splice watermark (`7212f88`).
- Added guarded `journal_compact` support that removes only derivable telemetry
  while retaining durable tickets, memories, agents, and consumer cursors
  (`4d23b62`).
- Added never-lose memory migration and retrieval: archived v4 content is
  backfilled intact, oversize content remains preserved, and callers can opt in
  with `include_archived` (`d93538c`, `4014412`).
- Added an in-repository dashboard UI source tree whose pinned build reproduces
  the packaged single-file MCP App (`e9772e0`).
- Added the dependency-only `pursers` meta-package to install Central, the Python
  client, Personal, and the Personal import utility together (`218862d`).
- Added a packaged `pursers-wait-bridge` entry point and dependency metadata so
  the bridge can be run directly with `uvx` (`ea16793`).

### Changed

- Renamed internal distributions, modules, entry points, resources, and imports
  from `onboard_*` / `onboard-` to `pursers_*` / `pursers-`, including the wait
  bridge client import (`19a44f8`, `22aa1a0`).
- Transitioned Central authentication to signed JWT capabilities only, with
  fail-closed RS256/JWKS verification plus issuer and audience checks; no
  legacy-token mode was ever shipped (`83ad38c`, `f0f02da`).
- Moved all six distributions to PyPI Trusted Publishing with GitHub OIDC and no
  API tokens, using the `pypi` and `pypi-bridge` environments (`67848a1`).
- Made wheel and dashboard artifacts reproducible with a pinned build toolchain,
  deterministic build epoch, byte-level hashes, and a generated component lock
  (`19a44f8`, `00dc188`, `05aec22`).
- Expanded the dashboard roster with project, current-ticket, and duplicate-name
  context, then separated live agents from stale agents using a 60-minute activity
  threshold (`978348d`, `ccbc7d0`, `8776344`, `b7be2c7`, `4031367`, `f3d7760`,
  `7d0f14d`).
- Made v4 import decisions policy-driven and surfaced validation failures without
  leaking unsafe source details (`b42e437`, `d64d533`).

### Fixed

- Fixed late-joining workers missing pre-existing open tickets by exposing current
  open work through catchup and scanning the open backlog before blocking; closed
  tickets are not resurfaced (`3cd13a4`, `f794d19`).
- Fixed archive backfill under strict scrubbing by adding an explicit internal
  profile while preserving mandatory secret redaction (`4014412`, `a7fec9b`).
- Fixed ambiguous dashboard agent rows by warning on duplicate active names and by
  moving stale identities behind the live roster (`978348d`, `4031367`).

## [5.0.0a1] - 2026-08-22

### Added

- Introduced the Personal Preview as a local board for one owner and multiple
  explicitly named agent clients (`58c14e3`).
- Added the initial Today, Work, Agents, and Activity dashboard as a read-only MCP
  App, while keeping ticket, review, memory, state, and handoff mutations in agent
  chat (`58c14e3`).
- Published the preview with no supported host claim (`supportedHosts=[]`; the
  embedded package policy renders this as `supported_hosts=[]`) until a packaged
  candidate passes the live host release gate (`58c14e3`).
