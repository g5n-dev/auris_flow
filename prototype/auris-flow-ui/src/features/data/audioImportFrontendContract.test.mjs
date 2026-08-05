import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const apiSourceUrl = new URL("../../api/audioImportClient.ts", import.meta.url);
const sharedApiSourceUrl = new URL("../../api/client.ts", import.meta.url);
const apiRuntimeScopeSourceUrl = new URL("../../api/apiRuntimeScope.ts", import.meta.url);
const taskVersionPaginationSourceUrl = new URL("../../api/taskVersionPagination.ts", import.meta.url);
const tenantSourceUrl = new URL("../tenants/tenantMutations.ts", import.meta.url);
const drawerSourceUrl = new URL("./components/AudioImportDrawer.tsx", import.meta.url);
const formStepsSourceUrl = new URL("./components/AudioImportFormSteps.tsx", import.meta.url);
const sessionPanelSourceUrl = new URL("./components/AudioImportSessionPanel.tsx", import.meta.url);
const controllerSourceUrl = new URL("./audioImportFlowController.ts", import.meta.url);
const readbackHooksSourceUrl = new URL("./audioImportReadbackHooks.ts", import.meta.url);
const dataModuleSourceUrl = new URL("./DataModule.tsx", import.meta.url);
const dataProjectionSourceUrl = new URL("./dataProjection.ts", import.meta.url);
const shellNavigationSourceUrl = new URL("../../shell/useShellNavigation.ts", import.meta.url);
const navigationUrlStateSourceUrl = new URL("../../shell/navigationUrlState.ts", import.meta.url);
const listeningReadModelSourceUrl = new URL("../listening/hooks/useListeningReadModel.ts", import.meta.url);
const listeningQueueLoaderSourceUrl = new URL("../listening/hooks/listeningQueueLoader.ts", import.meta.url);
const platformE2eSourceUrl = new URL("../../../e2e/platform-bff.mjs", import.meta.url);
const staticCatalogUrl = new URL("../../catalogs/static-catalog.json", import.meta.url);

