# Fleet Dashboard

A standalone, loopback-only web dashboard for the active boards in the live project registry and their shared agent pool. It is a read-only viewer and does not require a browser extension, build step, or desktop host.

## Configure seats in the dashboard

Start the loopback Fleet Dashboard, open **Config**, and use **Add or update
seat** as the primary setup path. Enter only paths for the JWT token and CA
files; token contents never enter the browser. **Preview exact changes** shows
a redacted unified diff, and **Confirm and apply** creates timestamped backups
before atomic writes. Restart the selected host when the result shows **NEEDS
RESTART**.

The same page inventories seats, shows installed/pinned/latest bridge versions,
runs Doctor for one or all seats, upgrades the bridge in a background job, and
shows read-only project-registry coverage. Long jobs expose a job id and are
polled by the browser once per second. Config POSTs are loopback-only and each
plan/apply/doctor/install action is recorded in
`~/.pursers/fleet-dashboard/config-actions.jsonl` without credentials.

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

Open `http://127.0.0.1:8899`. Use `--port` to select another port. The central URL defaults to `https://127.0.0.1:8766/mcp` and can be changed with `--url` or `ONBOARD_CENTRAL_URL`. Use `--token-file /path/to/token` instead of the environment variable when preferred. The file must contain only the bearer token.

The server refuses non-loopback binding. It never returns tokens to the browser
or writes them to logs. Central TLS verification follows
`pursers_client.BoardClient` behavior.

### Multiple central instances

Use one viewer process for several independent trust domains with `--centrals`:

```json
[
  {
    "label": "personal",
    "url": "https://127.0.0.1:8766/mcp",
    "token_path": "personal.token",
    "home_board": "pursers",
    "stats_path": "personal-bridge-stats.json"
  },
  {
    "label": "work",
    "url": "https://127.0.0.1:9766/mcp",
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
