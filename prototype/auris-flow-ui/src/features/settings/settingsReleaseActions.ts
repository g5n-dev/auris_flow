import type { Dispatch, SetStateAction } from "react";

import {
  createBackendAction,
  decideBackendRun,
  type BackendActionReceipt
} from "../../api/client";
import type { AuthUser } from "../../shared/contracts/auth";
import type { OperationNotice } from "../../shared/contracts/operations";
import {
  backendRunStatusLabel,
  backendRunSucceeded,
  normalizeBackendRunStatus,
  operationStatusFromBackendRun
} from "../../shared/runtime/backendRunStatus";
import {
  backendReleaseRequester,
  refreshBackendRunReceipt
} from "../../api/backendRuns";
import { settingsShortTrace } from "./settingsDraftActions";
import type {
  SettingConfigRow,
  SettingDraftRecord
} from "./types";

type Setter<T> = Dispatch<SetStateAction<T>>;

type SettingsReleaseActionsInput = {
  activeDraft: SettingDraftRecord | null;
  activeTab: string;
  backendSettingDraftId: string | null;
  currentUser: AuthUser;
  settingResourceId: string;
  settingsReleaseGate: BackendActionReceipt | null;
  setBackendSettingDraftId: Setter<string | null>;
  setSettingDraft: Setter<SettingDraftRecord | null>;
  setSettingOverrides: Setter<Record<string, Partial<SettingConfigRow>>>;
  setSettingsAction: Setter<string | null>;
  setSettingsNotice: Setter<OperationNotice>;
  setSettingsReleaseGate: Setter<BackendActionReceipt | null>;
};

