import type { Dispatch, SetStateAction } from "react";

import {
  createPlatformSyncJob,
  createTenantResource,
  getTenantResource,
  patchTenantResource
} from "../../api/client";
import type { OperationNotice } from "../../shared/contracts/operations";
import type {
  TenantAsrBinding,
  TenantDraft,
  TenantRiskFilter,
  TenantRow
} from "./types";

type Setter<T> = Dispatch<SetStateAction<T>>;

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
    if (!input.canManageTenants) {
      input.setTenantNotice({ status: "error", title: "租户操作不可用", detail: input.tenantAdminUnavailableReason });
      return;
    }
    if (!input.canCreateTenant) {
      input.setTenantNotice({
        status: "error",
        title: "租户创建失败",
        detail: "租户名称和管理员不能为空。"
      });
      return;
    }
    const name = input.draftTenant.name.trim();
    const storage = input.draftTenant.quotaTemplate === "企业配额" ? "1.0 TB" : input.draftTenant.quotaTemplate === "轻量试点" ? "0.2 TB" : "0.6 TB";
    const tenantId = `tenant_${name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") || Date.now().toString(36)}`;
    input.setTenantAction("create");
    input.setTenantNotice({ status: "pending", title: "正在创建租户", detail: `${name} 正在写入 /api/v1/tenants，并等待读回隔离边界。` });
    try {
      const receipt = await createTenantResource({
        tenant_id: tenantId,
        tenant_code: tenantId,
        name,
        status: input.tenantSceneNeedsSetup ? "configuring" : "trial",
        admin: input.draftTenant.admin.trim(),
        scene: input.draftTenant.scene,
        quota_template: input.draftTenant.quotaTemplate,
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
        risk: input.tenantSceneNeedsSetup ? "待配置" : "正常"
      };
      input.setTenants((current) => [nextTenant, ...current.filter((tenant) => tenant.name !== nextTenant.name)]);
      input.setTenantBackendIds((current) => ({ ...current, [nextTenant.name]: receipt.data.id }));
      input.setSelectedTenantName(nextTenant.name);
      input.setRiskFilter("all");
      input.setDraftTenant({ name: "", admin: "项目管理员", scene: "汽车门店质检", quotaTemplate: "标准配额" });
      input.setTenantSceneCreateOpen(false);
      input.setTenantSceneDraft("");
      input.setTenantCreateOpen(false);
      input.setTenantNotice({
        status: "success",
        title: "租户已创建并读回",
        detail: `${receipt.data.id} · ${nextTenant.status} · trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? readback.meta?.trace_id ?? "no-trace"}。`
      });
    } catch (error) {
      input.setTenantNotice({
        status: "error",
        title: "租户创建失败",
        detail: `${error instanceof Error ? error.message : "unknown error"}。表单已保留，可修正权限或网络后重试。`
      });
    } finally {
      input.setTenantAction(null);
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
    if (!input.canManageTenants) {
      input.setTenantNotice({ status: "error", title: "租户操作不可用", detail: input.tenantAdminUnavailableReason });
      return;
    }
    if (input.tenantAction === "tenant-status") return;
    const nextStatus = input.selectedTenant.status === "暂停" ? "活跃" : "暂停";
    const tenantId = input.tenantBackendIds[input.selectedTenant.name];
    if (!tenantId) {
      input.setTenantNotice({ status: "error", title: "租户状态不可修改", detail: "当前原型行缺少后端 tenant_id，禁止仅修改浏览器状态。" });
      return;
    }
    input.setTenantAction("tenant-status");
    input.setTenantNotice({ status: "pending", title: `正在${nextStatus === "暂停" ? "暂停" : "恢复"}租户`, detail: `${tenantId} 正在写入 BFF 并等待状态读回。` });
    try {
      const receipt = await patchTenantResource(tenantId, {
        status: nextStatus === "暂停" ? "paused" : "active",
        source: "tenant_ui_status_change"
      });
      const readback = await getTenantResource(tenantId);
      const backendStatus = String(readback.data.status ?? receipt.data.status);
      const visibleStatus = backendStatus === "paused" ? "暂停" : "活跃";
      input.setTenants((current) => current.map((tenant) => tenant.name === input.selectedTenant.name
        ? { ...tenant, status: visibleStatus, risk: visibleStatus === "暂停" ? "待恢复" : "正常" }
        : tenant));
      input.setTenantNotice({
        status: "success",
        title: `租户已${visibleStatus === "暂停" ? "暂停" : "恢复"}并读回`,
        detail: `${tenantId} · ${backendStatus} · trace ${receipt.meta?.trace_id ?? receipt.data.trace_id ?? readback.meta?.trace_id ?? "no-trace"}。`
      });
    } catch (error) {
      input.setTenantNotice({
        status: "error",
        title: "租户状态写入失败",
        detail: `${error instanceof Error ? error.message : "unknown error"}。浏览器状态未变更，可重试。`
      });
    } finally {
      input.setTenantAction(null);
    }
  };
}

type PullTenantAsrMutationInput = SharedMutationSetters & {
  activeTenantAsr: TenantAsrBinding;
  selectedTenant: TenantRow;
  setTenantAsrRunOverrides: Setter<Record<string, Array<[string, string, string]>>>;
  tenantAction: string | null;
};

export function pullTenantAsrMutation(input: PullTenantAsrMutationInput) {
  return async () => {
    if (input.selectedTenant.status === "暂停") {
      input.setTenantNotice({
        status: "error",
        title: "ASR 拉取被阻断",
        detail: `${input.selectedTenant.name} 已暂停，恢复租户后才能创建增量拉取。`
      });
      return;
    }
    if (input.tenantAction === "asr-pull") return;
    input.setTenantAction("asr-pull");
    input.setTenantNotice({
      status: "pending",
      title: "正在拉取 ASR 数据",
      detail: `${input.activeTenantAsr.provider} 正在通过 /api/v1/platform-sync-jobs 创建增量任务。`
    });
    try {
      const receipt = await createPlatformSyncJob({
        sync_scope: "tenant_asr_incremental",
        tenant_name: input.selectedTenant.name,
        tenant_status: input.selectedTenant.status,
        provider: input.activeTenantAsr.provider,
        service_id: input.activeTenantAsr.serviceId,
        cursor: input.activeTenantAsr.cursor,
        endpoint: input.activeTenantAsr.endpoint,
        output_assets: input.activeTenantAsr.outputAssets.map(([asset, kind]) => ({ asset, kind })),
        source: "tenant_asr_panel"
      });
      const runId = receipt.data.raw.run_id ?? receipt.data.id;
      const traceId = receipt.meta?.trace_id ?? receipt.data.trace_id ?? "pending";
      const happenedAt = new Date().toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false
      });
      const runEntry: [string, string, string] = [happenedAt, "增量拉取已提交", `${runId} / Trace ${traceId}`];
      input.setTenantAsrRunOverrides((current) => ({
        ...current,
        [input.selectedTenant.name]: [
          runEntry,
          ...(current[input.selectedTenant.name] ?? [])
        ].slice(0, 4)
      }));
      input.setTenantAction(null);
      input.setTenantNotice({
        status: "success",
        title: "ASR 拉取已提交",
        detail: `${input.selectedTenant.name} 已创建平台同步任务 ${runId}，Trace ${traceId}。`
      });
    } catch (error) {
      input.setTenantAction(null);
      input.setTenantNotice({
        status: "error",
        title: "ASR 拉取提交失败",
        detail: error instanceof Error ? error.message : "平台同步任务创建失败，请重试。"
      });
    }
  };
}
