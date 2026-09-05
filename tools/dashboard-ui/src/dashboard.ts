import {
  App,
  PostMessageTransport,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
  type McpUiHostContext,
} from "@modelcontextprotocol/ext-apps";
import "./dashboard.css";

type DataMode = "live" | "stale" | "demo" | "demo-error";
type ViewName = "today" | "work" | "agents" | "fleet" | "links" | "activity";
type Agent = {
  id: string | null;
  name: string;
  status: string;
  role: string | null;
  focus: string | null;
  platform: string | null;
  idle_minutes: number;
  last_activity_at: string | null;
  lease_expires_at: string | null;
  stale: boolean;
  project?: string | null;
  duplicate?: boolean;
  duplicate_name?: boolean;
  suggested_name?: string | null;
  current_ticket?: Ticket | null;
  current_ticket_id?: string | null;
};
type Ticket = {
  id: string;
  title: string;
  description: string;
  status: string;
  priority: string;
  assigned_to: string | null;
  assigned_agent_id: string | null;
  claimed_agent_id: string | null;
  lease_expires_at: string | null;
  rejected: boolean;
  abandoned_count: number;
  rejection_count: number;
};
type BoardEvent = {
  id: string;
  seq: number | null;
  kind: string;
  text: string;
  actor_id: string | null;
  occurred_at: string | null;
  ticket_id: string | null;
  memory_id: string | null;
  status_from: string | null;
  status_to: string | null;
};
type Highlight = {
  id: string;
  type: string;
  title: string;
  summary: string;
  author: string | null;
  created_at: string | null;
  next_steps: string[];
  warnings: string[];
};
type Snapshot = {
  contract_version: number;
  data_mode: DataMode;
  activity_scope: "local-model-tools" | "synthetic-demo";
  fixture_provenance: string;
  board: { id: string; name: string };
  agents: Agent[];
  tickets: Ticket[];
  highlights: { latest_handoff: Highlight | null; important_pinned: Highlight | null };
  status: {
    ticket_status_counts: Record<string, number>;
    memory_type_counts: Record<string, number>;
    visible_memory_count: number;
    scrub_profile: string | null;
  };
  events: BoardEvent[];
  ticket_total: number;
  ticket_truncated: boolean;
  agent_total: number;
  agents_live: number;
  agent_truncated: boolean;
  event_cursor: number | null;
  dropped_events: number;
  has_more: boolean;
  connected: boolean;
  stale: boolean;
  feed_error: string | null;
  resync_notice: string | null;
  snapshot_at: string | null;
};
type FleetProject = {
  name: string | null;
  board_id: string | null;
  status: string | null;
  tickets_open: number | null;
  tickets_claimed: number | null;
  tickets_submitted: number | null;
};
type FleetSeat = {
  board_id: string | null;
  project: string | null;
  live: boolean | null;
  current_ticket_id: string | null;
};
type FleetPoolEntry = {
  principal_id: string | null;
  agent_name: string | null;
  pool_status: string | null;
  seats: FleetSeat[];
};
type FleetSnapshot = {
  schema_version: number | null;
  registry_warning: string | null;
  projects: FleetProject[];
  pool: FleetPoolEntry[];
  totals: {
    agents: number | null;
    busy: number | null;
    available: number | null;
    stale: number | null;
  };
};
type LinkNode = {
  memory_id: string;
  title: string;
  memory_type: string;
  created_at: string | null;
  pinned: boolean;
};
type LinkEdge = {
  kind: "ticket" | "file" | "tag" | "retracts";
  from: string;
  to: string;
  authority: "authoritative" | "suggested";
};
type LinkSnapshot = {
  schema_version: number;
  board_id: string;
  source_tool: string;
  relationship_authority: "authoritative";
  nodes: LinkNode[];
  edges: LinkEdge[];
  node_count: number;
  edge_count: number;
  returned_node_count: number;
  returned_edge_count: number;
  truncated: boolean;
};

type SearchHit = { view: ViewName; kind: string; title: string; detail: string };

const fallback: Snapshot = {
  contract_version: 2,
  data_mode: "demo",
  activity_scope: "synthetic-demo",
  fixture_provenance: "synthetic, authored for standalone embedded fallback",
  board: { id: "board-synthetic", name: "Personal Preview Demo" },
  agents: [
    { id: "AI-DEMO-1", name: "agent-alpha", status: "working", role: "builder", focus: "Personal UI shell", platform: "synthetic", idle_minutes: 1, last_activity_at: "2099-01-01T00:00:00Z", lease_expires_at: null, stale: false },
    { id: "AI-DEMO-2", name: "reviewer-β", status: "idle", role: "reviewer", focus: "Accessibility & special characters", platform: "synthetic", idle_minutes: 18, last_activity_at: "2099-01-01T00:00:00Z", lease_expires_at: null, stale: false },
  ],
  tickets: [
    { id: "TK-DEMO-1", title: "Shape the Personal Preview dashboard", description: "Synthetic example — no project data is loaded.", status: "claimed", priority: "high", assigned_to: "agent-alpha", assigned_agent_id: "AI-DEMO-1", claimed_agent_id: "AI-DEMO-1", lease_expires_at: null, rejected: false, abandoned_count: 0, rejection_count: 0 },
    { id: "TK-DEMO-2", title: "Review <safe> & readable — ทดสอบ", description: "HTML-like text stays inert: <img src=x onerror=alert(1)> · العربية · 中文 · 🧭", status: "submitted", priority: "medium", assigned_to: "reviewer-β", assigned_agent_id: "AI-DEMO-2", claimed_agent_id: null, lease_expires_at: null, rejected: false, abandoned_count: 0, rejection_count: 0 },
  ],
  highlights: {
    latest_handoff: { id: "MEM-DEMO-HANDOFF", type: "handoff", title: "UI shell ready for review", summary: "Synthetic handoff with the next checks for the Personal Preview.", author: "agent-alpha", created_at: "2099-01-01T00:03:00Z", next_steps: ["Check narrow layout", "Verify keyboard navigation"], warnings: [] },
    important_pinned: { id: "MEM-DEMO-WARNING", type: "warning", title: "Host proof is still pending", summary: "Synthetic reminder: SDK evidence is not real-host verification.", author: "reviewer-β", created_at: "2099-01-01T00:04:00Z", next_steps: [], warnings: ["Keep the preview label visible."] },
  },
  status: { ticket_status_counts: { claimed: 1, submitted: 1 }, memory_type_counts: { handoff: 1, warning: 1 }, visible_memory_count: 2, scrub_profile: "synthetic" },
  events: [
    { id: "EV-DEMO-1", seq: 1, kind: "ticket_status_changed", text: "TK-DEMO-1: open → claimed", actor_id: "AI-DEMO-1", occurred_at: "2099-01-01T00:01:00Z", ticket_id: "TK-DEMO-1", memory_id: null, status_from: "open", status_to: "claimed" },
    { id: "EV-DEMO-2", seq: 2, kind: "ticket_status_changed", text: "TK-DEMO-2: claimed → submitted", actor_id: "AI-DEMO-1", occurred_at: "2099-01-01T00:02:00Z", ticket_id: "TK-DEMO-2", memory_id: null, status_from: "claimed", status_to: "submitted" },
  ],
  ticket_total: 2,
  ticket_truncated: false,
  agent_total: 2,
  agents_live: 2,
  agent_truncated: false,
  event_cursor: 2,
  dropped_events: 0,
  has_more: false,
  connected: false,
  stale: true,
  feed_error: "No host bridge connected",
  resync_notice: null,
  snapshot_at: null,
};

