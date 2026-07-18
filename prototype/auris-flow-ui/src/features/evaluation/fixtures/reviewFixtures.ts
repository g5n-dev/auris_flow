import type { EvaluationBadcaseWorkflowItem, EvaluationManualReviewItem, EvaluationModelCompareRow } from "../types";
import type evaluationFixtureSchema from "./data/evaluation-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const evaluationFixture = await loadJsonFixture<typeof evaluationFixtureSchema>(
  new URL("./data/evaluation-fixtures.json", import.meta.url),
  "评测 fixture"
);


export const evaluationManualReviewSeedSource: EvaluationManualReviewItem[] = (evaluationFixture.reviewFixtures.evaluationManualReviewSeedSource as unknown as EvaluationManualReviewItem[]);

export const evaluationModelCompareRowsSource: EvaluationModelCompareRow[] = (evaluationFixture.reviewFixtures.evaluationModelCompareRowsSource as unknown as EvaluationModelCompareRow[]);

export const evaluationBadcaseWorkflowSeedSource: EvaluationBadcaseWorkflowItem[] = (evaluationFixture.reviewFixtures.evaluationBadcaseWorkflowSeedSource as unknown as EvaluationBadcaseWorkflowItem[]);