test("导入 API client 只通过业务接口完成配置、生产运行与批次回读", async () => {
  const source = await readFile(apiSourceUrl, "utf8");

  for (const route of [
    "/v1/connectors",
    "/connection-tests",
    "/record-previews",
    "/v1/task-versions",
    "/publish",
    "/v1/task-runs",
    "/v1/import-batches/"
  ]) {
    assert.match(source, new RegExp(route.replaceAll("/", "\\/")));
  }
  assert.doesNotMatch(source, /platform-sync-jobs/);
  assert.doesNotMatch(source, /execution_mode:\s*["']diagnostic["']/);
});

test("平台连接目录投影冻结租户、门店、origin、credential_ref 与测试路径", async () => {
  const [apiSource, controllerSource, formSource] = await Promise.all([
    readFile(apiSourceUrl, "utf8"),
    readFile(controllerSourceUrl, "utf8"),
    readFile(formStepsSourceUrl, "utf8")
  ]);

  for (const field of [
    "external_tenant_ref",
    "store_refs",
    "origin",
    "credential_ref",
    "test_path"
  ]) {
    assert.match(apiSource, new RegExp(field));
  }
  assert.match(controllerSource, /bindAudioImportDraftToPlatformConnection/);
  assert.match(controllerSource, /validateAudioImportPlatformBinding/);
  assert.match(controllerSource, /selectPlatformConnection/);
  assert.match(formSource, /flow\.selectPlatformConnection/);
  assert.match(formSource, /readOnly=\{frozenByConnection\}/);
  assert.match(formSource, /disabled=\{item\.status !== "active"\}/);
});

test("租户拉取复用已发布 TaskVersion，禁止继续调用旧 platform-sync-jobs", async () => {
  const source = await readFile(tenantSourceUrl, "utf8");

  assert.doesNotMatch(source, /createPlatformSyncJob/);
  assert.match(source, /runPublishedAudioImportTask/);
  assert.match(source, /getImportBatch/);
  assert.match(source, /getApiRuntimeScope/);
  assert.match(source, /rememberLatestAudioImportBatch/);
  assert.doesNotMatch(source, /platformScope[^]*tenant_ref|selectedTenant\.name[^]*includes\(tenantRef\)/);
  assert.doesNotMatch(source, /setTenantAsrRunOverrides|生产导入已提交/);
  assert.match(source, /execution_mode[^]*production|production[^]*execution_mode/);
});

test("高级画布的平台连接节点不再引导浏览器登录或明文凭证", async () => {
  const source = await readFile(staticCatalogUrl, "utf8");
  const catalog = JSON.parse(source);
  const platformConnectionNode = catalog.canvasNodeTemplates.find(
    (item) => item.key === "platform-login-adapter"
  );

  assert.equal(
    platformConnectionNode.endpoint,
    "/api/v1/platform-connections/{connection_id}/connection-tests"
  );
  assert.match(platformConnectionNode.authMode, /credential_ref/);
  assert.doesNotMatch(source, /platform-connections\/\{connection_id\}\/session/);
  assert.doesNotMatch(source, /password_secret_ref|platform_session\.access_token/);
});

test("配置与最近批次恢复覆盖完整分页，并按租户项目和目标资产隔离", async () => {
  const [apiSource, sharedApiSource, taskVersionPaginationSource, controllerSource] = await Promise.all([
    readFile(apiSourceUrl, "utf8"),
    readFile(sharedApiSourceUrl, "utf8"),
    readFile(taskVersionPaginationSourceUrl, "utf8"),
    readFile(controllerSourceUrl, "utf8")
  ]);

  assert.match(sharedApiSource, /listTaskVersions[^]*listAllTaskVersions/);
  assert.match(taskVersionPaginationSource, /next_cursor/);
  assert.match(taskVersionPaginationSource, /observedCursors/);
  assert.match(apiSource, /getApiScopeKey\(\)[^]*targetAssetKey/);
  assert.match(controllerSource, /setConnectorId\(""\)[^]*setTaskVersionId\(""\)[^]*setBatchId\(""\)/);
  assert.doesNotMatch(controllerSource, /versions\[0\]/);
});

test("音频导入扩展通过专用模块复用 API client，不继续膨胀冻结基线", async () => {
  const [
    sharedApiSource,
    runtimeScopeSource,
    taskVersionPaginationSource,
    controllerSource,
    readbackHooksSource
  ] = await Promise.all([
    readFile(sharedApiSourceUrl, "utf8"),
    readFile(apiRuntimeScopeSourceUrl, "utf8"),
    readFile(taskVersionPaginationSourceUrl, "utf8"),
    readFile(controllerSourceUrl, "utf8"),
    readFile(readbackHooksSourceUrl, "utf8")
  ]);

  assert.ok(sharedApiSource.trimEnd().split(/\r?\n/).length <= 3108);
  assert.match(sharedApiSource, /buildApiScopeKey/);
  assert.match(sharedApiSource, /readApiRuntimeScope/);
  assert.match(sharedApiSource, /listAllTaskVersions/);
  assert.match(runtimeScopeSource, /unbound-tenant[^]*unbound-project/);
  assert.match(taskVersionPaginationSource, /next_cursor[^]*observedCursors/);
  assert.match(controllerSource, /useAudioImportBatchReadback/);
  assert.match(controllerSource, /useAudioImportPublishReadback/);
  assert.match(readbackHooksSource, /setTimeout\(\(\)\s*=>\s*void load\(\),\s*1800\)/);
});

test("批次轮询串行等待回读，配置名称属于不可变版本指纹", async () => {
  const [readbackHooksSource, modelSource] = await Promise.all([
    readFile(readbackHooksSourceUrl, "utf8"),
    readFile(new URL("./audioImportFlowModel.ts", import.meta.url), "utf8")
  ]);

  assert.doesNotMatch(readbackHooksSource, /setInterval\(\(\)\s*=>\s*void load/);
  assert.match(readbackHooksSource, /setTimeout\(\(\)\s*=>\s*void load\(\),\s*1800\)/);
  assert.match(modelSource, /name:\s*draft\.name\.trim\(\)/);
});

test("记录预览使用字段映射感知的安全格式化器，不直接渲染源值", async () => {
  const source = await readFile(formStepsSourceUrl, "utf8");

  assert.match(source, /formatAudioImportPreviewValue/);
  assert.match(source, /audioUrlFieldPath:\s*draft\.fieldMapping\.audioUrl/);
  assert.doesNotMatch(source, /item\[field\]/);
  assert.doesNotMatch(source, /JSON\.stringify\(value\)/);
});

test("抽屉对用户只展示业务阶段，Dagster 仅出现在技术详情", async () => {
  const source = await readFile(drawerSourceUrl, "utf8");
  const publicSection = source.split("data-testid=\"audio-import-technical-details\"")[0];

  for (const label of ["等待执行", "读取清单", "下载音频", "校验入库", "生成会话"]) {
    assert.match(publicSection, new RegExp(label));
  }
  assert.doesNotMatch(publicSection, /Dagster/i);
  assert.match(source, /data-testid="audio-import-technical-details"[^]*Dagster/i);
});

test("批次在生成逐条记录前失败时仍展示错误占位并允许重试", async () => {
  const source = await readFile(drawerSourceUrl, "utf8");

  assert.match(source, /hasImportBatchFailures/);
  assert.match(source, /canRetryImportBatch/);
  assert.match(source, /批次在生成逐条失败记录前终止/);
  assert.doesNotMatch(source, /disabled=\{!failedItems\.length/);
});

test("失败记录只展示 BFF 恢复建议与服务端重试链，并按 failed 状态完整分页", async () => {
  const [apiSource, controllerSource, drawerSource, modelSource] = await Promise.all([
    readFile(apiSourceUrl, "utf8"),
    readFile(controllerSourceUrl, "utf8"),
    readFile(drawerSourceUrl, "utf8"),
    readFile(new URL("./audioImportFlowModel.ts", import.meta.url), "utf8")
  ]);

  assert.match(apiSource, /listAudioImportBatchItems/);
  assert.match(apiSource, /next_cursor/);
  assert.match(apiSource, /observedCursors/);
  assert.match(controllerSource, /listAudioImportBatchItems\(id,\s*\{\s*status:\s*"failed"\s*\}\)/);
  assert.match(modelSource, /recovery_suggestion/);
  assert.match(modelSource, /retry_lineage/);
  assert.match(modelSource, /retryable:\s*item\.retryable === true/);
  assert.match(drawerSource, /item\.recoverySuggestion/);
  assert.match(drawerSource, /item\.retryLineage\.sourceItemId/);
  assert.match(drawerSource, /item\.retryLineage\.rootItemId/);
  assert.doesNotMatch(drawerSource, /AUDIO_URL_EXPIRED[^]*源音频地址已过期/);
});

test("真实测试错误不会被步骤 blocker 遮住，发布同步成功可立即拉取", async () => {
  const [drawerSource, controllerSource] = await Promise.all([
    readFile(drawerSourceUrl, "utf8"),
    readFile(controllerSourceUrl, "utf8")
  ]);

  assert.match(drawerSource, /\[flow\.blockers\[0\],\s*flow\.detail\]/);
  assert.match(drawerSource, /const liveDetail/);
  assert.match(drawerSource, /data-testid="audio-import-save-draft"/);
  assert.match(controllerSource, /const saveDraft = async/);
  assert.match(controllerSource, /taskVersionStatus !== "draft"/);
  assert.match(controllerSource, /release\.data\.status[^]*published/);
});

test("生产读模型为空时仍保留首批音频导入入口", async () => {
  const [moduleSource, projectionSource] = await Promise.all([
    readFile(dataModuleSourceUrl, "utf8"),
    readFile(dataProjectionSourceUrl, "utf8")
  ]);

  assert.match(moduleSource, /EmptyAudioImportWorkspace/);
  assert.match(moduleSource, /AUDIO_IMPORT_COLD_START_ASSET\s*=\s*"auris\/audio\/raw_recordings"/);
  assert.match(moduleSource, /targetAssetKey:\s*AUDIO_IMPORT_COLD_START_ASSET/);
  assert.doesNotMatch(moduleSource, /targetAssetKey:\s*workspace\.selectedAsset\.assetKey/);
  assert.match(moduleSource, /!projection\.blockedReason/);
  assert.doesNotMatch(projectionSource, /!projectionItems\?\.length/);
});

test("抽屉步骤展示四态并禁止越过最早未完成步骤", async () => {
  const [drawerSource, controllerSource, modelSource] = await Promise.all([
    readFile(drawerSourceUrl, "utf8"),
    readFile(controllerSourceUrl, "utf8"),
    readFile(new URL("./audioImportFlowModel.ts", import.meta.url), "utf8")
  ]);

  for (const status of ["unvisited", "incomplete", "verified", "error"]) {
    assert.match(modelSource, new RegExp(`"${status}"`));
  }
  assert.match(drawerSource, /data-step-status=\{stepState\.status\}/);
  assert.match(drawerSource, /flow\.canVisitStep\(item\.id\)/);
  assert.doesNotMatch(drawerSource, /flow\.setStep\(item\.id\)/);
  assert.match(controllerSource, /canNavigateToAudioImportStep/);
  assert.match(controllerSource, /visitedSteps/);
  assert.match(controllerSource, /attemptedSteps/);
  assert.match(controllerSource, /const openDrawer[^]*step:\s*1/);
});

test("完整校验通过前保存草稿禁用并展示首个阻断原因", async () => {
  const [drawerSource, controllerSource] = await Promise.all([
    readFile(drawerSourceUrl, "utf8"),
    readFile(controllerSourceUrl, "utf8")
  ]);

  assert.match(controllerSource, /saveDraftBlockedReason/);
  assert.match(controllerSource, /validateCompleteAudioImport/);
  assert.match(drawerSource, /data-testid="audio-import-save-blocked-reason"/);
  assert.match(
    drawerSource,
    /data-testid="audio-import-save-draft"[^]*disabled=\{[^}]*flow\.saveDraftBlockedReason/
  );
});

test("保存草稿必须回读 TaskVersion 并校验冻结的 Connector 版本", async () => {
  const controllerSource = await readFile(controllerSourceUrl, "utf8");

  assert.match(controllerSource, /getTaskVersion\(saved\.data\.id\)/);
  assert.match(controllerSource, /readbackStatus\s*!==\s*"draft"/);
  assert.match(controllerSource, /readbackBinding\.connector_id/);
  assert.match(controllerSource, /readbackBinding\.connector_version/);
  assert.match(controllerSource, /草稿写入后回读不一致/);
  assert.match(
    controllerSource,
    /taskVersionId:\s*readbackId[^]*taskVersionStatus:\s*readbackStatus/
  );
});

test("必填控件声明 required 与 aria-required，校验失败聚焦首错", async () => {
  const [formSource, drawerSource, controllerSource] = await Promise.all([
    readFile(formStepsSourceUrl, "utf8"),
    readFile(drawerSourceUrl, "utf8"),
    readFile(controllerSourceUrl, "utf8")
  ]);

  for (const id of [
    "audio-import-platform-connection",
    "audio-import-platform-tenant",
    "audio-import-base-url",
    "audio-import-credential-ref",
    "audio-import-map-external-record-id",
    "audio-import-map-audio-url",
    "audio-import-map-started-at",
    "audio-import-cursor-field",
    "audio-import-initial-window",
    "audio-import-target-asset"
  ]) {
    assert.match(formSource, new RegExp(id));
  }
  assert.match(formSource, /required=\{required\}/);
  assert.match(formSource, /aria-required=\{required\}/);
  assert.match(formSource, /aria-invalid=/);
  assert.match(formSource, /id="audio-import-test-connection"/);
  assert.match(formSource, /id="audio-import-preview-records"/);
  assert.match(controllerSource, /firstAudioImportErrorFieldId/);
  assert.match(controllerSource, /validationFocusRevision/);
  assert.match(drawerSource, /document\.getElementById\(flow\.validationFocusFieldId\)/);
});

test("Dialog 圈闭焦点、隔离背景并在关闭后恢复触发点", async () => {
  const source = await readFile(drawerSourceUrl, "utf8");

  assert.match(source, /event\.key === "Tab"/);
  assert.match(source, /setAttribute\("inert",\s*""\)/);
  assert.match(source, /setAttribute\("aria-hidden",\s*"true"\)/);
  assert.match(source, /removeAttribute\("inert"\)/);
  assert.match(source, /previous\?\.isConnected[^]*previous\.focus\(\)/);
  assert.match(source, /role="dialog"/);
  assert.match(source, /aria-modal="true"/);
});

test("最近批次优先从 BFF 列表恢复，sessionStorage 仅作为提示", async () => {
  const [apiSource, controllerSource] = await Promise.all([
    readFile(apiSourceUrl, "utf8"),
    readFile(controllerSourceUrl, "utf8")
  ]);

  assert.match(apiSource, /listAudioImportBatches/);
  assert.match(apiSource, /\/v1\/import-batches/);
  assert.match(apiSource, /connector_id/);
  assert.match(apiSource, /task_version_id/);
  assert.match(apiSource, /target_asset_key/);
  assert.match(controllerSource, /latestAudioImportBatchIdFromBatches/);
  assert.match(controllerSource, /rememberedBatchHint/);
  assert.match(controllerSource, /listAudioImportBatches/);
  assert.doesNotMatch(controllerSource, /remembered\s*\?\s*\{\s*batchId:\s*remembered/);
});

test("已发布配置从 BFF 恢复验证语义并可直达发布与批次详情", async () => {
  const [controllerSource, e2eSource] = await Promise.all([
    readFile(controllerSourceUrl, "utf8"),
    readFile(platformE2eSourceUrl, "utf8")
  ]);

  assert.match(controllerSource, /recoveredPublishedVersion/);
  assert.match(controllerSource, /testedFingerprint:\s*restoredFingerprint/);
  assert.match(controllerSource, /previewedFingerprint:\s*restoredFingerprint/);
  assert.match(controllerSource, /visitedSteps:\s*\[1,\s*2,\s*3,\s*4,\s*5,\s*6\]/);
  assert.match(controllerSource, /taskVersionStatus[^]*published[^]*step:\s*6/);
  assert.match(e2eSource, /published configuration recovery must restore verified step semantics/);
  assert.match(e2eSource, /data-step-status/);
  assert.match(e2eSource, /newContext\(\{[^]*storageState/);
  assert.match(e2eSource, /window\.sessionStorage\.length/);
  assert.match(e2eSource, /coldContextRecovered:\s*true/);
});

test("查看新会话按 audio_session_id 精确进入主调听并支持刷新恢复", async () => {
  const [
    drawerSource,
    moduleSource,
    navigationSource,
    navigationUrlStateSource,
    readModelSource,
    queueLoaderSource,
    e2eSource
  ] = await Promise.all([
    readFile(drawerSourceUrl, "utf8"),
    readFile(dataModuleSourceUrl, "utf8"),
    readFile(shellNavigationSourceUrl, "utf8"),
    readFile(navigationUrlStateSourceUrl, "utf8"),
    readFile(listeningReadModelSourceUrl, "utf8"),
    readFile(listeningQueueLoaderSourceUrl, "utf8"),
    readFile(platformE2eSourceUrl, "utf8")
  ]);

  assert.match(drawerSource, /onOpenListeningSession/);
  assert.match(drawerSource, />查看新会话/);
  assert.match(drawerSource, />快速播放/);
  assert.match(drawerSource, /AudioImportSessionPanel/);
  assert.match(moduleSource, /objectKind:\s*"audioSession"/);
  assert.match(moduleSource, /objectId:\s*audioSessionId/);
  assert.match(moduleSource, /onOpenListeningSession=/);
  assert.match(navigationUrlStateSource, /audio_session_id/);
  assert.match(navigationUrlStateSource, /review_task_id/);
  assert.match(navigationUrlStateSource, /root_trace_id/);
  assert.match(
    navigationUrlStateSource,
    /const audioSessionId\s*=\s*query\.get\("audio_session_id"\)[^]*restoredModule\s*=\s*audioSessionId/
  );
  assert.match(navigationSource, /popstate/);
  assert.match(readModelSource, /requestedAudioSessionId/);
  assert.match(queueLoaderSource, /backendAudioSessionSample/);
  assert.match(readModelSource, /不会被 pending 队列首项覆盖/);
  assert.match(e2eSource, /pending review queue must not replace the imported audio_session_id/);
  assert.match(e2eSource, /audio_session_id URL intent must restore/);
});

test("快速播放在播放器绑定授权 URL 后直接触发真实媒体请求", async () => {
  const [source, e2eSource] = await Promise.all([
    readFile(sessionPanelSourceUrl, "utf8"),
    readFile(platformE2eSourceUrl, "utf8")
  ]);

  assert.doesNotMatch(source, /requestAnimationFrame/);
  assert.match(
    source,
    /audio\.src\s*=\s*nextUrl[^]*setPlaybackUrl\(nextUrl\)[^]*audio\.load\(\)[^]*await audio\.play\(\)/
  );
  assert.match(
    e2eSource,
    /const audioSessionId =[^]*createdSessionIds\.includes\(audioSessionId\)/,
    "批次会话属于无序集合，E2E 必须校验 UI 实际打开的会话归属而不是假定第一条顺序"
  );
  assert.match(e2eSource, /getAttribute\("src"\)\s*===\s*expectedUrl/);
  assert.match(e2eSource, /page\.request\.get\([^]*Range:\s*"bytes=0-1023"/);
  assert.match(e2eSource, /playbackUiBound:\s*true/);
  assert.match(e2eSource, /playbackRangeVerified:\s*true/);
});
