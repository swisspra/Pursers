# Zed AI landscape: Delta, agent platforms, and implications for Pursers

Status: research snapshot, 2026-09-20. Product claims below are tied to the
dated evidence ledger at the end of this document. Recommendations are our
analysis, not claims made by the cited vendors.

## Executive view

Zed and Pursers are converging on the same human problem from opposite ends.
Zed provides a fast, shared editor, agent threads, worktrees, model access, and
high-fidelity review. Pursers provides a durable work contract: assigned
tickets, renewable ownership, independently enforced review, exact evidence,
human-input routing, and resumable handoffs. Delta makes that complementarity
more visible. It is a separate multiplayer application for agent conversations
and code review, backed by DeltaDB; it is not merely a renamed Zed panel.
[Z7][Z8]

The strongest near-term move is not to imitate Delta's interface. Pursers
should make its governance available inside interfaces such as Zed through a
small ACP surface, keep the board as the source of truth, and invest in current
MCP multi-round-trip input, subscriptions, auditable skills, and team-level
evaluations. Cloud coding agents should be treated as execution backends rather
than alternate workflow authorities.

## What Zed ships today

| Surface | Current state on 2026-09-20 | Boundary that matters to Pursers | Evidence |
| --- | --- | --- | --- |
| Agent Panel | A first-party UI where an agent can inspect and edit the project, run commands, search the web, and call configured tools. Users can choose Zed's native Agent or an external agent. Threads can be archived and isolated in worktrees. | It is an interactive agent client and workspace, not a durable assignment/review protocol. | [Z1] |
| Parallel agents | Independent Agent Panel threads and Terminal Threads can run simultaneously, use different agents, and optionally use separate worktrees. Zed's April 2026 launch added the Threads Sidebar and worktree workflow. | Parallelism is user-directed. The documented surface does not define exclusive ticket claims, expiring leases, or a separate reviewer principal. | [Z3][Z4] |
| External agents and ACP | Zed owns the UI and thread history; the ACP agent owns runtime, authentication, model, tools, and configuration. The ACP Registry is the default discovery path and currently exposes agents including Claude Agent, Codex, OpenCode, GitHub Copilot, Cursor, and Pi. Custom `agent_servers` remain possible. Zed does not charge for external-agent usage. | This is the cleanest Pursers integration seam. ACP should expose a worker console without moving board invariants into the editor. | [Z2][Z5] |
| Hosted models and plans | Zed hosts models from several providers and also supports bring-your-own-key access. Personal is free; Pro is USD 10/month and includes USD 5 of hosted-model tokens; Business is USD 30/user/month. Hosted usage is priced at provider list price plus 10%, while external agents and BYOK remain available on the free tier. | Model procurement is Zed's concern. Pursers should remain model- and billing-neutral. | [Z6][Z9] |
| Collaboration and channels | Channels are persistent team rooms with shared projects, collaborative editing, voice, notes, and inherited permissions. Guests can be invited with read-only access. | This is real-time collaboration context, not a substitute for durable task state or review policy. A board link should attach to a channel/thread rather than duplicate it. | [Z10] |
| Long-running/background work | Zed's September preview added a setting that prevents idle sleep during long agent turns and improved ACP async-task wakeup. The documented cloud runner is part of Delta. We found no separate Zed-hosted, unattended coding-agent product in the reviewed official sources. | Keep “local long turn” and “cloud background agent” distinct in product language. | [Z11][Z12][Z7] |
| Release freshness | Zed stable 1.20.2 shipped on 2026-09-17; preview 1.21.0 shipped on 2026-09-16. | These dates bound the snapshot and prevent older ACP or agent behavior from being presented as current. | [Z11][Z12] |

## Delta: exact product, audience, and mechanics

Zed's exact public-beta description is:

> “Today, we're launching the public beta of Delta, a multiplayer environment for coding with agents and reviewing what they build.” [Z8]

Delta is a new application from Zed Industries. Its intended audience is a
software team that wants to delegate work to coding agents, keep human and
agent conversation attached to the evolving worktree, and review or continue a
teammate's session without losing context. The initial introduction described
it as an early preview; the public beta opened on 2026-09-16. Desktop downloads
are available for macOS, Linux, and Windows, the web client is usable now, and
mobile clients are described as forthcoming. The beta is free. Zed says paid
individual and team plans will follow and that a free version will remain, but
it has not published those plan prices in the reviewed sources. [Z7][Z8]

### How Delta works

1. **Thread plus worktree is one shared object.** DeltaDB records edits and
   human/agent messages between Git commits and synchronizes the conversation
   with the code state. Git remains compatible rather than being replaced.
   [Z7][Z8]
