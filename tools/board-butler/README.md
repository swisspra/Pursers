# Board butler

`board_butler.py` is a single resident coordinator seat that keeps findings
fresh for every active board in `project_registry` and drafts evidence-backed
answers to mechanically checkable coordinator questions. Each bounded refresh
runs the real `tools/coordinator/coordinator.py` derivation in shadow mode; the
butler never manufactures a timestamp or finding merely to satisfy freshness.
Inactive registry projects are not read or acted on.

Question handling is independently configured per board as `off`, `assist`, or
`autonomous`. `off` disables delivery, `assist` preserves the existing
evidence-backed draft, and `autonomous` may answer only an
`information` question whose deterministic class, evidence kind, active window,
rate limits, hold, kill state, and immutable answer scope all pass.
Two ticket actions are permitted, both derived entirely from current board
state and disabled unless the operator opts that board in with
`--act-on-board`: it parks (without canceling) an open ticket after the configured
number of `no_live_candidates` dispatch cycles when the board has no live
`can_work=true` seat, and it records refusal of a proposed escalation target
whose identity cannot work. Every other judgment, including scope, gate,
release, option, and version decisions, remains a draft for a human.

The policy is intentionally fail-closed. A mechanical class must match the
complete request; any residual or mixed clause escalates. Gate waivers, scope
changes, credentials, authority or budget changes, membership changes, and
PR/review/merge/publish/release actions always escalate. Unknown or incomplete
evidence also escalates. Mechanical drafts cite one named product or
repository source: `git merge-base`, `ticket_get`, a ticket annotation, or a
seat capability row.

## Board-state observations

Every registry refresh also runs one bounded observation engine over Central
state the butler already reads. `ObservationContext` contains the current
question inbox, compact ticket and agent rows, and at most 100 full ticket
projections. On the home board it also receives a read-only snapshot of the
host-wide full-gate request directory and process table. `OBSERVATION_RULES` is
the rule registry. Adding another condition means registering another read-only
predicate, not adding another action path. `derive_board_observations` gives
every result a stable key, and `merge_observation_findings` replaces the prior
derived set with a CAS-protected update to `coordinator_findings`.

Observations are warnings, ordered after every critical coordinator alert.
Within the observation set, held and repeated decisions rank ahead of inbox and
scope reconciliation. The state remains bounded to 50 findings and 4,800
characters: older non-critical rows are removed first, truncation is recorded,
and a flood of observations cannot displace a critical alert. If the 100-item
question or ticket-detail projection is incomplete, the butler emits a coverage
gap and makes no negative claim from the missing records.

The current duties are directly traceable:

1. `stale_open_question` pairs an open question only with an explicit question
   or message ID in a later decision. Chronology alone produces a named missing-
   correlation finding, not a reconciliation claim. It asks the coordinator to
   reconcile the inbox; it does not close or answer anything. Code:
   `_observe_stale_open_questions`. Tests:
   `test_stale_open_question_observer_reconciles_explicit_decision` and
   `test_stale_open_question_observer_refuses_chronology_only_match`.
2. `held_decision` finds a binding decision followed by another work offer or
   broadcast in the seven-day replay window, so the decision can travel with
   the ticket before another seat re-derives it. Code:
   `_observe_held_decisions`. Test:
   `test_held_decision_observer_carries_gate_across_later_dispatch`.
3. `standing_decision_repeated` uses only exact board-visible identifiers such
   as a repository path or manifest name. Two different seats asking about the
   same identifier covered by a decision establishes recurrence; free-text
   semantic similarity is never guessed. Code:
   `_observe_repeated_standing_decisions`. Test:
   `test_standing_decision_observer_detects_multiple_seat_relitigation`.
4. `decision_scope_drift` compares a decision's directed file changes with the
   ticket's declared related-file boundary and asks the coordinator to
   reconcile an outside path before final preflight. Code:
   `_observe_decision_scope_drift`. Test:
   `test_decision_scope_observer_flags_directed_out_of_boundary_path`.
5. `observation_replay_metrics` reports the exact number of explicitly linked
   open questions reconciled and deduplicated later question IDs that repeat an
   exact decision identifier within seven days. Test:
   `test_observation_replay_metrics_deduplicate_question_ids`.
6. `coverage_gap` names an incomplete question or ticket-detail projection and
   prevents an absence from being reported as proof. Test:
   `test_observation_coverage_gap_refuses_negative_claim`.
