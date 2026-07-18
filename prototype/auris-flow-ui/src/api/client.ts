import { parseApiRequestError } from "./apiRequestError";
import {
  clearBrowserSessionSecurityContext,
  getBrowserSessionCsrfToken,
  setBrowserSessionCsrfToken
} from "./authClient";
import type { AuthSession } from "../shared/contracts/auth";
export { ApiRequestError, isApiRequestError } from "./apiRequestError";

export type BackendStatus = "checking" | "online" | "degraded" | "offline";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api";
const DEMO_MODE = import.meta.env.VITE_DEMO_MODE === "true";
const DEMO_API_CONTEXT = {
  tenantId: "aurora_auto",
  projectId: "sales_qa",
  store: "极光中心店",
  date: "2025-05-26",
  model: "v2.3.1",
  label: "v1.8.4"
};
const EMPTY_API_CONTEXT: typeof DEMO_API_CONTEXT = {
  tenantId: "",
  projectId: "",
  store: "",
  date: "",
  model: "",
  label: ""
};
const DEFAULT_API_CONTEXT = DEMO_MODE ? DEMO_API_CONTEXT : EMPTY_API_CONTEXT;
let apiContext = { ...DEFAULT_API_CONTEXT };

const requestId = () => `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
const safeHeaderValue = (value: string) => (/^[\x00-\xff]*$/.test(value) ? value : encodeURIComponent(value));
const stableStringify = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableStringify(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
};
const stablePayloadHash = (value: unknown) => {
  const input = stableStringify(value);
  let hash = 0;
  for (let index = 0; index < input.length; index += 1) {
    hash = (hash * 31 + input.charCodeAt(index)) >>> 0;
  }
  return hash.toString(36);
};
export const stableIdempotencyKey = (scope: string, payload: unknown) => safeHeaderValue(`${scope}:${stablePayloadHash(payload)}`);
export type WriteRequestOptions = {
  idempotencyKey?: string;
  /**
   * A stable business-flow root. The server still creates a fresh trace_id for every
   * request and records this value as its X-Correlation-Id parent relation.
   */
  correlationId?: string;
};

const correlationHeaders = (options?: Pick<WriteRequestOptions, "correlationId">): Record<string, string> =>
  options?.correlationId?.trim()
    ? { "X-Correlation-Id": safeHeaderValue(options.correlationId.trim()) }
    : {};

const userIntentId = () =>
  globalThis.crypto?.randomUUID?.() ??
  `intent-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

export const createUserIntentIdempotencyKey = (scope: string) =>
  safeHeaderValue(`${scope}:intent:${userIntentId()}`);

const writeIdempotencyKey = (scope: string, options?: WriteRequestOptions) => {
  const callerKey = options?.idempotencyKey?.trim();
  return callerKey ? safeHeaderValue(callerKey) : createUserIntentIdempotencyKey(scope);
};

export type ApiRuntimeContext = Partial<typeof DEFAULT_API_CONTEXT>;
type ResolvedApiContext = typeof DEFAULT_API_CONTEXT;

function resolveApiContext(contextOverride?: ApiRuntimeContext): ResolvedApiContext {
  return {
    ...apiContext,
    ...Object.fromEntries(
      Object.entries(contextOverride ?? {}).filter(([, value]) => typeof value === "string")
    )
  } as ResolvedApiContext;
}

export function setApiContext(nextContext: ApiRuntimeContext) {
  apiContext = {
    ...apiContext,
    ...Object.fromEntries(
      Object.entries(nextContext).filter(([, value]) => typeof value === "string")
    )
  };
}

export function clearApiAuthContext() {
  apiContext = { ...DEFAULT_API_CONTEXT };
  clearBrowserSessionSecurityContext();
}

export function establishApiSession(session: AuthSession) {
  apiContext = {
    ...DEFAULT_API_CONTEXT,
    tenantId: session.user.tenant_id,
    projectId: session.user.project_id
  };
  setBrowserSessionCsrfToken(session.csrf_token ?? session.user.csrf_token);
}

export type ApiEnvelope<T> = {
  data: T;
  meta?: {
    trace_id?: string;
    request_id?: string;
    [key: string]: unknown;
  };
};

export type AudioPlaybackGrant = {
  audio_session_id: string;
  playback_url: string;
  expires_at: string;
  expires_in_seconds?: number;
  status: string;
  trace_id?: string;
};

export type ApiCollection<T> = {
  items: T[];
};

export type AudioSessionSummary = Record<string, unknown> & {
  id: string;
  status?: string;
  trace_id?: string;
  recording_id?: string;
};

export type AudioSessionDetail = AudioSessionSummary & {
  recording?: Record<string, unknown>;
  boundaries?: Array<Record<string, unknown>>;
  evidence_packs?: Array<Record<string, unknown>>;
  asr_segments?: Array<Record<string, unknown>>;
  vad_segments?: Array<Record<string, unknown>>;
  speaker_turns?: Array<Record<string, unknown>>;
  voiceprint_samples?: Array<Record<string, unknown>>;
  audio_quality_reports?: Array<Record<string, unknown>>;
  event_links?: Array<Record<string, unknown>>;
  listening_annotations?: Array<Record<string, unknown>>;
};

export type HumanReviewTask = Record<string, unknown> & {
  id: string;
  status?: string;
  queue?: string;
  audio_session_id?: string;
  evidence_pack_id?: string;
  trace_id?: string;
};

export type WorkItemReceipt = {
  id: string;
  status: string;
  trace_id?: string;
  [key: string]: unknown;
};

export type PlatformMutationReceipt = {
  id: string;
  status: string;
  trace_id?: string;
  route: string;
  source:
    | "work_items"
    | "label_versions"
    | "eval_runs"
    | "insight_actions"
    | "task_versions"
    | "knowledge_runs"
    | "data_asset_backfills"
    | "settings_drafts";
  raw: Record<string, unknown>;
};

