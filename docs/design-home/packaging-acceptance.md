# Packaging acceptance checklist

Baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (origin/main, 5.0.0a25).

This document maps every runtime asset, route, context, and config to its
packaged distribution path. It defines the exact reproducible packaging
commands, expected ZIP contents, no-source-tree dependency checks, relative
URL checks, secret/home-path scan, and isolated install acceptance. It is
the release gate for preventing source-test passes that ship broken ZIP or
embedded UI artifacts.

## Extension packaging

### Build command

```sh
python tools/aionui-extension/build.py
```

Produces `dist/pursers-aionui-0.1.0.zip`. The `--output` flag overrides the
destination path but the filename must match `pursers-aionui-{version}.zip`
where version comes from `aion-extension.json`.

### Expected ZIP contents (9 files)

| Path in ZIP | Source | Purpose |
| --- | --- | --- |
| `aion-extension.json` | `aion-extension.json` | Extension manifest |
| `README.md` | `README.md` | Operator documentation |
| `contexts/reviewer.md` | `contexts/reviewer.md` | Reviewer seat governance |
| `contexts/worker.md` | `contexts/worker.md` | Worker seat governance |
| `webui/app.js` | `webui/app.js` | Join form client logic |
| `webui/index.html` | `webui/index.html` | Settings tab entry point |
| `webui/routes.js` | `webui/routes.js` | API route handlers (join, status) |
| `webui/style.css` | `webui/style.css` | Join tab styles |
| `vendor/aion-hub-extension-schema-v0.json` | `vendor/aion-hub-extension-schema-v0.json` | Vendored JSON Schema |

### Deterministic build properties

- Fixed timestamp: `(1980, 1, 1, 0, 0, 0)` on every ZipInfo
- Compression: `ZIP_DEFLATED`, level 9
- File mode: `0o100644` (regular file, read-only for group/other)
- Two consecutive builds produce byte-identical archives

### Manifest validation

`aion-extension.json` validates against `vendor/aion-hub-extension-schema-v0.json`
(JSON Schema Draft 2020-12). Key constraints verified by tests:

- No `contributes.mcpServers` block (uses REST import path instead)
- 4 assistant presets: worker-codex, worker-claude, reviewer-codex, reviewer-claude
- Each preset has `contextFile` pointing to `contexts/worker.md` or `contexts/reviewer.md`
- Network: loopback-only (`127.0.0.1`, `localhost`, `::1`)
- Risk: moderate
- Engine: `^2.2.1`

### Runtime asset mapping

| Manifest declaration | Packaged path | Runtime resolution |
| --- | --- | --- |
| Settings tab entry | `webui/index.html` | AionUI renders as settings tab |
| Static assets | `webui/` (directory) | Served at `/pursers/assets/` prefix |
| API route: join | `webui/routes.js` | `POST /pursers/join` (auth required) |
| API route: status | `webui/routes.js` | `GET /pursers/status` (auth required) |
| Assistant context: worker | `contexts/worker.md` | Applied to conversation context |
| Assistant context: reviewer | `contexts/reviewer.md` | Applied to conversation context |

### No-source-tree dependencies

The ZIP contains only the 9 allowlisted files. No source-tree paths, build
scripts, or development dependencies are included. The extension:

- Does not reference `tools/`, `packages/`, or repository root paths
- Does not include `build.py`, `tests/`, or `__pycache__/`
- Does not bundle Node.js or Python runtime dependencies

### External dependency: pursers-wait-bridge

The extension calls `pursers-wait-bridge` (BRIDGE_COMMAND in `routes.js`,
line 6) through `execFile`. This is an external command that must be
installed separately:

```sh
uv tool install pursers-wait-bridge
# or
pipx install pursers-wait-bridge
```

If the bridge is missing, the Join tab returns HTTP 503 with
`error: bridge_not_installed` and an `install_hint` containing the install
command. The extension never echoes the door value in the error response.

### Relative URL checks

- `index.html` references `/pursers/assets/style.css` and `/pursers/assets/app.js`
- These URLs resolve through AionUI's static asset serving at the `/pursers/assets/` prefix
- `routes.js` uses `new URL(request.url).origin` for the MCP import endpoint
- No hardcoded `http://` or `https://` URLs except in README examples

### Secret and home-path scan

The test suite (`test_package.py:test_package_has_no_secrets_home_paths_or_private_identifiers`)
scans all ZIP contents for:

| Pattern | Why forbidden |
| --- | --- |
| `/Users/` | macOS home path |
| `/home/` | Linux home path |
| `C:\Users\` | Windows home path |
| `swissp` | Operator identifier |
| `.pursers/fleet-dashboard` | Fleet clone path |
| `BEGIN PRIVATE KEY` | Private key material |
| `Bearer ` | Auth header |
| `prs1.[A-Za-z0-9_-]{20,}` | Door credential |
| `eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+` | JWT token |

### Isolated install acceptance

1. Build the ZIP: `python tools/aionui-extension/build.py`
2. Install through AionUI Settings → Extensions → Install from file
3. Verify the "Pursers" settings tab appears
4. Verify the Join form renders with door input and Join button
5. Verify `GET /pursers/status` returns `ok: true` or `bridge_not_installed`
6. Do not unzip into an existing AionUI data directory by hand

### Extension tests

```sh
# Package, manifest, and context tests
python3 -m pytest tools/aionui-extension/tests/test_package.py tools/aionui-extension/tests/test_manifest.py tools/aionui-extension/tests/test_contexts.py -v

# Route contract tests (requires Node.js)
node --test tools/aionui-extension/tests/routes.test.cjs
```

| Test | Verifies |
| --- | --- |
| `test_package_contains_only_allowlisted_runtime_files` | ZIP namelist matches PACKAGE_FILES exactly |
| `test_package_build_is_byte_deterministic` | Two builds produce identical bytes |
| `test_package_has_no_secrets_home_paths_or_private_identifiers` | No forbidden patterns in ZIP |
| `test_manifest_validates_against_vendored_hub_schema` | JSON Schema validation passes |
| `test_manifest_contributes_backend_variants_without_mcp_block` | No mcpServers, correct preset variants |
| `test_permissions_are_loopback_only_and_moderate` | Network and risk constraints |
| `test_contexts_contain_seat_governance_verbatim` | Governance rules present in context files |
| `test_node_route_contract` (routes.test.cjs) | Join stores through bridge, status redacts, missing bridge handled |

## Dashboard-UI packaging

### Build command

```sh
cd tools/dashboard-ui
npm install
npm run build
```

The `build` script runs `vite build && cp dist/dashboard-entry.html dashboard.html`.
Vite with `vite-plugin-singlefile` inlines all JavaScript and CSS into a single
HTML file. The output is copied to `dashboard.html` in the dashboard-ui directory.

### Build output

| Artifact | Path | Size |
| --- | --- | --- |
| Built dashboard | `packages/personal/src/pursers_personal/resources/dashboard.html` | ~404 KB |
| Component lock | `packages/personal/src/pursers_personal/resources/component-lock.json` | ~4 KB |

### Component lock verification

`load_dashboard_html()` in `apps_server.py` (line 167) calls
`verify_component_artifacts()` before serving the dashboard. This verifies
the `component-lock.json` against installed component wheels and the dashboard
HTML hash. A mismatch prevents serving a stale or tampered dashboard.

### Component lock contents

`component-lock.json` records:

- `schema_version`: 1
- `product_version`: 5.0.0a25
- `build_toolchain`: exact build, setuptools, wheel, packaging, pyproject-hooks versions
- `components`: per-component version, wheel SHA-256, and member file SHA-256 hashes
  - `pursers-central` (0.1.0a29): 15 member files with hashes
  - `pursers-client` (0.1.0a22): member files with hashes
  - `pursers-personal` (5.0.0a25): member files with hashes
  - `pursers-personal-import` (5.0.0a3): member files with hashes
  - `pursers-wait-bridge` (0.1.0a15): member files with hashes
- `dashboard_html_sha256`: hash of the built dashboard.html

### No-source-tree dependencies in dashboard.html

The built `dashboard.html` is a self-contained single-file application:

- All JavaScript is inlined (no `<script src="...">` tags)
- All CSS is inlined in `<style>` tags
- No references to `tools/`, `packages/`, or source-tree paths
- No external CDN or font URLs
- The file contains synthetic fallback data for offline preview

### Relative URL checks for dashboard.html

The built file uses the MCP Apps `PostMessageTransport` bridge
(`window.parent` postMessage) for all data. No HTTP fetch calls are made
from the dashboard itself. All data flows through `callServerTool()`
which wraps `window.parent.postMessage`.

### Dashboard-UI tests

```sh
cd tools/dashboard-ui
npm run typecheck  # tsc --noEmit
```

TypeScript type checking validates the source before build. No runtime
test suite exists for the built HTML artifact itself; the component lock
and `verify_component_artifacts()` serve as the packaging integrity gate.

## Fleet dashboard packaging

The fleet dashboard (`tools/fleet-dashboard/fleet_dashboard.py`) is a
self-contained Python script with inline HTML/CSS/JS. It has no separate
build step. The HTML is embedded as a string constant and patched in-place
through `str.replace()` calls.

### No packaging required

- No ZIP, no build step, no external assets
- All HTML, CSS, and JavaScript are inline in the Python file
- Served directly by `BaseHTTPRequestHandler` at `http://127.0.0.1:8899`
- API key storage uses macOS Keychain (no plaintext in config files)

