import { ChevronDown, Database, Download, ListFilter, Plus, Search, ShieldCheck, Sparkles } from "lucide-react";
import { Suspense } from "react";
import type { ModuleCommandMode, ModuleScopeShortcut } from "../shared/contracts/application";
import type { ModuleInteractionModel } from "../shared/contracts/moduleInteractions";
import type { ModuleConfig, ModuleMetric, ProjectionMetricSource } from "../shared/contracts/modules";
import { LABEL_DEMO_MODE } from "../shared/runtime/demoMode";
import { DeepLinkSourceBar } from "../shared/ui/DeepLinkSourceBar";
import { MetricCards } from "../shared/ui/MetricCards";
import { AssetsModuleOutlet } from "./AssetsModuleOutlet";
import { CanvasModuleOutlet } from "./CanvasModuleOutlet";
import { DataModuleOutlet } from "./DataModuleOutlet";
import { EvaluationModuleOutlet } from "./EvaluationModuleOutlet";
import { HomeModuleOutlet } from "./HomeModuleOutlet";
import { InsightsModuleOutlet } from "./InsightsModuleOutlet";
import { KnowledgeModule, KnowledgeModuleLoadBoundary } from "./KnowledgeModuleOutlet";
import { LabelsModuleOutlet } from "./LabelsModuleOutlet";
import { ModuleCommandPanel } from "./ModuleCommandPanel";
import { ProjectsModuleOutlet } from "./ProjectsModuleOutlet";
import { SettingsModuleOutlet } from "./SettingsModuleOutlet";
import { TenantsModuleOutlet } from "./TenantsModuleOutlet";
import type { ModuleCommands } from "./useModuleCommands";
import type { ModuleProjectionController } from "./useModuleProjection";
import type { ModuleWorkspaceNavigation } from "./useModuleWorkspaceNavigation";
import type { ModuleWorkspaceProps } from "./moduleWorkspaceContracts";
import { getModuleTitle, moduleWriteArchitectures } from "./moduleWorkspaceCatalog";
import { routeHomeMetric } from "./projectionMetrics";

type ModuleWorkspaceViewProps = {
  workspace: ModuleWorkspaceProps;
  config: ModuleConfig;
  interaction: ModuleInteractionModel;
  scopeShortcuts: ModuleScopeShortcut[];
  navigation: ModuleWorkspaceNavigation;
  commands: ModuleCommands;
  projection: ModuleProjectionController;
  activeMetrics: ModuleMetric[];
  projectionMetricSource: ProjectionMetricSource;
  projectionItems?: unknown[];
  projectionListHydrated: boolean;
  projectionContentSource: "bff" | "mock" | "none";
  pageClassName: string;
  openScopeShortcut: (shortcut: ModuleScopeShortcut) => void;
  openScopeCommand: (mode: Extract<ModuleCommandMode, "filter" | "write">) => void;
};