const fleetFallback: FleetSnapshot = {
  schema_version: 1,
  registry_warning: "Synthetic fleet preview — no organization data is loaded.",
  projects: [
    { name: "preview", board_id: "board-synthetic", status: "active", tickets_open: 2, tickets_claimed: 1, tickets_submitted: 1 },
    { name: "paused-example", board_id: "board-paused", status: "paused", tickets_open: 0, tickets_claimed: 0, tickets_submitted: 0 },
  ],
  pool: [
    {
      principal_id: "PR-DEMO-1",
      agent_name: "agent-alpha",
      pool_status: "busy",
      seats: [{ board_id: "board-synthetic", project: "preview", live: true, current_ticket_id: "TK-DEMO-1" }],
    },
    {
      principal_id: "PR-DEMO-2",
      agent_name: "reviewer-β",
      pool_status: "available",
      seats: [{ board_id: "board-synthetic", project: "preview", live: true, current_ticket_id: null }],
    },
  ],
  totals: { agents: 2, busy: 1, available: 1, stale: 0 },
};

const linkFallback: LinkSnapshot = {
  schema_version: 1,
  board_id: "board-synthetic",
  source_tool: "synthetic-memory-links",
  relationship_authority: "authoritative",
  nodes: [
    { memory_id: "MEM-DEMO-LINK", title: "Synthetic release note", memory_type: "context", created_at: "2099-01-01T00:05:00Z", pinned: false },
  ],
  edges: [
    { kind: "ticket", from: "MEM-DEMO-LINK", to: "TK-DEMO-1", authority: "authoritative" },
    { kind: "file", from: "MEM-DEMO-LINK", to: "tools/dashboard-ui/src/dashboard.ts", authority: "authoritative" },
    { kind: "tag", from: "MEM-DEMO-LINK", to: "synthetic", authority: "authoritative" },
  ],
  node_count: 1,
  edge_count: 3,
  returned_node_count: 1,
  returned_edge_count: 3,
  truncated: false,
};

const BASE_FEED_DELAY_MS = 5_000;
const MAX_FEED_DELAY_MS = 60_000;
const MAX_AGENTS = 200;
const MAX_TICKETS = 500;
const MAX_EVENTS = 200;
const MAX_MAP_ENTRIES = 100;
const MAX_SHORT_TEXT_LENGTH = 512;
const MAX_LONG_TEXT_LENGTH = 4_096;
const MAX_SERIALIZED_RESULT_LENGTH = 4_000_000;
const app = new App({ name: "On Board Personal Preview", version: "5.0.0a19" });
const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const refreshButton = byId<HTMLButtonElement>("refresh");
const searchInput = byId<HTMLInputElement>("global-search");
const main = byId<HTMLElement>("main-content");
let snapshot = fallback;
let fleetSnapshot: FleetSnapshot | null = fleetFallback;
let fleetUnavailable = false;
let fleetBusy = false;
let linkSnapshot: LinkSnapshot | null = linkFallback;
let linksUnavailable = false;
let linksBusy = false;
let connected = false;
let feedBusy = false;
let feedTimer: number | undefined;
let feedDelayMs = BASE_FEED_DELAY_MS;
let activeView: ViewName = "today";
let lastRenderSignature = "";

const record = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown, fallbackValue = "", maxLength = MAX_SHORT_TEXT_LENGTH) => typeof value === "string" ? value.slice(0, maxLength) : fallbackValue;
const optionalText = (value: unknown, maxLength = MAX_SHORT_TEXT_LENGTH) => typeof value === "string" && value.length ? value.slice(0, maxLength) : null;
const finiteNumber = (value: unknown, fallbackValue = 0) => typeof value === "number" && Number.isFinite(value) ? value : fallbackValue;
const nonNegative = (value: unknown) => Math.max(0, finiteNumber(value));
const optionalNonNegative = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) : null;
const boolean = (value: unknown, fallbackValue = false) => typeof value === "boolean" ? value : fallbackValue;
const stringList = (value: unknown) => (Array.isArray(value) ? value : [])
  .filter((item): item is string => typeof item === "string")
  .slice(0, 8)
  .map((item) => item.slice(0, MAX_LONG_TEXT_LENGTH));

function decodeAgent(value: unknown): Agent | null {
  if (!record(value)) return null;
  return {
    id: optionalText(value.id),
    name: text(value.name, "unknown-agent"),
    status: text(value.status, "unknown"),
    role: optionalText(value.role),
    focus: optionalText(value.focus),
    platform: optionalText(value.platform),
    idle_minutes: nonNegative(value.idle_minutes),
    last_activity_at: optionalText(value.last_activity_at),
    lease_expires_at: optionalText(value.lease_expires_at),
    stale: boolean(value.stale),
    project: optionalText(value.project),
    duplicate: value.duplicate === true,
    duplicate_name: value.duplicate_name === true,
    suggested_name: optionalText(value.suggested_name),
    current_ticket: record(value.current_ticket) ? decodeTicket(value.current_ticket) : null,
    current_ticket_id: optionalText(value.current_ticket_id),
  };
}

function decodeTicket(value: unknown): Ticket | null {
  if (!record(value) || typeof value.id !== "string") return null;
  return {
    id: text(value.id),
    title: text(value.title, "(untitled)"),
    description: text(value.description, "", MAX_LONG_TEXT_LENGTH),
    status: text(value.status, "unknown"),
    priority: text(value.priority, "medium"),
    assigned_to: optionalText(value.assigned_to),
    assigned_agent_id: optionalText(value.assigned_agent_id),
    claimed_agent_id: optionalText(value.claimed_agent_id),
    lease_expires_at: optionalText(value.lease_expires_at),
    rejected: boolean(value.rejected),
    abandoned_count: nonNegative(value.abandoned_count),
    rejection_count: nonNegative(value.rejection_count),
  };
}

function decodeEvent(value: unknown): BoardEvent | null {
  if (!record(value) || typeof value.id !== "string") return null;
  return {
    id: text(value.id),
    seq: typeof value.seq === "number" && Number.isFinite(value.seq) ? value.seq : null,
    kind: text(value.kind, "updated"),
    text: text(value.text, "Board updated", MAX_LONG_TEXT_LENGTH),
    actor_id: optionalText(value.actor_id),
    occurred_at: optionalText(value.occurred_at),
    ticket_id: optionalText(value.ticket_id),
    memory_id: optionalText(value.memory_id),
    status_from: optionalText(value.status_from),
    status_to: optionalText(value.status_to),
  };
}

function decodeHighlight(value: unknown): Highlight | null {
  if (!record(value) || typeof value.id !== "string") return null;
  return {
    id: text(value.id),
    type: text(value.type, "context"),
    title: text(value.title, "Untitled note"),
    summary: text(value.summary, "", MAX_LONG_TEXT_LENGTH),
    author: optionalText(value.author),
    created_at: optionalText(value.created_at),
    next_steps: stringList(value.next_steps),
    warnings: stringList(value.warnings),
  };
}

function numberMap(value: unknown): Record<string, number> {
  if (!record(value)) return {};
  return Object.fromEntries(Object.entries(value)
    .slice(0, MAX_MAP_ENTRIES)
    .filter((entry): entry is [string, number] => typeof entry[1] === "number" && Number.isFinite(entry[1]))
    .map(([key, count]) => [key.slice(0, MAX_SHORT_TEXT_LENGTH), Math.max(0, count)]));
}

function isSnapshot(value: unknown): value is Snapshot {
  if (!record(value) || !record(value.board)) return false;
  return typeof value.board.id === "string"
    && typeof value.board.name === "string"
    && Array.isArray(value.agents)
    && Array.isArray(value.tickets)
    && Array.isArray(value.events);
}

