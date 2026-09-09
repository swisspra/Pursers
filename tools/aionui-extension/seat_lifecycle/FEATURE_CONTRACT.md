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
`handed_off` and `stale` are non-active results with distinct codes and bounded
same-identity rejoin guidance.

`disconnect` means cooperative retirement of the current seat. It must never
retire another identity. Active work or review leases remain Central-owned
blockers and are shown as recoverable errors. Rejoining with the same board,
principal, and agent name reactivates the identity; a different identity is a
conflict, not a replacement.

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

The integration owner pairs the reviewed component into shared files:

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
