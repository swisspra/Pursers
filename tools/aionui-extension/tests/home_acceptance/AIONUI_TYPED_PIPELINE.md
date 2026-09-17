# AionUi typed-evidence pipeline

The acceptance harness requires typed evidence in addition to a screenshot and
normalized accessibility tree. A visual pass is not a strict pass until every
canonical typed conjunct has a verifier-authenticated evidence record.

The rc6 run prepared filenames for typed evidence but did not install a typed
recorder, create verifier trust, or issue recorder commands. Six older browser
recipes were executable in `planning/gap-implementability-map.json`, six newer
recipes existed only as proposals in
`docs/design-home/context/typed-predicate-integration-delta.json`, and twelve
required sources had neither an executable request nor a trust recipe. This is
why all 21 AionUi rows were strict-blocked even when their visual checks passed.

`runner.py prepare-aionui-typed` now closes that procedure gap. It reads the
canonical predicates used by the harness, requires exactly 21 rows and 24
unique source IDs, installs `typed_evidence.py` outside the candidate checkout,
creates private verifier trust, and writes one executable command per record.
`runner.py record-aionui-typed` rebuilds the request with a fresh timestamp and
executes the pinned external recorder. A nonzero browser observer exit now
produces an authenticated `failure` record, and an execution timeout produces
an authenticated `blocked` record, including bounded raw stdout/stderr and full
stream digests. This prevents selector gaps and runtime failures from becoming
an unverifiable missing file. The normal `assemble` command continues to fail
closed if any referenced typed record is absent or invalid.

## Required records

| Acceptance row | Required source ID(s) | Producer | Why rc6 did not produce it |
|---|---|---|---|
| `door_connect` | `browser-door-connect-worker`; `browser-door-connect-reviewer` | `trusted_browser_state_v1` | Recipes existed, but phase B never installed or invoked the typed recorder. |
| `team_setup` | `browser-team-setup` | `trusted_browser_state_v1` | No executable recipe was wired; the old gap map also predated the selected-group marker already rendered by Home. |
| `five_workers_three_reviewers` | `browser-five-workers-three-reviewers` | `trusted_browser_state_v1` | No recipe or semantic roster counters were bound. |
| `ticket_offer_claim` | `browser-ticket-offer-claim-live`; `browser-ticket-offer-claim-expired` | `trusted_browser_state_v1` | No executable recipes were bound for the live and expired outcomes. |
| `ticket_submit_independent_review` | `browser-ticket-submit-independent-review` | `trusted_browser_state_v1` | No recipe was bound to the result and approval counters. |
| `result_visible` | `browser-result-visible` | `trusted_browser_state_v1` | No recipe was bound to the actor, transition, review, and cursor markers. |
| `pause_resume_stop` | `browser-pause-resume-stop` | `trusted_browser_state_v1` | The old row required a nonexistent Resume operation and therefore had no valid product recipe. |
| `clean_reconnect_after_rotation` | `browser-clean-reconnect-old-door`; `browser-clean-reconnect-new-door` | `trusted_browser_state_v1` | The old-door recipe existed, but the new-door cleanup recipe and counters were not bound. |
| `extension-join.state.joined` | `browser-extension-join-state-joined` | `trusted_browser_state_v1` | The recipe existed, but phase B never invoked the typed recorder. |
| `extension-join.state.status-loaded` | `browser-extension-join-state-status-loaded` | `trusted_browser_state_v1` | The gap map treated a stale fixture board value as a product blocker instead of supplying a runtime recipe. |
| `extension.environment-free-mcp-registration` | `browser-state-extension.environment-free-mcp-registration` | `trusted_browser_state_v1` | The approved delta recipe was not consumed by the phase-B runner. |
| `extension.idempotent-reconnect` | `browser-extension-idempotent-reconnect` | `trusted_browser_state_v1` | No recipe was bound to the connection and registration-error counters. |
| `extension.join-form` | `browser-extension-join-form` | `trusted_browser_state_v1` | The recipe existed, but phase B never invoked the typed recorder. |
| `extension.join-progress` | `browser-state-extension.join-progress` | `trusted_browser_state_v1` | The approved delta recipe was not consumed by the phase-B runner. |
| `extension.redacted-status-card` | `browser-extension-redacted-status-card` | `trusted_browser_state_v1` | The recipe existed, but phase B never invoked the typed recorder. |
| `extension.reviewer-preset-claude` | `browser-state-extension.reviewer-preset-claude` | `aionui_assistant_binding_v1` | The approved manifest-binding recipe was not consumed by the phase-B runner. |
| `extension.reviewer-preset-codex` | `browser-state-extension.reviewer-preset-codex` | `aionui_assistant_binding_v1` | The approved manifest-binding recipe was not consumed by the phase-B runner. |
| `extension.settings-navigation` | `browser-extension-settings-navigation` | `trusted_browser_state_v1` | The old row invented a hidden settings panel that Home does not have, so no truthful recipe existed. |
| `extension.worker-preset-claude` | `browser-state-extension.worker-preset-claude` | `aionui_assistant_binding_v1` | The approved manifest-binding recipe was not consumed by the phase-B runner. |
| `extension.worker-preset-codex` | `browser-state-extension.worker-preset-codex` | `aionui_assistant_binding_v1` | The approved manifest-binding recipe was not consumed by the phase-B runner. |
| `final.quickstart-candidate-flow` | `browser-final-quickstart-candidate-flow` | `trusted_browser_state_v1` | No recipe was bound to the real Open Quickstart control and raw-JSON absence marker. |

The `pause_resume_stop` and `extension.settings-navigation` catalog rows were
amended 2026-09-15 to product semantics. The former records successful pause
and cooperative-stop requests and proves that no Resume control exists. The
latter follows the visible `#open-quickstart` link to the already-visible
`#helper` section. Neither amendment adds a synthetic product control.

## Independent verifier procedure

Run the normal observer installation and `prepare` steps first. Copy the exact
candidate extension manifest and its `contexts/` directory into a private
external installation directory, then run:

```sh
python3 /PATH/TO/CANDIDATE/tools/aionui-extension/tests/home_acceptance/runner.py \
  prepare-aionui-typed \
  --observer /PATH/TO/VERIFIER/observer \
  --evidence /PATH/TO/VERIFIER/evidence \
  --dir /PATH/TO/VERIFIER/typed \
  --installed-manifest /PATH/TO/INSTALLED/aion-extension.json \
  --bridge-command /PATH/TO/EXACT/VENV/bin/pursers-wait-bridge \
  --bridge-wheel /PATH/TO/EXACT/pursers_wait_bridge-VERSION-py3-none-any.whl
```

The generated `aionui-typed-plan.json` contains 24 commands. Execute each
command against the real local candidate runtime after arranging the stated
precondition. Do not edit the generated trust or requests, reuse an evidence
directory, or substitute expected values for browser observations. Finally run
`assemble` and `validate`; those commands authenticate and evaluate the
product-produced records referenced by the 21 rows.

The bridge executable and wheel must be external verifier-owned artifacts.
Preparation records their reported version, wheel digest, and executable digest
in every browser record's signed observer provenance; recording and replay both
recheck all three bindings.

The six rc6 visual failures (`extension-join.state.bridge-missing`,
`extension-join.state.error`, `extension-join.state.initial`,
`extension-join.state.joining`, `extension-join.state.status-empty`, and
`extension.bounded-errors`) remain verifier-held capture boundaries under
`CQ-3eddc8f73c6e7671`. They are not confirmed product defects and are not
reclassified by this typed-evidence pipeline.
