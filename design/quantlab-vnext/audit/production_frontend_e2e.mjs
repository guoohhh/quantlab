import playwright from "file:///C:/Users/13533/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/index.js";
import fs from "node:fs/promises";
import path from "node:path";

const { chromium } = playwright;
const baseUrl = process.argv[2] || "http://127.0.0.1:8520";
const outputDir = process.argv[3];
if (!outputDir) throw new Error("output directory is required");
await fs.mkdir(outputDir, { recursive: true });

const browser = await chromium.launch({
  headless: true,
  executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
});
const report = {
  baseUrl,
  journey: [],
  layouts: {},
  consoleErrors: [],
  pageErrors: [],
};

async function waitForApp(page) {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });
  await page.getByText("QuantLab", { exact: true }).last().waitFor({ timeout: 60_000 });
  await page.waitForTimeout(2200);
}

async function navigate(page, label) {
  const option = page.locator('[data-testid="stSidebar"] label').filter({ hasText: label }).last();
  await option.waitFor({ state: "attached", timeout: 20_000 });
  await option.evaluate((element) => element.click());
  await page.waitForTimeout(1500);
}

async function inspect(page) {
  return page.evaluate(() => {
    const root = document.documentElement;
    const metrics = [...document.querySelectorAll('[data-testid="stMetricValue"]')].map((node) => {
      const rect = node.getBoundingClientRect();
      const parent = node.closest('[data-testid="stMetric"]')?.getBoundingClientRect();
      const textNode = node.querySelector("p") || node;
      return {
        text: node.textContent?.trim(),
        left: rect.left,
        right: rect.right,
        parentLeft: parent?.left,
        parentRight: parent?.right,
        clipped: Boolean(
          textNode.scrollWidth > textNode.clientWidth + 1 ||
          (parent && (rect.left < parent.left - 1 || rect.right > parent.right + 1))
        ),
      };
    });
    return {
      viewportWidth: innerWidth,
      documentWidth: root.scrollWidth,
      horizontalOverflow: root.scrollWidth > innerWidth + 2,
      clippedMetrics: metrics.filter((item) => item.clipped),
      metricValues: metrics.map((item) => item.text),
      exceptions: [...document.querySelectorAll('[data-testid="stException"]')]
        .map((node) => node.textContent?.trim()).filter(Boolean),
    };
  });
}

const desktop = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
desktop.on("console", (message) => {
  if (message.type() === "error") report.consoleErrors.push(`desktop: ${message.text()}`);
});
desktop.on("pageerror", (error) => report.pageErrors.push(`desktop: ${error.message}`));
await waitForApp(desktop);
report.layouts["1440-home"] = await inspect(desktop);
await desktop.screenshot({ path: path.join(outputDir, "01-1440-today-decision-center.png"), fullPage: true });

await navigate(desktop, "市场与发现");
await desktop.getByLabel("股票代码或名称").fill("510300");
await desktop.getByRole("button", { name: "搜索", exact: true }).click();
await desktop.waitForTimeout(1000);
await desktop.getByRole("button", { name: "前往 AI 研究", exact: true }).click();
await desktop.waitForTimeout(1500);
report.journey.push("发现");

const generateResearch = desktop.getByRole("button", { name: "生成研究报告", exact: true });
if (!(await generateResearch.isVisible().catch(() => false))) {
  await desktop.getByText("手动创建或切换研究", { exact: true }).click();
}
await generateResearch.click();
await desktop.getByText("当前报告身份", { exact: true }).waitFor({ timeout: 30_000 });
report.journey.push("研究");
await desktop.screenshot({ path: path.join(outputDir, "02-1440-research-workbench.png"), fullPage: true });
const researchBody = await desktop.locator("body").innerText();
report.longResearchTextVisible = researchBody.includes("资金口径与宏观证据并不完整");

await desktop.getByRole("button", { name: "为当前研究报告创建追问会话" }).click();
await desktop.waitForTimeout(1100);
const chatInput = desktop.getByPlaceholder("基于 ContextPack 继续追问");
await chatInput.fill("请解释这份报告最重要的反对证据，不要生成订单。 ");
await chatInput.press("Enter");
await desktop.waitForTimeout(2500);
report.journey.push("Chat");
report.chatMessageVisible = (await desktop.locator("body").innerText()).includes("最重要的反对证据");

