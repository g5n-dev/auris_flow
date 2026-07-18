import type { InsightsController } from "../controller/useInsightsController";
import { actionFeedbackAttrs } from "../../../shared/runtime/feedbackAttributes";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { BookOpen, BrainCircuit, FileText, GitBranch, Headphones, Sparkles, UserCheck } from "lucide-react";

export function InsightsSidePanel({ controller }: { controller: InsightsController }) {
  const { activeReport, agentOutput, chartRouteLabel, downstreamAssetFocus, evidenceForMetric, insightTaskActionPending, openInsightTarget, rangeConfig, reportDisabledReason, reportFlowActionLabel, reportFlowPending, reportReadyForAction, runAgentAction, selectedFact, selectedMetric, setInsightActionNotice, taskDisabledReason } = controller;
  return (
    <aside className="module-panel insight-side-panel">
              <PanelHeader title="智能解读 / 证据 / 报告" subtitle="点击指标或图表元素后同步刷新" icon={<BrainCircuit size={16} />} sticky />
              <div className="insight-agent-summary">
                <BrainCircuit size={22} />
                <span>Agentic 洞察助手</span>
                <strong>{selectedMetric.label} · {rangeConfig.label}</strong>
                <p>{agentOutput}</p>
                <div>
                  <button type="button" {...actionFeedbackAttrs("s,e")} onClick={() => runAgentAction("diagnose")}>
                    <Sparkles size={14} />
                    解释波动
                  </button>
                  <button
                    type="button"
                    disabled={reportFlowPending}
                    {...actionFeedbackAttrs("p,s,e,d")}
                    title={reportDisabledReason || undefined}
                    onClick={() => runAgentAction("report")}
                  >
                    <FileText size={14} />
                    {reportFlowActionLabel("生成报告")}
                  </button>
                  <button
                    type="button"
                    disabled={!reportReadyForAction || insightTaskActionPending}
                    {...actionFeedbackAttrs("p,s,e,d")}
                    title={taskDisabledReason || undefined}
                    onClick={() => runAgentAction("task")}
                  >
                    <UserCheck size={14} />
                    {insightTaskActionPending ? "创建中" : reportReadyForAction ? "创建动作" : activeReport?.metricResultIds?.length ? "等待报告" : "先生成报告"}
                  </button>
                </div>
              </div>
              <div className="insight-metric-dictionary">
                <span>指标字典</span>
                <strong>{selectedMetric.label}</strong>
                <p>{selectedMetric.meaning}</p>
                <dl>
                  <div><dt>公式</dt><dd>{selectedMetric.formula}</dd></div>
                  <div><dt>来源</dt><dd>{selectedMetric.source}</dd></div>
                  <div><dt>负责人</dt><dd>{selectedMetric.owner}</dd></div>
                  <div><dt>证据</dt><dd>{selectedMetric.evidenceCount} 条 · {selectedMetric.tags.slice(0, 3).join(" / ")}</dd></div>
                </dl>
                <button type="button" onClick={() => openInsightTarget(evidenceForMetric(selectedMetric)[0] ?? selectedFact, selectedMetric.drilldownRoute, "业务洞察 / 指标字典", selectedMetric.label)}>
                  <GitBranch size={14} />
                  下钻指标证据
                </button>
              </div>
              {selectedFact && (
                <div className="insight-evidence-detail">
                  <span>当前证据</span>
                  <strong>{selectedFact.eventType}</strong>
                  <p>{selectedFact.time} · {selectedFact.store} · {selectedFact.person}</p>
                  <dl>
                    <div><dt>音频</dt><dd>{selectedFact.audio}</dd></div>
                    <div><dt>单据</dt><dd>{selectedFact.doc}</dd></div>
                    <div><dt>资产</dt><dd>{selectedFact.assetKey}</dd></div>
                    <div><dt>分区</dt><dd>{selectedFact.partitionKey}</dd></div>
                    <div><dt>状态</dt><dd>{selectedFact.status}</dd></div>
                  </dl>
                  <div className="insight-evidence-tags">
                    {selectedFact.tags.map((tag) => <button key={tag} type="button" onClick={() => openInsightTarget(selectedFact, "labels", "业务洞察 / 证据标签", tag)}>{tag}</button>)}
                  </div>
                  <div className="insight-evidence-actions">
                    <button type="button" onClick={() => openInsightTarget(selectedFact, selectedFact.route, "业务洞察 / 当前证据", selectedFact.eventType)}>
                      <Headphones size={14} />
                      下钻处理
                    </button>
                    <button type="button" onClick={() => openInsightTarget(selectedFact, "assets", "业务洞察 / 当前证据", selectedFact.assetKey)}>
                      <GitBranch size={14} />
                      看血缘
                    </button>
                  </div>
                </div>
              )}
              {downstreamAssetFocus && (
                <div className="insight-downstream-panel">
                  <div className="insight-downstream-head">
                    <span>下游资产清单</span>
                    <strong>{downstreamAssetFocus.title}</strong>
                    <em>{downstreamAssetFocus.assetKey}</em>
                  </div>
                  <div className="insight-downstream-list">
                    {downstreamAssetFocus.items.map((item) => (
                      <button
                        key={`${downstreamAssetFocus.chartId}-${item.label}`}
                        type="button"
                        onClick={() => {
                          setInsightActionNotice({
                            status: "success",
                            title: `打开${item.label}`,
                            detail: `${item.value} 将进入 ${chartRouteLabel(item.route)}，保留当前图表和证据上下文。`
                          });
                          openInsightTarget(selectedFact, item.route, "业务洞察 / 下游资产清单", item.label);
                        }}
                      >
                        <span>{item.label}</span>
                        <strong>{item.value}</strong>
                        <em>{item.detail}</em>
                      </button>
                    ))}
                  </div>
                  <button type="button" className="insight-downstream-primary" onClick={() => openInsightTarget(selectedFact, downstreamAssetFocus.route, "业务洞察 / 下游资产清单", downstreamAssetFocus.title)}>
                    <GitBranch size={14} />
                    进入{chartRouteLabel(downstreamAssetFocus.route)}
                  </button>
                </div>
              )}
              {activeReport && (
                <div className="insight-side-report">
                  <span>当前报告草稿</span>
                  <strong>{activeReport.title}</strong>
                  <em>{activeReport.metricKeys.length} 指标 · {activeReport.chartIds.length} 图表 · {activeReport.evidenceIds.length} 证据</em>
                  <button type="button" onClick={() => openInsightTarget(selectedFact, "assets", "业务洞察 / 报告草稿", activeReport.title)}>
                    <BookOpen size={14} />
                    查看报告资产
                  </button>
                </div>
              )}
            </aside>
  );
}
