# Home ticket lifecycle contract and ownership map

Ticket: `TK-fc9727be2848`

Base: `d0dd3822acd92337355464ba5b9db81dde7a6ccd` (frozen main
`b06ce6627edb62fc588eee541fa568445b709054`)

## Product contract

Home may create a real, unassigned Pursers board ticket and refresh its persisted
status. The create form requires the same generated-ID fields as Central: title,
description, target URL, scope, and required fields. It also accepts priority,
tier, tags, and related files within bounded input limits.

Home may cancel a non-terminal ticket only by asking Central to enforce its
creator/current-executor/reviewer authority rule. The UI does not expose claim,
unclaim, renew, submit, review-claim, review, assignment, or synthetic status
transitions. Workers retain ownership of execution and independent reviewers
retain ownership of approval. Every displayed state is returned by `ticket_list`,
`ticket_get`, `ticket_create`, or `ticket_cancel` on the configured board.

The loopback helper keeps one board session open while it runs. Its dedicated
actor advertises `can_work=false` and `can_review=false`; it cannot receive work
or review dispatch. The stored door principal and Central remain the authority.
The browser never receives the door token or Central URL.

Requests require the existing exact Origin and helper-token checks. The helper
pins one configured board and rejects a body that names another board. Responses
use bounded, actionable error codes and never include credentials or raw backend
tracebacks.

## File ownership

This feature owns:

- `ticket_lifecycle/FEATURE_CONTRACT.md`
- `ticket_lifecycle/adapter.cjs`
- `ticket_lifecycle/service.py`
- `tests/ticket_lifecycle.test.cjs`
- `tests/test_ticket_lifecycle.py`

The minimal integration surface is:

- `host/helper.cjs`: lifecycle sidecar construction and shutdown only
- `webui/routes.js`: board-pinned lifecycle routes only
- `webui/index.html`, `webui/app.js`, `webui/style.css`: one accessible lifecycle section
- `build.py`, `README.md`, and package hashes: include and document the owned files
- `tools/wait-bridge/pursers_wait_server.py` and `pyproject.toml`: one persistent
  `ticket-lifecycle` sidecar entry point; no observer, listener, or shell rewrite

No home-acceptance observer/harness, Team adapter, door adapter, worker/reviewer
context, Fleet Dashboard, Personal MCP, Central, or client behavior is owned by
this ticket.

## Recovery states

- `not_connected`: connect a door for the configured board.
- `board_mismatch`: use the helper's configured board.
- `backend_unavailable`: keep form data, restart/reconnect the helper, then retry.
- `permission_denied`: use an authorized principal; no fallback mutation occurs.
- `ticket_not_found`: refresh the list and choose a persisted ticket.
- `conflict`: refresh because the ticket is already terminal or changed.
- `invalid_input`: correct the named field before retrying.

Keyboard operation, visible labels, a live status region, focusable ticket rows,
and text in addition to color are required. Authenticated browser evidence remains
an independent final assembled-candidate gate and is not claimed by component tests.
