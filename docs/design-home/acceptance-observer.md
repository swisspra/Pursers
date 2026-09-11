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
| surface product / version / build | observer only: signed AionUi listener for AionUi; verifier-pinned listener process plus exact clean-checkout artifact for Fleet; signed AionUi page bytes plus a live exact-source Personal MCP stdio process and process-authored private receipt for Personal |
| observation id, expected origin / sandbox board / candidate commit | caller (runner request), accepted only when they equal the independently observed values |
| Personal live transport attestation | the Personal MCP process, signed with the verifier-held challenge key over a verifier-chosen nonce; carried by the capture and recomputed by the observer, never trusted as snapshot text |
| assertions | canonical `acceptance-facts.json` declarations validated by `check_artifacts.py`; runner rejects generic or relabelled predicates before capture |

Accessibility predicates inspect only non-ignored AX nodes. Hidden panels cannot
satisfy a required visible-state fact.

The observer answers replay requests only from captures it recorded itself, so
caller-authored metadata, hand-made screenshots, fixtures, or a successful local
HTTP response cannot forge a pass. The harness additionally requires the
observer executable to be absolute, executable, outside both the checkout and the
evidence directory, and not group/world writable.

Because the harness spawns the observer with a stripped environment and a fixed
working directory, the observer reads its configuration from `observer.json`
beside itself, never from environment variables.

## Prerequisites

- isolated authenticated AionUi and Fleet origins on loopback; AionUi runs from the official
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
- the AionUi target serving canonical AionCore `GET /health` with exact runtime
  version and build time
- a sandbox board id prefixed `sandbox-` or `test-` (production boards are refused)
- ego lite / the `ego-browser` CLI for the default capture backend
- a verifier-generated Personal challenge key outside the checkout, not a symlink,
  mode `0600`, at least 32 bytes, and a Personal MCP launched with
  `--acceptance-challenge-key`
- `PURSERS_HOME_ACCEPTANCE_MUTATE=I_UNDERSTAND_SANDBOX_ONLY` for validation

## Reproducible commands

```sh
# 1. install verifier-owned per-surface bindings outside the checkout
python3 tools/aionui-extension/tests/home_acceptance/runner.py install-observer \
  --dir /PATH/TO/verifier-observer \
  --surface-manifest /PATH/TO/verifier-surface-manifest.json \
  --ego-browser /PATH/TO/ego-browser \
  --task-space <authenticated-isolated-task-space-id>

python3 tools/aionui-extension/tests/home_acceptance/typed_evidence.py install \
  --dir /PATH/TO/verifier-typed

# 2. report observed capability before claiming anything
python3 tools/aionui-extension/tests/home_acceptance/runner.py doctor \
  --observer /PATH/TO/verifier-observer \
  --target http://127.0.0.1:25808 \
  --probe-browser http://127.0.0.1:25808/

# 3. expand 198 core observations plus three additional final gates
python3 tools/aionui-extension/tests/home_acceptance/runner.py prepare \
  --observer /PATH/TO/verifier-observer \
  --evidence /PATH/TO/evidence \
  --manifest /PATH/TO/verifier-observations.json

# 4. run every command array in capture-plan.json, record suite receipts, then assemble
python3 tools/aionui-extension/tests/home_acceptance/runner.py assemble \
  --observer /PATH/TO/verifier-observer \
  --evidence /PATH/TO/evidence \
  --suite-manifest /PATH/TO/verifier-suites.json \
  --report /PATH/TO/evidence/report.json

# 5. validate the assembled report through the same installed observer
PURSERS_HOME_ACCEPTANCE_MUTATE=I_UNDERSTAND_SANDBOX_ONLY \
python3 tools/aionui-extension/tests/home_acceptance/runner.py validate \
  --observer /PATH/TO/verifier-observer \
  --typed-evaluator /PATH/TO/verifier-typed/typed_evidence.py \
  --typed-trust /PATH/TO/verifier-typed/trust.json \
  --report /PATH/TO/evidence/report.json \
  --target http://127.0.0.1:25808 \
  --board sandbox-home \
  --commit <full-40-hex-candidate-sha>
```

