# Fleet Dashboard

A standalone, loopback-only web dashboard for the active boards in the live project registry and their shared agent pool. It is a read-only viewer and does not require a browser extension, build step, or desktop host.

Local dashboard state defaults to `~/.pursers`. Set `PURSERS_STATE_DIR` to an
absolute writable directory to relocate the entire state root, including
`fleet-dashboard/` configuration and `workers/`. Paths are resolved lazily and
worker directories are created only when worker state is first written. If the
host denies process inspection, status and Doctor surfaces report `process
inspection unavailable` instead of failing the dashboard.

## Configure seats in the dashboard

Start the loopback Fleet Dashboard, open **Config**, and use **Add or update
seat** as the primary setup path. Enter only paths for the JWT token and CA
files; token contents never enter the browser. **Preview exact changes** shows
a redacted unified diff, and **Confirm and apply** creates timestamped backups
before atomic writes. Restart the selected host when the result shows **NEEDS
RESTART**.

If existing Codex, Goose, or Claude Desktop configs already contain managed
Pursers blocks, **Import discovered seats** shows the exact host, seat,
connector, role, board, and credential-reference mapping before import. Rows
with duplicate names or settings that disagree with inventory are marked as
conflicts. **Import and run Doctor** adds only conflict-free rows to
`seats.json`, never rewrites the host configs, and immediately runs Doctor on
the imported seats. Imported seats use `boards=registry` unless their managed
environment explicitly declares `PURSERS_BOARDS` or `ONBOARD_BOARDS`.

The same page inventories seats, shows installed/pinned/latest bridge versions,
runs Doctor for one or all seats, upgrades the bridge in a background job, and
shows read-only project-registry coverage. Long jobs expose a job id and are
polled by the browser once per second. Config POSTs are loopback-only and each
plan/apply/doctor/install action is recorded in
`~/.pursers/fleet-dashboard/config-actions.jsonl` without credentials.

**Create fleet clone** checks out `main`, detaches at `origin/main`, and verifies
that the working tree is clean before saving the registry path. Re-running it
fetches and fast-forwards a clean clone; partial first-time clones are removed
after a failure. Registry and Doctor report an empty working tree separately
from local changes. A dirty clone is never overwritten, and the API error gives
its exact path plus a `git status --short` inspection command.
Before cloning, the dashboard runs a non-interactive `git ls-remote` preflight
for the project's integration ref; Doctor reports that preflight per active
registry project. Git receives `GIT_TERMINAL_PROMPT=0`, a defined `HOME`, and a
PATH augmented with available Homebrew/local binary directories. Credential
helpers must therefore work non-interactively under the launchd service account.
Failures return and log the failing git subcommand with a bounded, scrubbed
stderr tail; credentials and operator home names are redacted.
Automatic legacy recovery requires the missing Git index left by the former
`--no-checkout` flow. Unstaged or staged deletions in an initialized clone stay
classified as local changes and are never restored automatically.

## Doors

The Config page includes a **Doors** panel managing secret-safe door credentials
for active projects and roles (`worker` and `reviewer`):

- **Door inventory table**: lists `(board x role)` for every active project in the
  project registry with its key identifier (`kid`), expiration date, and seats
  currently connected on that door principal (grouped by agent name with last activity).
- **Copy door string**: issues or fetches the non-secret `prs1.…` door string directly
  using `door_admin` as an in-process library without shelling out or logging credentials.
  The door string is returned once over loopback same-origin HTTP with
  `Cache-Control: no-store` and is never persisted in state files or listing endpoints.
- **Rotate**: generates a new RSA-2048 signing key version, publishes it to the public
  JWKS, and returns the new door string. A warning is displayed that existing seats
  operating under the previous key version must re-join with the new door string.

### Configuration and Central wiring

Doors require two configuration paths provided via environment variables, CLI options,
or the multi-central JSON configuration file:

```bash
export PURSERS_DOORS_KEYS_DIR="/PATH/TO/keys"
export PURSERS_JWKS_PATH="/PATH/TO/jwks.json"
```

