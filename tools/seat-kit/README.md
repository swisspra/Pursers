# seat-kit

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
```

Reviewer seats:

```text
list
list-all
get <TK>
approve <TK> <notes>
reject <TK> <notes> <fix>
```

`submit` keeps the worker active for review/retry. Include the model used and
real verification output in the notes required by the ticket.

## Wait verb

Both worker and reviewer seats include a `wait` command:

```text
wait --since <cursor> [--timeout <seconds>] [--poll]
```

Blocks until new work arrives using Central's `subscriptions/listen` mechanism
via `BoardClient.events()`. The default path performs one long subscription
wait on `board://<board>/journal` and never calls `ticket_list` during idle.

Returns a bounded JSON response:
```json
{"new_seq": <int>, "events": [...], "timed_out": true|false, "waited_s": <float>}
```

On `timed_out=true`, re-arm immediately by passing `--since <new_seq>`.

**Poll fallback:** Pass `--poll` to enable a 2-second `board_catchup` loop.
This is **not** the default — the default path uses subscriptions/listen only.

**Timeout:** Default 270s (Goose profile). Derives from the `--client` profile:
- `goose`: 300s host timeout → 270s block
- `codex`: 230s → 200s
- `claude`: 240s → 210s
- `generic`: 180s → 150s

## Relentless loop

The generated `AGENTS.md` and `.goosehints` contain the relentless loop:

1. **WAIT** — `bin/board.sh wait --since <cursor>` blocks until work arrives.
2. **CLAIM** — `bin/board.sh claim <TK>`.
3. **UNDERSTAND** — `bin/board.sh get <TK>`.
4. **DO** — Work. Renew every ~10 min with `bin/board.sh renew <TK>`.
5. **SUBMIT** — `bin/board.sh submit <TK> <summary> <notes> <files-csv>`.
6. **RE-ARM** — Return to WAIT. On timeout, re-arm with the returned cursor.

**Never** poll `bin/board.sh list` in a loop. The wait verb blocks on Central's
subscriptions/listen, using zero model turns except the re-arm.
