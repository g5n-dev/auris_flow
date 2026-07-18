import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const sourceUrl = new URL("./buildInsightAgentAction.ts", import.meta.url);

test("动作目标只使用报告冻结且强绑定的指标快照", async () => {
  const source = await readFile(sourceUrl, "utf8");
  const snapshotIndex = source.indexOf("governedReport.metricSnapshots?.[metricIndex]");
  const resultBindingIndex = source.indexOf("reportSnapshot.metric_result_id !== metricResultId");
  const keyBindingIndex = source.indexOf("reportSnapshot.metric_key !== selectedMetric.key");
  const presentationIndex = source.indexOf("snapshotValuePresentation(reportSnapshot).value");
  const guardIndex = source.indexOf("targetMetricValue === null");
  const targetIndex = source.indexOf("target_value: targetMetricValue + 2");

  assert(snapshotIndex >= 0, "动作未读取报告冻结指标快照");
  assert(resultBindingIndex > snapshotIndex, "缺少 metric_result_id 强绑定校验");
  assert(keyBindingIndex > snapshotIndex, "缺少 metric_key 强绑定校验");
  assert(presentationIndex > keyBindingIndex, "强绑定校验必须先于快照数值解析");
  assert(guardIndex > presentationIndex, "缺少冻结快照 N/A 数值目标门禁");
  assert(targetIndex > guardIndex, "数值目标必须在 null 门禁之后构造");
  assert.doesNotMatch(source, /target_value:\s*selectedMetric\.valueNumber/);
  assert.match(source, /报告冻结快照为 N\/A 或未物化.*禁止创建数值目标/);
});
