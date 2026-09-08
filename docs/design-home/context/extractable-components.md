# Extractable Components — Pursers Dashboard Surfaces

Components that appear on multiple pages or define shared UI patterns, suitable for extraction as reusable Superdesign DraftComponent entities.

## Layout Components (appear on most pages)

### AppHeader

- Source: `tools/dashboard-ui/dashboard-entry.html` — `<header class="app-header">`
- Category: layout
- Description: Dashboard header with brand block (product-mark, eyebrow, labels, h1, board-id) and header actions (source status, refresh button)
- Extractable props: `boardName` (string, default: "Personal Preview Demo"), `boardId` (string, default: "Local board"), `sourceLabel` (string, default: "Preparing local preview")
- Hardcoded: "OB" product mark, "On Board Personal" eyebrow, "Personal Preview" label, "Local" label, "Read-only" label, refresh icon "↻"

### ViewTabs (Dashboard-UI)

- Source: `tools/dashboard-ui/src/dashboard.ts` — `selectView()` + HTML `<nav class="view-tabs">`
- Category: layout
- Description: Horizontal tab navigation with 6 views (Today, Work, Agents, Fleet, Links, Activity). Active tab gets accent background. Keyboard navigation (Arrow Left/Right, Home, End).
- Extractable props: `activeView` (string, default: "today")
- Hardcoded: Tab labels, tab order, aria attributes

### SidebarNav (Fleet Dashboard)

- Source: `tools/fleet-dashboard/fleet_dashboard.py` line ~6090 — `<aside class="sidebar">`
- Category: layout
- Description: Vertical sidebar with brand logo, primary navigation (Overview, Boards, Agents, Operations), and footer text
- Extractable props: `activeNav` (string, default: "overview")
- Hardcoded: "P" brand mark, "Pursers Fleet" brand text, nav labels, nav icons (⌂, ▦, ◎, ⚙), "Loopback control plane" footer

### ConnectionBanner

- Source: `tools/dashboard-ui/src/dashboard.ts` — `renderConnection()` + HTML `<section class="connection-banner">`
- Category: layout
- Description: Status banner showing connection state (live, demo, stale, error) with status dot, title, and detail text. Sticky in fleet dashboard.
- Extractable props: `tone` (string: "live"|"demo"|"stale"|"error"), `title` (string), `detail` (string)
- Hardcoded: Status dot colors, "Loading board" default title, "Connecting through the local MCP Apps bridge." default detail

### SearchBar

- Source: `tools/dashboard-ui/src/dashboard.ts` — `renderSearch()` + HTML `<section class="command-bar">`
- Category: layout
- Description: Global search with icon, input, keyboard shortcut hint ("/"), and result count. Results overlay shows categorized matches.
- Extractable props: `placeholder` (string, default: "Search tickets, agents, links, activity")
- Hardcoded: "⌕" search icon, "/" kbd hint

### AppFooter (Dashboard-UI)

- Source: `tools/dashboard-ui/dashboard-entry.html` — `<footer class="app-footer">`
- Category: layout
- Description: Footer with product name and local/read-only notice
- Extractable props: none (static)
- Hardcoded: "On Board Personal Preview", "Local · Read-only · Actions stay in agent chat"

## Basic Components (used across pages)

### HealthCard

- Source: `tools/dashboard-ui/src/dashboard.ts` — `renderHealth()` + HTML `<article class="health-card card">`
- Category: basic
- Description: Project health card showing status (clear, work in progress, ready for review, needs attention, connection interrupted, demo data) with icon and detail text
- Extractable props: `tone` (string: "live"|"demo"|"stale"|"error"), `title` (string), `detail` (string)
- Hardcoded: "●" health icon, "Project health" kicker

### MetricCard

- Source: `tools/dashboard-ui/dashboard-entry.html` — `<div class="metric card"><dt>Label</dt><dd>Value</dd></div>`
- Category: basic
- Description: Large number display card with label above and value below
- Extractable props: `label` (string), `value` (string|number)
- Hardcoded: none

### Pill / Badge

- Source: `tools/dashboard-ui/src/dashboard.ts` — `pill()`
- Category: basic
- Description: Rounded pill badge for status, priority, or category. Supports tone variants (working, submitted, live, attention, warning, neutral).
- Extractable props: `label` (string), `tone` (string, default: none/neutral)
- Hardcoded: none

### TicketRow

- Source: `tools/dashboard-ui/src/dashboard.ts` — `ticketRow()`
- Category: basic
- Description: Compact ticket row with title, ID, status pill, priority pill, and optional lease countdown badge
- Extractable props: `ticketId` (string), `title` (string), `status` (string), `priority` (string), `leaseExpiresAt` (string|null)
- Hardcoded: none

