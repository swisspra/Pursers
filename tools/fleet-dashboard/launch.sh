#!/bin/sh
# Repository-owned Fleet Dashboard entry point for an operator-managed LaunchAgent.
set -eu

: "${PURSERS_FLEET_PYTHON:?set PURSERS_FLEET_PYTHON}"
: "${PURSERS_FLEET_REPO:?set PURSERS_FLEET_REPO}"
: "${PURSERS_FLEET_RUNTIME_DIR:?set PURSERS_FLEET_RUNTIME_DIR}"
: "${PURSERS_FLEET_STATE_DIR:?set PURSERS_FLEET_STATE_DIR}"

repo=$(cd "$PURSERS_FLEET_REPO" && pwd -P)
runtime=$(mkdir -p "$PURSERS_FLEET_RUNTIME_DIR" && cd "$PURSERS_FLEET_RUNTIME_DIR" && pwd -P)
state=$(mkdir -p "$PURSERS_FLEET_STATE_DIR" && cd "$PURSERS_FLEET_STATE_DIR" && pwd -P)

case "$runtime/" in "$repo/"*) echo "PURSERS_FLEET_RUNTIME_DIR must be outside the checkout" >&2; exit 64;; esac
case "$state/" in "$repo/"*) echo "PURSERS_FLEET_STATE_DIR must be outside the checkout" >&2; exit 64;; esac
test -f "$repo/tools/fleet-dashboard/fleet_dashboard.py"

umask 077
mkdir -p "$runtime/tmp" "$runtime/pycache" \
  "$state/workers" "$state/fleet-dashboard" "$state/fleet-dashboard/secrets"
export TMPDIR="$runtime/tmp"
export PYTHONPYCACHEPREFIX="$runtime/pycache"
export PURSERS_STATE_DIR="$state"

set -- \
  --host 127.0.0.1 \
  --port "${PURSERS_FLEET_PORT:-8899}" \
  --agent-name "${PURSERS_FLEET_AGENT_NAME:-fleet-dashboard-session-default}" \
  --workers-dir "$state/workers" \
  --seat-state-dir "$state/fleet-dashboard" \
  --butler-secrets-dir "$state/fleet-dashboard/secrets"

if [ -n "${PURSERS_FLEET_CENTRALS:-}" ]; then
  set -- "$@" --centrals "$PURSERS_FLEET_CENTRALS"
else
  : "${PURSERS_FLEET_URL:?set PURSERS_FLEET_URL or PURSERS_FLEET_CENTRALS}"
  : "${PURSERS_FLEET_TOKEN_PATH:?set PURSERS_FLEET_TOKEN_PATH or PURSERS_FLEET_CENTRALS}"
  set -- "$@" \
    --url "$PURSERS_FLEET_URL" \
    --token-file "$PURSERS_FLEET_TOKEN_PATH" \
    --home-board "${PURSERS_FLEET_HOME_BOARD:-pursers}"
fi

[ -z "${PURSERS_FLEET_DOORS_KEYS_DIR:-}" ] || set -- "$@" --doors-keys-dir "$PURSERS_FLEET_DOORS_KEYS_DIR"
[ -z "${PURSERS_FLEET_JWKS_PATH:-}" ] || set -- "$@" --jwks-path "$PURSERS_FLEET_JWKS_PATH"
[ -z "${PURSERS_FLEET_EVIDENCE_TRACE_CONFIG:-}" ] || set -- "$@" --evidence-trace-config "$PURSERS_FLEET_EVIDENCE_TRACE_CONFIG"

cd "$repo"
exec "$PURSERS_FLEET_PYTHON" "$repo/tools/fleet-dashboard/fleet_dashboard.py" "$@"
