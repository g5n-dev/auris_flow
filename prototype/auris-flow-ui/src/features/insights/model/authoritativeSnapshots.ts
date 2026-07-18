import type {
  InsightMetricSnapshot,
  InsightChartSpec,
  InsightReportDraft,
  InsightReportDocument
} from "../types";

type JsonRecord = Record<string, unknown>;

const asRecord = (value: unknown): JsonRecord | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : null;

const requiredString = (record: JsonRecord, key: string): string => {
  const value = record[key];
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`服务端快照缺少 ${key}。`);
  }
  return value.trim();
};

const requiredHash = (record: JsonRecord, key: string): string => {
  const value = requiredString(record, key);
  if (!/^[0-9a-f]{64}$/i.test(value)) {
    throw new Error(`服务端快照的 ${key} 不是合法 SHA-256。`);
  }
  return value.toLowerCase();
};

const requiredFiniteNumber = (record: JsonRecord, key: string): number => {
  const value = record[key];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`服务端快照缺少有限数值 ${key}。`);
  }
  return value;
};

const requiredPositiveSample = (record: JsonRecord): number => {
  const value = record.sample_size;
  if (typeof value !== "number" || !Number.isInteger(value) || value < 1) {
    throw new Error("服务端快照缺少有效 sample_size。");
  }
  return value;
};

const reasonCodes = (value: unknown): string[] => Array.isArray(value)
  ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim()))
  : [];

const governedOutcomeReasons = new Set([
  "LABEL_NOT_APPLICABLE",
  "LABEL_DEPRECATED_NOT_APPLICABLE",
  "ZERO_DENOMINATOR",
  "COVERAGE_GAP",
  "RECOMPUTE_REQUIRED",
  "FACT_SET_EMPTY",
  "SOURCE_DATA_MISSING",
  "MAPPING_RECOMPUTE_REQUIRED"
]);

const outcomeReasonsByStatus: Record<string, Set<string>> = {
  "not-applicable": new Set(["LABEL_NOT_APPLICABLE", "LABEL_DEPRECATED_NOT_APPLICABLE"]),
  "zero-denominator": new Set(["ZERO_DENOMINATOR"]),
  "coverage-gap": new Set(["COVERAGE_GAP", "SOURCE_DATA_MISSING", "FACT_SET_EMPTY"]),
  "recompute-required": new Set(["RECOMPUTE_REQUIRED", "MAPPING_RECOMPUTE_REQUIRED"])
};

const isExplicitUnavailableResult = (
  record: JsonRecord,
  comparison: JsonRecord | null
): boolean => {
  if (record.value !== null || record.sample_size !== 0) return false;
  const resultStatus = record.result_status;
  const outcomeReasons = reasonCodes(record.reason_codes);
  const allowedReasons = typeof resultStatus === "string"
    ? outcomeReasonsByStatus[resultStatus]
    : undefined;
  if (
    !allowedReasons ||
    !outcomeReasons.length ||
    outcomeReasons.some(
      (reason) => !governedOutcomeReasons.has(reason) || !allowedReasons.has(reason)
    )
  ) return false;
  const comparisonUnavailable = Boolean(
    comparison &&
    comparison.comparison_status !== "comparable" &&
    reasonCodes(comparison.reason_codes).length
  );
  const scopeUnavailable = Boolean(
    typeof record.comparability_status === "string" &&
    record.comparability_status !== "comparable" &&
    reasonCodes(record.comparability_reason_codes).length
  );
  return comparisonUnavailable || scopeUnavailable;
};

const requiredNonEmptyStringList = (record: JsonRecord, key: string): string[] => {
  const value = record[key];
  if (!Array.isArray(value) || !value.length) {
    throw new Error(`服务端标签快照缺少 ${key}。`);
  }
  const normalized = value.map((item) => {
    if (typeof item !== "string" || !item.trim()) {
      throw new Error(`服务端标签快照的 ${key} 包含无效版本 ID。`);
    }
    return item.trim();
  });
  if (new Set(normalized).size !== normalized.length) {
    throw new Error(`服务端标签快照的 ${key} 包含重复版本 ID。`);
  }
  return normalized;
};

