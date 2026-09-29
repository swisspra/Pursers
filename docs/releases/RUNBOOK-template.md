# Pursers host rollout template

Use this template for releases after v5.0.6. Replace every `/PATH/TO/...` and
version placeholder from the candidate's `tools/release_versions.toml`; do not
copy values from an older runbook. Only an operator may run these commands.
Keep the previous wheel directory, virtual environments, service definitions,
database snapshot, and checkout SHAs until verification is complete.

## 1. Freeze the release inputs

```sh
set -eu

export RELEASE_TAG=vX.Y.Z
: "${RELEASE_SHA:?set the approved full 40-hex commit}"
export RELEASE_CHECKOUT=/PATH/TO/clean-release-checkout
export RELEASE_WHEELS=/PATH/TO/verified-release-wheels
export ROLLOUT_STATE=/PATH/TO/private/rollout-state
export CENTRAL_DATA_DIR=/PATH/TO/private/central-data
export CENTRAL_DB="$CENTRAL_DATA_DIR/central.sqlite3"
export CENTRAL_LAUNCHER=/PATH/TO/host-owned/serve_tls.py
export CENTRAL_HEALTH_URL=https://127.0.0.1:8766/healthz
export CENTRAL_SMOKE_PORT=18766
export CENTRAL_SERVICE_KIND=launchd  # or systemd
export CENTRAL_SERVICE_FILE=/PATH/TO/service-definition
export CENTRAL_SERVICE_NAME=com.pursers.central

test "$(git -C "$RELEASE_CHECKOUT" rev-parse --verify "$RELEASE_TAG^{commit}")" = "$RELEASE_SHA"
test "$(git -C "$RELEASE_CHECKOUT" rev-parse --verify 'HEAD^{commit}')" = "$RELEASE_SHA"
python3 "$RELEASE_CHECKOUT/tools/release_train.py" check
install -d -m 700 "$ROLLOUT_STATE"
```

Verify `SHA256SUMS.txt` and the exact wheel cohort before installing anything:

```sh
(cd "$RELEASE_WHEELS" && shasum -a 256 -c SHA256SUMS.txt)
python3 - "$RELEASE_CHECKOUT" "$RELEASE_WHEELS" <<'PY'
import sys
from pathlib import Path

checkout, wheels = map(Path, sys.argv[1:])
sys.path.insert(0, str(checkout))
from tools.release_versions import expected_wheel_filenames

actual = {path.name for path in wheels.glob("*.whl")}
expected = set(expected_wheel_filenames())
if actual != expected:
    raise SystemExit(f"wheel cohort mismatch: actual={sorted(actual)!r}, expected={sorted(expected)!r}")
PY
```

## 2. Install exact Pursers packages while resolving third-party dependencies

The GitHub release directory contains only the Pursers wheels. Do not use
`--no-index` with that directory: packages such as `mcp[cli]`, `PyJWT`, and
`uvicorn` must resolve from the configured package index. Pin every Pursers
distribution explicitly. An optional constraints file may freeze already
approved third-party versions, but it must not replace the exact Pursers pins.

Read the pins from the candidate, not from this document:

```sh
eval "$(python3 - "$RELEASE_CHECKOUT/tools/release_versions.toml" <<'PY'
import shlex, sys, tomllib

document = tomllib.load(open(sys.argv[1], "rb"))
packages = document["packages"]
values = {
    "PRODUCT_VERSION": document["product"],
    "CENTRAL_VERSION": packages["central"],
    "CLIENT_VERSION": packages["client"],
    "WAIT_BRIDGE_VERSION": packages["wait_bridge"],
    "ACP_VERSION": packages["acp"],
    "IMPORT_VERSION": packages["import"],
}
for key, value in values.items():
    print(f"export {key}={shlex.quote(value)}")
PY
)"
```

For a new environment, install the exact required Pursers set and allow normal
index resolution for everything else:

```sh
uv pip install --python "$TARGET_VENV/bin/python" \
  --find-links "$RELEASE_WHEELS" \
  "pursers-central==$CENTRAL_VERSION" \
  "pursers-client==$CLIENT_VERSION" \
  "pursers-wait-bridge==$WAIT_BRIDGE_VERSION"
"$TARGET_VENV/bin/python" -m pip check
```

For an existing service environment, upgrade its complete installed Pursers
set in one transaction. The script keeps service-specific subsets, adds the
required client, and rejects unknown `pursers-*` distributions instead of
silently leaving an old pin behind.

```sh
requirements=$(
  "$SERVICE_PYTHON" - "$RELEASE_CHECKOUT/tools/release_versions.toml" <<'PY'
import importlib.metadata as metadata
import sys, tomllib

document = tomllib.load(open(sys.argv[1], "rb"))
packages = document["packages"]
expected = {
    "pursers": document["product"],
    "pursers-personal": document["product"],
    "pursers-central": packages["central"],
    "pursers-client": packages["client"],
    "pursers-wait-bridge": packages["wait_bridge"],
    "pursers-acp": packages["acp"],
    "pursers-personal-import": packages["import"],
}
installed = {
    dist.metadata["Name"].lower()
    for dist in metadata.distributions()
    if (dist.metadata.get("Name") or "").lower().startswith("pursers")
}
unknown = installed - expected.keys()
if unknown:
    raise SystemExit(f"unknown Pursers distributions: {sorted(unknown)!r}")
installed.add("pursers-client")
for name in sorted(installed):
    print(f"{name}=={expected[name]}")
PY
)
test -n "$requirements"
# Intentional word splitting: one validated requirement per line.
# shellcheck disable=SC2086
uv pip install --python "$SERVICE_PYTHON" \
  --find-links "$RELEASE_WHEELS" \
  $requirements
"$SERVICE_PYTHON" -m pip check
```

Prove that installed Python sources came from the verified wheel bytes. Run
this with each upgraded interpreter; it compares every non-metadata `.py`
member in each installed Pursers wheel with the installed file.

```sh
"$SERVICE_PYTHON" - "$RELEASE_WHEELS" <<'PY'
import hashlib
import importlib.metadata as metadata
import sys
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath

wheel_dir = Path(sys.argv[1])
checked = 0
for wheel in sorted(wheel_dir.glob("*.whl")):
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        name = Parser().parsestr(archive.read(metadata_name).decode()).get("Name")
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for member in archive.namelist():
            if not member.endswith(".py") or ".dist-info/" in member:
                continue
            installed = Path(distribution.locate_file(PurePosixPath(member)))
            if not installed.is_file():
                raise SystemExit(f"missing installed wheel member: {name}:{member}")
            if hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(
                archive.read(member)
            ).digest():
                raise SystemExit(f"source mismatch: {name}:{member}")
            checked += 1
if checked == 0:
    raise SystemExit("no installed Pursers Python sources were checked")
print(f"verified installed Pursers source files: {checked}")
PY
```

## 3. Upgrade Central side by side

Never replace Central's live environment in place. Create a new environment
and a release profile that records both artifact and installed-source hashes.

