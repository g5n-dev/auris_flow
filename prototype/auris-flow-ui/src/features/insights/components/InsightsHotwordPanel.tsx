import type { InsightsController } from "../controller/useInsightsController";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { hotwordCatalog } from "../catalog";
import { RotateCcw } from "lucide-react";

export function InsightsHotwordPanel({ controller }: { controller: InsightsController }) {
  const { hotwordAnalysisNotice, hotwordAnalysisPending, hotwordFilters, hotwordLoadNotice, hotwordStatistics, openHotwordBadcase, runHotwordAnalysis, setAgentOutput, setHotwordFilters, setHotwordLoadRevision, topbarContext } = controller;
  return (
    <section className="module-panel wide hotword-statistics-panel" data-testid="hotword-statistics-panel">
              <PanelHeader
                title="ASR 热词治理"
                subtitle="热词统计 → 易错词确认 → 词包修复；不与业务热门标签混用"
                icon={<button type="button" data-testid="hotword-statistics-refresh" title="刷新当前范围统计" aria-label="刷新热词统计" style={{ display: "grid", width: "100%", height: "100%", padding: 0, placeItems: "center", border: 0, color: "inherit", background: "transparent", cursor: "pointer" }} onClick={() => setHotwordLoadRevision((revision) => revision + 1)}><RotateCcw size={14} /></button>}
                sticky
              />
              <div className="hotword-statistics-toolbar">
                <label>
                  <span>开始日期</span>
                  <input
                    type="date"
                    value={hotwordFilters.startDate}
                    max={hotwordFilters.endDate}
                    onChange={(event) => setHotwordFilters((current) => ({ ...current, startDate: event.target.value }))}
                  />
                </label>
                <label>
                  <span>结束日期</span>
                  <input
                    type="date"
                    value={hotwordFilters.endDate}
                    min={hotwordFilters.startDate}
                    onChange={(event) => setHotwordFilters((current) => ({ ...current, endDate: event.target.value }))}
                  />
                </label>
                <label>
                  <span>门店</span>
                  <select value={hotwordFilters.storeId} onChange={(event) => setHotwordFilters((current) => ({ ...current, storeId: event.target.value }))}>
                    <option value="BJ-AURORA-001">{topbarContext.store}</option>
                    <option value="BJ-SKP-002">北京 SKP 店</option>
                    <option value="SH-JA-002">上海静安体验店</option>
                  </select>
                </label>
                <label>
                  <span>Provider</span>
                  <select value={hotwordFilters.provider} onChange={(event) => setHotwordFilters((current) => ({ ...current, provider: event.target.value }))}>
                    {hotwordCatalog.providers.map((provider) => <option key={provider} value={provider}>{provider}</option>)}
                  </select>
                </label>
                <label>
                  <span>模型</span>
                  <select value={hotwordFilters.modelVersion} onChange={(event) => setHotwordFilters((current) => ({ ...current, modelVersion: event.target.value }))}>
                    {hotwordCatalog.models.map((model, index) => <option key={model} value={model}>{index === 0 ? topbarContext.model : model}</option>)}
                  </select>
                </label>
                <label>
                  <span>词包版本</span>
                  <select value={hotwordFilters.versionId} onChange={(event) => setHotwordFilters((current) => ({ ...current, versionId: event.target.value }))}>
                    {hotwordCatalog.versions.map((version) => <option key={version.id} value={version.id}>{version.label}</option>)}
                  </select>
                </label>
              </div>
              <div className="hotword-statistics-metrics">
                {hotwordCatalog.metrics.map((metric) => {
                  const rawValue = hotwordStatistics.metrics[metric.key];
                  const value = Number.isFinite(rawValue) ? `${rawValue}${metric.suffix}` : "--";
                  return (
                  <button key={metric.key} type="button" onClick={() => setAgentOutput(`${metric.label}：${metric.detail}。当前 ${value}。`)}>
                    <span>{metric.label}</span>
                    <strong>{value}</strong>
                    <em>{metric.detail}</em>
                  </button>
                  );
                })}
              </div>
              <div className="hotword-discovery-summary" data-testid="hotword-annotation-discovery-summary">
                <div>
                  <span>标注修正发现</span>
                  <strong>{hotwordStatistics.discovery.annotation_correction_count} 次</strong>
                </div>
                <span>{hotwordStatistics.discovery.unique_terms} 个词 · {hotwordStatistics.discovery.impacted_session_count} 个会话</span>
                <span>达到“两次人工修正”阈值 {hotwordStatistics.discovery.threshold_met_term_count} 个</span>
                <b>discovery only · 不进入发布门禁</b>
              </div>
              <div className="hotword-governance-layout">
                <div className="hotword-error-table">
                  <div className="hotword-error-head">
                    {['标准词', '识别结果', '错误类型', '统计来源', '可信出现', '标注修正', '易错率', '证据等级', '下游影响', '优先级'].map((label) => <span key={label}>{label}</span>)}
                  </div>
                  {hotwordStatistics.items.map((item) => (
                    <button
                      key={item.badcase_id}
                      type="button"
                      data-testid={`hotword-drilldown-${item.badcase_id}`}
                      onClick={() => openHotwordBadcase(item)}
                    >
                      <strong>{item.canonical_term ?? item.standard_term ?? "待确认"}</strong>
                      <span>{item.recognized_text}</span>
                      <code>{item.error_type}</code>
                      <span>{item.data_source === "mixed" ? "模型 + 标注" : item.data_source === "listening_annotation" ? "人工标注" : "模型快照"}</span>
                      <span>{item.expected_count || "--"}</span>
                      <span>{item.annotation_correction_count ?? 0}</span>
                      <span>{Number.isFinite(item.error_rate) ? `${item.error_rate}%` : "--"}</span>
                      <span>{item.evidence_level}</span>
                      <span>{typeof item.downstream_impact === "string" ? item.downstream_impact : "实体/标签"}</span>
                      <b>{item.priority ?? item.priority_score ?? 0}</b>
                    </button>
                  ))}
                </div>
                <aside className="hotword-statistics-actions">
                  <div className={`operation-toast is-${hotwordLoadNotice.status}${hotwordLoadNotice.status === "error" ? " has-retry" : ""}`} role="status" aria-live="polite">
                    <strong>{hotwordLoadNotice.title}</strong>
                    <span>{hotwordLoadNotice.detail}</span>
                    {hotwordLoadNotice.status === "error" && (
                      <button
                        type="button"
                        className="operation-retry"
                        data-testid="hotword-load-retry"
                        onClick={() => setHotwordLoadRevision((revision) => revision + 1)}
                      >
                        重新加载关联
                      </button>
                    )}
                  </div>
                  <div className={`operation-toast is-${hotwordAnalysisNotice.status}`} role="status" aria-live="polite">
                    <strong>{hotwordAnalysisNotice.title}</strong>
                    <span>{hotwordAnalysisNotice.detail}</span>
                  </div>
                  <button
                    type="button"
                    className="primary"
                    data-testid="hotword-analysis-button"
                    disabled={hotwordAnalysisPending || Boolean(hotwordFilters.startDate && hotwordFilters.endDate && hotwordFilters.startDate > hotwordFilters.endDate)}
                    title={hotwordFilters.startDate > hotwordFilters.endDate ? "开始日期不能晚于结束日期，分析已阻断。" : "按当前筛选创建可追踪分析运行。"}
                    onClick={() => void runHotwordAnalysis()}
                  >
                    {hotwordAnalysisPending ? "分析创建中" : "分析易错词"}
                  </button>
                  <p>{hotwordCatalog.threshold}</p>
                </aside>
              </div>
            </section>
  );
}
