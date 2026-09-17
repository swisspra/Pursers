#!/bin/sh
# Repo-owned fleet entry. This path is intentionally shadow-only.
set -eu

: "${PURSERS_BUTLER_PYTHON:?set PURSERS_BUTLER_PYTHON}"
: "${PURSERS_BUTLER_REPO:?set PURSERS_BUTLER_REPO}"
: "${PURSERS_BUTLER_URL:?set PURSERS_BUTLER_URL}"
: "${PURSERS_BUTLER_TOKEN_PATH:?set PURSERS_BUTLER_TOKEN_PATH}"
: "${PURSERS_BUTLER_STATE_DIR:?set PURSERS_BUTLER_STATE_DIR}"
: "${PURSERS_BUTLER_HOME_BOARD:?set PURSERS_BUTLER_HOME_BOARD}"

runtime_mode=${PURSERS_BUTLER_RUNTIME_MODE:-shadow}
case "$runtime_mode" in
  shadow)
    set -- --runtime-mode shadow
    ;;
  active)
    : "${PURSERS_BUTLER_ACTIVE_AUTHORIZATION_FILE:?set PURSERS_BUTLER_ACTIVE_AUTHORIZATION_FILE}"
    : "${PURSERS_BUTLER_ACTIVE_BOARD:?set PURSERS_BUTLER_ACTIVE_BOARD}"
    set -- --runtime-mode active \
      --active-authorization-file "$PURSERS_BUTLER_ACTIVE_AUTHORIZATION_FILE" \
      --act-on-board "$PURSERS_BUTLER_ACTIVE_BOARD"
    ;;
  *)
    echo "PURSERS_BUTLER_RUNTIME_MODE must be shadow or active" >&2
    exit 64
    ;;
esac

exec "$PURSERS_BUTLER_PYTHON" "$PURSERS_BUTLER_REPO/tools/board-butler/board_butler.py" \
  --url "$PURSERS_BUTLER_URL" \
  --token-path "$PURSERS_BUTLER_TOKEN_PATH" \
  --home-board "$PURSERS_BUTLER_HOME_BOARD" \
  --agent-name board-butler-1 \
  --repo "$PURSERS_BUTLER_REPO" \
  --pid-file "$PURSERS_BUTLER_STATE_DIR/board-butler.pid" \
  --cursor-file "$PURSERS_BUTLER_STATE_DIR/board-butler.cursor.json" \
  --provider-secrets-dir "$PURSERS_BUTLER_STATE_DIR/secrets" \
  --runtime-status-file "$PURSERS_BUTLER_STATE_DIR/runtime.json" \
  --local-kill-file "$PURSERS_BUTLER_STATE_DIR/KILLED" \
  --refresh-seconds 60 \
  "$@"