CLI flags `--doors-keys-dir` and `--jwks-path` are also accepted. When these settings
are unset, door actions refuse with a descriptive message rather than failing silently.

Central must be told where the public JWKS file resides so it can verify incoming
door credentials without access to private key material. In Central's service configuration:

```bash
export CENTRAL_JWKS_PATH="/PATH/TO/jwks.json"
```

Central already supports `CENTRAL_JWKS_PATH` and re-reads the JWKS file to validate
incoming tokens. Operator files outside the dashboard checkout should not be modified.

## Add project

The **Add project** card provides single-action provisioning to transform onboarding
into "Add project once, copy door twice":

### Operator flow

1. Configure `PURSERS_DOORS_KEYS_DIR` and `PURSERS_JWKS_PATH` for the dashboard,
   and configure `CENTRAL_JWKS_PATH` pointing to the public JWKS for Central.
2. Open **Config** in the Fleet Dashboard (`http://127.0.0.1:8899/#/seats`).
3. Under **Add project**, fill out the form:
   - **Project name**: unique identifier (e.g., `my-service`).
   - **Board ID**: target board identifier matching `[A-Za-z0-9._-]{1,80}`.
   - **Work dir**: absolute path to the operator repository checkout.
   - **Integration ref**: target integration git ref (defaults to `main`).
4. Click **Add project**. The dashboard executes the following five steps in order,
   displaying status for each step:
   - **Registry add**: adds the project entry to `project_registry` under schema v1.
   - **Board create**: detects if the board exists, or creates the board and onboards
     the caller as administrative coordinator.
   - **Door principals**: adds board memberships for the worker door principal
     (`sub door:<board>:worker`, `client_id door-<board>-worker`) with role `member`,
     and the reviewer door principal (`sub door:<board>:reviewer`, `client_id door-<board>-reviewer`)
     with role `reviewer`.
   - **Policy defaults**: configures dispatch policy defaults (`offer_ttl_s 600`,
     `broadcast_reoffer_s 180`, `second_opinion true`, `fallback_broadcast true`)
     and review policy default (`strict`).
   - **Fleet clone**: prepares a dedicated fleet work checkout using `prepare_fleet_clone`.
5. The worker and reviewer `prs1.…` door strings are displayed once upon completion.
   Copy the respective door string for seats joining this board.
6. Seats join using `pursers-door join <door-string>`.

### Idempotency and guards

Re-running **Add project** with an existing project name reports `already present`
for every step and changes nothing. All door and project mutation endpoints enforce:
- Loopback client address check;
- Same-origin `Host` and `Origin` header validation;
- Application/json Content-Type requirement;
- Administrative token authorization on Central (non-admin callers are rejected with HTTP 403);
- No door strings or JWT-shaped substrings are ever logged or included in listing APIs.

The authorization check uses Central's `board_list` membership projection and
requires `membership_role=admin` on each affected board. A successful
member-authorized status or member listing is not treated as proof of admin
authority, and door key/JWKS files are not touched until this check succeeds.

## Release & Operations panel

The Config page integrates release status and guarded operational controls
without shell commands or credential exposure:

- **Release card**: telemetry from `release_versions.toml`, latest git/origin tags,
  CI status for `main` and the tag commit (via `gh run list`), PyPI distribution
  presence per package (JSON 200 is present, 404 is absent, and every other
  bounded HTTP/transport result is unavailable), GitHub Release status,
  and live Central version (`/healthz`) compared against staged `profile.env` pins.
- **Seat restart checklist**: attributes each wait-bridge PID to a configured
  host only from an exact seat marker or its process ancestry. Unprovable or
  ambiguous processes are reported under `unknown` and never copied onto every
  host. Only a stale bridge proven to belong to a host flags that host.
