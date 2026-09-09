# O1 HTTP-loopback cutover readiness

Audit date: 2026-09-08

## Scope and verdict

This is a read-only audit of the current single-machine deployment and its
AionUI Team consumers. No service, credential, JWKS, membership, registry,
host configuration, or seat identity was changed.

**Verdict: conditional NO-GO.** The released HTTP-loopback target is available
and its public artifacts verify, but activation must wait until all current
claims finish, an operator stages a coherent replacement credential set, and
all eight authoritative AionUI Team seat folders are prepared. The legacy
Fleet Dashboard inventory is incomplete and must not be used as the migration
roster.

## Findings and confirmed baseline

- `origin/main` and tag `v5.0.0a25` resolve to
  `c2ebac5de803a0f7a00468ec4d3cdf06e4719096`.
- The GitHub release published on 2026-09-08 contains six wheels plus
  `SHA256SUMS.txt`. A fresh download passed `shasum -a 256 -c
  SHA256SUMS.txt` for every wheel.
- The active Central health endpoint is healthy over HTTPS and reports
  `pursers-central==0.1.0a29`, the Central component in the a25 train. Plain
  HTTP to that same listener does not respond successfully.
- The active Central listener is loopback `127.0.0.1:8766`, but it is started
  through the predecessor `serve_tls.py` shim with certificate/key inputs.
- The Fleet Dashboard is reachable at `http://127.0.0.1:8899`. Its upstream
  Central connection and the coordinator's `--url` still select the current
  HTTPS resource.
- Current role credentials all have HTTPS `aud`; every inspected token has
  `resource == aud`. The coordinator main token has
  `board:read board:write board:coordinate`; intake has
  `board:read board:coordinate board:intake` and no `board:write`; the
  dashboard credential has `board:read board:write board:review`.
- At audit time `fullplatts` and `mi-mcp-prd` had no active tickets. `pursers`
  had four open unclaimed tickets and this audit ticket was claimed. That is
  not a cutover-safe window.

## URL-bound authentication

The released target URL is exactly:

```text
http://127.0.0.1:8766/mcp
```

For that target:

```text
CENTRAL_JWT_ISSUER=http://127.0.0.1:8766
CENTRAL_JWT_AUDIENCE=http://127.0.0.1:8766/mcp
JWT aud=http://127.0.0.1:8766/mcp
JWT resource=http://127.0.0.1:8766/mcp
```

Central uses strict single-value audience verification and separately requires
`resource` to equal the configured audience exactly. Scheme, hostname, port,
and path are identity-bearing. An HTTPS token cannot authenticate to the HTTP
target, even when both listeners use the same port and data. The issuer change
also changes the derived principal ID because it is computed from
`client_id`, issuer, and subject. Memberships for target principals must
therefore be committed before activation.

Same-machine clients use plain HTTP loopback without a CA file. A remote seat
must forward the remote Central port to its own `127.0.0.1:8766` and still use
the exact HTTP URL above. Do not expose Central directly or distribute a
private CA for this cutover.

```sh
ssh -N -L 8766:127.0.0.1:8766 '<REMOTE_HOST>'
```

## Authoritative AionUI Team inventory

The migration unit is the eight active AionUI Team seat folders below, not the
seven-seat predecessor CLI fleet and not the three-row dashboard inventory.

| Seat | Host | Role | Tier | Current integration |
| --- | --- | --- | ---: | --- |
| `pursers-gemini-goose-1` | Goose | worker | 1 | seat-local `bin/board.sh`, HTTPS |
| `pursers-glm-goose-2` | Goose | worker | 1 | seat-local `bin/board.sh`, HTTPS |
| `pursers-Qwen-goose-3` | Goose | worker | 2 | seat-local `bin/board.sh`, HTTPS |
| `pursers-cli-worker-1` | Codex | worker | 2 | seat-local `bin/board.sh`, HTTPS |
| `pursers-cli-worker-2` | Codex | worker | 2 | seat-local `bin/board.sh`, HTTPS |
| `pursers-cli-worker-3` | Codex | worker | 2 | seat-local `bin/board.sh`, HTTPS |
| `pursers-cli-reviewer-1` | Codex | reviewer | 2 | seat-local `bin/board.sh`, HTTPS |
| `pursers-cli-reviewer-2` | Codex | reviewer | 2 | seat-local `bin/board.sh`, HTTPS |

All eight wrappers select the registry pool and must be migrated together.
The five Codex seat-local `config.toml` files contain no Pursers MCP server;
the current AionUI MCP server registry also contains no Pursers entry. AionUI
Team supplies the runtime and the seat folders supply the board connector.
The Team lead is a control-plane identity, not a ninth worker/reviewer seat.

