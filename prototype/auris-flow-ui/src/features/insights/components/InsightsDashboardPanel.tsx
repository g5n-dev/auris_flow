import type { InsightsController } from "../controller/useInsightsController";
import { actionFeedbackAttrs } from "../../../shared/runtime/feedbackAttributes";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { Database, Eye, EyeOff, FileText, Plus, Sparkles } from "lucide-react";

export function InsightsDashboardPanel({ controller }: { controller: InsightsController }) {
  const { activeChartId, comparisonScope, currentTab, handleAddChartToReport, handleCreateReportFromDashboard, handleHideChart, handleInsightAgentAction, handleOpenChartSource, hiddenChartIds, insightActionNotice, rangeConfig, renderChart, renderReportCenter, reportDisabledReason, reportFlowActionLabel, reportFlowPending, selectedChartAction, setAgentOutput, setHiddenChartIds, setInsightActionNotice, view, visibleChartSpecs } = controller;
  return (
    <section className="module-panel insight-dashboard-panel">
              <div className="insight-dashboard-head">
                <PanelHeader
                  title={`${view.dimension} 智能 BI`}
                  subtitle={`${rangeConfig.reportScope} · ${comparisonScope} · 图表元素可刷新右侧证据`}
                  icon={<Sparkles size={16} />}
                  sticky
                />
                <div className="insight-dashboard-actions">
                  {hiddenChartIds.length > 0 && (
                    <button
                      type="button"
                      onClick={() => {
                        setHiddenChartIds([]);
                        setInsightActionNotice({
                          status: "success",
                          title: "图表已恢复",
                          detail: "当前看板图表已恢复，后续报告会重新引用可见图表。"
                        });
                        setAgentOutput("已恢复当前看板全部图表，右侧证据和报告草稿将按可见图表重新计算。");
                      }}
                    >
                      <Eye size={14} />
                      显示全部
                    </button>
                  )}
                  <button
                    type="button"
                    className={selectedChartAction?.action === "agent" ? "active" : ""}
                    {...actionFeedbackAttrs("s,e")}
                    onClick={() => handleInsightAgentAction()}
                  >
                    <Sparkles size={14} />
                  </button>
                  <button
                    type="button"
                    className={selectedChartAction?.action === "report" ? "active" : ""}
                    disabled={reportFlowPending}
                    {...actionFeedbackAttrs("p,s,e,d")}
                    title={reportDisabledReason || undefined}
                    onClick={handleCreateReportFromDashboard}
                  >
                    <FileText size={14} />
                    {reportFlowActionLabel("生成报告")}
                  </button>
                </div>
              </div>
              <div className={`insight-action-notice is-${insightActionNotice.status}`}>
                <strong>{insightActionNotice.title}</strong>
                <span>{insightActionNotice.detail}</span>
              </div>
              {currentTab === "reports" ? (
                renderReportCenter()
              ) : (
                <div className="insight-chart-spec-grid">
                  {visibleChartSpecs.length === 0 ? (
                    <div className="insight-empty-state">
                      <EyeOff size={18} />
                      <strong>图表已隐藏</strong>
                      <span>点击“显示全部”恢复当前看板。</span>
                    </div>
                  ) : visibleChartSpecs.map((chart) => (
                    <article key={chart.id} className={`insight-spec-card ${chart.type} ${activeChartId === chart.id ? "active" : ""}`}>
                      <div className="insight-spec-card-head sticky-card-head">
                        <div>
                          <span>{chart.source}</span>
                          <strong>{chart.title}</strong>
                          <em>{chart.subtitle}</em>
                        </div>
                        <div>
                          <button
                            type="button"
                            className={selectedChartAction?.chartId === chart.id && selectedChartAction.action === "source" ? "active" : ""}
                            title="查看数据源"
                            onClick={() => handleOpenChartSource(chart)}
                          >
                            <Database size={14} />
                          </button>
                          <button
                            type="button"
                            className={selectedChartAction?.chartId === chart.id && selectedChartAction.action === "report" ? "active" : ""}
                            title="加入报告"
                            onClick={() => handleAddChartToReport(chart)}
                          >
                            <Plus size={14} />
                          </button>
                          <button
                            type="button"
                            className={selectedChartAction?.chartId === chart.id && selectedChartAction.action === "hide" ? "active" : ""}
                            title="隐藏图表"
                            onClick={() => handleHideChart(chart)}
                          >
                            <EyeOff size={14} />
                          </button>
                        </div>
                      </div>
                      {renderChart(chart)}
                    </article>
                  ))}
                </div>
              )}
            </section>
  );
}
