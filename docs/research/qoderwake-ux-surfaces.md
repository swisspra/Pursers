# QoderWake surface language and a Pursers redesign direction

This is a visual study of the public QoderWake page and the product pages it links directly. It does not assess third-party task intake, policy, or product claims. The evidence was rendered in Ego Lite on 2026-09-14 at `1440x900` and `400x844`; the full-page PNGs and their URLs, timestamps, and hashes are recorded in [`manifest.sha256`](qoderwake-ux/surfaces/manifest.sha256). No theme control was exposed at either viewport, so there is no dark render to report. ([QoderWake desktop](qoderwake-ux/surfaces/qoderwake-1440x900-light.png), [QoderWake mobile](qoderwake-ux/surfaces/qoderwake-400x844-light.png))

## What the public pages actually show

### Overall visual language

The desktop QoderWake page uses a nearly white canvas, charcoal sans-serif type, a restrained moss-green accent, pale green announcement and feature fields, thin gray dividers, soft shadows, and large rounded product frames. The hierarchy is a centered hero, one large application image, a three-card benefit row, an asymmetric feature mosaic, and a sparse footer. The exact hero lines are “QoderWake” and “Autonomous AI Employees, On the Job.” ([1440px evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png))

At `400x844`, the wordmark and hamburger remain while the desktop navigation disappears. The hero centers above a scaled application image; every following group becomes one column. Cards retain generous separation, but text inside the scaled application image is too small to function as mobile product UI. ([400px evidence](qoderwake-ux/surfaces/qoderwake-400x844-light.png))

The linked pages share the pale announcement strip, white navigation, black sans-serif headings, muted gray supporting text, green accents, thin dashed or ruled dividers, rounded panels, and a repeated footer. Their content structures differ: most lead with a product image, while Agent SDK leads with prose and a comparison table. Their 400px behavior also differs; most feature grids stack, but the Agent SDK and Cloud Agents comparison tables remain wider than the viewport and are visibly cropped on the right. ([Qoder desktop](qoderwake-ux/surfaces/qoder-1440x900-light.png), [Qoder mobile](qoderwake-ux/surfaces/qoder-400x844-light.png), [Mobile desktop](qoderwake-ux/surfaces/mobile-1440x900-light.png), [Mobile mobile](qoderwake-ux/surfaces/mobile-400x844-light.png), [IDE desktop](qoderwake-ux/surfaces/ide-1440x900-light.png), [IDE mobile](qoderwake-ux/surfaces/ide-400x844-light.png), [JetBrains desktop](qoderwake-ux/surfaces/jetbrains-1440x900-light.png), [JetBrains mobile](qoderwake-ux/surfaces/jetbrains-400x844-light.png), [CLI desktop](qoderwake-ux/surfaces/cli-1440x900-light.png), [CLI mobile](qoderwake-ux/surfaces/cli-400x844-light.png), [Agent SDK desktop](qoderwake-ux/surfaces/agent-sdk-1440x900-light.png), [Agent SDK mobile](qoderwake-ux/surfaces/agent-sdk-400x844-light.png), [Cloud Agents desktop](qoderwake-ux/surfaces/cloud-agents-1440x900-light.png), [Cloud Agents mobile](qoderwake-ux/surfaces/cloud-agents-400x844-light.png))

### Directly linked product pages, page by page

