import type { InsightMetricSnapshot } from "../types";

type JsonRecord = Record<string, unknown>;

export type MetricScopePresentation = {
  snapshotBound: boolean;
  metricKey: string | null;
  taxonomyMode: string | null;
  sourceLabelVersionIds: string[];
  targetLabelVersionId: string | null;
  mappingBundleId: string | null;
  factSetGeneration: number | null;
  factAsOf: string | null;
  comparabilityStatus: string | null;
  comparabilityReasonCodes: string[];
  comparabilityLabel: string;
  showDelta: boolean;
  hiddenDeltaReason: string | null;
};

const asRecord = (value: unknown): JsonRecord | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : null;

const nonBlankString = (value: unknown): string | null =>
  typeof value === "string" && value.trim() ? value.trim() : null;

const stringList = (value: unknown): string[] =>
  Array.isArray(value)
    ? [...new Set(value.map(nonBlankString).filter((item): item is string => Boolean(item)))]
    : [];

const positiveInteger = (value: unknown): number | null =>
  typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;

const scopeRecord = (snapshot: JsonRecord): JsonRecord | null => {
  const direct = asRecord(snapshot.label_scope);
  if (direct) return direct;
  return asRecord(asRecord(snapshot.scope)?.label_scope);
};

const scopeValue = (
  snapshot: JsonRecord,
  scope: JsonRecord | null,
  key: string
): unknown => scope?.[key] ?? snapshot[key];

const comparabilityLabel = (status: string | null): string => {
  switch (status) {
    case "comparable":
      return "可比";
    case "partial":
    case "partially-comparable":
      return "部分可比";
    case "structural-break":
    case "structural-change":
      return "结构变化";
    case "not-applicable":
      return "不适用";
    case "non-comparable":
      return "不可比";
    default:
      return "未判定";
  }
};

const hiddenReason = (
  snapshotBound: boolean,
  status: string | null,
  reasonCodes: string[]
): string | null => {
  if (status === "comparable") return null;
  if (!snapshotBound) return "未绑定不可变指标快照，涨跌已隐藏。";
  if (!status) return "指标快照未返回可比性判定，涨跌已隐藏。";
  const reasons = reasonCodes.length ? `；原因：${reasonCodes.join("、")}` : "";
  return `可比性为 ${comparabilityLabel(status)}（${status}），涨跌已隐藏${reasons}。`;
};

export function buildMetricScopePresentation(
  snapshot: InsightMetricSnapshot | JsonRecord | null | undefined
): MetricScopePresentation {
  const record = asRecord(snapshot);
  if (!record) {
    return {
      snapshotBound: false,
      metricKey: null,
      taxonomyMode: null,
      sourceLabelVersionIds: [],
      targetLabelVersionId: null,
      mappingBundleId: null,
      factSetGeneration: null,
      factAsOf: null,
      comparabilityStatus: null,
      comparabilityReasonCodes: [],
      comparabilityLabel: "未判定",
      showDelta: false,
      hiddenDeltaReason: hiddenReason(false, null, [])
    };
  }

  const labelScope = scopeRecord(record);
  const status = nonBlankString(
    record.comparability_status ?? labelScope?.comparability_status
  );
  const reasonCodes = stringList(
    record.comparability_reason_codes ?? labelScope?.comparability_reason_codes
  );
  return {
    snapshotBound: true,
    metricKey: nonBlankString(record.metric_key),
    taxonomyMode: nonBlankString(scopeValue(record, labelScope, "taxonomy_mode")),
    sourceLabelVersionIds: stringList(
      scopeValue(record, labelScope, "source_label_version_ids")
    ),
    targetLabelVersionId: nonBlankString(
      scopeValue(record, labelScope, "target_label_version_id")
    ),
    mappingBundleId: nonBlankString(
      scopeValue(record, labelScope, "mapping_bundle_id")
    ),
    factSetGeneration: positiveInteger(
      scopeValue(record, labelScope, "fact_set_generation")
    ),
    factAsOf: nonBlankString(scopeValue(record, labelScope, "fact_as_of")),
    comparabilityStatus: status,
    comparabilityReasonCodes: reasonCodes,
    comparabilityLabel: comparabilityLabel(status),
    showDelta: status === "comparable",
    hiddenDeltaReason: hiddenReason(true, status, reasonCodes)
  };
}

