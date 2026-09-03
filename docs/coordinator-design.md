# Coordinator Control Seat: Phase 0 Design

## Purpose and boundary

The coordinator is an optional control seat above the existing worker loop. It
observes every active board in the home board's project registry, proposes or
performs bounded coordination actions, and gives the operator one place to see
what needs attention. It is not a scheduler in the critical path: workers keep
using `a2a_wait`, `ticket_claim`, `lease_renew`, `ticket_submit`, and independent
review exactly as they do today when the coordinator is absent.

The coordinator never edits project files, executes ticket work, reviews work,
merges branches, or handles credentials. Its runtime receives an already
provisioned capability from the host's secret store. Provisioning, rotation,
revocation, and inspection of that capability remain operator-only actions.

## Identity and authority

Use a dedicated principal, not a worker or reviewer principal, and a stable
coordinator seat name. The operator admits that principal as `member` on the
home board and every active registry board. `member` is sufficient for the
read model and ordinary ticket creation. `reviewer` is inappropriate because
the coordinator must never approve work, and `admin` grants unrelated powers
over admission, roles, scrub policy, review policy, and other workers' claims.

Least privilege differs by phase:

| Phase | Board role | Runtime capability | Allowed use |
| --- | --- | --- | --- |
| 1 | `member` | `board:read` only | Non-joining reads, detection, and reports |
| 2 | `member` initially | `board:read` + narrowly authorized coordination writes | Dispatch, expiry reap, and nudge |
| 3 | Same as phase 2 | Adds policy-gated `ticket_create` | Structured intake |

The current role set is enough for phase 1. It is not a safe authorization
model for phase 2: a `member` cannot atomically assign an existing ticket or
release another seat's unexpired claim, while `admin` is much broader than the
job. Before phase 2, add a narrow coordinator/dispatcher capability or role
that grants only the new assignment and nudge operations described below.

Phase 1 should use the non-joining read path used by `fleet_snapshot`.
`board_catchup(..., touch=false)` is the side-effect-free journal refetch for
subscribed seats: it preserves member activity, ticket leases, expired claims,
and the persisted consumer cursor even when `ack=true`. The default
`touch=true` behavior remains the compatibility path. Central does not reap a
seat merely because it is silent on a subscription, so no separate agent
heartbeat is required; a seat holding work keeps that work alive with the
existing `lease_renew` tool at its TTL-derived cadence. Phase 1 still reads the
registry through raw, non-joining
`board_state_get(key="project_registry")`, equivalent to the `RawBoardReader`
path behind `fleet_snapshot`, because starting the wait bridge joins a home
board seat. Cursor-based catchup begins only in phase 2, which may use
`a2a_wait(boards="registry")` with one cursor per board.

Committed events continue to publish the board journal cue. Events with exact
recipients also publish `board://<board>/agent/<agent_id>` after commit;
assignment, nudge, and review-result recipients are narrowed to their target
or ticket participants. Subscription authorization permits a principal to
listen only to its own agent IDs, while the journal remains board-member
scoped.

### Availability model

- Workers never call through the coordinator, so coordinator downtime cannot
  stop claiming, execution, submission, or review.
- A phase-1 standby reconstructs state from raw, non-joining
  `board_state_get(key="project_registry")`, bounded board snapshots, durable
  tickets, and its local comparison state. It keeps no journal cursor and has
  no resync dependency. In phase 2, a standby additionally restores per-board
  cursors; if one falls behind journal compaction, `resynced=true` triggers a
  full snapshot refresh.
- Phase 1 may run multiple read-only observers. Phases 2 and 3 use one active
  writer and a cold standby. There is no current compare-and-set leader lease,
  so automatic active-active writes are unsafe. A coordinator leader lease is
  a missing primitive; manual failover is required until it exists.
- Separate standby principals are preferred. If seats share one principal,
  their names remain attributable but compromise and revocation share one
  security boundary.

## Read model and invariants

At the start of every phase-1 cycle, use the raw, non-joining
`board_state_get(key="project_registry")` path equivalent to
`RawBoardReader`/`fleet_snapshot`. Do not call the wait bridge's
`project_registry_get` in phase 1, because starting that bridge joins a seat.
Ignore paused entries, deduplicate board IDs, and retain each active entry's
absolute `work_dir` only for read-only integration checks. Never infer a work
directory from a ticket. Phase-1 polls keep local comparison state only: no
journal cursor, catchup, or resync dependency. Phase 2 introduces cursor-based
catchup after its write-capable seat and safeguards are authorized.

For each board, build a bounded materialized view from:

- `fleet_snapshot`: active registry projects; open, claimed, and submitted
  counts; pooled seats grouped by principal and name; busy, available, and
  stale classification; current ticket IDs; truncation and unavailable-board
  warnings.
