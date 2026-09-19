# Pursers client

`pursers-client` is the asynchronous Python client for a Pursers Central MCP
service. It provides `BoardClient` for authenticated board, ticket, memory,
state, and event operations used by Pursers runtimes and automation tools.

Applications supply the Central URL, bearer credential, board ID, and seat
identity. The client does not start Central, create credentials, or manage
operator configuration.

## Zed stdio relay

`pursers-client` installs `pursers-mcp`, a credential-safe stdio MCP server for
Zed and other local MCP hosts:

```sh
uvx --from pursers-client==<VERSION> pursers-mcp \
  --central-url http://127.0.0.1:<PORT> \
  --board <BOARD_ID> \
  --token-file /PATH/TO/credential.jwt
```

Use `--ca-file /PATH/TO/private-ca.pem` when Central uses a private TLS CA.
`--central-url` accepts either the Central origin or its `/mcp` endpoint. The
token file is read without logging its contents, checked again before every
upstream connection generation, and re-read after an HTTP 401. Keep it private
(mode `0600`). The relay writes only MCP JSON-RPC frames to stdout; connection
diagnostics go to stderr and are also exposed as a tool error after the MCP
handshake, so Zed can show a missing credential or unreachable Central.

Zed 1.20.2 negotiates MCP `2025-11-25`. The relay terminates that local
protocol and opens a separate MCP `2026-07-28` connection to Central. It
forwards the principal's Central tool names, descriptions, schemas, and call
results unchanged. By default it exposes the 18 read/create/annotate,
question/human-request, evidence, and resumable-watch tools used by the Zed
flows; pass `--tools all` to expose every tool authorized for the principal.
Wait calls are capped at 50 seconds, below Zed's 60-second default, and their
upstream cursor result is preserved for the next call. Each request owns an
independent upstream SDK context, so a long-running wait cannot block another
call and credential rotation cannot cancel an in-flight request.

The relay also publishes five prompts that Zed exposes as slash commands:
`board`, `create <summary>`, `watch`, `evidence <ticket-id>`, and
`answer <ticket-id-and-answer>`. Their instructions keep output bounded, put
stable IDs first, and avoid fleet-wide dumps.
