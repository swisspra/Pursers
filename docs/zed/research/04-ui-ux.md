# Pursers in Zed: UI limits and a native-feeling UX

Research target: **Zed 1.20.2**, source tag `v1.20.2`, commit
[`7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f`](https://github.com/zed-industries/zed/tree/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f).
All Zed source links below are pinned to that commit. This is a design and
capability audit; it does not change a live Zed profile or Central instance.

## Conclusion

Pursers should not try to become a miniature dashboard inside Zed. The viable
product is a thin context-server extension plus, where the richer protocol is
worth it, the existing `pursers-acp` external agent:

- install and configure through Zed's existing Extensions and MCP settings UI;
- make the Agent Panel the primary surface;
- expose a small command vocabulary through MCP prompts and/or ACP available
  commands;
- return compact Markdown, images, and links from MCP; use ACP when native
  diffs, plans, modes, or richer permission affordances matter;
- open the existing Fleet dashboard in the system browser only when the user
  asks for the wider operational view; and
- keep an active `watch` turn when question delivery matters. Zed 1.20.2 does
  not expose arbitrary server-pushed user notifications.

The extension cannot add its own panel, status-bar item, Command Palette
action, or default keybinding. MCP Apps are not rendered, and MCP Resources are
not discoverable/readable by Zed's native Agent path. Those limits rule out an
embedded Pursers dashboard.

## Evidence standard

The public docs describe the supported product surface; the pinned source is
used for negative claims and renderer details. In particular, Zed documents
only MCP **Tools** and **Prompts**, plus
`notifications/tools/list_changed` ([MCP docs, lines 12–17][zed-mcp-features]).
The extension manifest's complete field list contains context servers and
legacy slash commands but no general UI/action/keymap contribution
([manifest, lines 83–123][zed-manifest]), while the extension trait exposes
server/configuration hooks but no arbitrary UI hook
([extension API, lines 68–198][zed-extension-api]).

“Unsupported” below means unsupported through the public extension, native MCP,
and ACP integration paths in this pinned release. It does not mean Zed itself
lacks the corresponding built-in UI.

## Reachable surfaces

| Surface | What Pursers can show or do | Limits in 1.20.2 | Evidence |
| --- | --- | --- | --- |
| Extension Gallery | A listing, install action, description, repository link, and an MCP context-server contribution. | This is installation/discovery, not a running app canvas. | Zed exposes MCP extensions through the gallery and MCP settings ([MCP docs, lines 27–38][zed-mcp-install]); the extension field is `context_servers` ([manifest, lines 108–116][zed-manifest]). |
| Context-server configuration modal | Markdown setup instructions, schema-validated JSON settings, and a default settings template. Saving writes the generated server entry to Zed settings. | It is a JSONC editor, not a custom form. “No hand-edited JSON file” is attainable; “no JSON-shaped input at all” is attainable only when valid defaults plus discovery/OAuth are sufficient. | The WIT contract defines instructions, JSON Schema, and defaults ([context-server WIT, lines 1–10][zed-context-wit]); the modal builds Markdown and an editor from them ([modal source, lines 127–155][zed-config-source]), validates the value ([lines 193–217][zed-config-validate]), and writes settings ([lines 644–663][zed-config-write]). |
| MCP status in Settings | The built-in state dot and tooltip show whether the server is active. | Pursers cannot relocate this or create a custom status-bar item. | Zed documents the green “Server is active” indicator ([MCP docs, lines 79–90][zed-mcp-config]); the public extension manifest/API has no status-bar contribution ([manifest][zed-manifest], [extension API][zed-extension-api]). |
| Agent Panel / tool calls | MCP tool text as rendered Markdown, MCP images, progress/final tool cards, errors, and clickable Markdown links. | MCP tool results do not gain ACP's structured diff or terminal renderer. An MCP text code fence can look like a diff but is not the native diff editor. Audio and resource results are ignored in the MCP-to-agent adapter. | Agent Panel is Zed's built-in chat/tool surface ([Agent Panel docs, lines 6–30][zed-agent-panel]); MCP text/image conversion and ignored variants are explicit in the adapter ([registry, lines 400–464][zed-mcp-convert]); ACP's renderer dispatches Markdown, images, structured diffs, and terminals ([thread view, lines 10197–10255][zed-tool-render]). |
| MCP prompts as slash commands | Named Pursers workflows such as `/pursers.board` or `/pursers.watch`, with descriptions and zero or one unstructured argument. | Name collisions are server-prefixed; prompts with more than one argument are omitted. A prompt seeds an agent turn—it is not a direct UI action. | Zed maps MCP prompts to available commands and handles collision prefixes/argument limits ([agent source, lines 1588–1635][zed-mcp-prompts]); invoking one calls `prompts/get`, injects its messages, then sends/resumes the turn ([lines 1934–2028][zed-mcp-prompt-run]). |
| MCP Resources | **No native user surface.** | Zed neither advertises Resources as supported nor lists/reads them on the native Agent path. Resource and ResourceLink blocks returned by an MCP tool are discarded by the MCP adapter. | The supported-feature list names only Tools and Prompts ([MCP docs][zed-mcp-features]); returned resource variants are ignored ([registry, lines 400–464][zed-mcp-convert]). |
| MCP Apps (`io.modelcontextprotocol/ui`, `ui://`) | **No native user surface.** | Zed 1.20.2 has no MCP App resource discovery/renderer. `_meta` or a `ui://` resource cannot turn a tool call into an embedded Pursers dashboard. | This follows from the documented Tools/Prompts-only feature set ([MCP docs][zed-mcp-features]) and the adapter dropping MCP resource results ([registry][zed-mcp-convert]). Pursers itself exposes its Personal app at `ui://pursers/dashboard` ([Pursers source][pursers-ui-uri]), which is why that existing UI does not appear in Zed. |
| ACP external-agent thread | Agent messages/thoughts, tool-call cards, Markdown, images, embedded resources, resource links, structured diffs, terminals, current mode, config options, plan/todo state, and available slash commands. | The ACP agent owns its runtime/auth/config. Zed's native profile does not automatically configure it. The shipped `pursers-acp` currently advertises text and resource links, not MCP Apps. | Zed hosts ACP agents in the Agent Panel ([External Agents docs, lines 6–22][zed-external-agents]); session updates include tool calls, plan, commands, modes, and config ([ACP thread, lines 2586–2639][zed-acp-updates]); structured content/diff/terminal are distinct variants ([ACP thread, lines 1807–1846][zed-acp-content]); Pursers documents its current boundary ([connecting clients, lines 517–548][pursers-zed-acp]). |
| Threads Sidebar / thread history | An installed ACP agent appears in the new-thread menu; its sessions live beside other agent threads. | This is Zed-owned navigation, not an extension-owned Pursers view. | Zed documents ACP agents in both the Agent Panel and Threads Sidebar, including their new-thread entry ([External Agents docs, lines 6–22][zed-external-agents]). |
| ACP modes | A compact built-in mode selector when the agent publishes session modes. | Pursers must implement and publish meaningful modes; an extension cannot restyle the selector. | Zed stores modes from ACP initialization/session state ([connection, lines 239–322][zed-acp-modes]) and uses the built-in mode selector ([mode selector, lines 43–195][zed-mode-selector]). |
| ACP permission prompts | Native allow/deny choices, including dropdown/pattern variants when the protocol supplies them. | This is ACP tool authorization, not a general-purpose modal API. Native Zed Agent MCP tools instead obey Zed's MCP tool-permission settings. | ACP authorization becomes a waiting tool-call state ([ACP thread, lines 3383–3417][zed-acp-auth]); Zed renders flat/dropdown/pattern choices ([thread view, lines 9379–9429][zed-permission-ui]); native MCP defaults to confirm/allow/deny ([MCP docs, lines 144–159][zed-mcp-permissions]). |
| ACP plan/todo | A built-in compact plan summary and pending/in-progress/completed rows. | Available only when the ACP agent sends plan updates. | Plan updates are consumed by the thread ([ACP updates][zed-acp-updates]) and rendered with state-specific icons ([thread view, lines 3794–3854][zed-plan-ui]). |
| Notifications | A desktop/sound notification after an active Agent Panel generation completes while Zed is in the background. | An MCP server cannot emit an arbitrary Pursers notification. Zed handles only tool-list-change notifications; question delivery therefore needs an active `watch` turn (or another external notification system). | Agent completion notifications are documented ([Agent Panel docs, lines 106–113][zed-agent-notifications]); the MCP registry subscribes specifically to `notifications/tools/list_changed` ([registry, lines 142–154][zed-tools-changed]). |
| Status bar | The user can use Zed's built-in Agent Panel sparkle button to open the panel. | No extension-defined Pursers status item, count, or attention badge. The MCP server state dot lives in Settings, not the status bar. | The built-in Agent Panel entry point is documented ([Agent Panel docs, lines 6–14][zed-agent-panel]); no status contribution exists in the manifest or trait ([manifest][zed-manifest], [extension API][zed-extension-api]). |
| Tasks / task templates | A static command can appear in the task picker if supplied by a language extension, or users/projects can define tasks in their own `tasks.json`. Tasks run in Zed terminals. | No dynamic Pursers task list or arbitrary task-registration API. A Pursers context-server-only extension has no reason to pretend to be a language extension. | Zed lists global, worktree, one-shot, and language-extension task sources ([Tasks docs, lines 65–80][zed-tasks]); task bindings target the existing `task::Spawn` action ([lines 196–230][zed-task-keybinding]). |
| Slash commands | MCP prompts in native Zed Agent and ACP `available_commands` in external-agent threads. | The legacy extension `slash_commands` manifest/API exists, but it is not a basis for this design: current native MCP prompts and ACP commands are the maintained, evidenced paths. | MCP mapping is in the native agent ([agent source][zed-mcp-prompts]); ACP command updates are consumed by the thread ([ACP updates][zed-acp-updates]); legacy fields/hooks remain in the extension manifest/trait ([manifest][zed-manifest], [extension API, lines 163–180][zed-extension-api]). |
| Open URL | Return an `https://` Markdown link from an MCP result or an ACP ResourceLink; activation opens it through Zed's link handling/system browser. | The extension WASM API cannot imperatively open an arbitrary URL. The user activates the link. | ACP ResourceLinks render as buttons that invoke link opening ([thread view, lines 10312–10380][zed-resource-link]); workspace link handling sends HTTP(S) to the system opener ([workspace, lines 5149–5200][zed-open-link]). |
| Review changes | Zed's built-in Review Changes multibuffer can show agent edits; an ACP tool can additionally send a native structured diff. | A plain MCP result cannot populate the structured ACP diff variant. | Review Changes is documented as a special diff view ([Agent Panel docs, lines 115–124][zed-review-changes]); ACP converts protocol diffs into finalized native diffs ([ACP thread, lines 1823–1844][zed-acp-content]). |

## Direct answers: extension-owned chrome

| Requested integration | Can an extension add it? | Closest supported substitute |
| --- | --- | --- |
| Custom panel | **No.** | Use the built-in Agent Panel through MCP, or a `pursers-acp` thread. Put the full fleet view in the existing browser dashboard. |
| Custom status-bar item | **No.** | Use the built-in Agent Panel button for entry and MCP Settings' state dot for connection health. Return a compact attention count inside `/pursers.board`. |
| Command Palette action | **No arbitrary action.** | The built-in Command Palette can open Extensions, Agent Panel/settings, tasks, and ACP threads. Product workflows should be Agent slash commands. |
| Shipped keybinding | **No arbitrary/default keymap contribution.** | Document optional user keybindings to existing Zed actions, such as opening a particular ACP agent thread or spawning a named user task. Zed's keymap editor binds existing actions ([Keymap docs, lines 30–53][zed-keymaps]); ACP documents `agent::NewExternalAgentThread` as bindable ([External Agents docs, lines 117–121][zed-acp-keybinding]). |

This is an intentionally conservative interpretation of the public surface. The
absence is not inferred from a single documentation omission: the pinned
manifest and extension trait are the contribution boundary, and neither has a
panel, status, action-registration, keymap, toast, or open-URL hook
([manifest][zed-manifest], [extension API][zed-extension-api]). The Command
Palette itself lists registered Zed actions ([Command Palette docs, lines
6–14][zed-command-palette]); an extension cannot register a new general action
through the exposed 1.20.2 API.

## Proposed UX

The names below are product proposals, not claims about commands shipped today.
Use the prefix only where Zed adds it to resolve collisions; otherwise favor
short names.

### 1. First install: extension to connected board

1. The user opens the Command Palette, runs **Zed: Extensions**, searches for
   **Pursers**, and installs the context-server extension. This is the normal
   documented MCP-extension route ([MCP docs][zed-mcp-install]).
2. Zed opens its native **Configure Pursers** modal. The extension supplies:
   short Markdown instructions; a strict schema; and a valid default such as a
   discovered local profile name or an OAuth-capable remote URL. Secrets never
   appear in the example or extension manifest.
3. If discovery/defaults are sufficient, the user only presses **Configure**.
   Zed validates and writes the settings entry. If a required value cannot be
   discovered, the user edits the small JSONC object in this modal—not a
   settings file—and the copy explains exactly one correction.
4. The user verifies the green state dot in **Settings → AI → MCP Servers**,
   opens the Agent Panel, and types `/pursers.connect` (proposed prompt) for a
   one-screen connection summary.

```text
┌─ Configure Pursers ───────────────────────────────┐
│ Connect this project to the discovered board.    │
│                                                   │
│ { "profile": "default", "board": "project" }  │
│                                                   │
│ Open Repository               [Cancel] [Configure]│
└───────────────────────────────────────────────────┘

Agent Panel
  /pursers.connect
  ✓ Connected to project-board · 3 workers · 1 reviewer
  Settings health: Server active
  [Open Fleet dashboard ↗]
```

The promise is **no hand-editing `settings.json`**, not an invented custom
form. The modal is demonstrably a Markdown description plus JSONC editor
([modal render, lines 794–866][zed-config-render]). If zero-input defaults are
unsafe or ambiguous, stop and ask for the minimum explicit board/profile value.

### 2. Daily loop: one thread, progressive disclosure

Keep four workflows and let normal language invoke the same tools:

- `/pursers.board` — a compact board summary and “needs you” list;
- `/pursers.create <summary>` — collect the minimum ticket fields, preview the
  mutation, then rely on Zed's native tool permission confirmation;
- `/pursers.watch` — hold an active streaming/long-running tool or ACP turn for
  claim, review, and human-question events; and
- `/pursers.evidence <ticket>` — show exact branch/commit, exact files, tests,
  and review state, with links to wider views.

```text
Agent Panel — Pursers

  /pursers.board

  project-board                                      updated now
  Needs you   1     Active   4     Review   2

  TK-123  Review evidence for retry fix       submitted
  TK-124  Human answer required               waiting

  [Watch this board]  [Open Fleet dashboard ↗]

  > /pursers.evidence TK-123
  Branch   codex/TK-123-fix
  Commit   0123456789abcdef…
  Files    2 exact tip paths
  Tests    48 passed
  Review   pending independent reviewer
```

Operational sequence:

1. **Board.** Return Markdown text sized for the panel, not the entire fleet
   table. Put stable IDs first so keyboard copy/search remains useful.
2. **Create.** The model calls the create tool. Zed's existing MCP permission
   prompt is the confirmation boundary; do not add a second “Are you sure?” in
   chat. Zed's default is already confirm ([MCP permissions][zed-mcp-permissions]).
3. **Watch claim and review.** `/pursers.watch` remains the active request. It
   updates one tool card/turn with meaningful state transitions instead of
   appending a message per heartbeat. When the turn completes in the
   background, Zed may issue its normal completion notification
   ([Agent notifications][zed-agent-notifications]).
4. **Review evidence.** MCP returns concise Markdown and links. If the daily
   experience runs through `pursers-acp`, it may also publish plan state and
   structured diffs; MCP alone must not claim a native diff.
5. **Merge.** Pursers can report the approved exact SHA and provide a link to
   the repository/hosting review surface. Merging remains an explicitly
   permissioned tool or external host action; the extension contributes no
   bespoke merge button.

### 3. A seat asks the human a question

The reliable Zed-native path is an already-active watch:

```text
Background: /pursers.watch is active
          │
          ├─ board event: human answer required
          ▼
Agent Panel
  TK-124 · worker-3 asks
  “Should the migration preserve the deprecated alias?”

  Reply: /pursers.answer TK-124 <answer>
  [Open ticket in Fleet dashboard ↗]
          │
          └─ active turn completes → Zed background notification
```

The user answers in the same thread; the answer tool gets the normal native
permission prompt. If no watch turn is active, Zed 1.20.2 provides no arbitrary
MCP notification channel to wake the user. The UI must say **“Watch must remain
active for in-Zed questions”** instead of implying background push.

ACP can present the same flow as a plan/tool update, but that does not change
the wake-up limitation. `pursers-acp` already starts its own wait bridge for the
watch intent, so the proposed integration must not start a second bridge
([Pursers Zed/ACP guide][pursers-zed-acp]).

## Reuse of existing Pursers UI

### Fleet dashboard

Use the Fleet dashboard as the full-fidelity operational view. It already owns
fleet home, boards, agents, operations, ticket timelines, and human-request
resolution ([Pursers README, lines 154–164][pursers-components]); architecture
identifies it as a loopback web UI ([architecture, lines 52–63][pursers-arch]).
MCP/ACP responses should return explicit `http://127.0.0.1:…` links only when a
configured local dashboard is known. Zed opens HTTP(S) links in the system
browser ([workspace link handling][zed-open-link]). Never auto-open it on
install, every state change, or watch completion.

### Personal MCP App

Do **not** claim that the existing MCP App opens inside Zed. Pursers exposes it
as `ui://pursers/dashboard` ([app source][pursers-ui-uri]), while Zed 1.20.2
does not support MCP Resources/Apps on the native Agent path. Its useful reuse
is conceptual and data-level:

- reuse the established Home/Projects/Work/Team/Approvals/Activity naming and
  bounded read projections;
- reuse concise copy and state ordering when formatting Markdown summaries;
- link to a separately supported MCP Apps host only when such a host URL is
  actually available; and
- never present the `ui://` URI as a browser URL or a Zed-embeddable page.

This preserves one information architecture without pretending that two host
rendering contracts are interchangeable.

## Fit with Zed conventions

The design follows Zed's keyboard-first, minimal-chrome shape: install and
health live in Settings, work lives in one Agent thread, commands are searchable
slash commands, and the broad dashboard is an explicit escape hatch. The
normal path needs no persistent Pursers chrome and no custom modal stack.

Progressive disclosure matters more than decoration:

- first response: counts, current ticket IDs, state, one next action;
- expanded evidence: branch/commit, exact files, tests, reviewer state;
- external dashboard: fleet-wide comparison, history, administration.

Animation adds little here. Tool execution and ACP plan state already have Zed
feedback. Pursers should not add animated ASCII spinners, continuously rewrite
the entire board, or use an image/GIF as live state. Keep the existing ticket
flow asset for documentation/onboarding, not the daily Agent Panel.

## Cut list

These ideas should be removed from an implementation brief even if they look
attractive in a mockup:

1. **Pursers side panel or webview** — unsupported and duplicates the Fleet
   dashboard.
2. **Status-bar queue badge** — unsupported, permanently consumes chrome, and
   makes a count look fresher than the event cursor proves.
3. **Embedded Personal MCP App** — unsupported; Zed drops the resource path
   required to render it.
4. **One Command Palette action per board operation** — unsupported and noisy;
   keep workflows in the Agent Panel's slash-command completion.
5. **Extension-owned default keybindings** — unsupported and conflict-prone;
   document optional user bindings to existing actions only.
6. **Modal wizard after the native configuration modal** — unnecessary modal
   stacking. Make defaults valid, then teach through the first Agent response.
7. **Automatic Fleet dashboard launch** — surprising context switch. Return one
   explicit link.
8. **Notification for every offer, claim, lease renewal, and review update** —
   impossible through arbitrary MCP push and hostile even if routed elsewhere.
   Reserve attention for a human question or user-requested watch completion.
9. **Fake native diff in an MCP code fence** — label it as text or use ACP's
   structured diff path.
10. **Dynamic tasks as a second command system** — tasks are static/user or
    language-extension templates, while Pursers state is dynamic. One command
    vocabulary is easier to learn.

## Implementation boundary

The first credible implementation can be small:

1. one context-server extension entry with instructions, strict schema, and
   useful defaults;
2. MCP tools plus four prompts whose outputs are bounded Markdown/image/link
   content;
3. `pursers-acp` as an optional richer thread for watch, plan, permissions, and
   structured diffs;
4. deep links to the existing Fleet dashboard; and
5. explicit capability text saying Personal MCP Apps do not render in Zed.

Anything that requires custom Zed chrome, arbitrary server notifications, or
an embedded HTML app requires an upstream Zed capability—not a workaround in
the Pursers extension.

## Source index

[zed-manifest]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension/src/extension_manifest.rs#L83-L123
[zed-extension-api]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_api/src/extension_api.rs#L68-L198
[zed-context-wit]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/extension_api/wit/since_v0.8.0/context-server.wit#L1-L10
[zed-config-source]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/agent_configuration/configure_context_server_modal.rs#L127-L155
[zed-config-validate]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/agent_configuration/configure_context_server_modal.rs#L193-L217
[zed-config-write]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/agent_configuration/configure_context_server_modal.rs#L644-L663
[zed-config-render]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/agent_configuration/configure_context_server_modal.rs#L794-L866
[zed-mcp-features]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/mcp.md#L12-L17
[zed-mcp-install]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/mcp.md#L27-L38
[zed-mcp-config]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/mcp.md#L79-L90
[zed-mcp-permissions]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/mcp.md#L144-L159
[zed-mcp-prompts]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent/src/agent.rs#L1588-L1635
[zed-mcp-prompt-run]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent/src/agent.rs#L1934-L2028
[zed-mcp-convert]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent/src/tools/context_server_registry.rs#L400-L464
[zed-tools-changed]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent/src/tools/context_server_registry.rs#L142-L154
[zed-agent-panel]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/agent-panel.md#L6-L30
[zed-agent-notifications]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/agent-panel.md#L106-L113
[zed-review-changes]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/agent-panel.md#L115-L124
[zed-external-agents]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/external-agents.md#L6-L22
[zed-acp-keybinding]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/ai/external-agents.md#L117-L121
[zed-acp-updates]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/acp_thread/src/acp_thread.rs#L2586-L2639
[zed-acp-content]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/acp_thread/src/acp_thread.rs#L1807-L1846
[zed-acp-auth]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/acp_thread/src/acp_thread.rs#L3383-L3417
[zed-acp-modes]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/acp_thread/src/connection.rs#L239-L322
[zed-mode-selector]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/mode_selector.rs#L43-L195
[zed-plan-ui]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/conversation_view/thread_view.rs#L3794-L3854
[zed-permission-ui]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/conversation_view/thread_view.rs#L9379-L9429
[zed-tool-render]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/conversation_view/thread_view.rs#L10197-L10255
[zed-resource-link]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/agent_ui/src/conversation_view/thread_view.rs#L10312-L10380
[zed-open-link]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/crates/workspace/src/workspace.rs#L5149-L5200
[zed-tasks]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/tasks.md#L65-L80
[zed-task-keybinding]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/tasks.md#L196-L230
[zed-keymaps]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/key-bindings.md#L30-L53
[zed-command-palette]: https://github.com/zed-industries/zed/blob/7c451e694f3c52ee0aeb01d7e28b5fa18cd0ad2f/docs/src/command-palette.md#L6-L14
[pursers-components]: ../../../README.md#whats-in-the-box
[pursers-arch]: ../../ARCHITECTURE.md#components
[pursers-ui-uri]: ../../../packages/personal/src/pursers_personal/apps_server.py#L54
[pursers-zed-acp]: ../../guides/connecting-clients.md#zed-and-other-acp-hosts
