# Typed evidence recorder contract

Version 1 implements the four non-visual evidence kinds authorized by AN360 and
bounded by AN366. It does not replace the screenshot and normalized
accessibility-tree evidence required for every acceptance observation. It also
does not decide report-level `prior_state` graph validity; the returned
correlation block exposes the fields the parent integrator needs for that check.

## Trust boundary

Install `typed_evidence.py` in a verifier-owned private directory outside the
candidate checkout. The verifier creates its own least-privilege `trust.json`
and pins both that installed path and its SHA-256. Candidate-authored config,
credentials, receipt keys, source identity strings, action inputs, or evidence
are not trusted. `candidate_checkout_root` is also pinned; recording fails if
the running module is inside it.

```sh
python3 /PATH/TO/CANDIDATE/typed_evidence.py install --dir /PATH/TO/VERIFIER
/PATH/TO/VERIFIER/typed_evidence.py record \
  --request /PATH/TO/request.json --trust /PATH/TO/trust.json \
  --output /PATH/TO/evidence.json
/PATH/TO/VERIFIER/typed_evidence.py evaluate \
  --evidence /PATH/TO/evidence.json --expected /PATH/TO/expected.json \
  --trust /PATH/TO/trust.json --output /PATH/TO/result.json
/PATH/TO/VERIFIER/typed_evidence.py evaluate-parent \
  --trust /PATH/TO/trust.json < /PATH/TO/parent-request.json
```

Callable integration uses:

```python
evidence = record_evidence(request, verifier_trust)
result = evaluate_evidence(evidence, expected, verifier_trust)
```

Both functions raise `TypedEvidenceError` on schema, trust, authenticity,
freshness, provenance, correlation, or replay failure. A valid evaluation may
return `passed: false` when authentic evidence does not satisfy the expected
conjuncts.

The top-level trust object has these exact keys:

```json
{
  "schema_version": 1,
  "verifier_id": "purser-reviewer-2",
  "trusted_module_path": "/PATH/TO/VERIFIER/typed_evidence.py",
  "module_sha256": "...",
  "candidate_checkout_root": "/PATH/TO/CANDIDATE",
  "candidate_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "board_id": "sandbox-board",
  "max_age_seconds": 300,
  "active_evidence_key": "evidence-key-1",
  "evidence_keys": {"evidence-key-1": "1111111111111111111111111111111111111111111111111111111111111111"},
  "http_sources": {},
  "receipt_sources": {},
  "log_sources": {},
  "state_sources": {},
  "replay_guard": {"path": "/PATH/TO/VERIFIER/replay.log", "consume": true}
}
```

Each source object is exact and versioned by its adapter value. Private headers,
HMAC keys, and paths occur only in verifier trust:

