import type { BackendActionReceipt } from "../../api/client";
import type { ComponentType } from "react";
import type { DataAssetItem } from "../../shared/contracts/dataAssets";
import type { ModuleKey } from "../../shared/contracts/navigation";
import type { WorkspaceProjectSceneBinding } from "../../shared/contracts/moduleWorkspaceGateway";
import type { VoiceprintDataViewProps } from "../../shared/contracts/voiceprint";

export type DataAggregateKey = "space" | "time" | "event" | "person";

export type DataModuleProps = {
  activeTab: string;
  setActiveModule: (module: ModuleKey) => void;
  selectedAssetId: string;
  setSelectedAssetId: (id: string) => void;
  openListeningFromDataAsset: (asset: DataAssetItem) => void;
  openAssetsFromDataAsset: (asset: DataAssetItem) => void;
  VoiceprintDataView: ComponentType<VoiceprintDataViewProps>;
  projectionItems?: unknown[];
  projectionSource?: "bff" | "mock";
  workspaceSceneBinding: WorkspaceProjectSceneBinding | null;
  workspaceSceneState: "pending" | "bound" | "unbound" | "error";
};

export type DataExportRun = BackendActionReceipt | null;

export type DataSceneProfileLock = {
  scene_profile_id: string;
  scene_profile_version_id: string;
  scene_profile_snapshot_sha256: string;
};
