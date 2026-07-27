import type { ModuleDeepLink, ModuleKey } from "./navigation";
import type { TopbarContextKey } from "./workspace";
import { backendId } from "../runtime/records";

export type Theme = "contrast" | "dark" | "light";

export type Lang = "zh" | "en";

export type ModuleCommandMode = "search" | "filter" | "write" | "export";

export type ModuleScopeShortcut = {
  label: string;
  tabId: string;
  tabLabel: string;
  detail: string;
};

export type TopbarPanelKey = TopbarContextKey | "notifications" | "account";

export type AccountSettingsTab = "profile" | "workspace" | "notifications" | "security";

export type AuthMode = "login" | "register";

export type AuthFormState = {
  name: string;
  email: string;
  password: string;
  tenant: string;
  inviteCode: string;
  remember: boolean;
};

export type TopbarContextOption = {
  id: string;
  value: string;
  meta: string;
};

export type TopbarCoreContextKey = TopbarContextKey;

export type TopbarVisibleContextKey = Exclude<TopbarContextKey, "tenant">;

export type MockMutationStatus = "草稿" | "校验中" | "待审批" | "已提交" | "失败";

export type MockMutationKind = "create" | "update" | "review" | "route";

export type MockMutationRecord = {
  id: string;
  scopeKey: string;
  idempotencyKey: string;
  moduleKey: Exclude<ModuleKey, "listening">;
  moduleTitle: string;
  action: string;
  target: string;
  route: ModuleKey;
  deepLink?: ModuleDeepLink;
  kind: MockMutationKind;
  status: MockMutationStatus;
  entityKey: string;
  api: string;
  payload: string;
  guardrail: string;
  downstream: string;
  createdAt: string;
  backendId?: string;
  backendStatus?: string;
  traceId?: string;
  unavailableReason?: string;
};

export type ModuleWriteArchitecture = {
  createObject: string;
  updateObject: string;
  api: string;
  payload: string;
  guardrail: string;
  downstream: string;
  missing: string[];
};
