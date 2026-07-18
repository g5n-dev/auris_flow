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
import type { BackendActionReceipt } from "../../../api/client";
import { createPlatformMutation, createUserIntentIdempotencyKey, getEvaluationRun, getLabelVersionResource, lockLabelVersionForEvaluation, retryBackendRun } from "../../../api/client";
import { backendRunFailed, backendRunSucceeded, normalizeBackendRunStatus } from "../../../shared/runtime/backendRunStatus";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { LABEL_EVAL_POLL_INTERVAL_MS, LABEL_EVAL_POLL_LIMIT } from "../constants";
import type { LabelEvalIntent } from "../types";

type BuildLabelsEvaluationActionsScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions & LabelsOptimizationActions & LabelsReviewActions & LabelsPersistenceActions & LabelsPromptActions;

export function buildLabelsEvaluationActions(backendPromptCandidateId: BuildLabelsEvaluationActionsScope["backendPromptCandidateId"], backendPromptVersionId: BuildLabelsEvaluationActionsScope["backendPromptVersionId"], labelAgentBackendRun: BuildLabelsEvaluationActionsScope["labelAgentBackendRun"], labelEvalInFlightRef: BuildLabelsEvaluationActionsScope["labelEvalInFlightRef"], labelEvalPollGenerationRef: BuildLabelsEvaluationActionsScope["labelEvalPollGenerationRef"], labelEvalPreflightRef: BuildLabelsEvaluationActionsScope["labelEvalPreflightRef"], labelEvalRequest: BuildLabelsEvaluationActionsScope["labelEvalRequest"], labelEvalRun: BuildLabelsEvaluationActionsScope["labelEvalRun"], labelEvaluationLock: BuildLabelsEvaluationActionsScope["labelEvaluationLock"], labelRootTraceId: BuildLabelsEvaluationActionsScope["labelRootTraceId"], labelShortTrace: BuildLabelsEvaluationActionsScope["labelShortTrace"], lastLabelEvalIntentRef: BuildLabelsEvaluationActionsScope["lastLabelEvalIntentRef"], lockedLabelVersionId: BuildLabelsEvaluationActionsScope["lockedLabelVersionId"], lockedPromptVersionId: BuildLabelsEvaluationActionsScope["lockedPromptVersionId"], optimizationInputs: BuildLabelsEvaluationActionsScope["optimizationInputs"], promptCandidateFact: BuildLabelsEvaluationActionsScope["promptCandidateFact"], setActionFeedback: BuildLabelsEvaluationActionsScope["setActionFeedback"], setExperimentState: BuildLabelsEvaluationActionsScope["setExperimentState"], setLabelEvalRequest: BuildLabelsEvaluationActionsScope["setLabelEvalRequest"], setLabelEvalRun: BuildLabelsEvaluationActionsScope["setLabelEvalRun"], setLabelEvaluationLock: BuildLabelsEvaluationActionsScope["setLabelEvaluationLock"], setSelectedExperimentMetric: BuildLabelsEvaluationActionsScope["setSelectedExperimentMetric"]) {
  const waitForLabelEvalPoll = () => new Promise<void>((resolve) => {
      window.setTimeout(resolve, LABEL_EVAL_POLL_INTERVAL_MS);
    });

  const applyLabelEvalReadback = (
      intent: LabelEvalIntent,
      run: BackendActionReceipt,
      traceId?: string
    ) => {
      const backendStatus = normalizeBackendRunStatus(run.status);
      const common = {
        backendStatus,
        runId: run.id,
        traceId: traceId ?? run.trace_id
      };
      setLabelEvalRun(run);
      intent.runId = run.id;
      lastLabelEvalIntentRef.current = intent;
      if (backendRunSucceeded(backendStatus)) {
        setExperimentState("影子评测中");
        setLabelEvalRequest({ ...common, status: "success" });
        setActionFeedback(
          `EvalRun ${run.id} 已由 GET 读回真实 ${backendStatus} 终态；当前候选、优化运行与评测集版本已锁定 · Trace ${labelShortTrace(common.traceId)}。`
        );
        return "terminal" as const;
      }
      if (backendRunFailed(backendStatus) || backendStatus === "blocked") {
        const reason = typeof run.raw.failure_reason === "string"
          ? run.raw.failure_reason
          : typeof run.raw.message === "string"
            ? run.raw.message
            : `后端 EvalRun 进入 ${backendStatus}`;
        setExperimentState("未开始");
        setLabelEvalRequest({ ...common, status: "failed", error: reason });
        setActionFeedback(`EvalRun ${run.id} 失败：${reason}。可按原用户意图重试，不创建重复评测。`);
        return "terminal" as const;
      }
      setLabelEvalRequest({ ...common, status: "pending" });
      setActionFeedback(`EvalRun ${run.id} 当前为 ${backendStatus || "pending"}，正在等待后端物化与真实终态。`);
      return "pending" as const;
    };

  const pollLabelEvalRun = async (intent: LabelEvalIntent, runId: string, initialTraceId?: string) => {
      const generation = ++labelEvalPollGenerationRef.current;
      let lastTraceId = initialTraceId;
      for (let attempt = 1; attempt <= LABEL_EVAL_POLL_LIMIT; attempt += 1) {
        if (labelEvalPollGenerationRef.current !== generation) return;
        try {
          const response = await getEvaluationRun(runId, { correlationId: labelRootTraceId });
          lastTraceId = response.meta?.trace_id ?? response.data.trace_id ?? lastTraceId;
          if (applyLabelEvalReadback(intent, response.data, lastTraceId) === "terminal") return;
        } catch (error) {
          const message = error instanceof Error ? error.message : "读取 EvalRun 失败";
          setLabelEvalRequest({
            status: "pending",
            backendStatus: "readback-pending",
            runId,
            traceId: lastTraceId,
            error: message
          });
          setActionFeedback(`EvalRun ${runId} 暂时无法读回：${message}。将继续受控轮询。`);
        }
        if (attempt < LABEL_EVAL_POLL_LIMIT) await waitForLabelEvalPoll();
      }
      setActionFeedback(`EvalRun ${runId} 尚未进入终态；已停止自动轮询，可手动刷新，且不会重复提交。`);
    };

  const executeLabelEvalIntent = async (intent: LabelEvalIntent) => {
      if (labelEvalInFlightRef.current) return;
      labelEvalInFlightRef.current = true;
      lastLabelEvalIntentRef.current = intent;
      setLabelEvalRequest({ status: "pending", backendStatus: "requesting" });
      setExperimentState("影子评测中");
      setSelectedExperimentMetric("F1");
      setActionFeedback("正在创建与当前候选版本绑定的 EvalRun；POST 受理不视为评测成功…");
      try {
        const receipt = await createPlatformMutation("evaluation", intent.payload, {
          idempotencyKey: intent.idempotencyKey,
          correlationId: labelRootTraceId
        });
        intent.runId = receipt.data.id;
        lastLabelEvalIntentRef.current = intent;
        setLabelEvalRun(receipt.data);
        setLabelEvalRequest({
          status: "pending",
          backendStatus: normalizeBackendRunStatus(receipt.data.status),
          runId: receipt.data.id,
          traceId: receipt.meta?.trace_id ?? receipt.data.trace_id
        });
        await pollLabelEvalRun(intent, receipt.data.id, receipt.meta?.trace_id ?? receipt.data.trace_id);
      } catch (error) {
        const message = error instanceof Error ? error.message : "未知错误";
        setExperimentState("未开始");
        setLabelEvalRequest({ status: "failed", backendStatus: "request-failed", error: message });
        setActionFeedback(`EvalRun 创建失败：${message}。可使用原幂等键重试同一用户意图。`);
      } finally {
        labelEvalInFlightRef.current = false;
      }
    };

  const retryLabelEval = async () => {
      const intent = lastLabelEvalIntentRef.current;
      if (!intent || labelEvalInFlightRef.current) return;
      if (!intent.runId) {
        await executeLabelEvalIntent(intent);
        return;
      }
      labelEvalInFlightRef.current = true;
      setLabelEvalRequest({
        status: "pending",
        backendStatus: "retrying",
        runId: intent.runId
      });
      setActionFeedback(`正在按原意图重试 EvalRun ${intent.runId}，不会复制或改写锁定输入…`);
      try {
        const receipt = await retryBackendRun(
          intent.runId,
          { reason: "标签治理页面按原锁定意图重试失败 EvalRun" },
          { idempotencyKey: intent.retryIdempotencyKey, correlationId: labelRootTraceId }
        );
        intent.runId = receipt.data.id;
        lastLabelEvalIntentRef.current = intent;
        setLabelEvalRun(receipt.data);
        await pollLabelEvalRun(intent, receipt.data.id, receipt.meta?.trace_id ?? receipt.data.trace_id);
      } catch (error) {
        const message = error instanceof Error ? error.message : "未知错误";
        setLabelEvalRequest({
          status: "failed",
          backendStatus: "retry-failed",
          runId: intent.runId,
          error: message
        });
        setActionFeedback(`EvalRun 原意图重试失败：${message}。可继续用同一幂等键安全重试。`);
      } finally {
        labelEvalInFlightRef.current = false;
      }
    };

  const refreshLabelEval = () => {
      const intent = lastLabelEvalIntentRef.current;
      if (!intent?.runId || labelEvalInFlightRef.current) return;
      labelEvalInFlightRef.current = true;
      void pollLabelEvalRun(intent, intent.runId, labelEvalRequest.traceId).finally(() => {
        labelEvalInFlightRef.current = false;
      });
    };

  const runPromptEval = async () => {
      if (
        labelEvalPreflightRef.current
        || labelEvalRequest.status === "pending"
        || labelEvalRequest.status === "success"
      ) return;
      const optimizationRun = labelAgentBackendRun;
      const promptVersionId = backendPromptVersionId;
      if (!optimizationRun || !backendRunSucceeded(optimizationRun.status)) {
        setActionFeedback("评测未提交：请先完成真实优化运行并等候 Prompt 候选物化。 ");
        return;
      }
      if (!backendPromptCandidateId || !promptVersionId) {
        setActionFeedback("评测未提交：优化回执尚未返回同时锁定的 PromptVersionCandidate 与 PromptVersion 强 ID。");
        return;
      }
      if (!lockedLabelVersionId || !labelRootTraceId) {
        setActionFeedback("评测未提交：缺少后端 LabelVersion 强 ID 或闭环 root trace。");
        return;
      }
      const promptApprovalStatus = String(promptCandidateFact?.status ?? "");
      if (!LABEL_DEMO_MODE && promptApprovalStatus !== "approved") {
        setActionFeedback(`评测未提交：Prompt 候选当前 ${promptApprovalStatus || "awaiting-review"}，必须完成两份密封审核或独立仲裁。`);
        return;
      }
      labelEvalPreflightRef.current = true;
      setLabelEvalRequest({ status: "pending", backendStatus: "locking-bundle" });
      setActionFeedback("正在校验 Prompt 双盲审批并冻结标签、模型、策略和评测集 Bundle…");
      try {
        const labelVersion = await getLabelVersionResource(lockedLabelVersionId, {
          correlationId: labelRootTraceId
        });
        const expectedResourceVersion = Number(labelVersion.data.resource_version);
        if (!Number.isInteger(expectedResourceVersion) || expectedResourceVersion < 1) {
          throw new Error("LabelVersion 读回缺少有效 resource_version，不能建立并发安全的评测锁");
        }
        const lock = await lockLabelVersionForEvaluation(
          lockedLabelVersionId,
          {
            expected_resource_version: expectedResourceVersion,
            prompt_version_id: promptVersionId,
            model_version: optimizationInputs.modelVersion,
            aggregation_policy_version_id: optimizationInputs.aggregationPolicyVersion,
            eval_dataset_version_id: optimizationInputs.evalDatasetVersion,
            optimization_run_id: optimizationRun.id,
            confirmation: "lock-for-evaluation"
          },
          {
            idempotencyKey: createUserIntentIdempotencyKey(
              `label_evaluation_lock_${lockedLabelVersionId}`
            ),
            correlationId: labelRootTraceId
          }
        );
        setLabelEvaluationLock(lock.data);
        setActionFeedback(
          `评测 Bundle 已冻结 ${lock.data.snapshot_sha256.slice(0, 12)}；正在创建 EvalRun…`
        );
        const runId = `eval_labeling_${Date.now().toString(36)}`;
        const intent: LabelEvalIntent = {
          payload: {
            run_id: runId,
            capability: "labeling",
            dataset_id: lock.data.eval_dataset_version_id,
            eval_dataset_version_id: lock.data.eval_dataset_version_id,
            model_version: lock.data.model_version,
            label_version_id: lock.data.label_version_id,
            prompt_version_id: lock.data.prompt_version_id,
            aggregation_policy_version_id: lock.data.aggregation_policy_version_id,
            optimization_run_id: lock.data.optimization_run_id,
            evaluation_suites: ["golden", "boundary", "adversarial", "fresh", "canary", "regression"],
            source: "ui_label_locked_evaluation"
          },
          idempotencyKey: createUserIntentIdempotencyKey(`label_eval_${runId}`),
          retryIdempotencyKey: createUserIntentIdempotencyKey(`label_eval_retry_${runId}`)
        };
        await executeLabelEvalIntent(intent);
      } catch (error) {
        const message = error instanceof Error ? error.message : "评测 Bundle 冻结失败";
        setExperimentState("未开始");
        setLabelEvalRequest({ status: "failed", backendStatus: "lock-failed", error: message });
        setActionFeedback(`评测未提交：${message}。标签版本与线上结果均未被覆盖。`);
      } finally {
        labelEvalPreflightRef.current = false;
      }
    };

  const labelEvalPending = labelEvalRequest.status === "pending";

  const labelEvalSucceeded = labelEvalRequest.status === "success"
      && Boolean(labelEvalRun)
      && backendRunSucceeded(labelEvalRun?.status);

  const labelEvalActionLabel = labelEvalPending
      ? labelEvalRequest.backendStatus === "locking-bundle" ? "冻结评测 Bundle" : "评测运行中"
      : labelEvalSucceeded
        ? "评测已完成"
        : labelEvaluationLock
          ? "运行锁定评测"
          : "锁定并运行评测";

  const promptReviewApproved = LABEL_DEMO_MODE || String(promptCandidateFact?.status ?? "") === "approved";

  const labelEvalSubmitDisabled = labelEvalPending
      || labelEvalSucceeded
      || !lockedLabelVersionId
      || !lockedPromptVersionId
      || !promptReviewApproved;

  return {
    waitForLabelEvalPoll,
    applyLabelEvalReadback,
    pollLabelEvalRun,
    executeLabelEvalIntent,
    retryLabelEval,
    refreshLabelEval,
    runPromptEval,
    labelEvalPending,
    labelEvalSucceeded,
    labelEvalActionLabel,
    promptReviewApproved,
    labelEvalSubmitDisabled
  };
}

export type LabelsEvaluationActions = ReturnType<typeof buildLabelsEvaluationActions>;
