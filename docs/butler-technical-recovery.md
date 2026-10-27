# Butler technical recovery and supervision policy (v1)

This is an opt-in, per-project extension to the authoritative `project_registry`
row. Merely deploying the resident or setting local `--runtime-mode active` does
not opt a project in. Omit the key for the unchanged legacy behavior. Operators
must explicitly install and activate the policy in their normal protected
configuration process; this ticket does not edit live configuration.

```json
{
  "butler_recovery_policy": {
    "schema_version": 1,
    "mode": "active",
    "activation_id": "operator:<opaque-activation-id>",
    "project_id": "<exact registry project name>",
    "board_id": "<exact registered board id>",
    "operations": [
      "technical_hold_resume",
      "bounded_runner_recovery",
      "dispatch_reconcile",
      "technical_question_answer"
    ],
    "sweep_interval_s": 300,
    "project_timeout_s": 120,
    "max_projects_per_sweep": 4,
    "page_size": 50,
    "max_no_progress_attempts": 3,
    "retry_backoff_s": 300
  }
}
```

All keys except `project_timeout_s` are required; unknown keys are rejected.
An omitted `project_timeout_s` defaults to 120 seconds. Explicit project overrides
remain unchanged. This deadline bounds a complete technical-recovery project
sweep, including selected reads, decisions and fresh-state checks; it does not
extend every network or provider request timeout. Keep the default at 120 and
tune a project's value only from measured complete-sweep latency, up to 300.
Increasing time does not increase attempts, seat caps or authority.
Names and board must match
an active registry route; `activation_id` is an opaque, explicit operator
activation marker. Numeric fields are bounded (sweep 30–86400s, project timeout
1–300s, page size 1–100, projects per sweep 1–32, no-progress attempts 1–20,
backoff 1–86400s). Empty/absent policy means no authority. Policy grants only
its declared operations on the exact project. It grants no board-admin role,
credential/permission change, customer protected-branch write, fabricated
approval, independent-review bypass, final QA waiver, production SSO inference,
or human decision authority by itself.

The resident's event subscription is a fast wake-up path; a bounded periodic
reconciliation sweep remains mandatory because event delivery can be missed and
questions can stall. Per-project bounded/paged reads must use canonical Central
APIs. Maintain a fair round-robin cursor, isolate each project timeout, and
initialize due times for every eligible project so a bounded batch yields to
omitted due projects on the next run-loop iteration. Preserve each project's
sweep interval and failure backoff. Record successful-observation freshness
separately from actual work progress and PID/service liveness. Any partial
projection/read failure is `degraded`,
never a healthy empty board. Every observed nonterminal ticket must carry a
current wait/action reason (for example live lease, independent review,
dispatch/dependency, real human request, or exhausted recovery policy). Idle
sleep is not supervision.

Both human-request and technical-question decisions receive the same read-only
workspace evidence before cache selection and again before any answer or resume.
The reader verifies the registered fleet clone, exact existing ticket worktree,
branch and Git common directory. It never creates a worktree, switches branches,
fetches source or runs project code. The coordinator's `work_dir` can differ from
the registered `fleet_clone_dir`. Missing, foreign or symlinked workspaces fail
closed. Git filesystem-monitor hooks and optional index writes are disabled.

Generated facts include the actual owned path, HEAD and a bounded digest of
source changes. Untracked diagnostics and runtime heartbeat files do not count
as source progress. Raw source and logs are not copied into the model context;
full ticket storage limits remain unchanged. A runner checkpoint without a
registered trustworthy location is explicitly unavailable, never fabricated.

The nonterminal work page intentionally omits closed tickets. The resident
separately reads at most eight registered same-workflow references, prioritizing
prerequisites and then other cohort members such as an accepted test harness.
These exact targeted reads support both model paths; they never load all closed
board history. A candidate SHA is independently accepted only when the closed
verdict matches the submission SHA and records a distinct reviewer principal.

