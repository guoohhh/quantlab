import playwright from "file:///C:/Users/13533/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/index.js";
import fs from "node:fs/promises";
import path from "node:path";

const { chromium } = playwright;
const baseUrl = process.argv[2] || "http://127.0.0.1:8513";
const outputDir = process.argv[3];
if (!outputDir) throw new Error("output directory is required");

await fs.mkdir(outputDir, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });

try {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded", timeout: 60_000 });
  await page.waitForTimeout(8_000);
  await page.getByText("模拟交易", { exact: true }).last().click();
  await page.waitForTimeout(4_000);

  const accountName = page.getByLabel("账户名称");
  if (await accountName.count()) {
    await accountName.fill("体验审计账户");
    await page.getByRole("button", { name: "创建模拟账户" }).click();
    await page.waitForTimeout(5_000);
  }

  await page.screenshot({ path: path.join(outputDir, "1-account-created.png"), fullPage: true });

  const symbol = page.getByLabel("标的");
  if (await symbol.count()) await symbol.fill("sh510300");
  const quantity = page.getByLabel("数量");
  if (await quantity.count()) await quantity.fill("100");
  await page.getByRole("button", { name: "运行 AI 交易前检查" }).click();
  await page.waitForTimeout(15_000);

  await page.screenshot({ path: path.join(outputDir, "2-pretrade-result.png"), fullPage: true });
  await fs.writeFile(
    path.join(outputDir, "simulator-transcript.txt"),
    await page.locator("body").innerText(),
    "utf8",
  );
} finally {
  await browser.close();
}
