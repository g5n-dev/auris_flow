import type { AuthUser } from "../shared/contracts/auth";
import type { DataAssetItem } from "../shared/contracts/dataAssets";
import type { ModuleWorkspaceGateway } from "../shared/contracts/moduleWorkspaceGateway";
import type { ModuleDeepLink, ModuleKey } from "../shared/contracts/navigation";
import type { TopbarContextState } from "../shared/contracts/workspace";

export type ModuleWorkspaceProps = {
  gateway: ModuleWorkspaceGateway;
  moduleKey: Exclude<ModuleKey, "listening">;
  currentUser: AuthUser;
  setActiveModule: (module: ModuleKey) => void;
  deepLink: ModuleDeepLink | null;
  navigateToTarget: (target: ModuleDeepLink) => void;
  selectedDataAssetId: string;
  setSelectedDataAssetId: (id: string) => void;
  selectedAssetKey: string;
  setSelectedAssetKey: (assetKey: string) => void;
  openListeningFromDataAsset: (asset: DataAssetItem) => void;
  openAssetsFromDataAsset: (asset: DataAssetItem) => void;
  topbarContext: TopbarContextState;
  projectIdByName: Record<string, string>;
  onProjectActivated: (projectName: string, projectId: string) => void;
};
