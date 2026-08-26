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
- bounded open and claimed ticket rows;
- a bounded recent activity feed per board; and
- agents grouped across boards by principal and agent name.

The browser polls `/api/fleet` every five seconds. Server reads are cached for five seconds. Every board snapshot is capped at 50 items per collection and 200,000 bytes; displayed board, ticket, event, title, and agent rows are capped again before JSON serialization. Paused registry projects are excluded.

Useful options:

```text
--home-board BOARD       Registry-bearing home board
--stale-seconds SECONDS  Stale threshold (default: 300)
--cache-seconds SECONDS  Server cache lifetime (default: 5)
--agent-name NAME        Read-only viewer identity
```