2. **Review is conversational and stateful.** A reviewer can comment on exact
   code, start an isolated review subthread, request a revision, and let the
   original agent or another participant continue from that context. Comments
   remain anchored as code changes. [Z7][Z8]
3. **Collaboration survives the originator.** Teammates can join the same
   conversation and worktree and continue it after the person who started the
   work logs off. [Z8]
4. **Execution can be remote while the client stays rich.** Zed describes a
   cloud runner, a Rust/WebAssembly/WebGL browser client, and third-party agent
   harness support beginning with Claude Code. [Z7]

No reviewed Zed source documents an exclusive ticket-claim lease, a mandatory
independent reviewer identity, verifier-owned acceptance fixtures, a public
self-hosted Delta server, or ACP/MCP as Delta's integration contract. That is a
statement about the reviewed documentation, not proof that such features can
never exist.

## Delta versus Pursers

Pursers behavior in this table comes from the repository's public feature and
workflow descriptions, especially ticket lifecycle, role separation, evidence,
questions, memory, and push-based waiting. [P1]

| Capability | Delta | Pursers | Product implication |
| --- | --- | --- | --- |
| Unit of work | A shared conversation plus worktree, compatible with Git commits. [Z7][Z8] | A durable ticket with status, scope, assignee, history, and exact submission metadata. [P1] | Link one board ticket to one or more Delta/Zed threads; do not equate the records. |
| Multi-agent coordination | People and agents can share, branch, review, and continue threads; third-party harness support begins with Claude Code. [Z7][Z8] | The board dispatches named roles and identities across model vendors and runtimes, with push-based wakeups. [P1] | Delta supplies shared working context; Pursers supplies dispatch and policy. |
| Ownership and leases | No exclusive-claim or renewable-lease contract is documented in the reviewed Delta sources. | Claims grant bounded ownership and renewable leases; stale work can be recovered. [P1] | Keep claim authority on the board even if work begins in Delta. |
| Independent review gate | Review subthreads preserve an isolated code copy and can send revision requests. [Z8] | Approval is enforced through a distinct reviewer role and principal; the worker cannot review its own submission. [P1] | Present Pursers approval in the UI as a separate state from ordinary code comments. |
| Evidence | DeltaDB preserves conversation, edits, and worktree state, providing unusually rich review context. [Z7][Z8] | Submission binds an exact branch/SHA, exact tip file list, commands, outputs, and observations; review can reject gaps. [P1] | Store a Delta thread reference as supplementary evidence, never as a replacement for the commit receipt. |
| Human-in-the-loop | Inline comments and shared threads let a person redirect work in context. [Z7][Z8] | Durable questions and human-input requests can block, wake, and resume the correct ticket. [P1] | Translate a board question into a visible editor prompt and write its answer back once. |
| Memory and handoff | DeltaDB replicates the conversation with its worktree; teammates can continue after the originator leaves. [Z7][Z8] | Board memory, checkpoints, handoffs, journal events, and cursors survive process replacement. [P1] | Delta is high-resolution task memory; Pursers is cross-task operational memory. |
| Cross-vendor agents | The announced third-party harness starts with Claude Code; broader Delta support is not specified in the reviewed pages. [Z7] | The protocol and board are model-neutral; clients can be MCP-based and the repository includes an ACP agent preview. [P1] | Pursers should preserve a portable contract instead of depending on one harness. |
| Local-first and self-hosting | Delta maintains local worktree copies and has native and web clients, while synchronization and its cloud runner are Zed services. No self-hosted Delta service is documented. [Z7][Z8] | The board and repository run under the operator's control and support local/headless workers. [P1] | Make deployment ownership explicit in any joint story. |
| MCP/ACP openness | No public Delta MCP/ACP contract was found in the reviewed official pages. Zed itself supports MCP tools and ACP agents. [Z1][Z2][Z7][Z8] | MCP is the primary tool/client boundary; an ACP adapter is a natural editor integration. [P1] | Integrate with Zed's documented ACP surface first; do not infer a Delta API. |

## Trend decisions

“Adopt now” means a bounded Pursers change is justified now. “Integrate later”
means preserve an interface and prototype after the dependency stabilizes.
“Watch” means gather evidence without putting it on the critical path.