- **Operator operations**:
  - `Publish from tag`: runs `gh workflow run publish-pypi.yml --ref <tag>`
  - `Stage Central`: resolves only the exact manifest version from the trusted
    staging root, verifies its independent build-metadata digest before mutation,
    preserves `profile.env` mode and ownership during a durable atomic pin update,
    installs the staged wheel `--no-deps`, and rolls back pins/artifacts on failure.
    Request-supplied wheel paths are rejected, and the button stays disabled until
    the wheel, digest, profile, and interpreter form a concrete confirmation plan.
  - `Kickstart Central`: restarts the Central service via `launchctl kickstart -k`
  - `Restart dashboard`: restarts the fleet dashboard launchd service
- **Safety model**:
  - All operations require loopback requests with same-origin validation and JSON payloads.
  - Interactive actions first create a short-lived server-side plan. The browser
    confirms its exact command and digest, then submits only that plan id/digest.
    Plans are one-use; execution revalidates stored version, path, metadata, and
    hash invariants instead of resolving replacement values after confirmation.
  - Stage jobs are serialized, and duplicate publish triggers are refused while
    an earlier job is queued or running.
  - Every plan/execution attempt, resolution or subprocess failure, and rollback
    outcome appends a scrubbed audit record to `config-actions.jsonl`. Rollback
    failures remain visible in the job result with recovery-step details.
  - Sensitive tokens and authorization values are never requested, stored, or logged.

### Manual-edit appendix

Direct editing remains available for recovery and headless use. Back up the
host config first, keep the token in a private file, use the timeout from
`HOST_PROFILES`, and run `seat_config.py doctor --json` afterward. Do not paste
a JWT into prompts or the seat inventory. Codex and Goose managed config files
contain a dashboard-generated token literal because GUI hosts do not reliably
forward connector environment variables to stdio MCP processes; keep those
files mode `0600` and regenerate them from the same private token file.

## Seat configuration library

`seat_config.py` is the write boundary used by the dashboard Config page. Host
adapters expose `inspect() -> dict`, `plan(desired) -> list[Change]`, and
`apply(plan) -> ApplyResult`; plans are human-readable and apply creates a
timestamped backup before each atomic config write. Available integrations are
`CodexAdapter`, `GooseAdapter`, `ClaudeDesktopAdapter`, and
`ClaudeCodeAdapter`. `BridgeInstaller.install()` uses a persistent `uv tool`
installation with private-CA overrides removed; generated host configs never
launch through `uvx`.

`PromptRenderer.render(desired)` fills the shared
`seat_prompt_template.txt`, while `SeatInventory` maintains the dashboard's
schema-1 `seats.json`. For a headless health report or repair:

```bash
python tools/fleet-dashboard/seat_config.py doctor --json
python tools/fleet-dashboard/seat_config.py doctor --fix --json
```

Doctor output never contains token contents. It checks config drift, host
timeout profile, bridge and Personal versions, token/CA paths, the managed
token literal, a host-equivalent bridge launch using only the configured env
block, Goose seat interpreter and hints, clean clone freshness, a five-second
push subscription, registry visibility, and whether a host restart is needed.
A reported `poll` mode is a warning and remains an explicit fallback only.
The inventory table keeps compact per-seat badges for config, token/CA,
identity, and host-runtime results after Doctor completes.

For Codex seats, the generated wait bridge and HTTP board connector use one
seat token. The adapter copies the token file value into the wait bridge's
managed env block, while the HTTP connector continues to name its
`bearer_token_env_var`. Doctor verifies file-to-literal equality, asks Central
to resolve both token sources to principal IDs, and reports `split identity`
when any source differs. Apply the generated config, set the connector
environment variable from the same seat token file, and restart Codex before
rerunning Doctor.
Worker seats target `pursers-dev` and reviewer seats target `pursers-review` by
default; inventory/API input may set `board_connector_name` explicitly. Each
apply replaces only that seat's wait/board pair, so both pairs coexist in one
Codex config and repeated applies do not clobber the other seat.
Generated role defaults are worker=`can_work`, reviewer=`can_review`, and no
work/review capability for coordinator or orchestrator; incompatible hybrids
are rejected before a host configuration is written.

## Run

