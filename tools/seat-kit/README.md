# seat-kit

<!-- release-train: product=5.0.0a26 bridge=0.1.0a16 client=0.1.0a23 -->

Generate a ready-to-use Pursers worker or reviewer seat in one command:

```sh
python tools/seat-kit/seat_new.py \
  --role worker \
  --name worker-a \
  --dest /path/to/worker-a \
  --door '<DOOR>' \
  --client codex
```

`--door` is the preferred alternative to `--central-url`, `--token-file`, and
`--board`. It writes a private door store beneath the generated seat; the
launcher contains no credential-file or CA environment lines. Use
`--allow-remote` to confirm a door whose Central URL is not loopback. The door
role must match `--role`.

The legacy explicit configuration remains available:

```sh
python tools/seat-kit/seat_new.py \
  --role worker \
  --name worker-a \
  --dest /path/to/worker-a \
  --central-url http://127.0.0.1:8766/mcp \
  --token-file /path/to/worker-a.jwt \
  --repo https://github.com/example/Pursers.git \
  --board fullplatts \
  --client codex \
  --tier-max 2 \
  --skills python,docs \
  --no-can-review
```

`--board` is optional: omit it and the seat serves every active registry board
(the CLI binds to the registry board, `--registry-board`, default `pursers`);
name a board to dedicate the seat to that board only.

`--repo` is optional. When supplied, the repository is cloned beneath the seat
using its repository basename. Without it, install `pursers-client` in the
Python environment used by the seat.

`--tier-max`, `--skills`, and `--can-review`/`--no-can-review` write the
dispatch capability environment into `bin/board.sh`. The generated seat also
declares whether it can work, plus its host and optional `--model` and
`--provider`, on every board join.

The destination must be new or empty. The generator creates:

- `bin/board.sh`: role-specific board CLI entry point
- `bin/board.py`: `pursers_client` adapter used by the shell entry point
- `AGENTS.md` and `.goosehints`: identical identity, loop, and governance rules
- the optional repository clone

Use `--upgrade` to regenerate those four managed files in an existing seat.
Every other file is preserved. An upgrade fetches the repository clone and
fast-forwards it only when it is clean, already on the remote default branch,
and has no divergent commits; otherwise it leaves the clone untouched and
prints a warning. `--python` selects the interpreter written to `bin/board.sh`
without resolving its symlink, so a virtual environment keeps its installed
packages. Omitting `--python` during an upgrade preserves the interpreter in the
existing launcher. A bare interpreter must import `pursers_client`, `mcp`, and
`httpx`; generation fails before reporting success when that dependency check
or the generated `board.py --help` self-check fails. The fleet dashboard chooses
the wait-bridge tool environment as the known runtime.

In explicit configuration, token contents are never read by the generator or copied into the seat.
`bin/board.sh` reads the configured token file at runtime and honors
`PURSERS_BOARD`. You may override the generated values with
`PURSERS_TOKEN_FILE` and `PURSERS_CENTRAL_URL`. Remote deployments that use a
private CA may set `PURSERS_CA_FILE`; the launcher then validates that file and
exports it as `SSL_CERT_FILE`.

See [Deployment transport](../../docs/deployment-transport.md) for the local
HTTP and remote forwarding model.

## Commands

Worker seats:

```text
list [--board <id>]
get <TK> [--board <id>]
claim <TK> [--board <id>]
renew <TK> [--board <id>]
submit <TK> <summary> <notes> <files-csv> [--board <id>]
wait --since '<cursor-or-json-map>' [--timeout <seconds>] [--boards registry|home|<id,id>] [--poll]
```

Reviewer seats:

```text
list [--board <id>]
list-all [--board <id>]
get <TK> [--board <id>]
review-claim <TK> [--board <id>]
renew <TK> [--board <id>]
review-release <TK> [reason] [--board <id>]
verify <TK> [--run-suites] [--board <id>]
approve <TK> <notes> [--force-approve-without-evidence] [--board <id>]
reject <TK> <notes> <fix> [--board <id>]
wait --submitted --since '<cursor-or-json-map>' [--timeout <seconds>] [--boards registry|home|<id,id>] [--poll]
```

