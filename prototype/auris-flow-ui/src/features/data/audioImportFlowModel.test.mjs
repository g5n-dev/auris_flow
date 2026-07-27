import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourceUrl = new URL("./audioImportFlowModel.ts", import.meta.url);
const previewSecurityUrl = new URL("./audioImportPreviewSecurity.ts", import.meta.url);

async function loadTypeScriptModule(moduleUrl) {
  const source = await readFile(moduleUrl, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022
    }
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(output).toString("base64")}`);
}

const loadModel = () => loadTypeScriptModule(sourceUrl);

const completeDraft = {
  name: "极光平台录音导入",
  platformConnectionId: "platform_conn_001",
  platformTenantKey: "aurora",
  storeScope: "BJ-001",
  baseUrl: "https://audio.example.test",
  requestPath: "/v1/recordings",
  credentialRef: "secret://aurora/audio-api",
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
  initialWindowStart: "2026-07-01T00:00",
  targetAssetKey: "auris/audio/raw_recordings",
  dedupePolicy: "external_id_checksum"
};

test("导入配置必须按平台、接口、验证预览、字段映射、游标目标逐步闭环", async () => {
  const { validateAudioImportStep } = await loadModel();

  assert.deepEqual(validateAudioImportStep(1, { ...completeDraft, platformConnectionId: "" }, {
    testedFingerprint: "",
    previewedFingerprint: "",
    mappingValid: false,
    mappingErrors: []
  }), ["请选择已存在的平台连接"]);
  assert.deepEqual(validateAudioImportStep(2, { ...completeDraft, credentialRef: "" }, {
    testedFingerprint: "",
    previewedFingerprint: "",
    mappingValid: false,
    mappingErrors: []
  }), ["请填写 credential_ref"]);
  assert.deepEqual(validateAudioImportStep(2, { ...completeDraft, pageSize: 251 }, {
    testedFingerprint: "",
    previewedFingerprint: "",
    mappingValid: false,
    mappingErrors: []
  }), ["分页大小必须是 1 到 250 的整数"]);

  const { configurationFingerprint } = await loadModel();
  const fingerprint = configurationFingerprint(completeDraft);
  assert.deepEqual(validateAudioImportStep(3, completeDraft, {
    testedFingerprint: fingerprint,
    previewedFingerprint: "",
    mappingValid: false,
    mappingErrors: []
  }), ["请预览真实源记录"]);
  assert.deepEqual(validateAudioImportStep(4, {
    ...completeDraft,
    fieldMapping: { ...completeDraft.fieldMapping, audioUrl: "" }
  }, {
    testedFingerprint: fingerprint,
    previewedFingerprint: fingerprint,
    mappingValid: true,
    mappingErrors: []
  }), ["请映射音频 URL"]);
  assert.deepEqual(validateAudioImportStep(5, completeDraft, {
    testedFingerprint: fingerprint,
    previewedFingerprint: fingerprint,
    mappingValid: true,
    mappingErrors: []
  }), []);
});

test("连接参数变化后，旧测试与预览回执失效", async () => {
  const { configurationFingerprint, isConfigurationVerified } = await loadModel();
  const fingerprint = configurationFingerprint(completeDraft);

  assert.equal(isConfigurationVerified(completeDraft, {
    testedFingerprint: fingerprint,
    previewedFingerprint: fingerprint,
    mappingValid: true,
    mappingErrors: []
  }), true);
  assert.equal(isConfigurationVerified(
    { ...completeDraft, requestPath: "/v2/recordings" },
    {
      testedFingerprint: fingerprint,
      previewedFingerprint: fingerprint,
      mappingValid: true,
      mappingErrors: []
    }
  ), false);
  assert.equal(isConfigurationVerified(
    {
      ...completeDraft,
      fieldMapping: { ...completeDraft.fieldMapping, audioUrl: "signed_download_url" }
    },
    {
      testedFingerprint: fingerprint,
      previewedFingerprint: fingerprint,
      mappingValid: true,
      mappingErrors: []
    }
  ), false);
});

test("真实预览允许先查看原始字段，但无效映射不能保存或发布", async () => {
  const {
    configurationFingerprint,
    validateAudioImportStep,
    validateCompleteAudioImport
  } = await loadModel();
  const fingerprint = configurationFingerprint(completeDraft);
  const verification = {
    testedFingerprint: fingerprint,
    previewedFingerprint: fingerprint,
    mappingValid: false,
    mappingErrors: ["cursor_policy.field", "audio_url"]
  };

  assert.deepEqual(validateAudioImportStep(3, completeDraft, verification), []);
  assert.match(
    validateCompleteAudioImport(completeDraft, verification).at(-1),
    /cursor_policy\.field.*audio_url/
  );
});

test("真实预览按音频 URL 映射路径强制脱敏，并支持 dotted path", async () => {
  const { formatAudioImportPreviewValue } = await loadTypeScriptModule(previewSecurityUrl);
  const record = {
    media: {
      download: {
        source: "https://audio.example.test/call.wav?token=never-render"
      }
    },
    playback: "https://audio.example.test/fallback.wav",
    harmless: "可展示内容"
  };

  const mappedValue = formatAudioImportPreviewValue({
    audioUrlFieldPath: "media.download.source",
    field: "media.download.source",
    record
  });
  assert.equal(mappedValue, "已返回（敏感值已隐藏）");
  assert.doesNotMatch(mappedValue, /https?:\/\/|never-render|token/i);

  const neutralUrlValue = formatAudioImportPreviewValue({
    audioUrlFieldPath: "another.path",
    field: "playback",
    record
  });
  assert.equal(neutralUrlValue, "已返回（敏感值已隐藏）");
  assert.doesNotMatch(neutralUrlValue, /https?:\/\//i);

  assert.equal(formatAudioImportPreviewValue({
    audioUrlFieldPath: "media.download.source",
    field: "harmless",
    record
  }), "可展示内容");
});

test("真实预览不会通过对象或敏感字段名泄露 token 与凭证", async () => {
  const { formatAudioImportPreviewValue } = await loadTypeScriptModule(previewSecurityUrl);
  const record = {
    metadata: {
      authorization: "Bearer secret-token",
      label: "录音"
    },
    opaque: "token=secret-value",
    password_hint: "secret-value"
  };

  for (const field of ["metadata", "opaque", "password_hint"]) {
    const rendered = formatAudioImportPreviewValue({
      audioUrlFieldPath: "media.source",
      field,
      record
    });
    assert.equal(rendered, "已返回（敏感值已隐藏）");
    assert.doesNotMatch(rendered, /Bearer|secret-token|secret-value/i);
  }
});

test("TaskVersion 冻结完整输入绑定且立即拉取只能使用 production", async () => {
  const {
    buildAudioImportTaskRunPayload,
    buildAudioImportTaskVersionPayload
  } = await loadModel();
  const payload = buildAudioImportTaskVersionPayload({
    draft: completeDraft,
    connectorId: "connector_001",
    connectorVersion: 3,
    sceneProfileLock: {
      scene_profile_id: "scene_001",
      scene_profile_version_id: "scene_version_001",
      scene_profile_snapshot_sha256: "a".repeat(64)
    }
  });

  assert.equal(payload.task_type_id, "audio-platform-import");
  assert.equal(payload.connector_id, "connector_001");
  assert.equal(payload.input_binding.connector_id, "connector_001");
  assert.equal(payload.input_binding.connector_version, "3");
  assert.equal(payload.input_binding.platform_connection_id, "platform_conn_001");
  assert.equal(payload.input_binding.source_type, "platform_audio_url_api");
  assert.deepEqual(payload.input_binding.platform_scope, {
    tenant_ref: "aurora",
    store_refs: ["BJ-001"]
  });
  assert.deepEqual(payload.input_binding.field_mapping, {
    external_record_id: "recording_id",
    audio_url: "audio_url",
    started_at: "started_at",
    agent_ref: "agent_id",
    store_ref: "store_id",
    device_ref: "device_id",
    duration_ms: "duration_ms"
  });
  assert.deepEqual(buildAudioImportTaskRunPayload("task_version_001"), {
    task_version_id: "task_version_001",
    trigger_type: "manual",
    execution_mode: "production"
  });
});

test("批次权威计数字段与导入项主键直接映射到前端业务统计", async () => {
  const {
    normalizeImportBatch,
    normalizeImportBatchItems
  } = await loadModel();

  assert.deepEqual(normalizeImportBatch({
    import_batch_id: "batch_public_001",
    task_run_id: "task_run_public_001",
    status: "partial",
    current_stage: "completed",
    total_items: 7,
    succeeded_items: 3,
    skipped_items: 2,
    failed_items: 2,
    root_trace_id: "trace-public"
  }), {
    id: "batch_public_001",
    taskRunId: "task_run_public_001",
    status: "partial",
    currentStage: "completed",
    total: 7,
    succeeded: 3,
    duplicates: 2,
    failed: 2,
    rootTraceId: "trace-public",
    createdAudioSessionIds: [],
    errorCode: "",
    errorReason: ""
  });

  assert.equal(normalizeImportBatchItems({
    items: [{
      import_item_id: "import_item_public_001",
      external_record_id: "recording_public_001",
      status: "failed",
      error_code: "AUDIO_URL_EXPIRED"
    }]
  })[0]?.id, "import_item_public_001");
});

test("批次响应兼容 payload 关联并将物化阶段与终态清晰投影", async () => {
  const {
    importBatchIdFromTaskRun,
    normalizeImportBatch,
    normalizeImportBatchItems
  } = await loadModel();

  assert.equal(importBatchIdFromTaskRun({
    id: "task_run_001",
    status: "pending",
    raw: { payload: { import_batch_id: "batch_001" } }
  }), "batch_001");

  assert.deepEqual(normalizeImportBatch({
    batch_id: "batch_001",
    task_run_id: "task_run_001",
    status: "completion_pending",
    current_stage: "materializing",
    total_count: 4,
    imported_count: 2,
    duplicate_count: 1,
    failed_count: 1,
    root_trace_id: "trace-root"
  }), {
    id: "batch_001",
    taskRunId: "task_run_001",
    status: "materializing",
    currentStage: "materializing",
    total: 4,
    succeeded: 2,
    duplicates: 1,
    failed: 1,
    rootTraceId: "trace-root",
    createdAudioSessionIds: [],
    errorCode: "",
    errorReason: ""
  });

  assert.deepEqual(normalizeImportBatchItems({
    items: [{
      id: "item_001",
      external_record_id: "recording_001",
      status: "succeeded",
      storage_object_version: "v3",
      audio_session_id: "session_001",
      root_trace_id: "trace-root"
    }]
  }), [{
    id: "item_001",
    externalRecordId: "recording_001",
    status: "succeeded",
    errorCode: "",
    objectVersion: "v3",
    audioSessionId: "session_001",
    rootTraceId: "trace-root"
  }]);
});

test("批次级失败即使尚无逐条记录与失败计数也保持可见且可重试", async () => {
  const {
    canRetryImportBatch,
    hasImportBatchFailures,
    normalizeImportBatch
  } = await loadModel();

  const failedBatch = normalizeImportBatch({
    import_batch_id: "batch_failed_before_listing",
    task_run_id: "task_run_failed_before_listing",
    status: "failed",
    current_stage: "completed",
    total_items: 0,
    failed_items: 0,
    error_code: "PLATFORM_CREDENTIAL_INVALID",
    reason: "平台凭证无效或已过期",
    root_trace_id: "trace-batch-failure"
  });
  assert.deepEqual(failedBatch, {
    id: "batch_failed_before_listing",
    taskRunId: "task_run_failed_before_listing",
    status: "failed",
    currentStage: "completed",
    total: 0,
    succeeded: 0,
    duplicates: 0,
    failed: 0,
    rootTraceId: "trace-batch-failure",
    createdAudioSessionIds: [],
    errorCode: "PLATFORM_CREDENTIAL_INVALID",
    errorReason: "平台凭证无效或已过期"
  });
  assert.equal(hasImportBatchFailures(failedBatch, 0), true);
  assert.equal(canRetryImportBatch(failedBatch, 0), true);

  const emptyPartialBatch = normalizeImportBatch({
    import_batch_id: "batch_partial_without_items",
    task_run_id: "task_run_partial_without_items",
    status: "partial",
    total_items: 0,
    failed_items: 0
  });
  assert.equal(hasImportBatchFailures(emptyPartialBatch, 0), true);
  assert.equal(canRetryImportBatch(emptyPartialBatch, 0), true);

  const succeededBatch = normalizeImportBatch({
    import_batch_id: "batch_succeeded",
    task_run_id: "task_run_succeeded",
    status: "succeeded"
  });
  assert.equal(hasImportBatchFailures(succeededBatch, 0), false);
  assert.equal(canRetryImportBatch(succeededBatch, 0), false);
});
