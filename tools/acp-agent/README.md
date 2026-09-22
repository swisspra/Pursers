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
Central authorization. Each watch owns a configured `pursers-wait-bridge`
stdio process, calls `a2a_wait` with a one-board list, reuses each positive
cursor unchanged, and tears the process down on cancellation. The ACP agent
does not open Central's journal or call `board_catchup` directly.

## Protocol baseline

Verified from the official documentation on 2026-09-14:

- [ACP v1 overview](https://agentclientprotocol.com/protocol/v1/overview)
- [initialization and capabilities](https://agentclientprotocol.com/protocol/v1/initialization)
- [session setup](https://agentclientprotocol.com/protocol/v1/session-setup)
- [prompt turns](https://agentclientprotocol.com/protocol/v1/prompt-turn)
- [tool calls and permission requests](https://agentclientprotocol.com/protocol/v1/tool-calls)
- [authentication](https://agentclientprotocol.com/protocol/v1/authentication)

The agent advertises only baseline text and resource-link prompts. It does not
advertise `loadSession`, filesystem, terminal, image, audio, embedded context,
editing, or model capabilities. As ACP v1 requires, `session/new` accepts and
initializes non-empty stdio `mcpServers` descriptors; those servers are scoped
to the session and closed with it.

If no usable profile exists, protocol `authenticate` and the terminal
`--login` method run the existing `pursers-personal setup` flow, then reconnect.
The setup child receives a narrow environment that excludes inherited board
tokens and token-file variables. Profile credentials are never placed in ACP
messages, IDE settings, command arguments, or registry metadata.

## Development launch

Install the package from `tools/acp-agent`, then select an existing Personal
profile with `ONBOARD_PERSONAL_PROFILE` or a project path. `--login` creates and
activates the Personal setup when the project has no profile yet:

```sh
pursers-acp --project /PATH/TO/PROJECT
pursers-acp --project /PATH/TO/PROJECT --login
```

## Published launch

Install the released package in its own environment, or run the exact release
with `uvx`. Point it at a project that already has a Pursers Personal profile;
add `--login` only when you want the existing Personal setup flow to create and
activate one:

```sh
python -m pip install "pursers-acp==0.1.2"
pursers-acp --project /PATH/TO/PROJECT

uvx --from "pursers-acp==0.1.2" pursers-acp --project /PATH/TO/PROJECT
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
agent servers are deprecated. `pursers/agent.json` and `pursers/icon.svg` are a
ready-to-copy upstream registry directory. They are validated against the
schema pinned in the tests and contain only public launch metadata. See
[`docs/zed/acp-registry-submission.md`](../../docs/zed/acp-registry-submission.md)
for the exact operator-owned validation and pull-request procedure.
