import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const sourceUrl = new URL("./authoritativeSnapshots.ts", import.meta.url);

const loadModel = async () => {
  const source = await readFile(sourceUrl, "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext }
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
};

const hash = (character) => character.repeat(64);

const metric = (key = "quoteConsistency", id = `metric-${key}`) => ({
  metric_result_id: id,
  metric_key: key,
  value: 86.2,
  unit: "%",
  sample_size: 128,
  status: "materialized",
  source_run_id: "run-current",
  snapshot_role: "aggregation",
  immutable: true,
  content_sha256: hash("1"),
  scope_sha256: hash("2"),
  source_manifest_sha256: hash("3"),
  comparison: {
    comparison_status: "structural-break",
    reason_codes: ["FACT_SET_GENERATION_CHANGED"],
    comparison_sha256: hash("4"),
    continuous_trend_allowed: false
  }
});

const completeMetric = (key = "quoteConsistency", id = `metric-${key}`) => ({
  ...metric(key, id),
  evidence_refs: ["evidence://audio/session-1", "evidence://document/quote-1"],
  comparable_series: [
    {
      metric_result_id: `${id}-baseline`,
      value: 83.1,
      scope_sha256: hash("5"),
      evidence_refs: ["evidence://audio/session-0"]
    },
    {
      metric_result_id: id,
      value: 86.2,
      scope_sha256: hash("2"),
      evidence_refs: ["evidence://audio/session-1"]
    }
  ],
  comparison: {
    comparison_status: "comparable",
    reason_codes: [],
    comparison_sha256: hash("4"),
    continuous_trend_allowed: true
  }
});

test("权威洞察展示必须同时具备完整快照、可比序列和证据引用", async () => {
  const { authoritativeInsightDisplayState } = await loadModel();
  assert.deepEqual(
    authoritativeInsightDisplayState([completeMetric()], ["quoteConsistency"]),
    { ready: true, reason: null }
  );

  const missingEvidence = completeMetric();
  delete missingEvidence.evidence_refs;
  assert.deepEqual(
    authoritativeInsightDisplayState([missingEvidence], ["quoteConsistency"]),
    { ready: false, reason: "指标 quoteConsistency 缺少 evidence_refs。" }
  );

  const missingSeries = completeMetric();
  delete missingSeries.comparable_series;
  assert.deepEqual(
    authoritativeInsightDisplayState([missingSeries], ["quoteConsistency"]),
    { ready: false, reason: "指标 quoteConsistency 缺少 comparable_series。" }
  );

  assert.deepEqual(
    authoritativeInsightDisplayState([completeMetric()], ["quoteConsistency", "riskReverseScore"]),
    { ready: false, reason: "缺少指标 riskReverseScore 的权威快照。" }
  );
});

test("BFF current 快照的 value/unit/sample 来自同一物化对象，重复即阻断", async () => {
  const { parseAuthoritativeMetricSnapshots, snapshotValuePresentation } = await loadModel();
  const snapshot = parseAuthoritativeMetricSnapshots([metric()])[0];
  assert.deepEqual(snapshotValuePresentation(snapshot), {
    value: 86.2,
    unit: "%",
    sampleSize: 128,
    text: "86.2%"
  });
  assert.throws(
    () => parseAuthoritativeMetricSnapshots([metric(), metric("quoteConsistency", "duplicate")]),
    /重复 current 快照/
  );
  assert.throws(
    () => parseAuthoritativeMetricSnapshots([{ ...metric(), sample_size: 0 }]),
    /sample_size/
  );
  for (const hashField of ["content_sha256", "scope_sha256", "source_manifest_sha256"]) {
    const incomplete = metric();
    delete incomplete[hashField];
    assert.throws(
      () => parseAuthoritativeMetricSnapshots([incomplete]),
      new RegExp(hashField)
    );
  }
});

test("标签 current 快照必须携带完整强 label_scope 锚点", async () => {
  const { parseAuthoritativeMetricSnapshots } = await loadModel();
  const strongMetric = {
    ...metric(),
    label_version_applicability: "required",
    label_scope: {
      taxonomy_mode: "normalized",
      source_label_version_ids: ["label-v1"],
      target_label_version_id: "label-v2",
      mapping_bundle_id: "mapping-v2",
      mapping_bundle_sha256: hash("5"),
      fact_namespace: "production",
      fact_set_id: "fact-set-v2",
      fact_set_manifest_sha256: hash("6"),
      fact_set_generation: 2,
      fact_as_of: "2026-07-18T10:00:00Z",
      metric_definition_versions: { quoteConsistency: "metric/3" },
      timezone: "Asia/Shanghai",
      period_boundary: "calendar-month:[start,end)",
      denominator_definition: "eligible business events"
    }
  };
  assert.equal(parseAuthoritativeMetricSnapshots([strongMetric]).length, 1);

  const noScope = { ...strongMetric };
  delete noScope.label_scope;
  assert.throws(() => parseAuthoritativeMetricSnapshots([noScope]), /强 label_scope/);

  for (const field of [
    "source_label_version_ids",
    "fact_namespace",
    "fact_set_id",
    "fact_set_manifest_sha256",
    "fact_set_generation",
    "fact_as_of",
    "metric_definition_versions",
    "timezone",
    "period_boundary",
    "denominator_definition",
    "mapping_bundle_sha256"
  ]) {
    const incomplete = {
      ...strongMetric,
      label_scope: { ...strongMetric.label_scope }
    };
    delete incomplete.label_scope[field];
    assert.throws(
      () => parseAuthoritativeMetricSnapshots([incomplete]),
      new RegExp(field)
    );
  }
});

test("显式 N/A 与真实数值 0 严格区分，未知或无原因的 null 不放行", async () => {
  const {
    parseAuthoritativeMetricSnapshots,
    snapshotAllowsContinuousTrend,
    snapshotValuePresentation
  } = await loadModel();
  const unavailable = {
    ...metric(),
    value: null,
    sample_size: 0,
    result_status: "zero-denominator",
    reason_codes: ["ZERO_DENOMINATOR"],
    comparability_status: "not-applicable",
    comparability_reason_codes: ["ZERO_DENOMINATOR"],
    comparison: {
      comparison_status: "structural-break",
      reason_codes: ["ZERO_DENOMINATOR"],
      comparison_sha256: hash("4"),
      continuous_trend_allowed: false
    }
  };
  const parsedUnavailable = parseAuthoritativeMetricSnapshots([unavailable])[0];
  assert.deepEqual(snapshotValuePresentation(parsedUnavailable), {
    value: null,
    unit: "%",
    sampleSize: 0,
    text: "N/A"
  });
  assert.equal(snapshotAllowsContinuousTrend(parsedUnavailable), false);

  const numericZero = { ...metric(), value: 0, sample_size: 128, result_status: "value" };
  assert.equal(
    snapshotValuePresentation(parseAuthoritativeMetricSnapshots([numericZero])[0]).text,
    "0%"
  );
  assert.throws(
    () => parseAuthoritativeMetricSnapshots([{
      ...unavailable,
      reason_codes: [],
      comparability_reason_codes: []
    }]),
    /有限数值 value/
  );
  assert.throws(
    () => parseAuthoritativeMetricSnapshots([{
      ...unavailable,
      reason_codes: ["FREE_FORM_REASON"]
    }]),
    /有限数值 value/
  );
});

test("没有逐点 scope/comparison 时，即便单个 comparison comparable 也不连线", async () => {
  const { snapshotAllowsContinuousTrend } = await loadModel();
  const comparable = {
    ...metric(),
    comparison: {
      comparison_status: "comparable",
      reason_codes: [],
      comparison_sha256: hash("4"),
      continuous_trend_allowed: true
    }
  };
  assert.equal(snapshotAllowsContinuousTrend(comparable), false);
  assert.equal(snapshotAllowsContinuousTrend({
    ...comparable,
    trend_points: [
      { metric_result_id: "metric-a", scope_sha256: hash("a"), comparison_sha256: hash("b") },
      { metric_result_id: "metric-b", scope_sha256: hash("c"), comparison_sha256: hash("d") }
    ]
  }), true);
});

const reportResource = () => {
  const snapshot = metric();
  return {
    report_id: "report-1",
    status: "generated",
    metric_result_ids: [snapshot.metric_result_id],
    metric_results: [snapshot],
    metric_scope_sha256: hash("a"),
    report_metric_binding_content_sha256: hash("b"),
    report_metric_binding_id: "binding-1",
    report_document: {
      schema_version: "auris.insight-report.v2",
      report_id: "report-1",
      title: "服务端冻结标题",
      metric_results: [{
        metric_result_id: snapshot.metric_result_id,
        metric_key: snapshot.metric_key,
        value: snapshot.value,
        unit: snapshot.unit,
        sample_size: snapshot.sample_size
      }],
      sections: [{
        order: 1,
        title: "服务端章节",
        summary: "服务端冻结正文覆盖 fixture。",
        metric_result_ids: [snapshot.metric_result_id]
      }]
    }
  };
};

test("generated 报告只采用服务端冻结正文和精确有序绑定", async () => {
  const { parseGeneratedReportResource } = await loadModel();
  const parsed = parseGeneratedReportResource(reportResource(), {
    id: "report-1",
    metricResultIds: ["metric-quoteConsistency"]
  });
  assert.equal(parsed.title, "服务端冻结标题");
  assert.equal(parsed.sections[0].body, "服务端冻结正文覆盖 fixture。");
  assert.equal(parsed.reportMetricBinding.contentSha256, hash("b"));

  const badOrder = reportResource();
  badOrder.metric_result_ids = ["metric-other", "metric-quoteConsistency"];
  assert.throws(
    () => parseGeneratedReportResource(badOrder, {
      id: "report-1",
      metricResultIds: ["metric-quoteConsistency"]
    }),
    /冻结顺序不一致/
  );
  const badBinding = reportResource();
  badBinding.report_metric_binding_content_sha256 = "not-a-hash";
  assert.throws(
    () => parseGeneratedReportResource(badBinding, {
      id: "report-1",
      metricResultIds: ["metric-quoteConsistency"]
    }),
    /SHA-256/
  );

  const unavailableReport = reportResource();
  const projected = unavailableReport.metric_results[0];
  Object.assign(projected, {
    value: null,
    sample_size: 0,
    result_status: "zero-denominator",
    reason_codes: ["ZERO_DENOMINATOR"],
    comparability_status: "not-applicable",
    comparability_reason_codes: ["ZERO_DENOMINATOR"]
  });
  Object.assign(unavailableReport.report_document.metric_results[0], {
    value: null,
    sample_size: 0,
    result_status: "zero-denominator",
    reason_codes: ["ZERO_DENOMINATOR"],
    comparability_status: "not-applicable",
    comparability_reason_codes: ["ZERO_DENOMINATOR"]
  });
  assert.equal(
    parseGeneratedReportResource(unavailableReport, {
      id: "report-1",
      metricResultIds: ["metric-quoteConsistency"]
    }).metricSnapshots[0].value,
    null
  );
});
