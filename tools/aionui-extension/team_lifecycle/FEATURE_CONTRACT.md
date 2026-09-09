# Home standalone seat-group lifecycle contract

Ticket: `TK-6db356ba00b8`

Base: `d0dd3822acd92337355464ba5b9db81dde7a6ccd` (frozen main
`b06ce6627edb62fc588eee541fa568445b709054`)

## Supported model

A Home group is board-scoped organization metadata for already joined standalone
Pursers seats. It is not an Aion Team, does not create or control processes, and
does not change board membership, dispatch, ticket ownership, or review authority.
Groups never cross a project board.

The configured board persists one `board_state` value under
`home_seat_groups_v1`:

```json
{"schema_version":1,"revision":3,"groups":[{"group_id":"group-a1b2c3d4e5f6","name":"Delivery","member_agent_ids":["AI-..."],"created_at":"...","updated_at":"..."}]}
```

The document contains at most 25 groups and 5,000 UTF-8 characters. A group has
one stable generated ID, a unique case-insensitive 1-80 character name, and 1-50
unique agent IDs that currently belong to the configured board. The sidecar
uses Central's `expected_sha256` precondition for every update and increments
the document revision. Concurrent edits fail as `conflict` instead of silently
overwriting state.

## Create, view, update, remove

- Create validates current `board_snapshot` membership, stores a new group, and
  never creates seats or a board.
- View reads the persisted document plus a fresh bounded board snapshot. Each
  member is projected with its real name, role, lifecycle status, and
  `present`/`missing` state. Missing or retired seats remain visible for recovery.
- Update requires `expected_revision` and may change the name and replace the
  member list. It cannot mutate a seat. Unknown or cross-board agent IDs fail.
- Remove requires `expected_revision`, omits the group from the next persisted
  document, and leaves all seats, tickets, board membership, and history intact.
  The empty schema document remains persisted because Central has no state-delete
  action.

The stored-door principal must hold `board:write` and board membership. Central
is the authority. The persistent helper actor sends only supported
`can_work=false` and `can_review=false` capabilities. The browser never receives
the door token or Central URL.

## Isolation, recovery, and accessibility

Existing exact helper-token and Origin checks apply. Routes and bodies are pinned
to the configured board. Errors are typed as `not_connected`, `board_mismatch`,
`permission_denied`, `group_not_found`, `member_not_found`, `conflict`,
`backend_unavailable`, or `invalid_input`, with no fallback mutation.

The UI uses labeled controls, a live status region, keyboard-reachable group
cards and member checkboxes, text status in addition to color, and explicit
confirmation before remove. Authenticated browser evidence remains a separate
independent assembled-candidate gate.

## File ownership

This feature owns:

- `team_lifecycle/FEATURE_CONTRACT.md`
- `team_lifecycle/adapter.cjs`
- `tests/team_lifecycle.test.cjs`
- `tests/test_team_lifecycle.py`
- `tools/wait-bridge/team_lifecycle.py`
- `tools/wait-bridge/tests/test_team_lifecycle_service.py`
- `tools/wait-bridge/tests/test_team_lifecycle_real_central.py`

Minimal shared integration edits are limited to `host/helper.cjs`,
`webui/routes.js`, `webui/index.html`, `webui/app.js`, `webui/style.css`,
`build.py`, `README.md`, `host/HELPER_CONTRACT.md`, and the wait-bridge module
allow-list/CLI dispatch. Observer/harness, shell, door, ticket, worker, reviewer,
Fleet Dashboard, Personal MCP, Central, and client behavior are not owned.
