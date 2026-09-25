#!/bin/sh
# Repository-owned Fleet Dashboard entry point for an operator-managed LaunchAgent.
set -eu

: "${PURSERS_FLEET_PYTHON:?set PURSERS_FLEET_PYTHON}"
: "${PURSERS_FLEET_REPO:?set PURSERS_FLEET_REPO}"
: "${PURSERS_FLEET_RUNTIME_DIR:?set PURSERS_FLEET_RUNTIME_DIR}"
: "${PURSERS_FLEET_STATE_DIR:?set PURSERS_FLEET_STATE_DIR}"
: "${PURSERS_BUTLER_STATE_DIR:?set PURSERS_BUTLER_STATE_DIR}"
: "${PURSERS_BUTLER_ENTRYPOINT:?set PURSERS_BUTLER_ENTRYPOINT}"
: "${PURSERS_BUTLER_PROVIDER_SECRETS_DIR:?set PURSERS_BUTLER_PROVIDER_SECRETS_DIR}"

umask 077
repo=$(cd "$PURSERS_FLEET_REPO" && pwd -P)
runtime=$(mkdir -p "$PURSERS_FLEET_RUNTIME_DIR" && cd "$PURSERS_FLEET_RUNTIME_DIR" && pwd -P)
state=$(mkdir -p "$PURSERS_FLEET_STATE_DIR" && cd "$PURSERS_FLEET_STATE_DIR" && pwd -P)
butler_state=$(mkdir -p "$PURSERS_BUTLER_STATE_DIR" && cd "$PURSERS_BUTLER_STATE_DIR" && pwd -P)
butler_secrets=$(mkdir -p "$PURSERS_BUTLER_PROVIDER_SECRETS_DIR" && cd "$PURSERS_BUTLER_PROVIDER_SECRETS_DIR" && pwd -P)
butler_entrypoint=$(cd "$(dirname "$PURSERS_BUTLER_ENTRYPOINT")" && printf '%s/%s\n' "$(pwd -P)" "$(basename "$PURSERS_BUTLER_ENTRYPOINT")")
chmod 700 "$butler_state" "$butler_secrets"

case "$runtime/" in "$repo/"*) echo "PURSERS_FLEET_RUNTIME_DIR must be outside the checkout" >&2; exit 64;; esac
case "$state/" in "$repo/"*) echo "PURSERS_FLEET_STATE_DIR must be outside the checkout" >&2; exit 64;; esac
case "$butler_state/" in "$repo/"*) echo "PURSERS_BUTLER_STATE_DIR must be outside the Fleet checkout" >&2; exit 64;; esac
case "$butler_secrets/" in "$repo/"*) echo "PURSERS_BUTLER_PROVIDER_SECRETS_DIR must be outside the Fleet checkout" >&2; exit 64;; esac
test -f "$repo/tools/fleet-dashboard/fleet_dashboard.py"
test -f "$butler_entrypoint"

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
  --butler-secrets-dir "$butler_secrets" \
  --butler-state-dir "$butler_state" \
  --butler-entrypoint "$butler_entrypoint"

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
