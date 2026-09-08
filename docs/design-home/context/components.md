# Components — Pursers Dashboard Surfaces

Shared/reusable UI component source for the primary dashboard (dashboard-ui) and the fleet dashboard. Framework: vanilla TypeScript DOM manipulation (dashboard-ui), inline JavaScript template literals (fleet-dashboard), and plain HTML/JS (extension).

Baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096` (origin/main, 5.0.0a25).

## Dashboard-UI Shared Primitives

Source: `tools/dashboard-ui/src/dashboard.ts` (1528 lines, 1–1528). Baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096`.

### element() — Generic DOM element factory

```typescript
function element<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, content?: string): HTMLElementTagNameMap[K] {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (content !== undefined) item.textContent = content;
  return item;
}
```

### emptyState() — Empty state card

```typescript
function emptyState(title: string, detail: string): HTMLElement {
  const item = element("div", "empty-state");
  item.append(element("strong", undefined, title), element("p", undefined, detail));
  return item;
}
```

### pill() — Badge / status pill

```typescript
function pill(label: string, tone?: string): HTMLElement {
  const item = element("span", "pill", label);
  if (tone) item.dataset.tone = tone;
  return item;
}
```

### agentField() — Agent field pill with empty state

```typescript
function agentField(label: string, value: string | null): HTMLElement {
  const item = pill(`${label} ${value ?? "\u2014"}`);
  if (value === null) item.dataset.empty = "true";
  return item;
}
```

### ticketRow() — Ticket row card (Today view)

```typescript
function ticketRow(ticket: Ticket): HTMLElement {
  const row = element("article", "list-row");
  const copy = element("div");
  copy.append(element("h3", undefined, ticket.title), element("p", "muted", ticket.id));
  const meta = element("div", "meta-row");
  meta.append(pill(ticket.status, toneForStatus(ticket.status)), pill(ticket.priority));
  const lease = leaseBadge(ticket.lease_expires_at);
  if (lease) meta.append(lease);
  copy.append(meta);
  row.append(copy);
  return row;
}
```

### leaseBadge() — Lease countdown badge

```typescript
function leaseBadge(value: string | null): HTMLElement | null {
  if (!value) return null;
  const item = pill(leaseText(value), "warning");
  item.classList.add("lease-countdown");
  item.dataset.leaseExpiresAt = value;
  return item;
}
```

### fleetTable() — Fleet data table builder

```typescript
function fleetTable(headers: string[]): { wrapper: HTMLElement; body: HTMLTableSectionElement } {
  const wrapper = element("div", "fleet-table-wrap");
  const table = element("table", "fleet-table");
  const head = element("thead");
  const row = element("tr");
  row.append(...headers.map(tableHeading));
  head.append(row);
  const body = element("tbody");
  table.append(head, body);
  wrapper.append(table);
  return { wrapper, body };
}
```

### copyButton() — Copy-to-clipboard button

```typescript
function copyButton(label: string, value: string): HTMLButtonElement {
  const button = element("button", "copy-button", label) as HTMLButtonElement;
  button.type = "button";
  button.dataset.copyValue = value;
  button.setAttribute("aria-label", `Copy ${value}`);
  return button;
}
```

### renderHighlight() — Highlight card (handoff / pinned note)

```typescript
function renderHighlight(container: HTMLElement, value: Highlight | null, emptyTitle: string, emptyDetail: string): void {
  if (!value) {
    container.replaceChildren(emptyState(emptyTitle, emptyDetail));
    return;
  }
  const item = element("article", "highlight");
  item.append(element("span", "highlight-type", value.type), element("h3", undefined, value.title));
  if (value.summary && value.summary !== value.title) item.append(element("p", "muted", value.summary));
  const meta = element("div", "meta-row");
  if (value.author) meta.append(pill(`by ${value.author}`));
  if (value.created_at) meta.append(pill(formatTime(value.created_at)));
  item.append(meta);
  const details = [...value.next_steps, ...value.warnings.map((warning) => `Warning: ${warning}`)];
  if (details.length) {
    const items = element("ul");
    details.forEach((detail) => items.append(element("li", undefined, detail)));
    item.append(items);
  }
  container.replaceChildren(item);
}
```

## Fleet-Dashboard Components

Source: `tools/fleet-dashboard/fleet_dashboard.py` (7507 lines, inline JS in HTML constant lines ~5828–6173, HTTP handler ~6435–7507). Baseline: `c2ebac5de803a0f7a00468ec4d3cdf06e4719096`.

### fleet dashboard helper: esc()

```javascript
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
```

### fleet dashboard helper: fmt()

```javascript
const fmt=v=>v?new Date(v).toLocaleString():'—';
```

### fleet dashboard: pageHead()

```javascript
function pageHead(kicker,title,copy,action=''){return `<header class="page-head"><div><p class="eyebrow">${esc(kicker)}</p><h2>${esc(title)}</h2><p class="muted">${esc(copy)}</p></div>${action}</header>`}
```

### fleet dashboard: pressureBadge()

```javascript
function pressureBadge(s){const label=s.pressure==='compact'?'COMPACT':s.pressure;return `<span class="status pressure-${esc(s.pressure)}" title="${esc(s.next_action)}">${esc(label)}</span>`}
```

### fleet dashboard: renderCentral() — Central section renderer

Renders per-central board cards, agent pool, and retire drawer. Source: lines ~5953-5990 of fleet_dashboard.py.

### fleet dashboard: ticketView() — Board ticket list

Renders sortable ticket table with expandable detail rows. Source: lines ~5900-5930.

### fleet dashboard: timelineView() — Board timeline

Groups events by day and ticket with expandable rows. Source: lines ~5900-5933.

### fleet dashboard: flowView() — Ticket flow kanban

4-column flow (Open → Claimed → Submitted → Closed today). Source: lines ~5940-5942.

### fleet dashboard: routesView() — Ticket provenance routes

Table of created/executed/submitted/reviewed stages per ticket plus per-seat load. Source: lines ~5944-5947.

## Extension Components

Source: `tools/aionui-extension/webui/app.js`

### Join form handler

```javascript
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const door = doorInput.value.trim();
  doorInput.value = '';
  message.textContent = 'Joining…';
  const response = await fetch('/pursers/join', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ door }),
  });
  const result = await readJson(response);
  if (!response.ok || !result.ok) {
    message.textContent = result.install_hint || 'Join failed. Ask your coordinator to check the door.';
    return;
  }
  message.textContent = `Joined and registered ${result.mcp_server}.`;
  showStatus(result.status);
});
```

### Status card renderer

```javascript
function showStatus(status) {
  for (const [field, value] of Object.entries(status)) {
    const target = card.querySelector(`[data-field="${field}"]`);
    if (!target) continue;
    target.textContent = field === 'exp' && value
      ? new Date(Number(value) * 1000).toLocaleString()
      : String(value || '—');
  }
  card.hidden = false;
}
```