export type ModuleProjectionReceipt = {
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

export type BackendActionReceipt = {
  id: string;
  status: string;
  trace_id?: string;
  affected_objects?: Array<{ type: string; id: string }>;
  next_actions?: Array<{ key: string; label: string; route?: string }>;
  raw: Record<string, unknown>;
};

export type LabelVersionEvaluationLock = {
  label_version_id: string;
  status: "locked";
  resource_version: number;
  label_resource_version: number;
  prompt_version_id: string;
  model_version: string;
  aggregation_policy_version_id: string;
  eval_dataset_version_id: string;
  optimization_run_id: string;
  snapshot_sha256: string;
  locked_at: string;
  locked_by: string;
  materialized: boolean;
  trace_id?: string;
  next_action: "create-eval-run";
};

export type InsightMetricRunPayload = {
  metric_keys: string[];
  time_range: string;
  store_ids?: string[];
  model_version?: string;
  label_version?: string;
  source?: string;
  [key: string]: unknown;
};

export type InsightMaterializedMetric = {
  id: string;
  metric_result_id: string;
  metric_key: string;
  status: string;
  source_run_id?: string;
  snapshot_role?: string;
  immutable?: boolean;
  trace_id?: string;
  [key: string]: unknown;
};
export type InsightMetricQuery = {
  time_range: string;
  store_id?: string;
  model_version?: string;
  label_version?: string;
  limit?: number;
  source_run_id?: string;
  metric_keys?: string[];
};
export type InsightReportRunPayload = {
  metric_result_ids: string[];
  [key: string]: unknown;
};

export type ProjectCreatePayload = {
  project_id: string;
  name: string;
  owner_name: string;
  scene: string;
  scene_setup_mode?: string;
  scene_objective?: string;
  data_mode: string;
  label_version?: string;
  label_binding_mode?: string;
  quality_target: string;
  status: string;
  source: string;
  next_action?: string;
  members?: Array<Record<string, unknown>>;
  member_user_ids?: string[];
};

export type SceneProfileManifest = {
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

export type SceneProfileVersion = {
  scene_profile_version_id: string;
  scene_profile_id: string;
  version: string;
  status: string;
  source_type: "human" | "import" | "model";
  manifest: SceneProfileManifest;
  manifest_sha256: string;
  validation_report?: Record<string, unknown>;
  review_record?: Record<string, unknown>;
  resource_version: number;
  requested_by?: string;
  reviewed_by?: string;
  published_by?: string;
  trace_id?: string;
};

export type SceneProfileSummary = {
  scene_profile_id: string;
  scene_key: string;
  name: string;
  description: string;
  status: string;
  current_published_version_id?: string;
  version_count?: number;
  trace_id?: string;
};

export type SceneProfileDetail = SceneProfileSummary & {
  versions: SceneProfileVersion[];
};

export type ProjectSceneProfileBinding = {
  binding_id: string;
  project_id: string;
  environment: "development" | "staging" | "production";
  scene_profile_id: string;
  scene_profile_version_id: string;
  manifest_sha256: string;
  status: string;
  resource_version: number;
  trace_id?: string;
  version: SceneProfileVersion;
};

export type ControlledExperimentMetricSnapshot = {
  metric_snapshot_id: string; experiment_id: string; snapshot_version: number;
  verdict: "insufficient_sample" | "blocked_sample_ratio" | "blocked_guardrail" | "promote" | "hold";
  primary_metric_key: string; evidence_sha256: string;
  scene_profile_id: string; scene_profile_version_id: string; scene_profile_snapshot_sha256: string;
  experiment_resource_version: number;
  sample_sizes: Record<"control" | "candidate", number>;
  assignment_counts?: Record<"control" | "candidate", number>; completion_rates?: Record<"control" | "candidate", number | null>;
  sample_ratio_diagnostic?: { status: "pass" | "blocked"; detected: boolean; assignment: ControlledExperimentSampleRatioDiagnostic; analysis_sample: ControlledExperimentSampleRatioDiagnostic };
  analysis_unit?: string; distinct_assignments?: number; outcome_count?: number;
  fact_source?: "signed_task_run_completion" | "local_system_completion" | "mixed_trusted_completion" | "no_observations"; source_kinds?: string[];
  source_run_count?: number; completion_receipt_count?: number;
  source_run_ids_sha256?: string; completion_receipt_ids_sha256?: string;
  calculator_refs?: Record<string, string | null>; calculator_engine?: string;
  min_sample_size_per_arm: number; confidence_level: number;
  primary_metric: Record<string, unknown> & {
    metric_key: string;
    display_name?: string;
    control_value: number | null;
    candidate_value: number | null;
    delta: number | null;
    confidence_low?: number | null; confidence_high?: number | null;
    p_value: number | null;
    status: string;
  };
  guardrails: Array<Record<string, unknown> & {
    metric_key: string; display_name?: string;
    control_value: number | null;
    candidate_value: number | null;
    delta: number | null;
    status: string;
  }>;
  trace_id?: string; created_at?: string;
};
type ControlledExperimentSampleRatioDiagnostic = {
  total: number; counts: Record<"control" | "candidate", number>; expected_ppm: Record<"control" | "candidate", number>; observed_ppm: Record<"control" | "candidate", number | null>; chi_square: number | null; p_value: number | null; alpha: number; detected: boolean;
};

export type ControlledExperimentVariantDimension =
  | "workflow"
  | "model"
  | "prompt"
  | "label_policy"
  | "bundle";

export type ControlledExperiment = {
  experiment_id: string;
  name: string;
  experiment_kind: "task_version";
  variant_dimension: ControlledExperimentVariantDimension;
  actual_changed_dimensions: Array<
    "workflow" | "model" | "prompt" | "label_schema" | "label_policy" | "other"
  >;
  variant_diff_sha256: string;
  task_type_id: string;
  control_task_version_id: string;
  candidate_task_version_id: string;
  scene_profile_id: string;
  scene_profile_version_id: string;
  scene_profile_snapshot_sha256: string;
  design_sha256: string;
  status: "draft" | "running" | "paused" | "stopped" | "decided";
  resource_version: number;
  hypothesis: string;
  allocation_unit: string;
  assignment_key_version?: string;
  arms: Array<{
    arm_key: "control" | "candidate";
    task_version_id: string;
    allocation_ppm: number;
    task_version_status?: string;
    task_version_snapshot_sha256?: string;
    task_version_behavior_sha256?: string;
    task_version_binding_sha256?: string;
    component_fingerprints?: Record<string, {
      present: boolean;
      sha256?: string | null;
      summary?: string[];
      field_names?: string[];
    }>;
  }>;
  primary_metric: Record<string, unknown> & {
    metric_key: string;
    display_name?: string;
    direction: "increase" | "decrease";
    minimum_effect: number;
  };
  guardrails: Array<Record<string, unknown> & {
    metric_key: string;
    display_name?: string;
    direction: "increase" | "decrease";
    maximum_regression: number;
  }>;
  min_sample_size_per_arm: number;
  confidence_level: number;
  counts: {
    assignments: number;
    exposures: number;
    outcomes: number;
    metric_snapshots: number;
    decisions: number;
  };
  latest_metric_snapshot?: ControlledExperimentMetricSnapshot | null;
  decisions?: Array<Record<string, unknown>>;
  trace_id?: string;
  started_at?: string | null;
  ended_at?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type ControlledExperimentCreatePayload = {
  experiment_id?: string;
  name: string;
  experiment_kind: ControlledExperiment["experiment_kind"];
  variant_dimension: ControlledExperimentVariantDimension;
  task_type_id: string;
  hypothesis: string;
  allocation_unit: "audio_session" | "conversation" | "store" | "user" | "device" | "business_object";
  arms: ControlledExperiment["arms"];
  primary_metric: {
    metric_key: string;
    direction: "increase" | "decrease";
    minimum_effect: number;
  };
  guardrails: Array<{
    metric_key: string;
    direction: "increase" | "decrease";
    maximum_regression: number;
  }>;
  min_sample_size_per_arm: number;
  confidence_level: 0.9 | 0.95 | 0.99;
};

export type SceneProfileGenerationPayload = {
  scene_profile_id?: string;
  scene_key: string;
  name: string;
  description: string;
  version: string;
  objective: string;
  model_ref: string;
  input_refs: string[];
  parent_version_id?: string;
};

export type ConnectorCreatePayload = {
  connector_id: string;
  name: string;
  source_type: string;
  status?: string;
  sync_mode?: string;
  target_asset_key?: string;
  schema_fields?: string[];
  source?: string;
  [key: string]: unknown;
};

const MODULE_PROJECTION_ROUTES: Record<string, string> = {
  home: "/v1/insights/ops-summary",
  tenants: "/v1/tenants",
  projects: "/v1/projects",
  canvas: "/v1/task-versions",
  data: "/v1/audio-sessions/aggregations",
  knowledge: "/v1/knowledge-sources",
  labels: "/v1/label-versions",
  insights: "/v1/insights/metrics",
  evaluation: "/v1/eval-runs",
  assets: "/v1/data-assets/recent",
  settings: "/v1/settings"
};
const moduleProjectionInFlight = new Map<string, Promise<ModuleProjectionReceipt>>();

export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<T>> {
  const requestContext = resolveApiContext(contextOverride);
  if (path.startsWith("/v1/") && (!requestContext.tenantId || !requestContext.projectId)) {
    throw new Error("尚未建立租户/项目上下文，已阻断使用演示场景默认值发起 BFF 请求");
  }
  const headers = new Headers(options.headers);
  headers.set("X-Tenant-Id", safeHeaderValue(requestContext.tenantId));
  headers.set("X-Project-Id", safeHeaderValue(requestContext.projectId));
  const optionalContextHeaders = [
    ["X-Store-Key", requestContext.store],
    ["X-Business-Date", requestContext.date],
    ["X-Model-Version", requestContext.model],
    ["X-Label-Version", requestContext.label]
  ] as const;
  optionalContextHeaders.forEach(([header, value]) => {
    if (value) headers.set(header, safeHeaderValue(value));
    else headers.delete(header);
  });
  // Browser requests authenticate only with the HttpOnly session cookie. Strip a
  // caller-provided bearer as a fail-closed guard against legacy UI code paths.
  headers.delete("Authorization");
  const requestMethod = (options.method ?? "GET").toUpperCase();
  const csrfToken = getBrowserSessionCsrfToken();
  if (!["GET", "HEAD", "OPTIONS"].includes(requestMethod) && csrfToken && !headers.has("X-CSRF-Token")) {
    headers.set("X-CSRF-Token", safeHeaderValue(csrfToken));
  }
  headers.set("X-Request-Id", requestId());
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
    credentials: "include"
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw parseApiRequestError(body, response.status);
  return body as ApiEnvelope<T>;
}

export type CalibrationSample = {
  source_case_id: string;
  evidence_ref: string;
};

export type CalibrationRound = {
  id: string;
  round_id: string;
  dataset_id: string;
  dataset_version: string;
  label_version: string;
  rubric_version: string;
  sample_manifest_sha256: string;
  status: "in_review" | "ready" | "published";
  sealed: boolean;
  my_role: "reviewer_a" | "reviewer_b" | "adjudicator" | "observer";
  resource_version: number;
  sample_count: number;
  paired_submission_count?: number;
  agreed_count?: number;
  conflict_count?: number;
  adjudication_count?: number;
  excluded_count?: number;
  observed_agreement_ppm?: number;
  cohen_kappa_micros?: number;
  cohen_kappa_defined?: boolean;
  root_trace_id: string;
  current_trace_id: string;
  created_at?: string;
  updated_at?: string;
  published_at?: string;
};

export type CalibrationAssignment = {
  assignment_id: string;
  round_id: string;
  item_id: string;
  review_task_id: string;
  slot: "A" | "B";
  ordinal: number;
  source_case_id: string;
  evidence_ref: string;
  status: "pending" | "submitted";
  resource_version: number;
  trace_id: string;
};

export type CalibrationConflict = {
  item_id: string;
  round_id: string;
  ordinal: number;
  source_case_id: string;
  evidence_ref: string;
  status: string;
  review_outcome: string;
  resource_version: number;
  adjudication_claimed: boolean;
  submissions: Array<{
    submission_id: string;
    slot: "A" | "B";
    value: unknown;
    submitted_at: string;
  }>;
};

export type CalibrationGoldRelease = {
  gold_set_version_id: string;
  gold_set_key: string;
  version_number: number;
  round_id: string;
  status: "published";
  annotation_count: number;
  sample_count: number;
  excluded_count: number;
  coverage_ppm: number;
  observed_agreement_ppm: number;
  cohen_kappa_micros: number;
  cohen_kappa_defined: boolean;
  conflict_count: number;
  adjudication_count: number;
  trace_id: string;
  published_at: string;
};

export type CalibrationDecisionValue = {
  decision: "pass" | "fail";
  reason_code?: string;
  evidence_refs?: string[];
};

export type GoldSetVersion = CalibrationGoldRelease & {
  id: string;
  dataset_id: string;
  dataset_version: string;
  label_version: string;
  rubric_version: string;
  sample_manifest_sha256: string;
  annotation_manifest_sha256: string;
  resource_version: 1;
  published_by: string;
  annotations?: Array<{
    gold_annotation_id: string;
    item_id: string;
    source_case_id: string;
    evidence_ref: string;
    value: CalibrationDecisionValue;
    canonical_value_sha256: string;
    resolution_source: "agreed" | "adjudicated";
    trace_id: string;
    created_at: string;
  }>;
};

export async function createCalibrationRound(
  payload: {
    dataset_id: string;
    dataset_version: string;
    label_version: string;
    rubric_version: string;
    reviewer_ids: [string, string];
    adjudicator_id: string;
    samples: CalibrationSample[];
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<CalibrationRound>> {
  return apiRequest<CalibrationRound>("/v1/calibration-rounds", {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey("calibration_round", options)
    },
    body: JSON.stringify(payload)
  });
}

export async function listCalibrationRounds(): Promise<ApiEnvelope<{ items: CalibrationRound[] }>> {
  return apiRequest<{ items: CalibrationRound[] }>("/v1/calibration-rounds?limit=50");
}

export async function getCalibrationRound(roundId: string): Promise<ApiEnvelope<CalibrationRound>> {
  return apiRequest<CalibrationRound>(`/v1/calibration-rounds/${encodeURIComponent(roundId)}`);
}

export async function listCalibrationAssignments(roundId: string): Promise<ApiEnvelope<{ items: CalibrationAssignment[] }>> {
  const query = new URLSearchParams({ mine: "true", round_id: roundId, limit: "100" });
  return apiRequest<{ items: CalibrationAssignment[] }>(`/v1/calibration-assignments?${query.toString()}`);
}

export async function submitCalibrationAssignment(
  assignmentId: string,
  payload: { value: CalibrationDecisionValue; expected_resource_version: number },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(
    `/v1/calibration-assignments/${encodeURIComponent(assignmentId)}/submissions`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(`calibration_submission_${assignmentId}`, options)
      },
      body: JSON.stringify(payload)
    }
  );
}

export async function listCalibrationConflicts(roundId: string): Promise<ApiEnvelope<{ items: CalibrationConflict[] }>> {
  return apiRequest<{ items: CalibrationConflict[] }>(
    `/v1/calibration-rounds/${encodeURIComponent(roundId)}/conflicts?limit=100`
  );
}

export async function claimCalibrationConflict(
  itemId: string,
  expectedResourceVersion: number,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(
    `/v1/calibration-items/${encodeURIComponent(itemId)}/adjudication-claims`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(`calibration_claim_${itemId}`, options)
      },
      body: JSON.stringify({ expected_resource_version: expectedResourceVersion })
    }
  );
}

