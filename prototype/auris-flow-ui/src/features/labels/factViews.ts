import type { BackendActionReceipt } from "../../api/client";
import { isRecordValue } from "../../shared/runtime/records";

export const displayLabelFactValue = (value: unknown) => {
  if (value === null || value === undefined) return "unknown";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return "结构化值";
  }
};

export const backendEvaluationMetricRows = (receipt: BackendActionReceipt | null): Array<[string, string, string, string, string]> => {
  if (!receipt) return [];
  const result = isRecordValue(receipt.raw.result) ? receipt.raw.result : receipt.raw;
  const rawMetrics = result.metrics;
  const baseline = isRecordValue(result.baseline_metrics) ? result.baseline_metrics : {};
  const normalizeVerdict = (value: unknown): string => {
    const normalized = String(value ?? "").toLowerCase();
    if (["pass", "passed", "success", "通过", "true"].includes(normalized)) return "通过";
    if (["block", "blocked", "fail", "failed", "阻断", "false"].includes(normalized)) return "阻断";
    return "观察";
  };
  const format = (value: unknown) => value === undefined || value === null ? "—" : String(value);
  if (Array.isArray(rawMetrics)) {
    return rawMetrics.flatMap((item) => {
      if (!isRecordValue(item)) return [];
      const name = String(item.metric ?? item.key ?? item.name ?? "").trim();
      if (!name) return [];
      return [[
        name,
        format(item.current ?? item.baseline ?? item.baseline_value),
        format(item.candidate ?? item.value ?? item.candidate_value),
        format(item.delta),
        normalizeVerdict(item.verdict ?? item.status ?? item.passed)
      ] as [string, string, string, string, string]];
    });
  }
  if (!isRecordValue(rawMetrics)) return [];
  return Object.entries(rawMetrics).flatMap(([name, rawValue]) => {
    const value = isRecordValue(rawValue) ? rawValue.value ?? rawValue.candidate : rawValue;
    const verdict = isRecordValue(rawValue) ? rawValue.verdict ?? rawValue.status ?? rawValue.passed : undefined;
    const current = isRecordValue(rawValue) ? rawValue.current ?? rawValue.baseline : baseline[name];
    const delta = isRecordValue(rawValue) ? rawValue.delta : undefined;
    return [[name, format(current), format(value), format(delta), normalizeVerdict(verdict)] as [string, string, string, string, string]];
  });
};
