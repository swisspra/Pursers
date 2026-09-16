import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";


const SOURCE = readFileSync(new URL("../src/dashboard.ts", import.meta.url), "utf8");
const ENTRY = readFileSync(new URL("../dashboard-entry.html", import.meta.url), "utf8");


class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.style = {};
    this.className = "";
    this.hidden = false;
    this.title = "";
    this.type = "";
    this._text = "";
    this.classList = {
      add: (...names) => {
        const values = new Set(this.className.split(/\s+/).filter(Boolean));
        names.forEach((name) => values.add(name));
        this.className = [...values].join(" ");
      },
      contains: (name) => this.className.split(/\s+/).includes(name),
    };
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren(...children) {
    this._text = "";
    this.children = [...children];
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  set textContent(value) {
    this._text = String(value ?? "");
    this.children = [];
  }

  get textContent() {
    return this._text + this.children.map((child) => (
      child instanceof FakeElement ? child.textContent : String(child)
    )).join("");
  }
}


function functionSource(name) {
  const match = new RegExp(`function\\s+${name}(?:<[^>{}]+>)?\\s*\\(`).exec(SOURCE);
  assert.ok(match, `function ${name} not found`);
  const start = match.index;
  const brace = SOURCE.indexOf("{", start);
  let depth = 0;
  let quote = null;
  let lineComment = false;
  let blockComment = false;
  for (let index = brace; index < SOURCE.length; index += 1) {
    const char = SOURCE[index];
    const next = SOURCE[index + 1];
    if (lineComment) {
      if (char === "\n") lineComment = false;
      continue;
    }
    if (blockComment) {
      if (char === "*" && next === "/") {
        blockComment = false;
        index += 1;
      }
      continue;
    }
    if (quote) {
      if (char === "\\") index += 1;
      else if (char === quote) quote = null;
      continue;
    }
    if (char === "/" && next === "/") {
      lineComment = true;
      index += 1;
    } else if (char === "/" && next === "*") {
      blockComment = true;
      index += 1;
    } else if (["'", "\"", "`"].includes(char)) {
      quote = char;
    } else if (char === "{") {
      depth += 1;
    } else if (char === "}") {
      depth -= 1;
      if (depth === 0) return SOURCE.slice(start, index + 1);
    }
  }
  assert.fail(`function ${name} is unterminated`);
}