Additional migration targets:

| Target | Required change |
| --- | --- |
| Central launcher/profile | Replace the TLS shim with the released plain-HTTP runtime; update issuer, audience, JWKS, and wheel pins as one activation. |
| Registry access | Preserve the `pursers` home registry and the active `pursers`, `fullplatts`, and `mi-mcp-prd` entries; use the target principals for every active board. |
| Coordinator | Replace both main and intake token files and change `--url` to the exact HTTP resource; preserve active/shadow mode and all other flags. |
| Fleet Dashboard | Change only its upstream Central URL, admin token reference, and CA setting; keep its own UI at `http://127.0.0.1:8899`. |
| Seat folders | Regenerate the managed launcher, `board.py`, `AGENTS.md`, and `.goosehints` from a25 while preserving each row's role/tier/capabilities and per-ticket worktrees. |
| AionUI Team | Pause and resume the two Teams as Teams. Do not start, stop, or revive standalone Goose CLI sessions. |

The existing `seats.json` has only three stale a12-era rows, including a
desktop orchestrator, one Codex worker, and one Codex reviewer. It is evidence
of inventory drift, not proof of the active fleet or current bridge version.

## Public target inputs

The released launcher contract is:

```sh
/PATH/TO/instance/.venv/bin/python \
  -m pursers_central.pursers_central_runtime \
  --host 127.0.0.1 --port 8766 --data-dir /PATH/TO/instance/data
```

The environment must set `CENTRAL_JWT_ISSUER`, `CENTRAL_JWT_AUDIENCE`, and
`CENTRAL_JWKS_PATH`; authentication remains JWT-only, admission remains invite,
and storage remains SQLite. `tools/central_scaffold.py` generates the
loopback-only layout and refuses an occupied port during `check`.

Retain both release checks before any installation:

```sh
gh release download v5.0.0a25 --repo swisspra/Pursers \
  --dir /PATH/TO/release-a25
cd /PATH/TO/release-a25
shasum -a 256 -c SHA256SUMS.txt
cd /PATH/TO/pursers-source
python tools/verify_publish_wheels.py --wheel-dir /PATH/TO/release-a25
```

The verified a25 component digests are:

```text
pursers_central-0.1.0a29-py3-none-any.whl
8defa64200fa53425079408744243e2392ee5ddbffaea14d249dc3766bbfa118

pursers_client-0.1.0a22-py3-none-any.whl
fe5d9a28e16ab5da5b5141798e814fbe68a95489f20ba2835ed0d44f54134cb4

pursers_wait_bridge-0.1.0a15-py3-none-any.whl
faa60203ee6a1857636322def8e5f3c92510c87983cbcd07d2ebd68b9e7d3611
```

## Ordered operator procedure

### A. Additive preparation; current HTTPS remains authoritative

1. **Declare the freeze.** Disable new dispatch and pause both AionUI Teams.
   Do not use `start-all`. Do not stop Central yet.
2. **Drain work.** On each of `pursers`, `fullplatts`, and `mi-mcp-prd`, require
   zero `claimed`, `submitted`, `reviewing`, `in_review`, `rejected`, and
   `needs_human` tickets. Open unclaimed work may remain only while dispatch is
   paused. Let live claims finish normally; never take over, retire, or expire a
   seat to manufacture a quiet window.
3. **Record the baseline.** Save `/healthz`, board/registry summaries, current
   service labels, process start times, listener ownership, and credential/JWKS
   metadata without token contents. Record old and target issuer/audience values
   in the private change record.
4. **Back up one rollback unit.** Copy and hash the Central data directory,
   launcher/profile/plist, active JWKS and private keys, named admin token files,
   coordinator configuration, dashboard configuration/state, all eight seat
   folders, and AionUI Team definitions into `/PATH/TO/backup`. Preserve modes,
   ownership, and timestamps. Do not copy secrets into the repository.
5. **Verify a25 artifacts.** Run both release checks above, then create a fresh
   target venv from the verified Central and client wheels. Record wheel
   filenames, SHA-256 values, Python version, and installed metadata.
6. **Prepare an independent target credential root.** Use
   `/PATH/TO/target-jwt/jwks.json` and `/PATH/TO/target-jwt/door-keys`; do not
   edit or rotate the active HTTPS JWKS. Set the private provisioner's `ISSUER`
   and `AUDIENCE` to the exact HTTP values above.
