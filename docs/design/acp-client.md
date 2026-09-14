# Central as an ACP client

Status: design spike, 2026-09-14

This proposal makes any Agent Client Protocol (ACP) agent eligible to run as a
Pursers seat. Central remains the authority for identity, dispatch, durability,
and independent review. A small seat runtime acts as the ACP client and owns one
agent subprocess. ACP is an execution interface; it does not replace the board
protocol or its trust boundaries.

This revives the operator idea in section 2 of the earlier discussion and
supersedes the 2026-09-14 zero-ACP decision.

## Specification baseline

All external sources below were fetched on 2026-09-14. ACP's stable wire
protocol is version 1. The official repository was inspected at commit
`205918585fc99d97aa10b0d3619d5678dfcda712`; its latest stable schema artifact
was `schema-v1.21.0`, published 2026-08-20. The schema artifact version must not
be confused with the negotiated integer wire protocol version.

ACP uses JSON-RPC 2.0, normally between a client and an agent subprocess. The
client starts with `initialize`, advertising its filesystem and terminal
capabilities; the agent returns the negotiated protocol version and its
capabilities. A client creates a session with an absolute working directory and
then drives each turn with `session/prompt`. During a turn the agent emits
`session/update`; the prompt response ends with a `stopReason` such as
`end_turn`, `max_tokens`, `refusal`, or `cancelled`.

`session/load` is optional. When supported, it replays the conversation as
updates before returning. That is useful recovery input, but it is not durable
workflow state. Cancellation is client-initiated and the agent must resolve
pending permission requests as cancelled before ending the prompt as
`cancelled`.

The authoritative references are:

