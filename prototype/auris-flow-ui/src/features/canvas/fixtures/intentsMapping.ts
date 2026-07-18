import type { CanvasIntentKey, MappingSuggestion } from "../types";
import type canvasFixtureSchema from "./data/canvas-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const canvasFixture = await loadJsonFixture<typeof canvasFixtureSchema>(
  new URL("./data/canvas-fixtures.json", import.meta.url),
  "画布 fixture"
);


export const canvasIntentsSource: Array<{
  key: CanvasIntentKey;
  label: string;
  description: string;
  taskId: string;
  output: string;
  step: string;
  trigger: string;
  scope: string;
  checks: string[];
}> = (canvasFixture.intentsMapping.canvasIntentsSource as unknown as { key: CanvasIntentKey; label: string; description: string; taskId: string; output: string; step: string; trigger: string; scope: string; checks: string[]; }[]);

export const loginRiskApiContractsSource = (canvasFixture.intentsMapping.loginRiskApiContractsSource as unknown as { name: string; method: string; path: string; auth: string; request: string; response: string; dagster: string; }[]);

export const createDefaultMappingSuggestions = (): Record<CanvasIntentKey, MappingSuggestion[]> =>
  structuredClone(canvasFixture.intentsMapping.createDefaultMappingSuggestions) as unknown as Record<CanvasIntentKey, MappingSuggestion[]>;
