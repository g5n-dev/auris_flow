import { useMemo } from "react";
import type { InsightChartSpec } from "../types";
import type { InsightChartBuilderScope } from "./insightChartScope";
import type { InsightChartPrelude } from "./buildInsightChartPrelude";
import type { InsightBusinessChartFactory } from "./buildInsightBusinessCharts";
import type { InsightStoreSalesChartFactory } from "./buildInsightStoreSalesCharts";
import type { InsightGovernanceChartFactory } from "./buildInsightGovernanceCharts";

export function useInsightChartSpecs(scope: InsightChartBuilderScope & InsightChartPrelude & InsightBusinessChartFactory & InsightStoreSalesChartFactory & InsightGovernanceChartFactory) {
  const { buildInsightBusinessCharts, buildInsightGovernanceCharts, buildInsightStoreSalesCharts, crosstalkRisk, dataset, driveFacts, evidenceComplete, insightMetrics, metricByKey, northStarScore, objectionResolution, quoteFacts, rangeConfig, rangedConversionProgress, rangedEffectiveReceptionRate, rangedQuoteConsistency, rangedRiskReverseScore, resolvedFacts, riskFacts, tagAssetQuality, testDriveIntent, validReceptionFacts, view, visibleMetrics } = scope;
  const chartSpecs = useMemo<Record<string, InsightChartSpec>>(() => ({
    ...buildInsightBusinessCharts(),
    ...buildInsightStoreSalesCharts(),
    ...buildInsightGovernanceCharts()
  }), [dataset, evidenceComplete, insightMetrics, metricByKey, objectionResolution, rangedQuoteConsistency, testDriveIntent, crosstalkRisk, tagAssetQuality, riskFacts, quoteFacts, resolvedFacts, driveFacts, view.chartIds.length, visibleMetrics.length, rangeConfig.labels, rangeConfig.reportScope, northStarScore, rangedConversionProgress, rangedEffectiveReceptionRate, rangedRiskReverseScore, validReceptionFacts]);
  return { chartSpecs };
}

export type InsightChartSpecs = ReturnType<typeof useInsightChartSpecs>;
