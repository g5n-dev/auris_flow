import type { LabelsActionScope } from "./labelsActionScope";
import type { LabelsPersistenceActions } from "./buildLabelsPersistenceActions";
import type { LabelsPromptActions } from "./buildLabelsPromptActions";
import type { LabelsEvaluationActions } from "./buildLabelsEvaluationActions";
import { buildLabelsReleasePolicy, labelPublishReason } from "./buildLabelsReleasePolicy";
import { createReleaseDeployment, createUserIntentIdempotencyKey, getReleaseDeployment, transitionReleaseDeployment, type BackendActionReceipt } from "../../../api/client";
import { backendRunFailed, normalizeBackendRunStatus } from "../../../shared/runtime/backendRunStatus";
import { LABEL_PUBLISH_POLL_INTERVAL_MS, LABEL_PUBLISH_POLL_LIMIT } from "../constants";
import type { LabelPublishAction, LabelPublishIntent } from "../types";

type BuildLabelsReleaseActionsScope = LabelsActionScope & LabelsPersistenceActions & LabelsPromptActions & LabelsEvaluationActions;

export function buildLabelsReleaseActions(backendPromptVersionId: BuildLabelsReleaseActionsScope["backendPromptVersionId"], backendReleaseDeploymentId: BuildLabelsReleaseActionsScope["backendReleaseDeploymentId"], labelEvalRun: BuildLabelsReleaseActionsScope["labelEvalRun"], labelEvalSucceeded: BuildLabelsReleaseActionsScope["labelEvalSucceeded"], labelPublishInFlightRef: BuildLabelsReleaseActionsScope["labelPublishInFlightRef"], labelPublishPollGenerationRef: BuildLabelsReleaseActionsScope["labelPublishPollGenerationRef"], labelPublishRequest: BuildLabelsReleaseActionsScope["labelPublishRequest"], labelRootTraceId: BuildLabelsReleaseActionsScope["labelRootTraceId"], lastLabelPublishIntentRef: BuildLabelsReleaseActionsScope["lastLabelPublishIntentRef"], lockedLabelVersionId: BuildLabelsReleaseActionsScope["lockedLabelVersionId"], optimizationInputs: BuildLabelsReleaseActionsScope["optimizationInputs"], releaseInputs: BuildLabelsReleaseActionsScope["releaseInputs"], setActionFeedback: BuildLabelsReleaseActionsScope["setActionFeedback"], setBackendReleaseDeployment: BuildLabelsReleaseActionsScope["setBackendReleaseDeployment"], setBackendReleaseDeploymentId: BuildLabelsReleaseActionsScope["setBackendReleaseDeploymentId"], setExperimentState: BuildLabelsReleaseActionsScope["setExperimentState"], setLabelPublishRequest: BuildLabelsReleaseActionsScope["setLabelPublishRequest"], setReleaseDecision: BuildLabelsReleaseActionsScope["setReleaseDecision"]) {
  const {
    labelPublishActionLabels,
    labelPublishPending,
    labelPublishBlocked,
    releaseBackendStatus,
    labelCandidatePublishDisabled,
    labelGrayPublishDisabled,
    labelPromotePublishDisabled,
    labelReleaseDisabledReason
  } = buildLabelsReleasePolicy(labelPublishRequest, labelEvalSucceeded);

  const waitForLabelPublishPoll = () => new Promise<void>((resolve) => {
      window.setTimeout(resolve, LABEL_PUBLISH_POLL_INTERVAL_MS);
    });

  const applyReleaseDeploymentReadback = (
      intent: LabelPublishIntent,
      deployment: BackendActionReceipt,
      traceId?: string
    ) => {
      setBackendReleaseDeployment(deployment);
      const backendStatus = normalizeBackendRunStatus(deployment.status);
      const common = {
        action: intent.action,
        actionLabel: intent.actionLabel,
        backendStatus,
        runId: deployment.id,
        traceId: traceId ?? deployment.trace_id
      };
      if (backendStatus === "blocked") {
        const reason = labelPublishReason(deployment);
        setReleaseDecision("blocked");
        setLabelPublishRequest({ ...common, status: "blocked", error: reason });
        setActionFeedback(`${intent.actionLabel}被后端 Bundle 门禁阻断：${reason}。未进入灰度。`);
        return "terminal" as const;
      }
      if (backendStatus === "rolled-back" || backendStatus === "rolled_back") {
        const reason = labelPublishReason(deployment);
        setReleaseDecision("rolled-back");
        setExperimentState("已回滚");
        setLabelPublishRequest({ ...common, status: "failed", error: reason });
        setActionFeedback(`在线保护已自动回滚 ${deployment.id}：${reason || "硬阈值触发"}。`);
        return "terminal" as const;
      }
      if (backendStatus === "completed" || backendStatus === "published") {
        setReleaseDecision("published");
        setExperimentState("完成");
        setLabelPublishRequest({ ...common, status: "success", backendStatus: "published" });
        setActionFeedback(`发布 Bundle ${deployment.id} 已完成（published）；只有此终态显示成功。`);
        return "terminal" as const;
      }
      if (["shadowing", "gray-releasing", "monitoring"].includes(backendStatus)) {
        setReleaseDecision(backendStatus);
        if (backendStatus === "gray-releasing" || backendStatus === "monitoring") setExperimentState("灰度中");
        setLabelPublishRequest({ ...common, status: "idle" });
        setActionFeedback(`发布 Bundle ${deployment.id} 当前为 ${backendStatus}；尚未发布，可执行下一人工动作或刷新监控。`);
        return "stable" as const;
      }
      if (backendRunFailed(backendStatus)) {
        const reason = labelPublishReason(deployment);
        setReleaseDecision("发布运行失败");
        setLabelPublishRequest({ ...common, status: "failed", error: reason });
        setActionFeedback(`${intent.actionLabel}失败：${reason}。`);
        return "terminal" as const;
      }
      setLabelPublishRequest({ ...common, status: "pending" });
      return "pending" as const;
    };

  const pollLabelPublishRun = async (intent: LabelPublishIntent, deploymentId: string, initialTraceId?: string) => {
      const generation = ++labelPublishPollGenerationRef.current;
      let lastTraceId = initialTraceId;
      for (let attempt = 1; attempt <= LABEL_PUBLISH_POLL_LIMIT; attempt += 1) {
        await waitForLabelPublishPoll();
        if (labelPublishPollGenerationRef.current !== generation) return;
        try {
          const response = await getReleaseDeployment(deploymentId, { correlationId: labelRootTraceId });
          lastTraceId = response.meta?.trace_id ?? response.data.trace_id ?? lastTraceId;
          const readbackState = applyReleaseDeploymentReadback(intent, response.data, lastTraceId);
          if (readbackState !== "pending") return;
        } catch (error) {
          const message = error instanceof Error ? error.message : "读取 ReleaseDeployment 失败";
          setLabelPublishRequest({
            status: "pending",
            action: intent.action,
            actionLabel: intent.actionLabel,
            backendStatus: "readback-pending",
            runId: deploymentId,
            traceId: lastTraceId,
            error: message
          });
        }
      }
      setActionFeedback(`ReleaseDeployment ${deploymentId} 仍未进入稳定终态；已停止自动轮询，可手动刷新。`);
    };

  const executeLabelPublishIntent = async (intent: LabelPublishIntent) => {
      if (labelPublishInFlightRef.current) return;
      labelPublishInFlightRef.current = true;
      lastLabelPublishIntentRef.current = intent;
      setLabelPublishRequest({
        status: "pending",
        action: intent.action,
        actionLabel: intent.actionLabel,
        backendStatus: "requesting"
      });
      setActionFeedback(`${intent.actionLabel}请求中（pending），候选版本仍未发布。`);
      try {
        let receipt;
        const existingDeploymentId = intent.deploymentId ?? backendReleaseDeploymentId;
        let resolvedDeploymentId = existingDeploymentId;
        if (intent.action === "gate" || intent.action === "candidate") {
          if (existingDeploymentId) {
            receipt = await getReleaseDeployment(existingDeploymentId, { correlationId: labelRootTraceId });
          } else {
            const deploymentId = `release_labeling_${Date.now().toString(36)}`;
            receipt = await createReleaseDeployment(
              {
                deployment_id: deploymentId,
                environment: "production",
                label_version_id: lockedLabelVersionId,
                prompt_version_id: backendPromptVersionId,
                model_version: optimizationInputs.modelVersion,
                aggregation_policy_version_id: optimizationInputs.aggregationPolicyVersion,
                eval_dataset_version_id: optimizationInputs.evalDatasetVersion,
                eval_run_id: labelEvalRun?.id,
                ...(releaseInputs.rollback.trim()
                  ? { rollback_target_deployment_id: releaseInputs.rollback.trim() }
                  : {})
              },
              { idempotencyKey: intent.idempotencyKey, correlationId: labelRootTraceId }
            );
            resolvedDeploymentId = deploymentId;
            setBackendReleaseDeploymentId(deploymentId);
            intent.deploymentId = deploymentId;
            lastLabelPublishIntentRef.current = intent;
          }
        } else {
          if (!existingDeploymentId) throw new Error("请先创建并通过发布 Bundle 门禁");
          const current = await getReleaseDeployment(existingDeploymentId, { correlationId: labelRootTraceId });
          const expectedStatus = normalizeBackendRunStatus(current.data.status);
          receipt = await transitionReleaseDeployment(
            existingDeploymentId,
            {
              action: intent.action === "gray" ? "approve-gray" : "promote",
              reason: releaseInputs.note.trim() || (intent.action === "gray" ? "人工批准 10% 灰度" : "稳定窗口完成后人工晋级"),
              expected_status: expectedStatus
            },
            { idempotencyKey: intent.idempotencyKey, correlationId: labelRootTraceId }
          );
        }
        resolvedDeploymentId = resolvedDeploymentId
          || (typeof receipt.data.raw.deployment_id === "string" ? receipt.data.raw.deployment_id : receipt.data.id);
        const traceId = receipt.data.trace_id ?? receipt.meta?.trace_id;
        const readbackState = applyReleaseDeploymentReadback(intent, receipt.data, traceId);
        if (readbackState === "pending") {
          void pollLabelPublishRun(intent, resolvedDeploymentId, traceId);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : "未知错误";
        setReleaseDecision("发布请求失败");
        setLabelPublishRequest({
          status: "failed",
          action: intent.action,
          actionLabel: intent.actionLabel,
          backendStatus: "failed",
          error: message
        });
        setActionFeedback(`${intent.actionLabel}失败（failed）：${message}。可重试同一用户动作。`);
      } finally {
        labelPublishInFlightRef.current = false;
      }
    };

  const startLabelPublish = (action: LabelPublishAction) => {
      if (labelPublishInFlightRef.current) return;
      if (!lockedLabelVersionId || !labelRootTraceId) {
        setLabelPublishRequest({
          status: "failed",
          action,
          actionLabel: labelPublishActionLabels[action],
          error: "缺少后端 LabelVersion 强 ID 或闭环 root trace"
        });
        setActionFeedback("发布失败（failed）：静态候选版本名不能进入 ReleaseDeployment Bundle。");
        return;
      }
      if (!backendPromptVersionId) {
        setLabelPublishRequest({
          status: "failed",
          action,
          actionLabel: labelPublishActionLabels[action],
          error: "请先完成优化运行并物化 Prompt 候选"
        });
        setActionFeedback("发布失败（failed）：缺少后端物化的 PromptVersionCandidate，尚未调用发布 API。");
        return;
      }
      if (!labelEvalSucceeded || !labelEvalRun?.id) {
        setLabelPublishRequest({
          status: "failed",
          action,
          actionLabel: labelPublishActionLabels[action],
          error: "请先等待与当前候选版本绑定的 EvalRun 真实成功"
        });
        setActionFeedback("发布失败（failed）：必须先由 GET 读回 EvalRun success/completed 终态；POST queued/running 不具备发布资格。");
        return;
      }
      if ((action === "gate" || action === "candidate") && !releaseInputs.rollback.trim()) {
        setLabelPublishRequest({
          status: "failed",
          action,
          actionLabel: labelPublishActionLabels[action],
          error: "生产发布 Bundle 必须锁定稳定回滚部署 ID"
        });
        setActionFeedback("发布未提交：请填写稳定的 rollback_target_deployment_id；禁止使用展示版本名代替部署事实。");
        return;
      }
      const trafficPercent = Number(releaseInputs.traffic);
      if (action === "gray" && trafficPercent !== 10) {
        setLabelPublishRequest({
          status: "failed",
          action,
          actionLabel: labelPublishActionLabels[action],
          error: "当前发布策略固定为人工批准 10% 灰度"
        });
        setActionFeedback("发布失败（failed）：L1→L2 阶段灰度比例固定为 10%，请修正配置。");
        return;
      }
      if (["gray", "execute"].includes(action) && !backendReleaseDeploymentId) {
        setLabelPublishRequest({
          status: "failed",
          action,
          actionLabel: labelPublishActionLabels[action],
          error: "请先提交发布 Bundle 门禁"
        });
        setActionFeedback("发布动作未提交：请先创建 ReleaseDeployment 并查看后端阻断项。");
        return;
      }
      const actionLabel = labelPublishActionLabels[action];
      const intent: LabelPublishIntent = {
        action,
        actionLabel,
        idempotencyKey: createUserIntentIdempotencyKey(`release_deployment_${lockedLabelVersionId}_${action}`),
        ...(backendReleaseDeploymentId ? { deploymentId: backendReleaseDeploymentId } : {})
      };
      void executeLabelPublishIntent(intent);
    };

  const retryLabelPublish = () => {
      const intent = lastLabelPublishIntentRef.current;
      if (!intent || labelPublishInFlightRef.current) return;
      void executeLabelPublishIntent(intent);
    };

  const refreshLabelPublish = () => {
      const intent = lastLabelPublishIntentRef.current;
      const runId = labelPublishRequest.runId;
      if (!intent || !runId || labelPublishInFlightRef.current) return;
      labelPublishInFlightRef.current = true;
      void pollLabelPublishRun(intent, runId, labelPublishRequest.traceId).finally(() => {
        labelPublishInFlightRef.current = false;
      });
    };

  const submitReleaseGate = () => startLabelPublish("gate");

  return {
    labelPublishActionLabels,
    labelPublishPending,
    labelPublishBlocked,
    releaseBackendStatus,
    labelCandidatePublishDisabled,
    labelGrayPublishDisabled,
    labelPromotePublishDisabled,
    labelReleaseDisabledReason,
    labelPublishReason,
    waitForLabelPublishPoll,
    applyReleaseDeploymentReadback,
    pollLabelPublishRun,
    executeLabelPublishIntent,
    startLabelPublish,
    retryLabelPublish,
    refreshLabelPublish,
    submitReleaseGate
  };
}

export type LabelsReleaseActions = ReturnType<typeof buildLabelsReleaseActions>;