```sh
export CENTRAL_VENV=/PATH/TO/private/central-venvs/$RELEASE_TAG
export CENTRAL_PROFILE="$ROLLOUT_STATE/central-release.env"
export CENTRAL_WHEEL=$(find "$RELEASE_WHEELS" -maxdepth 1 -name "pursers_central-${CENTRAL_VERSION}-*.whl" -print -quit)
export CLIENT_WHEEL=$(find "$RELEASE_WHEELS" -maxdepth 1 -name "pursers_client-${CLIENT_VERSION}-*.whl" -print -quit)
test -f "$CENTRAL_WHEEL"
test -f "$CLIENT_WHEEL"

uv venv --python /PATH/TO/python3.12 "$CENTRAL_VENV"
uv pip install --python "$CENTRAL_VENV/bin/python" \
  --find-links "$RELEASE_WHEELS" \
  "pursers-central==$CENTRAL_VERSION" \
  "pursers-client==$CLIENT_VERSION"
"$CENTRAL_VENV/bin/python" -m pip check

CENTRAL_SHA256=$(shasum -a 256 "$CENTRAL_WHEEL" | awk '{print $1}')
CLIENT_SHA256=$(shasum -a 256 "$CLIENT_WHEEL" | awk '{print $1}')
CENTRAL_SOURCE_SHA256=$(
  "$CENTRAL_VENV/bin/python" - <<'PY'
import hashlib, importlib.metadata as metadata
dist = metadata.distribution("pursers-central")
paths = sorted(
    p for p in (dist.files or []) if str(p).endswith(".py") and ".dist-info/" not in str(p)
)
h = hashlib.sha256()
for path in paths:
    data = dist.locate_file(path).read_bytes()
    h.update(str(path).encode() + b"\0" + data + b"\0")
print(h.hexdigest())
PY
)
CLIENT_SOURCE_SHA256=$(
  "$CENTRAL_VENV/bin/python" - <<'PY'
import hashlib, importlib.metadata as metadata
dist = metadata.distribution("pursers-client")
paths = sorted(
    p for p in (dist.files or []) if str(p).endswith(".py") and ".dist-info/" not in str(p)
)
h = hashlib.sha256()
for path in paths:
    data = dist.locate_file(path).read_bytes()
    h.update(str(path).encode() + b"\0" + data + b"\0")
print(h.hexdigest())
PY
)
umask 077
cat > "$CENTRAL_PROFILE" <<EOF
CENTRAL_VENV=$CENTRAL_VENV
CENTRAL_WHEEL=$CENTRAL_WHEEL
CENTRAL_WHEEL_SHA256=$CENTRAL_SHA256
CENTRAL_SOURCE_SHA256=$CENTRAL_SOURCE_SHA256
CLIENT_WHEEL=$CLIENT_WHEEL
CLIENT_WHEEL_SHA256=$CLIENT_SHA256
CLIENT_SOURCE_SHA256=$CLIENT_SOURCE_SHA256
EOF
chmod 600 "$CENTRAL_PROFILE"
```

Create an SQLite backup for the smoke test. Never point the candidate at the
live database directory.

```sh
export CENTRAL_SMOKE_DATA="$ROLLOUT_STATE/central-smoke-data"
install -d -m 700 "$CENTRAL_SMOKE_DATA"
sqlite3 "$CENTRAL_DB" ".backup '$CENTRAL_SMOKE_DATA/central.sqlite3'"
sqlite3 "$CENTRAL_SMOKE_DATA/central.sqlite3" 'PRAGMA integrity_check;' | grep -Fx ok
```

Start the host's `serve_tls.py` launcher on the alternate port with
`build_app(data_dir_override=...)`. The host-owned profile must provide its
normal TLS/auth settings; do not copy secrets into this repository.

```sh
set -a
. "$CENTRAL_PROFILE"
. /PATH/TO/private/current-central-auth.env
set +a
"$CENTRAL_VENV/bin/python" - "$CENTRAL_LAUNCHER" "$CENTRAL_SMOKE_DATA" "$CENTRAL_SMOKE_PORT" <<'PY' &
import importlib.util, sys
from pathlib import Path
import uvicorn

launcher, data_dir, port = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
spec = importlib.util.spec_from_file_location("rollout_serve_tls", launcher)
if spec is None or spec.loader is None:
    raise SystemExit("cannot load Central launcher")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
app = module.build_app(data_dir_override=data_dir)
uvicorn.run(app, host="127.0.0.1", port=port, server_header=False, access_log=False)
PY
SMOKE_PID=$!
trap 'kill "$SMOKE_PID" 2>/dev/null || true' EXIT HUP INT TERM
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  curl --fail --silent --show-error \
    "http://127.0.0.1:$CENTRAL_SMOKE_PORT/healthz" > "$ROLLOUT_STATE/central-smoke-health.json" && break
  sleep 2
done
test -s "$ROLLOUT_STATE/central-smoke-health.json"
python3 - "$ROLLOUT_STATE/central-smoke-health.json" "$CENTRAL_VERSION" <<'PY'
import json, sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
actual = payload.get("central_version", payload.get("version"))
if payload.get("status") != "ok" or actual != sys.argv[2]:
    raise SystemExit(f"Central smoke health/version mismatch: {payload!r}")
PY
"$CENTRAL_VENV/bin/python" -c \
  'import importlib.metadata as m,sys; assert m.version("pursers-central") == sys.argv[1]' \
  "$CENTRAL_VERSION"
kill "$SMOKE_PID"
wait "$SMOKE_PID" || true
trap - EXIT HUP INT TERM
```

