# Fleet Dashboard

A standalone, loopback-only web dashboard for the active boards in the live project registry and their shared agent pool. It is a read-only viewer and does not require a browser extension, build step, or desktop host.

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
