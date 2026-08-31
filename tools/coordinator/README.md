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
  --home-board pursers \
  --mode shadow \
  --once
```

`--dry-run` is stricter: it prints the computed state and performs no writes,
including finding and digest writes.

## Policy and safeguards

- Normal tickets starve at 30 minutes; critical tickets at 10 minutes.
- At one threshold, every idle eligible seat may receive a targeted nudge.
- At exactly twice the threshold, the oldest fleet-fair ticket is assigned to
  the least-loaded eligible seat. Critical work ranks before other priorities.
- Seats with three proven drops in seven days remain eligible but rank last.
- Assignment is atomic only while the ticket is open, unclaimed, and at the
  expected assignee. A lost race is reported and never overwritten.
- Operation keys are deterministic across restarts. Limits are one assignment
  per board per 10 minutes and three nudges per seat per hour.
- Three consecutive mutation failures open the circuit breaker and change the
  process's effective mode to shadow.

The runtime credential should contain only `board:read` and
`board:coordinate`, and its principal must be pre-admitted as a board `member`.
Central restricts that scope to `ticket_assign`, `agent_nudge`, the
`coordinator_findings` state key, and coordinator daily/weekly digest memories.
It cannot create, claim, submit, review, close, or cancel tickets; change board
membership or policy; or write other state or memories. Provisioning and
rotation remain operator-only actions.
