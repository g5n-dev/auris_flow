import type { ModuleKey } from "../../../shared/contracts/navigation";
import { chartDescriptors, cloneFixtureDescriptor } from "../fixtures/viewDescriptors";
import type { InsightChartSpec, InsightTone } from "../types";
import type { InsightChartBuilderScope } from "./insightChartScope";
import type { InsightChartPrelude } from "./buildInsightChartPrelude";

export function buildInsightGovernanceChartFactory(scope: InsightChartBuilderScope & InsightChartPrelude) {
  const { compactNumber, dataset, evidenceComplete, firstRiskFact, insightMetrics, qualitySeries, riskFacts, topTags, view, visibleMetrics } = scope;
  const buildInsightGovernanceCharts = () => {
    const tagBar = cloneFixtureDescriptor(chartDescriptors["tag-bar"]);
    const tagSankey = cloneFixtureDescriptor(chartDescriptors["tag-sankey"]);
    const tagTable = cloneFixtureDescriptor(chartDescriptors["tag-table"]);
    const qualityLine = cloneFixtureDescriptor(chartDescriptors["quality-line"]);
    const qualityRadar = cloneFixtureDescriptor(chartDescriptors["quality-radar"]);
    const qualityHeatmap = cloneFixtureDescriptor(chartDescriptors["quality-heatmap"]);
    const qualityTable = cloneFixtureDescriptor(chartDescriptors["quality-table"]);
    const reportCoverage = cloneFixtureDescriptor(chartDescriptors["report-coverage"]);
    const reportTable = cloneFixtureDescriptor(chartDescriptors["report-table"]);

    return {
    "tag-bar": {
      ...tagBar,
      bars: topTags.map((row) => ({
        label: row.label,
        value: row.count,
        detail: `${row.confidence}% 置信 · ${row.assetKey}`,
        tone: row.label.includes("冲突") || row.label.includes("低置信") || row.label.includes("串音") ? "amber" : "teal",
        factId: dataset.facts.find((fact) => fact.tags.includes(row.label))?.id,
        route: "labels"
      }))
    } as InsightChartSpec,
    "tag-sankey": {
      ...tagSankey,
      nodes: [
        ...topTags.slice(0, 4).map((row) => ({ id: `label-${row.label}`, label: row.label, value: `${row.count}`, column: 0, factId: dataset.facts.find((fact) => fact.tags.includes(row.label))?.id, route: "labels" as ModuleKey })),
        { id: "asset-audio", label: "音频片段", value: compactNumber(dataset.facts.length), column: 1, factId: dataset.facts[0]?.id, route: "assets" },
        { id: "asset-event", label: "事件标签资产", value: compactNumber(topTags.length), column: 1, factId: evidenceComplete[0]?.id, route: "assets" },
        { id: "sink-review", label: "Human Loop", value: compactNumber(riskFacts.length), column: 2, factId: riskFacts[0]?.id, route: "listening" },
        { id: "sink-report", label: "报告草稿", value: "2", column: 2, factId: evidenceComplete[0]?.id, route: "assets" }
      ],
      links: topTags.slice(0, 4).flatMap((row, index) => [
        { source: `label-${row.label}`, target: index % 2 ? "asset-event" : "asset-audio", value: Math.max(4, row.count * 4), tone: "teal" as InsightTone },
        { source: index % 2 ? "asset-event" : "asset-audio", target: row.label.includes("冲突") || row.label.includes("低置信") ? "sink-review" : "sink-report", value: Math.max(3, row.count * 2), tone: row.label.includes("冲突") ? "red" as InsightTone : "green" as InsightTone }
      ])
    } as InsightChartSpec,
    "tag-table": {
      ...tagTable,
      rows: topTags.map((row) => ({
        id: row.label,
        factId: dataset.facts.find((fact) => fact.tags.includes(row.label))?.id,
        route: "labels",
        cells: [row.label, String(row.count), `${row.confidence}%`, row.assetKey, row.label.includes("冲突") ? "复核队列" : "报告"]
      }))
    } as InsightChartSpec,
    "quality-line": {
      ...qualityLine,
      series: qualitySeries
    } as InsightChartSpec,
    "quality-radar": {
      ...qualityRadar,
      axes: dataset.qualityRows.map((row) => ({
        label: row.ability.replace("ASR ", ""),
        value: row.candidate,
        compare: row.baseline,
        factId: firstRiskFact?.id,
        route: row.route
      }))
    } as InsightChartSpec,
    "quality-heatmap": {
      ...qualityHeatmap,
      heatmap: {
        ...qualityHeatmap.heatmap,
        rows: dataset.storeRows.map((row, rowIndex) => ({
          label: row.store,
          detail: `风险 ${row.risk}`,
          values: [24, 42, 68, 38].map((value, index) => Math.min(98, value + row.risk * 7 + rowIndex * 5 + index * 3)),
          factId: dataset.facts.find((fact) => fact.store === row.store)?.id,
          route: "evaluation"
        }))
      }
    } as InsightChartSpec,
    "quality-table": {
      ...qualityTable,
      rows: dataset.qualityRows.map((row) => ({
        id: row.ability,
        factId: riskFacts[0]?.id,
        route: "evaluation",
        cells: [row.ability, `${row.baseline}`, `${row.candidate}`, `${row.delta}`, row.blocker]
      }))
    } as InsightChartSpec,
    "report-coverage": {
      ...reportCoverage,
      bars: reportCoverage.bars.map((bar, index) => ({
        ...bar,
        value: [insightMetrics.length, evidenceComplete.length, riskFacts.length, view.chartIds.length][index],
        factId: [dataset.facts[0]?.id, evidenceComplete[0]?.id, riskFacts[0]?.id, dataset.facts[0]?.id][index]
      }))
    } as InsightChartSpec,
    "report-table": {
      ...reportTable,
      rows: reportTable.rows.map((row, index) => ({
        ...row,
        ...(index === 0 ? {
          cells: [row.cells[0], row.cells[1], `${visibleMetrics.length}`, row.cells[3]]
        } : index === 1 ? {
          factId: dataset.facts[0]?.id,
          cells: [row.cells[0], row.cells[1], `${dataset.facts.length}`, row.cells[3]]
        } : index === 2 ? {
          factId: evidenceComplete[0]?.id,
          cells: [row.cells[0], row.cells[1], `${evidenceComplete.length}`, row.cells[3]]
        } : {
          factId: riskFacts[0]?.id,
          cells: [row.cells[0], row.cells[1], `${riskFacts.length}`, row.cells[3]]
        })
      }))
    } as InsightChartSpec
    } satisfies Record<string, InsightChartSpec>;
  };
  return { buildInsightGovernanceCharts };
}

export type InsightGovernanceChartFactory = ReturnType<typeof buildInsightGovernanceChartFactory>;