7. **Mint named control-plane tokens first.** In the operator-private
   `jwt_provision.py`, create separate files for:
   - `coordinator-main`: `board:read board:write board:coordinate`
   - `coordinator-intake`: `board:read board:coordinate board:intake` and never
     `board:write`
   - `dashboard-admin`: `board:read board:write board:review`

   Run it without printing token contents:

   ```sh
   python /PATH/TO/operator-private/jwt_provision.py
   ```

   Require token files and private keys to be regular mode-`0600` files and the
   containing directories to be mode `0700`.
8. **Issue, do not rotate, fleet doors.** Add the home-registry worker and
   reviewer public keys to the staged JWKS:

   ```sh
   umask 077
   pursers-door issue --board pursers --role worker \
     --central-url http://127.0.0.1:8766/mcp \
     --jwks /PATH/TO/target-jwt/jwks.json \
     --keys-dir /PATH/TO/target-jwt/door-keys \
     > /PATH/TO/target-jwt/worker.door

   pursers-door issue --board pursers --role reviewer \
     --central-url http://127.0.0.1:8766/mcp \
     --jwks /PATH/TO/target-jwt/jwks.json \
     --keys-dir /PATH/TO/target-jwt/door-keys \
     > /PATH/TO/target-jwt/reviewer.door

   pursers-door list --jwks /PATH/TO/target-jwt/jwks.json
   ```

   Require both door files to be mode `0600`. Read each door only through the
   approved secret handoff. Do not place a door in terminal output, logs,
   tickets, shell history, or this report.
9. **Commit target memberships while HTTPS is still live.** Derive the target
   principal IDs from each target token's `client_id`, HTTP issuer, and `sub`.
   With the current HTTPS admin credential, call
   `board_member_add(board_id=<BOARD>, principal_id=<TARGET_PRINCIPAL>,
   role=<ROLE>)` for every active board. Control-plane principals use the
   least-privileged membership that supports their duties; the dashboard and
   coordinator recovery principal remain admin, intake remains non-writing,
   the worker-door principal is member, and the reviewer-door principal is
   reviewer. Read back `board_members` after every add.
10. **Prepare all eight seat folders.** For each row in the authoritative table,
    run `tools/seat-kit/seat_new.py --upgrade` with its exact existing name,
    role, tier, client, and the applicable target door. Keep
    `PURSERS_BOARDS=registry`; preserve unrelated files and per-ticket
    worktrees. Save the generated diff under `/PATH/TO/cutover-evidence` and do
    not launch any seat yet.

    ```sh
    python tools/seat-kit/seat_new.py --upgrade --role worker \
      --name pursers-gemini-goose-1 --tier-max 1 --client goose \
      --python /PATH/TO/seat-python --dest /PATH/TO/pursers-gemini-goose-1 \
      --door '<TARGET_WORKER_DOOR>'
    python tools/seat-kit/seat_new.py --upgrade --role worker \
      --name pursers-glm-goose-2 --tier-max 1 --client goose \
      --python /PATH/TO/seat-python --dest /PATH/TO/pursers-glm-goose-2 \
      --door '<TARGET_WORKER_DOOR>'
    python tools/seat-kit/seat_new.py --upgrade --role worker \
      --name pursers-Qwen-goose-3 --tier-max 2 --client goose \
      --python /PATH/TO/seat-python --dest /PATH/TO/pursers-Qwen-goose-3 \
      --door '<TARGET_WORKER_DOOR>'

    python tools/seat-kit/seat_new.py --upgrade --role worker \
      --name pursers-cli-worker-1 --tier-max 2 --client codex \
      --python /PATH/TO/seat-python --dest /PATH/TO/pursers-cli-worker-1 \
      --door '<TARGET_WORKER_DOOR>'
    python tools/seat-kit/seat_new.py --upgrade --role worker \
      --name pursers-cli-worker-2 --tier-max 2 --client codex \
      --python /PATH/TO/seat-python --dest /PATH/TO/pursers-cli-worker-2 \
      --door '<TARGET_WORKER_DOOR>'
    python tools/seat-kit/seat_new.py --upgrade --role worker \
      --name pursers-cli-worker-3 --tier-max 2 --client codex \
      --python /PATH/TO/seat-python --dest /PATH/TO/pursers-cli-worker-3 \
      --door '<TARGET_WORKER_DOOR>'

    python tools/seat-kit/seat_new.py --upgrade --role reviewer \
      --name pursers-cli-reviewer-1 --tier-max 2 --client codex \
      --python /PATH/TO/seat-python --dest /PATH/TO/pursers-cli-reviewer-1 \
      --door '<TARGET_REVIEWER_DOOR>'
    python tools/seat-kit/seat_new.py --upgrade --role reviewer \
      --name pursers-cli-reviewer-2 --tier-max 2 --client codex \
      --python /PATH/TO/seat-python --dest /PATH/TO/pursers-cli-reviewer-2 \
      --door '<TARGET_REVIEWER_DOOR>'
    ```

    Set `PURSERS_BOARDS=registry` in the AionUI Team runtime or keep the Team's
    explicit `bin/board.sh wait --boards registry` contract. Do not accept the
    generated door-mode default of `home` as fleet coverage proof.
