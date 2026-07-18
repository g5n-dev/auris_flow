import type { EvaluationModuleProps } from "../types";
import type { EvaluationState } from "./useEvaluationState";
import type { EvaluationSelection } from "./buildEvaluationSelection";
import type { EvaluationFocusRecovery } from "./useEvaluationFocusRecovery";
import type { EvaluationContextActions } from "./buildEvaluationContextActions";
import type { HotwordPollingActions } from "./buildHotwordPollingActions";
import type { HotwordVersionRecovery } from "./useHotwordVersionRecovery";
import type { BackendActionReceipt } from "../../../api/client";
import { createEvaluationFeedbackTask, createPlatformMutation } from "../../../api/client";
import type { EvaluationCapabilityKey } from "../../../shared/contracts/evaluation";
import { backendRunStatusLabel, operationStatusFromBackendRun } from "../../../shared/runtime/backendRunStatus";
import type { EvaluationManualReviewItem } from "../types";

type BuildEvaluationRunActionsScope = EvaluationModuleProps & EvaluationState & EvaluationSelection & EvaluationFocusRecovery & EvaluationContextActions & HotwordPollingActions & HotwordVersionRecovery;

export function buildEvaluationRunActions(activeEvalRun: BuildEvaluationRunActionsScope["activeEvalRun"], badcaseWorkflow: BuildEvaluationRunActionsScope["badcaseWorkflow"], datasetDraft: BuildEvaluationRunActionsScope["datasetDraft"], labelVersion: BuildEvaluationRunActionsScope["labelVersion"], modelVersion: BuildEvaluationRunActionsScope["modelVersion"], pushRunRecord: BuildEvaluationRunActionsScope["pushRunRecord"], runScope: BuildEvaluationRunActionsScope["runScope"], selectedBadcaseWorkflow: BuildEvaluationRunActionsScope["selectedBadcaseWorkflow"], selectedCapability: BuildEvaluationRunActionsScope["selectedCapability"], selectedDataset: BuildEvaluationRunActionsScope["selectedDataset"], selectedLabelingCase: BuildEvaluationRunActionsScope["selectedLabelingCase"], selectedManualReview: BuildEvaluationRunActionsScope["selectedManualReview"], setActiveEvalRun: BuildEvaluationRunActionsScope["setActiveEvalRun"], setEvaluationAction: BuildEvaluationRunActionsScope["setEvaluationAction"], setEvaluationNotice: BuildEvaluationRunActionsScope["setEvaluationNotice"], setFeedbackDraft: BuildEvaluationRunActionsScope["setFeedbackDraft"], setManualReviews: BuildEvaluationRunActionsScope["setManualReviews"], setRunReceipt: BuildEvaluationRunActionsScope["setRunReceipt"], setSelectedBadcaseId: BuildEvaluationRunActionsScope["setSelectedBadcaseId"], setSelectedCapabilityKey: BuildEvaluationRunActionsScope["setSelectedCapabilityKey"], shortTrace: BuildEvaluationRunActionsScope["shortTrace"]) {
  const requestEvaluationRun = async (
      reason: string,
      overrides: Record<string, unknown> = {}
    ): Promise<BackendActionReceipt> => {
      const receipt = await createPlatformMutation("evaluation", {
        dataset_id: selectedDataset.id,
        dataset_version: selectedDataset.version,
        model_version: modelVersion,
        label_version: labelVersion,
        capability: "generic", target_capability: selectedCapability.key,
        source: "ui_evaluation_module",
        reason,
        ...overrides
      });
      const runState: BackendActionReceipt = {
        id: receipt.data.id,
        status: receipt.data.status,
        trace_id: receipt.meta?.trace_id ?? receipt.data.trace_id,
        raw: receipt.data.raw
      };
      setActiveEvalRun(runState);
      return runState;
    };

  const ensureEvaluationRun = async (reason: string): Promise<BackendActionReceipt> => {
      if (activeEvalRun) return activeEvalRun;
      return requestEvaluationRun(reason);
    };

  const requestEvaluationFeedbackTask = async (
      evalRun: BackendActionReceipt,
      payload: {
        badcase_refs: string[];
        target: string;
        reason: string;
        [key: string]: unknown;
      }
    ): Promise<BackendActionReceipt> => {
      const receipt = await createEvaluationFeedbackTask(evalRun.id, payload);
      return receipt.data;
    };

  const selectCapability = (key: EvaluationCapabilityKey) => {
      setSelectedCapabilityKey(key);
      const nextBadcase = badcaseWorkflow.find((item) => item.capability === key);
      if (nextBadcase) setSelectedBadcaseId(nextBadcase.id);
    };

  const runEvaluation = async () => {
      setEvaluationAction("run");
      setRunReceipt(`运行中：${selectedDataset.name} / ${modelVersion} / ${labelVersion}`);
      setEvaluationNotice({
        status: "pending",
        title: "评测运行中",
        detail: `${selectedDataset.name} / ${runScope}`
      });
      try {
        const runState = await requestEvaluationRun("manual_evaluation_run");
        const statusLabel = backendRunStatusLabel(runState.status);
        setRunReceipt(`BFF ${runState.id}：${selectedDataset.name}`);
        setFeedbackDraft(`待回流：${selectedCapability.badcases} 条 -> ${runState.id}`);
        pushRunRecord("自动化评测已提交", `${runState.id} / ${statusLabel} / trace ${shortTrace(runState.trace_id)}`, "运行中");
        setEvaluationNotice({
          status: operationStatusFromBackendRun(runState.status),
          title: "评测已提交",
          detail: `${runState.id} / trace ${shortTrace(runState.trace_id)}`
        });
      } catch (error) {
        setEvaluationNotice({
          status: "error",
          title: "评测提交失败",
          detail: error instanceof Error ? error.message : "BFF 请求失败，请重试。"
        });
      } finally {
        setEvaluationAction(null);
      }
    };

  const decideManualReview = (status: EvaluationManualReviewItem["status"]) => {
      setManualReviews((current) => current.map((item) => (item.id === selectedManualReview.id ? { ...item, status } : item)));
      const feedback = `HR-${selectedManualReview.id} 已更新为「${status}」：${selectedManualReview.title}`;
      setFeedbackDraft(feedback);
      pushRunRecord("人工评测决策", feedback, status === "已转 badcase" ? "待确认" : "完成");
      setEvaluationNotice({
        status: "success",
        title: "人工评测已回写",
        detail: `${selectedManualReview.candidateLabel} 已写入 Human Loop 处理记录，后续可进入标签规则或 badcase。`
      });
    };

  const saveDatasetDraft = () => {
      const detail = `${selectedDataset.name} 目标 ${datasetDraft.targetSize} 样本 / ${datasetDraft.owner} / ${datasetDraft.layer}`;
      pushRunRecord("评测集草稿已保存", detail);
      setEvaluationNotice({
        status: "success",
        title: "评测集草稿已保存",
        detail: `${selectedDataset.version} 已记录分层策略和补样目标，不覆盖线上评测集。`
      });
    };

  const createFeedbackTask = async () => {
      setEvaluationAction("feedback");
      setEvaluationNotice({
        status: "pending",
        title: "正在创建回流任务",
        detail: selectedBadcaseWorkflow.id
      });
      try {
        const evalRun = await ensureEvaluationRun("feedback_task_prerequisite");
        const feedbackRun = await requestEvaluationFeedbackTask(evalRun, {
          badcase_refs: [selectedBadcaseWorkflow.id, selectedLabelingCase.id],
          target: selectedBadcaseWorkflow.target,
          reason: selectedBadcaseWorkflow.title,
          owner: selectedBadcaseWorkflow.owner,
          source: "ui_badcase_board"
        });
        const feedbackTaskId =
          typeof feedbackRun.raw.feedback_task_id === "string" ? feedbackRun.raw.feedback_task_id : feedbackRun.id;
        setFeedbackDraft(`已创建回流任务 ${feedbackTaskId}`);
        pushRunRecord("回流任务已提交", `${feedbackRun.id} / ${shortTrace(feedbackRun.trace_id)}`, "运行中");
        setEvaluationNotice({
          status: operationStatusFromBackendRun(feedbackRun.status),
          title: "回流已提交",
          detail: `${feedbackTaskId} / ${shortTrace(feedbackRun.trace_id)}`
        });
      } catch (error) {
        setEvaluationNotice({
          status: "error",
          title: "回流创建失败",
          detail: error instanceof Error ? error.message : "BFF 请求失败，请重试。"
        });
      } finally {
        setEvaluationAction(null);
      }
    };

  return {
    requestEvaluationRun,
    ensureEvaluationRun,
    requestEvaluationFeedbackTask,
    selectCapability,
    runEvaluation,
    decideManualReview,
    saveDatasetDraft,
    createFeedbackTask
  };
}

export type EvaluationRunActions = ReturnType<typeof buildEvaluationRunActions>;
