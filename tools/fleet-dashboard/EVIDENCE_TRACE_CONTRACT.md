# Fleet runtime evidence trace contract

Version 1 is a narrow, opt-in observability channel for typed acceptance
evidence. It observes the existing loopback Fleet handler and the existing
`/api/attention` disposable-state API. It adds no endpoint, credential, or
authority. With no `--evidence-trace-config`, Fleet behavior is unchanged.

The verifier owns a private directory and creates this exact 0600 config:

```json
{
  "schema_version": 1,
  "output_path": "/PATH/TO/VERIFIER/fleet-evidence.jsonl",
  "max_bytes": 65536,
  "runtime_id": "fleet-runtime-1",
  "board_id": "sandbox-board",
  "surface": "fleet"
}
```

The output path must be absolute and its directory must be owned by the current
user with no group or other permissions. An existing output must be a regular
0600 file owned by that user and no larger than `max_bytes`. `max_bytes` is
bounded from 4096 through 1048576. Fleet fails closed at launch for an invalid
config, resolves its candidate commit from the git checkout containing the
executed `fleet_dashboard.py`, and hashes that entrypoint itself.

## Launch and capture

Use a clean candidate checkout and disposable seat-state directory. The
verifier pins this complete command, executable, cwd, PID/start time, listener,
entrypoint path and digest, checkout HEAD, and private trace config through the
typed collector's process/runtime trust:

```sh
/PATH/TO/PYTHON /PATH/TO/CANDIDATE/tools/fleet-dashboard/fleet_dashboard.py \
  --port 18921 \
  --token-file /PATH/TO/VERIFIER/token \
  --seat-state-dir /PATH/TO/VERIFIER/disposable-state \
  --evidence-trace-config /PATH/TO/VERIFIER/trace.json
```

For every before/action/after HTTP call, send these bounded correlation headers:

```text
X-Pursers-Observation-Id: fleet.attention-state
X-Pursers-Run-Id: run-1
X-Pursers-Action-Id: save-attention
X-Pursers-Entity-Id: TK-123
```

For `POST`, also send `X-Pursers-Action-SHA256`, equal to the SHA-256 of the
exact request bytes sent by the verifier. The same verifier-owned file can be
used as both typed collector `action_input_path` and HTTP request body:

```sh
ACTION_SHA256="$(shasum -a 256 /PATH/TO/VERIFIER/action.json | awk '{print $1}')"
curl --noproxy '*' --fail-with-body \
  -H 'Content-Type: application/json' \
  -H 'X-Pursers-Observation-Id: fleet.attention-state' \
  -H 'X-Pursers-Run-Id: run-1' \
  -H 'X-Pursers-Action-Id: save-attention' \
  -H 'X-Pursers-Entity-Id: TK-123' \
  -H "X-Pursers-Action-SHA256: ${ACTION_SHA256}" \
  --data-binary @/PATH/TO/VERIFIER/action.json \
  http://127.0.0.1:18921/api/attention
```

GET before and after calls use the four correlation headers without the action
digest. A correlated JSON response retains its product fields and adds an
`_evidence` object. It also returns the four correlation response headers
required by `trusted_http_v1`. These response bindings are suitable for the
collector:

```json
{
  "/_evidence/candidate_commit": "$candidate_commit",
  "/_evidence/board_id": "$board_id",
  "/_evidence/surface": "$surface",
  "/_evidence/entity": "$entity",
  "/_evidence/run_id": "$run_id",
  "/_evidence/action_id": "$action_id",
  "/_evidence/runtime_id": "fleet-runtime-1"
}
```

The actual handler derives `status`, `outcome`, `changed`, `effect`,
`before_sha256`, `after_sha256`, and `result_sha256` after invoking the existing
operation. `result_sha256` covers the original product response before the
`_evidence` member is added. Caller request fields never select those values.

All attention-state reads and writes in the server share one reentrant state
lock. A traced POST captures its immediate-before state and runs the actual save
under that lock; its authoritative save result is also its after-state. The
handler retains that tuple through response emission instead of reading state
again, so a later traced or untraced request cannot be attributed to the first
action. A traced GET likewise uses its single returned snapshot as both before
and after. Trace-file append serialization is separate and does not substitute
for this operation-level causal isolation.

## JSONL consumer schema

Only a POST with all valid correlation headers and a matching body digest
appends a record. Each complete correlation/action-digest tuple is appended at
most once per output, including after process restart. A replay or an append
failure receives the normal product response without trusted evidence metadata.
GET observations are returned over the bound HTTP response but do not add log
records. This lets `state_transition` capture before/action/after while
`log_assertion` selects the single real action record.

Every line has exactly these fields:

```json
{
  "schema_version": 1,
  "emitter": "fleet-dashboard-runtime",
  "timestamp": "2026-09-11T15:00:00+00:00",
  "runtime_id": "fleet-runtime-1",
  "pid": 12345,
  "candidate_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "entrypoint_sha256": "...",
  "board_id": "sandbox-board",
  "surface": "fleet",
  "observation_id": "fleet.attention-state",
  "run_id": "run-1",
  "action_id": "save-attention",
  "entity": "TK-123",
  "method": "POST",
  "path": "/api/attention",
  "status": 200,
  "outcome": "succeeded",
  "effect": "attention_state_changed",
  "changed": true,
  "before_sha256": "...",
  "after_sha256": "...",
  "result_sha256": "...",
  "action_sha256": "..."
}
```

Configure `process_captured_jsonl_v1` with that exact `document_keys` set,
`timestamp_pointer=/timestamp`, `runtime_pointer=/runtime_id`,
`action_digest_pointer=/action_sha256`, `emitter=fleet-dashboard-runtime`, and
the six context bindings for candidate commit, board, surface, entity, run, and
action. Pin the live Fleet process as the actual script entrypoint; an unrelated
argv data argument is not sufficient provenance.

No raw request or response body, state value, arbitrary header, token, door,
authorization material, or filesystem path is emitted. Invalid/missing
correlation, wrong body digest, unrelated routes, replays, and trace I/O failure
never produce a trusted log assertion. Failed product actions are recorded as
failed; the channel never manufactures a passing result.
