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

## recommended_cut

Build the next Zed work in this order:

1. **A 12-seat ACP plan projection.** Replace the current single `/watch` row
   with a deterministic, bounded list of one row per seat, ordered by
   needs-human, active, then idle. Emit the entire list on each meaningful
   change, using only the three ACP statuses. Put counts and long evidence in a
   message or tool card, not in row labels.
2. **Wake on actionable event by ending the watch turn.** Keep the current
   push-based wait inside an active `/watch` turn, but stop on the first
   human-question, review-needed, or failed-seat event after emitting one
   compact summary and the final plan. Returning `end_turn` lets Zed's existing
   background completion notification wake the human. The human can re-arm
   `/watch`; cursor persistence prevents replay. This is more reliable than
   idle-session updates because those render silently.
3. **A compact dynamic thread title.** Emit `SessionInfoUpdate.title`, for
   example `Pursers · 3 need you · 7 active`, whenever the actionable count
   changes. Treat it as a thread-list summary only, never as a badge or durable
   alert.

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
