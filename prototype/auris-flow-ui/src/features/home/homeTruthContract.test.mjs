import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (relativePath) => readFile(new URL(relativePath, import.meta.url), "utf8");

test("production 首页挂载 ops-summary 权威 reader 且不回退 fixture", async () => {
  const [contentSource, workspaceView, homeSource] = await Promise.all([
    read("../../workspace/moduleContentSource.ts"),
    read("../../workspace/ModuleWorkspaceView.tsx"),
    read("./HomeModule.tsx")
  ]);

  assert.match(contentSource, /AUTHORITATIVE_CONTENT_READERS[^]*"home"/);
  assert.match(workspaceView, /HomeModuleOutlet[^]*projectionData=\{projectionReceipt\?\.raw\}/);
  assert.match(workspaceView, /moduleKey !== "home" && <MetricCards/);
  assert.match(homeSource, /buildHomeTruthSummary\([^]*LABEL_DEMO_MODE[^]*: projectionData/);
  assert.match(homeSource, /LABEL_DEMO_MODE/);
  assert.match(homeSource, /业务运行摘要/);
});

test("首页默认首屏的业务摘要先于二级运行细节", async () => {
  const homeSource = await read("./HomeModule.tsx");
  const summaryIndex = homeSource.indexOf("renderBusinessSummary");
  const dashboardIndex = homeSource.indexOf("<HomeRunDashboard");
  assert.ok(summaryIndex >= 0);
  assert.ok(dashboardIndex > summaryIndex);
  assert.match(homeSource, /<details[^>]*className="home-secondary-details"/);
});
