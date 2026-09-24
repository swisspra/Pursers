# Autonomous Board Butler contract

Status: canonical Wave 0 design contract. This document defines interfaces for
the successor implementation tickets. It does not enable autonomous behavior.

## Purpose and invariants

Board Butler is one resident, board-owned coordinator identity. It observes all
active boards in `project_registry`, answers only policy-authorized coordinator
questions, consumes durable commands, reconciles pre-authorized seat capacity,
and invokes explicitly allowlisted MCP v2 connectors. It is a control-plane
principal, not a model host and not a substitute for an operator, worker, or
reviewer.

These invariants are normative:

- Existing boards keep their current shadow behavior. Absence of the new
  per-board configuration means `shadow`; migration must never select
  `autonomous`.
- Autonomous mode requires an explicit per-board `enabled=true`, a matching
  human-created active-authorization fingerprint, and a valid immutable
  envelope. Any missing, stale, or invalid input resolves to `shadow`,
  `degraded`, or `killed`, never a permissive default.
- A human or board administrator creates principals, credentials, seat
  templates, connector secret references, and ceilings. Butler can select and
  instantiate only approved templates inside those ceilings. It cannot mint a
  principal, issue or rotate a credential, read a secret value, edit its
  envelope, or increase a budget or concurrency ceiling.
- Butler cannot work a ticket, review, approve, open or merge a pull request,
  push main, tag, publish, release, change review policy, weaken independent
  review, or review its own output.
- Model output and connector output are untrusted proposals/data. Deterministic
  policy validates every state change at the boundary that performs it.
- Every mutating operation uses compare-and-swap (CAS), a stable idempotency
  key, bounded input/output, and an append-only redacted audit record.
- The immediate kill switch is checked before dequeue, before an external
  call, and immediately before a mutation. It blocks new work while preserving
  audit visibility and in-flight mutation truth.

The existing `coordinator_config.board_butler` version 1 shadow contract and
local `active-authorized.json` remain authoritative until an implementation
ticket adds version 2. No document in this change is executable configuration.

## Ownership and trust boundaries

Central owns board state, command ordering, question state, agent lifecycle,
CAS tokens, and the append-only audit journal. Board Butler owns policy
evaluation and reconciliation planning, but Central rechecks its board-bound
identity and authorization on every write. A host-local executor owns process,
checkout, and ACP-seat lifecycle. It accepts only typed requests signed by the
configured local Butler service and returns observations; it has no board
administrator credential and cannot create principals or credentials.

Direct API and ACP runners are replaceable model-execution adapters. They
receive a bounded task packet, public policy, and redacted observations. Board
tokens, provider credentials, connector credentials, private paths, and the
host executor control channel never enter a model prompt. The adapters return
proposals only.

Host limits are operator-owned runtime configuration, not product defaults. On
the current host, the binding ceiling is **12 concurrently running agent
processes total across worker, reviewer, and ACP-worker roles**. The executor
must also observe and report Butler/coordinator control-plane process counts so
they cannot disappear from physical-capacity accounting. The role ceiling and
a separately configured total host-process ceiling are both enforced; neither
is model-adjustable. No implementation may bake `12` into a library default.

MCP connectors are board/project-scoped capabilities. Their credentials live
behind opaque secret references resolved by their transport adapter. Tool and
resource names are exact allowlists, results are size-limited and treated as
untrusted data, and failure of one connector does not stop stewardship of
other boards.

Fleet is an operator UI and projection. It writes only validated versioned
config or durable commands with CAS. It cannot mint credentials, create
authorization, bypass the executor, or infer success from a request being
accepted.

## Versioned interfaces

Normative JSON Schemas are committed beside this document:

- [`schemas/autonomous-butler-config-v1.schema.json`](schemas/autonomous-butler-config-v1.schema.json)
- [`schemas/autonomous-butler-command-v1.schema.json`](schemas/autonomous-butler-command-v1.schema.json)
- [`schemas/autonomous-butler-command-v2.schema.json`](schemas/autonomous-butler-command-v2.schema.json)
- [`schemas/autonomous-butler-state-v1.schema.json`](schemas/autonomous-butler-state-v1.schema.json)
- [`schemas/autonomous-butler-executor-v1.schema.json`](schemas/autonomous-butler-executor-v1.schema.json)
- [`schemas/autonomous-butler-audit-v1.schema.json`](schemas/autonomous-butler-audit-v1.schema.json)
- [`schemas/autonomous-butler-control-audit-v2.schema.json`](schemas/autonomous-butler-control-audit-v2.schema.json)
- [`schemas/autonomous-butler-model-v1.schema.json`](schemas/autonomous-butler-model-v1.schema.json)

All schemas reject unknown fields. Identifiers are opaque bounded strings;
secret references are `secret_ref` identifiers, never paths or values. Times
are UTC RFC 3339 strings. Byte and item limits are enforced before parsing and
again before persistence. Schema validation is necessary but not sufficient:
Central and the executor also enforce actor, board, authorization, CAS, and
ceiling constraints, including `min <= target <= max`, desired values no higher
than the immutable envelope, command-kind-specific payload fields, and the
sum-of-roles concurrency limits that portable JSON Schema cannot express.

### Configuration and immutable envelope

`autonomous_butler_config_v1` is stored per board under coordinator-managed
state. Its required top-level `enabled` boolean is the one authoritative
per-board enable signal. `desired.mode=autonomous` requires `enabled=true` and
an `authorization`; `enabled=false` requires `desired.mode=shadow`. The mutable
desired section selects mode, runner, counts, cooldowns, budgets, and connector
enablement. The human-owned envelope contains approved template IDs, connector
IDs, and maximums. Butler may reduce desired values or request an allowed value;
it cannot edit the envelope. There is no second enable flag in legacy config,
authorization, local files, or runtime state.

The config references human-owned `host_runtime` values. Its
`agent_process_ceiling` caps the combined worker, reviewer, and ACP-worker
processes on that host. `total_process_ceiling` additionally includes observed
Butler/coordinator control-plane processes and any other explicitly accounted
resident agent process. `control_plane_processes` is an observed/reserved count,
not capacity Butler may consume. The current host config must carry
`agent_process_ceiling=12`; another host must receive an explicit operator value
and cannot inherit 12 as a default.

The envelope fingerprint is SHA-256 over canonical JSON of the envelope. The
separate active authorization binds `board_id`, config generation, envelope
fingerprint, and expiry. Enabling autonomous mode is valid only when all four
match current state. Editing the envelope or increasing a desired ceiling
invalidates authorization and immediately demotes the effective mode to
`shadow` until a human re-authorizes it.

Counts are expressed independently for `worker`, `reviewer`, and `acp_worker`
as `min`, `target`, and `max`, with `min <= target <= max`. Their sum must not
exceed the board ceiling, the envelope ceiling, or host runtime's agent-process
ceiling. Agent-role processes plus observed control-plane processes must not
exceed `total_process_ceiling`. A worker and reviewer must never share a
principal. A Butler principal has `can_work=false` and `can_review=false`. ACP
workers are still workers for review-independence and capacity accounting.

Budgets have immutable maxima and mutable desired limits. Implementations must
reserve before invoking a runner or connector, commit measured usage after the
call, and release stale reservations. If accounting is unavailable or a limit
would be exceeded, the action is not started. Butler cannot infer token or cost
usage from prose.

### Desired and actual state

The desired state is the validated configuration at one CAS generation. Actual
state is an observation in `autonomous_butler_state_v1`; it never masquerades
as desired state. Each role reports ready, busy, starting, draining, unhealthy,
and stopped counts plus the exact template IDs and opaque seat IDs represented.
Host process state reports role-agent and control-plane counts separately, the
two configured ceilings, and observation freshness. A missing or stale process
observation blocks scale-up rather than assuming spare capacity.
Connector and executor health include observation time and bounded reason
codes. Stale observations become `unknown`, not healthy.

