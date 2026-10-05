import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const testDir = dirname(fileURLToPath(import.meta.url));
const checkout = resolve(testDir, "../../..");
const servedCheckout = resolve(process.env.PURSERS_SETTINGS_SOURCE_REPO || checkout);
const artifactDir = resolve(
  process.env.PURSERS_SETTINGS_ARTIFACT_DIR ||
    resolve(checkout, ".artifacts/settings-layout"),
);
const stateDir = await mkdtemp(resolve(tmpdir(), "pursers-settings-browser-"));
const serverScript = resolve(
  checkout,
  "tools/fleet-dashboard/tests/display_acceptance_server.py",
);
const python = process.env.PYTHON || "python3";
const server = spawn(
  python,
  [
    serverScript,
    "--repo",
    servedCheckout,
    "--port",
    "0",
    "--state-dir",
    stateDir,
    "--mode",
    "populated",
  ],
  { stdio: ["ignore", "pipe", "pipe"] },
);

let serverErrors = "";
server.stderr.setEncoding("utf8");
server.stderr.on("data", chunk => {
  serverErrors += chunk;
});

function firstLine(stream) {
  return new Promise((accept, reject) => {
    let buffer = "";
    const timer = setTimeout(
      () => reject(new Error(`fixture server timeout\n${serverErrors}`)),
      20_000,
    );
    stream.setEncoding("utf8");
    stream.on("data", chunk => {
      buffer += chunk;
      const index = buffer.indexOf("\n");
      if (index < 0) return;
      clearTimeout(timer);
      accept(buffer.slice(0, index).trim());
    });
    server.once("exit", code => {
      clearTimeout(timer);
      reject(new Error(`fixture server exited ${code}\n${serverErrors}`));
    });
  });
}

function assertGeometry(row) {
  const failures = [];
  if (row.layoutClass !== "settings-page") failures.push("legacy page grid remains");
  if (row.rootScrollWidth > row.rootClientWidth + 1) failures.push("page overflows horizontally");
  if (row.toolbarScrollWidth > row.toolbarClientWidth + 1) failures.push("toolbar clips controls");
  if (row.navScrollWidth > row.navClientWidth + 1) failures.push("section navigation clips");
  if (row.overlaps.length) failures.push(`control overlap: ${row.overlaps.join(", ")}`);
  if (row.outside.length) failures.push(`outside page: ${row.outside.join(", ")}`);
  if (row.narrowSections.length) failures.push(`narrow sections: ${row.narrowSections.join(", ")}`);
  if (!row.sectionsOrdered) failures.push("sections are not vertically ordered");
  if (row.circularNav.length) failures.push(`pill/circular navigation: ${row.circularNav.join(", ")}`);
  if (row.clippedActions.length) failures.push(`clipped actions: ${row.clippedActions.join(", ")}`);
  if (row.autonomousCards !== 1) failures.push(`configured autonomous cards: ${row.autonomousCards}`);
  if (row.approvedTemplates !== 10) failures.push(`approved template fixture count: ${row.approvedTemplates}`);
  if (failures.length) throw new Error(`${failures.join("; ")}\n${JSON.stringify(row, null, 2)}`);
}

