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
  EMPTY_TENANT_DRAFT,
  createTenantMutation,
  pullTenantAsrMutation,
  updateTenantStatusMutation
} from "./tenantMutations";
import type {
  TenantAsrBinding,
  TenantDraft,
  TenantRiskFilter,
  TenantRow,
  TenantModuleProps
} from "./types";

const bffTenantAudioImportUnavailable: TenantAsrBinding = {
  provider: "BFF 未提供连接器详情",
  serviceId: "请在数据资产新建导入配置",
  status: "等待已发布导入配置",
  endpoint: "由已发布 Connector 快照冻结",
  auth: "credential_ref 仅由服务端解析",
  pullMode: "手动立即拉取",
  cursor: "由 BFF 批次回读",
  quota: "由当前项目服务端策略控制",
  retention: "由对象存储策略控制",
  nextRun: "未调度",
  quality: "本轮不执行 ASR",
  pullSources: [],
  outputAssets: [
    ["auris/audio/raw_recordings", "Asset", "平台音频 URL 导入后的内部对象与会话"]
  ],
  runs: [],
  guardrails: [
    ["事实来源", "配置、运行、批次和会话只从当前租户项目 BFF 回读"],
    ["执行入口", "拉取一次只创建最新匹配的已发布 production TaskRun"]
  ]
};

const tenantProjectionRows = (
  source: TenantModuleProps["projectionSource"],
  items?: unknown[]
) => source === "bff" ? normalizeTenantProjectionItems(items ?? []) : tenantRows;

const tenantIdsByName = (
  source: TenantModuleProps["projectionSource"],
  rows: TenantRow[]
) => source === "bff"
  ? Object.fromEntries(rows.flatMap(({ name, tenantId }) => tenantId ? [[name, tenantId]] : []))
  : { ...backendTenantIdByName };

export function useTenantWorkspace({
  activeTab,
  setActiveModule,
  navigateToTarget,
  currentUser,
  projectionItems,
  projectionSource
}: TenantModuleProps) {
  const initialTenants = tenantProjectionRows(projectionSource, projectionItems);
  const [tenants, setTenants] = useState<TenantRow[]>(() => initialTenants);
  const [selectedTenantName, setSelectedTenantName] = useState(initialTenants[0]?.name ?? "");
  const [tenantQuery, setTenantQuery] = useState("");
  const [riskFilter, setRiskFilter] = useState<TenantRiskFilter>("all");
  const [tenantCreateOpen, setTenantCreateOpen] = useState(false);
  const [tenantSceneCreateOpen, setTenantSceneCreateOpen] = useState(false);
  const [tenantSceneDraft, setTenantSceneDraft] = useState("");
  const [tenantNotice, setTenantNotice] = useState<OperationNotice>({
    status: "idle",
    title: "等待租户操作",
    detail: "筛选、创建、暂停/恢复和平台音频拉取都会写入租户审计回执。"
  });
  const [tenantAction, setTenantAction] = useState<string | null>(null);
  const [tenantBackendIds, setTenantBackendIds] = useState<Record<string, string>>(() => ({ ...backendTenantIdByName }));
  useEffect(() => {
    const nextTenants = tenantProjectionRows(projectionSource, projectionItems);
    setTenants(nextTenants);
    setSelectedTenantName((current) => nextTenants.some((tenant) => tenant.name === current)
      ? current
      : nextTenants[0]?.name ?? "");
    setTenantBackendIds(tenantIdsByName(projectionSource, nextTenants));
  }, [projectionItems, projectionSource]);
  const [draftTenant, setDraftTenant] = useState<TenantDraft>(EMPTY_TENANT_DRAFT);

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
  const truthMode = projectionSource === "bff";
  const activeTenantAsr = truthMode
    ? bffTenantAudioImportUnavailable
    : tenantAsrBindings[selectedTenant.name] ?? defaultTenantAsrBinding;
  const activeTenantAsrRuns = activeTenantAsr.runs;
  const filteredTenantRows = filterTenants(tenants, tenantQuery, riskFilter);
  const activeAuditItems = truthMode
    ? []
    : tenantAuditItems[selectedTenant.name] ?? [
      ["刚刚", "租户创建", `${selectedTenant.name} 已建立隔离空间`],
      ["待配置", "项目接入", "创建项目后通过任务配置接入数据"],
      ["待配置", "权限初始化", "补充管理员、标注员和审计角色"]
    ];
  const activeTenantProjects = truthMode
    ? []
    : tenantProjects[selectedTenant.name] ?? [];
  const activeTenantMembers = truthMode
    ? []
    : tenantMembers[selectedTenant.name] ?? [
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
      detail: `${selectedTenant.name} / 平台音频接入输出资产`
    }, "租户音频接入", "tenants", asset));
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
    selectedTenant,
    setTenantAction,
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