export async function adjudicateCalibrationConflict(
  itemId: string,
  payload: {
    decision: "accept_a" | "accept_b" | "revise" | "exclude";
    reason: string;
    expected_resource_version: number;
    value?: CalibrationDecisionValue;
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(
    `/v1/calibration-items/${encodeURIComponent(itemId)}/adjudications`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(`calibration_adjudication_${itemId}`, options)
      },
      body: JSON.stringify(payload)
    }
  );
}

export async function releaseCalibrationGold(
  roundId: string,
  payload: { gold_set_key: string; expected_resource_version: number },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<CalibrationGoldRelease>> {
  return apiRequest<CalibrationGoldRelease>(
    `/v1/calibration-rounds/${encodeURIComponent(roundId)}/gold-releases`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(`calibration_gold_${roundId}`, options)
      },
      body: JSON.stringify(payload)
    }
  );
}

export async function listGoldSetVersions(
  goldSetKey?: string
): Promise<ApiEnvelope<{ items: GoldSetVersion[] }>> {
  const query = new URLSearchParams({ limit: "50" });
  if (goldSetKey) query.set("gold_set_key", goldSetKey);
  return apiRequest<{ items: GoldSetVersion[] }>(`/v1/gold-set-versions?${query.toString()}`);
}

export async function getGoldSetVersion(
  goldSetVersionId: string
): Promise<ApiEnvelope<GoldSetVersion>> {
  return apiRequest<GoldSetVersion>(
    `/v1/gold-set-versions/${encodeURIComponent(goldSetVersionId)}`
  );
}

export async function createAudioPlaybackGrant(
  audioSessionId: string,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<AudioPlaybackGrant>> {
  const idempotencyScope = `audio_playback_grant_${audioSessionId}`;
  return apiRequest<AudioPlaybackGrant>(
    `/v1/audio-sessions/${encodeURIComponent(audioSessionId)}/playback-grants`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(idempotencyScope, options)
      }
    }
  );
}

export async function listAudioSessions(params: {
  status?: string;
  limit?: number;
  cursor?: string;
} = {}): Promise<ApiEnvelope<ApiCollection<AudioSessionSummary>>> {
  const query = new URLSearchParams({ limit: String(params.limit ?? 50) });
  if (params.status) query.set("status", params.status);
  if (params.cursor) query.set("cursor", params.cursor);
  return apiRequest<ApiCollection<AudioSessionSummary>>(
    `/v1/audio-sessions?${query.toString()}`
  );
}

export async function getAudioSession(
  audioSessionId: string
): Promise<ApiEnvelope<AudioSessionDetail>> {
  return apiRequest<AudioSessionDetail>(
    `/v1/audio-sessions/${encodeURIComponent(audioSessionId)}`
  );
}

export async function listAudioSessionAnnotations(
  audioSessionId: string
): Promise<ApiEnvelope<ApiCollection<Record<string, unknown>>>> {
  return apiRequest<ApiCollection<Record<string, unknown>>>(
    `/v1/audio-sessions/${encodeURIComponent(audioSessionId)}/annotations`
  );
}

function projectionCount(data: unknown): number {
  if (Array.isArray(data)) return data.length;
  if (!data || typeof data !== "object") return 0;
  const record = data as Record<string, unknown>;
  if (Array.isArray(record.items)) return record.items.length;
  if (Array.isArray(record.metrics)) return record.metrics.length;
  if (Array.isArray(record.funnels)) return record.funnels.length;
  if (Array.isArray(record.reports)) return record.reports.length;
  return Object.keys(record).length;
}

function projectionCollectionItems(data: unknown): unknown[] | undefined {
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== "object") return undefined;
  const items = (data as Record<string, unknown>).items;
  return Array.isArray(items) ? items : undefined;
}

export async function getBackendHealth(): Promise<BackendStatus> {
  try {
    const response = await fetch("/readyz", { credentials: "include" });
    const payload = await response.json().catch(() => ({}));
    if (response.ok && payload?.status === "ok") return "online";
    if (response.status === 503 || payload?.status === "degraded") return "degraded";
    return "offline";
  } catch {
    return "offline";
  }
}

export type ModuleProjectionOptions = {
  context?: ApiRuntimeContext;
  signal?: AbortSignal;
  force?: boolean;
};

export async function loadModuleProjection(
  moduleKey: string,
  options: ModuleProjectionOptions = {}
): Promise<ModuleProjectionReceipt> {
  const route = MODULE_PROJECTION_ROUTES[moduleKey];
  if (!route) {
    throw new Error(`缺少投影路由：${moduleKey}`);
  }
  const requestContext = resolveApiContext(options.context);
  const cacheKey = [
    moduleKey,
    requestContext.tenantId,
    requestContext.projectId,
    requestContext.store,
    requestContext.date,
    requestContext.model,
    requestContext.label
  ].join("|");
  const canDedupe = !options.signal && !options.force;
  const existing = moduleProjectionInFlight.get(cacheKey);
  if (existing && canDedupe) return existing;

  const request = apiRequest<unknown>(route, { signal: options.signal }, requestContext).then((response) => {
    const count = projectionCount(response.data);
    const state: ModuleProjectionReceipt["state"] = count > 0 ? "synced" : "empty";
    return {
      moduleKey,
      route,
      count,
      state,
      source: "bff" as const,
      collectionItems: projectionCollectionItems(response.data),
      trace_id: response.meta?.trace_id,
      summary: state === "synced" ? `BFF ${count} 条对象` : "BFF 0 条对象 · 空数据",
      raw: response.data
    };
  });
  if (!canDedupe) return request;

  moduleProjectionInFlight.set(cacheKey, request);
  void request.then(
    () => {
      if (moduleProjectionInFlight.get(cacheKey) === request) moduleProjectionInFlight.delete(cacheKey);
    },
    () => {
      if (moduleProjectionInFlight.get(cacheKey) === request) moduleProjectionInFlight.delete(cacheKey);
    }
  );
  return request;
}

export async function createWorkItem(payload: Record<string, unknown>, options?: WriteRequestOptions) {
  return apiRequest<WorkItemReceipt>("/v1/work-items", {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey("work_item", options)
    },
    body: JSON.stringify(payload)
  });
}

function normalizeMutationReceipt(
  source: PlatformMutationReceipt["source"],
  route: string,
  data: Record<string, unknown>
): PlatformMutationReceipt {
  const id =
    String(data.id ?? data.run_id ?? data.label_version_id ?? data.work_item_id ?? "pending");
  return {
    id,
    status: String(data.status ?? "pending"),
    trace_id: typeof data.trace_id === "string" ? data.trace_id : undefined,
    route,
    source,
    raw: data
  };
}

function payloadString(payload: Record<string, unknown>, key: string, fallback: string) {
  const value = payload[key];
  return typeof value === "string" && value.trim() ? value : fallback;
}

export function normalizeActionReceipt(data: Record<string, unknown>, metaTraceId?: string): BackendActionReceipt {
  const affectedObjects = Array.isArray(data.affected_objects)
    ? data.affected_objects.filter(
        (item): item is { type: string; id: string } =>
          Boolean(item) &&
          typeof item === "object" &&
          typeof (item as Record<string, unknown>).type === "string" &&
          typeof (item as Record<string, unknown>).id === "string"
      )
    : undefined;
  const nextActions = Array.isArray(data.next_actions)
    ? data.next_actions.filter(
        (item): item is { key: string; label: string; route?: string } =>
          Boolean(item) &&
          typeof item === "object" &&
          typeof (item as Record<string, unknown>).key === "string" &&
          typeof (item as Record<string, unknown>).label === "string"
      )
    : undefined;
  return {
    id: String(
      data.id ??
        data.run_id ??
        data.extraction_run_id ??
        data.aggregation_run_id ??
        data.aggregate_id ??
        data.observation_id ??
        data.optimization_run_id ??
        data.badcase_id ??
        data.prompt_version_id ??
        data.prompt_asset_id ??
        data.deployment_id ??
        data.pack_id ??
        data.version_id ??
        data.item_id ??
        data.project_id ??
        data.connector_id ??
        data.tenant_id ??
        "pending"
    ),
    status: String(data.status ?? "pending"),
    trace_id: typeof data.trace_id === "string" ? data.trace_id : metaTraceId,
    affected_objects: affectedObjects,
    next_actions: nextActions,
    raw: data
  };
}

function normalizeReleaseDeploymentReceipt(
  data: Record<string, unknown>,
  metaTraceId?: string
): BackendActionReceipt {
  const receipt = normalizeActionReceipt(data, metaTraceId);
  return {
    ...receipt,
    id: typeof data.deployment_id === "string" && data.deployment_id.trim()
      ? data.deployment_id
      : receipt.id
  };
}

export async function createBackendAction(
  path: string,
  scope: string,
  payload: Record<string, unknown> = {},
  options?: WriteRequestOptions,
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(path, {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(scope, options),
      ...correlationHeaders(options)
    },
    body: JSON.stringify(payload)
  }, contextOverride);
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function createProjectResource(
  payload: ProjectCreatePayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction("/v1/projects", "project_create", payload as Record<string, unknown>, options);
}

