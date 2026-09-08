# Fleet unblock diagnosis: stale offers and Team-local waiting (resubmit-2)

Baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (origin/main, 5.0.0a25).

## Diagnosis scope

READ-ONLY investigation of why seats with live CLI processes do not claim
Pursers offers. No confirmed operational root cause was established. No
affected-seat command/process/schema evidence confirms any root cause.

## CLI wait path

The actual affected-seat wait path is `bin/board.sh wait --boards registry`
which calls `seat_new._cmd_wait()` (line 746), delegating to
`pursers_client.wait_for_boards()` (project_registry.py line 225).

### `wait_for_boards()` behavior (project_registry.py:225-490)

1. Joins each selected board via `board_join`, caching agent_id per board
2. For each board, calls a local `drain()` function (line 460) that:
   - Reads bounded `board_catchup` pages
   - Advances the cursor past irrelevant events one by one
   - Returns immediately at the first relevant event (line 466)
3. Opens a single raw listen subscription bounded by timeout
4. **No reconnect loop**: the subscription is bounded by the timeout; if
   the stream ends, the wait returns, it does not reconnect
5. Returns the registry response schema:

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

The cursor advances past irrelevant events in `drain()` (line 460-466):
irrelevant events have their seq recorded in `cursors[board_id]` via
`max(cursors[board_id], event_seq)`, so they are not re-processed on the
next wait. The cursor is returned as `new_seq` in the response. The caller
re-arms with `new_seq` on the next wait call.

### Re-arm behavior

After timeout (`timed_out=true`) or clean early stream end, the response
map is returned with the cursor advanced to `new_seq`. The caller re-arms
with `new_seq`. On non-timeout errors, the error propagates and produces
no `new_seq`; the caller retains and reuses the last successfully returned
full registry cursor after reporting the sanitized error.

## Role resolution

`_resolve_wait_for(wait_for, role)` (pursers_wait_server.py:4066) maps
`auto` → `claimable` for worker, `submitted` for reviewer. Explicit
`submitted` for non-reviewer raises `ToolError`. This is correct and not
a failure mode for properly configured seats.

## MCP wait bridge context (separate from CLI path)

The following constants exist only in `tools/wait-bridge/pursers_wait_server.py`
(the autonomous MCP wait bridge), not in the `bin/board.sh` CLI path:

| Constant | Value | Line | Scope |
| --- | --- | --- | --- |
| `BACKLOG_RESURFACE_INTERVAL_S` | 600 seconds | 203-207 | MCP bridge only |
| `BACKLOG_SUPPRESSION_LIMIT` | 500 entries | 225 | MCP bridge only |

The MCP bridge also has `BoardClient.events()` with a separate fixed-delay
reconnect loop (not exponential backoff). These are inapplicable to the
affected CLI seats unless actual evidence proves an MCP tool wait was used.

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

2. **Cursor advancement past unprocessed offers:** The `drain()` function
   (line 460) advances the cursor past events deemed irrelevant by the
   seat's `agent_id`, selected kinds, and `ticket_is_relevant()` check
   (which verifies `offered_agent_id == my_agent_id`, `recipient_identities`
   includes `my_agent_id`, and `dispatch_state` matches the wait mode). If
   an offer event was deemed irrelevant, the cursor advances past it.
   **Evidence needed:** Compare the seat's saved cursor, agent_id, selected
   kinds, and the event's `offered_agent_id`/`recipient_identities` against
   the contemporaneous `ticket_get` dispatch state.

No other hypotheses are offered for the CLI path. MCP-bridge-specific
mechanisms (backlog suppression, subscription reconnect) are not
applicable unless affected-seat evidence proves an MCP tool wait.

## Non-destructive recovery recommendation

No confirmed root cause was established. The smallest non-destructive
coordinator prompt that preserves existing seat identity and cursor:

1. **Send a Team wake message** to the affected seat. This re-activates
   the model turn, which re-enters the relentless loop and re-arms the
   wait from the persisted cursor (`new_seq` from the last response).

2. **If the cursor is ahead of unprocessed events:** The coordinator
   should verify the seat's cursor against the board's latest seq. If
   the cursor advanced past an unprocessed offer, the coordinator can
   annotate the affected ticket with the correct cursor value.

3. **Do NOT:** kill processes, forget doors, rejoin with new identity,
   restart seats, or use `--poll`/cursor-0 catchups.

## Limitations

- No confirmed operational root cause. No affected-seat evidence was
  inspected.
- The relationship between AionUI conversation status and model
  availability is not fully understood from this codebase.
- No code changes were made. No tests were run. Read-only diagnosis.
- Private seat paths and credentials were not accessed or recorded.