| Page | Literal 1440px structure | Literal 400px behavior and limits |
|---|---|---|
| Qoder | The centered hero reads “Meet New Qoder” and “Agentic Coding Platform For Everyone,” followed by a large framed application view with a left rail, an “Active Conversations” heatmap, and a prompt area. “How Qoder works” is an asymmetric green, gray, and blue card mosaic; “Extend the way you work” pairs connector counts with a logo grid. ([1440px evidence](qoderwake-ux/surfaces/qoder-1440x900-light.png)) | The navigation becomes a hamburger; the framed application view is reduced rather than recomposed. The “How Qoder works” mosaic becomes a vertical card sequence, the connector figures sit above the logo grid, and the footer becomes two text columns. Motion, hover states, and a true product empty or error state are not observable. ([400px evidence](qoderwake-ux/surfaces/qoder-400x844-light.png)) |
| Mobile | “Qoder Mobile” leads into a two-column rounded panel: a phone render on aqua at left and “Manage your AI agents anywhere,” platform buttons, and supporting copy at right. “Agent control, wherever you are” uses four equal dashed columns; “Qoder for Glasses” uses a wide text-and-glasses panel. ([1440px evidence](qoderwake-ux/surfaces/mobile-1440x900-light.png)) | The hero panel stacks the phone render above the copy and full-width platform buttons. The four control cards become one dashed vertical list, while the glasses copy sits above the product image in one rounded card. Loading, empty, approval-pending, error, and motion states are not observable. ([400px evidence](qoderwake-ux/surfaces/mobile-400x844-light.png)) |
| IDE | “Qoder IDE” and “Agentic IDE for Real Software” precede a wide split panel with an IDE screenshot on green at left and product copy plus a three-row operating-system selector at right. “Deep Intelligence, Real Engineering” ends in four equal dashed feature columns. ([1440px evidence](qoderwake-ux/surfaces/ide-1440x900-light.png)) | The split panel becomes one card with the screenshot above the copy and operating-system rows. The four feature columns stack inside one dashed outline. Task progress, empty editor, error, hover, and motion states are not observable. ([400px evidence](qoderwake-ux/surfaces/ide-400x844-light.png)) |
| JetBrains Plugin | “Qoder JetBrains Plugin” leads into a split panel with a JetBrains screenshot on teal at left and “Built for JetBrains IDE Developers” plus one green action at right. “Production-grade Backend Intelligence” has a Product Hunt mark and four equal dashed feature columns. ([1440px evidence](qoderwake-ux/surfaces/jetbrains-1440x900-light.png)) | The screenshot sits above the copy and full-width action in one card; the four feature columns become a vertical dashed list. No in-product task state, empty state, failure state, hover, or motion is observable. ([400px evidence](qoderwake-ux/surfaces/jetbrains-400x844-light.png)) |
| CLI | “Qoder CLI” leads into a split panel with a terminal transcript on yellow-green at left and “Terminal-native, AI-powered” plus three install-command rows at right. “Engineering in the Terminal” combines three metrics with four dashed feature columns; “Integrated capabilities” ends in a wide SDK card. ([1440px evidence](qoderwake-ux/surfaces/cli-1440x900-light.png)) | The terminal image moves above the copy and command rows. Metrics visible on desktop are absent from the 400px render; the four features stack, and the SDK labels and command fields become a compact vertical card. Live command progress, empty terminal, errors, hover, and motion are not observable. ([400px evidence](qoderwake-ux/surfaces/cli-400x844-light.png)) |
| Agent SDK | The page opens with the centered heading “The Agent Harness that drives Qoder is now available as an SDK.” and a green “Document” action; there is no dominant framed product image. A large “Three paths, clear at a glance” comparison table follows, then a six-cell dashed capability grid, overlapping ecosystem cards, a four-step “How to use” line, and four scenario cards. ([1440px evidence](qoderwake-ux/surfaces/agent-sdk-1440x900-light.png)) | The comparison table does not reflow: only its left columns fit and the right side is cropped at the 400px edge. The six capabilities stack, the ecosystem cards sit below their copy, the usage steps form a vertical sequence, and the scenarios become one column. Interactive table access, loading, empty, error, hover, and motion are not observable. ([400px evidence](qoderwake-ux/surfaces/agent-sdk-400x844-light.png)) |
| Cloud Agents | A centered two-line hero sits above three actions and a dense rounded systems diagram. “Integrate in minutes. Ship in hours.” is a three-column comparison table; “Patterns proven in real products” is a horizontal four-item line; “Turn agents into callable cloud services” is a six-cell dashed grid. ([1440px evidence](qoderwake-ux/surfaces/cloud-agents-1440x900-light.png)) | The systems diagram becomes a tall vertical card. The comparison table remains wider than the viewport and its right column is cropped; the four patterns and six service cells stack vertically. Runtime status, service emptiness, errors, hover, and motion are not observable. ([400px evidence](qoderwake-ux/surfaces/cloud-agents-400x844-light.png)) |

### AI employee identity

The most concrete identity treatment is a black lanyard badge under the exact heading “Your First AI Employee.” It uses a cartoon portrait, a role label, and the visual language of a staff credential. The adjacent copy says an employee has a name, role, onboarding date, scoped permissions, and record, but the badge render itself does not visibly show an onboarding date or live work status. ([desktop evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png), [mobile evidence](qoderwake-ux/surfaces/qoderwake-400x844-light.png))

