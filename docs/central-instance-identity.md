# Central instance identity

Each Central data directory owns one non-secret deployment identity. It is stored
as `central-instance.json` beside the SQLite database and has the form
`CI-<64 lowercase hex characters>`. Central and Personal create it under a
cross-process lock, write it atomically, and thereafter treat malformed or replaced
identity state as a startup/security error.

Personal profiles persist the expected instance ID in `profile.json`. Every
profile-backed Central tool call carries that value as
`io.onboard/expected-instance`. Central compares it before authorization, board
loading, reads, writes, or review operations. A mismatch returns the safe typed
client outcome `CentralInstanceMismatchError`; the response does not disclose the
connected instance's ID.

Profile-bound subscription requests add one non-notifying
`pursers-instance://binding/CI-...` resource alongside the ordinary board resource
URIs. Central validates that binding before scope checks, board loading, stream
accounting, listener registration, activity/liveness mutation, or event reads.
The client library applies the same contract to `BoardClient.events`, project
registry multi-board waits, and Wait Bridge journal/agent subscriptions. Legacy
clients without an expected instance binding retain their existing same-instance
subscription behavior.

New server-generated ticket IDs hash the Central instance ID, board ID, and board
sequence. Therefore equal board IDs and aligned sequences in independent Centrals
do not produce the same ticket ID. Existing ticket IDs remain valid and safe because
the connection-level instance assertion is still required at profile boundaries.

## Lifecycle semantics

- New and legacy stores: the identity is created once on first initialization or
  startup. Existing Personal profiles missing the additive binding are updated
  atomically from their own Central data directory before a connection is opened.
- Backup and restore: back up `central-instance.json` with the entire data directory.
  A restored replacement preserves the original identity and existing profiles.
- Live clone: a simultaneously operated clone must receive a new identity while it
  is offline with `pursers-central fork-instance DATA_DIR`. Old profiles then fail
  closed against the clone; they are not silently rebound.
- Board export/import: board archives do not contain or replace deployment identity.
  Imported boards use the destination Central identity for newly generated tickets.
- Credential rotation: issuer keys, tokens, and profile credential generations may
  rotate without changing the Central instance identity.
- Rollback: integration rollback retains the Personal profile and Central data, so
  it also retains the instance binding. Restoring a full backup restores both.

The identifier is routing metadata, not a credential. It may appear in local health
and doctor output, but local paths, bearer tokens, signing keys, and credential
contents must never be derived from or embedded in it.
