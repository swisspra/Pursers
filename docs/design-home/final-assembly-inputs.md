# Final Home assembly inputs

The final candidate starts at approved Home baseline
`c7c9a2ed2923a0c8bc4b2c4fce51491fa9dfe96a`. Approved feature deltas are
imported from their declared base `d0dd3822acd92337355464ba5b9db81dde7a6ccd`
and resolved semantically on the baseline rather than by replacing shared files.

| Capability | Approved input | Assembled interface |
| --- | --- | --- |
| Ticket lifecycle | `0b0f0b78385a0315aa4e93e574fa967d0cef65ab` | `/pursers/tickets`, `/pursers/tickets/status`, `/pursers/tickets/get`, `/pursers/tickets/create`, `/pursers/tickets/cancel` |
| Submitted results | `792dcae5dfe1a194329471fd914f0f6b1518db27` and its two ancestors after the declared base | read-only `/pursers/results`; explicit Central and board pin |
| Standalone groups | `20bb6bc5c54ad7b233dfc790a48d3bea335a92da` | `/pursers/groups`, `/pursers/groups/status`, `/pursers/groups/create`, `/pursers/groups/update`, `/pursers/groups/remove` |

The shared helper owns separate ticket and group sidecars. Both are closed on
helper shutdown. Result reads use the loopback Fleet endpoint with no browser or
session credentials, no redirects, a five-second timeout, and a bounded body.
Every Home route retains the exact Origin, local token, body-size, no-store, and
selected-board checks. Result startup additionally requires a safe `--central`
label and validates both returned `central` and `board_id`.

The deterministic package currently includes all ticket, result, and group
adapters and contracts. Shared `helper.cjs`, `routes.js`, `app.js`, styles,
documentation, package allow-list, and wait-bridge entry points retain all three
interfaces. Native Aion Team routes remain compatibility-only.

Two required inputs remain gated on coordinator approval:

- `TK-08269cf76117`: standalone seat join, truthful lifecycle status, and safe
  disconnect or retirement. Final assembly owns its helper, routes, UI, and
  package wiring after an approved executable interface and SHA are recorded.
- `TK-a8802d838910`: Fleet persistent read-only session and fail-closed
  deployment/rollback input. Only its coordinator-approved cumulative SHA may
  be imported.

After both inputs are approved, regenerate the cumulative integration manifest,
build the exact-SHA deterministic package, and run all repository, security,
leak, and rollback gates. The independent reviewer alone installs the observer,
owns the authenticated browser session, and captures the nine required final
observations against that immutable SHA.
