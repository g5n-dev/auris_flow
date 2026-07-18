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
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { labelPromptLifecycle, promptEvalMetrics, promptOptimizationSuggestions } from "../fixtures/governanceCatalog";
import { AlertTriangle, BarChart3, BrainCircuit, Check, Gauge, GitBranch, Plus, Sparkles, Tags, UserCheck } from "lucide-react";

type BuildLabelsWorkbenchRendersScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions & LabelsOptimizationActions & LabelsReviewActions & LabelsPersistenceActions & LabelsPromptActions & LabelsEvaluationActions & LabelsReleaseActions & LabelsCoreRenders & LabelsInputRenders & LabelsDecisionRenders;

export function buildLabelsWorkbenchRenders(activeCandidate: BuildLabelsWorkbenchRendersScope["activeCandidate"], activeIntent: BuildLabelsWorkbenchRendersScope["activeIntent"], activeScenario: BuildLabelsWorkbenchRendersScope["activeScenario"], applyCandidateAction: BuildLabelsWorkbenchRendersScope["applyCandidateAction"], applyPromptSuggestion: BuildLabelsWorkbenchRendersScope["applyPromptSuggestion"], createPromptCandidateVersion: BuildLabelsWorkbenchRendersScope["createPromptCandidateVersion"], experimentState: BuildLabelsWorkbenchRendersScope["experimentState"], extractionState: BuildLabelsWorkbenchRendersScope["extractionState"], labelBadcaseActionHint: BuildLabelsWorkbenchRendersScope["labelBadcaseActionHint"], labelCandidates: BuildLabelsWorkbenchRendersScope["labelCandidates"], labelEntityAction: BuildLabelsWorkbenchRendersScope["labelEntityAction"], labelEvalActionLabel: BuildLabelsWorkbenchRendersScope["labelEvalActionLabel"], labelEvalSubmitDisabled: BuildLabelsWorkbenchRendersScope["labelEvalSubmitDisabled"], openLabelEvidence: BuildLabelsWorkbenchRendersScope["openLabelEvidence"], promptFieldRows: BuildLabelsWorkbenchRendersScope["promptFieldRows"], promptInputs: BuildLabelsWorkbenchRendersScope["promptInputs"], promptVariant: BuildLabelsWorkbenchRendersScope["promptVariant"], releaseDecision: BuildLabelsWorkbenchRendersScope["releaseDecision"], runExtractionTask: BuildLabelsWorkbenchRendersScope["runExtractionTask"], runPromptEval: BuildLabelsWorkbenchRendersScope["runPromptEval"], selectedExperimentMetric: BuildLabelsWorkbenchRendersScope["selectedExperimentMetric"], selectedMetricRow: BuildLabelsWorkbenchRendersScope["selectedMetricRow"], selectedPromptField: BuildLabelsWorkbenchRendersScope["selectedPromptField"], setActionFeedback: BuildLabelsWorkbenchRendersScope["setActionFeedback"], setSelectedCandidateId: BuildLabelsWorkbenchRendersScope["setSelectedCandidateId"], setSelectedExperimentMetric: BuildLabelsWorkbenchRendersScope["setSelectedExperimentMetric"], setSelectedPromptField: BuildLabelsWorkbenchRendersScope["setSelectedPromptField"], updatePromptInput: BuildLabelsWorkbenchRendersScope["updatePromptInput"]) {
  const renderExtractionWorkbench = () => (
      <section className="module-panel wide label-extraction-panel">
        <PanelHeader title="智能抽取任务" subtitle="选择样本、标签目标、模型/Prompt 版本和阈值；结果只写 LabelCandidate" icon={<Sparkles size={16} />} />
        <div className="label-extraction-layout">
          <div className="label-extraction-form">
            {[
              ["数据范围", activeScenario.source, "同门店/同日期/同任务分区"],
              ["标签目标", activeIntent.intent, activeScenario.levels.join(" / ")],
              ["模型/Prompt", "tagger-llm-2026.06", "prompt_quote_guard_v19_rc2"],
              ["抽取策略", "证据优先 + 冲突不覆盖", "命中低置信时送 Human Loop"],
              ["置信阈值", "0.78", "低于阈值只进入候选和 badcase"]
            ].map(([label, value, hint]) => (
              <button key={label} type="button" onClick={() => setActionFeedback(`抽取参数：${label}=${value}。${hint}`)}>
                <span>{label}</span>
                <strong>{value}</strong>
                <em>{hint}</em>
              </button>
            ))}
            <button type="button" className={`label-run-extraction ${extractionState}`} onClick={runExtractionTask} disabled={labelEntityAction === "extraction-run"}>
              <Sparkles size={14} />
              {labelEntityAction === "extraction-run" ? "请求处理中" : extractionState === "failed" ? "重试抽取" : extractionState === "running" ? "刷新抽取状态" : extractionState === "completed" ? "重新运行抽取" : "运行抽取"}
            </button>
          </div>

          <div className="label-candidate-list" aria-label="候选标签列表">
            <div>
              <span>LabelCandidate</span>
              <strong>{labelCandidates.length} 条候选</strong>
            </div>
            {labelCandidates.map((candidate) => (
              <button
                key={candidate.id}
                type="button"
                data-testid={`label-candidate-${candidate.id}`}
                className={candidate.id === activeCandidate.id ? "active" : ""}
                onClick={() => {
                  setSelectedCandidateId(candidate.id);
                setActionFeedback(`已选择 ${candidate.id}，右侧显示证据、Prompt、Trace 和人审状态。`);
                }}
              >
                <span>{candidate.source} · {candidate.level}</span>
                <strong>{candidate.title}</strong>
                <em>{candidate.evidence}</em>
                <b>{candidate.confidence}%</b>
              </button>
            ))}
          </div>

          <aside className="label-candidate-detail">
            <div className="label-candidate-detail-head">
              <div>
                <span>{activeCandidate.id}</span>
                <strong>{activeCandidate.title}</strong>
                <p>{activeCandidate.evidence}</p>
              </div>
              <b>{activeCandidate.confidence}%</b>
            </div>
            <div className="label-candidate-meta">
              {[
                ["来源", activeCandidate.source],
                ["Prompt", activeCandidate.promptVersion],
                ["模型", activeCandidate.modelVersion],
                ["Trace", activeCandidate.traceId],
                ["人审", activeCandidate.humanState],
                ["冲突", activeCandidate.conflict],
                ["影响资产", activeCandidate.assetImpact]
              ].map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
            <div className="label-candidate-actions">
              <button type="button" disabled={labelEntityAction !== null} onClick={() => void applyCandidateAction("accept")}>
                <Check size={14} />
                接受当前
              </button>
              <button type="button" disabled={labelEntityAction !== null} onClick={() => void applyCandidateAction("batch")}>
                <Tags size={14} />
                批量接受
              </button>
              <button type="button" disabled={labelEntityAction !== null} onClick={() => void applyCandidateAction("human")}>
                <UserCheck size={14} />
                送 Human Loop
              </button>
              <button type="button" data-testid="label-badcase-action" disabled={labelEntityAction !== null} title={labelBadcaseActionHint} onClick={() => void applyCandidateAction("badcase")}>
                <AlertTriangle size={14} />
                加入 badcase
              </button>
              <button type="button" className="primary" disabled={labelEntityAction !== null} onClick={() => void applyCandidateAction("rule")}>
                <GitBranch size={14} />
                生成规则候选
              </button>
            </div>
          </aside>
        </div>
      </section>
    );

  const renderPromptWorkbench = () => (
      <section className="module-panel wide label-prompt-panel">
        <PanelHeader title="Prompt 优化工作台" subtitle="Prompt 是资产，不是文本框；修改只创建候选版本并进入影子评测" icon={<BrainCircuit size={16} />} />
        <div className="label-prompt-lifecycle">
          {labelPromptLifecycle.map(([stage, object, detail], index) => (
            <button
              key={stage}
              type="button"
              className={[
                stage === "候选" && promptVariant === "candidate" ? "active" : "",
                stage === "影子评测" && experimentState === "影子评测中" ? "active" : "",
                stage === "发布" && releaseDecision !== "灰度观察" ? "active" : ""
              ].join(" ")}
              onClick={() => setActionFeedback(`${stage}：${object}。${detail}`)}
            >
              <b>{index + 1}</b>
              <strong>{stage}</strong>
              <span>{object}</span>
              <em>{detail}</em>
            </button>
          ))}
        </div>
        <div className="label-prompt-layout">
          <div className="label-prompt-editor">
            <div className="label-prompt-field-tabs">
              {promptFieldRows.map(([field, label, detail]) => (
                <button key={field} type="button" className={selectedPromptField === field ? "active" : ""} onClick={() => setSelectedPromptField(field)}>
                  <strong>{label}</strong>
                  <span>{detail}</span>
                </button>
              ))}
            </div>
            <label className="label-prompt-textarea">
              <span>{promptFieldRows.find((row) => row[0] === selectedPromptField)?.[1]}</span>
              <textarea value={promptInputs[selectedPromptField]} onChange={(event) => updatePromptInput(selectedPromptField, event.target.value)} rows={10} />
            </label>
            <div className="label-prompt-actions">
              <button type="button" onClick={createPromptCandidateVersion}>
                <Plus size={14} />
                创建候选 Prompt
              </button>
              <button type="button" onClick={() => void runPromptEval()} disabled={labelEvalSubmitDisabled}>
                <Gauge size={14} />
                {labelEvalActionLabel}
              </button>
              <button type="button" disabled={labelEntityAction !== null} onClick={() => void applyCandidateAction("human")}>
                <UserCheck size={14} />
                送高风险人审
              </button>
            </div>
          </div>

          <div className="label-prompt-compare">
            <div className="label-prompt-version current">
              <span>当前 Prompt</span>
              <strong>prompt_quote_guard_v18</strong>
              <p>识别报价意图，输出标签和置信度。缺少四层标签层级、冲突原因和 TraceRef。</p>
              <b>线上 v1.8.4</b>
            </div>
            <div className="label-prompt-version candidate">
              <span>候选 Prompt</span>
              <strong>prompt_quote_guard_v19_rc2</strong>
              <p>{promptInputs.definition}</p>
              <b>候选版本 / 影子评测</b>
            </div>
            <div className="label-prompt-suggestions">
              <span>badcase 自动建议</span>
              {promptOptimizationSuggestions.map((suggestion) => (
                <button key={suggestion.title} type="button" onClick={() => applyPromptSuggestion(suggestion.field, suggestion.detail)}>
                  <strong>{suggestion.title}</strong>
                  <em>{suggestion.detail}</em>
                </button>
              ))}
            </div>
          </div>
        </div>
      </section>
    );

  const renderEvaluationWorkbench = () => (
      <section className="module-panel wide label-eval-panel">
        <PanelHeader title="效果评价与 A/B" subtitle="A/B 不改生产数据，只通过 prompt_version / model_version / tag_version / run_tags 做影子运行" icon={<BarChart3 size={16} />} />
        <div className="label-eval-layout">
          <div className="label-eval-datasets">
            {[
              ["固定评测集", "quote-risk-v3", "248 样本 / 42 badcase"],
              ["badcase 回归集", "amount-conflict-regression", "金额冲突、串音污染、单据缺失"],
              ["人工黄金集", "human-gold-v12", "人工接受/拒绝样本"]
            ].map(([title, id, detail]) => (
              <button key={id} type="button" onClick={() => setActionFeedback(`已选择评测集 ${id}：${detail}`)}>
                <span>{title}</span>
                <strong>{id}</strong>
                <em>{detail}</em>
              </button>
            ))}
          </div>
          <div className="label-eval-table">
            <div className="label-eval-row head">
              <span>指标</span>
              <span>当前</span>
              <span>候选</span>
              <span>变化</span>
              <span>结论</span>
            </div>
            {promptEvalMetrics.map(([metric, current, candidate, delta, verdict]) => (
              <button
                key={metric}
                type="button"
                className={selectedExperimentMetric === metric ? "label-eval-row active" : "label-eval-row"}
                onClick={() => {
                  setSelectedExperimentMetric(metric);
                  setActionFeedback(`${metric} 已聚焦：可回到候选标签、Trace 或 Human Loop 查看差异样本。`);
                }}
              >
                <span>{metric}</span>
                <strong>{current}</strong>
                <strong>{candidate}</strong>
                <em>{delta}</em>
                <b>{verdict}</b>
              </button>
            ))}
          </div>
          <aside className="label-ab-config">
            <div>
              <span>影子运行配置</span>
              <strong>{selectedMetricRow[0]} · {selectedMetricRow[4]}</strong>
            </div>
            {[
              ["prompt_version", "prompt_quote_guard_v19_rc2"],
              ["model_version", activeCandidate.modelVersion],
              ["tag_version", "v1.9.0-rc2"],
              ["run_tags", "shadow, arm=A, no-write-prod"],
              ["trace_id", activeCandidate.traceId]
            ].map(([label, value]) => (
              <button key={label} type="button" onClick={() => setActionFeedback(`${label}=${value}`)}>
                <span>{label}</span>
                <strong>{value}</strong>
              </button>
            ))}
            <div className="label-prompt-actions">
              <button type="button" onClick={() => void runPromptEval()} disabled={labelEvalSubmitDisabled}>{labelEvalActionLabel}</button>
              <button type="button" onClick={() => openLabelEvidence("Prompt 证据样本")}>下钻证据样本</button>
              <button type="button" className="primary" data-testid="label-badcase-action" disabled={labelEntityAction !== null} title={labelBadcaseActionHint} onClick={() => void applyCandidateAction("badcase")}>生成回流任务</button>
            </div>
          </aside>
        </div>
      </section>
    );

  return {
    renderExtractionWorkbench,
    renderPromptWorkbench,
    renderEvaluationWorkbench
  };
}

export type LabelsWorkbenchRenders = ReturnType<typeof buildLabelsWorkbenchRenders>;