- `board_status`: exact status counts, agent projection, review policy, and
  latest journal sequence.
- `board_snapshot`: bounded agents and tickets with an exact journal splice
  watermark and explicit omitted counts.
- `ticket_list` and `ticket_get`: current ticket bodies and durable submission,
  rejection, abandonment, and review history.
- `board_catchup` or `a2a_wait`: phase-2 wake cues only. Events are cues to
  refetch current state, never the authoritative state themselves.
- Project memories, checkpoints, and handoffs: context for reports, not a
  substitute for ticket state.

Never make a write decision from a truncated snapshot, an unavailable-board
row, a registry warning, or an event alone. Refetch the full ticket and verify
its status immediately before any mutation. Keep a local decision ledger with
input watermark, proposed action, reason, outcome, and retry key; do not put
credentials or project content in it.

## Responsibility loops

### 1. Dispatch and rebalance

The existing worker pool already performs safe pull-based dispatch: open work
wakes pool seats, and `ticket_claim` arbitrates races. The coordinator adds
fairness and explicit routing without replacing that path.

1. Read the active registry and `fleet_snapshot`.
2. For every board, refetch `ticket_list(status="open")` and a bounded
   `board_snapshot` for live seats.
3. Exclude handed-off, stale, busy, non-member, and wrong-role seats. Prefer an
   available seat already present on the ticket's board. Rank tickets by
   priority, then age; apply operator-configured board weights only after
   critical work.
4. Detect starvation when a board has eligible open work but no recent claims,
   while another board has available cross-board seats. Record a proposed
   ticket-to-exact-`agent_id` mapping and its reason.
5. Refetch the ticket and seat immediately before dispatch. Apply only if the
   ticket is still open and the seat is still eligible.
6. Return to `a2a_wait(boards="registry")` with the returned cursor map. A lost
   assignment race is normal; refetch and recompute rather than retry blindly.

`ticket_create(assigned_to=<exact agent_id>)` can route a newly created ticket.
There is no existing mutation to assign or reassign an already-open ticket.
Cancel-and-recreate would split history and is forbidden. Phase 1 therefore
reports proposed dispatches only. Phase 2 requires an atomic `ticket_assign`
primitive with expected current status/assignee, exact agent ID, reason,
idempotency key, and a journal event. It must not let the coordinator claim the
work on a worker's behalf.

Rebalancing changes assignments, not board membership. Admission and role
changes remain operator actions. If no eligible seat exists, the coordinator
reports capacity starvation rather than admitting or moving principals.

### 2. Babysit claims and leases

1. Read claimed/in-progress tickets and their holder, `lease_expires_at`,
   `lease_renewed_at`, `abandoned_count`, and last activity from the snapshot.
2. Classify them deterministically:
   - **healthy**: holder is live and lease has adequate remaining time;
   - **at risk**: holder is stale or the lease is inside the configured warning
     window;
   - **expired**: server time is past `lease_expires_at`;
   - **repeat abandoner**: abandonment count crosses the operator threshold.
3. For at-risk work, issue one deduplicated nudge and wait through a grace
   window. A project memory is not sufficient: `memory_written` is deliberately
   ignored by the wait bridge and is not a targeted wake-up.
4. For expired work, refetch the ticket and call `board_reap`. The server, not
   the coordinator, decides whether the lease is actually expired and releases
   only pre-submission states. Submitted and closed work remains durable.
5. Escalate repeated abandonment, missing seats, or a failed reap to the
   operator. Never force-release an unexpired claim.

There is no targeted nudge/acknowledgement primitive today. Phase 2 needs a
bounded `agent_nudge` or ticket-attention event with recipient agent ID,
ticket ID, reason code, dedupe key, expiry, acknowledgement, and rate limit.
`ticket_unclaim` is not a substitute: it is limited to the current claimer or
an admin, and giving the coordinator admin would violate least privilege.

### 3. Human intake

The LLM sits at the language boundary only. It converts the operator's sentence
into a candidate intent and asks for missing meaning. A deterministic intake
validator owns the final board, target, scope, and schema.

1. Resolve candidate boards from the active registry; never invent a board or
   work directory.
2. Let the LLM propose title, description, acceptance evidence, and useful
   context. Treat all proposed values as untrusted input.
3. Deterministically require the generated-ticket contract: non-empty
   `description`, routed `target_url`, one of the supported scopes, and
   non-empty `required_fields`. Validate priority, forbidden actions, tags,
   related files, and an optional exact assignee.
4. Apply the operator approval matrix. Ambiguous routing, external sends,
   destructive work, access changes, credential/security work, merges, and
   other high-impact scopes require confirmation. Missing information remains
   a draft, never a guessed ticket.
5. Refetch for likely duplicates, then call `ticket_create` once. Store the
   returned ticket ID and show the exact created payload to the operator.

