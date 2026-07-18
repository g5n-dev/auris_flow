import type { EvaluationModuleProps } from "../types";
import type { EvaluationState } from "./useEvaluationState";
import { evaluationCapabilityRows, evaluationDatasets, evaluationLabelingCases, evaluationLabelingMetrics, evaluationModelCompareRows, evaluationPromptExperiment, evaluationPromptSuggestions } from "../catalog";
import type { EvaluationBadcaseWorkflowItem, EvaluationPromptExperiment } from "../types";

type BuildEvaluationSelectionScope = EvaluationModuleProps & EvaluationState;

export function buildEvaluationSelection(appliedPromptSuggestions: BuildEvaluationSelectionScope["appliedPromptSuggestions"], badcaseCapabilityFilter: BuildEvaluationSelectionScope["badcaseCapabilityFilter"], badcaseWorkflow: BuildEvaluationSelectionScope["badcaseWorkflow"], currentUser: BuildEvaluationSelectionScope["currentUser"], hotwordBadcaseRecovery: BuildEvaluationSelectionScope["hotwordBadcaseRecovery"], manualReviews: BuildEvaluationSelectionScope["manualReviews"], promptStatus: BuildEvaluationSelectionScope["promptStatus"], selectedBadcaseId: BuildEvaluationSelectionScope["selectedBadcaseId"], selectedCapabilityKey: BuildEvaluationSelectionScope["selectedCapabilityKey"], selectedDatasetId: BuildEvaluationSelectionScope["selectedDatasetId"], selectedLabelingCaseId: BuildEvaluationSelectionScope["selectedLabelingCaseId"], selectedLabelingTask: BuildEvaluationSelectionScope["selectedLabelingTask"], selectedManualId: BuildEvaluationSelectionScope["selectedManualId"], selectedPromptSuggestionId: BuildEvaluationSelectionScope["selectedPromptSuggestionId"]) {
  const selectedCapability = evaluationCapabilityRows.find((row) => row.key === selectedCapabilityKey) ?? evaluationCapabilityRows[1];

  const selectedDataset = evaluationDatasets.find((dataset) => dataset.id === selectedDatasetId) ?? evaluationDatasets[0];

  const selectedManualReview = manualReviews.find((item) => item.id === selectedManualId) ?? manualReviews[0];

  const selectedLabelingMetric = evaluationLabelingMetrics.find((metric) => metric.taskKey === selectedLabelingTask) ?? evaluationLabelingMetrics[0];

  const labelingCasesForTask = evaluationLabelingCases.filter((item) => item.taskKey === selectedLabelingMetric.taskKey);

  const selectedLabelingCase = evaluationLabelingCases.find((item) => item.id === selectedLabelingCaseId) ?? labelingCasesForTask[0] ?? evaluationLabelingCases[0];

  const selectedPromptSuggestion = evaluationPromptSuggestions.find((item) => item.id === selectedPromptSuggestionId) ?? evaluationPromptSuggestions[0];

  const promptExperiment: EvaluationPromptExperiment = {
      ...evaluationPromptExperiment,
      labelTask: selectedLabelingMetric.task,
      status: promptStatus,
      candidateF1: Math.round((evaluationPromptExperiment.candidateF1 + appliedPromptSuggestions.length * 0.2) * 10) / 10,
      conflictRate: Math.max(1.8, Math.round((evaluationPromptExperiment.conflictRate - appliedPromptSuggestions.length * 0.25) * 10) / 10)
    };

  const selectedCompareRow = evaluationModelCompareRows.find((row) => row.key === selectedCapabilityKey) ?? evaluationModelCompareRows[1];

  const canApproveHotwordVersion = currentUser.roles.includes("model_engineer");

  const canPublishHotwordVersion = currentUser.roles.includes("project_admin");

  const selectedBadcaseRecord = badcaseWorkflow.find((item) => item.id === selectedBadcaseId);

  const selectedBadcaseResolved = Boolean(
      selectedBadcaseRecord &&
      (selectedBadcaseRecord.capability !== "asr-hotword" || hotwordBadcaseRecovery === "resolved")
    );

  const selectedBadcaseWorkflow: EvaluationBadcaseWorkflowItem = selectedBadcaseResolved && selectedBadcaseRecord ? selectedBadcaseRecord : {
      id: selectedBadcaseId,
      capability: selectedBadcaseRecord?.capability ?? (badcaseCapabilityFilter === "asr-hotword" ? "asr-hotword" : selectedCapabilityKey),
      title: "Badcase 未恢复",
      severity: "未知",
      status: "待归因",
      source: "后端对象未找到",
      rootCause: "无法从 /api/v1/badcases 恢复该对象。",
      fix: "请检查 deep link 对象 ID、项目范围或对象是否已归档。",
      target: "blocked",
      owner: "待确认"
    };

  const visibleBadcaseWorkflow = badcaseCapabilityFilter === "all"
      ? badcaseWorkflow
      : badcaseWorkflow.filter((item) => item.capability === badcaseCapabilityFilter);

  return {
    selectedCapability,
    selectedDataset,
    selectedManualReview,
    selectedLabelingMetric,
    labelingCasesForTask,
    selectedLabelingCase,
    selectedPromptSuggestion,
    promptExperiment,
    selectedCompareRow,
    canApproveHotwordVersion,
    canPublishHotwordVersion,
    selectedBadcaseRecord,
    selectedBadcaseResolved,
    selectedBadcaseWorkflow,
    visibleBadcaseWorkflow
  };
}

export type EvaluationSelection = ReturnType<typeof buildEvaluationSelection>;
