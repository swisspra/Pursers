# ACP seat protocol harness

This directory contains the protocol-only P0 for running an Agent Client
Protocol agent as a Pursers seat. It deliberately has no board integration.
`acp_client.py` is a dependency-free asyncio ACP client and
`tests/fake_acp_agent.py` is a scriptable agent subprocess used for conformance
tests.

## Protocol baseline

The implementation targets ACP wire protocol **version 1**. The following
official sources were fetched on 2026-09-14:

- [ACP v1 overview](https://agentclientprotocol.com/protocol/v1/overview)
- [initialization and capability negotiation](https://agentclientprotocol.com/protocol/v1/initialization)
- [session setup and optional loading](https://agentclientprotocol.com/protocol/v1/session-setup)
- [prompt turns, updates, cancellation, and stop reasons](https://agentclientprotocol.com/protocol/v1/prompt-turn)
- [permission requests and outcomes](https://agentclientprotocol.com/protocol/v1/tool-calls)
- [official repository at the inspected commit](https://github.com/agentclientprotocol/agent-client-protocol/tree/205918585fc99d97aa10b0d3619d5678dfcda712)
- [stable `schema-v1.21.0` artifact](https://github.com/agentclientprotocol/agent-client-protocol/releases/tag/schema-v1.21.0)

ACP v1 negotiates a single integer major version. `schema-v1.21.0` is the
versioned schema artifact inspected for this implementation, not a different
wire-protocol major.

The baseline implemented here is JSON-RPC 2.0 over newline-delimited subprocess
stdio: `initialize`, `session/new`, `session/prompt`, `session/update`,
`session/request_permission`, and `session/cancel`. `session/load` is implemented
when the agent advertises `agentCapabilities.loadSession`.

## Client API

`ACPClient` spawns one command, negotiates capabilities, creates or loads a
session, runs prompt turns, exposes updates through `next_update()` or the
`updates()` async iterator, resolves permission requests through a callback,
cancels turns, applies request/update timeouts, and fails pending calls if the
subprocess exits or violates framing.

The permission callback receives the complete `session/request_permission`
params object. It may return an offered option ID, an ACP outcome object, or
`None` for cancellation. Selecting an option that the agent did not offer is a
protocol error. Cancelling a prompt preempts an asynchronous callback and
returns ACP's `cancelled` permission outcome.

The library is intentionally narrow. Filesystem and terminal client methods,
board dispatch, identity, lease renewal, sandboxing, persistence, and submission
belong to later phases.

Repository integration is limited to the required suite entry in
[`tools/ci_manifest.py`](../ci_manifest.py). The focused coverage lives in
[`tests/test_acp_client.py`](tests/test_acp_client.py) and exercises the
subprocess fake directly; it does not fabricate board responses or claim board
acceptance.

## Fake-agent scripts

Run the fake agent with a JSON script:

```sh
python3 tools/acp-seat/tests/fake_acp_agent.py --script /PATH/TO/script.json
```

Top-level fields include:

- `protocolVersion`: override the initialize response for negotiation tests;
- `sessionId`: fixed ID returned by `session/new`;
- `loadSession` / `--load-session`: advertise and implement `session/load`;
- `loadUpdates`: update objects replayed during `session/load`;
- `promptActions`: ordered scripted actions;
- `stopReason`: final reason, defaulting to `end_turn`.

Supported prompt actions are `update`, `permission`, `sleep`,
`wait_for_cancel`, `crash`, and `raw`. A permission action may provide
`toolCall`, `options`, and `expectedOutcome`; a mismatch returns a scripted
JSON-RPC error. `raw` exists only for invalid-framing tests.

Example:

```json
{
  "sessionId": "demo",
  "promptActions": [
    {
      "type": "update",
      "update": {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "working"}
      }
    },
    {
      "type": "permission",
      "expectedOutcome": {
        "outcome": "selected",
        "optionId": "reject-once"
      }
    }
  ],
  "stopReason": "end_turn"
}
```

Run the focused suite with:

```sh
python3 -m pytest -q tools/acp-seat/tests
```
