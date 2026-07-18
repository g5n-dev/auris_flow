import type { ApiRuntimeContext } from "../../api/client";
import type { AuthUser } from "../../shared/contracts/auth";
import type { ModuleKey } from "../../shared/contracts/navigation";

export type ProjectRow = {
  name: string;
  owner: string;
  status: string;
  added: string;
  pending: number;
  pass: string;
  asset: string;
  projectId?: string;
  traceId?: string;
  scene?: string;
  dataMode?: string;
  labelVersion?: string;
  qualityTarget?: string;
};

export type ProjectDraft = {
  name: string;
  owner: string;
  scene: string;
  sceneObjective: string;
  dataMode: string;
  labelVersion: string;
  qualityTarget: string;
};

export type SceneGenerationDraft = {
  sceneKey: string;
  name: string;
  description: string;
  version: string;
  objective: string;
  modelRef: string;
  inputRefs: string;
};

export type ProjectStatusFilter = "all" | "running" | "attention";
export type SceneAction = "idle" | "generating" | "validating" | "reviewing" | "publishing" | "binding";

export type AutomotiveDemoProfile = {
  scene: string;
  datasource: string;
  labelVersion: string;
  modelChain: string;
  quality: string;
  ownerTeam: string;
};

export type ProjectModuleProps = {
  activeTab: string;
  setActiveModule: (module: ModuleKey) => void;
  currentUser: AuthUser | null;
  onProjectActivated: (projectName: string, projectId: string) => void;
  apiContext: ApiRuntimeContext;
  projectionItems?: unknown[];
  projectionSource: "bff" | "mock";
};