The observation manifest has exact top-level fields `schema_version`,
`operator_topology`, and `observations`. `operator_topology` must declare two
`vertex_ai/gemini-3.8-flash` Goose workers at tier 1, three `sol-high-fast`
Codex workers and three `sol-high-fast` Codex reviewers at tier 2, plus an
explicit optional Opus row whose count is zero unless enabled. `prepare` copies
this into `capture-plan.json`; `assemble` preserves it in the report. The 198
core observations remain mandatory. The runner additionally requires
`final.quickstart-candidate-flow`, `final.fleet-503-recovery`, and
`final.o1-readiness-rollback`, producing 201 concrete captures.

An observation whose canonical predicate contains nonvisual `all_of`
conjuncts also carries one `typed_evidence` row per conjunct. Each row binds a
bounded evidence path plus exact `run_id`, `action_id`, `entity`, and
`causal_index`; the surface, sandbox board, candidate SHA, and observation ID
come from the canonical report graph. Browser capture is still mandatory.
Visual conjuncts remain exact accessibility assertions, while a typed-only
fact must carry an explicit `browser context:` assertion so its screenshot and
accessibility state remain substantive without pretending that visible copy
proves the backend fact.

The harness never accepts a report-authored typed result. A verifier-owned
evaluator must authenticate the referenced record and return a passing result
bound to the full correlation above and the exact canonical-conjunct digest.
An absent evaluator is a blocked capability, not a pass or skip. The parent
integration checkpoint deliberately keeps that gate closed until the separate
typed collector has an independently reviewed immutable SHA.

The evaluator executable and its trust file must both be absolute regular
files outside the candidate checkout and evidence directory. The executable
must not be group/world writable; the trust file must be mode `0600`. The
harness sends one closed request on stdin and accepts only the exact correlated
result schema, so neither the report nor a candidate-local executable can
select the trust root.

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

The surface manifest has exactly `aionui`, `fleet`, and `personal`. The installer
derives the candidate from clean `git HEAD`, hashes tracked Fleet and Personal
artifacts itself, and persists those values in its private config. A signed-origin
shortcut is intentionally rejected for Fleet: the real Fleet UI is a distinct
origin, so serving or labelling it as AionUi would erase actual-product provenance.

```json
{"schema_version":1,"candidate_commit":"FULL_SHA","surfaces":{
  "aionui":{"adapter":"signed-aionui","target":{"base_url":"http://127.0.0.1:18822","board_id":"sandbox-home"}},
  "fleet":{"adapter":"pinned-process-artifact","target":{"base_url":"http://127.0.0.1:18821","board_id":"sandbox-home"},"artifact":"tools/fleet-dashboard/fleet_dashboard.py"},
  "personal":{"adapter":"pinned-signed-aionui-personal-mcp","target":{"base_url":"http://127.0.0.1:18822","board_id":"sandbox-home"},"artifact":"packages/personal/src/pursers_personal/resources/dashboard.html","runtime":{"artifact":"packages/personal/src/pursers_personal/apps_server.py","challenge_key":"/PATH/TO/verifier-runtime/personal-acceptance-challenge.key","pid_file":"/PATH/TO/verifier-runtime/personal.pid","receipt":"/PATH/TO/verifier-runtime/personal-runtime.json"}}
}}
```

For Personal, the isolated browser hashes the fetched page bytes and the observer
also requires one live `pursers_personal.cli mcp` process whose exact command,
candidate source, sandbox board, PID and private process-authored runtime receipt
match the clean verifier checkout. A stale or unrelated MCP process is rejected.
For Fleet, the observer requires one
listener and proves its process command executes the pinned dashboard artifact.

### Personal live transport challenge

The bindings above prove that a matching process exists. They do not prove that
the process answering the browser is that process, so every Personal capture also
carries a verifier-issued challenge.

**The verifier owns the key.** Generate it yourself. Never accept a key authored
by a worker, and never place one inside the candidate checkout. The observer
refuses a key that is a symlink, that carries any group or other permission bit,
or that is shorter than 32 bytes. Keep it beside the pid file and the runtime
receipt, outside the checkout:

```sh
umask 077
head -c 48 /dev/urandom > /PATH/TO/verifier-runtime/personal-acceptance-challenge.key
chmod 600 /PATH/TO/verifier-runtime/personal-acceptance-challenge.key
```

