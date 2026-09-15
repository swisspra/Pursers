// One-TaskSpace worker dry-run. This is explicitly not verifier-owned evidence.
const fs = await import("node:fs/promises");

const origin = process.env.BETA_FIXTURE_ORIGIN;
const recipePath = process.env.BETA_RECIPE_PATH;
if (!origin || !recipePath) throw new Error("BETA_FIXTURE_ORIGIN and BETA_RECIPE_PATH are required");

const selected = new Set([14, 18, 20, 40, 70, 95, 97, 133]);
const plan = JSON.parse(await fs.readFile(recipePath, "utf8"));
const jobs = plan.rows.filter((row) => selected.has(row.index));
if (jobs.length !== selected.size) throw new Error(`expected ${selected.size} recipes, found ${jobs.length}`);

const runRecipeInDocument = async (input) => {
  const read = ({ selector, property }) => {
    const matches = [...document.querySelectorAll(selector)];
    if (property === "count") return matches.length;
    const element = matches[0];
    if (!element) throw new Error(`selector did not match: ${selector}`);
    if (property === "text") return (element.textContent || "").trim();
    if (property === "value") return element.value;
    if (property === "hidden") return Boolean(element.hidden);
    throw new Error(`unsupported property: ${property}`);
  };
  const capture = (fields) => Object.fromEntries(fields.map((field) => [field.path, read(field)]));
  const before = capture(input.before || []);
  const action = {};
  for (const step of input.actions || []) {
    if (step.kind === "wait") await new Promise((resolve) => setTimeout(resolve, step.milliseconds));
    else if (step.kind === "click") {
      const target = document.querySelector(step.selector);
      if (!target) throw new Error(`action selector did not match: ${step.selector}`);
      target.click();
    } else if (step.kind === "press_key") {
      const target = document.querySelector(step.selector);
      if (!target) throw new Error(`action selector did not match: ${step.selector}`);
      target.focus();
      target.dispatchEvent(new KeyboardEvent("keydown", { key: step.key, bubbles: true, cancelable: true }));
      target.dispatchEvent(new KeyboardEvent("keyup", { key: step.key, bubbles: true, cancelable: true }));
      action[step.path] = step.key;
      continue;
    } else if (step.kind !== "observe") throw new Error(`unsupported action: ${step.kind}`);
    action[step.path] = true;
  }
  if (input.settle_milliseconds) {
    await new Promise((resolve) => setTimeout(resolve, input.settle_milliseconds));
  }
  return { before, action, after: capture(input.after || []) };
};

const mcpAppContext = async () => {
  const tree = await cdp("Page.getFrameTree");
  const root = tree && tree.frameTree;
  if (!root) throw new Error("main frame unavailable");
  const frames = [];
  const collect = (entry) => {
    frames.push(entry);
    for (const child of entry.childFrames || []) collect(child);
  };
  collect(root);
  const matches = [];
  for (const entry of frames.slice(1)) {
    const world = await cdp("Page.createIsolatedWorld", {
      frameId: entry.frame.id,
      worldName: "pursers-beta-mcp-app-dry-run",
      grantUniveralAccess: false,
    });
    if (!world || !world.executionContextId) continue;
    const probe = await cdp("Runtime.evaluate", {
      expression: `(() => ({
        embedded: window.parent !== window,
        title: document.title,
        hasBoard: Boolean(document.querySelector('#board-id')),
        hasNavigation: Boolean(document.querySelector('#tab-home, #tab-today'))
      }))()`,
      contextId: world.executionContextId,
      returnByValue: true,
    });
    const value = probe && probe.result ? probe.result.value : null;
    if (value && value.embedded === true
        && value.title === "On Board Personal Preview"
        && value.hasBoard === true && value.hasNavigation === true) {
      matches.push(world.executionContextId);
    }
  }
  if (matches.length !== 1) {
    throw new Error(matches.length
      ? "Personal MCP App frame is ambiguous"
      : "Personal MCP App frame is unavailable");
  }
  return matches[0];
};

const runRecipe = async (page, recipe, surface) => {
  if (surface !== "mcp-app") return page.evaluate(runRecipeInDocument, recipe);
  const contextId = await mcpAppContext();
  const evaluated = await cdp("Runtime.evaluate", {
    expression: `(${runRecipeInDocument.toString()})(${JSON.stringify(recipe)})`,
    contextId,
    awaitPromise: true,
    returnByValue: true,
  });
  if (evaluated && evaluated.exceptionDetails) {
    throw new Error(evaluated.exceptionDetails.text || "MCP App recipe failed");
  }
  return evaluated && evaluated.result ? evaluated.result.value : null;
};

const check = (transition, assertion) => {
  const actual = transition[assertion.phase][assertion.path];
  if (assertion.op === "eq") return actual === assertion.value;
  if (assertion.op === "gte") return actual >= assertion.value;
  if (assertion.op === "contains") return String(actual).includes(assertion.value);
  throw new Error(`unsupported assertion: ${assertion.op}`);
};

const task = await taskSpace("TK-3951189e9012 beta recipe dry-run");
console.log(JSON.stringify({ task_space_id: task.spaceId }));
const page = task.page("p1");
const results = [];
try {
  for (const row of jobs) {
    const path = row.surface === "aionui"
      ? "/extension/"
      : row.surface === "fleet"
        ? "/fleet/"
        : "/mcp-host/one/";
    await page.goto(`${origin}${path}`);
    if (row.surface === "aionui" && row.index !== 18) {
      await page.fill("#helper-url", `${origin}/`);
      await page.fill("#helper-token", "synthetic-helper-access-000000000000");
      await page.click("#connect-helper", { label: "Connect synthetic helper" });
      await page.waitForFunction(() => document.querySelector("#connection-pill")?.textContent?.trim() === "Connected", undefined, { timeout: 10_000 });
    } else if (row.surface === "fleet") {
      await page.waitForFunction(() => document.querySelector("#state")?.textContent?.includes("Updated"), undefined, { timeout: 10_000 });
    }
    const recipe = row.trust_source_recipe_fragment.recipe;
    const transition = await runRecipe(page, recipe, row.surface);
    const failed = row.canonical_predicate.assertions.filter((assertion) => !check(transition, assertion));
    results.push({
      label: "NOT final",
      not_final: true,
      row_index: row.index,
      observation_id: row.observation_id,
      source_id: row.canonical_predicate.source_id,
      surface: row.surface,
      browser_backend: "ego-browser",
      fixture_provenance: "synthetic disposable localhost",
      page_path: path,
      recipe,
      assertions: row.canonical_predicate.assertions,
      transition,
      passed: failed.length === 0,
      failed_assertions: failed,
    });
  }
} finally {
  await task.finish({ keep: [] });
}
console.log(`DRYRUN_JSON=${JSON.stringify(results)}`);
