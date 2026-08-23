# On Board Personal Import 5.0.0a2

Status: **DO NOT PUBLISH**. This is an unpublished alpha component intended for
later integration into `onboard-personal`. It does not modify or replace the
stable On Board v4 command.

The importer copies one local v4 `.agent-mem` board into a new, empty Personal
Central data root. It requires an explicit owner principal and owner agent; it
does not infer either identity from legacy labels. Legacy agents remain
unbound for later review.

## Safety boundary

- Stop Personal Central before every `import`, `retry`, `rollback`, and
  `status` operation. `--confirm-central-stopped` is an operator attestation;
  the tool does not detect or stop a running Central process.
- The source is read under its `.board.lock`. The tool verifies the live tree
  before and after the lock-bounded copy and performs all conversion from the
  sealed private copy.
- The completed proof becomes authoritative only after the durable
  `freeze_completed` state anchors its exact bytes and all three copied trees.
  If interruption occurs before that anchor, retry discards the unanchored
  output and revalidates the live source. Source writes after the anchor do not
  become a retry or rollback precondition.
- `full-source-backup` preserves every supported regular source file byte.
  Original metadata is recorded in the source seal, while private-copy modes
  are normalized. `source-snapshot` is a separate import-domain copy with a
  synthetic write fence; these hashes are intentionally different.
- Source symlinks, hard links, special entries, duplicate JSON keys, duplicate
  memory or ticket identifiers, and malformed record shapes fail closed.
- The alpha accepts at most 100,000 tree entries, 64 MiB per regular file, and
  256 MiB of regular-file payload per tree. Oversized inputs fail before
  installation; these are explicit compatibility limits, not truncation.
- The Central target must be absent or an empty private directory. Import into
  an existing non-empty Personal data root is intentionally out of scope.
- Conversion halts at `review_required` before target installation whenever a
  masked quarantine row or unbound legacy agent remains. A private worksheet
  is written under the run directory. Installation resumes only after a
  complete decision file and explicit bind-or-`RETIRE` coverage replay as
  idempotent, leaving zero `UNMAPPED` agents.
- Install and rollback use durable state, parent-directory fsync, private
  backups, and exact tree seals. A changed post-import Central tree is moved to
  rollback quarantine before the original empty baseline is restored.
- The stable v4 Homebrew installation is hashed immediately before and after
  conversion. Later package-manager changes do not disable retry or rollback.
- The tool opens no network listener and makes no remote call.

## Command contract

Use the canonical Homebrew Cellar directory, not the `opt` symlink:

```sh
STABLE_INSTALL="$(brew --cellar onboard-memory)/4.0.4"

onboard-personal-import import /path/to/project/.agent-mem /path/to/new-central-data \
  --run-dir /path/to/private-import-run \
  --board-id personal-board \
  --owner-principal-id personal-owner \
  --owner-agent-name local-agent \
  --stable-install-root "$STABLE_INSTALL" \
  --confirm-central-stopped

onboard-personal-import retry /path/to/private-import-run \
  --confirm-central-stopped

onboard-personal-import status /path/to/private-import-run \
  --confirm-central-stopped

onboard-personal-import decide /path/to/private-import-run \
  --policy /path/to/POLICY-signed.json

onboard-personal-import review /path/to/private-import-run \
  --decisions /path/to/private-decisions.json \
  --bindings /path/to/private-bindings.json \
  --confirm-central-stopped

onboard-personal-import rollback /path/to/private-import-run \
  --confirm-central-stopped
```

When import reports `review_required`, use the private files under the owned
run directory:

- `decide` accepts an owned `0600` policy JSON document with
  `schema_version: 1`, `status: "POLICY-SIGNED-READY"`, the matching
  `board_id` and `worksheet_sha256`, and a `rules` object mapping every rule
  present in the worksheet to `accept-as-is`, `redact-span`, or `drop`.
  It writes `evidence/policy-decisions-<policy-hash>.json` as an owned `0600`
  complete decisions file. Use `--output` to select another file in an owned
  `0700` directory. Mixed rules on one record escalate the entire record to
  the most restrictive action (`drop`, then `redact-span`, then
  `accept-as-is`). Secret-class rules cannot be auto-accepted.
- `evidence/quarantine-worksheet.json` lists masked quarantine rows. Create an
  owned `0600` decisions file with the same `board_id`, `worksheet_sha256`, and
  one entry per worksheet row. Set `status` to `REVIEWED-SIGNED-READY`, include
  `review_metadata.reviewed_at`, preserve each row's `record_key`,
  `record_type`, `record_id`, `field`, and `rules`, and replace `decision` with
  exactly `accept-as-is`, `redact-span`, or `drop`. A decision applies to the
  whole top-level legacy record, so every row for the same record must use the
  same action; mixed per-field actions fail closed.
  `accept-as-is` preserves reviewed content, except a secret-bearing structural
  memory, agent, ticket, artifact, or state key identifier is always replaced
  by its stable `sha256-...` safe identifier. `redact-span` uses the same
  structural normalization. This exception prevents the raw identifier from
  becoming a database key, worksheet key, or log value.
- `evidence/identity-binding-worksheet.json` explains every legacy identity;
  copy `evidence/identity-bindings-template.json` to a separate owned `0600`
  decisions file. Replace each applicable `PENDING` value with `RETIRE` or an approved
  Central principal ID. Keys use `record:<record_id>` so duplicate display
  names remain unambiguous. Delete template entries for quarantined agents that
  the quarantine decision drops; keep and decide entries restored by
  `accept-as-is` or `redact-span`, and update `entry_count` to the number of
  binding keys. Do not edit the sealed worksheet or template in place.

Pass the completed owned `0600` files to `review`. The tool copies and seals
them before use; incomplete, extra, ambiguous, or changed decisions fail before
installation.

The source, run directory, Central target, and stable installation must be
disjoint. Run and target parents must be owned by the current user and must not
be group- or other-writable. State, receipts, backups, worksheets, and
quarantine remain private under the run directory.

The runtime uses only the Python standard library. Building the distribution
requires the build dependencies declared in `pyproject.toml`. Nothing in this
alpha is published or installed by its preparation workflow.