The hero’s embedded product image is more operational. Its left rail lists four named Wakers with small avatars and specialty subtitles; the central “Assigned Waker” table shows avatar/name, role, a green-dot `Online` status, model, and workspace. This is identity as a scannable roster rather than a settings record. ([desktop evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png))

The public renders do not show an identity edit form, a complete employee-detail page, a blocked identity, or an offboarded identity. They also do not show how the promised onboarding date appears in product UI. ([desktop evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png), [mobile evidence](qoderwake-ux/surfaces/qoderwake-400x844-light.png))

### Team and roster

The exact heading “Multiple Wakers, Working Together” sits under a compact workspace card. That card leads with a workspace name, then a horizontal member strip of circular portraits and names, followed by group-skill rows; the visual reads as a team before it reads as infrastructure. ([desktop evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png), [mobile evidence](qoderwake-ux/surfaces/qoderwake-400x844-light.png))

The larger hero render combines the Waker sidebar, roster table, two IM-connection cards, and a recent-activity timeline within one rounded workspace. Status is visually quiet: a green dot and `Online`, with the current work context carried by the workspace and timeline rather than by a large colored banner. ([desktop evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png))

No public render shows a zero-member team, a partial-load state, duplicate names, stale members, or a team-level error. Those empty and failure states are therefore not observable. ([desktop evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png), [mobile evidence](qoderwake-ux/surfaces/qoderwake-400x844-light.png))

### Task and work

The hero workspace has tabs labelled `Overview`, `Memory`, `Task`, and `Expert Configuration`. Its recent-activity line mixes event types such as memory added, automation run, task created, and skill self-evolution, while a green enable control anchors the top-right. The visible default is a summary of people, connections, and activity; it is not a dense ticket queue. ([desktop evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png))

The three-card row uses one screenshot per work mode under the exact headings “A Teammate in the IM Chat,” “Autonomously Start Work on Its Own,” and “Multiple Wakers, Working Together.” The screenshots show a generated summary, a schedule form, and a team workspace respectively, so the page explains work through concrete artifacts rather than status counts alone. ([desktop evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png), [mobile evidence](qoderwake-ux/surfaces/qoderwake-400x844-light.png))

The static captures do not establish animation timing, easing, hover behavior, keyboard behavior, or a task’s live transition between states. No task-empty, waiting, blocked, retry, or error panel is visible. Those behaviors and states remain not observable. ([desktop evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png), [mobile evidence](qoderwake-ux/surfaces/qoderwake-400x844-light.png))

## Side-by-side with Pursers today

| Surface | QoderWake render | Pursers today | Design gap worth addressing |
|---|---|---|---|
| First impression | One centered promise followed immediately by a large product frame; green is reserved for emphasis. ([evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png)) | Fleet opens with navigation, health counts, “Waiting for you,” and a long attention list. ([evidence](../showcase/01-fleet-overview.png); implementation: `tools/fleet-dashboard/fleet_dashboard.py`) | Pursers proves operational depth first, but it does not introduce the active team or current work first. |
| Roster | Names, portraits, roles, `Online`, model, and workspace appear in one compact table; a separate workspace card uses a member strip. ([evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png)) | Personal already has initials, name, role, platform, project, ticket, idle time, and `working`/`idle` pills. ([evidence](../showcase/04-personal-agents.png); implementation: `tools/dashboard-ui/src/dashboard.ts`) Fleet’s `liveAgentCard()` presents role chips, name, central/boards, `busy`/`available`/`stale`, last-seen age, current work, and controls. | The information exists, but identity, availability, and current work compete at the same visual weight; Fleet lacks a clear read-only detail surface. |
| Work | The workspace uses tabs plus a recent-activity line, making team, task, and history feel like one object. ([evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png)) | AionUi shows a large ticket form and a separate recent-ticket rail; status pills include `open`, `claimed`, and `submitted`. ([evidence](../showcase/06-aionui-offer-claim.png)) | Pursers’ lifecycle is explicit, but the person doing the work and the next meaningful event are visually detached from the ticket. |
| Seat status | Identity uses a portrait/name/role treatment in the marketing render, while operational status is a small green-dot word. ([evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png)) | The simple AionUi status card is a state icon, title, summary, and seat fields; the broader Home screenshot emphasizes helper transport and connection state. ([evidence](../showcase/07-aionui-status.png); implementation: `tools/aionui-extension/webui/index.html`, `app.js`, `style.css`) | Transport truth must remain, but the card can lead with “who is this seat, what is it doing, what happens next?” |
| Responsive behavior | Marketing cards stack cleanly at 400px, but the hero’s desktop application screenshot is merely reduced. ([evidence](qoderwake-ux/surfaces/qoderwake-400x844-light.png)) | The current showcase is desktop-oriented and does not itself prove a 400px roster/detail composition. ([Fleet evidence](../showcase/01-fleet-overview.png), [Personal evidence](../showcase/04-personal-agents.png)) | Pursers should reflow real controls and data, not scale a desktop surface into illegibility. |