export async function listSceneProfiles(
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<{ items: SceneProfileSummary[] }>> {
  return apiRequest<{ items: SceneProfileSummary[] }>("/v1/scene-profiles", {}, contextOverride);
}

export async function getSceneProfile(
  sceneProfileId: string,
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<SceneProfileDetail>> {
  return apiRequest<SceneProfileDetail>(
    `/v1/scene-profiles/${encodeURIComponent(sceneProfileId)}`,
    {},
    contextOverride
  );
}

export async function getProjectSceneProfile(
  projectId: string,
  environment: ProjectSceneProfileBinding["environment"] = "production",
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<ProjectSceneProfileBinding | null>> {
  const query = new URLSearchParams({ environment, allow_missing: "true" });
  return apiRequest<ProjectSceneProfileBinding | null>(
    `/v1/projects/${encodeURIComponent(projectId)}/scene-profile?${query.toString()}`,
    {},
    { ...contextOverride, projectId }
  );
}

export async function createSceneProfileGenerationRun(
  payload: SceneProfileGenerationPayload,
  options?: WriteRequestOptions,
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    "/v1/scene-profile-generation-runs",
    "scene_profile_generation",
    payload as unknown as Record<string, unknown>,
    options,
    contextOverride
  );
}

export async function validateSceneProfileVersion(
  sceneProfileVersionId: string,
  options?: WriteRequestOptions,
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/scene-profile-versions/${encodeURIComponent(sceneProfileVersionId)}/validations`,
    `scene_profile_validate_${sceneProfileVersionId}`,
    {},
    options,
    contextOverride
  );
}

export async function reviewSceneProfileVersion(
  sceneProfileVersionId: string,
  decision: "approved" | "rejected",
  reason: string,
  options?: WriteRequestOptions,
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/scene-profile-versions/${encodeURIComponent(sceneProfileVersionId)}/reviews`,
    `scene_profile_review_${sceneProfileVersionId}`,
    { decision, reason },
    options,
    contextOverride
  );
}

export async function publishSceneProfileVersion(
  sceneProfileVersionId: string,
  reason: string,
  options?: WriteRequestOptions,
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/scene-profile-versions/${encodeURIComponent(sceneProfileVersionId)}/publish`,
    `scene_profile_publish_${sceneProfileVersionId}`,
    { reason },
    options,
    contextOverride
  );
}

export async function bindProjectSceneProfile(
  projectId: string,
  sceneProfileVersionId: string,
  expectedResourceVersion: number | undefined,
  options?: WriteRequestOptions,
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<ProjectSceneProfileBinding>> {
  return apiRequest<ProjectSceneProfileBinding>(
    `/v1/projects/${encodeURIComponent(projectId)}/scene-profile`,
    {
      method: "PUT",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(`scene_profile_bind_${projectId}`, options),
        ...correlationHeaders(options)
      },
      body: JSON.stringify({
        scene_profile_version_id: sceneProfileVersionId,
        environment: "production",
        ...(expectedResourceVersion === undefined ? {} : { expected_resource_version: expectedResourceVersion })
      })
    },
    { ...contextOverride, projectId }
  );
}

export async function createTenantResource(
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction("/v1/tenants", "tenant_create", payload, options);
}

export async function getTenantResource(
  tenantId: string
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(`/v1/tenants/${encodeURIComponent(tenantId)}`);
}

export async function patchTenantResource(
  tenantId: string,
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(`/v1/tenants/${encodeURIComponent(tenantId)}`, {
    method: "PATCH",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(`tenant_patch_${tenantId}`, options)
    },
    body: JSON.stringify(payload)
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function createConnectorResource(
  payload: ConnectorCreatePayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction("/v1/connectors", "connector_create", payload as Record<string, unknown>, options);
}

export async function createInsightMetricRun(
  payload: InsightMetricRunPayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const body = {
    source: "ui_insight_metric_run",
    ...payload
  };
  return createBackendAction("/v1/insights/metric-runs", "insight_metric_run", body, options);
}

export async function getInsightMetricRun(runId: string): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(`/v1/runs/${encodeURIComponent(runId)}`);
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}
export async function getMaterializedInsightMetrics(
  query: InsightMetricQuery
): Promise<ApiEnvelope<{ items: InsightMaterializedMetric[] }>> {
  const params = new URLSearchParams({
    time_range: query.time_range,
    limit: String(query.limit ?? 50)
  });
  if (query.store_id) params.set("store_id", query.store_id);
  if (query.model_version) params.set("model_version", query.model_version);
  if (query.label_version) params.set("label_version", query.label_version);
  if (query.source_run_id) params.set("source_run_id", query.source_run_id);
  for (const metricKey of query.metric_keys ?? []) params.append("metric_key", metricKey);
  return apiRequest<{ items: InsightMaterializedMetric[] }>(`/v1/insights/metrics?${params.toString()}`);
}
export async function createInsightReportRun(
  payload: InsightReportRunPayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const body = {
    source: "ui_insight_report",
    ...payload
  };
  return createBackendAction("/v1/insights/reports", "insight_report", body, options);
}

export async function getInsightReportResource(reportId: string): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(`/v1/insights/reports/${encodeURIComponent(reportId)}`);
}

export async function createInsightExperimentRun(
  actionId: string,
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/insights/actions/${encodeURIComponent(actionId)}/experiments`,
    `insight_experiment_${actionId}`,
    payload,
    options
  );
}

export async function createPlatformSyncJob(
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const body = {
    source: "ui_platform_sync",
    ...payload
  };
  return createBackendAction("/v1/platform-sync-jobs", "platform_sync_job", body, options);
}

function defaultAssetKey(payload: Record<string, unknown>) {
  const explicit = payloadString(payload, "asset_key", "");
  if (explicit) return explicit;
  const target = payload.target;
  if (target && typeof target === "object" && "asset_key" in target) {
    const assetKey = (target as Record<string, unknown>).asset_key;
    if (typeof assetKey === "string" && assetKey.trim()) return assetKey;
  }
  return "";
}

export async function createPlatformMutation(
  moduleKey: string,
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<PlatformMutationReceipt>> {
  if (moduleKey === "labels") {
    const optimizationRunId = payloadString(payload, "optimization_run_id", "");
    const baseVersion = payloadString(payload, "base_version", payloadString(payload, "label_version_id", ""));
    if (!baseVersion) {
      throw new Error("创建标签候选前必须由已绑定 SceneProfile 提供 label_version_id");
    }
    const changePayload = Object.fromEntries(
      Object.entries(payload).filter(([key]) => key !== "optimization_run_id")
    );
    const body = {
      base_version: baseVersion,
      source: "ui_command",
      ...(optimizationRunId ? { optimization_run_id: optimizationRunId } : {}),
      changeset: [changePayload]
    };
    const response = await apiRequest<Record<string, unknown>>("/v1/label-versions", {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey("label_version", options),
        ...correlationHeaders(options)
      },
      body: JSON.stringify(body)
    });
    return {
      ...response,
      data: normalizeMutationReceipt("label_versions", "labels", response.data)
    };
  }

  if (moduleKey === "evaluation") {
    const datasetId = payloadString(payload, "dataset_id", "");
    if (!datasetId) {
      throw new Error("创建评测运行前必须选择评测集");
    }
    const body = {
      ...payload,
      dataset_id: datasetId,
      source: payloadString(payload, "source", "ui_command")
    };
    const response = await apiRequest<Record<string, unknown>>("/v1/eval-runs", {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey("eval_run", options),
        ...correlationHeaders(options)
      },
      body: JSON.stringify(body)
    });
    return {
      ...response,
      data: normalizeMutationReceipt("eval_runs", "evaluation", response.data)
    };
  }

  if (moduleKey === "insights") {
    const body = {
      report_id: payload.report_id,
      metric_result_id: payload.metric_result_id,
      metric_key: payload.metric_key,
      action_type: payload.action_type,
      owner: payload.owner,
      evidence_refs: payload.evidence_refs || [],
      branch: payload.branch || "auto",
      risk_level: payload.risk_level || "medium",
      hypothesis: payload.hypothesis,
      target_value: payload.target_value,
      source: payload.source,
    };
    const response = await apiRequest<Record<string, unknown>>("/v1/insights/actions", {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey("insight_action", options)
      },
      body: JSON.stringify(body)
    });
    return {
      ...response,
      data: normalizeMutationReceipt("insight_actions", "insights", response.data)
    };
  }

  if (moduleKey === "canvas") {
    const taskTypeId = payloadString(payload, "task_type_id", "");
    if (!taskTypeId) {
      throw new Error("创建任务版本前必须由已绑定 SceneProfile 提供 task_type_id");
    }
    const body = {
      name: payloadString(payload, "action", "任务版本草稿"),
      source: "ui_command",
      task_type_id: taskTypeId,
      flow_template: taskTypeId,
      canvas_variant: payloadString(payload, "scene_profile_version_id", "scene-profile-bound"),
      status: "draft",
      payload
    };
    const response = await apiRequest<Record<string, unknown>>("/v1/task-versions", {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey("task_version", options)
      },
      body: JSON.stringify(body)
    });
    return {
      ...response,
      data: normalizeMutationReceipt("task_versions", "task-versions", response.data)
    };
  }

  if (moduleKey === "knowledge") {
    const indexId = payloadString(payload, "knowledge_index_id", "");
    if (!indexId) {
      throw new Error("构建知识索引前必须由已绑定 SceneProfile 提供 knowledge_index_id");
    }
    const body = {
      source: "ui_command",
      reason: payloadString(payload, "action", "知识库构建"),
      payload
    };
    const response = await apiRequest<Record<string, unknown>>(
      `/v1/knowledge-indexes/${encodeURIComponent(indexId)}/build-runs`,
      {
        method: "POST",
        headers: {
          "Idempotency-Key": writeIdempotencyKey("knowledge_build", options)
        },
        body: JSON.stringify(body)
      }
    );
    return {
      ...response,
      data: normalizeMutationReceipt("knowledge_runs", "knowledge-index-build", response.data)
    };
  }

  if (moduleKey === "assets") {
    const action = payloadString(payload, "action", "资产回填");
    const isQualityRetry = action.includes("质量");
    const selectedAssetKey = defaultAssetKey(payload);
    if (!selectedAssetKey) {
      throw new Error("资产操作必须从资产详情选择明确 asset_key，不能使用场景默认资产");
    }
    const assetKey = encodeURIComponent(selectedAssetKey);
    const requestedImpactScope = payload.impact_scope;
    const body = isQualityRetry
      ? {
          source: "ui_asset_quality",
          reason: payloadString(payload, "reason", action),
          impact_scope: "current_project",
          payload
        }
      : {
          partition_key: payloadString(payload, "partition_key", "current_project"),
          reason: payloadString(payload, "reason", action),
          impact_scope: requestedImpactScope && typeof requestedImpactScope === "object" && !Array.isArray(requestedImpactScope)
            ? requestedImpactScope
            : { scope: "current_project", overwrite_history: false }
        };
    const route = isQualityRetry ? "checks/retry" : "backfills";
    const response = await apiRequest<Record<string, unknown>>(`/v1/data-assets/${assetKey}/${route}`, {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(isQualityRetry ? "asset_check_retry" : "asset_backfill", options)
      },
      body: JSON.stringify(body)
    });
    return {
      ...response,
      data: normalizeMutationReceipt("data_asset_backfills", `data-assets/${route}`, response.data)
    };
  }

  if (moduleKey === "settings") {
    const body = {
      name: payloadString(payload, "action", "设置草稿"),
      source: "ui_command",
      status: "draft",
      payload
    };
    const response = await apiRequest<Record<string, unknown>>("/v1/settings/drafts", {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey("settings_draft", options)
      },
      body: JSON.stringify(body)
    });
    return {
      ...response,
      data: normalizeMutationReceipt("settings_drafts", "settings-drafts", response.data)
    };
  }

  const response = await createWorkItem(payload, options);
  return {
    ...response,
    data: normalizeMutationReceipt("work_items", "work-items", response.data)
  };
}

