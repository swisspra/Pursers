# Pursers Home acceptance

This document defines the release acceptance boundary for the AionUi extension,
Team lifecycle, and dashboard parity. It is not proof that AionUi currently
implements the complete flow. The checked-in harness discovers current source
capabilities, performs only a read-only live status probe, and skips the full
sequence until every required Team, seat, ticket, and result interface ships.

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

The current authenticated helper exposes `/pursers/join`, `/pursers/status`,
and the five `/pursers/onboarding/*` validate, connect, status, rotate, and
recover routes. Source discovery reads these shipped helper routes because
AionCore 0.2.1 treats `contributes.webui` as a static asset contribution and
does not execute extension JavaScript route handlers. Discovery therefore
recognizes `door_rotation`. It does not yet expose
the Team lifecycle, seat lifecycle, ticket lifecycle, or result-visibility
contracts required for destructive acceptance. When sibling implementations
land, update discovery only from their shipped interface contract; do not
invent routes or fixtures here.

This matrix uses the independently approved dashboard inventory from
`TK-f8a62bab8d05` at
`codex/TK-f8a62bab8d05-resubmit-8@0b0ef231793440b14402b76ab03f041dda70bd01`.
Its exact surface and state IDs, plus each independently observable item in its
Personal, Fleet Dashboard, extension, and Personal MCP descriptions, are
mandatory in `REQUIRED_INVENTORY`; no coarse group can satisfy several items.
Sibling contract provenance: the Team adapter `TK-628602eedb90` is approved at
`f1984667dd035b249fbce4d6536bf1a7d4ffb3a3` on `goose/TK-628602eedb90`
(`tools/aionui-extension/team/TEAM_ADAPTER_CONTRACT.md`,
`tools/aionui-extension/team/HOST_API_EVIDENCE.md`,
`tools/aionui-extension/team/adapter.cjs`); door onboarding `TK-4ba6bd1964de` is
approved at `ff749da633815f0b539b39155ae4d712a4bcdb44` on
`codex/TK-4ba6bd1964de`
(`tools/aionui-extension/door/DOOR_ONBOARDING_CONTRACT.md`,
`tools/aionui-extension/door/adapter.cjs`). Current integration main
`9e3b05072e4521c10d0f4390d7ac57ad7421c07a` ships the seven door routes, but
source discovery still finds no `team_lifecycle`, `seat_lifecycle`,
`ticket_lifecycle`, or `result_visibility` capability. The corresponding
mutation steps remain `SKIP` with that explicit reason rather than inferred
fixtures. This document claims no sibling contract coverage beyond these
citations.

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
session. The report is untrusted input: its labels, receipts, screenshots,
snapshots, assertions, and host endpoint cannot establish a GUI PASS by
themselves. Do not commit the report or screenshots. Required top-level fields
are:

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
    "version": "2.2.1",
    "build": "2026.09.08.1",
    "evidence": "host.json"
  },
  "steps": [
    {"id": "fresh_install", "status": "passed", "evidence": "observations/fresh-install.json"}
  ],
  "inventory": [
    {"id": "extension.join-form", "status": "passed", "evidence": "observations/extension-join-form.json"}
  ],
  "suites": [
    {
      "name": "repository-python",
      "command": "python3 tools/ci_manifest.py run",
      "status": "passed",
      "commit": "FULL_40_HEX_CANDIDATE_SHA",
      "evidence": "suites/repository-python.json"
    }
  ],
  "all_existing_suites_passed": true
}
```

The actual report must contain exactly every step and inventory ID enforced by
`harness.py`. The host, each observation, and each suite must use a distinct
structured JSON receipt; a generic file reused across claims is invalid. Every
receipt repeats the exact sandbox target and candidate commit. Validation binds
host identity through the verifier-owned observer. It first actively contacts
the explicit loopback `/pursers/status` endpoint; that response must expose
exact `host.product`, `host.version`, and `host.build` identity matching the
report. When that capability is unavailable, on macOS the observer can instead
prove that the live target listener is bundled AionCore inside the official
signed and notarized AionUi application, with matching process and bundle
versions, exact identity mode, signer team, and code-signing CDHash. When
neither source verifies, the real acceptance test reports SKIP rather than
PASS. A self-authored About, helper response, receipt, or successful loopback
HTTP response alone cannot establish host identity.

Browser receipts bind their exact step or inventory ID to a same-origin page
URL and timestamp. Each observation needs unique screenshot and accessibility
artifacts: a structurally valid PNG at least 320x180 and a substantive snapshot
whose wrapper repeats the observation ID, page URL, and capture time. Assertions
use `name`, `path`, `operator`, and `expected`; the validator resolves the path
against the snapshot and evaluates `equals` or `contains`. Self-declared
`passed=true` assertions, generic snapshots, shared minimal images, and reused
artifact digests are rejected. Screenshot and snapshot descriptors contain
relative `path` and lowercase `sha256` fields.

After those untrusted artifacts pass structural checks, the verifier must bind
every observation to one verifier-owned trusted browser session. That observer
must independently return the exact observation ID, sandbox target, host
identity, candidate commit, capture time, page URL, screenshot bytes, and live
accessibility snapshot. The harness compares the capture byte-for-byte with the
report artifacts and evaluates every assertion again against the observer
snapshot. A complete caller-generated bundle and a loopback status server with
self-selected identity strings therefore cannot produce `real_browser_host`.
The public verifier accepts a verifier-owned replay executable through
`--browser-observer` or `PURSERS_HOME_BROWSER_OBSERVER`. It sends each bounded
observation request as JSON on stdin and accepts one JSON capture on stdout.
The executable must be an absolute, executable, non-group/world-writable path
outside both the candidate checkout and evidence directory. Its response binds
one observer session ID plus the exact target, host identity, candidate SHA,
capture time, page URL, screenshot bytes (`screenshot_base64`), accessibility
snapshot, and assertions. The verifier runs it with no shell and a minimal
environment. An unavailable observer is capability `SKIP`; a configured
observer failure or mismatch is `FAIL`. Unit fixture observers remain internal
and are never release evidence.

The concrete observer does not trust the requested board or candidate SHA. Its
authenticated browser reads the deterministic `candidate.json` served beside
the installed Home entry point, additionally reads the same-origin Pursers
status contract when it exposes `extension.candidate_commit`, and reads the
current selected board from the rendered UI, all inside a verifier-created CDP
isolated world. The package builder generates that manifest from exact
`git HEAD`. Two observed candidate SHAs must agree, and the observer compares
the candidate and board values with the request before storing any capture, so
an arbitrary 40-hex request or sandbox-looking board cannot relabel another
installed runtime.

Suite receipts bind the exact suite name, command, commit, target, timestamps,
zero exit code, and hashed output log. The log must contain suite-specific
success markers, but markers are not execution proof: after artifact validation
the harness independently runs every allow-listed `REQUIRED_SUITES` command at
the verification checkout. Candidate diff verification is also rerun locally. References
must resolve to regular files inside the report directory. The validator rejects
missing, escaping, malformed, hash-mismatched, oversized, secret-bearing, and
private-path artifacts, and bounds both individual and aggregate bytes. The
candidate must resolve to a commit and equal `HEAD` of the verification
checkout. Suite names and commands must exactly match `REQUIRED_SUITES`, and
every suite row must carry that full lowercase 40-hex candidate SHA.

Receipt shapes are exact. A browser receipt has
`schema_version`, `evidence_kind=browser_observation`, `observation_id`,
`target`, `host`, `candidate_commit`, `captured_at`, `page_url`, `screenshot`,
`accessibility_snapshot`, and nonempty machine-evaluated `assertions`. The
snapshot JSON has `schema_version`, `observation_id`, `page_url`, `captured_at`,
and `snapshot`. A suite receipt has
`schema_version`, `evidence_kind=suite_run`, `name`, `command`, `commit`,
`target`, `started_at`, `finished_at`, `exit_code`, and `output`. Verification
requires the exact sandbox target, candidate SHA, and mutation opt-in:

```sh
export PURSERS_HOME_ACCEPTANCE_HOST_URL=http://127.0.0.1:8765
export PURSERS_HOME_ACCEPTANCE_BOARD=sandbox-home-acceptance
export PURSERS_HOME_ACCEPTANCE_MUTATE=I_UNDERSTAND_SANDBOX_ONLY
export PURSERS_HOME_ACCEPTANCE_EVIDENCE=/tmp/pursers-home/evidence.json
export PURSERS_HOME_ACCEPTANCE_COMMIT=FULL_40_HEX_CANDIDATE_SHA
export PURSERS_HOME_ACCEPTANCE_TOKEN_FILE=/VERIFIER/OWNED/pursers-home-token
export PURSERS_HOME_ACCEPTANCE_ORIGIN=http://127.0.0.1:AIONCORE_PORT
export PURSERS_HOME_BROWSER_OBSERVER=/VERIFIER/OWNED/browser-observer
pytest -q tools/aionui-extension/tests/home_acceptance -rs
```

`PURSERS_HOME_ACCEPTANCE_HOST_URL` is the loopback helper, not AionCore. Under the
static-only manifest the host serves extension assets but does not execute route
handlers, so `pursers/status` is answered by `host/helper.cjs`, which fails closed
unless the request carries both an allowed `Origin` and `x-pursers-home-token`.
`PURSERS_HOME_ACCEPTANCE_TOKEN_FILE` is the mode-0600 file passed to the helper as
`--token-file`, and `PURSERS_HOME_ACCEPTANCE_ORIGIN` is the exact AionCore origin
passed to it as `--origin`. The status probe reads the token from that file and
never logs it; a file readable by group or other is refused.

Both are optional and are only read together. With either unset the probe stays on
the historical unauthenticated request, so an unconfigured environment reports the
capability as unavailable and skips. A wrong token still yields the same skip. Neither
path can turn a missing capability into a pass.

`REQUIRED_SUITES` covers the repository-wide Python manifest runner, all three
extension Node suites, dashboard typecheck/build, repository leak scan, and
candidate diff check. Release readiness requires: all harness unit checks pass;
the read-only probe passes against the installed host; every end-to-end step and
dashboard inventory item is bound to the verifier-owned browser observer; and
this complete suite set passes at the same exact commit. A report-only result or
skip is not release-ready.
