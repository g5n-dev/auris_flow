import type { LabelsModuleProps } from "../types";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { LabelsReleaseState } from "./useLabelsReleaseState";
import type { LabelsCandidateModel } from "./buildLabelsCandidateModel";
import type { LabelsFocusModel } from "./useLabelsFocus";
import type { LabelsChangeModel } from "./buildLabelsChangeModel";
import type { LabelsGovernanceModel } from "./buildLabelsGovernanceModel";
import type { LabelsConflictModel } from "./buildLabelsConflictModel";
import type { LabelsIntentRecovery } from "./useLabelsIntentRecovery";
import type { BackendActionReceipt, LabelAggregate, LabelAggregationRun } from "../../../api/client";
import { createLabelAggregationRun, createUserIntentIdempotencyKey, getLabelAggregationPolicy, getLabelAggregationRun, getLabelExtractionRun, getLabelOptimizationRun, listLabelAggregates, listLabelObservations, listLabelTaxonomySuggestions } from "../../../api/client";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { labelScenarioPlaybooks, labelScenarioSourceMap } from "../fixtures/scenarioCatalog";
import type { LabelScenarioKey } from "../types";

type BuildLabelsNavigationActionsScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState & LabelsCandidateModel & LabelsFocusModel & LabelsChangeModel & LabelsGovernanceModel & LabelsConflictModel & LabelsIntentRecovery;

