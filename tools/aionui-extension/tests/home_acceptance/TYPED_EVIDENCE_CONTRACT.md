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
      "argv_prefix": ["/PATH/TO/CANDIDATE/emitter.py"],
      "argv_contains": [],
      "required_arguments": {
        "--action-input": "/PATH/TO/VERIFIER/action.json",
        "--output": "/PATH/TO/VERIFIER/central.jsonl"
      },
      "cwd": "/PATH/TO/CANDIDATE",
      "artifact_path": "/PATH/TO/CANDIDATE/emitter.py",
      "artifact_sha256": "...",
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
    "argv_prefix": ["-m", "pursers_personal.cli", "mcp"],
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
once and binds the command to the pinned artifact bytes.

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
that checkout which appears in the process command. Thus a matching-body echo
service, including one serving identical bytes from another cwd, cannot
substitute for the candidate runtime.
The recorder injects observation/run/action/entity correlation headers and
requires the real response to echo them. It records status, selected bounded
JSON values, and a body digest. Redirects cannot escape the trusted origin.
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

The `process_captured_jsonl_v1` adapter reads only a bounded tail from a
verifier-owned capture file. It requires an exact-schema entry produced by a
live pinned emitter process from a private verifier-owned action input. The
entry must repeat the action-input digest, runtime identity, freshness, and
candidate/board/surface/entity/run/action bindings. Exactly one match is
required. A substituted emitter, invented or hard-coded signed success, stale
line, duplicate match, unrelated entry, changed action input, or unbound process
fails. If the real pinned emitter/action cannot be captured, the caller must
report `collector_gap`; it must not manufacture success evidence.

Conjuncts are exact
`{"path":"/outcome","op":"eq","value":"accepted"}`.

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
for the actual Personal MCP receipt process, verifier action-to-pinned-emitter
log capture, and clean-checkout HTTP/state runtimes. No production mutation or
browser claim is supplied by this delta. Reviewer-owned setup and final browser
evidence remain separate gates. Opus owns wiring results into the shared runner,
harness, canonical facts, and report-level graph validation.
