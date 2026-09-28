# Pursers v5.0.6 local runtime rollout

This runbook moves the locally installed Pursers consumers from v5.0.5 (or a
pre-release checkout) to the single approved v5.0.6 commit. It is deliberately
staged and reversible. Keep the old wheel directory, runtime directories, and
checkout SHAs until post-rollout verification has passed.

Only the operator may perform this rollout. The commands below download,
install, switch checkouts, and restart local services. Workers and reviewers
must not run them. Do not push `main`, create or move a tag, publish a package,
delete a runtime, or edit a launchd job while following this runbook.

## 1. Establish the approved release

Start in a clean clone of the Pursers repository. Replace every placeholder
before running a command. Committed files and captured public evidence must
retain placeholders such as `/PATH/TO/...`; never commit a home directory,
hostname, token path, or other personal identifier.

```sh
set -eu

export RELEASE_TAG=v5.0.6
export RELEASE_SHA=<APPROVED_FULL_40_HEX_SHA>
export PURSERS_HOME=/PATH/TO/.pursers
export RELEASE_DOWNLOAD=/PATH/TO/downloaded-v5.0.6
export NEW_WHEEL_DIR="$PURSERS_HOME/central/.private-arm/wheels/v5.0.6"
export OLD_WHEEL_DIR="$PURSERS_HOME/central/.private-arm/wheels/v5.0.5"
export FLEET_REPO="$PURSERS_HOME/runtimes/fleet-dashboard/repo"
export COORDINATOR_REPO="$PURSERS_HOME/coordinator/src"
export ROLLOUT_STATE="$PURSERS_HOME/rollout/v5.0.6"
export RELEASE_SHORT="$(printf %.8s "$RELEASE_SHA")"
export NEW_REGISTRY_RUNTIME="$PURSERS_HOME/runtimes/registry-main-$RELEASE_SHORT"

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

The manifest at `RELEASE_SHA` must say product `5.0.6`, wait bridge `0.1.3`,
and the approved package versions. Do not infer a missing version from an old
checkout.

## 2. Capture the baseline inventory

The doctor only reads files, Git state, launchd state, and the process list. Its
inventory mode deliberately does not require post-rollout proof files.

```sh
python3 tools/rollout_doctor.py \
  --inventory-only \
  --release-sha "$RELEASE_SHA" \
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

Record the current SHAs before any checkout switch. These are rollback inputs,
not guessed values.

```sh
install -d -m 700 "$ROLLOUT_STATE"
git -C "$FLEET_REPO" rev-parse --verify 'HEAD^{commit}' \
  > "$ROLLOUT_STATE/fleet.previous-sha"
git -C "$COORDINATOR_REPO" rev-parse --verify 'HEAD^{commit}' \
  > "$ROLLOUT_STATE/coordinator.previous-sha"
test -z "$(git -C "$FLEET_REPO" status --porcelain --untracked-files=all)"
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
RELEASE_CHECKOUT=/PATH/TO/clean-v5.0.6-checkout
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
(cd "$NEW_WHEEL_DIR" && shasum -a 256 -c SHA256SUMS.txt)
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

## 6. Refresh the service checkouts

Both service clones must be clean. Fetch the tag, prove its exact SHA, and
switch to a detached checkout. Do not pull or merge an unbounded branch tip.

```sh
for repo in "$FLEET_REPO" "$COORDINATOR_REPO"; do
  test -z "$(git -C "$repo" status --porcelain --untracked-files=all)"
  git -C "$repo" fetch origin "refs/tags/$RELEASE_TAG:refs/tags/$RELEASE_TAG"
  test "$(git -C "$repo" rev-parse --verify "$RELEASE_TAG^{commit}")" = "$RELEASE_SHA"
  git -C "$repo" checkout --detach "$RELEASE_SHA"
  test "$(git -C "$repo" rev-parse --verify 'HEAD^{commit}')" = "$RELEASE_SHA"
done
```

If a service cannot import from its existing environment at the new checkout,
install the manifest-approved wheels into that service's existing environment
from `NEW_WHEEL_DIR`, using `--no-index --find-links`. Never resolve a release
runtime from the public package index.

Rollback a checkout with its recorded full SHA:

```sh
git -C "$FLEET_REPO" checkout --detach "$(cat "$ROLLOUT_STATE/fleet.previous-sha")"
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
fresh `last_event_at`, plus normal ticket dispatch. A loaded process alone is
not sufficient.

Rollback: check out `coordinator.previous-sha`, restore its previous runtime
packages if required, kickstart only the coordinator, and recheck the digest.

### Board Butler

Board Butler shares `FLEET_REPO`; do not move the checkout again. Preserve its
authorization, token, state, and kill-file paths.

```sh
launchctl kickstart -k "gui/$(id -u)/com.pursers.board-butler"
launchctl print "gui/$(id -u)/com.pursers.board-butler"
```

Success requires its effective state to remain `autonomous` and its runtime to
advertise the landed approved-review merge capability from `TK-ee3d61fd`.
Rollback by restoring `fleet.previous-sha`, restoring compatible packages, and
restarting Board Butler; then also restart and recheck Fleet Dashboard because
the two jobs share the checkout.

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

## 8. Collect post-rollout proof and run the doctor

Store private proof under `<PURSERS_HOME>/rollout/v5.0.6/proofs`; do not commit
it. The operator's evidence collector must produce these JSON files:

| File | Required fields |
| --- | --- |
| `coordinator-digest.json` | `connected: true`, fresh ISO-8601 `last_event_at` |
| `wait-worker.json` | `mode: "push"`, or non-empty `mode_by_board` whose values are all `push` |
| `wait-reviewer.json` | same push evidence for a reviewer |
| `board-butler.json` | `effective_state: "autonomous"`; `capabilities` includes `approved_merge`, `pr-review-merge`, or `TK-ee3d61fd` |
| `dashboard.json` | exact `release_sha`; `visual_shell: "warm-guided-home-v1"` |
| `release-checks.json` | exact `release_sha`; `ci_manifest: "pass"`; `release_train: "pass"` |

The proof must come from product-produced responses or observed runtime state.
Do not construct expected values and present them as observations.

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