The effective state machine is:

```text
config absent / legacy ------------------------------> shadow
autonomous requested + invalid/missing authorization -> shadow
valid autonomous request ----------------------------> pending
pending + reconciliation in progress ----------------> applying
targets met and dependencies healthy ----------------> autonomous
recoverable partial failure -------------------------> degraded
policy breach, repeated veto, or budget fault --------> auto_demoted
kill switch engaged ---------------------------------> killed
```

`pending`, `applying`, `degraded`, `auto_demoted`, and `killed` are durable
truthful projections, not animation states. A kill does not rewrite an already
committed Central transaction. The state records that transaction and prevents
the next one. Recovery from `killed` requires a human command; recovery from
`auto_demoted` requires a human authorization refresh. A process restart alone
does neither.

## Durable command lifecycle

Human and A2A commands use `autonomous_butler_command_v1`. Allowed kinds are
`set_desired_state`, `reconcile_now`, `drain_seat`, `retire_seat`,
`enable_connector`, `disable_connector`, `veto_action`, `kill`, and `resume`.
Commands cannot carry credentials, arbitrary shell, raw MCP arguments, a new
template, or raised ceilings.

Central's durable submission surface writes `autonomous_butler_command_v2`.
Version 2 preserves the v1 intents and transition evidence while adding the
stable request ID, authenticated sender/authority, project target, bounded
expiry and priority, canonical request digest, complete transition history,
and typed terminal result required for replay-safe Human/A2A operation. The v1
schema remains immutable and readable. Command/config mutations use the
redacted `autonomous_butler_control_audit_v2`; the original general audit v1
schema remains unchanged.

Lifecycle is monotonic:

```text
accepted -> validating -> pending -> applying -> succeeded
                    \-> rejected
                              \----> failed
accepted/pending/applying ----------> cancelled
```

`accepted` means only that Central durably stored a syntactically valid
request. `succeeded` requires product-produced actual state proving the effect.
`cancelled` while applying means cancellation was requested; the result must
say whether the executor reached a commit point. Terminal states never reopen.
Every command snapshot has a required typed `transition` record containing
`prior_revision`, `current_revision`, `actor_id`, `reason_code`, `audit_id`, and
`occurred_at`. `current_revision` equals the command's top-level `revision`;
accepted creation uses prior revision zero, and every later transition advances
exactly one revision. The linked audit record has the same actor, reason, and
command ID. Missing or inconsistent transition evidence rejects the write.

The command ID is the idempotency key. Replays with identical canonical bytes
return the existing command. Reuse with different bytes is rejected. Commands
carry `expected_config_revision`; a mismatch rejects before side effects.
Per-seat commands additionally carry the last observed seat generation, so a
retired and recreated name cannot be targeted accidentally.

`kill` is accepted through a dedicated bounded path even when normal queues or
models are unhealthy. It first persists `kill_requested`, then stops new
dispatch, cancels cancellable external calls, asks the executor to drain owned
processes, and records resulting actual state. `resume` never enables
autonomous mode by itself; it clears the kill latch only if current config and
authorization still validate.

## Reconciliation

One board-scoped reconciliation lease prevents concurrent reconcilers. Each
cycle reads one consistent snapshot, validates authorization, computes a pure
plan, persists the plan with its input revisions, and executes at most the
configured operation and start-rate ceilings.

Scale-up selects only an approved template with a pre-provisioned credential
reference and asks the host executor to instantiate it. Scale-down selects only
idle Butler-managed seats, marks one draining, waits for lease/offer release,
and asks the executor to stop it before requesting Central retirement. Busy,
reviewing, manually managed, or unknown seats are not retired. Minimum reviewer
capacity is checked before and after every change. Cooldowns survive restart.

