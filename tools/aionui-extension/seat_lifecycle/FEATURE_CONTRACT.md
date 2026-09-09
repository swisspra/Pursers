# Standalone seat lifecycle contract

This component manages one standalone Pursers seat. It does not use Aion Team
Mode, kill processes, or infer board state from local files.

## Operations

| Operation | Source of truth | Consequence |
| --- | --- | --- |
| `join(input)` | Door onboarding plus a post-join Central board read | Preserves the exact returned `agent_id`, `principal_id`, `agent_name`, board, role, and lifecycle state. |
| `status(input)` | A fresh Central board read with retired seats included | Returns only the exact configured board and exact preserved identity. Missing or mismatched identity fails closed. |
| `disconnect(input)` | Central self-retirement followed by verified Central read-back | Requires the exact confirmation string. Local door removal happens only after Central reports the identity retired. |

Every dependency result must explicitly report `ok: true`; a valid-looking
identity never overrides a failed join result. Status returns the exact Central
lifecycle as `outcome`: `active` and `retired` are successful reads, while
`handed_off`, `stale`, and literal Central `unknown` are non-active results with
distinct codes and bounded same-identity rejoin guidance. Missing or malformed
lifecycle data is instead an `invalid_board_response`; it is never coerced to
`unknown` or `active`.

`disconnect` means cooperative retirement of the current seat. It must never
retire another identity. Active work or review leases remain Central-owned
blockers and are shown as recoverable errors. Rejoining with the same board,
principal, agent ID, agent name, and role may reactivate the identity. A bound
selector mismatch is rejected before `joinSeat` runs. The dependency receives
`expected_identity` and must verify it before mutation; a different identity is
a conflict, not a replacement.

`expected_board` and `expected_identity` are adapter-owned fields and are
rejected if present in caller JSON. A fresh join also rejects caller-supplied
`agent_id` or `principal_id`. A bound rejoin may supply those two selectors only
when they equal preserved state; they are not forwarded directly. The adapter
constructs the dependency request from `board`, `agent_name`, `role`, and the
allow-listed optional public fields `door`, `tier_max`, `assistant_id`, `model`,
and `folder`, then adds its own `expected_board` and derived
`expected_identity`.

## Trust and isolation

- The authenticated loopback helper supplies all live dependencies. The static
  page never receives a Central credential.
- Every input and every dependency result is checked against the helper's exact
  configured board.
- Identity is bound by `agent_id`, `principal_id`, `agent_name`, board, and
  role. Status and retirement fail closed on any mismatch.
- Consequential retirement requires `confirm` equal to
  `retire <agent_name> from <board>` and an immediate preflight status read.
- Successful retirement requires a post-mutation Central read showing the same
  identity as `retired` before stored door state may be forgotten.
- Errors are bounded, contain no credential or Central URL, and state whether
  retry or operator recovery is appropriate.

## File ownership and pairing map

This ticket owns:

- `seat_lifecycle/FEATURE_CONTRACT.md`
- `seat_lifecycle/adapter.cjs`
- `tests/seat_lifecycle.test.cjs`

Assembly ticket `TK-a3f0627d27db` pairs the reviewed component into shared
files. It must implement this executable interface:

- `POST /pursers/seat-lifecycle/join` passes the JSON object to `join(input)`.
- `GET /pursers/seat-lifecycle/status` maps `agent_name` and `role` query values
  to `status(input)`; once identity is bound both values may be omitted.
- `POST /pursers/seat-lifecycle/disconnect` passes JSON `{ board, confirm }` to
  `disconnect(input)`.
- `host/helper.cjs` authenticates every route with its existing exact Origin,
  loopback-host, and `x-pursers-home-token` checks before dispatch. The adapter
  is constructed with that helper's exact configured board; route input cannot
  select another board. Tokens and doors are never returned to the page.
- `joinSeat(input)` receives `expected_board` and, for a rejoin, an
  `expected_identity` object containing exact `board`, `agent_id`,
  `principal_id`, `agent_name`, and `role`. It must compare these fields to the
  rejoin target before mutation and return `{ ok, identity, rejoined }`.
- `readBoard({ board, include_retired: true })` returns
  `{ ok, board_id, agents }`; `retireSelf({ board, agent_name })` returns
  `{ ok, board_id, agent }`; `forgetDoor({ board, role })` returns
  `{ ok, board, role }`. Every result is validated again by the adapter.
- Routes return the adapter result body unchanged. Map success to HTTP 200;
  malformed JSON/selectors to 400; helper auth/origin failures to 401/403;
  identity conflicts, mismatches, and active leases to 409; unavailable
  dependencies to 503; inconsistent dependency/read-back results to 502; and
  confirmation-required or non-active lifecycle results to 422. The Home UI
  renders `message`, `recovery`, `retryable`, and `confirmation`, requires the
  user to type the exact confirmation before disconnect, and never infers
  success from HTTP status alone.

The assembly ticket owns these shared pairing points:

- `host/helper.cjs`: provide authenticated `joinSeat`, `readBoard`,
  `retireSelf`, and `forgetDoor` dependencies.
- `webui/routes.js`: expose board-bound lifecycle routes.
- `webui/index.html`, `webui/app.js`, and `webui/style.css`: render status,
  exact confirmation, recovery guidance, and accessible live results.
- `tests/host_helper.test.cjs` and browser acceptance: prove auth, origin,
  board isolation, and the assembled host flow without production mutation.

The component tests use injected fakes. Final acceptance still requires an
independent verifier to exercise the packaged candidate against authenticated
non-production board state.
