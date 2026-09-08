# Pursers for AionUi

This extension adds a guided Pursers Home plus Worker and Reviewer presets. Home
connects one project with a coordinator-issued door, prepares distinct Team
seats through a dry-run-first plan, reports partial results, and exposes bounded
pause, stop, recovery, and roster controls.

## Build and install

From the repository root:

```sh
python tools/aionui-extension/build.py
```

Install `dist/pursers-aionui-0.1.0.zip` through AionUi's extension installer.
Do not unzip it into an existing AionUi data directory by hand.

The Join tab expects `pursers-wait-bridge` on AionUi's executable path. Install
it with `uv tool install pursers-wait-bridge` or
`pipx install pursers-wait-bridge` if the tab reports that it is missing.

## Set up a project

1. Open Settings, then Pursers.
2. Paste the door supplied by your coordinator, check it, and connect. The input
   is cleared immediately and status only shows redacted metadata.
3. Open an existing AionUi Team conversation. Enter its exact Team and monitor
   lead identity plus a unique name, folder, role, and tier ceiling per seat.
4. Preview the Team plan. Starting seats requires a separate confirmation of
   that exact plan; each result is reported independently.
5. Start a new conversation and pick the matching Worker or Reviewer preset.

The Join route passes the door directly to `pursers-wait-bridge join`, which
owns the private mode-0600 credential store. The extension does not log or
persist the door. It then registers an environment-free stdio bridge through
AionUi's local `POST /api/mcp/servers/import` endpoint.

## Backend onboarding contract

`door/adapter.cjs` provides typed `parse`, `validate`, `connect`, `status`,
`rotate`, and `recover` operations for a future beginner flow. The authenticated
routes are under `/pursers/onboarding/`; the existing `/pursers/join` and
`/pursers/status` response shapes remain compatible with the current UI.

Remote Central doors require HTTPS. HTTP doors are accepted only for loopback.
The adapter forwards a unique seat name and `PURSERS_TIER_MAX` to the shipped
wait bridge, returns an exact seat fragment for the Team adapter, and never
dispatches work. A partial MCP import is recoverable without replaying or
returning the door. See `door/DOOR_ONBOARDING_CONTRACT.md` for result codes,
idempotency, and current host limitations.

## Team controls

The authenticated `/pursers/team/` routes use the packaged approved Team
adapter. Status reads the host roster and task list. Plan is always read-only;
apply remains dry-run unless the request includes both
`options.confirm="apply-live"` and `options.dry_run=false`.

Pause interrupts one teammate turn with a safe-checkpoint message. Stop sends a
cooperative shutdown request and is not reported as complete until roster state
confirms it. The supported agent-facing host contract cannot create a Team,
archive a Team, remove a teammate, or resume one; Pursers Home states these
boundaries instead of simulating them. The dashboard remains the Pursers
Personal MCP app entrypoint (or `board_snapshot` fallback), not an invented URL.

## AionUi 2.2.1 limitations

- AionUi lists extension-declared MCP servers but does not inject them into
  Codex or Claude conversations. This extension deliberately has no
  `contributes.mcpServers` block and uses the authenticated REST import path.
- Extension assistant presets are contributed, but AionUi 2.2.1 may not expose
  them in the conversation preset picker. If a preset is not selectable, create
  the conversation explicitly and apply the matching context file.
- AionUi does not render Pursers MCP elicitation forms. Use the dashboard or the
  coordinator-provided fallback for human-input requests.
- Host-connected mutation verification remains an operator step. Repository
  tests cover the route, secret-handling, package, and static Home contracts.
