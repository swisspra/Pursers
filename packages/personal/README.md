# On Board Personal Preview 5.0.0a1

On Board Personal is a local board for one owner and multiple explicitly named
agent clients. MCP Apps is the primary read-only UI; agent chat retains ticket,
workflow-review, memory, state, and handoff tools.

This alpha is a new package. It does not replace, upgrade, or migrate
`onboard-memory-mcp==4.0.4`.

## Installation authorization

Possessing this package does not authorize installing or activating it. Only
one of these exact external attestations may do so:

1. An operator test manifest may authorize `HOST_PROOF_ONLY` installation and
   activation in a dedicated isolated account or VM. It must identify this
   exact wheel by filename and SHA-256, retain `release_status=DO_NOT_PUBLISH`
   and `supported_hosts=[]`, require synthetic data and an isolated Host
   profile, and name its test boundary. This path never authorizes ordinary
   use, publication, or a supported-Host claim.
2. An official release manifest may authorize ordinary installation only when
   it identifies this exact wheel by filename and SHA-256, has a release status
   that explicitly permits installation, and lists the exact Host product,
   version, and build in `supported_hosts`.

If neither matching external attestation exists, do not install or activate the
package. `DO_NOT_PUBLISH` or `supported_hosts=[]` never permits ordinary use;
those values are valid only for the separately declared `HOST_PROOF_ONLY` test
path. This embedded document does not declare the current release status or
claim support for any Host. Static, SDK, or source evidence alone cannot grant
installation authority or establish Host support.

## What the preview supports

- macOS, loopback HTTP, SQLite, signed JWT capability, invite admission;
- one stable agent identity per explicit `host + session` configuration;
- Today, Work, Agents, and Activity views through MCP Apps;
- model-only ticket, workflow-review, memory, handoff, and board-state tools;
- no writable App controls, remote service, team/account UI, or v4 import.

Claude Desktop's configured `primary` session is one agent identity shared by
that MCP process; this preview does not claim automatic per-chat identities.
Additional agent identities require separately named client/session entries.
The App's Activity view contains bounded events observed from model tools in
that server process; it does not read or acknowledge the full Central journal.

These capabilities describe the package design. They do not establish Host
support or installation authority.

## Local security boundary

Credentials stay in a private profile directory and never enter Claude config,
App HTML, iframe messages, tool results, or logs. Clients disable proxy
environment variables, the port is randomized per profile, and Central binds
only to `127.0.0.1`.

This alpha does not provide pinned loopback TLS or a Unix-domain socket. Treat
all local processes and OS users on the Mac as inside its trust boundary; do not
use it on a shared or untrusted machine. It is not a remote or multi-user
security boundary.

## Lifecycle implementation boundary

Setup, rollback, and uninstall refuse to run while Claude or its bundle helpers
are active. The installer serializes its transaction, verifies exact ownership,
hashes, modes, and console provenance, and retains a recovery file if an
unsupported concurrent external edit is detected. Keep Claude closed; arbitrary
concurrent config editors are outside the supported installer boundary.

While the integration is active, its private `0600` receipt contains an exact
rollback backup of the pre-existing Claude config, which may include credentials
owned by other MCP entries. After verified rollback or uninstall, On Board
removes those backup bytes from the terminal receipt while retaining only their
hash, existence, and file mode for audit.

This embedded document intentionally provides no installation, integration,
activation, restart, rotation, rollback, or uninstall procedure. An official
external manifest that matches the wheel, together with its release materials,
must govern any authorized lifecycle operation. No command in this package
publishes a release.
