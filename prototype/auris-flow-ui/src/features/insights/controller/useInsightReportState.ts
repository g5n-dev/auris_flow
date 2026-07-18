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
import type { InsightReportDraftBuilder } from "./buildInsightReportDraft";
import type { ModuleKey } from "../../../shared/contracts/navigation";
import type { OperationNotice } from "../../../shared/contracts/operations";
import { INSIGHT_METRIC_POLL_LIMIT, INSIGHT_REPORT_POLL_LIMIT } from "../reportPolicy";
import type { InsightReportDraft, InsightReportFlowState } from "../types";
import { useEffect, useRef, useState } from "react";

export function useInsightReportState(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics & InsightView & InsightSelectionState & InsightEvidenceActions & InsightChartSpecs & InsightChartSelection & InsightContext & InsightReportDraftBuilder) {
  const { buildReportDraft, insightContextKey } = scope;
  const [reportDrafts, setReportDrafts] = useState<InsightReportDraft[]>(() => [
      { ...buildReportDraft("business", "经营日报", "待生成"), id: "RPT-DAILY", createdAt: "今日 09:30" },
      { ...buildReportDraft("store", "门店周报", "草稿"), id: "RPT-STORE", createdAt: "周一 10:00" },
      { ...buildReportDraft("sales", "销售训练包", "待生成"), id: "RPT-TRAIN", createdAt: "按需生成" },
      { ...buildReportDraft("reports", "管理层摘要", "待导出"), id: "RPT-EXEC", createdAt: "本周 18:00" }
    ]);

  const [activeReportId, setActiveReportId] = useState(reportDrafts[0]?.id ?? "");

  const contextReportDrafts = reportDrafts.filter((report) => report.contextKey === insightContextKey);

  const activeReport = contextReportDrafts.find((report) => report.id === activeReportId) ?? contextReportDrafts[0];

  const [reportFlowState, setReportFlowState] = useState<InsightReportFlowState>({
      status: "idle",
      stage: "idle",
      attempt: 0,
      detail: "尚未启动真实指标聚合。"
    });

  const reportFlowPendingRef = useRef(false);

  const reportOperationRef = useRef(0);

  const reportFlowPending = reportFlowState.status === "pending";

  const reportReadyForAction = Boolean(
      activeReport?.backendState === "generated" &&
      activeReport.metricResultIds?.length === activeReport.metricKeys.length
    );

  const reportFlowActionLabel = (fallback: string) => {
      if (reportFlowPending) {
        return reportFlowState.stage.startsWith("metric")
          ? `聚合中 ${Math.max(reportFlowState.attempt, 1)}/${INSIGHT_METRIC_POLL_LIMIT}`
          : `报告生成中 ${Math.max(reportFlowState.attempt, 1)}/${INSIGHT_REPORT_POLL_LIMIT}`;
      }
      return reportFlowState.status === "failed" ? "重试生成" : fallback;
    };

  useEffect(() => {
      const scopedReport = reportDrafts.find((report) => report.contextKey === insightContextKey);
      setActiveReportId(scopedReport?.id ?? "");
    }, [insightContextKey]);

  const [insightActionNotice, setInsightActionNotice] = useState<OperationNotice>({
      status: "idle",
      title: "等待 BI 操作",
      detail: "点击智能解读、生成报告、图表按钮或下游资产后，右侧会同步证据与报告上下文。"
    });

  const [insightTaskActionPending, setInsightTaskActionPending] = useState(false);

  const [selectedChartAction, setSelectedChartAction] = useState<{ chartId: string; action: "agent" | "source" | "report" | "hide" | "downstream"; title: string } | null>(null);

  const [downstreamAssetFocus, setDownstreamAssetFocus] = useState<{
      chartId: string;
      title: string;
      route: ModuleKey;
      factId?: string;
      assetKey: string;
      items: Array<{ label: string; value: string; detail: string; route: ModuleKey }>;
    } | null>(null);

  return {
    reportDrafts,
    setReportDrafts,
    activeReportId,
    setActiveReportId,
    contextReportDrafts,
    activeReport,
    reportFlowState,
    setReportFlowState,
    reportFlowPendingRef,
    reportOperationRef,
    reportFlowPending,
    reportReadyForAction,
    reportFlowActionLabel,
    insightActionNotice,
    setInsightActionNotice,
    insightTaskActionPending,
    setInsightTaskActionPending,
    selectedChartAction,
    setSelectedChartAction,
    downstreamAssetFocus,
    setDownstreamAssetFocus
  };
}

export type InsightReportState = ReturnType<typeof useInsightReportState>;
