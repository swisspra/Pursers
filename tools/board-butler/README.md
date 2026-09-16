# Board butler (shadow mode)

`board_butler.py` is a single resident coordinator seat that drafts evidence-backed
answers to mechanically checkable coordinator questions. It never answers a
question and never mutates a ticket. Its only Central write is a CAS-protected
merge of `would_answer` findings into `coordinator_findings`.

The policy is intentionally fail-closed. Gate waivers, scope changes, release
actions, membership changes, and registry changes always escalate. Unknown or
incomplete evidence also escalates. Mechanical drafts cite one named product or
repository source: `git merge-base`, `ticket_get`, a ticket annotation, or a
seat capability row.

The runner acquires an exclusive pidfile lock before reading credentials or
opening Central. It joins as a coordinator with `can_work=false` and
`can_review=false`, then refuses to run if another working or reviewing seat
shares its principal. Deploy it with its own credential and state paths; do not
reuse a worker or reviewer token.

One-cycle shadow validation:

```sh
PYTHONPATH=packages/client/src \
python3 tools/board-butler/board_butler.py \
  --url https://central.example.invalid/mcp \
  --token-path /PATH/TO/board-butler.jwt \
  --home-board pursers \
  --agent-name board-butler-1 \
  --repo /PATH/TO/Pursers \
  --pid-file /PATH/TO/state/board-butler.pid \
  --cursor-file /PATH/TO/state/board-butler.cursor.json \
  --once --dry-run
```

Without `--once`, the process holds one reconnecting journal/seat subscription
and sleeps inside the push stream until a coordinator-question cue arrives.
There is no polling fallback, repeated ticket list, or cursor-0 catch-up. The
cursor file is reused across restarts. `--dry-run` prints the proposed finding
and makes no Central write.

Draft caps default to five per hour and two per ticket. They may be set with
`--drafts-per-hour` and `--drafts-per-ticket`, or with the equivalent
`board_butler.drafts_per_hour` and `board_butler.drafts_per_ticket` keys in
`coordinator_config`. A cap hit is reported as `butler_rate_limited`.

Run the suite with:

```sh
python3 -m pytest -q tools/board-butler/tests
```