From the repository root, with the client package available in the current Python environment:

```bash
export ONBOARD_CENTRAL_TOKEN="..."
python tools/fleet-dashboard/fleet_dashboard.py
```

Open `http://127.0.0.1:8899`. Use `--port` to select another port. The central URL defaults to `http://127.0.0.1:8766/mcp` and can be changed with `--url` or `ONBOARD_CENTRAL_URL`. Use `--token-file /path/to/token` instead of the environment variable when preferred. The file must contain only the bearer token.

The server refuses non-loopback binding. It never returns tokens to the browser
or writes them to logs. Central TLS verification follows
`pursers_client.BoardClient` behavior.

The dashboard uses one persistent, serialized Central session per board. Its
identity must be in the reserved `fleet-dashboard-session-*` namespace and has
explicit `can_work=false` and `can_review=false` capabilities. A restart may
reclaim that identity only when Central confirms that its role, capabilities,
platform, and ownership marker all match. A collision with a worker, reviewer,
or differently marked identity is refused instead of being taken over.
The dashboard and Central must therefore be deployed from the same approved
candidate or a later release that includes the matching-takeover contract.

Run the focused identity/session regression with these exact pytest node IDs
(the two parametrized functions expand to five cases, for ten cases total):

```bash
python3 -m pytest -q \
  packages/client/tests/test_per_call_identity.py::test_takeover_and_memory_identity_are_forwarded \
  packages/client/tests/test_per_call_identity.py::test_context_startup_forwards_explicit_takeover_policy \
  tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_fetcher_real_client_uses_reserved_read_only_session_identity \
  tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_real_central_matching_takeover_protects_worker_and_reviewer_identities \
  tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_config_api_reuses_dashboard_identity_after_restart_and_concurrently \
  tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_fetcher_reconnects_once_after_transport_failure \
  tools/fleet-dashboard/tests/test_fleet_dashboard.py::test_cli_refuses_names_outside_dashboard_session_namespace
```

### Coordinator-only exact-SHA deployment and rollback

Do not deploy from a dirty operator checkout. The coordinator should prepare an
isolated detached worktree and a staged copy of the existing LaunchAgent. Set
the variables below to the reviewed candidate and the current deployment paths;
do not place bearer tokens in the shell command or plist.

```bash
FLEET_CLONE=/PATH/TO/pursers-fleet-clone
CANDIDATE_SHA=0123456789abcdef0123456789abcdef01234567
CANDIDATE_ROOT=/PATH/TO/fleet-dashboard-candidates/$CANDIDATE_SHA
LIVE_PLIST=/PATH/TO/Library/LaunchAgents/com.pursers.fleet-dashboard.plist
STAGED_PLIST=/PATH/TO/staging/com.pursers.fleet-dashboard.plist
BACKUP_PLIST=/PATH/TO/backups/com.pursers.fleet-dashboard.plist.before-$CANDIDATE_SHA
JOB=gui/$(id -u)/com.pursers.fleet-dashboard

git -C "$FLEET_CLONE" fetch origin "$CANDIDATE_SHA"
git -C "$FLEET_CLONE" worktree add --detach "$CANDIDATE_ROOT" "$CANDIDATE_SHA"
test "$(git -C "$CANDIDATE_ROOT" rev-parse HEAD)" = "$CANDIDATE_SHA"
python3 - "$LIVE_PLIST" "$BACKUP_PLIST" "$STAGED_PLIST" <<'PY'
import os
import stat
import sys

source_path, *destination_paths = sys.argv[1:]
source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
source_fd = os.open(source_path, source_flags)
try:
    source_info = os.fstat(source_fd)
    if not stat.S_ISREG(source_info.st_mode):
        raise SystemExit("live LaunchAgent must be a regular file")
    chunks = []
    while chunk := os.read(source_fd, 1024 * 1024):
        chunks.append(chunk)
    contents = b"".join(chunks)
finally:
    os.close(source_fd)

for destination_path in destination_paths:
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination_path, flags)
    except FileNotFoundError:
        descriptor = os.open(
            destination_path,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    try:
        destination_info = os.fstat(descriptor)
        if not stat.S_ISREG(destination_info.st_mode) or destination_info.st_nlink != 1:
            raise SystemExit("staged and backup LaunchAgents must be unlinked regular files")
        # Existing destinations become private before truncation or secret writes.
        os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, 0)
        view = memoryview(contents)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
python3 - "$STAGED_PLIST" "$CANDIDATE_ROOT/tools/fleet-dashboard/fleet_dashboard.py" <<'PY'
import plistlib
import sys

path, candidate = sys.argv[1:]
with open(path, "rb") as source:
    document = plistlib.load(source)
arguments = document.get("ProgramArguments")
matches = [
    index for index, value in enumerate(arguments or [])
    if isinstance(value, str) and value.endswith("tools/fleet-dashboard/fleet_dashboard.py")
]
if len(matches) != 1:
    raise SystemExit("LaunchAgent must contain exactly one fleet_dashboard.py argument")
arguments[matches[0]] = candidate
if "--agent-name" in arguments:
    index = arguments.index("--agent-name")
    if index + 1 >= len(arguments):
        raise SystemExit("--agent-name is missing its value")
    arguments[index + 1] = "fleet-dashboard-session-default"
else:
    arguments.extend(["--agent-name", "fleet-dashboard-session-default"])
with open(path, "wb") as target:
    plistlib.dump(document, target, sort_keys=True)
PY
plutil -lint "$STAGED_PLIST"
install -m 600 "$STAGED_PLIST" "$LIVE_PLIST"
launchctl bootout "$JOB" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$LIVE_PLIST"
```

