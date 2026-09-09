# Real AionUi Home acceptance: verifier-owned observer and runner

`tests/home_acceptance/harness.py` validates an evidence report, but a report
alone cannot establish that a real browser ever rendered the real host: the
report is written by the party that wants it to pass. The harness therefore
replays every observation through a **verifier-owned observer** executable and
compares the replay against the report artifacts byte for byte
(`_validate_trusted_browser_observations`). Without that observer the harness
raises `AcceptanceCapabilityUnavailable` — a missing observer is a non-pass, not
a skip.

This document describes the concrete observer and the reproducible runner that
drive it.

- observer: `tools/aionui-extension/tests/home_acceptance/browser_observer.py`
- runner: `tools/aionui-extension/tests/home_acceptance/runner.py`
- regressions: `tools/aionui-extension/tests/home_acceptance/test_browser_observer.py`

## Trust boundary

| Value | Authored by |
|---|---|
| screenshot bytes, accessibility tree, observed page URL, selected board, `captured_at`, `observer_id` | observer only, from the authenticated real browser/UI channel |
| running extension candidate SHA | observer only, fetched by that browser from the installed `candidate.json` and from the same-origin host status route, both inside a verifier-created CDP isolated world; when both answer they must agree |
| host product / version / build | observer only, bound from the same-origin host status route inside that isolated world, or from the live listener to the signed and notarized AionUi bundle when that contract is unavailable |
| observation id, expected origin / sandbox board / candidate commit, assertions | caller (runner request), accepted only when they equal the independently observed values |

The observer answers replay requests only from captures it recorded itself, so
caller-authored metadata, hand-made screenshots, fixtures, or a successful local
HTTP response cannot forge a pass. The harness additionally requires the
observer executable to be absolute, executable, outside both the checkout and the
evidence directory, and not group/world writable.

Because the harness spawns the observer with a stripped environment and a fixed
working directory, the observer reads its configuration from `observer.json`
beside itself, never from environment variables.

## Prerequisites

- an isolated authenticated AionUi host on loopback, running from the official
  signed and notarized macOS AionUi bundle, and/or serving same-origin
  `GET /pursers/status` to the browser with JSON `schema_version: 1`,
  `host.product`, `host.version`, `host.build`, and optional
  `extension.candidate_commit`
- the exact candidate ZIP installed so its deterministic
  `webui/candidate.json` is served beside the Home entry point
- connected Home UI exposing its current board through
  `[data-helper-field="board"]`; the observer refuses empty/non-sandbox values
- CDP `Page.createIsolatedWorld` support; host-status and candidate fetches plus
  selected-board DOM reads run in that verifier world, so page-owned overrides
  of `fetch` or `Document.prototype.querySelector` cannot forge those bindings
- the same target serving canonical AionCore `GET /health` with exact runtime
  version and build time
- a sandbox board id prefixed `sandbox-` or `test-` (production boards are refused)
- ego lite / the `ego-browser` CLI for the default capture backend
- `PURSERS_HOME_ACCEPTANCE_MUTATE=I_UNDERSTAND_SANDBOX_ONLY` for validation

## Reproducible commands

```sh
# 1. install the observer into a verifier-owned directory outside the checkout
python3 tools/aionui-extension/tests/home_acceptance/runner.py install-observer \
  --dir /PATH/TO/verifier-observer \
  --ego-browser /PATH/TO/ego-browser \
  --task-space <authenticated-isolated-task-space-id>

# 2. report observed capability before claiming anything
python3 tools/aionui-extension/tests/home_acceptance/runner.py doctor \
  --observer /PATH/TO/verifier-observer \
  --target http://127.0.0.1:25808 \
  --probe-browser http://127.0.0.1:25808/

# 3. record one real browser observation into the evidence directory
python3 tools/aionui-extension/tests/home_acceptance/runner.py capture \
  --observer /PATH/TO/verifier-observer \
  --evidence /PATH/TO/evidence \
  --observation door_connect \
  --target http://127.0.0.1:25808 \
  --board sandbox-home \
  --commit <full-40-hex-candidate-sha> \
  --page http://127.0.0.1:25808/ \
  --assertions /PATH/TO/assertions.json

# 4. validate the assembled report through the same installed observer
PURSERS_HOME_ACCEPTANCE_MUTATE=I_UNDERSTAND_SANDBOX_ONLY \
python3 tools/aionui-extension/tests/home_acceptance/runner.py validate \
  --observer /PATH/TO/verifier-observer \
  --report /PATH/TO/evidence/report.json \
  --target http://127.0.0.1:25808 \
  --board sandbox-home \
  --commit <full-40-hex-candidate-sha>
```