## Ranked proposals

Ranking weighs first-impression improvement against implementation effort. The proposals change presentation and vocabulary, not the underlying authorization or lifecycle rules.

### 1. Make the active team the Fleet hero — high impact / small effort / beta.2

Lead the Agents view with a compact strip of the first four active identities: avatar or initials, stable display name, role, status, and current ticket title. Keep central/board metadata one level down. Implement in `tools/fleet-dashboard/fleet_dashboard.py` within `renderAgentsHub()` and `liveAgentCard()`. Size: **S**.

```text
Your active team                                      4 active
[MO] mong1-worker-6   WORKING   QoderWake surfaces  ›
[RE] reviewer-2       WAITING   Review queue        ›
```

Acceptance: at 1440px the strip and full card grid coexist without horizontal scrolling; at 400px each row stacks identity above current work; every row and detail trigger is keyboard reachable; status does not rely on color; light and dark themes preserve the existing contrast tokens.

### 2. Use one human status vocabulary — high impact / medium effort / beta.2

Map operational states to four display words: **Idle** (available, no owned work), **Working** (active claim), **Waiting** (submitted/review or explicit human dependency), and **Blocked** (actionable failure or expired/stale work). Preserve raw state in details and accessible text. Implement the mapping where Fleet projections are prepared and rendered in `tools/fleet-dashboard/fleet_dashboard.py`, and mirror it in `tools/aionui-extension/webui/app.js`. Size: **M**.

```text
● Working     current task and last activity
○ Idle        ready for offered work
◐ Waiting     names the reviewer or human dependency
! Blocked     names the condition and recovery action
```

Acceptance: both widths show the word plus an icon; keyboard focus reveals raw state in the detail panel; screen readers receive word and explanation; light/dark tests distinguish all four without color-only meaning.

### 3. Add a read-only agent detail drawer — high impact / medium effort / beta.2

Open a detail drawer from each Fleet identity. Reuse the existing dialog styling in `tools/fleet-dashboard/fleet_dashboard.py`, but separate read-only identity and work context from management controls. Show name, role, joined/onboarded date when the product data supplies it, current status, current ticket, board seats, last activity, and a short event line. Size: **M**.

```text
┌ Agent detail ─────────────────────────────── [×] ┐
│ [MO] mong1-worker-6        WORKING               │
│ Worker · joined 15 Sep · last active 2m ago      │
│ Current work  TK-…  QoderWake surfaces           │
│ Seats         pursers / worker                    │
│ Recent        claimed → capture → validation      │
└───────────────────────────────────────────────────┘
```

Acceptance: 1440px uses a bounded side drawer and 400px uses a full-width sheet; focus moves into the drawer, is trapped, and returns to the trigger; Escape and close work; missing dates render “Not available,” not invented data; both themes retain visible focus and hierarchy.

### 4. Recompose the AionUi status card around identity — high impact / small effort / beta.2

Keep the current connection truth, but promote seat name, role, human status, current work, and next action above low-frequency fields such as key ID and expiry. Implement in `tools/aionui-extension/webui/index.html`, `app.js`, and `style.css`. Size: **S**.

```text
[MO] mong1-worker-6                         WORKING
Worker on pursers
QoderWake surface research · active 2m ago
[Open current ticket]
Connection details ▾
```

Acceptance: the card fits without clipping at both widths; the disclosure is a real button with `aria-expanded`; Tab order follows the visual order; raw connection fields remain available; ready, empty, unavailable, and recovery states pass in light and dark system themes.

