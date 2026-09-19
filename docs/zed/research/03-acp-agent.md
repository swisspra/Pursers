# Zed R3: `pursers-acp` as an external agent

Research date: 2026-09-19. The tested host was the installed Zed
`1.20.2+stable.360.7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f`; the source
baseline is the matching upstream tag
[`v1.20.2`](https://github.com/zed-industries/zed/tree/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f).

## Result

`pursers-acp` 0.1.0 is protocol-compatible with Zed 1.20.2 for its current,
small surface. Zed requested ACP protocol version `1`; the agent returned
version `1`; Zed created a session; and a `board status` prompt reached the
agent and returned data from a throwaway loopback Central. There was no
protocol-version error.

This is not yet a “Pursers software house” agent. Today it is a deterministic
Pursers Personal board assistant. It does not run a model, inspect or edit the
project, use a terminal, claim or submit work, review work, or perform
coordinator operations. It should be offered as a focused secondary board
console. For the primary Zed workflow today, use a Pursers MCP context server
with Zed Agent: Zed Agent supplies the coding/reasoning loop and Zed supports
MCP tools and prompts directly. Zed notes that tool selection remains
model-dependent, so a dedicated MCP-only profile is appropriate where a board
operation must be selected reliably. Sources: [Zed MCP documentation],
[`pursers-acp` README], and [agent dispatch implementation].

## How Zed 1.20.2 launches an external agent

The supported development configuration is the global `agent_servers` map.
Each key is the agent ID. A custom entry has `type: "custom"`, `command`,
optional `args`, and optional string-to-string `env`; Zed also accepts
`default_mode`, `default_config_options`, and
`favorite_config_option_values`. This is both the documented shape and the
shape deserialized by 1.20.2. Sources: [Zed External Agents documentation] and
[Zed 1.20.2 settings type].

Use an absolute executable path because a GUI application's `PATH` need not be
the same as an interactive shell's. The normal project-selected setup is:

```json
{
  "agent_servers": {
    "pursers": {
      "type": "custom",
      "command": "/PATH/TO/VENV/bin/pursers-acp",
      "args": ["--project", "/PATH/TO/PROJECT"],
      "env": {
        "TMPDIR": "/PATH/TO/USER-CACHE/tmp"
      }
    }
  }
}
```

The project must already have a usable Pursers Personal profile. If the
profile is outside the default Personal profile root, select it explicitly:

```json
{
  "agent_servers": {
    "pursers": {
      "type": "custom",
      "command": "/PATH/TO/VENV/bin/pursers-acp",
      "args": ["--profile", "/PATH/TO/PRIVATE/profile.json"],
      "env": {
        "TMPDIR": "/PATH/TO/USER-CACHE/tmp"
      }
    }
  }
}
```

Do not put a JWT, door, API key, or token-file content in `args` or `env`.
`pursers-acp` selects the Personal profile and reads its protected capability
file itself. Its supported CLI selectors and secret-handling boundary are in
[`agent.py`] and the existing [client connection guide].

### Extensions and the ACP Registry

An extension cannot register an agent server in Zed 1.20.2. Agent-server
extensions are deprecated in favor of the ACP Registry; the 1.20.2
`ExtensionManifest` has no `agent_servers` field, and its `Extension` trait has
no agent-server command hook. Sources: [Zed agent-server extension docs],
[1.20.2 `ExtensionManifest`], and [1.20.2 `Extension` trait].

The source still contains the orphaned historical `AgentServerManifestEntry`
shape: an old `[agent_servers.<id>]` entry had `name`, `env`, `icon`, and
per-platform `[agent_servers.<id>.targets.<os>-<arch>]` records containing
`archive`, `cmd`, `args`, optional `sha256`, and target `env`. That type is not
a field of the current manifest and is not an operative registration route.
Source: [historical manifest structs retained in 1.20.2].

The replacement is a real Zed registry store. Zed 1.20.2 fetches the ACP
Registry v1 index, represents binary and npm agents, installs selected agents,
and migrates a fixed set of old extension IDs to registry IDs. Sources:
[registry store] and [agent-server store]. `pursers/agent.json` is already a
registry-format descriptor, but manual `agent_servers` configuration remains
the appropriate development path until that descriptor is accepted and
discoverable in the public registry.

## Protocol version and capabilities

ACP uses JSON-RPC 2.0. The baseline flow is `initialize`, optional
`authenticate`, `session/new`, `session/prompt`, `session/update`, and optional
`session/cancel`; capabilities gate optional session, filesystem, terminal,
mode, and other methods. Source: [ACP v1 overview].

Zed 1.20.2 depends on Rust `agent-client-protocol = 2.0.0`, but that is the SDK
crate release, not the wire protocol number. On the wire, Zed explicitly sends
`ProtocolVersion::V1` and rejects only a response below v1. Sources:
[Zed dependency] and [Zed initialization].

| Area | What Zed 1.20.2 implements | What `pursers-acp` 0.1.0 implements |
| --- | --- | --- |
| Initialization | Sends v1, client info, and capabilities. | Returns v1 unconditionally and advertises only empty `promptCapabilities`, agent info, and auth methods. |
| Sessions | Always uses `session/new`; conditionally supports list, load, resume, close, and delete when the agent advertises them. | Implements only `session/new`; validates an absolute `cwd`; starts and probes forwarded stdio MCP servers; returns an in-memory session ID. |
| Prompts | Sends `session/prompt` and notifies `session/cancel`. | Implements both. Accepts text; silently ignores `resource_link` blocks; rejects other content types. |
| Output and tool calls | Handles `session/update`, including message chunks, tool calls/updates, plans, available commands, and mode/config updates. | Sends agent-message chunks and tool-call/tool-call-update records. It does not send plans, available-command updates, or mode/config updates. |
| Permission requests | Handles `session/request_permission` in the Agent Panel. | Uses it for every `ticket_create` and `ticket_annotate`; only `allow_once` executes the write. |
| Filesystem | Advertises and handles `fs/read_text_file` and `fs/write_text_file`. | Does not call either method and does not advertise image/audio/embedded-context support. |
| Terminals | Advertises terminal support and handles create, output, release, wait, and kill. | Does not call terminal methods. It may advertise a terminal auth method only when no Personal profile is usable and the client offered terminal auth. |
| Slash commands | Renders agent-advertised available commands and submits selected commands through the prompt turn. | Does not advertise slash commands; its command grammar is plain prompt text. |
| Modes | Reads modes returned by session setup and sends `session/set_mode`. | Does not return modes or implement `session/set_mode`. |
| MCP forwarding | Adds configured Zed MCP servers to session setup. | Connects to every forwarded stdio server and calls `tools/list`, but no prompt command invokes those tools. |

Zed-side sources for this matrix: [client capability declaration], [client
handlers], [session and prompt implementation], and [slash-command update
handling]. Agent-side sources: [protocol dispatch], [agent session setup], [prompt
grammar], [permission request], and [forwarded MCP startup]. The ACP
capability contracts are described by [initialization], [session setup],
[prompt turns], [tool calls], [filesystem], [terminals], [slash commands], and
[session modes].

There is one negotiation weakness that did not block Zed: the agent returns
`1` even when the client requests a different version. A future implementation
should select a mutually supported version or fail initialization rather than
silently claiming v1. Source: [agent initialization].

## What the agent does on the board

The agent uses an existing human Personal profile. During initialization it
calls `board_status`, finds agents belonging to the profile's principal,
prefers an active identity, and records that identity's name for the two
permitted writes. It never onboards a new seat. Source: [Personal board
connection].

Its complete prompt vocabulary is:

- `my tickets`: combines `board_status` and `ticket_list`, then keeps tickets
  created by or claimed by the Personal principal, or assigned to one of that
  principal's agent IDs.
- `my offers`: reports current offers for identities owned by the principal.
- `board status`: returns a bounded selection of board fields.
- `create ticket <title> :: <description>`: requests permission, then creates
  one unassigned `interactive-no-send` ticket.
- `annotate TK-… <text>`: requests permission, then adds a note.
- `watch <board>`: starts one wait-bridge subprocess for the profile's own
  board and streams bounded event summaries until the turn is cancelled.

Sources: [board reads and writes], [watch implementation], and [prompt
dispatch]. There are no claim, renew, submit, review, assignment, policy,
coordinator-question, memory, state, handoff, repository, model, filesystem,
or terminal operations in the dispatch table. Therefore the process uses an
existing Personal principal and an existing display identity for attribution;
it does not itself represent a dedicated worker seat or a coordinator
conversation.

## Isolated Zed acceptance

The acceptance run used:

- the installed Zed 1.20.2 binary and
  `--user-data-dir /PATH/TO/ISOLATED/ZED-DATA`;
- an isolated settings file under that data directory;
- a private Personal profile under `/PATH/TO/USER-CACHE`;
- a newly generated board and Central SQLite data directory;
- a random non-default loopback port; and
- a transparent stdio recorder around the repository's exact `pursers-acp`
  source.

The operator's Zed config, normal Application Support directory, installed
extensions, and live Central ports were not used. The only user prompt was the
read-only `board status` command. The Agent Panel displayed the same board
response present in the transcript.

The following lines are literal protocol records from that run, except that
the absolute project path is replaced with the required
`/PATH/TO/PROJECT` placeholder. No token or door value appears in the trace.

```text
C> {"jsonrpc":"2.0","id":"9833c79c-89a1-4d9a-84e1-17af69e1b543","method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{"fs":{"readTextFile":true,"writeTextFile":true},"terminal":true,"session":{"configOptions":{"boolean":{}}},"auth":{"terminal":true},"elicitation":{"form":{},"url":{}},"_meta":{"terminal_output":true,"terminal-auth":true}},"clientInfo":{"name":"zed","title":"Zed","version":"1.20.2+stable.360.7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f"}}}
A> {"jsonrpc":"2.0","id":"9833c79c-89a1-4d9a-84e1-17af69e1b543","result":{"protocolVersion":1,"agentCapabilities":{"promptCapabilities":{}},"agentInfo":{"name":"pursers-acp","title":"Pursers Board Assistant","version":"0.1.0"},"authMethods":[{"id":"pursers-personal-profile","name":"Use Pursers Personal","description":"Use the selected existing human Personal profile","type":"agent"}]}}
C> {"jsonrpc":"2.0","id":"de6a7bbe-3cc6-43f9-a51f-c6b5d3e0999d","method":"session/new","params":{"cwd":"/PATH/TO/PROJECT","mcpServers":[]}}
A> {"jsonrpc":"2.0","id":"de6a7bbe-3cc6-43f9-a51f-c6b5d3e0999d","result":{"sessionId":"pursers-d0d8159008f24d73b35e9209ae03e05a"}}
C> {"jsonrpc":"2.0","id":"c3543e60-b186-4fcf-a66a-2c878f092195","method":"session/prompt","params":{"sessionId":"pursers-d0d8159008f24d73b35e9209ae03e05a","prompt":[{"type":"text","text":"board status"}]}}
A> {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"pursers-d0d8159008f24d73b35e9209ae03e05a","update":{"sessionUpdate":"agent_message_chunk","messageId":"msg-8438f4b2171846518b04ee879aa9c39d","content":{"type":"text","text":"Board status\n\n```json\n{\n  \"board_id\": \"tk-8eb59f848649-8d1d1cc4d0654fc193c71705\",\n  \"dispatch_enabled\": true,\n  \"latest_seq\": 1,\n  \"review_policy\": \"strict\",\n  \"status_counts\": null\n}\n```"}}}}
A> {"jsonrpc":"2.0","id":"c3543e60-b186-4fcf-a66a-2c878f092195","result":{"stopReason":"end_turn"}}
```

The source-module launch also wrote a Python `runpy` warning because
`pursers_acp.__init__` imports `pursers_acp.agent` before
`python -m pursers_acp.agent` executes it. The documented console-script launch above
does not use that diagnostic-only module invocation, and the warning did not
enter stdout or affect ACP framing.

## Primary-surface decision

Use the MCP context server as the primary surface for **running a Pursers
software house from Zed today**. Zed Agent already owns the conversational
reasoning, project context, editing, terminal, and tool-permission loop; MCP
adds Pursers tools and prompts to that capable agent. In contrast,
`pursers-acp` replaces Zed Agent for the thread but currently supplies only six
fixed board commands and no coding or coordinator loop. Sources: [Zed MCP
documentation], [Zed External Agents documentation], and [agent dispatch
implementation].

Keep ACP as the preferred long-term dedicated Pursers operator experience:
it has a first-class thread identity, can stream board activity, and owns its
permission prompts and interaction model. That becomes the better primary
surface only after it can represent the intended coordinator/seat semantics
and either perform or delegate the full work loop. This is a product
recommendation inferred from the verified capability differences above, not a
claim made by either protocol specification.

## Build-phase gaps

1. **Identity contract:** decide whether an ACP thread is a human Personal
   board console, a dedicated coordinator identity, or a worker seat. Do not
   keep selecting an arbitrary existing identity for write attribution if the
   product calls the thread a coordinator.
2. **Full workflow:** add the bounded coordinator/worker operations needed to
   create, route, inspect, claim, renew, submit, review, and resolve work—or
   explicitly delegate those operations to a separate seat runtime. Preserve
   permission prompts for every write.
3. **Real agent loop:** add model/reasoning and repository execution, or state
   clearly that the ACP thread is only an operator console. Without this, MCP
   plus Zed Agent remains the functional software-house surface.
4. **MCP forwarding:** expose forwarded `mcpServers` to the turn implementation
   or stop claiming more than validation/lifecycle support. The current code
   connects and lists tools but never calls them.
5. **Session lifecycle:** persist sessions and advertise only implemented
   list/load/resume/close/delete capabilities so Zed thread history and import
   can work.
6. **Discoverability:** advertise the fixed prompt vocabulary through ACP
   available-command updates; add modes only if they map to real, enforced
   behavior.
7. **Version negotiation:** fail unsupported versions or negotiate a shared
   version instead of always returning v1.
8. **Content capability accuracy:** either implement and advertise richer
   prompt content, filesystem, and terminal use, or continue to advertise the
   narrow text/resource-link surface and test Zed's rendering of rejection
   errors.
9. **Remote Central:** design a file-backed, private-CA-capable profile path if
   a Zed ACP thread must reach a non-Personal Central. Never move bearer values
   into Zed settings.
10. **Distribution:** publish and verify the registry entry, target artifacts,
    checksums, and upgrade behavior; keep manual custom-agent setup as the
    development fallback.
11. **Acceptance automation:** retain a secret-free stdio transcript assertion
    for initialize/new/prompt/update/result and a manual isolated-Zed smoke
    test for the Agent Panel and permission UI.

## Sources

- [Zed External Agents documentation]: https://zed.dev/docs/ai/external-agents
- [Zed MCP documentation]: https://zed.dev/docs/ai/mcp
- [Zed agent-server extension docs]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/extensions/agent-servers.md
- [Zed 1.20.2 settings type]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/settings_content/src/agent.rs#L666-L797
- [1.20.2 `ExtensionManifest`]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension/src/extension_manifest.rs#L83-L123
- [1.20.2 `Extension` trait]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension/src/extension.rs#L49-L159
- [historical manifest structs retained in 1.20.2]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension/src/extension_manifest.rs#L232-L285
- [registry store]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/project/src/agent_registry_store.rs#L15-L163
- [agent-server store]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/project/src/agent_server_store.rs#L209-L444
- [Zed dependency]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/Cargo.toml#L519
- [Zed initialization]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L990-L1024
- [client capability declaration]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L766-L794
- [client handlers]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L677-L764
- [session and prompt implementation]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L1637-L1983
- [slash-command update handling]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/acp_thread/src/acp_thread.rs#L2615-L2640
- [ACP v1 overview]: https://agentclientprotocol.com/protocol/v1/overview
- [initialization]: https://agentclientprotocol.com/protocol/v1/initialization
- [session setup]: https://agentclientprotocol.com/protocol/v1/session-setup
- [prompt turns]: https://agentclientprotocol.com/protocol/v1/prompt-turn
- [tool calls]: https://agentclientprotocol.com/protocol/v1/tool-calls
- [filesystem]: https://agentclientprotocol.com/protocol/v1/file-system
- [terminals]: https://agentclientprotocol.com/protocol/v1/terminals
- [slash commands]: https://agentclientprotocol.com/protocol/v1/slash-commands
- [session modes]: https://agentclientprotocol.com/protocol/v1/session-modes
- [`pursers-acp` README]: ../../../tools/acp-agent/README.md
- [`agent.py`]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L1092
- [client connection guide]: ../../guides/connecting-clients.md#zed-and-other-acp-hosts
- [protocol dispatch]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L392
- [agent initialization]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L456
- [agent session setup]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L533
- [prompt grammar]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L825
- [permission request]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L733
- [forwarded MCP startup]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L949
- [Personal board connection]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L117
- [board reads and writes]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L192
- [watch implementation]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L287
- [prompt dispatch]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L619
- [agent dispatch implementation]: ../../../tools/acp-agent/src/pursers_acp/agent.py#L619