`list` returns only unclaimed submitted tickets and includes `review_state`.
Use `review-claim` before verification, `renew` every ~5 minutes, and
`review-release` only when abandoning without a verdict. `approve` and
`reject` idempotently ensure that this reviewer holds the lease first; a
`review_already_claimed` conflict means another reviewer won, so return to
push-wait without polling.

`submit` keeps the worker active for review/retry. Include the model used and
real verification output in the notes required by the ticket. For code tickets
whose `required_fields` include `branch_and_commit`, notes must contain exactly
one `branch_and_commit: platform/branch @ <full-40-hex-sha>` line. The seat
fetches that branch from its configured `origin`, verifies the SHA is an exact
commit and the current remote tip, then canonicalizes the submitted
`branch_and_commit` line and adds the matching machine-derived remote tip before
notes truncation. A missing/non-Git route or malformed, nonexistent, stale, or
mismatched SHA fails before `ticket_submit`, so correct the route or evidence
and retry. The submit mutation does not run, but the invocation's normal
`board_join` may renew an already-held lease. Explicit research-only tickets
without that required field retain the no-Git submit path.

Reviewer `verify` fetches and detaches the submitted SHA, compares the commit
stat and paths with `files_changed`, reports every remote branch containing the
SHA, rejects a SHA already on `origin/main`, runs a bounded credential leak
scan, and optionally re-runs allow-listed pytest/unittest commands found in the
ticket evidence. Generic rules cover JWTs, bearer tokens, private-key headers,
API-key shapes, and macOS/Linux/Windows home-directory paths. Operator-specific
regexes are loaded one per line from `~/.pursers/leak-markers.txt`; set
`PURSERS_LEAK_MARKERS_FILE` to override the path. `verify` and `approve` print
only the loaded marker count, never the regexes. An empty marker file is a WARN
to record in `review_notes`, not an approval blocker. Documented fixtures
`/Users/synthetic-user`, `example.com`, and `127.0.0.1` remain exempt. Approval
is refused before any board call unless notes contain
a full 40-hex SHA, a recognizable test tail, `leak-scan: clean|N matches`, and
`model: NAME`. The emergency `--force-approve-without-evidence` flag is enabled
only when an operator explicitly sets
`PURSERS_ALLOW_FORCE_APPROVE_WITHOUT_EVIDENCE=1`; its use is appended to the
review notes. Rejection always requires non-empty fix instructions.

Suite replay accepts `pytest`, `py.test`, or `python[3] -m pytest/unittest`,
optionally prefixed by one relative, worktree-contained `PYTHONPATH=...`
assignment. Shell substitutions, separators, redirects, other environment
assignments, absolute paths, and parent-directory escapes are rejected without
executing the command. Pytest options are explicitly allow-listed. Positional,
`--junitxml`, `--basetemp`, ignore, and deselection paths are resolved even when
the final leaf does not exist, so an outward symlink ancestor is refused before
pytest can write. Pytest module, plugin, and configuration escape options and
every `@argument-file` form are refused before pytest can expand them;
unittest replay requires `discover`, so submitted dotted modules cannot resolve
from the host interpreter environment. Replayed suites receive a minimal
environment: host Python/pytest injection variables are discarded, user-site and
pytest plugin autoloading are disabled, and only an explicitly parsed bounded
`PYTHONPATH` is restored. The verifier uses its own interpreter and a temporary
empty pytest configuration under the detached clone, with root and conftest
discovery bounded to that clone.

## HARD-verify checklist

Before approval:
1. Fetch and detach the exact submitted 40-hex SHA in the seat clone.
2. Compare git show --stat and changed paths with files_changed and ticket scope.
3. Confirm the SHA is on `origin/<submitted-branch>` and never on `origin/main`.
4. Re-run every claimed suite and compare the real result tails.
5. Review the diff against the ticket and its dependencies, including exact field, parameter, and event names.
6. Run the credential leak scan and report clean or the bounded match count.
7. Confirm every required_field is present and truthful.
8. Put the SHA, re-run tails, leak-scan result, and model in review_notes.

