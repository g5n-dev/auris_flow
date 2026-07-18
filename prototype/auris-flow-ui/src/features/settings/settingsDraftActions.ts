import type { Dispatch, SetStateAction } from "react";

import { createBackendAction } from "../../api/client";
import type { OperationNotice } from "../../shared/contracts/operations";
import type {
  SettingConfigRow,
  SettingDraftRecord,
  SettingEditor,
  SettingPolicyBundle
} from "./types";

type Setter<T> = Dispatch<SetStateAction<T>>;

export const settingsShortTrace = (trace?: string) => trace ? trace.slice(0, 12) : "no-trace";

type SettingsDraftActionsInput = {
  activeBundle: SettingPolicyBundle;
  activeTab: string;
  draftId: string;
  selectedSetting: SettingConfigRow;
  settingEditor: SettingEditor;
  settingResourceId: string;
  setBackendSettingDraftId: Setter<string | null>;
  setSettingDraft: Setter<SettingDraftRecord | null>;
  setSettingsAction: Setter<string | null>;
  setSettingsNotice: Setter<OperationNotice>;
  setSettingsReleaseGate: Setter<import("../../api/client").BackendActionReceipt | null>;
};

export function createSettingsDraftActions(input: SettingsDraftActionsInput) {
  const updateDraft = async (status: "草稿" | "校验中" | "待发布") => {
    if (!input.settingEditor.value.trim() || !input.settingEditor.owner.trim()) {
      input.setSettingsNotice({
        status: "error",
        title: "配置草稿无效",
        detail: "修改后的值和负责人不能为空。"
      });
      return;
    }
    const nextDraft: SettingDraftRecord = {
      tab: input.activeTab,
      bundle: input.activeBundle.title,
      name: input.selectedSetting.name,
      value: input.settingEditor.value,
      owner: input.settingEditor.owner,
      policy: input.settingEditor.policy,
      reason: input.settingEditor.reason,
      approver: input.settingEditor.approver,
      rollback: input.settingEditor.rollback,
      status
    };
    input.setSettingsAction(status === "校验中" ? "validate" : "draft");
    input.setSettingsNotice({
      status: "pending",
      title: status === "校验中" ? "正在保存并校验配置" : "正在保存配置草稿",
      detail: `${input.selectedSetting.name} 将写入当前租户/项目的配置草稿和审计链路。`
    });
    try {
      const receipt = await createBackendAction(
        "/v1/settings/drafts",
        `settings_draft_${input.settingResourceId}`,
        {
          settings_draft_id: input.draftId,
          setting_id: input.settingResourceId,
          name: input.selectedSetting.name,
          source: "settings_module",
          bundle: input.activeBundle.title,
          changes: {
            value: input.settingEditor.value,
            owner: input.settingEditor.owner,
            policy: input.settingEditor.policy
          },
          reason: input.settingEditor.reason,
          approver: input.settingEditor.approver,
          rollback: input.settingEditor.rollback,
          scope: input.selectedSetting.scope,
          status: "draft"
        }
      );
      input.setBackendSettingDraftId(receipt.data.id);
      input.setSettingsReleaseGate(null);
      input.setSettingDraft(nextDraft);
      input.setSettingsNotice({
        status: status === "校验中" ? "pending" : "success",
        title: status === "校验中" ? "配置校验中" : status === "待发布" ? "配置已提交发布" : "配置草稿已保存",
        detail: `${receipt.data.id} 已写入 BFF；trace：${settingsShortTrace(receipt.data.trace_id)}。`
      });
    } catch (error) {
      input.setSettingsAction(null);
      input.setSettingsNotice({
        status: "error",
        title: "配置草稿保存失败",
        detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
      });
      return;
    }
    if (status === "校验中") {
      window.setTimeout(() => {
        input.setSettingsAction(null);
        input.setSettingDraft((current) => current && current.name === input.selectedSetting.name ? { ...current, status: "待发布" } : current);
        input.setSettingsNotice({
          status: "success",
          title: "配置校验通过",
          detail: `${input.selectedSetting.name} 的权限、资产影响和回滚点已校验，等待提交发布。`
        });
      }, 760);
    } else {
      input.setSettingsAction(null);
    }
  };

  return { updateDraft };
}
