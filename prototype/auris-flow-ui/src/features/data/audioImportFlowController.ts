import { useCallback, useReducer } from "react";

import {
  createAudioImportConnector,
  getImportBatch,
  listAudioImportBatches,
  listAudioImportBatchItems,
  listAudioImportConnectors,
  listPlatformConnections,
  patchAudioImportConnector,
  previewAudioImportRecords,
  publishAudioImportTaskVersion,
  recalledLatestAudioImportBatch,
  rememberLatestAudioImportBatch,
  retryAudioImportTaskRun,
  runPublishedAudioImportTask,
  saveAudioImportTaskVersion,
  testAudioImportConnection,
  type PlatformConnectionOption
} from "../../api/audioImportClient";
import { getTaskVersion, listTaskVersions } from "../../api/client";
import type { OperationNotice } from "../../shared/contracts/operations";
import { LABEL_DEMO_MODE } from "../../shared/runtime/demoMode";
import {
  bindAudioImportDraftToPlatformConnection,
  buildAudioImportTaskVersionPayload,
  buildAudioImportStepStates,
  buildConnectorPayload,
  canNavigateToAudioImportStep,
  configurationFingerprint,
  defaultAudioImportDraft,
  firstAudioImportErrorFieldId,
  importBatchIdFromTaskRun,
  latestAudioImportBatchIdFromBatches,
  normalizeImportBatch,
  normalizeImportBatchItems,
  audioImportErrorFieldIds,
  validateAudioImportStep,
  validateAudioImportPlatformBinding,
  validateCompleteAudioImport,
  type AudioImportBatch,
  type AudioImportBatchItem,
  type AudioImportDraft,
  type AudioImportVerification
} from "./audioImportFlowModel";
import {
  useAudioImportBatchReadback,
  useAudioImportPublishReadback
} from "./audioImportReadbackHooks";
import type { DataSceneProfileLock } from "./types";

type AudioImportFlowInput = {
  targetAssetKey: string;
  sceneProfileLock: DataSceneProfileLock | null;
  setDataNotice: (notice: OperationNotice) => void;
};

const emptyVerification: AudioImportVerification = {
  testedFingerprint: "",
  previewedFingerprint: "",
  mappingValid: false,
  mappingErrors: []
};
const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
const text = (value: unknown) => typeof value === "string" ? value : "";
const errorText = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;
const positiveNumber = (value: unknown, fallback = 1) => {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback;
};
const mappingKeys = [
  ["externalRecordId", "external_record_id", true],
  ["audioUrl", "audio_url", true],
  ["startedAt", "started_at", true],
  ["agentRef", "agent_ref", false],
  ["storeRef", "store_ref", false],
  ["deviceRef", "device_ref", false],
  ["durationMs", "duration_ms", false]
] as const;

function draftFromConnector(source: Record<string, unknown>, fallback: AudioImportDraft) {
  const scope = record(source.platform_scope);
  const pagination = record(source.pagination);
  const mapping = record(source.field_mapping);
  const cursor = record(source.cursor_policy);
  const fieldMapping = { ...fallback.fieldMapping };
  mappingKeys.forEach(([target, key, required]) => {
    fieldMapping[target] = text(mapping[key]) || (required ? fallback.fieldMapping[target] : "");
  });
  const start = text(cursor.initial_window_start);
  return {
    ...fallback,
    name: text(source.name) || fallback.name,
    platformConnectionId: text(source.platform_connection_id),
    platformTenantKey: text(scope.tenant_ref),
    storeScope: Array.isArray(scope.store_refs)
      ? scope.store_refs.map(text).filter(Boolean).join(",") : "",
    baseUrl: text(source.base_url),
    requestPath: text(source.request_path) || fallback.requestPath,
    credentialRef: text(source.credential_ref),
    pageSize: positiveNumber(pagination.page_size, fallback.pageSize),
    cursorParam: text(pagination.cursor_param) || fallback.cursorParam,
    nextCursorPath: text(pagination.next_cursor_path) || fallback.nextCursorPath,
    fieldMapping,
    cursorField: text(cursor.field) || fallback.cursorField,
    initialWindowStart: start ? start.slice(0, 16) : fallback.initialWindowStart,
    targetAssetKey: text(source.target_asset_key) || fallback.targetAssetKey
  };
}

