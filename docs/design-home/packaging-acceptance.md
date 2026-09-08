# Packaging Acceptance Checklist

## 1. Evidence boundary

- Release baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (`5.0.0a25`).
- Validated integration source: `9e3b05072e4521c10d0f4390d7ac57ad7421c07a`, which includes the approved foundation and door onboarding/loopback integration.
- The measurements below are for that integration source. They are not acceptance evidence for pending Dashboard-UI or extension UI branches and must be rerun on the final release-train SHA.
- This ticket changes only this checklist. It does not bump a version, promote a release, or alter a package.

## 2. Packaged surfaces

### 2.1 AionUI extension ZIP

`tools/aionui-extension/build.py` creates `pursers-aionui-0.1.0.zip`. Its allowlist contains exactly these 12 members:

| ZIP member | Runtime purpose |
| --- | --- |
| `aion-extension.json` | Extension manifest |
| `README.md` | Operator documentation |
| `contexts/reviewer.md` | Reviewer conversation context |
| `contexts/worker.md` | Worker conversation context |
| `door/adapter.cjs` | Door validation, connect, rotation, and recovery adapter |
| `door/DOOR_ONBOARDING_CONTRACT.md` | Shipped adapter contract |
| `security/loopback.cjs` | Loopback/origin enforcement |
| `webui/app.js` | Settings-tab client |
| `webui/index.html` | Settings-tab entry point |
| `webui/routes.js` | HTTP route entry point |
| `webui/style.css` | Settings-tab styles |
| `vendor/aion-hub-extension-schema-v0.json` | Offline manifest schema |

Every ZIP member has timestamp `(1980, 1, 1, 0, 0, 0)`, mode `0o100644`, and DEFLATE level 9. Tests build twice and require byte-identical archives.

The current manifest maps these runtime entry points:

| Declaration | Packaged entry point | Runtime route or mount |
| --- | --- | --- |
| Settings tab | `webui/index.html` | Pursers settings tab |
| Static assets | `webui/` | `/pursers/assets` |
| Join | `webui/routes.js` | `POST /pursers/join` |
| Legacy status | `webui/routes.js` | `GET /pursers/status` |
| Validate | `webui/routes.js` | `POST /pursers/onboarding/validate` |
| Connect | `webui/routes.js` | `POST /pursers/onboarding/connect` |
| Onboarding status | `webui/routes.js` | `GET /pursers/onboarding/status` |
| Rotate | `webui/routes.js` | `POST /pursers/onboarding/rotate` |
| Recover | `webui/routes.js` | `POST /pursers/onboarding/recover` |
| Worker context | `contexts/worker.md` | Worker preset context |
| Reviewer context | `contexts/reviewer.md` | Reviewer preset context |

The packaged `README.md` truthfully contains a documentary reference to `tools/aionui-extension/build.py`. The automated gate allows that documentation reference while proving every runtime member is free of `tools/` and `packages/` source-tree paths. It also resolves every HTML `src`/`href` through the manifest's declared `/pursers/assets` to an actual ZIP member. This is runtime dependency proof, not the false claim that no packaged text mentions a source path.

`webui/routes.js` invokes the separately installed `pursers-wait-bridge`. The ZIP intentionally does not bundle that executable or its Python dependencies. Missing-bridge behavior is covered by the route tests and returns bounded `bridge_not_installed` data without echoing a door.

### 2.2 Embedded Personal dashboard

Source files are `tools/dashboard-ui/dashboard-entry.html` and `tools/dashboard-ui/src/*`. Vite single-file output is first created as `tools/dashboard-ui/dashboard.html`, then explicitly promoted to `packages/personal/src/pursers_personal/resources/dashboard.html`. Promotion is not automatic.

`packages/personal/src/pursers_personal/resources/component-lock.json` attests the promoted resource:

- `view.resource`: `pursers_personal/resources/dashboard.html`
- `view.sha256`: `746c6eccd85afcc38588c8b9e1946ff2c91a7eb8477783c2f0d6bff0f4c6d922`
- `view.size_bytes`: `404280`
- locked components: `pursers-central==0.1.0a29` and `pursers-client==0.1.0a22`
- pinned build toolchain: `build==1.3.0`, `setuptools==80.9.0`, `wheel==0.45.1`, `packaging==25.0`, `pyproject-hooks==1.2.0`

The Personal wheel includes both `dashboard.html` and `component-lock.json`. Verification installs the rebuilt Central, Client, and Personal wheels into the same fresh gate venv used by every Python command.

### 2.3 Fleet dashboard

`tools/fleet-dashboard/fleet_dashboard.py` contains its HTML, CSS, and JavaScript inline. It has no ZIP or front-end build step. It imports installed Pursers packages at runtime and is not a standalone binary.

## 3. Canonical automated gate

