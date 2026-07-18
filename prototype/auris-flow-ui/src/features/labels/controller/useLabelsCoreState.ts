import type { LabelsModuleProps } from "../types";
import type { ClosedLoopReviewReceipt, HumanReviewDecisionBatchReceipt, LabelAggregate, LabelAggregationRun, LabelObservation, LabelTaxonomySuggestion } from "../../../api/client";
import type { OperationNotice } from "../../../shared/contracts/operations";
import type { LabelExtractionState, LabelFactReadState, LabelIntentKey, LabelReviewState, LabelScenarioKey } from "../types";
import { useState } from "react";

export function useLabelsCoreState(scope: LabelsModuleProps) {
  const [activeIntentKey, setActiveIntentKey] = useState<LabelIntentKey>("quote");

  const [sourceFilter, setSourceFilter] = useState("冲突样本");

  const [draftStatus, setDraftStatus] = useState<"草稿" | "已校准" | "待实验">("草稿");

  const [experimentState, setExperimentState] = useState<"未开始" | "影子评测中" | "灰度中" | "完成" | "已回滚">("影子评测中");

  const [selectedExperimentMetric, setSelectedExperimentMetric] = useState("人工接受率");

  const [actionFeedback, setActionFeedback] = useState("Agent 已生成候选标签草稿，只写入 v1.9.0-rc2 候选版本，不覆盖当前生效版本。");

  const [activeScenarioKey, setActiveScenarioKey] = useState<LabelScenarioKey>("salesQa");

  const [agentRunState, setAgentRunState] = useState<"idle" | "running" | "completed" | "failed">("idle");

  const [agentStepIndex, setAgentStepIndex] = useState(0);

  const [selectedReviewId, setSelectedReviewId] = useState("HR-1029");

  const [selectedConflictKey, setSelectedConflictKey] = useState("conflict-0");

  const [conflictDecision, setConflictDecision] = useState("待仲裁");

  const [draftInputs, setDraftInputs] = useState({
      tagName: "",
      definition: "",
      trigger: "",
      conflict: "",
      positive: "",
      negative: ""
    });

  const [reviewInputs, setReviewInputs] = useState({
      assignee: "质检运营",
      note: "核对证据句、单据字段和标签层级后再写回候选版本。"
    });

  const [conflictNote, setConflictNote] = useState("保留线上结果，候选版本只写入灰度观察队列。");

  const [releaseInputs, setReleaseInputs] = useState({
      note: "",
      traffic: "10",
      approver: "项目管理员",
      rollback: "",
      blockerReason: "",
      action: "灰度观察"
    });

  const [releaseChecks, setReleaseChecks] = useState<Record<string, boolean>>({
      "影响资产已确认": true,
      "Human Loop 已处理": false,
      "回滚路径已确认": true
    });

  const [selectedCandidateId, setSelectedCandidateId] = useState("LC-quote-01");

  const [selectedCandidateIds, setSelectedCandidateIds] = useState<string[]>([]);

  const [reviewStatesByCandidateId, setReviewStatesByCandidateId] = useState<Record<string, LabelReviewState>>({});

  const [reviewDraftStatesByCandidateId, setReviewDraftStatesByCandidateId] = useState<Record<string, LabelReviewState>>({});

  const [backendReviewTaskIdsByCandidateId, setBackendReviewTaskIdsByCandidateId] = useState<Record<string, string>>({});

  const [batchDecisionReceipt, setBatchDecisionReceipt] = useState<HumanReviewDecisionBatchReceipt | null>(null);

  const setReviewState = (
      next: LabelReviewState | ((current: LabelReviewState) => LabelReviewState),
      candidateId = selectedCandidateId
    ) => {
      setReviewStatesByCandidateId((current) => {
        const currentState = current[candidateId] ?? "待人工";
        const nextState = typeof next === "function" ? next(currentState) : next;
        return currentState === nextState ? current : { ...current, [candidateId]: nextState };
      });
    };

  const [extractionState, setExtractionState] = useState<LabelExtractionState>("idle");

  const [labelFactReadState, setLabelFactReadState] = useState<LabelFactReadState>("idle");

  const [labelFactReadError, setLabelFactReadError] = useState("");

  const [labelObservations, setLabelObservations] = useState<LabelObservation[]>([]);

  const [labelAggregates, setLabelAggregates] = useState<LabelAggregate[]>([]);

  const [labelAggregationBackendRun, setLabelAggregationBackendRun] = useState<LabelAggregationRun | null>(null);

  const [labelTaxonomySuggestions, setLabelTaxonomySuggestions] = useState<LabelTaxonomySuggestion[]>([]);

  const [closedLoopReviewProgress, setClosedLoopReviewProgress] = useState<Record<string, ClosedLoopReviewReceipt>>({});

  const [promptCandidateFact, setPromptCandidateFact] = useState<Record<string, unknown> | null>(null);

  const [promptReviewProgress, setPromptReviewProgress] = useState<ClosedLoopReviewReceipt | null>(null);

  const [labelEntityAction, setLabelEntityAction] = useState<string | null>(null);

  const [labelEntityNotice, setLabelEntityNotice] = useState<OperationNotice>({
      status: "idle",
      title: "等待标签实体操作",
      detail: "Agent、Human Loop、候选和规则动作只有在 BFF 返回并读回后才更新页面状态。"
    });

  return {
    activeIntentKey,
    setActiveIntentKey,
    sourceFilter,
    setSourceFilter,
    draftStatus,
    setDraftStatus,
    experimentState,
    setExperimentState,
    selectedExperimentMetric,
    setSelectedExperimentMetric,
    actionFeedback,
    setActionFeedback,
    activeScenarioKey,
    setActiveScenarioKey,
    agentRunState,
    setAgentRunState,
    agentStepIndex,
    setAgentStepIndex,
    selectedReviewId,
    setSelectedReviewId,
    selectedConflictKey,
    setSelectedConflictKey,
    conflictDecision,
    setConflictDecision,
    draftInputs,
    setDraftInputs,
    reviewInputs,
    setReviewInputs,
    conflictNote,
    setConflictNote,
    releaseInputs,
    setReleaseInputs,
    releaseChecks,
    setReleaseChecks,
    selectedCandidateId,
    setSelectedCandidateId,
    selectedCandidateIds,
    setSelectedCandidateIds,
    reviewStatesByCandidateId,
    setReviewStatesByCandidateId,
    reviewDraftStatesByCandidateId,
    setReviewDraftStatesByCandidateId,
    backendReviewTaskIdsByCandidateId,
    setBackendReviewTaskIdsByCandidateId,
    batchDecisionReceipt,
    setBatchDecisionReceipt,
    setReviewState,
    extractionState,
    setExtractionState,
    labelFactReadState,
    setLabelFactReadState,
    labelFactReadError,
    setLabelFactReadError,
    labelObservations,
    setLabelObservations,
    labelAggregates,
    setLabelAggregates,
    labelAggregationBackendRun,
    setLabelAggregationBackendRun,
    labelTaxonomySuggestions,
    setLabelTaxonomySuggestions,
    closedLoopReviewProgress,
    setClosedLoopReviewProgress,
    promptCandidateFact,
    setPromptCandidateFact,
    promptReviewProgress,
    setPromptReviewProgress,
    labelEntityAction,
    setLabelEntityAction,
    labelEntityNotice,
    setLabelEntityNotice
  };
}

export type LabelsCoreState = ReturnType<typeof useLabelsCoreState>;
