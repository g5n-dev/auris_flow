import { defineConfig } from "playwright/test";

const defaultBaselineDir = new URL(
  "../audit-iteration/frontend-decomposition/visual-regression/screenshots/",
  import.meta.url
).pathname;
const baselineDir = process.env.AURIS_VISUAL_BASELINE_DIR || defaultBaselineDir;

export default defineConfig({
  testDir: new URL("./", import.meta.url).pathname,
  testMatch: "visual-regression.spec.mjs",
  outputDir: new URL("../e2e/artifacts/visual-regression/", import.meta.url).pathname,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: {
    timeout: 10_000,
    toHaveScreenshot: {
      animations: "disabled",
      maxDiffPixelRatio: 0.001,
      threshold: 0.2
    }
  },
  snapshotPathTemplate: `${baselineDir}/{arg}{ext}`,
  use: {
    browserName: "chromium",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    colorScheme: "light",
    reducedMotion: "reduce",
    viewport: { width: 1440, height: 900 },
    screen: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure"
  }
});