Verify the loaded definition, process source, candidate SHA, and endpoints
without printing the job environment or command line:

```bash
EXPECTED_SOURCE="$CANDIDATE_ROOT/tools/fleet-dashboard/fleet_dashboard.py"
PLIST_SOURCE=$(python3 - "$LIVE_PLIST" <<'PY'
import plistlib
import sys
with open(sys.argv[1], "rb") as source:
    arguments = plistlib.load(source)["ProgramArguments"]
matches = [value for value in arguments if isinstance(value, str) and value.endswith("tools/fleet-dashboard/fleet_dashboard.py")]
if len(matches) != 1:
    raise SystemExit(1)
print(matches[0])
PY
)
test "$PLIST_SOURCE" = "$EXPECTED_SOURCE"
test "$(git -C "$CANDIDATE_ROOT" rev-parse HEAD)" = "$CANDIDATE_SHA"
PID=$(launchctl print "$JOB" | awk '/pid =/{print $3; exit}')
test -n "$PID"
PROCESS_COMMAND=$(ps -p "$PID" -o command=)
case "$PROCESS_COMMAND" in *"$EXPECTED_SOURCE"*) ;; *) exit 1 ;; esac
for path in / /api/fleet /api/config /api/config/registry /api/attention; do
  curl --fail --silent --show-error --output /dev/null "http://127.0.0.1:8899$path"
done
```

Rollback restores the saved definition and reloads it. Resolve its source and
SHA from the isolated previous worktree, then repeat the same non-printing
process-source and endpoint checks above with those previous values.

```bash
install -m 600 "$BACKUP_PLIST" "$LIVE_PLIST"
launchctl bootout "$JOB"
launchctl bootstrap "gui/$(id -u)" "$LIVE_PLIST"
PREVIOUS_SOURCE=$(python3 - "$LIVE_PLIST" <<'PY'
import plistlib
import sys
with open(sys.argv[1], "rb") as source:
    arguments = plistlib.load(source)["ProgramArguments"]
matches = [value for value in arguments if isinstance(value, str) and value.endswith("tools/fleet-dashboard/fleet_dashboard.py")]
if len(matches) != 1:
    raise SystemExit(1)
print(matches[0])
PY
)
PREVIOUS_ROOT=${PREVIOUS_SOURCE%/tools/fleet-dashboard/fleet_dashboard.py}
PREVIOUS_SHA=$(git -C "$PREVIOUS_ROOT" rev-parse HEAD)
test -n "$PREVIOUS_SHA"
PID=$(launchctl print "$JOB" | awk '/pid =/{print $3; exit}')
test -n "$PID"
PROCESS_COMMAND=$(ps -p "$PID" -o command=)
case "$PROCESS_COMMAND" in *"$PREVIOUS_SOURCE"*) ;; *) exit 1 ;; esac
for path in / /api/fleet /api/config /api/config/registry /api/attention; do
  curl --fail --silent --show-error --output /dev/null "http://127.0.0.1:8899$path"
done
```

