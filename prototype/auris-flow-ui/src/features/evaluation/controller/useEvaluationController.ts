import type { EvaluationModuleProps } from "../types";
import { useEvaluationState } from "./useEvaluationState";
import { buildEvaluationSelection } from "./buildEvaluationSelection";
import { useEvaluationFocusRecovery } from "./useEvaluationFocusRecovery";
import { buildEvaluationContextActions } from "./buildEvaluationContextActions";
import { buildHotwordPollingActions } from "./buildHotwordPollingActions";
import { useHotwordVersionRecovery } from "./useHotwordVersionRecovery";
import { buildEvaluationRunActions } from "./buildEvaluationRunActions";
import { buildEvaluationBadcaseActions } from "./buildEvaluationBadcaseActions";
import { buildHotwordGateModel } from "./buildHotwordGateModel";
import { buildHotwordReleaseActions } from "./buildHotwordReleaseActions";
import { buildEvaluationLabelPromptActions } from "./buildEvaluationLabelPromptActions";
import { buildEvaluationPrimaryRenders } from "./buildEvaluationPrimaryRenders";
import { buildEvaluationManualRenders } from "./buildEvaluationManualRenders";
import { buildEvaluationFinalRenders } from "./buildEvaluationFinalRenders";

export function useEvaluationController(props: EvaluationModuleProps) {
  const { currentUser: v0, focus: v1, navigateToTarget: v2, setActiveModule: v3, setActiveTab: v4 } = props;
  const state = useEvaluationState(props);
  const { currentView: v5, selectedCapabilityKey: v6, setSelectedCapabilityKey: v7, badcaseCapabilityFilter: v8, setBadcaseCapabilityFilter: v9, selectedBadcaseId: v10, setSelectedBadcaseId: v11, selectedDatasetId: v12, setSelectedDatasetId: v13, selectedManualId: v14, setSelectedManualId: v15, manualMode: v16, setManualMode: v17, selectedLabelingTask: v18, setSelectedLabelingTask: v19, selectedLabelingCaseId: v20, setSelectedLabelingCaseId: v21, promptStatus: v22, setPromptStatus: v23, appliedPromptSuggestions: v24, setAppliedPromptSuggestions: v25, selectedPromptSuggestionId: v26, setSelectedPromptSuggestionId: v27, candidatePromptDraft: v28, setCandidatePromptDraft: v29, modelVersion: v30, setModelVersion: v31, labelVersion: v32, setLabelVersion: v33, runScope: v34, setRunScope: v35, runReceipt: v36, setRunReceipt: v37, activeEvalRun: v38, setActiveEvalRun: v39, feedbackDraft: v40, setFeedbackDraft: v41, evaluationAction: v42, setEvaluationAction: v43, hotwordCandidateVersion: v44, setHotwordCandidateVersion: v45, hotwordBaselineVersion: v46, setHotwordBaselineVersion: v47, hotwordVersionLoading: v48, setHotwordVersionLoading: v49, hotwordBadcaseRecovery: v50, setHotwordBadcaseRecovery: v51, hotwordEvalRunId: v52, setHotwordEvalRunId: v53, hotwordEvalPassed: v54, setHotwordEvalPassed: v55, hotwordEvalResult: v56, setHotwordEvalResult: v57, hotwordPublished: v58, setHotwordPublished: v59, setHotwordPublishRecovery: v60, hotwordPollGenerationRef: v61, hotwordPollTimerRef: v62, hotwordPublishRetryRunRef: v63, manualReviews: v64, setManualReviews: v65, badcaseWorkflow: v66, setBadcaseWorkflow: v67, datasetDraft: v68, setDatasetDraft: v69, badcaseDrafts: v70, setBadcaseDrafts: v71, runRecords: v72, setRunRecords: v73, evaluationNotice: v74, setEvaluationNotice: v75 } = state;
  const selection = buildEvaluationSelection(v24, v8, v66, v0, v50, v64, v22, v10, v6, v12, v20, v18, v14, v26);
  const { selectedCapability: v76, selectedDataset: v77, selectedManualReview: v78, selectedLabelingMetric: v79, labelingCasesForTask: v80, selectedLabelingCase: v81, selectedPromptSuggestion: v82, promptExperiment: v83, selectedCompareRow: v84, canApproveHotwordVersion: v85, canPublishHotwordVersion: v86, selectedBadcaseRecord: v87, selectedBadcaseResolved: v88, selectedBadcaseWorkflow: v89, visibleBadcaseWorkflow: v90 } = selection;
  const focusRecovery = useEvaluationFocusRecovery(v5, v1, v6, v9, v67, v75, v51, v11, v7, v13, v21);
  const contextActions = buildEvaluationContextActions(v70, v5, v64, v2, v72, v89, v76, v84, v81, v73);
  const { selectedBadcaseDraft: v91, runRecordTuples: v92, openEvaluationCaseEvidence: v93, openEvaluationBadcaseEvidence: v94, openEvaluationAssetLineage: v95, gateRows: v96, tabNarrative: v97, narrative: v98, pushRunRecord: v99, shortTrace: v100 } = contextActions;
  const pollingActions = buildHotwordPollingActions(v61, v62, v63, v99, v75, v47, v45, v55, v57, v53, v60, v59, v100);
  const { numericHotwordMetrics: v101, captureHotwordEvalResult: v102, syncHotwordVersionState: v103, discoverHotwordCandidateVersion: v104, refreshHotwordCandidateVersion: v105, waitForHotwordPoll: v106, pollHotwordBuildRun: v107, pollHotwordEvalRun: v108, pollHotwordPublishRun: v109 } = pollingActions;
  const versionRecovery = useHotwordVersionRecovery(v102, v0, v5, v104, v61, v62, v107, v108, v43, v75, v49);
  const runActions = buildEvaluationRunActions(v38, v66, v68, v32, v30, v99, v34, v89, v76, v77, v81, v78, v39, v43, v75, v41, v65, v37, v11, v7, v100);
  const { requestEvaluationRun: v110, ensureEvaluationRun: v111, requestEvaluationFeedbackTask: v112, selectCapability: v113, runEvaluation: v114, decideManualReview: v115, saveDatasetDraft: v116, createFeedbackTask: v117 } = runActions;
  const badcaseActions = buildEvaluationBadcaseActions(v104, v42, v61, v107, v99, v105, v91, v88, v89, v71, v67, v43, v75, v100, v103);
  const { updateBadcaseDraft: v118, applyBadcaseStatusLocally: v119, moveBadcaseStatus: v120, addSelectedBadcaseToHotwordCandidate: v121 } = badcaseActions;
  const gateModel = buildHotwordGateModel(v56);
  const { hotwordMetricValue: v122, hotwordMetricPlaceholder: v123, hotwordMetricDisplay: v124, hotwordPercentDisplay: v125, hotwordCerWerDisplay: v126, hotwordGateResultLabel: v127, hotwordGateRow: v128, hotwordGateRows: v129 } = gateModel;
  const releaseActions = buildHotwordReleaseActions(v85, v86, v0, v104, v42, v44, v54, v52, v61, v63, v108, v109, v99, v105, v43, v75, v55, v57, v53, v60, v100, v103);
  const { runHotwordShadowEval: v130, approveHotwordCandidate: v131, publishHotwordCandidate: v132 } = releaseActions;
  const labelPromptActions = buildEvaluationLabelPromptActions(v24, v28, v111, v83, v99, v112, v110, v89, v81, v79, v3, v4, v25, v67, v29, v43, v75, v41, v23, v21, v19, v27, v100);
  const { selectLabelingTask: v133, handleLabelingAction: v134, generatePromptSuggestions: v135, applyPromptSuggestion: v136, createPromptCandidate: v137, runPromptShadowEval: v138, createPromptReleaseDraft: v139, jumpToLabelPromptWorkbench: v140 } = labelPromptActions;
  const primaryRenders = buildEvaluationPrimaryRenders(v24, v136, v28, v137, v139, v42, v40, v96, v135, v134, v140, v32, v80, v30, v98, v93, v83, v114, v138, v36, v92, v34, v113, v133, v76, v12, v81, v79, v82, v3, v4, v29, v41, v33, v31, v35, v13, v21, v15, v27);
  const { renderAutoView: v141, renderLabelingView: v142, renderPromptView: v143 } = primaryRenders;
  const manualRenders = buildEvaluationManualRenders(v0, v68, v115, v40, v16, v64, v2, v93, v116, v77, v78, v3, v69, v17, v13, v15);
  const { renderStandardManualView: v144, renderManualView: v145, renderSetsView: v146 } = manualRenders;
  const finalRenders = buildEvaluationFinalRenders(v121, v131, v8, v66, v85, v86, v117, v5, v42, v74, v50, v46, v44, v54, v56, v52, v129, v63, v58, v48, v120, v95, v94, v93, v132, v141, v142, v145, v143, v146, v114, v130, v113, v91, v88, v89, v84, v77, v3, v4, v9, v11, v7, v13, v118, v90);
  const { renderCompareView: v147, renderBadcaseView: v148, renderCurrentView: v149 } = finalRenders;
  return {
    ...props,
    ...state,
    ...selection,
    ...focusRecovery,
    ...contextActions,
    ...pollingActions,
    ...versionRecovery,
    ...runActions,
    ...badcaseActions,
    ...gateModel,
    ...releaseActions,
    ...labelPromptActions,
    ...primaryRenders,
    ...manualRenders,
    ...finalRenders
  };
}

export type EvaluationController = ReturnType<typeof useEvaluationController>;
