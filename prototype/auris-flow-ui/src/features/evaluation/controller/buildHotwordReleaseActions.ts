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
import { createHotwordEvalRun, patchHotwordPackVersion, publishHotwordPackVersion, retryBackendRun } from "../../../api/client";
import { hotwordVersionView } from "../../../shared/runtime/hotwordVersionViews";

type BuildHotwordReleaseActionsScope = EvaluationModuleProps & EvaluationState & EvaluationSelection & EvaluationFocusRecovery & EvaluationContextActions & HotwordPollingActions & HotwordVersionRecovery & EvaluationRunActions & EvaluationBadcaseActions & HotwordGateModel;

export function buildHotwordReleaseActions(canApproveHotwordVersion: BuildHotwordReleaseActionsScope["canApproveHotwordVersion"], canPublishHotwordVersion: BuildHotwordReleaseActionsScope["canPublishHotwordVersion"], currentUser: BuildHotwordReleaseActionsScope["currentUser"], discoverHotwordCandidateVersion: BuildHotwordReleaseActionsScope["discoverHotwordCandidateVersion"], evaluationAction: BuildHotwordReleaseActionsScope["evaluationAction"], hotwordCandidateVersion: BuildHotwordReleaseActionsScope["hotwordCandidateVersion"], hotwordEvalPassed: BuildHotwordReleaseActionsScope["hotwordEvalPassed"], hotwordEvalRunId: BuildHotwordReleaseActionsScope["hotwordEvalRunId"], hotwordPollGenerationRef: BuildHotwordReleaseActionsScope["hotwordPollGenerationRef"], hotwordPublishRetryRunRef: BuildHotwordReleaseActionsScope["hotwordPublishRetryRunRef"], pollHotwordEvalRun: BuildHotwordReleaseActionsScope["pollHotwordEvalRun"], pollHotwordPublishRun: BuildHotwordReleaseActionsScope["pollHotwordPublishRun"], pushRunRecord: BuildHotwordReleaseActionsScope["pushRunRecord"], refreshHotwordCandidateVersion: BuildHotwordReleaseActionsScope["refreshHotwordCandidateVersion"], setEvaluationAction: BuildHotwordReleaseActionsScope["setEvaluationAction"], setEvaluationNotice: BuildHotwordReleaseActionsScope["setEvaluationNotice"], setHotwordEvalPassed: BuildHotwordReleaseActionsScope["setHotwordEvalPassed"], setHotwordEvalResult: BuildHotwordReleaseActionsScope["setHotwordEvalResult"], setHotwordEvalRunId: BuildHotwordReleaseActionsScope["setHotwordEvalRunId"], setHotwordPublishRecovery: BuildHotwordReleaseActionsScope["setHotwordPublishRecovery"], shortTrace: BuildHotwordReleaseActionsScope["shortTrace"], syncHotwordVersionState: BuildHotwordReleaseActionsScope["syncHotwordVersionState"]) {
  const runHotwordShadowEval = async () => {
      if (evaluationAction) return;
      setEvaluationAction("hotword_eval");
      setHotwordEvalPassed(false);
      setHotwordEvalResult({ baselineMetrics: null, candidateMetrics: null, gatePassed: null, blockedReasons: [] });
      setEvaluationNotice({
        status: "pending",
        title: "热词影子评测创建中",
        detail: "正在从 API 恢复 ready_for_eval 候选和 Provider 编译产物。"
      });
      try {
        const candidate = await discoverHotwordCandidateVersion(false);
        if (!candidate) throw new Error("尚无候选版本，请先从已确认 Badcase 加入候选词");
        if (candidate.status !== "ready_for_eval") {
          throw new Error(`${candidate.id} 为 ${candidate.status}，尚未 ready_for_eval`);
        }
        if (!candidate.providerArtifactRef || candidate.compiledProvider !== "auris-audio-stack") {
          throw new Error(`${candidate.id} 缺少 auris-audio-stack 编译产物`);
        }
        const response = await createHotwordEvalRun(candidate.id, {
          eval_dataset_id: "evalset-asr-hotword-v1",
          provider: "auris-audio-stack",
          expected_resource_version: candidate.resourceVersion
        });
        const runId = response.data.id;
        const trace = response.meta?.trace_id ?? response.data.trace_id;
        setHotwordEvalRunId(runId);
        setEvaluationNotice({
          status: "pending",
          title: "影子评测运行已创建",
          detail: `${runId} · pending / gate 尚未生成 · Trace ${trace ?? "no-trace"}`
        });
        pushRunRecord("ASR 热词影子评测", `${runId} / pending / ${shortTrace(trace)}`, "运行中");
        const generation = hotwordPollGenerationRef.current + 1;
        hotwordPollGenerationRef.current = generation;
        await pollHotwordEvalRun(candidate.id, runId, generation, trace);
      } catch (error) {
        setHotwordEvalPassed(false);
        setEvaluationNotice({
          status: "error",
          title: "热词影子评测已阻断",
          detail: error instanceof Error ? error.message : "后端未返回可锁定 EvalRun。"
        });
      } finally {
        setEvaluationAction(null);
      }
    };

  const approveHotwordCandidate = async () => {
      if (!canApproveHotwordVersion) {
        setEvaluationNotice({
          status: "error",
          title: "模型负责人审批已阻断",
          detail: `当前身份 ${currentUser.name} 缺少 model_engineer 角色，不会伪造审批成功。`
        });
        return;
      }
      setEvaluationAction("hotword_approve");
      setEvaluationNotice({
        status: "pending",
        title: "模型负责人审批提交中",
        detail: "正在 GET 候选版本以获取实时 resource_version。"
      });
      try {
        if (!hotwordCandidateVersion) throw new Error("候选版本尚未从 API 恢复");
        const liveVersion = await refreshHotwordCandidateVersion(hotwordCandidateVersion.id);
        if (liveVersion.status !== "review_required" || !liveVersion.evalRunId || !liveVersion.evalLocked) {
          throw new Error(`${liveVersion.id} 当前为 ${liveVersion.status}，缺少成功且锁定的 EvalRun`);
        }
        const response = await patchHotwordPackVersion(liveVersion.id, {
          expected_resource_version: liveVersion.resourceVersion,
          status: "approved",
          eval_run_id: liveVersion.evalRunId
        });
        const approved = hotwordVersionView(response.data.raw) ?? await refreshHotwordCandidateVersion(liveVersion.id);
        syncHotwordVersionState(approved);
        if (approved.status !== "approved" || !approved.modelApprovedBy) {
          throw new Error("审批响应未返回 approved/model_approved_by");
        }
        setEvaluationNotice({
          status: "success",
          title: "模型负责人审批已完成",
          detail: `${approved.id} · approved / rv${approved.resourceVersion} · ${approved.modelApprovedBy} · Trace ${response.meta?.trace_id ?? response.data.trace_id ?? "no-trace"}`
        });
        pushRunRecord("ASR 热词模型负责人审批", `${approved.id} / rv${approved.resourceVersion} / ${shortTrace(response.meta?.trace_id ?? response.data.trace_id)}`);
      } catch (error) {
        setEvaluationNotice({
          status: "error",
          title: "模型负责人审批失败",
          detail: error instanceof Error ? error.message : "审批未落账，版本保持不变。"
        });
      } finally {
        setEvaluationAction(null);
      }
    };

  const publishHotwordCandidate = async () => {
      const retryRunId = hotwordPublishRetryRunRef.current;
      if (!canPublishHotwordVersion) {
        setEvaluationNotice({
          status: "error",
          title: "词包发布已阻断",
          detail: `${currentUser.name} 缺少 project_admin。`
        });
        return;
      }
      if (!hotwordCandidateVersion || !hotwordEvalRunId || !hotwordEvalPassed) {
        setEvaluationNotice({
          status: "error",
          title: "词包发布已阻断",
          detail: "缺少已批准候选或锁定 EvalRun。"
        });
        return;
      }
      setEvaluationAction("hotword_publish");
      setHotwordPublishRecovery({ status: "pending", runId: retryRunId ?? undefined, detail: retryRunId ? `重试 ${retryRunId}` : "创建发布运行" });
      setEvaluationNotice({
        status: "pending",
        title: retryRunId ? "正在重试发布 Run" : "正在创建发布 Run",
        detail: retryRunId ? `${retryRunId} · 不重复 /publish。` : `EvalRun ${hotwordEvalRunId}`
      });
      try {
        const liveVersion = await refreshHotwordCandidateVersion(hotwordCandidateVersion.id);
        if (liveVersion.status !== "approved" || !liveVersion.modelApprovedBy) {
          throw new Error(`${liveVersion.id} / ${liveVersion.status} / 无模型审批`);
        }
        if (liveVersion.modelApprovedBy === currentUser.userId) {
          throw new Error("审批人与发布人必须不同");
        }
        if (!liveVersion.evalRunId || !liveVersion.evalLocked) {
          throw new Error("缺少锁定 EvalRun");
        }
        const response = retryRunId
          ? await retryBackendRun(retryRunId, { reason: "项目管理员重试词包发布" })
          : await publishHotwordPackVersion(liveVersion.id, {
              expected_resource_version: liveVersion.resourceVersion,
              eval_run_id: liveVersion.evalRunId,
              confirmation: "publish"
            });
        const publishRunId = response.data.id;
        const trace = response.meta?.trace_id ?? response.data.trace_id;
        hotwordPublishRetryRunRef.current = publishRunId;
        setHotwordPublishRecovery({
          status: "pending",
          runId: publishRunId,
          traceId: trace,
          detail: retryRunId ? `${retryRunId} -> ${publishRunId}` : "已创建"
        });
        setEvaluationNotice({
          status: "pending",
          title: retryRunId ? "发布重试已创建" : "词包发布运行已创建",
          detail: `${publishRunId} · pending · Trace ${trace ?? "no-trace"}`
        });
        pushRunRecord(retryRunId ? "热词发布重试" : "热词发布", `${publishRunId} / pending / ${shortTrace(trace)}`, "运行中");
        const generation = hotwordPollGenerationRef.current + 1;
        hotwordPollGenerationRef.current = generation;
        await pollHotwordPublishRun(liveVersion.id, publishRunId, generation, trace);
      } catch (error) {
        const detail = error instanceof Error ? error.message : "未生成 TaskVersion。";
        setHotwordPublishRecovery({
          status: "failed",
          runId: hotwordPublishRetryRunRef.current ?? undefined,
          detail
        });
        setEvaluationNotice({
          status: "error",
          title: "词包人工发布失败",
          detail
        });
      } finally {
        setEvaluationAction(null);
      }
    };

  return {
    runHotwordShadowEval,
    approveHotwordCandidate,
    publishHotwordCandidate
  };
}

export type HotwordReleaseActions = ReturnType<typeof buildHotwordReleaseActions>;
