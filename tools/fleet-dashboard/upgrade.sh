#!/bin/sh
# Move the dedicated Fleet checkout to an exact origin/main commit and restart it.
set -eu

: "${PURSERS_FLEET_REPO:?set PURSERS_FLEET_REPO}"
: "${PURSERS_FLEET_STATE_DIR:?set PURSERS_FLEET_STATE_DIR}"
: "${PURSERS_FLEET_PYTHON:?set PURSERS_FLEET_PYTHON}"

if [ "$#" -ne 1 ]; then
  echo "usage: PURSERS_FLEET_REPO=/PATH/TO/CLONE PURSERS_FLEET_STATE_DIR=/PATH/TO/STATE PURSERS_FLEET_PYTHON=/PATH/TO/PYTHON $0 <40-hex-origin-main-sha>" >&2
  exit 64
fi
target=$1
case "$target" in *[!0-9a-f]*|'') echo "target must be a lowercase full commit SHA" >&2; exit 64;; esac
[ "${#target}" -eq 40 ] || { echo "target must be a lowercase full commit SHA" >&2; exit 64; }

repo=$(git -C "$PURSERS_FLEET_REPO" rev-parse --show-toplevel)
state=$(mkdir -p "$PURSERS_FLEET_STATE_DIR" && cd "$PURSERS_FLEET_STATE_DIR" && pwd -P)
case "$state/" in "$repo/"*) echo "PURSERS_FLEET_STATE_DIR must be outside the checkout" >&2; exit 64;; esac

test -z "$(git -C "$repo" status --porcelain --untracked-files=all)" || {
  echo "Fleet checkout is dirty; refusing to replace it" >&2
  exit 65
}

job="gui/$(id -u)/${PURSERS_FLEET_LAUNCHD_LABEL:-com.pursers.fleet-dashboard}"
launchctl print "$job" >/dev/null
git -C "$repo" fetch --no-tags origin main
resolved=$(git -C "$repo" rev-parse --verify "$target^{commit}")
[ "$resolved" = "$target" ] || { echo "target did not resolve exactly" >&2; exit 65; }
git -C "$repo" merge-base --is-ancestor "$target" refs/remotes/origin/main || {
  echo "target is not in origin/main history" >&2
  exit 65
}

previous=$(git -C "$repo" rev-parse --verify 'HEAD^{commit}')
deployments="$state/deployments"
mkdir -p "$deployments"
git -C "$repo" checkout --detach "$target"
printf '%s\n' "$previous" >"$deployments/previous-sha.tmp"
mv "$deployments/previous-sha.tmp" "$deployments/previous-sha"
printf '%s\n' "$target" >"$deployments/current-sha.tmp"
mv "$deployments/current-sha.tmp" "$deployments/current-sha"

restore_previous() {
  git -C "$repo" checkout --detach "$previous"
  printf '%s\n' "$previous" >"$deployments/current-sha.tmp"
  mv "$deployments/current-sha.tmp" "$deployments/current-sha"
}

if ! git -C "$repo" diff --quiet "$previous" "$target" -- \
  packages/client/pyproject.toml packages/central/pyproject.toml; then
  if command -v uv >/dev/null 2>&1; then
    if ! uv pip install --python "$PURSERS_FLEET_PYTHON" \
      -e "$repo/packages/client" -e "$repo/packages/central"; then
      restore_previous
      echo "dependency reinstall failed; checkout restored to $previous" >&2
      exit 69
    fi
  elif ! "$PURSERS_FLEET_PYTHON" -m pip install \
    -e "$repo/packages/client" -e "$repo/packages/central"; then
    restore_previous
    echo "dependency reinstall failed; checkout restored to $previous" >&2
    exit 69
  fi
fi

if ! (cd "$repo" && "$PURSERS_FLEET_PYTHON" -c \
  'import pursers_client, pursers_central, mcp'); then
  restore_previous
  echo "dependency import probe failed; checkout restored to $previous" >&2
  exit 69
fi

if ! launchctl kickstart -k "$job"; then
  restore_previous
  echo "restart failed; checkout restored to $previous" >&2
  exit 69
fi

echo "Fleet Dashboard upgraded: $previous -> $target"
echo "Verify: curl --fail --silent http://127.0.0.1:${PURSERS_FLEET_PORT:-8899}/api/version"
