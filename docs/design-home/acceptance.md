# Pursers Home acceptance

This document defines the release acceptance boundary for the AionUi extension,
Team lifecycle, and dashboard parity. It is not proof that AionUi currently
implements the complete flow. The checked-in harness discovers current source
capabilities, performs only a read-only live status probe, and skips the full
sequence until the Team and door sibling contracts ship to `origin/main`.

## Safety boundary

- Run only against a dedicated AionUi profile and a disposable board whose ID
  starts with `sandbox-` or `test-`.
- The harness refuses `pursers`, `fullplatts`, and `mi-mcp-prd`, non-loopback
  hosts, URLs containing credentials, and mutation without the exact opt-in
  `PURSERS_HOME_ACCEPTANCE_MUTATE=I_UNDERSTAND_SANDBOX_ONLY`.
- Do not use an operator or production checkout, production Central, personal
  Team, real customer data, or an existing user Team. No acceptance step may
  mutate an actual user Team.
- Door strings, JWTs, bearer tokens, cookies, authorization headers, private
  paths, personal hostnames, and company identifiers must not enter reports,
  screenshots, logs, fixtures, commits, or ticket notes.
- Use a non-zero saved event cursor and subscription-based wait. Do not use
  cursor `0`, `--poll`, or a repeated list/get loop as an event wait.
- A source test, fake bridge, mocked browser, synthetic fixture, or static UI
  screenshot cannot establish GUI acceptance. Missing real-host capability or
  evidence is `SKIP` with its precise reason, never `PASS`.

## Capability gate

Run source discovery first:

```sh
python tools/aionui-extension/tests/home_acceptance/harness.py discover
```

The current extension manifest exposes `/pursers/join` and `/pursers/status`.
It does not yet expose the Team lifecycle, door rotation, seat lifecycle,
ticket lifecycle, or result-visibility contracts required for destructive
acceptance. When sibling implementations land, update discovery only from
their shipped interface contract; do not invent routes or fixtures here.

This matrix uses the dashboard sibling's published inventory from
`TK-f8a62bab8d05` at `dfa4fa535bbc9aa665f0d8856971472651659af5`, the head of
its resubmit branch `codex/TK-f8a62bab8d05-resubmit-1` (awaiting review). That
artifact enumerates the Personal, Fleet Dashboard, extension, and Personal MCP
server surfaces. Sibling contract status at this revision: the Team adapter
`TK-628602eedb90` is accepted and published at
`f1984667dd035b249fbce4d6536bf1a7d4ffb3a3` on `goose/TK-628602eedb90`
(`tools/aionui-extension/team/TEAM_ADAPTER_CONTRACT.md`,
`tools/aionui-extension/team/HOST_API_EVIDENCE.md`,
`tools/aionui-extension/team/adapter.cjs`); door onboarding `TK-4ba6bd1964de`
is published awaiting review at
`c63f8d40df9b64509583ab391cc441a16ad8f86c` on `codex/TK-4ba6bd1964de`
(`tools/aionui-extension/door/DOOR_ONBOARDING_CONTRACT.md`,
`tools/aionui-extension/door/adapter.cjs`). Neither sibling is merged into
`origin/main` yet, so source discovery still finds no `team_lifecycle`,
`door_rotation`, `seat_lifecycle`, `ticket_lifecycle`, or `result_visibility`
capability in the shipped manifest, and the corresponding mutation steps
remain `SKIP` with that explicit reason rather than inferred fixtures. This
document claims no sibling contract coverage beyond these citations; when the
siblings ship to `main`, update discovery only from their shipped interfaces.

An installed real host can be probed read-only:

```sh
python tools/aionui-extension/tests/home_acceptance/harness.py probe-status \
  --host-url http://127.0.0.1:8765
```

This calls only the existing `/pursers/status` route, disables proxy use, rejects
cross-origin redirects, and returns a bounded summary without seat names, board
IDs, key IDs, credentials, or door material.

## End-to-end sequence

Each step requires real browser observation and host-side evidence. Record the
same exact step IDs in the evidence report.

1. `fresh_install`: start from a dedicated clean AionUi profile, install the
   exact extension artifact, launch the real host, and verify the Pursers entry
   loads without pre-existing Pursers state.
2. `door_connect`: connect the disposable worker and reviewer doors through the
   shipped join UI; verify redacted board, role, seat, push mode, key ID, expiry,
   and environment-free MCP registration. Never capture the door string.
3. `team_setup`: create or select only the disposable Team through the shipped
   Team adapter, then prove idempotent reopen/reload behavior.
4. `six_workers_two_reviewers`: join six unique work seat identities and two
   unique reviewer identities. Duplicate display name and duplicate principal
   cases must be visible and must not collapse identities.
5. `ticket_offer_claim`: create a disposable ticket, observe its targeted offer,
   claim only that live offer, and prove a stale/expired offer cannot be claimed.
6. `ticket_submit_independent_review`: submit the claimed ticket and complete a
   review from a different principal. Exercise pending human approval and its
   resolved state without approving production work.
7. `result_visible`: verify the closed result, independent review label, actor,
   status transition, and current cursor appear in the Home and dashboard views.
8. `pause_resume_stop`: pause, resume, then stop the disposable Team/seats using
   the shipped lifecycle adapter; verify state and disabled-action transitions.
