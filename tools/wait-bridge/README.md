# Pursers wait bridge

`pursers_wait_server.py` is a stdio MCP server that exposes blocking
`a2a_wait` and parsed registry lookup through `project_registry_get`. MCP v2
`subscriptions/listen` is the default; `PURSERS_WAIT_MODE=poll` is an explicit
compatibility fallback. Push subscribes to `board://<board>/journal` plus the
per-seat URI, then uses `BoardClient.events()` for reconnect/dedup and an
authoritative catchup. A Central with `board_catchup(touch=False)` gets a pure
refetch; older Centrals fall back loudly to `ack=False` for that deployment.
Keep it on stdio; wrapping it in an HTTP transport adds request timeouts that
defeat the wait behavior.

## Requirements

- Python 3.11 or newer
- `mcp==2.1.1`
- the repository's `packages/client/src` on `PYTHONPATH`
- an On Board bearer token with access to the configured board

The imported source was reconstructed from board memories `MEM-000001` and
`MEM-000002`. Before the instance-naming change, the 14,252-byte file matched
SHA-256 `1a0981ec6cc47aed8eeb5e8f488bef260ab6b5fd5c7c88e2cd99604654103e1a`.

## Seat setup

Install the bridge with `uvx` or `pipx`, then paste the single door issued by
your Pursers administrator:

```sh
pursers-wait-bridge join '<DOOR>'
```

The command validates the door, refuses non-loopback Central URLs unless you
confirm them with `--allow-remote`, writes private `doors.json` state with mode
`0600`, onboards the seat, and reports the board, role, seat name, push probe,
and verifier outcome. It never prints the embedded credential. Add
`pursers-wait-bridge` to the host as a stdio MCP server with no environment
block. A host that needs an explicit executable can use the `uvx
pursers-wait-bridge` or `pipx run pursers-wait-bridge` command form.

Use `--name NAME` for an explicit seat name. Otherwise the first unused
`<role>-<short-hostname>-<n>` name is recorded. Central's active-name collision
refusal is returned unchanged; the bridge never silently takes over a live
seat. Door maintenance is explicit and redacted:

```sh
pursers-wait-bridge status
pursers-wait-bridge join --rotate '<REPLACEMENT_DOOR>'
pursers-wait-bridge forget --board BOARD --role worker
```

`--rotate` requires an existing entry with the same board and role. `status`
shows board, role, key ID, expiration, recorded seat names, and current push
mode, but not URLs or credentials.

Legacy explicit environment remains supported. Runtime values resolve in this
order:

| Value | First choice | Stored-door fallback |
| --- | --- | --- |
| Central URL | `ONBOARD_CENTRAL_URL` | `u` |
| credential | `ONBOARD_CENTRAL_TOKEN`, then `ONBOARD_CENTRAL_TOKEN_FILE` | `t` |
| board | `ONBOARD_BOARD_ID` | `b`, requiring the only stored board when omitted |
| role | `PURSERS_ROLE` | `r`, requiring the only stored role when omitted |
| seat name | `ONBOARD_AGENT_NAME` | first unused `<role>-<short-hostname>-<n>` in `doors.json` |

Each explicit value wins independently. This allows, for example, an explicit
URL with the stored board, role, and credential. Ambiguous stored boards or
roles fail closed and tell the operator which selector to set.

## Environment

| Variable | Required | Purpose |
| --- | --- | --- |
| `ONBOARD_CENTRAL_TOKEN` | no | Explicit Central credential; overrides `ONBOARD_CENTRAL_TOKEN_FILE` and stored-door state. |
| `ONBOARD_CENTRAL_TOKEN_FILE` | no | Explicit credential file; used before stored-door state when the direct credential is absent. |
| `ONBOARD_CENTRAL_URL` | no | Central MCP URL; defaults to `http://127.0.0.1:8766/mcp`. |
| `ONBOARD_BOARD_ID` | no | Board ID; defaults to `pursers`. |
| `ONBOARD_AGENT_NAME` | no | Base board identity; defaults to `pursers-wait-bridge`. |
| `ONBOARD_AGENT_INSTANCE` | no | Stable per-instance suffix, such as `window-a`. |
| `PURSERS_ROLE` | no | Explicit seat role: `worker`, `reviewer`, `orchestrator`, or `coordinator`. When omitted, Central maps reviewer membership to `reviewer`; admin/member membership maps to `worker`. |
| `PURSERS_WAIT_MODE` | no | `push` (default) or explicit compatibility `poll`; a subscription error polls only that board for the current call and push is retried on re-arm. |
| `PURSERS_CENTRAL_CONNECTION_CAP` | no | Process-wide Central connection ceiling; defaults to `4` and accepts `1`-`64`. One slot is reserved for ordinary calls and excess board subscriptions fall back to polling with a clear stderr warning. |
| `PURSERS_KEEPALIVE_IDLE_LIMIT_S` | no | Maximum seconds since this stdio session's last model tool call before background lease renewal pauses. Defaults to three times each claim's live TTL. |
| `PURSERS_BACKLOG_RESURFACE_INTERVAL_S` | no | Seconds before an unchanged open broadcast ticket may wake the same idle identity again; defaults to `600`. |
| `PURSERS_HOST` | no | `codex` (default), `codex-cli`, `goose`, `claude-code`, `claude-desktop`, or `headless`; selects the safe call ceiling. |
| `PURSERS_HOST_TIMEOUT_S` | no | Explicit host/runner deadline in seconds; overrides the named profile. |
| `PURSERS_TIER_MAX` | no | Maximum dispatch tier (`1`-`3`) declared when the seat joins. |
| `PURSERS_SKILLS` | no | Comma-separated dispatch skills declared when the seat joins. |
| `PURSERS_CAN_REVIEW` | no | Boolean reviewer capability declared when the seat joins. |
| `PURSERS_CAN_WORK` | no | Boolean worker capability declared when the seat joins. |
| `PURSERS_MODEL` / `PURSERS_PROVIDER` | no | Optional model and provider metadata included in the declaration. |
| `PURSERS_BRIDGE_STATE_DIR` | no | Directory containing `doors.json`; defaults to the bridge state directory under the user's Pursers data. |

