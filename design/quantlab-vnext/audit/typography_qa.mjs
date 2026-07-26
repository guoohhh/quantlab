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
const page = await browser.newPage({
  viewport: { width: 1440, height: 1050 },
  deviceScaleFactor: 2,
  reducedMotion: "reduce",
});
const errors = [];
page.on("console", (message) => {
  if (message.type() === "error") errors.push(message.text());
});
page.on("pageerror", (error) => errors.push(error.message));

await page.goto(baseUrl, { waitUntil: "networkidle" });
await page.evaluate(async () => {
  await document.fonts.ready;
});

async function inspectTypography(label) {
  return page.evaluate((pageLabel) => {
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none"
        && style.visibility !== "hidden"
        && Number(style.opacity) > 0
        && !element.closest('[aria-hidden="true"], [hidden]')
        && rect.width > 0
        && rect.height > 0
        && rect.right > 0
        && rect.bottom > 0
        && rect.left < window.innerWidth
        && rect.top < window.innerHeight;
    };
    const textElements = [...document.querySelectorAll("body *")].filter((element) => {
      if (!visible(element)) return false;
      if (!element.textContent?.trim()) return false;
      return [...element.children].every((child) => !child.textContent?.trim());
    });
    const smallText = textElements
      .map((element) => ({
        selector: `${element.tagName.toLowerCase()}${element.className ? `.${String(element.className).trim().replace(/\s+/g, ".")}` : ""}`,
        text: element.textContent.trim().slice(0, 48),
        size: Number.parseFloat(getComputedStyle(element).fontSize),
        color: getComputedStyle(element).color,
      }))
      .filter((item) => item.size < 11.5)
      .sort((a, b) => a.size - b.size);

    const body = getComputedStyle(document.body);
    const heading = getComputedStyle(document.querySelector(".page-view.active h1"));
    return {
      label: pageLabel,
      dpr: window.devicePixelRatio,
      fontAvailability: {
        dengXian: document.fonts.check('16px "DengXian"'),
        microsoftYaHeiUi: document.fonts.check('16px "Microsoft YaHei UI"'),
        notoSansSc: document.fonts.check('16px "Noto Sans SC"'),
        notoSerifSc: document.fonts.check('16px "Noto Serif SC"'),
        bahnschrift: document.fonts.check('16px "Bahnschrift"'),
      },
      rendering: {
        bodyFont: body.fontFamily,
        bodySize: body.fontSize,
        textRendering: body.textRendering,
        headingFont: heading.fontFamily,
        headingSize: heading.fontSize,
        headingWeight: heading.fontWeight,
        noiseDisplay: getComputedStyle(document.body, "::after").display,
        noiseOpacity: getComputedStyle(document.body, "::after").opacity,
      },
      smallTextCount: smallText.length,
      smallestSamples: smallText.slice(0, 30),
    };
  }, label);
}

const results = { errors, pages: [] };
results.pages.push(await inspectTypography("today"));
await page.screenshot({ path: path.join(outputDir, "hidpi-today-fold.png"), animations: "disabled", scale: "device" });

await page.locator('.sidebar [data-target="researchCenter"]').click();
await page.waitForTimeout(380);
results.pages.push(await inspectTypography("researchCenter"));
await page.screenshot({ path: path.join(outputDir, "hidpi-research-center-fold.png"), animations: "disabled", scale: "device" });

await page.locator('button.thesis-row[data-symbol="600519"]').click();
await page.waitForTimeout(380);
results.pages.push(await inspectTypography("research"));
await page.screenshot({ path: path.join(outputDir, "hidpi-research-fold.png"), animations: "disabled", scale: "device" });

for (const target of ["discover", "help", "lab"]) {
  await page.locator(`.sidebar [data-target="${target}"]`).click();
  await page.waitForTimeout(240);
  results.pages.push(await inspectTypography(target));
}

await fs.writeFile(path.join(outputDir, "typography-results.json"), JSON.stringify(results, null, 2), "utf8");
await browser.close();