### Multiple central instances

Use one viewer process for several independent trust domains with `--centrals`:

```json
[
  {
    "label": "personal",
    "url": "http://127.0.0.1:8766/mcp",
    "token_path": "personal.token",
    "home_board": "pursers",
    "stats_path": "personal-bridge-stats.json"
  },
  {
    "label": "work",
    "url": "http://127.0.0.1:9766/mcp",
    "token_path": "work.token",
    "home_board": "work-registry",
    "stats_path": "work-bridge-stats.json"
  }
]
```

The JSON file and every referenced token file must be regular files with mode
`0600`. Relative token and optional `stats_path` paths resolve from the JSON
file's directory. Labels must be unique and contain only letters, digits, `.`,
`_`, or `-`.

```bash
chmod 600 centrals.json personal.token work.token
python tools/fleet-dashboard/fleet_dashboard.py --centrals centrals.json
```

Each central gets its own summary, board group, agent pool, cache, error state,
detail routes, findings, overhead route, and coordinator config target. The
browser requests and renders each central independently with a four-second
timeout, so an unavailable or nonresponsive central does not hide healthy ones.
When bridge stats report a subscription failure, Needs attention shows
`push unavailable: <reason>` until a later healthy push return clears it.
In multi-central mode, overhead is read only from that entry's `stats_path`; an
entry without one reports its overhead unavailable instead of using another
trust domain's global stats. Tokens remain server-side. Without `--centrals`,
all existing single-central flags and the global overhead path continue to work.

## What it shows

- online, busy, available, and stale pool counts;
- open, claimed, submitted, and closed-today ticket counts per active board;
- bounded active ticket rows (open, claimed, and submitted);
- a bounded recent activity feed per board; and
- agents grouped across boards by principal and agent name, with expandable
  per-board role, claim, and last-seen details.

Select a board to open its hash-routed detail view. The detail API returns all
statuses from a `board_snapshot(limit=1000, max_bytes=300000)` source, ordered
with active claims first and then by update time. Ticket descriptions, required
fields, latest submission summaries, and review labels are compact projections;
full submission history is never sent to the browser. The activity feed uses
`board_catchup(max_events=100, ack=false)` and stays oldest-to-newest.

The browser polls `/api/fleet?central=<label>` every five seconds. While a detail
route is open, it also polls `/api/board/<board-id>?central=<label>` every five
seconds; the detail poll stops when the route closes. Server reads are cached
for five seconds. Every board
snapshot is capped at 1,000 items per collection and 300,000 bytes; detail JSON
is capped at 300,000 bytes and reports omitted ticket rows. Truncated fleet
ticket counts are shown as lower bounds with a `>=` prefix. Paused registry
projects and unknown detail routes are excluded.

Useful options:

```text
--home-board BOARD       Registry-bearing home board
--stale-seconds SECONDS  Stale threshold (default: 300)
--cache-seconds SECONDS  Server cache lifetime (default: 5)
--agent-name NAME        Read-only viewer identity
--centrals FILE          0600 multi-central JSON configuration
```

`/api/fleet`, `/api/board/<board-id>`, `/api/overhead`, and `/api/config`
accept `central=<label>` and default to the first configured central. Successful
responses include the selected `central` label. `/api/centrals` exposes only the
ordered labels and default label; it never exposes URLs, token paths, or tokens.
