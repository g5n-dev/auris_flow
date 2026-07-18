import type { EvaluationModuleProps } from "../types";
import type { EvaluationState } from "./useEvaluationState";
import type { EvaluationSelection } from "./buildEvaluationSelection";
import type { EvaluationFocusRecovery } from "./useEvaluationFocusRecovery";
import type { EvaluationContextActions } from "./buildEvaluationContextActions";
import type { HotwordPollingActions } from "./buildHotwordPollingActions";
import type { HotwordVersionRecovery } from "./useHotwordVersionRecovery";
import type { EvaluationRunActions } from "./buildEvaluationRunActions";
import type { EvaluationBadcaseActions } from "./buildEvaluationBadcaseActions";
import type { HotwordGateModel } from "./buildHotwordGateModel";
import type { HotwordReleaseActions } from "./buildHotwordReleaseActions";
import type { EvaluationLabelPromptActions } from "./buildEvaluationLabelPromptActions";
import { TimelineList } from "../../../shared/ui/FactDisplays";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { evaluationCapabilityRows, evaluationDatasets, evaluationLabelingCases, evaluationLabelingMetrics, evaluationManualReviewSeed, evaluationPromptSuggestions } from "../catalog";
import type { EvaluationLabelingCase } from "../types";
import { Activity, AlertTriangle, ArrowRight, BookOpen, BrainCircuit, FileText, Gauge, Headphones, ListFilter, Play, Settings, ShieldCheck, Sparkles, Tags } from "lucide-react";

type BuildEvaluationPrimaryRendersScope = EvaluationModuleProps & EvaluationState & EvaluationSelection & EvaluationFocusRecovery & EvaluationContextActions & HotwordPollingActions & HotwordVersionRecovery & EvaluationRunActions & EvaluationBadcaseActions & HotwordGateModel & HotwordReleaseActions & EvaluationLabelPromptActions;