export async function createExportRun(
  payload: {
    target: string;
    object_id: string;
    format?: string;
    source?: string;
    [key: string]: unknown;
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const body = {
    format: "jsonl",
    source: "ui_global_command",
    ...payload
  };
  return createBackendAction("/v1/exports", "export_run", body, options);
}

export async function saveTaskVersionDraft(
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction("/v1/task-versions", "task_version_draft", payload, options);
}

export async function getTaskVersion(
  taskVersionId: string
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(
    `/v1/task-versions/${encodeURIComponent(taskVersionId)}`
  );
}

export async function listTaskVersions(): Promise<ApiEnvelope<{ items: Array<Record<string, unknown>> }>> {
  return apiRequest<{ items: Array<Record<string, unknown>> }>("/v1/task-versions?limit=100");
}

export async function publishTaskVersionDraft(
  taskVersionId: string,
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/task-versions/${encodeURIComponent(taskVersionId)}/publish`,
    `task_version_publish_${taskVersionId}`,
    payload,
    options
  );
}

export async function listControlledExperiments(
  status?: ControlledExperiment["status"]
): Promise<ApiEnvelope<{ items: ControlledExperiment[] }>> {
  const query = status ? `?${new URLSearchParams({ status }).toString()}` : "";
  return apiRequest<{ items: ControlledExperiment[] }>(`/v1/experiments${query}`);
}

export async function getControlledExperiment(
  experimentId: string
): Promise<ApiEnvelope<ControlledExperiment>> {
  return apiRequest<ControlledExperiment>(
    `/v1/experiments/${encodeURIComponent(experimentId)}`
  );
}

export async function createControlledExperiment(
  payload: ControlledExperimentCreatePayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<ControlledExperiment>> {
  return apiRequest<ControlledExperiment>("/v1/experiments", {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey("controlled_experiment_create", options),
      ...correlationHeaders(options)
    },
    body: JSON.stringify(payload)
  });
}

export async function startControlledExperiment(
  experimentId: string,
  expectedResourceVersion: number,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<ControlledExperiment>> {
  return apiRequest<ControlledExperiment>(
    `/v1/experiments/${encodeURIComponent(experimentId)}/start`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(`controlled_experiment_start_${experimentId}`, options),
        ...correlationHeaders(options)
      },
      body: JSON.stringify({ expected_resource_version: expectedResourceVersion })
    }
  );
}

export async function computeControlledExperimentMetrics(
  experimentId: string,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<ControlledExperimentMetricSnapshot>> {
  return apiRequest<ControlledExperimentMetricSnapshot>(
    `/v1/experiments/${encodeURIComponent(experimentId)}/metric-snapshots`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(`controlled_experiment_compute_${experimentId}`, options),
        ...correlationHeaders(options)
      },
      body: JSON.stringify({})
    }
  );
}

export async function decideControlledExperiment(
  experimentId: string,
  payload: {
    decision: "pause" | "resume" | "stop" | "promote_candidate" | "reject_candidate";
    metric_snapshot_id?: string;
    expected_resource_version: number;
    reason: string;
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(
    `/v1/experiments/${encodeURIComponent(experimentId)}/decisions`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(`controlled_experiment_decision_${experimentId}`, options),
        ...correlationHeaders(options)
      },
      body: JSON.stringify(payload)
    }
  );
}

export async function runTaskVersionOnce(
  payload: {
    task_version_id: string;
    trigger_type?: string;
    partition_key?: string;
    run_key?: string;
    [key: string]: unknown;
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const body = {
    trigger_type: "manual",
    ...payload
  };
  return createBackendAction("/v1/task-runs", `task_run_${payload.task_version_id}`, body, options);
}

export async function getBackendRun(runId: string): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(`/v1/runs/${encodeURIComponent(runId)}`);
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function retryBackendRun(
  runId: string,
  payload: { reason: string; payload_overrides?: Record<string, unknown> },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/runs/${encodeURIComponent(runId)}/retries`,
    `run_retry_${runId}`,
    payload,
    options
  );
}