For local HTTP, remote port forwarding, and public-certificate guidance, see
[Deployment transport](../../docs/deployment-transport.md).

## Doors

A door is one shared bearer credential for a board and seat role. Every
conversation still joins with its own `agent_name`, so worker or reviewer seats
that enter through the same door remain distinct Central agents. The lead can
keep a separately named credential. Treat the complete `prs1.…` door string as
a secret: it packages the exact Central URL, board, role, and signed token into
the one value a seat setup flow needs.

Create or refresh the current worker door (the command prints only the door
string):

```console
pursers-door issue --board BOARD --role worker \
  --central-url http://127.0.0.1:8766/mcp \
  --jwks /PATH/TO/jwks.json --keys-dir /PATH/TO/door-keys
```

Use `--role reviewer` for the reviewer door. A named lead credential uses
`--named --sub NAME --scope 'board:read board:write board:review'`. Private
RSA-2048 keys are stored under `--keys-dir` with mode `0600`; the JWKS contains
only public keys and non-secret listing metadata.

Rotation creates the next versioned key and removes the prior public key in one
atomic JWKS replacement:

```console
pursers-door rotate --board BOARD --role worker \
  --central-url http://127.0.0.1:8766/mcp \
  --jwks /PATH/TO/jwks.json --keys-dir /PATH/TO/door-keys
pursers-door list --jwks /PATH/TO/jwks.json
```

Rotation is revocation: Central reloads the JWKS for every verification and
there is no separate revocation list, so a token signed by the removed `kid`
fails immediately. `revoke-kid KID` removes an individual public key. `list`
and `decode` show only board/role, `kid`, and expiry metadata and never render
token material.

Keep Central on loopback when practical. Remote seats should reach it through
the port-forward pattern in [Deployment transport](../../docs/deployment-transport.md)
and use the exact forwarded Central URL in the issued door.

With no `ONBOARD_AGENT_INSTANCE`, the effective name is exactly
`ONBOARD_AGENT_NAME`, preserving the single-instance behavior. When the value
is set, the effective name is `<ONBOARD_AGENT_NAME>-<ONBOARD_AGENT_INSTANCE>`.
Give each host window a unique, durable instance value and reuse that value
after restarts. The explicit value is the stability anchor; the bridge does
not guess window identity or create lock files that can swap identities when
processes restart in a different order.

## Connection hygiene

The bridge keeps one HTTP client for its Central URL for the life of the stdio
process. Ordinary calls and subscription reconnects share that client's
bounded pool instead of creating a new pool per wait. The default
four-connection ceiling reserves one connection for ordinary calls and permits
up to three concurrent board subscriptions; an additional subscription logs
`Central connection cap hit` and uses the existing per-board poll fallback.

Some desktop hosts launch a fresh stdio bridge for every conversation and do
not necessarily terminate old children when the host restarts. Those orphaned
processes each retain their own bounded pool, so the per-process cap is not a
substitute for process cleanup. Stop the host first, then inspect only bridge
processes and their Central sockets:

```sh
pgrep -af 'pursers_wait_server.py'
lsof -nP -a -p PID -iTCP
```

After confirming a listed PID belongs to an obsolete bridge, stop it with
`kill PID`; use the host's normal process manager for managed services. Restart
the host and confirm that only the expected current bridge processes remain.

## Per-call identities

`a2a_wait` also accepts an optional `agent_name`. Omitting it uses the
process-level identity above with the original behavior. Supplying it lets one
bridge process and one Central connection serve multiple session identities:

```text
a2a_wait(since_seq=0, project="PROJECT_PLACEHOLDER", agent_name="session-a")
```

Holder updates use the same push path as offers. A worker or reviewer receives
`reason="held_ticket_update"` when its held ticket is annotated, rejected,
cancelled, parked/unparked, answered by a human, or loses a lease. Refetch the
ticket and act on its authoritative state before re-arming.