export function buildEvaluationPrimaryRenders(appliedPromptSuggestions: BuildEvaluationPrimaryRendersScope["appliedPromptSuggestions"], applyPromptSuggestion: BuildEvaluationPrimaryRendersScope["applyPromptSuggestion"], candidatePromptDraft: BuildEvaluationPrimaryRendersScope["candidatePromptDraft"], createPromptCandidate: BuildEvaluationPrimaryRendersScope["createPromptCandidate"], createPromptReleaseDraft: BuildEvaluationPrimaryRendersScope["createPromptReleaseDraft"], evaluationAction: BuildEvaluationPrimaryRendersScope["evaluationAction"], feedbackDraft: BuildEvaluationPrimaryRendersScope["feedbackDraft"], gateRows: BuildEvaluationPrimaryRendersScope["gateRows"], generatePromptSuggestions: BuildEvaluationPrimaryRendersScope["generatePromptSuggestions"], handleLabelingAction: BuildEvaluationPrimaryRendersScope["handleLabelingAction"], jumpToLabelPromptWorkbench: BuildEvaluationPrimaryRendersScope["jumpToLabelPromptWorkbench"], labelVersion: BuildEvaluationPrimaryRendersScope["labelVersion"], labelingCasesForTask: BuildEvaluationPrimaryRendersScope["labelingCasesForTask"], modelVersion: BuildEvaluationPrimaryRendersScope["modelVersion"], narrative: BuildEvaluationPrimaryRendersScope["narrative"], openEvaluationCaseEvidence: BuildEvaluationPrimaryRendersScope["openEvaluationCaseEvidence"], promptExperiment: BuildEvaluationPrimaryRendersScope["promptExperiment"], runEvaluation: BuildEvaluationPrimaryRendersScope["runEvaluation"], runPromptShadowEval: BuildEvaluationPrimaryRendersScope["runPromptShadowEval"], runReceipt: BuildEvaluationPrimaryRendersScope["runReceipt"], runRecordTuples: BuildEvaluationPrimaryRendersScope["runRecordTuples"], runScope: BuildEvaluationPrimaryRendersScope["runScope"], selectCapability: BuildEvaluationPrimaryRendersScope["selectCapability"], selectLabelingTask: BuildEvaluationPrimaryRendersScope["selectLabelingTask"], selectedCapability: BuildEvaluationPrimaryRendersScope["selectedCapability"], selectedDatasetId: BuildEvaluationPrimaryRendersScope["selectedDatasetId"], selectedLabelingCase: BuildEvaluationPrimaryRendersScope["selectedLabelingCase"], selectedLabelingMetric: BuildEvaluationPrimaryRendersScope["selectedLabelingMetric"], selectedPromptSuggestion: BuildEvaluationPrimaryRendersScope["selectedPromptSuggestion"], setActiveModule: BuildEvaluationPrimaryRendersScope["setActiveModule"], setActiveTab: BuildEvaluationPrimaryRendersScope["setActiveTab"], setCandidatePromptDraft: BuildEvaluationPrimaryRendersScope["setCandidatePromptDraft"], setFeedbackDraft: BuildEvaluationPrimaryRendersScope["setFeedbackDraft"], setLabelVersion: BuildEvaluationPrimaryRendersScope["setLabelVersion"], setModelVersion: BuildEvaluationPrimaryRendersScope["setModelVersion"], setRunScope: BuildEvaluationPrimaryRendersScope["setRunScope"], setSelectedDatasetId: BuildEvaluationPrimaryRendersScope["setSelectedDatasetId"], setSelectedLabelingCaseId: BuildEvaluationPrimaryRendersScope["setSelectedLabelingCaseId"], setSelectedManualId: BuildEvaluationPrimaryRendersScope["setSelectedManualId"], setSelectedPromptSuggestionId: BuildEvaluationPrimaryRendersScope["setSelectedPromptSuggestionId"]) {
  const renderAutoView = () => (
      <>
        <section className="module-panel wide evaluation-run-console">
          <PanelHeader title={narrative.title} subtitle={narrative.subtitle} icon={<Gauge size={16} />} />
          <div className="evaluation-auto-layout">
            <section className="evaluation-param-panel">
              <div className="evaluation-section-title">
                <Settings size={15} />
                <span>运行参数</span>
              </div>
              <label>
                <span>评测集</span>
                <select value={selectedDatasetId} onChange={(event) => setSelectedDatasetId(event.target.value)}>
                  {evaluationDatasets.map((dataset) => (
                    <option key={dataset.id} value={dataset.id}>{dataset.name}</option>
                  ))}
                </select>
              </label>
              <label>
                <span>模型版本</span>
                <select value={modelVersion} onChange={(event) => setModelVersion(event.target.value)}>
                  {["prod-v4", "prod-v5", "candidate-2026.07"].map((version) => (
                    <option key={version} value={version}>{version}</option>
                  ))}
                </select>
              </label>
              <label>
                <span>标签版本</span>
                <select value={labelVersion} onChange={(event) => setLabelVersion(event.target.value)}>
                  {["v1.8.4", "v1.9.0-rc2", "v1.9.0-shadow"].map((version) => (
                    <option key={version} value={version}>{version}</option>
                  ))}
                </select>
              </label>
              <label>
                <span>运行范围</span>
                <input value={runScope} onChange={(event) => setRunScope(event.target.value)} />
              </label>
              <button type="button" className="evaluation-primary-action" disabled={evaluationAction === "run"} onClick={runEvaluation}>
                <Play size={14} />
                {evaluationAction === "run" ? "运行中" : "运行评测"}
              </button>
            </section>
            <section className="evaluation-score-panel">
              <div className="evaluation-score-hero">
                <span>综合得分</span>
                <strong>91.2</strong>
                <em>{runReceipt}</em>
              </div>
              <div className="evaluation-score-grid">
                {evaluationCapabilityRows.map((row) => (
                  <button key={row.key} type="button" className={row.key === selectedCapability.key ? "active" : ""} onClick={() => selectCapability(row.key)}>
                    <span>{row.ability}</span>
                    <strong>{row.candidate}</strong>
                    <em className={row.delta.startsWith("-") ? "negative" : "positive"}>{row.delta}</em>
                    <i><b style={{ width: `${Math.min(100, Number(row.candidate))}%` }} /></i>
                  </button>
                ))}
              </div>
            </section>
            <section className="evaluation-gate-stack">
              <div className="evaluation-section-title">
                <ShieldCheck size={15} />
                <span>发布门禁</span>
              </div>
              {gateRows.map((gate) => (
                <button key={gate.label} type="button" className={`evaluation-gate-card ${gate.tone}`} onClick={() => gate.label === "需人工" && setSelectedManualId(evaluationManualReviewSeed[0].id)}>
                  <span>{gate.label}</span>
                  <strong>{gate.value}</strong>
                  <em>{gate.detail}</em>
                </button>
              ))}
            </section>
          </div>
        </section>
        <section className="module-panel wide evaluation-record-panel">
          <PanelHeader title="运行记录" subtitle="评测运行、门禁变化和回流动作都会落到当前项目运行记录" icon={<Activity size={16} />} />
          <TimelineList items={runRecordTuples} />
        </section>
      </>
    );

  const renderLabelingView = () => {
      const visibleCases = labelingCasesForTask.length ? labelingCasesForTask : evaluationLabelingCases.slice(0, 4);
      const issueTypes: EvaluationLabelingCase["issue"][] = ["误打", "漏打", "冲突", "需人审"];
      const metricCards = [
        ["Precision", `${selectedLabelingMetric.precision}%`, "误打控制"],
        ["Recall", `${selectedLabelingMetric.recall}%`, "漏打控制"],
        ["F1", `${selectedLabelingMetric.f1}%`, "版本门禁"],
        ["人审一致率", `${selectedLabelingMetric.humanAgreement}%`, "人工裁决一致"],
        ["冲突率", `${selectedLabelingMetric.conflictRate}%`, "标签/单据/ASR 冲突"],
        ["低置信率", `${selectedLabelingMetric.lowConfidenceRate}%`, "需人审候选"]
      ];
      return (
        <>
          <section className="module-panel wide evaluation-labeling-panel">
            <PanelHeader title={narrative.title} subtitle={narrative.subtitle} icon={<Tags size={16} />} />
            <div className="evaluation-labeling-layout">
              <aside className="evaluation-labeling-task-list">
                <span>标签任务</span>
                {evaluationLabelingMetrics.map((metric) => (
                  <button
                    key={metric.taskKey}
                    type="button"
                    className={metric.taskKey === selectedLabelingMetric.taskKey ? "active" : ""}
                    onClick={() => selectLabelingTask(metric.taskKey)}
                  >
                    <strong>{metric.task}</strong>
                    <em>{metric.samples} 样本 · badcase {metric.badcases}</em>
                    <b>{metric.f1}% F1</b>
                    <small>{metric.promptVersion}</small>
                  </button>
                ))}
              </aside>
              <section className="evaluation-labeling-main">
                <div className="evaluation-version-strip">
                  <div>
                    <span>线上标签版本</span>
                    <strong>{selectedLabelingMetric.onlineVersion}</strong>
                  </div>
                  <ArrowRight size={16} />
                  <div className="candidate">
                    <span>候选标签版本</span>
                    <strong>{selectedLabelingMetric.candidateVersion}</strong>
                  </div>
                  <div>
                    <span>Owner</span>
                    <strong>{selectedLabelingMetric.owner}</strong>
                  </div>
                </div>
                <div className="evaluation-labeling-metrics">
                  {metricCards.map(([label, value, detail]) => (
                    <button key={label} type="button" onClick={() => setFeedbackDraft(`${selectedLabelingMetric.task} / ${label}：${value}，${detail}`)}>
                      <span>{label}</span>
                      <strong>{value}</strong>
                      <em>{detail}</em>
                    </button>
                  ))}
                </div>
                <div className="evaluation-conflict-matrix">
                  <div className="evaluation-section-title">
                    <ListFilter size={15} />
                    <span>标签混淆 / 冲突矩阵</span>
                  </div>
                  <div>
                    {issueTypes.map((issue) => {
                      const taskCount = evaluationLabelingCases.filter((item) => item.taskKey === selectedLabelingMetric.taskKey && item.issue === issue).length;
                      const totalCount = evaluationLabelingCases.filter((item) => item.issue === issue).length;
                      return (
                        <button key={issue} type="button" className={selectedLabelingCase.issue === issue ? "active" : ""} onClick={() => {
                          const nextCase = evaluationLabelingCases.find((item) => item.taskKey === selectedLabelingMetric.taskKey && item.issue === issue) ?? evaluationLabelingCases.find((item) => item.issue === issue);
                          if (nextCase) setSelectedLabelingCaseId(nextCase.id);
                        }}>
                          <strong>{issue}</strong>
                          <span>{taskCount || totalCount}</span>
                          <em>{taskCount ? "当前任务命中" : "跨任务样本"}</em>
                        </button>
                      );
                    })}
                  </div>
                </div>
              </section>
              <aside className="evaluation-labeling-case-list">
                <span>证据样本</span>
                {visibleCases.map((item) => (
                  <button key={item.id} type="button" className={item.id === selectedLabelingCase.id ? "active" : ""} onClick={() => setSelectedLabelingCaseId(item.id)}>
                    <strong>{item.label}</strong>
                    <em>{item.evidenceWindow}</em>
                    <b>{item.issue}</b>
                    <small>{item.confidence}% · {item.source}</small>
                  </button>
                ))}
              </aside>
            </div>
          </section>
          <section className="module-panel evaluation-labeling-evidence">
            <PanelHeader title="当前证据与回流动作" subtitle={`${selectedLabelingCase.id} / ${selectedLabelingCase.source}`} icon={<Headphones size={16} />} />
            <div className="evaluation-labeling-evidence-body">
              <div className="evaluation-evidence-card">
                <span>{selectedLabelingCase.evidenceWindow}</span>
                <strong>{selectedLabelingCase.label}</strong>
                <p>{selectedLabelingCase.asr}</p>
              </div>
              <div className="evaluation-labeling-diff">
                <div><span>人工期望</span><strong>{selectedLabelingCase.expected}</strong></div>
                <div><span>候选输出</span><strong>{selectedLabelingCase.predicted}</strong></div>
                <div><span>问题类型</span><strong>{selectedLabelingCase.issue}</strong></div>
                <div><span>置信度</span><strong>{selectedLabelingCase.confidence}%</strong></div>
              </div>
              <div className="evaluation-action-row compact">
                <button type="button" onClick={() => openEvaluationCaseEvidence()}>下钻证据详情</button>
                <button type="button" onClick={() => handleLabelingAction("review")}>送人审</button>
                <button type="button" onClick={() => handleLabelingAction("badcase")}>加入 badcase</button>
              </div>
              <div className="evaluation-action-row compact">
                <button type="button" onClick={() => handleLabelingAction("rule")}>生成规则候选</button>
                <button type="button" className="primary" onClick={() => {
                  handleLabelingAction("prompt");
                  setActiveTab("prompt");
                }}>
                  生成 Prompt 优化建议
                </button>
                <button type="button" onClick={() => setActiveModule("labels")}>标签管理</button>
              </div>
            </div>
          </section>
        </>
      );
    };

  const renderPromptView = () => {
      const gateCards = [
        ["打标 F1", `${promptExperiment.candidateF1}%`, promptExperiment.candidateF1 >= 92 ? "pass" : "warn"],
        ["冲突率", `${promptExperiment.conflictRate}%`, promptExperiment.conflictRate <= 4 ? "pass" : "warn"],
        ["人审一致率", `${promptExperiment.humanAgreement}%`, promptExperiment.humanAgreement >= 90 ? "pass" : "warn"],
        ["badcase 回归", `${promptExperiment.badcaseRegression}%`, promptExperiment.badcaseRegression >= 95 ? "pass" : "warn"]
      ];
      return (
        <>
          <section className="module-panel wide evaluation-prompt-panel">
            <PanelHeader title={narrative.title} subtitle={narrative.subtitle} icon={<BrainCircuit size={16} />} />
            <div className="evaluation-prompt-layout">
              <section className="evaluation-prompt-badcase">
                <div className="evaluation-section-title">
                  <AlertTriangle size={15} />
                  <span>badcase 输入</span>
                </div>
                <div className="evaluation-prompt-case">
                  <span>{selectedLabelingMetric.task} / {selectedLabelingCase.issue}</span>
                  <strong>{selectedLabelingCase.label}</strong>
                  <p>{selectedLabelingCase.asr}</p>
                  <em>{selectedLabelingCase.evidenceWindow} · {selectedLabelingCase.source}</em>
                </div>
                <div className="evaluation-action-row compact">
                  <button type="button" onClick={generatePromptSuggestions}>生成建议</button>
                  <button type="button" onClick={() => handleLabelingAction("badcase")}>加入 badcase</button>
                  <button type="button" onClick={() => openEvaluationCaseEvidence()}>定位证据</button>
                </div>
              </section>
              <section className="evaluation-prompt-suggestions">
                <div className="evaluation-section-title">
                  <Sparkles size={15} />
                  <span>自动建议</span>
                </div>
                <div className="evaluation-prompt-suggestion-list">
                  {evaluationPromptSuggestions.map((suggestion) => (
                    <button
                      key={suggestion.id}
                      type="button"
                      className={[
                        suggestion.id === selectedPromptSuggestion.id ? "active" : "",
                        appliedPromptSuggestions.includes(suggestion.id) ? "applied" : ""
                      ].filter(Boolean).join(" ")}
                      onClick={() => setSelectedPromptSuggestionId(suggestion.id)}
                    >
                      <span>{suggestion.title}</span>
                      <strong>{suggestion.detail}</strong>
                      <em>{suggestion.impact}</em>
                    </button>
                  ))}
                </div>
                <button type="button" className="evaluation-primary-action" onClick={() => applyPromptSuggestion(selectedPromptSuggestion)}>
                  应用当前建议
                </button>
              </section>
              <section className="evaluation-prompt-candidate">
                <div className="evaluation-section-title">
                  <FileText size={15} />
                  <span>候选 PromptVersion</span>
                </div>
                <div className="evaluation-prompt-version-row">
                  <div><span>Current</span><strong>{promptExperiment.currentVersion}</strong></div>
                  <div><span>Candidate</span><strong>{promptExperiment.candidateVersion}</strong></div>
                </div>
                <label>
                  <span>候选草稿</span>
                  <textarea rows={10} value={candidatePromptDraft} onChange={(event) => setCandidatePromptDraft(event.target.value)} />
                </label>
                <div className="evaluation-action-row compact">
                  <button type="button" onClick={createPromptCandidate}>创建候选</button>
                  <button type="button" className="primary" disabled={evaluationAction === "prompt"} onClick={runPromptShadowEval}>
                    {evaluationAction === "prompt" ? "评测中" : "运行影子评测"}
                  </button>
                  <button type="button" onClick={jumpToLabelPromptWorkbench}>Prompt 工作台</button>
                </div>
              </section>
            </div>
          </section>
          <section className="module-panel wide evaluation-prompt-shadow">
            <PanelHeader title="影子评测与发布门禁" subtitle={`${promptExperiment.dataset} / ${promptExperiment.status}`} icon={<ShieldCheck size={16} />} />
            <div className="evaluation-prompt-shadow-grid">
              <div className="evaluation-prompt-shadow-score">
                <span>Current F1</span>
                <strong>{promptExperiment.currentF1}%</strong>
                <em>{promptExperiment.currentVersion}</em>
              </div>
              <div className="evaluation-prompt-shadow-score candidate">
                <span>Candidate F1</span>
                <strong>{promptExperiment.candidateF1}%</strong>
                <em>{promptExperiment.candidateVersion}</em>
              </div>
              {gateCards.map(([label, value, state]) => (
                <button key={label} type="button" className={`evaluation-prompt-gate ${state}`} onClick={() => setFeedbackDraft(`${label} 门禁：${value}`)}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                  <em>{state === "pass" ? "通过" : "需观察"}</em>
                </button>
              ))}
            </div>
            <div className="evaluation-action-row compact">
              <button type="button" onClick={generatePromptSuggestions}>重新生成建议</button>
              <button type="button" disabled={evaluationAction === "prompt"} onClick={runPromptShadowEval}>
                {evaluationAction === "prompt" ? "提交中" : "重跑影子评测"}
              </button>
              <button type="button" className="primary" disabled={evaluationAction === "prompt_release"} onClick={createPromptReleaseDraft}>
                {evaluationAction === "prompt_release" ? "提交中" : "生成发布草稿"}
              </button>
            </div>
          </section>
          <section className="module-panel evaluation-prompt-trace">
            <PanelHeader title="建议解释" subtitle={`${selectedPromptSuggestion.title} / ${selectedPromptSuggestion.impact}`} icon={<BookOpen size={16} />} />
            <div className="evaluation-prompt-trace-card">
              <span>{selectedPromptSuggestion.title}</span>
              <strong>{selectedPromptSuggestion.detail}</strong>
              <p>{selectedPromptSuggestion.example}</p>
              <em>状态：{promptExperiment.status} · 已应用 {appliedPromptSuggestions.length} 项</em>
            </div>
            <div className="evaluation-feedback-status">
              <Sparkles size={15} />
              <span>{feedbackDraft}</span>
            </div>
          </section>
        </>
      );
    };

  return {
    renderAutoView,
    renderLabelingView,
    renderPromptView
  };
}

export type EvaluationPrimaryRenders = ReturnType<typeof buildEvaluationPrimaryRenders>;
