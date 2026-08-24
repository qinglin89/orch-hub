#!/usr/bin/env node

import { mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve } from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const MAIN_REPO = "acme-web";
const MAIN_TASK = "2026-08-20-audit-ready-deployments";
const TOKEN = "orch-hub-demo";
let activeBrowser = null;

function parseArgs(argv) {
  const values = {
    baseUrl: "http://127.0.0.1:8877/",
    cdpUrl: "http://127.0.0.1:9333/",
    output: "dist/visual-demo-hires/screenshots",
    scene: "all",
  };
  for (let index = 2; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!value) throw new Error(`${flag} requires a value`);
    if (flag === "--base-url") values.baseUrl = value;
    else if (flag === "--cdp-url") values.cdpUrl = value;
    else if (flag === "--output") values.output = value;
    else if (flag === "--scene") values.scene = value;
    else throw new Error(`unknown argument: ${flag}`);
  }
  values.output = resolve(values.output);
  values.baseUrl = new URL(values.baseUrl);
  values.cdpUrl = new URL(values.cdpUrl);
  for (const [label, url] of [
    ["base URL", values.baseUrl],
    ["CDP URL", values.cdpUrl],
  ]) {
    if (!["127.0.0.1", "localhost", "::1"].includes(url.hostname)) {
      throw new Error(`${label} must use a loopback host`);
    }
  }
  return values;
}

async function waitForPaint(page) {
  await page.evaluate(async () => {
    if (document.fonts?.ready) await document.fonts.ready;
    await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  });
}

async function capture(page, output, filename) {
  await waitForPaint(page);
  const path = resolve(output, filename);
  await page.screenshot({ path, type: "png", animations: "disabled" });
  process.stdout.write(`${path}\n`);
}

async function navigate(page, baseUrl, hash, readySelector) {
  const url = new URL(baseUrl);
  url.hash = hash;
  await page.goto(url.href, { waitUntil: "networkidle" });
  await page.locator(readySelector).first().waitFor({ state: "visible" });
  await page.evaluate(() => window.scrollTo(0, 0));
}

async function main() {
  const options = parseArgs(process.argv);
  await mkdir(options.output, { recursive: true });

  const browser = await chromium.connectOverCDP(options.cdpUrl.href);
  activeBrowser = browser;
  const context = browser.contexts()[0];
  if (!context) throw new Error("the CDP browser exposed no default context");

  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (
      ["127.0.0.1", "localhost", "::1"].includes(url.hostname) ||
      url.protocol === "data:"
    ) {
      await route.continue();
    } else {
      await route.abort("blockedbyclient");
    }
  });

  const page = context.pages()[0] || (await context.newPage());
  const cdp = await context.newCDPSession(page);
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 900,
    deviceScaleFactor: 2,
    mobile: false,
    screenWidth: 1440,
    screenHeight: 900,
  });
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.addInitScript(({ token }) => {
    localStorage.setItem("orch-hub-token", token);
    localStorage.setItem("orch-hub-theme", "dark");
  }, { token: TOKEN });

  if (options.scene === "reports") {
    await navigate(page, options.baseUrl, "/reports", ".card.report");
    await capture(page, options.output, "06-report-feed.png");
    await browser.close();
    activeBrowser = null;
    return;
  }
  if (options.scene !== "all") {
    throw new Error(`unsupported scene: ${options.scene}`);
  }

  await navigate(page, options.baseUrl, "/overview", "h1");
  await page.getByRole("heading", { name: "Operations overview" }).waitFor();
  await capture(page, options.output, "01-overview.png");

  await navigate(
    page,
    options.baseUrl,
    `/tasks/${MAIN_REPO}/${MAIN_TASK}`,
    "h1"
  );
  const startButton = page.locator(".open-run-options");
  if (await startButton.count()) {
    await startButton.click();
    await page.locator("dialog.run-options[open]").waitFor();
    await page.locator("#opt-dev-mode").selectOption("excellent");
    await page.waitForFunction(
      () =>
        document
          .querySelector('.config-panel[data-state="ready"]')
          ?.textContent.includes("claude-opus-5")
    );
    await page.locator("#opt-review-mode").selectOption("standard");
    await page.waitForFunction(
      () =>
        document
          .querySelector('.config-panel[data-state="ready"]')
          ?.textContent.includes("gpt-5.4")
    );
    await page.locator(".opt-plan-gate").check();
    await capture(page, options.output, "02-launch-config.png");
    await page.locator('.start-form button[type="submit"]').click();
    await page.waitForTimeout(750);
  }
  // The task view does not poll a newly-created question continuously. Reloading
  // the same isolated route reads the real control-dir question once it exists.
  await navigate(
    page,
    options.baseUrl,
    `/tasks/${MAIN_REPO}/${MAIN_TASK}`,
    "h1"
  );
  await page.getByRole("heading", { name: "Waiting for an answer" }).waitFor({
    timeout: 15_000,
  });
  await page.evaluate(() => window.scrollTo(0, 0));
  await capture(page, options.output, "03-plan-gate.png");

  await page.locator(".answer-send").click();
  await page.waitForTimeout(3_500);
  await navigate(page, options.baseUrl, "/runs", "#run-groups");
  const mainGroup = page.locator(".task-group", { hasText: MAIN_TASK }).first();
  await mainGroup.locator(".task-group-head").click();
  await mainGroup.locator(".card.run").waitFor({ state: "visible" });
  await capture(page, options.output, "04-run-history.png");

  await navigate(
    page,
    options.baseUrl,
    `/evaluations/${MAIN_REPO}/${MAIN_TASK}`,
    ".eval-gen-btn"
  );
  await page.locator(".eval-gen-btn").click();
  await page.locator("#eval-pick").waitFor({ state: "visible" });
  await page.locator(".eval-sem-btn").click();
  await page.locator(".sem-report:not(.sem-absent)").waitFor({
    state: "visible",
    timeout: 15_000,
  });
  await page.locator(".sem-report:not(.sem-absent)").evaluate((element) => {
    window.scrollTo(0, element.offsetTop - 64);
  });
  await capture(page, options.output, "05-evaluation.png");

  await navigate(page, options.baseUrl, "/reports", ".card.report");
  await capture(page, options.output, "06-report-feed.png");

  process.stdout.write(
    JSON.stringify({
      output: options.output,
      cssViewport: { width: 1440, height: 900 },
      deviceScaleFactor: 2,
      pixelOutput: { width: 2880, height: 1800 },
    }) + "\n"
  );
  await browser.close();
  activeBrowser = null;
}

main().catch(async (error) => {
  try {
    await activeBrowser?.close();
  } catch {
    // The capture error below is the useful failure; cleanup is best effort.
  }
  process.stderr.write(`hires capture: ${error.stack || error}\n`);
  process.exitCode = 1;
});
