import type { ModuleKey } from "../../../shared/contracts/navigation";
import { chartDescriptors, cloneFixtureDescriptor } from "../fixtures/viewDescriptors";
import type { InsightChartSpec, InsightTone } from "../types";
import type { InsightChartBuilderScope } from "./insightChartScope";
import type { InsightChartPrelude } from "./buildInsightChartPrelude";

export function buildInsightBusinessChartFactory(scope: InsightChartBuilderScope & InsightChartPrelude) {
  const { buildTrend, dataset, driveFacts, evidenceComplete, firstRiskFact, northStar, northStarScore, objectionResolution, quoteFacts, rangeConfig, rangedConversionProgress, rangedEffectiveReceptionRate, rangedQuoteConsistency, rangedRiskReverseScore, resolvedFacts, riskFacts, tagAssetQuality, topStores, topTags, validReceptionFacts } = scope;
  const buildInsightBusinessCharts = () => {
    const trend = cloneFixtureDescriptor(chartDescriptors["business-trend"]);
    const sankey = cloneFixtureDescriptor(chartDescriptors["business-sankey"]);
    const radar = cloneFixtureDescriptor(chartDescriptors["business-radar"]);
    const table = cloneFixtureDescriptor(chartDescriptors["business-table"]);

    return {
    "business-trend": {
      ...trend,
      subtitle: `${rangeConfig.reportScope} 内按同一事实表计算，不固定近 7 天`,
      source: `InsightFact 聚合 / ${rangeConfig.reportScope}`,
      xLabels: rangeConfig.labels,
      eventMarkers: trend.eventMarkers.map((marker, index) => ({
        ...marker,
        ...(index === 0 ? {
          index: Math.min(1, Math.max(rangeConfig.labels.length - 1, 0)),
          detail: `${dataset.context.label} 开始回流报价、试驾和异议标签`,
          factId: evidenceComplete[0]?.id
        } : index === 1 ? {
          index: Math.min(2, Math.max(rangeConfig.labels.length - 1, 0)),
          factId: quoteFacts[0]?.id
        } : {
          index: Math.max(rangeConfig.labels.length - 1, 0),
          factId: resolvedFacts[0]?.id ?? driveFacts[0]?.id
        })
      })),
      summaryCards: trend.summaryCards.map((card, index) => ({
        ...card,
        ...(index === 0 ? {
          value: `异议化解 ${objectionResolution}%`,
          factId: resolvedFacts[0]?.id,
          pointIndex: Math.max(rangeConfig.labels.length - 1, 0)
        } : index === 1 ? {
          value: `报价一致 ${rangedQuoteConsistency}%`,
          tone: rangedQuoteConsistency < 80 ? "red" as InsightTone : "amber" as InsightTone,
          factId: quoteFacts[0]?.id,
          pointIndex: Math.max(rangeConfig.labels.length - 1, 0)
        } : {
          factId: riskFacts[0]?.id ?? evidenceComplete[0]?.id,
          pointIndex: Math.max(rangeConfig.labels.length - 1, 0)
        })
      })),
      series: trend.series.map((series, index) => ({
        ...series,
        ...(index === 0 ? {
          values: buildTrend(northStarScore, 1.4),
          factIds: evidenceComplete.map((fact) => fact.id),
          delta: northStar.delta
        } : index === 1 ? {
          values: buildTrend(objectionResolution, 2.3),
          factIds: resolvedFacts.map((fact) => fact.id)
        } : index === 2 ? {
          tone: rangedQuoteConsistency < 80 ? "red" as InsightTone : "amber" as InsightTone,
          values: buildTrend(rangedQuoteConsistency, -0.6),
          factIds: quoteFacts.map((fact) => fact.id),
          direction: rangedQuoteConsistency < 80 ? "down" as const : "flat" as const
        } : {
          values: buildTrend(rangedConversionProgress, 1.1),
          factIds: driveFacts.map((fact) => fact.id)
        })
      }))
    } as InsightChartSpec,
    "business-sankey": {
      ...sankey,
      nodes: [
        ...topStores.slice(0, 3).map((row) => ({ id: `store-${row.store}`, label: row.store, value: `${row.total} 条`, column: 0, factId: dataset.facts.find((fact) => fact.store === row.store)?.id, route: "data" as ModuleKey })),
        ...topTags.slice(0, 3).map((row) => ({ id: `tag-${row.label}`, label: row.label, value: `${row.count}`, column: 1, factId: dataset.facts.find((fact) => fact.tags.includes(row.label))?.id, route: "labels" as ModuleKey })),
        { id: "action-review", label: "证据审查", value: `${riskFacts.length}`, column: 2, factId: firstRiskFact?.id, route: "listening" },
        { id: "action-report", label: "报告回写", value: "2", column: 2, factId: evidenceComplete[0]?.id, route: "assets" }
      ],
      links: ([
        { source: `store-${topStores[0]?.store}`, target: `tag-${topTags[0]?.label}`, value: 16, tone: "amber" },
        { source: `store-${topStores[1]?.store}`, target: `tag-${topTags[1]?.label}`, value: 12, tone: "teal" },
        { source: `store-${topStores[2]?.store}`, target: `tag-${topTags[2]?.label}`, value: 9, tone: "violet" },
        { source: `tag-${topTags[0]?.label}`, target: "action-review", value: 11, tone: "red" },
        { source: `tag-${topTags[1]?.label}`, target: "action-report", value: 8, tone: "green" },
        { source: `tag-${topTags[2]?.label}`, target: "action-review", value: 6, tone: "amber" }
      ] satisfies Array<{ source: string; target: string; value: number; tone: InsightTone }>).filter((link) => link.source !== "store-undefined" && link.target !== "tag-undefined")
    } as InsightChartSpec,
    "business-radar": {
      ...radar,
      axes: radar.axes.map((axis, index) => ({
        ...axis,
        value: [rangedEffectiveReceptionRate, rangedConversionProgress, rangedQuoteConsistency, tagAssetQuality, rangedRiskReverseScore][index],
        factId: [validReceptionFacts[0]?.id, resolvedFacts[0]?.id, quoteFacts[0]?.id, evidenceComplete[0]?.id, firstRiskFact?.id][index]
      }))
    } as InsightChartSpec,
    "business-table": {
      ...table,
      rows: dataset.facts.slice(0, 6).map((fact) => ({
        id: fact.id,
        factId: fact.id,
        route: fact.route,
        cells: [fact.time, fact.eventType, fact.tags.slice(0, 3).join(" / "), fact.assetKey, fact.status]
      }))
    } as InsightChartSpec
    } satisfies Record<string, InsightChartSpec>;
  };
  return { buildInsightBusinessCharts };
}

export type InsightBusinessChartFactory = ReturnType<typeof buildInsightBusinessChartFactory>;
