import type { Dispatch, SetStateAction } from "react";

import {
  createTenantResource,
  getApiRuntimeScope,
  getTenantResource,
  listTaskVersions,
  patchTenantResource
} from "../../api/client";
import {
  getImportBatch,
  rememberLatestAudioImportBatch,
  runPublishedAudioImportTask
} from "../../api/audioImportClient";
import type { OperationNotice } from "../../shared/contracts/operations";
import type {
  TenantDraft,
  TenantRiskFilter,
  TenantRow
} from "./types";

type Setter<T> = Dispatch<SetStateAction<T>>;

export const EMPTY_TENANT_DRAFT: TenantDraft = {
  name: "",
  admin: "项目管理员",
  scene: "汽车门店质检",
  quotaTemplate: "标准配额"
};

const asRecord = (value: unknown) =>
  value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
const errorText = (error: unknown, fallback: string) =>
  error instanceof Error ? error.message : fallback;

type SharedMutationSetters = {
  setTenantAction: Setter<string | null>;
  setTenantNotice: Setter<OperationNotice>;
};

type CreateTenantMutationInput = SharedMutationSetters & {
  canCreateTenant: boolean;
  canManageTenants: boolean;
  draftTenant: TenantDraft;
  setDraftTenant: Setter<TenantDraft>;
  setRiskFilter: Setter<TenantRiskFilter>;
  setSelectedTenantName: Setter<string>;
  setTenantBackendIds: Setter<Record<string, string>>;
  setTenantCreateOpen: Setter<boolean>;
  setTenantSceneCreateOpen: Setter<boolean>;
  setTenantSceneDraft: Setter<string>;
  setTenants: Setter<TenantRow[]>;
  tenantAdminUnavailableReason: string;
  tenantSceneNeedsSetup: boolean;
};

export function createTenantMutation(input: CreateTenantMutationInput) {
  return async () => {
    const {
      canCreateTenant,
      canManageTenants,
      draftTenant,
      setDraftTenant,
      setRiskFilter,
      setSelectedTenantName,
      setTenantAction,
      setTenantBackendIds,
      setTenantCreateOpen,
      setTenantNotice,
      setTenantSceneCreateOpen,
      setTenantSceneDraft,
      setTenants,
      tenantAdminUnavailableReason,
      tenantSceneNeedsSetup
    } = input;
    if (!canManageTenants) {
      setTenantNotice({ status: "error", title: "租户操作不可用", detail: tenantAdminUnavailableReason });
      return;
    }
    if (!canCreateTenant) {
      setTenantNotice({
        status: "error",
        title: "租户创建失败",
        detail: "租户名称和管理员不能为空。"
      });
      return;
    }
    const name = draftTenant.name.trim();
    const storage = draftTenant.quotaTemplate === "企业配额" ? "1.0 TB" : draftTenant.quotaTemplate === "轻量试点" ? "0.2 TB" : "0.6 TB";
    const tenantId = `tenant_${name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") || Date.now().toString(36)}`;
    setTenantAction("create");
    setTenantNotice({ status: "pending", title: "正在创建租户", detail: `${name} 正在写入 /api/v1/tenants，并等待读回隔离边界。` });
    try {
      const receipt = await createTenantResource({
        tenant_id: tenantId,
        tenant_code: tenantId,
        name,
        status: tenantSceneNeedsSetup ? "configuring" : "trial",
        admin: draftTenant.admin.trim(),
        scene: draftTenant.scene,
        quota_template: draftTenant.quotaTemplate,
        source: "tenant_ui"
      });
      const readback = await getTenantResource(receipt.data.id);
      const backendStatus = String(readback.data.status ?? receipt.data.status);
      const nextTenant = {
        name: String(readback.data.name ?? name),
        status: backendStatus === "active" ? "活跃" : backendStatus === "paused" ? "暂停" : backendStatus === "trial" ? "试运行" : "配置中",
        projects: Number(readback.data.projects ?? 0),
        members: Number(readback.data.members ?? 1),
        storage,
        risk: tenantSceneNeedsSetup ? "待配置" : "正常"
      };
      setTenants((current) => [nextTenant, ...current.filter((tenant) => tenant.name !== nextTenant.name)]);
      setTenantBackendIds((current) => ({ ...current, [nextTenant.name]: receipt.data.id }));
      setSelectedTenantName(nextTenant.name);
      setRiskFilter("all");
      setDraftTenant(EMPTY_TENANT_DRAFT);
      setTenantSceneCreateOpen(false);
      setTenantSceneDraft("");
      setTenantCreateOpen(false);
      setTenantNotice({
        status: "success",
        title: "租户已创建并读回",
        detail: `${receipt.data.id} · ${nextTenant.status} · trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? readback.meta?.trace_id ?? "no-trace"}。`
      });
    } catch (error) {
      setTenantNotice({
        status: "error",
        title: "租户创建失败",
        detail: `${errorText(error, "unknown error")}。表单已保留，可修正权限或网络后重试。`
      });
    } finally {
      setTenantAction(null);
    }
  };
}

type UpdateTenantStatusMutationInput = SharedMutationSetters & {
  canManageTenants: boolean;
  selectedTenant: TenantRow;
  setTenants: Setter<TenantRow[]>;
  tenantAction: string | null;
  tenantAdminUnavailableReason: string;
  tenantBackendIds: Record<string, string>;
};

