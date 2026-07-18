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
import type { BackendActionReceipt } from "../../../api/client";
import { createLabelExtractionRun, createLabelOptimizationRun, getPromptVersionCandidate, retryBackendRun } from "../../../api/client";
import { backendRunFailed, backendRunStatusLabel, backendRunSucceeded, operationStatusFromBackendRun } from "../../../shared/runtime/backendRunStatus";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { labelAgentRunSteps } from "../fixtures/scenarioCatalog";

type BuildLabelsOptimizationActionsScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery & LabelsNavigationActions;

export function buildLabelsOptimizationActions(activeIntent: BuildLabelsOptimizationActionsScope["activeIntent"], activeScenario: BuildLabelsOptimizationActionsScope["activeScenario"], extractionSubjectKey: BuildLabelsOptimizationActionsScope["extractionSubjectKey"], labelAgentBackendRun: BuildLabelsOptimizationActionsScope["labelAgentBackendRun"], labelEntityAction: BuildLabelsOptimizationActionsScope["labelEntityAction"], labelExtractionBackendRun: BuildLabelsOptimizationActionsScope["labelExtractionBackendRun"], labelInputSha256: BuildLabelsOptimizationActionsScope["labelInputSha256"], labelRootTraceId: BuildLabelsOptimizationActionsScope["labelRootTraceId"], labelShortTrace: BuildLabelsOptimizationActionsScope["labelShortTrace"], lockedLabelVersionId: BuildLabelsOptimizationActionsScope["lockedLabelVersionId"], lockedPromptVersionId: BuildLabelsOptimizationActionsScope["lockedPromptVersionId"], optimizationInputs: BuildLabelsOptimizationActionsScope["optimizationInputs"], readLabelBackendRun: BuildLabelsOptimizationActionsScope["readLabelBackendRun"], readMaterializedLabelFacts: BuildLabelsOptimizationActionsScope["readMaterializedLabelFacts"], resetLabelEvalState: BuildLabelsOptimizationActionsScope["resetLabelEvalState"], setActionFeedback: BuildLabelsOptimizationActionsScope["setActionFeedback"], setAgentRunState: BuildLabelsOptimizationActionsScope["setAgentRunState"], setAgentStepIndex: BuildLabelsOptimizationActionsScope["setAgentStepIndex"], setBackendPromptCandidateId: BuildLabelsOptimizationActionsScope["setBackendPromptCandidateId"], setBackendPromptVersionId: BuildLabelsOptimizationActionsScope["setBackendPromptVersionId"], setBackendReleaseDeployment: BuildLabelsOptimizationActionsScope["setBackendReleaseDeployment"], setBackendReleaseDeploymentId: BuildLabelsOptimizationActionsScope["setBackendReleaseDeploymentId"], setDraftStatus: BuildLabelsOptimizationActionsScope["setDraftStatus"], setExperimentState: BuildLabelsOptimizationActionsScope["setExperimentState"], setExtractionState: BuildLabelsOptimizationActionsScope["setExtractionState"], setLabelAgentBackendRun: BuildLabelsOptimizationActionsScope["setLabelAgentBackendRun"], setLabelAggregates: BuildLabelsOptimizationActionsScope["setLabelAggregates"], setLabelAggregationBackendRun: BuildLabelsOptimizationActionsScope["setLabelAggregationBackendRun"], setLabelEntityAction: BuildLabelsOptimizationActionsScope["setLabelEntityAction"], setLabelEntityNotice: BuildLabelsOptimizationActionsScope["setLabelEntityNotice"], setLabelExtractionBackendRun: BuildLabelsOptimizationActionsScope["setLabelExtractionBackendRun"], setLabelPublishRequest: BuildLabelsOptimizationActionsScope["setLabelPublishRequest"], setLabelTaxonomySuggestions: BuildLabelsOptimizationActionsScope["setLabelTaxonomySuggestions"], setPromptCandidateFact: BuildLabelsOptimizationActionsScope["setPromptCandidateFact"], setPromptReviewProgress: BuildLabelsOptimizationActionsScope["setPromptReviewProgress"]) {
  const executeLabelOptimization = async (kind: "agent" | "extraction") => {
      if (labelEntityAction) return;
      if (
        !lockedLabelVersionId ||
        (!LABEL_DEMO_MODE && (!labelRootTraceId || !lockedPromptVersionId))
      ) {
        setLabelEntityNotice({
          status: "error",
          title: `${kind === "agent" ? "场景 Agent" : "标签抽取"}已阻断`,
          detail: "请先保存 LabelVersion 与绑定该版本的 PromptVersion 草稿，并使用后端返回的强 ID 与 root trace 启动后续运行。"
        });
        setActionFeedback("运行未提交：静态 candidateTagVersion 仅用于展示，不能作为真实运行锁定版本。");
        return;
      }
      const existing = kind === "agent" ? labelAgentBackendRun : labelExtractionBackendRun;
      const actionKey = kind === "agent" ? "agent-run" : "extraction-run";
      const actionLabel = kind === "agent" ? "场景 Agent" : "标签抽取";
      setLabelEntityAction(actionKey);
      setLabelEntityNotice({
        status: "pending",
        title: `${actionLabel}请求处理中`,
        detail: existing
          ? `${existing.id} 正在刷新或重试。`
          : kind === "agent"
            ? "正在创建锁定版本的 LabelOptimizationRun。"
            : "正在创建锁定版本的 LabelExtractionRun。"
      });
      if (kind === "agent") {
        setAgentRunState("running");
        setAgentStepIndex(0);
      } else {
        setExtractionState("running");
      }
      try {
        let receipt: BackendActionReceipt;
        if (existing && backendRunFailed(existing.status)) {
          receipt = (await retryBackendRun(existing.id, {
            reason: `${actionLabel}失败后由标签治理页面重试`,
            payload_overrides: { partition_key: optimizationInputs.partitionKey }
          }, { correlationId: labelRootTraceId })).data;
        } else if (existing && !backendRunSucceeded(existing.status)) {
          receipt = await readLabelBackendRun(kind, existing);
        } else {
          const runSuffix = `${Date.now().toString(36)}_${lockedLabelVersionId.replace(/[^A-Za-z0-9._:-]/g, "_")}`;
          receipt = kind === "agent"
            ? (await createLabelOptimizationRun({
                optimization_run_id: `label_opt_${runSuffix}`,
                label_version_id: lockedLabelVersionId,
                prompt_version_id: lockedPromptVersionId,
                model_version: optimizationInputs.modelVersion,
                aggregation_policy_version_id: optimizationInputs.aggregationPolicyVersion,
                eval_dataset_version_id: optimizationInputs.evalDatasetVersion,
                trigger_reason: {
                  kind: "manual",
                  reason_codes: ["UI_MANUAL_OPTIMIZATION"],
                  source_feedback_ids: []
                },
                budget: {
                  max_rounds: 3,
                  candidates_per_round: 3,
                  max_duration_minutes: 120,
                  max_cost_micros: 2_000_000,
                  min_macro_f1_gain_ppm: 20_000,
                  max_critical_recall_regression_ppm: 5_000
                },
                sample_set: optimizationInputs.sampleSet,
                partition_key: optimizationInputs.partitionKey,
                source: "labels_ui"
              }, { correlationId: labelRootTraceId })).data
            : (await createLabelExtractionRun({
                extraction_run_id: `label_extract_${runSuffix}`,
                label_version_id: lockedLabelVersionId,
                prompt_version_id: lockedPromptVersionId,
                model_version: optimizationInputs.modelVersion,
                schema_version: "label-output-v1",
                aggregation_policy_version_id: optimizationInputs.aggregationPolicyVersion,
                subject_scope: "conversation",
                subject_refs: [{
                  subject_key: extractionSubjectKey,
                  evidence_ref: `source:${activeScenario.key}:${activeIntent.key}`,
                  data_range: optimizationInputs.dataRange
                }],
                input_sha256: await labelInputSha256(),
                execution_mode: optimizationInputs.shadowOnly ? "shadow" : "production"
              }, { correlationId: labelRootTraceId })).data;
        }
        const readback = await readLabelBackendRun(kind, receipt);
        const operationStatus = operationStatusFromBackendRun(readback.status);
        if (kind === "agent") {
          setLabelAgentBackendRun(readback);
          const promptCandidateIds = Array.isArray(readback.raw.prompt_candidate_ids)
            ? readback.raw.prompt_candidate_ids.filter((item): item is string => typeof item === "string" && Boolean(item))
            : [];
          if (operationStatus === "success" && promptCandidateIds[0]) {
            resetLabelEvalState();
            setBackendReleaseDeploymentId("");
            setBackendReleaseDeployment(null);
            setLabelPublishRequest({ status: "idle" });
            setBackendPromptCandidateId(promptCandidateIds[0]);
            try {
              const promptCandidate = await getPromptVersionCandidate(promptCandidateIds[0], {
                correlationId: labelRootTraceId
              });
              setPromptCandidateFact(promptCandidate.data);
              const materializedPromptVersionId = String(promptCandidate.data.prompt_version_id ?? "");
              setBackendPromptVersionId(materializedPromptVersionId);
              setPromptReviewProgress(null);
            } catch {
              setPromptCandidateFact({ id: promptCandidateIds[0], status: "awaiting-review" });
              setBackendPromptVersionId("");
            }
          }
          setAgentRunState(operationStatus === "success" ? "completed" : operationStatus === "error" ? "failed" : "running");
          setAgentStepIndex(operationStatus === "success" ? labelAgentRunSteps.length - 1 : 0);
        } else {
          setLabelExtractionBackendRun(readback);
          setExtractionState(operationStatus === "success" ? "completed" : operationStatus === "error" ? "failed" : "running");
          if (operationStatus === "success") {
            try {
              setLabelAggregationBackendRun(null);
              setLabelAggregates([]);
              setLabelTaxonomySuggestions([]);
              const facts = await readMaterializedLabelFacts(readback, true);
              setActionFeedback(
                `${actionLabel}：${readback.id} 已物化 ${facts.observations.length} 条 Observation / ${facts.aggregates.length} 条 Aggregate · trace ${labelShortTrace(readback.trace_id)}。`
              );
            } catch {
              setActionFeedback(`${actionLabel}已完成，但 Observation/Aggregate 读回失败；可在候选区安全重试，不会重复创建抽取运行。`);
            }
          }
        }
        if (operationStatus === "success") {
          setDraftStatus("待实验");
          setExperimentState("影子评测中");
        }
        setLabelEntityNotice({
          status: operationStatus,
          title: operationStatus === "success" ? `${actionLabel}已完成` : operationStatus === "error" ? `${actionLabel}失败，可重试` : `${actionLabel}已提交`,
          detail: `${readback.id} · ${backendRunStatusLabel(readback.status)} · trace ${labelShortTrace(readback.trace_id)}。`
        });
        if (kind === "agent" || operationStatus !== "success") {
          setActionFeedback(`${actionLabel}：${readback.id} / ${backendRunStatusLabel(readback.status)} / trace ${labelShortTrace(readback.trace_id)}。页面不推断尚未返回的候选结果。`);
        }
      } catch (error) {
        if (kind === "agent") setAgentRunState("failed");
        else setExtractionState("failed");
        setLabelEntityNotice({ status: "error", title: `${actionLabel}请求失败，可重试`, detail: `${error instanceof Error ? error.message : "unknown error"}。未生成本地成功结果。` });
        setActionFeedback(`${actionLabel}请求失败：${error instanceof Error ? error.message : "unknown error"}`);
      } finally {
        setLabelEntityAction(null);
      }
    };

  return {
    executeLabelOptimization
  };
}

export type LabelsOptimizationActions = ReturnType<typeof buildLabelsOptimizationActions>;
