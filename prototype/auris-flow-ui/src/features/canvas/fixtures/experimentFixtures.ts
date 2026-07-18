
import type canvasFixtureSchema from "./data/canvas-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const canvasFixture = await loadJsonFixture<typeof canvasFixtureSchema>(
  new URL("./data/canvas-fixtures.json", import.meta.url),
  "画布 fixture"
);
export const taskExperimentArmsSource = (canvasFixture.experimentFixtures.taskExperimentArmsSource as unknown as { arm: string; canvas: string; traffic: string; assignment: string; writeback: string; note: string; }[]);

export const taskExperimentMetricsSource = (canvasFixture.experimentFixtures.taskExperimentMetricsSource as unknown as string[][]);

export const experimentMetricContextSource = (canvasFixture.experimentFixtures.experimentMetricContextSource as unknown as string[][]);

export const experimentMetricSuggestionsSource = (canvasFixture.experimentFixtures.experimentMetricSuggestionsSource as unknown as { key: string; name: string; category: string; layer: string; source: string; formula: string; window: string; owner: string; confidence: number; status: string; reason: string; events: string[]; guardrails: string[]; sql: string; action: string; risk: string; }[]);

export const experimentMetricObservationsSource = (canvasFixture.experimentFixtures.experimentMetricObservationsSource as unknown as { label: string; value: string; compare: string; state: string; tone: string; detail: string; trend: number[]; }[]);

export const experimentMetricLineageSource = (canvasFixture.experimentFixtures.experimentMetricLineageSource as unknown as string[][]);