async function geometry(page, label) {
  return page.evaluate(currentLabel => {
    const visible = node => {
      const style = getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return !node.hidden && style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const rect = node => {
      const value = node.getBoundingClientRect();
      return { left: value.left, right: value.right, top: value.top, bottom: value.bottom, width: value.width, height: value.height };
    };
    const root = document.querySelector(".settings-page, .settings-groups");
    const toolbar = document.querySelector(".settings-control-bar");
    const nav = document.querySelector(".settings-section-nav");
    if (!root || !toolbar || !nav) throw new Error("Settings layout did not render");
    const rootRect = rect(root);
    const toolbarItems = [...toolbar.children].filter(visible);
    const overlaps = [];
    for (let left = 0; left < toolbarItems.length; left += 1) {
      for (let right = left + 1; right < toolbarItems.length; right += 1) {
        const a = rect(toolbarItems[left]);
        const b = rect(toolbarItems[right]);
        const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (width > 1 && height > 1) overlaps.push(`${left}:${right}`);
      }
    }
    const candidates = [...root.querySelectorAll("button,input,select,textarea,.settings-section,.settings-section-nav,.autonomous-card,.autonomous-card .section-title,.autonomous-state,.autonomous-config-form,.settings-command-row")].filter(visible);
    const outside = candidates.filter(node => {
      const value = rect(node);
      return value.left < rootRect.left - 1 || value.right > rootRect.right + 1;
    }).map(node => node.getAttribute("name") || node.dataset.settingsMode || node.id || node.className || node.tagName.toLowerCase()).slice(0, 20);
    const sections = [...root.querySelectorAll(":scope > .settings-section")].filter(visible);
    const sectionRects = sections.map(rect);
    const narrowSections = sections.filter(node => rect(node).width < root.clientWidth - 1).map(node => node.id || node.getAttribute("aria-labelledby") || "section");
    const sectionsOrdered = sectionRects.every((value, index) => index === 0 || value.top >= sectionRects[index - 1].bottom - 1);
    const circularNav = [...nav.querySelectorAll("a")].filter(visible).filter(node => {
      const value = rect(node);
      // CSS zoom scales DOMRects, while offsetHeight remains in layout CSS pixels.
      // Judge the component's authored geometry so 200% reflow does not turn a
      // healthy 35px navigation link into a false 70px "pill" failure.
      const layoutHeight = node.offsetHeight || value.height;
      const radius = Number.parseFloat(getComputedStyle(node).borderTopLeftRadius) || 0;
      return layoutHeight > 54 || radius >= layoutHeight / 2;
    }).map(node => node.textContent.trim());
    const clippedActions = [...root.querySelectorAll("button,.button,.primary-action")].filter(visible).filter(node => node.scrollWidth > node.clientWidth + 1 || node.scrollHeight > node.clientHeight + 1).map(node => node.textContent.trim()).slice(0, 20);
    const internalScrollers = [...root.querySelectorAll(".table-scroll,.settings-plan pre")].filter(visible).map(node => ({ className: node.className, scrollWidth: node.scrollWidth, clientWidth: node.clientWidth }));
    const autonomousCards = root.querySelectorAll('.autonomous-card[data-pursers-autonomous-board="fixture-board"] .autonomous-config-form').length;
    const templateText = root.querySelector(".autonomous-state .meta:last-child")?.textContent || "";
    const approvedTemplates = (templateText.match(/template:synthetic-workflow-/g) || []).length;
    return {
      label: currentLabel,
      layoutClass: root.className,
      viewport: { width: innerWidth, height: innerHeight },
      rootClientWidth: root.clientWidth,
      rootScrollWidth: root.scrollWidth,
      toolbarClientWidth: toolbar.clientWidth,
      toolbarScrollWidth: toolbar.scrollWidth,
      navClientWidth: nav.clientWidth,
      navScrollWidth: nav.scrollWidth,
      overlaps,
      outside,
      narrowSections,
      sectionsOrdered,
      circularNav,
      clippedActions,
      autonomousCards,
      approvedTemplates,
      internalScrollers,
      sectionWidths: sectionRects.map(value => value.width),
    };
  }, label);
}

async function openSettings(page, url) {
  await page.goto(`${url}/#/settings`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("[data-settings-bound='true']", { timeout: 20_000 });
  await page.waitForSelector("[data-settings-family='board_policy']", { timeout: 20_000 });
}

async function exerciseInteractions(page) {
  await page.evaluate(() => {
    const select = document.querySelector("[data-settings-scope]");
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.evaluate(() => {
    const search = document.querySelector("[data-settings-search]");
    search.value = "dispatch";
    search.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForFunction(() => document.querySelectorAll(".settings-section-nav a").length === 1);
  await page.evaluate(() => {
    const search = document.querySelector("[data-settings-search]");
    search.value = "";
    search.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForFunction(() => document.querySelectorAll(".settings-section-nav a").length === 4);
  await page.click("[data-settings-mode='advanced']");
  await page.waitForFunction(() => document.querySelector("[data-settings-mode='advanced']")?.getAttribute("aria-pressed") === "true");
  await page.click("[data-settings-reload]");
  await page.waitForSelector("[data-settings-bound='true']", { timeout: 20_000 });
  const field = "[data-settings-family='board_policy'] input[name='stale_after_days']";
  await page.fill(field, "17");
  await page.focus(field);
  await page.evaluate(() => document.querySelector("[data-settings-reload]").click());
  await page.waitForFunction(() => {
    const input = document.querySelector("[data-settings-family='board_policy'] input[name='stale_after_days']");
    return input?.value === "17" && document.activeElement === input;
  }, undefined, { timeout: 20_000 });
  await page.click("[data-settings-mode='simple']");
  return page.evaluate(() => ({
    scope: document.querySelector("[data-settings-scope]")?.selectedOptions[0]?.dataset.board,
    search: document.querySelector("[data-settings-search]")?.value,
    mode: document.querySelector("[data-settings-mode='simple']")?.getAttribute("aria-pressed"),
    preserved: document.querySelector("[data-settings-family='board_policy'] input[name='stale_after_days']")?.value,
    dirty: document.querySelector("[data-settings-family='board_policy']")?.dataset.dirty,
  }));
}

await mkdir(artifactDir, { recursive: true });
const url = await firstLine(server.stdout);
if (!url.startsWith("http://127.0.0.1:")) throw new Error(`unexpected fixture URL: ${url}`);

let browser;
try {
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1116, height: 900 } });
  const results = [];
  for (const width of [390, 768, 1024, 1116, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await openSettings(page, url);
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      for (const mode of ["simple", "advanced"]) {
        await page.click(`[data-settings-mode='${mode}']`);
        const label = `${width}-${theme}-${mode}`;
        const row = await geometry(page, label);
        assertGeometry(row);
        results.push(row);
        await page.screenshot({ path: resolve(artifactDir, `${label}.png`), fullPage: true });
      }
    }
  }

  await page.setViewportSize({ width: 768, height: 900 });
  await openSettings(page, url);
  await page.evaluate(() => {
    document.documentElement.style.scrollbarGutter = "stable";
    document.documentElement.style.zoom = "200%";
  });
  for (const theme of ["light", "dark"]) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
    for (const mode of ["simple", "advanced"]) {
      await page.click(`[data-settings-mode='${mode}']`);
      const label = `zoom-200-${theme}-${mode}`;
      const row = await geometry(page, label);
      assertGeometry(row);
      results.push(row);
      await page.screenshot({ path: resolve(artifactDir, `${label}.png`) });
      const screenshotStyle = await page.addStyleTag({
        content: ".mobile-shell-bar,.sidebar,.skip-link{display:none!important}",
      });
      await page.locator('.autonomous-card[data-pursers-autonomous-board="fixture-board"]').screenshot({
        path: resolve(artifactDir, `${label}-automation.png`),
      });
      await screenshotStyle.evaluate(node => node.remove());
    }
  }

  await page.evaluate(() => {
    document.documentElement.style.zoom = "";
    document.documentElement.style.scrollbarGutter = "";
  });
  await page.setViewportSize({ width: 1116, height: 900 });
  await openSettings(page, url);
  const interactions = await exerciseInteractions(page);
  if (interactions?.scope !== "fixture-board" || interactions.search !== "" || interactions.mode !== "true" || interactions.preserved !== "17" || interactions.dirty !== "1") {
    throw new Error(`Settings interaction regression: ${JSON.stringify(interactions)}`);
  }
  const evidence = { schema: 1, servedCheckout, url, interactions, results };
  await writeFile(resolve(artifactDir, "results.json"), `${JSON.stringify(evidence, null, 2)}\n`);
  console.log(`settings-layout-browser=pass cases=${results.length} artifacts=${artifactDir}`);
  console.log(JSON.stringify(interactions));
} finally {
  if (browser) await browser.close();
  server.kill("SIGTERM");
  await new Promise(resolveExit => server.once("exit", resolveExit));
  await rm(stateDir, { recursive: true, force: true });
}
