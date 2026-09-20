# Zed integration surface audit and roadmap cut

This audit answers one narrow question: what can Pursers actually place in
Zed, using the stable product that exists now? The answer is to keep MCP as a
tool-and-prompt bridge and use one ACP thread as the richer supervisory
surface. Neither route can create a Pursers pane, window, tab, icon, or badge.

## observations

### Pinned release

The current stable release checked on **2026-09-20** is **Zed 1.20.2**, released
2026-09-17. The installed macOS application reports `1.20.2` (bundle build
`20260917.044955`), and the official `v1.20.2` tag resolves to commit
`7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f`. Zed 1.21.0 is a preview release,
so it is outside this stable audit. The release page, installed application,
tag, and source checkout all agree. [S1] [S2]

Notes 01–07 were already pinned to that same stable commit. There is therefore
no Zed release delta to merge into their conclusions. There is one Pursers
delta: after note 03 was written, commit
`41acaa4bf0c65e54ecee7c3763c3f79db3e3d37b` added
`available_commands_update` and a one-entry ACP plan for `/watch`. The current
agent sends that entry as `in_progress`, streams board events while the prompt
remains active, and sends `completed` as the turn exits. [P1] The statement in
note 03 that the agent does not send plans or available-command updates is now
stale; note 04's host-surface conclusions remain correct.

### No-active-turn delivery

An MCP context server has no usable Pursers notification channel in Zed
1.20.2. Zed documents Tools and Prompts only. Its Agent registry subscribes
only to `notifications/tools/list_changed`, whose effect is to reload the tool
catalog; it does not subscribe to `notifications/message`, resource-change, or
prompt-change notifications even though the transport has types for them.
Nothing from that path is rendered as a new thread message, badge, or desktop
notification. [S3] [S4] [S5]

ACP is different. Zed keeps the ACP connection's notification handler running
for the lifetime of the connection, independently of a `session/prompt`
request. A `session/update` addressed to a registered, loaded session is
forwarded to that thread even when no prompt is active. If the thread entity
has been released, Zed removes its session registration; a later update for
that unknown session is logged and dropped. [S6] [S7]

The update can therefore change the loaded thread body, plan, or title while
the user is not prompting. It does **not** itself produce a badge or operating
system notification. Zed's completion notification is driven by the thread's
`Stopped` event after a prompt response, while permission and elicitation
requests have their own waiting notifications. A free-standing message, plan,
or title `session/update` emits none of those events. [S8] [S9]

The current `pursers-acp` does not yet exploit idle-session delivery. Its
`/watch` implementation holds `session/prompt` open for the whole watch and
emits updates inside that active turn. [P1]

### ACP plan behavior

ACP v1 Plan is a complete replacement document: every update carries the full
ordered list and its current statuses. Zed applies entries by index, reuses the
existing Markdown objects to avoid flicker, appends or truncates to the new
length, and then rerenders. There is no entry identifier and no per-row patch
operation in the stable Plan shape. Independent status changes are possible by
resending the full list with only the desired rows changed. [A1] [S10]

Zed imposes no plan-entry count in this path: the renderer iterates every
entry. A collapsed plan shows the first `in_progress` row and a remaining
count, or a total/completed count; expanding it shows all rows. The three
defined statuses are `pending`, `in_progress`, and `completed`, rendered with
pending, animated-progress, and complete icons. Entry `content` is parsed and
rendered as Markdown. Priority is stored but has no distinct visual treatment
in this renderer. [A1] [S11] [S12]

The expanded list is capped at `max_h_40` and has its own vertical scrolling;
it sits in the activity bar below the separately scrolling conversation. Plan
updates call `notify()` but do not call a scroll-to-top or scroll-to-end API.
The source therefore establishes a bounded, independently scrollable list and
no intentional scroll jump. Exact pixel-position retention across a GPUI
rerender is not asserted by a test in the inspected source and remains
unverified. [S10] [S12] [S13]

### Titles, tabs, windows, and additional threads

An ACP agent can set the **thread title** with
`session/update: session_info_update`. Zed stores the title, emits
`TitleUpdated`, refreshes the visible title editor and thread metadata, and
uses it as the conversation title. This is the only reviewed agent-controlled
surface outside the message/plan/tool body. It is not an arbitrary workspace
tab label or window title API. [A2] [S14] [S15]

