# Add agents to a board

This guide starts with a local Central profile created by `pursers-central
init` and adds three authenticated principals to its board:

- a worker that can claim and submit work;
- an independent reviewer that can approve or reject submissions; and
- a coordinator that can manage workflow and coordination operations.

The examples use file-based credentials throughout. A JWT or `prs1.…` door is
a secret: do not put one in a command argument, source file, checked-in host
configuration, ticket, or log.

This procedure was verified on 2026-09-19 against the 5.0.0 source tree and a
fresh throwaway Central instance on a non-default loopback port.

## Roles, membership, and scopes

Three different concepts are involved:

- A **principal** is the authenticated identity derived from a signed
  credential. Independent review is enforced at this level.
- An **agent name** is the stable name of one seat, such as `worker-1`. It is
  not a credential and does not create a new principal.
- A **board membership role** controls how a principal belongs to a board.
  Membership roles are `admin`, `member`, and `reviewer`. The `role` passed to
  `board_onboard` selects the seat's runtime role: `worker`, `reviewer`, or
  `coordinator`.

The authorization scopes are:

| Scope | What it permits |
| --- | --- |
| `board:read` | Read board state and use board event and wait surfaces. |
| `board:write` | Create, claim, update, and submit tickets. Administrative membership changes also require this scope and an `admin` membership. |
| `board:review` | Claim reviews and record approve or reject verdicts. Under the default strict policy, the principal must also have `reviewer` membership. |
| `board:coordinate` | Use coordinator-only workflow controls, such as policy, routing, assignment, and human-resolution operations. The principal still needs board membership. |
| `board:intake` | Create unassigned intake tickets with a coordinator operation key. It is intended for a separate, write-less intake credential, not for an ordinary worker. |

Use these minimum combinations for the three seats in this guide:

| Seat | Credential scopes | Membership | `board_onboard` role |
| --- | --- | --- | --- |
| Worker | `board:read board:write` | `member` | `worker` |
| Reviewer | `board:read board:review` | `reviewer` | `reviewer` |
| Coordinator | `board:read board:write board:coordinate` | `member` | `coordinator` |

The reviewer must use a different credential and principal from the worker.
Changing only `agent_name` does not make a review independent: two names that
authenticate with the same credential still have the same principal.

## 1. Create the quickstart instance

Install Pursers in a dedicated virtual environment, then initialize a private
profile. First ask the operating system for a currently free loopback port:

```bash
python3 - <<'PY'
import socket

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
```

Record the printed number and use that same value in every terminal. The
verified run printed `63821`, so that value is used below. Port selection has
a small race between releasing the check socket and starting Central; if
`pursers-central run` reports that the address is already in use, select a new
port and initialize a new throwaway profile with it.

```bash
python3 -m venv /PATH/TO/pursers-venv
. /PATH/TO/pursers-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install pursers

INSTANCE=/PATH/TO/quickstart
PORT=63821
BOARD=quickstart
pursers-central init "$INSTANCE" --port "$PORT" --board "$BOARD"
pursers-central run "$INSTANCE"
```

Run Central in its own terminal or process manager. `init` creates
`admin.jwt`, `worker.jwt`, `jwks.json`, and `profile.env`. The generated
`admin.jwt` and `worker.jwt` intentionally authenticate as the same initial
owner principal. They are useful for the first connection, but they cannot
provide an independent reviewer.

In another terminal, define the port again and confirm health without reading
a credential:

```bash
PORT=63821
curl --fail --silent "http://127.0.0.1:${PORT}/healthz"
```

Read these non-secret values from `profile.env` and use them exactly:

```text
CENTRAL_JWT_AUDIENCE=http://127.0.0.1:63821/mcp
CENTRAL_JWT_ISSUER=http://127.0.0.1:63821/quickstart
PURSERS_BOARD_ID=quickstart
```

Do not infer the issuer from the MCP URL. A quickstart issuer includes the
board path; a door signed with a different issuer will fail authentication.

## 2. Install the door issuer

The `pursers` meta-package does not install `pursers-door`. Install the
optional Wait Bridge package in the same environment:

```bash
python -m pip install pursers-wait-bridge
command -v pursers-door
command -v pursers-wait-bridge
```

`pursers-door` adds public verification keys to the instance JWKS and stores
private RSA keys under `--keys-dir`. It does not add the resulting principal
to a board; membership is a separate administrator action.

Create a private credential directory:

```bash
PRIVATE=/PATH/TO/private/agent-credentials
mkdir -p "$PRIVATE/door-keys"
chmod 700 "$PRIVATE" "$PRIVATE/door-keys"
```

The following commands show every value required by a quickstart instance.
They redirect each secret door to a mode-`0600` file instead of printing it:

