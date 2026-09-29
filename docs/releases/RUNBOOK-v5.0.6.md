# Pursers v5.0.6 local runtime rollout

This runbook moves the locally installed Pursers consumers from v5.0.5 (or a
pre-release checkout) to the single approved v5.0.6 commit. It is deliberately
staged and reversible. Keep the old wheel directory, runtime directories, and
checkout SHAs until post-rollout verification has passed.

Only the operator may perform this rollout. The commands below download,
install, switch checkouts, and restart local services. Workers and reviewers
must not run them. Do not push `main`, create or move a tag, publish a package,
or delete a runtime while following this runbook. The only launchd definition
change is the explicit, backed-up Board Butler runtime switch in section 7.

## Errata recorded after the live rollout

The live v5.0.6 rollout exposed eight defects in the original procedure below.
The original numbered steps remain as the historical record; the corrected,
version-agnostic procedure is
[`RUNBOOK-template.md`](RUNBOOK-template.md) and supersedes the affected
commands:

1. A seven-wheel release directory cannot satisfy third-party dependencies.
   Exact-pin every Pursers distribution from `--find-links`, leave the package
   index enabled for third-party dependencies, optionally constrain them from
   an approved environment, and hash-compare installed Pursers sources with
   the verified wheel members.
2. Upgrade each service interpreter's complete installed Pursers set. Central
   requires its own side-by-side venv, artifact/source-hash profile, SQLite
   `.backup` smoke test on an alternate port through
   `serve_tls.build_app(data_dir_override=...)`, pre-cutover DB snapshot,
   launchd/systemd repoint, `/healthz` and version checks, and exact rollback.
3. Before restarting Fleet Dashboard, add and validate
   `PURSERS_BUTLER_STATE_DIR`, `PURSERS_BUTLER_ENTRYPOINT`, and
   `PURSERS_BUTLER_PROVIDER_SECRETS_DIR`; keep private directories outside the
   checkout.
4. A host without a fleet executor may upgrade Butler and record
   `fleet.status=disabled` as the expected deviation. Never fabricate executor
   settings. Partial settings still fail closed. Use bounded retries when a
   launchd `bootout` followed by `bootstrap` returns `Bad request`.
5. The real release checksum file prefixes names with `./` and includes the
   AionUi and Home assets. Wheel-only staging must filter wheel rows when
   invoking `shasum`; the doctor accepts the prefix, ignores non-wheel rows,
   and still requires every staged wheel to be listed and match.
6. The permanent Butler conflict fix is the bounded `StateWriteConflict` retry,
   not the temporary hotfix strings. The doctor accepts that implementation
   only when the reviewed fix commit is an ancestor of the running checkout.
7. `/api/version` exposes the running SHA, not a visual-shell identifier.
   Dashboard evidence binds the real release SHA and never fabricates a shell
   field.
8. Coordinator freshness comes from collector `observed_at`/`heartbeat_at`,
   not the last board event. A quiet board is not a stale subscription.

### Linux/systemd corrections from the Azure rollout

The live Linux rollout now keeps persistent data in the version-neutral
`/PATH/TO/services/pursers/{central,config,credentials,state,bin,backups}` tree.
Each release lives side-by-side at
`/PATH/TO/services/pursers-vX.Y.Z/{venv,release-assets,source}`, where `source`
is the tag checkout and no longer lives under `projects/`. Keep the operator
project `work_dir` at `/PATH/TO/projects/pursers`, logs at
`/PATH/TO/logs/pursers`, and `TMPDIR` at `/PATH/TO/cache/pursers`. An upgrade
repoints only the unit `ExecStart` venv/source paths. Before upgrading, archive
the pre-upgrade units and database together as a tarball under
`/PATH/TO/services/pursers/backups/`.

That host had neither `uv` nor `gh`. Use an operator-approved asset transfer,
filter `SHA256SUMS.txt` to wheel rows, then create the new environment with
`python3 -m venv`. Freeze the old venv and remove all old `pursers*` and `mcp`
rows—including extras, editable URLs, and PEP 508 direct references such as
`pursers @ file:///...` and `mcp @ file:///...`—before using it as a
constraint. Install every Pursers distribution at the exact v5.0.6 version
with `python -m pip install --find-links`, and require `python -m pip check`.
Reuse that same side-by-side venv for Central; the Linux route must not invoke
`uv` later. Do not use the old MCP pin to constrain the release.
This includes pip's two-line local-editable form: remove a Pursers/MCP
`# Editable install with no version control (NAME==VERSION)` plus following
`-e /PATH/TO/...` pair, and fail closed on every other editable until the
operator replaces it with an immutable constraint.
The reusable template contains the executable commands and complete cohort
guard.

