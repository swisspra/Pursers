# Pursers fleet coordinator

The coordinator observes every active board in the home board's
`project_registry`. Phase 2 adds atomic assignment while
leaving worker claim, submission, and independent review paths unchanged.

## Modes and kill switch

`--mode shadow` is the default. It computes the same decisions as active mode
and writes `would_assign` findings, but performs zero workflow mutations.
`--mode active` performs `ticket_assign` calls and
records each outcome in `coordinator_findings` and the digest.

Mode is a process-start flag and cannot be toggled at runtime. The kill switch
is either:

- restart without `--mode active` (the process returns to shadow); or
- stop the coordinator entirely. Workers and reviewers continue normally.

Example one-cycle shadow validation:

```sh
PYTHONPATH=packages/client/src \
python tools/coordinator/coordinator.py \
  --token-path /absolute/path/to/coordinator-token \
  --intake-token-path /absolute/path/to/coordinator-intake-token \
  --home-board pursers \
  --mode shadow \
  --once
```

`--dry-run` is stricter: it prints the computed state and performs no writes,
including finding and digest writes.

## Cue-driven refresh

After one startup materialization, the daemon keeps one
`BoardClient.events()` subscription on each active registry board's journal.
The driver uses `board_catchup(touch=false, acknowledge=false)` and never enters
`BoardClient`, so the read path does not join or touch an agent seat. A cue
refreshes only its board; healthy idle time causes no Central RPC.

`--poll-seconds` is not a primary loop interval. It bounds the
subscription-loss recovery delay and defaults to 60 seconds. Consecutive loss
steps wait about 1, 2, 4, 8, 16, 32, then at most 60 seconds, with 10 percent
jitter. Only the first loss for a pending step is logged. After that actual
delay, the daemon performs one fallback refresh for the affected board and
then re-listens from its last local cursor. A healthy cue resets the streak;
fallback reads keep increasing the loss streak until streaming recovers, while
other healthy boards remain subscribed.

## Policy and safeguards

Many seats may share one bearer principal. The security and workflow identity
is the exact seat (`agent_id`), derived from board, principal, and agent name;
coordinators must therefore reason about offers, claims, reviews, and executor
authority by `agent_id`, not by principal alone.

Central exposes pending operator decisions as `needs_human` tickets. A worker
calls `ticket_request_human`, which releases its lease and records the bounded
question, optional flat answer schema, and safe handoff URL. An admitted board
admin or a `board:coordinate` principal resolves the matching `request_id` with
`ticket_human_resolve`: accepted answers reopen and immediately re-dispatch the
ticket without preferring the asker; declined requests may stay parked or
cancel the ticket; a dismissed request stays parked and may be asked again.
Coordinator digests include unresolved human requests, while Dispatcher never
offers or counts them as starving work.

- Normal tickets starve at 30 minutes; critical tickets at 10 minutes.
- At one threshold, the Dispatcher continues offering work to eligible seats;
  the coordinator makes no duplicate wake call.
- At exactly twice the threshold, the oldest fleet-fair ticket is assigned to
  the least-loaded eligible seat. Critical work ranks before other priorities.
- Seats with three proven drops in seven days remain eligible but rank last.
- Assignment is atomic only while the ticket is open, unclaimed, and at the
  expected assignee. A lost race is reported and never overwritten.
- Central publishes `coordinator_assignment` cues only to the selected agent.
  Ordinary `ticket_created` and reopened-ticket events
  remain visible to all admitted workers through open-backlog catch-up.
- Operation keys are deterministic across restarts. The limit is one assignment
  per board per 10 minutes.
- Three consecutive mutation failures open the circuit breaker and change the
  process's effective mode to shadow.
- Central re-enters the offer cycle for an unclaimed broadcast ticket after
  `dispatch_policy.broadcast_reoffer_s` (600 seconds by default). Each cycle
  may retry live eligible workers, but never coordinator/orchestrator seats or
  any seat with `can_work=false`.
- Board status and the fleet dashboard flag broadcast tickets still unclaimed
  beyond that threshold under **Needs attention**.

## Dual credentials for intake

The daemon uses two principals. `--token-path` is the main credential for
joining boards, reading fleet state, assignments, findings, queue drain,
and digests, including the read used to verify an idempotent replay.
`--intake-token-path` is used only by a non-joining `ticket_create` call. The
intake credential must include
`board:read board:intake`, may include `board:coordinate`, and must not include
`board:write`: Central reserves `coordinator_op_key` for write-less intake
principals and rejects it for a normal writer.

