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
import { operationStatusFromBackendRun } from "../../../shared/runtime/backendRunStatus";
import { evaluationLabelingCases, evaluationLabelingMetrics, evaluationPromptSuggestions } from "../catalog";
import type { EvaluationPromptSuggestion } from "../types";

type BuildEvaluationLabelPromptActionsScope = EvaluationModuleProps & EvaluationState & EvaluationSelection & EvaluationFocusRecovery & EvaluationContextActions & HotwordPollingActions & HotwordVersionRecovery & EvaluationRunActions & EvaluationBadcaseActions & HotwordGateModel & HotwordReleaseActions;

export function buildEvaluationLabelPromptActions(appliedPromptSuggestions: BuildEvaluationLabelPromptActionsScope["appliedPromptSuggestions"], candidatePromptDraft: BuildEvaluationLabelPromptActionsScope["candidatePromptDraft"], ensureEvaluationRun: BuildEvaluationLabelPromptActionsScope["ensureEvaluationRun"], promptExperiment: BuildEvaluationLabelPromptActionsScope["promptExperiment"], pushRunRecord: BuildEvaluationLabelPromptActionsScope["pushRunRecord"], requestEvaluationFeedbackTask: BuildEvaluationLabelPromptActionsScope["requestEvaluationFeedbackTask"], requestEvaluationRun: BuildEvaluationLabelPromptActionsScope["requestEvaluationRun"], selectedBadcaseWorkflow: BuildEvaluationLabelPromptActionsScope["selectedBadcaseWorkflow"], selectedLabelingCase: BuildEvaluationLabelPromptActionsScope["selectedLabelingCase"], selectedLabelingMetric: BuildEvaluationLabelPromptActionsScope["selectedLabelingMetric"], setActiveModule: BuildEvaluationLabelPromptActionsScope["setActiveModule"], setActiveTab: BuildEvaluationLabelPromptActionsScope["setActiveTab"], setAppliedPromptSuggestions: BuildEvaluationLabelPromptActionsScope["setAppliedPromptSuggestions"], setBadcaseWorkflow: BuildEvaluationLabelPromptActionsScope["setBadcaseWorkflow"], setCandidatePromptDraft: BuildEvaluationLabelPromptActionsScope["setCandidatePromptDraft"], setEvaluationAction: BuildEvaluationLabelPromptActionsScope["setEvaluationAction"], setEvaluationNotice: BuildEvaluationLabelPromptActionsScope["setEvaluationNotice"], setFeedbackDraft: BuildEvaluationLabelPromptActionsScope["setFeedbackDraft"], setPromptStatus: BuildEvaluationLabelPromptActionsScope["setPromptStatus"], setSelectedLabelingCaseId: BuildEvaluationLabelPromptActionsScope["setSelectedLabelingCaseId"], setSelectedLabelingTask: BuildEvaluationLabelPromptActionsScope["setSelectedLabelingTask"], setSelectedPromptSuggestionId: BuildEvaluationLabelPromptActionsScope["setSelectedPromptSuggestionId"], shortTrace: BuildEvaluationLabelPromptActionsScope["shortTrace"]) {
  const selectLabelingTask = (taskKey: string) => {
      setSelectedLabelingTask(taskKey);
      const nextCase = evaluationLabelingCases.find((item) => item.taskKey === taskKey);
      if (nextCase) setSelectedLabelingCaseId(nextCase.id);
      const metric = evaluationLabelingMetrics.find((item) => item.taskKey === taskKey);
      if (metric) {
        setFeedbackDraft(`${metric.task} 打标评测已切换：${metric.candidateVersion} F1 ${metric.f1}，Prompt ${metric.promptVersion}`);
      }
    };

  const handleLabelingAction = (action: "review" | "badcase" | "rule" | "prompt") => {
      const actionText = {
        review: "送人审",
        badcase: "加入 badcase",
        rule: "生成规则候选",
        prompt: "生成 Prompt 优化建议"
      }[action];
      if (action === "badcase") {
        setBadcaseWorkflow((current) => [
          {
            id: `LB-${selectedLabelingCase.id}`,
            capability: "tagging" as const,
            title: `${selectedLabelingCase.label} ${selectedLabelingCase.issue}`,
            severity: selectedLabelingCase.issue === "冲突" ? "高" : "中",
            status: "待回流" as const,
            source: selectedLabelingCase.evidenceWindow,
            rootCause: selectedLabelingCase.asr,
            fix: `回流 ${selectedLabelingMetric.task} 打标黄金集，并同步 Prompt 反例。`,
            target: "Prompt 优化 / 打标黄金集",
            owner: selectedLabelingMetric.owner
          },
          ...current
        ].slice(0, 8));
      }
      if (action === "prompt") {
        setPromptStatus("已有建议");
        setSelectedPromptSuggestionId(evaluationPromptSuggestions[0].id);
      }
      setFeedbackDraft(`${actionText}：${selectedLabelingCase.id} / ${selectedLabelingCase.label} / ${selectedLabelingCase.issue}`);
      pushRunRecord("打标评测动作", `${actionText} -> ${selectedLabelingCase.id}`, action === "review" ? "待确认" : "完成");
      setEvaluationNotice({
        status: "success",
        title: `已${actionText}`,
        detail: `${selectedLabelingCase.label} 已绑定 ${selectedLabelingMetric.task}、${selectedLabelingMetric.candidateVersion} 和证据窗口。`
      });
    };

  const generatePromptSuggestions = () => {
      setPromptStatus("已有建议");
      setEvaluationNotice({
        status: "success",
        title: "Prompt 优化建议已生成",
        detail: `${selectedLabelingMetric.task} 的 ${selectedLabelingCase.issue} 样本已生成 ${evaluationPromptSuggestions.length} 类建议。`
      });
      pushRunRecord("Prompt 建议生成", `${selectedLabelingMetric.task} / ${selectedLabelingCase.id}`, "完成");
    };

  const applyPromptSuggestion = (suggestion: EvaluationPromptSuggestion) => {
      setSelectedPromptSuggestionId(suggestion.id);
      setAppliedPromptSuggestions((current) => Array.from(new Set([...current, suggestion.id])));
      setCandidatePromptDraft((current) => `${current}\n\n[${suggestion.title}] ${suggestion.detail}\n示例：${suggestion.example}`);
      setPromptStatus("已有建议");
      setEvaluationNotice({
        status: "success",
        title: "建议已应用到候选 Prompt",
        detail: `${suggestion.title} 已写入候选草稿，预计影响 ${suggestion.impact}。`
      });
    };

  const createPromptCandidate = () => {
      setPromptStatus("候选已创建");
      setCandidatePromptDraft(`PromptVersion ${promptExperiment.candidateVersion}\n任务：${selectedLabelingMetric.task}\n来源 badcase：${selectedLabelingCase.id}\n已应用建议：${appliedPromptSuggestions.length || 1} 项\n策略：冲突样本不自动通过，必须输出 source、confidence、prompt_version 和 human_review_required。`);
      pushRunRecord("候选 Prompt 创建", `${promptExperiment.candidateVersion} / ${selectedLabelingMetric.task}`, "待确认");
      setEvaluationNotice({
        status: "success",
        title: "候选 Prompt 已创建",
        detail: `${promptExperiment.candidateVersion} 只写候选版本，不覆盖线上 Prompt。`
      });
    };

  const runPromptShadowEval = async () => {
      setEvaluationAction("prompt");
      setEvaluationNotice({
        status: "pending",
        title: "影子评测运行中",
        detail: `${promptExperiment.dataset} / ${promptExperiment.candidateVersion}`
      });
      try {
        const runState = await requestEvaluationRun("prompt_shadow_eval", {
          dataset_id: "prompt-regression",
          dataset_version: promptExperiment.dataset,
          capability: "generic",
          current_prompt_version: promptExperiment.currentVersion,
          candidate_prompt_version: promptExperiment.candidateVersion,
          prompt_version: promptExperiment.candidateVersion,
          badcase_refs: [selectedLabelingCase.id, selectedBadcaseWorkflow.id],
          draft_length: candidatePromptDraft.length,
          suggestion_count: appliedPromptSuggestions.length
        });
        setPromptStatus("候选已创建");
        pushRunRecord("Prompt 影子评测已提交", `${runState.id} / ${shortTrace(runState.trace_id)}`, "运行中");
        setEvaluationNotice({
          status: operationStatusFromBackendRun(runState.status),
          title: "影子评测提交",
          detail: `${runState.id} / trace ${shortTrace(runState.trace_id)}`
        });
      } catch (error) {
        setEvaluationNotice({
          status: "error",
          title: "影子提交失败",
          detail: error instanceof Error ? error.message : "BFF 请求失败，请重试。"
        });
      } finally {
        setEvaluationAction(null);
      }
    };

  const createPromptReleaseDraft = async () => {
      setEvaluationAction("prompt_release");
      setEvaluationNotice({
        status: "pending",
        title: "正在生成发布草稿",
        detail: `${promptExperiment.candidateVersion} -> 发布门禁`
      });
      try {
        const evalRun = await ensureEvaluationRun("prompt_release_draft_prerequisite");
        const feedbackRun = await requestEvaluationFeedbackTask(evalRun, {
          badcase_refs: [selectedLabelingCase.id, selectedBadcaseWorkflow.id],
          target: "Prompt 发布草稿 / 发布门禁",
          reason: `${promptExperiment.candidateVersion} badcase 回归 ${promptExperiment.badcaseRegression}%`,
          prompt_version: promptExperiment.currentVersion,
          candidate_version: promptExperiment.candidateVersion,
          draft_length: candidatePromptDraft.length,
          source: "ui_prompt_release_draft"
        });
        const feedbackTaskId =
          typeof feedbackRun.raw.feedback_task_id === "string" ? feedbackRun.raw.feedback_task_id : feedbackRun.id;
        setPromptStatus("发布草稿");
        setFeedbackDraft(`Prompt 发布草稿 ${feedbackTaskId}`);
        pushRunRecord("Prompt 发布草稿已提交", `${feedbackRun.id} / ${promptExperiment.candidateVersion}`, "待确认");
        setEvaluationNotice({
          status: operationStatusFromBackendRun(feedbackRun.status),
          title: "草稿已提交",
          detail: `${feedbackTaskId} / trace ${shortTrace(feedbackRun.trace_id)}`
        });
      } catch (error) {
        setEvaluationNotice({
          status: "error",
          title: "草稿提交失败",
          detail: error instanceof Error ? error.message : "BFF 请求失败，请重试。"
        });
      } finally {
        setEvaluationAction(null);
      }
    };

  const jumpToLabelPromptWorkbench = () => {
      setActiveModule("labels");
      setActiveTab("prompt");
    };

  return {
    selectLabelingTask,
    handleLabelingAction,
    generatePromptSuggestions,
    applyPromptSuggestion,
    createPromptCandidate,
    runPromptShadowEval,
    createPromptReleaseDraft,
    jumpToLabelPromptWorkbench
  };
}

export type EvaluationLabelPromptActions = ReturnType<typeof buildEvaluationLabelPromptActions>;