Generated IDs enforce the core rich-ticket fields, but there is no idempotency
key or semantic duplicate guard. Phase 3 requires an intake idempotency key so
a retry cannot create duplicate work. Required-field labels are also not
validated against structured submission values; that gap matters to reporting
and integration watch.

### 4. Integration watch

Closing a ticket proves review, not integration. The read-only detector runs
independently from review:

1. List recently closed tickets and inspect their latest submission history.
2. Resolve the board's registered `work_dir`; read no other tree.
3. Extract the submitted commit reference. In phase 1 this may require a
   conservative parser over the required-field summary. If the reference is
   absent or ambiguous, report `unknown`, never `unmerged`.
4. In the local repository, verify that the object exists and run a read-only
   ancestry check against the operator-configured integration ref.
5. Classify `integrated`, `closed-unmerged`, `missing-ref`, `unknown-policy`, or
   `repository-unavailable`. Surface age and ticket/branch/commit evidence.
6. Deduplicate locally and continue reporting until the ancestry check passes
   or the operator records an explicit exception.

The registry does not carry an integration ref, submissions do not expose a
structured artifact/commit field, and tickets have no integrated/waived state.
Those are genuine missing primitives. Add registry integration policy,
structured submission artifacts, and an audited integration acknowledgement.
The coordinator must never merge, push, delete a branch, or mark integration
complete from prose alone.

### 5. Daily and weekly reporting

`fleet_snapshot` already supplies the current cross-board workload and pool:
active projects, open/claimed/submitted totals, grouped seats, busy/available/
stale state, current ticket IDs, truncation counts, and unavailable-board
warnings. It is the current-state section of every digest, not a historical
report.

The coordinator adds:

- aging buckets and oldest open/claimed/submitted tickets;
- at-risk and expired leases, repeat abandonment, and board starvation;
- submissions awaiting review and review/rejection latency;
- closed-today/this-week throughput and reopened-after-rejection counts;
- closed-but-unmerged and unknown integration states;
- capacity by board, unavailable boards, truncation warnings, and phase-2
  cursor resyncs when applicable;
- unresolved operator decisions and changes since the prior digest.

Daily digests emphasize action now; weekly digests show trends and repeated
failure patterns. Metrics reconcile against current durable ticket bodies and
explicit snapshot counts. The coordinator labels lower bounds when data is
truncated. Current ticket listing is bounded and not cursor-paginated, and the
journal may be compacted, so exact long-horizon reporting needs a paginated
ticket export or durable metrics projection. Until then, retain a local
read-only materialized history and state the coverage window in every digest.

## Missing primitive summary

| Missing primitive | Why existing tools are insufficient | Required before |
| --- | --- | --- |
| Pure journal read or read-only wake cue | The wait/catchup path mutates seat activity and may renew leases | Strict phase 1 event-driven mode |
| Narrow coordinator authorization | `member` lacks coordination mutations; `admin` and `reviewer` grant unrelated power | Phase 2 |
| Atomic `ticket_assign` with state precondition and idempotency | Existing tickets cannot be assigned without cancel/recreate; claiming on behalf is wrong | Phase 2 dispatch |
| Targeted nudge with acknowledgement, expiry, dedupe, and rate limit | Project memories do not wake a specific seat and are ignored by the wait bridge | Phase 2 babysitting |
| Single-writer coordinator leader lease | Board state has no compare-and-set TTL ownership | Automatic phase 2/3 failover |
| Structured submission artifacts plus integration policy/ack | Commit refs and merge policy are prose or external; closure is not integration | Reliable integration watch |
| Intake idempotency key | A transport retry can create duplicate generated-ID tickets | Phase 3 |
| Paginated ticket export or durable metrics projection | Bounded lists and compactable journal cannot prove complete long-horizon totals | Exact historical digests |

## Human supervision policy

| Action | Autonomous | Operator queue |
| --- | --- | --- |
| Read registry, snapshots, status, tickets, and visible project memories | Yes | On access or truncation failure |
| Detect starvation, stale work, and integration gaps | Yes | Report findings and evidence |
| Produce daily/weekly digest | Yes | Operator chooses delivery channel and retention |
| Reap a server-confirmed expired lease | Phase 2, within rate limit | Repeated abandonment or failed reap |
| Assign open work or nudge a seat | Phase 2, after narrow primitives exist | Ambiguous eligibility, cross-policy routing, or override |
| Create a low-risk, policy-complete ticket | Phase 3 | Ambiguous/high-impact intake |
| Merge, push, deploy, send externally, or delete | Never | Always |
| Admit/remove members; change roles, registry, scrub policy, or review policy | Never | Always |
| Approve/reject work or bypass independent review | Never | Always |
| Read, print, copy, rotate, issue, or revoke credentials | Never | Always |

