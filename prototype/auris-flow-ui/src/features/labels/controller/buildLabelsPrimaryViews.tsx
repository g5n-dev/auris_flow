import type { LabelsModuleProps } from "../types";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { LabelsReleaseState } from "./useLabelsReleaseState";
import type { LabelsCandidateModel } from "./buildLabelsCandidateModel";
import type { LabelsFocusModel } from "./useLabelsFocus";
import type { LabelsChangeModel } from "./buildLabelsChangeModel";
import type { LabelsGovernanceModel } from "./buildLabelsGovernanceModel";
import type { LabelsConflictModel } from "./buildLabelsConflictModel";
import type { LabelsIntentRecovery } from "./useLabelsIntentRecovery";
import type { LabelsNavigationActions } from "./buildLabelsNavigationActions";
import type { LabelsOptimizationActions } from "./buildLabelsOptimizationActions";
import type { LabelsReviewActions } from "./buildLabelsReviewActions";
import type { LabelsPersistenceActions } from "./buildLabelsPersistenceActions";
import type { LabelsPromptActions } from "./buildLabelsPromptActions";
import type { LabelsEvaluationActions } from "./buildLabelsEvaluationActions";
import type { LabelsReleaseActions } from "./buildLabelsReleaseActions";
import type { LabelsCoreRenders } from "./buildLabelsCoreRenders";
import type { LabelsInputRenders } from "./buildLabelsInputRenders";
import type { LabelsDecisionRenders } from "./buildLabelsDecisionRenders";
import type { LabelsWorkbenchRenders } from "./buildLabelsWorkbenchRenders";
import type { LabelsContractRenders } from "./buildLabelsContractRenders";
import type { LabelsRunRailModel } from "./buildLabelsRunRailModel";
import type { LabelsShellRenders } from "./buildLabelsShellRenders";
import { layerLevelConfigs } from "../../../shared/fixtures/labelLayers";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { labelHierarchyBlueprint, promptOptimizationSuggestions } from "../fixtures/governanceCatalog";
import { BrainCircuit, Database, FileText, GitBranch, Plus, Settings, Sparkles, Tags, UserCheck } from "lucide-react";

type BuildLabelsPrimaryViewsScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions & LabelsOptimizationActions & LabelsReviewActions & LabelsPersistenceActions & LabelsPromptActions & LabelsEvaluationActions & LabelsReleaseActions & LabelsCoreRenders & LabelsInputRenders & LabelsDecisionRenders & LabelsWorkbenchRenders & LabelsContractRenders & LabelsRunRailModel & LabelsShellRenders;