For the Central smoke test, copy the live Central directory into private
rollout state, create the candidate database with SQLite `.backup`, and rewrite
the copy's `profile.env` values for `ONBOARD_CENTRAL_PORT`,
`CENTRAL_JWT_AUDIENCE`, and `ONBOARD_CENTRAL_DATA_DIR`. Start only the copy with
`"$CENTRAL_VENV/bin/pursers-central" run "$CENTRAL_SMOKE_PROFILE"`; give that
single process one PID, one bounded health/version probe, and one cleanup. Do
not also start the host `serve_tls.py` adapter on the same port. Never point the
candidate at the live profile or database.

The `pursers-butler` and `pursers-fleet-executor` units use
`StartLimitBurst=1` and `Requires=pursers-central`. Restarting Central can stop
and auto-start both dependents, consuming their single start allowance. After
repointing the units, do not repeatedly restart them. Run exactly one
`reset-failed` followed by one `start` for each unit, in this order:

```sh
systemctl --user reset-failed pursers-central
systemctl --user start pursers-central
systemctl --user reset-failed pursers-fleet-executor
systemctl --user start pursers-fleet-executor
systemctl --user reset-failed pursers-butler
systemctl --user start pursers-butler
```

Verify each unit is active before advancing to the next one. The coordinator's
live result was all three units active on the v5.0.6 venv/source, with Butler
reporting client `0.1.5` and `events-reconnect` without a traceback.

Run the updated doctor before and after the rollout. It inventories inconsistent
Pursers pins, the three Fleet variables, and whether the executor is fully
provisioned, absent, or partially configured. It reads launchd on macOS and
systemd user-service state on Linux, including the Central interpreter.

## 1. Establish the approved release

Start in a clean clone of the Pursers repository. Replace every placeholder
before running a command. Committed files and captured public evidence must
retain placeholders such as `/PATH/TO/...`; never commit a home directory,
hostname, token path, or other personal identifier.

```sh
set -eu

export RELEASE_TAG=v5.0.6
: "${RELEASE_SHA:?set the operator-approved full 40-hex release SHA}"
export PURSERS_HOME=/PATH/TO/.pursers
export RELEASE_DOWNLOAD=/PATH/TO/downloaded-v5.0.6
export NEW_WHEEL_DIR="$PURSERS_HOME/central/.private-arm/wheels/v5.0.6"
export OLD_WHEEL_DIR="$PURSERS_HOME/central/.private-arm/wheels/v5.0.5"
export FLEET_REPO="$PURSERS_HOME/runtimes/fleet-dashboard/repo"
export COORDINATOR_REPO="$PURSERS_HOME/coordinator/src"
export RELEASE_CHECKOUT=/PATH/TO/clean-v5.0.6-checkout
export FLEET_PYTHON=/PATH/TO/fleet-dashboard-python
export COORDINATOR_PYTHON=/PATH/TO/coordinator-python
export FLEET_PLIST=/PATH/TO/Library/LaunchAgents/com.pursers.fleet-dashboard.plist
export BUTLER_PLIST=/PATH/TO/Library/LaunchAgents/com.pursers.board-butler.plist
export BUTLER_RUNTIME_STATUS=/PATH/TO/private/board-butler-state/runtime.json
export BUTLER_LOG=/PATH/TO/private/logs/board-butler.log
export BUTLER_FIX_SHA=${BUTLER_FIX_SHA:-ee5c9e436ce35fd906c0ac943559482046e9186a}
export BUTLER_HOTFIX_BACKUP=board_butler.py.bak-hotfix-precondition-20260928163252
export ROLLOUT_STATE="$PURSERS_HOME/rollout/v5.0.6"
export RELEASE_SHORT="$(printf %.8s "$RELEASE_SHA")"
export NEW_REGISTRY_RUNTIME="$PURSERS_HOME/runtimes/registry-main-$RELEASE_SHORT"
export BUTLER_PYTHON="$NEW_REGISTRY_RUNTIME/.venv/bin/python"

case "$RELEASE_SHA" in
  *[!0-9a-f]*|'') echo "RELEASE_SHA must be lowercase hex" >&2; exit 64 ;;
esac
test "${#RELEASE_SHA}" -eq 40
test "$PURSERS_HOME" != /PATH/TO/.pursers
test ! -e "$RELEASE_DOWNLOAD"
test ! -e "$NEW_WHEEL_DIR"
test ! -e "$NEW_REGISTRY_RUNTIME"
```

Fetch without changing the checkout, then prove that the immutable tag peels
to the coordinator-approved commit. Stop on any mismatch.

```sh
git fetch --tags origin "refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"
test "$(git rev-parse --verify "$RELEASE_TAG^{commit}")" = "$RELEASE_SHA"
test "$(git rev-parse --verify "$RELEASE_SHA^{commit}")" = "$RELEASE_SHA"
git merge-base --is-ancestor "$RELEASE_SHA" refs/remotes/origin/main
git show "$RELEASE_SHA:tools/release_versions.toml"
```