### AgentCard

- Source: `tools/dashboard-ui/src/dashboard.ts` — `renderAgents()`
- Category: basic
- Description: Agent card with avatar (2-char initials), name, status pills, focus text, meta row (role, platform, project, ticket, idle time), duplicate-name warning
- Extractable props: `agentName` (string), `status` (string), `role` (string|null), `platform` (string|null), `project` (string|null), `currentTicketId` (string|null), `idleMinutes` (number), `stale` (boolean), `duplicateName` (boolean)
- Hardcoded: avatar initials derived from name

### WorkCard

- Source: `tools/dashboard-ui/src/dashboard.ts` — `renderWork()` work-card section
- Category: basic
- Description: Work ticket card with ticket ID, title, description, meta pills (priority, status, assigned_to, lease, abandoned count)
- Extractable props: `ticketId`, `title`, `description`, `priority`, `status`, `assignedTo`, `leaseExpiresAt`, `abandonedCount`
- Hardcoded: none

### EmptyState

- Source: `tools/dashboard-ui/src/dashboard.ts` — `emptyState()`
- Category: basic
- Description: Placeholder content for empty sections with bold title and muted detail text
- Extractable props: `title` (string), `detail` (string)
- Hardcoded: none

### CopyButton

- Source: `tools/dashboard-ui/src/dashboard.ts` — `copyButton()` + `copyValue()`
- Category: basic
- Description: Small button that copies text to clipboard. Shows "Copied" or "Copy unavailable" feedback. Falls back to textarea selection.
- Extractable props: `label` (string), `value` (string)
- Hardcoded: 1.5s feedback timeout

### TimelineItem

- Source: `tools/dashboard-ui/src/dashboard.ts` — `renderTimeline()`
- Category: basic
- Description: Event item with sequence number, kind label, text, actor pill, and timestamp
- Extractable props: `seq` (number|null), `kind` (string), `text` (string), `actorId` (string|null), `occurredAt` (string|null)
- Hardcoded: "#" prefix for seq, "_" replaced with " " in kind

### HighlightCard

- Source: `tools/dashboard-ui/src/dashboard.ts` — `renderHighlight()`
- Category: basic
- Description: Highlight card for handoff or pinned note with type label, title, summary, author pill, timestamp, and next-steps/warnings list
- Extractable props: `type` (string), `title` (string), `summary` (string), `author` (string|null), `createdAt` (string|null), `nextSteps` (string[]), `warnings` (string[])
- Hardcoded: "Warning: " prefix in warnings list

### FleetTable

- Source: `tools/dashboard-ui/src/dashboard.ts` — `fleetTable()` + `renderFleet()` + `renderLinks()`
- Category: basic
- Description: Scrollable data table with column headers, used for fleet projects, fleet pool, and can be reused for any tabular data
- Extractable props: `headers` (string[])
- Hardcoded: none

### LinkMemory

- Source: `tools/dashboard-ui/src/dashboard.ts` — `renderLinks()`
- Category: basic
- Description: Memory entry card within a link group, showing title, memory ID, type pill, pinned badge, file list with copy buttons, tag pills
- Extractable props: `memoryId` (string), `title` (string), `memoryType` (string), `pinned` (boolean), `createdAt` (string|null), `files` (Array), `tags` (Array)
- Hardcoded: none

### Fleet BoardCard

- Source: `tools/fleet-dashboard/fleet_dashboard.py` — `renderBoardsHub()` and `renderCentral()`
- Category: basic
- Description: Board summary card with eyebrow (central), label, board ID, ticket count pills, and action links (Workspace, Flow, Timeline, Changes, Routes)
- Extractable props: `central` (string), `label` (string), `boardId` (string), `counts` (Record<string, number>)
- Hardcoded: action link labels

### JoinForm

- Source: `tools/aionui-extension/webui/index.html` — `<form id="join-form">`
- Category: basic
- Description: Door/token input form with label, password input, and submit button
- Extractable props: `placeholder` (string, default: none)
- Hardcoded: "Paste your door" label, "Join" button text

### StatusCard

- Source: `tools/aionui-extension/webui/index.html` — `<section id="status-card">`
- Category: basic
- Description: Definition list showing board, role, seat name, push mode, key ID, and expiry
- Extractable props: `board`, `role`, `seatName`, `pushMode`, `kid`, `exp`
- Hardcoded: "Status" heading, field labels