function decodeSnapshot(value: unknown): Snapshot | null {
  if (!isSnapshot(value)) return null;
  const raw = value as unknown as Record<string, unknown>;
  if (raw.contract_version !== 2) return null;
  if (raw.data_mode !== "live" && raw.data_mode !== "stale" && raw.data_mode !== "demo") return null;
  if (raw.activity_scope !== "local-model-tools" && raw.activity_scope !== "synthetic-demo") return null;
  if (typeof raw.connected !== "boolean" || typeof raw.stale !== "boolean") return null;
  const provenance = text(raw.fixture_provenance, "unknown source");
  const mode: DataMode = raw.data_mode;
  const connectedValue = raw.connected;
  const staleValue = raw.stale;
  const activityScope = raw.activity_scope;
  if (mode === "demo" && activityScope !== "synthetic-demo") return null;
  if ((mode === "live" || mode === "stale") && activityScope !== "local-model-tools") return null;
  if (mode === "live" && (!connectedValue || staleValue)) return null;
  if ((mode === "stale" || mode === "demo") && (connectedValue || !staleValue)) return null;
  const highlights: Record<string, unknown> = record(raw.highlights) ? raw.highlights : {};
  const status: Record<string, unknown> = record(raw.status) ? raw.status : {};
  const agents = value.agents.slice(0, MAX_AGENTS).map(decodeAgent).filter((item): item is Agent => item !== null);
  const tickets = value.tickets.slice(0, MAX_TICKETS).map(decodeTicket).filter((item): item is Ticket => item !== null);
  const events = value.events.slice(0, MAX_EVENTS).map(decodeEvent).filter((item): item is BoardEvent => item !== null);
  const agentTotal = Math.max(agents.length, value.agents.length, nonNegative(raw.agent_total));
  const agentsLive = Math.min(
    agentTotal,
    typeof raw.agents_live === "number"
      ? nonNegative(raw.agents_live)
      : agents.filter((agent) => !agent.stale).length,
  );
  const ticketTotal = Math.max(tickets.length, value.tickets.length, nonNegative(raw.ticket_total));
  return {
    contract_version: 2,
    data_mode: mode,
    activity_scope: activityScope,
    fixture_provenance: provenance,
    board: { id: text(value.board.id), name: text(value.board.name) },
    agents,
    tickets,
    highlights: { latest_handoff: decodeHighlight(highlights.latest_handoff), important_pinned: decodeHighlight(highlights.important_pinned) },
    status: {
      ticket_status_counts: numberMap(status.ticket_status_counts),
      memory_type_counts: numberMap(status.memory_type_counts),
      visible_memory_count: nonNegative(status.visible_memory_count),
      scrub_profile: optionalText(status.scrub_profile),
    },
    events,
    ticket_total: ticketTotal,
    ticket_truncated: boolean(raw.ticket_truncated) || value.tickets.length > tickets.length || ticketTotal > tickets.length,
    agent_total: agentTotal,
    agents_live: agentsLive,
    agent_truncated: boolean(raw.agent_truncated) || value.agents.length > agents.length || agentTotal > agents.length,
    event_cursor: typeof raw.event_cursor === "number" && Number.isFinite(raw.event_cursor) ? raw.event_cursor : null,
    dropped_events: nonNegative(raw.dropped_events) + Math.max(0, value.events.length - events.length),
    has_more: boolean(raw.has_more),
    connected: connectedValue,
    stale: staleValue,
    feed_error: optionalText(raw.feed_error) ?? optionalText(raw.error),
    resync_notice: optionalText(raw.resync_notice),
    snapshot_at: optionalText(raw.snapshot_at),
  };
}

function decodeFleetProject(value: unknown): FleetProject | null {
  if (!record(value)) return null;
  return {
    name: optionalText(value.name),
    board_id: optionalText(value.board_id),
    status: optionalText(value.status),
    tickets_open: optionalNonNegative(value.tickets_open),
    tickets_claimed: optionalNonNegative(value.tickets_claimed),
    tickets_submitted: optionalNonNegative(value.tickets_submitted),
  };
}

function decodeFleetSeat(value: unknown): FleetSeat | null {
  if (!record(value)) return null;
  return {
    board_id: optionalText(value.board_id),
    project: optionalText(value.project),
    live: typeof value.live === "boolean" ? value.live : null,
    current_ticket_id: optionalText(value.current_ticket_id),
  };
}

function decodeFleetPoolEntry(value: unknown): FleetPoolEntry | null {
  if (!record(value)) return null;
  return {
    principal_id: optionalText(value.principal_id),
    agent_name: optionalText(value.agent_name),
    pool_status: optionalText(value.pool_status),
    seats: (Array.isArray(value.seats) ? value.seats : [])
      .slice(0, MAX_AGENTS)
      .map(decodeFleetSeat)
      .filter((item): item is FleetSeat => item !== null),
  };
}

function decodeFleetSnapshot(value: unknown): FleetSnapshot | null {
  if (!record(value)) return null;
  const totals = record(value.totals) ? value.totals : {};
  return {
    schema_version: optionalNonNegative(value.schema_version),
    registry_warning: optionalText(value.registry_warning, MAX_LONG_TEXT_LENGTH),
    projects: (Array.isArray(value.projects) ? value.projects : [])
      .slice(0, MAX_TICKETS)
      .map(decodeFleetProject)
      .filter((item): item is FleetProject => item !== null),
    pool: (Array.isArray(value.pool) ? value.pool : [])
      .slice(0, MAX_AGENTS)
      .map(decodeFleetPoolEntry)
      .filter((item): item is FleetPoolEntry => item !== null),
    totals: {
      agents: optionalNonNegative(totals.agents),
      busy: optionalNonNegative(totals.busy),
      available: optionalNonNegative(totals.available),
      stale: optionalNonNegative(totals.stale),
    },
  };
}

function looksLikeFleetSnapshot(value: unknown): boolean {
  if (!record(value)) return false;
  return ["schema_version", "registry_warning", "projects", "pool", "totals"]
    .some((key) => key in value);
}

function decodeLinkNode(value: unknown): LinkNode | null {
  if (!record(value) || typeof value.memory_id !== "string") return null;
  return {
    memory_id: text(value.memory_id),
    title: text(value.title, "Untitled memory"),
    memory_type: text(value.memory_type, "context"),
    created_at: optionalText(value.created_at),
    pinned: boolean(value.pinned),
  };
}

function decodeLinkEdge(value: unknown): LinkEdge | null {
  if (!record(value) || typeof value.from !== "string" || typeof value.to !== "string") return null;
  if (!["ticket", "file", "tag", "retracts"].includes(String(value.kind))) return null;
  const authority = value.authority === "suggested" ? "suggested" : "authoritative";
  return {
    kind: value.kind as LinkEdge["kind"],
    from: text(value.from),
    to: text(value.to, "", MAX_LONG_TEXT_LENGTH),
    authority,
  };
}

function decodeLinkSnapshot(value: unknown): LinkSnapshot | null {
  if (!record(value) || value.schema_version !== 1 || value.relationship_authority !== "authoritative") return null;
  if (!Array.isArray(value.nodes) || !Array.isArray(value.edges)) return null;
  return {
    schema_version: 1,
    board_id: text(value.board_id),
    source_tool: text(value.source_tool, "memory_links"),
    relationship_authority: "authoritative",
    nodes: value.nodes.slice(0, MAX_TICKETS).map(decodeLinkNode).filter((item): item is LinkNode => item !== null),
    edges: value.edges.slice(0, 1_000).map(decodeLinkEdge).filter((item): item is LinkEdge => item !== null),
    node_count: nonNegative(value.node_count),
    edge_count: nonNegative(value.edge_count),
    returned_node_count: nonNegative(value.returned_node_count),
    returned_edge_count: nonNegative(value.returned_edge_count),
    truncated: boolean(value.truncated),
  };
}

function looksLikeLinkSnapshot(value: unknown): boolean {
  return record(value) && value.source_tool === "memory_links" && Array.isArray(value.nodes) && Array.isArray(value.edges);
}

function serializedWithinLimit(value: unknown): boolean {
  try {
    return JSON.stringify(value).length <= MAX_SERIALIZED_RESULT_LENGTH;
  } catch {
    return false;
  }
}

function structured(result: { structuredContent?: unknown; content?: Array<{ type: string; text?: string }> }): unknown {
  const raw = result.structuredContent;
  if (raw !== undefined) {
    if (!serializedWithinLimit(raw)) return null;
    if (record(raw) && "result" in raw) return raw.result;
    return raw;
  }
  const body = result.content?.find((item) => item.type === "text")?.text;
  if (!body || body.length > MAX_SERIALIZED_RESULT_LENGTH) return null;
  try {
    const decoded: unknown = JSON.parse(body);
    return record(decoded) && "result" in decoded ? decoded.result : decoded;
  } catch {
    return null;
  }
}

