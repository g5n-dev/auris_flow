import type { EvaluationPromptExperiment, EvaluationPromptSuggestion } from "../types";
import type evaluationFixtureSchema from "./data/evaluation-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const evaluationFixture = await loadJsonFixture<typeof evaluationFixtureSchema>(
  new URL("./data/evaluation-fixtures.json", import.meta.url),
  "评测 fixture"
);


export const evaluationPromptExperimentSource: EvaluationPromptExperiment = (evaluationFixture.promptFixtures.evaluationPromptExperimentSource as unknown as EvaluationPromptExperiment);

export const evaluationPromptSuggestionsSource: EvaluationPromptSuggestion[] = (evaluationFixture.promptFixtures.evaluationPromptSuggestionsSource as unknown as EvaluationPromptSuggestion[]);
