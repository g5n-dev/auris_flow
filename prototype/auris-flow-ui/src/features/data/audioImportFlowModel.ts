export type AudioImportFieldMapping = {
  externalRecordId: string;
  audioUrl: string;
  startedAt: string;
  agentRef: string;
  storeRef: string;
  deviceRef: string;
  durationMs: string;
};
export type AudioImportDraft = {
  name: string;
  platformConnectionId: string;
  platformTenantKey: string;
  storeScope: string;
  baseUrl: string;
  requestPath: string;
  credentialRef: string;
  paginationMode: "cursor";
  pageSize: number;
  cursorParam: string;
  nextCursorPath: string;
  fieldMapping: AudioImportFieldMapping;
  cursorField: string;
  initialWindowStart: string;
  targetAssetKey: string;
  dedupePolicy: "external_id_checksum";
};
export type AudioImportPlatformBinding = {
  id: string;
  status: string;
  externalTenantRef: string;
  storeRefs: string[];
  origin: string;
  credentialRef: string;
  testPath?: string;
};
export type AudioImportVerification = {
  testedFingerprint: string;
  previewedFingerprint: string;
  mappingValid: boolean;
  mappingErrors: string[];
};
export type AudioImportBatchStatus =
  | "queued" | "running" | "materializing" | "partial"
  | "succeeded" | "failed" | "cancelled";
export type AudioImportCurrentStage =
  | "queued" | "listing" | "downloading" | "verifying" | "materializing" | "completed";
export type AudioImportBatch = {
  id: string;
  taskRunId: string;
  status: AudioImportBatchStatus;
  currentStage: AudioImportCurrentStage;
  total: number;
  succeeded: number;
  duplicates: number;
  failed: number;
  rootTraceId: string;
  createdAudioSessionIds: string[];
  errorCode: string;
  errorReason: string;
  recoverySuggestion: string;
  retryable: boolean;
  retryLineage: {
    sourceTaskRunId: string;
    sourceBatchId: string;
    rootTaskRunId: string;
    rootBatchId: string;
    attempt: number;
  };
};
export type AudioImportBatchItem = {
  id: string;
  externalRecordId: string;
  status: string;
  errorCode: string;
  objectVersion: string;
  audioSessionId: string;
  rootTraceId: string;
  recoverySuggestion: string;
  retryable: boolean;
  retryLineage: {
    sourceBatchId: string;
    sourceItemId: string;
    rootBatchId: string;
    rootItemId: string;
    attempt: number;
  };
};
export type AudioImportStepStatus =
  | "unvisited"
  | "incomplete"
  | "verified"
  | "error";
export type AudioImportStepState = {
  id: number;
  status: AudioImportStepStatus;
  errors: string[];
};

export const AUDIO_IMPORT_STEPS = [
  "关联平台", "配置接口", "测试与预览", "字段映射", "游标与目标", "发布与拉取"
].map((label, index) => ({ id: index + 1, label }));
export const AUDIO_IMPORT_STEP_STATUS_LABELS: Record<AudioImportStepStatus, string> = {
  unvisited: "未访问",
  incomplete: "未完成",
  verified: "已验证",
  error: "有错误"
};
const DEFAULT_INITIAL_WINDOW_DAYS = 7;

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
const text = (value: unknown) => typeof value === "string" ? value.trim() : "";
const number = (value: unknown) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
};
const first = (source: Record<string, unknown>, ...keys: string[]) => {
  for (const key of keys) if (source[key] !== undefined) return source[key];
};

