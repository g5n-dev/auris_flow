import { useEffect, useState } from "react";

import type { OperationNotice } from "../../shared/contracts/operations";
import { backendTenantIdByName } from "../../shared/runtime/backendEntityIds";
import { withDeepLinkOrigin } from "../../shared/runtime/deepLinks";
import {
  createTenantSceneValue,
  defaultTenantAsrBinding,
  tenantAsrBindings,
  tenantAuditItems,
  tenantMembers,
  tenantProjects,
  tenantRows,
  tenantSceneOptions
} from "./fixtures";
import {
  deriveTenantQuotaRows,
  filterTenants,
  normalizeTenantProjectionItems,
  resolveTenantOutputAssetKey
} from "./model";
import {
  createTenantMutation,
  pullTenantAsrMutation,
  updateTenantStatusMutation
} from "./tenantMutations";
import type { TenantDraft, TenantRiskFilter, TenantRow, TenantModuleProps } from "./types";

export function useTenantWorkspace({
  activeTab,
  setActiveModule,
  navigateToTarget,
  currentUser,
  projectionItems,
  projectionSource
}: TenantModuleProps) {
  const initialTenants = projectionSource === "bff"
    ? normalizeTenantProjectionItems(projectionItems ?? [])
    : tenantRows;
  const [tenants, setTenants] = useState<TenantRow[]>(initialTenants);
  const [selectedTenantName, setSelectedTenantName] = useState(initialTenants[0]?.name ?? "");
  const [tenantQuery, setTenantQuery] = useState("");
  const [riskFilter, setRiskFilter] = useState<TenantRiskFilter>("all");
  const [tenantCreateOpen, setTenantCreateOpen] = useState(false);
  const [tenantSceneCreateOpen, setTenantSceneCreateOpen] = useState(false);
  const [tenantSceneDraft, setTenantSceneDraft] = useState("");
  const [tenantNotice, setTenantNotice] = useState<OperationNotice>({
    status: "idle",
    title: "等待租户操作",
    detail: "筛选、创建、暂停/恢复、ASR 拉取都会写入租户审计回执。"
  });
  const [tenantAction, setTenantAction] = useState<string | null>(null);
  const [tenantBackendIds, setTenantBackendIds] = useState<Record<string, string>>(() => ({ ...backendTenantIdByName }));
  const [tenantAsrRunOverrides, setTenantAsrRunOverrides] = useState<Record<string, Array<[string, string, string]>>>({});
  useEffect(() => {
    const nextTenants: TenantRow[] = projectionSource === "bff"
      ? normalizeTenantProjectionItems(projectionItems ?? [])
      : tenantRows;
    setTenants(nextTenants);
    setSelectedTenantName((current) => nextTenants.some((tenant) => tenant.name === current)
      ? current
      : nextTenants[0]?.name ?? "");
    setTenantBackendIds(projectionSource === "bff"
      ? Object.fromEntries(nextTenants.flatMap((tenant) => tenant.tenantId ? [[tenant.name, tenant.tenantId]] : []))
      : { ...backendTenantIdByName });
  }, [projectionItems, projectionSource]);
  const [draftTenant, setDraftTenant] = useState<TenantDraft>({
    name: "",
    admin: "项目管理员",
    scene: "汽车门店质检",
    quotaTemplate: "标准配额"
  });

  const tenantSceneUnbound = draftTenant.scene === "通用租户（稍后配置）";
  const tenantSceneCustom = Boolean(
    draftTenant.scene &&
      !tenantSceneOptions.some((option) => option.value === draftTenant.scene) &&
      draftTenant.scene !== createTenantSceneValue
  );
  const tenantSceneNeedsSetup = tenantSceneUnbound || tenantSceneCustom;
  const canManageTenants = currentUser.roles.includes("system");
  const tenantAdminUnavailableReason = "当前身份缺少 system 平台角色；租户创建、暂停和恢复必须由平台管理员通过 BFF 执行。";
  const selectedTenant = tenants.find((tenant) => tenant.name === selectedTenantName) ?? tenants[0];
  const canCreateTenant = draftTenant.name.trim().length > 0 && draftTenant.admin.trim().length > 0;
  const activeTenantAsr = tenantAsrBindings[selectedTenant.name] ?? defaultTenantAsrBinding;
  const activeTenantAsrRuns = [
    ...(tenantAsrRunOverrides[selectedTenant.name] ?? []),
    ...activeTenantAsr.runs
  ];
  const filteredTenantRows = filterTenants(tenants, tenantQuery, riskFilter);
  const activeAuditItems = tenantAuditItems[selectedTenant.name] ?? [
    ["刚刚", "租户创建", `${selectedTenant.name} 已建立隔离空间`],
    ["待配置", "项目接入", "创建项目后通过任务配置接入数据"],
    ["待配置", "权限初始化", "补充管理员、标注员和审计角色"]
  ];
  const activeTenantProjects = tenantProjects[selectedTenant.name] ?? [];
  const activeTenantMembers = tenantMembers[selectedTenant.name] ?? [
    { name: draftTenant.admin || "项目管理员", role: "租户管理员", scope: "待分配", status: "待邀请", lastSeen: "未登录" }
  ];
  const quotaRows = deriveTenantQuotaRows(selectedTenant);

  const openTenantOutputAsset = (asset: string) => {
    const assetKey = resolveTenantOutputAssetKey(asset);
    navigateToTarget(withDeepLinkOrigin({
      module: "assets",
      tab: "lineage",
      objectKind: "asset",
      objectId: assetKey,
      focusMode: "lineage",
      title: asset,
      detail: `${selectedTenant.name} / ASR 接入输出资产`
    }, "租户 ASR 接入", "tenants", asset));
  };
  const selectTenantScene = (scene: string) => {
    if (scene === createTenantSceneValue) {
      setTenantSceneCreateOpen(true);
      return;
    }
    setTenantSceneCreateOpen(false);
    setDraftTenant((draft) => ({ ...draft, scene }));
  };
  const createTenantSceneDraft = () => {
    const sceneName = tenantSceneDraft.trim();
    if (!sceneName) {
      setTenantNotice({
        status: "error",
        title: "业务场景名称缺失",
        detail: "请输入新场景名称后再生成场景草案。"
      });
      return;
    }
    setDraftTenant((draft) => ({ ...draft, scene: sceneName }));
    setTenantSceneDraft("");
    setTenantSceneCreateOpen(false);
    setTenantNotice({
      status: "success",
      title: "场景草案已生成",
      detail: `${sceneName} 已写入当前新建租户表单。`
    });
  };

  const createTenant = createTenantMutation({
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
  });
  const updateTenantStatus = updateTenantStatusMutation({
    canManageTenants,
    selectedTenant,
    setTenantAction,
    setTenantNotice,
    setTenants,
    tenantAction,
    tenantAdminUnavailableReason,
    tenantBackendIds
  });
  const pullTenantAsrOnce = pullTenantAsrMutation({
    activeTenantAsr,
    selectedTenant,
    setTenantAction,
    setTenantAsrRunOverrides,
    setTenantNotice,
    tenantAction
  });

  return {
    activeAuditItems,
    activeTab,
    activeTenantAsr,
    activeTenantAsrRuns,
    activeTenantMembers,
    activeTenantProjects,
    canCreateTenant,
    canManageTenants,
    createTenant,
    createTenantSceneDraft,
    draftTenant,
    filteredTenants: filteredTenantRows,
    openTenantOutputAsset,
    pullTenantAsrOnce,
    projectionSource,
    quotaRows,
    riskFilter,
    selectTenantScene,
    selectedTenant,
    selectedTenantName,
    setActiveModule,
    setDraftTenant,
    setRiskFilter,
    setSelectedTenantName,
    setTenantCreateOpen,
    setTenantNotice,
    setTenantQuery,
    setTenantSceneCreateOpen,
    setTenantSceneDraft,
    tenantAction,
    tenantAdminUnavailableReason,
    tenantBackendIds,
    tenantCreateOpen,
    tenantNotice,
    tenantQuery,
    tenantSceneCreateOpen,
    tenantSceneCustom,
    tenantSceneDraft,
    tenantSceneNeedsSetup,
    tenantSceneUnbound,
    updateTenantStatus
  };
}

export type TenantWorkspace = ReturnType<typeof useTenantWorkspace>;
