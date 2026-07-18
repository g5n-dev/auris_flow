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
import type { InsightAgentAction } from "./buildInsightAgentAction";
import type { InsightChartRenderer } from "./buildInsightChartRenderer";
import {
  buildMetricScopePresentation,
  buildMetricScopeSetPresentation
} from "../model/metricScopePresentation";
import { actionFeedbackAttrs } from "../../../shared/runtime/feedbackAttributes";
import { Download, FileText, RotateCcw, Sparkles, X } from "lucide-react";

export function buildInsightReportRenderer(scope: InsightsModuleProps & HotwordInsightsState & InsightDatasetState & InsightTimeRangeState & InsightComparisonState & InsightMetrics & InsightView & InsightSelectionState & InsightEvidenceActions & InsightChartSpecs & InsightChartSelection & InsightContext & InsightReportDraftBuilder & InsightReportState & InsightReportGuards & InsightReportExecution & InsightReportActions & InsightAgentAction & InsightChartRenderer) {
  const { activeReport, contextReportDrafts, deleteReport, exportReport, handleCreateReportFromDashboard, reportDisabledReason, reportFlowActionLabel, reportFlowPending, setActiveReportId } = scope;
  const renderReportCenter = () => (
      <div className="insight-report-workbench">
        <div className="insight-report-library">
          {!contextReportDrafts.length && (
            <div className="insight-empty-state">
              <FileText size={18} />
              <strong>当前业务范围暂无报告</strong>
              <span>租户、项目、门店、时间或版本已变化，请基于当前范围重新生成。</span>
            </div>
          )}
          {contextReportDrafts.map((report) => {
            const comparability = buildMetricScopeSetPresentation(report.metricSnapshots ?? []);
            return (
              <button key={report.id} type="button" className={`insight-report-item ${activeReport?.id === report.id ? "active" : ""}`} onClick={() => setActiveReportId(report.id)}>
                <span>{report.status}{report.backendState ? ` · ${report.backendState}` : ""}</span>
                <strong>{report.title}</strong>
                <em>{report.scope}</em>
                <small>{report.metricResultIds?.length ?? 0}/{report.metricKeys.length} 指标快照 · {comparability.label} · {report.chartIds.length} 图表 · {report.evidenceIds.length} 证据</small>
              </button>
            );
          })}
        </div>
        {!activeReport && (
          <div className="insight-report-editor insight-empty-state">
            <Sparkles size={18} />
            <strong>等待生成当前范围报告</strong>
            <span>系统不会复用其他租户、项目或筛选范围的报告与指标快照。</span>
            <button
              type="button"
              disabled={reportFlowPending}
              {...actionFeedbackAttrs("p,s,e,d")}
              title={reportDisabledReason || undefined}
              onClick={handleCreateReportFromDashboard}
            >
              {reportFlowActionLabel("生成报告")}
            </button>
          </div>
        )}
        {activeReport && (
          <div className="insight-report-editor">
            <div>
              <span>报告预览</span>
              <strong>{activeReport.title}</strong>
              <p>{activeReport.summary}</p>
            </div>
            <div className="insight-report-sections">
              {activeReport.sections.map((section) => (
                <article key={section.title}>
                  <b>{section.title}</b>
                  <p>{section.body}</p>
                </article>
              ))}
            </div>
            <div className="insight-report-sections" aria-label="报告冻结统计口径">
              {activeReport.metricSnapshots?.length ? activeReport.metricSnapshots.map((snapshot) => {
                const presentation = buildMetricScopePresentation(snapshot);
                return (
                  <article key={snapshot.metric_result_id}>
                    <b>{snapshot.metric_key} · {presentation.comparabilityLabel}</b>
                    <p>
                      taxonomy {presentation.taxonomyMode ?? "未返回"}；源版本 {presentation.sourceLabelVersionIds.join(" / ") || "未返回"}；
                      目标版本 {presentation.targetLabelVersionId ?? "未返回"}；Mapping {presentation.mappingBundleId ?? "未返回"}；
                      FactSet Generation {presentation.factSetGeneration ?? "未返回"}；fact_as_of {presentation.factAsOf ?? "未返回"}。
                    </p>
                    <p>{presentation.comparabilityStatus ?? "未返回可比性状态"} · {presentation.comparabilityReasonCodes.join("、") || presentation.hiddenDeltaReason || "服务端未返回额外原因"}</p>
                  </article>
                );
              }) : (
                <article>
                  <b>统计口径尚未绑定</b>
                  <p>当前报告没有返回不可变 metric snapshot/scope；未用筛选条件猜测标签版本，普通涨跌已隐藏。</p>
                </article>
              )}
            </div>
            <div className="insight-report-actions">
              <button
                type="button"
                disabled={reportFlowPending}
                {...actionFeedbackAttrs("p,s,e,d")}
                title={reportDisabledReason || undefined}
                onClick={handleCreateReportFromDashboard}
              >
                <RotateCcw size={14} />
                {reportFlowActionLabel("重生成")}
              </button>
              <button type="button" {...actionFeedbackAttrs("s,e")} onClick={() => exportReport(activeReport, "markdown")}>
                <Download size={14} />
                导出 MD
              </button>
              <button type="button" {...actionFeedbackAttrs("s,e")} onClick={() => exportReport(activeReport, "json")}>
                <Download size={14} />
                导出 JSON
              </button>
              <button type="button" className="danger" {...actionFeedbackAttrs("s,e")} onClick={() => deleteReport(activeReport.id)}>
                <X size={14} />
                删除
              </button>
            </div>
          </div>
        )}
      </div>
    );

  return {
    renderReportCenter
  };
}

export type InsightReportRenderer = ReturnType<typeof buildInsightReportRenderer>;