Take a second SQLite `.backup` immediately before cutover and back up the
complete service definition. Then repoint exactly one service definition to
`CENTRAL_VENV` and `CENTRAL_PROFILE`.

```sh
sqlite3 "$CENTRAL_DB" ".backup '$ROLLOUT_STATE/central.pre-cutover.sqlite3'"
sqlite3 "$ROLLOUT_STATE/central.pre-cutover.sqlite3" 'PRAGMA integrity_check;' | grep -Fx ok
cp -p "$CENTRAL_SERVICE_FILE" "$ROLLOUT_STATE/central.service.before"
```

For launchd, edit the staged plist, run `plutil -lint`, then use a bounded
`bootout`/`bootstrap` retry. For systemd, stage the updated unit/drop-in, run
`systemd-analyze verify`, `systemctl daemon-reload`, and restart only Central.
The launch wrapper or unit must consume `PURSERS_CENTRAL_PYTHON`; merely adding
an unused environment variable does not repoint the service. For a direct
systemd `ExecStart`, replace only the interpreter and preserve the reviewed
host, port, TLS, and profile arguments.

```sh
case "$CENTRAL_SERVICE_KIND" in
  launchd)
    /usr/libexec/PlistBuddy -c \
      "Add :EnvironmentVariables:PURSERS_CENTRAL_PYTHON string $CENTRAL_VENV/bin/python" \
      "$CENTRAL_SERVICE_FILE" 2>/dev/null || \
      /usr/libexec/PlistBuddy -c \
        "Set :EnvironmentVariables:PURSERS_CENTRAL_PYTHON $CENTRAL_VENV/bin/python" \
        "$CENTRAL_SERVICE_FILE"
    /usr/libexec/PlistBuddy -c \
      "Add :EnvironmentVariables:CENTRAL_PROFILE string $CENTRAL_PROFILE" \
      "$CENTRAL_SERVICE_FILE" 2>/dev/null || \
      /usr/libexec/PlistBuddy -c \
        "Set :EnvironmentVariables:CENTRAL_PROFILE $CENTRAL_PROFILE" \
        "$CENTRAL_SERVICE_FILE"
    plutil -lint "$CENTRAL_SERVICE_FILE"
    test "$(plutil -extract EnvironmentVariables.PURSERS_CENTRAL_PYTHON raw \
      "$CENTRAL_SERVICE_FILE")" = "$CENTRAL_VENV/bin/python"
    launchctl bootout "gui/$(id -u)/$CENTRAL_SERVICE_NAME" || true
    for attempt in 1 2 3 4 5; do
      launchctl bootstrap "gui/$(id -u)" "$CENTRAL_SERVICE_FILE" && break
      test "$attempt" -lt 5 || exit 1
      sleep "$attempt"
    done
    ;;
  systemd)
    # Edit the staged unit or drop-in before this point so it contains either
    # Environment=PURSERS_CENTRAL_PYTHON=$CENTRAL_VENV/bin/python with a
    # profile-aware launcher, or a direct ExecStart using that interpreter.
    grep -F "$CENTRAL_VENV/bin/python" "$CENTRAL_SERVICE_FILE"
    grep -F "$CENTRAL_PROFILE" "$CENTRAL_SERVICE_FILE"
    systemd-analyze verify "$CENTRAL_SERVICE_FILE"
    systemctl --user daemon-reload
    systemctl --user restart "$CENTRAL_SERVICE_NAME"
    ;;
  *) echo "CENTRAL_SERVICE_KIND must be launchd or systemd" >&2; exit 64 ;;
esac
curl --fail --silent --show-error "$CENTRAL_HEALTH_URL" > "$ROLLOUT_STATE/central-health.json"
if test "$CENTRAL_SERVICE_KIND" = systemd; then
  CENTRAL_PID=$(systemctl --user show "$CENTRAL_SERVICE_NAME" --property MainPID --value)
  test "$(tr '\0' '\n' < "/proc/$CENTRAL_PID/cmdline" | head -1)" = \
    "$CENTRAL_VENV/bin/python"
fi
python3 - "$ROLLOUT_STATE/central-health.json" "$CENTRAL_VERSION" <<'PY'
import json, sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
actual = payload.get("central_version", payload.get("version"))
if payload.get("status") != "ok" or actual != sys.argv[2]:
    raise SystemExit(f"Central health/version mismatch: {payload!r}")
PY
"$CENTRAL_VENV/bin/python" -c \
  'import importlib.metadata as m,sys; assert m.version("pursers-central") == sys.argv[1]' \
  "$CENTRAL_VERSION"
```

