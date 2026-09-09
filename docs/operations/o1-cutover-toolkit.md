# O1 cutover toolkit: staged prepare / preflight / dry-run / activate / rollback

Status: preparation tooling only. Nothing here activates the cutover, mints a
credential, or touches a live service.

Source of truth: the approved readiness audit `docs/operations/o1-readiness.md`
(ticket `TK-3776ba2309a8`, commit
`47bd5d39f2b75fa166a12948200f71bc665099ed`). This document only describes the
executable form of that runbook; where the two differ, the runbook wins.

Implementation: `tools/o1_cutover.py` (standard library only).
Tests: `tools/tests/test_o1_cutover.py` (part of the `release-tools` suite).

## 1. What the toolkit does and refuses to do

| Responsibility | Toolkit | Operator |
| --- | --- | --- |
| Staging skeleton, private command sheets | yes (`prepare`) | supplies the config |
| Copy + hash the whole rollback unit | yes (`backup`) | chooses the backup root |
| Release digest and launcher hash preflight | yes (`verify-artifacts`) | downloads the release |
| Drain, membership, seat, config, legacy gates | yes (`dry-run`) | produces the snapshots |
| Minting named tokens and doors | no | yes, private `jwt_provision.py` / `pursers-door` |
| Board membership writes | no | yes, with the current admin credential |
| Service and AionUI Team lifecycle | no | yes, from the command sheet |
| Atomic live-file activation | yes (`activate`, gated) | confirms and restarts |
| Reverse-order restore | yes (`rollback`, gated) | confirms and restarts |

Hard refusals built into the tool:

* No service, Team, token, JWKS, seat or registry lifecycle mutation. Steps of
  that kind are emitted as an operator command sheet and stay `execution:
  operator` in the plan.
* No fake hot-reload API. If the Team lifecycle adapter
  (`tools/aionui-extension/team/adapter.cjs`) is absent, `gate_host_capability`
  reports the honest gap (`operator_manual`); if it is declared but missing, the
  gate refuses.
* `start-all` is never referenced, and any staged configuration that mentions it,
  a CA override (`PURSERS_CA_FILE`, `SSL_CERT_FILE`, `SSL_CERT_DIR`,
  `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`) or the `--poll` compatibility path is
  refused.
* Sandbox evidence never satisfies a production URL-bound gate: evidence and
  membership snapshots must name the exact target resource.
* No private path, token body, door body or key material is printed. Reports are
  sanitized to `/PATH/TO/...` placeholders; the unsanitized command sheet is
  written into the private staging root only.

## 2. Private artifact layout

The operator picks one new staging root outside the repository and outside every
live configuration path. The operator config, board/membership/seat evidence,
every staged swap source and every generated output must resolve beneath that
staging root; symlink-parent escapes are refused. Every activation target must
resolve beneath `live_root`, never beneath the repository or staging root.
`backup_root` must be a distinct descendant of the staging root. All eight seat
roots must resolve to distinct canonical directories, and every configured live
swap target must remain unique after canonicalization across launcher, JWKS,
credential, door, dependent-config, and seat-managed files.
`validate_layout` enforces these bounds before prepare, backup, preflight,
dry-run, activation or rollback can write a byte, and also refuses relative,
absolute, traversing, backslash-delimited, colliding or symlink-crossing
rollback identifiers.

```text
/PATH/TO/staging/                 0700
  backup/                         rollback unit copy + manifest.json
  target-jwt/                     staged JWKS, named tokens, doors (0600)
    door-keys/                    staged door key material
  staged-configs/                 staged launcher, profile, coordinator, dashboard, bridge
  staged-seats/NN/                staged managed files for each of the eight seats
  cutover-evidence/               board, membership and seat-baseline snapshots,
                                  dry-run records, operator command sheets
  journal/activation-journal.json the only record of what activation replaced
```

Generated artifacts are mode `0600` inside mode `0700` directories. The
repository receives no generated artifact.

## 3. Operator sequence

All commands run from a checkout of the repository. `--config` points at the
operator-private configuration file (mode `0600`), never at a repository path.

