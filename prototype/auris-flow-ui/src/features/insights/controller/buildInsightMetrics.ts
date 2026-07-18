import type { InsightsModuleProps } from "../types";
import type { HotwordInsightsState } from "./useHotwordInsights";
import type { InsightDatasetState } from "./useInsightDataset";
import type { InsightTimeRangeState } from "./useInsightTimeRange";
import type { InsightComparisonState } from "./useInsightComparisonState";
import type { InsightMetric, InsightNorthStar } from "../types";
import { metricDescriptors, northStarDescriptor } from "../fixtures/viewDescriptors";
import type { InsightMetricKey, NorthStarComponentKey } from "../fixtures/viewDescriptors";

type MetricRuntime = Pick<
  InsightMetric,
  "value" | "valueNumber" | "rangeValues" | "evidenceCount" | "evidenceIds"
> & Partial<Pick<InsightMetric, "delta" | "tone">>;

type NorthStarComponentRuntime = Pick<
  InsightNorthStar["components"][number],
  "value" | "contribution"
> & Partial<Pick<InsightNorthStar["components"][number], "tone">>;

export function buildInsightMetrics(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState) {
  const { buildRangeValues, clampScore, conversionProgress, crosstalkRisk, dataset, driveFacts, effectiveReceptionRate, evidenceComplete, formatPercent, modelScore, northStarScore, objectionResolution, quoteConsistency, quoteFacts, rangeDeltaText, rangeValue, rangedConversionProgress, rangedEffectiveReceptionRate, rangedQuoteConsistency, rangedRiskReverseScore, resolvedFacts, riskFacts, riskReverseScore, tagAssetQuality, testDriveIntent, unique, validReceptionFacts } = scope;
  const metricRuntimeByKey: Record<InsightMetricKey, MetricRuntime> = {
    effectiveReceptionRate: {
      value: formatPercent(rangedEffectiveReceptionRate),
      valueNumber: rangedEffectiveReceptionRate,
      delta: rangeDeltaText("effectiveReceptionRate"),
      rangeValues: buildRangeValues("effectiveReceptionRate", effectiveReceptionRate),
      evidenceCount: validReceptionFacts.length,
      evidenceIds: validReceptionFacts.map((fact) => fact.id)
    },
    conversionProgress: {
      value: formatPercent(rangedConversionProgress),
      valueNumber: rangedConversionProgress,
      delta: rangeDeltaText("conversionProgress"),
      rangeValues: buildRangeValues("conversionProgress", conversionProgress),
      evidenceCount: resolvedFacts.length + driveFacts.length,
      evidenceIds: unique([...resolvedFacts, ...driveFacts].map((fact) => fact.id))
    },
    objectionResolution: {
      value: formatPercent(rangeValue("objectionResolution", objectionResolution)),
      valueNumber: rangeValue("objectionResolution", objectionResolution),
      delta: rangeDeltaText("objectionResolution"),
      rangeValues: buildRangeValues("objectionResolution", objectionResolution),
      evidenceCount: resolvedFacts.length,
      evidenceIds: resolvedFacts.map((fact) => fact.id)
    },
    quoteConsistency: {
      value: formatPercent(rangedQuoteConsistency),
      valueNumber: rangedQuoteConsistency,
      delta: `${rangedQuoteConsistency < 80 ? "需复核" : rangeDeltaText("quoteConsistency")}`,
      tone: rangedQuoteConsistency < 80 ? "red" : "amber",
      rangeValues: buildRangeValues("quoteConsistency", quoteConsistency),
      evidenceCount: quoteFacts.length,
      evidenceIds: quoteFacts.map((fact) => fact.id)
    },
    riskReverseScore: {
      value: formatPercent(rangedRiskReverseScore),
      valueNumber: rangedRiskReverseScore,
      delta: rangeDeltaText("riskReverseScore"),
      tone: rangedRiskReverseScore < 72 ? "red" : "blue",
      rangeValues: buildRangeValues("riskReverseScore", riskReverseScore),
      evidenceCount: riskFacts.length,
      evidenceIds: riskFacts.map((fact) => fact.id)
    },
    testDriveIntent: {
      value: formatPercent(rangeValue("testDriveIntent", testDriveIntent)),
      valueNumber: rangeValue("testDriveIntent", testDriveIntent),
      delta: rangeDeltaText("testDriveIntent"),
      rangeValues: buildRangeValues("testDriveIntent", testDriveIntent),
      evidenceCount: driveFacts.length,
      evidenceIds: driveFacts.map((fact) => fact.id)
    },
    crosstalkRisk: {
      value: `${crosstalkRisk}%`,
      valueNumber: crosstalkRisk,
      rangeValues: buildRangeValues("riskReverseScore", 100 - crosstalkRisk),
      evidenceCount: riskFacts.length,
      evidenceIds: dataset.facts.filter((fact) => fact.crosstalk || fact.lowConfidence).map((fact) => fact.id)
    },
    tagAssetQuality: {
      value: formatPercent(rangeValue("tagAssetQuality", tagAssetQuality)),
      valueNumber: rangeValue("tagAssetQuality", tagAssetQuality),
      delta: rangeDeltaText("tagAssetQuality"),
      rangeValues: buildRangeValues("tagAssetQuality", tagAssetQuality),
      evidenceCount: evidenceComplete.length,
      evidenceIds: evidenceComplete.map((fact) => fact.id)
    },
    modelQuality: {
      value: `${rangeValue("modelQuality", modelScore)}`,
      valueNumber: rangeValue("modelQuality", modelScore),
      rangeValues: buildRangeValues("modelQuality", modelScore, 0, 100),
      evidenceCount: riskFacts.length + evidenceComplete.length,
      evidenceIds: riskFacts.map((fact) => fact.id)
    }
  };
  const insightMetrics = metricDescriptors.map((descriptor) => ({
    ...descriptor,
    ...metricRuntimeByKey[descriptor.key],
    tags: [...descriptor.tags]
  } as InsightMetric));

  const componentRuntimeFor = (key: NorthStarComponentKey): NorthStarComponentRuntime => {
    switch (key) {
      case "effective":
        return {
          value: rangedEffectiveReceptionRate,
          contribution: clampScore(rangedEffectiveReceptionRate * 0.3)
        };
      case "conversion":
        return {
          value: rangedConversionProgress,
          contribution: clampScore(rangedConversionProgress * 0.3)
        };
      case "quote":
        return {
          value: rangedQuoteConsistency,
          contribution: clampScore(rangedQuoteConsistency * 0.2),
          tone: rangedQuoteConsistency < 80 ? "red" : "amber"
        };
      case "risk":
        return {
          value: rangedRiskReverseScore,
          contribution: clampScore(rangedRiskReverseScore * 0.2),
          tone: rangedRiskReverseScore < 72 ? "red" : "blue"
        };
    }
  };
  const northStar: InsightNorthStar = {
    ...northStarDescriptor,
    value: northStarScore,
    delta: rangeDeltaText("conversionProgress"),
    lift: `主要拉升来自 ${rangedConversionProgress >= rangedEffectiveReceptionRate ? "成交推进率" : "有效接待率"}，可沉淀销售训练样本。`,
    drag: rangedQuoteConsistency < rangedRiskReverseScore ? "报价一致率拖累明显，金额冲突需要优先复核。" : "风险反向分仍有压力，串音与低置信片段需要仲裁。",
    components: northStarDescriptor.components.map((descriptor) => ({
      ...descriptor,
      ...componentRuntimeFor(descriptor.key)
    } as InsightNorthStar["components"][number]))
  };

  const metricByKey = new Map(insightMetrics.map((metric) => [metric.key, metric]));

  return {
    insightMetrics,
    northStar,
    metricByKey
  };
}

export type InsightMetrics = ReturnType<typeof buildInsightMetrics>;
