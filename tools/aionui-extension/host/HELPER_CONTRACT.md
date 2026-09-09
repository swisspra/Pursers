# Pursers Home loopback helper contract

AionUi 2.2.1 embeds local extension settings pages as sandboxed AionCore-served
iframes. AionCore 0.2.1 resolves extension WebUI route metadata but does not
execute the declared JavaScript handlers. Pursers Home therefore uses an
explicit local helper instead of depending on an unsupported host route API.

The helper:

- binds only to a loopback hostname;
- accepts one exact loopback HTTP `Origin` configured at startup;
- requires `x-pursers-home-token` on every non-preflight request;
- reads the token from a regular mode-0600 file and never prints it;
- binds every onboarding request and status response to one configured board;
- uses a separately selected wait-bridge state directory;
- caps request bodies at 64 KiB and never returns door values;
- defers MCP registration to the authenticated same-origin AionCore API;
- removes AionUi conversation runtime variables before Team CLI calls;
- keeps one persistent, inert board actor for standalone group operations;
- pins group routes to the selected board and uses Central board-state CAS; and
- closes the group sidecar when the helper stops.

The last rule is deliberate. A settings iframe has no supported Team
conversation context in AionUi 2.2.1. Team status, plan, apply, pause, and stop
therefore return the host's `runtime_context_missing` response. Home presents
the actionable fallback—open or create the Team in AionUi and use its native
Team controls—without treating local validation or a mock roster as host
execution.

Start the helper after obtaining the AionCore origin used by the installed
settings iframe:

```sh
umask 077
openssl rand -hex 32 > /PATH/TO/pursers-home-token
node host/helper.cjs \
  --board sandbox-example \
  --origin http://127.0.0.1:25808 \
  --token-file /PATH/TO/pursers-home-token \
  --bridge-state-dir /PATH/TO/isolated-bridge-state \
  --bridge-bin /PATH/TO/pursers-wait-bridge \
  --aioncore-bin /PATH/TO/aioncore \
  --core-version 0.2.1
```

Paste the helper URL and token into Pursers Home. Both remain in page memory
only and must be entered again after a reload. A wrong origin, token, or board
fails closed. For normal use, select the actual connected board; isolated
verification must use a synthetic `sandbox-*` board and separate state path.
