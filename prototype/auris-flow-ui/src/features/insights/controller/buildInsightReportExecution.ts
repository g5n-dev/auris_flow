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
import { createInsightMetricRun, createInsightReportRun, getInsightMetricRun, getMaterializedInsightMetrics } from "../../../api/client";
import { backendRunFailed, backendRunSucceeded } from "../../../shared/runtime/backendRunStatus";
import { INSIGHT_METRIC_POLL_LIMIT, waitForInsightPoll } from "../reportPolicy";
import { bindMaterializedMetricSnapshots } from "../model/metricScopePresentation";
import type { InsightMetricSnapshot, InsightReportDraft } from "../types";

export function buildInsightReportExecution(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics & InsightView & InsightSelectionState & InsightEvidenceActions & InsightChartSpecs & InsightChartSelection & InsightContext & InsightReportDraftBuilder & InsightReportState & InsightReportGuards) {
  const { backendFailureDetail, buildReportDraft, currentTab, customRangeDraft, dataset, ensureReportOperationCurrent, hiddenChartIds, insightContextKey, labelVersionFilter, pollInsightReport, reportDrafts, reportFlowPendingRef, reportFlowState, reportOperationRef, selectedMetric, setActiveReportId, setAgentOutput, setInsightActionNotice, setReportDrafts, setReportFlowState, setSelectedChartAction, storeFilter, stringIds, timeRange, unique, views } = scope;
  const createReport = async () => {
      if (reportFlowPendingRef.current) {
        setInsightActionNotice({
          status: "pending",
          title: "报告链路执行中（pending）",
          detail: "当前聚合或报告生成尚未结束，请等待本次操作完成。"
        });
        return null;
      }
      const resumableReport = reportFlowState.status === "failed" && reportFlowState.reportId
        ? reportDrafts.find((report) =>
            report.id === reportFlowState.reportId &&
            report.contextKey === insightContextKey &&
            report.backendState !== "generated" &&
            !backendRunFailed(report.backendState)
          )
        : undefined;
      const visibleIds = views[currentTab].chartIds.filter((id) => !hiddenChartIds.includes(id));
      if (!resumableReport && !visibleIds.length) {
        const detail = "当前看板图表已全部隐藏，先点击“显示全部”或恢复至少一张图表后再生成报告。";
        setAgentOutput(detail);
        setInsightActionNotice({ status: "error", title: "报告未生成", detail });
        setReportFlowState({ status: "failed", stage: "failed", attempt: 0, detail });
        setSelectedChartAction(null);
        return null;
      }
      const operationId = ++reportOperationRef.current;
      const operationContextKey = insightContextKey;
      let metricRunId = resumableReport?.metricRunId;
      let reportId = resumableReport?.id;
      reportFlowPendingRef.current = true;
      try {
        if (resumableReport) {
          const generatedReport = await pollInsightReport(resumableReport, operationId, operationContextKey);
          setReportFlowState({
            status: "success",
            stage: "ready",
            attempt: 0,
            metricRunId: generatedReport.metricRunId,
            reportId: generatedReport.id,
            detail: "报告生成 success，可以创建下游动作。"
          });
          setInsightActionNotice({
            status: "success",
            title: "报告生成成功（success）",
            detail: `${generatedReport.id} 已生成，可创建动作。`
          });
          return generatedReport;
        }

        const draft = buildReportDraft(currentTab, currentTab === "reports" ? "管理层摘要" : views[currentTab].reportTitle, "待生成");
        const requestedMetricKeys = unique(draft.metricKeys);
        const metricScope = {
          time_range:
            timeRange === "custom"
              ? `${customRangeDraft.startDate}/${customRangeDraft.endDate}`
              : timeRange,
          store_ids: [storeFilter],
          model_version: dataset.context.model,
          label_version: labelVersionFilter
        };
        setReportFlowState({
          status: "pending",
          stage: "metric-create",
          attempt: 0,
          detail: `正在为 ${requestedMetricKeys.length} 个指标创建聚合运行。`
        });
        setInsightActionNotice({
          status: "pending",
          title: "指标聚合创建中（pending）",
          detail: `${requestedMetricKeys.length} 指标 / ${draft.chartIds.length} 图 / ${draft.evidenceIds.length} 证据`
        });
        const metricRunReceipt = await createInsightMetricRun({
          metric_keys: requestedMetricKeys,
          ...metricScope,
          source: "ui_insight_report"
        });
        metricRunId = typeof metricRunReceipt.data.raw.run_id === "string"
          ? metricRunReceipt.data.raw.run_id
          : metricRunReceipt.data.id;
        if (!metricRunId || metricRunId === "pending") {
          throw new Error("指标聚合接口未返回有效 run_id。");
        }
        if (metricRunReceipt.data.raw.run_type !== "insight_metric_aggregation") {
          throw new Error(`指标聚合返回了错误运行类型：${String(metricRunReceipt.data.raw.run_type ?? "unknown")}。`);
        }

        let metricResultIds: string[] | undefined;
        let metricSnapshots: InsightMetricSnapshot[] | undefined;
        for (let attempt = 1; attempt <= INSIGHT_METRIC_POLL_LIMIT; attempt += 1) {
          ensureReportOperationCurrent(operationId, operationContextKey);
          setReportFlowState({
            status: "pending",
            stage: "metric-poll",
            attempt,
            metricRunId,
            detail: `指标聚合 pending，轮询 ${attempt}/${INSIGHT_METRIC_POLL_LIMIT}。`
          });
          setInsightActionNotice({
            status: "pending",
            title: "指标聚合中（pending）",
            detail: `${metricRunId} / 轮询 ${attempt}/${INSIGHT_METRIC_POLL_LIMIT}`
          });
          const runReceipt = await getInsightMetricRun(metricRunId);
          const runState = runReceipt.data.status;
          if (backendRunFailed(runState)) {
            throw new Error(backendFailureDetail(runReceipt.data.raw) ?? `指标聚合运行状态为 ${runState}。`);
          }
          if (backendRunSucceeded(runState)) {
            const runMetricResultIds = stringIds(runReceipt.data.raw.metric_result_ids);
            if (runMetricResultIds.length !== requestedMetricKeys.length) {
              throw new Error(
                `聚合运行成功但只返回 ${runMetricResultIds.length}/${requestedMetricKeys.length} 个 metric_result_id。`
              );
            }
            setReportFlowState({
              status: "pending",
              stage: "metric-query",
              attempt,
              metricRunId,
              detail: "聚合 success，正在读取已物化指标快照。"
            });
            setInsightActionNotice({
              status: "pending",
              title: "指标聚合成功（success）",
              detail: `${metricRunId} 已完成，正在校验 materialized 快照。`
            });
            const metricsReceipt = await getMaterializedInsightMetrics({
              time_range: metricScope.time_range,
              store_id: metricScope.store_ids[0],
              model_version: metricScope.model_version,
              label_version: metricScope.label_version,
              limit: 50
            });
            const validatedMetricSnapshots = bindMaterializedMetricSnapshots(
              requestedMetricKeys,
              metricRunId,
              runMetricResultIds,
              metricsReceipt.data.items
            );
            metricSnapshots = validatedMetricSnapshots;
            metricResultIds = validatedMetricSnapshots.map((metric) => metric.metric_result_id);
            break;
          }
          if (attempt < INSIGHT_METRIC_POLL_LIMIT) await waitForInsightPoll();
        }
        if (!metricResultIds) {
          throw new Error(`指标聚合在 ${INSIGHT_METRIC_POLL_LIMIT} 次轮询后仍未成功，已停止报告创建。`);
        }

        ensureReportOperationCurrent(operationId, operationContextKey);
        setReportFlowState({
          status: "pending",
          stage: "report-create",
          attempt: 0,
          metricRunId,
          detail: `${metricResultIds.length} 个指标已物化，正在创建报告。`
        });
        setInsightActionNotice({
          status: "pending",
          title: "指标已物化，创建报告中（pending）",
          detail: `${metricResultIds.length} 个真实 metric_result_ids / ${metricRunId}`
        });
        const receipt = await createInsightReportRun({
          title: draft.title,
          report_type: currentTab === "reports" ? "management_summary" : currentTab,
          ...metricScope,
          owner: selectedMetric.owner,
          metric_result_ids: metricResultIds,
          evidence_refs: draft.evidenceIds,
          report_sections: draft.sections.map((section) => section.title),
          source: "ui_insight_report"
        });
        reportId = typeof receipt.data.raw.report_id === "string" ? receipt.data.raw.report_id : undefined;
        const runId = typeof receipt.data.raw.run_id === "string" ? receipt.data.raw.run_id : undefined;
        if (!reportId || !runId) {
          throw new Error("报告接口未返回真实 report_id 或 run_id，已拒绝使用本地草稿标识替代。");
        }
        const returnedMetricResultIds = stringIds(receipt.data.raw.metric_result_ids);
        if (
          returnedMetricResultIds.length !== metricResultIds.length ||
          metricResultIds.some((id, index) => returnedMetricResultIds[index] !== id)
        ) {
          throw new Error("报告接口未确认本次聚合的完整 metric_result_ids。");
        }
        const evidencePackIds = Array.isArray(receipt.data.raw.evidence_pack_ids)
          ? receipt.data.raw.evidence_pack_ids.filter((item): item is string => typeof item === "string")
          : draft.evidencePackIds ?? [];
        const backendDraft: InsightReportDraft = {
          ...draft,
          id: reportId,
          createdAt: `运行 ${runId}`,
          backendRunId: runId,
          backendState: receipt.data.status,
          metricRunId,
          metricResultIds: returnedMetricResultIds,
          metricSnapshots,
          evidencePackIds
        };
        setReportDrafts((current) => [backendDraft, ...current.filter((report) => report.id !== reportId)]);
        setActiveReportId(reportId);
        const generatedReport = await pollInsightReport(backendDraft, operationId, operationContextKey);
        const traceText = receipt.data.trace_id ? `trace ${receipt.data.trace_id.slice(0, 12)}` : "trace 待写入";
        setReportFlowState({
          status: "success",
          stage: "ready",
          attempt: 0,
          metricRunId,
          reportId,
          detail: "报告生成 success，可以创建下游动作。"
        });
        setInsightActionNotice({
          status: "success",
          title: "报告生成成功（success）",
          detail: `${reportId} / ${runId} / ${traceText} / 指标 ${metricResultIds.length}`
        });
        setAgentOutput(`${generatedReport.title} 已使用 ${metricResultIds.length} 个不可变指标快照生成，可继续创建动作。`);
        return generatedReport;
      } catch (error) {
        const detail = error instanceof Error ? error.message : "BFF 请求失败，请重试。";
        const title = reportId ? "报告生成失败（failed）" : metricRunId ? "指标聚合失败（failed）" : "报告链路失败（failed）";
        const retryDetail = `${detail} 可点击“重试生成”继续。`;
        setReportFlowState({
          status: "failed",
          stage: "failed",
          attempt: 0,
          metricRunId,
          reportId,
          detail: retryDetail
        });
        setAgentOutput(`${title}：${detail}`);
        setInsightActionNotice({ status: "error", title, detail: retryDetail });
        return null;
      } finally {
        reportFlowPendingRef.current = false;
      }
    };

  return {
    createReport
  };
}

export type InsightReportExecution = ReturnType<typeof buildInsightReportExecution>;