**Launch flag and manifest field.** The Personal MCP must now be started with
`--acceptance-challenge-key` in addition to `--candidate-source`,
`--candidate-commit`, `--board-id` and `--acceptance-runtime-receipt`, and the
same path must appear as `runtime.challenge_key` in the surface manifest, whose
Personal runtime field set is exactly `artifact`, `artifact_sha256`,
`challenge_key`, `pid_file` and `receipt`. The new flag belongs to the
all-or-none acceptance group, so an acceptance launch without a verifier key
raises instead of degrading, and the older command line is refused with
`lacks --acceptance-challenge-key`. Registration is conditional: a server started
without a challenge does not expose `acceptance_runtime_attest` at all, so a
non-acceptance server cannot be mistaken for one.

**Nonce order matters.** `capture` drives the browser itself, so the answer has
to be on the page before capture runs:

1. choose a fresh nonce per observation, 32 to 128 lowercase hex characters;
2. in the authenticated AionUi conversation, have the model call
   `acceptance_runtime_attest` with that nonce, so the answer travels the same
   stdio transport and lands in the conversation;
3. run capture with the same value:

```sh
python3 tools/aionui-extension/tests/home_acceptance/runner.py capture \
  --observer /PATH/TO/verifier-observer \
  --evidence /PATH/TO/evidence \
  --observation <observation-id> \
  --surface personal \
  --target http://127.0.0.1:18822 \
  --board sandbox-home \
  --commit <full-40-hex-candidate-sha> \
  --page http://127.0.0.1:18822/PATH/TO/home-entry \
  --assertions /PATH/TO/assertions.json \
  --attestation-nonce <the-same-nonce>
```

The generated default nonce only suits surfaces that carry no challenge. For
Personal, pass the exact value the conversation was asked to sign; a nonce the
runtime never answered fails closed.

**Why the challenge rides the browser evidence.** The observer has no channel to
the Personal stdio transport at all. `probe_host_identity` is pure local process
and code-signing inspection: `lsof` for the single listening PID, `ps` for its
argv, bundle layout, `Info.plist`, then `codesign`. There is no route for
invoking an MCP tool. The capture backend is the only other channel and it merely
drives a browser at a page URL. A side-channel poll is therefore impossible, and
it would also be weaker: under this design the answer literally travels the
transport AionUi uses and lands inside the accessibility snapshot the reviewer
already captures. The observer then recomputes the HMAC over the canonical claim
(`schema_version`, `server_name`, `version`, `build`, `candidate_commit`,
`candidate_source`, `board_id`, `pid`, `transport`, `nonce`, serialized with
sorted keys and compact separators) and compares it with `hmac.compare_digest`.
The attested PID, artifact digest, candidate SHA, board and `stdio` transport
must equal the independently probed values, so a decoy that merely looks right on
the command line, a stale answer from before key rotation, a replayed nonce, an
unsigned claim or a missing answer all fail closed. Snapshot text stays untrusted
until that recomputation succeeds.

The harness repeats that recomputation independently at validate time, so the
challenge is enforced by the final acceptance authority and not only inside
capture. Each Personal observation receipt carries its `attestation` and
`attestation_nonce`; `harness.py` binds the claim to that observation's own
runtime binding, then re-derives the HMAC using the key, the runtime pid and the
pinned candidate source read from the verifier's own `observer.json`, never from
the report. Two observations may not answer the same nonce. If that material is
missing or unreadable the report is refused: an absent key means the challenge
cannot be validated, which is a refusal rather than a pass.

Note: worker-3 rebinds its private handoff, meaning the challenge key, pid file,
receipt and the launched runtime, to the final combined source after source
freeze. The paths above describe the shape, not a promise that an existing
handoff already carries them.

## Evidence directory layout

```
/PATH/TO/evidence/
  report.json                     the evidence report the harness validates
  host-identity-<surface>.json    independently observed surface identity receipt
  capture-plan.json               exact 201-command expansion (198 core + 3 final gates)
  assertions/<id>.json            verifier-authored assertions
  specs/<id>.json                 caller request that was sent to the observer
  observations/<id>.json          browser_observation receipt
  typed/<id>-<n>.json              authenticated nonvisual recorder output
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
integrated candidate. Until the four operator-split lifecycle/result feature
tickets are assembled, full acceptance reports those capabilities unavailable;
generic Fleet, seat CLI, or Personal APIs, fallback UI, mock routes, and the
legacy native-Team adapter do not count as host execution. The supported host
transport is tracked by TK-23f86d56ff99. Final integrated real-host acceptance
remains a separate release gate.

The focused trust-boundary suite also proves that a requested arbitrary
40-hex SHA or syntactically valid sandbox board cannot relabel a different
running extension/UI state: either mismatch exits non-zero before the private
capture store is written.