```json
{
  "http": {
    "adapter": "trusted_http_v1",
    "provenance": "fleet-runtime",
    "runtime_id": "fleet-runtime-1",
    "base_url": "http://127.0.0.1:18921",
    "surface": "fleet",
    "board_id": "sandbox-board",
    "candidate_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "methods": ["GET", "POST"],
    "headers": {},
    "timeout_seconds": 4,
    "select_allowlist": ["/status", "/result/id"],
    "response_bindings": {
      "/candidate": "$candidate_commit",
      "/board": "$board_id",
      "/surface": "$surface",
      "/entity": "$entity",
      "/run": "$run_id",
      "/action": "$action_id",
      "/runtime": "fleet-runtime-1"
    },
    "runtime": {
      "pid_file": "/PATH/TO/VERIFIER/runtime.pid",
      "command_sha256": "...",
      "start_time": "Thu Sep 11 05:00:00 2026",
      "executable": "/usr/bin/python3",
      "cwd": "/PATH/TO/CANDIDATE",
      "artifact_path": "/PATH/TO/CANDIDATE/server.py",
      "artifact_sha256": "...",
      "listener_port": 18921
    }
  },
  "receipt": {
    "adapter": "hmac_json_v1",
    "provenance": "personal-runtime-receipt",
    "runtime_id": "personal-runtime-1",
    "path": "/PATH/TO/VERIFIER/receipt.json",
    "hmac_key_hex": "2222222222222222222222222222222222222222222222222222222222222222",
    "signature_field": "signature",
    "document_keys": [
      "schema_version", "issuer", "runtime_id", "pid",
      "candidate_commit", "board_id", "surface", "entity",
      "run_id", "action_id", "transport", "role", "captured_at"
    ],
    "timestamp_pointer": "/captured_at",
    "max_age_seconds": 300,
    "required_bindings": {
      "/candidate_commit": "$candidate_commit",
      "/board_id": "$board_id",
      "/surface": "$surface",
      "/entity": "$entity",
      "/run_id": "$run_id",
      "/action_id": "$action_id"
    },
    "issuer_pointer": "/issuer",
    "issuer": "personal-verifier",
    "runtime_pointer": "/runtime_id",
    "transport_pointer": "/transport",
    "transport": "stdio",
    "process": null
  },
  "log": {
    "adapter": "process_captured_jsonl_v1",
    "provenance": "verifier-captured-emitter",
    "runtime_id": "central-runtime-1",
    "path": "/PATH/TO/VERIFIER/central.jsonl",
    "document_keys": [
      "emitter", "timestamp", "runtime_id", "candidate_commit",
      "board_id", "surface", "entity", "run_id", "action_id",
      "event", "outcome", "action_sha256"
    ],
    "timestamp_pointer": "/timestamp",
    "max_age_seconds": 300,
    "required_bindings": {
      "/candidate_commit": "$candidate_commit",
      "/board_id": "$board_id",
      "/surface": "$surface",
      "/entity": "$entity",
      "/run_id": "$run_id",
      "/action_id": "$action_id"
    },
    "emitter": "central-runtime",
    "runtime_pointer": "/runtime_id",
    "max_bytes": 65536,
    "action_input_path": "/PATH/TO/VERIFIER/action.json",
    "action_input_sha256": "...",
    "action_digest_pointer": "/action_sha256",
    "process": {
      "pid_file": "/PATH/TO/VERIFIER/emitter.pid",
      "argv0_names": ["python3", "Python"],
      "executable": "/PATH/TO/PYTHON",
      "executable_sha256": "...",
      "argv_prefix": ["/PATH/TO/CANDIDATE/emitter.py"],
      "argv_contains": [],
      "required_arguments": {
        "--action-input": "/PATH/TO/VERIFIER/action.json",
        "--output": "/PATH/TO/VERIFIER/central.jsonl"
      },
      "cwd": "/PATH/TO/CANDIDATE",
      "artifact_path": "/PATH/TO/CANDIDATE/emitter.py",
      "artifact_sha256": "...",
      "entrypoint": {
        "kind": "script",
        "module": "",
        "path": "/PATH/TO/CANDIDATE/emitter.py",
        "sha256": "...",
        "resolver": "",
        "resolver_sha256": ""
      },
      "receipt_pid_pointer": "/pid"
    }
  },
  "state": {
    "adapter": "trusted_http_state_v1",
    "provenance": "fleet-runtime-state",
    "runtime_id": "fleet-runtime-1",
    "http_source_id": "fleet-api",
    "http_source_config_sha256": "..."
  },
  "process": {
    "pid_file": "/PATH/TO/VERIFIER/runtime.pid",
    "argv0_names": ["python3", "Python"],
    "executable": "/PATH/TO/PYTHON",
    "executable_sha256": "...",
    "argv_prefix": ["-I", "-m", "pursers_personal.cli", "mcp"],
    "argv_contains": [],
    "required_arguments": {
      "--candidate-source": "/PATH/TO/CANDIDATE/apps_server.py",
      "--candidate-commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "--board-id": "sandbox-board",
      "--acceptance-runtime-receipt": "/PATH/TO/VERIFIER/receipt.json"
    },
    "cwd": "/PATH/TO/CANDIDATE",
    "artifact_path": "/PATH/TO/CANDIDATE/apps_server.py",
    "artifact_sha256": "...",
    "entrypoint": {
      "kind": "isolated_module",
      "module": "pursers_personal.cli",
      "path": "/PATH/TO/CANDIDATE/pursers_personal/cli.py",
      "sha256": "...",
      "resolver": "/PATH/TO/ISOLATED/PYTHON",
      "resolver_sha256": "..."
    },
    "receipt_pid_pointer": "/pid"
  }
}
```