const validateStrongLabelScope = (record: JsonRecord, metricKey: string): void => {
  if (record.label_version_applicability !== "required") return;
  const scope = asRecord(record.label_scope);
  if (!scope) throw new Error(`标签指标 ${metricKey} 缺少强 label_scope。`);
  const taxonomyMode = requiredString(scope, "taxonomy_mode");
  if (!(["native", "normalized", "recomputed"] as string[]).includes(taxonomyMode)) {
    throw new Error(`标签指标 ${metricKey} 的 taxonomy_mode 无效。`);
  }
  requiredNonEmptyStringList(scope, "source_label_version_ids");
  requiredString(scope, "fact_namespace");
  requiredString(scope, "fact_set_id");
  requiredHash(scope, "fact_set_manifest_sha256");
  const generation = scope.fact_set_generation;
  if (typeof generation !== "number" || !Number.isInteger(generation) || generation < 1) {
    throw new Error(`标签指标 ${metricKey} 缺少有效 fact_set_generation。`);
  }
  const factAsOf = requiredString(scope, "fact_as_of");
  if (
    !/(?:Z|[+-]\d{2}:\d{2})$/i.test(factAsOf) ||
    Number.isNaN(Date.parse(factAsOf))
  ) throw new Error(`标签指标 ${metricKey} 的 fact_as_of 不含可验证时区。`);
  const definitionVersions = asRecord(scope.metric_definition_versions);
  if (
    !definitionVersions ||
    !Object.keys(definitionVersions).length ||
    Object.entries(definitionVersions).some(([key, value]) => !key.trim() || typeof value !== "string" || !value.trim())
  ) throw new Error(`标签指标 ${metricKey} 缺少完整 metric_definition_versions。`);
  requiredString(scope, "timezone");
  requiredString(scope, "period_boundary");
  requiredString(scope, "denominator_definition");

  const target = scope.target_label_version_id;
  const bundleId = scope.mapping_bundle_id;
  const bundleHash = scope.mapping_bundle_sha256;
  if (taxonomyMode === "native" && (target != null || bundleId != null || bundleHash != null)) {
    throw new Error(`标签指标 ${metricKey} 的 native scope 不应绑定 target/mapping。`);
  }
  if (taxonomyMode === "normalized") {
    requiredString(scope, "target_label_version_id");
    requiredString(scope, "mapping_bundle_id");
    requiredHash(scope, "mapping_bundle_sha256");
  }
  if (taxonomyMode === "recomputed") requiredString(scope, "target_label_version_id");
};

const strictStringIds = (value: unknown, field: string): string[] => {
  if (!Array.isArray(value) || !value.length) {
    throw new Error(`服务端报告缺少 ${field}。`);
  }
  const ids = value.map((item) => {
    if (typeof item !== "string" || !item.trim()) {
      throw new Error(`服务端报告的 ${field} 包含无效 ID。`);
    }
    return item.trim();
  });
  if (new Set(ids).size !== ids.length) {
    throw new Error(`服务端报告的 ${field} 包含重复 ID。`);
  }
  return ids;
};

