import type { InsightChartBuilderScope } from "./insightChartScope";
import type { InsightTone } from "../types";

export function buildInsightChartPrelude(scope: InsightChartBuilderScope) {
  const { dataset, riskFacts } = scope;
  const firstRiskFact = riskFacts[0] ?? dataset.facts[0];
  const topTags = dataset.tagCounts.slice(0, 7);
  const topStores = dataset.storeRows.slice(0, 5);
  const topSales = dataset.salesRows.slice(0, 5);
  const qualitySeries = dataset.qualityRows.map((row) => ({
        key: row.ability,
        label: row.ability,
        tone: row.delta < 0 ? "amber" as InsightTone : "teal" as InsightTone,
        values: [row.baseline - 0.8, row.baseline - 0.4, row.baseline, row.candidate - row.delta / 2, row.candidate],
        factIds: Array(5).fill(firstRiskFact?.id)
      }));
  return { firstRiskFact, topTags, topStores, topSales, qualitySeries };
}

export type InsightChartPrelude = ReturnType<typeof buildInsightChartPrelude>;
