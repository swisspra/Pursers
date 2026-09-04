# Pursers fleet coordinator

The coordinator observes every active board in the home board's
`project_registry`. Phase 2 adds targeted nudges and atomic assignment while
leaving worker claim, submission, and independent review paths unchanged.

## Modes and kill switch

`--mode shadow` is the default. It computes the same decisions as active mode
and writes `would_nudge` / `would_assign` findings, but performs zero workflow
mutations. `--mode active` performs `agent_nudge` and `ticket_assign` calls and
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

`--poll-seconds` is not the primary loop interval. It defaults to 900 seconds
and is used only after a board's subscription is lost: the daemon waits that
delay, performs one fallback refresh for that board, then re-listens from its
last local cursor. Other healthy boards remain subscribed.

## Policy and safeguards

- Normal tickets starve at 30 minutes; critical tickets at 10 minutes.
- At one threshold, every idle eligible seat may receive a targeted nudge.
- At exactly twice the threshold, the oldest fleet-fair ticket is assigned to
  the least-loaded eligible seat. Critical work ranks before other priorities.
- Seats with three proven drops in seven days remain eligible but rank last.
- Assignment is atomic only while the ticket is open, unclaimed, and at the
  expected assignee. A lost race is reported and never overwritten.
- Central publishes `coordinator_nudge` and `coordinator_assignment` cues only
  to the selected agent. Ordinary `ticket_created` and reopened-ticket events
  remain visible to all admitted workers through open-backlog catch-up.
- Operation keys are deterministic across restarts. Limits are one assignment
  per board per 10 minutes and three nudges per seat per hour.
- Three consecutive mutation failures open the circuit breaker and change the
  process's effective mode to shadow.

## Dual credentials for intake

The daemon uses two principals. `--token-path` is the main credential for
joining boards, reading fleet state, assignments/nudges, findings, queue drain,
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