The `http`, `receipt`, `log`, `state`, and `process` labels above are explanatory;
their values are placed under the corresponding source ID or `process` field.
Receipt and log `document_keys` are exact top-level schemas. Receipt HMAC covers
the canonical complete document after removing only the signature member.
Unknown, missing, or wrong-typed nested data fails closed, including empty
containers.
`personal_runtime_receipt_v1` is the non-HMAC adapter for the actual Personal
runtime receipt. It requires the exact schema-version-1 document and pinned
Personal version, and pins the candidate checkout HEAD, source path and digest,
build, product/server identity, board, transport, private receipt file, and live
producer PID/argv/cwd. Process trust checks each required CLI flag/value exactly
once, pins the live executable bytes, and requires either the pinned script at
argv position one or an isolated `-I -m` invocation whose resolver selects the
pinned module bytes. A candidate path used only as an unrelated data argument
does not bind the entrypoint.

## Closed common shapes

All objects use exact key equality. Unknown or missing keys fail closed.

```json
{
  "schema_version": 1,
  "kind": "http_response|receipt_field|log_assertion|state_transition",
  "context": {
    "observation_id": "fleet.ticket-row",
    "run_id": "run-1",
    "action_id": "read-status",
    "entity": "TK-123",
    "surface": "aionui|fleet|personal",
    "board_id": "sandbox-board",
    "candidate_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "issued_at": "2026-09-11T05:00:00Z",
    "causal_index": 3
  },
  "recorder": {}
}
```

The expected predicate repeats the exact context and uses the parent-authorized
`all_of` shape:

```json
{
  "schema_version": 1,
  "kind": "http_response",
  "context": {},
  "all_of": []
}
```

Operators are closed to `eq`, `ne`, `contains`, `in`, `gt`, `gte`, `lt`, and
`lte`. Comparisons are type-aware and case-sensitive. No expression or regular
expression evaluation exists.

Authenticated evidence has this exact envelope:

```json
{
  "schema_version": 1,
  "kind": "http_response",
  "context": {},
  "captured_at": "2026-09-11T05:00:01Z",
  "source": {
    "source_id": "fleet-api",
    "adapter": "trusted_http_v1",
    "provenance": "fleet-runtime",
    "runtime_id": "fleet-runtime-1",
    "module_sha256": "...",
    "source_config_sha256": "..."
  },
  "record": {},
  "payload_sha256": "...",
  "auth": {"key_id": "verifier-key", "hmac_sha256": "..."}
}
```

Evidence is HMAC-authenticated by a verifier-owned evidence key. Selected data
is bounded and rejects token-like values, sensitive keys, and private home
paths. Evidence contains source IDs and digests, never source paths or trust
secrets. Successful evaluation can atomically consume the evidence ID in a
private replay journal.

The result is exact and report-friendly:

```json
{
  "schema_version": 1,
  "kind": "state_transition",
  "observation_id": "fleet.ticket-row",
  "passed": true,
  "checks": [],
  "correlation": {
    "run_id": "run-1",
    "action_id": "submit-ticket",
    "entity": "TK-123",
    "surface": "fleet",
    "board_id": "sandbox-board",
    "candidate_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "causal_index": 4,
    "runtime_id": "fleet-runtime-1"
  },
  "expected_digest": "...",
  "evidence_id": "..."
}
```

The parent validator must use `observation_id`, `entity`, `board_id`,
`runtime_id`, and `causal_index` to ensure each `prior_state.observation` exists,
passed independently in the same report, refers to the same relevant entity,
precedes the dependent observation, and is neither self-referential nor cyclic.

## `http_response`

Recorder:

```json
{
  "source_id": "fleet-api",
  "action_origin": "verifier_api",
  "request": {
    "method": "GET",
    "path": "/status",
    "body": null,
    "select": ["/status", "/result/id"]
  }
}
```