11. **Prepare dependent configs without applying them.** Stage coordinator and
    dashboard configs pointing to the HTTP URL and new named token files. Remove
    local `PURSERS_CA_FILE`/`SSL_CERT_FILE` inputs. Stage the released Central
    launcher with the target profile and staged JWKS. Diff every target against
    its backup.

    Preserve all current policy values represented by placeholders:

    ```sh
    python tools/coordinator/coordinator.py \
      --url http://127.0.0.1:8766/mcp \
      --token-path /PATH/TO/coordinator-main.jwt \
      --intake-token-path /PATH/TO/coordinator-intake.jwt \
      --home-board pursers --agent-name '<COORDINATOR_NAME>' \
      --mode '<CURRENT_MODE>' --poll-seconds '<CURRENT_VALUE>' \
      --enable-intake

    python tools/fleet-dashboard/fleet_dashboard.py \
      --port 8899 --url http://127.0.0.1:8766/mcp \
      --token-file /PATH/TO/dashboard-admin.jwt \
      --doors-keys-dir /PATH/TO/target-jwt/door-keys \
      --jwks-path /PATH/TO/target-jwt/jwks.json \
      --home-board pursers --agent-name fleet-dashboard-session-default
    ```

    Append each existing coordinator policy flag unchanged. Produce and review
    the final argv before launch; do not collapse preserved flags into one
    shell argument.
12. **Run sandbox acceptance.** Use a copied/synthetic data root, a different
    loopback port, and separate sandbox credentials whose `aud`/`resource`
    exactly match that sandbox URL. Do not use production-target tokens at the
    sandbox port. Require:
    - HTTP `/healthz` reports `status=ok` and `version=0.1.0a29`;
    - HTTPS to the sandbox listener fails;
    - admin registry reads cover all expected sandbox boards;
    - one worker can join, receive/refetch a cue, claim, renew, submit, and wait;
    - one independent reviewer can review-claim and close;
    - coordinator `--once --dry-run` passes scope checks;
    - dashboard reads succeed without a CA override;
    - `registry_doctor.py --json` and `seat_config.py doctor --json` complete
      with no fleet-critical failure;
    - no credential, private path, or door appears in captured output.

Sandbox success validates the wheel and flow, not the production URL-bound
tokens. Production credentials receive their first end-to-end proof during the
canary activation below.

### B. Activation; this is the token-invalidating boundary

1. Reconfirm the freeze and the zero-held-work gate. Re-run and hash the final
   data backup. Record a single rollback decision owner and timeout.
2. Stop the coordinator, Fleet Dashboard, and both AionUI Teams. Stop the old
   Central last. Confirm no writer remains and port `8766` is free.
3. Atomically activate the target Central profile, plain-HTTP launcher, a25
   venv, target JWKS, and copied data root. Do not mix the old JWKS with new
   tokens or the new JWKS with old tokens.
4. Start Central only. Require:

   ```sh
   curl -fsS http://127.0.0.1:8766/healthz
   ```

   Confirm `status=ok`, `version=0.1.0a29`, expected board counts, SQLite
   backend, and no TLS listener. Also require an HTTPS probe to fail.
5. With the new dashboard admin token, verify `board_list`, `board_members`, and
   the `project_registry` read for all three active boards. Do not continue if
   any target principal lacks its intended membership.
6. Activate the coordinator's HTTP URL plus both new token files and start it.
   Require successful main/intake scope preflight, a fresh heartbeat, and no
   circuit-breaker or `board_unreachable` finding.
7. Activate the Fleet Dashboard upstream HTTP URL and new admin token, clear its
   local CA override, then start it. Keep the dashboard UI itself on
   `http://127.0.0.1:8899`. Confirm Central and coordinator health cards.
8. Start exactly one Codex worker canary from AionUI Team. Verify a new process,
   exact seat name, HTTP resource, target principal, registry visibility, push
   subscription, and a no-write wait timeout.
