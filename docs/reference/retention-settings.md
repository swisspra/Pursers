# Central retention settings contract

Central exposes an admin-only, board-scoped settings contract through
`board_retention_settings_get`, `board_retention_settings_validate`,
`board_retention_settings_preview`, and `board_retention_settings_apply`. The
contract schema version is `1`.

| Field | Default | Inclusive range | Maintenance consumer |
|---|---:|---:|---|
| `archive_after_days` | `2` | `0`–`365` | Ticket archive sweep |
| `inline_history_limit` | `50` | `1`–`500` | Ticket history bounding |
| `invite_prune_after_days` | `7` | `0`–`365` | Expired invite pruning |
| `journal_retention_days` | `7` | `0`–`365` | Journal compaction window |
| `journal_row_cap` | `50000` | `501`–`1000000` | Journal hard row cap |

All five fields are operational in this version. `get` returns their current values,
defaults, ranges, revision, operational/unsupported field lists, and migration
metadata. Legacy board documents remain compatible: missing fields use the defaults,
and the settings revision starts at `0`.

`validate` accepts a partial `changes` object and returns the normalized candidate,
changed fields, ranges, and current revision. `preview` adds an `expected_revision`
CAS check and reports the candidate revision. Both reject unknown fields, booleans,
non-integers, and out-of-range values. Neither writes board state.

`apply` accepts the same partial object and revision, plus Central's standard optional
`expected_generation` fence. A changed write advances the revision once, persists all
five effective values atomically, and records an audit entry with the actor, old/new
revision, changed fields, previous/current values, and timestamp. A no-op write does
not advance the revision or create an audit entry.

Saving settings never archives tickets, bounds ticket histories, prunes invites,
compacts journals, changes leases, or runs another maintenance action. Destructive
maintenance remains a separate explicit operation through `board_archive_run` or
`journal_compact`. The older `board_journal_retention_set` tool retains its historical
immediate-compaction behavior for compatibility and is not the dashboard settings-save
contract.