The verifier trust source pins the exact origin, surface, board, candidate,
runtime, allowed methods/selectors, private request headers, timeout, and
response JSON bindings. Runtime verification requires a private PID file,
process start time, executable, full command digest, process cwd, exact listening
PID and port, clean candidate-checkout HEAD, and an artifact path/digest inside
that checkout which is the executed script at argv position one. Thus a
matching-body echo
service, including one serving identical bytes from another cwd, cannot
substitute for the candidate runtime.
The recorder injects observation/run/action/entity correlation headers and
requires the real response to echo them. For a JSON request body it also sends
`X-Pursers-Action-SHA256` over the exact bounded canonical bytes placed on the
wire. It records status, selected bounded JSON values, and a body digest.
Redirects cannot escape the trusted origin.
Direct HTTP evidence is always `verifier_api`; it never claims a browser action.

Conjuncts are exact
`{"target":"status|field|action_origin","path":"","op":"eq","value":200}`.

## `receipt_field`

Recorder:

```json
{"source_id":"personal-receipt","fields":["/role","/transport","/pid"]}
```

The explicit `hmac_json_v1` adapter reads a private verifier-pinned receipt,
recomputes HMAC over the canonical complete document, enforces freshness and
context bindings, and can bind receipt PID to a private PID file plus live
process argv. This is the adapter point for the validated Personal HMAC path.
Reading arbitrary worker JSON or matching a receipt-only PID is insufficient.
For the actual Personal receipt, `personal_runtime_receipt_v1` performs the
candidate/build/process checks described above against a live
`python -m pursers_personal.cli mcp` producer and records authenticity as
`verifier_bound_personal_runtime`. A forged schema/version or arbitrary Python
process cannot satisfy this adapter.

Conjuncts are exact `{"path":"/role","op":"eq","value":"worker"}`.

## `log_assertion`

Recorder:

```json
{"source_id":"central-log","field_equals":{"/event":"ticket_submitted"}}
```

`fleet_evidence_trace_v1` is the product-backed adapter for the opt-in Fleet
trace contract. Its verifier-owned source configuration pins an exact
`document_keys` set and JSON pointers for timestamp, schema version, runtime,
live PID, executed-entrypoint digest, HTTP status, changed flag, outcome, and
all SHA-256 fields. It also pins the private action bytes and their digest, plus
the exact trusted Fleet HTTP source configuration. The trace must contain the
candidate/board/surface/observation/entity/run/action bindings and must come
from the same live PID and entrypoint already proven by that HTTP source.
Exactly one fresh matching record is accepted.

The exact verifier trust source for that producer is:

```json
{
  "adapter": "fleet_evidence_trace_v1",
  "provenance": "fleet-runtime-evidence-trace",
  "runtime_id": "fleet-runtime-1",
  "path": "/PATH/TO/VERIFIER/fleet-evidence.jsonl",
  "document_keys": [
    "schema_version", "emitter", "timestamp", "runtime_id", "pid",
    "candidate_commit", "entrypoint_sha256", "board_id", "surface",
    "observation_id", "run_id", "action_id", "entity", "method", "path",
    "status", "outcome", "effect", "changed", "before_sha256",
    "after_sha256", "result_sha256", "action_sha256"
  ],
  "timestamp_pointer": "/timestamp",
  "max_age_seconds": 300,
  "required_bindings": {
    "/candidate_commit": "$candidate_commit",
    "/board_id": "$board_id",
    "/surface": "$surface",
    "/observation_id": "$observation_id",
    "/entity": "$entity",
    "/run_id": "$run_id",
    "/action_id": "$action_id"
  },
  "emitter": "fleet-dashboard-runtime",
  "runtime_pointer": "/runtime_id",
  "max_bytes": 65536,
  "action_input_path": "/PATH/TO/VERIFIER/action.json",
  "action_input_sha256": "...",
  "action_digest_pointer": "/action_sha256",
  "http_source_id": "fleet-api",
  "http_source_config_sha256": "...",
  "schema_version_pointer": "/schema_version",
  "pid_pointer": "/pid",
  "entrypoint_digest_pointer": "/entrypoint_sha256",
  "status_pointer": "/status",
  "changed_pointer": "/changed",
  "outcome_pointer": "/outcome",
  "effect_pointer": "/effect",
  "sha256_pointers": [
    "/before_sha256", "/after_sha256", "/result_sha256",
    "/action_sha256", "/entrypoint_sha256"
  ]
}
```