export function parseAuthoritativeMetricSnapshots(
  items: readonly unknown[],
  options: { requireComparison?: boolean } = {}
): InsightMetricSnapshot[] {
  const byKey = new Map<string, InsightMetricSnapshot>();
  const result: InsightMetricSnapshot[] = [];
  for (const item of items) {
    const record = asRecord(item);
    if (!record) throw new Error("BFF 返回了非对象指标快照。");
    const metricResultId = requiredString(record, "metric_result_id");
    const metricKey = requiredString(record, "metric_key");
    if (
      record.status !== "materialized" ||
      record.snapshot_role !== "aggregation" ||
      record.immutable !== true
    ) {
      throw new Error(`指标 ${metricKey} 不是不可变 materialized aggregation 快照。`);
    }
    const comparison = asRecord(record.comparison);
    const unavailable = isExplicitUnavailableResult(record, comparison);
    if (!unavailable) requiredFiniteNumber(record, "value");
    requiredString(record, "unit");
    if (!unavailable) requiredPositiveSample(record);
    requiredString(record, "source_run_id");
    requiredHash(record, "content_sha256");
    requiredHash(record, "scope_sha256");
    requiredHash(record, "source_manifest_sha256");
    validateStrongLabelScope(record, metricKey);
    if (byKey.has(metricKey)) {
      throw new Error(`BFF 为指标 ${metricKey} 返回了重复 current 快照。`);
    }
    if (options.requireComparison !== false) {
      if (!comparison) throw new Error(`指标 ${metricKey} 缺少服务端双快照 comparison。`);
      requiredString(comparison, "comparison_status");
      requiredHash(comparison, "comparison_sha256");
    }
    const snapshot = { ...record, metric_result_id: metricResultId, metric_key: metricKey } as InsightMetricSnapshot;
    byKey.set(metricKey, snapshot);
    result.push(snapshot);
  }
  return result;
}

export function snapshotValuePresentation(snapshot: InsightMetricSnapshot): {
  value: number | null;
  unit: string;
  sampleSize: number;
  text: string;
} {
  const record = snapshot as JsonRecord;
  const comparison = asRecord(record.comparison);
  if (isExplicitUnavailableResult(record, comparison)) {
    return { value: null, unit: requiredString(record, "unit"), sampleSize: 0, text: "N/A" };
  }
  const value = requiredFiniteNumber(record, "value");
  const unit = requiredString(record, "unit");
  const sampleSize = requiredPositiveSample(record);
  const compactUnit = unit === "percent" || unit === "%" ? "%" : ` ${unit}`;
  return { value, unit, sampleSize, text: `${value}${compactUnit}` };
}

export function snapshotAllowsContinuousTrend(snapshot: InsightMetricSnapshot): boolean {
  const record = snapshot as JsonRecord;
  if (record.result_status != null && record.result_status !== "value") return false;
  const comparison = asRecord(record.comparison);
  if (
    comparison?.comparison_status !== "comparable" ||
    comparison.continuous_trend_allowed !== true
  ) return false;
  const points = record.trend_points;
  return Array.isArray(points) && points.length > 1 && points.every((point) => {
    const item = asRecord(point);
    return Boolean(
      item &&
      typeof item.metric_result_id === "string" &&
      /^[0-9a-f]{64}$/i.test(String(item.scope_sha256 ?? "")) &&
      /^[0-9a-f]{64}$/i.test(String(item.comparison_sha256 ?? ""))
    );
  });
}

export const chartMetricKeys = (chart: InsightChartSpec): string[] => [
  ...new Set(
    chart.metricKeys?.length
      ? chart.metricKeys
      : (chart.series ?? []).map((series) => series.metricKey).filter((key): key is string => Boolean(key))
  )
];

export const chartPairComparable = (
  chart: InsightChartSpec,
  metricSnapshotByKey: Map<string, InsightMetricSnapshot>
): boolean => {
  const keys = chartMetricKeys(chart);
  return Boolean(keys.length) && keys.every((key) => {
    const comparison = metricSnapshotByKey.get(key)?.comparison;
    return Boolean(
      comparison &&
      typeof comparison === "object" &&
      (comparison as Record<string, unknown>).comparison_status === "comparable" &&
      (comparison as Record<string, unknown>).continuous_trend_allowed === true
    );
  });
};

export function parseGeneratedReportResource(
  raw: unknown,
  expected: Pick<InsightReportDraft, "id" | "metricResultIds">
): Pick<
  InsightReportDraft,
  | "title"
  | "summary"
  | "sections"
  | "metricResultIds"
  | "metricSnapshots"
  | "authoritativeReportDocument"
  | "reportMetricBinding"
