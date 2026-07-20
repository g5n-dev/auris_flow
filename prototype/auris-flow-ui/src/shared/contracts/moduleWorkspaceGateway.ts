import type { ModuleKey } from "./navigation";

export type WorkspaceApiContext = Partial<{
  tenantId: string;
  projectId: string;
  store: string;
  date: string;
  model: string;
  label: string;
}>;

export type WorkspaceApiEnvelope<T> = {
  data: T;
  meta?: {
    trace_id?: string;
    request_id?: string;
    [key: string]: unknown;
  };
};

export type WorkspaceBackendActionReceipt = {
  id: string;
  status: string;
  trace_id?: string;
  affected_objects?: Array<{ type: string; id: string }>;
  next_actions?: Array<{ key: string; label: string; route?: string }>;
  raw: Record<string, unknown>;
};

export type WorkspaceModuleProjectionReceipt = {
  moduleKey: string;
  route: string;
  count: number;
  state: "synced" | "empty";
  source: "bff";
  collectionItems?: unknown[];
  trace_id?: string;
  summary: string;
  raw: unknown;
};

export type WorkspaceSceneManifest = {
  schema_version: "scene-profile/1";
  scene_key: string;
  display_name: string;
  description: string;
  locales: string[];
  capabilities: string[];
  roles: Array<{ role_key: string; display_name: string; description: string }>;
  entities: Array<{ object_key: string; display_name: string; schema_ref?: string; required?: boolean }>;
  events: Array<{ object_key: string; display_name: string; schema_ref?: string; required?: boolean }>;
  document_types: Array<{ object_key: string; display_name: string; schema_ref?: string; required?: boolean }>;
  data_contract_refs: string[];
  task_type_refs: string[];
  label_version_refs: string[];
  prompt_version_refs: string[];
  knowledge_index_refs: string[];
  eval_dataset_version_refs: string[];
  connector_refs?: string[];
  model_service_refs?: string[];
  hotword_pack_version_refs?: string[];
  rubric_refs?: string[];
  output_sink_refs?: string[];
  dimensions?: Array<{
    dimension_key: string;
    display_name: string;
    value_type: "string" | "date" | "datetime" | "number" | "boolean" | "id";
    scope_level: "tenant" | "project" | "organization" | "location" | "object" | "run";
    required?: boolean;
  }>;
  action_bindings?: Array<{
    action_key: string;
    display_name: string;
    capability: string;
    task_type_ref?: string;
    data_contract_refs: string[];
    connector_refs: string[];
    model_service_refs: string[];
    label_version_ref?: string;
    prompt_version_ref?: string;
    knowledge_index_ref?: string;
    eval_dataset_version_ref?: string;
    hotword_pack_version_ref?: string;
    rubric_ref?: string;
    gold_set_key?: string;
    output_sink_refs: string[];
    human_review_required?: boolean;
  }>;
  metrics: Array<{
    metric_key: string;
    display_name: string;
    unit: string;
    calculator_ref: string;
    evidence_refs: string[];
  }>;
  release_requirements: Array<{
    requirement_key: string;
    gate_kind: string;
    metric_key: string;
    operator: string;
    threshold_ppm: number;
  }>;
  governance: {
    human_review_required: boolean;
    model_may_publish: false;
    retention_policy_ref: string;
    privacy_policy_ref: string;
  };
};

export type WorkspaceSceneProfileVersion = {
  scene_profile_version_id: string;
  scene_profile_id: string;
  version: string;
  status: string;
  source_type: "human" | "import" | "model";
  manifest: WorkspaceSceneManifest;
  manifest_sha256: string;
  validation_report?: Record<string, unknown>;
  review_record?: Record<string, unknown>;
  resource_version: number;
  requested_by?: string;
  reviewed_by?: string;
  published_by?: string;
  trace_id?: string;
};

export type WorkspaceProjectSceneBinding = {
  binding_id: string;
  project_id: string;
  environment: "development" | "staging" | "production";
  scene_profile_id: string;
  scene_profile_version_id: string;
  manifest_sha256: string;
  status: string;
  resource_version: number;
  trace_id?: string;
  version: WorkspaceSceneProfileVersion;
};

export type WorkspacePlatformMutationReceipt = {
  id: string;
  status: string;
  trace_id?: string;
  route: string;
  source: string;
  raw: Record<string, unknown>;
};

export type WorkspaceWriteRequestOptions = {
  idempotencyKey?: string;
  correlationId?: string;
};

export interface ModuleWorkspaceGateway {
  getProjectSceneProfile(
    projectId: string,
    environment: WorkspaceProjectSceneBinding["environment"],
    context?: WorkspaceApiContext
  ): Promise<WorkspaceApiEnvelope<WorkspaceProjectSceneBinding | null>>;
  loadModuleProjection(
    moduleKey: string,
    options?: { context?: WorkspaceApiContext; signal?: AbortSignal; force?: boolean }
  ): Promise<WorkspaceModuleProjectionReceipt>;
  createExportRun(
    payload: { target: string; object_id: string; format?: string; source?: string; [key: string]: unknown }
  ): Promise<WorkspaceApiEnvelope<WorkspaceBackendActionReceipt>>;
  getBackendRun(runId: string): Promise<WorkspaceApiEnvelope<WorkspaceBackendActionReceipt>>;
  createPlatformMutation(
    moduleKey: Exclude<ModuleKey, "listening">,
    payload: Record<string, unknown>,
    options?: WorkspaceWriteRequestOptions
  ): Promise<WorkspaceApiEnvelope<WorkspacePlatformMutationReceipt>>;
}