The adapter fixes those document keys and pointer values as part of version 1;
changing the verifier list cannot define a weaker schema. It also requires
`emitter=fleet-dashboard-runtime`, `method=POST`, `path=/api/attention`, derives
`outcome` from the status class and `effect` from `changed`, and checks that
`changed` agrees with the before/after digests.

Collection snapshots the verifier-private JSONL file, sends the canonical bytes
from `action_input_path` to the pinned Fleet process with `POST /api/attention`,
and accepts only one correlated record appended by that call. The HTTP response
must expose the complete matching record under `/_evidence`, report
`log_emitted=true`, return the same status, and expose `/items`. The consumer
independently recomputes both `after_sha256` and `result_sha256` from the exact
original `{\"items\": ...}` product response. This rejects a concurrent
request's after-state being attributed to the observed action. Pre-existing
records, replaced file prefixes, non-canonical action files, and response/log
disagreement fail closed. `select_allowlist` must therefore include `/items`,
every `/_evidence/<document key>`, and `/_evidence/log_emitted`.

The private action JSON must not contain any producer-owned trace field. In
particular, caller input cannot provide status, outcome, changed/effect values,
state digests, runtime identity, candidate identity, PID, or entrypoint digest.
Those values must be derived by the real Fleet handler after the actual action.
A changed HTTP source, substituted process, wrong PID/entrypoint, caller-authored
result, stale/duplicate record, wrong correlation, schema extension, invalid
JSON type, or malformed digest fails closed.

`process_captured_jsonl_v1` remains the generic pinned-emitter adapter. It reads
only a bounded tail from a verifier-owned capture file and binds a live exact
emitter process to private action bytes. It is not evidence that a product
action occurred merely because a verifier fixture emitted a matching line.
Final Fleet acceptance must use `fleet_evidence_trace_v1` with the independently
reviewed producer from `TK-3df615678068`; if that producer is unavailable, the
caller reports `collector_gap` instead of manufacturing success evidence.
The focused suite's real-product test runs only when
`PURSERS_FLEET_EVIDENCE_CHECKOUT` names a clean checkout of the independently
reviewed producer commit; otherwise it is explicitly skipped rather than
substituting a test emitter.

Conjuncts are exact
`{"path":"/outcome","op":"eq","value":"succeeded"}`.

## `state_transition`

Recorder:

```json
{
  "source_id": "fleet-state",
  "before": {"method":"GET","path":"/state","body":null,"select":["/state"]},
  "action": {"method":"POST","path":"/actions","body":{"next":"busy"},"select":["/accepted"]},
  "after": {"method":"GET","path":"/state","body":null,"select":["/state"]}
}
```

The `trusted_http_state_v1` adapter pins the referenced HTTP source configuration
digest and runs before, action, and after against that one verified runtime.
Every phase has the same board/candidate/surface/entity/run/action correlation.
The signed record repeats the runtime and HTTP-source digest, preserves causal
timestamps, and rejects changed identity, source, or order. Expected conjuncts
cover positive and supported negative action results:

```json
{"phase":"before|action|after","path":"/state|/status","op":"eq","value":"idle"}
```

## Current integration boundary

The disposable tests prove executable producer-to-recorder-to-evaluator paths
for the actual Personal MCP receipt process and clean-checkout HTTP/state
runtimes. Preparatory Fleet-trace tests are negative-only until the independently
reviewed `TK-3df615678068` producer is available; they prove that forged result,
runtime, entrypoint, correlation, and JSON-type variants cannot become PASS.
The final positive must launch the real Fleet handler, execute its disposable
`POST /api/attention` action, and consume the resulting product trace. No
production mutation or browser claim is supplied by this delta. Reviewer-owned
setup and final browser evidence remain separate gates. The shared runner invokes
the installed module's `evaluate-parent` command for every nonvisual canonical
conjunct. That command authenticates the evidence with verifier-owned trust,
binds the full observation correlation, evaluates the canonical source/state
meaning, and returns the exact result schema consumed by the parent harness.
