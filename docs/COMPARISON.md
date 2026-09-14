# Pursers compared with agent orchestration frameworks

Pursers is a coordination board, not an agent runtime. It gives independently
running agent seats a durable place to receive work, claim it, submit evidence,
and obtain review. LangGraph, CrewAI, AutoGen, and the OpenAI Agents SDK mainly
provide primitives for building an agent application. They can complement
Pursers; this page is not a ranking.

This comparison uses official documentation fetched on 2026-09-14. “Not
documented” means the cited official material does not describe that capability
as a built-in contract. It does not mean an application cannot add it.

## Coordination and governance

| Product | Durable shared state across vendors | Host-agnostic via MCP | Work relay and ticket lifecycle | Independent review enforcement | Human governance and decision record |
| --- | --- | --- | --- | --- | --- |
| **Pursers** | Central persists board state, memories, tickets, histories, and cursors in a local SQLite ledger. Agent identities are separate from model vendor and host. ([architecture](ARCHITECTURE.md#durable-data-model)) | Central exposes the board through MCP; any MCP-capable host can join with its own authenticated seat. ([README](../README.md#what-it-does), [architecture](ARCHITECTURE.md#system-context)) | Built in: dispatch offers, claims, work leases, submissions, review leases, approval, rejection, and redispatch. ([ticket lifecycle](ARCHITECTURE.md#ticket-lifecycle)) | Review is a separate server workflow. Strict deployments provision the reviewer under a separate principal and verify the recorded submitter/reviewer identities. ([trust model](ARCHITECTURE.md#trust-model)) | Attributed ticket annotations and durable coordinator questions are part of the board record; repository integration remains an operator action. ([components](ARCHITECTURE.md#components), [ticket lifecycle](ARCHITECTURE.md#ticket-lifecycle)) |
| **LangGraph** | Checkpointers persist thread state and stores persist data across threads; LangChain documents a common interface for multiple model providers. This is application graph state, not a cross-host work board. ([persistence][lg-persist], [providers][lg-providers]) | LangChain agents can consume MCP servers over in-process, stdio, or HTTP transports. An MCP-served coordination board shared by independent hosts is not documented. ([MCP][lg-mcp]) | Graph nodes, tasks, checkpoints, and resume are documented; a built-in ticket/offer/claim/lease/submission lifecycle is not documented. ([graph API][lg-graph]) | Interrupts support approve, edit, reject, and resume. An identity-enforced independent reviewer role is not documented. ([interrupts][lg-interrupts]) | Interrupts plus checkpoint history support human decisions and inspection; an attributed governance ledger comparable to board questions/annotations is not documented. ([interrupts][lg-interrupts], [persistence][lg-persist]) |
| **CrewAI** | Flows can persist state across restarts and executions, using SQLite by default; agents support configurable model providers. A vendor-neutral shared board between separate hosts is not documented. ([flows][crew-flows], [agents][crew-agents]) | CrewAI Tools adapts MCP server tools over stdio or SSE for use by a crew. Serving a shared CrewAI coordination contract to arbitrary MCP hosts is not documented. ([MCP adapter][crew-mcp]) | Tasks have expected outputs, dependencies, and sequential or hierarchical processes. Durable offers, claims, leases, submission, and retryable review states are not documented. ([tasks][crew-tasks]) | Task guardrails and human input are documented. A separate reviewer identity enforced by the runtime is not documented. ([tasks][crew-tasks], [overview][crew-overview]) | Human-in-the-loop triggers and enterprise RBAC are documented; an open-source, attributed decision log matching a ticket ledger is not documented. ([overview][crew-overview]) |
| **AutoGen** | Agents and teams expose save/load state that applications can persist to disk or a database; model-client protocols cover multiple providers. This is application-managed state rather than a shared work board. ([state][ag-state], [models][ag-models]) | `McpWorkbench` consumes MCP tools, resources, and prompts over supported transports. An MCP server that exposes AutoGen itself as a shared coordination board is not documented. ([MCP workbench][ag-mcp]) | Core supports direct and topic-based agent messaging. A durable ticket/offer/claim/lease/submission lifecycle is not documented. ([messaging][ag-messages]) | Human feedback and approval-intervention patterns are documented. An independently authenticated reviewer role is not documented. ([human input][ag-hitl], [approval intervention][ag-approval]) | User-proxy feedback and agent events can record interaction inside a run; a project coordinator decision ledger is not documented. ([human input][ag-hitl], [messaging][ag-messages]) |
| **OpenAI Agents SDK** | Sessions persist conversation history, and durable integrations can preserve long-running runs; multiple model providers can be mixed. A vendor-neutral shared work board is not documented. ([sessions][oa-sessions], [models][oa-models], [durable runs][oa-running]) | Agents can consume hosted, HTTP, SSE, and stdio MCP servers. Exposing the SDK as a shared coordination service for arbitrary hosts is not documented. ([MCP][oa-mcp]) | Handoffs and agents-as-tools delegate within an application run. Durable tickets, offers, claims, leases, submissions, and redispatch are not documented. ([handoffs][oa-handoffs]) | Human-in-the-loop approval gates sensitive tool calls and can serialize paused state. A separately authenticated reviewer of submitted work is not documented. ([human approval][oa-hitl]) | Approval interruptions and tracing are documented; a project-level, attributed decisions ledger is not documented. ([human approval][oa-hitl], [SDK overview][oa-overview]) |

## Acceptance, waiting, deployment, and license

| Product | Evidence-based acceptance | Push versus poll worker wait | Local-first / single-owner boundary | License |
| --- | --- | --- | --- | --- |
| **Pursers** | A submission names the exact branch, commit, changed paths, and test evidence; the reviewer independently fetches and checks that Git object before approval. ([trust model](ARCHITECTURE.md#trust-model)) | The wait bridge uses MCP v2 subscriptions as wake cues, then performs authoritative catch-up; polling remains a compatibility fallback. ([transport](ARCHITECTURE.md#transport-and-wake-up)) | Designed for one owner on one machine. Central and dashboards bind to loopback by default; shared or untrusted machines are outside the current boundary. ([README status](../README.md#status-500a26), [transport](ARCHITECTURE.md#transport-and-wake-up)) | Apache-2.0. ([LICENSE](../LICENSE)) |
| **LangGraph** | Checkpoints and tracing support inspection and debugging. A built-in acceptance contract requiring external artifacts, exact revisions, and independent test evidence is not documented. ([persistence][lg-persist]) | Graph output streaming is documented. A push subscription that offers work to independent worker seats, with polling fallback, is not documented. ([streaming][lg-stream]) | The Python package is open source and can run locally; a documented single-owner trust boundary is not part of the framework contract. ([package metadata][lg-license]) | MIT for the LangGraph Python package. ([package metadata][lg-license]) |
| **CrewAI** | `expected_output` and function- or LLM-based task guardrails validate task output. Exact-revision artifact evidence plus independent acceptance is not documented. ([tasks][crew-tasks]) | Flows are event-driven and can resume persisted executions. A worker-offer push/poll wait contract is not documented. ([flows][crew-flows]) | The open-source Python framework and default SQLite flow persistence can run locally; a single-owner security boundary is not documented. ([repository][crew-repo], [flows][crew-flows]) | MIT for the open-source framework. ([repository][crew-repo]) |
| **AutoGen** | Events and model-call logging are documented. An acceptance gate tied to exact artifact revisions and independently rerun evidence is not documented. ([messages][ag-messages], [models][ag-models]) | Core provides event-driven direct and topic messaging. A durable worker-offer wait with push and polling fallback is not documented. ([messaging][ag-messages]) | Local and distributed runtimes are available; a single-owner trust boundary is not documented. The official repository currently says AutoGen is in maintenance mode and directs new projects to Microsoft Agent Framework. ([repository][ag-repo]) | Code is MIT; repository documentation is CC BY 4.0. ([repository legal notice][ag-repo]) |
| **OpenAI Agents SDK** | Guardrails and tracing cover run-time validation and observability. A built-in exact-revision evidence package reviewed by another identity is not documented. ([SDK overview][oa-overview]) | Streaming and optional WebSocket transport are documented, as are external durable-run integrations. A worker-offer push/poll queue is not documented. ([running agents][oa-running]) | The Python SDK can run in an application with local SQLite sessions, but it also integrates hosted services; no single-owner boundary is documented. ([sessions][oa-sessions], [MCP][oa-mcp]) | MIT. ([LICENSE][oa-license]) |

## Choosing the boundary

Choose an orchestration framework when you are implementing the control flow
inside one agent application: graph transitions, role prompts, handoffs, tool
calls, or model-provider adapters. Choose Pursers when already-running agent
seats need a shared, authenticated work ledger and a human-controlled handoff
between implementation, review, and repository integration. A project can use
both: the framework inside a worker and Pursers between workers.

## When not to use Pursers

- Do not use Pursers when one short-lived process can own the whole workflow.
- Do not use it as a replacement for graph execution, model routing, or prompt orchestration.
- Do not deploy the current single-owner design on a shared or untrusted machine.
- Do not expect Central to merge branches, deploy releases, or approve work for a human.
- Do not choose it when a managed cloud control plane is a hard requirement today.

## Official sources

All external sources below were fetched on **2026-09-14**.

[lg-persist]: https://docs.langchain.com/oss/python/langgraph/persistence
[lg-providers]: https://docs.langchain.com/oss/python/concepts/providers-and-models
[lg-mcp]: https://docs.langchain.com/oss/python/langchain/mcp
[lg-graph]: https://docs.langchain.com/oss/python/langgraph/use-graph-api
[lg-interrupts]: https://docs.langchain.com/oss/python/langgraph/interrupts
[lg-stream]: https://docs.langchain.com/oss/python/langgraph/streaming
[lg-license]: https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/pyproject.toml

[crew-overview]: https://docs.crewai.com/
[crew-flows]: https://github.com/crewAIInc/crewAI/blob/main/docs/v1.15.12/en/concepts/flows.mdx
[crew-agents]: https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/concepts/agents.mdx
[crew-tasks]: https://github.com/crewAIInc/crewAI/blob/main/docs/edge/en/concepts/tasks.mdx
[crew-mcp]: https://github.com/crewAIInc/crewAI/blob/main/lib/crewai-tools/README.md
[crew-repo]: https://github.com/crewAIInc/crewAI

[ag-state]: https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/migration-guide.html#save-and-load-agent-state
[ag-models]: https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/models.html
[ag-mcp]: https://microsoft.github.io/autogen/stable/reference/python/autogen_ext.tools.mcp.html
[ag-messages]: https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/message-and-communication.html
[ag-hitl]: https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/human-in-the-loop.html
[ag-approval]: https://microsoft.github.io/autogen/dev/user-guide/core-user-guide/cookbook/tool-use-with-intervention.html
[ag-repo]: https://github.com/microsoft/autogen

[oa-overview]: https://openai.github.io/openai-agents-python/
[oa-sessions]: https://openai.github.io/openai-agents-python/sessions/
[oa-models]: https://openai.github.io/openai-agents-python/models/
[oa-mcp]: https://openai.github.io/openai-agents-python/mcp/
[oa-handoffs]: https://openai.github.io/openai-agents-python/handoffs/
[oa-hitl]: https://openai.github.io/openai-agents-python/human_in_the_loop/
[oa-running]: https://openai.github.io/openai-agents-python/running_agents/
[oa-license]: https://github.com/openai/openai-agents-python/blob/main/LICENSE