await desktop.getByRole("button", { name: "前往组合与交易", exact: true }).click();
await desktop.waitForTimeout(1600);
report.layouts["1440-portfolio-before"] = await inspect(desktop);
await desktop.getByRole("button", { name: "运行 AI 交易前检查", exact: true }).click();
await desktop.getByText("AI 交易前检查卡", { exact: true }).waitFor({ timeout: 30_000 });
report.journey.push("交易前检查");
await desktop.screenshot({ path: path.join(outputDir, "03-1440-pretrade-check.png"), fullPage: true });

await desktop.getByRole("button", { name: "确认创建模拟订单", exact: true }).click();
await desktop.waitForTimeout(1800);
report.journey.push("用户确认");
const afterConfirm = await desktop.locator("body").innerText();
report.pendingVisible = afterConfirm.includes("待处理") || afterConfirm.includes("待成交");

const settle = desktop.getByRole("button", { name: "用服务器行情尝试成交", exact: true });
if (await settle.count()) {
  await settle.first().click();
  await desktop.waitForTimeout(1700);
}
report.journey.push("pending/成交");
const portfolioBody = await desktop.locator("body").innerText();
report.positionVisible = portfolioBody.includes("sh510300");
report.layouts["1440-portfolio-after"] = await inspect(desktop);
await desktop.screenshot({ path: path.join(outputDir, "04-1440-portfolio-complex-orders.png"), fullPage: true });

await desktop.getByText("单笔交易复盘", { exact: true }).click();
await desktop.getByLabel("复盘记录").fill(
  "浏览器端到端复盘：研究身份、用户确认、订单事件与最终持仓已经按隔离测试账本连接。"
);
await desktop.getByRole("button", { name: "保存复盘", exact: true }).click();
await desktop.waitForTimeout(800);
await navigate(desktop, "决策复盘");
await desktop.waitForTimeout(1200);
const reviewBody = await desktop.locator("body").innerText();
report.reviewVisible = reviewBody.includes("浏览器端到端复盘");
report.journey.push("持仓");
report.journey.push("复盘");
await desktop.screenshot({ path: path.join(outputDir, "05-1440-decision-review.png"), fullPage: true });

for (const viewport of [
  { key: "900", width: 900, height: 900 },
  { key: "390", width: 390, height: 844 },
]) {
  const page = await browser.newPage({ viewport: { width: viewport.width, height: viewport.height } });
  page.on("console", (message) => {
    if (message.type() === "error") report.consoleErrors.push(`${viewport.key}: ${message.text()}`);
  });
  page.on("pageerror", (error) => report.pageErrors.push(`${viewport.key}: ${error.message}`));
  await waitForApp(page);
  report.layouts[`${viewport.key}-home`] = await inspect(page);
  await page.screenshot({ path: path.join(outputDir, `06-${viewport.key}-today.png`), fullPage: true });
  await navigate(page, "组合与交易");
  report.layouts[`${viewport.key}-portfolio`] = await inspect(page);
  await page.screenshot({ path: path.join(outputDir, `07-${viewport.key}-portfolio.png`), fullPage: true });
  await navigate(page, "研究台");
  report.layouts[`${viewport.key}-research`] = await inspect(page);
  await page.screenshot({ path: path.join(outputDir, `08-${viewport.key}-research.png`), fullPage: true });
  await page.close();
}

await desktop.close();
report.summary = {
  journeyComplete: ["发现", "研究", "Chat", "交易前检查", "用户确认", "pending/成交", "持仓", "复盘"]
    .every((step) => report.journey.includes(step)),
  horizontalOverflowCount: Object.values(report.layouts).filter((item) => item.horizontalOverflow).length,
  clippedMetricCount: Object.values(report.layouts).reduce(
    (total, item) => total + item.clippedMetrics.length,
    0,
  ),
  exceptionCount: Object.values(report.layouts).reduce(
    (total, item) => total + item.exceptions.length,
    0,
  ),
  consoleErrorCount: report.consoleErrors.length,
  pageErrorCount: report.pageErrors.length,
};
await fs.writeFile(path.join(outputDir, "production-frontend-e2e.json"), JSON.stringify(report, null, 2), "utf8");
await browser.close();

const required = [
  report.summary.journeyComplete,
  report.longResearchTextVisible,
  report.chatMessageVisible,
  report.positionVisible,
  report.reviewVisible,
  report.summary.horizontalOverflowCount === 0,
  report.summary.clippedMetricCount === 0,
  report.summary.exceptionCount === 0,
  report.summary.consoleErrorCount === 0,
  report.summary.pageErrorCount === 0,
];
if (required.some((value) => !value)) process.exitCode = 1;