const initialState = (targetAssetKey: string) => ({
  open: false,
  step: 1,
  draft: defaultAudioImportDraft(targetAssetKey),
  verification: emptyVerification,
  connectorId: "",
  connectorVersion: 1,
  persistedFingerprint: "",
  taskVersionId: "",
  taskVersionStatus: "",
  taskVersionFingerprint: "",
  platformConnections: [] as PlatformConnectionOption[],
  platformConnectionsDetail: "",
  previewRecords: [] as Array<Record<string, unknown>>,
  previewFields: [] as string[],
  batchId: "",
  batch: null as AudioImportBatch | null,
  batchItems: [] as AudioImportBatchItem[],
  action: "",
  attemptedSteps: [] as number[],
  detail: "请选择平台连接并配置音频 URL 导入。",
  rememberedBatchHint: "",
  validationFocusFieldId: "",
  validationFocusRevision: 0,
  visitedSteps: [1] as number[]
});
type FlowState = ReturnType<typeof initialState>;
type StateChange = Partial<FlowState> | ((state: FlowState) => Partial<FlowState>);
const reduceState = (state: FlowState, change: StateChange) => ({
  ...state,
  ...(typeof change === "function" ? change(state) : change)
});

function deriveAudioImportFlowState(
  state: FlowState,
  sceneProfileLock: DataSceneProfileLock | null
) {
  const {
    attemptedSteps, draft, platformConnections, step, taskVersionFingerprint,
    taskVersionStatus, verification, visitedSteps
  } = state;
  const fingerprint = configurationFingerprint(draft);
  const selectedPlatformConnection = platformConnections.find(
    (item) => item.id === draft.platformConnectionId
  );
  const platformBindingErrors = selectedPlatformConnection
    ? validateAudioImportPlatformBinding(draft, selectedPlatformConnection)
    : draft.platformConnectionId && !LABEL_DEMO_MODE
      ? ["所选平台连接不在当前项目连接目录中"]
      : [];
  const blockers = Array.from(new Set([
    ...validateAudioImportStep(step, draft, verification),
    ...([1, 2].includes(step) ? platformBindingErrors : [])
  ]));
  const completeBlockers = Array.from(new Set([
    ...validateCompleteAudioImport(draft, verification),
    ...platformBindingErrors
  ]));
  const taskVersionCurrent = taskVersionFingerprint === fingerprint;
  const stepStates = buildAudioImportStepStates({
    draft,
    verification,
    visitedSteps,
    attemptedSteps,
    releaseVerified: taskVersionCurrent && taskVersionStatus === "published"
  });
  const activeValidationErrors = attemptedSteps.includes(step)
    ? (step === 6 ? completeBlockers : blockers) : [];
  return {
    blockers,
    completeBlockers,
    fingerprint,
    invalidFieldIds: audioImportErrorFieldIds(activeValidationErrors),
    platformBindingErrors,
    saveDraftBlockedReason: !sceneProfileLock
      ? "缺少已发布 SceneProfile，无法保存草稿。"
      : completeBlockers[0] ?? "",
    selectedPlatformConnection,
    stepStates,
    taskVersionCurrent
  };
}

