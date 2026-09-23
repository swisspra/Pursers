# Pursers client

<!-- mcp-name: io.github.swisspra/pursers -->

`pursers-client` is the asynchronous Python client for a Pursers Central MCP
service. It provides `BoardClient` for authenticated board, ticket, memory,
state, and event operations used by Pursers runtimes and automation tools.

Applications supply the Central URL, bearer credential, board ID, and seat
identity. `BoardClient` itself does not manage operator configuration. The
Zed-facing `pursers-mcp` relay has a separate, consent-gated local first-run
path described below.

## Zed stdio relay

`pursers-client` installs `pursers-mcp`, a credential-safe stdio MCP server for
Zed and other local MCP hosts:

```sh
uvx --from pursers-client==<VERSION> pursers-mcp \
  --central-url http://127.0.0.1:<PORT> \
  --board <BOARD_ID> \
  [--token-file /PATH/TO/credential.jwt] \
  [--repository-root /PATH/TO/WORK]
```

Use `--ca-file /PATH/TO/private-ca.pem` when Central uses a private TLS CA.
`--central-url` accepts either the Central origin or its `/mcp` endpoint. The
When `--token-file` is omitted it defaults to
`~/.pursers/central/worker.jwt`. The token file is read without logging its contents, checked again before every
upstream connection generation, and re-read after an HTTP 401. Keep it private
(mode `0600`). The relay writes only MCP JSON-RPC frames to stdout; connection
diagnostics go to stderr. When Central is unavailable, the MCP session remains
usable and exposes `pursers_setup_status` and `pursers_setup`. The latter uses
MCP elicitation to ask before creating files or starting a background process.
It initializes `~/.pursers/central`, writes Central output to `central.log`,
creates the configured board, reconnects, and emits
`notifications/tools/list_changed`. A failed setup stops any Central process it
started. For the managed local token, the relay hides the internal
`agent_name` field and supplies the setup identity itself, so the first ticket
can be created without discovering an implementation-only seat name.

Zed 1.20.2 negotiates MCP `2025-11-25`. The relay terminates that local
protocol and opens a separate MCP `2026-07-28` connection to Central. It
forwards the principal's Central tool names, descriptions, schemas, and call
results unchanged. By default it exposes the 18 read/create/annotate,
question/human-request, evidence, and resumable-watch tools used by the Zed
flows; pass `--tools all` to expose every tool authorized for the principal.
When a code ticket requires `branch_and_commit`, start the relay with one or
more explicit `--repository-root` values and call its wrapped `ticket_submit`
with `repository` set to the clone-owning checkout. The relay accepts exactly
one `branch_and_commit: branch/with-slash @ <full-40-hex-sha>` line, resolves
the checkout beneath an allowed root, checks the exact `origin` branch tip,
and signs the preflight with its file-held credential. The repository path and
credential are never forwarded to the MCP host or Central. Caller-supplied
`submission_preflight` is rejected, and Central continues to reject raw code
submissions without a valid clone-owned proof.
Wait calls are capped at 50 seconds, below Zed's 60-second default, and their
upstream cursor result is preserved for the next call. Each request owns an
independent upstream SDK context, so a long-running wait cannot block another
call and credential rotation cannot cancel an in-flight request.

The relay also publishes six prompts that Zed exposes as slash commands:
`board`, `create <summary>`, `watch`, `evidence <ticket-id>`, and
`answer <ticket-id-and-answer>`, plus the first-run `setup` prompt. Their instructions keep output bounded, put
stable IDs first, and avoid fleet-wide dumps.