export async function decideBackendRun(
  runId: string,
  decision: "approved" | "rejected",
  reason: string,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/runs/${encodeURIComponent(runId)}/decisions`,
    `run_decision_${runId}_${decision}`,
    { decision, reason },
    options
  );
}

export async function createEvaluationFeedbackTask(
  evalRunId: string,
  payload: {
    badcase_refs: string[];
    target: string;
    reason?: string;
    [key: string]: unknown;
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const body = {
    source: "ui_evaluation_module",
    ...payload
  };
  const response = await apiRequest<Record<string, unknown>>(`/v1/eval-runs/${encodeURIComponent(evalRunId)}/feedback-tasks`, {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(`eval_feedback_${evalRunId}`, options)
    },
    body: JSON.stringify(body)
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function syncKnowledgeSource(
  sourceId: string,
  payload: Record<string, unknown>,
  options?: WriteRequestOptions,
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(`/v1/knowledge-sources/${encodeURIComponent(sourceId)}/sync-runs`, {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(`knowledge_source_sync_${sourceId}`, options)
    },
    body: JSON.stringify(payload)
  }, contextOverride);
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function buildKnowledgeIndex(
  indexId: string,
  payload: Record<string, unknown>,
  options?: WriteRequestOptions,
  contextOverride?: ApiRuntimeContext
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(`/v1/knowledge-indexes/${encodeURIComponent(indexId)}/build-runs`, {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(`knowledge_index_build_${indexId}`, options)
    },
    body: JSON.stringify(payload)
  }, contextOverride);
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function runSettingsProviderTest(
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>("/v1/settings/provider-tests", {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey("settings_provider_test", options)
    },
    body: JSON.stringify(payload)
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function submitHumanReviewDecision(
  taskId: string,
  payload: {
    decision: "accepted" | "approved" | "confirm" | "modified" | "rejected" | "blocked" | "escalated";
    note?: string;
    changes?: Array<{
      target_type: "label_candidate" | "label_aggregate" | "prompt_version_candidate" | "taxonomy_suggestion" | "event_link" | "evidence_pack" | "conversation_boundary" | "voiceprint_sample" | "work_item";
      target_id: string;
      fields: Record<string, unknown>;
    }>;
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(`/v1/human-review-tasks/${encodeURIComponent(taskId)}/decisions`, {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(`human_review_decision_${taskId}`, options)
    },
    body: JSON.stringify(payload)
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function createHumanReviewTask(
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction("/v1/human-review-tasks", "human_review_task", payload, options);
}

export async function listHumanReviewTasks(params: {
  queue?: string;
  status?: string;
  limit?: number;
  cursor?: string;
} = {}): Promise<ApiEnvelope<ApiCollection<HumanReviewTask>>> {
  const query = new URLSearchParams({ limit: String(params.limit ?? 50) });
  if (params.queue) query.set("queue", params.queue);
  if (params.status) query.set("status", params.status);
  if (params.cursor) query.set("cursor", params.cursor);
  return apiRequest<ApiCollection<HumanReviewTask>>(
    `/v1/human-review-tasks?${query.toString()}`
  );
}

export async function getHumanReviewTask(
  taskId: string,
  options?: Pick<WriteRequestOptions, "correlationId">
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(
    `/v1/human-review-tasks/${encodeURIComponent(taskId)}`,
    { headers: correlationHeaders(options) }
  );
}

export type HumanReviewDecisionBatchResult = {
  review_task_id: string;
  aggregate_id?: string;
  status: "success" | "skipped" | "failed";
  decision_id?: string;
  decision?: "accepted" | "rejected" | "escalated";
  reason_code?: string;
};

export type HumanReviewDecisionBatchReceipt = {
  batch_id: string;
  status: "completed" | "partial" | "failed";
  cohort?: {
    label_id: string;
    risk_level: "low";
    policy_version_id: string;
  } | null;
  counts: {
    success: number;
    skipped: number;
    failed: number;
  };
  results: HumanReviewDecisionBatchResult[];
  trace_id: string;
};

export async function submitHumanReviewDecisionBatch(
  payload: {
    items: Array<{
      review_task_id: string;
      decision: "accepted" | "rejected" | "escalated";
      note?: string;
    }>;
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<HumanReviewDecisionBatchReceipt>> {
  return apiRequest<HumanReviewDecisionBatchReceipt>("/v1/human-review-decision-batches", {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey("human_review_decision_batch", options)
    },
    body: JSON.stringify(payload)
  });
}

export async function createLabelOptimizationRun(
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction("/v1/label-optimization-runs", "label_optimization_run", payload, options);
}

export async function getLabelOptimizationRun(
  runId: string,
  options?: Pick<WriteRequestOptions, "correlationId">
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(
    `/v1/label-optimization-runs/${encodeURIComponent(runId)}`,
    { headers: correlationHeaders(options) }
  );
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function createLabelExtractionRun(
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction("/v1/label-extraction-runs", "label_extraction_run", payload, options);
}

export async function getLabelExtractionRun(
  runId: string,
  options?: Pick<WriteRequestOptions, "correlationId">
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(
    `/v1/label-extraction-runs/${encodeURIComponent(runId)}`,
    { headers: correlationHeaders(options) }
  );
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export type LabelEvidenceReference = {
  type?: string;
  id?: string;
  uri?: string;
  sha256?: string;
  [key: string]: unknown;
};

export type LabelObservation = {
  observation_id: string;
  extraction_run_id: string;
  subject_scope: string;
  subject_key: string;
  evidence_ref: LabelEvidenceReference;
  label_version_id: string;
  raw_label: string;
  label_id: string | null;
  value: unknown;
  value_type: string;
  source_family: string;
  source_type: string;
  model_version: string;
  prompt_version_id: string;
  schema_version: string;
  calibration_version_id?: string | null;
  raw_confidence: number;
  calibrated_confidence?: number | null;
  input_sha256: string;
  output_sha256: string;
  status: "materialized";
  trace_id: string;
  created_at?: string;
};

export type LabelAggregate = {
  aggregate_id: string;
  aggregation_run_id: string;
  label_version_id: string;
  policy_version_id: string;
  calibration_version_ids?: string[];
  subject_scope: string;
  subject_key: string;
  label_id: string;
  value_type: string;
  value: unknown;
  score: number;
  margin?: number | null;
  risk_level: "low" | "medium" | "high";
  decision: "require-review" | "auto-accept" | "abstain";
  status: "awaiting-review" | "accepted" | "rejected" | "abstained";
  reason_codes: string[];
  explanation?: Record<string, unknown>;
  deterministic_hash: string;
  review_task_id?: string | null;
  trace_id: string;
  members?: Array<{
    aggregate_member_id: string;
    observation_id: string;
    included: boolean;
    source_family: string;
    evidence_sha256: string;
    calibrated_confidence?: number | null;
    contribution_score?: number | null;
    exclusion_reason?: string | null;
    explanation?: Record<string, unknown>;
  }>;
};

export type LabelAggregationRun = {
  aggregation_run_id: string;
  label_version_id: string;
  policy_version_id: string;
  mode: "l1" | "l2";
  status: string;
  observation_count: number;
  aggregate_count: number;
  input_sha256: string;
  result_sha256?: string | null;
  aggregate_ids: string[];
  taxonomy_suggestion_ids: string[];
  review_task_ids: string[];
  trace_id: string;
};

export type LabelTaxonomySuggestion = {
  suggestion_id: string;
  label_version_id: string;
  raw_labels: string[];
  normalized_label: string;
  observation_ids: string[];
  proposed_action: string;
  status: string;
  review_task_id?: string | null;
  canonical_target_label_id?: string | null;
  trace_id: string;
};

export type ClosedLoopReviewReceipt = {
  target_id?: string;
  aggregate_id?: string;
  suggestion_id?: string;
  candidate_id?: string;
  submission_id?: string;
  adjudication_id?: string;
  review_task_id?: string;
  status: string;
  received_reviews?: number;
  trace_id: string;
  next_action?: string;
};

export async function listLabelObservations(filters: {
  subjectScope?: string;
  subjectKey?: string;
  labelVersionId?: string;
  limit?: number;
  correlationId?: string;
} = {}): Promise<ApiEnvelope<{ items: LabelObservation[] }>> {
  const params = new URLSearchParams();
  if (filters.subjectScope) params.set("subject_scope", filters.subjectScope);
  if (filters.subjectKey) params.set("subject_key", filters.subjectKey);
  if (filters.labelVersionId) params.set("label_version_id", filters.labelVersionId);
  params.set("limit", String(filters.limit ?? 50));
  return apiRequest<{ items: LabelObservation[] }>(`/v1/label-observations?${params.toString()}`, {
    headers: filters.correlationId ? { "X-Correlation-Id": safeHeaderValue(filters.correlationId) } : {}
  });
}

export async function listLabelAggregates(filters: {
  status?: LabelAggregate["status"];
  limit?: number;
  correlationId?: string;
} = {}): Promise<ApiEnvelope<{ items: LabelAggregate[] }>> {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  params.set("limit", String(filters.limit ?? 50));
  return apiRequest<{ items: LabelAggregate[] }>(`/v1/label-aggregates?${params.toString()}`, {
    headers: filters.correlationId ? { "X-Correlation-Id": safeHeaderValue(filters.correlationId) } : {}
  });
}

export async function getLabelAggregationPolicy(
  policyVersionId: string,
  options?: Pick<WriteRequestOptions, "correlationId">
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(
    `/v1/label-aggregation-policies/${encodeURIComponent(policyVersionId)}`,
    { headers: correlationHeaders(options) }
  );
}

export async function createLabelAggregationRun(
  payload: {
    aggregation_run_id: string;
    label_version_id: string;
    policy_version_id: string;
    observation_ids: string[];
    mode: "l1" | "l2";
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<LabelAggregationRun>> {
  return apiRequest<LabelAggregationRun>("/v1/label-aggregation-runs", {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey("label_aggregation_run", options),
      ...correlationHeaders(options)
    },
    body: JSON.stringify(payload)
  });
}

export async function getLabelAggregationRun(
  aggregationRunId: string,
  options?: Pick<WriteRequestOptions, "correlationId">
): Promise<ApiEnvelope<LabelAggregationRun>> {
  return apiRequest<LabelAggregationRun>(
    `/v1/label-aggregation-runs/${encodeURIComponent(aggregationRunId)}`,
    { headers: correlationHeaders(options) }
  );
}

export async function listLabelTaxonomySuggestions(filters: {
  status?: string;
  limit?: number;
  correlationId?: string;
} = {}): Promise<ApiEnvelope<{ items: LabelTaxonomySuggestion[] }>> {
  const params = new URLSearchParams({ limit: String(filters.limit ?? 50) });
  if (filters.status) params.set("status", filters.status);
  return apiRequest<{ items: LabelTaxonomySuggestion[] }>(
    `/v1/label-taxonomy-suggestions?${params.toString()}`,
    { headers: filters.correlationId ? { "X-Correlation-Id": safeHeaderValue(filters.correlationId) } : {} }
  );
}

type ClosedLoopReviewPayload = {
  decision: "accepted" | "modified" | "rejected";
  note?: string;
  value?: unknown;
  taxonomy_action?: "alias" | "create" | "merge" | "split" | "reject";
  canonical_target_label_id?: string;
};

type ClosedLoopAdjudicationPayload = ClosedLoopReviewPayload & { reason: string };

async function postClosedLoopReview(
  path: string,
  scope: string,
  payload: ClosedLoopReviewPayload | ClosedLoopAdjudicationPayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<ClosedLoopReviewReceipt>> {
  return apiRequest<ClosedLoopReviewReceipt>(path, {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(scope, options),
      ...correlationHeaders(options)
    },
    body: JSON.stringify(payload)
  });
}

export const submitLabelAggregateReview = (
  aggregateId: string,
  payload: ClosedLoopReviewPayload,
  options?: WriteRequestOptions
) => postClosedLoopReview(
  `/v1/label-aggregates/${encodeURIComponent(aggregateId)}/review-submissions`,
  `label_aggregate_review_${aggregateId}`,
  payload,
  options
);

export const adjudicateLabelAggregateReview = (
  aggregateId: string,
  payload: ClosedLoopAdjudicationPayload,
  options?: WriteRequestOptions
) => postClosedLoopReview(
  `/v1/label-aggregates/${encodeURIComponent(aggregateId)}/adjudications`,
  `label_aggregate_adjudication_${aggregateId}`,
  payload,
  options
);

export const submitLabelTaxonomyReview = (
  suggestionId: string,
  payload: ClosedLoopReviewPayload,
  options?: WriteRequestOptions
) => postClosedLoopReview(
  `/v1/label-taxonomy-suggestions/${encodeURIComponent(suggestionId)}/review-submissions`,
  `label_taxonomy_review_${suggestionId}`,
  payload,
  options
);

export const adjudicateLabelTaxonomyReview = (
  suggestionId: string,
  payload: ClosedLoopAdjudicationPayload,
  options?: WriteRequestOptions
) => postClosedLoopReview(
  `/v1/label-taxonomy-suggestions/${encodeURIComponent(suggestionId)}/adjudications`,
  `label_taxonomy_adjudication_${suggestionId}`,
  payload,
  options
);

export async function getPromptVersionCandidate(
  candidateId: string,
  options?: Pick<WriteRequestOptions, "correlationId">
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(
    `/v1/prompt-version-candidates/${encodeURIComponent(candidateId)}`,
    { headers: correlationHeaders(options) }
  );
}

type PromptReviewPayload = {
  decision: "accepted" | "modified" | "rejected";
  note?: string;
  field_diff?: Record<string, { before?: unknown; after: unknown; reason: string }>;
};

export async function submitPromptCandidateReview(
  candidateId: string,
  payload: PromptReviewPayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<ClosedLoopReviewReceipt>> {
  return postClosedLoopReview(
    `/v1/prompt-version-candidates/${encodeURIComponent(candidateId)}/review-submissions`,
    `prompt_candidate_review_${candidateId}`,
    payload,
    options
  );
}

export async function adjudicatePromptCandidateReview(
  candidateId: string,
  payload: PromptReviewPayload & { reason: string },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<ClosedLoopReviewReceipt>> {
  return postClosedLoopReview(
    `/v1/prompt-version-candidates/${encodeURIComponent(candidateId)}/adjudications`,
    `prompt_candidate_adjudication_${candidateId}`,
    payload,
    options
  );
}

export async function createLabelBadcase(
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction("/v1/badcases", "label_badcase", payload, options);
}

export async function getEvaluationRun(
  evalRunId: string,
  options?: Pick<WriteRequestOptions, "correlationId">
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(
    `/v1/eval-runs/${encodeURIComponent(evalRunId)}`,
    { headers: correlationHeaders(options) }
  );
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function createPromptVersion(
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction("/v1/prompt-versions", "prompt_version", payload, options);
}

export async function createReleaseDeployment(
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await createBackendAction("/v1/release-deployments", "release_deployment", payload, options);
  return {
    ...response,
    data: normalizeReleaseDeploymentReceipt(response.data.raw, response.meta?.trace_id)
  };
}

export async function getReleaseDeployment(
  deploymentId: string,
  options?: Pick<WriteRequestOptions, "correlationId">
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(
    `/v1/release-deployments/${encodeURIComponent(deploymentId)}`,
    { headers: correlationHeaders(options) }
  );
  return { ...response, data: normalizeReleaseDeploymentReceipt(response.data, response.meta?.trace_id) };
}

export async function transitionReleaseDeployment(
  deploymentId: string,
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await createBackendAction(
    `/v1/release-deployments/${encodeURIComponent(deploymentId)}/transitions`,
    `release_deployment_transition_${deploymentId}`,
    payload,
    options
  );
  return {
    ...response,
    data: normalizeReleaseDeploymentReceipt(response.data.raw, response.meta?.trace_id)
  };
}

export async function getLabelVersionResource(
  labelVersionId: string,
  options?: Pick<WriteRequestOptions, "correlationId">
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(
    `/v1/label-versions/${encodeURIComponent(labelVersionId)}`,
    { headers: correlationHeaders(options) }
  );
}

export async function lockLabelVersionForEvaluation(
  labelVersionId: string,
  payload: {
    expected_resource_version: number;
    prompt_version_id: string;
    model_version: string;
    aggregation_policy_version_id: string;
    eval_dataset_version_id: string;
    optimization_run_id: string;
    confirmation: "lock-for-evaluation";
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<LabelVersionEvaluationLock>> {
  return apiRequest<LabelVersionEvaluationLock>(
    `/v1/label-versions/${encodeURIComponent(labelVersionId)}/evaluation-lock`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(`label_evaluation_lock_${labelVersionId}`, options),
        ...correlationHeaders(options)
      },
      body: JSON.stringify(payload)
    }
  );
}

export async function createQualityAppeal(
  sourceDecisionId: string,
  payload: {
    reason: string;
    evidence_refs: string[];
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>("/v1/quality-appeals", {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(
        `quality_appeal_${sourceDecisionId}`,
        options
      )
    },
    body: JSON.stringify({
      source_decision_id: sourceDecisionId,
      ...payload
    })
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function saveConversationBoundary(
  boundaryId: string,
  payload: {
    audio_session_id: string;
    start_ms: number;
    end_ms: number;
    decision?: string;
    merged_slice_ids?: string[];
    split_slice_ids?: string[];
    extension_ids?: string[];
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(`/v1/conversation-boundaries/${encodeURIComponent(boundaryId)}`, {
    method: "PATCH",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(`conversation_boundary_${boundaryId}`, options)
    },
    body: JSON.stringify(payload)
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

type ListeningAnnotationPayload = {
  id?: string;
  annotation_id: string;
  audio_session_id?: string;
  track: string;
  label: string;
  left: number;
  width: number;
  start_time: string;
  end_time: string;
  field_key?: string;
  value?: string;
  confidence?: number;
  review_state?: string;
  evidence_ref?: string;
  write_target?: string;
  source_text?: string;
  note?: string;
  assignee?: string;
  source?: string;
  [key: string]: unknown;
};

export async function saveListeningAnnotation(
  audioSessionId: string,
  payload: ListeningAnnotationPayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(`/v1/audio-sessions/${encodeURIComponent(audioSessionId)}/annotations`, {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(`listening_annotation_${audioSessionId}_${payload.annotation_id}`, options)
    },
    body: JSON.stringify(payload)
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export type AsrTranscriptCorrectionPayload = {
  annotation_id: string;
  annotation_kind: "asr-transcript-correction";
  confirmation: "record_correction";
  track: "asr";
  audio_session_id: string;
  recognized_text: string;
  corrected_text: string;
  error_type: HotwordErrorType;
  evidence_window: string;
  evidence_storage_object_id: string;
  hotword_pack_version_id: string;
  source_badcase_id?: string;
  source_asr_segment_id?: string;
};

export async function saveAsrTranscriptCorrection(
  audioSessionId: string,
  payload: AsrTranscriptCorrectionPayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(
    `/v1/audio-sessions/${encodeURIComponent(audioSessionId)}/annotations`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(
          `asr_transcript_correction_${audioSessionId}_${payload.annotation_id}`,
          options
        )
      },
      body: JSON.stringify(payload)
    }
  );
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

type EventLinkPayload = {
  id?: string;
  audio_session_id: string;
  source_event_id: string;
  event_ref?: string;
  target_doc_id?: string;
  document_ref?: string;
  relation_type: string;
  join_keys?: string[];
  confidence: number;
  status: string;
  relation_state?: string;
  evidence_window?: string;
};

export async function createEventLink(
  payload: EventLinkPayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>("/v1/event-links", {
    method: "POST",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(`event_link_${payload.source_event_id}`, options)
    },
    body: JSON.stringify(payload)
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function patchEventLink(
  eventLinkId: string,
  payload: EventLinkPayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(`/v1/event-links/${encodeURIComponent(eventLinkId)}`, {
    method: "PATCH",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(`event_link_patch_${eventLinkId}`, options)
    },
    body: JSON.stringify(payload)
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function getEventLink(
  eventLinkId: string
): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(`/v1/event-links/${encodeURIComponent(eventLinkId)}`);
}

export type HotwordErrorType =
  | "missing_term"
  | "misrecognition"
  | "alias_gap"
  | "weight_issue"
  | "false_boost";

export type HotwordStatisticsMetrics = {
  coverage_rate: number;
  recall_rate: number;
  error_rate: number;
  false_boost_rate: number;
  impacted_sessions: number;
};

export type HotwordStatisticsItem = {
  badcase_id: string;
  canonical_term?: string;
  standard_term?: string;
  recognized_text: string;
  error_type: HotwordErrorType;
  expected_count: number;
  correct_count?: number;
  error_count?: number;
  weighted_error_count?: number;
  error_rate: number;
  manual_correction_count?: number;
  annotation_correction_count?: number;
  data_source?: "metric_snapshot" | "listening_annotation" | "mixed";
  source_counts?: { metric_snapshot?: number; listening_annotation?: number };
  eligible_for_release_gate?: boolean;
  evidence_level: string;
  candidate_state?: "confirmed" | "suspected";
  downstream_impact?: string | Record<string, unknown>;
  priority?: number;
  priority_score?: number;
  evidence_ref?: string;
  evidence_storage_object_id?: string;
  hotword_pack_version_id?: string;
  root_trace_id?: string;
  [key: string]: unknown;
};

export type HotwordStatistics = {
  metrics: HotwordStatisticsMetrics;
  items: HotwordStatisticsItem[];
  discovery: {
    annotation_correction_count: number;
    unique_terms: number;
    impacted_session_count: number;
    threshold_met_term_count: number;
    evidence_level: string;
    eligible_for_release_gate: boolean;
  };
  filters?: Record<string, unknown>;
  [key: string]: unknown;
};

export type HotwordStatisticsQuery = {
  date_from?: string;
  date_to?: string;
  store_id?: string;
  provider?: string;
  model_version?: string;
  hotword_pack_version_id?: string;
};

export type HotwordBadcaseCreatePayload = {
  badcase_id?: string;
  capability: "asr-hotword";
  standard_term: string;
  recognized_text: string;
  error_type: HotwordErrorType;
  evidence_storage_object_id: string;
  evidence_ref?: string;
  evidence_level: "discovery";
  hotword_pack_version_id: string;
  expected_count: number;
  correct_count: number;
  weighted_error_count: number;
  manual_correction_count: 0;
  business_weight: number;
  downstream_impact?: Record<string, unknown>;
  root_cause?: string;
  fix_suggestion?: string;
};

export type HotwordVersionItemPayload = {
  canonical_term: string;
  aliases: string[];
  category: string;
  weight: number;
  source_badcase_id?: string;
};

export type HotwordEvalRunPayload = {
  eval_dataset_id: string;
  provider: string;
  expected_resource_version: number;
};

export type HotwordAnalysisRunPayload = {
  date_from?: string;
  date_to?: string;
  store_id?: string;
  provider?: string;
  model_version?: string;
  hotword_pack_version_id?: string;
};

type HotwordStatisticsFinalResponse = {
  summary: {
    coverage_rate: number | null;
    recall_rate: number | null;
    error_rate: number | null;
    false_boost_rate: number | null;
    impacted_session_count: number;
    trusted_expected_count?: number;
    correct_hit_count?: number;
    weighted_error_count?: number;
    recognized_hotword_count?: number;
    false_insertion_count?: number;
  };
  discovery_summary: {
    annotation_correction_count: number;
    unique_terms: number;
    impacted_session_count: number;
    threshold_met_term_count: number;
    evidence_level: string;
    eligible_for_release_gate: boolean;
  };
  items: Array<{
    standard_term: string;
    recognized_forms?: string[];
    error_type: HotwordErrorType;
    expected_count: number;
    human_correction_count?: number;
    annotation_correction_count?: number;
    source_counts?: { metric_snapshot?: number; listening_annotation?: number };
    error_rate: number;
    evidence_level: string;
    evidence_confidence?: number;
    business_weight?: number;
    priority?: number;
    suspected?: boolean;
    impacted_session_count?: number;
    badcase_ids?: string[];
    root_trace_id?: string;
  }>;
  discovery_items: Array<{
    standard_term: string;
    recognized_forms?: string[];
    error_type: HotwordErrorType;
    annotation_correction_count: number;
    human_correction_count: number;
    evidence_level: string;
    evidence_confidence: number;
    candidate_state: "suspected";
    threshold_met: boolean;
    suspected: true;
    eligible_for_release_gate: false;
    priority: number;
    impacted_session_count: number;
    source_counts: { metric_snapshot: number; listening_annotation: number };
    badcase_ids: string[];
    correction_ids: string[];
    root_trace_id?: string;
  }>;
  dimensions: Record<string, unknown>;
};

const requiredHotwordNumber = (value: unknown, field: string) => {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`热词统计响应缺少有效字段 ${field}`);
  }
  return value;
};

const hotwordRatioPercent = (value: unknown, field: string) => {
  const numeric = requiredHotwordNumber(value, field);
  if (numeric < 0 || numeric > 1) throw new Error(`热词统计字段 ${field} 必须为 0–1 比率`);
  return Math.round(numeric * 1000) / 10;
};

const optionalHotwordRatioPercent = (value: unknown, field: string) =>
  value === null ? Number.NaN : hotwordRatioPercent(value, field);

const normalizedHotwordStatistics = (
  data: HotwordStatisticsFinalResponse
): HotwordStatistics => {
  const finalProjection = data as HotwordStatisticsFinalResponse;
  if (
    !finalProjection.summary ||
    !finalProjection.discovery_summary ||
    !Array.isArray(finalProjection.items) ||
    !Array.isArray(finalProjection.discovery_items) ||
    !finalProjection.dimensions ||
    typeof finalProjection.dimensions !== "object" ||
    Array.isArray(finalProjection.dimensions)
  ) {
    throw new Error("热词统计响应必须使用 summary/discovery_summary/items/discovery_items/dimensions 契约");
  }
  const summary = finalProjection.summary;
  const trustedItems: HotwordStatisticsItem[] = finalProjection.items.map((item, index) => {
    const expectedCount = requiredHotwordNumber(item.expected_count, `items[${index}].expected_count`);
    const errorRate = hotwordRatioPercent(item.error_rate, `items[${index}].error_rate`);
    const badcaseId = item.badcase_ids?.find((id) => typeof id === "string" && id.length > 0);
    const evidenceConfidence = typeof item.evidence_confidence === "number"
      ? ` ${Math.round(item.evidence_confidence * 100) / 100}`
      : "";
    const annotationCorrectionCount = typeof item.annotation_correction_count === "number"
      ? item.annotation_correction_count
      : 0;
    return {
      badcase_id: badcaseId ?? `stat-term-${index + 1}`,
      standard_term: item.standard_term,
      canonical_term: item.standard_term,
      recognized_text: item.recognized_forms?.filter(Boolean).join(" / ") || "待关联 ASR Badcase",
      error_type: item.error_type,
      expected_count: expectedCount,
      weighted_error_count: Math.round(expectedCount * (errorRate / 100) * 1000) / 1000,
      error_rate: errorRate,
      manual_correction_count: item.human_correction_count,
      annotation_correction_count: annotationCorrectionCount,
      data_source: annotationCorrectionCount > 0 ? "mixed" : "metric_snapshot",
      source_counts: item.source_counts,
      evidence_level: `${item.evidence_level}${evidenceConfidence}`,
      candidate_state: item.suspected ? "suspected" : "confirmed",
      downstream_impact: typeof item.impacted_session_count === "number"
        ? `${item.impacted_session_count} 个影响会话`
        : "待下钻 Badcase 确认",
      priority: item.priority,
      priority_score: item.priority,
      root_trace_id: item.root_trace_id,
      business_weight: item.business_weight,
      badcase_ids: item.badcase_ids
    };
  });
  const trustedTerms = new Set(
    trustedItems.map((item) => String(item.standard_term ?? item.canonical_term ?? "").normalize("NFKC").toLocaleLowerCase())
  );
  const annotationOnlyItems: HotwordStatisticsItem[] = finalProjection.discovery_items
    .filter((item) => !trustedTerms.has(item.standard_term.normalize("NFKC").toLocaleLowerCase()))
    .map((item, index) => ({
      badcase_id: item.badcase_ids[0] ?? item.correction_ids[0] ?? `annotation-term-${index + 1}`,
      standard_term: item.standard_term,
      canonical_term: item.standard_term,
      recognized_text: item.recognized_forms?.filter(Boolean).join(" / ") || "受限证据",
      error_type: item.error_type,
      expected_count: 0,
      weighted_error_count: 0,
      error_rate: Number.NaN,
      manual_correction_count: item.human_correction_count,
      annotation_correction_count: item.annotation_correction_count,
      data_source: "listening_annotation",
      source_counts: item.source_counts,
      evidence_level: `${item.evidence_level} ${Math.round(item.evidence_confidence * 100) / 100}`,
      candidate_state: "suspected",
      downstream_impact: `${item.impacted_session_count} 个标注影响会话`,
      priority: item.priority,
      priority_score: item.priority,
      root_trace_id: item.root_trace_id,
      badcase_ids: item.badcase_ids,
      correction_ids: item.correction_ids,
      threshold_met: item.threshold_met,
      eligible_for_release_gate: false
    }));
  return {
    metrics: {
      coverage_rate: optionalHotwordRatioPercent(summary.coverage_rate, "summary.coverage_rate"),
      recall_rate: optionalHotwordRatioPercent(summary.recall_rate, "summary.recall_rate"),
      error_rate: optionalHotwordRatioPercent(summary.error_rate, "summary.error_rate"),
      false_boost_rate: optionalHotwordRatioPercent(summary.false_boost_rate, "summary.false_boost_rate"),
      impacted_sessions: requiredHotwordNumber(summary.impacted_session_count, "summary.impacted_session_count")
    },
    items: [...trustedItems, ...annotationOnlyItems],
    discovery: {
      annotation_correction_count: requiredHotwordNumber(
        finalProjection.discovery_summary.annotation_correction_count,
        "discovery_summary.annotation_correction_count"
      ),
      unique_terms: requiredHotwordNumber(
        finalProjection.discovery_summary.unique_terms,
        "discovery_summary.unique_terms"
      ),
      impacted_session_count: requiredHotwordNumber(
        finalProjection.discovery_summary.impacted_session_count,
        "discovery_summary.impacted_session_count"
      ),
      threshold_met_term_count: requiredHotwordNumber(
        finalProjection.discovery_summary.threshold_met_term_count,
        "discovery_summary.threshold_met_term_count"
      ),
      evidence_level: finalProjection.discovery_summary.evidence_level,
      eligible_for_release_gate: finalProjection.discovery_summary.eligible_for_release_gate
    },
    filters: finalProjection.dimensions,
    summary
  };
};

export async function getHotwordStatistics(
  query: HotwordStatisticsQuery = {}
): Promise<ApiEnvelope<HotwordStatistics>> {
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  const suffix = params.size ? `?${params.toString()}` : "";
  const response = await apiRequest<HotwordStatisticsFinalResponse>(
    `/v1/hotword-statistics${suffix}`
  );
  return { ...response, data: normalizedHotwordStatistics(response.data) };
}

export async function listHotwordBadcases(query: {
  error_type?: HotwordErrorType;
  status?: string;
  hotword_pack_version_id?: string;
  limit?: number;
} = {}): Promise<ApiEnvelope<{ items: HotwordStatisticsItem[] }>> {
  const params = new URLSearchParams({ capability: "asr-hotword" });
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  return apiRequest<{ items: HotwordStatisticsItem[] }>(`/v1/badcases?${params.toString()}`);
}

export async function createHotwordBadcase(
  payload: HotwordBadcaseCreatePayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction("/v1/badcases", "hotword_badcase_create", payload as Record<string, unknown>, options);
}

export async function patchHotwordBadcase(
  badcaseId: string,
  payload: {
    expected_resource_version: number;
    status?: "pending-attribution" | "pending-review" | "pending-backflow" | "in-regression" | "rejected";
    root_cause?: string;
    fix_suggestion?: string;
    downstream_impact?: Record<string, unknown>;
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(`/v1/badcases/${encodeURIComponent(badcaseId)}`, {
    method: "PATCH",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(`hotword_badcase_patch_${badcaseId}`, options)
    },
    body: JSON.stringify(payload)
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function decideHotwordBadcase(
  badcaseId: string,
  payload: {
    decision: "confirmed" | "rejected" | "needs-evidence";
    reason: string;
    expected_resource_version: number;
  },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/badcases/${encodeURIComponent(badcaseId)}/decisions`,
    `hotword_badcase_decision_${badcaseId}`,
    payload,
    options
  );
}