```bash
CENTRAL_URL=http://127.0.0.1:63821/mcp
ISSUER=http://127.0.0.1:63821/quickstart
JWKS=/PATH/TO/quickstart/jwks.json
KEYS_DIR=/PATH/TO/private/agent-credentials/door-keys

(umask 077; pursers-door issue \
  --board quickstart \
  --role worker \
  --central-url "$CENTRAL_URL" \
  --issuer "$ISSUER" \
  --jwks "$JWKS" \
  --keys-dir "$KEYS_DIR" \
  > /PATH/TO/private/agent-credentials/worker.door)

(umask 077; pursers-door issue \
  --board quickstart \
  --role reviewer \
  --central-url "$CENTRAL_URL" \
  --issuer "$ISSUER" \
  --jwks "$JWKS" \
  --keys-dir "$KEYS_DIR" \
  > /PATH/TO/private/agent-credentials/reviewer.door)

(umask 077; pursers-door issue \
  --board quickstart \
  --named \
  --sub quickstart-coordinator \
  --scope 'board:read board:write board:coordinate' \
  --central-url "$CENTRAL_URL" \
  --issuer "$ISSUER" \
  --jwks "$JWKS" \
  --keys-dir "$KEYS_DIR" \
  > /PATH/TO/private/agent-credentials/coordinator.door)
```

The Wait Bridge's `join` command currently accepts a door only as a positional
argument, which can expose it through process inspection and shell history.
For file-only handling, extract each embedded bearer credential into another
private file without printing it:

```bash
python3 - /PATH/TO/private/agent-credentials <<'PY'
import base64
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
for name in ("worker", "reviewer", "coordinator"):
    door = (root / f"{name}.door").read_text(encoding="utf-8").strip()
    encoded = door.split(".", 1)[1]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    destination = root / f"{name}.jwt"
    destination.write_text(payload["t"] + "\n", encoding="utf-8")
    destination.chmod(0o600)
PY
```

This extraction is the current file-based workaround for the named
coordinator credential. A `named` door is not a worker or reviewer door and
cannot be stored by `pursers-wait-bridge join` on this release.

## 3. Determine and add the three principals

Central derives a principal ID from the credential's public `client_id`,
issuer, and subject. The following command uses only the non-secret values
already supplied to `pursers-door`:

```bash
python3 - <<'PY'
import hashlib
import json

issuer = "http://127.0.0.1:63821/quickstart"
identities = {
    "worker": ("door-quickstart-worker", "door:quickstart:worker"),
    "reviewer": ("door-quickstart-reviewer", "door:quickstart:reviewer"),
    "coordinator": ("quickstart-coordinator", "quickstart-coordinator"),
}
for name, (client_id, subject) in identities.items():
    canonical = json.dumps([client_id, issuer, subject], separators=(",", ":"))
    print(f"{name}: PR-{hashlib.sha256(canonical.encode()).hexdigest()}")
PY
```

Principal IDs are identifiers, not credentials. It is safe to use the printed
IDs in membership operations.

Connect an MCP client with `/PATH/TO/quickstart/admin.jwt`, call
`board_onboard` as a stable owner seat, and then call `board_member_add` three
times. Replace each `PR-…` placeholder with the corresponding result above:

```json
{
  "board_id": "quickstart",
  "agent_name": "board-owner",
  "role": "worker",
  "allow_takeover": true,
  "capabilities": {
    "can_work": true,
    "can_review": false,
    "tier_max": 2,
    "max_parallel": 1
  }
}
```

```json
{"board_id":"quickstart","agent_name":"board-owner","principal_id":"PR-WORKER","role":"member"}
```

```json
{"board_id":"quickstart","agent_name":"board-owner","principal_id":"PR-REVIEWER","role":"reviewer"}
```

```json
{"board_id":"quickstart","agent_name":"board-owner","principal_id":"PR-COORDINATOR","role":"member"}
```

The first object is the `board_onboard` input. The next three objects are
`board_member_add` inputs.

`board_invite` is an alternative for a worker or reviewer when the receiving
MCP client can pass the returned single-use `invite_token` directly to
`board_join` without logging it. An invite can create only `member` or
`reviewer` membership, expires, and is consumed once. A `coordinator` join
cannot consume an invite, so direct `board_member_add` is the consistent path
for all three principals in this guide.

## 4. Start the seats

Configure each host or Wait Bridge process to read its JWT file. For the Wait
Bridge, provide these values as private process environment rather than as
checked-in configuration:

```text
ONBOARD_CENTRAL_URL=http://127.0.0.1:63821/mcp
ONBOARD_BOARD_ID=quickstart
ONBOARD_CENTRAL_TOKEN_FILE=/PATH/TO/private/agent-credentials/worker.jwt
ONBOARD_AGENT_NAME=worker-1
PURSERS_ROLE=worker
PURSERS_CAN_WORK=true
PURSERS_CAN_REVIEW=false
```

