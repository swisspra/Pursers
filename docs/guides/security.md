# Security guide

Pursers Central protects a board with signed credentials, permission scopes,
and board membership. Those controls do not replace operating-system security.
Run Central and its clients under an account you trust, keep credential files
private, and keep the service on loopback unless you have deliberately built a
private remote-access path.

## Trust model

Every Central request must carry an RS256 JWT. Central reads its JWKS on every
verification, selects exactly one RSA signing key by the JWT's `kid`, and checks
the signature, issuer (`iss`), exact audience (`aud`), resource, subject,
valid-from time, expiry, and scopes. Removing a public key from the JWKS therefore
revokes tokens signed by that key on their next request.

After verifying the JWT, Central derives a principal from the token's
`client_id`, issuer, and subject. Authorization then has three layers:

1. The token needs the scope required by the operation, such as `board:read`,
   `board:write`, `board:review`, or `board:coordinate`.
2. The principal must be an admitted member of the board. Admission is
   invite-only after an unbound administrator creates the board.
3. A board-bound token can access only the board named in its signed
   `pursers_board` claim or trusted door-key metadata. A board-bound worker
   credential cannot create a new board.

Membership roles and token scopes are separate checks. Giving a token a broad
scope does not add its principal to a board, and adding a principal to a board
does not add missing scopes to its token.

This model does **not** protect Central from:

- the local OS account that owns its data and credentials;
- another process running as that same OS account;
- disclosure through a command line, environment dump, log, terminal history,
  backup, or copied configuration; or
- network eavesdropping when plain loopback HTTP is moved onto another network.

The default quickstart uses HTTP on `127.0.0.1`. JWT authentication still
applies, but HTTP itself provides no encryption. Do not expose that listener on
a LAN or the public internet.

## Credential files and lifetimes

`pursers-central init` creates its instance directory and data directory with
mode `0700`. It creates `profile.env`, `signing-key.pem`, `jwks.json`,
`admin.jwt`, and `worker.jwt` with mode `0600`. The JWKS contains public key
material, but the quickstart still keeps it private because it also describes
the active trust configuration.

The generated quickstart tokens last 365 days. A door token lasts 180 days by
default; `pursers-door issue` and `pursers-door rotate` accept `--exp-days` to
set a shorter lifetime. Expiry is a backstop, not a substitute for revocation.
Use the shortest lifetime that your renewal process can reliably support.

Door RSA private keys under `--keys-dir` are `0600`. Central's
`request-state.keys` file is also `0600`; it protects resumable multi-round MCP
request state and is not a bearer token.

### Put tokens in files, not argv or environment values

Command arguments are visible to process-listing tools. Environment values can
be inherited by child processes and may appear in diagnostics or crash reports.
Store a bearer token in a `0600` file and configure clients with the file path:

```text
ONBOARD_CENTRAL_TOKEN_FILE=/PATH/TO/private/worker.jwt
```

Do not set `ONBOARD_CENTRAL_TOKEN` to the token value, and do not paste a JWT or
complete `prs1.…` door string into a command. A door string contains a bearer
token and must be handled as a secret.

Check a running installation without printing any credential:

```bash
/bin/ps -axo pid=,command= | grep '[p]ursers'
```

The output may contain paths such as `pursers-central run
/PATH/TO/private/instance`; it must not contain a JWT, `Bearer` value, or
`prs1.…` string. If it does, stop that process, remove the value from its launch
configuration and shell history, and follow the leak runbook below.

## Local Central

Choose a free loopback port. This example uses `43127`:

```bash
python3 -m venv /PATH/TO/venv
/PATH/TO/venv/bin/python -m pip install pursers-central
/PATH/TO/venv/bin/pursers-central init /PATH/TO/private/instance \
  --port 43127 --board example-board
/PATH/TO/venv/bin/pursers-central run /PATH/TO/private/instance
```

In another terminal, check the unauthenticated loopback health endpoint:

```bash
curl --fail --silent http://127.0.0.1:43127/healthz
```

A healthy response includes `"status":"ok"` and
`"store_backend":"sqlite"`. The health endpoint does not require a token and
does not expose one.

