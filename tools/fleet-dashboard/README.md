# Fleet Dashboard

A standalone, loopback-only web dashboard for the active boards in the live project registry and their shared agent pool. It is a read-only viewer and does not require a browser extension, build step, or desktop host.

## Run

From the repository root, with the client package available in the current Python environment:

```bash
export ONBOARD_CENTRAL_TOKEN="..."
python tools/fleet-dashboard/fleet_dashboard.py
```

Open `http://127.0.0.1:8899`. Use `--port` to select another port. The central URL defaults to `https://127.0.0.1:8766/mcp` and can be changed with `--url` or `ONBOARD_CENTRAL_URL`. Use `--token-file /path/to/token` instead of the environment variable when preferred. The file must contain only the bearer token.

The server refuses non-loopback binding. It never returns the token to the browser or writes it to logs. Central TLS verification follows `pursers_client.BoardClient` behavior.

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

The browser polls `/api/fleet` every five seconds. While a detail route is open,
it also polls `/api/board/<board-id>` every five seconds; the detail poll stops
when the route closes. Server reads are cached for five seconds. Every board
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
```