Broadcasts return `reason="broadcast"`. Workers may claim an OPEN work
broadcast, and reviewers may review-claim a SUBMITTED review broadcast, only
after a refetch confirms `dispatch_state.state=broadcast` and no live offer to
another seat. Central rejects races; never claim a ticket with a live offer to
someone else.

`wait_for="auto"` is the default: the effective role returned by Central is
used, so a seat declared or membership-defaulted as `reviewer` waits for
`submitted` tickets, while a `worker` waits for `claimable` tickets.
Coordinator and orchestrator seats must select an explicit view. Token scopes
authorize actions but never select the wait mode. Callers
may select `claimable` explicitly; selecting `submitted` requires a joined
reviewer seat, whose join also requires `board:review`. Reviewer filtering ignores
ticket-created/claimed noise and wakes on submissions, resubmissions, and
review-lease changes.

Codex- and Goose-managed seats set `PURSERS_REQUIRE_TOKEN_MATCH=1` and copy the
seat token file value into the bridge's private host-config env block. Their
launcher compares that explicit value with the token file used by the bridge
and refuses startup when they differ. A missing connector value reports
`connector token not visible to the bridge process; see Codex env forwarding`;
a present mismatch reports `split identity`. Token values are never included
in errors or logs. Doctor also launches the exact managed command and env block,
opens an MCP session, calls `project_registry_get`, and compares the connector
and bridge principals at Central. Regenerate the seat configuration and restart
the host after changing either token source.

An explicit identity is joined when its call starts. Joins are stateless and
idempotent; the bridge deliberately keeps no mutable join cache and does not
rate-limit them. It never changes the shared client's process-level
`agent_name` or joined identity. Catchup, relevance filtering, backlog scans,
and heartbeat selection instead receive the call-local name and deterministic
agent ID explicitly. If Central reports that an explicit identity was handed
off, the bridge rejoins it once and retries catchup once.

Central's `board_join` may reap expired leases across the board, even though an
existing active identity does not produce another join journal event. A newly
used name also was not a recipient of old journal entries. It can recover
currently `open` work through the bridge's backlog scan, but it cannot discover
old non-open history through `a2a_wait`.

## How work reaches a seat

When any capability variable above is present, the bridge declares that seat
on join and again after every subscription reconnect. A dispatch-enabled board
then wakes a worker only for its own `ticket_offered` event, offer expiry or
revocation, and events for tickets it already holds. A reviewer waiting for
submitted work wakes only for its own `review_offered` event and its review
lease lifecycle. The returned `reason` is `offer`, and the event includes
`offer: {ticket_id, board_id, expires_at, tier, skills_required}`.
Offer addressing is checked before ticket refetch; a refetch failure discards
the cue instead of waking a seat that was not proven to own it.

Do not claim an unoffered ticket. If an offer expires or is revoked, re-arm and
wait for the next offer. When none of the capability variables is set, the
seat remains legacy and open/backlog broadcast behavior is unchanged.

## Multi-board response

Pass `boards` to operate one worker identity across several boards without
opening another HTTP client:

```text
a2a_wait(
  boards=["project-a", "project-b", "invite-only-board"],
  since_seq={"project-a": 12, "project-b": 34, "invite-only-board": 0},
  agent_name="pool-worker"
)
```

Omitting `boards` permanently selects the original single-board path: its
`new_seq` remains an integer, `resynced` remains a boolean, events are not
tagged, and `skipped_boards` is absent. With `boards` present, the response is:

```json
{
  "new_seq": {"project-a": 13, "project-b": 34, "invite-only-board": 0},
  "events": [
    {"board_id": "project-a", "kind": "ticket_created", "ticket_id": "TK-PLACEHOLDER"}
  ],
  "waited_s": 0.0,
  "timed_out": false,
  "reason": "journal",
  "resynced": {"project-a": false, "project-b": false, "invite-only-board": false},
  "skipped_boards": {"invite-only-board": "access denied reason"}
}
```

`new_seq` includes every requested board and must be passed back unchanged on
re-arm. Each board keeps its own cursor, generation token, deterministic agent
ID, filtering, and entry backlog scan. Events always carry `board_id`.
Invite-required boards that the bearer cannot access are skipped. In push
mode, each accessible board subscribes independently to
`board://<board_id>/journal` and `board://<board_id>/agent/<agent_id>`; an
authoritative event advances only its board, while a failed subscription
degrades only that board to polling for the current call. The entry backlog
scan also snapshots leases held by the exact derived agent ID. Background
keepalive renews only those exact ticket IDs at about 40% of their current TTL.
It continues outside `a2a_wait` only while this stdio session has model tool
activity within the configured idle limit. An in-progress `a2a_wait` is live
activity. After the limit (default: three claim TTLs), renewal pauses, logs and
returns a `lease_keepalive_paused` cue with `keepalive paused: model idle`, and
lets the lease lapse. The next tool call resumes renewal only if the same
authenticated identity still holds the lease.
scan also snapshots leases held by the exact derived agent ID. During the live
wait, Central discovery is limited to one bounded backlog scan per resurfacing
cadence; only snapshotted ticket IDs receive `lease_renew`, at
`min(300s, ttl/3)`.

