import type { AuthUser } from "../../shared/contracts/auth";
import type { BackendAffectedObjectRef } from "../../api/client";
import type { ModuleDeepLink, ModuleKey } from "../../shared/contracts/navigation";
import type { TopbarContextState } from "../../shared/contracts/workspace";

export type ListeningFeatureProps = {
  active: boolean;
  activeModule: ModuleKey;
  currentUser: AuthUser;
  focus: ModuleDeepLink | null;
  getModuleTitle: (module: ModuleKey) => string;
  navigateModuleRoot: (module: ModuleKey) => void;
  navigateToTarget: (target: ModuleDeepLink) => void;
  registerListeningNavigationResolver: (resolver: ListeningNavigationResolver | null) => void;
  setSelectedDataAssetId: (id: string) => void;
  setSelectedAssetKey: (key: string) => void;
  topbarContext: TopbarContextState;
};

export type ListeningNavigationResolver = (target: ModuleDeepLink) => ModuleDeepLink | null;

export type Mode = "simple" | "evidence" | "matrix";

export type ListeningScope = "segment" | "conversation";

export type ListeningToolMode = "search" | "filter" | "reception" | "rerun";

export type PanelTab = "agent" | "docs" | "diff" | "crosstalk";

export type MarkState = "main" | "crosstalk" | "duplicate" | "none";

export type SimpleAiRecommendState = "idle" | "running" | "ready";

export type EvidenceModuleKey = "deviceBar" | "minimap" | "islands" | "waveform" | "tracks" | "transcript" | "spine";

export type ListeningDeviceKey = "A-1001" | "B-2001" | "Hall-Mic" | "Drive-01";

export type EvidencePageConfig = {
  density: "comfortable" | "compact";
  showQueue: boolean;
  showRightPanel: boolean;
  modules: Record<EvidenceModuleKey, boolean>;
};

export type AppealableReviewDecision = {
  decisionId: string;
  reviewTaskId: string;
  evidenceRefs: string[];
  sampleTitle: string;
  rootTraceId: string;
  affectedObjects: BackendAffectedObjectRef[];
  idempotencyKey: string;
};