Within an opted-in policy, verified owned-workspace diagnostics and preparing
independently accepted same-project dependencies in that owned branch are
ordinary technical continuation. Butler need not ask a human to choose an
already verified owned diagnostics path. Other worktrees, old candidates,
protected customer branches, independent review and final combined QA remain
protected. Unknown external SSO, business and security contracts still require
human input. Changing substantive workspace/source evidence invalidates a stale
recommendation; lease and unrelated dispatch activity do not establish progress.
Every paid question attempt reserves the shared no-progress budget, including
retries of the same question ID. An unchanged superseded recommendation reuses
its durable proposal through all current authorization, source and exact-question
fences; it never buys another decision. If the fence still fails, the question
stays open. Unknown answer outcomes remain receipt-only. Evidence timestamps and
list ordering do not replenish budgets; genuinely changed source or independent
review evidence can permit another bounded decision. The four most recent unique
scoped facts remain selected before canonical sorting, and registered workflow
references retain priority within the eight-item evidence limit.

Only exact, currently pending human-request IDs may ever be resolved. For
structured requests, the resident supports a bounded flat object subset with
string properties (optional enum/length constraints) and boolean properties
(optional boolean-only enum), plus optional or required fields. It validates a
nonempty exact answer before calling Central; boolean values must be JSON
`true`/`false` (never strings or numbers). Unknown fields are rejected even when
the request omits `additionalProperties`. Nested, numeric, malformed or oversized
schemas remain held. Schema shape is transport,
not authority: missing external SSO registration, security decisions and unknown
business contracts remain
human decisions. Typed Central content contains only schema fields; operation
identity and own-principal resolution receipts are recorded in durable Butler
state. A lost response is reconciled against exact Central content and actor;
restart reuses the persisted recommendation and never pays the provider twice.

Refetch
the full ticket and verify status, request ID, unresolved resolution and
project/board route immediately before a mutation. Use the configured model only
for a changed substantive decision, not for status classification. Bound input
to current request plus relevant evidence summaries; retrieve detailed evidence
separately. Cache recommendations by canonical digest of board/project/ticket/
request/evidence/policy, persist typed recommendation and resolution receipt,
and replay without a second provider call or duplicate mutation. Honor timeout,
retry backoff and no-progress caps. Schema/authority/scope fences remain
mechanical and deny unknown facts; real SSO registration and unsafe or expanded
scope remain with a human. Never infer approval or passing tests from keywords
or free-text claims.

Tests should use disposable Central and fake providers/clocks. Exercise lost
wakes plus idle backlog, paging/fairness when one project stalls, incomplete
projection degradation, unchanged-input no-repeat provider/replay behavior,
real human hold preservation, exact-request refetch and mutation, and unchanged
unconfigured behavior. Do not test against customer MCP or paid production
providers.

## Resident execution and cost controls

`CentralBackend.__aenter__` starts a separate supervision task only in active,
non-dry-run mode without a kill switch or veto. Its timer does not depend on new
board events or a successful legacy refresh. Shutdown cancels this task before
closing authenticated clients. Each selected project reads one bounded `work`
page; positive cursors continue on later ticks. A page is routed through the
shared `resolve_registry_target` contract before any detail read or decision.
Only a matching project with an explicit valid policy may act. One malformed
policy is reported for that project and does not disable another tenant.

`technical_hold_resume` permits a typed recommendation about an existing pending
hold. `bounded_runner_recovery` additionally permits recommendations to resume
an interrupted runner. `dispatch_reconcile` records canonical dispatch wait
reasons; the existing Central reaper and workflow controller retain ownership
of lease reconciliation and dependency release. It does not authorize direct
holder edits or stealing an active lease.

`technical_question_answer` is a separate opt-in for ordinary pending worker
questions. Butler answers the exact question using current ticket requirements
and bounded same-project source/review summaries. A candidate SHA is distinct
from an independently accepted SHA. The worker may retain its work lease while
receiving an answer; this operation changes neither that lease nor the ticket's
work or review state. Missing external SSO registration, security or business
decisions and final approval remain escalated.

