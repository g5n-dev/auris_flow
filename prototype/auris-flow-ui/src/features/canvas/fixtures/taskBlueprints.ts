import type { CanvasIntentKey, FlowStageKey } from "../types";
import type canvasFixtureSchema from "./data/canvas-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const canvasFixture = await loadJsonFixture<typeof canvasFixtureSchema>(
  new URL("./data/canvas-fixtures.json", import.meta.url),
  "画布 fixture"
);


export const loginRiskDagsterCompatibilitySource = (canvasFixture.taskBlueprints.loginRiskDagsterCompatibilitySource as unknown as { item: string; business: string; dagster: string; status: string; detail: string; }[]);

export const loginRiskScenarioPoliciesSource = (canvasFixture.taskBlueprints.loginRiskScenarioPoliciesSource as unknown as string[][]);

export const taskTypeBlueprintsSource: Array<{
  key: string;
  name: string;
  intentKey: CanvasIntentKey;
  status: string;
  owner: string;
  sla: string;
  reusableCanvases: string;
  description: string;
  defaultCanvas: string;
}> = (canvasFixture.taskBlueprints.taskTypeBlueprintsSource as unknown as { key: string; name: string; intentKey: CanvasIntentKey; status: string; owner: string; sla: string; reusableCanvases: string; description: string; defaultCanvas: string; }[]);

export const taskFlowStagesSource: Array<{
  key: FlowStageKey;
  title: string;
  intentKey: CanvasIntentKey;
  nodeId: string;
  dagsterObject: string;
  product: string;
  output: string;
  edgeLabel: string;
  detail: string;
  chips: string[];
}> = (canvasFixture.taskBlueprints.taskFlowStagesSource as unknown as { key: FlowStageKey; title: string; intentKey: CanvasIntentKey; nodeId: string; dagsterObject: string; product: string; output: string; edgeLabel: string; detail: string; chips: string[]; }[]);

export const taskCanvasVariantsSource = (canvasFixture.taskBlueprints.taskCanvasVariantsSource as unknown as { key: string; name: string; type: string; role: string; status: string; traffic: string; nodes: string; version: string; owner: string; changed: string; guardrail: string; }[]);