The manifest at `RELEASE_SHA` must match the default approved map: product
`5.0.6`, client `0.1.5`, wait bridge `0.1.3`, central `0.1.4`, ACP `0.1.4`,
and import `5.0.0`. A changed map fails verification; do not trust prose or an
old checkout over `tools/release_versions.toml` at the approved SHA.

## 2. Capture the baseline inventory

The doctor only reads files, Git state, launchd state, and the process list. Its
inventory mode deliberately does not require post-rollout proof files.

```sh
python3 tools/rollout_doctor.py \
  --inventory-only \
  --release-sha "$RELEASE_SHA" \
  --release-tag "$RELEASE_TAG" \
  --wheel-dir "$OLD_WHEEL_DIR" \
  --sha256s "$OLD_WHEEL_DIR/SHA256SUMS.txt" \
  --pursers-home "$PURSERS_HOME" \
  > /PATH/TO/v5.0.6-before.json
```

Review the inventory for all of these consumers before proceeding:

- the `pursers-wait-bridge` uv tool, including its wait-bridge and client
  versions and the release named by `find-links`;
- every `registry-main-*` and `review-*` runtime under
  `<PURSERS_HOME>/runtimes`;
- `<PURSERS_HOME>/runtimes/fleet-dashboard/repo`, which serves both Fleet
  Dashboard and Board Butler;
- `<PURSERS_HOME>/coordinator/src`;
- `com.pursers.fleet-dashboard`, `com.pursers.coordinator`,
  `com.pursers.board-butler`, and `com.pursers.mong1-supervisor`;
- Claude Desktop and Zed configurations that launch `pursers-wait-bridge`.

The expected baseline uv tool is wait bridge `0.1.2`, client `0.1.4`, with
`find-links` pointing to the v5.0.5 wheel directory. A different baseline is
not automatically wrong, but it must be explained in the rollout record.

The live Fleet/Butler clone is intentionally dirty while the TK-164fb22b
precondition-conflict stopgap is installed. The inventory must show both
`clean: false` and the exact two `dirty_paths` below. Capture the patch and
backup before any checkout operation. Stop if any other path is dirty.

Record the current SHAs before any checkout switch. These are rollback inputs,
not guessed values.

```sh
install -d -m 700 "$ROLLOUT_STATE"
git -C "$FLEET_REPO" rev-parse --verify 'HEAD^{commit}' \
  > "$ROLLOUT_STATE/fleet.previous-sha"
git -C "$COORDINATOR_REPO" rev-parse --verify 'HEAD^{commit}' \
  > "$ROLLOUT_STATE/coordinator.previous-sha"
FLEET_DIRTY=$(git -C "$FLEET_REPO" status --porcelain --untracked-files=all)
EXPECTED_FLEET_DIRTY=$(printf '%s\n%s' \
  ' M tools/board-butler/board_butler.py' \
  "?? tools/board-butler/$BUTLER_HOTFIX_BACKUP")
test "$FLEET_DIRTY" = "$EXPECTED_FLEET_DIRTY"
grep -F 'TK-164fb22b' "$FLEET_REPO/tools/board-butler/board_butler.py"
git -C "$FLEET_REPO" diff --binary -- tools/board-butler/board_butler.py \
  > "$ROLLOUT_STATE/TK-164fb22b.patch"
test -s "$ROLLOUT_STATE/TK-164fb22b.patch"
cp -p "$FLEET_REPO/tools/board-butler/$BUTLER_HOTFIX_BACKUP" \
  "$ROLLOUT_STATE/$BUTLER_HOTFIX_BACKUP"
shasum -a 256 "$ROLLOUT_STATE/TK-164fb22b.patch" \
  "$ROLLOUT_STATE/$BUTLER_HOTFIX_BACKUP" \
  > "$ROLLOUT_STATE/TK-164fb22b.SHA256SUMS"
test -z "$(git -C "$COORDINATOR_REPO" status --porcelain --untracked-files=all)"
```

## 3. Download, verify, and stage the release wheels

Download the release to a new directory. `SHA256SUMS.txt` comes from the same
GitHub release as the wheels, and the release tag has already been bound to
`RELEASE_SHA` above.

```sh
mkdir -m 700 "$RELEASE_DOWNLOAD"
gh release download "$RELEASE_TAG" --repo swisspra/Pursers \
  --dir "$RELEASE_DOWNLOAD" \
  --pattern '*.whl' \
  --pattern SHA256SUMS.txt
(cd "$RELEASE_DOWNLOAD" && shasum -a 256 -c SHA256SUMS.txt)
```

Use the release checkout itself to verify that the downloaded wheel cohort has
the seven exact manifest-derived filenames. Extra or missing wheels fail the
rollout.

