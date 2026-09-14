# Getting started with Pursers 5.0.0b1

Pursers 5.0.0b1 is a single-owner beta for one trusted Mac. It runs Central on
loopback, keeps board data in SQLite, and lets MCP hosts such as Claude Desktop,
Codex, and AionUi work on the same durable board. It is not a remote or
multi-user security boundary.

This guide starts from the six wheels attached to the `v5.0.0b1` GitHub release
and builds the extension ZIP from that exact tag. Do not substitute files from
another tag. For the product boundary and architecture, see the
[README](../README.md) and
[architecture overview](ARCHITECTURE.md).

## Before you begin

You need:

- macOS and a trusted local user account;
- Python 3.11 through 3.14 (`python3 --version`);
- GitHub CLI (`gh`), `git`, and enough space for a virtual environment and local SQLite data;
- an MCP host: Claude Desktop, Codex CLI, or both;
- AionUi 2.2.1 only if you want to inspect the optional extension ZIP.

Keep every JWT, invite, and `prs1.…` door private. Do not paste credentials into
tickets, commits, logs, screenshots, or shared host configuration. The examples
use `/PATH/TO/...` placeholders deliberately.

## 1. Download and install the release

Create an empty directory, download the checksum file and all six wheels from
the same tag, then verify them before installing:

```bash
mkdir -p pursers-5.0.0b1 && cd pursers-5.0.0b1
gh release download v5.0.0b1 \
  --repo swisspra/Pursers \
  --pattern '*.whl' \
  --pattern SHA256SUMS.txt \
  --dir .
shasum -a 256 -c SHA256SUMS.txt
```

The approved beta build has these exact wheel hashes:

```text
af2d2722652ce5f952b50d4a50933edc13e12a10a88977c8344bf76b2faf87cd  pursers-5.0.0b1-py3-none-any.whl
197fee2894b1c7b0204f844a4c2030d4a9245489dd8fca61693fca6e09a6a636  pursers_central-0.1.0a30-py3-none-any.whl
e2314191a354ab2ab0d0020c1ef80cc0049909eaf5219fcdff0c0f24847b677e  pursers_client-0.1.0a23-py3-none-any.whl
2946cde056bfc1a6b38c1e7b07ed8b3d71ba1b77a7fb3b4e69f7f81db1839935  pursers_personal-5.0.0b1-py3-none-any.whl
34e7d992dffc7b50560706ecdb4c51a5ce67e50edb5c24ec8de2cd48259ed590  pursers_personal_import-5.0.0a3-py3-none-any.whl
102d6eb35daae393c8fef589c8b3e60b02ab985d01856f64afb99ba3ba94834f  pursers_wait_bridge-0.1.0a16-py3-none-any.whl
```

