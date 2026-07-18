import type { CanvasIntentKey, CanvasNodeContext, ScheduleControl, TaskSectionMeta, TaskScheduleMode } from "../types";
import type canvasViewFixtureSchema from "./data/canvas-view-fixtures.json";
import { loadJsonFixture } from "../../../shared/runtime/jsonFixture";

const canvasViewFixture = await loadJsonFixture<typeof canvasViewFixtureSchema>(
  new URL("./data/canvas-view-fixtures.json", import.meta.url),
  "画布视图 fixture"
);

export type CanvasIconKey =
  | "Activity"
  | "BrainCircuit"
  | "Database"
  | "GitBranch"
  | "Headphones"
  | "Link2"
  | "Play"
  | "Radio"
  | "RotateCcw"
  | "Sparkles"
  | "Tags"
  | "UserCheck";

export type CanvasNodeContextDescriptor = Omit<CanvasNodeContext, "fields"> & {
  fields: Array<[string, string | null]>;
};

export type CanvasTaskDagNodeDescriptor = {
  id: string;
  column: string;
  label: string;
  asset: string | null;
  detail: string | null;
  kind: string;
  nodeId: string;
  intentKeys: CanvasIntentKey[];
  actionKey?: string;
};

export type CanvasNodeDescriptor = {
  id: string;
  name: string;
  iconKey: CanvasIconKey;
  x: number | null;
  y: number | null;
  status: string | null;
  metaA: string;
  metaB: string | null;
  role: string;
  confidence: number | null;
  intentKeys: CanvasIntentKey[];
  tags: string[];
  active?: boolean;
};

export type CanvasScheduleTriggerDescriptor = {
  iconKey: CanvasIconKey;
  label: string;
  title: string;
  description: string;
  entry: string;
  primary?: string;
  when: string;
  controls: ScheduleControl[];
};

export type CanvasRunConfigDescriptor = [string, string | null];

export type CanvasSchedulePlanDescriptor = {
  title: string | null;
  dagsterObject: string;
  definition: string;
  trigger: string | null;
  timezone: string | null;
  partition: string | null;
  runKey: string | null;
  productState: string | null;
  guardrails: Array<string | null>;
  dagsterRows: Array<Array<string | null>>;
};

export type CanvasSchedulePlanItem = {
  title: string;
  dagsterObject: string;
  definition: string;
  trigger: string;
  timezone: string;
  partition: string;
  runKey: string;
  productState: string;
  guardrails: string[];
  dagsterRows: string[][];
};

export type CanvasSchedulePlan = Record<TaskScheduleMode, CanvasSchedulePlanItem>;

export type CanvasNullableRow = Array<string | null>;

export const canvasRunConfigDescriptors = canvasViewFixture.runConfig as unknown as CanvasRunConfigDescriptor[];

export const canvasSchedulePlanDescriptors = canvasViewFixture.schedulePlans as unknown as Record<
  TaskScheduleMode,
  CanvasSchedulePlanDescriptor
>;

export const canvasDagsterCompatibilityDescriptors = canvasViewFixture.dagsterCompatibilityRows as unknown as Record<
  TaskScheduleMode,
  CanvasNullableRow[]
>;

export const canvasScheduleOutputSinkDescriptors = canvasViewFixture.scheduleOutputSinks as unknown as string[][];

export const canvasNodeContextDescriptors = canvasViewFixture.nodeContexts as unknown as Record<string, CanvasNodeContextDescriptor>;

export const canvasTaskDagDescriptors = canvasViewFixture.taskDag as unknown as {
  nodes: CanvasTaskDagNodeDescriptor[];
  columns: Array<{ id: string; label: string; hint: string }>;
  edges: Array<[string, string]>;
};

export const canvasNodeCollectionDescriptors = canvasViewFixture.nodeCollections as unknown as {
  source: CanvasNodeDescriptor[];
  process: CanvasNodeDescriptor[];
  special: CanvasNodeDescriptor[];
};

export const canvasSectionDescriptors = canvasViewFixture.sectionMeta as unknown as Record<string, TaskSectionMeta>;

export const canvasScheduleTriggerDescriptors = canvasViewFixture.scheduleTriggers as unknown as Record<
  TaskScheduleMode,
  CanvasScheduleTriggerDescriptor
>;
