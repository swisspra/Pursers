# ACP seat protocol harness

This directory contains the ACP v1 protocol client and the P1 Pursers seat
runtime. `acp_client.py` is a dependency-free asyncio ACP client,
`pursers_acp_seat.py` maps one configured ACP subprocess to one verified board
seat, and `tests/fake_acp_agent.py` is a scriptable subprocess used for
conformance and in-process Central tests.

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

The `ACPClient` library remains intentionally narrow: filesystem and terminal
client methods are not advertised. Board dispatch, identity, lease renewal,
sandboxing, and submission belong to the separate seat runtime below.

The seat runtime keeps the mode-`0600` board token in the parent process,
verifies both identity IDs returned by Central, consumes wait-bridge offers,
creates one standalone Git clone per ticket, sends only ticket scope and
decision annotations to ACP, renews the lease independently, and projects
bounded updates and permission decisions to board checkpoints. On `end_turn`,
it validates structured completion against the actual branch, full commit, and
tip diff, publishes the branch from the parent process, and verifies the remote
commit before submitting. The last board mutation of a successful ticket is
`ticket_submit`; lease renewal stops before that terminal mutation. Cancellation,
agent failure, or invalid evidence
creates a checkpoint and safely unclaims the ticket.

ACP v1 does not standardize token usage. The `acp` host therefore records null
usage by omission unless the prompt result supplies the explicit numeric
extension `usage.{turns,reported_turns,input_tokens,output_tokens}`. The seat
accepts only complete non-negative counters and forwards them with the existing
ticket submission; it never estimates from text length and never stores prompt
or completion content.

The production runtime is macOS-only because it fails closed unless
`sandbox-exec` is available. Its OS profile denies network access and writes
outside the ticket clone and temporary directory. Every run receives a scratch
`HOME`, `XDG_CONFIG_HOME`, and global Git config containing only the seat's
configured commit identity; operator Git configuration is explicitly denied.
Mutable Git metadata stays inside that clone, so the sandboxed agent can stage
and commit without access to the source repository. The ACP permission broker
also canonicalizes requested paths, rejects protected Git/credential paths,
never selects `allow_always`, and permits terminal requests only with an argv
and cwd inside the clone.

The focused coverage lives in
[`tests/test_acp_client.py`](tests/test_acp_client.py) and
[`tests/test_pursers_acp_seat.py`](tests/test_pursers_acp_seat.py). The latter
uses the actual in-process Central server and MCP tools for create, offer,
claim, checkpoint, renewal, and submit; only the ACP subprocess is fake. This
proves the adapter path, not provider-authenticated browser or Gemini
acceptance. See [Getting started](../../docs/GETTING-STARTED.md#8-run-an-acp-agent-as-a-seat)
for the private config and `gemini --acp` command.

## Manual real-agent smoke

Follow the private config recipe in Getting started, choose a throwaway board,
and create a ticket that permits one harmless documentation edit. If Gemini CLI
needs existing login material, copy only that material to a dedicated narrow
path and add it to the policy's `fs_roots`; never add the operator home. Start
the seat with:

```sh
gemini --version
python3 tools/acp-seat/pursers_acp_seat.py \
  --config /PATH/TO/private/acp-seat.json
```

Offer the throwaway ticket to the configured seat, then verify that Central
records a claim, at least one bounded ACP checkpoint, lease renewal, and a
submitted exact branch/commit. Verify separately that a network permission is
rejected and an agent crash leaves the ticket open with a release checkpoint.
Delete the throwaway board data only after preserving non-secret test evidence.

**Sandbox execution status (2026-09-14):** not executed in sandbox: gemini not
installed, Ego Lite unavailable. Per coordinator decision, the deterministic
fake-agent/in-process-Central test is the executed P1 acceptance path; the
coordinator or operator runs this provider-authenticated smoke later. Do not
install Gemini or launch Ego Lite merely to run the repository suite.

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