function element<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, content?: string): HTMLElementTagNameMap[K] {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (content !== undefined) item.textContent = content;
  return item;
}

function emptyState(title: string, detail: string): HTMLElement {
  const item = element("div", "empty-state");
  item.append(element("strong", undefined, title), element("p", undefined, detail));
  return item;
}

function pill(label: string, tone?: string): HTMLElement {
  const item = element("span", "pill", label);
  if (tone) item.dataset.tone = tone;
  return item;
}

function shortTicketId(value: string | null): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  const body = trimmed.startsWith("TK-") ? trimmed.slice(3) : trimmed;
  return `TK-${body.slice(0, 8)}`;
}

function agentField(label: string, value: string | null): HTMLElement {
  const item = pill(`${label} ${value ?? "\u2014"}`);
  if (value === null) item.dataset.empty = "true";
  return item;
}

function toneForStatus(status: string): string {
  if (["submitted", "reviewing", "in_review", "closed"].includes(status)) return "submitted";
  if (["claimed", "in_progress", "creating_report", "working"].includes(status)) return "working";
  if (["rejected", "canceled", "terminated"].includes(status)) return "attention";
  return "neutral";
}

function ticketCounts(data: Snapshot): { open: number; working: number; submitted: number; attention: number } {
  const counts = data.status.ticket_status_counts;
  const count = (statuses: string[]) => statuses.reduce((sum, status) => sum + (counts[status] ?? data.tickets.filter((ticket) => ticket.status === status).length), 0);
  return {
    open: count(["open", "assigned"]),
    working: count(["claimed", "in_progress", "creating_report"]),
    submitted: count(["submitted", "reviewing", "in_review"]),
    attention: count(["rejected"]),
  };
}

function formatTime(value: string | null): string {
  if (!value) return "Time unavailable";
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return "Time unavailable";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(parsed);
}

function leaseText(value: string): string {
  const seconds = Math.max(0, Math.floor((Date.parse(value) - Date.now()) / 1000));
  if (!Number.isFinite(seconds) || seconds <= 0) return "lease overdue";
  if (seconds >= 86_400) return `lease ${Math.ceil(seconds / 86_400)}d`;
  if (seconds >= 3_600) return `lease ${Math.ceil(seconds / 3_600)}h`;
  if (seconds >= 60) return `lease ${Math.ceil(seconds / 60)}m`;
  return `lease ${seconds}s`;
}

function leaseBadge(value: string | null): HTMLElement | null {
  if (!value) return null;
  const item = pill(leaseText(value), "warning");
  item.classList.add("lease-countdown");
  item.dataset.leaseExpiresAt = value;
  return item;
}

function updateLeaseCountdowns(): void {
  document.querySelectorAll<HTMLElement>("[data-lease-expires-at]").forEach((item) => {
    const value = item.dataset.leaseExpiresAt;
    if (value) item.textContent = leaseText(value);
  });
}

function setTextIfChanged(target: HTMLElement, value: string): void {
  if (target.textContent !== value) target.textContent = value;
}

function renderConnection(data: Snapshot): void {
  const banner = byId<HTMLElement>("connection-banner");
  const title = byId<HTMLElement>("connection-title");
  const detail = byId<HTMLElement>("connection-detail");
  let tone: string;
  let titleText: string;
  let detailText: string;
  if (data.data_mode === "demo-error") {
    tone = "error";
    titleText = "Synthetic connection-error simulation";
    detailText = "No project state is shown. This fixture previews the read-only outage treatment.";
  } else if (data.data_mode === "demo") {
    tone = "demo";
    titleText = "Demo data — Central unavailable";
    detailText = "Everything below is synthetic and exists only to preview the local, read-only interface.";
  } else if (data.data_mode === "stale" || data.stale) {
    tone = data.feed_error ? "error" : "stale";
    titleText = "Showing the last known local state";
    detailText = data.feed_error ? "The local connection is unavailable. Retrying with bounded backoff." : "The activity feed is resynchronizing.";
  } else {
    tone = "live";
    titleText = "Connected to the local board";
    detailText = "Authorized Central projection; credentials remain outside the View.";
  }
  if (banner.dataset.tone !== tone) banner.dataset.tone = tone;
  setTextIfChanged(title, titleText);
  setTextIfChanged(detail, detailText);
}

function renderActivityScope(data: Snapshot): void {
  const synthetic = data.activity_scope === "synthetic-demo";
  setTextIfChanged(
    byId("recent-activity-scope"),
    synthetic ? "Synthetic preview activity" : "Observed by this MCP process",
  );
  setTextIfChanged(
    byId("activity-scope-copy"),
    synthetic
      ? "Synthetic preview events are authored fixtures; they do not represent local MCP-process activity or a complete audit log."
      : "Activity observed by this local MCP process; it is bounded and not a complete audit log.",
  );
}

function activityEmptyDetail(data: Snapshot): string {
  return data.activity_scope === "synthetic-demo"
    ? "Synthetic preview events appear here only to demonstrate the interface."
    : "Events appear after model tools run in this local MCP process.";
}

function renderHealth(data: Snapshot): void {
  const card = byId<HTMLElement>("health-title").closest<HTMLElement>(".health-card")!;
  const title = byId<HTMLElement>("health-title");
  const detail = byId<HTMLElement>("health-detail");
  const counts = ticketCounts(data);
  if (data.data_mode === "demo-error") {
    card.dataset.tone = "stale";
    title.textContent = "Simulated connection interruption";
    detail.textContent = "Synthetic UI state only; this is not a real Central outage.";
  } else if (data.data_mode === "demo") {
    card.dataset.tone = "demo";
    title.textContent = "Demo data";
    detail.textContent = "Connect the local Central service to see project health.";
  } else if (data.stale || data.feed_error) {
    card.dataset.tone = "stale";
    title.textContent = "Connection interrupted";
    detail.textContent = "Last-known information remains visible while the dashboard retries.";
  } else if (counts.attention > 0) {
    card.dataset.tone = "error";
    title.textContent = "Needs attention";
    detail.textContent = `${counts.attention} rejected item${counts.attention === 1 ? "" : "s"} need another pass.`;
  } else if (counts.submitted > 0) {
    card.dataset.tone = "live";
    title.textContent = "Ready for review";
    detail.textContent = `${counts.submitted} submission${counts.submitted === 1 ? " is" : "s are"} waiting for review.`;
  } else if (counts.working > 0) {
    card.dataset.tone = "live";
    title.textContent = "Work in progress";
    detail.textContent = `${counts.working} item${counts.working === 1 ? " is" : "s are"} currently being worked.`;
  } else if (counts.open > 0) {
    card.dataset.tone = "live";
    title.textContent = "Work is ready";
    detail.textContent = `${counts.open} open item${counts.open === 1 ? " is" : "s are"} ready to be claimed.`;
  } else {
    card.dataset.tone = "live";
    title.textContent = "Board is clear";
    detail.textContent = "No claimed or submitted work needs attention right now.";
  }
}

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

