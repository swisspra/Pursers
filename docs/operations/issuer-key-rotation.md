# Rotate the Central issuer key

Central signs every operator, worker, reviewer, and coordinator JWT with one
issuer private key. Central publishes the corresponding public keys in its
JWKS. A JWT's `kid` selects one of those public keys. Because Central reloads
the JWKS for every request, an old and a new issuer key can overlap without a
Central restart or an interruption to board membership, tickets, or leases.

Rotate the issuer key when you suspect it was copied or exposed, when staff or
operators change, when a deployment is forked or handed over, and before
issued tokens expire. For routine hygiene, rotate at least every 90 days and
after every custody change. Quickstart tokens start with a 365-day lifetime.
Rotation preserves each token's original lifetime and starts that lifetime
again at the rotation time; deployments with custom tokens keep their custom
lifetime in the same way.

Issuer keys are not door keys. Rotate door keys separately in the Fleet
dashboard's **Doors** panel. `rotate-key` preserves every door-key JWKS entry.

## Zero-downtime quickstart procedure

The quickstart form reads `signing-key.pem`, `jwks.json`, `admin.jwt`, and
`worker.jwt` from one instance directory. Before rotating, keep private backup
copies of the current token files so that you can roll clients back during the
overlap window:

```bash
install -d -m 700 /PATH/TO/private/issuer-rotation-backup
install -m 600 /PATH/TO/pursers-local/admin.jwt /PATH/TO/private/issuer-rotation-backup/admin.jwt
install -m 600 /PATH/TO/pursers-local/worker.jwt /PATH/TO/private/issuer-rotation-backup/worker.jwt
pursers-central rotate-key /PATH/TO/pursers-local
```

The command prints the old and new kids, paths, and token count. It never
prints a token or private-key value. It moves the old private key to
`signing-key.OLD_KID.retired.pem`, creates a new `signing-key.pem`, keeps both
public issuer keys in `jwks.json`, and re-signs both token files. All private
files remain mode `0600`.

Distribute the new token files to every client through your private credential
channel. Restart each client, or use its documented credential-reload action.
Do not retire the old key yet.

From every client, call `board_status` and confirm that the expected board,
agent identity, principal, role, and capabilities are present. Alternatively,
watch the Central log while each known client reconnects and confirm that
there are no authentication failures. Account for every client before moving
on.

Use a protected copy of an old token as a retirement guard. Set `OLD_KID` to
the exact old kid printed by `rotate-key`, then retire it:

```bash
OLD_KID='REPLACE_WITH_THE_EXACT_OLD_KID'
pursers-central retire-key /PATH/TO/pursers-local \
  --kid "$OLD_KID" \
  --check-token /PATH/TO/pursers-local/admin.jwt \
  --check-token /PATH/TO/pursers-local/worker.jwt
```

`retire-key` refuses to proceed if a checked token still carries `OLD_KID`. It
also refuses to remove a door key, the current signing key, or the last issuer
key. On success it removes the old public key atomically and deletes the
matching retired private key.

Delete the private backup only after the retirement succeeds and the normal
backup-retention policy permits it:

```bash
rm /PATH/TO/private/issuer-rotation-backup/admin.jwt
rm /PATH/TO/private/issuer-rotation-backup/worker.jwt
rmdir /PATH/TO/private/issuer-rotation-backup
```

## Generic deployment procedure

For an issuer whose files live outside the quickstart layout, name the current
private key, JWKS, and every token file explicitly. A token file may contain a
bare JWT or one `Authorization: Bearer JWT` header; the command preserves that
format.

```bash
pursers-central rotate-key \
  --key /PATH/TO/private/signing-key.pem \
  --jwks /PATH/TO/private/issuer.jwks.json \
  --token /PATH/TO/private/admin.jwt \
  --token /PATH/TO/private/worker.headers
```

Distribute and reload the rewritten token files, then verify every client as
described above. Retire the printed old kid only after all clients have moved:

```bash
OLD_KID='REPLACE_WITH_THE_EXACT_OLD_KID'
pursers-central retire-key \
  --key /PATH/TO/private/signing-key.pem \
  --jwks /PATH/TO/private/issuer.jwks.json \
  --kid "$OLD_KID" \
  --check-token /PATH/TO/private/admin.jwt \
  --check-token /PATH/TO/private/worker.headers
```

The retired private-key filename is derived from the current key path. For
`signing-key.pem`, it is `signing-key.OLD_KID.retired.pem` in the same
directory.

## Roll back before retirement

Before `retire-key`, both public issuer keys remain valid. A client that cannot
use its new token can restore its protected old token file and reconnect; no
Central restart is required. Investigate and repeat distribution before
retiring the old key. After `retire-key`, that rollback is intentionally no
longer possible.

## Hard cutover

Use a hard cutover only when every existing issuer token must stop working
immediately. Re-run `init` with the instance's actual port and board:

```bash
pursers-central init /PATH/TO/pursers-local \
  --port 8766 \
  --board pursers-local \
  --force
```

This replaces the private key, JWKS, profile, and quickstart tokens in one
operation. Every token issued by the old key fails immediately, and connected
clients must receive the new files before they can authenticate again. Unlike
`rotate-key`, this path provides no overlap and does not preserve door-key JWKS
entries.
