# Zed 1.20.2 context servers: Pursers manual proof

## Result

Zed 1.20.2 can use Pursers Central directly as a remote MCP context server.
An isolated Zed profile connected to an isolated Central over streamable HTTP,
loaded 53 Pursers tools, exposed them to the native Agent Panel, and completed
read-only `board_status` and `ticket_list` calls. No HTTP-to-stdio bridge is
needed for ordinary board tools.

The compatibility boundary is MCP version negotiation. Pursers targets MCP
2026-07-28, while Zed 1.20.2 offers and accepts only 2025-11-25 and three older
versions. Central successfully negotiated 2025-11-25, so the classic
`tools/list` and `tools/call` flow works. Zed cannot consume 2026-07-28-only
features such as Pursers' subscription/listen wait path through its native MCP
client. That path still needs the existing stdio wait bridge.

This research is pinned to the installed build
`1.20.2+stable.360.7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f` and the matching
[Zed `v1.20.2` source tag](https://github.com/zed-industries/zed/tree/v1.20.2).
Pursers' declared protocol baseline is documented in the
[repository README](../../../README.md), and its subscription/listen design is
described in [the push-wait design](../../push-wait-design.md).

## Zed 1.20.2 behavior

| Area | Finding | Evidence |
| --- | --- | --- |
| Settings shape | Custom servers live under `context_servers`. Local stdio entries use `command`, `args`, and `env`; remote entries use `url` and optional `headers`. Both variants accept `enabled` and `timeout`. | [Zed MCP docs](https://github.com/zed-industries/zed/blob/v1.20.2/docs/src/ai/mcp.md#L51-L75), [settings types](https://github.com/zed-industries/zed/blob/v1.20.2/crates/settings_content/src/project.rs#L425-L476), and [stdio command fields](https://github.com/zed-industries/zed/blob/v1.20.2/crates/settings_content/src/project.rs#L513-L523) |
| Transports | Local servers use stdio. Remote servers accept `http` or `https` and POST JSON-RPC with both `application/json` and `text/event-stream` in `Accept`; responses may be JSON or SSE. Static headers are copied onto each request. | [transport selection](https://github.com/zed-industries/zed/blob/v1.20.2/crates/context_server/src/context_server.rs#L36-L82) and [HTTP/SSE implementation](https://github.com/zed-industries/zed/blob/v1.20.2/crates/context_server/src/transport/http.rs#L101-L123) |
| Authentication | An explicit `Authorization` header works. When it is omitted, Zed can start the standard MCP OAuth flow. The documented remote-header setting is a literal string map; Zed 1.20.2 documents no token-file or environment interpolation for it. | [Zed MCP docs](https://github.com/zed-industries/zed/blob/v1.20.2/docs/src/ai/mcp.md#L64-L75) and [settings type](https://github.com/zed-industries/zed/blob/v1.20.2/crates/settings_content/src/project.rs#L442-L456) |
| Agent Panel surface | Zed exposes MCP tools and prompts. It requests both when a server reaches `Running`. Resources may be advertised during initialization, but Zed's native Agent registry does not load or expose them. Sampling, discovery, and elicitation are also outside the documented MCP feature set. | [supported features](https://github.com/zed-industries/zed/blob/v1.20.2/docs/src/ai/mcp.md#L12-L17) and [registry loading](https://github.com/zed-industries/zed/blob/v1.20.2/crates/agent/src/tools/context_server_registry.rs#L170-L265) |
| Approval UX | `agent.tool_permissions.default` is `confirm` by default; `allow` auto-approves and `deny` blocks. A specific MCP tool can be addressed as `mcp:<server>:<tool_name>`. Profiles can enable only selected context-server tools. | [tool permissions](https://github.com/zed-industries/zed/blob/v1.20.2/docs/src/ai/mcp.md#L92-L159) |
| Notifications | Zed reloads tools on `notifications/tools/list_changed`. The registry does not subscribe to prompt/resource change notifications, even though protocol types for them exist. | [documented notification](https://github.com/zed-industries/zed/blob/v1.20.2/docs/src/ai/mcp.md#L14-L17), [tool subscription](https://github.com/zed-industries/zed/blob/v1.20.2/crates/agent/src/tools/context_server_registry.rs#L124-L167), and [notification types](https://github.com/zed-industries/zed/blob/v1.20.2/crates/context_server/src/types.rs#L108-L126) |
| Timeouts | The global default is 60 seconds. Per-server `timeout` is measured in seconds, applies to tool calls, and is capped at 600 seconds for both HTTP and stdio. A timed-out request fails with `Context server request timeout`. | [default](https://github.com/zed-industries/zed/blob/v1.20.2/crates/project/src/project_settings.rs#L723-L731), [cap and application](https://github.com/zed-industries/zed/blob/v1.20.2/crates/project/src/context_server_store.rs#L34-L36), [HTTP/stdio cap](https://github.com/zed-industries/zed/blob/v1.20.2/crates/project/src/context_server_store.rs#L1025-L1056), and [timeout result](https://github.com/zed-industries/zed/blob/v1.20.2/crates/context_server/src/client.rs#L471-L484) |
| Tool limits | Tool names are normalized to ASCII letters, digits, `_`, or `-` and truncated to 64 bytes; duplicate names are prefixed with a normalized server ID when space permits. Source inspection found no separate total MCP-tool-count cap. The selected model still has to support tools and fit their schemas in its request/context limits. | [name normalization](https://github.com/zed-industries/zed/blob/v1.20.2/crates/agent/src/thread.rs#L72-L92) and [duplicate handling](https://github.com/zed-industries/zed/blob/v1.20.2/crates/agent/src/thread.rs#L4190-L4228) |
| Protocol version | Zed sends 2025-11-25 and accepts only 2025-11-25, 2025-06-18, 2025-03-26, or 2024-11-05. It adds `MCP-Protocol-Version` after negotiation where required. It does not support 2026-07-28. | [version constants](https://github.com/zed-industries/zed/blob/v1.20.2/crates/context_server/src/types.rs#L8-L16), [negotiation](https://github.com/zed-industries/zed/blob/v1.20.2/crates/context_server/src/protocol.rs#L28-L68), and [HTTP header](https://github.com/zed-industries/zed/blob/v1.20.2/crates/context_server/src/transport/http.rs#L35-L59) |

The absence of a total tool-count cap is a source-inspection result, not a claim
that every model can accept an unlimited tool schema. In the proof below, Zed
loaded 53 Pursers tools and sent 68 tools in total after its built-ins were
included.

## Exact manual configuration

These are the context-server and approval fields used by the successful
direct-HTTP run, with environment-specific values replaced by placeholders.
The token must not be committed or printed. The isolated proof used `allow` so
the deterministic local model could complete without UI approval; use
`confirm` plus per-tool rules for a real profile.

```json
{
  "context_server_timeout": 90,
  "context_servers": {
    "pursers-zed-r2": {
      "url": "http://127.0.0.1:<CENTRAL_PORT>/mcp",
      "headers": {
        "Authorization": "Bearer <TOKEN>"
      },
      "timeout": 90
    }
  },
  "agent": {
    "tool_permissions": {
      "default": "allow"
    }
  }
}
```

For a manual proof, place this only in the isolated profile's `settings.json`,
start a throwaway Central on `<CENTRAL_PORT>` with a separate data directory,
then launch Zed with that profile (the installed 1.20.2 CLI accepts
`--user-data-dir`):

```sh
zed --user-data-dir /PATH/TO/ZED-DATA /PATH/TO/EMPTY-PROJECT
```

Open **Settings → AI → MCP Servers** and check for the green “Server is
active” indicator, then ask the native Agent Panel to call `board_status` or
`ticket_list` for a throwaway board. The UI location and indicator are documented by Zed
[here](https://github.com/zed-industries/zed/blob/v1.20.2/docs/src/ai/mcp.md#L77-L99).

Do not copy a production bearer into a shared settings file. For unattended
installation, the extension should generate a local stdio launcher that reads
the credential from a mode-0600 token file and keeps stdout exclusively for
MCP framing. The existing
[`tools/wait-bridge/pursers_wait_server.py`](../../../tools/wait-bridge/pursers_wait_server.py)
already follows that rule and reads `ONBOARD_CENTRAL_TOKEN_FILE`; its own
module contract requires stdio because HTTP request timeouts defeat a genuinely
blocking wait.

## Reproduction and literal evidence

The test used:

- a copy of the installed Zed app with a distinct bundle ID and
  `ZED_STATELESS=1`, because another Zed instance was already running;
- `--user-data-dir /PATH/TO/ZED-DATA` and an empty throwaway project;
- a throwaway Central on `127.0.0.1:28714`, with its own data directory and
  board `zed-r2-worker14`; and
- a deterministic local model endpoint solely to make the native Zed Agent
  select `board_status` and then `ticket_list`. It did not synthesize the MCP
  response; Central produced that response.

The following are literal, token-free lines from Zed's trace log:

```text
2026-09-19T23:48:16+07:00 INFO  [zed] ========== starting zed version 1.20.2+stable.360.7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f, sha 7c451e6 ==========
2026-09-19T23:48:16+07:00 DEBUG [context_server] starting context server pursers-zed-r2
2026-09-19T23:48:16+07:00 TRACE [context_server::client] outgoing message: {"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"Zed","version":"0.1.0"}}}
2026-09-19T23:48:17+07:00 TRACE [context_server::client] recv: {"jsonrpc":"2.0","id":0,"result":{"capabilities":{"experimental":{},"prompts":{"listChanged":false},"resources":{"listChanged":false,"subscribe":false},"tools":{"listChanged":false}},"protocolVersion":"2025-11-25","serverInfo":{"name":"On Board Central Skeleton","version":"0.1.0a9"}}}
2026-09-19T23:48:17+07:00 TRACE [context_server::client] outgoing message: {"jsonrpc":"2.0","id":1,"method":"tools/list"}
2026-09-19T23:48:59+07:00 DEBUG [agent::thread] Request includes 68 tools
2026-09-19T23:50:27+07:00 DEBUG [agent::thread] Running tool board_status
2026-09-19T23:50:27+07:00 TRACE [context_server::client] outgoing message: {"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"board_status","arguments":{"board_id":"zed-r2-worker14"}}}
2026-09-19T23:55:39+07:00 DEBUG [agent::thread] Running tool ticket_list
2026-09-19T23:55:39+07:00 TRACE [context_server::client] outgoing message: {"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"ticket_list","arguments":{"board_id":"zed-r2-worker14","include_closed":false}}}
2026-09-19T23:55:39+07:00 TRACE [context_server::client] recv: {"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text","text":"{\n  \"ok\": true,\n  \"tickets\": [],\n  \"count\": 0,\n  \"total_matching\": 0,\n  \"archived_matching\": 0,\n  \"filters\": {\n    \"status\": null,\n    \"assigned_to\": null,\n    \"include_closed\": false,\n    \"include_archived\": true,\n    \"review_unclaimed_only\": false,\n    \"ticket_ids\": null\n  },\n  \"latest_seq\": 1,\n  \"view\": \"work\",\n  \"include_dispatch_history\": false,\n  \"id_map\": {}\n}"}],"isError":false,"structuredContent":{"ok":true,"tickets":[],"count":0,"total_matching":0,"archived_matching":0,"filters":{"status":null,"assigned_to":null,"include_closed":false,"include_archived":true,"review_unclaimed_only":false,"ticket_ids":null},"latest_seq":1,"view":"work","include_dispatch_history":false,"id_map":{}}}}
```

The `tools/list` response is one very long JSON line. These are literal
fragments from that same Zed `recv` line, limited to the names relevant to this
proof:

```text
"name":"board_onboard"
"name":"ticket_get"
"name":"ticket_list"
"name":"board_status"
```

Parsing only `name` fields from that response produced `tools=53`; the Agent
request's 68-tool count above includes Zed built-ins. The expanded results were
also visible in the Agent Panel: `board_status` showed `ok: true` and the
requested board, while the independently logged `ticket_list` result above
showed the product-produced empty list. The first `board_status` call,
intentionally made before the throwaway credential was added to that board,
returned `board access denied: principal is not a member`; after onboarding the
same principal, the retry succeeded. This proves both
transport/authentication enforcement and a read-only product result rather
than only schema self-consistency.

## Compatibility gaps and smallest fixes

1. **MCP 2026-07-28 is downgraded.** Classic Pursers tools already work, so no
   Central change is required for `tools/list` or `tools/call`. Keep the older
   negotiated surface working in
   [`packages/central/src/pursers_central/central.py`](../../../packages/central/src/pursers_central/central.py).
   Do not advertise native Zed support for subscription/listen, Tasks, or other
   2026-07-28-only behavior until Zed adds that protocol version.

2. **A long push wait does not fit native HTTP tool calls.** Zed's default is
   60 seconds and its hard cap is 600 seconds. Continue to expose `a2a_wait`
   through the stdio-only
   [`tools/wait-bridge/pursers_wait_server.py`](../../../tools/wait-bridge/pursers_wait_server.py),
   whose implementation owns subscription/listen and lease maintenance. The
   extension should install it as a second context server and re-arm waits from
   the returned positive cursor.

3. **Direct HTTP makes the bearer a literal settings value.** The smallest
   safe automation is a generated stdio launcher that receives only a token
   file path in `env`, reads it without logging, and talks to Central. Reuse the
   token-file conventions in
   [`tools/seat-kit/seat_new.py`](../../../tools/seat-kit/seat_new.py) and the
   stderr-only logging discipline already present in the wait bridge. If direct
   HTTP remains an opt-in path, clearly warn that the bearer is stored in the
   isolated Zed settings file and provide rotation/removal.

4. **Stdio stdout must remain protocol-only.** A banner or diagnostic written
   to stdout would corrupt MCP framing. Direct HTTP avoided that failure mode
   in this proof; `pursers-personal mcp` was therefore not used. Any generated
   launcher must send diagnostics to stderr, following
   [`tools/wait-bridge/pursers_wait_server.py`](../../../tools/wait-bridge/pursers_wait_server.py),
   which already documents and implements that split.

5. **Board membership must precede the first tool call.** The observed denial
   before onboarding is expected authorization, not a transport defect. The
   extension must provision or join the selected identity first, then run a
   read-only `board_status` health check. It must never silently substitute a
   different seat identity.

6. **The full tool catalog is noisy.** Zed loaded all 53 Central tools, and the
   native Agent request reached 68 tools with built-ins. There is no Zed
   count-based failure here, but the schemas consume model context and broaden
   the action surface. The extension should create a conservative profile:
   enable read-only/status tools by default, require confirmation for
   mutations, and let the user opt into role-appropriate tools. Zed already
   supports per-profile context-server selection and per-tool permissions.

7. **Resources and native MCP elicitation are unavailable.** Expose required
   workflows as tools/prompts, or route interactive wait/elicitation behavior
   through the dedicated bridge/agent integration. Do not build the extension
   UI around MCP resources that Zed 1.20.2 does not surface.

## What the extension must automate

The eventual Zed extension should:

1. collect the Central URL, board, exact seat identity, and token-file path;
2. provision/onboard that identity before enabling tools;
3. install a credential-safe local launcher for the direct tool server and the
   stdio wait bridge, without copying bearer contents into repository files;
4. set a bounded timeout, test `initialize`/`tools/list`, and run a read-only
   `board_status` health check;
5. create a least-privilege Agent profile and tool-permission rules appropriate
   to worker, reviewer, or coordinator roles;
6. detect Zed's negotiated MCP version and disable or bridge 2026-07-28-only
   behavior when the result is 2025-11-25;
7. show actionable states for missing membership, expired credentials,
   unreachable Central, timeout, and protocol mismatch; and
8. remove generated configuration and rotate/revoke credentials cleanly on
   uninstall.

The manual proof establishes the transport and Agent Panel path. It does not
claim that a future extension has already automated credential custody,
onboarding, role-scoped profiles, or the v2 wait bridge.
