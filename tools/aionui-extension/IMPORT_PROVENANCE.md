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

`INTEGRATION_FILES.sha256` records every cumulative changed path relative to
the frozen main, excluding only the checksum manifest itself. The submission
must declare the exact single-commit diff so verifier manifest comparison and
cumulative provenance describe the same tree.
