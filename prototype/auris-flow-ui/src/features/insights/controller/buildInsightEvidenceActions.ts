import type { InsightsModuleProps } from "../types";
import type { HotwordInsightsState } from "./useHotwordInsights";
import type { InsightDatasetState } from "./useInsightDataset";
import type { InsightTimeRangeState } from "./useInsightTimeRange";
import type { InsightComparisonState } from "./useInsightComparisonState";
import type { InsightMetrics } from "./buildInsightMetrics";
import type { InsightView } from "./buildInsightView";
import type { InsightSelectionState } from "./useInsightSelectionState";
import type { DeepLinkFocusMode, ModuleDeepLink, ModuleKey } from "../../../shared/contracts/navigation";
import { withDeepLinkOrigin } from "../../../shared/runtime/deepLinks";
import type { InsightFact, InsightMetric } from "../types";

export function buildInsightEvidenceActions(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics & InsightView & InsightSelectionState) {
  const { clampScore, dataset, navigateToTarget, rangeConfig, setActiveChartId, setAgentOutput, setSelectedFactId } = scope;
  const evidenceForMetric = (metric: InsightMetric) => {
      const rows = metric.evidenceIds.map((id) => dataset.facts.find((fact) => fact.id === id)).filter((fact): fact is InsightFact => Boolean(fact));
      return rows.length ? rows : dataset.facts.filter((fact) => metric.tags.some((tag) => fact.tags.includes(tag))).slice(0, 4);
    };

  const selectEvidence = (factId: string | undefined, chartId: string, reason: string) => {
      if (factId) setSelectedFactId(factId);
      setActiveChartId(chartId);
      setAgentOutput(`${reason}。已定位到 ${factId ?? "当前"} 证据，并同步右侧证据链。`);
    };

  const insightBindingForFact = (fact?: InsightFact) => {
      const hasQuote = Boolean(fact?.amountConflict || fact?.tags.includes("报价金额冲突") || fact?.tags.includes("报价金额"));
      const hasCrosstalk = Boolean(fact?.crosstalk || fact?.tags.includes("串音疑似"));
      const hasLowConfidence = Boolean(fact?.lowConfidence || fact?.tags.includes("低置信片段"));
      if (hasCrosstalk) {
        return {
          sampleId: "sample-af-129",
          evidenceId: "EVP-drive-129",
          dataAssetId: "AF-129",
          assetKey: "auris/audio/voice_segments",
          labelIntentKey: "crosstalk",
          badcaseId: "C-1028",
          window: fact?.time ?? "12:28:01 - 12:29:28",
          focusMode: "matrix" as DeepLinkFocusMode
        };
      }
      if (hasLowConfidence) {
        return {
          sampleId: "sample-af-131",
          evidenceId: "EVP-asr-131",
          dataAssetId: "AF-131",
          assetKey: "auris/model/asr_transcripts",
          labelIntentKey: "dealIntent",
          badcaseId: "A-4107",
          window: fact?.time ?? "09:15 - 09:18",
          focusMode: "evidence" as DeepLinkFocusMode
        };
      }
      if (fact?.tags.includes("试驾时间") || fact?.tags.includes("试驾预约承接")) {
        return {
          sampleId: "sample-af-128",
          evidenceId: "EVP-drive-129",
          dataAssetId: "AF-128",
          assetKey: fact.assetKey || "auris/events/document_links",
          labelIntentKey: "testDrive",
          badcaseId: "T-8812",
          window: fact.time,
          focusMode: "evidence" as DeepLinkFocusMode
        };
      }
      return {
        sampleId: "sample-af-128",
        evidenceId: "EVP-quote-128",
        dataAssetId: "AF-128",
        assetKey: fact?.assetKey || (hasQuote ? "auris/label/event_tags" : "auris/audio/voice_segments"),
        labelIntentKey: hasQuote ? "quote" : "dealIntent",
        badcaseId: hasQuote ? "T-8812" : "B-2031",
        window: fact?.time ?? "12:27:18 - 12:28:01",
        focusMode: "evidence" as DeepLinkFocusMode
      };
    };

  const targetForInsightFact = (fact: InsightFact | undefined, targetModule?: ModuleKey, title?: string): ModuleDeepLink => {
      const binding = insightBindingForFact(fact);
      const module = targetModule ?? fact?.route ?? "listening";
      const targetTitle = title ?? fact?.eventType ?? "洞察证据";
      const targetDetail = fact ? `${fact.store} / ${fact.doc} / ${fact.status}` : "业务洞察聚合事实";
      if (module === "assets") {
        return { module: "assets", tab: "lineage", objectKind: "asset", objectId: binding.assetKey, focusMode: "lineage", title: targetTitle, detail: targetDetail };
      }
      if (module === "data") {
        return { module: "data", tab: "relations", objectKind: "dataAsset", objectId: binding.dataAssetId, focusMode: "detail", title: targetTitle, detail: targetDetail };
      }
      if (module === "labels") {
        return { module: "labels", tab: "schema", objectKind: "labelIntent", objectId: binding.labelIntentKey, focusMode: "detail", title: targetTitle, detail: targetDetail };
      }
      if (module === "evaluation") {
        return { module: "evaluation", tab: "badcase", objectKind: "evaluationBadcase", objectId: binding.badcaseId, focusMode: "detail", title: targetTitle, detail: targetDetail };
      }
      if (module === "knowledge") {
        return { module: "knowledge", tab: "graph", objectKind: "knowledge", objectId: binding.evidenceId, focusMode: "path", title: targetTitle, detail: targetDetail };
      }
      return { module: "listening", objectKind: "reviewSample", objectId: binding.sampleId, focusMode: binding.focusMode, title: targetTitle, detail: targetDetail, window: binding.window };
    };

  const openInsightTarget = (fact: InsightFact | undefined, targetModule?: ModuleKey, originLabel = "业务洞察 / 关联值", title?: string) => {
      navigateToTarget(withDeepLinkOrigin(targetForInsightFact(fact, targetModule, title), originLabel, "insights", title ?? fact?.eventType));
    };

  const buildTrend = (base: number, drift: number) => rangeConfig.labels.map((_, index) => {
      const distance = rangeConfig.labels.length - 1 - index;
      return clampScore(base - drift * distance + (index % 2 ? 0.8 : -0.4));
    });

  return {
    evidenceForMetric,
    selectEvidence,
    insightBindingForFact,
    targetForInsightFact,
    openInsightTarget,
    buildTrend
  };
}

export type InsightEvidenceActions = ReturnType<typeof buildInsightEvidenceActions>;