7. `full_gate_queue` reports queue depth, oldest wait, and the inferred active
   holder class at depth 3 or age 15 minutes; depth 8 or age 60 minutes requests
   human attention. Dead request PIDs are ignored and Butler never removes a
   request or kills a process.
8. `unanswered_questions` reports five open questions or a 30-minute oldest
   wait, including questions attached to terminal tickets. Twenty open
   questions or a live-work blocker older than two hours requests human
   attention.
9. `approved_not_landed` reuses `tools/stranded_approvals.py`, including its
   equivalent-content check. An approval not proven landed after one hour is
   reported; 24 hours, or an unknown approval time, requests human attention.
10. `rejection_loop` reports two review rejections and requests human attention
    at three. Terminal tickets (`closed`, `rejected`, `canceled`, `terminated`)
    are excluded from both the alert/overflow count and current fleet rework
    demand. Historical rejection counts remain on the tickets. `role_imbalance` compares unassignable work/review demand with
    idle opposite-role capacity, escalating at eight tickets or any critical
    unassignable review. Per-ticket detail is capped at three rows per signal;
    an aggregate row records any omitted backlog.

`fleet_demand_snapshot` is the machine-readable companion to those nags. On
every refresh it replaces `board_butler.demand_snapshot` in the same
CAS-protected state document with current gate depth,
oldest queue wait and holder class; unassignable work/review counts and ages;
idle role capacity; rejection/rework load; and normalized host load, memory,
and disk headroom. The future reconciler may consume this structure within its
operator-set ceiling; this observer never starts, stops, or changes a seat.

Shadow and active runtime modes observe and report identically. Active mode
adds the two existing, separately authorized mechanical actions and is also a
prerequisite for `answering_mode=autonomous`. No observer deletes files or board
data, merges work, changes membership, assigns a seat, dispatches cleanup, or
performs an operator action.

## MCP v2 connector boundary

`ConnectorDeclaration` and `ConnectorRuntime` implement the provider-neutral
client boundary for the canonical `autonomous_butler_config_v1` connector
shape. A declaration is scoped by the runtime's exact board and project IDs and
pins one of two transports (`stdio` or `streamable_http`) plus protocol revision
`2026-07-28`. Endpoint and credential values do not appear in the declaration:
trusted host code resolves the opaque `endpoint_ref` and `secret_ref` only while
opening a connection. Construction also requires the human-owned immutable
envelope's approved connector IDs; an unapproved declaration is rejected.

The runtime uses the Python MCP v2 `Client` for both transports. Stdio receives
an executable and argument vector, never a shell command, and suppresses the
untrusted child stderr stream. Streamable HTTP requires TLS except on explicit
loopback, rejects URL credentials, query strings, and fragments, ignores ambient
proxy settings, and disables redirect following so credentials cannot move to
another origin. Hostnames are resolved to public addresses (or canonical
loopback), pinned for the runtime, and connected by IP while preserving the
original TLS hostname and `Host` header. A changed resolution, private/special
destination, loopback escape, or cross-origin request fails closed. Both stdio
newline frames and HTTP JSON/SSE frames are byte-bounded before the MCP parser
or model construction runs. HTTP requests require identity encoding, and any
response that declares another content encoding is rejected before its body is
consumed. Unsupported transports and protocol revisions fail validation before
connection. The MCP v2 streamable HTTP client supports a stateless server that
does not issue a session ID while preserving the exact `2026-07-28` revision
check.

The optional `--connector-config /PATH/TO/connectors.json` runtime file must be
an owned, non-symlink, mode-0600 JSON file. Secret files referenced by it must
also be absolute, owned, non-symlink mode-0600 files. The config binds endpoint
and secret references to declarations, but construction still rejects every
connector ID outside `approved_connector_ids` (or
`envelope.approved_connector_ids`). Streamable HTTP endpoints may carry bounded
non-secret `static_headers` and multiple `secret_headers`; every secret header
has its own `secret_ref` (resolved through the top-level `secrets` map) or
absolute `file`, plus its own optional `prefix`. A header may set
`"optional": true` and list exact tool names or bounded prefix globs in
`unlocks`; an absent optional secret omits that header and its tool family from
probe expectations instead of failing the connector. The legacy declaration
`secret_ref` with endpoint `secret_header`/`secret_prefix` remains supported.
The versioned, side-effect-free adapter API, canonical fields, migration rules,
redaction behavior, and desired/effective comparison are documented in
[Connector and source configuration contract](../../docs/connector-source-configuration.md).

