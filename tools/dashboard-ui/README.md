# Pursers dashboard UI source

Source for the single-file MCP App bundle served as `ui://pursers/dashboard`.

Build:

    NODE_ENV= npm ci --include=dev
    NODE_ENV= npm run build        # writes ./dashboard.html (single file)

Then copy `dashboard.html` to
`packages/personal/src/pursers_personal/resources/dashboard.html`,
update `EXPECTED_VIEW_SHA256` / `EXPECTED_VIEW_SIZE` in
`tools/regenerate_component_lock.py`, regenerate the component lock with that
script, and update the exact-view-lock test in
`packages/personal/tests/test_apps_contract.py`.

The `package-lock.json` is authoritative — always `npm ci`, never `npm install`.

Real-browser feed-error acceptance (requires a Full Access Ego Lite seat):

    python3 tests/run_feed_error_browser.py --ego-browser /PATH/TO/ego-browser

The check loads the built dashboard, sends product-shaped `tool-result` payloads
through the MCP Apps host transport, and asserts the exact sanitized
`feed_error` in both the DOM and Chromium accessibility tree. It also verifies
the stable contract selectors, keyboard focus, and absence of a raw sibling
error value from both surfaces.
