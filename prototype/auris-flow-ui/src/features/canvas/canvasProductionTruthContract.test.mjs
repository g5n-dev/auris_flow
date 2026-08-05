import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const readSource = async (path) =>
  readFile(new URL(path, import.meta.url), "utf8");

test("生产模式禁用本地指标假成功并在 UI 展示明确原因", async () => {
  const [actions, tab, toolbar] = await Promise.all([
    readSource("./controller/buildCanvasDraftModelActions.ts"),
    readSource("./components/tabs/TaskExperimentsTab.tsx"),
    readSource("./controller/buildCanvasToolbarModel.ts")
  ]);

  assert.match(actions, /blockUnconfiguredMetricAction\(\)/);
  assert.match(actions, /生产模式禁止由本地状态制造成功结果/);
  assert.match(tab, /disabled=\{Boolean\(metricDraftActionsDisabledReason\)\}/);
  assert.match(tab, /生产指标助手暂不可用/);
  assert.match(toolbar, /canvasMetricDraftDisabledReason/);
});

test("旧平台同步只保留 DEMO 兼容数据，生产节点库不可见且动作 fail closed", async () => {
  const [catalogText, runtimeModel, actions] = await Promise.all([
    readSource("../../catalogs/static-catalog.json"),
    readSource("./controller/useCanvasRuntimeModel.ts"),
    readSource("./controller/buildCanvasConfiguredNodeActions.ts")
  ]);
  const catalog = JSON.parse(catalogText);
  const legacyTemplate = catalog.canvasNodeTemplates.find((item) => item.key === "aizj-sync-job");

  assert.equal(legacyTemplate.productionDisabled, true);
  assert.match(legacyTemplate.disabledReason, /POST \/api\/v1\/task-runs/);
  assert.match(runtimeModel, /demoMode \|\| !template\.productionDisabled/);
  assert.match(actions, /!demoMode && template\.productionDisabled/);
});