The reconciler converges; it does not promise exactly-once external effects.
Before retrying an ambiguous executor result, Butler queries by operation ID.
The executor stores the operation/result before replying and treats an
identical replay as a read. A payload mismatch for an existing operation ID is
a policy violation. Central state is re-read after every external action; only
observed product state can complete a command.

No-capacity, host-unreachable, stale-observation, authorization-expired,
connector-failed, budget-exhausted, and CAS-conflict conditions are separated.
Backoff is bounded and jittered. A CAS conflict discards the plan and rereads;
it never blindly retries the stale mutation. One board's backoff does not block
other boards.

## Authority matrix

| Action | Human/admin | Butler | Model adapter | Host executor | Connector | Fleet |
|---|---:|---:|---:|---:|---:|---:|
| Create principal or credential | yes | never | never | never | never | never |
| Define template, secret ref, or immutable ceiling | yes | never | never | never | never | propose only |
| Enable autonomous mode / authorize envelope | yes | never | never | never | never | typed request only |
| Reduce desired capacity or budget | yes | within envelope | propose | never | never | typed request |
| Raise desired value within existing envelope | yes | policy-authorized | propose | never | never | typed request |
| Raise immutable ceiling or authority | yes | never | never | never | never | never |
| Start/stop approved seat process | request | request | never | typed request only | never | command only |
| Retire idle Butler-managed seat | authorize | request | never | stop only | never | command only |
| Answer eligible information question | yes | policy-authorized | draft only | never | data only | observe/veto |
| Work/review/approve/PR/merge/tag/publish/release | separate authorized roles | never | never | never | never | never |
| Invoke connector tool/resource | configure | exact allowlist | propose only | never | serve only | configure/observe |
| Engage kill switch | yes | policy breach only | never | obey | never | command |
| Clear kill or auto-demotion | yes | never | never | never | never | command with human authority |

“Yes” does not imply one principal has every administrator capability. Existing
Central scopes and separation of duties remain in force.

## Question answering and model execution

The resident resolves a per-board `answering_mode` of `off`, `assist`, or
`autonomous`. Omitted legacy configuration resolves to `assist`, so migration
cannot silently acquire answer authority. `autonomous` additionally requires
the existing active runtime authorization, an allowed answer class and evidence
kind, an active window, available rate limits, no kill/demotion state, and an
elapsed durable hold. The `pursers` showcase selects `autonomous` explicitly;
other boards inherit no such authority.

Question answering is opt-in by question kind. Only `information` questions
covered by a deterministic rule and complete cited evidence can be answered.
Decision, deliverable, approval, waiver, scope, version, release, merge,
membership, registry, secret, and ambiguous questions always escalate. Butler
cannot answer a question it asked or one concerning approval of its own work.

The direct API and ACP adapters implement the same strict
`autonomous_butler_model_v1` request/result envelope. A request binds its ID,
board, subject, task kind, policy digest, bounded task input plus its canonical
digest, evidence references, output byte cap, token/cost maxima, deadline,
opaque cancellation token, and the ID plus SHA-256 of the task-specific output
schema. A result binds the same request, board, and
policy digest; returns bounded `proposal_json` or a typed failure; carries
bounded citation references; and reports measured input, output, total-token,
and cost usage. `measured=false`, missing usage, arithmetic inconsistency, or
usage beyond the reservation fails closed in semantic validation. The proposal
JSON text is parsed only after its byte cap and must validate against the
request's exact task schema before any policy decision; it cannot choose tools
or authority. The common `task_input` itself is strict: bounded instruction,
observation references, and constraints only.

Cancellation is keyed only by the request's opaque cancellation token. The
adapter checks it before provider dispatch and before returning a proposal;
late provider output is discarded. The deterministic layer verifies citations,
redacts, applies the hold/veto window, rechecks question state and CAS, and only
then calls Central's answer path. Timeout, malformed output, policy-digest
drift, cancellation, missing citations, or provider error yields a result with
`proposal_json=null`, a fixed bounded `reason_code`, and a typed error category/code/
retryability record, never a partial answer.