The reviewer uses `reviewer.jwt`, `ONBOARD_AGENT_NAME=reviewer-1`,
`PURSERS_ROLE=reviewer`, `PURSERS_CAN_WORK=false`, and
`PURSERS_CAN_REVIEW=true`. The coordinator uses `coordinator.jwt`,
`ONBOARD_AGENT_NAME=coordinator-1`, `PURSERS_ROLE=coordinator`, and both work
and review capabilities set to `false`.

Call `board_onboard` from each connection and verify the returned values:

- `worker-1`: role `worker`, the worker principal, `can_work=true`;
- `reviewer-1`: role `reviewer`, the reviewer principal,
  `can_review=true`; and
- `coordinator-1`: role `coordinator`, the coordinator principal.

Reuse each stable `agent_name` after restarts. If you intentionally reconnect
the same seat while Central still considers it active, use
`allow_takeover=true`. Never use takeover to borrow another seat's name.

## 5. Prove independent review

Use synthetic content on the local board:

1. Create a ticket assigned to `worker-1`.
2. From the worker connection, call `ticket_claim` and then `ticket_submit`.
3. From the worker connection, attempt `ticket_review` with verdict `approve`.
   Central must refuse it. A worker door lacks `board:review`; even a
   review-capable credential cannot independently review a submission made by
   the same principal.
4. From `reviewer-1`, call `ticket_review_claim`, then call `ticket_review`
   with verdict `approve` and meaningful review notes.
5. Read the ticket and confirm `status=closed`, `review_verdict=approve`, and
   different submitted and reviewed principal IDs.

The verified throwaway run produced these secret-free results:

```text
worker join:      role=worker      principal=PR-(worker)
reviewer join:    role=reviewer    principal=PR-(reviewer)
coordinator join: role=coordinator principal=PR-(coordinator)
worker submit:    status=submitted
worker review:    denied: authenticated principal lacks board:review authorization
reviewer claim:   reviewer_agent_name=reviewer-1
reviewer verdict: status=closed verdict=approve
```

## Change or remove access

`board_member_set_role` changes a principal's membership without changing its
credential scopes. The new role must be `admin`, `member`, or `reviewer`.
Changing membership does not add a missing OAuth scope, so both layers must
still agree. Central refuses to demote the last board administrator.

Use `agent_retire` when one named seat is no longer active but the principal
should remain a board member. Retirement affects the seat identity, not every
seat that shares the credential.

Use `board_member_remove` as a board administrator to remove the whole
principal. It removes that principal's seats from the board and revokes its
outstanding targeted invites while preserving tickets and other durable board
data. Central refuses to remove the last administrator.

## Rotate or revoke a door

Rotation writes a new private key, replaces the public key in the JWKS, and
therefore revokes tokens signed by the prior key. Redirect the replacement
door to a private file, extract a new JWT as shown above, update the seat's
token file, and restart that seat:

```bash
(umask 077; pursers-door rotate \
  --board quickstart \
  --role worker \
  --central-url http://127.0.0.1:63821/mcp \
  --issuer http://127.0.0.1:63821/quickstart \
  --jwks /PATH/TO/quickstart/jwks.json \
  --keys-dir /PATH/TO/private/agent-credentials/door-keys \
  > /PATH/TO/private/agent-credentials/worker.door.next)
```

Use the same command with `--role reviewer` for the reviewer. For the
coordinator, replace `--role worker` with:

```text
--named --sub quickstart-coordinator --scope 'board:read board:write board:coordinate'
```

List only non-secret door metadata:

```bash
pursers-door list --jwks /PATH/TO/quickstart/jwks.json
```

To revoke one current key without replacement, copy its non-secret `kid` from
that listing and run:

```bash
pursers-door revoke-kid KID --jwks /PATH/TO/quickstart/jwks.json
```

Revocation stops future authentication but does not by itself remove board
membership. Use `board_member_remove` as well when the principal should no
longer belong to the board.

## Troubleshooting

### Authentication fails immediately

Check the exact `CENTRAL_JWT_AUDIENCE` and `CENTRAL_JWT_ISSUER` in the generated
`profile.env`. The `--central-url` value must equal the audience, while
`--issuer` must equal the issuer. Do not print or decode the bearer token while
diagnosing the mismatch.

### Joining reports that the principal is not a board member

Issuing a door creates a credential, not membership. Recompute the principal
ID from the same issuer, subject, and client ID, then have an administrator
call `board_member_add` with the intended membership role.

### A reviewer has `board:review` but still cannot review

Under the default strict review policy, the principal also needs `reviewer`
membership. Confirm both the credential scope and the membership returned by
`board_members`. Also confirm that the reviewer principal differs from the
submission principal.

### A coordinator cannot join

A coordinator needs `board:coordinate`, an existing `member` or `admin`
membership, and `role=coordinator` on `board_onboard`. A coordinator cannot use
an invite to create its own membership.
