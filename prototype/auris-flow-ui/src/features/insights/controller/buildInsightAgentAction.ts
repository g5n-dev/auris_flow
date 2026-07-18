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
import type { InsightReportGuards } from "./buildInsightReportGuards";
import type { InsightReportExecution } from "./buildInsightReportExecution";
import type { InsightReportActions } from "./buildInsightReportActions";
import { createInsightExperimentRun, createPlatformMutation, getInsightReportResource } from "../../../api/client";
import { backendRunFailed, operationStatusFromBackendRun } from "../../../shared/runtime/backendRunStatus";

export function buildInsightAgentAction(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics & InsightView & InsightSelectionState & InsightEvidenceActions & InsightChartSpecs & InsightChartSelection & InsightContext & InsightReportDraftBuilder & InsightReportState & InsightReportGuards & InsightReportExecution & InsightReportActions) {
  const { activeReport, handleCreateReportFromDashboard, insightContextKey, insightTaskActionPending, reportReadyForAction, selectedFact, selectedMetric, setAgentOutput, setInsightActionNotice, setInsightTaskActionPending, setReportDrafts, setReportFlowState } = scope;
  const runAgentAction = (action: "diagnose" | "report" | "task") => {
      if (action === "report") {
        handleCreateReportFromDashboard();
        return;
      }
      if (action === "task") {
        if (insightTaskActionPending) {
          setInsightActionNotice({ status: "pending", title: "动作创建中", detail: "当前动作尚未结束，请等待回执。" });
          return;
        }
        setInsightTaskActionPending(true);
        setInsightActionNotice({ status: "pending", title: "创建", detail: "" });
        void (async () => {
          try {
            let governedReport = activeReport;
            if (!governedReport || !reportReadyForAction || !governedReport.metricResultIds?.length) {
              throw new Error("只有后端状态为 generated 且指标快照完整的报告才能创建动作，请先生成或重试报告。");
            }
            if (governedReport.contextKey !== insightContextKey) {
              throw new Error("报告范围与当前租户、项目或筛选条件不一致，请重新生成报告。");
            }
            const reportReceipt = await getInsightReportResource(governedReport.id);
            const reportState = typeof reportReceipt.data.status === "string" ? reportReceipt.data.status : governedReport.backendState;
            setReportDrafts((current) => current.map((report) =>
              report.id === governedReport!.id
                ? { ...report, backendState: reportState, status: reportState === "generated" ? "已生成" : report.status }
                : report
            ));
            governedReport = { ...governedReport, backendState: reportState };
            if (reportState !== "generated") {
              setReportFlowState({
                status: backendRunFailed(reportState) ? "failed" : "pending",
                stage: backendRunFailed(reportState) ? "failed" : "report-poll",
                attempt: 0,
                metricRunId: governedReport.metricRunId,
                reportId: governedReport.id,
                detail: `报告状态为 ${reportState ?? "unknown"}，动作门禁保持关闭。`
              });
              throw new Error(`报告当前状态为 ${reportState ?? "unknown"}，需等待指标聚合与报告生成完成后再创建动作。`);
            }
            setReportFlowState({
              status: "success",
              stage: "ready",
              attempt: 0,
              metricRunId: governedReport.metricRunId,
              reportId: governedReport.id,
              detail: "报告 generated，动作门禁已通过。"
            });
            const governedMetricResultIds = governedReport.metricResultIds;
            if (!governedMetricResultIds?.length) {
              throw new Error("报告未返回受治理的指标快照，请重试生成报告。");
            }
            const metricIndex = governedReport.metricKeys.indexOf(selectedMetric.key);
            if (metricIndex < 0) {
              throw new Error(`当前报告未冻结指标 ${selectedMetric.label}，请在当前指标下重新生成报告。`);
            }
            const metricResultId = governedMetricResultIds[metricIndex];
            if (!metricResultId) {
              throw new Error(`指标 ${selectedMetric.label} 缺少不可变快照，禁止创建下游动作。`);
            }
            const riskLevel = ["quoteConsistency", "crosstalkRisk"].includes(selectedMetric.key) ? "high" : "medium";
            const actionReceipt = await createPlatformMutation("insights", {
              report_id: governedReport.id,
              metric_result_id: metricResultId,
              metric_key: selectedMetric.key,
              action_type: "create_training_action",
              owner: selectedMetric.owner,
              evidence_refs: governedReport.evidenceIds,
              branch: "auto",
              risk_level: riskLevel,
              hypothesis: selectedMetric.action,
              target_value: selectedMetric.valueNumber + 2,
              source: "ui"
            });
            const actionRaw = actionReceipt.data.raw;
            const branch = typeof actionRaw.branch === "string" ? actionRaw.branch : "human_review";
            const traceId = actionReceipt.meta?.trace_id || actionReceipt.data.trace_id || "";
            if (branch === "experiment" && actionReceipt.data.status === "experiment_ready") {
              const experimentReceipt = await createInsightExperimentRun(actionReceipt.data.id, {
                allocation_percent: 20,
                duration_days: 7,
                min_sample_size: 100,
                primary_metric_key: selectedMetric.key,
                hypothesis: selectedMetric.action,
                candidate: { action_type: "create_training_action", hypothesis: selectedMetric.action },
                control: { action_type: "current_policy", strategy: "保持当前策略与抽检口径" },
                guardrails: { max_risk_rate: 0.06 },
                source: "ui_agentic_insight"
              });
              const experimentId = typeof experimentReceipt.data.raw.insight_experiment_id === "string"
                ? experimentReceipt.data.raw.insight_experiment_id
                : experimentReceipt.data.id;
              setInsightActionNotice({
                status: operationStatusFromBackendRun(experimentReceipt.data.status),
                title: "动作与效果实验已创建",
                detail: `${actionReceipt.data.id} / ${experimentId} / trace ${traceId.slice(0, 12)}`
              });
            } else {
              const reviewTaskId = typeof actionRaw.review_task_id === "string" ? actionRaw.review_task_id : "待生成";
              setInsightActionNotice({
                status: "success",
                title: "高风险动作已进入人工复核",
                detail: `${actionReceipt.data.id} / ${reviewTaskId} / trace ${traceId.slice(0, 12)}`
              });
            }
          } catch (error) {
            const detail = error instanceof Error ? error.message : "BFF 请求失败，请重试。";
            setInsightActionNotice({ status: "error", title: "动作创建失败", detail });
          } finally {
            setInsightTaskActionPending(false);
          }
        })();
        return;
      }
      const message = `已解释 ${selectedMetric.label} ${selectedMetric.value}，证据 ${selectedFact?.eventType ?? "事实"} 可下钻。`;
      setAgentOutput(message);
      setInsightActionNotice({
        status: "success",
        title: "已解释",
        detail: message
      });
    };

  return {
    runAgentAction
  };
}

export type InsightAgentAction = ReturnType<typeof buildInsightAgentAction>;
