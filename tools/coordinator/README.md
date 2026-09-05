# Pursers fleet coordinator

The coordinator observes every active board in the home board's
`project_registry`. Phase 2 adds targeted dispatch preferences while
leaving worker claim, submission, and independent review paths unchanged.

## Modes and kill switch

`--mode shadow` is the default. It computes the same decisions as active mode
and writes `would_nudge` / `would_assign` findings, but performs zero workflow
mutations. `--mode active` makes one atomic `ticket_update` per planned ticket,
setting a single deterministic `prefer_agents` target so the dispatcher can
offer work to that seat. Central checks the planned ticket and work-offer state
and deduplicates the operation key before changing the ticket or replacing an
offer. The coordinator records each outcome in `coordinator_findings` and the
digest.

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

`--poll-seconds` is not the primary loop interval. It defaults to 900 seconds
and is used only after a board's subscription is lost: the daemon waits that
delay, performs one fallback refresh for that board, then re-listens from its
last local cursor. Other healthy boards remain subscribed.

## Policy and safeguards

- Normal tickets starve at 30 minutes; critical tickets at 10 minutes.
- At one threshold, the coordinator chooses one deterministic idle eligible
  seat and refreshes the ticket's dispatcher preference once.
- At exactly twice the threshold, the oldest fleet-fair ticket is strongly
  preferred to one least-loaded eligible seat. Critical work ranks before
  other priorities; this remains a dispatcher preference, not an assignment.
- Seats with three proven drops in seven days remain eligible but rank last.
- Central revokes any stale offer and lets the dispatcher issue the replacement
  offer. Ordinary `ticket_created` and reopened-ticket events remain visible to
  all admitted workers through open-backlog catch-up.
- Operation keys are deterministic and server-deduplicated across restarts.
  Each planning pass makes at most one preference mutation per ticket. Stage
  two is limited to one ticket per board per 10 minutes; stage one is limited
  to three selected-ticket preference refreshes per seat per hour.
- Three consecutive mutation failures open the circuit breaker and change the
  process's effective mode to shadow.

## Dual credentials for intake

The daemon uses two principals. `--token-path` is the main credential for
joining boards, reading fleet state, dispatch preferences, findings, queue drain,
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
continues all non-intake work unchanged.

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

```text
coordinator-main:   board:read board:write board:coordinate
coordinator-intake: board:read board:intake
```

Board membership is admission, while token scopes authorize coordinator
actions. An admitted `admin`, `member`, or `reviewer` principal may therefore
join a `coordinator` or `orchestrator` seat and use its narrowly scoped
coordination/intake operations when its token carries the required scope. A
coordinate-only join never consumes an invite or changes board membership.

For dispatch controls, an admitted active `coordinator` or `orchestrator` seat
with `board:coordinate` may call `ticket_update` on any live ticket; it need not
be the ticket creator or an `admin` member. `board_dispatch_policy_set` remains
limited to an `admin` board membership. Other admin-only configuration tools
keep their existing `board:write` requirements. The shipped coordinator uses
the stricter keyed preference form: it requires an open, unclaimed, unassigned
snapshot and an exact current work-offer match; stale or replayed plans do not
revoke or reissue offers.
