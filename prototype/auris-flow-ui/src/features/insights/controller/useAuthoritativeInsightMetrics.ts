import { getMaterializedInsightMetrics } from "../../../api/client";
import { useEffect, useMemo, useState } from "react";
import type { InsightsModuleProps } from "../types";
import type { InsightDatasetState } from "./useInsightDataset";
import type { InsightTimeRangeState } from "./useInsightTimeRange";
import type { InsightComparisonState } from "./useInsightComparisonState";
import {
  parseAuthoritativeMetricSnapshots
} from "../model/authoritativeSnapshots";
import type { InsightMetricSnapshot } from "../types";

type AuthoritativeMetricState = {
  status: "loading" | "ready" | "error";
  snapshots: InsightMetricSnapshot[];
  error: string | null;
};

export function useAuthoritativeInsightMetrics(
  scope: InsightsModuleProps & InsightDatasetState & InsightTimeRangeState & InsightComparisonState
) {
  const {
    customRangeDraft,
    dataset,
    labelVersionFilter,
    metricProjectionItems,
    storeFilter,
    timeRange
  } = scope;
  const initialState = (): AuthoritativeMetricState => {
    if (!metricProjectionItems) return { status: "loading", snapshots: [], error: null };
    try {
      return {
        status: "ready",
        snapshots: parseAuthoritativeMetricSnapshots(metricProjectionItems),
        error: null
      };
    } catch (error) {
      return {
        status: "error",
        snapshots: [],
        error: error instanceof Error ? error.message : "BFF 指标快照解析失败。"
      };
    }
  };
  const [state, setState] = useState<AuthoritativeMetricState>(initialState);
  const effectiveTimeRange = timeRange === "custom"
    ? `${customRangeDraft.startDate}/${customRangeDraft.endDate}`
    : timeRange;

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading", snapshots: [], error: null });
    void getMaterializedInsightMetrics({
      time_range: effectiveTimeRange,
      store_id: storeFilter,
      model_version: dataset.context.model,
      label_version: labelVersionFilter,
      limit: 50
    }).then((receipt) => {
      if (cancelled) return;
      const snapshots = parseAuthoritativeMetricSnapshots(receipt.data.items);
      if (!snapshots.length) throw new Error("当前筛选范围缺少明确的 current 指标快照。");
      setState({ status: "ready", snapshots, error: null });
    }).catch((error) => {
      if (cancelled) return;
      setState({
        status: "error",
        snapshots: [],
        error: error instanceof Error ? error.message : "BFF 指标快照读取失败。"
      });
    });
    return () => { cancelled = true; };
  }, [dataset.context.model, effectiveTimeRange, labelVersionFilter, storeFilter]);

  const snapshotByMetricKey = useMemo(
    () => new Map(state.snapshots.map((snapshot) => [snapshot.metric_key, snapshot])),
    [state.snapshots]
  );
  return {
    authoritativeMetricStatus: state.status,
    authoritativeMetricError: state.error,
    authoritativeMetricSnapshots: state.snapshots,
    snapshotByMetricKey
  };
}

export type AuthoritativeInsightMetrics = ReturnType<typeof useAuthoritativeInsightMetrics>;