function renderToday(data: Snapshot): void {
  const counts = ticketCounts(data);
  byId("metric-open").textContent = String(counts.open);
  byId("metric-working").textContent = String(counts.working);
  byId("metric-submitted").textContent = String(counts.submitted);
  byId("metric-agents").textContent = String(data.agents_live);
  renderHealth(data);

  const work = data.tickets.filter((ticket) => !["closed", "canceled", "terminated"].includes(ticket.status)).slice(0, 4);
  byId("today-work").replaceChildren(...(work.length ? work.map(ticketRow) : [emptyState("No current work", "Open or claimed tickets will appear here.")]));

  const agents = data.agents.filter((agent) => !agent.stale).slice(0, 4).map((agent) => {
    const row = element("div", "list-row");
    const copy = element("div");
    copy.append(element("h3", undefined, agent.name), element("p", "muted", agent.focus ?? `${Math.round(agent.idle_minutes)}m idle`));
    row.append(copy, pill(agent.status, toneForStatus(agent.status)));
    return row;
  });
  byId("today-agents").replaceChildren(...(agents.length ? agents : [emptyState("No agents yet", "Agents appear after they join this local board.")]));

  renderHighlight(byId("latest-handoff"), data.highlights.latest_handoff, "No handoff yet", "A project handoff will appear after an agent records one.");
  renderHighlight(byId("important-pinned"), data.highlights.important_pinned, "No decision or warning in the loaded pinned digest", "The bounded pinned digest has no decision, blocker, or warning to show.");
  renderTimeline(byId("recent-activity"), newestEvents(data.events).slice(0, 5), "No activity observed", activityEmptyDetail(data));
}

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

const WORK_GROUPS: Array<{ title: string; statuses: string[] }> = [
  { title: "Open", statuses: ["open", "assigned"] },
  { title: "Working", statuses: ["claimed", "in_progress", "creating_report"] },
  { title: "Submitted", statuses: ["submitted", "reviewing", "in_review"] },
  { title: "Needs attention", statuses: ["rejected"] },
  { title: "Done", statuses: ["closed"] },
  { title: "Ended", statuses: ["canceled", "terminated"] },
];

function renderWork(data: Snapshot): void {
  byId("work-total").textContent = `${data.ticket_total || data.tickets.length} total`;
  const notice = byId("work-notice");
  notice.replaceChildren();
  if (data.ticket_truncated) {
    const item = element("div", "notice");
    item.dataset.tone = "warning";
    item.append(element("p", undefined, "This view is showing the first 500 authorized tickets. Counts still reflect the full board."));
    notice.append(item);
  }
  const known = new Set(WORK_GROUPS.flatMap((group) => group.statuses));
  const groups = [...WORK_GROUPS, { title: "Other", statuses: [...new Set(data.tickets.filter((ticket) => !known.has(ticket.status)).map((ticket) => ticket.status))] }];
  const rendered = groups.map((group) => {
    const tickets = data.tickets.filter((ticket) => group.statuses.includes(ticket.status));
    if (!tickets.length) return null;
    const section = element("section", "work-group");
    const heading = element("h3");
    heading.append(document.createTextNode(group.title), pill(String(tickets.length)));
    const items = element("div", "work-group-list");
    tickets.forEach((ticket) => {
      const card = element("article", "work-card");
      const copy = element("div");
      copy.append(element("p", "ticket-id", ticket.id), element("h3", undefined, ticket.title));
      if (ticket.description) copy.append(element("p", "muted", ticket.description));
      const meta = element("div", "meta-row");
      meta.append(pill(ticket.priority), pill(ticket.status, toneForStatus(ticket.status)));
      if (ticket.assigned_to) meta.append(pill(ticket.assigned_to));
      const lease = leaseBadge(ticket.lease_expires_at);
      if (lease) meta.append(lease);
      if (ticket.abandoned_count) meta.append(pill(`abandoned ×${ticket.abandoned_count}`, "attention"));
      copy.append(meta);
      card.append(copy);
      items.append(card);
    });
    section.append(heading, items);
    return section;
  }).filter((item): item is HTMLElement => item !== null);
  byId("work-groups").replaceChildren(...(rendered.length ? rendered : [emptyState("No tickets yet", "Tickets created through agent chat will appear here.")]));
}

function renderAgents(data: Snapshot): void {
  byId("agents-total").textContent = `${data.agents_live} live / ${data.agent_total} total`;
  const notice = byId("agents-notice");
  notice.replaceChildren();
  if (data.agent_truncated) {
    const item = element("div", "notice");
    item.dataset.tone = "warning";
    item.append(element("p", undefined, `This View is capped at ${data.agents.length} loaded agents; ${data.agent_total} are visible to the adapter.`));
    notice.append(item);
  }
  const orderedAgents = [...data.agents].sort((left, right) => Number(left.stale) - Number(right.stale));
  const cards = orderedAgents.map((agent) => {
    const card = element("article", "agent-card");
    card.dataset.stale = String(agent.stale);
    const heading = element("div", "agent-heading");
    const identity = element("div", "agent-heading");
    identity.append(element("span", "agent-avatar", agent.name.slice(0, 2).toUpperCase()), element("h3", undefined, agent.name));
    const states = element("div", "agent-state-badges");
    states.append(pill(agent.status, toneForStatus(agent.status)));
    if (agent.stale) states.append(pill("stale"));
    if (agent.duplicate_name === true) {
      const marker = pill("duplicate name", "warning");
      marker.title = "duplicate display name";
      states.append(marker);
    }
    heading.append(identity, states);
    card.append(heading);
    if (agent.focus) card.append(element("p", "muted", agent.focus));
    const meta = element("div", "meta-row");
    if (agent.role) meta.append(pill(agent.role));
    if (agent.platform) meta.append(pill(agent.platform));
    meta.append(agentField("project", agent.project ?? null));
    meta.append(agentField("ticket", shortTicketId(agent.current_ticket_id ?? agent.current_ticket?.id ?? null)));
    meta.append(pill(`${Math.round(agent.idle_minutes)}m idle`));
    card.append(meta);
    if (agent.duplicate) {
      const warn = element("div", "notice");
      warn.dataset.tone = "warning";
      const msg = agent.suggested_name
        ? `Duplicate name — join as “${agent.suggested_name}” instead.`
        : "Duplicate active name — pick a unique agent name.";
      warn.append(element("p", undefined, msg));
      card.append(warn);
    }
    if (agent.current_ticket) {
      card.append(element("p", "muted", `Working on ${agent.current_ticket.id}: ${agent.current_ticket.title}`));
    } else {
      const owned = data.tickets.filter((ticket) => ticket.claimed_agent_id === agent.id || ticket.assigned_agent_id === agent.id || ticket.assigned_to === agent.name).length;
      card.append(element("p", "muted", `${owned} linked work item${owned === 1 ? "" : "s"}`));
    }
    return card;
  });
  byId("agents-grid").replaceChildren(...(cards.length ? cards : [emptyState("No agents yet", "Agents appear after they join this local board.")]));
}

function fleetValue(value: number | null): string {
  return value === null ? "—" : String(value);
}

function tableHeading(label: string): HTMLTableCellElement {
  const heading = element("th", undefined, label);
  heading.scope = "col";
  return heading;
}

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

