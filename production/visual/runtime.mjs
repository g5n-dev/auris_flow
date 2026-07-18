import { readFile } from "node:fs/promises";
import { chromium } from "playwright";

const requiredEnvironment = (name) => {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`missing visual runner environment: ${name}`);
  return value;
};

const packageData = JSON.parse(
  await readFile(new URL("./node_modules/playwright/package.json", import.meta.url), "utf8")
);
const osRelease = await readFile("/etc/os-release", "utf8");
const prettyName = osRelease
  .split("\n")
  .find((line) => line.startsWith("PRETTY_NAME="))
  ?.slice("PRETTY_NAME=".length)
  .replace(/^"|"$/g, "");

const browser = await chromium.launch({ headless: true });
let browserVersion;
try {
  browserVersion = browser.version();
} finally {
  await browser.close();
}

const descriptor = {
  runtime_kind: "pinned-playwright-container",
  platform: requiredEnvironment("AURIS_VISUAL_RUNNER_PLATFORM"),
  runner_image: requiredEnvironment("AURIS_VISUAL_RUNNER_IMAGE"),
  runner_contract_sha256: requiredEnvironment("AURIS_VISUAL_RUNNER_CONTRACT_SHA256"),
  playwright_version: packageData.version,
  browser_name: "chromium",
  browser_version: browserVersion,
  node_version: process.version,
  os_release: prettyName || "unknown",
  reproducibility_scope: requiredEnvironment("AURIS_VISUAL_REPRODUCIBILITY_SCOPE")
};

process.stdout.write(`${JSON.stringify(descriptor, Object.keys(descriptor).sort())}\n`);