export function buildLabelsPrimaryViews(activeCandidate: BuildLabelsPrimaryViewsScope["activeCandidate"], activeCandidateDraft: BuildLabelsPrimaryViewsScope["activeCandidateDraft"], activeIntent: BuildLabelsPrimaryViewsScope["activeIntent"], activeScenario: BuildLabelsPrimaryViewsScope["activeScenario"], applyCandidateAction: BuildLabelsPrimaryViewsScope["applyCandidateAction"], applyPromptSuggestion: BuildLabelsPrimaryViewsScope["applyPromptSuggestion"], backendPromptCandidateId: BuildLabelsPrimaryViewsScope["backendPromptCandidateId"], batchDecisionReceipt: BuildLabelsPrimaryViewsScope["batchDecisionReceipt"], batchPreflightPassed: BuildLabelsPrimaryViewsScope["batchPreflightPassed"], batchPreflightReason: BuildLabelsPrimaryViewsScope["batchPreflightReason"], candidateDrafts: BuildLabelsPrimaryViewsScope["candidateDrafts"], closedLoopReviewProgress: BuildLabelsPrimaryViewsScope["closedLoopReviewProgress"], createLabelDraft: BuildLabelsPrimaryViewsScope["createLabelDraft"], createPromptCandidateVersion: BuildLabelsPrimaryViewsScope["createPromptCandidateVersion"], draftInputs: BuildLabelsPrimaryViewsScope["draftInputs"], draftStatus: BuildLabelsPrimaryViewsScope["draftStatus"], editableDraftTagName: BuildLabelsPrimaryViewsScope["editableDraftTagName"], extractionState: BuildLabelsPrimaryViewsScope["extractionState"], hasAuthoritativeCandidate: BuildLabelsPrimaryViewsScope["hasAuthoritativeCandidate"], hasBoundReviewTask: BuildLabelsPrimaryViewsScope["hasBoundReviewTask"], labelAggregationBackendRun: BuildLabelsPrimaryViewsScope["labelAggregationBackendRun"], labelEntityAction: BuildLabelsPrimaryViewsScope["labelEntityAction"], labelEvalActionLabel: BuildLabelsPrimaryViewsScope["labelEvalActionLabel"], labelEvalSubmitDisabled: BuildLabelsPrimaryViewsScope["labelEvalSubmitDisabled"], labelTaxonomySuggestions: BuildLabelsPrimaryViewsScope["labelTaxonomySuggestions"], lockedLabelVersionId: BuildLabelsPrimaryViewsScope["lockedLabelVersionId"], lockedPromptVersionId: BuildLabelsPrimaryViewsScope["lockedPromptVersionId"], optimizationInputs: BuildLabelsPrimaryViewsScope["optimizationInputs"], promptCandidateFact: BuildLabelsPrimaryViewsScope["promptCandidateFact"], promptFieldRows: BuildLabelsPrimaryViewsScope["promptFieldRows"], promptInputs: BuildLabelsPrimaryViewsScope["promptInputs"], promptReviewProgress: BuildLabelsPrimaryViewsScope["promptReviewProgress"], refreshPromptCandidateFact: BuildLabelsPrimaryViewsScope["refreshPromptCandidateFact"], renderLabelFactReadState: BuildLabelsPrimaryViewsScope["renderLabelFactReadState"], renderV2Page: BuildLabelsPrimaryViewsScope["renderV2Page"], renderV2ScenarioRail: BuildLabelsPrimaryViewsScope["renderV2ScenarioRail"], reviewPromptCandidate: BuildLabelsPrimaryViewsScope["reviewPromptCandidate"], reviewTaxonomySuggestion: BuildLabelsPrimaryViewsScope["reviewTaxonomySuggestion"], runExtractionTask: BuildLabelsPrimaryViewsScope["runExtractionTask"], runPromptEval: BuildLabelsPrimaryViewsScope["runPromptEval"], saveDraftRule: BuildLabelsPrimaryViewsScope["saveDraftRule"], selectLabelCandidate: BuildLabelsPrimaryViewsScope["selectLabelCandidate"], selectedCandidateIds: BuildLabelsPrimaryViewsScope["selectedCandidateIds"], selectedPromptField: BuildLabelsPrimaryViewsScope["selectedPromptField"], sendLabelHumanLoop: BuildLabelsPrimaryViewsScope["sendLabelHumanLoop"], setActionFeedback: BuildLabelsPrimaryViewsScope["setActionFeedback"], setSelectedPromptField: BuildLabelsPrimaryViewsScope["setSelectedPromptField"], toggleLabelCandidateSelection: BuildLabelsPrimaryViewsScope["toggleLabelCandidateSelection"], updateDraftInput: BuildLabelsPrimaryViewsScope["updateDraftInput"], updateOptimizationInput: BuildLabelsPrimaryViewsScope["updateOptimizationInput"], updatePromptInput: BuildLabelsPrimaryViewsScope["updatePromptInput"]) {
  const renderSchemaV2 = () =>
      renderV2Page(
        "schema",
        "标签体系",
        "从标签域、标签组、标签、标签值到 L1-L9 证据轨道，检查当前场景覆盖和缺口。",
        renderV2ScenarioRail(),
        <>
          <PanelHeader title="标签层级与证据轨道" subtitle={`${activeScenario.name} / ${activeIntent.intent}`} icon={<Tags size={16} />} />
          <div className="label-v2-blueprint">
            {labelHierarchyBlueprint.map((item) => (
              <button key={item.level} type="button" onClick={() => setActionFeedback(`${item.level}：${item.contract}`)}>
                <span>{item.level}</span>
                <strong>{item.name}</strong>
                <p>{item.contract}</p>
                <em>{item.example}</em>
              </button>
            ))}
          </div>
          <div className="label-v2-track-grid" aria-label="L1 到 L9 证据轨道映射">
            {layerLevelConfigs.map((level) => {
              const match = activeIntent.layers[level.key];
              return (
                <button
                  key={level.key}
                  type="button"
                  className={match ? "mapped" : "gap"}
                  onClick={() =>
                    setActionFeedback(
                      match
                        ? `${level.label} 已映射：${match.tag} / ${match.state} / ${match.evidence}`
                        : `${level.label} 仍有缺口：需要补充 ${level.tags.slice(0, 3).join("、")} 相关候选。`
                    )
                  }
                >
                  <span>{level.label}</span>
                  <strong>{match?.tag ?? "待补齐"}</strong>
                  <em>{match?.evidence ?? level.tags.slice(0, 4).join(" / ")}</em>
                  <b>{match ? match.state : "缺口"}</b>
                </button>
              );
            })}
          </div>
          {!LABEL_DEMO_MODE ? (
            <div className="label-taxonomy-suggestion-workbench" data-testid="label-taxonomy-suggestions">
              <div className="label-fact-state is-ready">
                <strong>未知标签归一化候选</strong>
                <p>
                  {labelAggregationBackendRun
                    ? `${labelAggregationBackendRun.aggregation_run_id} 仅展示本次运行的 taxonomy_suggestion_ids。`
                    : "完成当前 AggregationRun 后读取建议；未知文本不会直接进入线上标签。"}
                </p>
              </div>
              {labelTaxonomySuggestions.map((suggestion) => {
                const progress = closedLoopReviewProgress[suggestion.suggestion_id];
                return (
                  <div key={suggestion.suggestion_id} className="label-v2-candidate-row">
                    <div>
                      <span>{suggestion.suggestion_id}</span>
                      <strong>{suggestion.raw_labels.join(" / ")}</strong>
                      <em>{suggestion.normalized_label} · {suggestion.observation_ids.length} 条 Observation · {progress?.status ?? suggestion.status}</em>
                    </div>
                    <button
                      type="button"
                      data-testid={`taxonomy-review-${suggestion.suggestion_id}`}
                      disabled={labelEntityAction !== null || ["accepted", "rejected"].includes(progress?.status ?? suggestion.status)}
                      onClick={() => void reviewTaxonomySuggestion(suggestion)}
                    >
                      {progress?.status === "awaiting-adjudication" ? "独立仲裁" : "提交密封审核"}
                    </button>
                  </div>
                );
              })}
            </div>
          ) : null}
        </>,
        <>
          <PanelHeader title="当前对象" subtitle="体系调整会影响候选、评测和发布" icon={<GitBranch size={16} />} />
          <div className="label-v2-detail">
            <strong>{activeIntent.intent}</strong>
            <p>{activeIntent.evidence}</p>
            {activeIntent.trace.map(([label, value]) => (
              <button key={label} type="button" onClick={() => setActionFeedback(`${label}：${value}`)}>
                <span>{label}</span>
                <em>{value}</em>
              </button>
            ))}
          </div>
          <div className="label-v2-actions">
            <button type="button" onClick={createLabelDraft}>
              <Plus size={14} />
              新建候选标签
            </button>
            <button
              type="button"
              onClick={sendLabelHumanLoop}
              disabled={labelEntityAction !== null || !hasAuthoritativeCandidate || !hasBoundReviewTask}
              title={!hasAuthoritativeCandidate ? "请先运行抽取并读取后端 LabelAggregate" : !hasBoundReviewTask ? "当前 LabelAggregate 缺少 review_task_id" : undefined}
            >
              <UserCheck size={14} />
              送人工核准
            </button>
          </div>
        </>
      );

  const renderExtractionV2 = () =>
      renderV2Page(
        "extraction",
        "智能抽取",
        "从 ASR、单据、事件和人工标注生成 LabelCandidate，候选先进入版本草稿和人审队列。",
        <>
          <PanelHeader title="抽取输入" subtitle="输入资产与当前标签目标" icon={<Database size={16} />} />
          <div className="label-v2-list compact">
            {activeScenario.data.map((item) => (
              <button key={item} type="button" onClick={() => setActionFeedback(`输入资产：${item}`)}>
                <span>输入资产</span>
                <strong>{item}</strong>
                <em>{activeScenario.source}</em>
              </button>
            ))}
          </div>
          <div className="label-v2-form-mini">
            <label>
              <span>阈值</span>
              <input value={optimizationInputs.threshold} onChange={(event) => updateOptimizationInput("threshold", event.target.value)} />
            </label>
            <label>
              <span>策略</span>
              <input value={optimizationInputs.strategy} onChange={(event) => updateOptimizationInput("strategy", event.target.value)} />
            </label>
            <button type="button" className="primary" onClick={runExtractionTask} disabled={labelEntityAction === "extraction-run" || (!LABEL_DEMO_MODE && (!lockedLabelVersionId || !lockedPromptVersionId))} title={!LABEL_DEMO_MODE && (!lockedLabelVersionId || !lockedPromptVersionId) ? "先保存 LabelVersion 与绑定的 PromptVersion 草稿" : undefined}>
              {labelEntityAction === "extraction-run" ? "请求处理中" : extractionState === "failed" ? "重试抽取" : extractionState === "running" ? "刷新抽取状态" : "运行抽取"}
            </button>
          </div>
        </>,
        <>
          <PanelHeader title="候选标签队列" subtitle={`${candidateDrafts.length} 条 LabelCandidate / ${lockedLabelVersionId || "LabelVersion 未锁定"}`} icon={<Sparkles size={16} />} />
          {renderLabelFactReadState()}
          <div className="label-v2-candidates">
            {candidateDrafts.map((candidate) => (
              <div key={candidate.id} className={`label-v2-candidate-row ${selectedCandidateIds.includes(candidate.id) ? "is-selected" : ""}`}>
                <label className="label-candidate-select">
                  <input
                    type="checkbox"
                    aria-label={`选择候选 ${candidate.id}`}
                    checked={selectedCandidateIds.includes(candidate.id)}
                    onChange={() => toggleLabelCandidateSelection(candidate.id)}
                  />
                  <span>批量选择</span>
                </label>
                <button
                  type="button"
                  data-testid={`label-candidate-${candidate.id}`}
                  className={candidate.id === activeCandidate.id ? "active" : ""}
                  onClick={() => {
                    selectLabelCandidate(candidate.id);
                    setActionFeedback(`${candidate.id} 已选中：证据、冲突、人审状态和资产写入目标已刷新。`);
                  }}
                >
                  <span>{candidate.level}</span>
                  <strong>{candidate.title}</strong>
                  <p>{candidate.evidence}</p>
                  <em>{candidate.route}</em>
                  <b>{candidate.confidence}%</b>
                </button>
              </div>
            ))}
          </div>
          <div className={`label-batch-preflight ${batchPreflightPassed ? "passed" : "blocked"}`} role="status" data-testid="label-batch-preflight">
            <strong>已选 {selectedCandidateIds.length} 条</strong>
            <span>{batchPreflightReason}</span>
            <button
              type="button"
              data-testid="label-batch-accept"
              disabled={labelEntityAction !== null || !batchPreflightPassed}
              onClick={() => void applyCandidateAction("batch")}
            >
              {labelEntityAction === "human-decision-batch" ? "批量裁决中" : "批量接受"}
            </button>
          </div>
          {batchDecisionReceipt ? (
            <div className="label-batch-receipt" data-testid="label-batch-receipt" role="status">
              <strong>{batchDecisionReceipt.batch_id} · {batchDecisionReceipt.status}</strong>
              <span>success {batchDecisionReceipt.counts.success} / skipped {batchDecisionReceipt.counts.skipped} / failed {batchDecisionReceipt.counts.failed}</span>
              {batchDecisionReceipt.results.map((result) => (
                <div key={result.review_task_id} className={`is-${result.status}`}>
                  <b>{result.aggregate_id ?? result.review_task_id}</b>
                  <em>{result.status}</em>
                  <span>{result.reason_code ?? result.decision ?? "已处理"}</span>
                </div>
              ))}
            </div>
          ) : null}
        </>,
        <>
          <PanelHeader title="候选详情" subtitle="选择候选后可写入版本或送审" icon={<FileText size={16} />} />
          {!hasAuthoritativeCandidate ? <div className="label-fact-state is-empty"><strong>暂无候选详情</strong><p>后端事实物化后可查看证据、版本、冲突与 Trace。</p></div> : null}
          {hasAuthoritativeCandidate ? <div className="label-v2-detail">
            <strong>{activeCandidateDraft.title}</strong>
            <p>{activeCandidateDraft.evidence}</p>
            {[
              ["写入目标", activeCandidateDraft.writeTarget],
              ["Prompt", activeCandidateDraft.promptVersion],
              ["模型", activeCandidateDraft.modelVersion],
              ["冲突", activeCandidateDraft.conflict],
              ["Trace", activeCandidateDraft.traceId],
              ["人审", activeCandidateDraft.humanState]
            ].map(([label, value]) => (
              <button
                key={label}
                type="button"
                data-testid={label === "人审" ? "label-active-review-state" : undefined}
                onClick={() => setActionFeedback(`${label}：${value}`)}
              >
                <span>{label}</span>
                <em>{value}</em>
              </button>
            ))}
          </div> : null}
          <div className="label-v2-actions">
            <button type="button" disabled={labelEntityAction !== null || !hasAuthoritativeCandidate || !hasBoundReviewTask} title={!hasBoundReviewTask ? "当前 Aggregate 缺少 review_task_id" : undefined} onClick={() => void applyCandidateAction("accept")}>接受当前</button>
            <button type="button" disabled={labelEntityAction !== null || !hasAuthoritativeCandidate || !hasBoundReviewTask} title={!hasBoundReviewTask ? "当前 Aggregate 缺少 review_task_id" : undefined} onClick={() => void applyCandidateAction("human")}>送 Human Loop</button>
            <button type="button" className="primary" disabled={labelEntityAction !== null || !hasAuthoritativeCandidate} onClick={() => void applyCandidateAction("rule")}>生成规则候选</button>
          </div>
        </>
      );

  const renderRulesPromptV2 = () =>
      renderV2Page(
        "rules",
        "规则 / Prompt",
        "把标签规则、正负例、冲突策略和 Prompt 资产放在同一个编辑闭环里，保存后只生成候选版本。",
        <>
          <PanelHeader title="编辑字段" subtitle="Prompt 字段与规则字段联动" icon={<BrainCircuit size={16} />} />
          <div className="label-v2-list compact">
            {promptFieldRows.map(([field, label, detail]) => (
              <button
                key={field}
                type="button"
                className={selectedPromptField === field ? "active" : ""}
                onClick={() => setSelectedPromptField(field)}
              >
                <span>{label}</span>
                <strong>{field}</strong>
                <em>{detail}</em>
              </button>
            ))}
          </div>
          <div className="label-v2-chip-group">
            {LABEL_DEMO_MODE ? promptOptimizationSuggestions.map((suggestion) => (
                <button key={suggestion.title} type="button" onClick={() => applyPromptSuggestion(suggestion.field, suggestion.detail)}>
                  {suggestion.title}
                </button>
              )) : (
                <div className="label-fact-state is-ready" data-testid="prompt-suggestion-fact-state">
                  <strong>联调模式不展示静态 Prompt 建议</strong>
                  <p>建议必须来自当前 OptimizationRun 的 badcase/失败簇，并物化为 PromptVersionCandidate。</p>
                </div>
              )}
          </div>
        </>,
        <>
          <PanelHeader title="规则定义" subtitle={`${editableDraftTagName} / ${draftStatus}`} icon={<Settings size={16} />} />
          <div className="label-v2-editor-grid">
            <label>
              <span>规则名称</span>
              <input value={draftInputs.tagName} onChange={(event) => updateDraftInput("tagName", event.target.value)} />
            </label>
            <label>
              <span>标签定义</span>
              <textarea value={draftInputs.definition} onChange={(event) => updateDraftInput("definition", event.target.value)} rows={4} />
            </label>
            <label>
              <span>触发条件</span>
              <textarea value={draftInputs.trigger} onChange={(event) => updateDraftInput("trigger", event.target.value)} rows={3} />
            </label>
            <label>
              <span>冲突策略</span>
              <textarea value={draftInputs.conflict} onChange={(event) => updateDraftInput("conflict", event.target.value)} rows={3} />
            </label>
            <label>
              <span>正例</span>
              <textarea value={draftInputs.positive} onChange={(event) => updateDraftInput("positive", event.target.value)} rows={3} />
            </label>
            <label>
              <span>负例</span>
              <textarea value={draftInputs.negative} onChange={(event) => updateDraftInput("negative", event.target.value)} rows={3} />
            </label>
          </div>
          <div className="label-v2-editor">
            <span>{promptFieldRows.find((row) => row[0] === selectedPromptField)?.[1]}</span>
            <textarea value={promptInputs[selectedPromptField]} onChange={(event) => updatePromptInput(selectedPromptField, event.target.value)} rows={7} />
          </div>
        </>,
        <>
          <PanelHeader title="版本对比与建议" subtitle="当前 Prompt 不被直接覆盖" icon={<GitBranch size={16} />} />
          <div className="label-v2-compare">
            <div>
              <span>当前</span>
              <strong>{optimizationInputs.currentTagVersion}{LABEL_DEMO_MODE ? "" : " · 配置基线"}</strong>
              <p>{LABEL_DEMO_MODE ? "线上 Prompt 只读，保留历史评测和回滚能力。" : "此处只显示父版本配置；后续运行与发布只使用下方后端强 ID。"}</p>
            </div>
            <div>
              <span>候选</span>
              <strong>{lockedLabelVersionId || "LabelVersion 未锁定"}</strong>
              <p>{lockedPromptVersionId || "PromptVersion 未锁定"}</p>
            </div>
          </div>
          <div className="label-v2-actions">
            <button type="button" onClick={saveDraftRule}>保存规则草稿</button>
            <button type="button" onClick={createPromptCandidateVersion}>保存 PromptVersion 草稿</button>
            {!LABEL_DEMO_MODE ? (
              <button
                type="button"
                data-testid="prompt-double-blind-review"
                disabled={!backendPromptCandidateId || labelEntityAction !== null || String(promptCandidateFact?.status ?? "") === "approved"}
                onClick={() => void reviewPromptCandidate()}
              >
                {promptReviewProgress?.status === "awaiting-adjudication" ? "独立仲裁 Prompt" : "提交 Prompt 密封审核"}
              </button>
            ) : null}
            {!LABEL_DEMO_MODE && backendPromptCandidateId ? (
              <button type="button" onClick={() => void refreshPromptCandidateFact()}>刷新 Prompt 审批</button>
            ) : null}
            <button type="button" className="primary" onClick={() => void runPromptEval()} disabled={labelEvalSubmitDisabled}>{labelEvalActionLabel}</button>
          </div>
          {!LABEL_DEMO_MODE ? (
            <div className="label-release-eval-binding" data-testid="prompt-review-binding">
              <span>Prompt 强事实</span>
              <strong>{backendPromptCandidateId || "尚无 PromptVersionCandidate"}</strong>
              <em>{lockedPromptVersionId || "PromptVersion 未物化"} · {String(promptCandidateFact?.status ?? "awaiting-review")}</em>
            </div>
          ) : null}
        </>
      );

  return {
    renderSchemaV2,
    renderExtractionV2,
    renderRulesPromptV2
  };
}

export type LabelsPrimaryViews = ReturnType<typeof buildLabelsPrimaryViews>;