## Remote access and TLS

For a direct remote deployment, all of these values must agree:

- `--tls-certfile` and `--tls-keyfile` are supplied together;
- the certificate covers the hostname clients use;
- `--allowed-host` names that exact host;
- `CENTRAL_JWT_AUDIENCE` is the exact public MCP URL; and
- every client uses that URL and trusts the issuing CA, for example through an
  `SSL_CERT_FILE=/PATH/TO/private/ca.pem` setting supported by its launcher.

The intended shape for a tailnet-only service is:

```bash
export CENTRAL_JWT_ISSUER='https://central.example.ts.net'
export CENTRAL_JWT_AUDIENCE='https://central.example.ts.net:43127/mcp'
export CENTRAL_JWKS_PATH=/PATH/TO/private/jwks.json

pursers-central \
  --host 100.64.0.10 \
  --port 43127 \
  --data-dir /PATH/TO/private/central-data \
  --tls-certfile /PATH/TO/private/central.crt \
  --tls-keyfile /PATH/TO/private/central.key \
  --allowed-host central.example.ts.net
```

Bind only the machine's Tailscale address, restrict the port with tailnet ACLs
and the host firewall, and do not add a router port-forward or public DNS record.
Client launchers then need the public URL, a token **file** path, and, for a
private CA, its CA file:

```text
ONBOARD_CENTRAL_URL=https://central.example.ts.net:43127/mcp
ONBOARD_CENTRAL_TOKEN_FILE=/PATH/TO/private/worker.jwt
SSL_CERT_FILE=/PATH/TO/private/ca.pem
```

**Not verified on this release:** the shipped `pursers-central` CLI accepts the
TLS and allowed-host options, but the tested runtime still refuses a non-loopback
`--host` with `ValueError: Personal Central host must be loopback`. Do not deploy
the direct-binding example until that restriction is removed and the complete
TLS path passes your own connection test.

The currently supported remote workaround is to leave Central on
`127.0.0.1` and carry that loopback connection through a private SSH tunnel over
Tailscale:

```bash
ssh -N -L 43127:127.0.0.1:43127 central-user@central.example.ts.net
```

Connect the client to `http://127.0.0.1:43127/mcp` on the client machine. The
SSH and Tailscale layers protect the remote hop, Central is never publicly
bound, and the token's audience remains the loopback URL. This tunnel example
was not run in the release check because no disposable tailnet host was
available.

## Issuer keys and door keys

These keys solve different problems:

- The **issuer key** signs ordinary Central credentials such as the quickstart
  administrator and worker tokens. Retiring it affects every token whose `kid`
  names that key.
- A **door key** signs one board- and role-specific door credential. Its public
  JWK includes trusted board metadata. Revoking it affects only tokens signed by
  that door key.

Use the [issuer-key rotation runbook](../operations/issuer-key-rotation.md) for
the zero-downtime `pursers-central rotate-key` and `retire-key` sequence. Keep
old and new public keys in the JWKS during the overlap, distribute replacement
token files, verify the new credentials, and only then retire the old `kid`.

**Not verified on this release:** the linked sibling runbook and the
`rotate-key`/`retire-key` subcommands were not present in the tested checkout.
Do not substitute `pursers-central init --force`; that replaces the quickstart
credential set immediately and is not a verified zero-downtime rotation.

Door rotation is available now. It creates a new versioned key and atomically
removes the previous door public key, so the old door stops working immediately.
Coordinate the consumer update before running it. Because the command prints a
secret door string, redirect it straight into a private file:

```bash
umask 077
pursers-door rotate \
  --board BOARD \
  --role worker \
  --central-url https://central.example.ts.net:43127/mcp \
  --issuer https://central.example.ts.net \
  --jwks /PATH/TO/private/jwks.json \
  --keys-dir /PATH/TO/private/door-keys \
  --exp-days 30 \
  > /PATH/TO/private/new-worker.door

pursers-door list --jwks /PATH/TO/private/jwks.json
```