```json
{
  "schema_version": 1,
  "approved_connector_ids": ["connector:example"],
  "connectors": [{
    "connector_id": "connector:example",
    "protocol_revision": "2026-07-28",
    "endpoint_ref": "endpoint:example",
    "tools_read_only": ["example_search"],
    "tools_risky_mutating": ["example_change"],
    "tools_denied": ["example_admin"]
  }],
  "endpoints": {
    "endpoint:example": {
      "transport": "streamable_http",
      "url": "https://mcp.example.invalid/mcp",
      "secret_headers": {
        "Authorization": {"file": "/PATH/TO/auth.secret", "prefix": "Bearer"},
        "X-Service-Token": {
          "file": "/PATH/TO/service.secret",
          "prefix": "",
          "unlocks": ["service_*"],
          "optional": true
        }
      },
      "static_headers": {"X-Project-Key": "default-project"}
    }
  }
}
```

The same file may declare a bounded `sources` array. Each source names one
declared read-only tool and maps its product response into the generic intake
shape. For example, a SonarQube source can route project keys through the
Central project registry:

```json
{
  "sources": [{
    "source_id": "sonarqube",
    "connector_id": "connector:sonarqube",
    "list_tool": "sonar_search_sonar_issues_in_projects",
    "fixed_args": {"projects": ["example-service"]},
    "items_path": "issues",
    "field_map": {
      "external_id": "key",
      "revision": "updateDate",
      "title": "message",
      "body": "message",
      "link": "url",
      "project_hint": "project"
    },
    "routing": {
      "project_map": {"example-service": "Example Service"}
    },
    "mode": "ask",
    "content_type": "structured"
  }]
}
```

The resident constructs the source poller from these declarations. Shadow mode
records bounded `source-intake-would-ask` findings without changing intake
state; active mode writes the asks. An unavailable connector or invalid source
is isolated as a bounded `unavailable` or `invalid` finding so the resident can
continue refreshing. `--connector-probe` remains strict and exits non-zero for
the same configuration or connection failure.

Operator-owned auto-onboarding policy can include `delivery_policy`,
`delivery_policy_group`, and an explicit `activate_delivery_policy: true`. The
activation option validates the effective policy and remote refs, creates only an
absent owned integration branch, and records the exact policy revision. A new
repository that resolves to batch or branch-only delivery without this flag stays
unregistered with `delivery_setup_required`; it cannot silently use legacy
per-ticket PR delivery.

