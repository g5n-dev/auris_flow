import staticCatalog from "../../modules/staticCatalog";
import type { EvaluationCapabilityRow } from "../../shared/contracts/evaluation";
import type { evaluationDatasetsSource, evaluationLabelingCasesSource, evaluationLabelingMetricsSource } from "./fixtures/datasetFixtures";
import type { evaluationPromptExperimentSource, evaluationPromptSuggestionsSource } from "./fixtures/promptFixtures";
import type { evaluationBadcaseWorkflowSeedSource, evaluationManualReviewSeedSource, evaluationModelCompareRowsSource } from "./fixtures/reviewFixtures";

const evaluationCatalog = (staticCatalog as {
  evaluationCatalog: {
    capabilityRows: EvaluationCapabilityRow[];
    datasets: typeof evaluationDatasetsSource;
    labelingMetrics: typeof evaluationLabelingMetricsSource;
    labelingCases: typeof evaluationLabelingCasesSource;
    promptExperiment: typeof evaluationPromptExperimentSource;
    promptSuggestions: typeof evaluationPromptSuggestionsSource;
    manualReviewSeed: typeof evaluationManualReviewSeedSource;
    modelCompareRows: typeof evaluationModelCompareRowsSource;
    badcaseWorkflowSeed: typeof evaluationBadcaseWorkflowSeedSource;
  };
}).evaluationCatalog;

export const evaluationCapabilityRows = evaluationCatalog.capabilityRows;
export const evaluationDatasets = evaluationCatalog.datasets;
export const evaluationLabelingMetrics = evaluationCatalog.labelingMetrics;
export const evaluationLabelingCases = evaluationCatalog.labelingCases;
export const evaluationPromptExperiment = evaluationCatalog.promptExperiment;
export const evaluationPromptSuggestions = evaluationCatalog.promptSuggestions;
export const evaluationManualReviewSeed = evaluationCatalog.manualReviewSeed;
export const evaluationModelCompareRows = evaluationCatalog.modelCompareRows;
export const evaluationBadcaseWorkflowSeed = evaluationCatalog.badcaseWorkflowSeed;