Run the following block verbatim from a fresh checkout of the candidate SHA. It creates exactly one fresh gate root and one explicit venv; every documented Python command uses `$PYTHON`. The lock generator creates its own pinned temporary wheel-build environment internally, as implemented by the tool.

```sh
#!/bin/sh
set -eu

GATE_ROOT=$(mktemp -d /tmp/pursers-packaging-gate.XXXXXX)
PYTHON="$GATE_ROOT/venv/bin/python"
export GATE_ROOT PYTHON

python3.12 -m venv "$GATE_ROOT/venv"
"$PYTHON" -m pip install --disable-pip-version-check --no-input \
  pytest==9.1.1 jsonschema==4.26.0

"$PYTHON" tools/aionui-extension/build.py \
  --output "$GATE_ROOT/pursers-aionui-0.1.0.zip"
"$PYTHON" -m pytest -q tools/aionui-extension/tests/
node --test tools/aionui-extension/tests/routes.test.cjs
node --test tools/aionui-extension/tests/door_adapter.test.cjs
node --test tools/aionui-extension/tests/team_adapter.test.cjs

(
  cd tools/dashboard-ui
  NODE_ENV= npm ci --include=dev
  npm run typecheck
  npm run build
)
cp tools/dashboard-ui/dashboard.html \
  packages/personal/src/pursers_personal/resources/dashboard.html

mkdir "$GATE_ROOT/wheels"
"$PYTHON" tools/regenerate_component_lock.py --wheel-dir "$GATE_ROOT/wheels"
"$PYTHON" -m pip install --disable-pip-version-check --no-input \
  "$GATE_ROOT"/wheels/pursers_central-0.1.0a29-py3-none-any.whl \
  "$GATE_ROOT"/wheels/pursers_client-0.1.0a22-py3-none-any.whl \
  "$GATE_ROOT"/wheels/pursers_personal-5.0.0a25-py3-none-any.whl

PYTHONPATH=packages/personal/src:packages/central/src:packages/client/src \
  "$PYTHON" -m pytest -q packages/personal/tests/test_apps_contract.py \
  -k test_exact_view_lock

"$PYTHON" - <<'PY'
from pursers_personal.artifacts import verify_component_artifacts

result = verify_component_artifacts()
print("PASS component verification:", sorted(result))
PY

"$PYTHON" - <<'PY'
import hashlib
import json
from pathlib import Path

built = Path("tools/dashboard-ui/dashboard.html").read_bytes()
promoted = Path(
    "packages/personal/src/pursers_personal/resources/dashboard.html"
).read_bytes()
lock = json.loads(
    Path(
        "packages/personal/src/pursers_personal/resources/component-lock.json"
    ).read_text(encoding="utf-8")
)
digest = hashlib.sha256(built).hexdigest()
assert built == promoted
assert digest == lock["view"]["sha256"]
assert len(built) == lock["view"]["size_bytes"]
print(f"PASS dashboard promotion: {digest}, {len(built)} bytes")
PY

"$PYTHON" - <<'PY'
import re
from pathlib import Path

path = Path("packages/personal/src/pursers_personal/resources/dashboard.html")
content = path.read_text(encoding="utf-8")
assert not re.search(r"<script[^>]+src=", content, re.IGNORECASE)
assert not re.search(
    r"<link[^>]+rel=[\"']?stylesheet", content, re.IGNORECASE
)
assert "@font-face" not in content
assert not re.search(r"url\(https?://", content)
print("PASS dashboard resources: self-contained HTML")
PY

"$PYTHON" - <<'PY'
import json
import re
from pathlib import Path
from zipfile import ZipFile

path = Path(__import__("os").environ["GATE_ROOT"]) / "pursers-aionui-0.1.0.zip"
with ZipFile(path) as archive:
    names = archive.namelist()
    assert len(names) == 12
    member_bytes = sum(item.file_size for item in archive.infolist())
    runtime_names = [name for name in names if name != "README.md"]
    runtime_text = "\n".join(
        archive.read(name).decode("utf-8", errors="replace")
        for name in runtime_names
    )
    readme = archive.read("README.md").decode("utf-8")
    assert "tools/aionui-extension/build.py" in readme
    assert "tools/" not in runtime_text
    assert "packages/" not in runtime_text
    manifest = json.loads(archive.read("aion-extension.json"))
    tabs = manifest["contributes"]["settingsTabs"]
    assert len(tabs) == 1
    entry = tabs[0]["entryPoint"]
    assert entry in names
    for route in manifest["contributes"]["webui"]["apiRoutes"]:
        assert route["entryPoint"] in names
    html = archive.read(entry).decode("utf-8")
    references = re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", html)
    static = manifest["contributes"]["webui"]["staticAssets"]
    assert len(static) == 1
    prefix = static[0]["urlPrefix"].rstrip("/")
    directory = static[0]["directory"].rstrip("/")
    assert all(reference.startswith(prefix + "/") for reference in references)
    assert all(
        f"{directory}/{reference.removeprefix(prefix + '/')}" in names
        for reference in references
    )
print(
    f"PASS extension ZIP: {len(path.read_bytes())} archive bytes, "
    f"{member_bytes} uncompressed member bytes, 12 members, "
    "runtime source-tree independent, declared asset URLs resolve"
)
PY

"$PYTHON" -m pytest -q tools/fleet-dashboard/tests/test_release_ops.py
"$PYTHON" tools/leak_scan.py tools/aionui-extension/
"$PYTHON" tools/leak_scan.py tools/fleet-dashboard/fleet_dashboard.py
git diff --check
git status --short
printf 'GATE_ROOT=%s\n' "$GATE_ROOT"
```

