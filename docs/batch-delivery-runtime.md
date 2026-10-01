# Batch delivery runtime

The batch delivery runtime collects independently reviewed ticket commits on an
owned integration branch and exposes either one customer pull request or one
immutable delivery branch. It never merges the customer pull request, pushes the
mapped base branch, deletes a source branch, or uses a model to make scheduling or
merge decisions.

This runtime is separate from the legacy `per_ticket_pr` path. Selecting
`per_ticket_pr` returns control to the existing delivery implementation without
changing its index records, pull requests, branches, or status vocabulary. Unknown
delivery modes fail closed; they are not treated as batch delivery.

## Runtime modes

- `batch_pr` integrates approved commits on the owned working branch, validates the
  exact cumulative head, creates an immutable or explicitly rolling snapshot, and
  creates or reuses one correlated customer pull request for the mapped repository
  and target branch.
- `branch_only` performs the same reviewed integration and cumulative validation,
  but stops after publishing the immutable snapshot branch. It never calls a pull
  request connector.
- `per_ticket_pr` remains the legacy path. The batch runtime does not adopt or
  rewrite its records.

The resident Butler resolves the shared `delivery_policy` with
`resolve_delivery_policy`, then `runtime_policy_from_resolved` compiles that exact
effective policy into the runtime contract. Repository identity and the fleet-owned
clone come from the same registry project. A missing resolver, unsupported schedule,
unconfigured repair runner, mismatched clone origin, or operator-owned checkout fails
closed before any delivery mutation. The effective policy digest is frozen in each
active batch. Later global, group, or repository edits affect only a subsequent batch.

A saved `batch_pr` or `branch_only` policy remains a draft. The resident path activates
it only when the project also contains a versioned `delivery_policy_activation` record
with `schema_version: 1`, `state: active`, a non-empty `activation_id`, and the exact
compiled `policy_revision`. No activation record preserves the legacy delivery path;
a stale or malformed activation fails closed. Deploying new runtime capability alone
therefore never changes routing or PR behavior for an existing saved draft.

The supported triggers are:

- `ready`: collect all reviewed members seen in one deterministic Butler writeback
  cycle, then release only with an explicit cohort ID and the exact member set. A
  first completed ticket therefore cannot freeze the snapshot while later members in
  the selected cohort are still integrating.
- `manual`: require an authorized, non-empty request ID. Request IDs are durably
  bound to one batch and cannot be reused for another batch.
- `scheduled`: require an `HH:MM` wall-clock value and an IANA timezone. A release is
  eligible only during that exact local minute. Missed windows are not replayed,
  which prevents restart catch-up storms.

There is no issue-count trigger.

## Durable state and recovery

`BatchLedger` uses an atomic write, file `fsync`, rename, and directory `fsync`.
The ledger records the stable repository/target route, unique batch key, effective
policy revision, ticket and issue membership, exact source commits, evidence digest,
working and snapshot heads, validation, pull request and merge identifiers, manual
request IDs, schedule slots, and every remote-mutation reservation.

A reservation is saved before a connector or Git mutation. A process restart can
therefore distinguish a confirmed result from a reserved or unknown outcome.
`reconcile_unknown` reads the remote outcome through the adapter and updates the
ledger before another attempt is allowed. Duplicate ticket events are deduplicated
across restarts and delivery modes. A ticket whose commit or evidence changes is
blocked until it has a new independent approval.

## Branch and pull request invariants

The mapped base, owned integration branch, and customer target must be distinct for
`batch_pr`. `branch_only` uses the mapped base as its route identity but never writes
it; its owned integration branch remains distinct.
The runtime reads the ticket source ref and requires it to equal the independently
approved full commit SHA. It also requires the owned integration ref to equal the
ledger head before every integration. Adapter mutations receive an expected old
head and an operation ID; implementations must use compare-and-swap semantics and
must not force-push.

The working integration branch is distinct from each delivery snapshot. A frozen
snapshot cannot receive later work. New approved work continues on the owned
integration branch and queues in the next batch while the customer reviews the
current pull request. A known active batch therefore causes `customer_pr_slot_busy`,
not a failed merge. An uncorrelated or multiply active customer pull request blocks
delivery for operator inspection.

Rolling snapshots are allowed only when the recorded customer pull request declares
the snapshot mutable. Every update is correlated to the same batch key, reserves the
remote mutation first, and marks the previous cumulative validation stale. Frozen
snapshots and externally edited refs are never rewritten.

The pull request body lists each ticket, exact commit, source issues, summary, tests,
blockers, and baseline failures. It explicitly states that customer merge is manual
and does not claim that Sonar findings are resolved.

## Validation and conflict repair

Before release, the adapter must return a successful validation bound to the exact
cumulative working head and current mapped base head. The customer target is read
again immediately before pull request reconciliation. A changed target invalidates
the validation and blocks delivery.

Conflicts are not resolved with blanket `ours` or `theirs` strategies. If no
configured repair runner is available, the member and batch enter an explicit
blocked state. A runner result is accepted only when it supplies a new full commit
SHA plus fresh independent-review and validation evidence bound to that SHA.

Repository locks are keyed by stable repository and customer target, so only the
integration mutation for one route is serialized. Independent repositories continue
concurrently. Waiting for a customer pull request slot does not stop workers from
producing reviewed commits.

## Customer completion

The runtime observes customer pull requests but never completes them. A completed
pull request is recorded only when its source SHA matches the batch snapshot, its
merge identifier is valid, and the adapter reconciles the current mapped target.
The reconciliation contract permits squash merges, where the target head is not the
snapshot or merge commit. Completion moves every member to `customer_merged`; it
does not create, complete, retarget, or close any downstream pull request.

Abandoned and externally edited pull requests remain explicit ledger states. Existing
legacy pull requests and source-intake index records are not automatically adopted,
retargeted, or closed. Migration is an operator-controlled action.

## Adapter contract

The runtime is connector-neutral. A production adapter must implement read-only ref
and pull request reconciliation plus compare-and-swap branch mutation, reviewed
integration, exact cumulative validation, customer pull request create/update, remote
operation reconciliation, and customer merge confirmation. The methods exercised by
the runtime are:

```text
read_ref
ensure_branch
integrate_reviewed
validate_cumulative
list_customer_prs
create_customer_pr
update_customer_pr
reconcile_operation
get_customer_pr
confirm_customer_merge
request_conflict_repair  # only when a repair runner is configured
```

`VerifiedGitConnectorAdapter` is the resident production implementation. It verifies
the fleet clone's origin against the registry mapping, uses isolated temporary Git
worktrees, normal non-force pushes, exact remote-ref readback, configured validation
commands without a shell, and the resident policy-gated ADO connector for customer PR
operations. `branch_only` never invokes that connector. Every PR create/update is
correlated in the body and read back by exact PR ID, source branch, source SHA, target
branch, and batch key. Unknown rolling updates keep their reservation until that full
identity is confirmed.

An adapter that cannot provide one of the required checks returns an unavailable or
unknown result. The runtime exposes a blocked state instead of fabricating a
successful check or activating a partial mode.
