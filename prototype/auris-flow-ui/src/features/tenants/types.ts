import type { AuthUser } from "../../shared/contracts/auth";
import type { ModuleDeepLink, ModuleKey } from "../../shared/contracts/navigation";

export type TenantRow = {
  name: string;
  status: string;
  projects: number;
  members: number;
  storage: string;
  risk: string;
  tenantId?: string;
  traceId?: string;
};

export type TenantProject = {
  name: string;
  status: string;
  dataScope: string;
  members: number;
  assets: string;
  risk: string;
};

export type TenantMember = {
  name: string;
  role: string;
  scope: string;
  status: string;
  lastSeen: string;
};

export type TenantAsrBinding = {
  provider: string;
  serviceId: string;
  status: string;
  endpoint: string;
  auth: string;
  pullMode: string;
  cursor: string;
  quota: string;
  retention: string;
  nextRun: string;
  quality: string;
  pullSources: Array<[string, string, string]>;
  outputAssets: Array<[string, string, string]>;
  runs: Array<[string, string, string]>;
  guardrails: Array<[string, string]>;
};

export type TenantDraft = {
  name: string;
  admin: string;
  scene: string;
  quotaTemplate: string;
};

export type TenantRiskFilter = "all" | "risk" | "active" | "trial" | "paused";

export type TenantModuleProps = {
  activeTab: string;
  setActiveModule: (module: ModuleKey) => void;
  navigateToTarget: (target: ModuleDeepLink) => void;
  currentUser: AuthUser;
  projectionItems?: unknown[];
  projectionSource: "bff" | "mock";
};
