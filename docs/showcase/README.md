# Pursers product showcase

These images were captured from disposable loopback-only instances with light
themes and synthetic data. No production board, personal profile, credential,
or user data appears in the captures. Every PNG is 1,440 pixels wide and was
losslessly optimized after capture.

## Fleet Dashboard

[![Fleet overview](01-fleet-overview.png)](01-fleet-overview.png)

The Fleet overview shows a live disposable Central with a populated board,
agent availability, ticket totals, and bounded attention findings.

## Personal dashboard

[![Personal Today view](02-personal-today.png)](02-personal-today.png)

The Personal **Today** view combines health, active work, agents, continuity,
pinned context, and recent activity. The bundled dashboard deliberately labels
this disconnected fixture as synthetic demo data.

[![Personal Work view](03-personal-work.png)](03-personal-work.png)

The **Work** view groups the bundled demo tickets by lifecycle state.

[![Personal Agents view](04-personal-agents.png)](04-personal-agents.png)

The **Agents** view shows the read-only local roster and current focus.

## AionUi extension

[![Pursers Home join view](05-aionui-join.png)](05-aionui-join.png)

Pursers Home is served by the bundled AionCore from a freshly built extension
ZIP. The first view presents the loopback helper and project-door join flow
without displaying either secret.

[![Pursers Home offer and claim view](06-aionui-offer-claim.png)](06-aionui-offer-claim.png)

The board-backed ticket view shows live offers from the disposable Central and
the result of an exact-identity claim by `aion-showcase-worker`.

[![Pursers Home status view](07-aionui-status.png)](07-aionui-status.png)

The status view confirms the board, Central domain, authenticated loopback
transport, and bundled AionCore version while keeping the helper token hidden.

## Staging commands

The screenshots used ports `31821` through `31825`; these avoid the normal
Pursers, AionUi, and acceptance-test ports. Starting from an isolated clone of
`origin/main`, the exact service layout was:

```sh
ROOT=/PATH/TO/ISOLATED/CLONE
RUN="$ROOT/.showcase-runtime"

# A disposable Central fixture was seeded as showcase-harbor with local-only,
# mode-0600 JWT/JWKS files under $RUN/central and synthetic agents/tickets.
env \
  CENTRAL_JWT_ISSUER=https://showcase.example \
  CENTRAL_JWT_AUDIENCE=http://127.0.0.1:31821/mcp \
  CENTRAL_JWKS_PATH="$RUN/central/jwks.json" \
  "$RUN/venv/bin/python" -c \
  'from pursers_central.pursers_central_runtime import main; main()' \
  --host 127.0.0.1 --port 31821 --data-dir "$RUN/central/data"

env PURSERS_STATE_DIR="$RUN/fleet-state" \
  "$RUN/venv/bin/python" "$ROOT/tools/fleet-dashboard/fleet_dashboard.py" \
  --host 127.0.0.1 --port 31822 \
  --url http://127.0.0.1:31821/mcp \
  --token-file "$RUN/central/admin.jwt" \
  --home-board showcase-harbor --agent-name showcase-viewer \
  --stale-seconds 300 --cache-seconds 1

python3 -m http.server 31823 --bind 127.0.0.1 \
  --directory "$ROOT/packages/personal/src/pursers_personal/resources"
```

The AionUi images were staged from the authorized Home UI checkpoint
`6c514f87ed39445d18d6812e0946c19ba2eca7fe`; the documentation changes remain
based on `origin/main`.

```sh
git -C "$ROOT" worktree add --detach "$RUN/product" \
  6c514f87ed39445d18d6812e0946c19ba2eca7fe
python3 "$RUN/product/tools/aionui-extension/build.py"
mkdir -p "$RUN/aion/extensions/pursers" "$RUN/aion/core-data"
unzip -q "$RUN/product/dist/pursers-aionui-0.1.0.zip" \
  -d "$RUN/aion/extensions/pursers"

env AIONUI_EXTENSIONS_PATH="$RUN/aion/extensions" \
  '/Applications/AionUi.app/Contents/Resources/bundled-aioncore/darwin-arm64/aioncore' \
  --host 127.0.0.1 --port 31824 --data-dir "$RUN/aion/core-data" \
  --app-version 2.2.2 --local --managed-resources-mode bundled

node "$RUN/aion/extensions/pursers/host/helper.cjs" \
  --host 127.0.0.1 --port 31825 \
  --board showcase-harbor --central default \
  --origin http://127.0.0.1:31824 \
  --token-file "$RUN/aion/helper-token" \
  --bridge-state-dir "$RUN/aion/bridge-state" \
  --bridge-bin "$RUN/venv/bin/pursers-wait-bridge" \
  --aioncore-bin \
  '/Applications/AionUi.app/Contents/Resources/bundled-aioncore/darwin-arm64/aioncore' \
  --fleet-url http://127.0.0.1:31822 --core-version 0.2.2
```

The browser was the operator-approved Ego Lite CLI. Long pages used a 1,455
CSS-pixel device viewport so the 15-pixel scrollbar left an exact 1,440-pixel
PNG; viewport captures used 1,440 CSS pixels directly.

```sh
'/Applications/ego lite.app/Contents/Frameworks/ego Framework.framework/Versions/Current/Helpers/ego-browser' nodejs < capture.js

python3 - <<'PY'
from pathlib import Path
from PIL import Image

for path in sorted(Path("docs/showcase").glob("*.png")):
    output = path.with_suffix(".optimized.png")
    with Image.open(path) as image:
        image.save(output, format="PNG", optimize=True, compress_level=9)
    output.replace(path)
PY
```

All disposable Central, Fleet, Personal, helper, and AionCore processes were
stopped after capture.

## Example tool-call transcripts

These are text-only examples for desktop hosts. Identifiers are illustrative;
no credential is included.

### Claude Desktop — join and accept offered work

```text
Claude → board_onboard
{"board_id":"sandbox-project","agent_name":"claude-worker-1","role":"worker",
 "allow_takeover":true,"capabilities":{"can_work":true,"can_review":false,
 "tier_max":2,"max_parallel":1}}

Pursers → agent_name=claude-worker-1, role=worker,
          work_offer.ticket_id=TK-example-001

Claude → ticket_claim
{"board_id":"sandbox-project","agent_name":"claude-worker-1",
 "ticket_id":"TK-example-001"}

Pursers → ok=true, status=claimed, claimed_by=claude-worker-1
```

### Codex — inspect, renew, and submit

```text
Codex → ticket_get
{"board_id":"sandbox-project","ticket_id":"TK-example-001"}

Pursers → status=claimed, scope=READ-ONLY,
          required_fields=[branch_and_commit, files_changed, test-output]

Codex → lease_renew
{"board_id":"sandbox-project","agent_name":"codex-worker-1",
 "ticket_id":"TK-example-001","renewal_source":"model"}

Pursers → ok=true, lease_kind=work, ttl_s=900

Codex → ticket_submit
{"board_id":"sandbox-project","agent_name":"codex-worker-1",
 "ticket_id":"TK-example-001","files_changed":["docs/example.md"],
 "summary":"Documented the verified flow.","stay_active":false}

Pursers → ok=true, status=submitted
```