| Trend | Verdict | One-line reason and concrete Pursers move |
| --- | --- | --- |
| ACP ecosystem and registry | **Adopt now** | ACP is already a multi-client distribution channel used by Zed and JetBrains; harden `pursers-acp`, document its board-authority boundary, and prepare a minimal registry-quality package. [Z2][Z5] |
| MCP revision 2026-07-28 | **Adopt now** | The revision removes sessions and server-initiated calls, adds discovery and multi-round-trip input, and replaces resource-specific subscriptions with `subscriptions/listen`; test these paths as first-class interoperability contracts. [M1][M2] |
| Python SDK v2 `Resolve`/`Elicit` | **Adopt now** | This SDK abstraction lets one tool obtain validated human input over legacy elicitation or 2026-07-28 multi-round-trip retries; map it to durable Pursers questions without exposing the answer as model-supplied input. [M3] |
| MCP Apps UI | **Integrate later** | The official extension makes sandboxed interactive `ui://` interfaces portable across several hosts, but host capability and CSP differences warrant a thin ticket/review pilot before replacing the dashboard. [M4][M5] |
| A2A | **Watch** | A2A standardizes discovery and opaque agent-to-agent task exchange across HTTP, JSON-RPC, and gRPC, but does not supply Pursers' lease, evidence, or independent-review semantics; consider it later for federation. [A1][A2] |
| OpenAI Codex cloud | **Integrate later** | Isolated cloud tasks, parallel execution, repository integrations, and reviewable diffs make a useful worker backend, while the board should continue to own assignment and acceptance. [B1] |
| Claude Code agent teams | **Watch** | Shared task lists and direct teammate messaging overlap with coordination, but the feature is experimental, disabled by default, and token-intensive; learn from it rather than building a hard dependency. [B2] |
| Cursor cloud/background agents | **Integrate later** | Dedicated remote VMs, long-running execution, evidence artifacts, and PR delivery fit a worker adapter once secrets, network, cost, and source-of-truth boundaries are explicit. [B3][B4] |
| GitHub Copilot coding agent | **Integrate later** | Its GitHub Actions environment, branch/PR loop, and bounded sessions are a good repository worker target, but the one-repository/one-PR task model should stay behind the board contract. [B5] |
| skills.sh ecosystem | **Adopt now** | Reusable agent skills are an efficient distribution mechanism, but the catalog itself warns that audits cannot guarantee safety; support pinned, reviewed skills rather than ambient installation. [S1] |
| Team-agent evaluations | **Adopt now** | Add scenarios that score claim exclusivity, handoff recovery, independent review, question resumption, and evidence integrity; use SWE-bench artifacts as a coding baseline, not as a proxy for team governance. [E1][E2] |
| Prompt caching and cost controls | **Adopt now** | Stable prompt prefixes, compact recurring board receipts, and cache-hit telemetry reduce repeated context cost; preserve dynamic ticket state after the stable prefix and track quality alongside spend. [C1][P2] |

### Evaluation shape to add

A team benchmark should retain the product-produced journal, questions, leases,
submissions, reviews, and exact repository state. Minimum scenarios are two
workers racing for one task, lease expiry and takeover, reviewer self-approval
attempt, rejected evidence followed by a corrected resubmission, a human
question across process restart, and a cross-vendor handoff. Report success,
wall time, model/tool cost, duplicate work, human interventions, and evidence
completeness. Zed's public metrics are a useful adoption and latency signal,
but Zed explicitly distinguishes them from outcome quality; SWE-bench likewise
provides reproducible issue-resolution artifacts rather than team-governance
coverage. [E1][E2]

## Neutral partnership brief

### What each side fills

- **Zed fills Pursers' interaction gap:** a polished, shared editor; native
  worktrees and terminals; model access; inline collaboration; and an installed
  ACP client/registry surface. [Z1][Z2][Z3][Z5][Z10]
- **Pursers fills Zed/Delta's governance gap:** durable work assignment,
  renewable ownership, role-separated approval, structured evidence,
  resumable human questions, and operational memory across clients and agent
  vendors. [P1]
- **Delta raises the quality of review context:** its synchronized conversation
  and worktree can explain why code changed; Pursers can add the enforceable
  receipt and decision trail around that context. [Z7][Z8][P1]

### Three integration shapes

1. **Pursers ACP agent in Zed.** Publish a small ACP adapter that lists offered
   tickets, claims only explicit offers, streams work status, surfaces durable
   questions, and shows submission/review results. The board remains
   authoritative; Zed owns presentation and local thread history.
2. **Governed review inside the editor.** Expose read-only ticket evidence and
   reviewer actions through ACP/MCP. Open the exact commit/worktree in Zed for
   inspection, but let the board enforce separate-principal approval and record
   the verdict.
3. **Delta context attachment.** If Zed publishes a supported Delta integration
   API, store a stable Delta thread/worktree reference on the ticket and bring
   board status into the shared review. Keep Git SHA and board journal as the
   portable fallback; do not depend on an undocumented interface.

### Risks and safeguards

