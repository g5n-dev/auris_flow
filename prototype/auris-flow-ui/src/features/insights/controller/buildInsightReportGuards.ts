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
import type { InsightReportState } from "./useInsightReportState";
import { getInsightReportResource } from "../../../api/client";
import { backendRunFailed } from "../../../shared/runtime/backendRunStatus";
import { isRecordValue } from "../../../shared/runtime/records";
import { INSIGHT_REPORT_POLL_LIMIT, waitForInsightPoll } from "../reportPolicy";
import type { InsightReportDraft } from "../types";

export function buildInsightReportGuards(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics & InsightView & InsightSelectionState & InsightEvidenceActions & InsightChartSpecs & InsightChartSelection & InsightContext & InsightReportDraftBuilder & InsightReportState) {
  const { activeReport, dataset, insightContextKeyRef, insightTaskActionPending, reportFlowPending, reportFlowState, reportOperationRef, reportReadyForAction, setInsightActionNotice, setReportDrafts, setReportFlowState, unique } = scope;
  const storeOptions = unique([dataset.context.store, ...dataset.storeRows.map((row) => row.store)]).slice(0, 4);

  const salesOptions = ["全部销售", ...dataset.salesRows.map((row) => row.person.split(" / ")[0]).slice(0, 3)];

  const labelVersionOptions = unique([dataset.context.label, "v1.8.4", "v1.9.0-rc2"]);

  const reportDisabledReason = reportFlowPending ? reportFlowState.detail : "";

  const taskDisabledReason = insightTaskActionPending
      ? "动作创建中，暂勿重复提交。"
      : reportReadyForAction
        ? ""
        : activeReport?.metricResultIds?.length
          ? "报告尚未生成完成。"
          : "先生成报告并冻结指标快照。";

  const stringIds = (value: unknown) => Array.isArray(value)
      ? unique(value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())).map((item) => item.trim()))
      : [];

  const backendFailureDetail = (raw: Record<string, unknown>) => {
      const failure = isRecordValue(raw.failure) ? raw.failure : undefined;
      return typeof failure?.message === "string" && failure.message.trim() ? failure.message : undefined;
    };

  const ensureReportOperationCurrent = (operationId: number, operationContextKey: string) => {
      if (reportOperationRef.current !== operationId) {
        throw new Error("已有新的报告操作接管当前流程。");
      }
      if (insightContextKeyRef.current !== operationContextKey) {
        throw new Error("租户、项目或筛选范围已变化，旧范围链路已停止；请在当前范围重试。");
      }
    };

  const pollInsightReport = async (
      report: InsightReportDraft,
      operationId: number,
      operationContextKey: string
    ): Promise<InsightReportDraft> => {
      let lastState = report.backendState ?? "generating";
      for (let attempt = 1; attempt <= INSIGHT_REPORT_POLL_LIMIT; attempt += 1) {
        ensureReportOperationCurrent(operationId, operationContextKey);
        setReportFlowState({
          status: "pending",
          stage: "report-poll",
          attempt,
          metricRunId: report.metricRunId,
          reportId: report.id,
          detail: `报告生成 pending，轮询 ${attempt}/${INSIGHT_REPORT_POLL_LIMIT}。`
        });
        setInsightActionNotice({
          status: "pending",
          title: "报告生成中（pending）",
          detail: `${report.id} / 轮询 ${attempt}/${INSIGHT_REPORT_POLL_LIMIT}`
        });
        const receipt = await getInsightReportResource(report.id);
        lastState = typeof receipt.data.status === "string" ? receipt.data.status : lastState;
        const nextReport: InsightReportDraft = {
          ...report,
          backendState: lastState,
          status: lastState === "generated" ? "已生成" : report.status
        };
        setReportDrafts((current) => current.map((item) => item.id === report.id ? nextReport : item));
        if (lastState === "generated") return nextReport;
        if (backendRunFailed(lastState)) {
          throw new Error(backendFailureDetail(receipt.data) ?? `报告后端状态为 ${lastState}。`);
        }
        if (attempt < INSIGHT_REPORT_POLL_LIMIT) await waitForInsightPoll();
      }
      throw new Error(`报告 ${report.id} 在 ${INSIGHT_REPORT_POLL_LIMIT} 次轮询后仍为 ${lastState}。`);
    };

  return {
    storeOptions,
    salesOptions,
    labelVersionOptions,
    reportDisabledReason,
    taskDisabledReason,
    stringIds,
    backendFailureDetail,
    ensureReportOperationCurrent,
    pollInsightReport
  };
}

export type InsightReportGuards = ReturnType<typeof buildInsightReportGuards>;
