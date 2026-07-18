import type { LabelsModuleProps } from "../types";
import type { LabelsCandidateModel } from "./buildLabelsCandidateModel";
import type { LabelsChangeModel } from "./buildLabelsChangeModel";
import type { LabelsConflictModel } from "./buildLabelsConflictModel";
import type { LabelsGovernanceModel } from "./buildLabelsGovernanceModel";
import type { LabelsNavigationActions } from "./buildLabelsNavigationActions";
import type { LabelsOptimizationActions } from "./buildLabelsOptimizationActions";
import type { LabelsReviewActions } from "./buildLabelsReviewActions";
import type { LabelsCoreState } from "./useLabelsCoreState";
import type { LabelsFocusModel } from "./useLabelsFocus";
import type { LabelsIntentRecovery } from "./useLabelsIntentRecovery";
import type { LabelsReleaseState } from "./useLabelsReleaseState";

export type LabelsActionScope = LabelsModuleProps
  & LabelsCoreState
  & LabelsReleaseState
  & LabelsCandidateModel
  & LabelsFocusModel
  & LabelsChangeModel
  & LabelsGovernanceModel
  & LabelsConflictModel
  & LabelsIntentRecovery
  & LabelsNavigationActions
  & LabelsOptimizationActions
  & LabelsReviewActions;
