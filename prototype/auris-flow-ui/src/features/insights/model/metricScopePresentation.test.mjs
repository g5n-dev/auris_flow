import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const sourceUrl = new URL("./metricScopePresentation.ts", import.meta.url);

const loadModel = async () => {
  const source = await readFile(sourceUrl, "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext
    }
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
};

const normalizedSnapshot = {
  metric_result_id: "metric-conversion-42",
  metric_key: "conversionProgress",
  label_version_applicability: "required",
  label_scope: {
    taxonomy_mode: "normalized",
    source_label_version_ids: ["lv_18", "lv_19"],
    target_label_version_id: "lv_19",
    mapping_bundle_id: "lmb_to_lv_19_20250531",
    fact_set_generation: 42,
    fact_as_of: "2025-05-31T23:59:59Z"
  },
  comparability_status: "comparable",
  comparability_reason_codes: ["MAPPING_EXACT"]
};

test("完整快照展示冻结口径且 comparable 才保留普通涨跌", async () => {
  const { buildMetricScopePresentation, metricDeltaPresentation } = await loadModel();
  const presentation = buildMetricScopePresentation(normalizedSnapshot);

  assert.equal(presentation.taxonomyMode, "normalized");
  assert.deepEqual(presentation.sourceLabelVersionIds, ["lv_18", "lv_19"]);
  assert.equal(presentation.targetLabelVersionId, "lv_19");
  assert.equal(presentation.mappingBundleId, "lmb_to_lv_19_20250531");
  assert.equal(presentation.factSetGeneration, 42);
  assert.equal(presentation.factAsOf, "2025-05-31T23:59:59Z");
  assert.equal(presentation.comparabilityStatus, "comparable");
  assert.deepEqual(presentation.comparabilityReasonCodes, ["MAPPING_EXACT"]);
  assert.equal(presentation.showDelta, true);
  assert.deepEqual(metricDeltaPresentation("+2.4pp", presentation), {
    visible: true,
    text: "+2.4pp",
    reason: null
  });
});

test("partial、structural-break、not-applicable 都隐藏正负趋势并保留服务端原因", async () => {
  const { buildMetricScopePresentation, metricDeltaPresentation } = await loadModel();

  for (const status of ["partial", "structural-break", "not-applicable"]) {
    const presentation = buildMetricScopePresentation({
      ...normalizedSnapshot,
      comparability_status: status,
      comparability_reason_codes: ["MAPPING_COVERAGE_GAP", "MAPPING_RECOMPUTE_REQUIRED"]
    });
    const delta = metricDeltaPresentation("-3.1pp", presentation);

    assert.equal(presentation.showDelta, false, status);
    assert.equal(delta.visible, false, status);
    assert.equal(delta.text, "涨跌已隐藏", status);
    assert.match(delta.reason, /MAPPING_COVERAGE_GAP/, status);
    assert.doesNotMatch(delta.text, /[+-]/, status);
  }
});

test("缺少权威快照或可比性字段时 fail closed，不用筛选值猜测版本口径", async () => {
  const { buildMetricScopePresentation, metricDeltaPresentation } = await loadModel();
  const missing = buildMetricScopePresentation(undefined);
  const incomplete = buildMetricScopePresentation({
    metric_result_id: "metric-incomplete",
    metric_key: "conversionProgress"
  });

  assert.equal(missing.snapshotBound, false);
  assert.equal(missing.taxonomyMode, null);
  assert.deepEqual(missing.sourceLabelVersionIds, []);
  assert.equal(missing.showDelta, false);
  assert.match(metricDeltaPresentation("+9pp", missing).reason, /未绑定不可变指标快照/);

  assert.equal(incomplete.snapshotBound, true);
  assert.equal(incomplete.showDelta, false);
  assert.match(metricDeltaPresentation("+9pp", incomplete).reason, /未返回可比性判定/);
});

