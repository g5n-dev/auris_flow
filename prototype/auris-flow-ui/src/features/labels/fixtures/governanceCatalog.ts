import type { PromptFieldKey } from "../../../shared/contracts/prompts";
import type { AutomationLevelKey } from "../types";
import type labelsFixtureSchema from "./data/labels-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const labelsFixture = await loadJsonFixture<typeof labelsFixtureSchema>(
  new URL("./data/labels-fixtures.json", import.meta.url),
  "标签 fixture"
);


export const labelAutomationLevels = (labelsFixture.governanceCatalog.labelAutomationLevels as unknown as ({ key: "L0"; name: string; writePolicy: string; owner: string; upgrade: string; blockers: string[]; } | { key: "L1"; name: string; writePolicy: string; owner: string; upgrade: string; blockers: string[]; } | { key: "L2"; name: string; writePolicy: string; owner: string; upgrade: string; blockers: string[]; } | { key: "L3"; name: string; writePolicy: string; owner: string; upgrade: string; blockers: string[]; } | { key: "L4"; name: string; writePolicy: string; owner: string; upgrade: string; blockers: string[]; })[]);

export const labelHierarchyBlueprint = (labelsFixture.governanceCatalog.labelHierarchyBlueprint as unknown as { level: string; name: string; contract: string; example: string; }[]);

export const labelPromptLifecycle = (labelsFixture.governanceCatalog.labelPromptLifecycle as unknown as [string, string, string][]);

export const promptOptimizationSuggestions = (labelsFixture.governanceCatalog.promptOptimizationSuggestions as unknown as ({ field: "positive"; title: string; detail: string; } | { field: "negative"; title: string; detail: string; } | { field: "conflict"; title: string; detail: string; } | { field: "schema"; title: string; detail: string; } | { field: "postprocess"; title: string; detail: string; })[]);

export const promptEvalMetrics = (labelsFixture.governanceCatalog.promptEvalMetrics as unknown as [string, string, string, string, string][]);

export const promptBackendContracts = (labelsFixture.governanceCatalog.promptBackendContracts as unknown as [string, string, string][]);

export const promptAdapterRows = (labelsFixture.governanceCatalog.promptAdapterRows as unknown as [string, string, string][]);
