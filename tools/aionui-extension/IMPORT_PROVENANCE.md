# Imported extension candidate provenance

This integration starts from frozen Pursers main
`b06ce6627edb62fc588eee541fa568445b709054` and imports the complete unapproved
extension candidate `8ae39dafb9e1a681961c22259dccc19164231591` from
`codex/TK-df38c5eb3b9f-resubmit-2` without modifying that submitted branch.

Imported paths:

- `tools/aionui-extension/README.md`
- `tools/aionui-extension/aion-extension.json`
- `tools/aionui-extension/build.py`
- `tools/aionui-extension/tests/routes.test.cjs`
- `tools/aionui-extension/tests/test_manifest.py`
- `tools/aionui-extension/tests/test_package.py`
- `tools/aionui-extension/tests/test_routes.py`
- `tools/aionui-extension/tests/test_webui.py`
- `tools/aionui-extension/vendor/PROVENANCE.md`
- `tools/aionui-extension/vendor/aion-hub-extension-schema-v0.json`
- `tools/aionui-extension/webui/app.js`
- `tools/aionui-extension/webui/index.html`
- `tools/aionui-extension/webui/routes.js`
- `tools/aionui-extension/webui/style.css`

No approval or evidence transfers with the import. The final cumulative commit,
all imported files, helper transport changes, package, browser behavior, and
security boundary require fresh independent review.

The runtime continuation imports the observer-only delta from
`TK-fa155aa34187` remote tip
`codex/TK-fa155aa34187-resubmit-3@e4fa835ff9eaca3af5156c74c0c7335fbe43316a`:

- `CHANGELOG.md`
- `docs/design-home/acceptance-observer.md`
- `docs/design-home/acceptance.md`
- `tools/aionui-extension/tests/home_acceptance/browser_observer.py`
- `tools/aionui-extension/tests/home_acceptance/runner.py`
- `tools/aionui-extension/tests/home_acceptance/test_browser_observer.py`

The approved acceptance-harness implementation already present in this runtime
branch was preserved across the observer import. The imported observer fix
creates a CDP isolated world and binds every page-derived status, candidate,
and selected-board read to its execution context so page JavaScript cannot
forge the values. This integration also adds a deterministic installed-candidate
manifest and signed live-listener identity proof because the supported AionCore
0.2.1 host serves extension assets but does not execute packaged
`/pursers/status` handlers. The submitted observer input and this
non-mechanical contract change receive fresh review; no observer-ticket
approval is asserted.

The final train assembles on approved Home baseline
`c7c9a2ed2923a0c8bc4b2c4fce51491fa9dfe96a`. It imports these
coordinator-approved cumulative inputs:

- ticket lifecycle `0b0f0b78385a0315aa4e93e574fa967d0cef65ab`;
- submitted results `792dcae5dfe1a194329471fd914f0f6b1518db27`, including its two
  ancestors after declared base `d0dd3822acd92337355464ba5b9db81dde7a6ccd`;
- standalone groups `20bb6bc5c54ad7b233dfc790a48d3bea335a92da`; and
- Fleet persistent sessions and deployment `e229dcb08879666475c532fd7296f4c5a2d9167b`,
  including all four ancestors after frozen main; and
- standalone seat lifecycle `557fbc8af9362031dee6f18db84e3c604f4d133f`,
  including its four ancestors after the declared base.

Shared helper, routes, UI, package, and wait-bridge files preserve the union of
the approved behaviors. Result reads retain the explicit Central-and-board pin;
ticket, group, and seat operations retain separate board-pinned sidecars; the Fleet input
retains matching-only read-only takeover and fail-closed deployment and
rollback. Exact integration conflicts and remaining inputs are mapped in
`docs/design-home/final-assembly-inputs.md`.

`INTEGRATION_FILES.sha256` records every cumulative changed path relative to
the frozen main, excluding only the checksum manifest itself. The submission
must declare the exact cumulative diff and ancestry so verifier manifest
comparison and cumulative provenance describe the same tree.