export function defaultAudioImportDraft(targetAssetKey: string): AudioImportDraft {
  const start = new Date(
    Date.now() - DEFAULT_INITIAL_WINDOW_DAYS * 86_400_000
  );
  start.setMinutes(start.getMinutes() - start.getTimezoneOffset());
  return {
    name: "平台音频 URL 导入",
    platformConnectionId: "",
    platformTenantKey: "",
    storeScope: "",
    baseUrl: "",
    requestPath: "/v1/recordings",
    credentialRef: "",
    paginationMode: "cursor",
    pageSize: 100,
    cursorParam: "cursor",
    nextCursorPath: "paging.next_cursor",
    fieldMapping: {
      externalRecordId: "recording_id",
      audioUrl: "audio_url",
      startedAt: "started_at",
      agentRef: "agent_id",
      storeRef: "store_id",
      deviceRef: "device_id",
      durationMs: "duration_ms"
    },
    cursorField: "updated_at",
    initialWindowStart: start.toISOString().slice(0, 16),
    targetAssetKey,
    dedupePolicy: "external_id_checksum"
  };
}

const normalizedStoreRefs = (value: string) =>
  value.split(",").map(text).filter(Boolean);
const normalizedOrigin = (value: string) =>
  value.trim().replace(/\/+$/, "");

export function bindAudioImportDraftToPlatformConnection(
  draft: AudioImportDraft,
  connection: AudioImportPlatformBinding
): AudioImportDraft {
  const testPath = text(connection.testPath);
  return {
    ...draft,
    platformConnectionId: text(connection.id),
    platformTenantKey: text(connection.externalTenantRef),
    storeScope: connection.storeRefs.map(text).filter(Boolean).join(","),
    baseUrl: normalizedOrigin(connection.origin),
    credentialRef: text(connection.credentialRef),
    requestPath: text(draft.requestPath) || testPath || "/"
  };
}

export function validateAudioImportPlatformBinding(
  draft: AudioImportDraft,
  connection: AudioImportPlatformBinding
) {
  const errors: string[] = [];
  if (text(connection.status).toLowerCase() !== "active") {
    errors.push("所选平台连接尚未验证为可用状态");
  }
  if (text(draft.platformConnectionId) !== text(connection.id)) {
    errors.push("平台连接 ID 与冻结连接不一致");
  }
  if (text(draft.platformTenantKey) !== text(connection.externalTenantRef)) {
    errors.push("平台租户标识与所选连接不一致");
  }
  const allowedStoreRefs = new Set(connection.storeRefs.map(text).filter(Boolean));
  if (
    allowedStoreRefs.size
    && normalizedStoreRefs(draft.storeScope).some((item) => !allowedStoreRefs.has(item))
  ) {
    errors.push("门店范围超出所选连接授权范围");
  }
  if (normalizedOrigin(draft.baseUrl) !== normalizedOrigin(connection.origin)) {
    errors.push("平台 API 地址与所选连接不一致");
  }
  if (text(draft.credentialRef) !== text(connection.credentialRef)) {
    errors.push("credential_ref 与所选连接不一致");
  }
  return errors;
}

export function configurationFingerprint(draft: AudioImportDraft) {
  const value = JSON.stringify(
    draft,
    (_key, item) => typeof item === "string" ? item.trim() : item
  );
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash = Math.imul(hash ^ value.charCodeAt(index), 16777619);
  }
  return `audio-import-${(hash >>> 0).toString(36)}`;
}

export function isConfigurationVerified(
  draft: AudioImportDraft,
  verification: AudioImportVerification
) {
  const fingerprint = configurationFingerprint(draft);
  return verification.testedFingerprint === fingerprint
    && verification.previewedFingerprint === fingerprint
    && verification.mappingValid;
}