```sh
# 0. Emit a placeholder configuration and fill in the private values.
python tools/o1_cutover.py template > /PATH/TO/staging/o1-cutover.json
chmod 600 /PATH/TO/staging/o1-cutover.json

# 1. Print the ordered plan and its gate requirements (no side effects).
python tools/o1_cutover.py plan

# 2. Create the staging skeleton and the private command sheets.
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json prepare

# 3. Freeze, drain and snapshot (operator actions; the toolkit normalizes the dumps).
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json \
  snapshot-boards \
  --from-json /PATH/TO/board-dump.json \
  --out /PATH/TO/staging/cutover-evidence/board-snapshot.json
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json \
  snapshot-memberships \
  --from-json /PATH/TO/membership-dump.json \
  --out /PATH/TO/staging/cutover-evidence/membership-snapshot.json

# 4. Back up and hash the entire rollback unit, then verify artifacts.
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json backup
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json verify-artifacts

# 5. Non-mutating dry run: proves every target and every gate.
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json dry-run

# 6. Activation (token-invalidating boundary). Operator confirmation is mandatory
#    and the evidence record must be fresh and plan-matching.
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json activate \
  --operator-confirmed \
  --evidence /PATH/TO/staging/cutover-evidence/<FRESH_DRY_RUN_EVIDENCE>.json

# 7. Rollback, if any checkpoint trips.
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json rollback \
  --operator-confirmed

# Rehearsal only: interrupt activation after journalling one step, then roll back.
python tools/o1_cutover.py --config /PATH/TO/staging/o1-cutover.json activate \
  --operator-confirmed --evidence /PATH/TO/staging/cutover-evidence/dry.json \
  --rehearse-fail-at swap-configs
```

Exit codes: `0` success, `1` gate or phase refusal, `2` configuration error.

## 4. Activation preconditions (all must pass)

`dry-run` and `activate` evaluate every gate in `GATES`; absent evidence is a
refusal, never a pass.

| Gate | Refuses when |
| --- | --- |
| `gate_staging` | staging root missing, not `0700`, incomplete, inside the repository, config not `0600`, backup root not a distinct descendant, aliased canonical seat/live targets, or an unsafe/colliding rollback identifier |
| `gate_url` | target is not exactly `http://127.0.0.1:8766/mcp`, previous equals target |
| `gate_artifacts` | a pinned a25 wheel digest differs, staged launcher omits `pursers_central.pursers_central_runtime`, still names `serve_tls`, binds another host/port, profile omits issuer/audience/JWKS or sets a CA override, declared venv interpreter missing, launcher hash preflight mismatch |
| `gate_backup` | manifest absent, member missing, hash or mode drift, incomplete rollback unit, or any activation target drifted since the backup |
| `gate_drain` | snapshot absent/stale, dispatch or Teams not paused, any active board missing, any nonzero `claimed`/`submitted`/`reviewing`/`in_review`/`rejected`/`needs_human` |
| `gate_credentials` | named credential or door missing, wrong mode, world-readable parent, `aud` not single-valued and exact, `resource` != `aud`, issuer mismatch, missing or forbidden scope (`coordinator-intake` must never hold `board:write`), non-empty JOSE-header `kid` missing or absent from the staged JWKS, or the active JWKS rotated during preparation |
| `gate_membership` | no expectation declared, snapshot absent/stale, snapshot captured against another resource, or a target principal missing/mis-roled on any active board |
| `gate_seats` | seat count != 8, duplicate names, role outside worker/reviewer, tier outside 1..2, name/role/tier drift against the pre-cutover baseline, seat folder or staged managed file missing, staged wrapper losing `PURSERS_BOARDS=registry` or the registry wait scope, or binding another resource |
| `gate_configs` | coordinator/dashboard/bridge config missing, required content absent, CA override, `start-all`, `--poll`, coordinator not on the target resource, dashboard not keeping its own UI on `http://127.0.0.1:8899` |
| `gate_legacy` | no legacy disabled marker declared or a declared marker missing |
| `gate_host_capability` | a declared Team lifecycle adapter is absent |

`activate` additionally requires:

1. `--operator-confirmed`;
2. a dry-run evidence record whose `plan_hash` matches the current plan, whose
   `target_url` is the exact target resource, whose `ok` is true, whose
   `mutations` list is empty, and whose `recorded_at` is inside
   `evidence_max_age_s` (default 900s);
3. no interrupted activation in the journal (roll back first).

## 5. Activation and rollback mechanics

Activation replaces live files only, in plan order:

```text
swap-launcher -> swap-jwks -> swap-credentials -> swap-configs -> swap-seats
```

For each step the toolkit

1. records a `started` journal entry with, per target, the identifier, live and
   staged paths, the pre-activation SHA-256 and the staged mode;
2. writes every target through a temporary file plus `os.replace` (atomic where
   the filesystem allows it) and re-hashes the result;
3. records a `done` entry.

Because the `started` entry is written before any byte moves, a crash between
the two entries leaves a detectable interruption: `interrupted_steps` names it,
`dry-run` refuses to move forward, and `rollback` repairs it.

`rollback` verifies backup integrity and proves that the backed-up named
credentials bind `previous_url` and that every JOSE-header signing `kid` is in
the backed-up JWKS before its first journal or live-file write. Every recorded
step must then contain exactly one canonical journal target for each configured
swap, including its matching backup reference, pre-activation hash, staged
hash, and mode. Missing, duplicate, extra, malformed, or mismatched target
metadata refuses rollback before any restore or journal write. The toolkit then
requires the journal's schema/toolkit identifiers and `entries`/`targets`
collections to retain their generated JSON shapes; non-object entries or targets
are refused rather than discarded. It then validates whole-step progression:
recorded activation steps must be an ordered
prefix of the configured sequence, each step must have exactly one valid
`started` / optional `done` / optional `rolled_back` progression, and activation
cannot advance past an incomplete step. A configured step absent from the
journal is accepted only when all of its live targets still match the verified
backup. This distinguishes a legitimate early interruption from a deleted
middle/final step after those files changed. Missing, duplicated, reordered, or
malformed whole-step entries and absent-step live drift all refuse before the
first restore or journal write. The toolkit then restores active steps in
reverse order (`steps_needing_rollback`), re-hashes each restored file against
the recorded pre-activation value, and repeats the coherence check on the
restored live state. A mixed backup is refused before a restore; a non-coherent
post-restore check returns failure.

Service restart order stays with the operator, exactly as in the runbook:
coordinator, dashboard and Teams stop first and the old Central stops last; on
rollback the old HTTPS Central starts first and only then coordinator,
dashboard and the canary seats.

## 6. Artifact identifiers and hashes

Board-facing reports use two identifier families:

* rollback-unit identifiers, which are also backup-relative paths:
  `launcher/profile.env`, `launcher/launch-central.sh`, `jwks/jwks.json`,
  `credentials/<NAME>.jwt`, `doors/<ROLE>.door`, `configs/<FILE>`,
  `seats/<NN>/<MANAGED_FILE>`, plus operator-declared `extra_backup_paths`;
* committed deliverable digests, computed with
  `shasum -a 256 tools/o1_cutover.py tools/tests/test_o1_cutover.py
  docs/operations/o1-cutover-toolkit.md`.

The private backup manifest (`backup/manifest.json`) stores `identifier`,
`rel_path`, `sha256`, `size` and `mode` for every member, and its own digest is
reported by `backup`. Only sanitized identifiers and digests belong on a board
or in a ticket; real filesystem paths belong in the private command sheet.

## 7. Honest limitations

* This ticket prepared tooling. No real host was probed, no live file was read
  or written, no credential or door was minted, and no service or Team was
  touched. Every behaviour above is proven by `tools/tests/test_o1_cutover.py`
  against synthetic temporary trees.
* A real `dry-run` needs the operator-private configuration (live paths, backup
  root, staged artifacts, board/membership/seat snapshots). Until the operator
  supplies it, no claim about the production installation is made.
* Named-token minting stays an authorized private operator step: the public
  scaffold documents `jwt_provision.py` but does not ship it, and this toolkit
  does not reimplement it.
* The released door CLI is board-labelled. Fleet-wide door use is safe only if
  the resulting principals are explicitly admitted on every active board and
  `gate_membership` proves it; otherwise issue per-board doors.
* Seat folder regeneration still runs through `tools/seat-kit/seat_new.py
  --upgrade` with the door supplied through the approved secret handoff, so that
  no door value reaches argv, shell history, logs or a ticket. The toolkit
  verifies the staged result; it does not mint it.
* Sandbox acceptance on another loopback port validates the wheel and the flow
  only. Production URL-bound credentials receive their first end-to-end proof
  during the canary activation.
