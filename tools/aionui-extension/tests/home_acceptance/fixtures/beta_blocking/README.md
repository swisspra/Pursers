# Beta-blocking synthetic fixtures

These fixtures support TK-3951189e9012 only. They are synthetic, operate on
`sandbox-home-acceptance`, and must never be treated as verifier-owned evidence.

Row mapping:

- 1: `valid-worker-door.json`; materialized only for a disposable loopback port.
- 2: `valid-reviewer-door.json`; materialized only for a disposable loopback port.
- 15: `extension-status-loaded.json`; helper/onboarding status for the exact sandbox board.
- 98: `personal-board-empty.json`; empty `board_snapshot` response.
- 123: `personal-board-identity.json`; exact board identity expectation.
- 128: `personal-fleet-projects.json`; `fleet_snapshot` with one synthetic project.

`fleet-board.json` is additional disposable-server support for the Fleet recipe
dry-runs. Door descriptors contain metadata only; `beta_blocking_fixtures.py`
materializes temporary credentials in memory. No production Central client is
used by `beta_blocking_fixture_server.py`.

The fixture server also exposes `/mcp-host/one/`, a parent host that loads the
exact tracked Personal dashboard as a sandboxed child frame and completes the
MCP Apps postMessage handshake. The `absent`, `ambiguous`, `wrong-bytes`, and
`wrong-board` variants are fail-closed browser cases. They remain synthetic and
must not be promoted to final acceptance evidence.
