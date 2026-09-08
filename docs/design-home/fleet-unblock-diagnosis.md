# Fleet unblock diagnosis: stale offers and Team-local waiting (resubmit-1)

Baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (origin/main, 5.0.0a25).

## Diagnosis scope

READ-ONLY investigation of why seats with live CLI processes do not claim
Pursers offers. The actual affected-seat wait path is `bin/board.sh wait`
which calls `seat_new._cmd_wait()` (line 746) and, for `--boards registry`,
delegates to `pursers_client.wait_for_boards()` (project_registry.py line 225).
This is distinct from the autonomous MCP wait bridge (`pursers_wait_server.py`)
which serves MCP tool calls; both share the same `BoardClient.events()`
subscription underneath.

No confirmed operational root cause was established. The following are
hypotheses from source analysis that require verification against actual
affected-seat command, process, and schema evidence.

## CLI wait path

### `bin/board.sh wait` → `seat_new._cmd_wait()` (line 746)

The seat CLI wait command:

1. Resolves the cursor from `--since` argument or persisted state
2. For `--boards registry`, calls `wait_for_boards()` with the active
   registry boards
3. For `--boards home`, uses `BoardClient.events()` directly on the home board
4. Prints the result JSON and exits; the relentless loop re-arms with `new_seq`

### `wait_for_boards()` (project_registry.py line 225)

Multi-board wait that:

1. Joins each selected board with `board_join` (caches agent_id per board in
   `_registry_wait_sessions`)
2. Drains catchup pages per board (bounded by `MAX_CATCHUP_PAGES_PER_BOARD`)
3. Opens a single listen subscription across all board journals
4. Returns the first relevant event or a timeout with `new_seq` cursor map

### Event-kind selection

The `submitted` flag (from `--submitted` CLI flag) selects event kinds:

- Worker (default): `{"ticket_created"} | DISPATCH_KINDS` (ticket_offered,
  offer_expired, offer_revoked, review_offered)
- Reviewer (`--submitted`): `SUBMITTED_RELEVANT_KINDS` (submission, review
  lease, dispatch kinds)

### Role resolution (`_resolve_wait_for`, line 4066)

```python
if selected == WAIT_FOR_AUTO:
    if role == "reviewer":
        return WAIT_FOR_SUBMITTED
    if role == "worker":
        return WAIT_FOR_CLAIMABLE
if selected == WAIT_FOR_SUBMITTED and role != "reviewer":
    raise ToolError("wait_for='submitted' requires board:review authorization")
```

Auto resolution correctly maps worker→claimable and reviewer→submitted.
Explicit `submitted` for a non-reviewer raises `ToolError`. Role mismatch
is not a realistic failure mode for correctly configured seats.

## Backlog constants (exact)

| Constant | Value | Source line | Purpose |
| --- | --- | --- | --- |
| `BACKLOG_RESURFACE_INTERVAL_S` | 600 seconds (10 minutes) | line 203-207 | Prevents re-surfacing the same ticket in backlog scans within a 10-minute window |
| `BACKLOG_SUPPRESSION_LIMIT` | 500 entries | line 225 | Maximum backlog fingerprint cache size; oldest evicted when exceeded |

Environment override: `PURSERS_BACKLOG_RESURFACE_INTERVAL_S` (must be > 0;
defaults to 600 if unset or invalid).

## Hypotheses (unverified, require affected-seat evidence)

### H1: Stale CLI process with no model consumer

The `bin/board.sh wait` command is invoked by the seat's relentless loop
(AGENTS.md). When the model turn that drives the loop ends (conversation
"finished"), the CLI process may still be blocking on the subscription.
When the wait eventually returns, there is no model to process the result
and re-arm.

**Evidence needed:** Check whether the affected seats have `bin/board.sh`
processes that are alive but whose parent model/conversation has ended.
The ticket reports that Goose-2/worker-3 acted while persisted conversation
status remained `finished`, which may falsify this hypothesis — if the
seat acted, it had a consumer.

