import type { LabelsModuleProps } from "../types";
import { useLabelsCoreState } from "./useLabelsCoreState";
import { useLabelsReleaseState } from "./useLabelsReleaseState";
import { buildLabelsCandidateModel } from "./buildLabelsCandidateModel";
import { useLabelsFocus } from "./useLabelsFocus";
import { buildLabelsChangeModel } from "./buildLabelsChangeModel";
import { buildLabelsGovernanceModel } from "./buildLabelsGovernanceModel";
import { buildLabelsConflictModel } from "./buildLabelsConflictModel";
import { useLabelsIntentRecovery } from "./useLabelsIntentRecovery";
import { buildLabelsNavigationActions } from "./buildLabelsNavigationActions";
import { buildLabelsOptimizationActions } from "./buildLabelsOptimizationActions";
import { buildLabelsReviewActions } from "./buildLabelsReviewActions";
import { buildLabelsPersistenceActions } from "./buildLabelsPersistenceActions";
import { buildLabelsPromptActions } from "./buildLabelsPromptActions";
import { buildLabelsEvaluationActions } from "./buildLabelsEvaluationActions";
import { buildLabelsReleaseActions } from "./buildLabelsReleaseActions";
import { buildLabelsCoreRenders } from "./buildLabelsCoreRenders";
import { buildLabelsInputRenders } from "./buildLabelsInputRenders";
import { buildLabelsDecisionRenders } from "./buildLabelsDecisionRenders";
import { buildLabelsWorkbenchRenders } from "./buildLabelsWorkbenchRenders";
import { buildLabelsContractRenders } from "./buildLabelsContractRenders";
import { buildLabelsRunRailModel } from "./buildLabelsRunRailModel";
import { buildLabelsShellRenders } from "./buildLabelsShellRenders";
import { buildLabelsPrimaryViews } from "./buildLabelsPrimaryViews";
import { buildLabelsReviewReleaseViews } from "./buildLabelsReviewReleaseViews";