export function buildLabelsNavigationActions(activeCandidate: BuildLabelsNavigationActionsScope["activeCandidate"], activeIntent: BuildLabelsNavigationActionsScope["activeIntent"], activeScenario: BuildLabelsNavigationActionsScope["activeScenario"], hasAuthoritativeCandidate: BuildLabelsNavigationActionsScope["hasAuthoritativeCandidate"], labelAggregationBackendRun: BuildLabelsNavigationActionsScope["labelAggregationBackendRun"], labelExtractionBackendRun: BuildLabelsNavigationActionsScope["labelExtractionBackendRun"], labelRootTraceId: BuildLabelsNavigationActionsScope["labelRootTraceId"], lockedLabelVersionId: BuildLabelsNavigationActionsScope["lockedLabelVersionId"], optimizationInputs: BuildLabelsNavigationActionsScope["optimizationInputs"], resetLabelEvalState: BuildLabelsNavigationActionsScope["resetLabelEvalState"], setActionFeedback: BuildLabelsNavigationActionsScope["setActionFeedback"], setActiveIntentKey: BuildLabelsNavigationActionsScope["setActiveIntentKey"], setActiveScenarioKey: BuildLabelsNavigationActionsScope["setActiveScenarioKey"], setAgentRunState: BuildLabelsNavigationActionsScope["setAgentRunState"], setAgentStepIndex: BuildLabelsNavigationActionsScope["setAgentStepIndex"], setBackendLabelVersionId: BuildLabelsNavigationActionsScope["setBackendLabelVersionId"], setBackendPromptCandidateId: BuildLabelsNavigationActionsScope["setBackendPromptCandidateId"], setBackendPromptVersionId: BuildLabelsNavigationActionsScope["setBackendPromptVersionId"], setBackendReleaseDeployment: BuildLabelsNavigationActionsScope["setBackendReleaseDeployment"], setBackendReleaseDeploymentId: BuildLabelsNavigationActionsScope["setBackendReleaseDeploymentId"], setBatchDecisionReceipt: BuildLabelsNavigationActionsScope["setBatchDecisionReceipt"], setClosedLoopReviewProgress: BuildLabelsNavigationActionsScope["setClosedLoopReviewProgress"], setConflictDecision: BuildLabelsNavigationActionsScope["setConflictDecision"], setDraftStatus: BuildLabelsNavigationActionsScope["setDraftStatus"], setLabelAgentBackendRun: BuildLabelsNavigationActionsScope["setLabelAgentBackendRun"], setLabelAggregates: BuildLabelsNavigationActionsScope["setLabelAggregates"], setLabelAggregationBackendRun: BuildLabelsNavigationActionsScope["setLabelAggregationBackendRun"], setLabelEntityAction: BuildLabelsNavigationActionsScope["setLabelEntityAction"], setLabelEntityNotice: BuildLabelsNavigationActionsScope["setLabelEntityNotice"], setLabelExtractionBackendRun: BuildLabelsNavigationActionsScope["setLabelExtractionBackendRun"], setLabelFactReadError: BuildLabelsNavigationActionsScope["setLabelFactReadError"], setLabelFactReadState: BuildLabelsNavigationActionsScope["setLabelFactReadState"], setLabelObservations: BuildLabelsNavigationActionsScope["setLabelObservations"], setLabelRootTraceId: BuildLabelsNavigationActionsScope["setLabelRootTraceId"], setLabelTaxonomySuggestions: BuildLabelsNavigationActionsScope["setLabelTaxonomySuggestions"], setPromptCandidateFact: BuildLabelsNavigationActionsScope["setPromptCandidateFact"], setPromptReviewProgress: BuildLabelsNavigationActionsScope["setPromptReviewProgress"], setSelectedCandidateId: BuildLabelsNavigationActionsScope["setSelectedCandidateId"], setSelectedCandidateIds: BuildLabelsNavigationActionsScope["setSelectedCandidateIds"], setSelectedConflictKey: BuildLabelsNavigationActionsScope["setSelectedConflictKey"], setSelectedReviewId: BuildLabelsNavigationActionsScope["setSelectedReviewId"], setSourceFilter: BuildLabelsNavigationActionsScope["setSourceFilter"]) {
  const handleIntentAction = (message: string) => {
      setActionFeedback(message);
    };

  const selectScenario = (scenarioKey: LabelScenarioKey) => {
      const scenario = labelScenarioPlaybooks.find((item) => item.key === scenarioKey) ?? labelScenarioPlaybooks[0];
      setActiveScenarioKey(scenario.key);
      setActiveIntentKey(scenario.primaryIntent);
      setSourceFilter(labelScenarioSourceMap[scenario.key]);
      setDraftStatus("草稿");
      setSelectedReviewId("HR-1029");
      setSelectedConflictKey("conflict-0");
      setConflictDecision("待仲裁");
      setAgentRunState("idle");
      setLabelAgentBackendRun(null);
      setLabelExtractionBackendRun(null);
      setLabelFactReadState("idle");
      setLabelFactReadError("");
      setLabelObservations([]);
      setLabelAggregates([]);
      setLabelAggregationBackendRun(null);
      setLabelTaxonomySuggestions([]);
      setClosedLoopReviewProgress({});
      setBackendLabelVersionId("");
      setLabelRootTraceId("");
      setBackendPromptVersionId("");
      setBackendPromptCandidateId("");
      setPromptCandidateFact(null);
      setPromptReviewProgress(null);
      setBackendReleaseDeploymentId("");
      setBackendReleaseDeployment(null);
      resetLabelEvalState();
      setLabelEntityAction(null);
      setLabelEntityNotice({ status: "idle", title: "场景已切换", detail: `${scenario.name} 等待新的后端实体操作。` });
      setAgentStepIndex(0);
      setActionFeedback(`已切换场景：${scenario.name}。候选标签将基于 ${scenario.source} 重新生成。`);
    };

  const labelShortTrace = (traceId?: string) => traceId ? traceId.slice(0, 12) : "no-trace";

  const readLabelBackendRun = async (kind: "agent" | "extraction", receipt: BackendActionReceipt) => {
      try {
        return kind === "agent"
          ? (await getLabelOptimizationRun(receipt.id, { correlationId: labelRootTraceId })).data
          : (await getLabelExtractionRun(receipt.id, { correlationId: labelRootTraceId })).data;
      } catch {
        return receipt;
      }
    };

  const extractionSubjectKey = LABEL_DEMO_MODE && hasAuthoritativeCandidate
      ? activeCandidate.id
      : `subject-${activeIntent.key}-01`;

  const readMaterializedLabelFacts = async (run: BackendActionReceipt, forceNewAggregation = false) => {
      setLabelFactReadState("loading");
      setLabelFactReadError("");
      try {
        if (!lockedLabelVersionId) {
          throw new Error("尚未保存 LabelVersion，不能读取或聚合未锁定版本的 Observation");
        }
        if (!labelRootTraceId) {
          throw new Error("LabelVersion 缺少 root trace，已阻断跨页事实链");
        }
        const observationResponse = await listLabelObservations({
          subjectScope: "conversation",
          subjectKey: extractionSubjectKey,
          labelVersionId: lockedLabelVersionId,
          limit: 200,
          correlationId: labelRootTraceId
        });
        const observations = observationResponse.data.items.filter(
          (observation) =>
            observation.extraction_run_id === run.id &&
            observation.label_version_id === lockedLabelVersionId
        );
        setLabelObservations(observations);
        if (observations.length === 0) {
          setLabelAggregationBackendRun(null);
          setLabelAggregates([]);
          setLabelTaxonomySuggestions([]);
          setLabelFactReadState("empty");
          return { observations, aggregates: [] as LabelAggregate[] };
        }

        let aggregationRun: LabelAggregationRun;
        const materializedAggregationRunId = typeof run.raw.aggregation_run_id === "string"
          ? run.raw.aggregation_run_id
          : "";
        if (materializedAggregationRunId) {
          aggregationRun = (
            await getLabelAggregationRun(materializedAggregationRunId, {
              correlationId: labelRootTraceId
            })
          ).data;
        } else if (
          !forceNewAggregation &&
          labelAggregationBackendRun &&
          labelAggregationBackendRun.label_version_id === lockedLabelVersionId
        ) {
          aggregationRun = (
            await getLabelAggregationRun(labelAggregationBackendRun.aggregation_run_id, {
              correlationId: labelRootTraceId
            })
          ).data;
        } else {
          const policyResponse = await getLabelAggregationPolicy(
            optimizationInputs.aggregationPolicyVersion,
            { correlationId: labelRootTraceId }
          );
          const policyLabelVersionId = String(policyResponse.data.label_version_id ?? "");
          if (policyLabelVersionId !== lockedLabelVersionId) {
            throw new Error(
              `聚合策略 ${optimizationInputs.aggregationPolicyVersion} 锁定 ${policyLabelVersionId || "unknown"}，与当前 LabelVersion ${lockedLabelVersionId} 不一致`
            );
          }
          const policyMode = policyResponse.data.mode === "l2" ? "l2" : "l1";
          const aggregationRunId = `label_agg_${run.id.replace(/[^A-Za-z0-9._:-]/g, "_")}_${Date.now().toString(36)}`;
          aggregationRun = (
            await createLabelAggregationRun(
              {
                aggregation_run_id: aggregationRunId,
                label_version_id: lockedLabelVersionId,
                policy_version_id: optimizationInputs.aggregationPolicyVersion,
                observation_ids: observations.map((observation) => observation.observation_id),
                mode: policyMode
              },
              {
                idempotencyKey: createUserIntentIdempotencyKey(`label_aggregation_${run.id}`),
                correlationId: labelRootTraceId
              }
            )
          ).data;
        }
        if (
          aggregationRun.label_version_id !== lockedLabelVersionId ||
          aggregationRun.policy_version_id !== optimizationInputs.aggregationPolicyVersion
        ) {
          throw new Error(`AggregationRun ${aggregationRun.aggregation_run_id} 的锁定版本与当前事实链不一致`);
        }
        const extractionAggregateIds = Array.isArray(run.raw.aggregate_ids)
          ? run.raw.aggregate_ids.filter((item): item is string => typeof item === "string")
          : [];
        if (
          extractionAggregateIds.length > 0 &&
          [...extractionAggregateIds].sort().join("|") !== [...aggregationRun.aggregate_ids].sort().join("|")
        ) {
          throw new Error(`ExtractionRun ${run.id} 与 AggregationRun ${aggregationRun.aggregation_run_id} 的 aggregate_ids 不一致`);
        }
        setLabelAggregationBackendRun(aggregationRun);
        const [aggregateResponse, taxonomyResponse] = await Promise.all([
          listLabelAggregates({ limit: 200, correlationId: labelRootTraceId }),
          listLabelTaxonomySuggestions({ limit: 200, correlationId: labelRootTraceId })
        ]);
        const aggregateIds = new Set(aggregationRun.aggregate_ids);
        const aggregates = aggregateResponse.data.items.filter(
          (aggregate) =>
            aggregate.aggregation_run_id === aggregationRun.aggregation_run_id &&
            aggregateIds.has(aggregate.aggregate_id)
        );
        const suggestionIds = new Set(aggregationRun.taxonomy_suggestion_ids);
        const taxonomySuggestions = taxonomyResponse.data.items.filter(
          (suggestion) => suggestionIds.has(suggestion.suggestion_id)
        );
        setLabelAggregates(aggregates);
        setLabelTaxonomySuggestions(taxonomySuggestions);
        const firstCandidateId = aggregates[0]?.aggregate_id ?? observations[0]?.observation_id;
        if (firstCandidateId) setSelectedCandidateId(firstCandidateId);
        setSelectedCandidateIds([]);
        setBatchDecisionReceipt(null);
        if (aggregates[0]?.review_task_id) setSelectedReviewId(aggregates[0].review_task_id);
        setLabelFactReadState(aggregates.length > 0 || taxonomySuggestions.length > 0 ? "ready" : "empty");
        return { observations, aggregates };
      } catch (error) {
        const message = error instanceof Error ? error.message : "读取标签事实失败";
        setLabelFactReadError(message);
        setLabelFactReadState("failed");
        throw error;
      }
    };

  const retryMaterializedLabelFacts = () => {
      if (!labelExtractionBackendRun) return;
      void readMaterializedLabelFacts(labelExtractionBackendRun).catch(() => undefined);
    };

  const labelInputSha256 = async () => {
      if (!globalThis.crypto?.subtle) {
        throw new Error("当前浏览器不支持 Web Crypto，无法生成抽取输入的 SHA-256 锁定哈希");
      }
      const canonicalInput = JSON.stringify({
        scenario: activeScenario.key,
        subject_key: extractionSubjectKey,
        data_range: optimizationInputs.dataRange,
        sample_set: optimizationInputs.sampleSet
      });
      const digest = await globalThis.crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(canonicalInput)
      );
      return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
    };

  return {
    handleIntentAction,
    selectScenario,
    labelShortTrace,
    readLabelBackendRun,
    extractionSubjectKey,
    readMaterializedLabelFacts,
    retryMaterializedLabelFacts,
    labelInputSha256
  };
}

export type LabelsNavigationActions = ReturnType<typeof buildLabelsNavigationActions>;
