import type { WorkspaceModuleProjectionReceipt } from "../shared/contracts/moduleWorkspaceGateway";
import type { ModuleKey } from "../shared/contracts/navigation";
import type { ModuleMetric, ProjectionDisplayState } from "../shared/contracts/modules";
import { isRecordValue } from "../shared/runtime/records";

export function routeHomeMetric(metric: ModuleMetric) {
  if (metric.label.includes("音频")) return "data";
  if (metric.label.includes("复核")) return "listening";
  if (metric.label.includes("资产")) return "assets";
  if (metric.label.includes("模型")) return "evaluation";
  if (metric.label.includes("项目")) return "projects";
  if (metric.label.includes("自动")) return "evaluation";
  return "home";
}

export function formatProjectionValue(value: unknown, suffix = "") {
  if (typeof value === "number") {
    return `${value.toLocaleString("zh-CN", { maximumFractionDigits: 1 })}${suffix}`;
  }
  if (typeof value === "string" && value.trim()) {
    return value.endsWith(suffix) ? value : `${value}${suffix}`;
  }
  return undefined;
}

export function opsMetricProjection(
  raw: Record<string, unknown>,
  metricKey: string,
  fallbackKey: string,
  suffix = ""
) {
  const metric = Array.isArray(raw.metrics)
    ? raw.metrics.find(
        (item) => isRecordValue(item) && item.metric_key === metricKey
      )
    : undefined;
  const candidate = isRecordValue(metric) ? metric.value : raw[fallbackKey];
  const value = formatProjectionValue(candidate, suffix);
  const serverDelta = isRecordValue(metric) && typeof metric.delta === "string" && metric.delta.trim()
    ? metric.delta
    : "BFF projection";
  return value ? { value, delta: `${serverDelta} · 后端投影` } : undefined;
}

export function projectedItemsCount(raw: unknown) {
  if (!isRecordValue(raw) || !Array.isArray(raw.items)) return undefined;
  return raw.items.length.toLocaleString("zh-CN");
}

export function projectedMetricValue(raw: unknown, metricKeys: string[]) {
  if (!isRecordValue(raw)) return undefined;
  const metrics = Array.isArray(raw.metrics) ? raw.metrics : Array.isArray(raw.items) ? raw.items : [];
  const firstMetric = metrics.find(
    (item) => isRecordValue(item) && metricKeys.includes(String(item.metric_key ?? item.key ?? item.id ?? ""))
  );
  const value = firstMetric?.value;
  return formatProjectionValue(value);
}

export function projectedStatusCount(raw: unknown, statuses: string[]) {
  if (!isRecordValue(raw) || !Array.isArray(raw.items)) return undefined;
  const normalizedStatuses = raw.items
    .filter(isRecordValue)
    .map((item) => typeof item.status === "string" ? item.status.toLowerCase() : undefined)
    .filter((status): status is string => Boolean(status));
  if (normalizedStatuses.length === 0) return undefined;
  return normalizedStatuses.filter((status) => statuses.includes(status)).length.toLocaleString("zh-CN");
}

export function projectedAggregateCount(raw: unknown) {
  if (!isRecordValue(raw) || !Array.isArray(raw.items)) return undefined;
  const counts = raw.items.map((item) => {
    if (!isRecordValue(item)) return undefined;
    const value = item.count ?? item.total;
    return typeof value === "number" && Number.isFinite(value) ? value : undefined;
  });
  if (counts.length === 0 || counts.some((value) => value === undefined)) return undefined;
  return counts.reduce<number>((sum, value) => sum + (value ?? 0), 0).toLocaleString("zh-CN");
}

export function deriveProjectionMetrics(
  moduleKey: Exclude<ModuleKey, "listening">,
  fallbackMetrics: ModuleMetric[],
  projectionReceipt: WorkspaceModuleProjectionReceipt | null,
  projectionStatus: ProjectionDisplayState
): ModuleMetric[] {
  if (projectionStatus === "pending") {
    return fallbackMetrics.map((metric) => ({ ...metric, value: "—", delta: "正在读取 BFF projection" }));
  }
  if (projectionStatus === "empty") {
    return fallbackMetrics.map((metric) => ({ ...metric, value: "—", delta: "BFF 空数据 · 后端投影" }));
  }
  if (projectionStatus === "degraded" || !projectionReceipt || !isRecordValue(projectionReceipt.raw)) {
    return fallbackMetrics.map((metric) => ({ ...metric, delta: `${metric.delta} · Mock fixture · 降级` }));
  }

  const raw = projectionReceipt.raw;
  const itemCount = projectedItemsCount(raw);
  const unavailableMetric = (metric: ModuleMetric): ModuleMetric => ({
    ...metric,
    value: "—",
    delta: "BFF 未提供此指标 · 后端投影"
  });
  const patchByLabel = (labelMatch: string, value?: string) =>
    fallbackMetrics.map((metric) =>
      value !== undefined && metric.label.includes(labelMatch)
        ? { ...metric, value, delta: "BFF 列表计数 · 后端投影" }
        : unavailableMetric(metric)
    );

  if (moduleKey === "home") {
    return fallbackMetrics.map((metric) => {
      if (metric.label.includes("项目数")) {
        const projection = opsMetricProjection(raw, "projects", "project_count");
        return projection ? { ...metric, ...projection } : unavailableMetric(metric);
      }
      if (metric.label.includes("今日音频")) {
        const projection = opsMetricProjection(raw, "today_audio", "audio_count");
        return projection ? { ...metric, ...projection } : unavailableMetric(metric);
      }
      if (metric.label.includes("自动通过率")) {
        const projection = opsMetricProjection(raw, "auto_pass_rate", "auto_pass_rate", "%");
        return projection ? { ...metric, ...projection } : unavailableMetric(metric);
      }
      if (metric.label.includes("待人工")) {
        const projection = opsMetricProjection(raw, "human_review", "pending_count");
        return projection ? { ...metric, ...projection } : unavailableMetric(metric);
      }
      if (metric.label.includes("异常资产")) {
        const projection = opsMetricProjection(raw, "asset_risk", "anomaly_count");
        return projection ? { ...metric, ...projection } : unavailableMetric(metric);
      }
      if (metric.label.includes("模型异常")) {
        const projection = opsMetricProjection(raw, "model_anomaly", "model_anomaly_count");
        return projection ? { ...metric, ...projection } : unavailableMetric(metric);
      }
      return unavailableMetric(metric);
    });
  }

  if (moduleKey === "insights") {
    // Top-level projection cards cannot prove comparable_series + evidence_refs.
    // The insights controller performs that full validation before rendering values.
    return fallbackMetrics.map(unavailableMetric);
  }
  if (moduleKey === "canvas" && itemCount) return patchByLabel("编排版本", itemCount);
  if (moduleKey === "data") return patchByLabel("音频片段", projectedAggregateCount(raw));
  if (moduleKey === "assets" && itemCount) return patchByLabel("数据资产", itemCount);
  if (moduleKey === "tenants") return patchByLabel("活跃租户", projectedStatusCount(raw, ["active"]));
  if (moduleKey === "projects") return patchByLabel("运行项目", projectedStatusCount(raw, ["active", "running"]));

  return fallbackMetrics.map(unavailableMetric);
}