```sh
test "$(git -C "$RELEASE_CHECKOUT" rev-parse --verify 'HEAD^{commit}')" = "$RELEASE_SHA"
python3 - "$RELEASE_CHECKOUT" "$RELEASE_DOWNLOAD" <<'PY'
import sys
from pathlib import Path

checkout = Path(sys.argv[1])
download = Path(sys.argv[2])
sys.path.insert(0, str(checkout))
from tools.release_versions import expected_wheel_filenames

actual = {path.name for path in download.glob("*.whl")}
expected = set(expected_wheel_filenames())
if actual != expected:
    raise SystemExit(f"wheel cohort mismatch: actual={sorted(actual)!r}, expected={sorted(expected)!r}")
print("verified wheels:", ", ".join(sorted(actual)))
PY
```

Stage the already verified bytes without overwriting the v5.0.5 directory.

```sh
install -d -m 700 "$NEW_WHEEL_DIR"
cp "$RELEASE_DOWNLOAD"/*.whl "$RELEASE_DOWNLOAD/SHA256SUMS.txt" "$NEW_WHEEL_DIR/"
grep -E '  (\./)?[^/]+\.whl$' "$NEW_WHEEL_DIR/SHA256SUMS.txt" |
  (cd "$NEW_WHEEL_DIR" && shasum -a 256 -c -)
test -d "$OLD_WHEEL_DIR"
```

Rollback before installation: leave the new directory in place for diagnosis;
no consumer points at it yet.

## 4. Upgrade the host wait bridge

Precondition: the checksum and cohort checks above passed, and the v5.0.5 wheel
directory still exists.

```sh
uv tool install --force \
  --find-links "$NEW_WHEEL_DIR" \
  'pursers-wait-bridge==0.1.3'
"$HOME/.local/share/uv/tools/pursers-wait-bridge/bin/python" - <<'PY'
from importlib.metadata import version
assert version("pursers-wait-bridge") == "0.1.3"
assert version("pursers-client") == "0.1.5"
print("wait bridge 0.1.3; client 0.1.5")
PY
```

If either check fails, restore the previous tool immediately:

```sh
uv tool install --force \
  --find-links "$OLD_WHEEL_DIR" \
  'pursers-wait-bridge==0.1.2'
```

Quit and reopen Claude Desktop and Zed after a successful tool install so each
host starts the new bridge process. Their MCP configuration paths should not
change. Confirm in each host that a saved positive-cursor wait reaches push
mode; do not reset a cursor to zero to manufacture a test.

## 5. Build the new registry runtime

Create a new runtime alongside the old one. Do not repurpose or overwrite an
existing runtime directory.

```sh
install -d -m 700 "$NEW_REGISTRY_RUNTIME"
git clone --no-checkout /PATH/TO/PURSERS-REMOTE "$NEW_REGISTRY_RUNTIME/src"
git -C "$NEW_REGISTRY_RUNTIME/src" fetch origin \
  "refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"
test "$(git -C "$NEW_REGISTRY_RUNTIME/src" rev-parse --verify "$RELEASE_TAG^{commit}")" = "$RELEASE_SHA"
git -C "$NEW_REGISTRY_RUNTIME/src" checkout --detach "$RELEASE_SHA"
uv venv --python /PATH/TO/python3.12 "$NEW_REGISTRY_RUNTIME/.venv"
uv pip install \
  --python "$NEW_REGISTRY_RUNTIME/.venv/bin/python" \
  --no-index --find-links "$NEW_WHEEL_DIR" \
  pursers-central pursers-client pursers-wait-bridge
"$NEW_REGISTRY_RUNTIME/.venv/bin/python" -m pip check
"$NEW_REGISTRY_RUNTIME/.venv/bin/python" - <<'PY'
from importlib.metadata import version
assert version("pursers-wait-bridge") == "0.1.3"
assert version("pursers-client") == "0.1.5"
print("registry runtime imports and versions passed")
PY
```

Update only the operator-controlled registry runtime reference after those
checks pass. Capture the old reference verbatim first. The exact configuration
location is host-owned and must not be copied into this repository.

Rollback: restore that captured reference to the previous runtime. Keep both
runtime directories; do not delete either one during the rollout.

## 6. Refresh the service checkouts without losing the Butler hotfix

The coordinator clone must be clean. The Fleet/Butler clone must have exactly
the captured TK-164fb22b dirt and no other change. Fetch the tag without
touching either worktree, then prove its exact SHA. Do not pull or merge an
unbounded branch tip.

```sh
for repo in "$FLEET_REPO" "$COORDINATOR_REPO"; do
  git -C "$repo" fetch origin "refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"
  test "$(git -C "$repo" rev-parse --verify "$RELEASE_TAG^{commit}")" = "$RELEASE_SHA"
done
test -z "$(git -C "$COORDINATOR_REPO" status --porcelain --untracked-files=all)"
git -C "$COORDINATOR_REPO" checkout --detach "$RELEASE_SHA"
test "$(git -C "$COORDINATOR_REPO" rev-parse --verify 'HEAD^{commit}')" = "$RELEASE_SHA"
```

