# Pursers Central

Pursers Central is the loopback MCP service that owns board state. Run it with
the console script or the equivalent Python module:

```bash
export CENTRAL_JWT_ISSUER='https://issuer.example'
export CENTRAL_JWT_AUDIENCE='http://127.0.0.1:8766/mcp'
export CENTRAL_JWKS_PATH=/PATH/TO/private/credential.jwks.json

pursers-central \
  --host 127.0.0.1 \
  --port 8766 \
  --data-dir /PATH/TO/private/central-data \
  --log-level info
```

`python -m pursers_central` accepts the same arguments. The runtime also reads
`ONBOARD_CENTRAL_HOST`, `ONBOARD_CENTRAL_PORT`,
`ONBOARD_CENTRAL_DATA_DIR`, and `ONBOARD_CENTRAL_LOG_LEVEL`; command-line
arguments override those environment defaults.

At startup the runtime selects JWT authentication, the SQLite store, and
invite-only admission. `CENTRAL_JWT_ISSUER`, `CENTRAL_JWT_AUDIENCE`, and
`CENTRAL_JWKS_PATH` must describe the credential issuer used by this Central
instance. Keep the JWKS and data directory private.

Modern MCP multi-round trips seal `requestState` with a restart-safe keyring.
Central creates `request-state.keys` in its private data directory with mode
0600, or reads the path in `CENTRAL_REQUEST_STATE_KEY_FILE`. Each non-empty
line is one key of at least 32 bytes: the first key seals and every key
unseals. For zero-downtime rotation, fully deploy `[OLD, NEW]`, then
`[NEW, OLD]`, then remove `OLD` one 3600-second request-state TTL after the
second phase is fully deployed. Never store the keyring in the repository.

The startup banner prints the MCP bind URL, data directory, and health URL.
Check the service without a credential:

```bash
curl --fail --silent http://127.0.0.1:8766/healthz
```

A healthy response has `"status":"ok"` and `"store_backend":"sqlite"`.

## Tool response views

Central keeps complete tickets, memories, and journal events in its SQLite
ledger, but its default model-facing response view is `compact`. Successful
calls to `ticket_update`, `ticket_annotate`, `ticket_claim`, `ticket_unclaim`,
`lease_renew`, `memory_write`, and `memory_checkpoint` return a small mutation
receipt instead of repeating the complete affected record and its histories.
Use the corresponding read tool, such as `ticket_get` or `memory_read`, when the
complete current state is needed.

Ticket mutation receipts contain `ok`, `ticket_id`, current `status` and
`parked` state, board `generation`, a bounded `dispatch_state`, the last
`revoked_offer` when applicable, and `at`; annotation receipts also contain
`annotation_id`. Lease renewal receipts contain `ok`, `ticket_id`,
`lease_expires_at`, and `at`. Memory write and checkpoint receipts contain
`ok`, `memory_id`, `scope`, `generation`, and `at`. Unsuccessful structured
results retain their error details instead of being projected as successful
receipts. Routing-only
`recipient_identities` fields are never included in MCP responses, including
when the full response view is selected. They remain in the durable journal so
Central can authorize and filter events before returning them.

A board administrator can restore the legacy response shape for compatibility:

```text
board_response_view_set(
  board_id="BOARD_ID",
  agent_name="ADMIN_AGENT_NAME",
  response_view="full"
)
```

Set `response_view="compact"` to restore the default. The setting is durable and
reported by `board_list`, `board_snapshot`, and `board_status`.

Structured schema-v2 checkpoint and handoff memories are returned through their
named fields (`summary`, `remaining_tasks`, `next_steps`, `files`, `blockers`,
and `warnings` as applicable). Their legacy rendered `content` copy remains in
storage but is omitted from memory read projections, so the same information is
not sent twice.