Operator-specific leak regexes come from `~/.pursers/leak-markers.txt`, one per line; `PURSERS_LEAK_MARKERS_FILE` overrides that path. Record an empty marker file as a WARN in review_notes, not a blocker. Never print marker values.

Approval notes must contain a full 40-hex SHA, a real `N passed`, `Ran N tests`, or `OK` tail, `leak-scan: clean|N matches`, and `model: NAME`. The emergency flag works only when the operator explicitly sets `PURSERS_ALLOW_FORCE_APPROVE_WITHOUT_EVIDENCE=1`, and its use is appended to review_notes.

Rejecting is normal and cheap; a wrong approval is expensive.

## Wait verb

### How work reaches a seat

On dispatch-enabled boards, a generated worker wakes only for its own work
offer, an expiry or revocation of that offer, or events for a ticket it already
holds. A reviewer wakes only for its own review offer and review-lease events.
The event contains the offer expiry, tier, and required skills. Claim only the
ticket offered to this seat; if it expires, re-arm and wait. Claiming a ticket
offered to another seat prints: `this ticket was offered to another seat; wait
for your own offer`. Boards without dispatch keep legacy broadcast behavior.

Worker seats include:

```text
wait --since '<cursor-or-json-map>' [--timeout <seconds>] [--boards registry|home|<id,id>] [--poll]
```

Reviewer seats use the explicit submitted-work filter:

```text
wait --submitted --since '<cursor-or-json-map>' [--timeout <seconds>] [--boards registry|home|<id,id>] [--poll]
```

The default `--boards registry` path reads the home board's `project_registry`,
joins every active authorized board, and performs one MCP 2026-07-28
`subscriptions/listen` call covering all journal cues. It passes
`acknowledge=False` and `touch=False`, so refetch cannot touch
activity, acknowledge a cursor, renew/reap leases, or otherwise mutate Central.
It never calls `ticket_list` while idle. A generated seat fails closed with a
clear error when its installed `pursers_client` predates this approved pure
subscription API.

Returns a bounded JSON response:
```json
{"new_seq": {"board": 1}, "events": [...], "timed_out": true|false, "waited_s": <float>, "boards": [...], "skipped_boards": {}}
```

Each call reads at most eight catch-up pages and returns at most one relevant
event per board. The timeout covers that catch-up work. When more history
remains, `new_seq` stops at the last processed event; immediate re-arms deliver
the remainder without loss or duplicate delivery.

Each event carries `board_id` and the registered `fleet_clone_dir` when one is
configured (otherwise the legacy `work_dir`). Route `get`, `claim`,
`renew`, `submit`, and review verbs with `--board <id>`. On `timed_out=true`,
re-arm immediately by passing the complete JSON `new_seq` map to `--since`.
Use `--boards home` for the legacy one-board/scalar-cursor path; an explicit
comma-separated list overrides registry selection.

Ticket targets may use the legacy `project-name/path` form or an exact HTTPS
`repository_url` stored on that project registry entry. URL routing is scoped
to the ticket's board and never falls back to a board default. Unknown,
cross-board, ambiguous, missing, and non-git routes fail before claim or review
work begins.

Worker claims are refused with `operator_checkout_read_only` whenever routing
would select an operator-owned `work_dir`. Create the project's fleet clone in
the dashboard Config page first; reviewers continue to verify in temporary
clones.

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
5. **SUBMIT** — Push the candidate branch, copy the exact output of
   `git rev-parse HEAD` into `branch_and_commit`, then run
   `bin/board.sh submit <TK> <summary> <notes> <files-csv>`. If preflight
   reports a moved remote tip, refresh the SHA evidence before retrying.
6. **RE-ARM** — After a successful submit, leave its branch immutable and
   immediately wait for the next eligible ticket; do not wait for review.
7. **RETRY CUES** — On a later rejection cue, get the ticket, follow its fix
   instructions in a fresh candidate branch, resubmit, then re-arm again.

**Never** poll `bin/board.sh list` in a loop. The wait verb blocks on Central's
subscriptions/listen, using zero model turns except the re-arm.
