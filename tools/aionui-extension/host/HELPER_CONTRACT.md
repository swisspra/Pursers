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
- binds result reads to one explicit Central label plus board, and requires both
  identities in the Fleet response;
- uses a separately selected wait-bridge state directory;
- caps request bodies at 64 KiB and never returns door values;
- reads only the selected board from a loopback Fleet dashboard, with a 5-second
  timeout, 512 KiB response cap, no credentials, and no redirects;
- defers MCP registration to the authenticated same-origin AionCore API; and
- removes AionUi conversation runtime variables before Team CLI calls.

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
  --central work \
  --origin http://127.0.0.1:25808 \
  --token-file /PATH/TO/pursers-home-token \
  --bridge-state-dir /PATH/TO/isolated-bridge-state \
  --bridge-bin /PATH/TO/pursers-wait-bridge \
  --aioncore-bin /PATH/TO/aioncore \
  --fleet-url http://127.0.0.1:8899 \
  --core-version 0.2.1
```

Paste the helper URL and token into Pursers Home. Both remain in page memory
only and must be entered again after a reload. A wrong origin, token, or board
fails closed. For normal use, select the actual connected board; isolated
verification must use a synthetic `sandbox-*` board and separate state path.
`GET /pursers/results` is the only result route. It is read-only,
Central-and-board-pinned,
and returns the allow-listed projection defined in
`result_visibility/FEATURE_CONTRACT.md`; it never returns submission or review
notes.
