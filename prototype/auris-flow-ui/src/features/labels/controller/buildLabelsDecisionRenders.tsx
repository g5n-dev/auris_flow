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
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { AlertTriangle, ArrowRight, BarChart3, BrainCircuit, Check, Gauge, Settings, ShieldCheck, UserCheck, Workflow } from "lucide-react";

type BuildLabelsDecisionRendersScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions & LabelsOptimizationActions & LabelsReviewActions & LabelsPersistenceActions & LabelsPromptActions & LabelsEvaluationActions & LabelsReleaseActions & LabelsCoreRenders & LabelsInputRenders;

export function buildLabelsDecisionRenders(activeCandidate: BuildLabelsDecisionRendersScope["activeCandidate"], applyCandidateAction: BuildLabelsDecisionRendersScope["applyCandidateAction"], applyReviewDecision: BuildLabelsDecisionRendersScope["applyReviewDecision"], focus: BuildLabelsDecisionRendersScope["focus"], gateIsBlocked: BuildLabelsDecisionRendersScope["gateIsBlocked"], generateOptimizationRunDraft: BuildLabelsDecisionRendersScope["generateOptimizationRunDraft"], labelBadcaseActionHint: BuildLabelsDecisionRendersScope["labelBadcaseActionHint"], labelCandidatePublishDisabled: BuildLabelsDecisionRendersScope["labelCandidatePublishDisabled"], labelEntityAction: BuildLabelsDecisionRendersScope["labelEntityAction"], labelEvalActionLabel: BuildLabelsDecisionRendersScope["labelEvalActionLabel"], labelEvalRun: BuildLabelsDecisionRendersScope["labelEvalRun"], labelEvalSubmitDisabled: BuildLabelsDecisionRendersScope["labelEvalSubmitDisabled"], labelEvalSucceeded: BuildLabelsDecisionRendersScope["labelEvalSucceeded"], labelEvaluationLock: BuildLabelsDecisionRendersScope["labelEvaluationLock"], labelGrayPublishDisabled: BuildLabelsDecisionRendersScope["labelGrayPublishDisabled"], labelOptimizationRun: BuildLabelsDecisionRendersScope["labelOptimizationRun"], labelPromotePublishDisabled: BuildLabelsDecisionRendersScope["labelPromotePublishDisabled"], labelPublishPending: BuildLabelsDecisionRendersScope["labelPublishPending"], labelPublishRequest: BuildLabelsDecisionRendersScope["labelPublishRequest"], labelReleaseDisabledReason: BuildLabelsDecisionRendersScope["labelReleaseDisabledReason"], openLabelEvidence: BuildLabelsDecisionRendersScope["openLabelEvidence"], releaseInputs: BuildLabelsDecisionRendersScope["releaseInputs"], runPromptEval: BuildLabelsDecisionRendersScope["runPromptEval"], saveReleaseConfig: BuildLabelsDecisionRendersScope["saveReleaseConfig"], selectedEvaluationMetric: BuildLabelsDecisionRendersScope["selectedEvaluationMetric"], setActionFeedback: BuildLabelsDecisionRendersScope["setActionFeedback"], setExperimentState: BuildLabelsDecisionRendersScope["setExperimentState"], setReleaseDecision: BuildLabelsDecisionRendersScope["setReleaseDecision"], setSelectedExperimentMetric: BuildLabelsDecisionRendersScope["setSelectedExperimentMetric"], setSelectedPromptField: BuildLabelsDecisionRendersScope["setSelectedPromptField"], startLabelPublish: BuildLabelsDecisionRendersScope["startLabelPublish"], submitReleaseGate: BuildLabelsDecisionRendersScope["submitReleaseGate"], updateReleaseInput: BuildLabelsDecisionRendersScope["updateReleaseInput"]) {
  const renderDecisionRail = (focus: "evaluation" | "release") => {
      const blockingChecks = labelOptimizationRun.gateChecks.filter((check) => check.blocking);
      return (
        <aside className={`label-decision-rail ${gateIsBlocked ? "blocked" : "passed"}`}>
          <div className="label-decision-head">
            <span>当前决策与下一步</span>
            <strong>{labelOptimizationRun.decision.label}</strong>
            <p>{selectedEvaluationMetric.gateImpact}</p>
          </div>
          <div className="label-decision-focus">
            <span>{focus === "evaluation" ? "当前指标" : "发布门禁引用指标"}</span>
            <strong>{selectedEvaluationMetric.metric} · {selectedEvaluationMetric.verdict}</strong>
            <em>{selectedEvaluationMetric.requiredAction}</em>
          </div>
          <div className="label-decision-blockers">
            <span>{blockingChecks.length > 0 ? "阻断项" : "通过项"}</span>
            {(blockingChecks.length > 0 ? blockingChecks : labelOptimizationRun.gateChecks.filter((check) => !check.blocking).slice(0, 3)).map((check) => (
              <button key={check.id} type="button" className={check.blocking ? "blocked" : "passed"} onClick={() => setActionFeedback(`${check.label}：${check.detail}；动作：${check.requiredAction}`)}>
                <b>{check.status}</b>
                <strong>{check.label}</strong>
                <em>{check.requiredAction}</em>
              </button>
            ))}
          </div>
          <div className="label-decision-actions">
            <button type="button" onClick={() => {
              setSelectedPromptField("definition");
              setActionFeedback("已进入继续优化路径：先补 Prompt 定义/规则，再重新运行影子评测。");
            }}>
              <BrainCircuit size={14} />
              继续优化
            </button>
            <button type="button" disabled={labelEntityAction !== null} onClick={() => void applyCandidateAction("human")}>
              <UserCheck size={14} />
              送 Human Loop
            </button>
            <button type="button" disabled={labelEntityAction === "human-decision"} onClick={() => void applyReviewDecision("已接受", "标记人审通过", "人工确认当前候选；其他效果指标仍独立决定发布门禁。") }>
              <Check size={14} />
              {labelEntityAction === "human-decision" ? "提交中" : "标记人审通过"}
            </button>
            <button type="button" onClick={() => {
              setExperimentState("影子评测中");
              setReleaseDecision("仅影子评测");
              setActionFeedback("已保持影子运行：候选版本继续评测，不写线上标签。");
            }}>
              <Gauge size={14} />
              保持影子运行
            </button>
            <button type="button" className="primary" disabled={labelGrayPublishDisabled} title={labelReleaseDisabledReason("gray")} onClick={() => startLabelPublish("gray")}>
              <ShieldCheck size={14} />
              {labelPublishPending && labelPublishRequest.action === "gray" ? "灰度发布 · pending" : "灰度发布"}
            </button>
            <button type="button" className="primary" disabled={labelCandidatePublishDisabled} title={labelReleaseDisabledReason("candidate")} onClick={() => startLabelPublish("candidate")}>
              <Check size={14} />
              {labelPublishPending && labelPublishRequest.action === "candidate" ? "发布候选版本 · pending" : "发布候选版本"}
            </button>
          </div>
        </aside>
      );
    };

  const renderUnifiedEvaluationDetail = () => (
      <section className="module-panel wide label-run-detail-panel">
        <div className="label-run-detail-layout">
          <main className="label-run-main">
            <PanelHeader title="效果评价" subtitle="同一评测集对比当前版本和候选版本；点击指标会同步到门禁判断" icon={<BarChart3 size={16} />} />
            <div className="label-run-metric-list">
              {labelOptimizationRun.metrics.map((metric) => (
                <button
                  key={metric.id}
                  type="button"
                  className={`label-run-metric-row ${metric.verdict === "通过" ? "passed" : metric.verdict === "观察" ? "watch" : "blocked"} ${selectedEvaluationMetric.id === metric.id ? "active" : ""}`}
                  onClick={() => {
                    setSelectedExperimentMetric(metric.metric);
                    setActionFeedback(`${metric.metric} 已聚焦：${metric.gateImpact} 建议动作：${metric.requiredAction}`);
                  }}
                >
                  <strong>{metric.metric}</strong>
                  <span>{metric.current}</span>
                  <ArrowRight size={13} />
                  <span>{metric.candidate}</span>
                  <em>{metric.delta}</em>
                  <b>{metric.verdict}</b>
                  <small>{metric.attribution}</small>
                </button>
              ))}
            </div>
            <div className="label-run-section-grid">
              <div className="label-run-section">
                <div>
                  <span>提升来源</span>
                  <strong>{selectedEvaluationMetric.metric}</strong>
                </div>
                <p>{selectedEvaluationMetric.attribution}</p>
                <p>{selectedEvaluationMetric.gateImpact}</p>
              </div>
              <div className="label-run-section">
                <div>
                  <span>差异样本</span>
                  <strong>{activeCandidate.id}</strong>
                </div>
                <p>{selectedEvaluationMetric.sample}</p>
                <div className="label-run-sample-actions">
                  <button type="button" onClick={() => openLabelEvidence("评测差异样本")}>下钻证据详情</button>
                  <button type="button" data-testid="label-badcase-action" disabled={labelEntityAction !== null} title={labelBadcaseActionHint} onClick={() => void applyCandidateAction("badcase")}>加入 badcase</button>
                </div>
              </div>
            </div>
            <div className="label-run-failure-grid">
              {labelOptimizationRun.metrics.filter((metric) => metric.blocking).map((metric) => (
                <button key={metric.id} type="button" className="label-run-failure-card" onClick={() => setSelectedExperimentMetric(metric.metric)}>
                  <span>门禁阻断</span>
                  <strong>{metric.metric}</strong>
                  <p>{metric.requiredAction}</p>
                </button>
              ))}
            </div>
            <div className="label-run-toolbar">
              <button type="button" className="primary" onClick={() => void runPromptEval()} disabled={labelEvalSubmitDisabled}>
                <Gauge size={14} />
                {labelEvalActionLabel}
              </button>
              <button type="button" onClick={generateOptimizationRunDraft}>
                <Workflow size={14} />
                生成运行草稿
              </button>
              <button type="button" data-testid="label-badcase-action" disabled={labelEntityAction !== null} title={labelBadcaseActionHint} onClick={() => void applyCandidateAction("badcase")}>
                <AlertTriangle size={14} />
                生成回流任务
              </button>
            </div>
          </main>
          {renderDecisionRail("evaluation")}
        </div>
      </section>
    );

  const renderUnifiedReleaseDetail = () => (
      <section className="module-panel wide label-run-detail-panel">
        <div className="label-run-detail-layout">
          <main className="label-run-main">
            <PanelHeader title="发布门禁" subtitle="消费效果评价产生的 EvalRun 与 AssetCheck，未通过只能继续优化、人审或影子运行" icon={<ShieldCheck size={16} />} />
            <div className="label-run-gate-list">
              {labelOptimizationRun.gateChecks.map((check) => (
                <button
                  key={check.id}
                  type="button"
                  className={`label-run-gate-card ${check.blocking ? "blocked" : check.status === "观察" ? "watch" : "passed"}`}
                  onClick={() => {
                    if (labelOptimizationRun.metrics.some((metric) => metric.metric === check.sourceMetric)) setSelectedExperimentMetric(check.sourceMetric);
                    setActionFeedback(`${check.label}：${check.detail}；下一步：${check.requiredAction}`);
                  }}
                >
                  <span>{check.status}</span>
                  <strong>{check.label}</strong>
                  <em>{check.sourceMetric}</em>
                  <p>{check.detail}</p>
                </button>
              ))}
            </div>
            <div className="label-release-strategy-grid">
              <label>
                <span>灰度比例</span>
                <input value={releaseInputs.traffic} onChange={(event) => updateReleaseInput("traffic", event.target.value)} />
              </label>
              <label>
                <span>回滚部署 ID</span>
                <input value={releaseInputs.rollback} onChange={(event) => updateReleaseInput("rollback", event.target.value)} />
              </label>
              <label>
                <span>审批人</span>
                <input value={releaseInputs.approver} onChange={(event) => updateReleaseInput("approver", event.target.value)} />
              </label>
              <label>
                <span>发布动作</span>
                <select value={releaseInputs.action} onChange={(event) => updateReleaseInput("action", event.target.value)}>
                  <option>灰度观察</option>
                  <option>发布候选</option>
                  <option>仅影子评测</option>
                </select>
              </label>
              <label className="wide">
                <span>阻断原因 / 发布说明</span>
                <textarea value={releaseInputs.blockerReason || releaseInputs.note} onChange={(event) => updateReleaseInput("blockerReason", event.target.value)} rows={3} />
              </label>
            </div>
            <div className="label-release-metric-summary">
              {labelOptimizationRun.metrics.slice(0, 5).map((metric) => (
                <button key={metric.id} type="button" className={selectedEvaluationMetric.id === metric.id ? "active" : ""} onClick={() => setSelectedExperimentMetric(metric.metric)}>
                  <span>{metric.metric}</span>
                  <strong>{metric.candidate}</strong>
                  <em>{metric.verdict}</em>
                </button>
              ))}
            </div>
            <div
              className={`label-release-eval-binding ${labelEvalSucceeded ? "bound" : "missing"}`}
              data-label-eval-run-id={labelEvalRun?.id ?? ""}
              data-label-evaluation-lock-sha={labelEvaluationLock?.snapshot_sha256 ?? ""}
            >
              <span>发布评测事实</span>
              <strong>{labelEvalRun?.id ?? "尚未绑定 EvalRun"}</strong>
              <em>
                {labelEvalSucceeded
                  ? `${labelOptimizationRun.runId} / Bundle ${labelEvaluationLock?.snapshot_sha256.slice(0, 12) ?? "missing"}`
                  : labelEvalRun
                    ? `当前 ${labelEvalRun.status}，等待 GET 真实成功终态`
                    : labelEvaluationLock
                      ? `Bundle ${labelEvaluationLock.snapshot_sha256.slice(0, 12)} 已冻结，等待 EvalRun`
                      : "先通过 Prompt 双盲审批，再锁定评测 Bundle"}
              </em>
            </div>
            <div className="label-run-toolbar">
              <button type="button" onClick={saveReleaseConfig}>
                <Settings size={14} />
                保存门禁配置
              </button>
              <button type="button" onClick={submitReleaseGate} disabled={labelPublishPending}>
                <ShieldCheck size={14} />
                {labelPublishPending && labelPublishRequest.action === "gate" ? "提交门禁判断 · pending" : "提交门禁判断"}
              </button>
              <button type="button" className="primary" disabled={labelPromotePublishDisabled} title={labelReleaseDisabledReason("execute")} onClick={() => startLabelPublish("execute")}>
                <Check size={14} />
                {labelPublishPending && labelPublishRequest.action === "execute" ? "执行发布动作 · pending" : "执行发布动作"}
              </button>
            </div>
          </main>
          {renderDecisionRail("release")}
        </div>
      </section>
    );

  const renderSharedDagsterStatusCompact = () => (
      <section className="module-panel wide label-run-dagster-compact">
        <PanelHeader title="统一执行状态" subtitle="效果评价产生评测运行和资产检查；发布门禁消费检查并生成发布决策和资产物化" icon={<Workflow size={16} />} />
        <div className="label-runrequest-compact-grid">
          {labelOptimizationRun.dagsterRunDraft.map(([label, value]) => (
            <button key={label} type="button" onClick={() => setActionFeedback(`${label}: ${value}`)}>
              <span>{label}</span>
              <strong>{value}</strong>
            </button>
          ))}
        </div>
        <div className="label-run-dagster-objects">
          {[
            ["任务定义", labelOptimizationRun.input.jobName],
            ["资产选择", "候选标签 + 评测运行 + 人审任务 + 发布决策"],
            ["运行请求", `${labelOptimizationRun.runId} / ${labelOptimizationRun.input.partitionKey}`],
            ["AssetCheck", gateIsBlocked ? "blocked_by_gate" : "passed_for_release"],
            ["资产生成记录", labelOptimizationRun.dagsterStatus === "已回写" ? "已回写候选与评测资产" : "等待发布回写"]
          ].map(([name, detail]) => (
            <button key={name} type="button" onClick={() => setActionFeedback(`${name}：${detail}`)}>
              <span>{name}</span>
              <strong>{detail}</strong>
            </button>
          ))}
        </div>
      </section>
    );

  return {
    renderDecisionRail,
    renderUnifiedEvaluationDetail,
    renderUnifiedReleaseDetail,
    renderSharedDagsterStatusCompact
  };
}

export type LabelsDecisionRenders = ReturnType<typeof buildLabelsDecisionRenders>;
