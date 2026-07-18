import type { LabelsModuleProps } from "../types";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { LabelsReleaseState } from "./useLabelsReleaseState";
import type { DeepLinkFocusMode } from "../../../shared/contracts/navigation";
import { layerLevelConfigs } from "../../../shared/fixtures/labelLayers";
import { LABEL_DEMO_MODE } from "../../../shared/runtime/demoMode";
import { displayLabelFactValue } from "../factViews";
import { labelAutomationLevels } from "../fixtures/governanceCatalog";
import { labelIntentFlows, labelScenarioPlaybooks } from "../fixtures/scenarioCatalog";
import type { LabelCandidate, LabelReviewState } from "../types";
import { useMemo } from "react";

type BuildLabelsCandidateModelScope = LabelsModuleProps & LabelsCoreState & LabelsReleaseState;

export function buildLabelsCandidateModel(activeIntentKey: BuildLabelsCandidateModelScope["activeIntentKey"], activeScenarioKey: BuildLabelsCandidateModelScope["activeScenarioKey"], automationLevel: BuildLabelsCandidateModelScope["automationLevel"], backendLabelVersionId: BuildLabelsCandidateModelScope["backendLabelVersionId"], backendPromptVersionId: BuildLabelsCandidateModelScope["backendPromptVersionId"], draftInputs: BuildLabelsCandidateModelScope["draftInputs"], labelAggregates: BuildLabelsCandidateModelScope["labelAggregates"], labelExtractionBackendRun: BuildLabelsCandidateModelScope["labelExtractionBackendRun"], labelObservations: BuildLabelsCandidateModelScope["labelObservations"], navigateToTarget: BuildLabelsCandidateModelScope["navigateToTarget"], optimizationInputs: BuildLabelsCandidateModelScope["optimizationInputs"], releaseChecks: BuildLabelsCandidateModelScope["releaseChecks"], releaseInputs: BuildLabelsCandidateModelScope["releaseInputs"], reviewDraftStatesByCandidateId: BuildLabelsCandidateModelScope["reviewDraftStatesByCandidateId"], reviewInputs: BuildLabelsCandidateModelScope["reviewInputs"], reviewStatesByCandidateId: BuildLabelsCandidateModelScope["reviewStatesByCandidateId"], selectedCandidateId: BuildLabelsCandidateModelScope["selectedCandidateId"], setBackendReviewTaskIdsByCandidateId: BuildLabelsCandidateModelScope["setBackendReviewTaskIdsByCandidateId"], setDraftInputs: BuildLabelsCandidateModelScope["setDraftInputs"], setReleaseInputs: BuildLabelsCandidateModelScope["setReleaseInputs"], setReviewDraftStatesByCandidateId: BuildLabelsCandidateModelScope["setReviewDraftStatesByCandidateId"], setReviewInputs: BuildLabelsCandidateModelScope["setReviewInputs"], setReviewState: BuildLabelsCandidateModelScope["setReviewState"]) {
  const lockedLabelVersionId = LABEL_DEMO_MODE
      ? (backendLabelVersionId || optimizationInputs.candidateTagVersion)
      : backendLabelVersionId;

  const lockedPromptVersionId = backendPromptVersionId;

  const activeIntent = labelIntentFlows.find((item) => item.key === activeIntentKey) ?? labelIntentFlows[0];

  const activeScenario = labelScenarioPlaybooks.find((scenario) => scenario.key === activeScenarioKey) ?? labelScenarioPlaybooks[0];

  const activeAutomation = labelAutomationLevels.find((level) => level.key === automationLevel) ?? labelAutomationLevels[2];

  const recommendedAutomation = labelAutomationLevels.find((level) => level.key === "L2") ?? labelAutomationLevels[2];

  const activeLayerCount = layerLevelConfigs.filter((level) => Boolean(activeIntent.layers[level.key])).length;

  const conflictCount = activeIntent.conflicts.length;

  const activeLayerEntries = layerLevelConfigs
      .map((level) => ({ level, match: activeIntent.layers[level.key] }))
      .filter((entry) => Boolean(entry.match));

  const primaryLayer = activeLayerEntries.find((entry) => entry.level.key === "intent") ?? activeLayerEntries[0];

  const draftLevel = primaryLayer?.level ?? layerLevelConfigs[1];

  const draftMatch = primaryLayer?.match;

  const draftTagName = draftMatch?.tag ?? activeIntent.intent;

  const editableDraftTagName = draftInputs.tagName.trim() || draftTagName;

  const updateDraftInput = (key: keyof typeof draftInputs, value: string) => setDraftInputs((current) => ({ ...current, [key]: value }));

  const updateReviewInput = (key: keyof typeof reviewInputs, value: string) => setReviewInputs((current) => ({ ...current, [key]: value }));

  const updateReleaseInput = (key: keyof typeof releaseInputs, value: string) => setReleaseInputs((current) => ({ ...current, [key]: value }));

  const releaseCheckItems = Object.keys(releaseChecks);

  const sourceOptions = ["ASR聚类", "冲突样本", "单据事件", "洞察异常", "人工标注"];

  const experimentRows = [
      ["人工接受率", "72.4%", "84.6%", "+12.2pp", "通过"],
      ["标签冲突率", "8.1%", "5.4%", "-2.7pp", activeIntent.risk === "高" ? "观察" : "通过"],
      ["误报样本", "31", activeIntent.risk === "高" ? "28" : "18", activeIntent.risk === "高" ? "-3" : "-13", activeIntent.risk === "高" ? "观察" : "通过"],
      ["资产影响", "4 下游", "12 下游", "+8", "需审批"],
      ["Human Review Rate", "18.6%", "12.1%", "-6.5pp", "通过"]
    ];

  const demoLabelCandidates = useMemo<LabelCandidate[]>(
      () => [
        {
          id: `LC-${activeIntent.key}-01`,
          title: editableDraftTagName,
          level: `L${draftLevel.level} ${draftLevel.category}`,
          value: activeIntent.risk === "高" ? "转人工复核" : "候选接受",
          source: "Agent建议",
          evidence: activeIntent.evidence,
          promptVersion: "prompt_quote_guard_v19_rc2",
          modelVersion: "tagger-llm-2026.06",
          confidence: activeIntent.confidence,
          conflict: activeIntent.conflicts[0]?.detail ?? "无强冲突，保留发布前抽检",
          humanState: reviewStatesByCandidateId[`LC-${activeIntent.key}-01`] ?? "待人工",
          assetImpact: "auris/label/event_tags · auris/eval/badcases",
          traceId: `tr-${activeIntent.key}-122718`,
          action: activeIntent.risk === "高" ? "送 Human Loop" : "候选版本"
        },
        {
          id: `LC-${activeIntent.key}-02`,
          title: activeIntent.layers.qa?.tag ?? "质检判定候选",
          level: "L6 质检标签",
          value: activeIntent.risk === "低" ? "可回填" : "发布阻断",
          source: "系统门禁",
          evidence: activeIntent.conflicts[0]?.detail ?? activeIntent.blockers[0] ?? activeIntent.evidence,
          promptVersion: "prompt_quality_gate_v12_candidate",
          modelVersion: "tagger-llm-2026.06",
          confidence: Math.max(58, activeIntent.confidence - 7),
          conflict: activeIntent.blockers[0] ?? "发布前抽检",
          humanState: reviewStatesByCandidateId[`LC-${activeIntent.key}-02`] ?? (activeIntent.risk === "高" ? "待人工" : "抽检"),
          assetImpact: "auris/quality/review_queue",
          traceId: `tr-${activeIntent.key}-quality`,
          action: "生成规则候选"
        },
        {
          id: `LC-${activeIntent.key}-03`,
          title: activeIntent.layers.agent?.tag ?? "Agent动作候选",
          level: "L4 标签值/动作",
          value: activeIntent.layers.agent?.state ?? "建议动作",
          source: "Agent建议",
          evidence: activeIntent.layers.agent?.evidence ?? "候选动作仅进入人审，不直接执行",
          promptVersion: "prompt_agent_action_v08_shadow",
          modelVersion: "action-planner-2026.06",
          confidence: Math.max(55, activeIntent.confidence - 12),
          conflict: "高风险动作需要人工确认后才可回填业务系统",
          humanState: reviewStatesByCandidateId[`LC-${activeIntent.key}-03`] ?? "待人工",
          assetImpact: "HumanReviewTask · downstream_webhook",
          traceId: `tr-${activeIntent.key}-action`,
          action: activeIntent.layers.agent?.tag ?? "转人工仲裁"
        }
      ],
      [
        activeIntent.blockers,
        activeIntent.confidence,
        activeIntent.conflicts,
        activeIntent.evidence,
        activeIntent.key,
        activeIntent.layers.agent,
        activeIntent.layers.qa,
        activeIntent.risk,
        draftLevel.category,
        draftLevel.level,
        editableDraftTagName,
        reviewStatesByCandidateId
      ]
    );

  const observationById = useMemo(
      () => new Map(labelObservations.map((observation) => [observation.observation_id, observation])),
      [labelObservations]
    );

  const backendLabelCandidates = useMemo<LabelCandidate[]>(() => {
      if (labelAggregates.length > 0) {
        return labelAggregates.map((aggregate) => {
          const firstMember = aggregate.members?.find((member) => member.included) ?? aggregate.members?.[0];
          const observation = firstMember ? observationById.get(firstMember.observation_id) : undefined;
          const humanState: LabelReviewState = aggregate.status === "accepted"
            ? "已接受"
            : aggregate.status === "rejected" || aggregate.status === "abstained"
              ? "已拒绝"
              : reviewStatesByCandidateId[aggregate.aggregate_id] ?? "待人工";
          return {
            id: aggregate.aggregate_id,
            title: aggregate.label_id,
            level: `聚合标签 · ${aggregate.value_type}`,
            value: displayLabelFactValue(aggregate.value),
            source: "聚合事实",
            evidence: aggregate.reason_codes.length > 0
              ? aggregate.reason_codes.join(" / ")
              : `${aggregate.members?.length ?? 0} 条 Observation 聚合`,
            promptVersion: observation?.prompt_version_id ?? "由成员 Observation 锁定",
            modelVersion: observation?.model_version ?? "多来源聚合",
            confidence: Math.round(aggregate.score * 1000) / 10,
            conflict: aggregate.decision === "require-review"
              ? `需人审：${aggregate.reason_codes.join(" / ") || "策略分流"}`
              : aggregate.decision === "abstain"
                ? "聚合弃权，不制造负事实"
                : "后端策略判定可接受",
            humanState,
            assetImpact: `${aggregate.aggregation_run_id} · ${aggregate.deterministic_hash.slice(0, 12)}`,
            traceId: aggregate.trace_id,
            action: aggregate.decision === "require-review" ? "送 Human Loop" : aggregate.decision === "auto-accept" ? "L2 候选事实" : "弃权"
          };
        });
      }
      return labelObservations.map((observation) => ({
        id: observation.observation_id,
        title: observation.label_id ?? observation.raw_label,
        level: `Observation · ${observation.value_type}`,
        value: displayLabelFactValue(observation.value),
        source: "模型观察",
        evidence: displayLabelFactValue(observation.evidence_ref),
        promptVersion: observation.prompt_version_id,
        modelVersion: observation.model_version,
        confidence: Math.round((observation.calibrated_confidence ?? observation.raw_confidence) * 1000) / 10,
        conflict: observation.label_id ? "等待确定性聚合" : "未知标签：等待 Taxonomy Suggestion",
        humanState: reviewStatesByCandidateId[observation.observation_id] ?? "待人工",
        assetImpact: `${observation.extraction_run_id} · ${observation.output_sha256.slice(0, 12)}`,
        traceId: observation.trace_id,
        action: "等待聚合"
      }));
    }, [labelAggregates, labelObservations, observationById, reviewStatesByCandidateId]);

  const labelCandidates = LABEL_DEMO_MODE ? demoLabelCandidates : backendLabelCandidates;

  const emptyCandidate: LabelCandidate = {
      id: "no-backend-label-fact",
      title: "等待后端标签事实",
      level: "尚无 Observation / Aggregate",
      value: "—",
      source: "聚合事实",
      evidence: "运行真实抽取后读取不可变 Observation 与确定性 Aggregate。",
      promptVersion: optimizationInputs.promptVersion,
      modelVersion: optimizationInputs.modelVersion,
      confidence: 0,
      conflict: "无后端事实时不生成本地候选",
      humanState: "待人工",
      assetImpact: "未物化",
      traceId: labelExtractionBackendRun?.trace_id ?? "等待 trace_id",
      action: "运行抽取"
    };

  const activeCandidate = labelCandidates.find((candidate) => candidate.id === selectedCandidateId) ?? labelCandidates[0] ?? emptyCandidate;

  const hasAuthoritativeCandidate = LABEL_DEMO_MODE || labelCandidates.length > 0;

  const authoritativeCandidateReviewState: LabelReviewState = ["已接受", "已修改", "已拒绝"].includes(activeCandidate.humanState)
      ? activeCandidate.humanState as LabelReviewState
      : "待人工";

  const reviewState = reviewStatesByCandidateId[activeCandidate.id] ?? authoritativeCandidateReviewState;

  const reviewDraftState = reviewDraftStatesByCandidateId[activeCandidate.id] ?? "待人工";

  const resetCandidateReview = (candidateId: string) => {
      setReviewState("待人工", candidateId);
      setReviewDraftStatesByCandidateId((current) => {
        if (!current[candidateId]) return current;
        const next = { ...current };
        delete next[candidateId];
        return next;
      });
      setBackendReviewTaskIdsByCandidateId((current) => {
        if (!current[candidateId]) return current;
        const next = { ...current };
        delete next[candidateId];
        return next;
      });
    };

  const openLabelEvidence = (title = activeIntent.intent, focusMode: DeepLinkFocusMode = "evidence") => {
      navigateToTarget({
        module: "listening",
        objectKind: "reviewSample",
        objectId: activeIntent.key === "crosstalk" ? "sample-af-129" : "sample-af-128",
        focusMode,
        title,
        detail: `${activeIntent.intent} / ${activeCandidate.id}`,
        origin: { label: "标签治理 / 证据关联", module: "labels", objectLabel: activeCandidate.id }
      });
    };

  return {
    lockedLabelVersionId,
    lockedPromptVersionId,
    activeIntent,
    activeScenario,
    activeAutomation,
    recommendedAutomation,
    activeLayerCount,
    conflictCount,
    activeLayerEntries,
    primaryLayer,
    draftLevel,
    draftMatch,
    draftTagName,
    editableDraftTagName,
    updateDraftInput,
    updateReviewInput,
    updateReleaseInput,
    releaseCheckItems,
    sourceOptions,
    experimentRows,
    demoLabelCandidates,
    observationById,
    backendLabelCandidates,
    labelCandidates,
    emptyCandidate,
    activeCandidate,
    hasAuthoritativeCandidate,
    authoritativeCandidateReviewState,
    reviewState,
    reviewDraftState,
    resetCandidateReview,
    openLabelEvidence
  };
}

export type LabelsCandidateModel = ReturnType<typeof buildLabelsCandidateModel>;