async function readAudioImportRecovery(
  targetAssetKey: string
): Promise<Partial<FlowState>> {
  const fallback = defaultAudioImportDraft(targetAssetKey);
  const [connectorResponse, versionResponse, connectionResult] = await Promise.all([
    listAudioImportConnectors(),
    listTaskVersions(),
    listPlatformConnections().then(
      ({ data }) => ({ items: data.items, error: "" }),
      (error) => ({ items: [], error: errorText(error, "平台连接读取失败") })
    )
  ]);
  const versions = versionResponse.data.items.filter(
    (item) => item.task_type_id === "audio-platform-import"
  );
  const matchingVersions = versions.filter((item) => {
    const direct = record(item.input_binding);
    const nested = record(record(item.payload).input_binding);
    return text(direct.target_asset_key || nested.target_asset_key) === targetAssetKey;
  });
  const version = matchingVersions[matchingVersions.length - 1];
  const binding = record(version?.input_binding ?? record(version?.payload).input_binding);
  const recoveredConnectorId = text(binding.connector_id);
  const connectors = connectorResponse.data.items;
  const connector = connectors.find(
    (item) => text(item.id ?? item.connector_id) === recoveredConnectorId
  ) ?? connectors.find((item) => item.target_asset_key === targetAssetKey);
  let recovered: Partial<FlowState> = {
    platformConnections: connectionResult.items,
    platformConnectionsDetail: connectionResult.error,
    draft: fallback,
    detail: connector
      ? "配置已恢复；修改发布前须重新测试和预览。"
      : "暂无配置，请先关联平台。"
  };
  if (connector) {
    const id = text(connector.id ?? connector.connector_id);
    const connectorDraft = draftFromConnector(connector, fallback);
    const selectedConnection = connectionResult.items.find(
      (item) => item.id === connectorDraft.platformConnectionId
    );
    const restoredDraft = selectedConnection
      ? bindAudioImportDraftToPlatformConnection(connectorDraft, selectedConnection)
      : connectorDraft;
    const restoredVersion = positiveNumber(
      connector.connector_version ?? connector.version ?? connector.resource_version
    );
    const restoredFingerprint = configurationFingerprint(restoredDraft);
    const persistedConnectorFingerprint = configurationFingerprint(connectorDraft);
    const frozenVersion = Number(binding.connector_version);
    const frozenConnectorMatches = Boolean(
      version
      && text(binding.connector_id) === id
      && Number.isFinite(frozenVersion)
      && frozenVersion === restoredVersion
      && restoredFingerprint === persistedConnectorFingerprint
    );
    const recoveredPublishedVersion =
      frozenConnectorMatches && text(version?.status).toLowerCase() === "published";
    recovered = {
      ...recovered,
      connectorId: id,
      connectorVersion: restoredVersion,
      draft: restoredDraft,
      persistedFingerprint: persistedConnectorFingerprint,
      taskVersionFingerprint: frozenConnectorMatches ? restoredFingerprint : "",
      ...(recoveredPublishedVersion ? {
        verification: {
          testedFingerprint: restoredFingerprint,
          previewedFingerprint: restoredFingerprint,
          mappingValid: true,
          mappingErrors: []
        },
        visitedSteps: [1, 2, 3, 4, 5, 6]
      } : {}),
      ...(version ? {
        taskVersionId: text(version.id ?? version.task_version_id),
        taskVersionStatus: text(version.status)
      } : {})
    };
  }
  const batchResult = await listAudioImportBatches({
    connectorId: text(recovered.connectorId) || recoveredConnectorId || undefined,
    taskVersionId: text(recovered.taskVersionId)
      || text(version?.id ?? version?.task_version_id)
      || undefined,
    targetAssetKey
  }).then(
    ({ data }) => ({ items: data.items, error: "" }),
    (error) => ({ items: [], error: errorText(error, "同步批次列表读取失败") })
  );
  const latestBatchId = latestAudioImportBatchIdFromBatches(batchResult.items);
  const rememberedBatchHint = recalledLatestAudioImportBatch(targetAssetKey);
  const batchRecoveryDetail = latestBatchId
    ? `最近同步批次 ${latestBatchId} 已从 BFF 列表恢复。`
    : batchResult.error
      ? `${batchResult.error}；未恢复批次。${
          rememberedBatchHint ? ` 浏览器曾记录 ${rememberedBatchHint}，仅作为提示。` : ""
        }`
      : `BFF 列表中没有匹配当前任务版本和目标资产的同步批次。${
          rememberedBatchHint ? ` 浏览器曾记录 ${rememberedBatchHint}，仅作为提示。` : ""
        }`;
  return {
    ...recovered,
    batchId: latestBatchId,
    ...(text(recovered.taskVersionStatus).toLowerCase() === "published"
      && text(recovered.taskVersionFingerprint)
      ? { step: 6 }
      : {}),
    rememberedBatchHint,
    detail: `${text(recovered.detail)} ${batchRecoveryDetail}`.trim()
  };
}