Provider selection is per board. Provider credentials are resolved in the
adapter process from private references and are absent from task packets,
logs, state, audit details, and model output. Switching provider does not alter
policy, authority, or replay identity.

Before delivery the resident persists a per-question answer audit containing
only the public coordinator identity, deterministic authority digest, answer
digest, bounded answer/evidence, hold, status, attempts, reason code, and public
Central event IDs. The question remains open for human coordinators during the
hold and every final policy/evidence check. Butler answers through `BoardClient`
only after the hold releases; that atomic call computes the authenticated host
binding internally and is the ownership boundary. The binding, bearer token,
provider key, and private paths never enter the model packet or durable audit.
Mechanical admission requires one allowlisted grammar to consume the complete
request. A recognized lookup plus any residual clause escalates, as do all
human-only credential, authority, budget, membership, registry, review, merge,
publish, and release categories.
On restart the bounded refresh replays open or accepted questions. A question
accepted by the same Butler identity is first returned to `open` through
Central's authenticated, accepting-owner-only, idempotent `release` action.
The resident then rereads product evidence and policy and relies on Central's
idempotent answered state to avoid duplicate delivery. A veto, kill,
policy/evidence drift, or delivery failure therefore leaves an open question
available to a human coordinator.
Repeated vetoes or bounded delivery failures automatically fall back from
`autonomous` to `assist`.

## MCP v2 connector contract

Each connector declaration fixes transport (`stdio` or `streamable_http`), MCP
protocol revision, endpoint/command reference, secret reference, exact tool and
resource allowlists, input/output byte limits, timeout, concurrency, and rate
limit. Every tool entry is a typed declaration with `effect=read_only|mutating`,
`replay=safe_with_stable_call_id|never`, and a nullable
`stable_call_id_field`. Safe replay requires a named field; `never` requires
null. The call ID is derived once from board, connector, command/operation, tool
name, and canonical arguments, is persisted before dispatch, and is supplied in
that declared input field. A changed payload under the same call ID is a policy
violation. Unknown protocol revisions and transports fail validation. HTTP must
use TLS except explicit loopback; redirects, DNS rebinding, and credential
forwarding to another origin are rejected. Stdio uses an approved executable
reference, not a shell string.

Discovery is filtered before exposure to the planner. A requested name and
argument object are validated against both the declaration and discovered
schema. Results are capped before full parsing, stripped of untrusted `_meta`
authority hints, redacted, and stored only as bounded evidence. Tool output
cannot add another tool call, change policy, or become a command. Cancellation
and timeout terminate only that call. Reconnect uses bounded backoff. A
mutating tool declared `replay=never` is never retried after an ambiguous
result; one declared `safe_with_stable_call_id` may be queried or replayed only
with the persisted identical stable call ID and canonical bytes.

Risky tools require the separately implemented command/config policy gate from
`TK-9a70ac2d1bfa`; an allowlist alone is insufficient. Connector health is an
observation with last-success and last-failure times, never proof of authority.

## Host executor protocol

`autonomous_butler_executor_v1` defines requests and results. Allowed actions
are `inspect`, `start`, `drain`, and `stop`; there is no arbitrary command.
Requests bind board, operation ID, seat/template IDs, expected seat generation,
deadline, and authorization fingerprint. Every request also carries required
`caller_auth` using `local_ed25519_v1`: allowlisted local `key_id`, single-use
nonce, signing time, canonical request digest, and Ed25519 signature. The digest
is SHA-256 over RFC 8785 canonical JSON of the request with `caller_auth`
omitted. The signature covers the ASCII context `pursers-executor-v1`, a zero
byte, the 64-hex digest, a zero byte, and the UTF-8 `key_id`, `nonce`, and
`signed_at` values separated by zero bytes. The executor accepts
only keys pinned in operator-owned local configuration, rejects timestamps
outside the configured narrow skew window, and durably reserves `(key_id,
nonce)` before mutation. Nonce reuse with different bytes is a policy breach;
an identical `operation_id` plus digest returns the stored result with
`replayed=true`. Result envelopes repeat the verified request digest. Transport
must be an owner-only local Unix socket; TCP and inherited board credentials
are forbidden.

