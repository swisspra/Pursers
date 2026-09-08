# Packaging Acceptance Checklist

## 1. Context and Baseline vs. Final-Train Boundary

- **Baseline Facts**: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (origin/main at released 5.0.0a25).
- **Current Integration Tip**: `9e3b05072e4521c10d0f4390d7ac57ad7421c07a` (incorporates foundation chore `133d5f5` and approved door onboarding backend + loopback hardening `9e3b050`).
- **Boundary Distinction**: This document distinguishes validated baseline facts from current integration state and pending final release-train acceptance. Baseline values reflect verified repository facts at `c2ebac5de803a0f7a00468ec4d3cdf06e4719096`. Current integration state reflects landed extensions and tooling up to `9e3b05072e4521c10d0f4390d7ac57ad7421c07a`. Final-train artifact values and ZIP inventory remain pending until full UI and release-train consolidation lands.

This document maps every runtime asset, route, context, and config to its packaged distribution path. It defines exact reproducible packaging commands, expected archive contents, no-source-tree dependency checks, relative URL checks, secret/home-path scan, and isolated install acceptance.

---

## 2. Extension Packaging

### 2.1. Build Command

```sh
python3 tools/aionui-extension/build.py
```

Produces `dist/pursers-aionui-0.1.0.zip`. The `--output` flag overrides the destination path, but the filename must match `pursers-aionui-{version}.zip` where version is derived from `aion-extension.json`.

### 2.2. Archive Inventory: Baseline vs. Current Integration

At release baseline (`c2ebac5`), the extension packaging prototype contained 9 allowlisted files. On current integration `origin/main` (`9e3b050`), approved door onboarding and security loopback have expanded the packaged inventory to 12 files.

- **ZIP Archive Size**: `16256` compressed archive bytes
- **Uncompressed Member Size**: `44919` uncompressed member bytes across 12 files

| Path in ZIP | Source | Purpose | Uncompressed Size | Added |
| --- | --- | --- | --- | --- |
| `aion-extension.json` | `aion-extension.json` | Extension manifest | 3886 B | Baseline |
| `README.md` | `README.md` | Operator documentation | 2646 B | Baseline |
| `contexts/reviewer.md` | `contexts/reviewer.md` | Reviewer seat governance | 1078 B | Baseline |
| `contexts/worker.md` | `contexts/worker.md` | Worker seat governance | 1067 B | Baseline |
| `door/adapter.cjs` | `door/adapter.cjs` | Door onboarding adapter logic | 16224 B | Integration (`9e3b050`) |
| `door/DOOR_ONBOARDING_CONTRACT.md` | `door/DOOR_ONBOARDING_CONTRACT.md` | Door onboarding contract specification | 3758 B | Integration (`9e3b050`) |
| `security/loopback.cjs` | `security/loopback.cjs` | Loopback request validation | 546 B | Integration (`9e3b050`) |
| `webui/app.js` | `webui/app.js` | Join form client logic | 1802 B | Baseline |
| `webui/index.html` | `webui/index.html` | Settings tab entry point | 1211 B | Baseline |
| `webui/routes.js` | `webui/routes.js` | API route handlers (join, status) | 6243 B | Baseline |
| `webui/style.css` | `webui/style.css` | Join tab styles | 861 B | Baseline |
| `vendor/aion-hub-extension-schema-v0.json` | `vendor/aion-hub-extension-schema-v0.json` | Vendored JSON Schema | 5597 B | Baseline |

### 2.3. Deterministic Build Properties

- **Fixed timestamp**: `(1980, 1, 1, 0, 0, 0)` on every `ZipInfo` entry.
- **Compression**: `ZIP_DEFLATED`, level 9.
- **File mode**: `0o100644` (regular file, read-only for group/other).
- **Byte-level reproducibility**: Two consecutive builds produce byte-identical archives (`16256` bytes).

### 2.4. Manifest Validation