If intake is enabled without a usable intake token, the daemon prints one line
and leaves asks queued as drafts. Findings distinguish asks that have explicit
human approval from auto-category asks that do not. A write-scoped intake token
is also refused locally with the same approval distinction; the main credential
continues read-only analysis, while coordinator mutations remain in shadow mode.

### Approve or decline an ask

The fleet dashboard keeps a new ask pending until the coordinator publishes a
matching draft title and category. Before that draft arrives, the dashboard
keeps **Decline** available but hides **Approve** and title editing. Approving
records the human decision in `coordinator_intake`; declining removes the ask
and records a bounded tombstone for visibility. The coordinator alone creates
an approved ticket with the separate `board:intake` credential. Approval
bypasses the category matrix, but not the scope check, rate limit, circuit
breaker, or deterministic operation-key replay protection.

The live config equivalent is `intake.token_path`:

```json
{
  "schema_version": 1,
  "intake": {
    "enabled": true,
    "token_path": "/absolute/path/to/coordinator-intake-token"
  }
}
```

The fleet dashboard preserves this key when editing other coordinator settings
and accepts only `null` or a safe absolute path.

Provision both principals as board members. This example intentionally lists
scopes only; token minting and key material remain operator-only:

| Credential | Required scopes | Optional scopes | Forbidden scopes |
| --- | --- | --- | --- |
| `coordinator-main` | `board:read board:write board:coordinate` | — | — |
| `coordinator-intake` | `board:read board:intake` | `board:coordinate` | `board:write` |

At startup the daemon decodes the JWT scope claims locally without signature
verification and never logs either token. A mismatch is named by credential and
missing or forbidden scope, and forces shadow mode. Use `--strict-scopes` when a
scope mismatch should stop a one-shot validation or supervised deployment.

Board joins and writes are isolated per board. An inaccessible registry board
produces a `board_unreachable` finding (including a scrubbed reason) on the home
board while healthy boards continue. Repeated messages for the same board are
logged at most once per five minutes. If the home board is inaccessible, the
daemon stays alive and retries with exponential backoff capped at five minutes.

Board membership is admission, while token scopes authorize coordinator
actions. An admitted `admin`, `member`, or `reviewer` principal may therefore
join a `coordinator` or `orchestrator` seat and use its narrowly scoped
coordination/intake operations when its token carries the required scope. A
coordinate-only join never consumes an invite or changes board membership.

`ticket_assign` is the manual dispatch escape hatch. It additionally requires
`admin` board membership and `board:coordinate`; ordinary member/reviewer
coordinator seats rely on Dispatcher offers instead.

For tier, skills, and preference controls, a `board:coordinate` credential may
call `ticket_update` under the existing creator-or-admin membership check. It may
also call `board_dispatch_policy_set`, but that operation remains limited to an
`admin` board membership. Other admin-only configuration tools keep their
existing `board:write` requirements.

Offers are enforced server-side. Worker and reviewer principals cannot claim a
ticket offered to another seat; broadcast fallback claims still require a live,
capable, non-excluded seat. Board admins and `board:coordinate` principals keep
their operator-recovery bypass. An admin or coordinator can call
`ticket_update(..., parked=true)` to keep an open ticket visible while preventing
offers, broadcasts, and ordinary seat claims; `parked=false` immediately
re-dispatches it.

## Attach operator-run evidence

When a required live or credentialed check can run only in an operator session,
record its exact result on the ticket with `ticket_annotate(...,
kind="evidence")`. Central scrubs and attributes the text to the authenticated
principal without claiming, submitting, or changing workflow state. Put the
returned `AN-...` identifier in the worker submission's corresponding required
field so the reviewer can verify the evidence and its author. Keep credentials
out of annotation text.

## Troubleshooting runbook

| Symptom | Check | Recovery |
| --- | --- | --- |
| Scope preflight names a missing `board:coordinate` scope, or a new registry board reports `board_unreachable` | Compare both credentials with the scope matrix above. | Have the token operator mint a replacement `coordinator-main` token with `board:read board:write board:coordinate`, install it at the configured token path, and restart the daemon. Never add `board:write` to the intake token. |
| The home board is unavailable | Confirm Central reachability and the home-board membership; the daemon log should show capped in-process retries rather than repeated process starts. | Restore Central or membership. The running daemon retries automatically, with a maximum five-minute delay. |
| A ticket stays claimed for hours while its branch is not moving | Inspect the ticket's `claim_age_s`, `lease_renewal_source`, and `lease_keepalive_only_age_s`. The fleet dashboard flags claims renewed only by keepalive for more than three live claim TTLs. | Confirm the model session is no longer working, then use admin `ticket_unclaim` to return the ticket to the queue. Do not delete or rewrite the worker branch; its continuation evidence remains available to the next claimant. |