Use `--role reviewer` for a reviewer door. To revoke one door without issuing a
replacement, obtain its non-secret `kid` from `pursers-door list`, then run:

```bash
pursers-door revoke-kid KID --jwks /PATH/TO/private/jwks.json
```

Central reloads the JWKS for every verification, so removal takes effect on the
next request. `list` prints only board, role, `kid`, and expiry metadata.

## Request-state keyring rotation

Central creates `request-state.keys` in its private data directory, or uses the
path in `CENTRAL_REQUEST_STATE_KEY_FILE`. Each non-empty line is a key of at
least 32 bytes. The first line seals new state; every line can unseal existing
state.

For a rolling rotation, deploy these contents in order:

1. `[OLD, NEW]`
2. `[NEW, OLD]`
3. after every process has used step 2 for at least the 3600-second request-state
   TTL, `[NEW]`

See the [Central request-state keyring notes](../../packages/central/README.md#quickstart).
Never store this keyring in the repository.

## “I think a token leaked”

1. Do not paste the suspected value into a ticket, chat, log, or command line.
   Record only its credential type and non-secret `kid` if known.
2. Contain access. If you know the principal, an administrator can remove its
   board membership while you rotate credentials. This preserves the journal.
3. Revoke the right key:
   - leaked door string or door token: `pursers-door revoke-kid KID`;
   - leaked ordinary token: follow the issuer-key runbook, replace affected
     token files, and retire its issuer `kid` after the overlap;
   - leaked issuer private key: treat every token signed by it as compromised,
     rotate immediately, replace all affected credentials, then retire it; or
   - leaked request-state key: rotate the request-state keyring and retain the
     old key only for the 3600-second unseal window.
4. Restart or reload every consumer that cached a replaced credential, then
   verify the revoked credential is rejected and the replacement works.
5. Review what the principal did. Use `board_catchup` with a read-capable admin
   seat, `ack=false`, and `touch=false`; page from a known safe cursor and inspect
   the durable events for the principal ID and its agent IDs:

   ```text
   board_catchup(
     board_id="BOARD_ID",
     agent_name="ADMIN_AGENT_NAME",
     cursor=KNOWN_SAFE_CURSOR,
     limit=100,
     ack=false,
     touch=false
   )
   ```

   Continue from each returned `next_cursor`. Correlate fields such as `actor`,
   `caller_principal_id`, `target_principal_id`, and the ticket or memory
   reference. If the response says resynchronization is required, record the
   compaction boundary: events older than retained journal history cannot be
   recovered from `board_catchup` alone.
6. Preserve the incident timeline and the non-secret principal, agent, board,
   `kid`, and journal IDs. Never preserve the leaked credential itself in the
   board.

## Personal and dashboard boundaries

Pursers Personal binds Central to `127.0.0.1`, uses randomized profile ports,
and does not provide pinned loopback TLS or a Unix-domain socket. Treat the
local OS account and all processes running as that account as inside the trust
boundary. For remote seats, use a separate Central deployment or the private
tunnel workaround above; do not republish Personal's listener.

The Fleet Dashboard also refuses any bind host other than `127.0.0.1`.
Administrative requests require a loopback `Host`, and browser requests with an
`Origin` header must be same-origin. The page does not turn the dashboard into
a remote administration surface. A malicious process running as the same OS
account remains inside the boundary and may be able to reach local services or
read that account's files.

## Release verification

Checked against the repository build on 2026-09-19 using a private throwaway
directory and free loopback port `43127`:

- `pursers-central init` completed and created the instance files as `0600` and
  directories as `0700`;
- `pursers-central run` started on `127.0.0.1:43127` and `/healthz` returned
  `status=ok` and `store_backend=sqlite`;
- `/bin/ps` showed only executable and instance paths for the tested Central
  process, with no token in argv;
- `pursers-door issue`, `rotate`, `list`, and `revoke-kid` completed without
  exposing the generated door strings; and
- `request-state.keys` was created with mode `0600`.

The direct remote bind, a real TLS client connection, a real Tailscale tunnel,
and issuer-key rotation were not verified for the reasons stated above.
