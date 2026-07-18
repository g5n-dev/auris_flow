import { AssetsTabContent } from "./components/AssetsTabContent";
import type { AssetsModuleProps } from "./types";
import { useAssetsWorkspace } from "./useAssetsWorkspace";

export function AssetsModule(props: AssetsModuleProps) {
  const workspace = useAssetsWorkspace(props);
  return (
    <div className={`module-grid asset-grid asset-dagster-grid asset-view-${workspace.activeTab}`}>
      <div className={`operation-toast asset-operation-toast is-${workspace.assetNotice.status}`} role="status" aria-live="polite">
        <strong>{workspace.assetNotice.title}</strong>
        <span>{workspace.assetNotice.detail}</span>
      </div>
      <AssetsTabContent workspace={workspace} />
    </div>
  );
}