Neither MCP nor standard ACP exposes a request to create a Zed pane/window,
set an icon or attention badge, or open a second top-level thread. ACP
`session/new`, `session/load`, `session/list`, and `session/fork` are
client-to-agent requests: they let Zed ask the agent to manage sessions, not
the reverse. Zed has a private `subagent_session_info` tool-call metadata path
that can load a nested subagent view when the agent also supports session
loading, but that is neither a second top-level thread nor a portable ACP
surface and should not be a Pursers dependency. [A3] [S16] [S17]

### MCP Resources and MCP Apps recheck

There is no change to the negative result in note 04. Zed 1.20.2's native MCP
registry loads Tools and Prompts, not Resources. Its MCP tool adapter converts
text and images, ignores audio, and explicitly discards returned Resource and
ResourceLink blocks. No `ui://` or `io.modelcontextprotocol/ui` renderer exists
in the inspected Zed tree. Pursers does expose its Personal app at
`ui://pursers/dashboard`, but the Zed relay registers only tools and prompts,
so that app cannot appear inside Zed. [S3] [S4] [S18] [P2] [P3]

## capability_table

| Capability | MCP extension | ACP agent | Verdict and owner | Exact source ref |
| --- | --- | --- | --- | --- |
| Deliver a user-visible update with no active turn | **No.** Server notifications are not surfaced; tool-list change only reloads tools. | **Yes, conditional.** `session/update` reaches a loaded, registered thread while the connection lives. | Use ACP for loaded-thread state; the ACP agent owns delivery and Zed owns rendering. Do not promise delivery after the thread is released. | [S4], [S6], [S7] |
| Desktop/sound notification for an idle-session update | **No.** | **No.** Message/plan/title updates do not emit `Stopped`. Prompt completion, permission, and elicitation can notify. | Zed owns notifications; Pursers cannot request one arbitrarily. | [S8], [S9] |
| Show 12 concurrent seat items | **No structured surface.** A tool may return Markdown text only. | **Yes.** One plan may contain all 12 rows; the renderer has no row cap. | ACP agent owns the full ordered plan; Zed renders all entries. | [A1], [S10], [S12] |
| Change seat statuses independently | **Text must be regenerated.** | **Yes, by full-list replacement.** Resend all entries with the changed row status. | ACP agent owns stable ordering and the authoritative full list. There are no stable per-entry IDs in ACP v1 Plan. | [A1], [S10] |
| Plan statuses | None beyond authored text. | `pending`, `in_progress`, `completed`. | ACP defines status; Zed maps it to three visual states. | [A1], [S12] |
| Plan entry markup | Tool text can render as normal thread Markdown. | Entry content renders as Markdown; priority is not visibly distinguished. | Zed owns rendering. Keep each row short enough for the compact activity bar. | [S11], [S12] |
| Plan scrolling while updating | Normal thread scroll only. | Expanded plan has a height cap and its own vertical scroll; updates have no explicit auto-scroll call. Exact rerender offset retention is unverified. | Zed owns scroll behavior; ACP must not depend on a preserved pixel offset. | [S10], [S12], [S13] |
| Change thread title | **No.** | **Yes.** `SessionInfoUpdate.title` updates the loaded thread title. | ACP agent owns the title string; Zed owns metadata and display. | [A2], [S14], [S15] |
| Change workspace tab label or window title | **No.** | **No standard API.** The thread title does not grant workspace chrome control. | Zed owns workspace chrome. | [S14], [S19], [S20] |
| Add or change an icon/badge | **No.** | **No.** No such client capability or session-update variant is exposed. | Zed owns icons and badges. | [A2], [S16], [S19] |
| Create a second top-level thread | **No.** | **No.** Session creation is initiated by the client. A Zed-specific nested subagent path is not equivalent. | The human/Zed client creates top-level threads. | [A3], [S17] |
| Discover/read MCP Resources in native Zed | **No user surface.** Types exist, but the native registry does not list/read them. | ACP can render its own embedded resources, but that is ACP content, not native MCP Resource discovery. | MCP host support is missing; do not build a Zed workflow around Resources. | [S3], [S4], [S18] |
| Render the Pursers MCP App | **No.** Zed has no MCP Apps renderer and discards the required resource path. | **No.** ACP content blocks are not MCP Apps. | Keep `ui://pursers/dashboard` for hosts that explicitly support MCP Apps. | [S18], [P2], [P3] |

### Coordinator-required ACP and Agent Panel checks