export function updateTenantStatusMutation(input: UpdateTenantStatusMutationInput) {
  return async () => {
    const {
      canManageTenants,
      selectedTenant,
      setTenantAction,
      setTenantNotice,
      setTenants,
      tenantAction,
      tenantAdminUnavailableReason,
      tenantBackendIds
    } = input;
    if (!canManageTenants) {
      setTenantNotice({ status: "error", title: "租户操作不可用", detail: tenantAdminUnavailableReason });
      return;
    }
    if (tenantAction === "tenant-status") return;
    const nextStatus = selectedTenant.status === "暂停" ? "活跃" : "暂停";
    const tenantId = tenantBackendIds[selectedTenant.name];
    if (!tenantId) {
      setTenantNotice({ status: "error", title: "租户状态不可修改", detail: "当前原型行缺少后端 tenant_id，禁止仅修改浏览器状态。" });
      return;
    }
    setTenantAction("tenant-status");
    setTenantNotice({ status: "pending", title: `正在${nextStatus === "暂停" ? "暂停" : "恢复"}租户`, detail: `${tenantId} 正在写入 BFF 并等待状态读回。` });
    try {
      const receipt = await patchTenantResource(tenantId, {
        status: nextStatus === "暂停" ? "paused" : "active",
        source: "tenant_ui_status_change"
      });
      const readback = await getTenantResource(tenantId);
      const backendStatus = String(readback.data.status ?? receipt.data.status);
      const visibleStatus = backendStatus === "paused" ? "暂停" : "活跃";
      setTenants((current) => current.map((tenant) => tenant.name === selectedTenant.name
        ? { ...tenant, status: visibleStatus, risk: visibleStatus === "暂停" ? "待恢复" : "正常" }
        : tenant));
      setTenantNotice({
        status: "success",
        title: `租户已${visibleStatus === "暂停" ? "暂停" : "恢复"}并读回`,
        detail: `${tenantId} · ${backendStatus} · trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? readback.meta?.trace_id ?? "no-trace"}。`
      });
    } catch (error) {
      setTenantNotice({
        status: "error",
        title: "租户状态写入失败",
        detail: `${errorText(error, "unknown error")}。浏览器状态未变更，可重试。`
      });
    } finally {
      setTenantAction(null);
    }
  };
}

type PullTenantAsrMutationInput = SharedMutationSetters & {
  selectedTenant: TenantRow;
  tenantAction: string | null;
};

export function pullTenantAsrMutation(input: PullTenantAsrMutationInput) {
  return async () => {
    const {
      selectedTenant,
      setTenantAction,
      setTenantNotice,
      tenantAction
    } = input;
    if (selectedTenant.status === "暂停") {
      setTenantNotice({
        status: "error",
        title: "平台音频拉取被阻断",
        detail: `${selectedTenant.name} 已暂停，恢复租户后才能创建音频导入运行。`
      });
      return;
    }
    if (tenantAction === "asr-pull") return;
    setTenantAction("asr-pull");
    setTenantNotice({
      status: "pending",
      title: "正在创建平台音频导入",
      detail: "正在按当前租户的平台范围查找最新已发布音频导入 TaskVersion，并以 execution_mode=production 创建运行。"
    });
    try {
      const runtimeTenantId = getApiRuntimeScope().tenantId;
      if (
        !runtimeTenantId
        || !selectedTenant.tenantId
        || runtimeTenantId !== selectedTenant.tenantId
      ) {
        throw new Error(
          "当前租户行与认证会话的 tenant_id 不一致，禁止跨租户选择或运行导入配置"
        );
      }
      const versionsResponse = await listTaskVersions();
      const publishedImports = versionsResponse.data.items.filter((item) =>
        item.task_type_id === "audio-platform-import"
        && String(item.status ?? "").toLowerCase() === "published"
      );
      const matchingVersion = publishedImports[publishedImports.length - 1];
      const taskVersionId = String(matchingVersion?.id ?? matchingVersion?.task_version_id ?? "");
      if (!taskVersionId) {
        throw new Error("当前认证租户与项目下没有已发布的平台音频导入配置，请先在数据资产完成配置和发布");
      }
      const inputBinding = asRecord(matchingVersion?.input_binding)
        ?? asRecord(asRecord(matchingVersion?.payload)?.input_binding)
        ?? {};
      const targetAssetKey = String(inputBinding.target_asset_key ?? "").trim();
      if (!targetAssetKey) {
        throw new Error("已发布导入配置缺少冻结的 target_asset_key，不能安全交接同步批次");
      }
      const receipt = await runPublishedAudioImportTask(taskVersionId);
      const runId = receipt.data.raw.run_id ?? receipt.data.id;
      const runPayload = asRecord(receipt.data.raw.payload) ?? {};
      const batchId = String(receipt.data.raw.import_batch_id ?? runPayload.import_batch_id ?? "");
      if (!batchId) throw new Error("TaskRun 回执缺少 import_batch_id");
      const batchReadback = await getImportBatch(batchId);
      const batchStatus = String(batchReadback.data.status ?? "queued");
      rememberLatestAudioImportBatch(targetAssetKey, batchId);
      const traceId = receipt.meta?.trace_id ?? receipt.data.trace_id ?? "pending";
      setTenantNotice({
        status: batchStatus === "succeeded"
          ? "success"
          : ["partial", "failed", "cancelled"].includes(batchStatus)
            ? "error"
            : "pending",
        title: "音频导入运行已创建并回读",
        detail: `${selectedTenant.name} 使用当前认证项目下的已发布版本 ${taskVersionId} 创建 production 运行 ${runId}；同步批次 ${batchId} 当前为 ${batchStatus}，Trace ${traceId}。打开数据资产 ${targetAssetKey} 可继续回读本批次。`
      });
    } catch (error) {
      setTenantNotice({
        status: "error",
        title: "平台音频拉取提交失败",
        detail: errorText(error, "生产音频导入运行创建失败，请重试。")
      });
    } finally {
      setTenantAction(null);
    }
  };
}
