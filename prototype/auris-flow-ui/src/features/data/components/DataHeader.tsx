import {
  Download,
  Eye,
  EyeOff,
  ListFilter,
  Plus,
  ShieldCheck
} from "lucide-react";

import {
  backendRunFailed,
  backendRunSucceeded
} from "../../../shared/runtime/backendRunStatus";
import { aggregateMeta } from "../fixtures";
import type { DataWorkspace } from "../useDataWorkspace";

export function DataHeader({ workspace }: { workspace: DataWorkspace }) {
  const {
    activeDataTabLabel,
    aggregationOrder,
    canExportData,
    canImportConnector,
    canUsePivot,
    connectorImportBlockedReason,
    dataAction,
    dataAssetCount,
    dataExportBlockedReason,
    dataExportRun,
    exportDataAssets,
    isContractCollapsed,
    isPivotCollapsed,
    isRelationView,
    openConnectorImport,
    pendingDataAssetCount,
    setDataNotice,
    setIsContractCollapsed,
    setIsPivotCollapsed,
    sceneProfileBlockedReason,
    sceneProfileLock,
    truthMode,
    visibleDataAssets
  } = workspace;
  return <section className="data-reference-head">
        <div>
          <h2>{isRelationView ? "项目关系资产" : "项目数据资产"}</h2>
          <p>
            {isRelationView
              ? `当前视图：跨实体关系链 · ${visibleDataAssets.length}/${dataAssetCount} 个叶子对象 · 断链待处理 ${pendingDataAssetCount}`
              : `当前聚合：${activeDataTabLabel} · ${visibleDataAssets.length}/${dataAssetCount} 个叶子对象 · 待处理 ${pendingDataAssetCount}`}
          </p>
          <div className="data-asset-inline-status">
            <span>{truthMode ? `BFF 会话 ${visibleDataAssets.length}` : isRelationView ? "接口关系已对齐 3" : "接口已对齐 3"}</span>
            <span>{truthMode ? `待处理 ${pendingDataAssetCount}` : isRelationView ? "断链需确认 1" : "需确认 1"}</span>
            <span>{isRelationView ? "关系路径" : "优先级"} {aggregationOrder.map((key) => aggregateMeta[key].label).join(" → ")}</span>
          </div>
        </div>
        <div>
          <button
            type="button"
            className="data-connect-button"
            data-testid="data-connector-import"
            disabled={dataAction === "connector-import" || !canImportConnector}
            title={!canImportConnector ? connectorImportBlockedReason : undefined}
            onClick={openConnectorImport}
          >
            <Plus size={15} />
            {dataAction === "connector-import" ? "创建中" : "连接器导入"}
          </button>
          {!canImportConnector && (
            <span data-testid="data-connector-blocked-reason">{connectorImportBlockedReason}</span>
          )}
          {!sceneProfileLock && (
            <span data-testid="data-project-write-blocked-reason">{sceneProfileBlockedReason}</span>
          )}
          <button
            type="button"
            className="data-contract-button"
            aria-expanded={!isContractCollapsed}
            disabled={truthMode}
            title={truthMode ? "当前页只展示运行时 BFF 事实；接口契约请查阅项目文档" : undefined}
            onClick={() => setIsContractCollapsed((current) => !current)}
          >
            <ShieldCheck size={15} />
            {isContractCollapsed ? "接口详情" : "隐藏接口"}
          </button>
          <button
            type="button"
            className={isPivotCollapsed ? "pivot-visibility-toggle" : "pivot-visibility-toggle active"}
            aria-expanded={!isPivotCollapsed}
            disabled={!canUsePivot}
            onClick={() => setIsPivotCollapsed((current) => !current)}
            title={!canUsePivot ? "BFF 维度矩阵读模型尚未接入" : isPivotCollapsed ? "显示维度矩阵" : "隐藏维度矩阵"}
          >
            {isPivotCollapsed ? <Eye size={15} /> : <EyeOff size={15} />}
            {isPivotCollapsed ? "显示矩阵" : "隐藏矩阵"}
          </button>
          <button
            type="button"
            disabled={!canUsePivot}
            title={!canUsePivot ? "BFF 维度矩阵读模型尚未接入" : undefined}
            onClick={() => {
              setIsPivotCollapsed(false);
              setDataNotice({
                status: "success",
                title: "筛选面板已打开",
                detail: "可按空间、时间、事件、人物调整聚合顺序和局部筛选。"
              });
            }}
          >
            <ListFilter size={15} />
            筛选
          </button>
          <button
            type="button"
            data-testid="data-export"
            disabled={dataAction === "export" || !canExportData}
            title={!canExportData ? dataExportBlockedReason : undefined}
            onClick={exportDataAssets}
          >
            <Download size={15} />
            {dataAction === "export" ? "处理中" : dataExportRun && backendRunFailed(dataExportRun.status) ? "重试导出" : dataExportRun && !backendRunSucceeded(dataExportRun.status) ? "刷新导出" : "导出"}
          </button>
        </div>
      </section>;
}