export async function listHotwordPacks(): Promise<ApiEnvelope<{ items: Array<Record<string, unknown>> }>> {
  return apiRequest<{ items: Array<Record<string, unknown>> }>("/v1/hotword-packs");
}

export async function createHotwordPack(
  payload: { name: string; language: string; domain: string },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction("/v1/hotword-packs", "hotword_pack_create", payload, options);
}

export async function createHotwordPackVersion(
  packId: string,
  payload: { version: string; baseline_version_id?: string | null },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/hotword-packs/${encodeURIComponent(packId)}/versions`,
    `hotword_pack_version_create_${packId}`,
    payload,
    options
  );
}

export async function listHotwordPackVersions(
  packId: string,
  query: { status?: string; limit?: number } = {}
): Promise<ApiEnvelope<{ items: Array<Record<string, unknown>> }>> {
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  const suffix = params.size ? `?${params.toString()}` : "";
  return apiRequest<{ items: Array<Record<string, unknown>> }>(
    `/v1/hotword-packs/${encodeURIComponent(packId)}/versions${suffix}`
  );
}

export async function getHotwordPackVersion(versionId: string): Promise<ApiEnvelope<Record<string, unknown>>> {
  return apiRequest<Record<string, unknown>>(`/v1/hotword-pack-versions/${encodeURIComponent(versionId)}`);
}

export async function patchHotwordPackVersion(
  versionId: string,
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(`/v1/hotword-pack-versions/${encodeURIComponent(versionId)}`, {
    method: "PATCH",
    headers: {
      "Idempotency-Key": writeIdempotencyKey(`hotword_pack_version_patch_${versionId}`, options)
    },
    body: JSON.stringify(payload)
  });
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function addHotwordVersionItem(
  versionId: string,
  payload: HotwordVersionItemPayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/hotword-pack-versions/${encodeURIComponent(versionId)}/items`,
    `hotword_version_item_create_${versionId}_${payload.canonical_term}`,
    payload as Record<string, unknown>,
    options
  );
}