The executor independently loads the approved template, verifies its digest and
local ceiling, uses private paths, and returns opaque process/checkout handles.
Results never include environment, command lines containing secrets,
credential contents, or private paths.

The executor cannot onboard a new principal or issue a credential. A template
must already bind an administrator-created principal and credential reference.
Central remains authoritative for whether a seat is eligible and retired; a
local process existing does not prove board membership, and board membership
does not prove a live process.

## Audit and redaction

Every policy decision, command transition, model/connector reservation,
external call, executor operation, CAS conflict, demotion, and kill transition
emits `autonomous_butler_audit_v1`. Records contain digests and references, not
payload copies. The redaction layer removes authorization headers, cookies,
tokens, keys, credential references, environment variables, private paths, and
provider responses before persistence. Redaction failure drops the optional
detail and records `detail_redacted`; it never stores the unsafe value.

Audit ordering uses Central journal sequence plus record ID. Consumers persist
a positive cursor and de-duplicate record IDs. Retention may compact detail,
but command terminal status, actor, timestamps, policy/envelope digests,
operation IDs, and outcome remain durable.

## Failure, replay, and safety behavior

- Restart: recover positive cursors, command records, reservations, cooldowns,
  operation IDs, kill latch, and demotion state before accepting work.
- Duplicate event: compare ID and canonical digest; reuse the prior result.
- Lost response: query Central, connector (only if idempotent), or executor by
  operation ID before retry.
- Partial scale-up: report `degraded`, stop or quarantine the orphan through
  the executor, and never count it ready until both board and host agree.
- Partial scale-down: preserve minimum capacity; an unknown stop/retire result
  is `degraded` and blocks another reduction.
- Provider or connector failure: fail the one action closed, trip its circuit
  breaker after the configured threshold, and continue other board duties.
- Invalid/stale config or authorization: no mutation; effective `shadow` with
  a reason and audit record.
- Budget/accounting uncertainty: stop new billable work and show
  `budget_exhausted` or `accounting_unknown`.
- Clock uncertainty: deadlines and windows fail closed; ordering relies on
  Central revisions, not host wall-clock order.
- Kill during mutation: let an atomic Central commit resolve, cancel other
  cancellable work, observe the result, and remain `killed`.

## Dashboard contract

Fleet Settings exposes per-board mode, runner, approved templates, desired
min/target/max counts, board and host ceilings, cooldowns, budgets, connectors,
and authorization fingerprint. Team shows desired versus actual capacity and
seat lifecycle. Activity shows commands and redacted audits. Controls create
typed commands with the displayed config revision and require confirmation for
kill, resume, drain, retire, and autonomous enablement.

The UI labels `pending`, `applying`, `degraded`, `killed`, and `auto-demoted`
exactly as returned. It does not optimistically display a desired value as
actual, silently retry a CAS conflict, or hide stale/unknown observations.
Secrets and private references are displayed only as “configured”/“missing”.
Keyboard, focus, status announcement, reduced-motion, contrast, and destructive
confirmation behavior must satisfy WCAG 2.1 AA tests in the Fleet ticket.

## Migration compatibility

1. Schema files and readers land first; unknown versions fail closed.
2. Existing version 1 shadow configuration is read unchanged. New fields have
   no synthetic autonomous defaults.
3. Writers add `autonomous_butler_config_v1` only after all readers support it.
   A write requires the current CAS revision and preserves the separate legacy
   `coordinator_config.board_butler` version 1 fields.
4. New state/command/audit keys are additive. Older Fleet and Butler versions
   ignore them and remain shadow-only.
5. Per-board autonomous enablement requires a fresh human authorization after
   migration. Bulk migration cannot create it.
6. Rollback disables new writes and leaves durable records readable; it never
   converts an autonomous board to an unreported active legacy mode.