Decide whether the release contains the reviewed permanent TK-164fb22b fix.
The coordinator-approved source commit is
`ee5c9e436ce35fd906c0ac943559482046e9186a` (landed on main at `1f2f3895`;
the Board Butler suite passed 268 tests). `BUTLER_FIX_SHA` defaults to that
commit; use the literal `NONE` only under a later coordinator decision. A
matching comment or ticket string is not proof of ancestry.

```sh
RELEASE_HAS_BUTLER_FIX=false
if test "$BUTLER_FIX_SHA" != NONE; then
  case "$BUTLER_FIX_SHA" in
    *[!0-9a-f]*|'') echo "BUTLER_FIX_SHA must be full lowercase hex or NONE" >&2; exit 64 ;;
  esac
  test "${#BUTLER_FIX_SHA}" -eq 40
  if git -C "$FLEET_REPO" merge-base --is-ancestor "$BUTLER_FIX_SHA" "$RELEASE_SHA"; then
    RELEASE_HAS_BUTLER_FIX=true
  fi
fi
```

Preserve the exact dirty state in Git's object store before switching. This is
not permission to reset arbitrary dirt: the exact-path check in section 2 must
have passed. Record the immutable stash commit for rollback.

```sh
git -C "$FLEET_REPO" stash push --include-untracked \
  -m 'v5.0.6 rollout preserve TK-164fb22b' -- \
  tools/board-butler/board_butler.py \
  "tools/board-butler/$BUTLER_HOTFIX_BACKUP"
git -C "$FLEET_REPO" rev-parse --verify refs/stash \
  > "$ROLLOUT_STATE/fleet.hotfix-stash-sha"
test -z "$(git -C "$FLEET_REPO" status --porcelain --untracked-files=all)"
git -C "$FLEET_REPO" checkout --detach "$RELEASE_SHA"
test "$(git -C "$FLEET_REPO" rev-parse --verify 'HEAD^{commit}')" = "$RELEASE_SHA"
```

If the approved fix is in the tag, keep the release checkout clean. Otherwise,
explicitly carry forward only the captured patch. A patch that no longer
applies cleanly is a hard stop requiring a reviewed port; never discard it or
force a checkout.

```sh
if test "$RELEASE_HAS_BUTLER_FIX" = true; then
  test -z "$(git -C "$FLEET_REPO" status --porcelain --untracked-files=all)"
else
  git -C "$FLEET_REPO" apply --check "$ROLLOUT_STATE/TK-164fb22b.patch"
  git -C "$FLEET_REPO" apply "$ROLLOUT_STATE/TK-164fb22b.patch"
  test "$(git -C "$FLEET_REPO" status --porcelain --untracked-files=all)" = \
    ' M tools/board-butler/board_butler.py'
  grep -F 'TK-164fb22b' "$FLEET_REPO/tools/board-butler/board_butler.py"
  git -C "$FLEET_REPO" diff --binary --no-ext-diff | shasum -a 256 | \
    awk '{print $1}' > "$ROLLOUT_STATE/fleet.carried-hotfix.sha256"
fi
```

The service source SHA and its Python environment are separate rollout inputs.
The v5.0.5 Board Butler can run the Fleet checkout from the registry runtime
environment, so changing only the clone leaves it on client `0.1.4`. Read the
approved version from the release checkout and upgrade every distinct service
interpreter from `NEW_WHEEL_DIR`; do not resolve a release runtime from the
public package index.

```sh
CLIENT_VERSION=$(python3 -c \
  'import sys,tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["packages"]["client"])' \
  "$RELEASE_CHECKOUT/tools/release_versions.toml")

for python in "$FLEET_PYTHON" "$COORDINATOR_PYTHON" "$BUTLER_PYTHON"; do
  test -x "$python"
  uv pip install --python "$python" --no-index --find-links "$NEW_WHEEL_DIR" \
    "pursers-client==$CLIENT_VERSION"
  "$python" -c \
    'import importlib.metadata as m,sys; assert m.version("pursers-client") == sys.argv[1]' \
    "$CLIENT_VERSION"
done
```

If two services share one interpreter, the repeated exact install is harmless
and must produce the same version check. Rollback requires reinstalling each
interpreter's recorded v5.0.5 package set from `OLD_WHEEL_DIR`, not merely
moving the Git checkout back.

Rollback a checkout with its recorded full SHA:

