import {
  apiRequest,
  createBackendAction,
  createConnectorResource,
  getApiScopeKey,
  normalizeActionReceipt,
  type ApiEnvelope,
  type BackendActionReceipt,
  type ConnectorCreatePayload,
  type TaskRunCreatePayload,
  type WriteRequestOptions
} from "./client";
import { listAllCollectionItems } from "./taskVersionPagination";

export type AudioImportConnector = Record<string, unknown> & {
  id?: string;
  connector_id?: string;
  status?: string;
  version?: number;
  resource_version?: number;
  platform_connection_id?: string;
};

export type PlatformConnectionOption = Record<string, unknown> & {
  id: string;
  name: string;
  status: string;
  externalTenantRef: string;
  storeRefs: string[];
  origin: string;
  credentialRef: string;
  testPath: string;
};

export type AudioImportPreview = {
  records: Array<Record<string, unknown>>;
  fields: string[];
  mappingValid: boolean;
  mappingErrors: string[];
  traceId?: string;
};

const writeKey = (scope: string) =>
  `${scope}:intent:${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;

const latestBatchKey = (targetAssetKey: string) =>
  `auris-flow:audio-import:last-batch:${getApiScopeKey()}:${targetAssetKey}`;

export function rememberLatestAudioImportBatch(
  targetAssetKey: string,
  importBatchId: string
) {
  globalThis.sessionStorage?.setItem(latestBatchKey(targetAssetKey), importBatchId);
}

export function recalledLatestAudioImportBatch(targetAssetKey: string) {
  return globalThis.sessionStorage?.getItem(latestBatchKey(targetAssetKey)) ?? "";
}

const collectionItems = (value: unknown): Array<Record<string, unknown>> => {
  const items = Array.isArray(value)
    ? value
    : value && typeof value === "object"
      ? (value as Record<string, unknown>).items
      : [];
  return Array.isArray(items)
    ? items.filter((item): item is Record<string, unknown> =>
        Boolean(item) && typeof item === "object" && !Array.isArray(item)
      )
    : [];
};
const stringItems = (value: unknown) =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

export async function listAudioImportConnectors() {
  const { response, items } = await listAllCollectionItems(
    apiRequest,
    "/v1/connectors",
    "Connector",
    collectionItems
  );
  return {
    ...response,
    data: {
      items: items.filter(
        (item) => item.source_type === "platform_audio_url_api"
      ) as AudioImportConnector[]
    }
  };
}

export async function listPlatformConnections(): Promise<ApiEnvelope<{ items: PlatformConnectionOption[] }>> {
  const { response, items: records } = await listAllCollectionItems(
    apiRequest,
    "/v1/platform-connections",
    "平台连接",
    collectionItems
  );
  const items = records.map((item) => ({
    ...item,
    id: String(item.id ?? item.platform_connection_id ?? ""),
    name: String(item.name ?? item.display_name ?? item.id ?? "未命名平台连接"),
    status: String(item.status ?? "unknown").trim().toLowerCase(),
    externalTenantRef: String(item.external_tenant_ref ?? "").trim(),
    storeRefs: stringItems(item.store_refs).map((value) => value.trim()).filter(Boolean),
    origin: String(item.origin ?? "").trim().replace(/\/+$/, ""),
    credentialRef: String(item.credential_ref ?? "").trim(),
    testPath: String(item.test_path ?? "/").trim() || "/"
  })).filter((item) => item.id);
  return { ...response, data: { items } };
}

export async function listAudioImportBatches(input: {
  connectorId?: string;
  taskVersionId?: string;
  targetAssetKey: string;
}): Promise<ApiEnvelope<{ items: Array<Record<string, unknown>> }>> {
  const query = new URLSearchParams({
    target_asset_key: input.targetAssetKey,
    limit: "1"
  });
  if (input.connectorId) query.set("connector_id", input.connectorId);
  if (input.taskVersionId) query.set("task_version_id", input.taskVersionId);
  const response = await apiRequest<unknown>(
    `/v1/import-batches?${query.toString()}`
  );
  return {
    ...response,
    data: { items: collectionItems(response.data) }
  };
}

export function createAudioImportConnector(
  payload: ConnectorCreatePayload,
  options?: WriteRequestOptions
) {
  return createConnectorResource(payload, options);
}

export async function patchAudioImportConnector(
  connectorId: string,
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
): Promise<ApiEnvelope<BackendActionReceipt>> {
  const response = await apiRequest<Record<string, unknown>>(
    `/v1/connectors/${encodeURIComponent(connectorId)}`,
    {
      method: "PATCH",
      headers: {
        "Idempotency-Key": options?.idempotencyKey ?? writeKey(`connector_patch_${connectorId}`)
      },
      body: JSON.stringify(payload)
    }
  );
  return {
    ...response,
    data: normalizeActionReceipt(response.data, response.meta?.trace_id)
  };
}

export async function testAudioImportConnection(
  connectorId: string,
  payload: Record<string, unknown> = {}
) {
  return runConnectorAction(connectorId, "/connection-tests", payload);
}

export async function previewAudioImportRecords(
  connectorId: string,
  payload: Record<string, unknown> = {}
): Promise<ApiEnvelope<AudioImportPreview>> {
  const response = await runConnectorAction(
    connectorId,
    "/record-previews",
    { limit: 3, ...payload }
  );
  const records = collectionItems(response.data.records ?? response.data);
  const explicitFields = stringItems(response.data.fields);
  return {
    ...response,
    data: {
      records: records.slice(0, 3),
      fields: explicitFields.length
        ? explicitFields
        : Array.from(new Set(records.flatMap((record) => Object.keys(record)))),
      mappingValid: response.data.mapping_valid === true,
      mappingErrors: stringItems(response.data.mapping_errors),
      traceId: response.meta?.trace_id
    }
  };
}

function runConnectorAction(
  connectorId: string,
  action: "/connection-tests" | "/record-previews",
  payload: Record<string, unknown>
) {
  return apiRequest<Record<string, unknown>>(
    `/v1/connectors/${encodeURIComponent(connectorId)}${action}`,
    {
      method: "POST",
      headers: {
        "Idempotency-Key": writeKey(`connector_${action}_${connectorId}`)
      },
      body: JSON.stringify(payload)
    }
  );
}

export function saveAudioImportTaskVersion(
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
) {
  return createBackendAction(
    "/v1/task-versions",
    "audio_import_task_version",
    payload,
    options
  );
}

export function publishAudioImportTaskVersion(
  taskVersionId: string,
  payload: Record<string, unknown>,
  options?: WriteRequestOptions
) {
  return createBackendAction(
    `/v1/task-versions/${encodeURIComponent(taskVersionId)}/publish`,
    `audio_import_task_publish_${taskVersionId}`,
    payload,
    options
  );
}

export function runPublishedAudioImportTask(
  taskVersionId: string,
  options?: WriteRequestOptions
) {
  const payload: TaskRunCreatePayload = {
    task_version_id: taskVersionId,
    trigger_type: "manual",
    execution_mode: "production"
  };
  return createBackendAction(
    "/v1/task-runs",
    `audio_import_task_run_${taskVersionId}`,
    payload,
    options
  );
}

export function getImportBatch(importBatchId: string) {
  return apiRequest<Record<string, unknown>>(importBatchPath(importBatchId));
}

export function getImportBatchItems(
  importBatchId: string,
  input: {
    status?: "queued" | "running" | "succeeded" | "skipped" | "failed";
    cursor?: string;
    limit?: number;
  } = {}
) {
  const query = new URLSearchParams({
    limit: String(input.limit ?? 200)
  });
  if (input.status) query.set("status", input.status);
  if (input.cursor) query.set("cursor", input.cursor);
  return apiRequest<unknown>(
    `${importBatchPath(importBatchId)}/items?${query.toString()}`
  );
}

export async function listAudioImportBatchItems(
  importBatchId: string,
  input: {
    status?: "queued" | "running" | "succeeded" | "skipped" | "failed";
  } = {}
): Promise<ApiEnvelope<{ items: Array<Record<string, unknown>> }>> {
  const items: Array<Record<string, unknown>> = [];
  const observedCursors = new Set<string>();
  let cursor = "";
  for (let page = 0; page < 50; page += 1) {
    const response = await getImportBatchItems(importBatchId, {
      status: input.status,
      cursor,
      limit: 200
    });
    items.push(...collectionItems(response.data));
    const nextCursor = String(response.meta?.next_cursor ?? "");
    if (!nextCursor) return { ...response, data: { items } };
    if (observedCursors.has(nextCursor)) {
      throw new Error("导入失败记录分页游标重复，无法完成批次回读");
    }
    observedCursors.add(nextCursor);
    cursor = nextCursor;
  }
  throw new Error("导入失败记录超过前端安全分页上限，请缩小状态筛选范围");
}

const importBatchPath = (id: string) =>
  `/v1/import-batches/${encodeURIComponent(id)}`;

export function retryAudioImportTaskRun(
  taskRunId: string,
  options?: WriteRequestOptions
) {
  return createBackendAction(
    `/v1/task-runs/${encodeURIComponent(taskRunId)}/retries`,
    `audio_import_task_retry_${taskRunId}`,
    { reason: "重试导入批次失败项", execution_mode: "production" },
    options
  );
}
