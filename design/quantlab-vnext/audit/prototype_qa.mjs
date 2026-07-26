import playwright from "file:///C:/Users/13533/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/index.js";
import fs from "node:fs/promises";
import path from "node:path";

const { chromium } = playwright;
const baseUrl = process.argv[2] || "http://127.0.0.1:8123";
const outputDir = process.argv[3];
if (!outputDir) throw new Error("output directory is required");

await fs.mkdir(outputDir, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
});

const results = { consoleErrors: [], desktop: {}, compact: {}, mobile: {} };

async function shot(page, name) {
  await page.waitForTimeout(460);
  await page.screenshot({ path: path.join(outputDir, `${name}.png`), fullPage: true });
  return page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
    overflow: document.documentElement.scrollWidth > window.innerWidth + 2,
  }));
}

const desktop = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
desktop.on("console", (message) => {
  if (message.type() === "error") results.consoleErrors.push(message.text());
});
desktop.on("pageerror", (error) => results.consoleErrors.push(error.message));
await desktop.goto(baseUrl, { waitUntil: "networkidle", timeout: 30_000 });
results.desktop.today = await shot(desktop, "desktop-01-today");

for (const [target, name] of [
  ["discover", "desktop-02-discover"],
  ["researchCenter", "desktop-02b-research-center"],
  ["portfolio", "desktop-04-portfolio"],
  ["journal", "desktop-05-journal"],
  ["help", "desktop-06-help"],
  ["lab", "desktop-07-lab"],
]) {
  await desktop.locator(`.sidebar [data-target="${target}"]`).click();
  results.desktop[target] = await shot(desktop, name);
}

await desktop.locator('.sidebar [data-target="discover"]').click();
await desktop.locator('.discovery-card[data-symbol="600519"]').click();
results.desktop.research = await shot(desktop, "desktop-03-research");

await desktop.locator(".research-page .open-order").first().click();
await shot(desktop, "desktop-08-order-draft");
await desktop.locator("#orderNext").click();
await shot(desktop, "desktop-09-order-check");
await desktop.locator("#orderNext").click();
await desktop.locator("#confirmCheckbox").check();
await shot(desktop, "desktop-10-order-confirm");
await desktop.locator(".order-close").click();

await desktop.locator("#researchChat").click();
await shot(desktop, "desktop-11-chat");
await desktop.locator(".chat-drawer .drawer-close").click();

for (const [value, name] of [
  ["loading", "desktop-12-state-loading"],
  ["degraded", "desktop-13-state-degraded"],
  ["empty", "desktop-14-state-empty"],
  ["error", "desktop-15-state-error"],
  ["background", "desktop-16-state-background"],
]) {
  await desktop.locator("#stateSelector").selectOption(value);
  await shot(desktop, name);
}
await desktop.locator("#stateSelector").selectOption("normal");

const compact = await browser.newPage({ viewport: { width: 900, height: 900 } });
compact.on("console", (message) => {
  if (message.type() === "error") results.consoleErrors.push(message.text());
});
compact.on("pageerror", (error) => results.consoleErrors.push(error.message));
await compact.goto(baseUrl, { waitUntil: "networkidle", timeout: 30_000 });
results.compact.today = await shot(compact, "compact-01-today");
await compact.locator('.sidebar [data-target="discover"]').click();
results.compact.discover = await shot(compact, "compact-02-discover");
await compact.locator('.sidebar [data-target="researchCenter"]').click();
results.compact.researchCenter = await shot(compact, "compact-02b-research-center");
await compact.locator('.sidebar [data-target="lab"]').click();
results.compact.lab = await shot(compact, "compact-03-lab");
await compact.locator('.sidebar [data-target="help"]').click();
results.compact.help = await shot(compact, "compact-04-help");
await compact.close();

const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
mobile.on("console", (message) => {
  if (message.type() === "error") results.consoleErrors.push(message.text());
});
mobile.on("pageerror", (error) => results.consoleErrors.push(error.message));
await mobile.goto(baseUrl, { waitUntil: "networkidle", timeout: 30_000 });
results.mobile.today = await shot(mobile, "mobile-01-today");
await mobile.locator('.mobile-nav [data-target="discover"]').click();
results.mobile.discover = await shot(mobile, "mobile-02-discover");
await mobile.locator('.mobile-nav [data-target="researchCenter"]').click();
results.mobile.researchCenter = await shot(mobile, "mobile-02b-research-center");
await mobile.locator('.mobile-nav [data-target="discover"]').click();
await mobile.locator('.discovery-card[data-symbol="600519"]').click();
results.mobile.research = await shot(mobile, "mobile-03-research");
await mobile.locator(".research-page .open-order").first().click();
results.mobile.order = await shot(mobile, "mobile-04-order");
await mobile.locator(".order-close").click();
await mobile.locator('.mobile-nav [data-target="portfolio"]').click();
results.mobile.portfolio = await shot(mobile, "mobile-05-portfolio");

await fs.writeFile(path.join(outputDir, "qa-results.json"), JSON.stringify(results, null, 2), "utf8");
await browser.close();
