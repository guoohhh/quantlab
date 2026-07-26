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
  const demoExpander = page.getByText("黑客松稳定 Demo", { exact: true });
  if (await demoExpander.count()) await demoExpander.click();
  await page.waitForTimeout(1_000);
  const runButton = page.getByRole("button", { name: "运行冻结历史 Demo" });
  if (await runButton.count()) {
    await runButton.click();
    await page.waitForTimeout(25_000);
  }
  await page.screenshot({ path: path.join(outputDir, "historical-demo.png"), fullPage: true });
  await fs.writeFile(
    path.join(outputDir, "historical-demo.txt"),
    await page.locator("body").innerText(),
    "utf8",
  );
} finally {
  await browser.close();
}
