import type { BackendActionReceipt } from "../../api/client";
import type { AuthUser } from "../../shared/contracts/auth";

export type SettingConfigRow = {
  name: string;
  value: string;
  desc: string;
  risk: string;
  scope: string;
  owner: string;
  api: string;
  asset: string;
  policy: string;
};

export type SettingPolicyTone = "blue" | "green" | "amber" | "violet";

export type SettingPolicyBundle = {
  id: string;
  title: string;
  intent: string;
  objective: string;
  owner: string;
  risk: string;
  tone: SettingPolicyTone;
  targets: string[];
  gates: string[];
  outcome: string;
};

export type SettingBundleEntry = {
  tabId: string;
  tabLabel: string;
  row: SettingConfigRow;
};

export type SettingDraftRecord = {
  tab: string;
  bundle: string;
  name: string;
  value: string;
  owner: string;
  policy: string;
  reason: string;
  approver: string;
  rollback: string;
  status: "草稿" | "校验中" | "待发布" | "已发布" | "已拒绝";
};

export type SettingEditor = {
  value: string;
  owner: string;
  policy: string;
  reason: string;
  approver: string;
  rollback: string;
};

export type AsrServiceProfile = {
  name: string;
  serviceId: string;
  endpoint: string;
  version: string;
  auth: string;
  timeout: string;
  retry: string;
  owner: string;
  ioManager: string;
  providers: string[][];
  request: string[][];
  response: string[][];
  assets: string[][];
  dagster: string[][];
};

export type AudioServiceParamGroup = {
  title: string;
  rows: string[][];
};

export type SettingsModuleProps = {
  activeTab: string;
  setActiveTab: (tab: string) => void;
  currentUser: AuthUser;
};

export type SettingsReleaseState = BackendActionReceipt | null;
