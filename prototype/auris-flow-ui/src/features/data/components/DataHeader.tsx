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
import { dataAssets } from "../dataAssets";
import type { DataWorkspace } from "../useDataWorkspace";

export function DataHeader({ workspace }: { workspace: DataWorkspace }) {
  const {
    activeDataTabLabel,
    aggregationOrder,
    dataAction,
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
    visibleDataAssets
  } = workspace;
  return <section className="data-reference-head">
        <div>
          <h2>{isRelationView ? "项目关系资产" : "项目数据资产"}</h2>
          <p>
            {isRelationView
              ? `当前视图：跨实体关系链 · ${visibleDataAssets.length}/${dataAssets.length} 个叶子对象 · 断链待处理 ${pendingDataAssetCount}`
              : `当前聚合：${activeDataTabLabel} · ${visibleDataAssets.length}/${dataAssets.length} 个叶子对象 · 待处理 ${pendingDataAssetCount}`}
          </p>
          <div className="data-asset-inline-status">
            <span>{isRelationView ? "接口关系已对齐 3" : "接口已对齐 3"}</span>
            <span>{isRelationView ? "断链需确认 1" : "需确认 1"}</span>
            <span>{isRelationView ? "关系路径" : "优先级"} {aggregationOrder.map((key) => aggregateMeta[key].label).join(" → ")}</span>
          </div>
        </div>
        <div>
          <button type="button" className="data-connect-button" disabled={dataAction === "connector-import"} onClick={openConnectorImport}>
            <Plus size={15} />
            {dataAction === "connector-import" ? "创建中" : "连接器导入"}
          </button>
          <button type="button" className="data-contract-button" aria-expanded={!isContractCollapsed} onClick={() => setIsContractCollapsed((current) => !current)}>
            <ShieldCheck size={15} />
            {isContractCollapsed ? "接口详情" : "隐藏接口"}
          </button>
          <button
            type="button"
            className={isPivotCollapsed ? "pivot-visibility-toggle" : "pivot-visibility-toggle active"}
            aria-expanded={!isPivotCollapsed}
            onClick={() => setIsPivotCollapsed((current) => !current)}
            title={isPivotCollapsed ? "显示维度矩阵" : "隐藏维度矩阵"}
          >
            {isPivotCollapsed ? <Eye size={15} /> : <EyeOff size={15} />}
            {isPivotCollapsed ? "显示矩阵" : "隐藏矩阵"}
          </button>
          <button
            type="button"
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
          <button type="button" disabled={dataAction === "export"} onClick={exportDataAssets}>
            <Download size={15} />
            {dataAction === "export" ? "处理中" : dataExportRun && backendRunFailed(dataExportRun.status) ? "重试导出" : dataExportRun && !backendRunSucceeded(dataExportRun.status) ? "刷新导出" : "导出"}
          </button>
        </div>
      </section>;
}