export function createSettingsReleaseActions(input: SettingsReleaseActionsInput) {
  const markSettingPublished = (draft: SettingDraftRecord, runState: BackendActionReceipt) => {
    input.setSettingOverrides((current) => ({
      ...current,
      [`${input.activeTab}:${draft.name}`]: {
        value: draft.value,
        owner: draft.owner,
        policy: draft.policy
      }
    }));
    input.setSettingDraft({ ...draft, status: "已发布" });
    input.setSettingsNotice({
      status: "success",
      title: "配置已发布",
      detail: `${runState.id} 已物化到 ${input.settingResourceId}；trace：${settingsShortTrace(runState.trace_id)}。`
    });
  };

  const approveSettingDraft = async () => {
    if (!input.activeDraft) {
      input.setSettingsNotice({
        status: "error",
        title: "没有可发布草稿",
        detail: "先保存草稿，再创建发布门禁。"
      });
      return;
    }
    if (!input.backendSettingDraftId) {
      input.setSettingsNotice({
        status: "error",
        title: "配置草稿尚未落库",
        detail: "等待草稿写入 BFF 后再创建发布门禁。"
      });
      return;
    }
    input.setSettingsAction("approve");
    if (input.settingsReleaseGate && normalizeBackendRunStatus(input.settingsReleaseGate.status) === "blocked") {
      if (backendReleaseRequester(input.settingsReleaseGate) === input.currentUser.userId) {
        const runState = await refreshBackendRunReceipt(input.settingsReleaseGate);
        input.setSettingsReleaseGate(runState);
        input.setSettingsAction(null);
        if (backendRunSucceeded(runState.status)) {
          markSettingPublished(input.activeDraft, runState);
        } else {
          input.setSettingsNotice({
            status: operationStatusFromBackendRun(runState.status),
            title: "等待其他管理员审批",
            detail: `${runState.id} 由 ${input.currentUser.name} 发起，禁止自审；状态已刷新。`
          });
        }
        return;
      }
      input.setSettingsNotice({
        status: "pending",
        title: "正在审批配置发布",
        detail: `${input.settingsReleaseGate.id} 将记录审批决定并重新进入发布队列。`
      });
      try {
        const decision = await decideBackendRun(
          input.settingsReleaseGate.id,
          "approved",
          `${input.activeDraft.name} 的权限、影响范围和回滚点已确认`
        );
        input.setSettingsReleaseGate(decision.data);
        await new Promise<void>((resolve) => window.setTimeout(resolve, 520));
        const runState = await refreshBackendRunReceipt(decision.data);
        input.setSettingsReleaseGate(runState);
        if (backendRunSucceeded(runState.status)) {
          markSettingPublished(input.activeDraft, runState);
        } else {
          input.setSettingsNotice({
            status: operationStatusFromBackendRun(runState.status),
            title: "发布门禁已放行",
            detail: `${runState.id} 当前${backendRunStatusLabel(runState.status)}；trace：${settingsShortTrace(runState.trace_id)}。`
          });
        }
      } catch (error) {
        input.setSettingsNotice({
          status: "error",
          title: "配置审批失败",
          detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
        });
      } finally {
        input.setSettingsAction(null);
      }
      return;
    }
    if (input.settingsReleaseGate && ["pending", "running", "submitted", "dispatched"].includes(normalizeBackendRunStatus(input.settingsReleaseGate.status))) {
      const runState = await refreshBackendRunReceipt(input.settingsReleaseGate);
      input.setSettingsReleaseGate(runState);
      input.setSettingsAction(null);
      if (backendRunSucceeded(runState.status)) {
        markSettingPublished(input.activeDraft, runState);
      } else {
        input.setSettingsNotice({
          status: operationStatusFromBackendRun(runState.status),
          title: "发布状态已刷新",
          detail: `${runState.id} 当前${backendRunStatusLabel(runState.status)}；trace：${settingsShortTrace(runState.trace_id)}。`
        });
      }
      return;
    }
    input.setSettingsNotice({
      status: "pending",
      title: "正在创建发布门禁",
      detail: `${input.activeDraft.name} 正在绑定审批、回滚和 trace。`
    });
    try {
      const receipt = await createBackendAction("/v1/settings/publish-requests", "settings_publish", {
        draft_id: input.backendSettingDraftId
      });
      input.setSettingsReleaseGate(receipt.data);
      input.setSettingDraft({ ...input.activeDraft, status: "待发布" });
      input.setSettingsAction(null);
      input.setSettingsNotice({
        status: "pending",
        title: "发布门禁已创建",
        detail: `${receipt.data.id} · ${receipt.data.trace_id?.slice(0, 12)}`
      });
    } catch (error) {
      input.setSettingsAction(null);
      input.setSettingsNotice({
        status: "error",
        title: "发布门禁创建失败",
        detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
      });
    }
  };

  const rejectSettingDraft = async () => {
    if (!input.activeDraft) return;
    if (input.settingsReleaseGate && normalizeBackendRunStatus(input.settingsReleaseGate.status) === "blocked") {
      input.setSettingsAction("approve");
      try {
        const receipt = await decideBackendRun(
          input.settingsReleaseGate.id,
          "rejected",
          `${input.activeDraft.name} 发布门禁人工退回，保留草稿继续修改`
        );
        input.setSettingsReleaseGate(receipt.data);
        input.setSettingDraft({ ...input.activeDraft, status: "已拒绝" });
        input.setSettingsNotice({
          status: "error",
          title: "配置草稿已退回",
          detail: `${receipt.data.id} 已取消，线上配置未变化；trace：${settingsShortTrace(receipt.data.trace_id)}。`
        });
      } catch (error) {
        input.setSettingsNotice({
          status: "error",
          title: "配置退回失败",
          detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
        });
      } finally {
        input.setSettingsAction(null);
      }
      return;
    }
    input.setSettingDraft({ ...input.activeDraft, status: "已拒绝" });
    input.setSettingsNotice({
      status: "error",
      title: "配置草稿已退回",
      detail: `${input.activeDraft.name} 未写入线上配置，可修改后重新提交。`
    });
  };

  const discardSettingDraft = () => {
    if (!input.activeDraft) return;
    input.setSettingDraft(null);
    input.setBackendSettingDraftId(null);
    input.setSettingsReleaseGate(null);
    input.setSettingsNotice({
      status: "success",
      title: "配置草稿已放弃",
      detail: `${input.activeDraft.name} 的本地草稿已清除，线上配置未变。`
    });
  };

  return {
    approveSettingDraft,
    discardSettingDraft,
    rejectSettingDraft
  };
}
