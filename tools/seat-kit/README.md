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
