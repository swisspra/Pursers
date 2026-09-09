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
| Fleet persistent session and deployment | `e229dcb08879666475c532fd7296f4c5a2d9167b` and all four ancestors after frozen main `b06ce6627edb62fc588eee541fa568445b709054` | matching-only read-only dashboard takeover plus fail-closed deploy, verify, and rollback runbooks |
| Standalone seat lifecycle | `557fbc8af9362031dee6f18db84e3c604f4d133f` and its four ancestors after the declared base | `/pursers/seat-lifecycle/join`, `/pursers/seat-lifecycle/status`, `/pursers/seat-lifecycle/disconnect`; exact identity preservation and verified cooperative retirement |

The shared helper owns separate ticket, group, and seat sidecars. All are closed on
helper shutdown. Result reads use the loopback Fleet endpoint with no browser or
session credentials, no redirects, a five-second timeout, and a bounded body.
Every Home route retains the exact Origin, local token, body-size, no-store, and
selected-board checks. Result startup additionally requires a safe `--central`
label and validates both returned `central` and `board_id`.

The deterministic package includes all ticket, result, group, and seat
adapters and contracts. Shared `helper.cjs`, `routes.js`, `app.js`, styles,
documentation, package allow-list, and wait-bridge entry points retain all four
interfaces. Native Aion Team routes remain compatibility-only.

No feature input remains gated. The final assembly regenerates the cumulative
integration manifest, builds the exact-SHA deterministic package, and runs all
repository, security, leak, and rollback gates. The independent reviewer alone installs the observer,
owns the authenticated browser session, and captures the nine required final
observations against that immutable SHA.
