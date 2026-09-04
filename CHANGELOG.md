# Changelog

All notable changes to Pursers are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `a2a_wait(wait_for="auto")` now derives reviewer waits from the joined role,
  wakes reviewers only for submitted/review work, suppresses unchanged backlog
  cues after their first process-local return, and reports whether each return
  came from the journal, backlog, or timeout.
- `BoardClient.events()` now owns MCP listen scopes in a dedicated producer
  task, and generated seats close the event stream explicitly after one cue,
  preventing early-exit cancel-scope errors and false nonzero wait exits.
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