`aion-extension.json` validates against `vendor/aion-hub-extension-schema-v0.json` (JSON Schema Draft 2020-12). Key constraints verified by tests:
- No `contributes.mcpServers` block (uses REST import path instead).
- 4 assistant presets: `worker-codex`, `worker-claude`, `reviewer-codex`, `reviewer-claude`.
- Each preset has `contextFile` pointing to `contexts/worker.md` or `contexts/reviewer.md`.
- Network: loopback-only (`127.0.0.1`, `localhost`, `::1`).
- Risk: moderate; Engine: `^2.2.1`.

### 2.5. Runtime Asset Mapping

| Manifest declaration | Packaged path | Runtime resolution |
| --- | --- | --- |
| Settings tab entry | `webui/index.html` | AionUI renders as settings tab |
| Static assets | `webui/` (directory) | Served at `/pursers/assets/` prefix |
| API route: join | `webui/routes.js` | `POST /pursers/join` (auth required) |
| API route: status | `webui/routes.js` | `GET /pursers/status` (auth required) |
| Assistant context: worker | `contexts/worker.md` | Applied to conversation context |
| Assistant context: reviewer | `contexts/reviewer.md` | Applied to conversation context |
| Door adapter | `door/adapter.cjs` | Door onboarding and credential persistence |
| Security loopback | `security/loopback.cjs` | Restricts mutations to loopback origins |

### 2.6. No-Source-Tree Runtime Dependencies

The extension archive contains only the 12 allowlisted files. The packaged `README.md` contains a documentary source-build command (`python tools/aionui-extension/build.py`), but all packaged executable runtime files (`webui/app.js`, `webui/index.html`, `webui/routes.js`, `webui/style.css`, `door/adapter.cjs`, `security/loopback.cjs`) have zero runtime dependencies on the source tree or repository paths (`tools/`, `packages/`).

Deterministic verification of runtime source-tree independence:
```sh
python3 -c '
import zipfile, re
with zipfile.ZipFile("dist/pursers-aionui-0.1.0.zip") as z:
    for name in z.namelist():
        if name.startswith("webui/") or name.startswith("door/") or name.startswith("security/"):
            data = z.read(name).decode("utf-8")
            assert not re.search(r"\b(tools|packages)/", data), f"Runtime source-tree reference in {name}"
print("PASS: runtime extension files contain zero source-tree dependencies")
'
```

### 2.7. External Dependency: pursers-wait-bridge

The extension calls `pursers-wait-bridge` (`BRIDGE_COMMAND` in `routes.js`) through `execFile`. This is an external command that must be installed separately:
```sh
uv tool install pursers-wait-bridge
# or
pipx install pursers-wait-bridge
```
If the bridge is missing, the Join tab returns HTTP 503 with `error: bridge_not_installed` and an `install_hint` containing the install command. The extension never echoes the door credential in the error response.

### 2.8. Secret and Home-Path Scan