## Project registry

The home board stores one string-valued board-state entry under the namespaced
key `project_registry`. Its value is JSON with this schema:

```json
{
  "schema_version": 1,
  "projects": {
    "project-a": {
      "board_id": "project-a-board",
      "work_dir": "/ABSOLUTE/PATH/TO/PROJECT-A",
      "work_dir_owner": "operator",
      "fleet_clone_dir": "/ABSOLUTE/PATH/TO/PURSERS-FLEET/clones/project-a",
      "repository_url": "https://example.test/org/project-a",
      "status": "active"
    },
    "project-b": {
      "board_id": "project-b-board",
      "work_dir": "/ABSOLUTE/PATH/TO/PROJECT-B",
      "status": "paused"
    }
  }
}
```

`board_state_update` receives that serialized JSON string as `value`.
`project_registry_get()` reads the home board and returns the parsed object
directly in the same `{schema_version, projects}` shape, so a worker can map a
claimed ticket's board to its `fleet_clone_dir`. An omitted `work_dir_owner`
defaults to `operator`; seats must not claim work routed to that checkout until
a fleet-owned clone is configured. Legacy ticket targets keep the
`project-name/path` form. A ticket may instead use the exact HTTPS
`repository_url`; URL targets never fall back to another project or to a board
default.

### Registry administration CLI

Use `registry_admin.py` to validate, inspect, and edit the registry without
calling raw board-state tools. It reads the current document, validates the
complete schema before any mutation, writes to the home `pursers` board, then
reads back and compares the stored document. A mismatch exits non-zero and
prints a diff. Each write includes the SHA-256 of the document that was read,
so a concurrent registry change aborts instead of being overwritten. The
bearer token is read only from `ONBOARD_CENTRAL_TOKEN` and
is never printed.

```sh
python tools/wait-bridge/registry_admin.py show
python tools/wait-bridge/registry_admin.py add project-a \
  --board-id project-a-board --work-dir /ABSOLUTE/PATH/TO/PROJECT-A \
  --work-dir-owner operator \
  --fleet-clone-dir /ABSOLUTE/PATH/TO/PURSERS-FLEET/clones/project-a \
  --repository-url https://example.test/org/project-a
python tools/wait-bridge/registry_admin.py set-repository-url project-a \
  https://example.test/org/project-a
python tools/wait-bridge/registry_admin.py pause project-a
python tools/wait-bridge/registry_admin.py activate project-a
python tools/wait-bridge/registry_admin.py remove project-a
```

`add` refuses an existing name unless `--force` is supplied. All mutations
refuse malformed current state, unknown names, relative work directories, and
empty board IDs before writing. Repository URLs must be credential-free HTTPS
URLs, and one board cannot register the same active URL twice. Use
`set-repository-url` to add URL routing without replacing the rest of an entry.
`remove` prints the removed entry so it can be restored by hand. Override the default Central URL with
`ONBOARD_CENTRAL_URL` or the global `--central-url` option.

### Seat administration CLI

`seat_admin.py` provisions a principal across registry boards, applies the
reviewer runbook in member-add-before-role order, and verifies every write by
reading membership back. Token minting remains operator-run; the tool accepts
only the resulting principal ID and token file path, and never reads the target
seat token file or prints any JWT.

Successful adds persist a validated `seat_registry` definition on the home
board (name, principal, intended role, and registry/explicit board mode). This
lets `check` report a pending seat and lets `new-board` provision it before the
seat's first agent join. Existing memberships are read before writes: member
and reviewer roles are reused, admins are preserved, and incompatible role
requests fail closed.

```sh
python tools/wait-bridge/seat_admin.py add \
  --name worker-a --role worker --boards registry \
  --principal PR-PLACEHOLDER --token-path /PATH/TO/TOKEN
python tools/wait-bridge/seat_admin.py add \
  --name reviewer-a --role reviewer --boards board-a,board-b \
  --principal PR-PLACEHOLDER --token-path /PATH/TO/TOKEN
python tools/wait-bridge/seat_admin.py check --name worker-a
python tools/wait-bridge/seat_admin.py new-board --board board-new
python tools/wait-bridge/seat_admin.py retire \
  --name worker-a --boards registry
python tools/wait-bridge/seat_admin.py dedupe \
  --name worker-a --keep-principal PR-KEEP
python tools/wait-bridge/seat_admin.py dedupe \
  --name worker-a --keep-principal PR-KEEP --commit
python tools/wait-bridge/seat_admin.py prune-stale \
  --older-than-days 30 --dry-run --protect worker-a
python tools/wait-bridge/seat_admin.py prune-stale \
  --older-than-days 30 --commit --protect worker-a
```

