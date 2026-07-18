import { useEffect, useState } from "react";

import type { BackendActionReceipt } from "../../api/client";
import type { OperationNotice } from "../../shared/contracts/operations";
import {
  settingPolicyBundles,
  settingsRows,
  settingsTabs
} from "./catalog";
import { createSettingsDraftActions } from "./settingsDraftActions";
import { createSettingsProviderActions } from "./settingsProviderActions";
import { createSettingsReleaseActions } from "./settingsReleaseActions";
import type {
  SettingBundleEntry,
  SettingConfigRow,
  SettingDraftRecord,
  SettingEditor,
  SettingPolicyBundle,
  SettingsModuleProps
} from "./types";

export function useSettingsWorkspace({
  activeTab,
  setActiveTab,
  currentUser
}: SettingsModuleProps) {
  const tabLabel = settingsTabs.find((tab) => tab.id === activeTab)?.label ?? "模型配置";
  const [settingOverrides, setSettingOverrides] = useState<Record<string, Partial<SettingConfigRow>>>({});
  const baseRows = settingsRows[activeTab] ?? settingsRows.model;
  const rows = baseRows.map((row) => ({ ...row, ...(settingOverrides[`${activeTab}:${row.name}`] ?? {}) }));
  const [selectedSettingName, setSelectedSettingName] = useState(rows[0]?.name ?? "");
  const [activeBundleId, setActiveBundleId] = useState(settingPolicyBundles[0].id);
  const [settingDraft, setSettingDraft] = useState<SettingDraftRecord | null>(null);
  const [backendSettingDraftId, setBackendSettingDraftId] = useState<string | null>(null);
  const [settingsReleaseGate, setSettingsReleaseGate] = useState<BackendActionReceipt | null>(null);
  const [settingsAction, setSettingsAction] = useState<string | null>(null);
  const [settingsNotice, setSettingsNotice] = useState<OperationNotice>({
    status: "idle",
    title: "等待配置动作",
    detail: "草稿、校验、发布、退回和音频服务连通性测试都会写入配置审计。"
  });
  const [settingEditor, setSettingEditor] = useState<SettingEditor>({
    value: rows[0]?.value ?? "",
    owner: rows[0]?.owner ?? "",
    policy: rows[0]?.policy ?? "",
    reason: "按当前策略包创建配置草稿，先走 Policy Guard，再进入 Human Loop。",
    approver: rows[0]?.owner ?? "项目管理员",
    rollback: "v3.2.0"
  });

  const tabLabelById = new Map(settingsTabs.map((tab) => [tab.id, tab.label]));
  const allSettingEntries: SettingBundleEntry[] = Object.entries(settingsRows).flatMap(([tabId, tabRows]) =>
    tabRows.map((row) => ({
      tabId,
      tabLabel: tabLabelById.get(tabId) ?? tabId,
      row
    }))
  );
  const findBundleEntry = (name: string) => allSettingEntries.find((entry) => entry.row.name === name);
  const isBundleEntry = (entry: SettingBundleEntry | undefined): entry is SettingBundleEntry => Boolean(entry);
  const activeBundle = settingPolicyBundles.find((bundle) => bundle.id === activeBundleId) ?? settingPolicyBundles[0];
  const activeBundleEntries = activeBundle.targets.map(findBundleEntry).filter(isBundleEntry);
  const currentBundleEntries = activeBundleEntries.filter((entry) => entry.tabId === activeTab);
  const selectBundle = (bundle: SettingPolicyBundle) => {
    const firstTarget = bundle.targets.map(findBundleEntry).find(isBundleEntry);
    setActiveBundleId(bundle.id);
    if (firstTarget) {
      setActiveTab(firstTarget.tabId);
      setSelectedSettingName(firstTarget.row.name);
    }
  };
  const selectBundleEntry = (entry: SettingBundleEntry) => {
    setActiveTab(entry.tabId);
    setSelectedSettingName(entry.row.name);
  };
  useEffect(() => {
    const nextRows = settingsRows[activeTab] ?? settingsRows.model;
    if (!nextRows.some((row) => row.name === selectedSettingName)) {
      setSelectedSettingName(nextRows[0]?.name ?? "");
    }
  }, [activeTab, selectedSettingName]);

  const selectedSetting = rows.find((row) => row.name === selectedSettingName) ?? rows[0];
  const activeDraft = settingDraft?.tab === activeTab && settingDraft.name === selectedSetting.name ? settingDraft : null;
  const guardLevel = selectedSetting.risk.includes("高") ? "必须审批" : selectedSetting.risk.includes("中") ? "保存草稿 + 抽样校验" : "可直接保存";
  const draftId = `CFG-${activeTab}-${selectedSetting.name.replace(/[ /]/g, "").slice(0, 8)}`;
  const settingApiSegments = selectedSetting.api.split("/").filter(Boolean);
  const settingResourceId = settingApiSegments[settingApiSegments.length - 1] ?? `${activeTab}-${selectedSetting.name}`;
  const updateSettingEditor = (key: keyof SettingEditor, value: string) => {
    setSettingEditor((current) => ({ ...current, [key]: value }));
    if (settingsReleaseGate) {
      setSettingsReleaseGate(null);
      setBackendSettingDraftId(null);
    }
  };
  const settingEditorInvalidReason = !settingEditor.value.trim()
    ? "先填写修改后的值。"
    : !settingEditor.owner.trim()
      ? "先填写负责人。"
      : "";
  const validateDisabledReason =
    settingsAction === "validate" ? "配置校验中，暂勿重复发起。" : settingEditorInvalidReason;
  const submitPublishDisabledReason =
    settingsAction === "validate" ? "配置校验中，暂不提交发布。" : settingEditorInvalidReason;
  const providerTestDisabledReason =
    settingsAction === "asr-test" ? "连通性测试中，暂勿重复发起。" : "";
  const approveDisabledReason =
    settingsAction === "approve"
      ? "发布门禁处理中。"
      : activeDraft?.status === "已拒绝"
        ? "草稿已退回，先重新提交。"
        : activeDraft && !backendSettingDraftId
          ? "配置草稿正在写入 BFF。"
        : "";
  const rejectDisabledReason = settingsAction === "approve" ? "发布门禁创建中，暂不能退回。" : "";
  const discardDisabledReason = settingsAction === "approve" ? "发布门禁创建中，暂不能放弃。" : "";
  const smartActionDisabledReason = validateDisabledReason || submitPublishDisabledReason;
  const humanActionDisabledReason = approveDisabledReason || rejectDisabledReason || discardDisabledReason;

  useEffect(() => {
    setSettingEditor({
      value: selectedSetting.value,
      owner: selectedSetting.owner,
      policy: selectedSetting.policy,
      reason: `${selectedSetting.name} 变更影响 ${selectedSetting.scope}，需保留审计、回滚和人审记录。`,
      approver: selectedSetting.owner,
      rollback: "v3.2.0"
    });
  }, [activeTab, selectedSetting.name]);

  const draftActions = createSettingsDraftActions({
    activeBundle,
    activeTab,
    draftId,
    selectedSetting,
    settingEditor,
    settingResourceId,
    setBackendSettingDraftId,
    setSettingDraft,
    setSettingsAction,
    setSettingsNotice,
    setSettingsReleaseGate
  });
  const releaseActions = createSettingsReleaseActions({
    activeDraft,
    activeTab,
    backendSettingDraftId,
    currentUser,
    settingResourceId,
    settingsReleaseGate,
    setBackendSettingDraftId,
    setSettingDraft,
    setSettingOverrides,
    setSettingsAction,
    setSettingsNotice,
    setSettingsReleaseGate
  });
  const providerActions = createSettingsProviderActions({
    setSettingsAction,
    setSettingsNotice
  });

  return {
    activeBundle,
    activeBundleEntries,
    activeDraft,
    activeTab,
    approveDisabledReason,
    backendSettingDraftId,
    currentBundleEntries,
    currentUser,
    discardDisabledReason,
    draftId,
    guardLevel,
    humanActionDisabledReason,
    providerTestDisabledReason,
    rejectDisabledReason,
    rows,
    selectBundle,
    selectBundleEntry,
    selectedSetting,
    selectedSettingName,
    setActiveTab,
    setSelectedSettingName,
    settingDraft,
    settingEditor,
    settingEditorInvalidReason,
    settingPolicyBundles,
    settingResourceId,
    settingsAction,
    settingsNotice,
    settingsReleaseGate,
    smartActionDisabledReason,
    submitPublishDisabledReason,
    tabLabel,
    updateSettingEditor,
    validateDisabledReason,
    ...draftActions,
    ...releaseActions,
    ...providerActions
  };
}

export type SettingsWorkspace = ReturnType<typeof useSettingsWorkspace>;
