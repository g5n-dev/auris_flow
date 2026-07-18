import { moduleWorkspaceGateway } from "./app/moduleWorkspaceGateway";
import { useAuthSession } from "./app/useAuthSession";
import { useBackendHealth } from "./app/useBackendHealth";
import { useWorkspaceContext } from "./app/useWorkspaceContext";
import { AccountSettingsModal } from "./shell/AccountSettingsModal";
import { AuthPage } from "./shell/AuthPage";
import { Sidebar } from "./shell/Sidebar";
import { TopBar } from "./shell/TopBar";
import { useShellNavigation } from "./shell/useShellNavigation";
import { ListeningModuleOutlet } from "./workspace/ListeningModuleOutlet";
import { ModuleWorkspace } from "./workspace/ModuleWorkspace";
import { getModuleTitle } from "./workspace/moduleWorkspaceCatalog";

export default function App() {
  const auth = useAuthSession();
  const context = useWorkspaceContext(auth.currentUser);
  const navigation = useShellNavigation();
  const backendStatus = useBackendHealth();

  const handleLogout = async () => {
    navigation.setAccountSettingsOpen(false);
    await auth.logout();
  };

  if (auth.authRestoring) {
    return (
      <main className="auth-shell">
        <section className="auth-panel auth-restoring" aria-live="polite">
          <div className="auth-brand">
            <div className="brand-mark">A</div>
            <div>
              <span>Auris Flow</span>
              <strong>正在恢复安全会话</strong>
            </div>
          </div>
          <p>校验租户、项目和角色范围后进入工作台。</p>
        </section>
      </main>
    );
  }

  if (!auth.currentUser) {
    return (
      <AuthPage
        onLogin={auth.authenticate}
        onAuth={auth.acceptSession}
        restoreError={auth.authRestoreError}
        onRetryRestore={auth.retryRestore}
      />
    );
  }

  return (
    <div className="app-shell" data-theme={navigation.theme}>
      <Sidebar
        activeModule={navigation.activeModule}
        setActiveModule={navigation.navigateModuleRoot}
        currentUser={auth.currentUser}
        onOpenAccountSettings={() => navigation.setAccountSettingsOpen(true)}
        onLogout={handleLogout}
        logoutPending={auth.logoutPending}
      />
      <main className="workbench">
        <TopBar
          theme={navigation.theme}
          setTheme={navigation.setTheme}
          lang={navigation.lang}
          setLang={navigation.setLang}
          activeModule={navigation.activeModule}
          setActiveModule={navigation.navigateModuleRoot}
          currentUser={auth.currentUser}
          context={context.topbarContext}
          setContext={context.setTopbarContext}
          backendStatus={backendStatus}
          onOpenAccountSettings={() => navigation.setAccountSettingsOpen(true)}
          onLogout={handleLogout}
          logoutPending={auth.logoutPending}
        />
        <section className="workspace">
          <>
            <ListeningModuleOutlet
              active={navigation.activeModule === "listening"}
              activeModule={navigation.activeModule}
              currentUser={auth.currentUser}
              focus={navigation.deepLinkTarget?.module === "listening" ? navigation.deepLinkTarget : null}
              getModuleTitle={getModuleTitle}
              navigateModuleRoot={navigation.navigateModuleRoot}
              navigateToTarget={navigation.navigateToTarget}
              registerListeningNavigationResolver={navigation.registerListeningNavigationResolver}
              setSelectedDataAssetId={navigation.setSelectedDataAssetId}
              setSelectedAssetKey={navigation.setSelectedAssetKey}
              topbarContext={context.topbarContext}
            />
            {navigation.activeModule !== "listening" && (
              <ModuleWorkspace
                key={navigation.activeModule}
                gateway={moduleWorkspaceGateway}
                moduleKey={navigation.activeModule}
                currentUser={auth.currentUser}
                setActiveModule={navigation.navigateModuleRoot}
                deepLink={navigation.deepLinkTarget?.module === navigation.activeModule ? navigation.deepLinkTarget : null}
                navigateToTarget={navigation.navigateToTarget}
                selectedDataAssetId={navigation.selectedDataAssetId}
                setSelectedDataAssetId={navigation.setSelectedDataAssetId}
                selectedAssetKey={navigation.selectedAssetKey}
                setSelectedAssetKey={navigation.setSelectedAssetKey}
                openListeningFromDataAsset={navigation.openListeningFromDataAsset}
                openAssetsFromDataAsset={navigation.openAssetsFromDataAsset}
                topbarContext={context.topbarContext}
                projectIdByName={context.projectIdByName}
                onProjectActivated={context.activateProjectContext}
              />
            )}
          </>
        </section>
      </main>
      {navigation.accountSettingsOpen && (
        <AccountSettingsModal
          user={auth.currentUser}
          onSave={auth.setCurrentUser}
          onClose={() => navigation.setAccountSettingsOpen(false)}
          onLogout={handleLogout}
          logoutPending={auth.logoutPending}
        />
      )}
    </div>
  );
}
