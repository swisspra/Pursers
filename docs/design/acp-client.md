# ACP in both directions

Status: design spike, 2026-09-14

This proposal covers Central as an ACP client, so any ACP agent can run as a
Pursers seat, and Pursers as an ACP agent, so a human can use a board from an
ACP-capable IDE. Central remains the authority for identity, dispatch,
durability, and independent review. ACP is an interface; it does not replace
the board protocol or its trust boundaries.

This revives the operator idea in section 2 of the earlier discussion and
supersedes the 2026-09-14 zero-ACP decision.

## Specification baseline

All external sources below were fetched on 2026-09-14. ACP's stable wire
protocol is version 1. The official repository was inspected at commit
`205918585fc99d97aa10b0d3619d5678dfcda712`; its latest stable schema artifact
was `schema-v1.21.0`, published 2026-08-20. The schema artifact version must not
be confused with the negotiated integer wire protocol version.

That snapshot also contains `schema-v2.0.0-alpha.3` with
`protocolVersion: 2`. The official migration guide still labels the whole v2
surface draft. Both implementations therefore target stable v1 first. They may
later offer v2 behind a feature flag, negotiate each connection independently,
and retain v1. A v2 path must account for prompt acceptance no longer ending a
turn, completion moving to `state_update`, `session/load` becoming replaying
`session/resume`, and removal of client filesystem and execution methods.

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
- [v1-to-v2 migration](https://agentclientprotocol.com/protocol/v2/migration)
- [current ACP clients](https://agentclientprotocol.com/get-started/clients)
- [current ACP Registry](https://agentclientprotocol.com/get-started/registry)

## Direction A: Central as an ACP client

### Boundary and mapping

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
`134db9fa124273eed9133d0fd26a8d3039ea2f2a`, release
`v2026.09.14-134db9f`. Advertised features are discovery hints; the runtime's
actual `initialize` response is authoritative and is saved with the run.

| Agent | Registry version | Advertised ACP surface | Position |
| --- | --- | --- | --- |
| Gemini CLI | 0.59.0, `gemini --acp` | Native initialize/auth, new/load/prompt/cancel, modes, proxied filesystem, MCP | **First target:** official native mode, open source, and a focused stable surface. |
| Codex ACP | 1.11.0 | Auth, models/modes, shell/files/permissions, MCP, terminals, plans, web/image and review events | Second target; broad coverage is valuable after the boundary is proven. |
| Claude Agent ACP | 0.76.0 | Permissions, edits, tasks, nested agents, foreground/background terminals, client MCP and extensions | Third target; broad proprietary adapter increases the initial test matrix. |

Sources: [pinned registry](https://github.com/agentclientprotocol/registry/tree/134db9fa124273eed9133d0fd26a8d3039ea2f2a),
[registry release](https://github.com/agentclientprotocol/registry/releases/tag/v2026.09.14-134db9f),
[Gemini entry](https://github.com/agentclientprotocol/registry/blob/134db9fa124273eed9133d0fd26a8d3039ea2f2a/gemini/agent.json),
[Gemini ACP mode](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/acp-mode.md),
[Codex entry](https://github.com/agentclientprotocol/registry/blob/134db9fa124273eed9133d0fd26a8d3039ea2f2a/codex-acp/agent.json),
[Codex ACP](https://github.com/agentclientprotocol/codex-acp),
[Claude entry](https://github.com/agentclientprotocol/registry/blob/134db9fa124273eed9133d0fd26a8d3039ea2f2a/claude-acp/agent.json), and
[Claude Agent ACP](https://github.com/agentclientprotocol/claude-agent-acp).

## Direction B: Pursers as an ACP agent

`pursers-acp` is a second executable that an IDE launches as its ACP agent. It
is a board assistant, not a coding agent: it lists and changes board records and
streams board events, but never reads, edits, builds, or runs the user's source
tree. This is the IDE-native replacement for the read-only preview in
[`apps_server.py`](../../packages/personal/src/pursers_personal/apps_server.py),
not another seat runtime or a path around Central authorization.

### Process, identity, and authentication

One IDE-spawned process may own multiple ACP sessions. Each process loads the
human's existing Personal profile and mode-0600 token file and uses the same
Personal/Central MCP tool surface, backed by
[`client.py`](../../packages/client/src/pursers_client/client.py). It never
calls `board_onboard`, creates an agent name, or accepts a seat token. Central
derives `principal_id` from the human token on every request; ACP `agentInfo`
identifies only the `pursers-acp` implementation.

If no Personal credential is usable, v1 `initialize` returns an `authMethods`
entry. Protocol-driven `authenticate` starts the existing door/pairing setup;
terminal authentication may reproduce `pursers-acp login` only when the IDE
advertises `clientCapabilities.auth.terminal`. v2 renames this to `auth/login`.
The flow writes the Personal token/profile through the existing credential
helper, then reconnects and initializes; tokens never appear in prompts, ACP
updates, command arguments, project settings, or registry metadata.

The v1 capability response advertises text and resource-link prompts, bounded
session loading only if implemented, and implementation information. It does
not claim client filesystem, terminal, MCP, image, or code-edit capabilities.
Unknown prompt intents return a help card rather than guessing a board write.

### IDE prompt mapping

| Human intent | ACP and board mapping | Guardrail |
| --- | --- | --- |
| Start or restore | `initialize` + `session/new`; optional `session/load` restores only IDE dialogue, then refetches board state. | The authenticated Personal principal and Central membership decide visible boards. |
| List my tickets/offers | `session/prompt` calls `ticket_list` with principal-scoped filters and emits bounded `session/update` cards. | Read-only; never claim an offer or fabricate a seat identity. |
| Create a ticket | Request permission, then call `ticket_create` once with the displayed board, title, scope, and body. | Reject if the approved payload differs byte-for-byte from the call. |
| Annotate a ticket | Request permission, then call `ticket_annotate`; return the durable annotation ID. | No hidden coordinator decision or memory write. |
| Watch a board | Open `a2a_wait` through the push subscription used by [`pursers_wait_server.py`](../../tools/wait-bridge/pursers_wait_server.py); translate each authorized event to `session/update`. | Persist and reuse its positive cursor; no polling, cursor reset, or cross-board widening. |
| Review | After permission, compare authenticated and submission `principal_id` before any review claim or verdict. | Politely refuse same-principal/self-review and any unauthorized role; ACP never weakens independent review. |
| Cancel | `session/cancel` closes the active subscription/request and resolves pending permissions as cancelled. | It does not unclaim seat work, because this Personal process owns no seat claim. |

Board state is authoritative after crash or reconnect. ACP replay supplies UI
context only. A watch task reconnects from its saved cursor and publishes a
bounded gap/update card; it never turns a timer into synthetic progress.

### Permission policy for the human-facing agent

Reads that Central authorizes need no interactive prompt. Every mutation uses
`session/request_permission` with the exact board, operation, and bounded
payload shown to the human. `allow_once` authorizes exactly that one request;
deny or cancellation makes no board call. P0/P1 do not offer or remember
`allow_always`. Authentication, membership, ACP permission, and board policy
are cumulative gates: ACP consent cannot grant a missing board scope.

`pursers-acp` needs network access only to the configured Central/door endpoint
and writes only its Personal credential and bounded session/cursor state. It
never asks an IDE for filesystem or terminal access. Read-only watches remain
push subscriptions; create, annotate, and permitted review actions are the only
initial write intents.

### IDE discovery and packaging

Publish one `pursers/agent.json` entry conforming to the official
[registry format](https://github.com/agentclientprotocol/registry/blob/134db9fa124273eed9133d0fd26a8d3039ea2f2a/FORMAT.md),
with pinned distributions, platform/architecture targets, launch command,
version, license, repository, icon, and authentication metadata. Registry CI
must validate the handshake and advertised auth methods.

| Host | Installation metadata |
| --- | --- |
| Zed | Use the ACP Registry. For development only, document a custom `agent_servers` command; [Zed agent-server extensions](https://zed.dev/docs/ai/external-agents) are deprecated. |
| JetBrains IDEs | Use the same global ACP Registry entry; document its [custom-agent JSON](https://www.jetbrains.com/help/webstorm/use-ai-agents-with-webstorm.html) fallback and organization policy requirement. |
| VS Code | The [official ACP client list](https://agentclientprotocol.com/get-started/clients) currently points to marketplace extensions rather than a built-in client. Document compatible client extensions and the custom launch command; do not claim native registry ingestion. |
| Other ACP hosts | Prefer the registry entry; otherwise publish only host marketplace discovery/configuration metadata that launches the same executable, never a host-specific board adapter. |

The registry is for ACP agents, so direction A's Gemini/Codex/Claude selection
consumes it while direction B's `pursers-acp` publishes to it. Zed and
JetBrains can consume that shared record; hosts without registry support need
configuration instructions, not a protocol fork.

## Delivery phases

### P0: fake-agent conformance harness

Build a deterministic stdio agent, fake IDE client, and fake Board API. Exercise
JSON-RPC framing,
invalid/out-of-order/oversize messages, capability negotiation, every permission
branch, cancel during permission and terminal work, crash before and after a
stop response, load/no-load recovery, lease timing, bounded update projection,
push-watch cursor recovery, Personal write consent, self-review refusal, and
absence of tokens from prompts, environment, logs, and state.

### P1: one real agent, one sandbox ticket

Pin the registry and Gemini versions. Run one bounded documentation ticket on a
sandbox board with an isolated worktree and default-deny egress. Preserve the
initialize capability snapshot, offer/claim/prompt/update/renew/submit trace,
exact commit and tests, permission decisions, and crash-free teardown. A
different principal reviews the result.

In parallel, launch `pursers-acp` from one sandbox IDE client: authenticate a
disposable human principal, list offers, create and annotate one ticket after
two explicit permissions, receive one pushed board event, refuse self-review,
cancel the watch, and prove the source worktree was untouched.

### P2: adapters without governance drift

Exercise pinned Codex and Claude implementations through the same generic ACP
client, then widen ticket classes only from evidence. Implementation-specific
extensions may have policy profiles but must not require per-host dispatch
adapters. Worker and reviewer remain distinct board identities. ACP modes,
provider authentication, extensions, and model choice cannot relax ticket
scope, permission policy, evidence requirements, or independent review.
Publish the validated `pursers-acp` registry entry, exercise Zed and JetBrains
registry installs plus one VS Code client extension, and keep host behavior
limited to Personal board operations rather than code-editing tools.

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
ACP, remote multi-user ACP transport, interactive permissions for direction A,
weakening review independence, or adopting an unstable ACP v2 surface.