Question decisions and exact own-principal answer receipts are persisted before
and after mutation. A restart reuses a durable decision without another model
call. An ambiguous answer RPC is reconciled against the exact question, message
and answering principal. Worker lease renewal and dispatch heartbeats are not
decision evidence and do not invalidate the model cache. Repeated identical
questions under new IDs share a bounded no-progress budget; a provider/model
change cannot replenish it. Changed substantive requirements, source or review
evidence allow a fresh bounded continuation. Complete stored questions and
tickets remain available through their detail references.

The approved drafting provider receives one selected request and requirements,
plus the latest two authorization and evidence annotations of each kind. The
outbound JSON contract explicitly enumerates `decision` (`resume` or `escalate`),
allowed categories and required bounded text; typed resume requests also require
one schema-valid `answer` object. Mechanical validation rejects mismatched
categories, unknown decision values, malformed answers, extra keys and empty
text before mutation. The selection declares older omitted history and
references full `ticket_get`; original ticket data is unchanged. A context above
18,000 UTF-8 bytes becomes `detail_required` rather than silently truncated.
The configured chat request uses a finite 2048-token completion ceiling and
low reasoning effort, leaving practical budget for a final JSON object on
reasoning models. A `length` finish, empty response or malformed output fails
closed without mutation and records only bounded response ID, finish reason,
character/token counts, never prompt or response text. Empty boards and
unchanged escalated requests use no model. Unknown usage/cost is not reported as
a measured zero.

Per-project supervision, per-request decisions, and per-ticket recovery budgets
are separate durable state keys. Compare-and-set reserves a decision before a
paid call. Provider failures back off; restart reuses a stored recommendation.
The per-request input digest includes the configured provider model, protocol
and recovery contract version, so a semantic provider/contract correction can
invalidate a cached malformed-output attempt and permit one fresh bounded
provider attempt. Process restart and observation heartbeats do not change that
digest or reset attempts. This per-request invalidation does not reset the
separate per-ticket no-progress budget: unchanged requirements and evidence
allow at most `max_no_progress_attempts` automatic resumes, regardless of
provider-only model, protocol or contract changes. Only changed substantive
requirements or evidence reset that budget. The native atomic request ID fences
resolution, and a lost response is checked against the exact operation content
and Butler principal. Resuming work grants no review verdict.

Before examining each pending hold the supervisor persists its ticket ID and
page cursor. If the per-project deadline cancels a slow paid call, the next fresh
read of that same bounded page continues strictly after the last attempted hold;
unread rows and later pages remain eligible. A vanished marker on a changed
page causes a fresh start of that page, not a skipped projection.

The current full request and registry policy are fetched again immediately
before `ticket_human_resolve`. Unsupported answer schemas, explicit approval
requests, uncertain leases,
changed routes or requests remain held. The model may escalate a real domain
question. Successful observation time, complete traversal time and actual
recovery progress are recorded independently; a running process alone is not
reported as healthy. A traversal spanning multiple pages reports `catching_up`
until observed counts match the canonical terminal page.

To disable new automatic actions, remove the project's `butler_recovery_policy`
through the normal registry CAS process. Existing leases, accepted work and
human decisions remain intact. Keep the previous runtime and registry revision
for rollback; disabling policy is not a reason to reset the board.


Typed requests are a transport format. The supported subset accepts bounded flat
string properties with optional enum/length constraints and boolean properties
with an optional boolean-only enum; fields may be optional or required. Boolean
answers are strictly JSON booleans (not integer or string coercions). Omitted
`required` or `additionalProperties` follows the request contract. Answers still
contain only declared fields and must be nonempty. Field names are not a fixed
list of project-specific templates. Explicit approval requests remain held. The
adviser distinguishes the current technical question from a missing external
business or SSO contract; words in a title or a preservation constraint do not
automatically prevent scoped repair or publication for independent review.
