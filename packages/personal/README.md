# Pursers Personal 5.0.0

Pursers Personal is a local board for one owner and multiple explicitly named
agent clients. MCP Apps is the primary read-only UI; agent chat retains ticket,
workflow-review, memory, state, and handoff tools.

It does not replace, upgrade, or migrate `onboard-memory-mcp==4.0.4` in place.
The separate `pursers-personal-import` command copies a v4 board into a new,
empty Personal data root.

## Install and set up

Install into a dedicated virtual environment, then plan the integration for a
project:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install pursers-personal
pursers-personal setup --project /PATH/TO/PROJECT
```

Without flags, `setup` creates the private profile and prints the exact
integration plan without changing anything outside it. Quit Claude Desktop,
then apply the plan and start the owned service:

```bash
pursers-personal setup --project /PATH/TO/PROJECT --apply --activate
```

`--apply` writes the Claude Desktop MCP entry and the macOS LaunchAgent;
`--activate` (which requires `--apply`) starts that service and initializes the
board. Restart Claude Desktop afterwards. `pursers-personal doctor` shows the
effective identity and actionable checks, `restart` restarts only the owned
service, `rotate` rotates the local signing key, and `rollback` or `uninstall`
remove the integration while keeping profile data.

## What it supports

- macOS, loopback HTTP, SQLite, signed JWT capability, invite admission;
- one stable agent identity per explicit `host + session` configuration;
- Today, Work, Agents, and Activity views through MCP Apps;
- model-only ticket, workflow-review, memory, handoff, and board-state tools;
- no writable App controls, team/account UI, or in-place v4 import.

Claude Desktop's configured `primary` session is one agent identity shared by
that MCP process; Personal does not create automatic per-chat identities.
Additional agent identities require separately named client/session entries.
The App's Activity view contains bounded events observed from model tools in
that server process; it does not read or acknowledge the full Central journal.

## Local security boundary

Credentials stay in a private profile directory and never enter Claude config,
App HTML, iframe messages, tool results, or logs. Clients disable proxy
environment variables, the port is randomized per profile, and the Personal
service binds only to `127.0.0.1`.

The Personal service does not provide pinned loopback TLS or a Unix-domain
socket. Treat all local processes and OS users on the Mac as inside its trust
boundary; do not run it on a shared or untrusted machine. To serve agents on
other machines, run the separate `pursers-central` service with an
operator-supplied TLS certificate and an allowed host name (for example a
Tailscale MagicDNS name).

## Lifecycle boundary

Setup, rollback, and uninstall refuse to run while Claude or its bundle helpers
are active. The installer serializes its transaction, verifies exact ownership,
hashes, modes, and console provenance, and retains a recovery file if an
unsupported concurrent external edit is detected. Keep Claude closed; arbitrary
concurrent config editors are outside the supported installer boundary.

While the integration is active, its private `0600` receipt contains an exact
rollback backup of the pre-existing Claude config, which may include credentials
owned by other MCP entries. After verified rollback or uninstall, Pursers
Personal removes those backup bytes from the terminal receipt while retaining
only their hash, existence, and file mode for audit.
