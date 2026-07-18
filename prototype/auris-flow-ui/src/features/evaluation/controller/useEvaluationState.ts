import type { EvaluationModuleProps } from "../types";
import type { BackendActionReceipt } from "../../../api/client";
import type { EvaluationCapabilityKey } from "../../../shared/contracts/evaluation";
import type { OperationNotice } from "../../../shared/contracts/operations";
import type { HotwordPackVersionView } from "../../../shared/runtime/hotwordVersionViews";
import { evaluationBadcaseWorkflowSeed, evaluationLabelingCases, evaluationManualReviewSeed, evaluationPromptSuggestions } from "../catalog";
import type { EvaluationBadcaseWorkflowItem, EvaluationManualReviewItem, EvaluationPromptExperiment, EvaluationRunRecord, EvaluationViewKey } from "../types";
import { useRef, useState } from "react";

export function useEvaluationState(scope: EvaluationModuleProps) {
  const { activeTab } = scope;
  const currentView = (["auto", "labeling", "prompt", "manual", "sets", "compare", "badcase"].includes(activeTab) ? activeTab : "auto") as EvaluationViewKey;

  const [selectedCapabilityKey, setSelectedCapabilityKey] = useState<EvaluationCapabilityKey>("boundary");

  const [badcaseCapabilityFilter, setBadcaseCapabilityFilter] = useState<"all" | EvaluationCapabilityKey>("all");

  const [selectedBadcaseId, setSelectedBadcaseId] = useState("B-2031");

  const [selectedDatasetId, setSelectedDatasetId] = useState("quote-risk");

  const [selectedManualId, setSelectedManualId] = useState(evaluationManualReviewSeed[0].id);

  const [manualMode, setManualMode] = useState<"review" | "calibration">("review");

  const [selectedLabelingTask, setSelectedLabelingTask] = useState("quote_amount");

  const [selectedLabelingCaseId, setSelectedLabelingCaseId] = useState(evaluationLabelingCases[0].id);

  const [promptStatus, setPromptStatus] = useState<EvaluationPromptExperiment["status"]>("待生成建议");

  const [appliedPromptSuggestions, setAppliedPromptSuggestions] = useState<string[]>([]);

  const [selectedPromptSuggestionId, setSelectedPromptSuggestionId] = useState(evaluationPromptSuggestions[0].id);

  const [candidatePromptDraft, setCandidatePromptDraft] = useState("候选 Prompt 尚未创建。");

  const [modelVersion, setModelVersion] = useState("prod-v5");

  const [labelVersion, setLabelVersion] = useState("v1.9.0-rc2");

  const [runScope, setRunScope] = useState("当前项目 / 北京");

  const [runReceipt, setRunReceipt] = useState("待运行：prod-v5");

  const [activeEvalRun, setActiveEvalRun] = useState<BackendActionReceipt | null>(null);

  const [feedbackDraft, setFeedbackDraft] = useState("尚未生成回流任务");

  const [evaluationAction, setEvaluationAction] = useState<string | null>(null);

  const [hotwordCandidateVersion, setHotwordCandidateVersion] = useState<HotwordPackVersionView | null>(null);

  const [hotwordBaselineVersion, setHotwordBaselineVersion] = useState<HotwordPackVersionView | null>(null);

  const [hotwordVersionLoading, setHotwordVersionLoading] = useState(false);

  const [hotwordBadcaseRecovery, setHotwordBadcaseRecovery] = useState<"idle" | "loading" | "resolved" | "missing" | "error">("idle");

  const [hotwordEvalRunId, setHotwordEvalRunId] = useState<string | null>(null);

  const [hotwordEvalPassed, setHotwordEvalPassed] = useState(false);

  const [hotwordEvalResult, setHotwordEvalResult] = useState<{
      baselineMetrics: Record<string, number> | null;
      candidateMetrics: Record<string, number> | null;
      gatePassed: boolean | null;
      blockedReasons: string[];
    }>({ baselineMetrics: null, candidateMetrics: null, gatePassed: null, blockedReasons: [] });

  const [hotwordPublished, setHotwordPublished] = useState(false);

  const [, setHotwordPublishRecovery] = useState<{
      status: "idle" | "pending" | "failed" | "success";
      runId?: string;
      traceId?: string;
      taskVersionId?: string;
      detail: string;
    }>({ status: "idle", detail: "等待发布" });

  const hotwordPollGenerationRef = useRef(0);

  const hotwordPollTimerRef = useRef<number | null>(null);

  const hotwordPublishRetryRunRef = useRef<string | null>(null);

  const [manualReviews, setManualReviews] = useState<EvaluationManualReviewItem[]>(evaluationManualReviewSeed);

  const [badcaseWorkflow, setBadcaseWorkflow] = useState<EvaluationBadcaseWorkflowItem[]>(evaluationBadcaseWorkflowSeed);

  const [datasetDraft, setDatasetDraft] = useState({
      targetSize: "1500",
      owner: "质检运营",
      layer: "能力 × 门店 × 风险",
      note: "优先补边界、金额冲突和串音。"
    });

  const [badcaseDrafts, setBadcaseDrafts] = useState<Record<string, { rootCause: string; fix: string; target: string; owner: string }>>({});

  const [runRecords, setRunRecords] = useState<EvaluationRunRecord[]>([
      { time: "12:44:31", title: "边界回归评测", detail: "EVS-boundary-v8 / prod-v5 / 观察", status: "待确认" },
      { time: "12:18:07", title: "标签候选评测", detail: "EVS-Tag-v1.9.0-rc2 / 93.4 / 可发布", status: "完成" },
      { time: "11:52:20", title: "串音抽样评测", detail: "740 样本 / 9 条进入人工队列", status: "完成" }
    ]);

  const [evaluationNotice, setEvaluationNotice] = useState<OperationNotice>({
      status: "idle",
      title: "等待评测动作",
      detail: "评测、决策和回流都会记录到当前链路。"
    });

  return {
    currentView,
    selectedCapabilityKey,
    setSelectedCapabilityKey,
    badcaseCapabilityFilter,
    setBadcaseCapabilityFilter,
    selectedBadcaseId,
    setSelectedBadcaseId,
    selectedDatasetId,
    setSelectedDatasetId,
    selectedManualId,
    setSelectedManualId,
    manualMode,
    setManualMode,
    selectedLabelingTask,
    setSelectedLabelingTask,
    selectedLabelingCaseId,
    setSelectedLabelingCaseId,
    promptStatus,
    setPromptStatus,
    appliedPromptSuggestions,
    setAppliedPromptSuggestions,
    selectedPromptSuggestionId,
    setSelectedPromptSuggestionId,
    candidatePromptDraft,
    setCandidatePromptDraft,
    modelVersion,
    setModelVersion,
    labelVersion,
    setLabelVersion,
    runScope,
    setRunScope,
    runReceipt,
    setRunReceipt,
    activeEvalRun,
    setActiveEvalRun,
    feedbackDraft,
    setFeedbackDraft,
    evaluationAction,
    setEvaluationAction,
    hotwordCandidateVersion,
    setHotwordCandidateVersion,
    hotwordBaselineVersion,
    setHotwordBaselineVersion,
    hotwordVersionLoading,
    setHotwordVersionLoading,
    hotwordBadcaseRecovery,
    setHotwordBadcaseRecovery,
    hotwordEvalRunId,
    setHotwordEvalRunId,
    hotwordEvalPassed,
    setHotwordEvalPassed,
    hotwordEvalResult,
    setHotwordEvalResult,
    hotwordPublished,
    setHotwordPublished,
    setHotwordPublishRecovery,
    hotwordPollGenerationRef,
    hotwordPollTimerRef,
    hotwordPublishRetryRunRef,
    manualReviews,
    setManualReviews,
    badcaseWorkflow,
    setBadcaseWorkflow,
    datasetDraft,
    setDatasetDraft,
    badcaseDrafts,
    setBadcaseDrafts,
    runRecords,
    setRunRecords,
    evaluationNotice,
    setEvaluationNotice
  };
}

export type EvaluationState = ReturnType<typeof useEvaluationState>;
