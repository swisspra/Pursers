# AionUi door onboarding backend contract

This contract wraps the shipped door issuer, wait-bridge join flow, and AionUi
MCP import route without replacing them. It owns no visual UI and performs no
ticket dispatch. The Team handoff follows `TK-628602eedb90` at
`1541c0585942d130992683a01c32b8c86b7be10e`: a successful operation returns an
exact `TeamSpec.seats[]` fragment containing the stable seat `name`, Pursers
`role`, preserved `tier_max`, per-seat `folder`, and optional `assistant_id` and
`model`. The caller selects the existing Team and lead; this adapter never
creates or mutates a Team.

## Operations

| Operation | Mutation | Result |
|---|---|---|
| `parse(door)` | none | Redacted board, role, key ID, expiry, and transport metadata. Parsing is syntax-only; Central verifies the credential during connect. |
| `validate(input)` | none | Adds expected board/role, seat-name, tier, HTTPS/loopback, and Team-fragment checks. |
| `status()` | none | Calls `pursers-wait-bridge status`; returns stored redacted seats and push mode. |
| `connect(input)` | private local state, Central onboarding, MCP import | Calls the shipped bridge `join`; passes `--name`, secure remote confirmation for HTTPS only, and `PURSERS_TIER_MAX`. |
| `rotate(input)` | replaces an existing same-board/role private door, then reconnects/imports | Calls shipped `join --rotate`; never issues, mints, or rotates a real coordinator key itself. |
| `recover(input)` | MCP import only | Re-registers the environment-free bridge from redacted stored status after partial registration. |

All operations return `{ok, operation, outcome, ...}`. Successful outcomes are
`ready`, `connected`, `recovered`, or `rotated`. Failure codes are typed and
actionable: `invalid_door`, `expired_door`, `invalid_url`,
`insecure_remote_url`, `wrong_board`, `wrong_role`, `invalid_seat_name`,
`invalid_tier`, `invalid_folder`, `bridge_not_installed`, `bridge_rejected`,
`bridge_status_failed`, `server_unreachable`, `identity_conflict`, `rotation_required`,
`rotation_requires_existing`, `seat_name_required`,
`mcp_registration_failed`, `stored_door_not_found`, `seat_not_found`,
`invalid_recovery_target`, and `invalid_bridge_response`.

## Idempotency and recovery

Before joining, the adapter reads bridge status. The same board, role, key ID,
and seat name resumes MCP registration without another Central join. If one
stored seat exists and a retry omits its name, that seat is reused. Multiple
stored names require an explicit choice. A different key requires `rotate`.
Partial MCP import returns `outcome=partial`, `connected=true`, and
`retryable=true`; `recover` completes import without replaying the door.

## Security

- Remote Central requires HTTPS. HTTP is accepted only for loopback hosts.
- The raw door and embedded Central URL stay in an internal object only until
  the bridge call. They never appear in public results, logs, manifests, MCP
  import payloads, Team fragments, or error details.
- The shipped bridge remains the credential verifier and private `0600` store.
  The JavaScript parser never claims signature verification.
- MCP registration remains same-origin through the authenticated AionUi route,
  forwards only the existing cookie and CSRF headers, and stores an
  environment-free stdio command.
- Active seat collisions are never taken over. The adapter returns
  `identity_conflict` and asks for a unique seat name.

## Host boundary

The installed AionUi agent-facing Team contract cannot create Teams, set a
per-seat workspace/model/role/tier at spawn, or remove a teammate. The returned
seat fragment is therefore an input to the sibling Team adapter's dry-run/apply
flow. Any missing host surface remains explicit; no REST endpoint is inferred.
