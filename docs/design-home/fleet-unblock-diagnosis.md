# Fleet unblock diagnosis: stale offers and Team-local waiting (resubmit-5)

Original diagnosis baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096`
(5.0.0a25). Candidate branch is based on the current `origin/main` recorded in
the submission evidence. Source locations below were verified against exact
`origin/main` `919635c8924d091a74a614940a0aab3e549b7c7a`.

## Diagnosis scope

READ-ONLY investigation of why seats with live CLI processes do not claim
Pursers offers. No confirmed operational root cause was established. No
affected-seat command/process/schema evidence confirms any root cause.

## CLI wait path

The actual affected-seat wait path is `bin/board.sh wait --boards registry`
which calls `seat_new._cmd_wait()` (`tools/seat-kit/seat_new.py:748`),
delegating to `pursers_client.wait_for_boards()`
(`packages/client/src/pursers_client/project_registry.py:361`).

### `wait_for_boards()` behavior

1. Joins each selected board via `board_join`, caching agent_id per board
2. For each board, calls the local `drain()` function
   (`project_registry.py:442`) that:
   - Reads bounded `board_catchup` pages
   - Advances the cursor past irrelevant events one by one
   - Returns immediately at the first relevant event
     (`project_registry.py:612-618`)
3. Opens one raw listen subscription bounded by timeout
   (`project_registry.py:663-692`)
4. **No reconnect loop**: the subscription is bounded by the timeout; if
   the stream ends, the wait returns, it does not reconnect
5. Returns the registry response schema from the `response()` helper
   (`project_registry.py:630-640`):

```json
{
  "new_seq": {"board_id": cursor, ...},
  "events": [...],
  "timed_out": true,
  "waited_s": 42.0,
  "boards": ["active_boards"],
  "skipped_boards": {},
  "reason": "offer|held_ticket_update|broadcast|timeout"
}
```

### Cursor behavior

The cursor advances past irrelevant events at `project_registry.py:612-614`:
their seq is recorded in `cursors[board_id]` via
`max(cursors[board_id], event_seq)`. A relevant event records the same monotonic
advance before being returned at `project_registry.py:615-618`. The response
copies the complete cursor map into `new_seq` at `project_registry.py:630-640`;
the caller re-arms with that full map.

### Re-arm behavior

After `TimeoutError` or clean early stream end, control reaches
`response(events)` (`project_registry.py:693-695`) and returns the full cursor
map as `new_seq`. On non-timeout errors no catch converts the exception into a
response, so the error propagates and produces no `new_seq`; the caller retains
and reuses the last successfully returned full registry cursor after reporting
the sanitized error.

## Role resolution

`_resolve_wait_for(wait_for, role)`
(`tools/wait-bridge/pursers_wait_server.py:4087`) maps `auto` → `claimable`
for worker, `submitted` for reviewer. Explicit `submitted` for non-reviewer
raises `ToolError`. This is correct and not a failure mode for properly
configured seats.

## MCP wait bridge context (separate from CLI path)

The following constants exist only in `tools/wait-bridge/pursers_wait_server.py`
(the autonomous MCP wait bridge), not in the `bin/board.sh` CLI path:

| Constant | Value | Line | Scope |
| --- | --- | --- | --- |
| `BACKLOG_RESURFACE_INTERVAL_S` | 600 seconds | 204-210 | MCP bridge only |
| `BACKLOG_SUPPRESSION_LIMIT` | 500 entries | 226 | MCP bridge only |

The MCP bridge calls `BoardClient.events()` at
`tools/wait-bridge/pursers_wait_server.py:4857`; the method starts at
`packages/client/src/pursers_client/client.py:1235`. Its distinct reconnect
loop uses the fixed `reconnect_delay_s` at `client.py:1360` and `client.py:1367`
(not exponential backoff). These are inapplicable to the affected CLI seats
unless actual evidence proves an MCP tool wait was used.

## Replayable source-contract check

The allow-listed regression command below exercises bounded backlog drain,
full cursor-map round trips, per-seat work/review offer selection, skipped-board
handling, takeover reuse, and compacted-cursor resync:

```sh
python3 -m pytest -q packages/client/tests/test_project_registry.py -rs
```

Successful tail on the exact source base above:

```text
........................                                                 [100%]
24 passed in 2.03s
```

## Hypotheses (unverified, no confirmed root cause)

No affected-seat command, process, or schema evidence confirms any root
cause. The following remain unverified hypotheses:

1. **Stale CLI process with no model consumer:** The `bin/board.sh wait`
   process may be alive but its parent model/conversation has ended. When
   the wait returns, there is no model to process the result and re-arm.
   **Evidence needed:** Check whether affected seats have `bin/board.sh`
   processes alive without an active model consumer. The ticket reports
   that Goose-2/worker-3 acted while persisted conversation status remained
   `finished`, which may falsify this hypothesis.

2. **Cursor advancement past unprocessed offers:** The local `drain()` logic
   first filters by the selected event kinds and worker/reviewer mode. When a
   contemporaneous `ticket_get` exposes `dispatch_state`, an offered event is
   relevant only when the matching `work_offer.agent_id` or
   `review_offer.agent_id` equals the seat's board agent ID; offer lifecycle,
   held-ticket, lease, and broadcast events have their own inline conditions.
   Without a usable `dispatch_state`, the fallback worker broadcast path checks
   `recipient_identities`, while the reviewer fallback checks a submitted
   status transition. There is no `ticket_is_relevant()` call in
   `wait_for_boards()`, and no single inline branch jointly verifies every
   evidence field. If an event is deemed irrelevant, `drain()` advances past
   its sequence number.

   **Evidence needed:** A coordinator must compare the seat's board
   `agent_id`, the role-appropriate selected wait kinds, the event's
   `offered_agent_id` and `recipient_identities`, and the contemporaneous
   ticket offer plus `dispatch_state`. The saved cursor and event sequence by
   themselves establish neither delivery nor processing.

No other hypotheses are offered for the CLI path. MCP-bridge-specific
mechanisms (backlog suppression, subscription reconnect) are not
applicable unless affected-seat evidence proves an MCP tool wait.

## Non-destructive recovery recommendation

No wake was executed during this diagnosis. The exact OPERATOR Team wake
prompt for the existing seat is:

> Read your seat's `AGENTS.md` and resume with the same identity, profile,
> process, doors, configuration, and checkout. Reuse only the last full
> registry cursor map returned by a successful wait. For a worker, re-arm
> `bin/board.sh wait --boards registry --since '<FULL_CURSOR_MAP>' --timeout 300`.
> For a reviewer, re-arm
> `bin/board.sh wait --submitted --boards registry --since '<FULL_CURSOR_MAP>' --timeout 300`.
> Do not use `--poll`, `--boards home`, or cursor `0`. Verify either one
> returned relevant event or a bounded timeout whose returned `new_seq`
> advances the full registry cursor map. Persist that complete returned map
> and continue under the seat's re-arm rules. Do not kill or restart the seat,
> forget or replace doors, rejoin, change identity, or patch live state.

## Limitations

- No confirmed operational root cause. No affected-seat evidence was
  inspected.
- The relationship between AionUI conversation status and model
  availability is not fully understood from this codebase.
- No product code or live state was changed. This revision only corrects the
  source-backed diagnosis and recovery prompt; source checks, leak scan, and
  diff checks are recorded in the submission evidence.
- Private seat paths and credentials were not accessed or recorded.
