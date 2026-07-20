import { useState } from "react";

import type { DataAssetItem } from "../../shared/contracts/dataAssets";
import type { OperationNotice } from "../../shared/contracts/operations";
import { dataAssetCatalogRows } from "./catalog";
import { createDataOperations } from "./dataOperations";
import type { DataProjectionAssetItem } from "./dataProjection";
import { projectTruthAssetCatalog } from "./dataTruthModel";
import { aggregateMeta } from "./fixtures";
import { deriveDataRelations } from "./relations";
import type { DataAggregateKey, DataExportRun, DataModuleProps, DataSceneProfileLock } from "./types";

type DataWorkspaceInput = DataModuleProps & {
  dataItems: DataAssetItem[];
  truthMode: boolean;
};

export function useDataWorkspace({
  activeTab,
  setActiveModule,
  selectedAssetId,
  setSelectedAssetId,
  openListeningFromDataAsset,
  openAssetsFromDataAsset,
  dataItems,
  truthMode,
  workspaceSceneBinding,
  workspaceSceneState
}: DataWorkspaceInput) {
  const [aggregationOrder, setAggregationOrder] = useState<DataAggregateKey[]>(["space", "time", "event", "person"]);
  const [draggedAggregate, setDraggedAggregate] = useState<DataAggregateKey | null>(null);
  const [dragOverAggregate, setDragOverAggregate] = useState<DataAggregateKey | null>(null);
  const [aggregateFilters, setAggregateFilters] = useState<Record<DataAggregateKey, string[]>>({
    space: [],
    time: [],
    event: [],
    person: []
  });
  const [aggregateFilterInputs, setAggregateFilterInputs] = useState<Record<DataAggregateKey, string>>({
    space: "",
    time: "",
    event: "",
    person: ""
  });
  const [filterMenuKey, setFilterMenuKey] = useState<DataAggregateKey | null>(null);
  const [isPivotCollapsed, setIsPivotCollapsed] = useState(true);
  const [isContractCollapsed, setIsContractCollapsed] = useState(true);
  const [closedFolders, setClosedFolders] = useState<Set<string>>(() => new Set());
  const [dataNotice, setDataNotice] = useState<OperationNotice>({
    status: "idle",
    title: "等待数据操作",
    detail: "筛选、连接器导入、血缘查看和导出会保留当前聚合上下文。"
  });
  const [dataAction, setDataAction] = useState<string | null>(null);
  const [dataExportRun, setDataExportRun] = useState<DataExportRun>(null);
  const selectedAsset = dataItems.find((item) => item.id === selectedAssetId) ?? dataItems[0]!;
  const selectedAssetCatalog = truthMode
    ? projectTruthAssetCatalog(selectedAsset)
    : dataAssetCatalogRows.find((asset) => asset.assetKey === selectedAsset.assetKey) ?? dataAssetCatalogRows[0];
  const aggregateKeys = Object.keys(aggregateMeta) as DataAggregateKey[];
  const visibleDataAssets = dataItems.filter((item) =>
    aggregateKeys.every((key) => aggregateFilters[key].length === 0 || aggregateFilters[key].includes(item[key]))
  );
  const pendingDataAssetCount = visibleDataAssets.filter((item) => item.status !== "confirmed").length;
  const isRelationView = activeTab === "relations";
  const aggregateFilterOptions = (Object.keys(aggregateMeta) as DataAggregateKey[]).reduce(
    (options, key) => {
      const counts = dataItems.reduce<Map<string, number>>((acc, item) => {
        acc.set(item[key], (acc.get(item[key]) ?? 0) + 1);
        return acc;
      }, new Map());
      options[key] = Array.from(counts, ([value, count]) => ({ value, count }));
      return options;
    },
    {} as Record<DataAggregateKey, Array<{ value: string; count: number }>>
  );
  const groupItems = (items: DataAssetItem[], key: DataAggregateKey) => {
    return Array.from(
      items.reduce<Map<string, DataAssetItem[]>>((groups, item) => {
        const value = item[key];
        groups.set(value, [...(groups.get(value) ?? []), item]);
        return groups;
      }, new Map())
    );
  };
  const formatAggregateFilterValues = (values: string[]) => {
    if (values.length <= 2) return values.join("、");
    return `${values.slice(0, 2).join("、")} +${values.length - 2}`;
  };
  const openAggregateFilterMenu = (key: DataAggregateKey) => {
    setFilterMenuKey((current) => {
      if (current !== key) {
        setAggregateFilterInputs((inputs) => ({
          ...inputs,
          [key]: formatAggregateFilterValues(aggregateFilters[key])
        }));
      }
      return key;
    });
  };
  const toggleAggregateFilterValue = (key: DataAggregateKey, value: string) => {
    setAggregateFilters((current) => {
      const currentValues = current[key];
      const nextValues = currentValues.includes(value) ? currentValues.filter((item) => item !== value) : [...currentValues, value];
      setAggregateFilterInputs((inputs) => ({ ...inputs, [key]: formatAggregateFilterValues(nextValues) }));
      return { ...current, [key]: nextValues };
    });
    setClosedFolders(new Set());
    setDataNotice({
      status: "success",
      title: "聚合筛选已更新",
      detail: `${aggregateMeta[key].label}筛选已${aggregateFilters[key].includes(value) ? "移除" : "加入"}：${value}。`
    });
  };
  const clearAggregateFilterKey = (key: DataAggregateKey) => {
    setAggregateFilters((current) => ({ ...current, [key]: [] }));
    setAggregateFilterInputs((inputs) => ({ ...inputs, [key]: "" }));
    setClosedFolders(new Set());
    setDataNotice({
      status: "success",
      title: "筛选已清除",
      detail: `${aggregateMeta[key].label}维度已恢复全部数据。`
    });
  };
  const filteredAggregateOptions = (key: DataAggregateKey) => {
    const selectedSummary = formatAggregateFilterValues(aggregateFilters[key]);
    const query = aggregateFilterInputs[key].trim();
    const activeQuery = query && query !== selectedSummary ? query.toLowerCase() : "";
    return aggregateFilterOptions[key].filter((option) => !activeQuery || option.value.toLowerCase().includes(activeQuery));
  };
  const toggleFolder = (folderKey: string) => {
    setClosedFolders((current) => {
      const next = new Set(current);
      if (next.has(folderKey)) {
        next.delete(folderKey);
      } else {
        next.add(folderKey);
      }
      return next;
    });
  };
  const moveAggregate = (sourceKey: DataAggregateKey, targetKey: DataAggregateKey) => {
    if (sourceKey === targetKey) return;
    setAggregationOrder((current) => {
      const sourceIndex = current.indexOf(sourceKey);
      const targetIndex = current.indexOf(targetKey);
      if (sourceIndex < 0 || targetIndex < 0) return current;
      const next = [...current];
      const [moved] = next.splice(sourceIndex, 1);
      next.splice(targetIndex, 0, moved);
      return next;
    });
    setDataNotice({
      status: "success",
      title: "聚合优先级已调整",
      detail: `${aggregateMeta[sourceKey].label} 已移动到 ${aggregateMeta[targetKey].label} 附近，关系树会按新顺序展开。`
    });
  };

  const clearPriorityDragState = () => {
    setDraggedAggregate(null);
    setDragOverAggregate(null);
  };
  const dataTabLabelMap: Record<string, string> = {
    audio: "音频数据",
    people: "人物/声纹",
    events: "事件",
    relations: "关联视图"
  };
  const activeDataTabLabel = dataTabLabelMap[activeTab] ?? activeTab;
  const selectedAssetRelations = deriveDataRelations(
    selectedAsset,
    selectedAssetCatalog.name,
    openListeningFromDataAsset,
    openAssetsFromDataAsset
  );
  const relationRepairRows = visibleDataAssets.filter((item) => item.status !== "confirmed");
  const projectionAsset = selectedAsset as DataProjectionAssetItem;
  const bindingIsAuthoritative = workspaceSceneState === "bound"
    && workspaceSceneBinding?.environment === "production"
    && workspaceSceneBinding.status === "active"
    && workspaceSceneBinding.version.status === "published"
    && workspaceSceneBinding.scene_profile_id === workspaceSceneBinding.version.scene_profile_id
    && workspaceSceneBinding.scene_profile_version_id === workspaceSceneBinding.version.scene_profile_version_id
    && workspaceSceneBinding.manifest_sha256 === workspaceSceneBinding.version.manifest_sha256
    && /^[0-9a-f]{64}$/.test(workspaceSceneBinding.manifest_sha256);
  const sceneProfileLock: DataSceneProfileLock | null = bindingIsAuthoritative && workspaceSceneBinding
    ? {
        scene_profile_id: workspaceSceneBinding.scene_profile_id,
        scene_profile_version_id: workspaceSceneBinding.scene_profile_version_id,
        scene_profile_snapshot_sha256: workspaceSceneBinding.manifest_sha256
      }
    : null;
  const sceneProfileBlockedReason = workspaceSceneState === "pending"
    ? "正在读取当前项目的已发布 SceneProfile，项目级写入与导出保持禁用"
    : workspaceSceneState === "error"
      ? "SceneProfile 绑定读取失败，项目级写入与导出保持禁用"
      : workspaceSceneState === "unbound"
        ? "当前项目未绑定已发布 SceneProfile，项目级写入与导出保持禁用"
        : "SceneProfile 绑定快照不完整或发生漂移，项目级写入与导出保持禁用";
  const targetAssetVerified = !truthMode || projectionAsset.connectorImportEnabled === true;
  const canImportConnector = sceneProfileLock !== null && targetAssetVerified;
  const connectorImportBlockedReason = !sceneProfileLock
    ? sceneProfileBlockedReason
    : targetAssetVerified
      ? ""
      : projectionAsset.connectorImportBlockedReason || "当前数据对象未提供可验证的目标资产";
  const canExportData = sceneProfileLock !== null && Boolean(selectedAsset.assetKey);
  const dataExportBlockedReason = !sceneProfileLock
    ? sceneProfileBlockedReason
    : selectedAsset.assetKey
      ? ""
      : "当前数据对象未绑定可验证的目标资产，项目级导出保持禁用";

  const operations = createDataOperations({
    activeTab,
    aggregateFilters,
    aggregationOrder,
    dataAction,
    dataExportRun,
    isRelationView,
    canImportConnector,
    connectorImportBlockedReason,
    canExportData,
    dataExportBlockedReason,
    sceneProfileLock,
    selectedAsset,
    setDataAction,
    setDataExportRun,
    setDataNotice,
    visibleDataAssets
  });

  return {
    activeDataTabLabel,
    activeTab,
    aggregateFilterInputs,
    aggregateFilterOptions,
    aggregateFilters,
    aggregateKeys,
    aggregationOrder,
    clearAggregateFilterKey,
    clearPriorityDragState,
    closedFolders,
    dataAction,
    dataAssetCount: dataItems.length,
    dataExportRun,
    dataNotice,
    dragOverAggregate,
    draggedAggregate,
    filterMenuKey,
    filteredAggregateOptions,
    formatAggregateFilterValues,
    groupItems,
    isContractCollapsed,
    isPivotCollapsed,
    isRelationView,
    canImportConnector,
    canExportData,
    canUsePivot: !truthMode,
    connectorImportBlockedReason,
    dataExportBlockedReason,
    moveAggregate,
    openAggregateFilterMenu,
    openAssetsFromDataAsset,
    openListeningFromDataAsset,
    pendingDataAssetCount,
    relationRepairRows,
    selectedAsset,
    selectedAssetCatalog,
    selectedAssetId,
    setActiveModule,
    setAggregateFilterInputs,
    setAggregationOrder,
    setClosedFolders,
    setDataNotice,
    setDragOverAggregate,
    setDraggedAggregate,
    setFilterMenuKey,
    setIsContractCollapsed,
    setIsPivotCollapsed,
    setSelectedAssetId,
    sceneProfileLock,
    sceneProfileBlockedReason,
    toggleAggregateFilterValue,
    toggleFolder,
    truthMode,
    visibleDataAssets,
    ...selectedAssetRelations,
    ...operations
  };
}

export type DataWorkspace = ReturnType<typeof useDataWorkspace>;