Tracked tests in `test_package.py:test_package_has_no_secrets_home_paths_or_private_identifiers` scan all ZIP contents for:
- macOS (`/Users/`), Linux (`/home/`), and Windows (`C:\Users\`) home paths
- Operator identifiers (`swissp`) and fleet clone paths (`.pursers/fleet-dashboard`)
- Private key headers (`BEGIN PRIVATE KEY`), bearer headers (`Bearer `)
- Door credentials (`prs1.[A-Za-z0-9_-]{20,}`) and JWT tokens (`eyJ...`)

### 2.9. Extension Verification Commands & Executed Test Evidence

```sh
# 1. Build extension ZIP
python3 tools/aionui-extension/build.py

# 2. Run Python packaging and manifest test suite
# Covers all 4 test files: test_package.py (3), test_manifest.py (4), test_contexts.py (1), test_routes.py (1)
python3 -m pytest -q tools/aionui-extension/tests/
# Actual executed output: "9 passed in 0.15s"

# 3. Run Node route and adapter contract tests
node --test tools/aionui-extension/tests/routes.test.cjs
# Actual executed output: "pass 6, fail 0"

node --test tools/aionui-extension/tests/door_adapter.test.cjs
# Actual executed output: "pass 8, fail 0"

node --test tools/aionui-extension/tests/team_adapter.test.cjs
# Actual executed output: "pass 20, fail 0"
```

---

## 3. Dashboard-UI Packaging & Promotion Pipeline

### 3.1. Build and Explicit Promotion Pipeline

The Dashboard-UI build and promotion pipeline requires explicit, sequential execution. It is never automatic.

```sh
# Step 1: Install exact locked dependencies
cd tools/dashboard-ui
NODE_ENV= npm ci --include=dev

# Step 2: TypeScript type check
npm run typecheck

# Step 3: Single-file production build
# Produces dist/dashboard-entry.html and copies to tools/dashboard-ui/dashboard.html
npm run build

# Step 4: Explicit resource promotion copy to packages/personal
cd ../..
cp tools/dashboard-ui/dashboard.html packages/personal/src/pursers_personal/resources/dashboard.html

# Step 5: Compute generated and promoted artifact hash and size
shasum -a 256 tools/dashboard-ui/dashboard.html packages/personal/src/pursers_personal/resources/dashboard.html
# Validated baseline digest: 746c6eccd85afcc38588c8b9e1946ff2c91a7eb8477783c2f0d6bff0f4c6d922
# Validated baseline size: 404280 bytes

# Step 6: Verify and update EXPECTED_VIEW_SHA256 and EXPECTED_VIEW_SIZE
# In tools/regenerate_component_lock.py and packages/personal/tests/test_apps_contract.py

# Step 7: Regenerate component-lock.json with pinned wheels into self-contained wheel directory
mkdir -p /tmp/pursers-packaging-gate-2/wheels
python3 tools/regenerate_component_lock.py --wheel-dir /tmp/pursers-packaging-gate-2/wheels

# Step 8: Run exact-view contract test
PYTHONPATH=packages/personal/src:packages/central/src:packages/client/src \
  python3 -m pytest -q packages/personal/tests/test_apps_contract.py -k test_exact_view_lock
# Actual executed output: "1 passed, 136 deselected in 0.25s"

# Step 9: Compare generated, resource, and lock digests and size
python3 -c '
import json, hashlib
from pathlib import Path
built = Path("tools/dashboard-ui/dashboard.html").read_bytes()
promoted = Path("packages/personal/src/pursers_personal/resources/dashboard.html").read_bytes()
lock = json.loads(Path("packages/personal/src/pursers_personal/resources/component-lock.json").read_text(encoding="utf-8"))
b_hash = hashlib.sha256(built).hexdigest()
p_hash = hashlib.sha256(promoted).hexdigest()
l_hash = lock["view"]["sha256"]
assert b_hash == p_hash == l_hash, f"Hash mismatch: built={b_hash}, promoted={p_hash}, lock={l_hash}"
assert len(built) == len(promoted) == lock["view"]["size_bytes"], "Size mismatch"
print(f"PASS: digests and size match ({b_hash}, {len(built)} bytes)")
'
```

### 3.2. Deterministic Single-File & External Resource Assertion

The built `dashboard.html` must be a self-contained single-file application with zero external script, stylesheet, font, or CDN resources. The Vite modulepreload bootstrap may contain an inlined `fetch(e.href,n)` call as a build-tool artifact, which does not perform runtime data fetching (all runtime data flows through MCP Apps `PostMessageTransport`).

The following deterministic command verifies this contract:

```sh
python3 -c '
import re
from pathlib import Path

html_path = Path("packages/personal/src/pursers_personal/resources/dashboard.html")
assert html_path.is_file(), f"{html_path} missing"
content = html_path.read_text(encoding="utf-8")

# 1. Assert no external scripts
ext_scripts = re.findall(r"<script[^>]+src=", content, re.IGNORECASE)
assert not ext_scripts, f"External scripts found: {ext_scripts}"

# 2. Assert no external stylesheets
ext_links = re.findall(r"<link[^>]+rel=[\"\x27]?stylesheet", content, re.IGNORECASE)
assert not ext_links, f"External stylesheets found: {ext_links}"

# 3. Assert no external fonts or webfont imports
assert not re.search(r"@font-face", content), "External @font-face declaration found"

# 4. Assert no external CSS URLs
assert not re.search(r"url\(https?://", content), "External CSS URL found"

print("PASS: single self-contained HTML file with zero external script, stylesheet, font, or CDN resources")
'
```

### 3.3. Component Lock Schema and Contents

`component-lock.json` records:
- `schema_version`: 1
- `product_version`: 5.0.0a25 (baseline)
- `build_toolchain`: pinned build, setuptools, wheel, packaging, pyproject-hooks versions
- `components`: exactly two component entries with version, wheel SHA-256, and member file SHA-256 hashes:
  - `pursers-central` (`0.1.0a29`): 16 member files with SHA-256 hashes
  - `pursers-client` (`0.1.0a22`): 10 member files with SHA-256 hashes
- `view`: dashboard resource attestation:
  - `resource`: `pursers_personal/resources/dashboard.html`
  - `size_bytes`: `404280`
  - `sha256`: `746c6eccd85afcc38588c8b9e1946ff2c91a7eb8477783c2f0d6bff0f4c6d922`

### 3.4. Clean-Checkout Component Verification Recipe

From a clean checkout without pre-installed packages, `verify_component_artifacts()` requires an explicit virtual environment with the pinned component distributions installed. The following self-contained recipe creates a dedicated fresh environment under `/tmp/pursers-packaging-gate-2` and validates the installation:

```sh
# 1. Build pinned wheels into dedicated fresh directory
mkdir -p /tmp/pursers-packaging-gate-2/wheels
python3 tools/regenerate_component_lock.py --wheel-dir /tmp/pursers-packaging-gate-2/wheels

# 2. Create clean isolated virtual environment
python3.12 -m venv /tmp/pursers-packaging-gate-2/venv

# 3. Install built component wheels into isolated environment
/tmp/pursers-packaging-gate-2/venv/bin/pip install --disable-pip-version-check --no-input \
  /tmp/pursers-packaging-gate-2/wheels/pursers_central-0.1.0a29-py3-none-any.whl \
  /tmp/pursers-packaging-gate-2/wheels/pursers_client-0.1.0a22-py3-none-any.whl \
  /tmp/pursers-packaging-gate-2/wheels/pursers_personal-5.0.0a25-py3-none-any.whl

# 4. Execute component verification in clean environment
/tmp/pursers-packaging-gate-2/venv/bin/python -c '
from pursers_personal.artifacts import verify_component_artifacts
result = verify_component_artifacts()
print("PASS: verified components:", sorted(result.keys()))
'
# Actual executed output: "PASS: verified components: ['pursers-central', 'pursers-client']"
```

---

## 4. Fleet Dashboard Packaging

The fleet dashboard (`tools/fleet-dashboard/fleet_dashboard.py`) is a self-contained Python script with inline HTML, CSS, and JavaScript.

- **No build step required**: No ZIP, no compilation, no external bundled assets.
- **Serving**: Handled directly by Python `BaseHTTPRequestHandler` at `http://127.0.0.1:8899`.
- **Test execution**:
  ```sh
  python3 -m pytest -q tools/fleet-dashboard/tests/test_release_ops.py
  # Actual executed output: "21 passed in 0.23s"
  ```

---

## 5. Release Gate Checklist

### 5.1. Extension Release Gate

| Step | Executable Command | Owner | Pass Condition |
| --- | --- | --- | --- |
| 1. Build ZIP | `python3 tools/aionui-extension/build.py` | Extension developer | `dist/pursers-aionui-0.1.0.zip` exists (`16256` bytes archive / `44919` bytes uncompressed) |
| 2. Run Python test suite | `python3 -m pytest -q tools/aionui-extension/tests/` | Extension developer | 9 tests pass ("9 passed in 0.15s": test_package [3], test_manifest [4], test_contexts [1], test_routes [1]) |
| 3. Verify Node routes | `node --test tools/aionui-extension/tests/routes.test.cjs` | Extension developer | 6 tests pass, "pass 6, fail 0" |
| 4. Verify door adapter | `node --test tools/aionui-extension/tests/door_adapter.test.cjs` | Extension developer | 8 tests pass, "pass 8, fail 0" |
| 5. Verify team adapter | `node --test tools/aionui-extension/tests/team_adapter.test.cjs` | Extension developer | 20 tests pass, "pass 20, fail 0" |
| 6. Runtime dependency check | Deterministic script in Section 2.6 | Extension developer | PASS: runtime extension files contain zero source-tree dependencies |
| 7. Leak scan | `python3 tools/leak_scan.py tools/aionui-extension/` | Extension developer | clean (0 violations) |
| 8. Isolated install | Install ZIP via AionUI Settings -> Extensions -> Install from file | Operator | Settings tab renders Join form |

### 5.2. Dashboard-UI Release Gate

| Step | Executable Command | Owner | Pass Condition |
| --- | --- | --- | --- |
| 1. Install dependencies | `cd tools/dashboard-ui && NODE_ENV= npm ci --include=dev` | Dashboard developer | exit 0, 0 vulnerabilities |
| 2. Type check | `cd tools/dashboard-ui && npm run typecheck` | Dashboard developer | tsc --noEmit exit 0 |
| 3. Build single-file | `cd tools/dashboard-ui && npm run build` | Dashboard developer | `tools/dashboard-ui/dashboard.html` created (`404280` bytes) |
| 4. Promote to personal | `cp tools/dashboard-ui/dashboard.html packages/personal/src/pursers_personal/resources/dashboard.html` | Dashboard developer | File copied explicitly |
| 5. Digest & size check | `shasum -a 256 tools/dashboard-ui/dashboard.html packages/personal/src/pursers_personal/resources/dashboard.html` | Release engineer | Digests match exactly (`746c6ecc...`) |
| 6. Lock regeneration | `python3 tools/regenerate_component_lock.py --wheel-dir /tmp/pursers-packaging-gate-2/wheels` | Release engineer | `component-lock.json` updated with matching hash |
| 7. Exact-view test | `PYTHONPATH=packages/personal/src:packages/central/src:packages/client/src python3 -m pytest -q packages/personal/tests/test_apps_contract.py -k test_exact_view_lock` | Release engineer | 1 passed ("1 passed, 136 deselected in 0.25s") |
| 8. Clean-env verify | Self-contained 4-command recipe in Section 3.4 (under `/tmp/pursers-packaging-gate-2`) | Release engineer | PASS: verified components: ['pursers-central', 'pursers-client'] |
| 9. No external resources | Deterministic Python assertion script in Section 3.2 | Release engineer | PASS: single self-contained HTML file with zero external script, stylesheet, font, or CDN resources |

### 5.3. Fleet Dashboard Release Gate

| Step | Executable Command | Owner | Pass Condition |
| --- | --- | --- | --- |
| 1. Run tests | `python3 -m pytest -q tools/fleet-dashboard/tests/test_release_ops.py` | Fleet developer | All tests pass ("21 passed in 0.23s") |
| 2. Leak scan | `python3 tools/leak_scan.py tools/fleet-dashboard/fleet_dashboard.py` | Fleet developer | clean (0 violations) |
| 3. Start server | `python3 tools/fleet-dashboard/fleet_dashboard.py --token-file <TOKEN_FILE>` | Operator | Dashboard binds http://127.0.0.1:8899 |

---

## 6. Missing or Unpackaged Assets

The following items are not currently packaged as separate standalone artifacts:
1. **Logo / image assets**: None exist in source. All visual branding uses CSS text marks.
2. **Favicon**: None packaged for any surface; not a release blocker.
3. **AionUI preset picker**: Host limitation in AionUI 2.2.1; presets are defined in manifest but host may not display them in the picker.
4. **Native MCP elicitation forms**: Host limitation; handled via web forms in fleet dashboard or coordinator.
5. **Fleet dashboard source imports**: Fleet dashboard imports dependencies from `pursers_client`, `pursers_central`, and `runtime_environment`. It is designed to run from repository checkout or with installed wheels.

---

## 7. Related Tickets

- Extension UI implementation: `TK-df38c5eb3b9f`
- Design canvas: `TK-810b86e4b9c1`
- Journeys: `TK-1f8315536a3e`
- Quickstart guide: `TK-cadfa2b8b33f`
- Superdesign UI context: `TK-f8a62bab8d05`