```sh
if test -f "$ROLLOUT_STATE/fleet.carried-hotfix.sha256"; then
  git -C "$FLEET_REPO" apply --reverse --check "$ROLLOUT_STATE/TK-164fb22b.patch"
  git -C "$FLEET_REPO" apply --reverse "$ROLLOUT_STATE/TK-164fb22b.patch"
fi
git -C "$FLEET_REPO" checkout --detach "$(cat "$ROLLOUT_STATE/fleet.previous-sha")"
git -C "$FLEET_REPO" stash apply \
  "$(cat "$ROLLOUT_STATE/fleet.hotfix-stash-sha")"
test "$(git -C "$FLEET_REPO" status --porcelain --untracked-files=all)" = \
  "$EXPECTED_FLEET_DIRTY"
git -C "$COORDINATOR_REPO" checkout --detach "$(cat "$ROLLOUT_STATE/coordinator.previous-sha")"
```

## 7. Restart one service at a time

Never restart all jobs together. For each job: confirm the precondition, restart
only that job, run its success check, and roll it back before touching the next
job if the check fails.

### Fleet Dashboard

```sh
launchctl kickstart -k "gui/$(id -u)/com.pursers.fleet-dashboard"
launchctl print "gui/$(id -u)/com.pursers.fleet-dashboard"
curl --fail --silent http://127.0.0.1:8899/api/version
```

The response must identify `RELEASE_SHA`. Load the dashboard in a browser and
confirm the v5.0.6 warm guided-home visual shell, not merely an HTTP 200.

Rollback: check out `fleet.previous-sha`, reinstall that checkout's package
versions if required, kickstart only Fleet Dashboard, then repeat the version
and visual checks.

### Coordinator

```sh
launchctl kickstart -k "gui/$(id -u)/com.pursers.coordinator"
launchctl print "gui/$(id -u)/com.pursers.coordinator"
```

Success requires a registry digest subscription with `connected=true` and a
fresh collector `observed_at` or `heartbeat_at`, plus normal ticket dispatch.
`last_event_at` is board activity and may legitimately be old on a quiet board;
it is not a connection-freshness signal. A loaded process alone is not
sufficient.

Rollback: check out `coordinator.previous-sha`, restore its previous runtime
packages if required, kickstart only the coordinator, and recheck the digest.

### Board Butler

Board Butler shares `FLEET_REPO`; do not move the checkout again. Preserve its
authorization, token, state, and kill-file paths.

Before restarting it, prove the installed launchd definition enables the full
fleet path. `active` mode, the active authorization, the fleet observation and
state files, and all three executor settings are one atomic precondition. The
v5.0.6 launch wrapper must pass all of those settings to `board_butler.py`.
The Fleet launch template is also the deployment contract for the resident:
its explicit `PURSERS_BUTLER_ENTRYPOINT` must name the selected checkout, and
its shared provider-secret directory must remain outside that checkout.
Back up the definition, then move Board Butler off the v5.0.5 registry venv and
onto the new side-by-side registry runtime. Do not edit its credential paths or
fleet authorization values.

```sh
test ! -e "$ROLLOUT_STATE/board-butler.plist.before-v5.0.6"
cp -p "$BUTLER_PLIST" "$ROLLOUT_STATE/board-butler.plist.before-v5.0.6"
/usr/libexec/PlistBuddy -c \
  "Set :EnvironmentVariables:PURSERS_BUTLER_PYTHON $BUTLER_PYTHON" \
  "$BUTLER_PLIST"
plutil -lint "$BUTLER_PLIST"
test "$(plutil -extract EnvironmentVariables.PURSERS_BUTLER_PYTHON raw \
  "$BUTLER_PLIST")" = "$BUTLER_PYTHON"
test "$(plutil -extract EnvironmentVariables.PURSERS_BUTLER_REPO raw \
  "$BUTLER_PLIST")" = "$FLEET_REPO"
test "$(plutil -extract EnvironmentVariables.PURSERS_BUTLER_ENTRYPOINT raw \
  "$FLEET_PLIST")" = "$FLEET_REPO/tools/board-butler/board_butler.py"
BUTLER_PROVIDER_SECRETS_DIR=$(plutil -extract \
  EnvironmentVariables.PURSERS_BUTLER_PROVIDER_SECRETS_DIR raw "$FLEET_PLIST")
case "$BUTLER_PROVIDER_SECRETS_DIR/" in
  "$FLEET_REPO/"*)
    echo "PURSERS_BUTLER_PROVIDER_SECRETS_DIR must be outside the checkout" >&2
    exit 64
    ;;
esac
test "$(plutil -extract EnvironmentVariables.PURSERS_BUTLER_RUNTIME_MODE raw \
  "$BUTLER_PLIST")" = active
for key in \
  PURSERS_BUTLER_ACTIVE_AUTHORIZATION_FILE \
  PURSERS_BUTLER_ACTIVE_BOARD \
  PURSERS_BUTLER_FLEET_OBSERVATION_FILE \
  PURSERS_BUTLER_FLEET_STATE_FILE \
  PURSERS_BUTLER_FLEET_EXECUTOR_SOCKET \
  PURSERS_BUTLER_FLEET_EXECUTOR_KEY_ID \
  PURSERS_BUTLER_FLEET_EXECUTOR_PRIVATE_KEY
do
  test -n "$(plutil -extract "EnvironmentVariables.$key" raw "$BUTLER_PLIST")"
done
for option in \
  --active-authorization-file --fleet-observation-file --fleet-state-file \
  --fleet-executor-socket --fleet-executor-key-id --fleet-executor-private-key
do
  grep -F -- "$option" "$FLEET_REPO/tools/board-butler/launch.sh"
done
```