`add` rejects a seat name already used on any active registry board unless
`--force` is explicit. `new-board` discovers existing principals and roles
from active registry boards; it contains no board or project allowlist.

`retire` uses Central's principal membership removal, which clears the
principal's seats from the selected board pool while preserving tickets,
journal history, and other durable board data. It refuses every mutation when
the principal holds an active work or review claim. A seat seen within the
stale threshold (300 seconds by default) also requires `--force`. Duplicate
names across principals require `--principal`; registry-mode seat definitions must be
retired with `--boards registry`. Each removed membership is read back from
both membership and agents projections before success is printed.

`dedupe` resolves one seat name used by multiple principals across every active
fleet board. It keeps the named principal and plans retirement of all others.
Dry-run is the default and reports active work/review claims in the plan;
`--commit` refuses admin principals, either claim kind, or incomplete snapshots,
then removes and verifies each membership.

`prune-stale` aggregates each principal's latest seat activity across every
active registry board. It excludes reviewer/admin roles, names supplied by
repeatable `--protect`/`--protected` flags (comma-separated names are accepted),
unknown timestamps, recent seats, and memberships without complete agent
activity evidence on every board. Role aggregation and removal targets come
from membership rows even when `agent_names` is empty. Active work/review
claims are included in the plan as blockers and make `--commit` fail before
any write.
Dry-run is the default
and performs no membership or seat-registry writes; `--commit` executes the
printed plan, verifies every board read-back, and removes matching durable seat
definitions. Run live cleanup only with an operator admin token and only after
reviewing the dry-run plan.

If a later board removal or read-back fails after earlier removals succeeded,
the command exits non-zero after printing a structured partial-failure record.
That record contains every completed verified read-back, the failed and pending
boards, and confirmation that `seat_registry` was not changed; backend error
details are not printed.

### Registry doctor CLI

`registry_doctor.py` performs a read-only, registry-wide health check. It
checks Central authentication, active project work directories and integration
refs, bounded board snapshots, duplicate or stale seats, expired claims,
review backlog, coordinator freshness, and the bridge stats file. The default
human table and `--json` report contain only bounded details; exit codes are
`0` for PASS, `1` for WARN, and `2` for FAIL.

Each check reports both a runtime `status` and its declared `severity` and
`scope`. Overall health is computed only from FAIL-class checks, so advisory
WARN/INFO checks remain visible without declaring a fleet outage.

| Severity | Affects OVERALL | Intended use |
| --- | --- | --- |
| `FAIL` | Yes | Fleet-critical reachability, registry, board, project, snapshot, and complete ticket-scan checks |
| `WARN` | No | Actionable hygiene such as duplicate/stale seats, claim age, review backlog, and missing clone with no recent fleet work |
| `INFO` | No | Operator-owned state intentionally outside the fleet |

Ticket checks query each active status separately at Central's maximum bounded
limit and report exact omitted counts by board and status. Snapshot reads also
use the maximum byte bound. Duplicate-seat rows include principal, last-seen,
retire-candidate evidence, and the exact `seat_admin.py retire` command.

Pass a token file path rather than a token value. `PURSERS_DOCTOR_TOKEN_PATH`
or `ONBOARD_TOKEN_FILE` can supply the path, and `PURSERS_BRIDGE_STATS` can
override the default adjacent `bridge-stats.json` path.

```sh
python tools/wait-bridge/registry_doctor.py --token-path /PATH/TO/TOKEN
python tools/wait-bridge/registry_doctor.py --token-path /PATH/TO/TOKEN --json
```

Registry entries default to integration ref `main`. A future-compatible
`integration_ref` string can override it; `git_repo: false` explicitly marks a
project work directory as intentionally non-git.

Set `fleet: false` on an active registry entry for an operator-only project.
The doctor reports it as INFO, excludes its board and work directory from fleet
health/routing, and never fails because its `fleet_clone_dir` is absent. The
admin CLI can create this shape with `registry_admin.py add ... --operator-only`.

After Central is deployed with board-state support for the board's scrub
profile, seed and verify the initial registry with the bridge environment:

```sh
ONBOARD_CENTRAL_TOKEN=TOKEN_PLACEHOLDER \
  tools/wait-bridge/.venv/bin/python \
  tools/wait-bridge/seed_project_registry.py
```

The script writes the operator-supplied initial project entries from the
registry JSON file, reads the state back, fails if it differs, and prints the
verified parsed JSON. Never commit or print the real bearer token.

Pass the sentinel `boards="registry"` to read the registry once at the start
of that `a2a_wait` invocation. The bridge selects all active project board IDs,
deduplicates them in registry order, and always puts the configured home board
first. It never refetches the registry while the call is blocked:

```text
a2a_wait(
  boards="registry",
  since_seq={"pursers": 20, "project-a-board": 7},
  agent_name="pool-worker"
)
```

A valid sentinel call uses the multi-board response documented above. An
explicit list never consults the registry, and omitting `boards` retains the
original single-board behavior. If the registry is missing, unreadable, or
malformed, the sentinel call waits only on the home board and returns the
original single-board fields plus `registry_warning`; if `since_seq` was a
map, the home board's cursor is preserved:

```json
{
  "new_seq": 20,
  "events": [],
  "waited_s": 180.0,
  "timed_out": true,
  "resynced": false,
  "registry_warning": "project_registry unavailable; using 'pursers' only: reason"
}
```

## Legacy explicit-environment connector examples

Use placeholder paths and secrets as shown, then substitute values locally.
For a second window, duplicate the entry and change only the connector name
and `ONBOARD_AGENT_INSTANCE` to another stable value.

```json
{
  "mcpServers": {
    "pursers-wait-window-a": {
      "command": "/ABSOLUTE/PATH/TO/PYTHON",
      "args": [
        "/ABSOLUTE/PATH/TO/REPOSITORY/tools/wait-bridge/pursers_wait_server.py"
      ],
      "env": {
        "PYTHONPATH": "/ABSOLUTE/PATH/TO/REPOSITORY/packages/client/src",
        "ONBOARD_CENTRAL_URL": "https://CENTRAL_HOST.example/mcp",
        "ONBOARD_BOARD_ID": "BOARD_ID_PLACEHOLDER",
        "ONBOARD_CENTRAL_TOKEN": "TOKEN_PLACEHOLDER",
        "ONBOARD_AGENT_NAME": "purser-worker",
        "ONBOARD_AGENT_INSTANCE": "window-a"
      }
    }
  }
}
```

```toml
[mcp_servers.pursers-wait-window-a]
command = "/ABSOLUTE/PATH/TO/PYTHON"
args = ["/ABSOLUTE/PATH/TO/REPOSITORY/tools/wait-bridge/pursers_wait_server.py"]

[mcp_servers.pursers-wait-window-a.env]
PYTHONPATH = "/ABSOLUTE/PATH/TO/REPOSITORY/packages/client/src"
ONBOARD_CENTRAL_URL = "https://CENTRAL_HOST.example/mcp"
ONBOARD_BOARD_ID = "BOARD_ID_PLACEHOLDER"
ONBOARD_CENTRAL_TOKEN = "TOKEN_PLACEHOLDER"
ONBOARD_AGENT_NAME = "purser-worker"
ONBOARD_AGENT_INSTANCE = "window-a"
```

Never commit real bearer tokens or paste them into logs. Prefer the host's
secret storage when available, and restrict access to any local configuration
file that contains a token.

## Worker loop

Call `a2a_wait` with the last returned `new_seq`. On entry, it drains new
journal events in pages and resolves ticket relevance with one bounded active
ticket projection plus keyed batches only for IDs missing from a truncated
projection. The bridge verifies Central's echoed `ticket_ids` filter before
treating missing keyed results as authoritative, so older Centrals that ignore
the filter cannot stall replay. It also scans the currently open ticket
projection, so work older than the cursor still wakes the worker. Backlog cues use
`source="backlog_scan"`, carry no fabricated journal sequence, and leave
`new_seq` governed only by the real journal. An unchanged backlog ticket is
suppressed only until the per-identity cadence expires (10 minutes by default),
then it may wake that idle seat again. Dedupe still applies within one wait
call; another identity has an independent cadence. A bridge restart may surface
the ticket immediately. `reason` reports `journal`,
`backlog`, or `timeout`. A replay over 200 events is reduced to the latest
event per ticket and capped at 200 returned events; `compacted`, `dropped`, and
`event_counts` describe that summary. Omitting `since_seq` starts from and
advances Central's persisted cursor so a restart does not replay the same
history. Persisted-cursor pages are acknowledged as they are fully received.
Projection truncation, timeout, or failure therefore returns the processed
cursor and emits candidate cues with `projection_state="unprojected"`, plus a
`ticket_projection_unprojected` warning, instead of replaying the same page.
`partial=true` identifies that bounded projection state or a catch-up deadline.
A cursor beyond the journal head is clamped and reported under `warnings`
instead of failing. On `timed_out=true`, re-arm
immediately. Every event is a cue to refetch and claim current board state.
The bridge renews held leases while an `a2a_wait` is blocking and for a bounded
idle window after other tool calls on the same stdio session. Long-running work
should still call Central's `lease_renew` directly; Central records whether the
latest renewal came from the model or the bridge keepalive.

The requested `timeout_s` is capped at `host_timeout - margin`, where
`margin=min(60,max(30,ceil(10% of host_timeout)))`; Claude Desktop uses at
least 40s. Defaults are Codex/Codex CLI 620s/560s, Goose 300s/270s, Claude
Desktop 240s/200s, and Claude Code/headless 21,600s/21,540s. A normal timeout
returns `timed_out=true`; re-arm immediately with the returned cursor. Claude
Code receives a progress notification every 300s so its 30-minute stdio idle
timer does not cancel a healthy long wait. Progress never extends the hard
deadline.

