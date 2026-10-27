# Native workflow delivery runtime binding

`tools/board-butler/workflow_runtime.py` exposes this async binding API:

```python
runtime, compiled_policy = await build_workflow_runtime(backend, project)
```

The project must be the exact registered project view (the registry key is
`__project_name__`, or `name`) and carry its registered board identity. The
helper verifies the Central `project_registry` row is active, the repository
and fleet-owned clone match, and the effective `delivery_policy` resolves to an
activated supported batch mode. It then requires exactly one existing enabled,
board-scoped connector runtime with the necessary predeclared read-only and
risky mutation tools. A configured source's explicit project mapping may
narrow/authorize that route, but a source declaration is not required: a
coordinator-only project can use a connector runtime explicitly configured for
the exact registry project ID. Board-default/foreign/unmatched connectors do
not authorize a project. Missing, ambiguous, or unavailable inputs raise the
existing typed `board_butler.ConnectorDenied` error before any connector
operation occurs.

The result is the installed `integration_delivery.BatchDeliveryRuntime` plus
its compiled runtime policy. Native workflow/controller code supplies actual
independently reviewed members and validation results. Binding does not create
members, assert checks passed, publish, or merge. Git and customer PR actions
continue through `VerifiedGitConnectorAdapter`, `_ResidentBatchConnector`, and
the existing connector allowlist/policy gate. Mutation requires the precise
ephemeral source-writeback grant; the helper never replaces the configured
runtime gate, preserving that gate's decision and unknown-outcome behavior. A
home-board connector can serve a nonhome registry board only when an enabled
existing source declaration explicitly maps a hint to that exact registry
project (or declares registry-key routing). No route does not permit cloning or
rebinding the connector to a foreign board. Durable ledger
filenames are derived from the full project, board, repository, policy revision,
connector, and clone identity, so projects sharing a board cannot collide. The
cache uses the same complete identity.

Use a private state directory already owned by the resident when available; the
source-index-free coordinator fallback is `$PURSERS_STATE_DIR/board-butler/batch-delivery` (default `~/.pursers/board-butler/batch-delivery`). The resolved ledger directory must be absolute, private, and owned by the service user. Connector commands and validation commands are passed through existing APIs unchanged; this binding does not interpret shell syntax or expand credentials.

Focused tests in `tools/board-butler/tests/test_workflow_runtime.py` use only
throwaway local Git repositories and in-memory connector fakes. They make no
provider/model calls and no customer writes.