Rollback restores the saved definition and previous environment reference,
then restarts only Central. Do not restore the database unless a reviewed
schema/data rollback is explicitly required; retain both SQLite snapshots.

## 4. Fleet launch contract and Board Butler

Before restarting Fleet Dashboard, require these variables in its launchd or
systemd environment. Add missing values to a backed-up service definition;
state and provider secrets must remain outside the checkout.

```text
PURSERS_BUTLER_STATE_DIR=/PATH/TO/private/board-butler-state
PURSERS_BUTLER_ENTRYPOINT=/PATH/TO/FleetPursers/tools/board-butler/board_butler.py
PURSERS_BUTLER_PROVIDER_SECRETS_DIR=/PATH/TO/private/board-butler-provider-secrets
```

Run `tools/rollout_doctor.py --inventory-only` after staging the definition.
Its Fleet inventory must report `fleet_environment.ok=true`. A partial Fleet
contract is a hard stop.

Board Butler has two valid branches:

1. If all executor socket/key settings exist, require a post-restart refresh
   with `fleet.status=reconciled`.
2. If the host has no fleet executor, upgrade the Butler runtime but do not
   invent socket/key values. Record
   `deviations.fleet_executor={"status":"not_provisioned","recorded":true}`
   in the private proof and require the product-produced refresh to report
   `fleet.status=disabled`. The doctor reports this branch as `WARN`, not a
   failed rollout. A partial executor configuration remains a hard failure.

An older configuration that predates `TK-ee3d61fd` may also lack approved-merge
authority. Record
`deviations.approved_merge={"status":"not_granted","recorded":true}`; the
doctor reports that exact case as `WARN`. Missing authority without the
recorded reason remains `FAIL`. An IDE that is not installed is likewise a
non-blocking `WARN`; an installed IDE whose configuration omits the wait bridge
remains `FAIL`.

On launchd, `bootout` followed immediately by `bootstrap` can return
`Bad request`. Use a bounded retry and stop after five attempts:

```sh
launchctl bootout "gui/$(id -u)/com.pursers.board-butler" || true
for attempt in 1 2 3 4 5; do
  launchctl bootstrap "gui/$(id -u)" "$BUTLER_PLIST" && break
  test "$attempt" -lt 5 || exit 1
  sleep "$attempt"
done
launchctl print "gui/$(id -u)/com.pursers.board-butler"
```

## 5. Verify and retain rollback inputs

Run the release-specific doctor in verify mode. Every `FAIL` blocks the
rollout; `WARN` is permitted only for the explicitly recorded
executor-not-provisioned branch or inapplicable desktop MCP hosts on a systemd
server. The doctor selects launchd on macOS or systemd user services on Linux
and checks Central's interpreter as part of the service inventory. Retain the
before/after inventories, source
hash proof, database snapshots, service-definition backups, exact commands,
and health responses in the private rollout record.

Do not delete old environments or snapshots during the rollout. If a service
check fails, restore that service's complete previous definition and package
set before touching the next service.