export function useAudioImportFlow({
  targetAssetKey,
  sceneProfileLock,
  setDataNotice
}: AudioImportFlowInput) {
  const [state, change] = useReducer(reduceState, initialState(targetAssetKey));
  const {
    action, attemptedSteps, batch, batchId, batchItems, connectorId, connectorVersion, detail,
    draft, open, persistedFingerprint, platformConnections,
    platformConnectionsDetail, previewFields, previewRecords, rememberedBatchHint,
    step, taskVersionFingerprint, taskVersionId, taskVersionStatus, validationFocusFieldId,
    validationFocusRevision, verification, visitedSteps
  } = state;
  const {
    blockers, completeBlockers, fingerprint, invalidFieldIds, platformBindingErrors,
    saveDraftBlockedReason, selectedPlatformConnection, stepStates, taskVersionCurrent
  } = deriveAudioImportFlowState(state, sceneProfileLock);
  const setDetail = useCallback((detail: string) => change({ detail }), []);
  const setTaskVersionStatus = useCallback(
    (taskVersionStatus: string) => change({ taskVersionStatus }),
    []
  );
  const setConnectorId = (connectorId: string) => change({ connectorId });
  const setTaskVersionId = (taskVersionId: string) => change({ taskVersionId });
  const setBatchId = (batchId: string) => change({ batchId });
  const setDraft = (
    value: AudioImportDraft | ((current: AudioImportDraft) => AudioImportDraft)
  ) => change((current) => ({
    draft: typeof value === "function" ? value(current.draft) : value
  }));
  const selectPlatformConnection = (platformConnectionId: string) => {
    const connection = platformConnections.find(
      (item) => item.id === platformConnectionId
    );
    change((current) => ({
      draft: connection
        ? bindAudioImportDraftToPlatformConnection(current.draft, connection)
        : {
            ...current.draft,
            platformConnectionId,
            platformTenantKey: "",
            storeScope: "",
            baseUrl: "",
            credentialRef: ""
          },
      detail: connection
        ? `已绑定平台连接 ${connection.name} 的租户、门店范围、地址和凭证引用。`
        : "请选择当前项目中已验证的平台连接。"
    }));
  };
  const requestValidationFocus = (errors: string[], attemptedStep = step) =>
    change((current) => ({
      attemptedSteps: Array.from(new Set([...current.attemptedSteps, attemptedStep])),
      validationFocusFieldId: firstAudioImportErrorFieldId(errors),
      validationFocusRevision: current.validationFocusRevision + 1
    }));

  const refreshBatch = useCallback(async (id: string) => {
    const [batchResponse, itemsResponse] = await Promise.all([
      getImportBatch(id),
      listAudioImportBatchItems(id, { status: "failed" })
    ]);
    const next = normalizeImportBatch(batchResponse.data);
    change({
      batch: next,
      batchItems: normalizeImportBatchItems(itemsResponse.data),
      detail: `批次 ${next.id} 回读状态：${next.status}。`
    });
    return next;
  }, []);

  useAudioImportBatchReadback({ batchId, open, refreshBatch, setDetail });
  useAudioImportPublishReadback({
    open,
    setDetail,
    setTaskVersionStatus,
    taskVersionId,
    taskVersionStatus
  });

  const perform = async (
    action: string,
    pending: string,
    failure: string,
    operation: () => Promise<void>
  ) => {
    change({ action, ...(pending ? { detail: pending } : {}) });
    try {
      await operation();
    } catch (error) {
      setDetail(errorText(error, failure));
    } finally {
      change({ action: "" });
    }
  };
  const reject = (errors: string[], attemptedStep = step) => {
    if (!errors.length) return false;
    setDetail(errors[0]);
    requestValidationFocus(errors, attemptedStep);
    return true;
  };

  const recover = async () => {
    setConnectorId("");
    setTaskVersionId("");
    setBatchId("");
    change({
      attemptedSteps: [],
      connectorVersion: 1,
      persistedFingerprint: "",
      taskVersionStatus: "",
      taskVersionFingerprint: "",
      batch: null,
      batchItems: [],
      rememberedBatchHint: "",
      previewRecords: [],
      previewFields: [],
      validationFocusFieldId: "",
      visitedSteps: [1]
    });
    return perform("recover", "正在恢复配置与最近批次。", "配置回读失败", async () => {
      change(await readAudioImportRecovery(targetAssetKey));
    });
  };

  const openDrawer = () => {
    change({
      open: true,
      step: 1,
      verification: emptyVerification,
      attemptedSteps: [],
      validationFocusFieldId: "",
      visitedSteps: [1]
    });
    void recover();
  };

  const persistConnector = async () => {
    if (!sceneProfileLock) {
      throw new Error("缺少已发布 SceneProfile，无法保存配置");
    }
    const replace = Boolean(
      connectorId
      && ["published", "publishing"].includes(taskVersionStatus)
      && taskVersionFingerprint
      && taskVersionFingerprint !== fingerprint
    );
    const id = connectorId && !replace
      ? connectorId : `audio_import_connector_${Date.now().toString(36)}`;
    if (connectorId && persistedFingerprint === fingerprint) {
      return { id: connectorId, version: connectorVersion };
    }
    const payload = { ...buildConnectorPayload(draft, sceneProfileLock, id), connector_id: id };
    const response = connectorId && !replace
      ? await patchAudioImportConnector(connectorId, payload)
      : await createAudioImportConnector(payload);
    const resolvedId = response.data.id || id;
    const version = positiveNumber(
      response.data.raw.connector_version
      ?? response.data.raw.version
      ?? response.data.raw.resource_version,
      connectorVersion
    );
    change({
      connectorId: resolvedId,
      connectorVersion: version,
      persistedFingerprint: fingerprint,
      ...(replace ? {
        taskVersionId: "",
        taskVersionStatus: "",
        taskVersionFingerprint: ""
      } : {})
    });
    return { id: resolvedId, version };
  };

  const testConnection = async () => {
    if (reject(Array.from(new Set([
      ...[1, 2].flatMap((item) => validateAudioImportStep(item, draft, verification)),
      ...platformBindingErrors
    ])))) return;
    return perform("test", "正在通过 BFF 测试连接。", "连接测试失败", async () => {
      const connector = await persistConnector();
      const response = await testAudioImportConnection(connector.id);
      change({
        verification: { ...verification, testedFingerprint: fingerprint },
        detail: `连接通过；trace ${(response.meta?.trace_id ?? text(response.data.trace_id)) || "未返回"}。`
      });
    });
  };

  const previewRecordsFromSource = async () => {
    if (verification.testedFingerprint !== fingerprint) {
      setDetail("连接尚未通过测试，无法预览。");
      return;
    }
    return perform("preview", "正在读取最多 3 条源记录。", "记录预览失败", async () => {
      const connector = await persistConnector();
      const { data } = await previewAudioImportRecords(connector.id);
      if (!data.records.length) throw new Error("平台未返回可预览记录");
      change({
        previewRecords: data.records,
        previewFields: data.fields,
        verification: {
          ...verification,
          previewedFingerprint: fingerprint,
          mappingValid: data.mappingValid,
          mappingErrors: data.mappingErrors
        },
        detail: data.mappingValid
          ? `已读取 ${data.records.length} 条记录；映射有效，敏感字段已隐藏。`
          : `请修正映射：${data.mappingErrors.join("、") || "必填字段"}，再重新预览。`
      });
    });
  };

  const saveDraft = async () => {
    const activeSceneProfileLock = sceneProfileLock;
    if (!activeSceneProfileLock) {
      setDetail("缺少已发布 SceneProfile，无法保存草稿。");
      return;
    }
    if (reject(completeBlockers, 6)) return;
    if (
      taskVersionFingerprint === fingerprint
      && ["draft", "published", "publishing"].includes(taskVersionStatus)
    ) {
      setDetail(`当前配置已对应版本 ${taskVersionId}。`);
      return;
    }
    return perform("save-draft", "正在保存配置草稿。", "草稿保存失败", async () => {
      const connector = await persistConnector();
      const saved = await saveAudioImportTaskVersion(buildAudioImportTaskVersionPayload({
        draft,
        connectorId: connector.id,
        connectorVersion: connector.version,
        sceneProfileLock: activeSceneProfileLock
      }));
      const readback = await getTaskVersion(saved.data.id);
      const readbackId = text(readback.data.id ?? readback.data.task_version_id);
      const readbackStatus = text(readback.data.status).toLowerCase();
      const readbackPayload = record(readback.data.payload);
      const readbackBinding = record(
        readback.data.input_binding ?? readbackPayload.input_binding
      );
      if (
        readbackId !== saved.data.id
        || readbackStatus !== "draft"
        || text(readbackBinding.connector_id) !== connector.id
        || Number(readbackBinding.connector_version) !== connector.version
      ) {
        throw new Error("草稿写入后回读不一致，已停止发布流程，请刷新后重试");
      }
      change({
        taskVersionId: readbackId,
        taskVersionStatus: readbackStatus,
        taskVersionFingerprint: fingerprint,
        detail: `草稿 ${readbackId} 已写入并回读，可发布。`
      });
    });
  };

  const publish = async () => {
    if (reject(completeBlockers, 6)) return;
    if (!taskVersionId || taskVersionStatus !== "draft" || taskVersionFingerprint !== fingerprint) {
      setDetail("请先保存当前草稿再发布。");
      return;
    }
    return perform("publish", `正在发布 ${taskVersionId}。`, "配置发布失败", async () => {
      const release = await publishAudioImportTaskVersion(taskVersionId, {
        reason: "连接、预览和字段映射校验通过"
      });
      const published = release.data.status.toLowerCase() === "published";
      change({
        taskVersionStatus: published ? "published" : "publishing",
        detail: published
          ? `版本 ${taskVersionId} 已发布，可立即拉取。`
          : `发布请求 ${release.data.id} 已提交，等待读回。`
      });
    });
  };

  const startBatch = async (request: () => ReturnType<typeof runPublishedAudioImportTask>, missing: string) => {
    const response = await request();
    const id = importBatchIdFromTaskRun(response.data);
    if (!id) throw new Error(missing);
    setBatchId(id);
    rememberLatestAudioImportBatch(targetAssetKey, id);
    await refreshBatch(id);
    return id;
  };

  const run = async () => {
    if (!taskVersionId || taskVersionStatus !== "published" || taskVersionFingerprint !== fingerprint) {
      setDetail("仅当前配置的已发布版本可生产拉取。");
      return;
    }
    return perform("run", "正在创建生产导入运行。", "生产运行创建失败", async () => {
      const id = await startBatch(
        () => runPublishedAudioImportTask(taskVersionId),
        "TaskRun 回执缺少 import_batch_id"
      );
      setDataNotice({
        status: "pending",
        title: "音频导入运行已创建",
        detail: `${id} 正在执行；状态来自 BFF 回读。`
      });
    });
  };

  const retryFailedItems = async () => {
    if (!batch?.taskRunId) return;
    return perform("retry", "", "重试请求失败", async () => {
      await startBatch(
        () => retryAudioImportTaskRun(batch.taskRunId),
        "重试 TaskRun 回执缺少新的 import_batch_id"
      );
    });
  };

  const canVisitStep = (targetStep: number) =>
    canNavigateToAudioImportStep(targetStep, visitedSteps, stepStates);

  const goToStep = (targetStep: number) => {
    if (!canVisitStep(targetStep)) {
      const earliestIncomplete = stepStates.find(
        (item) => item.status !== "verified"
      )?.id ?? step;
      setDetail(
        visitedSteps.includes(targetStep)
          ? `请先完成第 ${earliestIncomplete} 步，不能越过最早未完成步骤。`
          : "该步骤尚未访问，请通过“下一步”按顺序完成配置。"
      );
      return;
    }
    change({ step: targetStep });
  };

  const next = () => {
    if (reject(blockers, step)) return;
    const targetStep = Math.min(6, step + 1);
    change((current) => ({
      attemptedSteps: Array.from(new Set([...current.attemptedSteps, step])),
      step: targetStep,
      visitedSteps: Array.from(new Set([...current.visitedSteps, targetStep]))
    }));
  };

  const previous = () => {
    const targetStep = Math.max(1, step - 1);
    if (visitedSteps.includes(targetStep)) change({ step: targetStep });
  };

  return {
    action,
    batch,
    batchItems,
    blockers,
    canVisitStep,
    close: () => change({ open: false }),
    completeBlockers,
    detail,
    draft,
    connectionVerified: verification.testedFingerprint === fingerprint,
    goToStep,
    invalidFieldIds,
    next,
    open,
    openDrawer,
    platformConnections,
    platformConnectionsDetail,
    selectedPlatformConnection,
    selectPlatformConnection,
    previewFields,
    previewVerified: verification.previewedFingerprint === fingerprint,
    previewRecords,
    previous,
    publish,
    rememberedBatchHint,
    refreshBatch: () => batchId ? refreshBatch(batchId) : Promise.resolve(null),
    retryFailedItems,
    run,
    saveDraftBlockedReason,
    saveDraft,
    setDraft,
    step,
    stepStates,
    taskVersionId,
    taskVersionCurrent,
    taskVersionStatus,
    testConnection,
    previewRecordsFromSource,
    validationFocusFieldId,
    validationFocusRevision,
    verification
  };
}

export type AudioImportFlow = ReturnType<typeof useAudioImportFlow>;