### 5. Attach a three-event workline to active identities — medium-high impact / medium effort / beta.2

Show only the current ticket and the three most useful recent transitions beneath an expanded agent, using the event data Fleet already receives. Do not create a second task system. Implement beside `agentLiveWork()` and `liveAgentCard()` in `tools/fleet-dashboard/fleet_dashboard.py`. Size: **M**.

```text
Current  TK-…  Capture visual evidence
17:48    Claimed
17:56    Evidence captured
Now      Working
```

Acceptance: chronological labels remain readable at both widths; keyboard users can expand/collapse the line; empty history says “No recent activity”; timestamps and status icons have text equivalents; light/dark themes preserve divider visibility.

### 6. Replace generic emptiness with state-specific next steps — medium impact / small effort / beta.2

Give each empty state a concrete explanation and safe next step: no seats connected, no active agents, no current task, and no recent activity. Update Fleet’s `renderAgentsHub()` and AionUi’s `createStartupView()` in `tools/aionui-extension/webui/app.js`. Size: **S**.

```text
No active agents
Connected seats are idle or currently unavailable.
[Show idle and stale seats]
```

Acceptance: each empty state is visible and accurate at both widths; any action is keyboard accessible and cannot mutate without the existing guard; the state is announced through the current live region; light/dark themes keep illustration or icon secondary to the message.

### 7. Use responsive information disclosure, not scaled tables — medium impact / medium effort / beta.2

Keep Fleet’s desktop density, but at 400px turn each agent into a card with the first line reserved for identity/status and a disclosure for boards, last seen, and controls. Apply within the embedded Fleet CSS and `liveAgentCard()` in `tools/fleet-dashboard/fleet_dashboard.py`. Size: **M**.

```text
1440: [identity] [status] [current work] [last active] [›]
 400: [identity] [status]
      current work
      Details ▾
```

Acceptance: no horizontal page scroll at 400px; 1440px retains fast scanning; DOM order matches reading order in both layouts; keyboard and 200% zoom do not hide controls; light/dark tokens cover cards, dividers, and focus rings.

### 8. Add restrained state transitions — medium impact / small effort / later

Use motion only when a status or current task changes: a short background fade on the changed row and the existing skeleton while loading. Do not animate idle cards continuously. Implement in Fleet’s embedded CSS and `tools/aionui-extension/webui/style.css`. Size: **S**.

```text
WAITING  ── data refresh ──>  WORKING
          one brief highlight, then still
```

Acceptance: the change is understandable with motion disabled; `prefers-reduced-motion: reduce` removes nonessential transitions; no layout shift occurs at either width; focus is unchanged during refresh; both themes use a subtle tokenized highlight.

## What not to copy

- Do not use the employee badge metaphor as the only identity surface. The public badge is memorable, but its render omits the live status and onboarding date mentioned beside it; Pursers must keep stable seat and board facts directly readable. ([evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png))
- Do not shrink a desktop application screenshot into a mobile card. The QoderWake page reflows its prose well at 400px, but the embedded roster text becomes illegible; Pursers should recompose its real controls. ([evidence](qoderwake-ux/surfaces/qoderwake-400x844-light.png))
- Do not collapse all healthy states into a green `Online` dot. That word is simple in the Qoder roster, but it does not distinguish idle, working, or waiting; Pursers needs those distinctions in text. ([evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png))
- Do not design only the populated showcase. Every QoderWake product frame shown here is filled with people, tasks, or activity; Pursers must keep honest empty, stale, blocked, and unavailable states. ([desktop evidence](qoderwake-ux/surfaces/qoderwake-1440x900-light.png), [mobile evidence](qoderwake-ux/surfaces/qoderwake-400x844-light.png))
- Do not copy the marketing page’s long sequence of similarly weighted feature panels into the operational product. The repeated matrix is consistent across linked pages, but an operator surface needs stronger prioritization of current work and exceptions. ([Qoder evidence](qoderwake-ux/surfaces/qoder-1440x900-light.png), [Cloud Agents evidence](qoderwake-ux/surfaces/cloud-agents-1440x900-light.png))

## Evidence limits

These PNGs prove only the public renders at the recorded time and sizes. They do not prove authenticated product behavior, animation, interactive state transitions, keyboard support, dark-theme behavior, or any state absent from the captures. No account was created, no download was installed, and no vendor was contacted.