Explicitly forbidden actions are: claiming worker work; editing project files;
reviewing or closing submissions; force-releasing unexpired claims; changing
membership or roles; altering registry entries or board policies; merging,
pushing, deploying, deleting branches, or sending messages outside the chosen
report channel; weakening required fields or forbidden actions; acting on
truncated/stale data; exposing credentials; and retrying a mutation without an
idempotency or state precondition.

## Failure modes and controls

### Coordinator down

Workers and reviewers continue their direct loops. A supervisor restarts the
coordinator. Phase 1 reloads the registry through the raw non-joining state
read, snapshots every board, and rebuilds local comparison state without a
journal cursor. Phase 2 resumes from per-board cursors or a full resync. No
worker lease is owned by the coordinator.

### Coordinator wrong

Phase 1 is shadow/read-only. Every proposed action includes a reason and source
watermark for operator comparison. Phase-2 writes use exact IDs, expected
state, idempotency keys, and immediate readback. On any mismatch, registry
warning, unavailable board, truncation, or repeated lost race, the board's
circuit breaker opens and the coordinator returns to report-only mode.

### Runaway loop or duplicate writers

Use per-board and global write budgets, exponential backoff with jitter, one
nudge per ticket/seat/grace window, one reap attempt per observed expiry, and a
hard daily intake cap. Mutation failures do not retry until current state is
refetched. Only one phase-2/3 writer is active; lack of a leader lease forces
manual failover. The operator has a kill switch that removes write capability
without affecting read access or workers.

### Partial or misleading data

Truncation and unavailable-board warnings are first-class report failures, not
zero counts. Phase 1 has no cursor/resync path; in phase 2, cursor resync
triggers a snapshot rebuild. Clock comparisons use server timestamps with a
configured skew allowance. Prose-only commit parsing may yield `unknown` but
never a confident negative. Local repository failures do not change board
state.

## Delivery phases and exit criteria

### Phase 1: read-only detection and reporting

Deliver `fleet_snapshot`-based current state, starvation/lease/integration
detectors, and daily/weekly digests. Use non-joining reads and local state; make
no board mutations.

Exit only when:

- an audit run confirms zero board writes by the runtime;
- restart simulations rebuild the same current view without a journal cursor
  or resync dependency;
- fixtures for starvation, stale/expired claims, unavailable/truncated boards,
  and closed-unmerged work produce the expected evidence and no unsafe action;
- sampled digest counts reconcile with board status and ticket bodies;
- credentials never appear in model context, reports, logs, or local ledgers;
- coordinator shutdown leaves the worker/reviewer path unchanged; and
- the operator answers the open questions below.

### Phase 2: dispatch and nudge writes

Add wake-driven monitoring, atomic assignment, targeted nudge, and server-
confirmed expiry reap. Do not add intake.

Entry requires the narrow coordinator authorization, atomic `ticket_assign`,
targeted nudge/ack, mutation idempotency, and a single-writer/failover control.
Exit requires a shadow comparison period, no double assignments, verified
state-precondition failures under races, effective rate/circuit breakers,
operator-visible audit reasons, and a demonstrated kill switch that returns
the coordinator to phase 1 without affecting workers.

### Phase 3: structured intake

Add LLM-assisted drafting behind the deterministic validator and approval
matrix. The LLM never chooses authorization or bypasses missing fields.

Exit requires duplicate-safe creation under transport retries, complete schema
validation, test coverage for every approval-matrix boundary, operator review
of a representative ticket sample, measured correction/duplicate rates within
the agreed threshold, and immediate rollback to phase 2 by disabling only
`ticket_create`.

## Open questions for the operator

1. Where should phase-1 reports appear: the read-only dashboard, a local file,
   a dedicated operator inbox, or more than one channel?
2. What thresholds define "available," "at risk," "starved," and "repeat
   abandoner" for each board: freshness, lease warning, grace, and escalation?
3. What fairness weights or service levels should apply across boards after
   critical priority is respected?
4. Should phase 2 introduce a narrow coordinator role/capability, or remain
   advisory until the operator performs every assignment and forced action?
5. Are coordinator assignments exclusive instructions or preferences that a
   worker may decline, and what skill/capacity metadata is authoritative?
6. What is the integration ref and merge policy for each registered work tree,
   and how are intentional non-merge closures acknowledged?
7. Must the coordinator remain permanently unable to merge (recommended), or
   is any future operator-approved merge path in scope?
8. Which intake categories always require confirmation, and which low-risk
   scopes may phase 3 create autonomously?
9. What digest retention and audit window are required, and may reports be
   written back as project memories after phase 1?
10. Is manual active/passive failover acceptable for phases 2 and 3, or must a
    server-enforced coordinator leader lease be built first?