For a per-call identity, the entry snapshot exact-filters
`claimed_by_agent_id`. This prevents substring matches such as `session-a` and
`session-a-2` from crossing over. Central authorizes `lease_renew` at principal
scope, not per agent identity, so exact ticket selection prevents accidental
renewals by the bridge but is not a server-side identity-isolation guarantee.

`bridge-stats.json` schema 4 keeps old bridge-to-Central request/response bytes
under `days` and legacy poll samples under `poll_cycles`. Model-visible cost is
separate under `model_wait`: per-seat UTC-hour counts of `a2a_wait` returns and
the exact serialized result bytes inserted into model context. The fleet
dashboard shows current-hour returns/bytes and 24-hour cue/timeout outcomes.
Subscription failures are retained under `push_unavailable` until that seat
records a healthy push return, so poll fallback is visible outside stderr.

Run the bridge tests with:

```sh
python -m unittest discover -s tools/wait-bridge/tests -v
```

Run the disposable private end-to-end probe with:

```sh
python tools/wait-bridge/tests/question_bridge_probe.py
```

## Coordinator questions

The bridge exposes Central's durable, non-pausing coordinator question flow
through the already configured credential and joined seat identity:

- `ticket_question_ask(ticket_id, message, kind[, message_id, in_reply_to])`
  is available only to a worker holding the work lease or a reviewer holding
  the review lease. `message_id` makes a retry idempotent.
- `board_question_inbox([state, ticket_id, limit])` is available only to the
  registered project coordinator identity.
- `ticket_question_answer(ticket_id, question_id[, action, message])` accepts
  with `action="accept"` or replies with `action="answer"`. The bridge computes
  `host_binding` inside `BoardClient`; it is never a tool argument or result.
- `board_question_wait(since_seq[, timeout_s, ticket_id])` waits for a
  coordinator question, and `ticket_question_wait(ticket_id, question_id,
  since_seq[, timeout_s])` waits for the correlated answer. Both use the
  existing journal/seat resource subscription, durable cursor and reconnecting
  event stream. They do not poll. Preserve each returned `new_seq`.

Question tools reject every undeclared argument, including credential,
binding, identity, board and project overrides. Central remains authoritative
for the ticket's project, lease holder, registered coordinator and projected
question visibility. A successful wait reports `delivery=protocol_delivered`
and `model_continuation=host_managed`: MCP delivery is proven, but whether a
host schedules another model turn is a separate host behavior.

Later host setup requires one stdio bridge process per explicit seat identity.
Configure the existing MCP server entry with `/PATH/TO/PYTHON` and
`/PATH/TO/REPOSITORY/tools/wait-bridge/pursers_wait_server.py`, then provide
`PYTHONPATH=/PATH/TO/REPOSITORY/packages/client/src`,
`ONBOARD_CENTRAL_URL`, `ONBOARD_BOARD_ID`, `ONBOARD_AGENT_NAME`, and
`PURSERS_ROLE`. Supply `ONBOARD_CENTRAL_TOKEN` through the host's private
environment or secret facility; never put it in a prompt or tool call. Also
declare the existing seat capabilities exactly: workers use
`PURSERS_CAN_WORK=true,PURSERS_CAN_REVIEW=false`, reviewers use the inverse,
and coordinators use both false. Restart/reload that host connector only after
the operator approves its profile change; this repository does not edit an
installed profile.

| Host surface | Protocol delivery | Model continuation |
| --- | --- | --- |
| Python MCP SDK / stdio disposable probe | Verified: ask, inbox, accept, rotated-credential reconnect, answer and correlated waits | Probe drives the next call explicitly |
| Codex / Codex CLI stdio | Supported by the same stdio tool contract; host profile change still required | Host-managed; not claimed by the disposable probe |
| Claude Code / Claude Desktop stdio | Supported by the same stdio tool contract; host profile change still required | Host-managed; not claimed by the disposable probe |
| Goose / AionUi imported stdio | Supported by the same stdio tool contract; host profile change still required | Host-managed; not claimed by the disposable probe |

The private probe uses four separate short-lived JWT credentials against a
disposable loopback Central. It verifies worker and reviewer asks, coordinator
accept/reply after credential rotation, exact answer correlation, role and
capability preservation, and rejection of secret or routing overrides. It is
not evidence of a run in any intended desktop host.

## Human requests (needs_human)