No cleanup command is part of the recipe. Each execution creates new paths and preserves its evidence until the operating system or operator removes it.

## 4. Executed evidence

The canonical block passed end-to-end in a fresh detached checkout at `9e3b05072e4521c10d0f4390d7ac57ad7421c07a` on 2026-09-08 UTC. The final successful run created a new randomized `/tmp/pursers-packaging-gate.*` root and used only its `venv/bin/python` for Python gates.

| Gate | Exact result |
| --- | --- |
| Extension Python suite | `9 passed in 0.67s`: `test_contexts.py` 1, `test_manifest.py` 4, `test_package.py` 3, `test_routes.py` 1 |
| Legacy route Node suite | `pass 6`, `fail 0` |
| Door adapter Node suite | `pass 8`, `fail 0` |
| Team adapter Node suite | `pass 20`, `fail 0` |
| Dashboard dependency install | 120 packages, 0 vulnerabilities |
| Dashboard typecheck/build | `tsc --noEmit` passed; 148 modules transformed; generated resource 404.28 kB |
| Exact-view contract | `1 passed, 136 deselected in 0.99s` |
| Installed component verification | `PASS component verification: ['pursers-central', 'pursers-client']` |
| Dashboard bytes | SHA-256 `746c6eccd85afcc38588c8b9e1946ff2c91a7eb8477783c2f0d6bff0f4c6d922`; 404280 bytes; generated, promoted, and lock values match |
| Dashboard external resources | `PASS dashboard resources: self-contained HTML` |
| Extension ZIP | 16256 archive bytes; 44919 uncompressed member bytes; 12 members |
| ZIP runtime/path proof | Runtime members have no source-tree path dependency; all declared entries exist; `/pursers/assets/*` references resolve to packaged `webui/*` members |
| Fleet release-ops suite | `21 passed in 0.12s` |
| Leak scans | Extension and Fleet: `clean (0 violations)` |
| Checkout integrity | `git diff --check` passed; `git status --short` produced no output after regeneration, proving generated resource and lock match tracked integration bytes |

The archive size is the compressed ZIP byte count. The uncompressed-member size is the sum of the 12 `ZipInfo.file_size` values; these are intentionally distinct metrics.

## 5. Release gate ownership

| Gate | Owner | Acceptance |
| --- | --- | --- |
| Extension build, manifest, package, route, adapter, leak gates | Extension developer | Canonical automated gate passes on final SHA |
| Dashboard dependency, typecheck, single-file build, explicit promotion | Dashboard developer | Canonical automated gate passes on final SHA |
| Component lock regeneration, wheel install, exact-view and artifact verification | Release engineer | Canonical automated gate passes on final SHA |
| Fleet release-ops and leak gates | Fleet developer | Canonical automated gate passes on final SHA |
| Install ZIP through AionUI Settings | Operator | Settings tab loads, all assets resolve, and join/status flows return typed safe states |
| Final archive/member inventory and digest record | Release engineer | Remeasured values recorded from final release-train SHA |

The AionUI installation row is manual and was not executed by this read-only audit. No GUI proof is claimed. Current measurements are integration evidence only; pending UI changes can alter the ZIP inventory, member bytes, routes, dashboard digest, and lock.

## 6. Known packaging boundaries

1. No standalone logo, image, or favicon file is packaged; current branding is text/CSS.
2. `pursers-wait-bridge` remains an external prerequisite and is not embedded in the ZIP.
3. Fleet dashboard runtime imports require installed Pursers packages or a repository checkout.
4. Host support for preset presentation and native elicitation must be verified manually on the supported AionUI release; the manifest alone is not GUI proof.
5. Final train acceptance must rerun the canonical block and manual install gate after the extension UI and Dashboard-UI branches are integrated.

## 7. Related work

- Extension UI: `TK-df38c5eb3b9f`
- Design canvas: `TK-810b86e4b9c1`
- Journeys: `TK-1f8315536a3e`
- Quickstart: `TK-cadfa2b8b33f`
- Approved inventory: `TK-f8a62bab8d05`