9. `clean_reconnect_after_rotation`: rotate only the disposable door, prove the
   old credential is rejected, reconnect with the new door, and prove no stale
   offer, duplicate identity, or orphaned MCP registration survives.

Current operator routing configuration is a deployment choice, not a model
intelligence claim:

| Seat kind | `tier_max` |
|---|---:|
| Gemini worker | `1` |
| GLM worker | `1` |
| Qwen worker | `2` |
| Codex worker | `2` |
| Reviewer | `2` |

The six workers may repeat a configured model kind, but all six seat names and
Central agent identities must be distinct. Both reviewers must be distinct from
each other and from every submitting worker.

## Dashboard inventory

Every item below must be observed with populated data and with its relevant
empty, loading, error, stale, or offline state. Duplicate identities, stale
offers, pending human approvals, and reconnect behavior are cross-cutting cases.

### Extension

- Pursers settings navigation, door join form, progress, bounded errors, and
  redacted status card.
- Worker and reviewer presets for Codex and Claude, plus environment-free MCP
  server registration and idempotent reconnect behavior.

### Personal MCP App

- Connection banner, board identity, data provenance, health, refresh, theme,
  keyboard tabs, and global loaded-data search including no-results state.
- Today: status metrics, current work, active agents, latest handoff, important
  pinned note, and recent activity.
- Work: total, status groups, ownership, priority, lease, rejection, abandonment,
  review readiness, and no-ticket state.
- Agents: total/live counts, role, platform, focus/current ticket, idle/lease,
  duplicate-name marker, duplicate identity, stale state, and no-agent state.
- Fleet: online/busy/available/stale totals, registry warning, projects, project
  ticket counts, shared pool, per-project seats, truncation, unavailable, and
  empty states.
- Links: authoritative source label, node/edge totals, ticket/file/tag/retract
  edges, pinned state, truncation, unavailable, and no-link states.
- Activity: scope disclosure, ordered bounded feed, cursor, dropped events,
  has-more/resync notice, stale/error/offline behavior, and no-activity state.
- Data sources: `board_snapshot`, `fleet_snapshot`, `link_snapshot`, and
  `board_event_feed`; confirm visibility boundaries without exposing or
  acknowledging the complete Central journal.

### Fleet Dashboard

- Per-Central availability isolation, updated state, global search/no-results,
  theme, density, keyboard help, refresh pause/resume, and offline/error states.
- Online/busy/available/stale pool metrics; board cards, ticket counts, bounded
  active rows; agent pool, current claims, duplicate names, retired/stale drawer;
  board detail, ticket metadata, activity, and truncation.
- Overview, Boards, Agents, and Operations hubs; board Tickets, Timeline,
  Changes, Flow, and Routes tabs; default-Central aliases and unknown-route
  recovery.
- Protocol overhead, coordinator configuration, worker management, intake, and
  findings, including empty/unavailable diagnostics and pending human input.
- Config: seat inventory, discovery/import conflicts, add/update preview, exact
  diff confirmation, bridge versions, Doctor, tier/skill/role capabilities,
  current offers, dispatch policy/gaps/history, and registry work trees.
- Doors: per-project worker/reviewer rows, key ID, expiry, connected seats, copy,
  rotation warning, disabled/unconfigured/error states, and secret-free output.
- Add project: registry, board, principals, policy, clone steps; idempotent rerun;
  one-time doors; partial failure and administrative authorization errors.
- Release and operations: manifest/tag/CI/PyPI/GitHub/Central status, restart
  checklist, immutable confirmation plan, job progress/result, rollback failure,
  disabled controls, and unavailable external services.

## Evidence report

The full test consumes an external JSON report produced during the real browser
session. Do not commit the report or screenshots. Required top-level fields are:

```json
{
  "schema_version": 1,
  "evidence_kind": "real_browser_host",
  "mocked": false,
  "synthetic": false,
  "target": {
    "base_url": "http://127.0.0.1:8765",
    "board_id": "sandbox-home-acceptance"
  },
  "host": {
    "product": "AionUi",
    "version": "EXACT_VERSION",
    "build": "EXACT_BUILD"
  },
  "steps": [
    {"id": "fresh_install", "status": "passed", "evidence": "BOUNDED_REFERENCE"}
  ],
  "inventory": [
    {"id": "extension.join", "status": "passed", "evidence": "BOUNDED_REFERENCE"}
  ],
  "suites": [
    {"name": "EXACT_COMMAND", "status": "passed", "commit": "EXACT_SHA"}
  ],
  "all_existing_suites_passed": true
}
```

The actual report must contain every step and every inventory ID enforced by
`harness.py`. Evidence references must be bounded, secret-free, and relative to
the isolated evidence directory. Verification also requires the exact sandbox
target and mutation opt-in:

```sh
export PURSERS_HOME_ACCEPTANCE_HOST_URL=http://127.0.0.1:8765
export PURSERS_HOME_ACCEPTANCE_BOARD=sandbox-home-acceptance
export PURSERS_HOME_ACCEPTANCE_MUTATE=I_UNDERSTAND_SANDBOX_ONLY
export PURSERS_HOME_ACCEPTANCE_EVIDENCE=/tmp/pursers-home/evidence.json
pytest -q tools/aionui-extension/tests/home_acceptance -rs
```

Release readiness requires: all harness unit checks pass; the read-only probe
passes against the installed host; every end-to-end step and dashboard inventory
item has real-browser evidence; and every existing repository suite passes at
the same exact commit. A skip is not release-ready.
