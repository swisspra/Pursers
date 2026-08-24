# Pursers wait bridge

`pursers_wait_server.py` is a stdio MCP server that exposes blocking
`a2a_wait`. It polls an On Board Central connector, filters journal events,
and renews leases while the tool call is blocked. Keep it on stdio; wrapping
it in an HTTP transport adds request timeouts that defeat the wait behavior.

## Requirements

- Python 3.11 or newer
- `mcp==2.0.0`
- the repository's `packages/client/src` on `PYTHONPATH`
- an On Board bearer token with access to the configured board

The imported source was reconstructed from board memories `MEM-000001` and
`MEM-000002`. Before the instance-naming change, the 14,252-byte file matched
SHA-256 `1a0981ec6cc47aed8eeb5e8f488bef260ab6b5fd5c7c88e2cd99604654103e1a`.

## Environment

| Variable | Required | Purpose |
| --- | --- | --- |
| `ONBOARD_CENTRAL_TOKEN` | yes | Bearer token for Central. Treat it as a secret. |
| `ONBOARD_CENTRAL_URL` | no | Central MCP URL; defaults to `https://127.0.0.1:8766/mcp`. |
| `ONBOARD_BOARD_ID` | no | Board ID; defaults to `pursers`. |
| `ONBOARD_AGENT_NAME` | no | Base board identity; defaults to `pursers-wait-bridge`. |
| `ONBOARD_AGENT_INSTANCE` | no | Stable per-instance suffix, such as `window-a`. |

With no `ONBOARD_AGENT_INSTANCE`, the effective name is exactly
`ONBOARD_AGENT_NAME`, preserving the single-instance behavior. When the value
is set, the effective name is `<ONBOARD_AGENT_NAME>-<ONBOARD_AGENT_INSTANCE>`.
Give each host window a unique, durable instance value and reuse that value
after restarts. The explicit value is the stability anchor; the bridge does
not guess window identity or create lock files that can swap identities when
processes restart in a different order.

## Per-call identities

`a2a_wait` also accepts an optional `agent_name`. Omitting it uses the
process-level identity above with the original behavior. Supplying it lets one
bridge process and one Central connection serve multiple session identities:

```text
a2a_wait(since_seq=0, project="PROJECT_PLACEHOLDER", agent_name="session-a")
```

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

## Connector examples

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
journal events and scans the first 100 currently open tickets, so work older
than the cursor still wakes the worker. Backlog cues use
`source="backlog_scan"`, carry no fabricated journal sequence, and leave
`new_seq` governed only by the real journal. On `timed_out=true`, re-arm
immediately. Every event is a cue to refetch and claim current board state.
The bridge renews held leases only while `a2a_wait` is blocking; long-running
work must call Central's `lease_renew` directly.

For a per-call identity, heartbeat lookup first uses the name and then
exact-filters `claimed_by_agent_id`. This prevents substring matches such as
`session-a` and `session-a-2` from crossing over. Central authorizes
`lease_renew` at principal scope, not per agent identity, so this exact ticket
selection prevents accidental renewals by the bridge but is not a server-side
identity-isolation guarantee.

Run the bridge tests with:

```sh
python -m unittest discover -s tools/wait-bridge/tests -v
```