### Fleet dashboard tests

```sh
python3 -m pytest tools/fleet-dashboard/tests/ -v
```

Tests cover HTTP handler routing, human request forms, board detail
rendering, and configuration endpoints.

## Release gate checklist

### Extension release gate

| Step | Command | Owner | Pass condition |
| --- | --- | --- | --- |
| 1. Build ZIP | `python tools/aionui-extension/build.py` | Extension developer | `dist/pursers-aionui-0.1.0.zip` exists |
| 2. Verify ZIP contents | `python3 -m pytest tools/aionui-extension/tests/test_package.py -v` | Extension developer | 3 tests pass |
| 3. Verify manifest | `python3 -m pytest tools/aionui-extension/tests/test_manifest.py -v` | Extension developer | 3 tests pass |
| 4. Verify contexts | `python3 -m pytest tools/aionui-extension/tests/test_contexts.py -v` | Extension developer | 1 test passes |
| 5. Verify routes | `node --test tools/aionui-extension/tests/routes.test.cjs` | Extension developer | 3 tests pass, "pass 3" in output |
| 6. Leak scan | `python3 tools/leak_scan.py tools/aionui-extension/webui/` | Extension developer | clean (0 violations) |
| 7. Isolated install | Manual: install ZIP through AionUI Settings | Operator | Settings tab and Join form render |

### Dashboard-UI release gate

| Step | Command | Owner | Pass condition |
| --- | --- | --- | --- |
| 1. Typecheck | `cd tools/dashboard-ui && npm run typecheck` | Dashboard developer | tsc --noEmit passes |
| 2. Build | `cd tools/dashboard-ui && npm run build` | Dashboard developer | `dashboard.html` produced |
| 3. Copy to resources | Automatic in build script | Dashboard developer | File at `packages/personal/src/pursers_personal/resources/dashboard.html` |
| 4. Component lock | `python3 -c "from pursers_personal.apps_server import verify_component_artifacts; verify_component_artifacts()"` | Release engineer | Verification passes |
| 5. No source-tree refs | `grep -c "tools/\|packages/" packages/personal/src/pursers_personal/resources/dashboard.html` | Release engineer | 0 (or only in synthetic fallback data) |

### Fleet dashboard release gate

| Step | Command | Owner | Pass condition |
| --- | --- | --- | --- |
| 1. Tests | `python3 -m pytest tools/fleet-dashboard/tests/ -v` | Fleet developer | All tests pass |
| 2. Leak scan | `python3 tools/leak_scan.py tools/fleet-dashboard/fleet_dashboard.py` | Fleet developer | clean (0 violations) |
| 3. Start | `python3 tools/fleet-dashboard/fleet_dashboard.py --token-file <TOKEN_FILE>` | Operator | Dashboard at http://127.0.0.1:8899 |

## Missing or unpackaged assets

The following items are not currently packaged but are documented for
awareness:

1. **Logo/image assets:** None exist. All visual identity is CSS-generated
   text marks. No packaging needed.
2. **Favicon:** None exists for any surface. Not a release blocker.
3. **AionUI preset picker:** Extension presets are contributed in the
   manifest but AionUI 2.2.1 may not expose them in the conversation
   preset picker. This is a host limitation, not a packaging gap.
4. **Native elicitation forms:** AionUI does not render Pursers MCP
   elicitation forms. Use the fleet dashboard or coordinator fallback.
   This is a host limitation, not a packaging gap.
5. **Fleet dashboard external dependencies:** The fleet dashboard imports
   `pursers_client`, `pursers_central`, `door_admin`, `seat_config`,
   `release_ops`, and `runtime_environment` from the repository source
   tree. These are not packaged separately; the fleet dashboard must be
   run from a repository checkout or with the packages installed.

## Related tickets

- Extension UI implementation: TK-df38c5eb3b9f
- Design canvas: TK-810b86e4b9c1
- Journeys: TK-1f8315536a3e
- Quickstart guide: TK-cadfa2b8b33f
- Superdesign init artifacts: TK-f8a62bab8d05
