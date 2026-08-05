import type { Dispatch, SetStateAction } from "react";

import { runSettingsProviderTest } from "../../api/client";
import type { OperationNotice } from "../../shared/contracts/operations";
import {
  backendRunFailed,
  backendRunStatusLabel,
  backendRunSubmitted,
  backendRunSucceeded,
  operationStatusFromBackendRun
} from "../../shared/runtime/backendRunStatus";
import { refreshBackendRunReceipt } from "../../api/backendRuns";
import { LABEL_DEMO_MODE } from "../../shared/runtime/demoMode";
import { asrServiceProfile } from "./catalog";
import { settingsShortTrace } from "./settingsDraftActions";

type Setter<T> = Dispatch<SetStateAction<T>>;

type SettingsProviderActionsInput = {
  setSettingsAction: Setter<string | null>;
  setSettingsNotice: Setter<OperationNotice>;
};

export function createSettingsProviderActions(input: SettingsProviderActionsInput) {
  const testAsrServiceConnection = async () => {
    input.setSettingsAction("asr-test");
    input.setSettingsNotice({
      status: "pending",
      title: "正在测试音频智能服务",
      detail: `${asrServiceProfile.endpoint} 正在通过 BFF 校验认证、超时、重试和响应契约。`
    });
    try {
      const receipt = await runSettingsProviderTest({
        source: "settings_module",
        service_id: asrServiceProfile.serviceId,
        provider_ref: "auris-audio-stack",
        endpoint: asrServiceProfile.endpoint,
        test_scope: "vad_diar_asr_contract",
        io_manager: asrServiceProfile.ioManager,
        timeout: asrServiceProfile.timeout,
        retry: asrServiceProfile.retry
      });
      const runState = await refreshBackendRunReceipt(receipt.data);
      const title = backendRunSucceeded(runState.status)
        ? "音频服务连通性通过"
        : backendRunSubmitted(runState.status)
          ? "Provider 测试已提交，等待外部回执"
          : backendRunFailed(runState.status)
            ? "Provider 测试运行异常"
            : "Provider 测试运行已创建";
      input.setSettingsAction(null);
      input.setSettingsNotice({
        status: operationStatusFromBackendRun(runState.status),
        title,
        detail: `${runState.id} 当前${backendRunStatusLabel(runState.status)}，trace：${settingsShortTrace(runState.trace_id)}。${
          backendRunSucceeded(runState.status) ? "Provider 响应契约已通过。" : "等待 provider worker 回写认证、超时和响应契约结果。"
        }`
      });
    } catch (error) {
      input.setSettingsAction(null);
      input.setSettingsNotice({
        status: "error",
        title: "音频服务连通性失败",
        detail: error instanceof Error ? error.message : "BFF 未返回可用错误信息。"
      });
    }
  };

  const saveAsrServiceDraft = () => {
    if (!LABEL_DEMO_MODE) {
      input.setSettingsNotice({
        status: "error",
        title: "服务配置写入尚未接入",
        detail: "当前生产 BFF 未提供服务配置草稿强资源；界面保留用于 DEMO，生产不会生成本地成功状态。"
      });
      return;
    }
    input.setSettingsNotice({
      status: "success",
      title: "DEMO：服务配置草稿已保存",
      detail: `${asrServiceProfile.name} 的 provider、IO Manager 和响应契约仅保存在演示状态。`
    });
  };

  return {
    saveAsrServiceDraft,
    testAsrServiceConnection
  };
}
