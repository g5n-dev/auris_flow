import { useMemo } from "react";
import type { ModuleCommandMode, ModuleScopeShortcut } from "../shared/contracts/application";
import type { ModuleMetric, ProjectionMetricSource } from "../shared/contracts/modules";
import { ModuleWorkspaceView } from "./ModuleWorkspaceView";
import type { ModuleWorkspaceProps } from "./moduleWorkspaceContracts";
import { buildScopeShortcuts, moduleConfigs, moduleInteractionModels } from "./moduleWorkspaceCatalog";
import { deriveProjectionMetrics } from "./projectionMetrics";
import { sceneAwareModuleConfig } from "./sceneAwareModuleConfig";
import { useModuleCommands } from "./useModuleCommands";
import { useModuleProjection } from "./useModuleProjection";
import { useModuleWorkspaceNavigation } from "./useModuleWorkspaceNavigation";

export function ModuleWorkspace(props: ModuleWorkspaceProps) {
  const {
    gateway,
    moduleKey,
    currentUser,
    deepLink,
    selectedAssetKey,
    topbarContext,
    projectIdByName
  } = props;
  const baseConfig = moduleConfigs[moduleKey];
  const interaction = moduleInteractionModels[moduleKey];
  const projection = useModuleProjection({
    gateway,
    moduleKey,
    currentUser,
    topbarContext,
    projectIdByName
  });
  const config = useMemo(
    () => sceneAwareModuleConfig(baseConfig, moduleKey, projection.workspaceSceneBinding),
    [baseConfig, moduleKey, projection.workspaceSceneBinding]
  );
  const navigation = useModuleWorkspaceNavigation({
    moduleKey,
    initialTab: baseConfig.tabs[0].id,
    tabs: config.tabs,
    deepLink
  });
  const commands = useModuleCommands({
    gateway,
    moduleKey,
    activeTab: navigation.activeTab,
    config,
    interaction,
    topbarContext,
    selectedAssetKey,
    workspaceSceneBinding: projection.workspaceSceneBinding
  });
  const scopeShortcuts = useMemo(() => buildScopeShortcuts(config), [config]);
  const dataTabMetricOverrides: Partial<Record<string, ModuleMetric[]>> = {
    people: [
      { label: "声纹对象", value: "3", delta: "员工/未知簇", tone: "blue" },
      { label: "可入库", value: "1", delta: "人工确认后生效", tone: "green" },
      { label: "待复核", value: "2", delta: "串音/身份/低质", tone: "amber" },
      { label: "平均评分", value: "75", delta: "质量评分", tone: "teal" }
    ],
    events: [
      { label: "认证事件", value: "46", delta: "报价/试驾/接待", tone: "blue" },
      { label: "已关联", value: "92.1%", delta: "音频+单据", tone: "green" },
      { label: "漏单", value: "39", delta: "缺音频或单据", tone: "red" },
      { label: "待回填", value: "12", delta: "影响下游", tone: "amber" }
    ],
    relations: [
      { label: "关联链路", value: "5,612", delta: "实体/事件/资产", tone: "teal" },
      { label: "跨项目复用", value: "328", delta: "员工/门店/音频", tone: "blue" },
      { label: "断链风险", value: "24", delta: "待修复", tone: "red" },
      { label: "血缘完整", value: "94.3%", delta: "可追踪", tone: "green" }
    ]
  };
  const baseMetrics = moduleKey === "data"
    ? dataTabMetricOverrides[navigation.activeTab] ?? config.metrics
    : config.metrics;
  const activeMetrics = deriveProjectionMetrics(
    moduleKey,
    baseMetrics,
    projection.projectionReceipt,
    projection.projectionStatus
  );
  const projectionMetricSource: ProjectionMetricSource = projection.projectionStatus === "synced"
    ? "bff"
    : projection.projectionStatus === "empty"
      ? "bff-empty"
      : projection.projectionStatus === "degraded"
        ? "mock"
        : "pending";
  const projectionListHydrated = moduleKey === "tenants" || moduleKey === "projects" || moduleKey === "knowledge";
  const projectionContentSource: "bff" | "mock" | "none" = projection.projectionStatus === "synced" && projectionListHydrated
    ? "bff"
    : projection.projectionStatus === "degraded" || projection.projectionStatus === "synced"
      ? "mock"
      : "none";
  const pageClassName = [
    "module-page",
    moduleKey === "home" ? "home-module-page" : "",
    moduleKey === "tenants" ? "tenant-module-page" : "",
    moduleKey === "data" ? "data-module-page" : "",
    moduleKey === "knowledge" ? "knowledge-module-page" : "",
    moduleKey === "labels" ? "label-module-page" : "",
    moduleKey === "canvas" ? "task-config-page" : "",
    moduleKey === "evaluation" ? "evaluation-module-page" : "",
    moduleKey === "insights" ? "insight-module-page" : ""
  ].filter(Boolean).join(" ");

  const openScopeShortcut = (shortcut: ModuleScopeShortcut) => {
    navigation.setActiveTab(shortcut.tabId);
    commands.setCommandMode(null);
    navigation.setScopeMenuOpen(false);
    commands.setCommandStatus("success");
    commands.setCommandFeedback(`${shortcut.label} 已定位到「${shortcut.tabLabel}」：可继续搜索、筛选、写入或导出当前上下文。`);
  };
  const openScopeCommand = (mode: Extract<ModuleCommandMode, "filter" | "write">) => {
    commands.setCommandMode(mode);
    navigation.setScopeMenuOpen(false);
    commands.setCommandStatus("idle");
    commands.setCommandFeedback(mode === "write" ? `${config.title} 写入入口已打开；写操作必须使用当前 SceneProfile 的强引用。` : `${config.title} 筛选入口已打开，可按当前 scope 收敛数据。`);
  };

  return (
    <ModuleWorkspaceView
      workspace={props}
      config={config}
      interaction={interaction}
      scopeShortcuts={scopeShortcuts}
      navigation={navigation}
      commands={commands}
      projection={projection}
      activeMetrics={activeMetrics}
      projectionMetricSource={projectionMetricSource}
      projectionItems={projection.projectionReceipt?.collectionItems}
      projectionListHydrated={projectionListHydrated}
      projectionContentSource={projectionContentSource}
      pageClassName={pageClassName}
      openScopeShortcut={openScopeShortcut}
      openScopeCommand={openScopeCommand}
    />
  );
}