export function ModuleWorkspaceView({
  workspace,
  config,
  interaction,
  scopeShortcuts,
  navigation,
  commands,
  projection,
  activeMetrics,
  projectionMetricSource,
  projectionItems,
  projectionListHydrated,
  projectionContentSource,
  pageClassName,
  openScopeShortcut,
  openScopeCommand
}: ModuleWorkspaceViewProps) {
  const {
    moduleKey,
    currentUser,
    setActiveModule,
    deepLink,
    navigateToTarget,
    selectedDataAssetId,
    setSelectedDataAssetId,
    selectedAssetKey,
    setSelectedAssetKey,
    openListeningFromDataAsset,
    openAssetsFromDataAsset,
    topbarContext,
    onProjectActivated
  } = workspace;
  const { activeTab, resetWorkspaceScroll, scopeMenuOpen, setActiveTab, setScopeMenuOpen } = navigation;
  const {
    activeFilter,
    closeCommandPanel,
    commandFeedback,
    commandMode,
    commandStatus,
    createMutationRecord,
    currentMutationRecords,
    exportReceipt,
    moduleQuery,
    retryMutationRecord,
    setActiveFilter,
    setCommandFeedback,
    setCommandStatus,
    setModuleQuery,
    toggleCommandMode
  } = commands;
  const {
    projectionContext,
    projectionError,
    projectionReceipt,
    projectionStatus,
    retryProjection,
    workspaceSceneBinding,
    workspaceSceneState
  } = projection;
  const activeSceneManifest = workspaceSceneBinding?.version.manifest ?? null;

  return (
    <div className={pageClassName}>
      <div className="workspace-head module-head">
        <div>
          <div className="eyebrow">{config.eyebrow}</div>
          <h1>{config.title}</h1>
        </div>
        <div className="module-scope-wrap">
          <button
            type="button"
            className={`module-scope ${scopeMenuOpen ? "active" : ""}`}
            aria-haspopup="dialog"
            aria-expanded={scopeMenuOpen}
            onClick={() => setScopeMenuOpen((open) => !open)}
          >
            <Sparkles size={16} />
            <span>{config.scope}</span>
            <ChevronDown size={14} />
          </button>
          {scopeMenuOpen && (
            <div className="module-scope-menu" role="dialog" aria-label={`${config.title}能力入口`}>
              <div className="module-scope-menu-head">
                <span>能力入口</span>
                <strong>{config.title}</strong>
              </div>
              <div className="module-scope-shortcuts">
                {scopeShortcuts.map((shortcut) => (
                  <button key={`${shortcut.label}-${shortcut.tabId}`} type="button" onClick={() => openScopeShortcut(shortcut)}>
                    <span>{shortcut.label}</span>
                    <strong>{shortcut.tabLabel}</strong>
                    <em>{shortcut.detail}</em>
                  </button>
                ))}
              </div>
              <div className="module-scope-menu-actions">
                <button type="button" onClick={() => openScopeCommand("filter")}>
                  <ListFilter size={13} />
                  筛选当前范围
                </button>
                <button type="button" onClick={() => openScopeCommand("write")}>
                  <Plus size={13} />
                  创建 / 修改数据
                </button>
              </div>
            </div>
          )}
        </div>
        <div className="quick-actions">
          <button className={commandMode === "search" ? "active" : ""} aria-expanded={commandMode === "search"} onClick={() => toggleCommandMode("search")}>
            <Search size={15} />
            搜索
          </button>
          <button className={commandMode === "filter" ? "active" : ""} aria-expanded={commandMode === "filter"} onClick={() => toggleCommandMode("filter")}>
            <ListFilter size={15} />
            筛选
          </button>
          <button className={commandMode === "write" ? "active" : ""} aria-expanded={commandMode === "write"} onClick={() => toggleCommandMode("write")}>
            <Plus size={15} />
            写入
          </button>
          <button className={commandMode === "export" ? "active" : ""} aria-expanded={commandMode === "export"} onClick={() => toggleCommandMode("export")}>
            <Download size={15} />
            导出
          </button>
        </div>
      </div>

      {commandMode && (
        <ModuleCommandPanel
          config={config}
          interaction={interaction}
          mode={commandMode}
          query={moduleQuery}
          setQuery={setModuleQuery}
          activeFilter={activeFilter}
          setActiveFilter={setActiveFilter}
          feedback={commandFeedback}
          setFeedback={setCommandFeedback}
          status={commandStatus}
          setStatus={setCommandStatus}
          exportReceipt={exportReceipt}
          moduleKey={moduleKey}
          writeArchitecture={moduleWriteArchitectures[moduleKey]}
          mutationRecords={currentMutationRecords}
          createMutationRecord={createMutationRecord}
          retryMutationRecord={retryMutationRecord}
          setActiveModule={setActiveModule}
          navigateToTarget={navigateToTarget}
          scopeContext={topbarContext}
          sceneBinding={workspaceSceneBinding}
          close={closeCommandPanel}
        />
      )}

      <div className="module-tabs" role="tablist" aria-label={`${config.title} tabs`}>
        {config.tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            className={activeTab === tab.id ? "active" : ""}
            onClick={() => {
              setActiveTab(tab.id);
              resetWorkspaceScroll();
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {deepLink && (
        <DeepLinkSourceBar
          target={deepLink}
          onBack={deepLink.origin?.module ? () => setActiveModule(deepLink.origin?.module ?? "home") : undefined}
          getModuleTitle={getModuleTitle}
        />
      )}

      <MetricCards metrics={activeMetrics} source={projectionMetricSource} onMetricClick={moduleKey === "home" ? (metric) => setActiveModule(routeHomeMetric(metric)) : undefined} />

      <div
        className={`operation-toast module-projection-toast is-${projectionStatus === "degraded" ? "error" : projectionStatus === "pending" ? "pending" : "success"}`}
        role="status"
        aria-live="polite"
        data-testid="module-projection-state"
        data-state={projectionStatus}
        data-source={projectionStatus === "synced" || projectionStatus === "empty" ? "bff" : projectionStatus === "degraded" ? "mock" : "none"}
        data-content-source={projectionContentSource}
      >
        <strong>
          {projectionStatus === "pending"
            ? "正在读取顶部指标"
            : projectionStatus === "synced"
              ? "顶部指标投影已同步"
              : projectionStatus === "empty"
                ? "BFF 投影为空"
                : "降级模式 · Mock fixture"}
        </strong>
        <span>
          {projectionStatus === "synced" && projectionReceipt
            ? `${projectionReceipt.route} · ${projectionReceipt.summary}${projectionReceipt.trace_id ? ` · ${projectionReceipt.trace_id}` : ""} · ${projectionListHydrated ? "指标与主列表来源：BFF。" : "顶部指标来源：BFF；交互明细为 Mock fixture，不计入同步结果。"}`
            : projectionStatus === "empty" && projectionReceipt
              ? `${projectionReceipt.route} · ${projectionReceipt.summary}${projectionReceipt.trace_id ? ` · ${projectionReceipt.trace_id}` : ""} · 不回落本地 fixture。`
              : projectionStatus === "degraded"
                ? `${projectionError || "后端不可用"} · 指标与列表来自本地 Mock fixture，不代表同步成功。`
                : `${config.title} 正在读取当前租户、项目与业务日期的指标投影。`}
        </span>
        {projectionStatus === "degraded" && (
          <button type="button" onClick={retryProjection}>
            重试
          </button>
        )}
      </div>

      <section
        className={`scene-runtime-context is-${workspaceSceneState}`}
        data-testid="scene-runtime-context"
        data-state={workspaceSceneState}
        aria-label="当前场景运行上下文"
      >
        <div>
          <ShieldCheck size={14} />
          <span>SceneProfile</span>
          <strong>{activeSceneManifest?.display_name ?? (workspaceSceneState === "pending" ? "读取中" : "未绑定生产场景")}</strong>
        </div>
        {workspaceSceneBinding ? (
          <>
            <code>{workspaceSceneBinding.version.version}</code>
            <code title={workspaceSceneBinding.manifest_sha256}>{workspaceSceneBinding.manifest_sha256.slice(0, 12)}</code>
            <em>{activeSceneManifest?.scene_key} · 所有运行和写入必须锁定此快照</em>
          </>
        ) : (
          <em>{workspaceSceneState === "error" ? "场景绑定读取失败；不会使用汽车演示默认值。" : "生产写入不会回落到任何行业默认配置。"}</em>
        )}
      </section>

      {projectionStatus === "pending" && (
        <section className="module-panel wide" data-testid="module-projection-loading" role="status">
          正在读取 BFF 投影数据...
        </section>
      )}
      {projectionStatus === "empty" && moduleKey !== "insights" && (
        <section className="module-panel wide tenant-empty-state" data-testid="module-projection-empty" role="status">
          <Database size={22} />
          <strong>当前范围暂无 BFF 投影数据</strong>
          <span>后端已成功返回空集合；页面不会用本地 fixture 填充指标或列表。</span>
        </section>
      )}
      {projectionStatus === "empty" && moduleKey === "insights" && activeTab !== "quality" && (
        <InsightsModuleOutlet mode="empty" activeTab={activeTab} />
      )}
      {(projectionStatus === "synced" || projectionStatus === "degraded" || (projectionStatus === "empty" && moduleKey === "insights" && activeTab === "quality")) && (
        <>
          {moduleKey === "home" && <HomeModuleOutlet setActiveModule={setActiveModule} navigateToTarget={navigateToTarget} activeTab={activeTab} />}
          {moduleKey === "tenants" && <TenantsModuleOutlet activeTab={activeTab} setActiveModule={setActiveModule} navigateToTarget={navigateToTarget} currentUser={currentUser} projectionItems={projectionStatus === "synced" ? projectionItems : undefined} projectionSource={projectionStatus === "synced" ? "bff" : "mock"} />}
          {moduleKey === "projects" && (
            <ProjectsModuleOutlet
              activeTab={activeTab}
              setActiveModule={setActiveModule}
              currentUser={currentUser}
              onProjectActivated={onProjectActivated}
              apiContext={projectionContext}
              projectionItems={projectionStatus === "synced" ? projectionItems : undefined}
              projectionSource={projectionStatus === "synced" ? "bff" : "mock"}
            />
          )}
          {moduleKey === "canvas" && (
            <CanvasModuleOutlet
              activeTab={activeTab}
              setActiveTab={setActiveTab}
              focus={deepLink}
              currentUser={currentUser}
              sceneBinding={workspaceSceneBinding}
              apiContext={projectionContext}
              demoMode={LABEL_DEMO_MODE}
            />
          )}
          {moduleKey === "data" && (
            <DataModuleOutlet
              activeTab={activeTab}
              setActiveModule={setActiveModule}
              selectedAssetId={selectedDataAssetId}
              setSelectedAssetId={setSelectedDataAssetId}
              openListeningFromDataAsset={openListeningFromDataAsset}
              openAssetsFromDataAsset={openAssetsFromDataAsset}
            />
          )}
          {moduleKey === "knowledge" && (
            <KnowledgeModuleLoadBoundary>
              <Suspense
                fallback={(
                  <section className="module-panel wide" data-testid="knowledge-module-loading" role="status">
                    正在加载知识库...
                  </section>
                )}
              >
                <KnowledgeModule
                  activeTab={activeTab}
                  setActiveModule={setActiveModule}
                  navigateToTarget={navigateToTarget}
                  focus={deepLink}
                  projectionItems={projectionStatus === "synced" ? projectionItems : undefined}
                  projectionSource={projectionStatus === "synced" ? "bff" : "mock"}
                  sceneBinding={workspaceSceneBinding}
                  apiContext={projectionContext}
                  demoMode={LABEL_DEMO_MODE}
                />
              </Suspense>
            </KnowledgeModuleLoadBoundary>
          )}
          {moduleKey === "labels" && <LabelsModuleOutlet activeTab={activeTab} setActiveModule={setActiveModule} navigateToTarget={navigateToTarget} focus={deepLink} />}
          {moduleKey === "insights" && <InsightsModuleOutlet mode="full" activeTab={activeTab} setActiveModule={setActiveModule} navigateToTarget={navigateToTarget} topbarContext={topbarContext} metricProjectionItems={projectionStatus === "synced" ? projectionItems : undefined} />}
          {moduleKey === "evaluation" && <EvaluationModuleOutlet activeTab={activeTab} setActiveTab={setActiveTab} setActiveModule={setActiveModule} navigateToTarget={navigateToTarget} focus={deepLink} currentUser={currentUser} />}
          {moduleKey === "assets" && <AssetsModuleOutlet activeTab={activeTab} setActiveTab={setActiveTab} selectedAssetKey={selectedAssetKey} setSelectedAssetKey={setSelectedAssetKey} navigateToTarget={navigateToTarget} />}
          {moduleKey === "settings" && <SettingsModuleOutlet activeTab={activeTab} setActiveTab={setActiveTab} currentUser={currentUser} />}
        </>
      )}
    </div>
  );
}