`harness.py verify-evidence --browser-observer /PATH/TO/verifier-observer/browser_observer.py`
remains the equivalent single-source-of-truth entrypoint; `runner.py validate`
is a thin wrapper that adds a structured `passed` / `failed` / `blocked` outcome.

## Verifier-owned directory layout

```
/PATH/TO/verifier-observer/
  browser_observer.py     mode 0755, the executable the harness spawns
  observer.json           mode 0600, observer_id, store_dir, max_age_s, backend
  captures/<id>.json      mode 0600, one recorded capture per observation id
```

`observer_id` is minted at install time and reused; `--rotate-session` mints a
new one. `--task-space` pins capture to the already authenticated isolated
ego-browser task space instead of creating an unrelated browser context. All
observations in one report must share a single `observer_id`.

## Evidence directory layout

```
/PATH/TO/evidence/
  report.json                     the evidence report the harness validates
  host-identity.json              host_identity receipt, source host-api or signed listener
  specs/<id>.json                 caller request that was sent to the observer
  observations/<id>.json          browser_observation receipt
  artifacts/<id>.png              observed screenshot bytes
  artifacts/<id>.ax.json          observed accessibility snapshot
```

## Outcomes and exit codes

| Situation | Exit | Meaning |
|---|---|---|
| replay matches a stored capture | 0 | pass |
| observation id unknown to the observer | 3 | non-pass, nothing was ever observed |
| target, host, commit, `captured_at` or page URL disagree | 4 | non-pass, report is not bound to the capture |
| capture older than `max_age_s` | 5 | non-pass, stale evidence |
| observer home or config not verifier-private | 6 | non-pass, configuration refused |
| authenticated host status not the status contract and no signed-listener fallback, or installed candidate or selected-board binding missing | 7 | blocked, host, candidate, or board identity unavailable |
| browser channel failed or returned no substantive capture | 8 | blocked, no observation recorded |

Exit 7 and 8 are reported as `blocked`, never as pass and never as skip.

## Read-only probe against the combined candidate, 2026-09-09

Checkpoint commit `14354f0b6e8edb9c116bc3673c16d2db5b865e68`, isolated AionCore
started with `--app-version 2.2.1` and an isolated data directory, packaged
loopback helper bound to board `sandbox-home-acceptance`.

- the authenticated asset route returned the installed
  `webui/candidate.json`, and its `candidate_commit` equals the checkpoint SHA,
  so the running host is serving this candidate and not an earlier one
- `test_real_host_read_only_capability_probe`: `1 passed`
- three negative controls, all against the same live host: unset environment
  skips with the original capability-unavailable message, a wrong token skips
  identically, and a token file readable by group or other fails
- `test_real_browser_host_acceptance_evidence`: still skipped, no
  verifier-owned observer. This remains **blocked**, never a pass

## Observed runtime state, 2026-09-08 (earlier candidate)

The bullets in this section were recorded on 2026-09-08 against an earlier
candidate, before the five approved Home inputs were combined. They are retained
as prior evidence and are **not** evidence for the combined candidate. For the
combined candidate, only the read-only probe recorded in the section above has
been executed; it has still not been observed in an authenticated isolated
browser, and no PASS is claimed for that channel.

Against the installed authenticated AionUi host on `http://127.0.0.1:57210`:

- `browser_channel`: observed a substantive PNG and accessibility tree on the
  target origin
- `host_identity`: bound from the same-origin `GET /pursers/status` contract when
  it answers with the Pursers status payload, otherwise from the live loopback
  listener resolving to bundled AionCore inside the signed and notarized
  `com.aionui.app` bundle; the receipt records which source was used, and the
  observer records no capture when neither verifies
- `candidate_identity`: requires reinstalling the final cumulative ZIP so its
  generated `candidate.json` binds the browser observation to exact `git HEAD`;
  when the status contract also exposes `extension.candidate_commit` the two
  must match
- `runtime_health`: independently reports the AionCore version/build when the
  canonical `/health` route is available; it does not substitute for missing
  AionUi product identity
- observer transport is ready after that exact candidate installation

The real browser channel is proven by the observer run recorded with the
integrated candidate. Full acceptance reports the missing sibling
lifecycle/result capabilities instead of treating fallback UI or mock routes as
host execution, and the supported host transport is tracked by TK-23f86d56ff99.
Final integrated real-host acceptance remains a separate release gate.

The focused trust-boundary suite also proves that a requested arbitrary
40-hex SHA or syntactically valid sandbox board cannot relabel a different
running extension/UI state: either mismatch exits non-zero before the private
capture store is written.