## Successor dependency contracts

| Ticket | Consumes | Must produce |
|---|---|---|
| `TK-83cda3b950e2` | question eligibility, model boundary, audit/replay rules | policy-gated answers with hold/veto and deterministic evidence checks |
| `TK-9a70ac2d1bfa` | config, command lifecycle, immutable envelope, CAS rules | durable typed commands/config writes and risky-action policy gate |
| `TK-44fe76f801a1` | connector declaration and isolation rules | provider-neutral MCP v2 adapters, health, allowlists, redaction |
| `TK-24a185575384` | desired/actual model, state machine, ceilings, reconciliation | convergent board reconciler using executor operations |
| `TK-e226eae0e17b` | strict `autonomous_butler_model_v1` request/result contract | direct API and ACP adapters with identical policy boundary |
| `TK-3d68c7f28774` | executor schema and lifecycle limits | host-local idempotent inspect/start/drain/stop service |
| `TK-f0c240e31c94` | all schemas and dashboard truth contract | CAS-safe Fleet surfaces, controls, accessibility and secret-redaction tests |
| `TK-e15da805bb28` | all independently approved implementations | end-to-end proof from ticket arrival through independent review |

Successors may add implementation-specific fields only by publishing a new
schema version and documenting compatibility. They must not silently widen an
enum, relax `additionalProperties`, or repurpose an existing field.

## Required test strategy

Unit/property tests validate every schema, unknown-key rejection, bounds,
cross-field counts, immutable-envelope checks, and redaction corpus. State
machine tests cover every legal transition and reject terminal reopening,
stale CAS, changed idempotency payloads, ceiling increases, and false success.
Contract-negative fixtures additionally reject autonomous mode without the
required enable/authorization pair, model results without measured usage,
connector safe replay without a stable call-ID field, executor requests without
caller authentication, nonce or signature, and command snapshots without the
complete prior/current transition record.

Integration tests use fake direct-API, ACP, MCP stdio, MCP HTTP, Central, and
executor servers. They cover cancellation, disconnect after commit, discovery
drift, malformed/oversize data, reconnect, DNS/redirect controls, budget
reservation recovery, concurrent boards, one-connector isolation, process
orphan recovery, and kill at every side-effect boundary. Product-produced
responses/states—not expected-value fabrication—prove consumers.

Fleet browser tests cover all effective states, stale observations, CAS
conflicts, secret/path non-disclosure, confirmations, keyboard-only operation,
focus restoration, live-region status, touch targets, contrast, and reduced
motion. Migration tests start from real version 1 and absent config, proving
both remain shadow until explicit authorization.

End-to-end acceptance uses distinct principals for Butler, worker, reviewer,
and operator; demonstrates capacity within ceilings; answers one eligible
information question; denies a risky connector call; engages kill during an
in-flight action; resumes only with human authority; completes independent
review; and proves Butler never receives work/review/merge/release authority.

## Specification self-review

- Every requested capability has an owning boundary and a versioned schema.
- The current host's 12-agent role ceiling is represented as explicit
  host-scoped runtime data, with separate Butler/coordinator process accounting;
  it is not a product default.
- Every mutation has CAS, idempotency, authorization, audit, and kill checks.
- Desired state is never reported as actual state.
- Existing and absent configurations remain shadow; autonomous activation is
  explicit, board-scoped, fingerprint-bound, and human-created.
- Principal creation, credentials, secrets, authority expansion, review,
  merge, publish, and release remain outside Butler.
- Direct API, ACP, MCP, Fleet, and the host executor cannot bypass the same
  deterministic policy envelope.
- The common model adapter, connector replay declaration, executor caller
  authentication, autonomous enable signal, and command transition evidence
  are strict committed schemas rather than prose-only promises.
- Failure and replay rules cover ambiguous external effects and restart.
- Successor tickets have non-overlapping outputs and named dependencies.
- The design contains no credential value, private host path, version bump, or
  runtime behavior change.