function renderFleet(data: FleetSnapshot | null): void {
  const unavailable = fleetUnavailable || data === null;
  const totals = unavailable ? null : data.totals;
  byId("fleet-agents").textContent = fleetValue(totals?.agents ?? null);
  byId("fleet-busy").textContent = fleetValue(totals?.busy ?? null);
  byId("fleet-available").textContent = fleetValue(totals?.available ?? null);
  byId("fleet-stale").textContent = fleetValue(totals?.stale ?? null);

  const warning = byId<HTMLElement>("fleet-warning");
  warning.replaceChildren();
  const warningText = unavailable
    ? "Fleet snapshot is unavailable on this server."
    : data.registry_warning;
  if (warningText) {
    const notice = element("div", "notice");
    notice.dataset.tone = "warning";
    notice.append(element("p", undefined, warningText));
    warning.append(notice);
  }

  const projects = unavailable ? [] : data.projects;
  const projectContainer = byId<HTMLElement>("fleet-projects");
  if (!projects.length) {
    projectContainer.replaceChildren(emptyState(
      unavailable ? "Fleet data unavailable" : "No registered projects",
      unavailable ? "This server may not expose the fleet_snapshot tool yet." : "Active and paused registry projects will appear here.",
    ));
  } else {
    const { wrapper, body } = fleetTable(["Name", "Board", "Status", "Open", "Claimed", "Submitted"]);
    projects.forEach((project) => {
      const row = element("tr");
      row.append(
        element("td", undefined, project.name ?? "—"),
        element("td", "fleet-mono", project.board_id ?? "—"),
        element("td", undefined, project.status ?? "—"),
        element("td", "fleet-number", fleetValue(project.tickets_open)),
        element("td", "fleet-number", fleetValue(project.tickets_claimed)),
        element("td", "fleet-number", fleetValue(project.tickets_submitted)),
      );
      body.append(row);
    });
    projectContainer.replaceChildren(wrapper);
  }

  const pool = unavailable ? [] : data.pool;
  const poolContainer = byId<HTMLElement>("fleet-pool");
  if (!pool.length) {
    poolContainer.replaceChildren(emptyState(
      unavailable ? "Agent pool unavailable" : "No pool entries",
      unavailable ? "Counts and seats remain as em dashes until the tool is available." : "Cross-project worker seats will appear here.",
    ));
  } else {
    const { wrapper, body } = fleetTable(["Agent", "Pool status", "Seats"]);
    pool.forEach((entry) => {
      const row = element("tr");
      row.dataset.stale = String(entry.pool_status === "stale");
      const identity = element("td");
      identity.append(
        element("strong", undefined, entry.agent_name ?? "—"),
        element("small", "fleet-principal", entry.principal_id ?? "—"),
      );
      const status = element("td");
      status.append(pill(entry.pool_status ?? "—", entry.pool_status === "busy" ? "working" : entry.pool_status === "available" ? "submitted" : undefined));
      const seats = element("td");
      const seatList = element("div", "fleet-seats");
      if (entry.seats.length) {
        entry.seats.forEach((seat) => {
          const project = seat.project ?? seat.board_id ?? "—";
          const ticket = shortTicketId(seat.current_ticket_id) ?? "—";
          const chip = pill(`${project}:${ticket}`, seat.live === false ? "warning" : undefined);
          if (seat.board_id) chip.title = `board ${seat.board_id}`;
          seatList.append(chip);
        });
      } else {
        const empty = pill("—");
        empty.dataset.empty = "true";
        seatList.append(empty);
      }
      seats.append(seatList);
      row.append(identity, status, seats);
      body.append(row);
    });
    poolContainer.replaceChildren(wrapper);
  }
}

function copyButton(label: string, value: string): HTMLButtonElement {
  const button = element("button", "copy-button", label) as HTMLButtonElement;
  button.type = "button";
  button.dataset.copyValue = value;
  button.setAttribute("aria-label", `Copy ${value}`);
  return button;
}

async function copyValue(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // Fall through to the selection-based copy path for restricted Hosts.
  }
  const textarea = element("textarea", "copy-fallback") as HTMLTextAreaElement;
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  document.body.append(textarea);
  textarea.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  textarea.remove();
  return copied;
}

function renderLinks(data: LinkSnapshot | null): void {
  const container = byId<HTMLElement>("links-groups");
  const notice = byId<HTMLElement>("links-notice");
  const total = byId<HTMLElement>("links-total");
  notice.replaceChildren();
  if (linksUnavailable || data === null) {
    total.textContent = "—";
    container.replaceChildren(emptyState("Links unavailable", "This server may not expose the bounded link_snapshot tool yet."));
    return;
  }

  const synthetic = data.source_tool.startsWith("synthetic-");
  const authorityLabel = synthetic ? "Synthetic fixture" : "Authoritative";
  total.textContent = `${data.returned_node_count} memories · ${data.returned_edge_count} links`;
  byId("links-source").textContent = `${authorityLabel} · ${data.source_tool}`;
  if (data.truncated) {
    const warning = element("div", "notice");
    warning.dataset.tone = "warning";
    warning.append(element("p", undefined, `Showing ${data.returned_node_count} of ${data.node_count} memories and ${data.returned_edge_count} of ${data.edge_count} links.`));
    notice.append(warning);
  }

  const ticketIdsByMemory = new Map<string, string[]>();
  data.edges.filter((edge) => edge.kind === "ticket").forEach((edge) => {
    const values = ticketIdsByMemory.get(edge.from) ?? [];
    if (!values.includes(edge.to)) values.push(edge.to);
    ticketIdsByMemory.set(edge.from, values);
  });
  const groups = new Map<string, LinkNode[]>();
  data.nodes.forEach((node) => {
    const tickets = ticketIdsByMemory.get(node.memory_id) ?? ["No ticket"];
    tickets.forEach((ticket) => {
      const values = groups.get(ticket) ?? [];
      values.push(node);
      groups.set(ticket, values);
    });
  });

  const groupCards = [...groups.entries()]
    .sort(([left], [right]) => left === "No ticket" ? 1 : right === "No ticket" ? -1 : left.localeCompare(right))
    .map(([ticketId, nodes]) => {
      const group = element("section", "link-group card");
      const heading = element("div", "link-group-heading");
      const headingCopy = element("div");
      headingCopy.append(element("p", "section-kicker", ticketId === "No ticket" ? "Unlinked to a ticket" : "Ticket"), element("h2", undefined, ticketId));
      heading.append(headingCopy);
      if (ticketId !== "No ticket") heading.append(copyButton("Copy ID", ticketId));
      group.append(heading);

      const memoryList = element("div", "link-memory-list");
      nodes.forEach((node) => {
        const row = element("article", "link-memory");
        const rowHeading = element("div", "link-memory-heading");
        const title = element("div");
        title.append(element("h3", undefined, node.title), element("p", "link-id", node.memory_id));
        rowHeading.append(title, copyButton("Copy memory ID", node.memory_id));
        row.append(rowHeading);
        const meta = element("div", "meta-row");
        meta.append(pill(authorityLabel, synthetic ? "warning" : "submitted"), pill(node.memory_type));
        if (node.pinned) meta.append(pill("Pinned", "warning"));
        if (node.created_at) meta.append(pill(formatTime(node.created_at)));
        row.append(meta);

        const files = data.edges.filter((edge) => edge.from === node.memory_id && edge.kind === "file");
        const tags = data.edges.filter((edge) => edge.from === node.memory_id && edge.kind === "tag");
        if (files.length) {
          const fileList = element("div", "link-values");
          fileList.append(element("strong", undefined, "Files"));
          files.forEach((edge) => {
            const item = element("div", "link-value");
            item.append(element("code", undefined, edge.to), copyButton("Copy path", edge.to));
            if (edge.authority === "suggested") item.append(pill("Suggested", "warning"));
            fileList.append(item);
          });
          row.append(fileList);
        }
        if (tags.length) {
          const tagList = element("div", "meta-row");
          tagList.append(element("strong", undefined, "Tags"));
          tags.forEach((edge) => tagList.append(pill(`${edge.authority === "suggested" ? "Suggested · " : ""}${edge.to}`, edge.authority === "suggested" ? "warning" : undefined)));
          row.append(tagList);
        }
        memoryList.append(row);
      });
      group.append(memoryList);
      return group;
    });
  container.replaceChildren(...(groupCards.length ? groupCards : [emptyState("No explicit links", "Ticket, file, and tag relationships appear when project memories declare them.")]));
}

function newestEvents(events: BoardEvent[]): BoardEvent[] {
  return [...events].sort((left, right) => (right.seq ?? -1) - (left.seq ?? -1));
}

function renderTimeline(container: HTMLElement, events: BoardEvent[], emptyTitle: string, emptyDetail: string): void {
  const rows = events.map((event) => {
    const row = element("article", "timeline-item");
    row.append(element("span", "timeline-seq", event.seq === null ? "—" : `#${event.seq}`));
    const copy = element("div");
    copy.append(element("h3", undefined, event.kind.replaceAll("_", " ")), element("p", undefined, event.text));
    const meta = element("div", "meta-row");
    if (event.actor_id) meta.append(pill(event.actor_id));
    if (event.occurred_at) meta.append(element("span", "timeline-time", formatTime(event.occurred_at)));
    copy.append(meta);
    row.append(copy);
    return row;
  });
  container.replaceChildren(...(rows.length ? rows : [emptyState(emptyTitle, emptyDetail)]));
}