test("兼容 scope.label_scope 与已物化快照顶层字段，但不补造缺失字段", async () => {
  const { buildMetricScopePresentation } = await loadModel();
  const nested = buildMetricScopePresentation({
    metric_result_id: "metric-nested",
    metric_key: "tagAssetQuality",
    scope: { label_scope: normalizedSnapshot.label_scope },
    comparability_status: "comparable",
    comparability_reason_codes: []
  });
  const topLevel = buildMetricScopePresentation({
    metric_result_id: "metric-top-level",
    metric_key: "tagAssetQuality",
    taxonomy_mode: "recomputed",
    source_label_version_ids: ["lv_18"],
    target_label_version_id: "lv_20",
    fact_set_generation: 51,
    fact_as_of: "2025-06-30T23:59:59Z",
    comparability_status: "structural-change",
    comparability_reason_codes: ["RECOMPUTE_REQUIRED"]
  });

  assert.equal(nested.taxonomyMode, "normalized");
  assert.equal(nested.factSetGeneration, 42);
  assert.equal(topLevel.taxonomyMode, "recomputed");
  assert.equal(topLevel.mappingBundleId, null);
  assert.equal(topLevel.comparabilityStatus, "structural-change");
  assert.equal(topLevel.showDelta, false);
});

test("报告只有全部不可变快照均 comparable 时才允许展示组合涨跌", async () => {
  const { buildMetricScopeSetPresentation } = await loadModel();
  const comparable = { ...normalizedSnapshot, metric_key: "metric-a" };
  const partial = {
    ...normalizedSnapshot,
    metric_key: "metric-b",
    comparability_status: "partial",
    comparability_reason_codes: ["MAPPING_COVERAGE_GAP"]
  };

  assert.equal(buildMetricScopeSetPresentation([comparable]).showDelta, true);
  const mixed = buildMetricScopeSetPresentation([comparable, partial]);
  assert.equal(mixed.showDelta, false);
  assert.match(mixed.reason, /metric-b/);
  assert.match(mixed.reason, /MAPPING_COVERAGE_GAP/);
  assert.equal(buildMetricScopeSetPresentation([]).showDelta, false);
});

test("报告执行按请求指标顺序保留完整不可变快照，拒绝缺失与重复快照", async () => {
  const { bindMaterializedMetricSnapshots } = await loadModel();
  const first = {
    ...normalizedSnapshot,
    metric_result_id: "result-a",
    metric_key: "metric-a",
    source_run_id: "run-42",
    status: "materialized",
    snapshot_role: "aggregation",
    immutable: true
  };
  const second = {
    ...normalizedSnapshot,
    metric_result_id: "result-b",
    metric_key: "metric-b",
    source_run_id: "run-42",
    status: "materialized",
    snapshot_role: "aggregation",
    immutable: true
  };
  const ordered = bindMaterializedMetricSnapshots(
    ["metric-a", "metric-b"],
    "run-42",
    ["result-b", "result-a"],
    [second, { ...first, source_run_id: "another-run" }, first]
  );

  assert.deepEqual(ordered.map((item) => item.metric_result_id), ["result-a", "result-b"]);
  assert.strictEqual(ordered[0], first);
  assert.equal(ordered[0].label_scope.fact_set_generation, 42);
  assert.throws(
    () => bindMaterializedMetricSnapshots(
      ["metric-a"],
      "run-42",
      ["result-a"],
      [first, { ...first }]
    ),
    /重复快照/
  );
  assert.throws(
    () => bindMaterializedMetricSnapshots(
      ["metric-a", "metric-b"],
      "run-42",
      ["result-a", "result-b"],
      [first]
    ),
    /缺少已物化指标/
  );
});

test("只把 BFF projection 中已物化、不可变的 aggregation 结果作为权威快照", async () => {
  const { metricSnapshotsFromProjection } = await loadModel();
  const accepted = {
    ...normalizedSnapshot,
    source_run_id: "run-42",
    status: "materialized",
    snapshot_role: "aggregation",
    immutable: true
  };
  const snapshots = metricSnapshotsFromProjection([
    accepted,
    { ...accepted, metric_result_id: "mutable", immutable: false },
    { ...accepted, metric_result_id: "outcome", snapshot_role: "outcome" },
    { ...accepted, metric_result_id: "pending", status: "pending" },
    { arbitrary: "projection-row" }
  ]);

  assert.deepEqual(snapshots, [accepted]);
  assert.deepEqual(metricSnapshotsFromProjection(undefined), []);
});
