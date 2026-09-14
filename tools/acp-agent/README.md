# Pursers ACP agent

`pursers-acp` is a small ACP v1 board assistant for IDE hosts. It uses an
existing human Pursers Personal profile and never onboards, claims work, reads
source files, launches terminals, or accepts a seat token.

The initial intent surface is deliberately deterministic:

- `my tickets`
- `my offers`
- `board status`
- `create ticket <title> :: <description>`
- `annotate TK-… <text>`
- `watch <board>` (cancel the ACP turn to stop the subscription)

Every create or annotate action is displayed as an exact `rawInput` payload in
`session/request_permission`. Only `allow_once` executes it. Reads need only
Central authorization. Watches subscribe before establishing the starting
cursor, reuse each positive cursor, and call pure `board_catchup` after push
cues; they do not poll or reset to cursor zero.

## Protocol baseline

Verified from the official documentation on 2026-09-14:

- [ACP v1 overview](https://agentclientprotocol.com/protocol/v1/overview)
- [initialization and capabilities](https://agentclientprotocol.com/protocol/v1/initialization)
- [session setup](https://agentclientprotocol.com/protocol/v1/session-setup)
- [prompt turns](https://agentclientprotocol.com/protocol/v1/prompt-turn)
- [tool calls and permission requests](https://agentclientprotocol.com/protocol/v1/tool-calls)
- [authentication](https://agentclientprotocol.com/protocol/v1/authentication)

The agent advertises only baseline text and resource-link prompts. It does not
advertise `loadSession`, filesystem, terminal, MCP, image, audio, embedded
context, editing, or model capabilities.

## Development launch

Install the package from `tools/acp-agent`, then select an existing Personal
profile with `ONBOARD_PERSONAL_PROFILE` or a project path:

```sh
pursers-acp --project /PATH/TO/PROJECT
```

For current Zed development builds, the custom agent configuration is:

```json
{
  "agent_servers": {
    "pursers": {
      "type": "custom",
      "command": "pursers-acp",
      "args": ["--project", "/PATH/TO/PROJECT"],
      "env": {}
    }
  }
}
```

This shape was validated against the official
[Zed External Agents documentation](https://zed.dev/docs/ai/external-agents)
on 2026-09-14. Published installs should use the ACP Registry; extension-based
agent servers are deprecated.