export async function patchHotwordVersionItem(
  versionId: string,
  itemId: string,
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(
    `/v1/hotword-pack-versions/${encodeURIComponent(versionId)}/items/${encodeURIComponent(itemId)}`,
    {
      method: "PATCH",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(`hotword_version_item_patch_${itemId}`, options)
      },
      body: JSON.stringify(payload)
    }
  );
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function deleteHotwordVersionItem(
  versionId: string,
  itemId: string,
  expectedResourceVersion: number,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(
    `/v1/hotword-pack-versions/${encodeURIComponent(versionId)}/items/${encodeURIComponent(itemId)}?expected_resource_version=${expectedResourceVersion}`,
    {
      method: "DELETE",
      headers: {
        "Idempotency-Key": writeIdempotencyKey(`hotword_version_item_delete_${itemId}`, options)
      }
    }
  );
  return { ...response, data: normalizeActionReceipt(response.data, response.meta?.trace_id) };
}

export async function createHotwordAnalysisRun(
  payload: HotwordAnalysisRunPayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    "/v1/hotword-analysis-runs",
    "hotword_analysis_run",
    payload as Record<string, unknown>,
    options
  );
}

export async function createHotwordEvalRun(
  versionId: string,
  payload: HotwordEvalRunPayload,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/hotword-pack-versions/${encodeURIComponent(versionId)}/eval-runs`,
    `hotword_eval_run_${versionId}`,
    payload as unknown as Record<string, unknown>,
    options
  );
}

export async function publishHotwordPackVersion(
  versionId: string,
  payload: { expected_resource_version: number; eval_run_id: string; confirmation: "publish" },
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  return createBackendAction(
    `/v1/hotword-pack-versions/${encodeURIComponent(versionId)}/publish`,
    `hotword_publish_${versionId}`,
    payload,
    options
  );
}
