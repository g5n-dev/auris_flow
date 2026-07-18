import type { ComponentType } from "react";

import type { ApiRuntimeContext, ProjectSceneProfileBinding } from "../../api/client";
import type { AuthUser } from "../../shared/contracts/auth";
import type { ModuleDeepLink } from "../../shared/contracts/navigation";

export type CanvasModuleProps = {
  activeTab: string;
  setActiveTab: (tab: string) => void;
  focus: ModuleDeepLink | null;
  currentUser: AuthUser;
  sceneBinding: ProjectSceneProfileBinding | null;
  apiContext: ApiRuntimeContext;
  demoMode: boolean;
};

export type TaskScheduleMode = "定时运行" | "手动运行" | "数据到达触发" | "一次性回填";

export type TaskDraftValidationSeverity = "pass" | "warning" | "blocker";

export type TaskDraftValidationItem = {
  key: string;
  label: string;
  detail: string;
  severity: TaskDraftValidationSeverity;
};

export type TaskDraftValidation = {
  canPublish: boolean;
  blockers: TaskDraftValidationItem[];
  warnings: TaskDraftValidationItem[];
  passed: TaskDraftValidationItem[];
  summary: string;
};

export type ScheduleControlKind = "text" | "number" | "date" | "select" | "textarea";

export type ScheduleControl = {
  key: string;
  label: string;
  type: ScheduleControlKind;
  options?: string[];
  placeholder?: string;
  helper?: string;
  wide?: boolean;
};

export type CanvasIntentKey = "entity" | "audio" | "asset" | "review";

export type FlowStageKey = "ingest" | "agent" | "models" | "experiment" | "export";

export type ExecutionState = "idle" | "queued" | "running" | "success";

export type CanvasDrawerTab = "overview" | "mapping" | "plan" | "logs";

export type MappingSuggestionState = "pending" | "confirmed" | "applied" | "rejected";

export type MappingSuggestion = {
  id: string;
  sourceNodeId: string;
  sourceLabel: string;
  sourceField: string;
  targetField: string;
  targetOptions: string[];
  targetAsset: string;
  confidence: number;
  state: MappingSuggestionState;
  joinKey: string;
  policy: string;
  policyOptions: string[];
  reason: string;
  evidence: string[];
};

export type DagsterRunDraft = {
  jobName: string;
  partitionKey: string;
  assetSelection: string;
  runTags: string;
  runConfigJson: string;
  maxRetries: string;
  concurrencyLimit: string;
  materializationMode: string;
  failurePolicy: string;
  reason: string;
};

export type CanvasNodePosition = { x: number; y: number };

export type CanvasRunLog = {
  id: string;
  time: string;
  name: string;
  state: string;
};

export type CanvasDragState = {
  id: string;
  startX: number;
  startY: number;
  startLeft: number;
  startTop: number;
};

export type DagsterBinding = {
  definition: string;
  op: string;
  assetKey: string;
  ioManager: string;
  partition: string;
  deps: string[];
};

export type AssetOutputContract = {
  assetKey: string;
  displayName: string;
  kind: string;
  description: string;
  api: string;
  partition: string;
  materialization: string;
  upstream: string[];
  aggregateKeys: string[];
  schema: Array<[string, string]>;
};

export type CanvasNodeDraft = {
  name: string;
  dataKey: string;
  role: string;
  input: string;
  output: string;
  httpMethod: string;
  endpoint: string;
  sourceId: string;
  resourceType: string;
  queryParams: string;
  partitionRule: string;
  aggregateKeys: string;
  fieldMapping: string;
  mockPayload: string;
  writePolicy: string;
  dagsterOp: string;
  dagsterAsset: string;
  ioManager: string;
};

export type CanvasNode = {
  id: string;
  name: string;
  icon: ComponentType<{ size?: number }>;
  x: number;
  y: number;
  status: string;
  metaA: string;
  metaB: string;
  role: string;
  confidence: number;
  intentKeys: CanvasIntentKey[];
  tags: string[];
  active?: boolean;
  dagsterBinding?: DagsterBinding;
};

export type CanvasNodeContext = {
  type: string;
  relation: string;
  impact: string;
  version: string;
  usedBy: string[];
  fields: Array<[string, string]>;
};

export type TaskSectionMeta = {
  title: string;
  label: string;
  helper: string;
};

export type AddableCanvasNode = CanvasNode & {
  context: CanvasNodeContext;
};

export type CanvasNodeTemplate = {
  key: string;
  category: "平台数据同步抽取" | "智能处理流水线" | "平台处理结果推送" | "人工与控制";
  title: string;
  description: string;
  adapterKind?: string;
  authMode?: string;
  method?: string;
  endpoint?: string;
  dagsterDefinition?: string;
  defaultOpPrefix?: string;
  defaultIoManager?: string;
  outputSchema?: string[];
  outputContract?: AssetOutputContract;
  depsHint?: string;
  runtimeLinks?: Array<[string, string, string]>;
  x: number;
  y: number;
  node: Omit<CanvasNode, "id" | "name" | "icon" | "x" | "y"> & {
    name: string;
    icon: ComponentType<{ size?: number }>;
  };
  context: CanvasNodeContext;
};
