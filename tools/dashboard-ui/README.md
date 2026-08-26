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