A ticket blocked on a human calls Central's `ticket_request_human(message,
kind, requested_schema[, url])`, releases its work lease, and parks in the
`needs_human` state with a structured question. The bridge delivers the
question to the human through the host they already sit in:

- `board_human_requests(boards="registry" | list, answer=None)` — without
  `answer` it lists pending requests (`ticket_id`, board, `message`, `kind`,
  `asked_by`, schema summary). Under the 2026-07-28 protocol, the client must
  declare elicitation on each request in
  `_meta.io.modelcontextprotocol/clientCapabilities`; a bare
  `elicitation: {}` is form-only support for backwards compatibility. When
  declared, the tool
  returns an `InputRequiredResult` whose `inputRequests` carry one
  `elicitation/create` per pending request: `mode: "form"` with the ticket's
  `requested_schema` verbatim plus a mandatory `disposition` enum field
  (`reopen | park | cancel` with titles), or `mode: "url"` when the request
  carries a URL. If the ticket schema already owns `disposition`, the bridge
  chooses a collision-free internal field name. `{board, ticket_id,
  request_id}` ride `requestState`; on the
  retried call `accept` maps to `ticket_human_resolve(action="accept",
  content, disposition)`, `decline` to `resolve(decline, disposition from
  content or park)`, and `cancel` leaves the request pending ("asked later").
  On pre-2026 connections the same capability guard drives the legacy
  `elicitation/create` back-channel and the tool returns only its final result,
  never an `InputRequiredResult`. Clients that declared no elicitation get the list plus instructions to
  answer via `board_human_requests(answer={"ticket_id": ..., "action": ...,
  "content": {...}, "disposition": ...})` or the fleet dashboard. The bridge
  never sends a mode the client did not declare. Every result exposes the
  SDK-parsed declaration as `declared: {"form": bool, "url": bool, "raw":
  object|null}` (inside `_meta` on `InputRequiredResult`), and the bridge writes
  the same scrubbed value once to stderr for each call. A shared guard checks
  schema property names and titles for secrets or credentials: passwords, API
  keys, access tokens, and payment credentials require url mode. Names, email
  addresses, usernames, ordinary request prose, and file deliverables remain
  valid form fields.
- Push: the `human_input_requested` / `human_input_resolved` journal kinds
  wake orchestrator seats; `board_digest` shows a `human_requests` section
  and `board_digest_ack` clears it.
- Fleet dashboard: the hub "Waiting for you" panel renders each pending
  request as an inline form generated from `requested_schema`
  (string/number/boolean/enum/multi-enum, defaults, required) with a
  disposition selector; `POST /api/human/resolve` calls
  `ticket_human_resolve` with the coordinator token behind the same-origin
  loopback guard. URL-mode requests show the target host prominently and open
  in a new tab only on click.

Elicitation host declarations (probed 2026-09-06):

| Host / transport | raw declaration | measured result |
| --- | --- | --- |
| Claude Desktop / stdio | not captured by the pre-raw-value probe build | Live MCPB calls returned `elicitation_declared: false`; no form was rendered. An earlier fallback `answer` call accepted `choice=green`, `disposition=reopen`, and Central recorded the matching resolution. The corrected bridge will distinguish raw `null` from `{}` on the next host call. |
| Claude Desktop / HTTP custom connector | not measured | This is a separate per-request `_meta` path; no declaration is inferred from the stdio probe. |
| Codex app | not measured | The sandbox probe connector was not available to this Codex task, so no declaration is inferred. |
| AionUi 2.2.1 / imported stdio bridge | no usable native form renderer | Push is available through the stored-door wait bridge; AionUi did not render the requested elicitation schema, so use the dashboard or coordinator fallback. |
| Goose / stdio | not captured by the pre-raw-value probe build | Live stdio probe verified the fallback list + instructions path; no raw declaration was retained by that build. |

The fleet dashboard is the independent browser fallback, not an MCP App UI
resource associated with `board_human_requests`. Keeping it open does not alter
the MCP host's per-request capability declaration.

Live Claude Desktop probe record (2026-09-06): dependency commit `78cad2e` ran
in a loopback sandbox Central. Ticket `TK-claude-elicitation-probe` entered
`needs_human` with request `HR-b576ee9577c14ccd`. After correcting bare
`elicitation: {}` to mean form-only and selecting legacy back-channel versus
2026 MRTR by protocol version, Claude Desktop was fully restarted. Its MCPB
call still returned `elicitation_declared: false`, so no form was rendered and
no unsupported mode was sent. The measured fallback then submitted `action=accept`,
`content={"choice":"green"}`, and `disposition=reopen`; Central advanced the
journal, recorded that exact resolution, and reopened the ticket. This proves
the capability guard and fallback round trip on the measured host, but it does
not claim form-MRTR support that the host did not declare. A separate live
Python SDK 2.1.1 probe using protocol `2026-07-28` and an elicitation callback
did declare form mode on the request: the bridge returned the expected schema,
the SDK invoked the callback, retried the tool, and Central recorded
`action=accept`, `content={"choice":"green"}`, and `disposition=reopen` for
request `HR-4adce4d2f75071bc`. This verifies the bridge's 2026 per-request
capability and MRTR path independently of Claude Desktop's host support. A
second SDK probe in `2025-11-25` legacy mode invoked the form callback over
the back-channel and returned a final tool result (not MRTR), resolving
request `HR-48dc39e253b9604e` with `choice=blue` and `disposition=reopen`.
An additional operator-authorized Claude Desktop call against pending request
`HR-667f8dd55d230060` again returned `elicitation_declared: false` and rendered
no form. Those calls predated the `declared.raw` result field, so this record
does not infer whether the host sent nothing or the old bridge misread its
declaration. The corrected bridge logs and returns the SDK-parsed raw value on
the next call.
