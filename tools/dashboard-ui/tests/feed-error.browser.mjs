const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};

const dashboardUrl = "__PURSERS_DASHBOARD_TEST_URL__";
assert(
  dashboardUrl && dashboardUrl !== "__PURSERS_DASHBOARD_TEST_URL__",
  "PURSERS_DASHBOARD_TEST_URL is required",
);

const task = await taskSpace("TK-e1cea022b42f Personal feed_error browser acceptance");
const page = task.page("p1");
await page.goto(dashboardUrl);
await page.waitForFunction(() => {
  const frame = document.querySelector("#app-frame");
  return window.__pursersHostReady === true
    && frame?.contentDocument?.querySelector("[data-pursers-surface=personal]");
}, undefined, { timeout: 15_000 });

const baseSnapshot = {
  contract_version: 2,
  data_mode: "stale",
  activity_scope: "local-model-tools",
  fixture_provenance: "browser acceptance product payload",
  board: { id: "browser-acceptance", name: "Browser Acceptance" },
  agents: [],
  tickets: [],
  highlights: { latest_handoff: null, important_pinned: null },
  status: {
    ticket_status_counts: {},
    memory_type_counts: {},
    visible_memory_count: 0,
    scrub_profile: "internal",
  },
  events: [],
  ticket_total: 0,
  ticket_truncated: false,
  agent_total: 0,
  agents_live: 0,
  agent_truncated: false,
  event_cursor: 1,
  dropped_events: 0,
  has_more: false,
  connected: false,
  stale: true,
  feed_error: null,
  resync_notice: null,
  snapshot_at: null,
};

const sendSnapshot = async (feedError, rawError, cursor) => {
  const snapshot = { ...baseSnapshot, feed_error: feedError, raw_error: rawError, event_cursor: cursor };
  await page.evaluate((value) => window.__pursersSendSnapshot(value), snapshot);
};

const frameDocumentValue = (selector, property) => page.evaluate(
  ({ selector: target, property: key }) => {
    const doc = document.querySelector("#app-frame")?.contentDocument;
    const node = doc?.querySelector(target);
    return {
      exists: Boolean(node),
      value: node ? node[key] : null,
      text: node?.textContent ?? null,
      bodyText: doc?.body?.innerText ?? "",
      active: doc?.activeElement === node,
      state: node?.getAttribute("data-pursers-state") ?? null,
      connection: node?.getAttribute("data-pursers-connection") ?? null,
    };
  },
  { selector, property },
);

const appFrameId = async () => {
  const tree = await page.cdp("Page.getFrameTree");
  const pending = [tree.frameTree];
  while (pending.length) {
    const item = pending.shift();
    if (item?.frame?.url?.endsWith("/dashboard.html")) return item.frame.id;
    pending.push(...(item?.childFrames ?? []));
  }
  throw new Error("built Personal dashboard frame not found");
};

const assertAccessibility = async (frameId, expected, forbidden, stateName) => {
  const tree = await page.cdp("Accessibility.getFullAXTree", { frameId });
  const names = (tree.nodes ?? [])
    .filter((node) => node.ignored !== true)
    .map((node) => node.name?.value)
    .filter((value) => typeof value === "string");
  assert(names.includes(expected), `${stateName}: exact sanitized feed_error missing from AX tree`);
  assert(!JSON.stringify(tree).includes(forbidden), `${stateName}: unsanitized detail leaked into AX tree`);
  return names.length;
};

const frameId = await appFrameId();
const permissionSanitized = "Permission denied";
const permissionRaw = "Permission denied: internal transport detail";
await sendSnapshot(permissionSanitized, permissionRaw, 2);
await page.waitForFunction((expected) => {
  const doc = document.querySelector("#app-frame")?.contentDocument;
  return doc?.querySelector("#connection-detail")?.textContent === expected
    && doc?.querySelector("[data-pursers-panel=connection]")?.getAttribute("data-pursers-state") === "error";
}, permissionSanitized, { timeout: 10_000 });
const connection = await frameDocumentValue(
  "[data-pursers-panel=connection][data-pursers-state=error][data-pursers-connection=error]",
  "textContent",
);
assert(connection.exists, "permission-denied: connection contract selector missing");
assert(connection.text.includes(permissionSanitized), "permission-denied: sanitized feed_error missing from DOM");
assert(!connection.bodyText.includes(permissionRaw), "permission-denied: unsanitized detail leaked into DOM");
await page.focus("[data-pursers-panel=connection][data-pursers-state=error]");
const focusedConnection = await frameDocumentValue(
  "[data-pursers-panel=connection][data-pursers-state=error]",
  "textContent",
);
assert(focusedConnection.active, "permission-denied: connection error is not keyboard-focusable");
const connectionAxNodes = await assertAccessibility(
  frameId, permissionSanitized, permissionRaw, "permission-denied",
);

await page.click("#tab-activity", { label: "open activity error state" });
const activitySanitized = "Local feed unavailable";
const activityRaw = "Local feed unavailable: internal stack detail";
await sendSnapshot(activitySanitized, activityRaw, 3);
await page.waitForFunction((expected) => {
  const doc = document.querySelector("#app-frame")?.contentDocument;
  const panel = doc?.querySelector("[data-pursers-panel=activity-feed]");
  const alert = panel?.querySelector("[role=alert][data-pursers-state=error]");
  return panel?.getAttribute("data-pursers-state") === "error"
    && alert?.textContent === expected;
}, activitySanitized, { timeout: 10_000 });
const activity = await frameDocumentValue(
  "[data-pursers-panel=activity-feed][data-pursers-source=board-event-feed][data-pursers-state=error] [role=alert][data-pursers-state=error]",
  "textContent",
);
assert(activity.exists, "activity-error: activity contract selector missing");
assert(activity.text === activitySanitized, "activity-error: sanitized feed_error changed in DOM");
assert(!activity.bodyText.includes(activityRaw), "activity-error: unsanitized detail leaked into DOM");
await page.focus("[data-pursers-panel=activity-feed] [role=alert][data-pursers-state=error]");
const focusedActivity = await frameDocumentValue(
  "[data-pursers-panel=activity-feed] [role=alert][data-pursers-state=error]",
  "textContent",
);
assert(focusedActivity.active, "activity-error: activity alert is not keyboard-focusable");
const activityAxNodes = await assertAccessibility(
  frameId, activitySanitized, activityRaw, "activity-error",
);

const receipt = {
  ok: true,
  task_space_id: task.spaceId,
  product_url: dashboardUrl.replace(/:\/\/[^/]+/, "://HOST"),
  states: {
    "dashboard-ui.state.permission-denied": {
      dom_text: permissionSanitized,
      ax_exact: true,
      keyboard_focusable: true,
      raw_absent: true,
      ax_nodes: connectionAxNodes,
    },
    "personal.activity-error": {
      dom_text: activitySanitized,
      ax_exact: true,
      keyboard_focusable: true,
      raw_absent: true,
      ax_nodes: activityAxNodes,
    },
  },
};
await task.finish({ keep: [] });
console.log(JSON.stringify(receipt));
