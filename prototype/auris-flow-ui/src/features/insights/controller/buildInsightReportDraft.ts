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
import type { InsightChartSelection } from "./buildInsightChartSelection";
import type { InsightContext } from "./useInsightContext";
import type { InsightReportDraft, InsightTabKey } from "../types";

export function buildInsightReportDraft(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics & InsightView & InsightSelectionState & InsightEvidenceActions & InsightChartSpecs & InsightChartSelection & InsightContext) {
  const { comparisonScope, currentTab, dataset, evidenceComplete, evidenceForMetric, governedEvidenceRefs, hiddenChartIds, insightContextKey, northStar, rangeConfig, riskFacts, selectedMetric, storeFilter, views } = scope;
  const buildReportDraft = (sourceTab: InsightTabKey = currentTab, title = views[sourceTab].reportTitle, status: InsightReportDraft["status"] = "草稿"): InsightReportDraft => {
      const selectedEvidence = evidenceForMetric(selectedMetric).slice(0, 5);
      const governedRefs = governedEvidenceRefs(selectedEvidence);
      const selectedChartIds = views[sourceTab].chartIds.filter((id) => !hiddenChartIds.includes(id));
      return {
        id: `RPT-${Date.now().toString().slice(-6)}`,
        title,
        status,
        createdAt: "刚刚",
        sourceTab,
        scope: `${dataset.context.tenant} / ${dataset.context.project} / ${storeFilter} / ${rangeConfig.reportScope} / 对比 ${comparisonScope}`,
        metricKeys: views[sourceTab].metricKeys,
        chartIds: selectedChartIds,
        evidenceIds: governedRefs,
        evidencePackIds: governedRefs.filter((ref) => ref.startsWith("AF-")),
        contextKey: insightContextKey,
        summary: `${views[sourceTab].title} 基于 ${rangeConfig.reportScope}、${dataset.facts.length} 条事实、${dataset.tagCounts.length} 个标签和 ${evidenceComplete.length} 条可追溯证据生成，北极星 ${northStar.value}。`,
        sections: [
          { title: "北极星结论", body: `${northStar.label} 为 ${northStar.value}，公式：${northStar.formula}。${northStar.drag}` },
          { title: "核心指标", body: `${selectedMetric.label} 为 ${selectedMetric.value}，${selectedMetric.meaning}。建议：${selectedMetric.suggestion}` },
          { title: "异常归因", body: riskFacts.length ? `当前有 ${riskFacts.length} 条风险事实，主要集中在 ${riskFacts[0].store} 的 ${riskFacts[0].eventType}。` : "当前范围无高风险事实。" },
          { title: "证据引用", body: selectedEvidence.map((fact) => `${fact.time} ${fact.eventType} · ${fact.assetKey}`).join("；") },
          { title: "建议动作", body: selectedMetric.action }
        ]
      };
    };

  return {
    buildReportDraft
  };
}

export type InsightReportDraftBuilder = ReturnType<typeof buildInsightReportDraft>;
