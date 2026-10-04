# Shared registry fleet coverage

One authorized host pool can serve several active WORK boards. The control
board's `autonomous_butler_config_v1` remains the authority for the shared
templates and host ceilings; registry membership alone does not enable a board
or enlarge capacity. The local observer verifies every template principal and
seat identity on every selected active board before it considers that template
startable. A missing, stale, duplicated, or unauthorized identity therefore
fails closed.

Demand from active registry boards without their own autonomous config is
pooled into the configured home/control board exactly once. A board with its
own autonomous config is reconciled separately and is not also pooled. Only
runnable open/submitted tickets count: parked or human-blocked tickets are
reported as excluded. An expiring offer is reported as a starvation signal but
does not add a second capacity unit for the open ticket it already represents.
Seat leases and busy status are checked across every selected board, so a
principal busy away from the control board is not treated as idle or drained.

The published `autonomous_butler_state_v1.coverage` object is the operator and
dashboard contract. It names source boards, runnable and excluded demand,
duplicate rows, role/board/provider/host limits, the approved template pool,
and one bounded limiting reason: `role_cap`, `template_pool`, `authorization`,
`headroom`, `provider`, `cooldown`, or `none`.

## Operator migration

This migration changes authorization and managed services, so a worker must
not perform it.

1. Save the current per-board Butler config revisions, authorization
   fingerprints, owner-only local observation config, executor policy, executor
   database, fleet state, registry cursors, and service definition. Record all
   live work/review leases before changing a process.
2. Choose one explicit control board. Keep its existing human-approved host
   ceiling and immutable envelope; do not derive either from VM capacity or the
   number of registry projects.
3. Verify that each shared template has a distinct principal and that its exact
   seat identity, role, capabilities, and membership are valid on every active
   WORK board it may serve. Do not include HOBBY/PERSONAL boards.
4. Bind each shared template once in the owner-only local observation file. A
   redacted shape is:

   ```json
   {
     "templates": {
       "template:worker-01": {
         "board_id": "CONTROL_BOARD",
         "provider": "codex",
         "seat_id": "worker-01",
         "enabled": true
       },
       "template:reviewer-01": {
         "board_id": "CONTROL_BOARD",
         "provider": "codex",
         "seat_id": "reviewer-01",
         "enabled": true
       }
     },
     "providers": {
       "codex": {
         "kind": "codex_cli",
         "executable": "/PATH/TO/codex",
         "auth_file": "/PATH/TO/private/auth.json",
         "profile_file": "/PATH/TO/private/config.toml"
       }
     },
     "host_headroom": {
       "max_load_ratio": 0.95,
       "min_memory_headroom_ratio": 0.1,
       "min_disk_headroom_ratio": 0.1
     }
   }
   ```

5. Use the typed Butler config API/dashboard to retain the control board's
   current `desired` bounds and immutable envelope. Enabling another board
   requires its own explicit config and authorization; adding it to
   `project_registry` is not authorization.
6. Validate owner-only permissions and the exact template key set against the
   executor policy. Only then may the operator reload the service. Never
   preempt a live lease to make the new desired count fit.

## Live acceptance

After the operator-authorized reload, observe at least one complete refresh
without changing caps:

- `coverage.source_board_ids` contains every intended active WORK board once;
- a ready ticket away from the control board changes runnable demand, while a
  parked/human-blocked ticket changes only the excluded counters;
- a principal holding a lease on any board remains busy globally and is never
  selected for drain/stop;
- `effective_limits` matches the approved role, board, template, provider and
  host ceilings, and `limiting_reason` explains unmet demand;
- the number of active agent processes never exceeds `host_role_capacity`, and
  every scale-down waits for lease-free draining;
- simultaneous broadcast cues produce one server-side claim before a paid
  event-seat turn; losing seats record `claim_race_lost` and start no model;
- the dashboard's typed actual-state projection preserves the same `coverage`
  values instead of showing an inferred zero.

Run the focused repository suites before any live trial:

```sh
python3 -m pytest -q tools/board-butler/tests/test_fleet_reconciler.py tools/board-butler/tests/test_fleet_observation.py
python3 -m pytest -q tools/seat-kit/tests/test_event_seat.py
python3 -m pytest -q tools/fleet-dashboard/tests/test_butler_settings.py
```

## Rollback

Do not stop or re-role a lease holder. Disable further scale mutations through
the existing operator control, let in-flight work finish, and preserve the
newest executor database, fleet state, cursor, and operation receipts. Restore
the prior reviewed code/service definition and the exact saved owner-only local
config. Restore a prior Butler config only through a fresh typed CAS write and
valid human authorization; never copy an old authorization fingerprint onto a
new revision. Reload the service, confirm its exact revision and ceilings, then
verify all active leases and registry memberships again.
