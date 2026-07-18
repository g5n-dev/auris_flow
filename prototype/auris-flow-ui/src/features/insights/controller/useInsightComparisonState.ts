import type { InsightsModuleProps } from "../types";
import type { HotwordInsightsState } from "./useHotwordInsights";
import type { InsightDatasetState } from "./useInsightDataset";
import type { InsightTimeRangeState } from "./useInsightTimeRange";
import type { InsightTimeRange } from "../types";
import { useEffect, useMemo, useState } from "react";

export function useInsightComparisonState(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState) {
  const { clampScore, conversionProgress, dataset, effectiveReceptionRate, insightTimeRanges, quoteConsistency, riskReverseScore, timeRange } = scope;
  const [comparisonScope, setComparisonScope] = useState("同城均值");

  const [storeFilter, setStoreFilter] = useState(dataset.context.store);

  const [salesFilter, setSalesFilter] = useState("全部销售");

  const [labelVersionFilter, setLabelVersionFilter] = useState(dataset.context.label);

  useEffect(() => {
      setStoreFilter(dataset.context.store);
      setLabelVersionFilter(dataset.context.label);
    }, [dataset.context.label, dataset.context.store]);

  const rangeConfig = insightTimeRanges.find((item) => item.key === timeRange) ?? insightTimeRanges[2];

  const rangeDeltas = useMemo<Record<InsightTimeRange, Record<string, number>>>(() => ({
      today: { effectiveReceptionRate: -3.1, conversionProgress: -2.4, quoteConsistency: -4.6, riskReverseScore: -2.8, objectionResolution: -3.4, testDriveIntent: -1.8, tagAssetQuality: -1.2, modelQuality: -0.6 },
      "7d": { effectiveReceptionRate: 1.8, conversionProgress: 2.2, quoteConsistency: -1.7, riskReverseScore: -1.1, objectionResolution: 2.4, testDriveIntent: 1.6, tagAssetQuality: 1.3, modelQuality: 0.2 },
      "30d": { effectiveReceptionRate: 0, conversionProgress: 0, quoteConsistency: 0, riskReverseScore: 0, objectionResolution: 0, testDriveIntent: 0, tagAssetQuality: 0, modelQuality: 0 },
      "90d": { effectiveReceptionRate: 4.8, conversionProgress: 3.7, quoteConsistency: 1.4, riskReverseScore: 2.9, objectionResolution: 4.1, testDriveIntent: 3.2, tagAssetQuality: 2.5, modelQuality: 1.1 },
      custom: { effectiveReceptionRate: 2.6, conversionProgress: 1.4, quoteConsistency: -0.8, riskReverseScore: 1.6, objectionResolution: 1.7, testDriveIntent: 2.1, tagAssetQuality: 2, modelQuality: 0.8 }
    }), []);

  const rangeValue = (metricKey: string, base: number, min = 0, max = 100) => clampScore(base + (rangeDeltas[timeRange]?.[metricKey] ?? 0), min, max);

  const buildRangeValues = (metricKey: string, base: number, min = 0, max = 100): Record<InsightTimeRange, number> =>
      insightTimeRanges.reduce((values, item) => {
        values[item.key] = clampScore(base + (rangeDeltas[item.key]?.[metricKey] ?? 0), min, max);
        return values;
      }, {} as Record<InsightTimeRange, number>);

  const rangedEffectiveReceptionRate = rangeValue("effectiveReceptionRate", effectiveReceptionRate);

  const rangedConversionProgress = rangeValue("conversionProgress", conversionProgress);

  const rangedQuoteConsistency = rangeValue("quoteConsistency", quoteConsistency);

  const rangedRiskReverseScore = rangeValue("riskReverseScore", riskReverseScore);

  const northStarScore = clampScore(
      rangedEffectiveReceptionRate * 0.3 +
      rangedConversionProgress * 0.3 +
      rangedQuoteConsistency * 0.2 +
      rangedRiskReverseScore * 0.2
    );

  const rangeDeltaText = (metricKey: string, unit = "pp") => {
      const value = rangeDeltas[timeRange]?.[metricKey] ?? 0;
      return `${value >= 0 ? "+" : ""}${value}${unit}`;
    };

  return {
    comparisonScope,
    setComparisonScope,
    storeFilter,
    setStoreFilter,
    salesFilter,
    setSalesFilter,
    labelVersionFilter,
    setLabelVersionFilter,
    rangeConfig,
    rangeDeltas,
    rangeValue,
    buildRangeValues,
    rangedEffectiveReceptionRate,
    rangedConversionProgress,
    rangedQuoteConsistency,
    rangedRiskReverseScore,
    northStarScore,
    rangeDeltaText
  };
}

export type InsightComparisonState = ReturnType<typeof useInsightComparisonState>;
