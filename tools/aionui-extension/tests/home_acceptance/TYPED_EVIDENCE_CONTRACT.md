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
credentials, receipt keys, log keys, source identity strings, or evidence are
not trusted. `candidate_checkout_root` is also pinned; recording fails if the
running module is inside it.

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
  "action_origin": "verifier_api|browser_observed",
  "request": {
    "method": "GET",
    "path": "/status",
    "body": null,
    "select": ["/status", "/result/id"]
  }
}
```

The verifier trust source pins the exact origin, surface, board, candidate,
runtime, allowed methods, private request headers, timeout, and response JSON
bindings. The recorder injects observation/run/action/entity correlation
headers and requires the real response to echo them. It records status,
selected bounded JSON values, and a body digest. Redirects cannot escape the
trusted origin. `action_origin: verifier_api` proves only that direct API call;
an expected conjunct may require `browser_observed` when a UI action itself is
the claim.

Conjuncts are exact
`{"target":"status|field|action_origin","path":"","op":"eq","value":200}`.

## `receipt_field`

Recorder:

```json
{"source_id":"personal-receipt","fields":["/role","/transport","/pid"]}
```

The explicit `hmac_json_v1` adapter reads a private verifier-pinned receipt,
recomputes HMAC over an exact configured field list, enforces freshness and
context bindings, and can bind receipt PID to a private PID file plus live
process argv. This is the adapter point for the validated Personal HMAC path.
Reading arbitrary worker JSON or matching a receipt-only PID is insufficient.

Conjuncts are exact `{"path":"/role","op":"eq","value":"worker"}`.

## `log_assertion`

Recorder:

```json
{"source_id":"central-log","field_equals":{"/event":"ticket_submitted"}}
```

The `hmac_jsonl_v1` adapter reads only a bounded tail from a verifier-pinned log,
requires exactly one authentic fresh entry from the configured emitter, binds
candidate/board/surface/entity/run/action, and can verify the live emitter
process. A substituted emitter, forged success line, stale line, duplicate
match, or unrelated entry fails. The signature is removed from emitted data.
Logs establish only fields that the trusted emitter actually recorded.

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

The `trusted_http_state_v1` adapter runs before, action, and after against one
trusted HTTP runtime. Every phase has the same board/candidate/surface/entity/
run/action correlation. The signed record preserves causal timestamps and
rejects changed order. Expected conjuncts cover positive and supported negative
action results:

```json
{"phase":"before|action|after","path":"/state|/status","op":"eq","value":"idle"}
```

## Current integration boundary

The module and disposable tests prove executable recorder-to-evaluator
roundtrips. No real AionUi/Fleet/Personal verifier trust config, runtime session,
receipt HMAC key, authenticated log emitter, or production mutation is supplied
by this delta. Those remain reviewer-owned setup and final browser-evidence
gates. Opus owns wiring these results into the shared runner, harness, canonical
facts, and report-level graph validation.
