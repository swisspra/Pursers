# Fleet unblock diagnosis: stale offers and Team-local waiting

Baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (origin/main, 5.0.0a25).

## Diagnosis scope

READ-ONLY investigation of why seats with live CLI processes do not claim
Pursers offers. Covers the wait bridge relevance pipeline, cursor handling,
event-kind filtering, and AionUI conversation lifecycle interaction.

## Relevance pipeline (from source)

### 1. Event-kind filter (`_event_matches_wait`, line 4081)

The wait bridge filters events by `wait_for` mode before any relevance check:

| wait_for | TICKET_OFFERED | REVIEW_OFFERED | OFFER_EXPIRED/REVOKED | Other kinds |
| --- | --- | --- | --- | --- |
| claimable (worker) | accepted | rejected | only if offer_kind="work" | WORKER_WAIT_KINDS |
| submitted (reviewer) | rejected | accepted | only if offer_kind="review" | REVIEWER_WAIT_KINDS |

**Finding:** If a worker seat's `wait_for` is set to "submitted" (or auto-resolves
to submitted due to role mismatch), TICKET_OFFERED events are rejected at line
4085 (`return False`). The seat will never see work offers. Conversely, a
reviewer seat with `wait_for="claimable"` will never see review offers.

### 2. Offer-agent ID matching (`_is_relevant`, line 4211)

For TICKET_OFFERED and REVIEW_OFFERED events:

```python
if kind != offered_kind or event.get("offered_agent_id") != my_agent_id:
    return False
recipients = event.get("recipient_identities")
if isinstance(recipients, list) and my_agent_id not in recipients:
    return False
```

**Finding:** The event must carry `offered_agent_id` matching the seat's
`my_agent_id`. If the seat re-joined with a different agent name or identity
(same principal, different agent_id), previously dispatched offers targeting
the old agent_id are irrelevant. The board would need to re-dispatch to the
new agent_id.

### 3. Cursor monotonicity (cursor_map, line 2121-2151)

The wait bridge persists `cursor_map` and `ack_cursor_map` per board in a
seat-local state file. On each wait call:

1. The cursor is read from the state file (or the `since_seq` parameter)
2. `board_catchup` is called with that cursor to drain events
3. Events with seq <= cursor are already "seen" and skipped

**Finding:** If a seat saved a cursor ahead of the actual event seq (e.g.,
the cursor was advanced by a subscription cue but the event was never
processed by the model because the conversation finished), subsequent waits
will not replay that event. The cursor is monotonically increasing and
never goes backward.

### 4. Subscription lifecycle (BoardClient.events, line 1230)

The `events()` generator opens a streaming subscription, drains catchup,
then listens for cues. On subscription loss:

- Reconnects with exponential backoff (reconnect_delay_s)
- Re-drains from the last cursor state
- If `reconnect=False`, terminates with an error

**Finding:** If the subscription silently loses connection and the reconnect
loop is stuck (e.g., Central is unreachable), the wait blocks indefinitely
without processing new offers. The seat appears alive (process running) but
is not receiving events.

### 5. Backlog scan (ticket_is_relevant, backlog.py line 78)

The wait bridge performs periodic backlog scans of claimable/submitted
tickets. This is a fallback that surfaces relevant tickets even without
live subscription cues. It checks:

- `work_offer.agent_id == my_agent_id` for direct offers
- `state == "broadcast"` for broadcast offers
- `claimed_by_agent_id == my_agent_id` for held tickets

**Finding:** If the subscription is down, the backlog scan should still
surface relevant offers. However, the backlog scan has a suppression
interval (`BACKLOG_RESURFACE_INTERVAL_S`) that prevents re-surfacing the
same ticket within a window. If the offer was surfaced once but the
conversation didn't process it, the backlog won't re-surface it until the
interval expires.

## AionUI conversation lifecycle

The ticket reports: "AionUI conversations.status=finished is not reliable
for Team activity (Goose-2 claimed while that value stayed finished)."

**Finding:** AionUI manages conversations that invoke MCP tools. When the
model's turn completes, the conversation status becomes "finished." However:

1. The MCP server process (stdio bridge) may still be alive, blocking on
   a subscription. The wait tool call hasn't returned yet.
2. When the wait eventually returns (timeout or event), there is no model
   turn to process the result. The seat won't claim or submit.
