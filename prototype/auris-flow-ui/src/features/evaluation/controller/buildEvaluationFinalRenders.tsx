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
import type { EvaluationPrimaryRenders } from "./buildEvaluationPrimaryRenders";
import type { EvaluationManualRenders } from "./buildEvaluationManualRenders";
import type { EvaluationCapabilityKey } from "../../../shared/contracts/evaluation";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { evaluationModelCompareRows } from "../catalog";
import type { EvaluationBadcaseWorkflowItem } from "../types";
import { AlertTriangle, BarChart3, Eye, FileText, ShieldCheck } from "lucide-react";

type BuildEvaluationFinalRendersScope = EvaluationModuleProps & EvaluationState & EvaluationSelection & EvaluationFocusRecovery & EvaluationContextActions & HotwordPollingActions & HotwordVersionRecovery & EvaluationRunActions & EvaluationBadcaseActions & HotwordGateModel & HotwordReleaseActions & EvaluationLabelPromptActions & EvaluationPrimaryRenders & EvaluationManualRenders;

export function buildEvaluationFinalRenders(addSelectedBadcaseToHotwordCandidate: BuildEvaluationFinalRendersScope["addSelectedBadcaseToHotwordCandidate"], approveHotwordCandidate: BuildEvaluationFinalRendersScope["approveHotwordCandidate"], badcaseCapabilityFilter: BuildEvaluationFinalRendersScope["badcaseCapabilityFilter"], badcaseWorkflow: BuildEvaluationFinalRendersScope["badcaseWorkflow"], canApproveHotwordVersion: BuildEvaluationFinalRendersScope["canApproveHotwordVersion"], canPublishHotwordVersion: BuildEvaluationFinalRendersScope["canPublishHotwordVersion"], createFeedbackTask: BuildEvaluationFinalRendersScope["createFeedbackTask"], currentView: BuildEvaluationFinalRendersScope["currentView"], evaluationAction: BuildEvaluationFinalRendersScope["evaluationAction"], evaluationNotice: BuildEvaluationFinalRendersScope["evaluationNotice"], hotwordBadcaseRecovery: BuildEvaluationFinalRendersScope["hotwordBadcaseRecovery"], hotwordBaselineVersion: BuildEvaluationFinalRendersScope["hotwordBaselineVersion"], hotwordCandidateVersion: BuildEvaluationFinalRendersScope["hotwordCandidateVersion"], hotwordEvalPassed: BuildEvaluationFinalRendersScope["hotwordEvalPassed"], hotwordEvalResult: BuildEvaluationFinalRendersScope["hotwordEvalResult"], hotwordEvalRunId: BuildEvaluationFinalRendersScope["hotwordEvalRunId"], hotwordGateRows: BuildEvaluationFinalRendersScope["hotwordGateRows"], hotwordPublishRetryRunRef: BuildEvaluationFinalRendersScope["hotwordPublishRetryRunRef"], hotwordPublished: BuildEvaluationFinalRendersScope["hotwordPublished"], hotwordVersionLoading: BuildEvaluationFinalRendersScope["hotwordVersionLoading"], moveBadcaseStatus: BuildEvaluationFinalRendersScope["moveBadcaseStatus"], openEvaluationAssetLineage: BuildEvaluationFinalRendersScope["openEvaluationAssetLineage"], openEvaluationBadcaseEvidence: BuildEvaluationFinalRendersScope["openEvaluationBadcaseEvidence"], openEvaluationCaseEvidence: BuildEvaluationFinalRendersScope["openEvaluationCaseEvidence"], publishHotwordCandidate: BuildEvaluationFinalRendersScope["publishHotwordCandidate"], renderAutoView: BuildEvaluationFinalRendersScope["renderAutoView"], renderLabelingView: BuildEvaluationFinalRendersScope["renderLabelingView"], renderManualView: BuildEvaluationFinalRendersScope["renderManualView"], renderPromptView: BuildEvaluationFinalRendersScope["renderPromptView"], renderSetsView: BuildEvaluationFinalRendersScope["renderSetsView"], runEvaluation: BuildEvaluationFinalRendersScope["runEvaluation"], runHotwordShadowEval: BuildEvaluationFinalRendersScope["runHotwordShadowEval"], selectCapability: BuildEvaluationFinalRendersScope["selectCapability"], selectedBadcaseDraft: BuildEvaluationFinalRendersScope["selectedBadcaseDraft"], selectedBadcaseResolved: BuildEvaluationFinalRendersScope["selectedBadcaseResolved"], selectedBadcaseWorkflow: BuildEvaluationFinalRendersScope["selectedBadcaseWorkflow"], selectedCompareRow: BuildEvaluationFinalRendersScope["selectedCompareRow"], selectedDataset: BuildEvaluationFinalRendersScope["selectedDataset"], setActiveModule: BuildEvaluationFinalRendersScope["setActiveModule"], setActiveTab: BuildEvaluationFinalRendersScope["setActiveTab"], setBadcaseCapabilityFilter: BuildEvaluationFinalRendersScope["setBadcaseCapabilityFilter"], setSelectedBadcaseId: BuildEvaluationFinalRendersScope["setSelectedBadcaseId"], setSelectedCapabilityKey: BuildEvaluationFinalRendersScope["setSelectedCapabilityKey"], setSelectedDatasetId: BuildEvaluationFinalRendersScope["setSelectedDatasetId"], updateBadcaseDraft: BuildEvaluationFinalRendersScope["updateBadcaseDraft"], visibleBadcaseWorkflow: BuildEvaluationFinalRendersScope["visibleBadcaseWorkflow"]) {
  const renderCompareView = () => (
      <>
        <section className="module-panel wide evaluation-compare-panel">
          <PanelHeader title="候选模型对比台" subtitle={`${selectedDataset.name} / prod-v4 vs prod-v5 vs candidate`} icon={<BarChart3 size={16} />} />
          <div className="evaluation-model-columns">
            {["prod-v4", "prod-v5", "candidate"].map((model) => (
              <div key={model} className={model === selectedCompareRow.winner ? "winner" : ""}>
                <span>{model}</span>
                <strong>{model === "prod-v4" ? "基线" : model === "prod-v5" ? "当前线上" : "候选版本"}</strong>
                <em>{model === selectedCompareRow.winner ? "当前维度最优" : "参与同集对比"}</em>
              </div>
            ))}
          </div>
          <div className="evaluation-compare-matrix">
            <div className="evaluation-compare-head">
              {["能力", "prod-v4", "prod-v5", "candidate", "差异", "风险"].map((column) => <span key={column}>{column}</span>)}
            </div>
            {evaluationModelCompareRows.map((row) => (
              <button
                key={row.key}
                type="button"
                data-testid={`model-compare-${row.key}`}
                className={row.key === selectedCompareRow.key ? "active" : ""}
                onClick={() => selectCapability(row.key)}
              >
                <strong>{row.ability}</strong>
                <span>{row.prod4}</span>
                <span>{row.prod5}</span>
                <span>{row.candidate}</span>
                <b className={row.diff.startsWith("-") ? "negative" : "positive"}>{row.diff}</b>
                <em>{row.risk}</em>
              </button>
            ))}
          </div>
        </section>
        <section className="module-panel evaluation-compare-detail">
          <PanelHeader title="差异解释" subtitle={`${selectedCompareRow.ability} / ${selectedCompareRow.risk}`} icon={<Eye size={16} />} />
          <div className="evaluation-compare-insight">
            <span>{selectedCompareRow.winner}</span>
            <strong>{selectedCompareRow.ability}</strong>
            <p>{selectedCompareRow.evidence}</p>
          </div>
          <div className="evaluation-action-row compact">
            <button type="button" onClick={() => openEvaluationCaseEvidence()}>下钻证据</button>
            <button type="button" onClick={() => openEvaluationAssetLineage("模型对比资产血缘")}>资产血缘</button>
            <button type="button" className="primary" disabled={evaluationAction === "run"} onClick={runEvaluation}>
              {evaluationAction === "run" ? "提交中" : "重跑对比"}
            </button>
          </div>
        </section>
        {selectedCompareRow.key === "asr-hotword" && (
          <section className="module-panel wide hotword-eval-gate-panel" data-testid="hotword-eval-gates">
            <PanelHeader
              title="ASR 热词影子复测与人工发布"
              subtitle={`固定评测集 EVS-ASR-Hotword-v1 / baseline ${hotwordBaselineVersion?.id ?? "pack.current_version_id 待恢复"} / candidate ${hotwordCandidateVersion?.id ?? "待 API 恢复"}`}
              icon={<ShieldCheck size={16} />}
            />
            <div className="hotword-eval-context">
              <span><b>固定评测集</b> evalset-asr-hotword-v1</span>
              <span><b>Baseline</b> {hotwordBaselineVersion ? `${hotwordBaselineVersion.id} · ${hotwordBaselineVersion.version} · ${hotwordBaselineVersion.status}` : "pack.current_version_id 待恢复"}</span>
              <span><b>Candidate</b> {hotwordCandidateVersion?.id ?? "尚未创建"} · {hotwordVersionLoading ? "loading" : hotwordCandidateVersion?.status ?? "missing"}</span>
              <span><b>执行模式</b> shadow · 不覆盖历史资产</span>
            </div>
            <div className="hotword-gate-table">
              <div>
                {['门禁', 'Baseline', 'Candidate', '阈值', '结果'].map((label) => <span key={label}>{label}</span>)}
              </div>
              {hotwordGateRows.map((gate) => (
                <div key={gate.label}>
                  <strong>{gate.label}</strong>
                  <span>{gate.baseline}</span>
                  <span>{gate.candidate}</span>
                  <span>{gate.threshold}</span>
                  <b>{gate.result}</b>
                </div>
              ))}
            </div>
            {hotwordEvalResult.gatePassed !== null && (!hotwordEvalResult.baselineMetrics || !hotwordEvalResult.candidateMetrics) && (
              <p className="hotword-gate-detail-missing">
                EvalRun 仅返回 {hotwordEvalResult.gatePassed ? "gate passed" : "gate blocked"}，指标明细缺失，不展示演示值。
              </p>
            )}
            <div className="hotword-gate-actions">
              <button
                type="button"
                data-testid="hotword-shadow-eval"
                disabled={Boolean(evaluationAction) || hotwordCandidateVersion?.status !== "ready_for_eval"}
                title={evaluationAction ? "已有热词治理写操作进行中。" : hotwordCandidateVersion?.status !== "ready_for_eval" ? "评测已阻断：等待候选构建完成并进入 ready_for_eval。" : "只创建影子 EvalRun，不切换生产。"}
                onClick={() => void runHotwordShadowEval()}
              >
                {evaluationAction === "hotword_eval" ? "影子评测创建中" : "运行热词影子评测"}
              </button>
              <button
                type="button"
                data-testid="hotword-model-approve"
                disabled={
                  Boolean(evaluationAction) ||
                  !canApproveHotwordVersion ||
                  !hotwordEvalPassed ||
                  hotwordCandidateVersion?.status !== "review_required"
                }
                title={
                  !canApproveHotwordVersion
                    ? "审批已阻断：当前身份缺少 model_engineer 角色。"
                    : hotwordCandidateVersion?.status !== "review_required" || !hotwordEvalPassed
                      ? "审批已阻断：等待成功、锁定且通过门禁的 EvalRun。"
                      : "以实时 resource_version 提交模型负责人审批。"
                }
                onClick={() => void approveHotwordCandidate()}
              >
                {evaluationAction === "hotword_approve" ? "模型审批中" : hotwordCandidateVersion?.status === "approved" ? "模型已审批" : "模型负责人审批"}
              </button>
              <button
                type="button"
                className="primary"
                data-testid="hotword-manual-publish"
                disabled={
                  Boolean(evaluationAction) ||
                  !canPublishHotwordVersion ||
                  !hotwordEvalPassed ||
                  !hotwordEvalRunId ||
                  hotwordCandidateVersion?.status !== "approved" ||
                  hotwordPublished
                }
                title={
                  hotwordPublished
                    ? "该词包版本已发布，不能重复发布。"
                    : hotwordPublishRetryRunRef.current
                      ? `${hotwordPublishRetryRunRef.current} 失败；改走 /retries。`
                      : evaluationNotice.status === "error"
                        ? "发布未创建；可重试提交。"
                        : !canPublishHotwordVersion
                          ? "发布已阻断：当前身份缺少 project_admin 角色。"
                          : !hotwordEvalPassed || !hotwordEvalRunId || hotwordCandidateVersion?.status !== "approved"
                            ? "发布已阻断：缺少成功且锁定的 EvalRun、模型负责人审批或 Provider 编译产物。"
                            : "人工确认发布，并生成绑定词包版本的 TaskVersion 草稿。"
                }
                onClick={() => void publishHotwordCandidate()}
              >
                {evaluationAction === "hotword_publish"
                  ? "发布运行处理中"
                  : hotwordPublished
                    ? "词包已发布"
                    : hotwordPublishRetryRunRef.current
                      ? "重试发布运行"
                      : evaluationNotice.status === "error"
                        ? "重试创建发布"
                        : "人工发布词包"}
              </button>
              <span>{hotwordEvalRunId ? `EvalRun ${hotwordEvalRunId} · ${hotwordEvalPassed ? "门禁通过并锁定" : "pending/blocked"}` : "blocked：先完成候选构建与固定评测集影子复测"}</span>
              {!canApproveHotwordVersion && hotwordCandidateVersion?.status === "review_required" && <span>blocked：当前身份不能执行模型负责人审批</span>}
              {!canPublishHotwordVersion && hotwordCandidateVersion?.status === "approved" && <span>blocked：当前身份不能执行项目管理员发布确认</span>}
            </div>
          </section>
        )}
      </>
    );

  const renderBadcaseView = () => (
      <>
        <section className="module-panel wide evaluation-badcase-board">
          <PanelHeader title="badcase 归因与回流板" subtitle="按状态推进：待归因、待人审、待回流、已入回归" icon={<AlertTriangle size={16} />} />
          <div className="evaluation-badcase-capability-filter" role="group" aria-label="badcase 能力筛选">
            {[
              ["all", "全部能力"],
              ["asr-hotword", "ASR 热词"],
              ["boundary", "边界切分"],
              ["diarization", "串音识别"],
              ["tagging", "标签识别"],
              ["prompt", "Prompt 优化"]
            ].map(([key, label]) => (
              <button
                key={key}
                type="button"
                data-testid={key === "asr-hotword" ? "badcase-capability-asr-hotword" : undefined}
                className={badcaseCapabilityFilter === key ? "active" : ""}
                onClick={() => {
                  const nextFilter = key as "all" | EvaluationCapabilityKey;
                  setBadcaseCapabilityFilter(nextFilter);
                  const next = key === "all" ? badcaseWorkflow[0] : badcaseWorkflow.find((item) => item.capability === key);
                  if (next) {
                    setSelectedBadcaseId(next.id);
                    setSelectedCapabilityKey(next.capability);
                  }
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="evaluation-kanban">
            {(["待归因", "待人审", "待回流", "已入回归"] as EvaluationBadcaseWorkflowItem["status"][]).map((status) => (
              <div key={status} className="evaluation-kanban-column">
                <span>{status}</span>
                {visibleBadcaseWorkflow.filter((item) => item.status === status).map((item) => (
                  <button key={item.id} type="button" className={item.id === selectedBadcaseWorkflow.id ? "active" : ""} onClick={() => setSelectedBadcaseId(item.id)}>
                    <strong>{item.title}</strong>
                    <em>{item.source}</em>
                    <b>{item.severity}</b>
                  </button>
                ))}
              </div>
            ))}
          </div>
        </section>
        <section className="module-panel evaluation-badcase-editor">
          <PanelHeader title={selectedBadcaseWorkflow.title} subtitle={`${selectedBadcaseWorkflow.id} / ${selectedBadcaseWorkflow.status}`} icon={<FileText size={16} />} />
          {!selectedBadcaseResolved && (
            <div className="operation-toast is-error" role="status">
              <strong>Badcase 对象未恢复</strong>
              <span>{selectedBadcaseWorkflow.id} · {hotwordBadcaseRecovery} · 所有写操作 blocked，且不会回退到 B-2031。</span>
            </div>
          )}
          {selectedBadcaseWorkflow.capability === "asr-hotword" && (
            <div className="hotword-badcase-profile" data-testid="hotword-badcase-profile">
              {[
                ["Badcase ID", selectedBadcaseWorkflow.id],
                ["标准词", selectedBadcaseWorkflow.standardTerm ?? "待确认"],
                ["识别结果", selectedBadcaseWorkflow.recognizedText ?? "待确认"],
                ["错误类型", selectedBadcaseWorkflow.errorType ?? "misrecognition"],
                ["统计量", `可信出现 ${selectedBadcaseWorkflow.expectedCount ?? 0} / 易错率 ${selectedBadcaseWorkflow.errorRate ?? 0}%`],
                ["证据等级", selectedBadcaseWorkflow.evidenceLevel ?? "待确认"],
                ["下游影响", selectedBadcaseWorkflow.downstreamImpact ?? "待计算"],
                ["优先级", String(selectedBadcaseWorkflow.priority ?? 0)],
                ["root_trace_id", selectedBadcaseWorkflow.rootTraceId ?? "待后端生成"]
              ].map(([label, value]) => (
                <span key={label}>
                  <b>{label}</b>
                  <strong>{value}</strong>
                </span>
              ))}
            </div>
          )}
          <label>
            <span>根因</span>
            <textarea rows={3} value={selectedBadcaseDraft.rootCause} onChange={(event) => updateBadcaseDraft("rootCause", event.target.value)} />
          </label>
          <label>
            <span>修复建议</span>
            <textarea rows={3} value={selectedBadcaseDraft.fix} onChange={(event) => updateBadcaseDraft("fix", event.target.value)} />
          </label>
          <label>
            <span>回流目标</span>
            <input value={selectedBadcaseDraft.target} onChange={(event) => updateBadcaseDraft("target", event.target.value)} />
          </label>
          <label>
            <span>负责人</span>
            <input value={selectedBadcaseDraft.owner} onChange={(event) => updateBadcaseDraft("owner", event.target.value)} />
          </label>
          <div className="evaluation-action-row compact">
            <button
              type="button"
              disabled={!selectedBadcaseResolved || evaluationAction === "hotword_badcase_decision"}
              onClick={() => void moveBadcaseStatus("待人审")}
            >
              {evaluationAction === "hotword_badcase_decision" ? "处理中" : "送人审"}
            </button>
            <button
              type="button"
              data-testid={selectedBadcaseWorkflow.capability === "asr-hotword" ? "hotword-confirm-decision" : undefined}
              disabled={!selectedBadcaseResolved || evaluationAction === "hotword_badcase_decision"}
              title={selectedBadcaseWorkflow.capability === "asr-hotword" ? "人工确认后写入 decisions，成功才进入待回流。" : undefined}
              onClick={() => void moveBadcaseStatus("待回流")}
            >
              {evaluationAction === "hotword_badcase_decision" ? "确认提交中" : selectedBadcaseWorkflow.capability === "asr-hotword" ? "确认易错词" : "生成回流"}
            </button>
            <button
              type="button"
              className="primary"
              disabled={!selectedBadcaseResolved || evaluationAction === "hotword_badcase_decision"}
              onClick={() => void moveBadcaseStatus("已入回归")}
            >
              {evaluationAction === "hotword_badcase_decision" ? "处理中" : "加入回归集"}
            </button>
          </div>
          {selectedBadcaseWorkflow.capability === "asr-hotword" && (
            <div className="evaluation-action-row compact hotword-badcase-actions">
              <button
                type="button"
                className="primary"
                data-testid="hotword-candidate-add"
                disabled={!selectedBadcaseResolved || evaluationAction === "hotword_candidate" || selectedBadcaseWorkflow.status === "待归因"}
                title={selectedBadcaseWorkflow.status === "待归因" ? "blocked：Badcase 尚未完成人工确认。" : "写入候选版本，不修改已发布词包。"}
                onClick={() => void addSelectedBadcaseToHotwordCandidate()}
              >
                {evaluationAction === "hotword_candidate" ? "加入中" : "加入候选词包"}
              </button>
              <span>目标 {hotwordCandidateVersion?.id ?? "候选 version_id 待 API 恢复"} · candidate/shadow only</span>
            </div>
          )}
          <div className="evaluation-action-row compact">
            <button type="button" onClick={() => openEvaluationBadcaseEvidence()}>进入调听证据</button>
            <button type="button" onClick={() => setActiveModule("labels")}>生成标签规则</button>
            <button type="button" disabled={evaluationAction === "feedback"} onClick={createFeedbackTask}>
              {evaluationAction === "feedback" ? "创建中" : "创建任务"}
            </button>
          </div>
          <div className="evaluation-action-row compact">
            <button type="button" onClick={() => {
              updateBadcaseDraft("target", "Prompt 优化 / Prompt 回归集");
              setActiveTab("prompt");
            }}>
              回流 Prompt
            </button>
            <button type="button" onClick={() => {
              updateBadcaseDraft("target", "打标黄金集");
              setSelectedDatasetId("label-golden");
              setActiveTab("sets");
            }}>
              打标黄金集
            </button>
            <button type="button" onClick={() => {
              updateBadcaseDraft("target", "模型评测集");
              setSelectedDatasetId("boundary-flow");
              setActiveTab("sets");
            }}>
              模型评测集
            </button>
          </div>
        </section>
      </>
    );

  const renderCurrentView = () => {
      if (currentView === "labeling") return renderLabelingView();
      if (currentView === "prompt") return renderPromptView();
      if (currentView === "manual") return renderManualView();
      if (currentView === "sets") return renderSetsView();
      if (currentView === "compare") return renderCompareView();
      if (currentView === "badcase") return renderBadcaseView();
      return renderAutoView();
    };

  return {
    renderCompareView,
    renderBadcaseView,
    renderCurrentView
  };
}

export type EvaluationFinalRenders = ReturnType<typeof buildEvaluationFinalRenders>;