The distinction in this table is important: a green Zed surface is not a
current Pursers feature. `pursers-acp` presently dispatches only `initialize`,
`authenticate`, `session/new`, and `session/prompt`, and advertises only an
empty `promptCapabilities` object. It does emit basic board tool calls, but
those use `kind: other` plus `rawInput`; they do not include locations or raw
output. Every richer row below therefore needs agent work unless it explicitly
says “Zed-only.” [P4] [P5]

| Required capability | Zed 1.20.2 / current Pursers verdict | Worth building for one human supervising 12 seats? | Exact source ref |
| --- | --- | --- | --- |
| ACP `elicitation/create` and `elicitation/complete`; client form/URL | **Zed yes; Pursers no.** Zed advertises form and URL modes, handles session- and request-scoped create calls, and applies URL completion. `elicitation/complete` completes URL requests; form requests return their structured response directly. | **Yes, rank 2.** Map `coordinator_question` and `required_fields` to a bounded form; keep URL mode for future external approval/auth only. | [S21], [S22], [A4] |
| `fs/read_text_file` / `fs/write_text_file` | **Zed yes; Pursers no.** Zed advertises both and registers both agent-to-client handlers. This grants access to the open project, not a board-level abstraction. | **No for this cut.** It adds no advantage over worker-owned checkouts and risks blurring the board/worktree authority boundary. | [S21] |
| Terminal lifecycle: create, output, wait, kill, release | **Zed yes; Pursers no.** Zed advertises `terminal: true` and registers all five handlers; an ACP tool call can embed the created terminal. | **Later, narrowly.** A manually requested seat suite/gate is useful evidence, but do not make the supervisor agent an alternate job runner. | [S21], [A8] |
| `ConfigOptionUpdate`; boolean client capability and `valueId` | **Zed yes; Pursers no.** Zed advertises the new boolean extension, while select/value-ID remains the baseline selector form; incoming updates replace the full option set and refresh the selector UI. | **Yes, rank 3.** Expose board and tier as select/value-ID options and `needs-me only` as boolean; this is faster and safer than parsing slash-command flags. | [S21], [S23], [A5] |
| `CurrentModeUpdate` | **Zed yes; Pursers no.** Zed applies the agent-sent current mode ID to its mode selector. | **No separate build.** `watching`, `idle`, and `reviewing` are board state, not mutually exclusive operating modes; show them in the plan/title instead. | [S23], [S24] |
| Session capabilities: close, delete, list, resume, `loadSession` | **Zed yes when advertised; Pursers no.** Zed creates a session list only for `list`, gates delete/resume/load, and sends close when the final thread handle is released. | **Later.** Persist and restore one thread per active ticket, including board cursor and evidence history; do not manufacture one session per seat. | [S25], [S26], [A6] |
| Prompt capabilities: image, audio, `embeddedContext` | **Partial Zed; Pursers advertises none.** The ACP schema has all three. Zed's editor gates images and embedded context, but has no audio composer path in the inspected implementation. | **Image yes, later.** Accept a screenshot as ticket evidence; add embedded context only when the agent consumes it. Audio is unverified end-to-end and should not be promised. | [S27], [A7], [P4] |
| `mcpCapabilities.http` / `.sse` | **Protocol yes; Pursers no.** ACP gates HTTP and SSE server descriptors on agent capability bits; stdio is mandatory. Zed forwards configured stdio and HTTP servers, but its forwarding code has no SSE branch. | **No for this cut.** Pursers already supplies its own board client; MCP forwarding adds configuration paths, not supervisor visibility. SSE forwarding in this Zed release is unverified/absent. | [S28], [A9], [P4] |
| `AgentAuthCapabilities.logout` | **Zed yes when advertised; Pursers no.** Zed gates and sends `logout`; the ACP schema makes it optional. | **Later.** Useful for profile switching, but unrelated to scanning 12 seats and lower value than action surfacing. | [S26], [A10], [P4] |
| `ToolCallUpdate`: kinds, locations, raw input/output | **Zed yes; Pursers partial.** ACP carries read/edit/delete/move/search/execute/think/fetch/switch-mode/other, status, locations, raw input, and raw output. Zed renders kind-specific icons, raw input/output, and a one-location “Go to File” action. Pursers currently sends only `other`, status, `rawInput`, and textual content. | **Yes inside rank 1.** Emit honest kinds, bounded raw output, and a file/line location for evidence artifacts; this establishes click-to-file for a ticket result. | [A11], [S29], [S30], [P5] |
| Notifications; ending a turn on a question | **Zed yes at turn/wait boundaries, not on arbitrary updates.** This can reverse the current forever-open `/watch`: end on the first question/review/failure after final updates. | **Yes, rank 1.** It reuses Zed's completion/waiting notification instead of inventing a badge channel. | [S8], [S9], [P1] |
| Threads Sidebar and parallel threads | **Zed-only surface exists.** It groups threads by project and shows title, status indicator, and agent. An external agent can affect title, but status is only `Generating` while a prompt turn is running and `Idle` otherwise; it cannot publish a custom seat state indicator. | **Use, do not extend.** One thread per active ticket is sane; one per seat creates 12 client-managed conversations and is not. Put all seat state in one thread's plan. | [S31], [S32], [S14] |
| Worktree isolation and picker | **Zed client-owned.** The human picks/creates the worktree; ACP receives `cwd` and optional additional directories. An agent cannot pin or switch the thread to another worktree through a standard reverse request. | **Use operationally, no agent build.** Open the Pursers supervisor thread on the operator checkout; keep worker ticket worktrees outside it. | [S31], [S33], [S34] |
| Thread titles: auto, manual, regenerate, external-agent set | **All exist.** Zed supports auto/manual/regenerate; ACP `SessionInfoUpdate.title` lets Pursers replace the thread title. There is no ownership lock, so a later human edit or regeneration can replace it. | **Yes inside rank 1.** Use a compact actionable count, but treat it as a summary rather than an alert or durable identity. | [S14], [S15], [S35] |
| Review Changes multi-buffer diff, per-hunk controls, `agent.single_file_review` | **Zed yes when edits enter its action/diff model; Pursers no.** The panel can review all edits and accept/reject each hunk; inline review is opt-in. Current board tool calls produce no file diffs. | **No for board mutations.** Use it only if a future ticket-result tool emits a real patch; never synthesize a diff from claimed `files_changed`. | [S35], [A8], [P5] |
| `@` mentions: files, directories, symbols, previous threads, skills, diagnostics, branch diffs, URLs | **Zed yes; ACP fidelity depends on capabilities.** Files and symbols always appear; richer items are enabled as embedded context. Pursers currently receives only baseline resource links because it advertises no embedded context. | **Later.** Branch-diff and diagnostic context could improve a ticket question, but first implement truthful decoding and size limits. | [S27], [S35], [P4] |
| Terminal Threads as first-class panel entries | **Zed-only and yes.** They appear beside agent threads, with their own title, bell notification, and close lifecycle; they are not ACP sessions and do not enter Thread History. | **Do not build into Pursers.** A human may use one for an exceptional manual command, but seat supervision should stay in the ACP plan. | [S31], [S36] |
| Checkpoints, Follow Agent, Open Thread as Markdown | **Mixed.** Zed exposes all three, but checkpoints depend on tracked edits, Follow Agent depends on real tool locations, and Markdown export is a human action. Current Pursers supplies neither edits nor locations. | **Build only the location feed in rank 1.** It unlocks Follow Agent/click-through; use Markdown export as an existing evidence escape hatch, not a protocol deliverable. | [S30], [S35], [P5] |
| Agentic panel layout (`workspace::UseAgenticLayout`) | **Zed-only and yes.** The action places Threads Sidebar and Agent Panel together on the left and other panels on the right. ACP cannot select it. | **Recommend setup, no code.** It is the best stock layout for scanning one 12-seat plan alongside evidence files. | [S31], [S37] |

