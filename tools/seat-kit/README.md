# seat-kit

<!-- release-train: product=5.0.0a16 bridge=0.1.0a6 client=0.1.0a14 -->

Generate a ready-to-use Pursers worker or reviewer seat in one command:

```sh
python tools/seat-kit/seat_new.py \
  --role worker \
  --name worker-a \
  --dest /path/to/worker-a \
  --central-url https://127.0.0.1:8766/mcp \
  --token-file /path/to/worker-a.jwt \
  --ca-file /path/to/central-ca.pem \
  --repo https://github.com/example/Pursers.git \
  --board pursers \
  --client codex
```

`--repo` is optional. When supplied, the repository is cloned beneath the seat
using its repository basename. Without it, install `pursers-client` in the
Python environment used by the seat.

The destination must be new or empty. The generator creates:

- `bin/board.sh`: role-specific board CLI entry point
- `bin/board.py`: `pursers_client` adapter used by the shell entry point
- `AGENTS.md` and `.goosehints`: identical identity, loop, and governance rules
- the optional repository clone

The token contents are never read by the generator or copied into the seat.
`bin/board.sh` reads the configured token file at runtime, sets
`SSL_CERT_FILE`, and honors `PURSERS_BOARD`. You may also override the generated
paths with `PURSERS_TOKEN_FILE`, `PURSERS_CA_FILE`, and
`PURSERS_CENTRAL_URL`.

## Commands

Worker seats:

```text
list
get <TK>
claim <TK>
renew <TK>
submit <TK> <summary> <notes> <files-csv>
wait --since <cursor> [--timeout <seconds>] [--poll]
```

Reviewer seats:

```text
list
list-all
get <TK>
approve <TK> <notes>
reject <TK> <notes> <fix>
wait --submitted --since <cursor> [--timeout <seconds>] [--poll]
```

`submit` keeps the worker active for review/retry. Include the model used and
real verification output in the notes required by the ticket.

## Wait verb

Worker seats include:

```text
wait --since <cursor> [--timeout <seconds>] [--poll]
```

Reviewer seats use the explicit submitted-work filter:

```text
wait --submitted --since <cursor> [--timeout <seconds>] [--poll]
```

The default path performs one long MCP 2026-07-28 `subscriptions/listen` wait
through `BoardClient.events()` on the board journal and the seat's exact agent
URI. It passes `acknowledge=False` and `touch=False`, so refetch cannot touch
activity, acknowledge a cursor, renew/reap leases, or otherwise mutate Central.
It never calls `ticket_list` while idle. A generated seat fails closed with a
clear error when its installed `pursers_client` predates this approved pure
subscription API.

Returns a bounded JSON response:
```json
{"new_seq": <int>, "events": [...], "timed_out": true|false, "waited_s": <float>}
```

On `timed_out=true`, re-arm immediately by passing `--since <new_seq>`.

**Poll fallback:** Pass `--poll` to explicitly select a 2-second
`board_catchup(..., touch=False)` loop. Push mode never falls back to polling
implicitly.

**Timeout:** The generated default derives from `--client`:

- `goose`: 300s host timeout -> 270s wait
- `codex`: 620s configured tool timeout -> 560s wait
- `claude`: 21,600s operational rotation -> 21,540s wait
- `generic`: conservative 180s host timeout -> 150s wait

For Goose's opt-in one-hour profile, put this exact line in the extension's
`config.yaml`:

```yaml
timeout: 3600
```

Then run `board.sh wait --timeout 3540 --since <cursor>`. For Codex, configure
`tool_timeout_sec = 620` before using its generated 560-second default.

## Relentless loop

The generated `AGENTS.md` and `.goosehints` contain the relentless loop:

1. **WAIT** — `bin/board.sh wait --since <cursor>` blocks until work arrives.
2. **CLAIM** — `bin/board.sh claim <TK>`.
3. **UNDERSTAND** — `bin/board.sh get <TK>`.
4. **DO** — Work. Renew every ~10 min with `bin/board.sh renew <TK>`.
5. **SUBMIT** — `bin/board.sh submit <TK> <summary> <notes> <files-csv>`.
6. **AWAIT REVIEW** — Keep the ticket slot occupied; on rejection, fix and
   resubmit the same ticket.
7. **RE-ARM** — Only after approval/closure, wait for the next ticket. On a
   timeout, re-arm with the returned cursor.

**Never** poll `bin/board.sh list` in a loop. The wait verb blocks on Central's
subscriptions/listen, using zero model turns except the re-arm.
