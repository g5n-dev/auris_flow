import type { InsightsModuleProps } from "../types";
import type { HotwordInsightsState } from "./useHotwordInsights";
import type { InsightDatasetState } from "./useInsightDataset";
import type { InsightTimeRangeState } from "./useInsightTimeRange";
import type { InsightComparisonState } from "./useInsightComparisonState";
import type { InsightMetrics } from "./buildInsightMetrics";
import type { InsightView } from "./buildInsightView";
import type { InsightSelectionState } from "./useInsightSelectionState";
import type { InsightEvidenceActions } from "./buildInsightEvidenceActions";
import type { InsightChartSpecs } from "./useInsightChartSpecs";
import type { InsightChartSpec, InsightFact } from "../types";

export function buildInsightChartSelection(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics & InsightView & InsightSelectionState & InsightEvidenceActions & InsightChartSpecs) {
  const { chartSpecs, customRangeDraft, dataset, hiddenChartIds, labelVersionFilter, storeFilter, timeRange, unique, view } = scope;
  const visibleChartSpecs = view.chartIds.map((id) => chartSpecs[id]).filter((spec): spec is InsightChartSpec => Boolean(spec) && !hiddenChartIds.includes(spec.id));

  const governedEvidenceRefs = (facts: InsightFact[]) => unique(facts.flatMap((fact) => {
      if (fact.amountConflict) return ["AF-128", "BJ-041"];
      if (fact.crosstalk) return ["AF-129"];
      if (fact.lowConfidence) return ["AF-131"];
      if (fact.doc.includes("SJ-028")) return ["AF-130", "SJ-028"];
      return ["AF-130"];
    }));

  const insightContextKey = [
      dataset.context.tenant,
      dataset.context.project,
      storeFilter,
      timeRange === "custom" ? `${customRangeDraft.startDate}:${customRangeDraft.endDate}` : timeRange,
      dataset.context.model,
      labelVersionFilter
    ].join("::");

  return {
    visibleChartSpecs,
    governedEvidenceRefs,
    insightContextKey
  };
}

export type InsightChartSelection = ReturnType<typeof buildInsightChartSelection>;