## recommended_cut

Build only these three increments, in this order:

1. **Action-first 12-seat watch surface.** Replace the current single `/watch`
   row with a deterministic, bounded full plan ordered by needs-human, active,
   then idle. On the first question, review, or failure, emit a compact summary,
   final plan, and `end_turn` so Zed's stock notification wakes the human. Add
   the compact title `Pursers · 3 need you · 7 active`. For evidence-producing
   board calls, emit an honest tool kind, bounded raw output, and the artifact's
   real file/line location so Zed can click through and Follow Agent.
2. **Structured coordinator input.** Map a coordinator question and its
   `required_fields` to one session-scoped form elicitation with explicit
   accept/decline/cancel handling. Do not use URL elicitation unless the ticket
   truly requires an external approval or authentication page.
3. **Small session controls.** Publish board and tier as select/value-ID config
   options and `needs-me only` as a boolean. Apply `ConfigOptionUpdate` after
   server-side changes so the picker stays authoritative; keep slash commands
   as the compatible fallback.

Do **not** attempt a custom pane, window, workspace tab, status-bar badge,
agent-chosen icon, or twelve agent-created top-level threads. Do not try to
embed the Personal MCP App or route native MCP Resources through Zed 1.20.2.
Do not build an arbitrary MCP notification shim: Zed does not render that
channel. Keep MCP responsible for tools/prompts and ACP responsible for the
supervisory plan, thread messages, tool cards, and title.

