import type { Dispatch, SetStateAction } from "react";

import {
  createConnectorResource,
  createExportRun,
  getBackendRun,
  retryBackendRun,
  type BackendActionReceipt
} from "../../api/client";
import { refreshBackendRunReceipt } from "../../api/backendRuns";
import type { DataAssetItem } from "../../shared/contracts/dataAssets";
import type { OperationNotice } from "../../shared/contracts/operations";
import {
  backendRunFailed,
  backendRunStatusLabel,
  backendRunSucceeded,
  operationStatusFromBackendRun
} from "../../shared/runtime/backendRunStatus";
import { aggregateMeta } from "./fixtures";
import type { DataAggregateKey, DataExportRun, DataSceneProfileLock } from "./types";

type Setter<T> = Dispatch<SetStateAction<T>>;

type DataOperationsInput = {
  activeTab: string;
  aggregateFilters: Record<DataAggregateKey, string[]>;
  aggregationOrder: DataAggregateKey[];
  dataAction: string | null;
  dataExportRun: DataExportRun;
  isRelationView: boolean;
  canImportConnector: boolean;
  connectorImportBlockedReason: string;
  canExportData: boolean;
  dataExportBlockedReason: string;
  sceneProfileLock: DataSceneProfileLock | null;
  selectedAsset: DataAssetItem;
  setDataAction: Setter<string | null>;
  setDataExportRun: Setter<DataExportRun>;
  setDataNotice: Setter<OperationNotice>;
  visibleDataAssets: DataAssetItem[];
};

const dataShortTrace = (trace?: string) => trace ? trace.slice(0, 12) : "no-trace";

export function createDataOperations(input: DataOperationsInput) {
  const openConnectorImport = async () => {
    if (input.dataAction === "connector-import") return;
    if (!input.sceneProfileLock || !input.canImportConnector) {
      input.setDataNotice({
        status: "error",
        title: "连接器导入已禁用",
        detail: input.connectorImportBlockedReason || "当前数据对象没有经过服务端验证的目标资产。"
      });
      return;
    }
    const connectorId = `ui_connector_${Date.now().toString(36)}`;
    const connectorPayload = {
      connector_id: connectorId,
      name: "数据管理连接器导入",
      source_type: input.activeTab === "events" ? "authenticated_events_api" : input.isRelationView ? "relation_repair_api" : "audio_and_event_connector",
      status: "draft",
      sync_mode: "manual",
      target_asset_key: input.selectedAsset.assetKey,
      source: "data_module_connector_import",
      selected_asset_id: input.selectedAsset.id,
      ...input.sceneProfileLock
    };
    input.setDataAction("connector-import");
    input.setDataNotice({
      status: "pending",
      title: "正在创建连接器草稿",
      detail: `${input.selectedAsset.id} / ${input.selectedAsset.assetKey} 正在写入 BFF，成功后可继续配置认证、调度和输出映射。`
    });
    try {
      const receipt = await createConnectorResource(connectorPayload);
      input.setDataNotice({
        status: "success",
        title: "连接器资源已创建",
        detail: `${receipt.data.id} 已入台账，状态 ${receipt.data.status}；trace：${dataShortTrace(receipt.data.trace_id)}。`
      });
    } catch (error) {
      input.setDataNotice({
        status: "error",
        title: "连接器创建失败",
        detail: error instanceof Error ? `${error.message}。请检查当前租户/项目权限或稍后重试。` : "BFF 未返回可识别错误，请稍后重试。"
      });
    } finally {
      input.setDataAction(null);
    }
  };

  const exportDataAssets = async () => {
    if (input.dataAction === "export") return;
    if (!input.sceneProfileLock || !input.canExportData) {
      input.setDataNotice({
        status: "error",
        title: "数据导出已禁用",
        detail: input.dataExportBlockedReason || "当前项目缺少可验证的 SceneProfile 快照。"
      });
      return;
    }
    input.setDataAction("export");
    input.setDataNotice({
      status: "pending",
      title: input.dataExportRun && backendRunFailed(input.dataExportRun.status) ? "正在重试导出运行" : input.dataExportRun ? "正在刷新导出运行" : "正在创建导出运行",
      detail: `${input.visibleDataAssets.length} 个叶子对象将按 ${input.aggregationOrder.map((key) => aggregateMeta[key].label).join(" → ")} 导出；完成状态以 BFF 运行记录为准。`
    });
    try {
      let receipt: BackendActionReceipt;
      if (input.dataExportRun && backendRunFailed(input.dataExportRun.status)) {
        const retried = await retryBackendRun(input.dataExportRun.id, {
          reason: "数据管理导出失败后由用户重试",
          payload_overrides: {
            selected_asset_ids: input.visibleDataAssets.map((asset) => asset.id),
            ...input.sceneProfileLock
          }
        });
        receipt = retried.data;
      } else if (input.dataExportRun && !backendRunSucceeded(input.dataExportRun.status)) {
        receipt = (await getBackendRun(input.dataExportRun.id)).data;
      } else {
        const created = await createExportRun({
          target: "data_assets",
          object_id: input.selectedAsset.assetKey,
          format: "jsonl",
          source: "data_module",
          selected_asset_ids: input.visibleDataAssets.map((asset) => asset.id),
          aggregation_order: input.aggregationOrder,
          filters: input.aggregateFilters,
          ...input.sceneProfileLock
        });
        receipt = created.data;
      }
      const runState = await refreshBackendRunReceipt(receipt);
      input.setDataExportRun(runState);
      input.setDataNotice({
        status: operationStatusFromBackendRun(runState.status),
        title: backendRunFailed(runState.status) ? "导出运行失败，可重试" : backendRunSucceeded(runState.status) ? "导出运行已完成" : "导出运行已创建",
        detail: `${runState.id} · ${backendRunStatusLabel(runState.status)} · trace ${dataShortTrace(runState.trace_id)}；原始音频 URL 不出当前租户边界。`
      });
    } catch (error) {
      input.setDataNotice({
        status: "error",
        title: "导出请求失败，可重试",
        detail: `${error instanceof Error ? error.message : "unknown error"}。未生成本地成功回执。`
      });
    } finally {
      input.setDataAction(null);
    }
  };

  return {
    exportDataAssets,
    openConnectorImport
  };
}
