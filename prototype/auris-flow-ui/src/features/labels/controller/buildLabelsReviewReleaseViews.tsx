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
import type { LabelsPrimaryViews } from "./buildLabelsPrimaryViews";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { LabelLifecycleSummary } from "../components/LabelLifecycleSummary";
import { labelAutomationLevels } from "../fixtures/governanceCatalog";
import { BarChart3, Check, Gauge, ShieldCheck, UserCheck, Workflow } from "lucide-react";

type BuildLabelsReviewReleaseViewsScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions & LabelsOptimizationActions & LabelsReviewActions & LabelsPersistenceActions & LabelsPromptActions & LabelsEvaluationActions & LabelsReleaseActions & LabelsCoreRenders & LabelsInputRenders & LabelsDecisionRenders & LabelsWorkbenchRenders & LabelsContractRenders & LabelsRunRailModel & LabelsShellRenders & LabelsPrimaryViews;

export function buildLabelsReviewReleaseViews(activeAutomation: BuildLabelsReviewReleaseViewsScope["activeAutomation"], activeCandidate: BuildLabelsReviewReleaseViewsScope["activeCandidate"], activeReviewTask: BuildLabelsReviewReleaseViewsScope["activeReviewTask"], automationLevel: BuildLabelsReviewReleaseViewsScope["automationLevel"], dagsterDraftRows: BuildLabelsReviewReleaseViewsScope["dagsterDraftRows"], dagsterDraftState: BuildLabelsReviewReleaseViewsScope["dagsterDraftState"], evaluationMetrics: BuildLabelsReviewReleaseViewsScope["evaluationMetrics"], gateFactPending: BuildLabelsReviewReleaseViewsScope["gateFactPending"], gateIsBlocked: BuildLabelsReviewReleaseViewsScope["gateIsBlocked"], generateOptimizationRunDraft: BuildLabelsReviewReleaseViewsScope["generateOptimizationRunDraft"], hasBoundReviewTask: BuildLabelsReviewReleaseViewsScope["hasBoundReviewTask"], labelCandidatePublishDisabled: BuildLabelsReviewReleaseViewsScope["labelCandidatePublishDisabled"], labelEntityAction: BuildLabelsReviewReleaseViewsScope["labelEntityAction"], labelEvalActionLabel: BuildLabelsReviewReleaseViewsScope["labelEvalActionLabel"], labelEvalPending: BuildLabelsReviewReleaseViewsScope["labelEvalPending"], labelEvalRequest: BuildLabelsReviewReleaseViewsScope["labelEvalRequest"], labelEvalRun: BuildLabelsReviewReleaseViewsScope["labelEvalRun"], labelEvalSubmitDisabled: BuildLabelsReviewReleaseViewsScope["labelEvalSubmitDisabled"], labelEvalSucceeded: BuildLabelsReviewReleaseViewsScope["labelEvalSucceeded"], labelEvaluationLock: BuildLabelsReviewReleaseViewsScope["labelEvaluationLock"], labelGrayPublishDisabled: BuildLabelsReviewReleaseViewsScope["labelGrayPublishDisabled"], labelOptimizationRun: BuildLabelsReviewReleaseViewsScope["labelOptimizationRun"], labelPromotePublishDisabled: BuildLabelsReviewReleaseViewsScope["labelPromotePublishDisabled"], labelPublishBlocked: BuildLabelsReviewReleaseViewsScope["labelPublishBlocked"], labelPublishPending: BuildLabelsReviewReleaseViewsScope["labelPublishPending"], labelPublishRequest: BuildLabelsReviewReleaseViewsScope["labelPublishRequest"], labelReleaseDisabledReason: BuildLabelsReviewReleaseViewsScope["labelReleaseDisabledReason"], lockedLabelVersionId: BuildLabelsReviewReleaseViewsScope["lockedLabelVersionId"], optimizationInputs: BuildLabelsReviewReleaseViewsScope["optimizationInputs"], releaseDecision: BuildLabelsReviewReleaseViewsScope["releaseDecision"], releaseGateSummaries: BuildLabelsReviewReleaseViewsScope["releaseGateSummaries"], releaseInputs: BuildLabelsReviewReleaseViewsScope["releaseInputs"], renderV2Page: BuildLabelsReviewReleaseViewsScope["renderV2Page"], retryLabelEval: BuildLabelsReviewReleaseViewsScope["retryLabelEval"], reviewDecisionActions: BuildLabelsReviewReleaseViewsScope["reviewDecisionActions"], reviewDecisionRows: BuildLabelsReviewReleaseViewsScope["reviewDecisionRows"], reviewDraftState: BuildLabelsReviewReleaseViewsScope["reviewDraftState"], reviewInputs: BuildLabelsReviewReleaseViewsScope["reviewInputs"], reviewTasks: BuildLabelsReviewReleaseViewsScope["reviewTasks"], runPromptEval: BuildLabelsReviewReleaseViewsScope["runPromptEval"], saveReleaseConfig: BuildLabelsReviewReleaseViewsScope["saveReleaseConfig"], saveReviewAndNext: BuildLabelsReviewReleaseViewsScope["saveReviewAndNext"], selectAutomationLevel: BuildLabelsReviewReleaseViewsScope["selectAutomationLevel"], selectLabelReviewTask: BuildLabelsReviewReleaseViewsScope["selectLabelReviewTask"], selectReviewDraft: BuildLabelsReviewReleaseViewsScope["selectReviewDraft"], selectedEvaluationMetric: BuildLabelsReviewReleaseViewsScope["selectedEvaluationMetric"], selectedExperimentMetric: BuildLabelsReviewReleaseViewsScope["selectedExperimentMetric"], setActionFeedback: BuildLabelsReviewReleaseViewsScope["setActionFeedback"], setSelectedExperimentMetric: BuildLabelsReviewReleaseViewsScope["setSelectedExperimentMetric"], startLabelPublish: BuildLabelsReviewReleaseViewsScope["startLabelPublish"], submitReleaseGate: BuildLabelsReviewReleaseViewsScope["submitReleaseGate"], updateReleaseInput: BuildLabelsReviewReleaseViewsScope["updateReleaseInput"], updateReviewInput: BuildLabelsReviewReleaseViewsScope["updateReviewInput"], validateDagsterDraft: BuildLabelsReviewReleaseViewsScope["validateDagsterDraft"]) {
  const renderReviewV2 = () =>
      renderV2Page(
        "review",
        "评测人审",
        "把效果评价、badcase、Human Loop 合并到一个复核工作台，人工决策会回写候选版本和发布门禁。",
        <>
          <PanelHeader title="评测集" subtitle="固定集 / badcase / 黄金集" icon={<BarChart3 size={16} />} />
          <div className="label-v2-list compact">
            {(LABEL_DEMO_MODE ? [
              ["固定评测集", "quote-risk-v3", "248 样本 / 42 badcase"],
              ["badcase 回归集", "amount-conflict-regression", "金额冲突、串音、单据缺失"],
              ["人工黄金集", "human-gold-v12", "接受/拒绝样本"]
            ] : [[
              "锁定评测集版本",
              optimizationInputs.evalDatasetVersion,
              labelEvalRun ? `由 EvalRun ${labelEvalRun.id} 返回真实指标` : "样本量、分层与 Gold 状态等待读回，不使用静态展示值"
            ]]).map(([title, id, detail]) => (
              <button key={id} type="button" onClick={() => setActionFeedback(`已聚焦 ${id}：${detail}`)}>
                <span>{title}</span>
                <strong>{id}</strong>
                <em>{detail}</em>
              </button>
            ))}
          </div>
          <button type="button" className="label-v2-wide-action" onClick={() => void runPromptEval()} disabled={labelEvalSubmitDisabled}>
            {labelEvalPending && labelEvalRequest.backendStatus !== "locking-bundle"
              ? "评测运行中 · 等待真实终态"
              : labelEvalActionLabel}
          </button>
          <div
            className={`label-release-eval-binding ${labelEvaluationLock ? "bound" : "missing"}`}
            data-testid="label-evaluation-lock-binding"
            data-label-evaluation-lock-sha={labelEvaluationLock?.snapshot_sha256 ?? ""}
          >
            <span>评测 Bundle</span>
            <strong>{labelEvaluationLock ? `已冻结 · rv${labelEvaluationLock.resource_version}` : "尚未冻结"}</strong>
            <em>
              {labelEvaluationLock
                ? `${labelEvaluationLock.snapshot_sha256.slice(0, 12)} · ${labelEvaluationLock.eval_dataset_version_id}`
                : "Prompt 双盲审批通过后，锁定标签、模型、策略与评测集"}
            </em>
          </div>
        </>,
        <>
          <PanelHeader title="指标对比与 badcase" subtitle={`${selectedEvaluationMetric.metric} / ${selectedEvaluationMetric.verdict}`} icon={<Gauge size={16} />} />
          <div className="label-v2-table">
            <div className="head">
              <span>指标</span>
              <span>当前</span>
              <span>候选</span>
              <span>变化</span>
              <span>结论</span>
            </div>
            {!LABEL_DEMO_MODE && evaluationMetrics.length === 0 ? (
              <div className={`label-fact-state is-${labelEvalRequest.status === "failed" ? "failed" : labelEvalPending ? "loading" : "empty"}`} data-testid="label-eval-fact-state" role="status">
                <strong>{labelEvalPending ? "正在读取 EvalRun 指标" : labelEvalRequest.status === "failed" ? "EvalRun 失败或读回失败" : "尚无后端评测指标"}</strong>
                <p>{labelEvalRequest.error ?? "非 demo 模式不会显示静态 Precision、Recall、F1 或门禁结论。"}</p>
                {labelEvalRequest.status === "failed" ? <button type="button" onClick={() => void retryLabelEval()}>重试评测</button> : null}
              </div>
            ) : null}
            {evaluationMetrics.map((metric) => (
              <button
                key={metric.id}
                type="button"
                className={selectedExperimentMetric === metric.metric ? "active" : ""}
                onClick={() => {
                  setSelectedExperimentMetric(metric.metric);
                  setActionFeedback(`${metric.metric}：${metric.sample}；下一步：${metric.requiredAction}`);
                }}
              >
                <span>{metric.metric}</span>
                <strong>{metric.current}</strong>
                <strong>{metric.candidate}</strong>
                <em>{metric.delta}</em>
                <b>{metric.verdict}</b>
              </button>
            ))}
          </div>
          <div className="label-v2-review-list">
            {reviewDecisionRows.length === 0 ? (
              <div className="label-fact-state is-empty" role="status">
                <strong>暂无候选级审核任务</strong>
                <p>联调模式只读取 LabelAggregate.review_task_id，不会在页面创建通用闭环任务。</p>
              </div>
            ) : null}
            {reviewDecisionRows.map((task) => (
              <button
                key={task.id}
                type="button"
                data-testid={`label-review-task-${task.id}`}
                className={task.id === activeReviewTask.id ? "active" : ""}
                onClick={() => {
                  const reviewTask = reviewTasks.find((item) => item.id === task.id);
                  if (reviewTask) selectLabelReviewTask(reviewTask);
                }}
              >
                <span>{task.id} · {task.status}</span>
                <strong>{task.title}</strong>
                <em>{task.detail}</em>
              </button>
            ))}
          </div>
        </>,
        <>
          <PanelHeader title="人工处理" subtitle={hasBoundReviewTask ? activeReviewTask.id : "等待强绑定任务"} icon={<UserCheck size={16} />} />
          <div className="label-review-keyboard-hint" aria-label="人审键盘快捷键">
            <span><kbd>↑</kbd><kbd>↓</kbd> 切换任务</span>
            <span><kbd>A</kbd> 接受</span>
            <span><kbd>M</kbd> 修改</span>
            <span><kbd>R</kbd> 拒绝</span>
          </div>
          <div className="label-v2-detail">
            <strong>{activeReviewTask.title}</strong>
            <p>{activeReviewTask.detail}</p>
          </div>
          <div className="label-v2-form-mini">
            <label>
              <span>处理人</span>
              <input value={reviewInputs.assignee} onChange={(event) => updateReviewInput("assignee", event.target.value)} />
            </label>
            <label>
              <span>处理说明</span>
              <textarea value={reviewInputs.note} onChange={(event) => updateReviewInput("note", event.target.value)} rows={4} />
            </label>
          </div>
          <div className="label-v2-actions">
            {reviewDecisionActions.map((action) => (
              <button
                key={action.state}
                type="button"
                className={reviewDraftState === action.state ? "active" : ""}
                disabled={!hasBoundReviewTask || labelEntityAction !== null}
                onClick={() => selectReviewDraft(action.state)}
              >
                {action.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="label-review-save-next primary"
            data-testid="label-review-save-next"
            disabled={!hasBoundReviewTask || reviewDraftState === "待人工" || labelEntityAction !== null}
            onClick={() => void saveReviewAndNext()}
          >
            {labelEntityAction === "human-decision" ? "保存中" : "保存并下一条"}
          </button>
        </>
      );

  const renderReleaseV2 = () =>
      renderV2Page(
        "release",
        "版本发布",
        "发布页只处理候选版本、影响资产、门禁阻断、执行草稿、灰度和回滚动作。",
        <>
          <PanelHeader title="发布门禁" subtitle={gateFactPending ? "等待后端 Bundle 判断" : gateIsBlocked ? "存在阻断" : "可进入灰度"} icon={<ShieldCheck size={16} />} />
          {gateFactPending ? (
            <div className="label-fact-state is-empty" data-testid="label-release-gate-fact-state" role="status">
              <strong>尚无 ReleaseDeployment 门禁事实</strong>
              <p>下方配置只是提交草稿；只有后端 Bundle 返回的 blocked_reasons、状态与监控指标才会显示为门禁结论。</p>
            </div>
          ) : null}
          <div className="label-v2-list compact">
            {releaseGateSummaries.map((item) => (
              <button
                key={item.key}
                type="button"
                className={item.passed ? "passed" : "blocked"}
                onClick={() => setActionFeedback(`${item.label}：${item.detail}`)}
              >
                <span>{item.state}</span>
                <strong>{item.label}</strong>
                <em>{item.detail}</em>
              </button>
            ))}
          </div>
          <div className="label-v2-chip-group">
            {labelAutomationLevels.map((level) => (
              <button
                key={level.key}
                type="button"
                className={automationLevel === level.key ? "active" : ""}
                onClick={() => selectAutomationLevel(level.key)}
                disabled={!LABEL_DEMO_MODE && (level.key === "L3" || level.key === "L4")}
                title={!LABEL_DEMO_MODE && (level.key === "L3" || level.key === "L4") ? "L1→L2 阶段不开放自动发布" : undefined}
              >
                {level.key} {level.name}{!LABEL_DEMO_MODE && (level.key === "L3" || level.key === "L4") ? " · 未开放" : ""}
              </button>
            ))}
          </div>
        </>,
        <>
          <PanelHeader title="发布草稿" subtitle={`${lockedLabelVersionId || "LabelVersion 未锁定"} / ${dagsterDraftState}`} icon={<Workflow size={16} />} />
          <LabelLifecycleSummary labelVersionId={LABEL_DEMO_MODE ? "" : lockedLabelVersionId} />
          <div className="label-v2-editor-grid">
            <label>
              <span>发布说明</span>
              <textarea value={releaseInputs.note} onChange={(event) => updateReleaseInput("note", event.target.value)} rows={4} />
            </label>
            <label>
              <span>灰度比例</span>
              <input value={releaseInputs.traffic} onChange={(event) => updateReleaseInput("traffic", event.target.value)} />
            </label>
            <label>
              <span>审批人</span>
              <input value={releaseInputs.approver} onChange={(event) => updateReleaseInput("approver", event.target.value)} />
            </label>
            <label>
              <span>回滚部署 ID</span>
              <input value={releaseInputs.rollback} onChange={(event) => updateReleaseInput("rollback", event.target.value)} />
            </label>
            <label>
              <span>发布动作</span>
              <select value={releaseInputs.action} onChange={(event) => updateReleaseInput("action", event.target.value)}>
                <option>灰度观察</option>
                <option>发布候选</option>
                <option>仅影子评测</option>
                <option>阻断发布</option>
              </select>
            </label>
            <label>
              <span>阻断原因 / 放行依据</span>
              <textarea value={releaseInputs.blockerReason} onChange={(event) => updateReleaseInput("blockerReason", event.target.value)} rows={3} />
            </label>
          </div>
          <div className="label-v2-table dagster">
            {dagsterDraftRows.map(([label, value]) => (
              <button key={label} type="button" onClick={() => setActionFeedback(`${label}: ${value}`)}>
                <span>{label}</span>
                <strong>{value}</strong>
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
        </>,
        <>
          <PanelHeader title="发布动作" subtitle={`当前决策：${releaseDecision}`} icon={<Check size={16} />} />
          <div className="label-v2-detail">
            <strong>{labelOptimizationRun.decision.label}</strong>
            <p>{labelOptimizationRun.decision.nextActions.join(" / ")}</p>
            {[
              ["影响资产", activeCandidate.assetImpact],
              ["Job", optimizationInputs.jobName],
              ["Partition", optimizationInputs.partitionKey],
              ["自动化等级", `${automationLevel} · ${activeAutomation.name}`],
              ["回滚", releaseInputs.rollback]
            ].map(([label, value]) => (
              <button key={label} type="button" onClick={() => setActionFeedback(`${label}：${value}`)}>
                <span>{label}</span>
                <em>{value}</em>
              </button>
            ))}
          </div>
          <div className="label-v2-actions">
            <button type="button" onClick={generateOptimizationRunDraft}>生成运行草稿</button>
            <button type="button" onClick={validateDagsterDraft} disabled={dagsterDraftState === "未生成"}>校验执行映射</button>
            <button type="button" onClick={saveReleaseConfig}>保存草稿</button>
            <button type="button" onClick={submitReleaseGate} disabled={labelPublishPending || labelPublishBlocked}>
              {labelPublishPending && labelPublishRequest.action === "gate" ? "提交门禁 · pending" : "提交门禁"}
            </button>
            <button type="button" onClick={() => startLabelPublish("gray")} disabled={labelGrayPublishDisabled} title={labelReleaseDisabledReason("gray")}>
              {labelPublishPending && labelPublishRequest.action === "gray" ? "灰度发布 · pending" : "灰度发布"}
            </button>
            <button type="button" data-testid="label-publish-candidate" onClick={() => startLabelPublish("candidate")} disabled={labelCandidatePublishDisabled} title={labelReleaseDisabledReason("candidate")}>
              {labelPublishPending && labelPublishRequest.action === "candidate" ? "发布候选 · pending" : "发布候选"}
            </button>
            <button type="button" className="primary" onClick={() => startLabelPublish("execute")} disabled={labelPromotePublishDisabled} title={labelReleaseDisabledReason("execute")}>
              {labelPublishPending && labelPublishRequest.action === "execute" ? "执行发布动作 · pending" : "执行发布动作"}
            </button>
          </div>
        </>
      );

  return {
    renderReviewV2,
    renderReleaseV2
  };
}

export type LabelsReviewReleaseViews = ReturnType<typeof buildLabelsReviewReleaseViews>;
