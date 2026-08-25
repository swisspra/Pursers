# Changelog

All notable changes to Pursers are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [5.0.0a2] - 2026-08-25

### Added

- Added a stdio wait bridge with stable instance names, project-filtered backlog
  scans, lease heartbeats, and per-call `agent_name` identities so multiple host
  sessions can share one connector without mutating client identity state
  (`4759573`, `fe47967`, `341ca18`).
- Added MCP v2 journal-cue wakeups through `subscriptions/listen`, with refetching
  of authoritative events and automatic polling fallback when push is unavailable
  (`5b3c30d`).
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
- Transitioned Central authentication to signed JWT capabilities with fail-closed
  RS256/JWKS verification, issuer and audience checks, and documented the temporary
  legacy-token transition boundary (`83ad38c`, `f0f02da`).
- Made wheel and dashboard artifacts reproducible with a pinned build toolchain,
  deterministic build epoch, byte-level hashes, and a generated component lock
  (`19a44f8`, `00dc188`, `05aec22`).
- Expanded the dashboard roster with project, current-ticket, and duplicate-name
  context, then separated live agents from stale agents using a 60-minute activity
  threshold (`978348d`, `ccbc7d0`, `8776344`, `b7be2c7`, `4031367`).
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
