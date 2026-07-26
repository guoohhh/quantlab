import playwright from "file:///C:/Users/13533/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/index.js";
import fs from "node:fs/promises";
import path from "node:path";

const { chromium } = playwright;
const baseUrl = process.argv[2] || "http://127.0.0.1:8513";
const outputDir = process.argv[3];
if (!outputDir) throw new Error("output directory is required");

const workspaces = ["今日", "市场与发现", "研究台", "组合与交易", "决策复盘", "专业空间", "帮助中心"];
const viewports = [
  { key: "desktop-1440", width: 1440, height: 1000, allPages: true },
  { key: "compact-900", width: 900, height: 900, allPages: false },
  { key: "mobile-390", width: 390, height: 844, allPages: false },
];

await fs.mkdir(outputDir, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
});
const report = { baseUrl, workspaces, viewports: {}, consoleErrors: [], pageErrors: [] };

async function waitForApp(page) {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });
  await page.getByText("QuantLab", { exact: true }).last().waitFor({ timeout: 60_000 });
  await page.waitForTimeout(2500);
}

async function openSidebar(page) {
  const target = page.getByText("投资工作区", { exact: true });
  if (await target.isVisible().catch(() => false)) return;
  const controls = [
    page.locator('[data-testid="stSidebarCollapsedControl"] button'),
    page.locator('[data-testid="stSidebarCollapsedControl"]'),
    page.getByRole("button", { name: /sidebar|侧边栏|菜单/i }),
  ];
  for (const control of controls) {
    if (await control.first().isVisible().catch(() => false)) {
      await control.first().click();
      await page.waitForTimeout(350);
      return;
    }
  }
}

async function selectWorkspace(page, label) {
  await openSidebar(page);
  const option = page.locator('[data-testid="stSidebar"] label').filter({ hasText: label }).last();
  await option.waitFor({ state: "visible", timeout: 20_000 });
  await option.evaluate((element) => element.click());
  await page.waitForTimeout(1600);
}

async function inspect(page) {
  return page.evaluate(() => {
    const root = document.documentElement;
    const body = document.body;
    const h1 = document.querySelector(".ql-workspace-head h1");
    const sidebarLabel = [...document.querySelectorAll('[data-testid="stSidebar"] label p')]
      .find((node) => ["今日", "市场与发现", "研究台"].includes(node.textContent?.trim()));
    const exceptions = [...document.querySelectorAll('[data-testid="stException"]')]
      .map((node) => node.textContent?.trim()).filter(Boolean);
    return {
      viewportWidth: window.innerWidth,
      documentWidth: root.scrollWidth,
      bodyWidth: body.scrollWidth,
      horizontalOverflow: root.scrollWidth > window.innerWidth + 2 || body.scrollWidth > window.innerWidth + 2,
      h1FontSize: h1 ? getComputedStyle(h1).fontSize : null,
      bodyFontSize: getComputedStyle(body).fontSize,
      sidebarFontSize: sidebarLabel ? getComputedStyle(sidebarLabel).fontSize : null,
      exceptions,
    };
  });
}

for (const viewport of viewports) {
  const page = await browser.newPage({ viewport: { width: viewport.width, height: viewport.height } });
  page.on("console", (message) => {
    if (message.type() === "error") report.consoleErrors.push(`${viewport.key}: ${message.text()}`);
  });
  page.on("pageerror", (error) => report.pageErrors.push(`${viewport.key}: ${error.message}`));
  await waitForApp(page);
  const targets = viewport.allPages ? workspaces : ["今日", "组合与交易", "帮助中心"];
  const states = {};
  for (const label of targets) {
    await selectWorkspace(page, label);
    states[label] = await inspect(page);
    await page.screenshot({
      path: path.join(outputDir, `${viewport.key}-${String(workspaces.indexOf(label) + 1).padStart(2, "0")}-${label}.png`),
      fullPage: true,
    });
  }

  if (viewport.key === "desktop-1440") {
    await selectWorkspace(page, "组合与交易");
    const create = page.getByRole("button", { name: "创建模拟账户" });
    if (await create.count()) {
      await page.getByLabel("账户名称").fill("vNext 浏览器验收账户");
      await create.click();
      await page.waitForTimeout(2200);
      states["组合与交易-创建账户后"] = await inspect(page);
      await page.screenshot({ path: path.join(outputDir, "desktop-1440-08-account-created.png"), fullPage: true });
    }
    await selectWorkspace(page, "帮助中心");
    const quickStart = page.getByText("快速开始：完成一次完整路径", { exact: true });
    if (await quickStart.count()) {
      await quickStart.click();
      await page.waitForTimeout(350);
      states["帮助中心-展开教程"] = await inspect(page);
      await page.screenshot({ path: path.join(outputDir, "desktop-1440-09-help-expanded.png"), fullPage: true });
    }
  }
  report.viewports[viewport.key] = states;
  await page.close();
}

report.summary = {
  horizontalOverflowCount: Object.values(report.viewports)
    .flatMap((group) => Object.values(group))
    .filter((item) => item.horizontalOverflow).length,
  exceptionCount: Object.values(report.viewports)
    .flatMap((group) => Object.values(group))
    .reduce((sum, item) => sum + item.exceptions.length, 0),
  consoleErrorCount: report.consoleErrors.length,
  pageErrorCount: report.pageErrors.length,
};

await fs.writeFile(path.join(outputDir, "browser-acceptance.json"), JSON.stringify(report, null, 2), "utf8");
await browser.close();
if (Object.values(report.summary).some((value) => value !== 0)) process.exitCode = 1;
