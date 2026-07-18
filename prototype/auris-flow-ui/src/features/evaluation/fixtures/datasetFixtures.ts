import type { EvaluationDatasetProfile, EvaluationLabelingCase, EvaluationLabelingMetric } from "../types";
import type evaluationFixtureSchema from "./data/evaluation-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const evaluationFixture = await loadJsonFixture<typeof evaluationFixtureSchema>(
  new URL("./data/evaluation-fixtures.json", import.meta.url),
  "评测 fixture"
);


export const evaluationDatasetsSource: EvaluationDatasetProfile[] = (evaluationFixture.datasetFixtures.evaluationDatasetsSource as unknown as EvaluationDatasetProfile[]);

export const evaluationLabelingMetricsSource: EvaluationLabelingMetric[] = (evaluationFixture.datasetFixtures.evaluationLabelingMetricsSource as unknown as EvaluationLabelingMetric[]);

export const evaluationLabelingCasesSource: EvaluationLabelingCase[] = (evaluationFixture.datasetFixtures.evaluationLabelingCasesSource as unknown as EvaluationLabelingCase[]);