- [protocol overview](https://agentclientprotocol.com/protocol/v1/overview)
- [initialization](https://agentclientprotocol.com/protocol/v1/initialization)
- [session setup and loading](https://agentclientprotocol.com/protocol/v1/session-setup)
- [prompt turns and stop reasons](https://agentclientprotocol.com/protocol/v1/prompt-turn)
- [tool calls and permission requests](https://agentclientprotocol.com/protocol/v1/tool-calls)
- [filesystem methods](https://agentclientprotocol.com/protocol/v1/file-system)
- [terminal methods](https://agentclientprotocol.com/protocol/v1/terminals)
- [agent-provider authentication](https://agentclientprotocol.com/protocol/v1/authentication)
- [pinned protocol repository](https://github.com/agentclientprotocol/agent-client-protocol/tree/205918585fc99d97aa10b0d3619d5678dfcda712)
- [`schema-v1.21.0` release](https://github.com/agentclientprotocol/agent-client-protocol/releases/tag/schema-v1.21.0)
- [pinned v1 method schema](https://github.com/agentclientprotocol/agent-client-protocol/blob/205918585fc99d97aa10b0d3619d5678dfcda712/schema/v1/meta.json)

## Boundary and mapping

The adapter translates workflow events; it must not equate protocols more
strongly than their guarantees allow.

| Pursers operation | ACP operation | Adapter responsibility |
| --- | --- | --- |
| `board_onboard` | `initialize` + `session/new` | Verify the board seat, negotiate ACP v1, then bind each claimed ticket's new session `cwd` to that seat's worktree. |
| offer / `ticket_claim` | `session/prompt` | Claim before sending the full ticket and current decisions as a client-driven turn. |
| progress journal / memory notes | `session/update` | Project bounded plans, tool evidence, and checkpoints; never persist raw reasoning, secrets, or every streaming chunk. |
| `ticket_submit` | prompt `stopReason` | Treat a successful stop as a request to validate and submit, never as proof of completion. |
| cancel / `ticket_unclaim` | `session/cancel` | Cancel the prompt, settle permissions and child processes, then release only after the runtime regains control. |
| board catch-up | `session/load` | Combine authoritative board history with optional conversation replay; board state wins on disagreement. |

Three ACP gaps are deliberately covered by Pursers:

1. **No principal.** ACP authenticates an agent to its model provider but does
   not define the human or service principal allowed to mutate a board. Central
   derives the principal and seat identity from a verified board token.
2. **No durable workflow.** An ACP session may be transient and loading is
   optional. Tickets, claims, leases, journal events, review state, and explicit
   memory checkpoints remain durable board records.
3. **No agent-initiated turn.** ACP agents respond to client prompts. The seat
   runtime consumes offers, claims work, and initiates the prompt; the agent
   never needs to poll or initiate a board turn.

## Identity and authority

The runtime obtains a board-issued seat token/door and calls Central. Central's
verified token determines `principal_id`; the board, principal, and configured
seat name determine `agent_id`. The ACP process cannot assert either value,
choose another seat name, read the token, or invoke board mutations directly.
ACP `authenticate` is separate agent-provider authentication and grants no board
authority.

The concrete behavior already exists in
[`central.py`](../../packages/central/src/pursers_central/central.py) and
[`client.py`](../../packages/client/src/pursers_client/client.py): onboarding
returns the verified identity and claims, renewals, submissions, and unclaims
are checked against it. The runtime passes ticket text into the ACP session but
keeps credentials and authorization context out of prompts, environment
variables, updates, and persisted session state.

## Seat runtime

Add `tools/acp-seat/` beside the existing
[`worker-runtime`](../../tools/worker-runtime/pursers_worker.py). The first
implementation should reuse its board adapter, isolated-worktree, path-jail,
sanitized-environment, output-limit, and lease-renewal semantics. Shared pieces
may be extracted later; the initial spike should not entangle ACP framing with
the existing chat-worker loop.

There is exactly one ACP subprocess and at most one active ticket per seat. The
runtime persists only restart-safe metadata: board and verified seat IDs,
ticket and worktree, ACP implementation/version/capability snapshot, session ID,
whether a prompt was in flight, and journal cursor. The board token remains in
its mode-0600 token file and is never copied into state.

The normal lifecycle is:

1. Onboard using the seat token and verify the returned agent and principal.
2. Spawn the configured agent, `initialize` at protocol v1, and record its
   actual capabilities.
3. Wait for an offer, claim it, fetch the latest ticket and decisions, create a
   session rooted at its isolated worktree, and send one prompt containing that
   authoritative scope.
4. While the prompt is pending, renew the board lease independently of agent
   output. Convert selected updates into bounded progress evidence.
5. On `end_turn`, validate that the claim is still held, paths and changed files
   are in scope, tests and evidence are real, the commit is reachable, and the
   structured completion is sufficient. Only then submit. Other stop reasons
   cause a bounded retry, coordinator question, or safe unclaim.

`end_turn` alone never means “ticket complete.” Likewise, an update claiming a
test passed is evidence to verify, not an authorization decision.

### Crash and restart

On crash, stop child terminals and the ACP subprocess. Restart with the same
token file, re-onboard the same identity, inspect only that seat's claim, and
recreate or recover its worktree. If advertised, `session/load` (or resume) may
restore conversation context. Otherwise start a new session and prompt it with
a bounded board catch-up plus the last explicit checkpoint. Never claim a
second ticket during recovery.

The lease-renewal task belongs to the runtime, not to the ACP prompt. A runtime
crash therefore stops renewal until recovery. Recovery either reissues the
prompt with a persisted idempotency key and verified state or cancels and
unclaims; it must not silently submit a duplicated turn. Startup orphan cleanup
should follow the existing worker runtime and
[`WORKER-DIRECTIVE.md`](../../tools/wait-bridge/WORKER-DIRECTIVE.md).

## Permission policy

Central's ACP client is non-interactive. It answers
`session/request_permission` from an immutable per-ticket board-policy snapshot
and only with an option the agent actually supplied. If no offered option is
safe, it selects `reject_once`; P0 and P1 never select an `allow_always` option.

| Request | Automatic decision |
| --- | --- |
| Filesystem read | `allow_once` only for a canonical real path inside the current seat worktree, excluding credential and token paths. |
| Filesystem write | `allow_once` only for a worker with a live claim, an allowed ticket path, and a canonical destination inside the worktree; reject secret paths and protected Git metadata. Review seats reject writes. |
| Terminal | `allow_once` only after validating argv, cwd, environment, timeout, output, and process limits. The cwd must be in the worktree and credential variables are removed. |
| Network | ACP v1 exposes no generic client network capability. Enforce egress in the OS/runtime sandbox: default deny, or allow only board-policy destinations. Never infer network access from a tool permission request. |

ACP filesystem paths are absolute. The runtime must resolve symlinks, junctions,
and mount boundaries and fail closed on escape. Because an agent can also use
its own filesystem APIs, `additionalDirectories` and proxied filesystem methods
are not a sandbox: the subprocess itself needs the same OS boundary. On cancel,
pending permission requests receive `cancelled` and no command starts afterward.

## Candidate agents

Registry metadata was inspected at commit
`fd2cc1d622a88e2e8f1fc9c8807417c4856b1ca2`, release
`v2026.09.14-fd2cc1d`. Advertised features are discovery hints; the runtime's
actual `initialize` response is authoritative and is saved with the run.

| Agent | Registry version | Advertised ACP surface | Position |
| --- | --- | --- | --- |
| Gemini CLI | 0.59.0, `gemini --acp` | Native initialize/auth, new/load/prompt/cancel, modes, proxied filesystem, MCP | **First target:** official native mode, open source, and a focused stable surface. |
| Codex ACP | 1.11.0 | Auth, models/modes, shell/files/permissions, MCP, terminals, plans, web/image and review events | Second target; broad coverage is valuable after the boundary is proven. |
| Claude Agent ACP | 0.76.0 | Permissions, edits, tasks, nested agents, foreground/background terminals, client MCP and extensions | Third target; broad proprietary adapter increases the initial test matrix. |

Sources: [pinned registry](https://github.com/agentclientprotocol/registry/tree/fd2cc1d622a88e2e8f1fc9c8807417c4856b1ca2),
[registry release](https://github.com/agentclientprotocol/registry/releases/tag/v2026.09.14-fd2cc1d),
[Gemini entry](https://github.com/agentclientprotocol/registry/blob/fd2cc1d622a88e2e8f1fc9c8807417c4856b1ca2/gemini/agent.json),
[Gemini ACP mode](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/acp-mode.md),
[Codex entry](https://github.com/agentclientprotocol/registry/blob/fd2cc1d622a88e2e8f1fc9c8807417c4856b1ca2/codex-acp/agent.json),
[Codex ACP](https://github.com/agentclientprotocol/codex-acp),
[Claude entry](https://github.com/agentclientprotocol/registry/blob/fd2cc1d622a88e2e8f1fc9c8807417c4856b1ca2/claude-acp/agent.json), and
[Claude Agent ACP](https://github.com/agentclientprotocol/claude-agent-acp).

## Delivery phases

### P0: fake-agent conformance harness

Build a deterministic stdio agent and fake Board API. Exercise JSON-RPC framing,
invalid/out-of-order/oversize messages, capability negotiation, every permission
branch, cancel during permission and terminal work, crash before and after a
stop response, load/no-load recovery, lease timing, bounded update projection,
and absence of tokens from prompts, environment, logs, and state.

### P1: one real agent, one sandbox ticket

Pin the registry and Gemini versions. Run one bounded documentation ticket on a
sandbox board with an isolated worktree and default-deny egress. Preserve the
initialize capability snapshot, offer/claim/prompt/update/renew/submit trace,
exact commit and tests, permission decisions, and crash-free teardown. A
different principal reviews the result.

### P2: adapters without governance drift

Exercise pinned Codex and Claude implementations through the same generic ACP
client, then widen ticket classes only from evidence. Implementation-specific
extensions may have policy profiles but must not require per-host dispatch
adapters. Worker and reviewer remain distinct board identities. ACP modes,
provider authentication, extensions, and model choice cannot relax ticket
scope, permission policy, evidence requirements, or independent review.

## Preconditions, risks, and non-goals

The process-spawned-identity bug ticket `TK-e5cfe34cebdf` must be merged before
P0. This precondition is already satisfied on the design base by merge commit
`b737a7a8f9e89423087fdb55869f280076386a77`, which makes dispatcher offers depend
on live eligible seats rather than process-discovered names. Implementations
must retain a regression test for that behavior.

Primary risks and mitigations are:

- stdio deadlock/backpressure and orphan processes: bounded queues, timeouts,
  process groups, deterministic teardown;
- duplicated prompts after crashes: persisted idempotency keys and board-first
  reconciliation before resume;
- replay divergence: board state is authoritative and ACP replay is context only;
- ambiguous or forged tool metadata: validate concrete paths, argv, and policy,
  not display text;
- direct filesystem or network bypass: OS sandbox the whole agent process;
- board/provider authentication confusion: separate credentials and never pass
  the board token to ACP;
- secret-bearing or unbounded updates: redact, cap, and persist only selected
  evidence/checkpoints;
- mutable registry entries or capability drift: pin artifacts and verify the
  initialize response on every spawn;
- over-trusting stop reasons: require runtime validation before board mutation;
- unstable future protocol changes: remain on negotiated ACP v1 until a later
  design explicitly adopts another major version.

Non-goals are embedding an ACP process in Central, replacing the board API with
ACP, remote multi-user ACP transport, interactive permission prompts, weakening
review independence, or adopting an unstable ACP v2 surface.