export function validateAudioImportStep(
  step: number,
  draft: AudioImportDraft,
  verification: AudioImportVerification
) {
  const errors: string[] = [];
  const requireText = (value: string, message: string) => {
    if (!value.trim()) errors.push(message);
  };
  if (step === 1) {
    requireText(draft.platformConnectionId, "请选择已存在的平台连接");
    requireText(draft.platformTenantKey, "请填写平台租户标识");
  } else if (step === 2) {
    requireText(draft.name, "请填写配置名称");
    requireText(draft.baseUrl, "请填写平台 API 地址");
    if (draft.baseUrl.trim()) {
      try {
        const url = new URL(draft.baseUrl.trim());
        if (url.protocol !== "https:" || url.pathname !== "/" || url.search || url.hash) {
          errors.push("API 地址必须是 HTTPS origin");
        }
      } catch {
        errors.push("API 地址格式无效");
      }
    }
    requireText(draft.requestPath, "请填写录音清单请求路径");
    if (draft.requestPath.trim() && !/^\/(?!\/)/.test(draft.requestPath.trim())) {
      errors.push("录音清单路径必须以单个 / 开头");
    }
    requireText(draft.credentialRef, "请填写 credential_ref");
    if (!Number.isInteger(draft.pageSize) || draft.pageSize < 1 || draft.pageSize > 250) {
      errors.push("分页大小必须是 1 到 250 的整数");
    }
    requireText(draft.cursorParam, "请填写持久增量游标参数名");
    requireText(draft.nextCursorPath, "请填写下一持久游标字段路径");
  } else if (step === 3) {
    const fingerprint = configurationFingerprint(draft);
    if (verification.testedFingerprint !== fingerprint) errors.push("请完成真实连通性测试");
    if (verification.previewedFingerprint !== fingerprint) errors.push("请预览真实源记录");
  } else if (step === 4) {
    requireText(draft.fieldMapping.externalRecordId, "请映射外部录音 ID");
    requireText(draft.fieldMapping.audioUrl, "请映射音频 URL");
    requireText(draft.fieldMapping.startedAt, "请映射通话时间");
    if (draft.storeScope.trim() && !draft.fieldMapping.storeRef.trim()) {
      errors.push("配置门店范围后必须映射门店 ID");
    }
    const fingerprint = configurationFingerprint(draft);
    const recordMappingErrors = verification.mappingErrors.filter(
      (error) => !error.toLowerCase().includes("cursor")
    );
    if (
      verification.previewedFingerprint === fingerprint
      && !verification.mappingValid
      && (recordMappingErrors.length || !verification.mappingErrors.length)
    ) {
      errors.push(`真实预览字段映射未通过：${
        recordMappingErrors.join("、") || "请检查必填字段"
      }`);
    }
  } else if (step === 5) {
    requireText(draft.cursorField, "请填写唯一递增游标字段");
    if (!draft.initialWindowStart) errors.push("请设置首次拉取开始时间");
    else if (Number.isNaN(Date.parse(draft.initialWindowStart))) {
      errors.push("首次拉取开始时间格式无效");
    }
    requireText(draft.targetAssetKey, "请选择目标音频资产");
    const fingerprint = configurationFingerprint(draft);
    const cursorMappingErrors = verification.mappingErrors.filter(
      (error) => error.toLowerCase().includes("cursor")
    );
    if (
      verification.previewedFingerprint === fingerprint
      && !verification.mappingValid
      && cursorMappingErrors.length
    ) {
      errors.push(`真实预览游标映射未通过：${cursorMappingErrors.join("、")}`);
    }
  }
  return errors;
}

export function validateCompleteAudioImport(
  draft: AudioImportDraft,
  verification: AudioImportVerification
) {
  const errors = [1, 2, 3, 4, 5].flatMap(
    (step) => validateAudioImportStep(step, draft, verification)
  );
  return Array.from(new Set(errors));
}

export function buildAudioImportStepStates(input: {
  draft: AudioImportDraft;
  verification: AudioImportVerification;
  visitedSteps: number[];
  attemptedSteps: number[];
  releaseVerified: boolean;
}): AudioImportStepState[] {
  const visited = new Set(input.visitedSteps);
  const attempted = new Set(input.attemptedSteps);
  return AUDIO_IMPORT_STEPS.map(({ id }) => {
    if (!visited.has(id)) return { id, status: "unvisited", errors: [] };
    const errors = id === 6
      ? validateCompleteAudioImport(input.draft, input.verification)
      : validateAudioImportStep(id, input.draft, input.verification);
    const status: AudioImportStepStatus = id === 6 && input.releaseVerified
      ? "verified"
      : errors.length
        ? attempted.has(id) ? "error" : "incomplete"
        : id === 6 ? "incomplete" : "verified";
    return { id, status, errors };
  });
}