> {
  const record = asRecord(raw);
  if (!record || record.status !== "generated") {
    throw new Error("报告尚未进入 generated，不能读取冻结正文。");
  }
  const reportId = requiredString(record, "report_id");
  if (reportId !== expected.id) throw new Error("报告 GET 返回了错误 report_id。");
  const metricResultIds = strictStringIds(record.metric_result_ids, "metric_result_ids");
  if (
    !expected.metricResultIds ||
    metricResultIds.length !== expected.metricResultIds.length ||
    metricResultIds.some((id, index) => id !== expected.metricResultIds?.[index])
  ) {
    throw new Error("报告 GET 的 metric_result_ids 与创建时冻结顺序不一致。");
  }

  const metricResults = Array.isArray(record.metric_results) ? record.metric_results : [];
  metricResults.forEach((item) => {
    const metric = asRecord(item);
    if (!metric) throw new Error("报告 GET 包含无效 MetricResult。");
    requiredHash(metric, "content_sha256");
    requiredHash(metric, "scope_sha256");
    requiredHash(metric, "source_manifest_sha256");
  });
  const metricSnapshots = parseAuthoritativeMetricSnapshots(
    metricResults,
    { requireComparison: false }
  );
  const projectedIds = metricSnapshots.map((item) => item.metric_result_id);
  if (
    projectedIds.length !== metricResultIds.length ||
    projectedIds.some((id, index) => id !== metricResultIds[index])
  ) throw new Error("报告 GET 的 metric_results 缺失、重复或乱序。");

  const document = asRecord(record.report_document);
  if (!document || document.schema_version !== "auris.insight-report.v2") {
    throw new Error("报告 GET 缺少合法 report_document。");
  }
  if (requiredString(document, "report_id") !== reportId) {
    throw new Error("report_document 与报告资源 ID 不一致。");
  }
  const documentMetrics = Array.isArray(document.metric_results) ? document.metric_results : [];
  const documentIds = strictStringIds(
    documentMetrics.map((item) => asRecord(item)?.metric_result_id),
    "report_document.metric_results"
  );
  if (
    documentIds.length !== metricResultIds.length ||
    documentIds.some((id, index) => id !== metricResultIds[index])
  ) throw new Error("report_document 指标缺失、重复或乱序。");

  documentMetrics.forEach((item, index) => {
    const frozen = asRecord(item);
    const projected = metricSnapshots[index] as JsonRecord;
    if (!frozen) throw new Error("report_document 包含无效指标快照。");
    for (const field of [
      "metric_key",
      "value",
      "unit",
      "sample_size",
      "result_status",
      "reason_codes",
      "comparability_status",
      "comparability_reason_codes"
    ] as const) {
      if (JSON.stringify(frozen[field]) !== JSON.stringify(projected[field])) {
        throw new Error(`report_document 的 ${field} 与绑定 MetricResult 不一致。`);
      }
    }
  });

  const rawSections = Array.isArray(document.sections) ? document.sections : [];
  if (!rawSections.length) throw new Error("report_document 缺少冻结正文 sections。");
  const sections = rawSections.map((item, index) => {
    const section = asRecord(item);
    if (!section || section.order !== index + 1) {
      throw new Error("report_document sections 顺序不连续。");
    }
    const ids = strictStringIds(section.metric_result_ids, "section.metric_result_ids");
    if (ids.some((id, itemIndex) => id !== metricResultIds[itemIndex])) {
      throw new Error("report_document section 的指标绑定顺序漂移。");
    }
    return {
      title: requiredString(section, "title"),
      body: requiredString(section, "summary")
    };
  });

  const metricScopeSha256 = requiredHash(record, "metric_scope_sha256");
  const contentSha256 = requiredHash(record, "report_metric_binding_content_sha256");
  const bindingId = requiredString(record, "report_metric_binding_id");
  return {
    title: requiredString(document, "title"),
    summary: sections[0].body,
    sections,
    metricResultIds,
    metricSnapshots,
    authoritativeReportDocument: document as InsightReportDocument,
    reportMetricBinding: {
      bindingId,
      contentSha256,
      metricScopeSha256
    }
  };
}
