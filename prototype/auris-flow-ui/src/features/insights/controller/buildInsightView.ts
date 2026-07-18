import type { InsightsModuleProps } from "../types";
import type { HotwordInsightsState } from "./useHotwordInsights";
import type { InsightDatasetState } from "./useInsightDataset";
import type { InsightTimeRangeState } from "./useInsightTimeRange";
import type { InsightComparisonState } from "./useInsightComparisonState";
import type { InsightMetrics } from "./buildInsightMetrics";
import type { InsightMetric, InsightTabKey, InsightViewConfig } from "../types";
import { viewDescriptors } from "../fixtures/viewDescriptors";
import type { InsightViewDescriptor } from "../fixtures/viewDescriptors";

export function buildInsightView(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics) {
  const { currentTab, metricByKey } = scope;
  const views = Object.fromEntries(
    (Object.entries(viewDescriptors) as Array<[InsightTabKey, InsightViewDescriptor]>).map(([key, descriptor]) => [
      key,
      {
        ...descriptor,
        metricKeys: [...descriptor.metricKeys],
        chartIds: [...descriptor.chartIds]
      }
    ])
  ) as Record<InsightTabKey, InsightViewConfig>;

  const view = views[currentTab];

  const visibleMetrics = view.metricKeys.map((key) => metricByKey.get(key)).filter((metric): metric is InsightMetric => Boolean(metric));

  return {
    views,
    view,
    visibleMetrics
  };
}

export type InsightView = ReturnType<typeof buildInsightView>;