function renderActivity(data: Snapshot): void {
  byId("activity-total").textContent = `${data.events.length} retained`;
  const notices: HTMLElement[] = [];
  if (data.resync_notice) {
    const item = element("div", "notice");
    item.dataset.tone = "warning";
    item.append(element("p", undefined, data.resync_notice));
    notices.push(item);
  }
  if (data.dropped_events > 0) {
    const item = element("div", "notice");
    item.append(element("p", undefined, `${data.dropped_events} older event${data.dropped_events === 1 ? " was" : "s were"} dropped from this bounded View.`));
    notices.push(item);
  }
  if (data.has_more) {
    const item = element("div", "notice");
    item.append(element("p", undefined, "More activity is available and will arrive on the next bounded poll."));
    notices.push(item);
  }
  byId("activity-notice").replaceChildren(...notices);
  renderTimeline(byId("activity-list"), newestEvents(data.events), "No activity observed", activityEmptyDetail(data));
}

function sourceFor(data: Snapshot): string {
  if (data.data_mode === "demo-error") return "Synthetic error simulation · no project data";
  if (data.data_mode === "demo") return "Synthetic demo · no project data";
  if (data.data_mode === "stale" || data.stale) return "Last-known local state · reconnecting";
  return "Live local Central · authorized projection";
}

function renderActivePanel(data: Snapshot): void {
  if (activeView === "today") renderToday(data);
  else if (activeView === "work") renderWork(data);
  else if (activeView === "agents") renderAgents(data);
  else if (activeView === "fleet") renderFleet(fleetSnapshot);
  else if (activeView === "links") renderLinks(linkSnapshot);
  else renderActivity(data);
}

function semanticRenderSignature(data: Snapshot): string {
  return JSON.stringify({ ...data, snapshot_at: null });
}

function render(data: Snapshot): void {
  const signature = semanticRenderSignature(data);
  snapshot = data;
  if (signature === lastRenderSignature) {
    updateLeaseCountdowns();
    return;
  }
  lastRenderSignature = signature;
  document.body.dataset.scenario = data.data_mode;
  document.body.dataset.renderState = data.data_mode === "demo-error" ? "demo-error" : data.feed_error && data.data_mode !== "demo" ? "error" : data.data_mode;
  main.setAttribute("aria-busy", "false");
  byId("name").textContent = data.board.name;
  byId("board-id").textContent = data.board.id;
  byId("source").textContent = sourceFor(data);
  renderConnection(data);
  renderActivityScope(data);
  renderActivePanel(data);
  renderSearch(searchInput.value);
  updateLeaseCountdowns();
}

function searchCorpus(data: Snapshot): SearchHit[] {
  const hits: SearchHit[] = [];
  data.tickets.forEach((ticket) => hits.push({ view: "work", kind: "Ticket", title: `${ticket.id} · ${ticket.title}`, detail: `${ticket.status} ${ticket.priority} ${ticket.description} ${ticket.assigned_to ?? ""} ${ticket.assigned_agent_id ?? ""}` }));
  data.agents.forEach((agent) => hits.push({ view: "agents", kind: "Agent", title: agent.name, detail: `${agent.status} ${agent.role ?? ""} ${agent.focus ?? ""} ${agent.platform ?? ""}` }));
  fleetSnapshot?.projects.forEach((project) => hits.push({ view: "fleet", kind: "Project", title: project.name ?? "—", detail: `${project.board_id ?? ""} ${project.status ?? ""}` }));
  fleetSnapshot?.pool.forEach((entry) => hits.push({ view: "fleet", kind: "Pool", title: entry.agent_name ?? "—", detail: `${entry.pool_status ?? ""} ${entry.principal_id ?? ""} ${entry.seats.map((seat) => `${seat.project ?? seat.board_id ?? ""} ${seat.current_ticket_id ?? ""}`).join(" ")}` }));
  linkSnapshot?.nodes.forEach((node) => {
    const linked = linkSnapshot?.edges.filter((edge) => edge.from === node.memory_id).map((edge) => edge.to).join(" ") ?? "";
    hits.push({ view: "links", kind: "Link", title: `${node.memory_id} · ${node.title}`, detail: `${node.memory_type} ${linked}` });
  });
  data.events.forEach((event) => hits.push({ view: "activity", kind: "Activity", title: event.text, detail: `${event.kind} ${event.actor_id ?? ""} ${event.ticket_id ?? ""} ${event.memory_id ?? ""} ${event.status_from ?? ""} ${event.status_to ?? ""}` }));
  const highlights = [data.highlights.latest_handoff, data.highlights.important_pinned].filter((item): item is Highlight => item !== null);
  highlights.forEach((item) => hits.push({ view: "today", kind: item.type, title: item.title, detail: `${item.summary} ${item.author ?? ""} ${item.next_steps.join(" ")} ${item.warnings.join(" ")}` }));
  return hits;
}

function renderSearch(rawQuery: string): void {
  const panel = byId<HTMLElement>("search-results");
  const container = byId<HTMLElement>("search-results-list");
  const counter = byId<HTMLElement>("search-count");
  const query = rawQuery.trim().toLocaleLowerCase();
  if (!query) {
    panel.hidden = true;
    counter.textContent = "";
    container.replaceChildren();
    return;
  }
  const focusedKey = document.activeElement instanceof HTMLButtonElement && document.activeElement.classList.contains("search-result")
    ? document.activeElement.dataset.resultKey ?? null
    : null;
  const matches = searchCorpus(snapshot).filter((item) => `${item.kind} ${item.title} ${item.detail}`.toLocaleLowerCase().includes(query));
  const buckets = new Map<string, SearchHit[]>();
  matches.forEach((item) => {
    const bucket = buckets.get(item.kind);
    if (bucket) bucket.push(item);
    else buckets.set(item.kind, [item]);
  });
  const hits: SearchHit[] = [];
  while (hits.length < 40 && [...buckets.values()].some((items) => items.length)) {
    buckets.forEach((items) => {
      if (hits.length < 40 && items.length) hits.push(items.shift()!);
    });
  }
  panel.hidden = false;
  counter.textContent = matches.length > hits.length
    ? `Showing ${hits.length} of ${matches.length} results`
    : `${matches.length} result${matches.length === 1 ? "" : "s"}`;
  const rows = hits.map((hit) => {
    const button = element("button", "search-result") as HTMLButtonElement;
    button.type = "button";
    button.dataset.targetView = hit.view;
    button.dataset.resultKey = `${hit.view}:${hit.kind}:${hit.title}:${hit.detail}`.slice(0, 1_024);
    button.append(element("small", undefined, hit.kind), element("strong", undefined, hit.title), element("span", undefined, hit.detail));
    return button;
  });
  container.replaceChildren(...(rows.length ? rows : [emptyState("No matching loaded data", "Try a ticket ID, agent name, status, or activity phrase.")]));
  if (focusedKey) {
    [...container.querySelectorAll<HTMLButtonElement>(".search-result")]
      .find((item) => item.dataset.resultKey === focusedKey)
      ?.focus();
  }
}

function selectView(view: ViewName, focusTab = false): void {
  activeView = view;
  document.querySelectorAll<HTMLButtonElement>("[role=tab][data-view]").forEach((tab) => {
    const selected = tab.dataset.view === view;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected) {
      tab.scrollIntoView({ block: "nearest", inline: "nearest" });
      if (focusTab) tab.focus();
    }
  });
  document.querySelectorAll<HTMLElement>("[data-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== view;
  });
  renderActivePanel(snapshot);
  if (view === "fleet" && connected && fleetUnavailable && !fleetBusy) void refreshFleet();
  if (view === "links" && connected && linksUnavailable && !linksBusy) void refreshLinks();
}

