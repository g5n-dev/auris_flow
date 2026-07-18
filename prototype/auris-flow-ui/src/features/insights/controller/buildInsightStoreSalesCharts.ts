import { chartDescriptors, cloneFixtureDescriptor } from "../fixtures/viewDescriptors";
import type { InsightChartSpec } from "../types";
import type { InsightChartBuilderScope } from "./insightChartScope";
import type { InsightChartPrelude } from "./buildInsightChartPrelude";

export function buildInsightStoreSalesChartFactory(scope: InsightChartBuilderScope & InsightChartPrelude) {
  const { dataset, driveFacts, evidenceComplete, evidenceForMetric, insightMetrics, metricByKey, objectionResolution, quoteConsistency, quoteFacts, resolvedFacts, riskFacts, tagAssetQuality, testDriveIntent, topSales, topStores } = scope;
  const buildInsightStoreSalesCharts = () => {
    const storeHeatmap = cloneFixtureDescriptor(chartDescriptors["store-heatmap"]);
    const storeRiskBar = cloneFixtureDescriptor(chartDescriptors["store-risk-bar"]);
    const storeTable = cloneFixtureDescriptor(chartDescriptors["store-table"]);
    const salesBar = cloneFixtureDescriptor(chartDescriptors["sales-bar"]);
    const salesRadar = cloneFixtureDescriptor(chartDescriptors["sales-radar"]);
    const salesTable = cloneFixtureDescriptor(chartDescriptors["sales-table"]);

    return {
    "store-heatmap": {
      ...storeHeatmap,
      heatmap: {
        ...storeHeatmap.heatmap,
        rows: topStores.map((row, rowIndex) => ({
          label: row.store,
          detail: `风险 ${row.risk} / 证据 ${row.total}`,
          values: [18, 22, 34, 68, 76, 44, 31, 24].map((value, index) => Math.min(98, value + row.risk * 9 + rowIndex * 4 - index)),
          factId: dataset.facts.find((fact) => fact.store === row.store)?.id,
          route: "listening"
        }))
      }
    } as InsightChartSpec,
    "store-risk-bar": {
      ...storeRiskBar,
      bars: topStores.map((row, index) => ({
        label: row.store,
        value: row.risk * 22 + row.quote * 12 + row.testDrive * 8,
        detail: `${row.total} 条事实 · 置信 ${row.confidence}%`,
        tone: index === 0 ? "red" : row.risk > 1 ? "amber" : "teal",
        factId: dataset.facts.find((fact) => fact.store === row.store)?.id,
        route: "data"
      }))
    } as InsightChartSpec,
    "store-table": {
      ...storeTable,
      rows: topStores.map((row) => ({
        id: row.store,
        factId: dataset.facts.find((fact) => fact.store === row.store)?.id,
        route: "data",
        cells: [row.store, `${row.total}`, `${row.risk}`, `${row.quote}`, `${row.testDrive}`]
      }))
    } as InsightChartSpec,
    "sales-bar": {
      ...salesBar,
      bars: topSales.map((row) => ({
        label: row.person,
        value: row.quote * 24 + row.resolved * 18 - row.risk * 6,
        detail: `报价 ${row.quote} · 承接 ${row.resolved} · 风险 ${row.risk}`,
        tone: row.risk > 1 ? "amber" : "green",
        factId: dataset.facts.find((fact) => fact.person === row.person)?.id,
        route: "labels"
      }))
    } as InsightChartSpec,
    "sales-radar": {
      ...salesRadar,
      axes: salesRadar.axes.map((axis, index) => ({
        ...axis,
        value: [quoteConsistency, objectionResolution, testDriveIntent, tagAssetQuality, Math.max(0, 100 - riskFacts.length * 10)][index],
        factId: [quoteFacts[0]?.id, resolvedFacts[0]?.id, driveFacts[0]?.id, evidenceComplete[0]?.id, riskFacts[0]?.id][index]
      }))
    } as InsightChartSpec,
    "sales-table": {
      ...salesTable,
      rows: evidenceForMetric(metricByKey.get("objectionResolution") ?? insightMetrics[0]).map((fact) => ({
        id: fact.id,
        factId: fact.id,
        route: fact.route,
        cells: [fact.person, fact.tags.includes("价格异议已承接") ? "沉淀优秀话术" : "人工复核", fact.evidenceRefs[0], fact.status]
      }))
    } as InsightChartSpec
    } satisfies Record<string, InsightChartSpec>;
  };
  return { buildInsightStoreSalesCharts };
}

export type InsightStoreSalesChartFactory = ReturnType<typeof buildInsightStoreSalesChartFactory>;
