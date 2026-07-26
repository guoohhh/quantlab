import playwright from "file:///C:/Users/13533/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/index.js";
import fs from "node:fs/promises";

const { chromium } = playwright;
const baseUrl = process.argv[2] || "http://127.0.0.1:8123";
const outputPath = process.argv[3];
if (!outputPath) throw new Error("output path is required");

const browser = await chromium.launch({
  headless: true,
  executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
});

const errors = [];
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.on("console", (message) => {
  if (message.type() === "error") errors.push(message.text());
});
page.on("pageerror", (error) => errors.push(error.message));
await page.goto(baseUrl, { waitUntil: "networkidle" });

async function shellState(label) {
  return page.evaluate((name) => ({
    label: name,
    page: document.querySelector(".page-view.active")?.dataset.page,
    orderClass: document.querySelector("#orderSheet")?.className,
    orderHidden: document.querySelector("#orderSheet")?.getAttribute("aria-hidden"),
    scrimClass: document.querySelector("#scrim")?.className,
  }), label);
}

const navigation = [await shellState("initial")];
for (const target of ["discover", "researchCenter", "portfolio", "journal", "help", "lab", "research"]) {
  if (target === "research") {
    await page.locator('.sidebar [data-target="discover"]').click();
    await page.locator('.discovery-card[data-symbol="600519"]').click();
  } else {
    await page.locator(`.sidebar [data-target="${target}"]`).click();
  }
  await page.waitForTimeout(420);
  navigation.push(await shellState(target));
}

await page.locator('.sidebar [data-target="researchCenter"]').click();
await page.locator('.research-center-page .workspace-lenses button').nth(1).click();
const researchDesk = await page.evaluate(() => ({
  page: document.querySelector(".page-view.active")?.dataset.page,
  visibleTheses: [...document.querySelectorAll("button.thesis-row")].filter((row) => !row.hidden).length,
  coverageDomains: document.querySelectorAll(".domain-list button").length,
  pipelineSteps: document.querySelectorAll(".research-pipeline button").length,
}));
await page.locator('button.thesis-row[data-symbol="600519"]').click();

await page.locator(".research-page .open-order").first().click();
await page.locator("#orderDirection").selectOption("reduce");
await page.locator("#orderQuantity").fill("200");
await page.locator("#orderQuantity").blur();
const reduceDraft = await page.evaluate(() => ({
  amount: document.querySelector("#orderAmount")?.textContent,
  cash: document.querySelector("#checkCashAfter")?.textContent,
  position: document.querySelector("#positionChange")?.textContent,
  summary: document.querySelector("#confirmOrderSummary")?.textContent,
  blocked: document.querySelector("#orderNext")?.disabled,
}));

await page.locator("#orderDirection").selectOption("exit");
const exitDraft = await page.evaluate(() => ({
  quantity: document.querySelector("#orderQuantity")?.value,
  quantityDisabled: document.querySelector("#orderQuantity")?.disabled,
  position: document.querySelector("#positionChange")?.textContent,
  summary: document.querySelector("#confirmOrderSummary")?.textContent,
}));

await page.locator("#orderDirection").selectOption("buy");
await page.locator("#orderQuantity").fill("2000");
await page.locator("#orderQuantity").blur();
await page.locator("#orderNext").click();
const blockedBuy = await page.evaluate(() => ({
  step: document.querySelector("#orderSheet")?.dataset.step,
  nextDisabled: document.querySelector("#orderNext")?.disabled,
  summary: document.querySelector("#orderCheckSummary strong")?.textContent,
}));
await page.locator(".order-close").click();

await page.locator(".research-page .open-order").first().click();
await page.locator("#orderNext").click();
await page.locator("#orderNext").click();
await page.locator("#confirmCheckbox").check();
await page.locator("#orderNext").click();
await page.waitForTimeout(420);
const confirmedOrder = await page.evaluate(() => ({
  page: document.querySelector(".page-view.active")?.dataset.page,
  appended: Boolean(document.querySelector(".order-timeline .demo-order")),
  text: document.querySelector(".order-timeline .demo-order strong")?.textContent,
  status: document.querySelector(".order-timeline .demo-order em")?.textContent,
}));

await page.locator("#activityTrigger").click();
await page.locator('[data-drawer-tab="notifications"]').click();
const drawerTabs = await page.evaluate(() => ({
  active: document.querySelector("[data-drawer-tab].active")?.dataset.drawerTab,
  visibleGroups: [...document.querySelectorAll("[data-drawer-group]")]
    .filter((item) => !item.hidden)
    .map((item) => item.dataset.drawerGroup),
}));
await page.locator("#activityDrawer .drawer-close").click();

await page.locator("#commandTrigger").click();
await page.locator("#paletteInput").fill("不存在的标的");
const palette = await page.evaluate(() => ({
  visibleResults: [...document.querySelectorAll(".palette-result")].filter((item) => !item.hidden).length,
  emptyVisible: !document.querySelector(".palette-empty")?.hidden,
}));
await page.keyboard.press("Escape");

await page.locator('.sidebar [data-target="research"]').count().catch(() => 0);
await page.locator('.sidebar [data-target="discover"]').click();
await page.locator('.discovery-card[data-symbol="600519"]').click();
await page.locator("#watchlistToggle").click();
const watchlist = await page.evaluate(() => ({
  pressed: document.querySelector("#watchlistToggle")?.getAttribute("aria-pressed"),
  label: document.querySelector("#watchlistToggle")?.textContent,
}));

const reduced = await browser.newPage({
  viewport: { width: 1440, height: 1000 },
  reducedMotion: "reduce",
});
await reduced.goto(baseUrl, { waitUntil: "networkidle" });
const reducedMotion = await reduced.evaluate(() => ({
  canvasDisplay: getComputedStyle(document.querySelector("#ambientField")).display,
  canvasWidth: document.querySelector("#ambientField").width,
  scrollBehavior: getComputedStyle(document.documentElement).scrollBehavior,
}));
await reduced.close();

const result = { errors, navigation, researchDesk, reduceDraft, exitDraft, blockedBuy, confirmedOrder, drawerTabs, palette, watchlist, reducedMotion };
await fs.writeFile(outputPath, JSON.stringify(result, null, 2), "utf8");
await browser.close();