Stop if any value or wrapper flag is missing. Restore the previous complete
plist/runtime tuple on rollback; never start an active Butler with a partial
executor configuration. The exact definition rollback is:

```sh
cp -p "$ROLLOUT_STATE/board-butler.plist.before-v5.0.6" "$BUTLER_PLIST"
plutil -lint "$BUTLER_PLIST"
```

```sh
BUTLER_LOG_OFFSET=$(wc -c < "$BUTLER_LOG")
launchctl bootout "gui/$(id -u)/com.pursers.board-butler"
launchctl bootstrap "gui/$(id -u)" "$BUTLER_PLIST"
launchctl print "gui/$(id -u)/com.pursers.board-butler"
python3 - "$BUTLER_RUNTIME_STATUS" "$BUTLER_LOG" "$BUTLER_LOG_OFFSET" <<'PY'
import json, sys, time
from pathlib import Path

runtime_path, log_path, offset = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
deadline = time.monotonic() + 120
last_fleet = None
while time.monotonic() < deadline:
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    if runtime.get("running") is not True or runtime.get("mode") != "active":
        raise SystemExit(f"board-butler runtime is not active: {runtime!r}")
    with log_path.open("r", encoding="utf-8") as handle:
        handle.seek(offset)
        lines = handle.readlines()
    for line in lines:
        marker = "board-butler: refresh "
        if marker not in line:
            continue
        observation = json.loads(line.split(marker, 1)[1])
        last_fleet = observation.get("fleet", {})
        if last_fleet.get("status") == "reconciled":
            print("board-butler fleet status=reconciled")
            raise SystemExit(0)
    time.sleep(2)
raise SystemExit(f"no new reconciled fleet refresh after restart: {last_fleet!r}")
PY
```

Success requires its effective state to remain `autonomous` and its runtime to
advertise the landed approved-review merge capability from `TK-ee3d61fd`.
A new refresh event after the saved log offset must report
`fleet.status=reconciled`; if the bounded wait sees only `disabled`, `shadow`,
a missing field, or no new refresh, the rollout failed even when
`butler_config_get` reports `effective_mode=autonomous`.
Rollback by restoring `fleet.previous-sha`, restoring compatible packages, and
restoring the saved plist, then reloading Board Butler so launchd uses the old
environment:

```sh
cp -p "$ROLLOUT_STATE/board-butler.plist.before-v5.0.6" "$BUTLER_PLIST"
launchctl bootout "gui/$(id -u)/com.pursers.board-butler"
launchctl bootstrap "gui/$(id -u)" "$BUTLER_PLIST"
launchctl print "gui/$(id -u)/com.pursers.board-butler"
```

Also restart and recheck Fleet Dashboard because the two jobs share the
checkout.

### Mong1 supervisor — drain first

Before touching `com.pursers.mong1-supervisor`, inspect every active WORK board
from the registry. A seat is drained only when it holds no claimed,
in-progress, or report-creating ticket and no lease renewal is due. Let claimed
seats finish and submit, or have them checkpoint and explicitly release through
the supported board operation. Renew active leases while waiting.

Never kill a claimed seat. Never infer idleness from a quiet terminal or an old
process timestamp.

Only after every Mong1 seat is board-idle:

```sh
launchctl kickstart -k "gui/$(id -u)/com.pursers.mong1-supervisor"
launchctl print "gui/$(id -u)/com.pursers.mong1-supervisor"
```

Success requires the expected worker and reviewer identities to reconnect with
their saved positive cursors and use push waits. If that fails, restore the
previous supervisor runtime reference and restart once more; do not reset
cursors or take over a seat identity.

### Cold-boot and power-loss recovery

An unexpected host loss abandons every in-flight claim. Do not resume the
rollout or assume that a pre-boot lease still exists. Bring Central and the
registry runtime up first, confirm every active WORK board is reachable, then
start the coordinator and inspect the board state. Preserve all seat worktrees
and checkpoints. Each worker/reviewer must reconnect with its own identity and
saved positive cursor, accept a newly issued offer, and reclaim through the
board before continuing. Never manufacture a claim, reset a cursor to zero, or
delete an abandoned worktree during recovery.

