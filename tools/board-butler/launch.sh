#!/bin/sh
# Repo-owned fleet entry. Shadow by default; active mode requires authorization.
set -eu

: "${PURSERS_BUTLER_PYTHON:?set PURSERS_BUTLER_PYTHON}"
: "${PURSERS_BUTLER_REPO:?set PURSERS_BUTLER_REPO}"
: "${PURSERS_BUTLER_URL:?set PURSERS_BUTLER_URL}"
: "${PURSERS_BUTLER_TOKEN_PATH:?set PURSERS_BUTLER_TOKEN_PATH}"
: "${PURSERS_BUTLER_STATE_DIR:?set PURSERS_BUTLER_STATE_DIR}"
: "${PURSERS_BUTLER_PROVIDER_SECRETS_DIR:?set PURSERS_BUTLER_PROVIDER_SECRETS_DIR}"
: "${PURSERS_BUTLER_HOME_BOARD:?set PURSERS_BUTLER_HOME_BOARD}"

umask 077
repo=$(cd "$PURSERS_BUTLER_REPO" && pwd -P)
state=$(mkdir -p "$PURSERS_BUTLER_STATE_DIR" && cd "$PURSERS_BUTLER_STATE_DIR" && pwd -P)
provider_secrets=$(mkdir -p "$PURSERS_BUTLER_PROVIDER_SECRETS_DIR" && cd "$PURSERS_BUTLER_PROVIDER_SECRETS_DIR" && pwd -P)
chmod 700 "$state" "$provider_secrets"
case "$state/" in "$repo/"*) echo "PURSERS_BUTLER_STATE_DIR must be outside the checkout" >&2; exit 64;; esac
case "$provider_secrets/" in "$repo/"*) echo "PURSERS_BUTLER_PROVIDER_SECRETS_DIR must be outside the checkout" >&2; exit 64;; esac
test -f "$repo/tools/board-butler/board_butler.py"

runtime_mode=${PURSERS_BUTLER_RUNTIME_MODE:-shadow}
case "$runtime_mode" in
  shadow)
    set -- --runtime-mode shadow
    ;;
  active)
    : "${PURSERS_BUTLER_ACTIVE_AUTHORIZATION_FILE:?set PURSERS_BUTLER_ACTIVE_AUTHORIZATION_FILE}"
    : "${PURSERS_BUTLER_ACTIVE_BOARD:?set PURSERS_BUTLER_ACTIVE_BOARD}"
    : "${PURSERS_BUTLER_FLEET_OBSERVATION_FILE:?set PURSERS_BUTLER_FLEET_OBSERVATION_FILE}"
    : "${PURSERS_BUTLER_FLEET_STATE_FILE:?set PURSERS_BUTLER_FLEET_STATE_FILE}"
    : "${PURSERS_BUTLER_FLEET_EXECUTOR_SOCKET:?set PURSERS_BUTLER_FLEET_EXECUTOR_SOCKET}"
    : "${PURSERS_BUTLER_FLEET_EXECUTOR_KEY_ID:?set PURSERS_BUTLER_FLEET_EXECUTOR_KEY_ID}"
    : "${PURSERS_BUTLER_FLEET_EXECUTOR_PRIVATE_KEY:?set PURSERS_BUTLER_FLEET_EXECUTOR_PRIVATE_KEY}"
    : "${PURSERS_BUTLER_FLEET_EXECUTOR_CONFIG:?set PURSERS_BUTLER_FLEET_EXECUTOR_CONFIG}"
    : "${PURSERS_BUTLER_SUPERVISOR_ROSTER_FILE:?set PURSERS_BUTLER_SUPERVISOR_ROSTER_FILE}"
    set -- --runtime-mode active \
      --active-authorization-file "$PURSERS_BUTLER_ACTIVE_AUTHORIZATION_FILE" \
      --act-on-board "$PURSERS_BUTLER_ACTIVE_BOARD" \
      --fleet-observation-file "$PURSERS_BUTLER_FLEET_OBSERVATION_FILE" \
      --fleet-state-file "$PURSERS_BUTLER_FLEET_STATE_FILE" \
      --fleet-executor-socket "$PURSERS_BUTLER_FLEET_EXECUTOR_SOCKET" \
      --fleet-executor-key-id "$PURSERS_BUTLER_FLEET_EXECUTOR_KEY_ID" \
      --fleet-executor-private-key "$PURSERS_BUTLER_FLEET_EXECUTOR_PRIVATE_KEY" \
      --fleet-executor-config "$PURSERS_BUTLER_FLEET_EXECUTOR_CONFIG" \
      --supervisor-roster-file "$PURSERS_BUTLER_SUPERVISOR_ROSTER_FILE"
    ;;
  *)
    echo "PURSERS_BUTLER_RUNTIME_MODE must be shadow or active" >&2
    exit 64
    ;;
esac

if [ -n "${PURSERS_BUTLER_CONNECTOR_CONFIG:-}" ]; then
  set -- "$@" --connector-config "$PURSERS_BUTLER_CONNECTOR_CONFIG"
fi
if [ -n "${PURSERS_BUTLER_SOURCE_INTAKE_INDEX_FILE:-}" ]; then
  set -- "$@" --source-intake-index-file "$PURSERS_BUTLER_SOURCE_INTAKE_INDEX_FILE"
fi
if [ -n "${PURSERS_BUTLER_INTAKE_ONBOARDING_CONFIG:-}" ]; then
  set -- "$@" --intake-onboarding-config "$PURSERS_BUTLER_INTAKE_ONBOARDING_CONFIG"
fi
if [ "${PURSERS_BUTLER_FLEET_OBSERVATION_MODE:-file}" = local ]; then
  : "${PURSERS_BUTLER_FLEET_LOCAL_CONFIG:?set PURSERS_BUTLER_FLEET_LOCAL_CONFIG}"
  : "${PURSERS_BUTLER_FLEET_EXECUTOR_STATE:?set PURSERS_BUTLER_FLEET_EXECUTOR_STATE}"
  set -- "$@" --fleet-observation-mode local --fleet-local-config "$PURSERS_BUTLER_FLEET_LOCAL_CONFIG" \
    --fleet-executor-state "$PURSERS_BUTLER_FLEET_EXECUTOR_STATE"
fi

exec "$PURSERS_BUTLER_PYTHON" "$repo/tools/board-butler/board_butler.py" \
  --url "$PURSERS_BUTLER_URL" \
  --token-path "$PURSERS_BUTLER_TOKEN_PATH" \
  --home-board "$PURSERS_BUTLER_HOME_BOARD" \
  --agent-name board-butler-1 \
  --repo "$repo" \
  --pid-file "$state/board-butler.pid" \
  --cursor-file "$state/board-butler.cursor.json" \
  --provider-secrets-dir "$provider_secrets" \
  --runtime-status-file "$state/runtime.json" \
  --local-kill-file "$state/KILLED" \
  --refresh-seconds 60 \
  "$@"