## source_refs

- **[S1]** Zed, [stable 1.20.2 release](https://zed.dev/releases/stable/1.20.2), 2026-09-17.
- **[S2]** Zed, [`v1.20.2` source tree at `7c451e…`](https://github.com/zed-industries/zed/tree/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f).
- **[S3]** Zed, [documented MCP support: Tools, Prompts, and tool-list change](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/mcp.md#L12-L17).
- **[S4]** Zed, [native MCP registry subscriptions and tool/prompt loading](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent/src/tools/context_server_registry.rs#L124-L235).
- **[S5]** Zed, [declared MCP notification types](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/context_server/src/types.rs#L96-L127).
- **[S6]** Zed, [ACP connection lifetime and notification handler registration](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L664-L763).
- **[S7]** Zed, [session registration lifetime and update routing](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L1165-L1212) and [unknown-session drop/forwarding](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L4747-L4848).
- **[S8]** Zed, [notification settings and prompt-completion contract](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/agent-panel.md#L106-L113).
- **[S9]** Zed, [`Stopped`, permission, and elicitation notification handling](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/conversation_view.rs#L1669-L1747).
- **[S10]** Zed, [complete plan application and replacement](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/acp_thread/src/acp_thread.rs#L3573-L3600).
- **[S11]** Zed, [plan model and Markdown construction](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/acp_thread/src/acp_thread.rs#L1952-L2010).
- **[S12]** Zed, [plan summary, all-entry renderer, statuses, and inner scroll](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/conversation_view/thread_view.rs#L3687-L3867).
- **[S13]** Zed, [conversation and activity bar are separate children](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/conversation_view/thread_view.rs#L12472-L12501).
- **[S14]** Zed, [`SessionInfoUpdate` title handling](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/acp_thread/src/acp_thread.rs#L2610-L2623).
- **[S15]** Zed, [conversation title refresh after `TitleUpdated`](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/conversation_view.rs#L1805-L1823).
- **[S16]** Zed, [the ACP client capabilities Zed advertises](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L766-L794).
- **[S17]** Zed, [Zed-specific nested subagent loading](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/conversation_view.rs#L2064-L2119).
- **[S18]** Zed, [MCP result conversion and explicit Resource/ResourceLink discard](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent/src/tools/context_server_registry.rs#L399-L464).
- **[S19]** Zed, [finite extension manifest contribution surface](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension/src/extension_manifest.rs#L83-L123).
- **[S20]** Zed, [finite extension trait surface](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_api/src/extension_api.rs#L68-L198).
- **[A1]** ACP Rust schema 1.5.0, [Plan replacement semantics](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/plan.rs.html#21-50) and [entries, priorities, and statuses](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/plan.rs.html#436-525).
- **[A2]** ACP Rust schema 1.5.0, [`SessionUpdate` variants](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/client.rs.html#90-139) and [`SessionInfoUpdate` fields](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/client.rs.html#231-260).
- **[A3]** ACP Rust schema 1.5.0, [`session/new` is a client request to the agent](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/agent.rs.html#1000-1037).
- **[P1]** Pursers at `ac9d2380…`, [current `/watch`, one-row plan, and update sender](https://github.com/swisspra/Pursers/blob/ac9d2380c6c26d39fc9adc7cf79851c97e4365f8/tools/acp-agent/src/pursers_acp/agent.py#L726-L749) and [plan payload](https://github.com/swisspra/Pursers/blob/ac9d2380c6c26d39fc9adc7cf79851c97e4365f8/tools/acp-agent/src/pursers_acp/agent.py#L880-L902).
- **[P2]** Pursers at `ac9d2380…`, [Personal MCP App URI](https://github.com/swisspra/Pursers/blob/ac9d2380c6c26d39fc9adc7cf79851c97e4365f8/packages/personal/src/pursers_personal/apps_server.py#L48-L57).
- **[P3]** Pursers at `ac9d2380…`, [Zed MCP relay registers tools and prompts only](https://github.com/swisspra/Pursers/blob/ac9d2380c6c26d39fc9adc7cf79851c97e4365f8/packages/client/src/pursers_client/mcp_proxy.py#L311-L343).
- **[S21]** Zed, [agent-to-client request handlers and advertised client capabilities](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L705-L794).
- **[S22]** Zed, [form/URL elicitation routing and URL completion](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L4565-L4680).
- **[S23]** Zed, [mode and config update handling](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L4747-L4797) and [set-mode/config requests](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L4376-L4472).
- **[S24]** Zed, [mode selector implementation](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/mode_selector.rs#L26-L159).
- **[S25]** Zed, [session list and delete implementation](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L559-L621) and [capability-gated session list](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L1044-L1063).
- **[S26]** Zed, [load, resume, and logout gates](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L1746-L1914) and [close on final thread release](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L1165-L1201).
- **[S27]** Zed, [image/embedded-context gates and available mention types](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/message_editor.rs#L68-L108) and [content-block conversion](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/message_editor.rs#L2138-L2179).
- **[S28]** Zed, [ACP forwarding of configured stdio and HTTP MCP servers](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L4328-L4373).
- **[S29]** Zed, [tool-call kind/status/content/location/raw data application](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/acp_thread/src/acp_thread.rs#L842-L1065).
- **[S30]** Zed, [tool-card raw data rendering and kind-specific treatment](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/conversation_view/thread_view.rs#L8177-L8405) and [location click-to-file](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/conversation_view/thread_view.rs#L9979-L10155).
- **[S31]** Zed docs, [Threads Sidebar, parallel threads, and Agentic layout](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/parallel-agents.md#L8-L64).
- **[S32]** Zed, [ACP thread status is only prompt-running `Generating` or `Idle`](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/acp_thread/src/acp_thread.rs#L2221-L2225) and [is derived from `running_turn`](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/acp_thread/src/acp_thread.rs#L2443-L2448).
- **[S33]** Zed docs, [human-owned worktree picker and isolation lifecycle](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/parallel-agents.md#L76-L86).
- **[S34]** Zed, [ACP session requests derive `cwd` and additional directories from client-selected work directories](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_servers/src/acp.rs#L1475-L1506).
- **[S35]** Zed docs, [checkpoints, Markdown export, titles, Follow Agent, review, and mentions](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/agent-panel.md#L72-L138).
- **[S36]** Zed docs, [Terminal Thread title, notification, and close behavior](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/terminal-threads.md#L55-L71).
- **[S37]** Zed docs, [`workspace::UseAgenticLayout` placement](https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/parallel-agents.md#L10-L18).
- **[A4]** ACP Rust schema 1.5.0, [client form/URL elicitation capabilities](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/elicitation.rs.html#1190-1367), [`elicitation/create`](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/elicitation.rs.html#1464-1535), and [`elicitation/complete`](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/elicitation.rs.html#2035-2077).
- **[A5]** ACP Rust schema 1.5.0, [select/boolean config options and value-ID fallback](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/agent.rs.html#2469-2717) and [boolean client capability](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/client.rs.html#1987-2054).
- **[A6]** ACP Rust schema 1.5.0, [optional list/delete/resume/close session capabilities](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/agent.rs.html#4158-4230).
- **[A7]** ACP Rust schema 1.5.0, [image, audio, and embedded-context prompt capabilities](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/agent.rs.html#4574-4619).
- **[A8]** ACP Rust schema 1.5.0, [tool-call terminal and diff content](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/tool_call.rs.html#491-647).
- **[A9]** ACP Rust schema 1.5.0, [MCP HTTP/SSE gates and mandatory stdio](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/agent.rs.html#2804-2838) and [`McpCapabilities`](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/agent.rs.html#4665-4722).
- **[A10]** ACP Rust schema 1.5.0, [optional logout capability](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/agent.rs.html#454-493).
- **[A11]** ACP Rust schema 1.5.0, [tool-call fields and partial updates](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/tool_call.rs.html#18-180), [kinds and statuses](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/tool_call.rs.html#427-489), and [file locations](https://docs.rs/agent-client-protocol-schema/1.5.0/src/agent_client_protocol_schema/v1/tool_call.rs.html#680-725).
- **[P4]** Pursers at `ac9d2380…`, [current request dispatch and empty advertised prompt capabilities](https://github.com/swisspra/Pursers/blob/ac9d2380c6c26d39fc9adc7cf79851c97e4365f8/tools/acp-agent/src/pursers_acp/agent.py#L478-L545).
- **[P5]** Pursers at `ac9d2380…`, [current basic board tool-call/update shape](https://github.com/swisspra/Pursers/blob/ac9d2380c6c26d39fc9adc7cf79851c97e4365f8/tools/acp-agent/src/pursers_acp/agent.py#L753-L813).