With `--source-intake-index-file /PATH/TO/private/source-intake-index.json`
the resident runs Butler-managed intake. The configured Butler model (the
`drafting` provider) decides when inputs change whether to pull, how many items, and
from which sources, given the ceiling (`envelope.host_seat_cap` minus intake
work in flight) and the board load; code clamps the answer to that ceiling and
pulls nothing when the model is unavailable. The resident reuses the decision
while source observations, board load, in-flight work, ceiling, and provider
settings are unchanged; source `observed_at` timestamps alone do not trigger
another model call. If every source has a successful observation with zero
open issues, the resident returns `no_open_issues` without calling or resolving
a model. Unknown, invalid, or failed counts are not treated as zero. The cache
lasts for the resident process; a restart needs a fresh decision only when
sources are not confirmed empty. Provider failures defer retries for 15 minutes
unless provider settings change. Decision logs include `model_called` to
distinguish new requests from reused decisions. A source may set `page_arg` and
`max_pages` so already-taken items (tracked in the 0600 index, not in Central)
are paged past cheaply. Writeback runs as a separate pass once a ticket is
closed with an approval, so delivery never waits for a pull. Its
`arg_template` may use `repository_project`, `repository_name`,
`target_branch`, `source_branch`, `approved_sha` and `ticket_title` (e.g. an
Azure DevOps `pull_request_create`); a tool without a stable call id is
attempted once. ADO delivery reconciles existing PRs through the declared read-only
`ado_pull_requests_list` tool, including after uncertain outcomes; it never blindly
repeats a create. Read/preflight failures back off for at least 60 seconds. See
[delivery states and recovery](../../docs/managed-intake.md#approved-delivery-and-remote-sha-preflight).

`--connector-probe` is a one-shot, token-free check. It connects, lists tools,
prints secret-free JSON, and exits non-zero when an enabled classified tool is
missing, an unclassified tool appears, or connection/protocol validation fails.
Classified denied tools are reported separately when advertised, but are never
added to the callable allowlist. Optional-secret families are included in the
comparison only when their header resolved successfully.

```sh
python3 tools/board-butler/board_butler.py \
  --connector-config /PATH/TO/connectors.json \
  --connector-probe
```

Discovery is filtered against the exact declared tool and resource allowlists
before it is returned to a planner. Tool calls are validated against the
discovered JSON Schema and the declaration's byte, timeout, concurrency, and
rate limits. Stable call IDs and the final payload digest are reserved through
the `ConnectorPersistence` seam before dispatch. A mutating tool declared with
`replay=never` is attempted once; a safely replayable tool may reconnect once
with the same reserved ID and bytes. Every risky tool requires an affirmative
`ConnectorPolicyDecision`; an absent or malformed gate fails closed.

Connector output is bounded, strips `_meta`, redacts credential-like fields,
resolved secret values, and private paths, and is returned only as untrusted
data with a digest. Health is an observation, not authority. Success, denial,
failure, and cancellation emit schema-valid redacted connector audit records
through the persistence seam. Failure or cancellation closes only that
connector call, so another board's stewardship continues.
`InMemoryConnectorPersistence` exists for tests and local probes only; a
production integration must provide durable call reservations and audit
storage.

Questions that propose proceeding despite a blocked, skipped, failed, or
never-reached suite use the coverage map declared by `tools/ci_manifest.py`.
The butler compares the submitted cumulative file list with the quoted suite
results. A non-passing suite escalates only when it covers the diff; an
unrelated blocked suite does not turn a docs-only submission into an escalation.

The resident runner acquires an exclusive pidfile lock before reading
credentials or opening Central. One-shot kill-switch and veto commands bypass
that resident lock so they remain operable during normal service; their durable
`coordinator_findings` mutation is still compare-and-swap protected. A second
resident remains lock-rejected and exits nonzero. The runner joins as a
coordinator with `can_work=false` and `can_review=false`, then refuses to run if
another working or reviewing seat shares its principal. Deploy it with its own
credential and state paths; do not reuse a worker or reviewer token.

One-cycle shadow validation:

```sh
PYTHONPATH=packages/client/src \
python3 tools/board-butler/board_butler.py \
  --url https://central.example.invalid/mcp \
  --token-path /PATH/TO/board-butler.jwt \
  --home-board pursers \
  --agent-name board-butler-1 \
  --repo /PATH/TO/Pursers \
  --pid-file /PATH/TO/state/board-butler.pid \
  --cursor-file /PATH/TO/state/board-butler.cursor.json \
  --refresh-seconds 60 \
  --once --dry-run
```

## Fleet service start, status, and emergency stop

The repo-owned fleet entry is `tools/board-butler/launch.sh`. It defaults to
`shadow` and the checked-in
`com.pursers.board-butler.plist.template` pins that mode explicitly. Replace
every `/PATH/TO/...` placeholder in a private staged copy, validate it, then the
operator turns it on with:

```sh
plutil -lint /PATH/TO/private/com.pursers.board-butler.plist
chmod 600 /PATH/TO/private/com.pursers.board-butler.plist
launchctl bootstrap "gui/$(id -u)" /PATH/TO/private/com.pursers.board-butler.plist
```

The template is not installed by the repository or by worker seats. Its
`KeepAlive.SuccessfulExit=false` policy restarts an unexpected crash but does
not loop after a clean fail-closed exit. The launcher publishes only PID, mode,
start time, and last activity to the private mode-0600 `runtime.json`; Fleet
also requires the private singleton pidfile to be locked by that PID and
verifies `/bin/ps` identifies a live, non-zombie `board_butler.py` process
before reporting a running state or sending `SIGTERM`. A stale runtime file is
therefore shown as **Configured · not running**, never as running.

For a single authorized host pool serving several WORK boards, follow the
[shared registry fleet migration, acceptance, and rollback runbook](../../docs/operations/shared-registry-fleet.md).

The Settings page distinguishes **Not configured**, **Configured · not
running**, **Running · shadow**, and **Running · active**, and shows the last
observed activity. **Stop butler now** creates the private local `KILLED`
marker before sending `SIGTERM`. A board write already accepted by Central
remains atomic; an interrupted question leaves its cursor unadvanced and is
replayed. The launchd restart sees the marker, exits successfully before
reading the token, and remains stopped. The operator deliberately resumes
shadow mode by removing that marker and running:

```sh
launchctl kickstart "gui/$(id -u)/com.pursers.board-butler"
```

Active mode cannot be reached through the dashboard config or by changing the
mode field alone. First create the separate owned mode-0600 authorization:

```sh
python3 tools/board-butler/authorize_active.py \
  --output /PATH/TO/private/board-butler-state/active-authorized.json \
  --confirm ENABLE-BOARD-BUTLER-ACTIVE
```

Then the operator stages a plist whose `PURSERS_BUTLER_RUNTIME_MODE` is
`active`, reviews the separately declared active board, and reloads the job.
The resident refuses active mode unless the authorization file is valid and at
least one `--act-on-board` is present. Active mode enables the two mechanical
ticket actions described above. A board still cannot answer questions unless
its resolved `answering_mode` is `autonomous` and deterministic answer scope
authorizes the exact class.

Repeat `--act-on-board` to opt in additional active registry boards. The acting
set is empty by default, and a configured board that is not active in the
registry is reported as ignored. Findings are still refreshed for every active
registry board regardless of the acting set. After a mechanical action, the
same cycle runs the real derivation again, so a newly parked ticket cannot
remain reported as starved until a later event.

The two operator-approved action classes form the default configured class set;
repeat `--active-action` to narrow that set. Every intended action is first
written durably to `coordinator_findings` with a release time and is shown in
Fleet's **Waiting for you** surface. It cannot execute before
`--action-hold-seconds` elapses, and `--veto-question BA-... --control-reason
<reason>` changes the durable hold to `vetoed`. For a non-home board, run the
control command with that board as `--home-board`. Adding another autonomous
class requires adding it to the configured class set; decision questions do not
become autonomous merely because the process is active.

Without `--once`, the process waits in the journal/seat push stream for a
coordinator-question cue, with a bounded timeout used to run the next registry
refresh. There is no status polling or cursor-0 catch-up. The positive cursor
file is reused across restarts; a zero or invalid cursor starts at the current
journal watermark. Pure typed transport failures, including nested exception
groups, reconnect up to three times from the last observed cursor. Mixed groups,
authentication, authorization and invalid event data fail closed. Retry and
exhaustion are recorded in the bounded `board_butler_subscription_health` state;
the resident then continues its normal refresh cycle without resetting the cursor.
A pure typed transport failure during registry refresh is recorded in the local
runtime status and skips directly to the next scheduled cycle instead of replaying
a refresh that may already have completed durable or mechanical work. Mixed,
authentication, authorization and invalid-data refresh failures still terminate
the resident fail-closed.
`--dry-run` prints the real derived state and any proposed question finding,
performs no ticket action, and makes no Central write or cursor-file update.

## Declared configuration

`coordinator_config.board_butler` is a strict versioned document. Unknown or
invalid keys fail closed and queue the question for the coordinator. Effective
settings resolve in this documented order: safe defaults, `global`, the named
`projects` override, then the `boards` override. Later layers change only the
keys they declare. The runner resolves the project name by matching its home
board in `project_registry`; `--project` is only the explicit fallback when a
registry row is unavailable or ambiguous.

```json
{
  "board_butler": {
    "schema_version": 1,
    "global": {
      "mode": "shadow",
      "answering_mode": "assist",
      "answer_scope": {
        "ancestry": "escalate",
        "ticket_status": "escalate",
        "seat_capability": "escalate",
        "waiver_applicability": "escalate",
        "corpus_lookup": "escalate",
        "coverage_check": "escalate"
      },
      "required_evidence_kinds": [
        "git_ancestry", "ticket_status", "annotation",
        "seat_capability", "manifest_coverage", "corpus"
      ],
      "ceilings": {"per_hour": 5, "per_ticket": 2, "per_board": 20},
      "hold_before_post_s": 3600,
      "active_windows": [
        {"days": ["mon", "tue"], "start": "00:00", "end": "06:00", "timezone": "UTC"}
      ],
      "kill_switch": true,
      "auto_demote": {
        "veto_count": 3, "failure_count": 3, "window_s": 3600
      },
      "classification": {
        "model": null, "endpoint_ref": null, "key_ref": null
      },
      "drafting": {"model": null, "endpoint_ref": null, "key_ref": null}
    },
    "projects": {"Pursers": {"ceilings": {"per_hour": 4}}},
    "boards": {
      "pursers": {
        "mode": "active",
        "answering_mode": "autonomous",
        "kill_switch": false,
        "answer_scope": {"ticket_status": "auto"},
        "required_evidence_kinds": ["ticket_status"],
        "hold_before_post_s": 7200
      }
    }
  }
}
```

The class list is not a general permission switch. `scope_change`,
`gate_waiver`, `release`, `membership`, and `registry` are permanently
escalation-only; validation rejects attempts to set them to `auto`. An empty or
disabled evidence floor is also rejected, as are self-review, merge-to-main,
and inline API-key fields because none belong to the schema. Model and endpoint
are configuration; `key_ref` is an opaque `file:<id>.key` reference into the
private 0600 provider secret directory, never a home path or credential value.
Provider entries may also contain bounded
non-secret `extra_headers`, `key_header`, `key_prefix`, and a relative
`validation_path`. The relative `draft_path` and explicit `draft_protocol`
select the draft contract. Existing and omitted settings remain
`pursers_json_v1` with `draft_path=draft`. To use a standard OpenAI-compatible
gateway such as LiteLLM, select `openai_chat_completions_v1` and set the path to
`chat/completions` when the endpoint already ends in `/v1`.
Fleet validates and saves these settings, then the resident
re-resolves them and reads the referenced key at the start of every question
cycle. No process restart or hand edit is required.

When the drafting provider is configured, the resident sends one bounded
request to the configured relative draft path for each question that clears the
local rate limits. `pursers_json_v1` sends `protocol`, `model`, an `input` object,
and `max_output_chars`, and reads a string `draft`. The opt-in
`openai_chat_completions_v1` contract sends the exact model, bounded system/user
messages, and `max_tokens`, then reads `choices[0].message.content`. Both
contracts use the optional headers and credential read from `key_ref`; secret
bytes never enter the prompt. Responses and draft text remain bounded, and
cross-origin redirects are refused before credentials can be forwarded.
Deterministic policy and evidence still set the verdict, and the provider supplies
only bounded phrasing. A provider failure or a response that contains the
credential fails closed to a fixed, key-free escalation message.

## Provider-neutral autonomous model runner

`AutonomousModelRunner` is the stable execution boundary for the strict
`autonomous_butler_model_v1` envelope. It takes one `ModelBackend`, an exact
`TaskSchemaRegistry`, a board policy-digest reader, opaque-token cancellation,
and a `ModelResultStore`. Direct API and ACP therefore receive the same bounded
request and return the same normalized result. The runner validates the task
input digest, current policy digest, deadline, exact task-schema digest,
proposal byte cap, citations, measured token/cost arithmetic, and reservations
before it releases a proposal to deterministic Butler policy. A caller's
deadline can shorten a run but cannot raise the adapter's 600-second hard cap.

`DirectAPIModelBackend` requires the reviewed explicit
`openai_chat_completions_v1` `ProviderRuntime`. It reuses the same proxy-free,
same-origin, bounded provider transport as shadow drafting, sends the exact
configured model, and never places the credential or cancellation token in the
prompt. `ACPModelBackend` uses the existing ACP v1 client with an absolute
pre-approved session root, an empty MCP-server list, a minimal environment, and
deny-by-default permission handling. Before launch it canonicalizes that root
and applies the production ACP seat's macOS process sandbox. Writes and process
scratch are confined to the session root; optional read roots and protected
files must be supplied explicitly. If the OS sandbox cannot be established,
the run fails closed. An ACP tool or permission request cannot grant filesystem
or MCP authority through this adapter.

Both backends normalize to `model`, `proposal`, `citations`, measured `usage`,
and `provider_request_ref`. Direct API usage and request identity come from the
provider response envelope, not model content. ACP requires a separate host
`pursers_model_usage` update; the agent message cannot self-report usage.
Missing measured usage,
changed model, malformed output, unknown citations, policy drift, timeout,
provider crash, and cancellation all produce a typed result with
`proposal_json=null`. No provider exception text is persisted. Use
`FileModelResultStore` under the Butler's private state directory for production:
it writes a mode-0600 result atomically before replying, returns the stored
result for an identical replay, and rejects a changed payload under the same
request ID. Selecting a backend does not rename or restart the Butler seat and
does not change its board scopes, work/review eligibility, concurrency, or
budget ceilings.

The three draft ceilings are real queue boundaries. A hit produces a
`butler_queued` finding with an `ESCALATE` verdict instead of dropping the
question. The default hourly value reuses `intake.rate_per_hour` when that
value is valid. Every finding reports the fully resolved settings and their
source layers.

Each draft contains a durable hold record with draft, release, and veto times,
so restarts do not reset the timer. Held drafts are projected into Fleet's
**Waiting for you** surface. Record a veto (including its required reason) or
engage the immediate kill switch with one-shot control commands:

```sh
python3 tools/board-butler/board_butler.py <normal arguments> \
  --veto-question CQ-example --control-reason "evidence is stale"
python3 tools/board-butler/board_butler.py <normal arguments> \
  --kill-switch --control-reason "operator incident"
```

Vetoes and bounded answer failures inside `auto_demote.window_s` are counted
from durable control state. Reaching either configured threshold demotes
`autonomous` to `assist` and reports the reason. Active windows accept IANA
timezones and weekday names (`mon` through `sun`), including overnight windows.

Autonomous delivery first persists the deterministic authority digest, answer
digest, evidence citation, hold, and coordinator identity in the per-question
evaluation record while leaving the Central question open for a human throughout
the vetoable hold. At release time Butler rereads the question, config,
kill/demotion state, veto state, and product evidence, then uses `BoardClient`
to make the atomic answer call. The client computes Central's host binding
internally, so neither the model request nor the audit record contains the
binding or credential. Any drift or delivery failure leaves the question open
and falls back to `assist`. Central's answer operation is idempotent, and the
per-question audit preserves the answered event ID across restart.

Question handling is replay-safe. The durable cursor advances only after the
finding write succeeds. Each bounded registry refresh replays open or accepted
coordinator-owned questions. A legacy/self-accepted replay first uses Central's
authenticated, owner-only `release` action to restore the question to `open`
before any veto, kill, policy, evidence, hold, or delivery exit. A crash after
the finding then resumes the same plan without retaining ownership or answering
twice. Rate ceilings are
enforced before provider work, output is bounded to 2,000 characters, and the
deterministic fallback consumes no model budget.

Run the suite with:

```sh
python3 -m pytest -q tools/board-butler/tests
```

## 2026-09-16 backlog replay

The authoritative correction `AN-000000001056` identifies 27 coordinator
questions, not 29. The worker-visible Central response returned the exact
question text, kind, and recorded answer for three records on
`TK-02bf4d01d662`; the other 24 ticket projections omitted their question
records. The committed fixture therefore replays those three records and names
all 24 unavailable ticket/question pairs without inventing messages or answers,
as the correction explicitly permits for a truthfully partial corpus.

The available replay classifies zero questions as `MECHANICAL`, three as
`ESCALATE`, and none as `UNKNOWN`. All three match their recorded disposition;
`CQ-53524d65cdb51016` escalates because a mechanical status fragment cannot
launder the residual document decision.

### Declared source counts

A source can declare an optional `observation` block. Butler calls the declared
read-only tool through the normal connector limits before deciding intake:

```json
"observation": {
  "read_tool": "sonar_issues_search",
  "arguments": {"pageSize": 1, "pageIndex": 1},
  "count_path": "paging.total",
  "max_age_s": 120
}
```

Include the same project and open-status filters used by the source's `fixed_args`
in the observation arguments. The tool must be declared `read_only`; names and
arguments depend on the server schema. This example deliberately omits severity:
a combined-severity source is useful when an upstream server's severity mapping
is incompatible. Pursers does not translate severity enums implicitly.

When every enabled source returns integer zero, intake returns `no_open_issues`
without resolving model credentials or issuing a model request, including after
restart. Missing, stale, invalid or failed observations are unknown, never zero.
`max_age_s` is bounded to 1–120 seconds (default 120). Without this optional block,
existing sources remain supported with unknown counts. Unchanged semantic inputs
reuse the model's previous successful decision; provider failures back off for
15 minutes. Board refreshes and read-only source checks can continue during that
interval. Approval writeback runs independently of the intake decision.

## Managed intake runtime

See [configuration, feature behavior, migration and rollback](../../docs/managed-intake.md)
for `tools/board-butler/deployment.py`, `tools/seat-kit/event_seat.py` and
`tools/ado-connector/git_credential.py`.