9. Start one Codex reviewer canary and repeat identity, registry, push, and
   no-work-claim checks. Only then start the remaining two Codex workers, two
   Codex reviewers total, and three Goose workers through AionUI Team.
10. Run:

   ```sh
   python tools/wait-bridge/registry_doctor.py \
     --token-path /PATH/TO/dashboard-admin.jwt --json
   python tools/fleet-dashboard/seat_config.py doctor --json
   ```

   Require eight active authoritative seats with the exact role/tier matrix,
   all three registry boards visible, push mode healthy, no split identity, no
   CA dependency, no stale bridge attributed to a current host, and zero active
   legacy Goose CLI processes.
11. Resume the AionUI Teams and then dispatch. Keep the rollback unit until a
   full worker-submit-independent-review cycle succeeds on the HTTP resource.

## Rollback checkpoints

Rollback immediately for health/version mismatch, missing membership,
authentication or issuer/audience failure, split identity, registry loss,
push failure on either canary role, coordinator scope/circuit failure, data
count mismatch, or any unexpected writer.

1. Pause dispatch and both AionUI Teams; stop coordinator and dashboard.
2. Stop the HTTP Central and confirm port `8766` is free.
3. Restore the old data snapshot, TLS launcher/profile/plist, old JWKS/private
   keys, old named and seat token files, all eight old seat folders, and old
   coordinator/dashboard configs from the same backup unit.
4. Start the old HTTPS Central only and verify its old `/healthz`, version,
   board counts, old issuer/audience, and old principal memberships.
5. Start the old coordinator and dashboard, then one old Codex worker canary
   and one old reviewer canary. Require authenticated registry and push checks.
6. Start the remaining AionUI Team seats and resume dispatch only after Doctor
   passes. Do not start legacy Goose CLI.

Token/JWKS coherence is non-negotiable: restoring only the URL, only token
files, or only JWKS creates a predictable outage. The staged HTTP keys and
memberships may remain dormant after an emergency rollback because the old
HTTPS verifier rejects their issuer/audience; remove them later under a
separate reviewed maintenance change. Never rotate the active key set as part
of preparation or rollback.

## Limitations and stale paths

- `README.md` still says "loopback TLS" in two places. For deployment, the
  newer `docs/deployment-transport.md` and released runtime default are
  authoritative: same-machine Central is plain HTTP loopback.
- The predecessor `serve_tls.py`, `renew_tls.sh`, numbered `01`-`06` cutover
  scripts, and `ARM.md` target the old TLS/a4/dev-auth/31-tool procedure. They
  are evidence only and must not be run for O1.
- `start-all` is permanently disallowed. The three Goose workers are AionUI
  Team seats; standalone Goose CLI lifecycle is not a fallback.
- Existing seat instructions still mention ten-minute renewals and the
  `--poll` compatibility path. O1 operations use the current three-minute
  renewal policy and subscription wait; do not switch to polling to hide a
  migration failure.
- T7 documents an AionUI 2.2.1 extension import route, but the current AionUI
  MCP registry has no Pursers server and there is no verified supported Host
  API that rewrites or hot-reloads all eight running Team seat connectors.
  Therefore the operator must use AionUI Team pause/restart plus the audited
  seat-folder changes. Source tests, mocks, and the extension package are not
  GUI or runtime proof.
- The public scaffold documents an operator-private no-argument
  `jwt_provision.py`, but does not ship that credential generator in this
  repository. Named-token minting remains an authorized private operator step;
  O1 does not invent a public endpoint or expose its configuration.
- The released door CLI is board-labelled. The fleet-wide use above is safe
  only if the resulting home-door principals are explicitly admitted on every
  active registry board and Doctor proves cross-board access before dispatch.
  Otherwise issue per-board doors and keep the credential selection inside the
  supported bridge state; never silently reuse a token on an unverified board.

## Validation required for GO

The coordinator's private change record should contain, without secret values:

- exact a25 tag/commit and verified release hashes;
- backup manifest hashes and restore owner;
- old and target issuer/audience strings;
- named-token and door `kid`/expiry metadata;
- per-board membership read-backs for every target principal;
- zero-held-work snapshot immediately before shutdown;
- sandbox acceptance results;
- production HTTP and negative HTTPS health probes;
- coordinator heartbeat/scope evidence;
- eight-seat identity, role, tier, registry, push, and Doctor results;
- a successful worker-submit-independent-review canary; and
- a timed rollback rehearsal or an explicit operator decision accepting an
  unexercised rollback.
