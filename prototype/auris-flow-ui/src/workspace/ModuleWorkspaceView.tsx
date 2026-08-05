import { ChevronDown, Database, Download, ListFilter, Plus, RefreshCw, Search, ShieldCheck, Sparkles } from "lucide-react";
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
import { resolveModuleDetailVisibility } from "./moduleContentSource";
import { ProjectsModuleOutlet } from "./ProjectsModuleOutlet";
import { SettingsModuleOutlet } from "./SettingsModuleOutlet";
import { TenantsModuleOutlet } from "./TenantsModuleOutlet";
import type { ModuleCommands } from "./useModuleCommands";
import type { ModuleProjectionController } from "./useModuleProjection";
import type { ModuleWorkspaceNavigation } from "./useModuleWorkspaceNavigation";
import type { ModuleWorkspaceProps } from "./moduleWorkspaceContracts";
import { getModuleTitle, moduleWriteArchitectures } from "./moduleWorkspaceCatalog";

const PROJECT_SCENE_BLOCKED = "项目级写入与导出保持禁用。";

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
    exportBlockedByScene,
    exportBlockedReason,
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
  const sceneBindingRequired = !["tenants", "projects", "settings"].includes(moduleKey);
  const sceneExportReasonId = `${moduleKey}-scene-profile-export-reason`;
  const {
    renderDetails: renderModuleDetails,
    detailsUnavailable: moduleDetailsUnavailable
  } = resolveModuleDetailVisibility({
    moduleKey,
    projectionStatus,
    contentSource: projectionContentSource,
    demoMode: LABEL_DEMO_MODE
  });

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
          <button
            className={commandMode === "export" ? "active" : ""}
            aria-expanded={commandMode === "export"}
            aria-describedby={exportBlockedByScene ? sceneExportReasonId : undefined}
            disabled={exportBlockedByScene}
            title={exportBlockedByScene ? exportBlockedReason : undefined}
            onClick={() => toggleCommandMode("export")}
          >
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
          sceneState={workspaceSceneState}
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

      {moduleKey !== "home" && <MetricCards metrics={activeMetrics} source={projectionMetricSource} />}

      <div
        className={`operation-toast module-projection-toast is-${projectionStatus === "degraded" ? "error" : projectionStatus === "pending" ? "pending" : "success"}`}
        role="status"
        aria-live="polite"
        data-testid="module-projection-state"
        data-state={projectionStatus}
        data-source={projectionStatus === "synced" || projectionStatus === "empty" ? "bff" : projectionStatus === "degraded" && LABEL_DEMO_MODE ? "mock" : "none"}
        data-content-source={projectionContentSource}
      >
        <strong>
          {projectionStatus === "pending"
            ? "正在读取顶部指标"
            : projectionStatus === "synced"
              ? "顶部指标投影已同步"
              : projectionStatus === "empty"
                ? "BFF 投影为空"
                : LABEL_DEMO_MODE ? "降级模式 · Mock fixture" : "BFF 投影不可用"}
        </strong>
        <span>
          {projectionStatus === "synced" && projectionReceipt
            ? `${projectionReceipt.route} · ${projectionReceipt.summary}${projectionReceipt.trace_id ? ` · ${projectionReceipt.trace_id}` : ""} · ${projectionListHydrated
                ? "指标与主列表来源：BFF。"
                : projectionContentSource === "bff"
                  ? "模块内部 BFF controller 与真实回执驱动。"
                  : projectionContentSource === "mock"
                    ? "BFF 指标；fixture 仅演示。"
                    : "BFF 明细尚未接入；未用本地 fixture。"}`
            : projectionStatus === "empty" && projectionReceipt
              ? `${projectionReceipt.route} · ${projectionReceipt.summary}${projectionReceipt.trace_id ? ` · ${projectionReceipt.trace_id}` : ""} · 不回落本地 fixture。`
              : projectionStatus === "degraded"
                ? LABEL_DEMO_MODE
                  ? `${projectionError || "后端不可用"} · 本地 Mock fixture，仅用于演示。`
                  : `${projectionError || "后端不可用"} · 生产 truth 模式不会回落本地 fixture。`
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
            <em>{activeSceneManifest?.scene_key} · 项目级运行/写入/导出锁定此快照</em>
          </>
        ) : (
          <>
            <em id={sceneExportReasonId}>
              {sceneBindingRequired
                ? workspaceSceneState === "pending"
                  ? `正在读取场景绑定；${PROJECT_SCENE_BLOCKED}`
                  : workspaceSceneState === "error"
                    ? `场景绑定读取失败；${PROJECT_SCENE_BLOCKED}`
                    : `当前项目未绑定生产场景；${PROJECT_SCENE_BLOCKED}`
                : "非项目级操作仍受权限、审计与幂等约束。"}
            </em>
            {workspaceSceneState === "error" && (
              <button type="button" onClick={retryProjection}>
                <RefreshCw size={12} />
                重试场景绑定
              </button>
            )}
          </>
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
      {LABEL_DEMO_MODE && projectionStatus === "empty" && moduleKey === "insights" && activeTab !== "quality" && (
        <InsightsModuleOutlet mode="empty" activeTab={activeTab} />
      )}
      {moduleDetailsUnavailable && (
        <section className="module-panel wide tenant-empty-state" data-testid="module-detail-unavailable" role="status">
          <Database size={22} />
          <strong>{projectionStatus === "degraded" ? "BFF 投影不可用" : "BFF 明细尚未接入"}</strong>
          <span>
            {projectionStatus === "degraded"
              ? `${projectionError || "后端不可用"}；未展示本地 fixture。`
              : `${config.title}：BFF 摘要已同步，但权威明细尚未接入；生产 truth 模式不会挂载本地 fixture 或可操作控件。`}
          </span>
        </section>
      )}
      {renderModuleDetails && (
        <>
          {moduleKey === "home" && <HomeModuleOutlet setActiveModule={setActiveModule} navigateToTarget={navigateToTarget} activeTab={activeTab} projectionData={projectionReceipt?.raw} projectionTraceId={projectionReceipt?.trace_id} />}
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
              navigateToTarget={navigateToTarget}
              selectedAssetId={selectedDataAssetId}
              setSelectedAssetId={setSelectedDataAssetId}
              openListeningFromDataAsset={openListeningFromDataAsset}
              openAssetsFromDataAsset={openAssetsFromDataAsset}
              projectionItems={projectionStatus === "synced" ? projectionItems : undefined}
              projectionSource={projectionStatus === "synced" ? "bff" : "mock"}
              workspaceSceneBinding={workspaceSceneBinding}
              workspaceSceneState={workspaceSceneState}
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
          {moduleKey === "assets" && (
            <AssetsModuleOutlet
              activeTab={activeTab}
              setActiveTab={setActiveTab}
              selectedAssetKey={selectedAssetKey}
              setSelectedAssetKey={setSelectedAssetKey}
              navigateToTarget={navigateToTarget}
              projectionItems={projectionItems}
              readScopeKey={JSON.stringify([String(projectionContext.tenantId ?? ""), String(projectionContext.projectId ?? "")])}
              workspaceSceneBinding={workspaceSceneBinding}
              workspaceSceneState={workspaceSceneState}
            />
          )}
          {moduleKey === "settings" && <SettingsModuleOutlet activeTab={activeTab} setActiveTab={setActiveTab} currentUser={currentUser} />}
        </>
      )}
    </div>
  );
}
