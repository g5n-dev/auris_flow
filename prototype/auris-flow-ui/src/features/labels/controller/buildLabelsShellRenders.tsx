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
import { normalizeBackendRunStatus } from "../../../shared/runtime/backendRunStatus";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { PanelHeader } from "../../../shared/ui/PanelHeader";
import { labelScenarioPlaybooks } from "../fixtures/scenarioCatalog";
import { Check, Gauge, Headphones, Layers, RotateCcw, Sparkles } from "lucide-react";
import type { ReactNode } from "react";

type BuildLabelsShellRendersScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions & LabelsOptimizationActions & LabelsReviewActions & LabelsPersistenceActions & LabelsPromptActions & LabelsEvaluationActions & LabelsReleaseActions & LabelsCoreRenders & LabelsInputRenders & LabelsDecisionRenders & LabelsWorkbenchRenders & LabelsContractRenders & LabelsRunRailModel;

export function buildLabelsShellRenders(actionFeedback: BuildLabelsShellRendersScope["actionFeedback"], activeScenario: BuildLabelsShellRendersScope["activeScenario"], agentRunState: BuildLabelsShellRendersScope["agentRunState"], governanceViews: BuildLabelsShellRendersScope["governanceViews"], handleLabelReviewKeyDown: BuildLabelsShellRendersScope["handleLabelReviewKeyDown"], labelBadcaseActionHint: BuildLabelsShellRendersScope["labelBadcaseActionHint"], labelEntityAction: BuildLabelsShellRendersScope["labelEntityAction"], labelEntityNotice: BuildLabelsShellRendersScope["labelEntityNotice"], labelEvalPending: BuildLabelsShellRendersScope["labelEvalPending"], labelEvalRequest: BuildLabelsShellRendersScope["labelEvalRequest"], labelExtractionBackendRun: BuildLabelsShellRendersScope["labelExtractionBackendRun"], labelFactReadError: BuildLabelsShellRendersScope["labelFactReadError"], labelFactReadState: BuildLabelsShellRendersScope["labelFactReadState"], labelNextAction: BuildLabelsShellRendersScope["labelNextAction"], labelPublishPending: BuildLabelsShellRendersScope["labelPublishPending"], labelPublishRequest: BuildLabelsShellRendersScope["labelPublishRequest"], labelRunRailSteps: BuildLabelsShellRendersScope["labelRunRailSteps"], labelUnifiedTraceId: BuildLabelsShellRendersScope["labelUnifiedTraceId"], lastLabelEvalIntentRef: BuildLabelsShellRendersScope["lastLabelEvalIntentRef"], lastLabelPublishIntentRef: BuildLabelsShellRendersScope["lastLabelPublishIntentRef"], lockedLabelVersionId: BuildLabelsShellRendersScope["lockedLabelVersionId"], lockedPromptVersionId: BuildLabelsShellRendersScope["lockedPromptVersionId"], openLabelEvidence: BuildLabelsShellRendersScope["openLabelEvidence"], refreshLabelEval: BuildLabelsShellRendersScope["refreshLabelEval"], refreshLabelPublish: BuildLabelsShellRendersScope["refreshLabelPublish"], retryLabelEval: BuildLabelsShellRendersScope["retryLabelEval"], retryLabelPublish: BuildLabelsShellRendersScope["retryLabelPublish"], retryMaterializedLabelFacts: BuildLabelsShellRendersScope["retryMaterializedLabelFacts"], runScenarioAgent: BuildLabelsShellRendersScope["runScenarioAgent"], saveCandidateVersion: BuildLabelsShellRendersScope["saveCandidateVersion"], selectScenario: BuildLabelsShellRendersScope["selectScenario"], setActionFeedback: BuildLabelsShellRendersScope["setActionFeedback"], setSourceFilter: BuildLabelsShellRendersScope["setSourceFilter"], sourceFilter: BuildLabelsShellRendersScope["sourceFilter"], sourceOptions: BuildLabelsShellRendersScope["sourceOptions"]) {
  const renderLabelOptimizationRunRail = () => {
      const currentStepIndex = Math.max(0, labelRunRailSteps.findIndex((step) => step.status !== "success"));
      return (
        <nav className="label-optimization-run-rail" aria-label="标签优化运行轨道" data-testid="label-optimization-run-rail" data-trace-id={labelUnifiedTraceId}>
          <div className="label-run-rail-next">
            <span>下一动作</span>
            <strong>{labelNextAction.title}</strong>
            <em>{labelNextAction.detail}</em>
            <small>root trace · {labelUnifiedTraceId}</small>
          </div>
          <ol>
            {labelRunRailSteps.map((step, index) => (
              <li
                key={step.label}
                className={`is-${step.status}`}
                data-state={step.status}
                aria-current={index === currentStepIndex ? "step" : undefined}
              >
                <b aria-hidden="true">{index + 1}</b>
                <span>{step.label}</span>
                <strong>{step.status === "success" ? "完成" : step.status === "pending" ? "处理中" : step.status === "failed" ? "失败" : step.status === "blocked" ? "阻断" : "待开始"}</strong>
                <em>{step.detail}</em>
              </li>
            ))}
          </ol>
        </nav>
      );
    };

  const renderLabelEntityNotice = () => (
      <div className={`operation-toast is-${labelEntityNotice.status}`} role="status" aria-live="polite" data-testid="label-entity-operation-status">
        <strong>{labelEntityNotice.title}</strong>
        <span>{labelEntityNotice.detail}</span>
        <small data-testid="label-badcase-action-hint">{labelBadcaseActionHint}</small>
      </div>
    );

  const renderV2ActionFeedback = () => (
      <>
        <div
          className="label-v2-feedback"
          role="status"
          aria-live="polite"
          data-label-publish-status={labelPublishRequest.status}
          data-label-backend-status={labelPublishRequest.backendStatus ?? "idle"}
        >
          <Sparkles size={14} />
          <span>{actionFeedback}</span>
          {(labelPublishRequest.status === "pending" || ["shadowing", "gray-releasing", "monitoring"].includes(normalizeBackendRunStatus(labelPublishRequest.backendStatus))) && labelPublishRequest.runId ? (
            <button
              type="button"
              onClick={refreshLabelPublish}
              data-label-publish-refresh="true"
              title="只刷新后端运行和标签版本状态，不重复创建发布请求"
            >
              <RotateCcw size={13} />
              刷新状态
            </button>
          ) : null}
          {labelPublishRequest.status === "failed" && lastLabelPublishIntentRef.current ? (
            <button
              type="button"
              onClick={retryLabelPublish}
              disabled={labelPublishPending}
              data-label-publish-retry="true"
              title="使用原幂等键重试同一发布动作"
            >
              <RotateCcw size={13} />
              重试
            </button>
          ) : null}
          {labelPublishRequest.status === "blocked" && labelPublishRequest.error ? (
            <strong className="label-v2-blocked-reason">{labelPublishRequest.error}</strong>
          ) : null}
        </div>
        <div
          className={`label-v2-feedback label-eval-readback is-${labelEvalRequest.status}`}
          role="status"
          aria-live="polite"
          data-label-eval-status={labelEvalRequest.status}
          data-label-eval-backend-status={labelEvalRequest.backendStatus ?? "idle"}
        >
          <Gauge size={14} />
          <span>
            {labelEvalRequest.status === "success"
              ? `EvalRun ${labelEvalRequest.runId} 已真实完成，可绑定发布 Bundle。`
              : labelEvalRequest.status === "pending"
                ? `EvalRun ${labelEvalRequest.runId ?? "创建中"} 正在读回 ${labelEvalRequest.backendStatus ?? "pending"}。`
                : labelEvalRequest.status === "failed"
                  ? `EvalRun ${labelEvalRequest.runId ?? "创建请求"} 失败：${labelEvalRequest.error ?? "等待安全重试"}`
                  : "EvalRun 尚未提交；发布 Bundle 保持禁用。"}
          </span>
          {labelEvalRequest.status === "pending" && labelEvalRequest.runId ? (
            <button type="button" onClick={refreshLabelEval} data-label-eval-refresh="true" title="只读回当前 EvalRun，不重复 POST">
              <RotateCcw size={13} />
              刷新评测
            </button>
          ) : null}
          {labelEvalRequest.status === "failed" && lastLabelEvalIntentRef.current ? (
            <button type="button" onClick={() => void retryLabelEval()} disabled={labelEvalPending} data-label-eval-retry="true" title="保留原锁定输入和幂等意图重试">
              <RotateCcw size={13} />
              原意图重试
            </button>
          ) : null}
        </div>
        {renderLabelEntityNotice()}
      </>
    );

  const renderV2Page = (mode: string, title: string, subtitle: string, left: ReactNode, main: ReactNode, right: ReactNode) => (
      <div
        className={`module-grid label-governance-v2 label-governance-v2-${mode}`}
        data-demo-mode={LABEL_DEMO_MODE ? "true" : "false"}
        data-testid={mode === "review" ? "label-review-workspace" : undefined}
        tabIndex={mode === "review" ? 0 : undefined}
        onKeyDown={mode === "review" ? handleLabelReviewKeyDown : undefined}
      >
        <section className="module-panel label-v2-hero">
          <div className="label-v2-hero-copy">
            <span>标签生产治理台{LABEL_DEMO_MODE ? " · DEMO" : " · 后端事实模式"}</span>
            <strong>{title}</strong>
            <p>{subtitle}</p>
          </div>
          <div className="label-v2-hero-metrics" aria-label="当前标签治理状态">
            {governanceViews.map((item) => (
              <button
                key={item.key}
                type="button"
                className={`label-v2-kpi ${item.tone}`}
                onClick={() => setActionFeedback(`${item.title}：${item.detail}，下一步：${item.action}`)}
              >
                <span>{item.title}</span>
                <strong>{item.value}</strong>
                <em>{item.detail}</em>
              </button>
            ))}
          </div>
          <div className="label-v2-hero-actions">
            <button type="button" onClick={runScenarioAgent} disabled={labelEntityAction === "agent-run" || (!LABEL_DEMO_MODE && (!lockedLabelVersionId || !lockedPromptVersionId))} title={!LABEL_DEMO_MODE && (!lockedLabelVersionId || !lockedPromptVersionId) ? "先保存 LabelVersion 与绑定的 PromptVersion 草稿" : undefined}>
              <Sparkles size={14} />
              {labelEntityAction === "agent-run" ? "请求处理中" : agentRunState === "failed" ? "重试智能创建" : agentRunState === "running" ? "刷新运行状态" : "启动智能创建"}
            </button>
            <button type="button" onClick={() => openLabelEvidence("标签生产治理台")}>
              <Headphones size={14} />
              证据审查
            </button>
            <button type="button" className="primary" onClick={saveCandidateVersion} disabled={labelEntityAction !== null} title="保存后端 LabelVersion 强 ID，并以其 trace_id 作为闭环 root">
              <Check size={14} />
              保存
            </button>
          </div>
          {renderLabelOptimizationRunRail()}
          {renderV2ActionFeedback()}
        </section>
        <section className="module-panel label-v2-column label-v2-left">{left}</section>
        <section className="module-panel label-v2-column label-v2-main">{main}</section>
        <section className="module-panel label-v2-column label-v2-right">{right}</section>
      </div>
    );

  const renderV2ScenarioRail = () => (
      <>
        <PanelHeader title="场景上下文" subtitle="当前任务只影响本项目候选版本" icon={<Layers size={16} />} />
        <div className="label-v2-list">
          {labelScenarioPlaybooks.map((scenario) => (
            <button
              key={scenario.key}
              type="button"
              className={scenario.key === activeScenario.key ? "active" : ""}
              onClick={() => selectScenario(scenario.key)}
            >
              <span>{scenario.source}</span>
              <strong>{scenario.name}</strong>
              <em>{scenario.goal}</em>
              <b>{scenario.confidence}%</b>
            </button>
          ))}
        </div>
        <div className="label-v2-chip-group" aria-label="样本来源">
          {sourceOptions.map((item) => (
            <button
              key={item}
              type="button"
              className={sourceFilter === item ? "active" : ""}
              onClick={() => {
                setSourceFilter(item);
                setActionFeedback(`样本来源已切换到 ${item}，候选抽取和评测集会按当前来源过滤。`);
              }}
            >
              {item}
            </button>
          ))}
        </div>
      </>
    );

  const renderLabelFactReadState = () => {
      if (LABEL_DEMO_MODE || labelFactReadState === "ready") return null;
      const title = labelFactReadState === "loading"
        ? "正在读取后端标签事实"
        : labelFactReadState === "failed"
          ? "Observation / Aggregate 读回失败"
          : labelFactReadState === "empty"
            ? "抽取已完成，但尚无可展示的标签事实"
            : "尚未运行真实标签抽取";
      const detail = labelFactReadState === "failed"
        ? labelFactReadError
        : labelFactReadState === "empty"
          ? "后端未返回当前 extraction_run / subject / label_version 对应的 Observation 或 Aggregate；页面不会回退到 mock 候选。"
          : labelFactReadState === "loading"
            ? "正在按 subject、标签版本与 extraction_run 读取不可变 Observation，并关联确定性 Aggregate。"
            : "点击“运行抽取”创建 LabelExtractionRun；成功后页面再读取后端物化结果。";
      return (
        <div className={`label-fact-state is-${labelFactReadState}`} role="status" data-testid={`label-fact-state-${labelFactReadState}`}>
          <strong>{title}</strong>
          <p>{detail}</p>
          {(labelFactReadState === "failed" || labelFactReadState === "empty") && labelExtractionBackendRun ? (
            <button type="button" onClick={retryMaterializedLabelFacts}>
              <RotateCcw size={14} />
              重试读取事实
            </button>
          ) : null}
        </div>
      );
    };

  return {
    renderLabelOptimizationRunRail,
    renderLabelEntityNotice,
    renderV2ActionFeedback,
    renderV2Page,
    renderV2ScenarioRail,
    renderLabelFactReadState
  };
}

export type LabelsShellRenders = ReturnType<typeof buildLabelsShellRenders>;