Start Board Butler only after its complete active/fleet preflight above passes.
A `board_state` compare-and-swap precondition conflict must be deferred while
the process stays alive, then replayed against fresh state. If Butler exits,
advances past the event without replay, or loops on stale state, the rollout
failed: retain logs/state, perform the exact rollback, and do not bypass the
precondition. Re-run the fleet `status=reconciled` check before the supervisor
or any remaining rollout step.

## 8. Collect post-rollout proof and run the doctor

Store private proof under `<PURSERS_HOME>/rollout/v5.0.6/proofs`; do not commit
it. The operator's evidence collector must produce these JSON files:

| File | Required fields |
| --- | --- |
| `coordinator-digest.json` | `connected: true`, fresh ISO-8601 `observed_at` or `heartbeat_at`; `last_event_at` is optional activity evidence only |
| `wait-worker.json` | `mode: "push"`, or non-empty `mode_by_board` whose values are all `push` |
| `wait-reviewer.json` | same push evidence for a reviewer |
| `board-butler.json` | `effective_state: "autonomous"`; approved-merge capability and `fleet.status: "reconciled"`, or an explicit recorded expected deviation for a pre-TK-ee3d61fd config / unprovisioned executor; `state_precondition_conflict` binds `TK-164fb22b` and the release SHA with `post_restart_refresh_seen: true`, `state_precondition_traceback: false`, and `evidence_kind: "live-runtime"`; when carried, `hotfix` binds the ticket, release SHA, `carried_forward: true`, and the doctor's worktree patch SHA-256 |
| `dashboard.json` | exact `release_sha` from the real `/api/version` response; do not fabricate a visual-shell identifier |
| `release-checks.json` | exact `release_sha`; `ci_manifest: "pass"`; `release_train: "pass"` |

The proof must come from product-produced responses or observed runtime state.
Do not construct expected values and present them as observations.

For conflict evidence, the doctor accepts the temporary
`_is_state_precondition_conflict` guards or the permanent `StateWriteConflict`
bounded retry only when `ee5c9e436ce35fd906c0ac943559482046e9186a` is an
ancestor of the running checkout.
Retain the post-restart refresh log and prove it contains no `state precondition
failed` traceback. If `RELEASE_HAS_BUTLER_FIX=true`, also run the permanent
fix's authoritative test file at the clean `RELEASE_SHA` checkout and record
the exact command/output alongside the approved fix SHA:

```sh
python3 -m pytest -q tools/board-butler/tests/test_board_butler.py
```

The approved `ee5c9e436ce35fd906c0ac943559482046e9186a` source evidence was
`268 passed`; the execution-time release checkout must independently pass.

At the clean `RELEASE_SHA` checkout, run the release gates:

```sh
python3 tools/release_train.py check
python3 tools/ci_manifest.py run
```

Record the exact commands, exit status, suite counts, and `RELEASE_SHA` in the
release-checks evidence. Then run verification mode:

```sh
python3 tools/rollout_doctor.py \
  --release-sha "$RELEASE_SHA" \
  --release-tag "$RELEASE_TAG" \
  --wheel-dir "$NEW_WHEEL_DIR" \
  --sha256s "$NEW_WHEEL_DIR/SHA256SUMS.txt" \
  --pursers-home "$PURSERS_HOME" \
  --proof-dir "$ROLLOUT_STATE/proofs" \
  > /PATH/TO/v5.0.6-after.json
```

The exit status must be zero, `summary.ok` must be true, and every reported
check must be `PASS`. Compare before and after inventories and retain both with
the private rollout record.

## 9. Stale runtime cleanup report

The doctor's `stale_runtime_cleanup` section is advisory. It marks
`safe_to_delete=true` only for a recognized registry/review runtime that is a
clean Git checkout, is not a symlink, is not referenced by launchd, a host MCP
configuration, or the process list, and is not at `RELEASE_SHA`.

List and review those entries after the rollout. This runbook authorizes no
deletion. A later cleanup change must name each exact runtime path and obtain
separate operator approval.

## 10. Rollback order

On any failed health check, stop the forward rollout and reverse only the
components already changed:

1. Restore the supervisor runtime reference, if changed, and restart it only
   after seats are drained.
2. Restore Board Butler and Fleet Dashboard to `fleet.previous-sha`, then
   restart and health-check each job separately.
3. Restore the coordinator to `coordinator.previous-sha`, restart it, and
   verify a fresh connected digest.
4. Restore the registry consumer's captured old runtime reference.
5. Reinstall wait bridge `0.1.2` from `OLD_WHEEL_DIR`, then restart Claude
   Desktop and Zed.
6. Run the doctor in inventory mode and attach the failure and rollback
   evidence to the incident record.

Do not delete the new checkout, wheel directory, or runtime while diagnosing a
rollback. Do not move the v5.0.6 tag or replace release assets; fix the source
and advance to a new release if immutable release bytes are wrong.
