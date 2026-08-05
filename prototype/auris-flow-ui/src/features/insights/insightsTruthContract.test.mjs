import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (relativePath) => readFile(new URL(relativePath, import.meta.url), "utf8");

test("权威快照不完整时隐藏洞察数值、图表、证据统计和 Agent 归因", async () => {
  const [scopePanel, workspaceView, projectionMetrics] = await Promise.all([
    read("./components/InsightsScopePanel.tsx"),
    read("./components/InsightsWorkspaceView.tsx"),
    read("../../workspace/projectionMetrics.ts")
  ]);

  assert.match(scopePanel, /authoritativeInsightDisplayReady/);
  assert.match(scopePanel, /尚未生成权威快照/);
  assert.match(scopePanel, /MetricSnapshot、comparable_series 与 evidence_refs/);
  assert.match(scopePanel, /retryAuthoritativeMetrics/);
  assert.match(scopePanel, /setActiveModule\("canvas"\)/);
  assert.match(workspaceView, /authoritativeInsightDisplayReady &&/);
  assert.match(
    workspaceView,
    /currentTab === "quality" && <InsightsHotwordPanel controller=\{controller\} \/>[^]*authoritativeInsightDisplayReady &&/,
    "独立使用 BFF 契约的 ASR 热词治理不得被业务 MetricSnapshot 门禁一并隐藏"
  );
  assert.match(projectionMetrics, /moduleKey === "insights"[^]*return fallbackMetrics\.map\(unavailableMetric\)/);
});