export function useLabelsController(props: LabelsModuleProps) {
  const { focus: v0, navigateToTarget: v1, setActiveModule: v2 } = props;
  const coreState = useLabelsCoreState(props);
  const { activeIntentKey: v3, setActiveIntentKey: v4, sourceFilter: v5, setSourceFilter: v6, draftStatus: v7, setDraftStatus: v8, experimentState: v9, setExperimentState: v10, selectedExperimentMetric: v11, setSelectedExperimentMetric: v12, actionFeedback: v13, setActionFeedback: v14, activeScenarioKey: v15, setActiveScenarioKey: v16, agentRunState: v17, setAgentRunState: v18, agentStepIndex: v19, setAgentStepIndex: v20, selectedReviewId: v21, setSelectedReviewId: v22, selectedConflictKey: v23, setSelectedConflictKey: v24, conflictDecision: v25, setConflictDecision: v26, draftInputs: v27, setDraftInputs: v28, reviewInputs: v29, setReviewInputs: v30, conflictNote: v31, setConflictNote: v32, releaseInputs: v33, setReleaseInputs: v34, releaseChecks: v35, setReleaseChecks: v36, selectedCandidateId: v37, setSelectedCandidateId: v38, selectedCandidateIds: v39, setSelectedCandidateIds: v40, reviewStatesByCandidateId: v41, setReviewStatesByCandidateId: v42, reviewDraftStatesByCandidateId: v43, setReviewDraftStatesByCandidateId: v44, backendReviewTaskIdsByCandidateId: v45, setBackendReviewTaskIdsByCandidateId: v46, batchDecisionReceipt: v47, setBatchDecisionReceipt: v48, setReviewState: v49, extractionState: v50, setExtractionState: v51, labelFactReadState: v52, setLabelFactReadState: v53, labelFactReadError: v54, setLabelFactReadError: v55, labelObservations: v56, setLabelObservations: v57, labelAggregates: v58, setLabelAggregates: v59, labelAggregationBackendRun: v60, setLabelAggregationBackendRun: v61, labelTaxonomySuggestions: v62, setLabelTaxonomySuggestions: v63, closedLoopReviewProgress: v64, setClosedLoopReviewProgress: v65, promptCandidateFact: v66, setPromptCandidateFact: v67, promptReviewProgress: v68, setPromptReviewProgress: v69, labelEntityAction: v70, setLabelEntityAction: v71, labelEntityNotice: v72, setLabelEntityNotice: v73 } = coreState;
  const releaseState = useLabelsReleaseState();
  const { labelAgentBackendRun: v74, setLabelAgentBackendRun: v75, labelExtractionBackendRun: v76, setLabelExtractionBackendRun: v77, promptVariant: v78, setPromptVariant: v79, selectedPromptField: v80, setSelectedPromptField: v81, promptInputs: v82, setPromptInputs: v83, releaseDecision: v84, setReleaseDecision: v85, automationLevel: v86, setAutomationLevel: v87, dagsterDraftState: v88, setDagsterDraftState: v89, backendLabelVersionId: v90, setBackendLabelVersionId: v91, labelRootTraceId: v92, setLabelRootTraceId: v93, backendPromptCandidateId: v94, setBackendPromptCandidateId: v95, backendPromptVersionId: v96, setBackendPromptVersionId: v97, backendLabelBadcaseIds: v98, setBackendLabelBadcaseIds: v99, backendReleaseDeploymentId: v100, setBackendReleaseDeploymentId: v101, backendReleaseDeployment: v102, setBackendReleaseDeployment: v103, labelEvaluationLock: v104, setLabelEvaluationLock: v105, labelEvalRun: v106, setLabelEvalRun: v107, labelEvalRequest: v108, setLabelEvalRequest: v109, labelEvalPreflightRef: v110, labelEvalInFlightRef: v111, labelEvalPollGenerationRef: v112, lastLabelEvalIntentRef: v113, labelPublishRequest: v114, setLabelPublishRequest: v115, labelPublishInFlightRef: v116, labelPublishPollGenerationRef: v117, lastLabelPublishIntentRef: v118, resetLabelEvalState: v119, labelBadcaseActionHint: v120, selectedChangeSource: v121, setSelectedChangeSource: v122, humanChangeDraft: v123, setHumanChangeDraft: v124, optimizationInputs: v125, setOptimizationInputs: v126 } = releaseState;
  const candidateModel = buildLabelsCandidateModel(v3, v15, v86, v90, v96, v27, v58, v76, v56, v1, v125, v35, v33, v43, v29, v41, v37, v46, v28, v34, v44, v30, v49);
  const { lockedLabelVersionId: v127, lockedPromptVersionId: v128, activeIntent: v129, activeScenario: v130, activeAutomation: v131, recommendedAutomation: v132, activeLayerCount: v133, conflictCount: v134, activeLayerEntries: v135, primaryLayer: v136, draftLevel: v137, draftMatch: v138, draftTagName: v139, editableDraftTagName: v140, updateDraftInput: v141, updateReviewInput: v142, updateReleaseInput: v143, releaseCheckItems: v144, sourceOptions: v145, experimentRows: v146, demoLabelCandidates: v147, observationById: v148, backendLabelCandidates: v149, labelCandidates: v150, emptyCandidate: v151, activeCandidate: v152, hasAuthoritativeCandidate: v153, authoritativeCandidateReviewState: v154, reviewState: v155, reviewDraftState: v156, resetCandidateReview: v157, openLabelEvidence: v158 } = candidateModel;
  const focusModel = useLabelsFocus(v0, v14, v4, v38, v22);
  const changeModel = buildLabelsChangeModel(v152, v129, v130, v86, v123, v106, v92, v125, v29, v121, v11);
  const { agentImprovementRows: v159, humanChangeRows: v160, changeSetRows: v161, visibleChangeSetRows: v162, evaluationMetricRows: v163, evaluationMetrics: v164, effectAttributionRows: v165, dagsterDraftRows: v166, emptyEvaluationMetric: v167, selectedEvaluationMetric: v168, selectedMetricRow: v169 } = changeModel;
  const governanceModel = buildLabelsGovernanceModel(v152, v129, v130, v86, v102, v161, v166, v88, v140, v164, v74, v58, v92, v125, v35, v84, v33, v155, v39, v168, v21);
  const { promptFieldRows: v170, reviewTaskCount: v171, draftReleaseGateChecks: v172, backendReleaseStatus: v173, backendBlockedReasons: v174, backendReleaseGateChecks: v175, releaseGateChecks: v176, releaseGateRows: v177, gateFactPending: v178, gateIsBlocked: v179, labelOptimizationRun: v180, reviewTasks: v181, emptyReviewTask: v182, activeReviewTask: v183, hasBoundReviewTask: v184, selectedBatchAggregates: v185, selectedBatchCohorts: v186, batchPreflightReason: v187, batchPreflightPassed: v188, reviewDecisionActions: v189 } = governanceModel;
  const conflictModel = buildLabelsConflictModel(v129, v1, v23);
  const { conflictCases: v190, activeConflict: v191, conflictImpactRows: v192, openLabelAsset: v193, openLabelIntentDetail: v194 } = conflictModel;
  const intentRecovery = useLabelsIntentRecovery(v129, v3, v130, v138, v87, v48, v32, v89, v28, v51, v59, v55, v53, v57, v126, v83, v79, v36, v85, v34, v30, v38, v40, v81);
  const navigationActions = buildLabelsNavigationActions(v152, v129, v130, v153, v60, v76, v92, v127, v125, v119, v14, v4, v16, v18, v20, v91, v95, v97, v103, v101, v48, v65, v26, v8, v75, v59, v61, v71, v73, v77, v55, v53, v57, v93, v63, v67, v69, v38, v40, v24, v22, v6);
  const { handleIntentAction: v195, selectScenario: v196, labelShortTrace: v197, readLabelBackendRun: v198, extractionSubjectKey: v199, readMaterializedLabelFacts: v200, retryMaterializedLabelFacts: v201, labelInputSha256: v202 } = navigationActions;
  const optimizationActions = buildLabelsOptimizationActions(v129, v130, v199, v74, v70, v76, v202, v92, v197, v127, v128, v125, v198, v200, v119, v14, v18, v20, v95, v97, v103, v101, v8, v10, v51, v75, v59, v61, v71, v73, v77, v115, v63, v67, v69);
  const { executeLabelOptimization: v203 } = optimizationActions;
  const reviewActions = buildLabelsReviewActions(v152, v183, v45, v64, v203, v123, v58, v70, v92, v197, v127, v157, v189, v156, v29, v155, v181, v14, v46, v48, v65, v26, v8, v10, v71, v73, v36, v44, v49, v38, v40, v24, v22);
  const { runScenarioAgent: v204, ensureLabelHumanReviewTask: v205, applyReviewDecision: v206, selectLabelCandidate: v207, toggleLabelCandidateSelection: v208, selectLabelReviewTask: v209, moveLabelReviewSelection: v210, selectReviewDraft: v211, saveReviewAndNext: v212, handleLabelReviewKeyDown: v213 } = reviewActions;
  const persistenceActions = buildLabelsPersistenceActions(v152, v191, v129, v183, v206, v188, v31, v27, v140, v205, v203, v153, v184, v123, v74, v70, v117, v92, v197, v118, v127, v128, v125, v157, v119, v29, v155, v185, v14, v18, v99, v91, v95, v97, v103, v101, v48, v65, v26, v8, v10, v75, v59, v61, v71, v73, v77, v53, v57, v115, v93, v63, v67, v69, v36, v44, v49, v42, v40, v22);
  const { applyConflictDecision: v214, persistLabelDraft: v215, saveDraftRule: v216, createLabelDraft: v217, modifyLabelRule: v218, sendLabelHumanLoop: v219, saveCandidateVersion: v220, runExtractionTask: v221, applyCandidateBatchDecision: v222, applyCandidateAction: v223 } = persistenceActions;
  const promptActions = buildLabelsPromptActions(v129, v206, v98, v94, v64, v123, v74, v70, v92, v197, v127, v125, v170, v82, v68, v119, v29, v14, v87, v95, v97, v103, v101, v65, v89, v8, v10, v75, v71, v73, v115, v126, v67, v83, v69, v79, v38, v81);
  const { updatePromptInput: v224, updateOptimizationInput: v225, toggleOptimizationInput: v226, generateOptimizationRunDraft: v227, validateDagsterDraft: v228, materializeDagsterResult: v229, selectAutomationLevel: v230, applyAgentImprovement: v231, applyHumanChangeDraft: v232, applyPromptSuggestion: v233, createPromptCandidateVersion: v234, refreshPromptCandidateFact: v235, reviewPromptCandidate: v236, reviewTaxonomySuggestion: v237 } = promptActions;
  const evaluationActions = buildLabelsEvaluationActions(v94, v96, v74, v111, v112, v110, v108, v106, v104, v92, v197, v113, v127, v128, v125, v66, v14, v10, v109, v107, v105, v12);
  const { waitForLabelEvalPoll: v238, applyLabelEvalReadback: v239, pollLabelEvalRun: v240, executeLabelEvalIntent: v241, retryLabelEval: v242, refreshLabelEval: v243, runPromptEval: v244, labelEvalPending: v245, labelEvalSucceeded: v246, labelEvalActionLabel: v247, promptReviewApproved: v248, labelEvalSubmitDisabled: v249 } = evaluationActions;
  const releaseActions = buildLabelsReleaseActions(v96, v100, v106, v246, v116, v117, v114, v92, v118, v127, v125, v33, v14, v103, v101, v10, v115, v85);
  const { labelPublishActionLabels: v250, labelPublishPending: v251, labelPublishBlocked: v252, releaseBackendStatus: v253, labelCandidatePublishDisabled: v254, labelGrayPublishDisabled: v255, labelPromotePublishDisabled: v256, labelReleaseDisabledReason: v257, labelPublishReason: v258, waitForLabelPublishPoll: v259, applyReleaseDeploymentReadback: v260, pollLabelPublishRun: v261, executeLabelPublishIntent: v262, startLabelPublish: v263, retryLabelPublish: v264, refreshLabelPublish: v265, submitReleaseGate: v266 } = releaseActions;
  const coreRenders = buildLabelsCoreRenders(v13, v131, v129, v183, v159, v206, v86, v217, v88, v140, v195, v153, v184, v123, v150, v70, v251, v114, v218, v158, v125, v132, v144, v35, v33, v189, v29, v155, v220, v169, v219, v14, v2, v36, v266, v143, v142);
  const { renderLabelDataActions: v267, saveReleaseConfig: v268, renderReleaseGateEditor: v269, renderHumanLoopWorkbench: v270, renderLabelClosedLoopStrip: v271 } = coreRenders;
  const inputRenders = buildLabelsInputRenders(v13, v131, v159, v231, v232, v86, v166, v88, v165, v0, v179, v227, v123, v160, v180, v127, v229, v125, v132, v230, v121, v11, v14, v124, v122, v12, v226, v225, v228, v162);
  const { renderOptimizationInputPanel: v272, renderAgentHumanChangePanel: v273, renderChangeEffectPanel: v274, renderAutomationDagsterPanel: v275, renderRunContextHeader: v276 } = inputRenders;
  const decisionRenders = buildLabelsDecisionRenders(v152, v223, v206, v0, v179, v227, v120, v254, v70, v247, v106, v249, v246, v104, v255, v180, v256, v251, v114, v257, v158, v33, v244, v268, v168, v14, v10, v85, v12, v81, v263, v266, v143);
  const { renderDecisionRail: v277, renderUnifiedEvaluationDetail: v278, renderUnifiedReleaseDetail: v279, renderSharedDagsterStatusCompact: v280 } = decisionRenders;
  const workbenchRenders = buildLabelsWorkbenchRenders(v152, v129, v130, v223, v233, v234, v9, v50, v120, v150, v70, v247, v249, v158, v170, v82, v78, v84, v221, v244, v11, v169, v80, v14, v38, v12, v81, v224);
  const { renderExtractionWorkbench: v281, renderPromptWorkbench: v282, renderEvaluationWorkbench: v283 } = workbenchRenders;
  const contractRenders = buildLabelsContractRenders(v13, v223, v70, v251, v114, v84, v177, v269, v14, v85, v266);
  const { renderBackendContractPanel: v284, renderReleaseGateDeepened: v285 } = contractRenders;
  const runRailModel = buildLabelsRunRailModel(v152, v129, v133, v130, v90, v102, v7, v151, v50, v178, v179, v153, v58, v60, v150, v108, v106, v246, v76, v54, v52, v56, v114, v92, v127, v177, v33, v29, v155, v41, v181);
  const { governanceViews: v286, candidateDrafts: v287, activeCandidateDraft: v288, reviewDecisionRows: v289, releaseGateSummaries: v290, extractionRailStatus: v291, aggregationRailStatus: v292, aggregateAwaitingReview: v293, aggregateReviewTerminal: v294, humanRailStatus: v295, evaluationRailStatus: v296, releaseRailStatus: v297, releaseLifecycleStatus: v298, releaseMonitorMetrics: v299, monitorRailStatus: v300, labelUnifiedTraceId: v301, labelRunRailSteps: v302, labelNextAction: v303 } = runRailModel;
  const shellRenders = buildLabelsShellRenders(v13, v130, v17, v286, v213, v120, v70, v72, v245, v108, v76, v54, v52, v303, v251, v114, v302, v301, v113, v118, v127, v128, v158, v243, v265, v242, v264, v201, v204, v220, v196, v14, v6, v5, v145);
  const { renderLabelOptimizationRunRail: v304, renderLabelEntityNotice: v305, renderV2ActionFeedback: v306, renderV2Page: v307, renderV2ScenarioRail: v308, renderLabelFactReadState: v309 } = shellRenders;
  const primaryViews = buildLabelsPrimaryViews(v152, v288, v129, v130, v223, v233, v94, v47, v188, v187, v287, v64, v217, v234, v27, v7, v140, v50, v153, v184, v60, v70, v247, v249, v62, v127, v128, v125, v66, v170, v82, v68, v235, v309, v307, v308, v236, v237, v221, v244, v216, v207, v39, v80, v219, v14, v81, v208, v141, v225, v224);
  const { renderSchemaV2: v310, renderExtractionV2: v311, renderRulesPromptV2: v312 } = primaryViews;
  const reviewReleaseViews = buildLabelsReviewReleaseViews(v131, v152, v183, v86, v166, v88, v164, v178, v179, v227, v184, v254, v70, v247, v245, v108, v106, v249, v246, v104, v255, v180, v256, v252, v251, v114, v257, v127, v125, v84, v290, v33, v307, v242, v189, v289, v156, v29, v181, v244, v268, v212, v230, v209, v211, v168, v11, v14, v12, v263, v266, v143, v142, v228);
  const { renderReviewV2: v313, renderReleaseV2: v314 } = reviewReleaseViews;
  return {
    ...props,
    ...coreState,
    ...releaseState,
    ...candidateModel,
    ...focusModel,
    ...changeModel,
    ...governanceModel,
    ...conflictModel,
    ...intentRecovery,
    ...navigationActions,
    ...optimizationActions,
    ...reviewActions,
    ...persistenceActions,
    ...promptActions,
    ...evaluationActions,
    ...releaseActions,
    ...coreRenders,
    ...inputRenders,
    ...decisionRenders,
    ...workbenchRenders,
    ...contractRenders,
    ...runRailModel,
    ...shellRenders,
    ...primaryViews,
    ...reviewReleaseViews
  };
}

export type LabelsController = ReturnType<typeof useLabelsController>;
