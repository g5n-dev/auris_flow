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
const controllerSourceUrl = new URL("./audioImportFlowController.ts", import.meta.url);
const readbackHooksSourceUrl = new URL("./audioImportReadbackHooks.ts", import.meta.url);
const dataModuleSourceUrl = new URL("./DataModule.tsx", import.meta.url);
const dataProjectionSourceUrl = new URL("./dataProjection.ts", import.meta.url);

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