export function metricDeltaPresentation(
  delta: string,
  presentation: MetricScopePresentation
): { visible: boolean; text: string; reason: string | null } {
  return presentation.showDelta
    ? { visible: true, text: delta, reason: null }
    : {
        visible: false,
        text: "涨跌已隐藏",
        reason: presentation.hiddenDeltaReason
      };
}

export function buildMetricScopeSetPresentation(
  snapshots: ReadonlyArray<InsightMetricSnapshot | JsonRecord>
): { showDelta: boolean; label: string; reason: string | null } {
  if (!snapshots.length) {
    return {
      showDelta: false,
      label: "未绑定口径",
      reason: "未绑定不可变指标快照，组合涨跌已隐藏。"
    };
  }
  const presentations = snapshots.map(buildMetricScopePresentation);
  if (presentations.every((item) => item.showDelta)) {
    return { showDelta: true, label: "全部可比", reason: null };
  }
  const reasons = presentations
    .filter((item) => !item.showDelta)
    .map((item) => `${item.metricKey ?? "未知指标"}：${item.hiddenDeltaReason}`);
  return {
    showDelta: false,
    label: "存在不可比指标",
    reason: reasons.join("；")
  };
}

export function bindMaterializedMetricSnapshots(
  requestedMetricKeys: readonly string[],
  metricRunId: string,
  runMetricResultIds: readonly string[],
  snapshots: ReadonlyArray<InsightMetricSnapshot | JsonRecord>
): InsightMetricSnapshot[] {
  const runMetricIdSet = new Set(runMetricResultIds);
  const materializedByKey = new Map<string, InsightMetricSnapshot>();
  for (const snapshot of snapshots) {
    const record = asRecord(snapshot);
    const metricResultId = nonBlankString(record?.metric_result_id);
    const metricKey = nonBlankString(record?.metric_key);
    if (
      !record ||
      !metricResultId ||
      !metricKey ||
      record.source_run_id !== metricRunId ||
      !runMetricIdSet.has(metricResultId) ||
      record.status !== "materialized" ||
      record.snapshot_role !== "aggregation" ||
      record.immutable !== true
    ) {
      continue;
    }
    if (materializedByKey.has(metricKey)) {
      throw new Error(`聚合运行 ${metricRunId} 为指标 ${metricKey} 返回了重复快照。`);
    }
    materializedByKey.set(metricKey, snapshot as InsightMetricSnapshot);
  }

  const missingMetricKeys = requestedMetricKeys.filter(
    (metricKey) => !materializedByKey.has(metricKey)
  );
  if (missingMetricKeys.length) {
    throw new Error(`聚合运行缺少已物化指标：${missingMetricKeys.join("、")}。`);
  }
  const ordered = requestedMetricKeys.map((metricKey) => materializedByKey.get(metricKey)!);
  const orderedIds = ordered.map((item) => item.metric_result_id);
  if (
    orderedIds.length !== runMetricResultIds.length ||
    runMetricResultIds.some((id) => !orderedIds.includes(id))
  ) {
    throw new Error("run 返回的 metric_result_ids 与物化指标查询结果不一致。");
  }
  return ordered;
}

export function metricSnapshotsFromProjection(
  items: readonly unknown[] | undefined
): InsightMetricSnapshot[] {
  if (!items) return [];
  return items.filter((item): item is InsightMetricSnapshot => {
    const record = asRecord(item);
    return Boolean(
      record &&
      nonBlankString(record.metric_result_id) &&
      nonBlankString(record.metric_key) &&
      record.status === "materialized" &&
      record.snapshot_role === "aggregation" &&
      record.immutable === true
    );
  });
}