The published [`v5.0.0b1` prerelease](https://github.com/swisspra/Pursers/releases/tag/v5.0.0b1)
contains exactly these six wheel assets and `SHA256SUMS.txt`.

Use a dedicated environment. Installing all six wheels together lets the
installer resolve their exact cross-package versions:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install ./*.whl
.venv/bin/pursers-personal --version
.venv/bin/pursers-wait-bridge --version
```

The final two commands must print `5.0.0b1` and `0.1.0a16` respectively.

## 2. Create the owner profile

Close Claude Desktop before changing its configuration. Choose a project, a
private profile root, and an unused loopback port. First preview the operation;
then repeat it with `--apply` after checking every path:

```bash
export PURSERS_PROJECT=/PATH/TO/project
export PURSERS_PROFILES=/PATH/TO/private/pursers-profiles
export CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
mkdir -p "$PURSERS_PROJECT" "$PURSERS_PROFILES"
chmod 700 "$PURSERS_PROFILES"

.venv/bin/pursers-personal --json setup \
  --project "$PURSERS_PROJECT" \
  --profiles-root "$PURSERS_PROFILES" \
  --port 8766 \
  --host-id claude-desktop \
  --session owner \
  --host-config "$CLAUDE_CONFIG"

.venv/bin/pursers-personal --json setup \
  --project "$PURSERS_PROJECT" \
  --profiles-root "$PURSERS_PROFILES" \
  --port 8766 \
  --host-id claude-desktop \
  --session owner \
  --host-config "$CLAUDE_CONFIG" \
  --apply
```

The applied result names the generated profile, owner principal, owner agent,
and first board. Record the `profile_path` and `board_id`; never record the
credential itself. The owner capability has `board:read`, `board:write`, and
`board:review`, while board admission remains invite-only.

Set these shell variables from the generated `profile.json`. `TOKEN_FILE` is
the file named by `files.token`, and `JWKS_FILE` is the file named by
`files.jwks`, both relative to the profile directory:

```bash
export PURSERS_PROFILE=/PATH/TO/profile.json
export PURSERS_DATA_DIR=/PATH/TO/profile-directory/central-data
export TOKEN_FILE=/PATH/TO/profile-directory/credential.jwt
export JWKS_FILE=/PATH/TO/profile-directory/credential.jwks.json
export CENTRAL_JWT_ISSUER='http://127.0.0.1:8766/personal-issuer/PROFILE_ID'
export CENTRAL_JWT_AUDIENCE='http://127.0.0.1:8766/mcp'
export CENTRAL_JWKS_PATH="$JWKS_FILE"
```

## 3. Start Central and check health

Keep this process running in its own terminal:

```bash
.venv/bin/python -c "from pursers_central.pursers_central_runtime import main; main()" \
  --data-dir "$PURSERS_DATA_DIR" \
  --host 127.0.0.1 \
  --port 8766
```

In another terminal, verify the production health endpoint:

```bash
curl --fail --silent http://127.0.0.1:8766/healthz
```

A healthy response starts with `{"status":"ok","store_backend":"sqlite"`.
It also reports the board count, journal head, uptime, and file-descriptor
pressure without exposing credentials.

When running the Fleet Dashboard from a source checkout, pass the same Central
URL explicitly, including its scheme:

```bash
python tools/fleet-dashboard/fleet_dashboard.py \
  --url http://127.0.0.1:8766/mcp \
  --token-file "$TOKEN_FILE"
```

Open `http://127.0.0.1:8899`. If Central is behind the repository's local TLS
wrapper, use its `https://` MCP URL instead.

> **Known b1 limitation:** `python -m pursers_central.pursers_central_runtime`
> exits silently and there is no `pursers-central` console script yet; fixed in
> beta.2 (see ticket “central: runnable entry point”). Use the `python -c`
> command above for beta.1. `pursers-personal central` is the Personal-embedded
> service and its `/healthz` returning 404 is expected in beta.1.

> **Known b1 shutdown noise:** Central can log
> `offer_deadline_error ... Cannot operate on a closed database` when a
> dispatcher offer-deadline task finishes during shutdown. The message is
> harmless to board data. The fix is tracked for beta.2 as “central:
> dispatcher offer-deadline task uses a closed sqlite connection
> (ProgrammingError x212 in live log)”.

## 4. Connect Claude Desktop

Claude Desktop can reach the HTTP MCP server through `mcp-remote`. Make a
private header file from the owner token without printing it:

```bash
export HEADER_FILE=/PATH/TO/private/pursers-owner.headers
(umask 077; {
  printf 'Authorization: Bearer '
  tr -d '\n' < "$TOKEN_FILE"
  printf '\n'
} > "$HEADER_FILE")
```

Merge this entry into `mcpServers` in
`~/Library/Application Support/Claude/claude_desktop_config.json`; keep your
existing entries:

```json
{
  "mcpServers": {
    "pursers": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@0.14.0",
        "http://127.0.0.1:8766/mcp",
        "--protocol",
        "auto",
        "--header-file",
        "/PATH/TO/private/pursers-owner.headers"
      ]
    }
  }
}
```

Restart Claude Desktop completely. Ask it to call `board_onboard` with the
generated board ID, the generated owner agent name, and role `worker`.
Authentication determines the owner principal; the agent name is not a
credential. Then use the direct `pursers` connection to set and verify the
strict review policy:

```text
board_review_policy_set(
  board_id="BOARD_ID",
  agent_name="OWNER_AGENT_NAME",
  review_policy="strict"
)
board_status(board_id="BOARD_ID")
```

The generated `pursers-personal` facade is the Personal preview and selects its
`workflow` policy when it onboards. Use the direct Central connection named
`pursers` for the strict board flow in this guide.

## 5. Connect Codex CLI

Add the HTTP server to `~/.codex/config.toml`. The default asks before every
tool; the read-only status tool is allowed automatically:

```toml
[mcp_servers.pursers]
url = "http://127.0.0.1:8766/mcp"
bearer_token_env_var = "PURSERS_OWNER_TOKEN"
default_tools_approval_mode = "prompt"

[mcp_servers.pursers.tools.board_status]
approval_mode = "auto"
```

Load the bearer only into the Codex process environment, then start Codex:

```bash
PURSERS_OWNER_TOKEN="$(tr -d '\n' < "$TOKEN_FILE")" codex
```

Do not put the token value in `config.toml`. In Codex, call `board_onboard` with
the same board and a distinct stable agent name for this seat.

## 6. Create an admission invite and the first ticket

The first successful owner `board_onboard` bootstraps the generated board and
its admin membership. From the owner-connected MCP host, make these tool calls:

```text
board_invite(
  board_id="BOARD_ID",
  agent_name="OWNER_AGENT_NAME",
  role="member",
  ttl_s=3600
)
```

The returned invite is single-use and short-lived. Give it only to the intended
principal over a private channel. That principal supplies it as `invite_token`
on its first `board_join` or `board_onboard`; do not store it in the repository.

Create a small first ticket with an explicit ID:

```text
ticket_create(
  board_id="BOARD_ID",
  agent_name="OWNER_AGENT_NAME",
  ticket_id="TK-first-task",
  title="Verify the Pursers beta setup"
)
```

For generated ticket IDs, also provide `description`, `target_url`, `scope`,
and `required_fields`. Valid scopes are `READ-ONLY`, `interactive-no-send`, and
`interactive`.

## 7. Add a worker seat and wait for work

An admission invite admits a principal. A worker door is a separate,
board-bound credential for a durable worker seat. Issue it on the owner Mac
into a mode-`0600` file so the secret `prs1.…` value is not printed:

```bash
export BOARD_ID=BOARD_ID
export WORKER_DOOR_FILE=/PATH/TO/private/worker.door
umask 077
.venv/bin/pursers-door issue \
  --board "$BOARD_ID" \
  --role worker \
  --central-url http://127.0.0.1:8766/mcp \
  --issuer "$CENTRAL_JWT_ISSUER" \
  --jwks "$JWKS_FILE" \
  --keys-dir /PATH/TO/private/door-keys \
  > "$WORKER_DOOR_FILE"
```

Use the issuer from the generated profile exactly. It includes the
`/personal-issuer/PROFILE_ID` suffix; substituting the bare Central origin makes
the derived principal differ from the token principal and Central rejects the
join.

The door principal must be a member before it can join an invite-only board.
Its beta.1 identity is deterministic. Derive its non-secret principal ID, then
use the owner-connected MCP host to provision it:

```bash
export WORKER_PRINCIPAL="$($PWD/.venv/bin/python -c 'import hashlib,json,os; b=os.environ["BOARD_ID"]; c=[f"door-{b}-worker",os.environ["CENTRAL_JWT_ISSUER"],f"door:{b}:worker"]; print("PR-"+hashlib.sha256(json.dumps(c,separators=(",",":")).encode()).hexdigest())')"
```

```text
board_member_add(
  board_id="BOARD_ID",
  agent_name="OWNER_AGENT_NAME",
  principal_id="WORKER_PRINCIPAL",
  role="member"
)
```

This direct provisioning is for the shared worker-door principal. The
single-use admission invite in the previous section remains the safer path for
an independently authenticated human principal.

On the worker host, install the same `pursers-wait-bridge` wheel, transfer the
door file through a private channel, then join with a unique, stable seat name:

```bash
pursers-wait-bridge join --name worker-1 "$(tr -d '\n' < /PATH/TO/private/worker.door)"
pursers-wait-bridge status
```

Register `pursers-wait-bridge` as a stdio MCP server with no environment block.
The model should call `a2a_wait` once without `since_seq`, then pass the complete
returned `new_seq` unchanged on every re-arm:

```text
a2a_wait(boards=["BOARD_ID"], only_mine=true, timeout_s=180)
a2a_wait(boards=["BOARD_ID"], only_mine=true, timeout_s=180,
         since_seq={"BOARD_ID": RETURNED_SEQUENCE})
```

`timed_out=true` with no events is normal. Re-arm it. An immediate result with
`reason="offer"` and `mode="poll"` is only a mode label; claim the offered
ticket normally. Never restart at cursor zero to look for work.

> **Known b1 offer catch-up limitation:** Offer events are push-only. A seat
> that misses the push can see only `offer_expired`. After a wait returns, call
> `ticket_get` for the pinned ticket and claim only when its current offer is
> unexpired and belongs to this exact seat; never claim from the expired event.
> The beta.2 fix is tracked as “wait-bridge: ticket_offered dropped for pinned
> / exclusion-fenced offers (seat sees only offer_expired)”, “wait-bridge
> offer-reconcile follow-up: keep one ticket_list per backlog cadence”, and
> “central+bridge: make offer events recoverable via catch-up”.

## 8. Install the AionUi extension ZIP

Build `pursers-aionui-0.1.0.zip` from the exact release tag; the wheel checksum
file does not cover this locally built artifact:

```bash
git clone --branch v5.0.0b1 --depth 1 https://github.com/swisspra/Pursers.git pursers-source
cd pursers-source
python3 tools/aionui-extension/build.py
test -f dist/pursers-aionui-0.1.0.zip
```

A production desktop install requires a publisher-provided managed AionUi Hub
entry. That route is external: AionUi 2.2.1 has no in-app import for an
arbitrary local ZIP, and this release contains no Hub publishing procedure.
Do not unzip the archive into an existing AionUi data directory.

You can nevertheless verify the exact ZIP reproducibly with the AionCore binary
bundled in AionUi. Unpack it as one extension directory, point
`AIONUI_EXTENSIONS_PATH` at only that directory's parent, and use isolated data:

```bash
mkdir -p /PATH/TO/aion-check/extensions/pursers \
         /PATH/TO/aion-check/data
unzip -q dist/pursers-aionui-0.1.0.zip \
  -d /PATH/TO/aion-check/extensions/pursers

AIONUI_EXTENSIONS_PATH=/PATH/TO/aion-check/extensions \
  /PATH/TO/AionUi.app/Contents/Resources/bundled-aioncore/darwin-arm64/aioncore \
  --host 127.0.0.1 \
  --port 25999 \
  --data-dir /PATH/TO/aion-check/data \
  --app-version 2.2.1 \
  --local
```

`--app-version 2.2.1` is required because the loader checks the manifest's
AionUi engine range. `--local` disables authentication, so use it only for this
loopback throwaway check. When `AIONCORE_READY` appears, verify discovery, the
settings page, and the candidate marker:

```bash
curl --fail http://127.0.0.1:25999/api/extensions
curl --fail http://127.0.0.1:25999/api/extensions/pursers/assets/webui/index.html
curl --fail http://127.0.0.1:25999/api/extensions/pursers/assets/webui/candidate.json
```

The last response must name the release commit. The packaged Home also needs
its authenticated loopback helper because AionCore 0.2.1 serves extension
assets but does not execute the declared JavaScript route handlers. Create a
mode-`0600` local token and start the packaged helper against the same origin:

```bash
umask 077
openssl rand -hex 32 > /PATH/TO/aion-check/home-token
node tools/aionui-extension/host/helper.cjs \
  --board "$BOARD_ID" \
  --central work \
  --origin http://127.0.0.1:25999 \
  --token-file /PATH/TO/aion-check/home-token \
  --bridge-state-dir /PATH/TO/private/bridge-state \
  --bridge-bin /PATH/TO/.venv/bin/pursers-wait-bridge \
  --aioncore-bin /PATH/TO/AionUi.app/Contents/Resources/bundled-aioncore/darwin-arm64/aioncore \
  --core-version 0.2.1 \
  --port 25998
```

After a managed install, open **Settings → Pursers**, enter that helper's
loopback URL and local token, connect a worker door, and verify that Home shows
the exact selected board. The helper token remains in page memory only. The
extension ZIP does not bundle Python or `pursers-wait-bridge`.

## Strict review in five lines

1. A worker finishes by submitting evidence; submission does not close the ticket.
2. A different authenticated principal claims the review.
3. The reviewer checks the exact branch, full commit SHA, changed files, and test output.
4. Rejection returns the ticket with concrete review notes for another offered claim.
5. Only an approving independent review closes the ticket under the strict policy.

## Troubleshooting common beta setup failures

### `invalid_token` or HTTP 401

Reproduced response:

```text
HTTP 401
{"error": "invalid_token", "error_description": "Authentication required"}
```

The token is missing, expired, malformed, signed by a key absent from the live
JWKS, or intended for a different audience. Confirm that the host reads the
current token file, `CENTRAL_JWKS_PATH` points to the same profile generation,
and the MCP URL exactly matches the token audience. Never print the token while
debugging; use `pursers-wait-bridge status` or `pursers-personal doctor` for a
redacted view.

### Missing board scope or wrong board

Typical product errors are:

```text
authenticated principal lacks board:read authorization
board access denied: token is not authorized for this board
```

Do not retry the same credential. Use an owner-generated credential containing
the required role scopes, and use the board embedded in a board-bound door.
Scopes authorize operations; an invite only grants membership and cannot add a
missing OAuth/JWT scope.

### `com.apple.macl` on a LaunchAgent file

If `launchctl bootstrap` rejects a copied plist, inspect its extended
attributes:

```bash
xattr -l "$HOME/Library/LaunchAgents/com.onboard.personal.PROFILE.plist"
```

If the listing contains `com.apple.macl`, close the owning host, remove only
that attribute, confirm the file is owned by you and mode `0600`, then retry the
exact bootstrap:

```bash
xattr -d com.apple.macl "$HOME/Library/LaunchAgents/com.onboard.personal.PROFILE.plist"
chmod 600 "$HOME/Library/LaunchAgents/com.onboard.personal.PROFILE.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.onboard.personal.PROFILE.plist"
```

Do not recursively strip attributes from `~/Library`.

### Port already in use

Reproduced startup error:

```text
ERROR: [Errno 48] error while attempting to bind on address ('127.0.0.1', 8766): [errno 48] address already in use
```

Another process already owns the chosen port. Stop the duplicate Central, or
create a new profile with a different explicit port and update the Central URL,
issuer, audience, Claude configuration, Codex configuration, and newly issued
doors together. Do not point an existing token at a different audience.

### Fleet dashboard fails against a TLS Central

The beta.1 Fleet dashboard defaults to
`http://127.0.0.1:8766/mcp`. If Central uses TLS, pass the HTTPS URL explicitly:

```bash
python3 tools/fleet-dashboard/fleet_dashboard.py \
  --url https://127.0.0.1:8766/mcp
```

Beta.1 does not provide a fail-fast hint when the URL scheme is wrong. The fix
is tracked for beta.2 as “P0 fleet-dashboard: /api/fleet and /api/workers
always 503 ExceptionGroup (httpx2.ReadError) in server mode against live
Central”.

### Expired offer

After an offer expires, a claim is refused with:

```text
ticket is not offered to this seat; wait for your offer
```

Do not force the claim or reuse a stale wait result. Re-arm `a2a_wait` with its
last returned `new_seq`; Central expires and redispatches offers
authoritatively. Claim only a fresh offer addressed to this exact seat.

## Candidate-path transcript

The release candidate was built from commit
`28f81308d1cf3d40c4ed38091cc02d9c7d0827aa` and exercised with a throwaway
profile and data directory. Paths and identifiers below are normalized to
placeholders; credential values were never printed.

```text
$ python3.12 --version
Python 3.12.13

$ python3.12 -m venv /PATH/TO/build-venv
$ /PATH/TO/build-venv/bin/python -m pip install build==1.3.0 setuptools==80.9.0 wheel==0.45.1 packaging==25.0 pyproject-hooks==1.2.0
Successfully installed build-1.3.0 packaging-25.0 pyproject-hooks-1.2.0 setuptools-80.9.0 wheel-0.45.1

$ export SOURCE_DATE_EPOCH=315532800 PYTHONHASHSEED=0
$ for PROJECT in central client import personal; do /PATH/TO/build-venv/bin/python -m build --wheel --no-isolation --outdir /PATH/TO/dist "packages/$PROJECT"; done
Successfully built pursers_central-0.1.0a30-py3-none-any.whl
Successfully built pursers_client-0.1.0a23-py3-none-any.whl
Successfully built pursers_personal_import-5.0.0a3-py3-none-any.whl
Successfully built pursers_personal-5.0.0b1-py3-none-any.whl

$ /PATH/TO/build-venv/bin/python -m pip wheel --no-deps --no-build-isolation --wheel-dir /PATH/TO/dist packages/pursers tools/wait-bridge
Successfully built pursers pursers-wait-bridge

$ /PATH/TO/build-venv/bin/python tools/verify_publish_wheels.py --wheel-dir /PATH/TO/dist
generator_ok=pursers-5.0.0b1-py3-none-any.whl:setuptools==80.9.0
generator_ok=pursers_central-0.1.0a30-py3-none-any.whl:setuptools==80.9.0
generator_ok=pursers_client-0.1.0a23-py3-none-any.whl:setuptools==80.9.0
generator_ok=pursers_personal-5.0.0b1-py3-none-any.whl:setuptools==80.9.0
generator_ok=pursers_personal_import-5.0.0a3-py3-none-any.whl:setuptools==80.9.0
generator_ok=pursers_wait_bridge-0.1.0a16-py3-none-any.whl:setuptools==80.9.0
publish_wheel_verification=pass wheels=6

$ shasum -a 256 /PATH/TO/dist/*.whl
af2d2722652ce5f952b50d4a50933edc13e12a10a88977c8344bf76b2faf87cd  pursers-5.0.0b1-py3-none-any.whl
197fee2894b1c7b0204f844a4c2030d4a9245489dd8fca61693fca6e09a6a636  pursers_central-0.1.0a30-py3-none-any.whl
e2314191a354ab2ab0d0020c1ef80cc0049909eaf5219fcdff0c0f24847b677e  pursers_client-0.1.0a23-py3-none-any.whl
2946cde056bfc1a6b38c1e7b07ed8b3d71ba1b77a7fb3b4e69f7f81db1839935  pursers_personal-5.0.0b1-py3-none-any.whl
34e7d992dffc7b50560706ecdb4c51a5ce67e50edb5c24ec8de2cd48259ed590  pursers_personal_import-5.0.0a3-py3-none-any.whl
102d6eb35daae393c8fef589c8b3e60b02ab985d01856f64afb99ba3ba94834f  pursers_wait_bridge-0.1.0a16-py3-none-any.whl

$ .venv/bin/python -m pip list --format=freeze | rg '^pursers'
pursers==5.0.0b1
pursers-central==0.1.0a30
pursers-client==0.1.0a23
pursers-personal==5.0.0b1
pursers-personal-import==5.0.0a3
pursers-wait-bridge==0.1.0a16

$ .venv/bin/pursers-personal --json setup --project /PATH/TO/project --profiles-root /PATH/TO/profiles --port 44321 --host-id claude-desktop --session owner --host-config /PATH/TO/claude_desktop_config.json --launch-agents-dir /PATH/TO/launch-agents --apply
{
  "central_initialization_required": true,
  "host_restart_required": true,
  "product_version": "5.0.0b1",
  "status": "applied"
}

$ .venv/bin/python -c "from pursers_central.pursers_central_runtime import main; main()" --data-dir /PATH/TO/data --host 127.0.0.1 --port 44321
INFO:     Started server process [PID]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:44321 (Press CTRL+C to quit)

$ curl --fail --silent http://127.0.0.1:44321/healthz
{"status":"ok","store_backend":"sqlite","board_count":0,"journal_head":0,"active_subscription_streams":0,"principal_stream_cap":32,"boards":{},"uptime_seconds":3.651,"last_error_class":null,"open_file_descriptors":13,"soft_file_descriptor_limit":1048576,"file_descriptor_pressure":0.0}

$ board_onboard(board_id="BOARD_ID", agent_name="owner-live-check", role="worker", allow_takeover=true, capabilities={"can_work":false,"can_review":false,"tier_max":2})
{"ok":true,"board_id":"BOARD_ID","agent_name":"owner-live-check","membership_role":"admin","agent_id":"AI-REDACTED","principal_id":"PR-REDACTED"}

$ board_review_policy_set(board_id="BOARD_ID", agent_name="owner-live-check", review_policy="strict")
{"ok":true,"review_policy":"strict"}

$ board_invite(board_id="BOARD_ID", agent_name="owner-live-check", role="member", ttl_s=3600)
{"ok":true,"invite_id":"IV-REDACTED","invite_token":"REDACTED","role":"member","single_use":true}

$ pursers-door issue --board BOARD_ID --role worker --central-url http://127.0.0.1:44321/mcp --issuer http://127.0.0.1:44321/personal-issuer/PROFILE_ID --jwks /PATH/TO/credential.jwks.json --keys-dir /PATH/TO/door-keys > /PATH/TO/worker.door
$ board_member_add(board_id="BOARD_ID", agent_name="owner-live-check", principal_id="PR-REDACTED", role="member")
{"ok":true,"principal_id":"PR-REDACTED","role":"member"}

$ pursers-wait-bridge join --name worker-live-check "$(tr -d '\n' < /PATH/TO/worker.door)"
board=BOARD_ID
role=worker
seat_name=worker-live-check
push=yes
verifier=accepted
push_mode=push

$ ticket_create(board_id="BOARD_ID", agent_name="owner-live-check", ticket_id="TK-live-first", title="Verify the Pursers beta setup", unassigned=true, tier=2)
{"ok":true,"ticket_id":"TK-live-first","status":"open","offer":{"agent_name":"worker-live-check","expires_at":"TIMESTAMP"}}

$ a2a_wait(boards=["BOARD_ID"], only_mine=true, timeout_s=1, since_seq={"BOARD_ID":42})
{"reason":"timeout","mode":"push","timed_out":true,"new_seq":{"BOARD_ID":42},"events":[]}

$ ticket_create(board_id="BOARD_ID", agent_name="owner-live-check", ticket_id="TK-live-offer-final-2", title="Verify exact offered wait and claim", assigned_to="worker-live-offer-final-2", tier=2)
{"ok":true,"ticket_id":"TK-live-offer-final-2","status":"open","offer_agent":"worker-live-offer-final-2"}

$ a2a_wait(boards=["BOARD_ID"], only_mine=true, timeout_s=10, since_seq={"BOARD_ID":42})
{"reason":"offer","mode":"poll","timed_out":false,"new_seq":{"BOARD_ID":44},"events":[{"seq":44,"ticket_id":"TK-live-offer-final-2","reason":"offer"}]}

$ ticket_claim(board_id="BOARD_ID", agent_name="worker-live-offer-final-2", ticket_id="TK-live-offer-final-2")
{"ok":true,"ticket_id":"TK-live-offer-final-2","status":"claimed","assigned_name":"worker-live-offer-final-2"}

The live probe used one second for the empty bootstrap and ten seconds for the
immediate offer only to keep the throwaway run bounded. The manual's
steady-state wait remains 180 seconds. The immediate offer was labelled
`mode="poll"`; the claim succeeded with the returned cursor unchanged.

$ npx -y mcp-remote@0.14.0 http://127.0.0.1:44321/mcp --protocol auto --header-file /PATH/TO/private/headers.txt
Connected to remote server using StreamableHTTPClientTransport
Proxy established successfully between local STDIO and remote StreamableHTTPClientTransport
{"claude_mcp_remote":{"ok":true,"protocol":"auto","tool_count":50,"board_id_matches":true}}

$ python /PATH/TO/throwaway/host_connection_probe.py
{"codex_native_http":{"ok":true,"toml_url_matches":true,"token_source":"PURSERS_BOARD_TOKEN","tool_count":50,"board_id_matches":true}}

$ python3 tools/aionui-extension/build.py --output /PATH/TO/pursers-aionui-0.1.0.zip
/PATH/TO/pursers-aionui-0.1.0.zip
$ shasum -a 256 /PATH/TO/pursers-aionui-0.1.0.zip
a94764d0c4864aa347f8ff61f21b181ae9b8f34be30a34a8636c9f64f7e424dd  pursers-aionui-0.1.0.zip

$ AIONUI_EXTENSIONS_PATH=/PATH/TO/extensions /PATH/TO/aioncore --host 127.0.0.1 --port 44322 --data-dir /PATH/TO/aion-data --app-version 2.2.1 --local
AIONCORE_LISTENING {"host":"127.0.0.1","port":44322}
AIONCORE_READY

$ python /PATH/TO/throwaway/aion_extension_http_probe.py
200 /api/extensions pursers=0.1.0
200 /api/extensions/pursers/assets/webui/index.html
200 /api/extensions/pursers/assets/webui/candidate.json
{"candidate_commit":"28f81308d1cf3d40c4ed38091cc02d9c7d0827aa","schema_version":1}

$ python /PATH/TO/throwaway/aionui_helper_probe.py
{"aionui_helper":{"ok":true,"board_id_matches":true,"central":"throwaway","transport":"authenticated_loopback_helper","core_version":"0.2.1"}}

$ curl --silent --show-error --write-out '\nHTTP %{http_code}\n' --request POST http://127.0.0.1:44321/mcp --header 'Accept: application/json, text/event-stream' --header 'Content-Type: application/json' --header 'Authorization: Bearer redacted' --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28","capabilities":{},"clientInfo":{"name":"getting-started-check","version":"1.0"}}}'
{"error": "invalid_token", "error_description": "Authentication required"}
HTTP 401

$ python3 tools/aionui-extension/build.py
dist/pursers-aionui-0.1.0.zip
```

The transcript exercises the candidate wheels, Central lifecycle, real wait and
claim path, Claude's `mcp-remote` adapter, Codex-compatible native HTTP
transport, and the AionUi-bundled AionCore plus packaged helper. It is still not
a substitute for verifier-owned GUI acceptance in Claude Desktop, Codex, or
AionUi.
