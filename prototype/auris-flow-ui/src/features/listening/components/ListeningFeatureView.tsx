import { apiRequest } from "../../../api/client";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { actionFeedbackAttrs } from "../../../shared/runtime/feedbackAttributes";
import { DeepLinkSourceBar } from "../../../shared/ui/DeepLinkSourceBar";
import { LazyBranchBoundary } from "../../../shared/ui/LazyBranchBoundary";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { getReceptionCandidatesForSample } from "../fixtures/reviewSamples";
import type { ListeningPresentation } from "../model/listeningPresentation";
import { EvidenceMode } from "./evidence/EvidenceMode";
import { EvidencePageConfigPanel } from "./evidence/EvidencePageConfigPanel";
import { MatrixMode } from "./matrix/MatrixMode";
import { ReviewDecisionTechnicalDetails } from "./ReviewDecisionTechnicalDetails";
import { SimpleMode } from "./simple/SimpleMode";
import { FileText, Headphones, ListFilter, RotateCcw, Search, Settings, ShieldCheck, X } from "lucide-react";
import { useState } from "react";

export function ListeningFeatureView({ controller }: { controller: ListeningPresentation }) {
  const { activeChip, activeQueue, activeReviewSummary, activeSample, agentState, appealComposerOpen, appealPending, appealReason, changeReviewQueue, completedSampleIds, confirmAndMoveNext, createdAppeal, evidencePageConfig, focus, getModuleTitle, latestReviewDecision, listeningActionPending, listeningModes, listeningNotice, listeningQuery, listeningReadDetail, listeningReadState, listeningRunState, listeningScope, listeningTool, lowConfidence, markState, mode, navigateModuleRoot, navigateToTarget, pageConfigOpen, panelTab, recordReviewChange, reviewQueueItems, selectedLabel, selectedWindow, setActiveChip, setAppealComposerOpen, setAppealReason, setEvidencePageConfig, setListeningNotice, setListeningQuery, setListeningReadRetry, setListeningScope, setListeningTool, setMode, setPageConfigOpen, setPanelTab, setSelectedWindow, submitLatestQualityAppeal, toggleListeningTool, updateAgentDecision, updateLowConfidence, updateMarkState } = controller;
  const [traceReadPending, setTraceReadPending] = useState(false);
  const viewLatestRootTrace = async () => {
    const rootTraceId = latestReviewDecision?.rootTraceId;
    if (!rootTraceId || traceReadPending) return;
    setTraceReadPending(true);
    setListeningNotice({
      status: "pending",
      title: "正在回读业务根 Trace",
      detail: `${rootTraceId} 将通过当前租户/项目上下文从 BFF 查询。`
    });
    try {
      const response = await apiRequest<{
        trace_id?: string;
        spans?: unknown[];
        nodes?: unknown[];
        edges?: unknown[];
      }>(`/v1/traces/${encodeURIComponent(rootTraceId)}`);
      if (response.data.trace_id !== rootTraceId) {
        throw new Error(
          `Trace 回读对象不一致：期望 ${rootTraceId}，实际 ${response.data.trace_id || "unknown"}`
        );
      }
      setListeningNotice({
        status: "success",
        title: "业务根 Trace 已回读",
        detail: `${rootTraceId} / ${response.data.spans?.length ?? 0} 个跨度 / ${response.data.nodes?.length ?? 0} 个对象 / ${response.data.edges?.length ?? 0} 条关系。`
      });
    } catch (error) {
      setListeningNotice({
        status: "error",
        title: "业务根 Trace 读取失败",
        detail: error instanceof Error ? error.message : `${rootTraceId} 无法从 BFF 回读。`
      });
    } finally {
      setTraceReadPending(false);
    }
  };
  return (
    listeningReadState !== "ready" ? (
                  <section className="module-panel wide" data-testid={`listening-read-${listeningReadState}`} aria-live="polite">
                    <PanelHeader
                      title={listeningReadState === "loading" || listeningReadState === "idle" ? "正在加载调听事实" : listeningReadState === "complete" ? "当前队列复核完成" : listeningReadState === "empty" ? "暂无待复核会话" : "调听事实读取失败"}
                      subtitle="HumanReviewTask → EvidencePack → AudioSession → ASR / 说话人 / 事件 / 标注"
                      icon={<Headphones size={16} />}
                    />
                    <div className={`operation-toast is-${listeningReadState === "error" ? "error" : listeningReadState === "empty" || listeningReadState === "complete" ? "idle" : "pending"}`}>
                      <strong>{listeningReadState === "complete" ? "服务端 pending 队列已为空" : listeningReadState === "empty" ? "当前筛选范围没有待办" : listeningReadState === "error" ? "未使用本地 fixture 兜底" : "正在组合权威读模型"}</strong>
                      <span>{listeningReadDetail}</span>
                      {(listeningReadState === "empty" || listeningReadState === "complete" || listeningReadState === "error") && (
                        <button type="button" onClick={() => setListeningReadRetry((current) => current + 1)}>
                          <RotateCcw size={14} />
                          {listeningReadState === "complete" ? "查看其他待审队列" : "重新读取"}
                        </button>
                      )}
                    </div>
                  </section>
                ) : (
                <>
                  <div
                    className="workspace-head module-head listening-head"
                    data-listening-source={LABEL_DEMO_MODE ? "demo" : "bff"}
                    data-listening-task-id={activeSample.reviewTaskId ?? ""}
                    title={listeningReadDetail}
                  >
                    <div className="listening-case-summary">
                      <div className="eyebrow">当前会话</div>
                      <h1>调听工作台</h1>
                      <div className="listening-case-chips" aria-label="当前证据上下文">
                        <span>{activeSample.sessionId}</span>
                        <span>{selectedWindow}</span>
                        <span title={activeSample.file}>{activeSample.file}</span>
                      </div>
                    </div>

                    <div
                      className="module-scope listening-scope review-status-summary"
                      aria-label="当前复核摘要"
                      title={`${LABEL_DEMO_MODE ? "演示数据源" : "BFF 权威读模型"}：${activeReviewSummary.api} / ${activeReviewSummary.dagsterAsset}`}
                    >
                      <Headphones size={16} />
                      <div className="review-status-copy">
                        <span>
                          {activeReviewSummary.title} · {activeReviewSummary.state} · 置信度 {activeReviewSummary.confidence}%
                        </span>
                        <small>
                          联动 {activeReviewSummary.queue}({activeReviewSummary.queueCount}) / {activeReviewSummary.dataAssetId} / {activeReviewSummary.assetKey}
                        </small>
                      </div>
                    </div>

                    <div className="listening-action-strip quick-actions" aria-label="调听工具">
                      <button onClick={() => setPageConfigOpen(true)} title="页面配置">
                        <Settings size={15} />
                        <span>配置</span>
                      </button>
                      <button
                        className={listeningTool === "search" ? "active" : ""}
                        aria-expanded={listeningTool === "search"}
                        disabled={!LABEL_DEMO_MODE}
                        onClick={() => toggleListeningTool("search")}
                        title={!LABEL_DEMO_MODE ? "生产搜索尚未绑定 BFF 查询参数，当前禁用。" : "DEMO：搜索证据"}
                      >
                        <Search size={15} />
                        <span>搜证据</span>
                      </button>
                      <button
                        className={listeningTool === "filter" ? "active" : ""}
                        aria-expanded={listeningTool === "filter"}
                        disabled={!LABEL_DEMO_MODE}
                        onClick={() => toggleListeningTool("filter")}
                        title={!LABEL_DEMO_MODE ? "生产筛选尚未绑定 BFF 查询参数，当前禁用。" : "DEMO：过滤复核队列"}
                      >
                        <ListFilter size={15} />
                        <span>过滤</span>
                      </button>
                      <button
                        className={listeningTool === "reception" ? "active" : ""}
                        aria-expanded={listeningTool === "reception"}
                        disabled={!LABEL_DEMO_MODE}
                        onClick={() => toggleListeningTool("reception")}
                        title={!LABEL_DEMO_MODE ? "生产 EventLink 候选尚未绑定对象版本，当前禁用。" : "DEMO：关联销售接待单"}
                      >
                        <FileText size={15} />
                        <span>接待单</span>
                      </button>
                      <button
                        className={listeningTool === "rerun" || listeningRunState === "pending" ? "active" : ""}
                        aria-expanded={listeningTool === "rerun"}
                        onClick={() => toggleListeningTool("rerun")}
                        disabled={!LABEL_DEMO_MODE || listeningRunState === "pending"}
                        title={!LABEL_DEMO_MODE ? "生产模式未接入受控重跑 TaskRun，当前操作已禁用。" : listeningRunState === "pending" ? "当前证据链正在重跑，完成后可再次发起。" : "重跑当前证据链"}
                        {...actionFeedbackAttrs("p,s,e,d")}
                      >
                        <RotateCcw size={15} />
                        <span>{listeningRunState === "pending" ? "重跑中" : "重跑"}</span>
                      </button>
                    </div>
                  </div>

                  {focus?.module === "listening" && (
                    <DeepLinkSourceBar target={focus} onBack={focus.origin?.module ? () => navigateModuleRoot(focus.origin?.module ?? "home") : undefined} getModuleTitle={getModuleTitle} />
                  )}

                  <div className="listening-control-row">
                    <div className="listening-mode-switch module-tabs" aria-label="工作模式" role="tablist">
                      {listeningModes.map((item) => (
                        <button
                          key={item.id}
                          className={mode === item.id ? "active" : ""}
                          aria-selected={mode === item.id}
                          role="tab"
                          disabled={!LABEL_DEMO_MODE && item.id === "matrix"}
                          title={
                            !LABEL_DEMO_MODE && item.id === "matrix"
                              ? "生产串音矩阵尚未读取权威设备关系，fixture 编辑已禁用。"
                              : undefined
                          }
                          onClick={() => {
                            setMode(item.id);
                            setListeningScope(item.scope);
                          }}
                        >
                          <span>{item.label}</span>
                          <small>{item.note}</small>
                        </button>
                      ))}
                    </div>

                    <div className="listening-queue-strip" aria-label="复核队列切换">
                      <span className="queue-strip-label">复核队列</span>
                      {reviewQueueItems.map((item) => (
                        <button
                          key={item.label}
                          data-testid={`review-head-queue-${item.label}`}
                          className={activeQueue === item.label ? "active" : ""}
                          data-tone={item.tone}
                          onClick={() => changeReviewQueue(item.label)}
                        >
                          <span>{item.label}</span>
                          <b>{item.count}</b>
                          <small>{item.hint}</small>
                        </button>
                      ))}
                    </div>
                  </div>

                  {listeningTool && (
                    <section className="listening-tool-panel" aria-label="调听工具面板">
                      <div>
                        <strong>{listeningTool === "search" ? "证据检索" : listeningTool === "filter" ? "队列过滤" : listeningTool === "reception" ? "销售接待单关联" : "重跑当前证据链"}</strong>
                        <span>
                          {listeningTool === "search"
                            ? `在 ${activeSample.sessionId}、${selectedWindow} 和关联资产中查找证据。`
                            : listeningTool === "filter"
                              ? "按风险类型、置信度、门店、说话人和处理状态缩小复核范围。"
                              : listeningTool === "reception"
                                ? `${activeSample.customer} · ${getReceptionCandidatesForSample(activeSample)[0]?.orderNo ?? "无候选"}`
                                : "只重跑当前证据样本的转写、串音、标签和单据比对，不影响整批任务。"}
                        </span>
                      </div>
                      {listeningTool === "search" && (
                        <input value={listeningQuery} onChange={(event) => setListeningQuery(event.target.value)} placeholder="搜索时间点、说话人、金额、单据号、资产 Key" autoFocus />
                      )}
                      {listeningTool === "filter" && (
                        <div className="listening-filter-chips">
                          {[activeReviewSummary.queue, activeReviewSummary.state, `置信度 ${activeReviewSummary.confidence}%`, activeReviewSummary.dataAssetId, "未完成"].map((chip) => (
                            <button key={chip}>{chip}</button>
                          ))}
                        </div>
                      )}
                      {listeningTool === "reception" && (
                        <div className="listening-filter-chips">
                          {getReceptionCandidatesForSample(activeSample).map((candidate) => (
                            <button
                              key={candidate.id}
                              onClick={() => {
                                setSelectedWindow(candidate.window);
                                setPanelTab("docs");
                              }}
                            >
                              {candidate.orderNo.replace("接待单 ", "")} · {candidate.match}%
                            </button>
                          ))}
                        </div>
                      )}
                      {listeningTool === "rerun" && (
                        <div className="listening-rerun-preview">
                          <span>调度任务</span>
                          <b>{activeReviewSummary.refreshJob}</b>
                          <em>{activeReviewSummary.assetKey}</em>
                        </div>
                      )}
                      <button className="listening-tool-close" onClick={() => setListeningTool(null)} aria-label="关闭工具面板">
                        <X size={15} />
                      </button>
                    </section>
                  )}

                  <div className={`operation-toast listening-operation-toast is-${listeningNotice.status} ${latestReviewDecision && !createdAppeal ? "has-action" : ""}`} role="status" aria-live="polite">
                    <strong>{listeningNotice.title}</strong>
                    <span>{listeningNotice.detail}</span>
                    {latestReviewDecision?.rootTraceId && (
                      <button
                        type="button"
                        className="listening-appeal-trigger"
                        data-testid="listening-view-trace"
                        onClick={() => void viewLatestRootTrace()}
                        disabled={traceReadPending}
                        title={traceReadPending ? "正在回读业务根 Trace。" : `查看业务根 Trace ${latestReviewDecision.rootTraceId}`}
                      >
                        <FileText size={14} />
                        {traceReadPending ? "读取 Trace…" : "查看 Trace"}
                      </button>
                    )}
                    {latestReviewDecision && !createdAppeal && (
                      <button
                        type="button"
                        className="listening-appeal-trigger"
                        onClick={() => setAppealComposerOpen(true)}
                        disabled={appealPending}
                        title={appealPending ? "申诉正在提交，完成后可继续处理。" : "提出质检申诉并在提交后显示立案回执。"}
                        {...actionFeedbackAttrs("p,s,e,d")}
                      >
                        <ShieldCheck size={14} />
                        提出申诉
                      </button>
                    )}
                    <ReviewDecisionTechnicalDetails
                      affectedObjects={latestReviewDecision?.affectedObjects ?? []}
                    />
                  </div>

                  {mode === "evidence" && (
                    <LazyBranchBoundary label="证据审查模式" minHeight={620} resetKey={mode} testId="listening-evidence-mode">
                      <EvidenceMode
                        panelTab={panelTab}
                        setPanelTab={setPanelTab}
                        setMode={setMode}
                        setListeningScope={setListeningScope}
                        selectedWindow={selectedWindow}
                        setSelectedWindow={setSelectedWindow}
                        markState={markState}
                        setMarkState={updateMarkState}
                        lowConfidence={lowConfidence}
                        setLowConfidence={updateLowConfidence}
                        onReviewChange={recordReviewChange}
                        selectedLabel={selectedLabel}
                        agentState={agentState}
                        setAgentState={updateAgentDecision}
                        activeQueue={activeQueue}
                        setActiveQueue={changeReviewQueue}
                        activeChip={activeChip}
                        setActiveChip={setActiveChip}
                        pageConfig={evidencePageConfig}
                        sample={activeSample}
                        navigateToTarget={navigateToTarget}
                        completedCount={completedSampleIds.length}
                        onConfirmNext={confirmAndMoveNext}
                        confirmPending={listeningActionPending}
                      />
                    </LazyBranchBoundary>
                  )}

                  {mode === "simple" && (
                    <LazyBranchBoundary label="简易调听模式" minHeight={620} resetKey={mode} testId="listening-simple-mode">
                      <SimpleMode
                        setMode={setMode}
                        activeChip={activeChip}
                        setActiveChip={setActiveChip}
                        listeningScope={listeningScope}
                        setListeningScope={setListeningScope}
                        sample={activeSample}
                      />
                    </LazyBranchBoundary>
                  )}
                  {mode === "matrix" && (
                    <LazyBranchBoundary label="串音矩阵模式" minHeight={620} resetKey={mode} testId="listening-matrix-mode">
                      <MatrixMode
                        setMode={setMode}
                        selectedWindow={selectedWindow}
                        setSelectedWindow={setSelectedWindow}
                        setMarkState={updateMarkState}
                      />
                    </LazyBranchBoundary>
                  )}
                  {pageConfigOpen && (
                    <LazyBranchBoundary label="证据页配置" minHeight={360} resetKey={String(pageConfigOpen)} testId="listening-page-config">
                      <EvidencePageConfigPanel
                        config={evidencePageConfig}
                        setConfig={setEvidencePageConfig}
                        close={() => setPageConfigOpen(false)}
                      />
                    </LazyBranchBoundary>
                  )}
                  {appealComposerOpen && latestReviewDecision && (
                    <div
                      className="entity-modal-scrim"
                      role="presentation"
                      onMouseDown={(event) => event.target === event.currentTarget && !appealPending && setAppealComposerOpen(false)}
                    >
                      <section className="entity-modal-panel quality-appeal-modal" role="dialog" aria-modal="true" aria-label="提出质检申诉">
                        <div className="entity-modal-head">
                          <div>
                            <span>质检治理 / 单案复议</span>
                            <strong>提出质检申诉</strong>
                            <p>申诉只生成独立案件，不修改原人工决定；领取和裁决必须由独立复议人完成。</p>
                          </div>
                          <button type="button" aria-label="关闭" onClick={() => setAppealComposerOpen(false)} disabled={appealPending}>
                            <X size={16} />
                          </button>
                        </div>
                        <div className="quality-appeal-source">
                          <div><span>源决定</span><strong>{latestReviewDecision.decisionId}</strong></div>
                          <div><span>复核任务</span><strong>{latestReviewDecision.reviewTaskId}</strong></div>
                          <div><span>根 Trace</span><strong>{latestReviewDecision.rootTraceId}</strong></div>
                          <div><span>来源样本</span><strong>{latestReviewDecision.sampleTitle}</strong></div>
                          <div><span>冻结证据</span><strong>{latestReviewDecision.evidenceRefs.join(" / ")}</strong></div>
                        </div>
                        <label className="quality-appeal-reason">
                          <span>申诉理由 <b>必填</b></span>
                          <textarea
                            rows={5}
                            value={appealReason}
                            onChange={(event) => setAppealReason(event.target.value)}
                            placeholder="说明原结论遗漏的证据、事实或适用规则；不要填写敏感原始音频 URL。"
                            autoFocus
                          />
                          <small>{appealReason.trim().length}/2000 · 至少 8 个字符</small>
                        </label>
                        <div className="entity-modal-actions">
                          <button type="button" onClick={() => setAppealComposerOpen(false)} disabled={appealPending}>取消</button>
                          <button
                            type="button"
                            className="primary"
                            onClick={() => void submitLatestQualityAppeal()}
                            disabled={appealPending || appealReason.trim().length < 8}
                            title={
                              appealPending
                                ? "申诉正在提交，完成后会显示立案回执。"
                                : appealReason.trim().length < 8
                                  ? "申诉理由至少需要 8 个字符。"
                                  : "提交后会显示立案结果和 trace。"
                            }
                            {...actionFeedbackAttrs("p,s,e,d")}
                          >
                            {appealPending ? "提交中..." : "提交申诉"}
                          </button>
                        </div>
                      </section>
                    </div>
                  )}
                </>
                )
  );
}