function setLoading(loading: boolean, message = "Loading authorized board state"): void {
  main.setAttribute("aria-busy", String(loading));
  refreshButton.disabled = loading;
  if (loading) {
    document.body.dataset.renderState = "loading";
    byId("connection-title").textContent = message;
    byId("connection-detail").textContent = "The local bridge is preparing a read-only snapshot.";
  }
}

function failureSnapshot(message: string): Snapshot {
  return {
    ...snapshot,
    data_mode: snapshot.activity_scope === "synthetic-demo" ? "demo-error" : "stale",
    stale: true,
    connected: false,
    feed_error: message,
  };
}

async function refreshSnapshot(): Promise<void> {
  if (!connected) return;
  setLoading(true);
  try {
    const result = await app.callServerTool({ name: "board_snapshot", arguments: {} });
    const value = decodeSnapshot(structured(result));
    if (value) render(value);
    else render(failureSnapshot("Invalid snapshot response"));
  } catch {
    render(failureSnapshot("Local bridge request failed"));
  } finally {
    setLoading(false);
  }
}

function acceptFleetSnapshot(value: FleetSnapshot): void {
  fleetSnapshot = value;
  fleetUnavailable = false;
  if (activeView === "fleet") renderFleet(fleetSnapshot);
  renderSearch(searchInput.value);
}

async function refreshFleet(): Promise<void> {
  if (!connected || fleetBusy) return;
  fleetBusy = true;
  try {
    const result = await app.callServerTool({ name: "fleet_snapshot", arguments: {} });
    const value = decodeFleetSnapshot(structured(result));
    if (value) acceptFleetSnapshot(value);
    else {
      fleetSnapshot = null;
      fleetUnavailable = true;
    }
  } catch {
    fleetSnapshot = null;
    fleetUnavailable = true;
  } finally {
    fleetBusy = false;
    if (activeView === "fleet") renderFleet(fleetSnapshot);
    renderSearch(searchInput.value);
  }
}

function acceptLinkSnapshot(value: LinkSnapshot): void {
  linkSnapshot = value;
  linksUnavailable = false;
  if (activeView === "links") renderLinks(linkSnapshot);
  renderSearch(searchInput.value);
}

async function refreshLinks(): Promise<void> {
  if (!connected || linksBusy) return;
  linksBusy = true;
  try {
    const result = await app.callServerTool({ name: "link_snapshot", arguments: {} });
    const value = decodeLinkSnapshot(structured(result));
    if (value) acceptLinkSnapshot(value);
    else {
      linkSnapshot = null;
      linksUnavailable = true;
    }
  } catch {
    linkSnapshot = null;
    linksUnavailable = true;
  } finally {
    linksBusy = false;
    if (activeView === "links") renderLinks(linkSnapshot);
    renderSearch(searchInput.value);
  }
}

function clearFeedTimer(): void {
  if (feedTimer !== undefined) window.clearTimeout(feedTimer);
  feedTimer = undefined;
}

function scheduleFeed(delayMs = feedDelayMs): void {
  clearFeedTimer();
  if (!connected || document.visibilityState === "hidden") return;
  feedTimer = window.setTimeout(() => {
    feedTimer = undefined;
    void refreshFeed();
  }, delayMs);
}

async function refreshFeed(): Promise<void> {
  if (!connected || feedBusy) return;
  feedBusy = true;
  let failed = false;
  try {
    const result = await app.callServerTool({ name: "board_event_feed", arguments: {} });
    const value = decodeSnapshot(structured(result));
    if (value) {
      failed = Boolean(value.feed_error || value.stale);
      render(value);
    } else {
      failed = true;
      render(failureSnapshot("Invalid feed response"));
    }
  } catch {
    failed = true;
    render(failureSnapshot("Local feed unavailable"));
  } finally {
    feedBusy = false;
    feedDelayMs = failed ? Math.min(MAX_FEED_DELAY_MS, Math.max(BASE_FEED_DELAY_MS, feedDelayMs * 2)) : BASE_FEED_DELAY_MS;
    scheduleFeed(feedDelayMs);
  }
}

function applyHostContext(context: McpUiHostContext): void {
  if (context.theme) applyDocumentTheme(context.theme);
  if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
  if (context.styles?.css?.fonts) applyHostFonts(context.styles.css.fonts);
  if (context.safeAreaInsets) {
    const root = document.documentElement.style;
    root.setProperty("--safe-top", `${Math.max(0, context.safeAreaInsets.top)}px`);
    root.setProperty("--safe-right", `${Math.max(0, context.safeAreaInsets.right)}px`);
    root.setProperty("--safe-bottom", `${Math.max(0, context.safeAreaInsets.bottom)}px`);
    root.setProperty("--safe-left", `${Math.max(0, context.safeAreaInsets.left)}px`);
  }
}

document.querySelectorAll<HTMLButtonElement>("[role=tab][data-view]").forEach((tab) => {
  tab.addEventListener("click", () => selectView(tab.dataset.view as ViewName));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const order: ViewName[] = ["today", "work", "agents", "fleet", "links", "activity"];
    const current = order.indexOf(activeView);
    const next = event.key === "Home" ? 0 : event.key === "End" ? order.length - 1 : (current + (event.key === "ArrowRight" ? 1 : -1) + order.length) % order.length;
    selectView(order[next], true);
  });
});

document.querySelectorAll<HTMLButtonElement>("[data-go-view]").forEach((button) => button.addEventListener("click", () => selectView(button.dataset.goView as ViewName, true)));
byId("search-results-list").addEventListener("click", (event) => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-target-view]");
  if (!button) return;
  searchInput.value = "";
  renderSearch("");
  selectView(button.dataset.targetView as ViewName, true);
});
searchInput.addEventListener("input", () => renderSearch(searchInput.value));
refreshButton.addEventListener("click", () => void Promise.all([refreshSnapshot(), refreshFleet(), refreshLinks()]));
byId("links-groups").addEventListener("click", (event) => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-copy-value]");
  if (!button) return;
  const original = button.textContent ?? "Copy";
  void copyValue(button.dataset.copyValue ?? "").then((copied) => {
    button.textContent = copied ? "Copied" : "Copy unavailable";
    window.setTimeout(() => { button.textContent = original; }, 1_500);
  });
});
document.addEventListener("keydown", (event) => {
  const target = event.target as HTMLElement;
  const typing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target.isContentEditable;
  if (event.key === "/" && !typing) {
    event.preventDefault();
    searchInput.focus();
  } else if (event.key === "Escape" && document.activeElement === searchInput) {
    searchInput.value = "";
    renderSearch("");
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") clearFeedTimer();
  else scheduleFeed(0);
});

const leaseTimer = window.setInterval(updateLeaseCountdowns, 1_000);
render(fallback);

// Register every Host handler before connect, as required by the SDK lifecycle.
app.addEventListener("toolinput", () => setLoading(true, "Receiving board state"));
app.addEventListener("toolresult", (result) => {
  const payload = structured(result);
  const value = decodeSnapshot(payload);
  if (value) {
    render(value);
    return;
  }
  const fleet = looksLikeFleetSnapshot(payload) ? decodeFleetSnapshot(payload) : null;
  if (fleet) {
    acceptFleetSnapshot(fleet);
    return;
  }
  const links = looksLikeLinkSnapshot(payload) ? decodeLinkSnapshot(payload) : null;
  if (links) acceptLinkSnapshot(links);
  else if (connected) void refreshSnapshot();
});
app.addEventListener("hostcontextchanged", applyHostContext);
app.onteardown = async () => {
  connected = false;
  clearFeedTimer();
  window.clearInterval(leaseTimer);
  return {};
};
app.onerror = () => render(failureSnapshot("Host bridge error"));

void app
  .connect(new PostMessageTransport(window.parent, window.parent))
  .then(() => {
    connected = true;
    const context = app.getHostContext();
    if (context) applyHostContext(context);
    void Promise.all([refreshFleet(), refreshLinks()]);
    scheduleFeed(0);
  })
  .catch(() => {
    connected = false;
    render(fallback);
  });