export function canNavigateToAudioImportStep(
  targetStep: number,
  visitedSteps: number[],
  stepStates: AudioImportStepState[]
) {
  if (!visitedSteps.includes(targetStep)) return false;
  const earliestIncomplete = stepStates.find(
    (item) => item.status !== "verified"
  )?.id ?? AUDIO_IMPORT_STEPS.length;
  return targetStep <= earliestIncomplete;
}

const audioImportErrorFields: Array<[string, string]> = [
  ["所选平台连接", "audio-import-platform-connection"],
  ["平台连接 ID", "audio-import-platform-connection"],
  ["请选择已存在的平台连接", "audio-import-platform-connection"],
  ["请填写平台租户标识", "audio-import-platform-tenant"],
  ["平台租户标识与", "audio-import-platform-tenant"],
  ["门店范围超出", "audio-import-store-scope"],
  ["请填写配置名称", "audio-import-config-name"],
  ["平台 API 地址", "audio-import-base-url"],
  ["API 地址", "audio-import-base-url"],
  ["录音清单请求路径", "audio-import-request-path"],
  ["录音清单路径", "audio-import-request-path"],
  ["credential_ref", "audio-import-credential-ref"],
  ["分页大小", "audio-import-page-size"],
  ["增量游标参数名", "audio-import-cursor-param"],
  ["下一持久游标字段路径", "audio-import-next-cursor-path"],
  ["真实连通性测试", "audio-import-test-connection"],
  ["预览真实源记录", "audio-import-preview-records"],
  ["audio_url", "audio-import-map-audio-url"],
  ["external_record_id", "audio-import-map-external-record-id"],
  ["started_at", "audio-import-map-started-at"],
  ["cursor_policy.field", "audio-import-cursor-field"],
  ["外部录音 ID", "audio-import-map-external-record-id"],
  ["音频 URL", "audio-import-map-audio-url"],
  ["通话时间", "audio-import-map-started-at"],
  ["门店 ID", "audio-import-map-store-ref"],
  ["唯一递增游标字段", "audio-import-cursor-field"],
  ["首次拉取开始时间", "audio-import-initial-window"],
  ["目标音频资产", "audio-import-target-asset"]
];

export function firstAudioImportErrorFieldId(errors: string[]) {
  for (const error of errors) {
    const match = audioImportErrorFields.find(([message]) => error.includes(message));
    if (match) return match[1];
  }
  return "";
}

export function audioImportErrorFieldIds(errors: string[]) {
  return Array.from(new Set(errors.map((error) =>
    firstAudioImportErrorFieldId([error])
  ).filter(Boolean)));
}

export function latestAudioImportBatchIdFromBatches(value: unknown) {
  const source = Array.isArray(value)
    ? value
    : Array.isArray(asRecord(value).items) ? asRecord(value).items as unknown[] : [];
  const matches = source.flatMap((item, index) => {
    const batch = asRecord(item);
    const importBatchId = text(batch.import_batch_id) || text(batch.id);
    if (!importBatchId) return [];
    const timestamp = Date.parse(
      text(batch.created_at) || text(batch.started_at) || text(batch.updated_at)
    );
    return [{
      id: importBatchId,
      timestamp: Number.isFinite(timestamp) ? timestamp : 0,
      index
    }];
  });
  matches.sort((left, right) =>
    right.timestamp - left.timestamp || left.index - right.index
  );
  return matches[0]?.id ?? "";
}

