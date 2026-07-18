import type { InsightsModuleProps } from "../types";
import type { HotwordInsightsState } from "./useHotwordInsights";
import type { InsightDatasetState } from "./useInsightDataset";
import type { InsightTimeRangeState } from "./useInsightTimeRange";
import type { InsightComparisonState } from "./useInsightComparisonState";
import type { InsightMetrics } from "./buildInsightMetrics";
import type { InsightView } from "./buildInsightView";
import { useEffect, useState } from "react";

export function useInsightSelectionState(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics & InsightView) {
  const { currentTab, dataset, insightMetrics, rangeConfig, view, visibleMetrics } = scope;
  const [selectedMetricKey, setSelectedMetricKey] = useState("quoteConsistency");

  const [selectedFactId, setSelectedFactId] = useState(dataset.facts[0]?.id ?? "");

  const [activeChartId, setActiveChartId] = useState(view.chartIds[0]);

  const [activeTrendSeriesKey, setActiveTrendSeriesKey] = useState("north");

  const [activeTrendPointIndex, setActiveTrendPointIndex] = useState(Math.max(rangeConfig.labels.length - 1, 0));

  const [hiddenChartIds, setHiddenChartIds] = useState<string[]>([]);

  const selectedMetric = visibleMetrics.find((metric) => metric.key === selectedMetricKey) ?? visibleMetrics[0] ?? insightMetrics[0];

  const selectedFact = dataset.facts.find((fact) => fact.id === selectedFactId) ?? dataset.facts[0];

  useEffect(() => {
      if (!view.metricKeys.includes(selectedMetricKey)) {
        setSelectedMetricKey(view.metricKeys[0]);
      }
      setActiveChartId(view.chartIds[0]);
    }, [currentTab, selectedMetricKey, view.chartIds, view.metricKeys]);

  useEffect(() => {
      setActiveTrendPointIndex(Math.max(rangeConfig.labels.length - 1, 0));
    }, [rangeConfig.labels.length]);

  useEffect(() => {
      if (!dataset.facts.some((fact) => fact.id === selectedFactId)) {
        setSelectedFactId(dataset.facts[0]?.id ?? "");
      }
    }, [dataset.facts, selectedFactId]);

  return {
    selectedMetricKey,
    setSelectedMetricKey,
    selectedFactId,
    setSelectedFactId,
    activeChartId,
    setActiveChartId,
    activeTrendSeriesKey,
    setActiveTrendSeriesKey,
    activeTrendPointIndex,
    setActiveTrendPointIndex,
    hiddenChartIds,
    setHiddenChartIds,
    selectedMetric,
    selectedFact
  };
}

export type InsightSelectionState = ReturnType<typeof useInsightSelectionState>;