3. The board sees the seat as alive (process running) but the seat is
   effectively dead (no model consumer).

**Evidence from source:** The wait bridge has no mechanism to detect
whether the host model is still listening. It blocks until timeout or event,
then returns the result to the MCP host. If the host doesn't process the
result, the seat is stuck.

## Wait JSON schema

The wait returns:

```json
{
  "events": [...],
  "new_seq": {"board_id": seq, ...},
  "reason": "offer|held_ticket_update|broadcast|timeout",
  "timed_out": true|false,
  "waited_s": 42.0
}
```

**Finding:** The caller re-arms with `new_seq`. If a stale .seat-wait-loop.sh
script is using an old JSON schema that doesn't parse `new_seq` correctly
(e.g., expects a single integer instead of a map), the cursor may not
advance, causing the wait to replay old events or skip new ones.

## Offer TTL and exclusion

Offers have `expires_at` (typically 10 minutes). If the seat's wait is
blocked in a stale subscription:

1. The offer is dispatched to the seat
2. The subscription doesn't deliver the cue
3. The offer expires (10 minutes)
4. The board requeues and broadcasts
5. The backlog scan may surface it, but only after the suppression interval

**Finding:** The combination of stale subscription + offer expiry + backlog
suppression can cause a seat to miss multiple offer cycles.

## Root cause assessment

The most probable cause of seats with live processes but unclaimed offers is:

1. **Stale MCP server process:** The AionUI conversation finished (model turn
   ended) but the MCP stdio server process is still alive, blocking on a
   subscription. The seat appears alive but has no model consumer to process
   returned events.

2. **Cursor advancement without processing:** The subscription advanced the
   cursor (via `cursor_callback`) even though the model didn't process the
   event. Subsequent waits skip the already-cursored event.

3. **Backlog suppression:** The backlog scan surfaced the offer once but
   suppressed re-surfacing within the interval, so the stale seat doesn't
   see it again.

## Recovery

### Smallest coordinator recovery

For each affected seat:

1. Check if the seat's MCP process is alive but the AionUI conversation is
   finished:
   ```sh
   # Check for stale wait-bridge processes
   ps aux | grep pursers-wait-bridge | grep -v grep
   ```

2. If the process is alive but the conversation is finished, stop the
   stale process:
   ```sh
   # Stop the stale MCP server process (PID from step 1)
   kill <PID>
   ```

3. Start a new conversation in AionUI with the matching Worker or Reviewer
   preset. The new conversation will re-join the board and re-arm the wait
   from the persisted cursor.

4. If the cursor is stale (ahead of unprocessed events), the coordinator
   can reset it:
   ```sh
   # Check the saved cursor
   pursers-wait-bridge status

   # If the cursor needs reset, the coordinator can use:
   # (No direct cursor reset command exists in the CLI.)
   # Instead, delete the seat state file and re-join:
   pursers-wait-bridge forget --board <BOARD_ID> --role <ROLE>
   pursers-wait-bridge join '<DOOR>'
   ```

### Risk

- Stopping the MCP process is safe; it only blocks on a subscription and
  holds no locks. The board will requeue any held offers.
- Forgetting and re-joining creates a new agent_id. Any offers targeted at
  the old agent_id will expire and be re-dispatched to the new one.
- No data loss: tickets, memories, and journal events are stored on Central,
  not in the seat process.

### Verification

After recovery:
1. The new conversation should receive a wait result within one timeout cycle
   (~300 seconds) if there are pending offers.
2. Check the board for the seat's agent_id in the dispatch history.
3. Verify the seat claims and submits work.

## Limitations

- This diagnosis is based on source code analysis, not live process
  inspection. The coordinator should verify the actual process state.
- The wait bridge's `BACKLOG_RESURFACE_INTERVAL_S` and
  `BACKLOG_SUPPRESSION_LIMIT` constants control backlog scan behavior but
  their exact values were not extracted in this bounded read.
- AionUI's internal conversation-to-MCP-process lifecycle management is
  not in this repository; the diagnosis infers behavior from the MCP
  stdio contract and the wait bridge's blocking design.
- No code changes were made. No tests were run. This is a read-only
  diagnosis.
- Private seat paths and credentials were not accessed or recorded.