export function buildConnectorPayload(
  draft: AudioImportDraft,
  sceneProfileLock: Record<string, string>,
  connectorId?: string
) {
  const start = new Date(draft.initialWindowStart);
  if (Number.isNaN(start.getTime())) throw new Error("首次拉取开始时间格式无效");
  const mapping = [
    ["external_record_id", draft.fieldMapping.externalRecordId],
    ["audio_url", draft.fieldMapping.audioUrl],
    ["started_at", draft.fieldMapping.startedAt],
    ["agent_ref", draft.fieldMapping.agentRef],
    ["store_ref", draft.fieldMapping.storeRef],
    ["device_ref", draft.fieldMapping.deviceRef],
    ["duration_ms", draft.fieldMapping.durationMs]
  ];
  return {
    ...(connectorId ? { connector_id: connectorId } : {}),
    name: draft.name.trim(),
    source_type: "platform_audio_url_api",
    platform_connection_id: draft.platformConnectionId.trim(),
    platform_scope: {
      tenant_ref: draft.platformTenantKey.trim(),
      store_refs: draft.storeScope.split(",").map(text).filter(Boolean)
    },
    base_url: draft.baseUrl.trim(),
    request_path: draft.requestPath.trim(),
    credential_ref: draft.credentialRef.trim(),
    pagination: {
      mode: draft.paginationMode,
      page_size: draft.pageSize,
      cursor_param: draft.cursorParam.trim(),
      next_cursor_path: draft.nextCursorPath.trim()
    },
    field_mapping: Object.fromEntries(
      mapping.map(([key, value]) => [key, value.trim()]).filter(([, value]) => value)
    ),
    cursor_policy: {
      field: draft.cursorField.trim(),
      initial_window_start: start.toISOString()
    },
    target_asset_key: draft.targetAssetKey.trim(),
    dedupe_policy: draft.dedupePolicy,
    ...sceneProfileLock
  };
}

export function buildAudioImportTaskVersionPayload(input: {
  draft: AudioImportDraft;
  connectorId: string;
  connectorVersion: number;
  sceneProfileLock: Record<string, string>;
}) {
  const { draft, connectorId, connectorVersion, sceneProfileLock } = input;
  const binding = buildConnectorPayload(draft, {}, connectorId);
  return {
    name: draft.name.trim(),
    status: "draft",
    task_type_id: "audio-platform-import",
    flow_template: "audio-platform-import",
    connector_id: connectorId,
    input_binding: {
      connector_id: connectorId,
      connector_version: String(connectorVersion),
      ...binding
    },
    payload: {
      connector_id: connectorId,
      connector_version: String(connectorVersion),
      input_binding: binding
    },
    ...sceneProfileLock
  };
}

export const buildAudioImportTaskRunPayload = (taskVersionId: string) => ({
  task_version_id: taskVersionId,
  trigger_type: "manual" as const,
  execution_mode: "production" as const
});

export function importBatchIdFromTaskRun(receipt: { raw?: Record<string, unknown> }) {
  const raw = asRecord(receipt.raw);
  return text(raw.import_batch_id) || text(asRecord(raw.payload).import_batch_id);
}

export function normalizeImportBatchStatus(value: unknown): AudioImportBatchStatus {
  const status = text(value).toLowerCase();
  const aliases: Record<string, AudioImportBatchStatus> = {
    success: "succeeded",
    completed: "succeeded",
    partially_succeeded: "partial",
    error: "failed",
    canceled: "cancelled",
    completion_pending: "materializing",
    writeback_pending: "materializing",
    submitted: "running",
    dispatching: "running",
    downloading: "running"
  };
  return aliases[status]
    || (["succeeded", "partial", "failed", "cancelled", "materializing", "running"].includes(status)
      ? status as AudioImportBatchStatus : "queued");
}

export function normalizeImportCurrentStage(
  value: unknown,
  status: AudioImportBatchStatus
): AudioImportCurrentStage {
  const stage = text(value).toLowerCase();
  if (["listing", "downloading", "verifying", "materializing", "completed"].includes(stage)) {
    return stage as AudioImportCurrentStage;
  }
  if (status === "materializing") return "materializing";
  if (status === "succeeded" || status === "partial") return "completed";
  return status === "running" ? "listing" : "queued";
}