function transpile(typeScript) {
  const directory = mkdtempSync(join(tmpdir(), "pursers-render-test-"));
  const input = join(directory, "subject.ts");
  const output = join(directory, "subject.js");
  writeFileSync(input, typeScript, "utf8");
  try {
    execFileSync(fileURLToPath(new URL("../node_modules/.bin/tsc", import.meta.url)), [
      input, "--ignoreConfig", "--noCheck", "--target", "es2022",
      "--module", "commonjs", "--outDir", directory,
    ], { stdio: "pipe" });
    return readFileSync(output, "utf8");
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}


function loadRenderers() {
  const names = [
    "element", "emptyState", "pill", "shortTicketId", "agentField",
    "toneForStatus", "leaseText", "leaseBadge", "renderAgents",
    "fleetValue", "tableHeading", "fleetTable", "renderFleet",
    "formatTime", "copyButton", "renderLinks",
  ];
  const elements = new Map();
  const document = {
    createElement: (tag) => new FakeElement(tag),
    getElementById: (id) => {
      if (!elements.has(id)) elements.set(id, new FakeElement("div"));
      return elements.get(id);
    },
  };
  const typeScript = `${names.map(functionSource).join("\n")}\n`
    + "globalThis.renderers = {renderAgents, renderFleet, renderLinks};";
  const javaScript = transpile(typeScript);
  const sandbox = {
    document,
    byId: (id) => document.getElementById(id),
    fleetTable: (headers) => {
      const wrapper = document.createElement("div");
      const table = document.createElement("table");
      const head = document.createElement("thead");
      const headingRow = document.createElement("tr");
      headers.forEach((label) => {
        const cell = document.createElement("th");
        cell.textContent = label;
        headingRow.append(cell);
      });
      const body = document.createElement("tbody");
      head.append(headingRow);
      table.append(head, body);
      wrapper.append(table);
      return { wrapper, body };
    },
    fleetUnavailable: false,
    linksUnavailable: false,
    projectFilter: "all",
    snapshot: null,
  };
  vm.runInNewContext(
    javaScript,
    sandbox,
  );
  return { ...sandbox.renderers, elements };
}


function descendants(root) {
  const result = [root];
  root.children.forEach((child) => {
    if (child instanceof FakeElement) result.push(...descendants(child));
  });
  return result;
}


test("agent cards execute lease rendering for future, overdue, and absent values", () => {
  const { renderAgents, elements } = loadRenderers();
  const future = new Date(Date.now() + 120_000).toISOString();
  const overdue = new Date(Date.now() - 60_000).toISOString();
  renderAgents({
    agents_live: 3,
    agent_total: 3,
    agent_truncated: false,
    tickets: [],
    agents: [
      { id: "AI-1", name: "future", status: "idle", idle_minutes: 5, stale: false, lease_expires_at: future },
      { id: "AI-2", name: "overdue", status: "idle", idle_minutes: 8, stale: false, lease_expires_at: overdue },
      { id: "AI-3", name: "absent", status: "idle", idle_minutes: 11, stale: false, lease_expires_at: null },
    ],
  });

  const cards = elements.get("agents-grid").children;
  assert.equal(cards.length, 3);
  assert.equal(cards[0].getAttribute("data-pursers-agent"), "AI-1");
  assert.equal(cards[0].getAttribute("data-pursers-status"), "idle");
  assert.equal(elements.get("agents-grid").getAttribute("data-pursers-state"), "ready");
  assert.match(cards[0].textContent, /5m idlelease [12]m/);
  assert.match(cards[1].textContent, /8m idlelease overdue/);
  assert.equal(cards[2].textContent.includes("lease"), false);
  const leaseNodes = cards.flatMap(descendants).filter((node) => node.classList.contains("lease-countdown"));
  assert.equal(leaseNodes.length, 2);
  assert.equal(leaseNodes[0].dataset.leaseExpiresAt, future);
});


test("fleet render shows only nonzero bounded truncation warnings", () => {
  const { renderFleet, elements } = loadRenderers();
  const snapshot = {
    registry_warning: null,
    projects: [],
    pool: [],
    totals: { agents: 1, busy: 0, available: 1, stale: 0 },
    truncation_counts: { projects: 2, boards: 0, agents: 3, tickets: 0, pool: 0 },
  };
  renderFleet(snapshot);
  const warning = elements.get("fleet-warning");
  assert.equal(warning.textContent, "Fleet snapshot is partial: 2 projects omitted, 3 agent records omitted.");
  assert.equal(warning.children[0].getAttribute("aria-label"), "Fleet data truncation");

  renderFleet({
    ...snapshot,
    truncation_counts: { projects: 0, boards: 0, agents: 0, tickets: 0, pool: 0 },
  });
  assert.equal(warning.textContent, "");
  assert.equal(warning.children.length, 0);
});


test("selector contract is present on rendered board, agent, seat, and panel state", () => {
  const { renderFleet, elements } = loadRenderers();
  renderFleet({
    registry_warning: null,
    projects: [{
      name: "Pursers", board_id: "sandbox-board", status: "active",
      tickets_open: 1, tickets_claimed: 2, tickets_submitted: 3,
    }],
    pool: [{
      agent_name: "worker-1", principal_id: "PR-1", pool_status: "busy",
      seats: [{ board_id: "sandbox-board", project: "Pursers", current_ticket_id: "TK-1", live: true }],
    }],
    totals: { agents: 1, busy: 1, available: 0, stale: 0 },
    truncation_counts: { projects: 0, boards: 0, agents: 0, tickets: 0, pool: 0 },
  });

  const boardRow = descendants(elements.get("fleet-projects")).find((node) => node.tagName === "TR" && node.getAttribute("data-pursers-board"));
  assert.equal(boardRow.getAttribute("data-pursers-board"), "sandbox-board");
  assert.equal(boardRow.getAttribute("data-pursers-status"), "active");
  assert.equal(elements.get("fleet-projects").getAttribute("data-pursers-state"), "ready");

  const agentRow = descendants(elements.get("fleet-pool")).find((node) => node.tagName === "TR" && node.getAttribute("data-pursers-agent"));
  assert.equal(agentRow.getAttribute("data-pursers-agent"), "PR-1");
  assert.equal(agentRow.getAttribute("data-pursers-status"), "busy");
  const seat = descendants(agentRow).find((node) => node.getAttribute("data-pursers-seat"));
  assert.equal(seat.getAttribute("data-pursers-seat"), "sandbox-board");
  assert.equal(seat.getAttribute("data-pursers-status"), "live");
});


test("selector contract boots with explicit loading and empty selection values", () => {
  for (const literal of [
    'data-pursers-surface="personal"',
    'data-pursers-state="loading"',
    'data-pursers-selected-board=""',
    'data-pursers-selected-ticket=""',
    'data-pursers-panel="connection"',
    'data-pursers-connection="loading"',
    'data-pursers-panel="boards"',
    'data-pursers-panel="tickets"',
    'data-pursers-panel="agents"',
    'data-pursers-panel="seats"',
  ]) assert.ok(ENTRY.includes(literal), `missing ${literal}`);
});


test("links render ticket, file, tag, and retracts as distinct controls", () => {
  const { renderLinks, elements } = loadRenderers();
  const base = {
    source_tool: "memory_links",
    relationship_authority: "authoritative",
    nodes: [{ memory_id: "MEM-1", title: "Evidence", memory_type: "context", pinned: false, created_at: null }],
    edges: [
      { kind: "ticket", from: "MEM-1", to: "TK-1", authority: "authoritative" },
      { kind: "file", from: "MEM-1", to: "src/app.py", authority: "authoritative" },
      { kind: "tag", from: "MEM-1", to: "acceptance", authority: "authoritative" },
      { kind: "retracts", from: "MEM-1", to: "MEM-OLD", authority: "authoritative" },
    ],
    node_count: 1,
    edge_count: 4,
    returned_node_count: 1,
    returned_edge_count: 4,
    truncated: false,
  };
  renderLinks(base);
  const groups = elements.get("links-groups");
  assert.match(groups.textContent, /TK-1/);
  assert.match(groups.textContent, /Filessrc\/app.py/);
  assert.match(groups.textContent, /Tagsacceptance/);
  assert.match(groups.textContent, /RetractsMEM-OLD/);
  const buttons = descendants(groups).filter((node) => node.tagName === "BUTTON");
  assert.deepEqual(
    buttons.map((button) => [button.textContent, button.dataset.copyValue]),
    [["Copy ID", "TK-1"], ["Copy memory ID", "MEM-1"], ["Copy path", "src/app.py"], ["Copy retracted memory ID", "MEM-OLD"]],
  );

  renderLinks({ ...base, edges: base.edges.filter((edge) => edge.kind !== "retracts") });
  assert.equal(elements.get("links-groups").textContent.includes("Retracts"), false);
  assert.equal(
    descendants(elements.get("links-groups")).some((node) => node.dataset.copyValue === "MEM-OLD"),
    false,
  );
});