**Status:** Not confirmed. The observation that seats acted while status
was `finished` suggests the relationship between conversation status and
model availability is more complex than a simple "finished = no consumer."

### H2: Cursor advancement without event processing

`BoardClient.events()` uses `cursor_callback` to advance the cursor during
drain. If the cursor advances past an event that the seat never processed
(e.g., the event was deduplicated or filtered), subsequent waits will not
replay it.

**Evidence needed:** Compare the seat's saved cursor value against the
seq of the unprocessed offer event. If cursor > offer seq, the event was
cursored but not processed.

**Status:** Not confirmed. The reviewer reports that reviewer-2 "received
and processed its saved-cursor CLI wait result," suggesting the cursor path
can work correctly.

### H3: Backlog suppression window

The backlog scan surfaces relevant tickets every 600 seconds
(`BACKLOG_RESURFACE_INTERVAL_S=600`). If an offer was surfaced once but
not acted on, the backlog will not re-surface it for 10 minutes. Combined
with offer TTL (typically 10 minutes), the offer may expire before the
backlog re-surfaces it.

**Evidence needed:** Check whether the affected offer's seq was within the
600-second suppression window when the seat's wait was active.

**Status:** Plausible but not confirmed.

### H4: Subscription loss without reconnect

`BoardClient.events()` reconnects on subscription loss with exponential
backoff. If Central is unreachable, the reconnect loop blocks indefinitely.
The seat process appears alive but is not receiving events.

**Evidence needed:** Check Central connectivity from the affected seat's
machine. Check whether the subscription is active or in a reconnect loop.

**Status:** Plausible but not confirmed.

## What was removed from the original report

- **`conversations.status=finished` causal inference:** Removed. The
  reviewer reports that Goose-2/worker-3 acted while persisted conversation
  status remained `finished`, falsifying the inference that `finished`
  means no consumer.
- **Kill/forget/rejoin/new-identity recovery:** Removed. The ticket
  explicitly forbids restarting/interrupting seats. The coordinator says
  not to act on kill/forget/new-identity recovery.
- **Stale MCP stdio process as root cause:** Reframed as hypothesis H1.
  The actual path is `bin/board.sh` → `seat_new._cmd_wait()`, not an
  autonomous MCP tool call.
- **Role mismatch as failure mode:** Corrected. `_resolve_wait_for("auto",
  role)` correctly maps worker→claimable and reviewer→submitted. Explicit
  `submitted` for non-reviewer raises `ToolError`.

## Non-destructive recovery recommendation

No confirmed root cause was established. The smallest non-destructive
coordinator prompt that preserves existing seat identity and cursor:

1. **Send a Team wake message** to the affected seat through the Team
   messaging system. This re-activates the model turn, which will re-enter
   the relentless loop and re-arm the wait from the persisted cursor.

2. **If the seat's wait returned but was not processed:** The next model
   turn should re-read the last wait result or re-arm from the saved cursor.
   No cursor reset is needed if the cursor is correct.

3. **If the cursor is ahead of unprocessed events:** This requires
   coordinator verification. The coordinator can check the seat's cursor
   against the board's latest seq. If the cursor is ahead, the coordinator
   should annotate the affected ticket with the correct cursor value and
   instruct the seat to re-arm from that value.

4. **Do NOT:** kill processes, forget doors, rejoin with new identity,
   restart seats, or use `--poll`/cursor-0 catchups.

## Limitations

- No confirmed operational root cause was established. All findings are
  hypotheses from source analysis.
- No live process inspection was performed. The coordinator should verify
  actual seat command, process, and schema evidence.
- The relationship between AionUI conversation status and model
  availability is not fully understood from this codebase.
- No code changes were made. No tests were run. This is a read-only
  diagnosis.
- Private seat paths and credentials were not accessed or recorded.
- The `wait_for_boards()` multi-board join caching
  (`_registry_wait_sessions`) and its interaction with offer dispatch
  was not fully traced in this bounded read.
