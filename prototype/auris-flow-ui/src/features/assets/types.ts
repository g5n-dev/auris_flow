import type { Dispatch, SetStateAction } from "react";

import type { ModuleDeepLink } from "../../shared/contracts/navigation";
import type { OperationNotice } from "../../shared/contracts/operations";
import type { WorkspaceProjectSceneBinding } from "../../shared/contracts/moduleWorkspaceGateway";

export type AssetCatalogRow = {
  name: string;
  domain: string;
  status: string;
  version: string;
  quality: number | null;
  assetKey: string;
  definition: string;
  partition: string;
  materialization: string;
  owner: string;
  freshness: string;
  upstream: string;
  downstream: string[];
  backfill: string;
  checks: string[];
};

export type AssetCompatibilityStatus = "兼容" | "需人工" | "需确认";
export type AssetCompatibilityCheck = readonly [string, AssetCompatibilityStatus, string];

export type AssetApiContract = {
  method: string;
  endpoint: string;
  purpose: string;
  dagster: string;
  response: string;
  tone: string;
};

export type AssetRunTimelineRow = readonly [string, string, string, string];

export type HotwordBackfillBinding = {
  hotwordPackVersionId: string;
  evalRunId: string;
  taskVersionId: string;
  rootTraceId: string;
  sourceAsset: string;
  sourceMaterializationId: string;
  sourceBadcaseIds: string[];
};

export type HotwordBackfillRecovery = {
  scopeKey: string;
  status: "loading" | "ready" | "blocked";
  binding?: HotwordBackfillBinding;
  reason: string;
};

export type AssetBackfillDraft = {
  scopeKey: string;
  assetKey: string;
  assetName: string;
  draftId: string;
  reason: string;
  status: "草稿" | "待审批";
  rootTraceId?: string;
  hotwordBinding?: HotwordBackfillBinding;
};

export type AssetsModuleProps = {
  activeTab: string;
  setActiveTab: (tab: string) => void;
  selectedAssetKey: string;
  setSelectedAssetKey: (assetKey: string) => void;
  navigateToTarget: (target: ModuleDeepLink) => void;
  projectionItems?: unknown[];
  readScopeKey: string;
  workspaceSceneBinding: WorkspaceProjectSceneBinding | null;
  workspaceSceneState: "pending" | "bound" | "unbound" | "error";
};

export type AssetActionState = {
  assetAction: string | null;
  assetNotice: OperationNotice;
  backfillDraft: AssetBackfillDraft | null;
  setAssetAction: Dispatch<SetStateAction<string | null>>;
  setAssetNotice: Dispatch<SetStateAction<OperationNotice>>;
  setBackfillDraft: Dispatch<SetStateAction<AssetBackfillDraft | null>>;
};

export type AssetMaterializationProjection = {
  materialization_id?: string;
  status?: string;
  source_materialization_id?: string;
  hotword_pack_version_id?: string;
};