export function normalizeImportBatch(value: unknown): AudioImportBatch {
  const raw = asRecord(value);
  const summary = asRecord(raw.summary);
  const retryLineage = asRecord(raw.retry_lineage);
  const status = normalizeImportBatchStatus(raw.status ?? raw.business_status);
  const sessions = raw.audio_session_ids
    ?? raw.created_audio_session_ids
    ?? summary.audio_session_ids
    ?? summary.created_audio_session_ids;
  const count = (...keys: string[]) => number(first(raw, ...keys) ?? first(summary, ...keys));
  return {
    id: text(first(raw, "id", "batch_id", "import_batch_id")),
    taskRunId: text(first(raw, "task_run_id", "run_id")),
    status,
    currentStage: normalizeImportCurrentStage(raw.current_stage, status),
    total: count("total_items", "total", "total_count"),
    succeeded: count("succeeded_items", "succeeded", "succeeded_count", "imported_count"),
    duplicates: count("skipped_items", "duplicates", "duplicate_count", "skipped_count"),
    failed: count("failed_items", "failed", "failed_count"),
    rootTraceId: text(first(raw, "root_trace_id", "trace_id")),
    createdAudioSessionIds: Array.isArray(sessions) ? sessions.map(text).filter(Boolean) : [],
    errorCode: text(raw.error_code ?? summary.error_code).slice(0, 128),
    errorReason: text(first(raw, "reason", "error_reason") ?? summary.reason).slice(0, 500),
    recoverySuggestion: text(raw.recovery_suggestion ?? summary.recovery_suggestion).slice(0, 500),
    retryable: raw.retryable === true,
    retryLineage: {
      sourceTaskRunId: text(retryLineage.source_task_run_id),
      sourceBatchId: text(retryLineage.source_import_batch_id),
      rootTaskRunId: text(retryLineage.root_task_run_id),
      rootBatchId: text(retryLineage.root_import_batch_id),
      attempt: number(retryLineage.attempt) || 1
    }
  };
}

export function normalizeImportBatchItems(value: unknown): AudioImportBatchItem[] {
  const raw = asRecord(value);
  const items = Array.isArray(value) ? value : Array.isArray(raw.items) ? raw.items : [];
  return items.map((value, index) => {
    const item = asRecord(value);
    const retryLineage = asRecord(item.retry_lineage);
    return {
      id: text(first(item, "import_item_id", "id", "item_id")) || `item-${index + 1}`,
      externalRecordId: text(first(item, "external_record_id", "source_record_id")),
      status: text(item.status) || "queued",
      errorCode: text(item.error_code),
      objectVersion: text(first(item, "object_version", "storage_object_version")),
      audioSessionId: text(item.audio_session_id),
      rootTraceId: text(first(item, "root_trace_id", "trace_id")),
      recoverySuggestion: text(item.recovery_suggestion).slice(0, 500),
      retryable: item.retryable === true,
      retryLineage: {
        sourceBatchId: text(retryLineage.source_import_batch_id),
        sourceItemId: text(retryLineage.source_import_item_id),
        rootBatchId: text(retryLineage.root_import_batch_id),
        rootItemId: text(retryLineage.root_import_item_id),
        attempt: number(retryLineage.attempt) || 1
      }
    };
  });
}

export const isImportBatchTerminal = (status: AudioImportBatchStatus) =>
  ["partial", "succeeded", "failed", "cancelled"].includes(status);
export const hasImportBatchFailures = (batch: AudioImportBatch, failedItems = 0) =>
  failedItems > 0 || batch.failed > 0 || ["failed", "partial"].includes(batch.status)
  || Boolean(batch.errorCode);
export const canRetryImportBatch = (batch: AudioImportBatch, failedItems = 0) =>
  Boolean(batch.taskRunId) && ["failed", "partial"].includes(batch.status)
  && hasImportBatchFailures(batch, failedItems) && batch.retryable;