| Risk | Safeguard |
| --- | --- |
| Two sources of truth for task status | Board owns assignment, lease, submission, and approval; editor/Delta state is linked context only. |
| Confusing collaborative review with independent approval | Label them separately and require the board's distinct reviewer principal for acceptance. |
| Platform/API churn, especially during Delta beta | Start with Zed's documented ACP interface; make Delta attachment conditional on a supported API and version it. |
| Agent or skill supply-chain risk | Pin versions, display provenance, require explicit permissions, and vet skills before registry/catalog distribution. |
| Cloud secrets and repository exposure | Use least-privilege, task-scoped credentials; keep a local/self-hosted path and record which execution backend handled each task. |
| Cost moves from models to duplicated orchestration | Measure end-to-end task cost, cache hits, retries, and duplicate execution—not only token price. |
| Product-positioning overlap | Describe Pursers as the governance/control plane and Zed/Delta as interaction and review environments; avoid claiming either side replaces the other. |

## Evidence ledger

Every external product claim above points to one or more entries below. All
pages were retrieved on **2026-09-20**. Publication/release dates are included
when the page states them.

### Zed and Delta

- **[Z1]** Zed, [Agent Panel](https://zed.dev/docs/ai/agent-panel).
- **[Z2]** Zed, [External agents](https://zed.dev/docs/ai/external-agents).
- **[Z3]** Zed, [Parallel agents](https://zed.dev/docs/ai/parallel-agents).
- **[Z4]** Zed, [Parallel agents](https://zed.dev/blog/parallel-agents), published 2026-04-22.
- **[Z5]** Zed, [Introducing the ACP Registry](https://zed.dev/blog/acp-registry), published 2026-01-28.
- **[Z6]** Zed, [Zed-hosted models](https://zed.dev/docs/account/zed-hosted-models).
- **[Z7]** Zed, [Introducing Delta](https://zed.dev/blog/introducing-delta), published 2026-08-12.
- **[Z8]** Zed, [Delta is now in public beta](https://zed.dev/blog/delta-public-beta), published 2026-09-16. The exact sentence quoted above is from this page.
- **[Z9]** Zed, [Pricing](https://zed.dev/pricing).
- **[Z10]** Zed, [Channels](https://zed.dev/docs/collaboration/channels).
- **[Z11]** Zed, [Stable release 1.20.2](https://zed.dev/releases/stable/1.20.2), released 2026-09-17.
- **[Z12]** Zed, [Preview release 1.21.0](https://zed.dev/releases/preview/1.21.0), released 2026-09-16.

### Protocols, agents, skills, evaluation, and cost

- **[M1]** Model Context Protocol, [2026-07-28 specification changes](https://blog.modelcontextprotocol.io/posts/2026-07-28/).
- **[M2]** MCP TypeScript SDK, [Supporting MCP 2026-07-28](https://ts.sdk.modelcontextprotocol.io/v2/migration/support-2026-07-28).
- **[M3]** MCP Python SDK, [`Resolve`: the new way to ask the user for input](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md).
- **[M4]** Model Context Protocol, [MCP Apps joins the official extensions registry](https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/), published 2026-01-26.
- **[M5]** MCP Apps, [Apps SDK API reference](https://apps.extensions.modelcontextprotocol.io/api/).
- **[A1]** A2A Project, [A2A protocol specification](https://github.com/a2aproject/A2A/blob/main/docs/specification.md).
- **[A2]** A2A Project, [A2A and MCP](https://github.com/a2aproject/A2A/blob/main/docs/index.md).
- **[B1]** OpenAI, [Codex cloud](https://learn.chatgpt.com/docs/cloud).
- **[B2]** Anthropic, [Orchestrate teams of Claude Code sessions](https://code.claude.com/docs/en/agent-teams).
- **[B3]** Cursor, [Cloud agents](https://prod.cursor.com/help/ai-features/background-agents).
- **[B4]** Cursor, [Pricing and usage](https://docs.cursor.com/account/pricing).
- **[B5]** GitHub, [About Copilot coding agent](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent).
- **[S1]** skills.sh, [Documentation](https://skills.sh/docs).
- **[E1]** SWE-bench, [Benchmark and evaluation artifacts](https://github.com/SWE-bench/SWE-bench).
- **[E2]** Zed, [Measuring agent adoption](https://zed.dev/blog/agent-metrics), published 2026-04-09.
- **[C1]** OpenAI, [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).

### Pursers basis

- **[P1]** Pursers repository, [README](../../../README.md), checked at this document's tested commit on 2026-09-20.
- **[P2]** Pursers repository, [cache-efficiency notes](../../performance/cache-efficiency.md), checked at this document's tested commit on 2026-09-20.

## Research limits

This is a public-document review, not hands-on acceptance testing of paid Zed,
Delta, or cloud-agent accounts. “Not documented” is therefore deliberately
narrow. Prices, beta availability, registries, and protocol support can change;
re-check the linked primary sources before making a commercial commitment.
