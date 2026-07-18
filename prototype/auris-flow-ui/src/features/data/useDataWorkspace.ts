import { useState } from "react";

import type { DataAssetItem } from "../../shared/contracts/dataAssets";
import type { OperationNotice } from "../../shared/contracts/operations";
import { dataAssetCatalogRows } from "./catalog";
import { dataAssets } from "./dataAssets";
import { createDataOperations } from "./dataOperations";
import { aggregateMeta } from "./fixtures";
import { deriveDataRelations } from "./relations";
import type { DataAggregateKey, DataExportRun, DataModuleProps } from "./types";

export function useDataWorkspace({
  activeTab,
  setActiveModule,
  selectedAssetId,
  setSelectedAssetId,
  openListeningFromDataAsset,
  openAssetsFromDataAsset
}: DataModuleProps) {
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
  const selectedAsset = dataAssets.find((item) => item.id === selectedAssetId) ?? dataAssets[0];
  const selectedAssetCatalog = dataAssetCatalogRows.find((asset) => asset.assetKey === selectedAsset.assetKey) ?? dataAssetCatalogRows[0];
  const aggregateKeys = Object.keys(aggregateMeta) as DataAggregateKey[];
  const visibleDataAssets = dataAssets.filter((item) =>
    aggregateKeys.every((key) => aggregateFilters[key].length === 0 || aggregateFilters[key].includes(item[key]))
  );
  const pendingDataAssetCount = visibleDataAssets.filter((item) => item.status !== "confirmed").length;
  const isRelationView = activeTab === "relations";
  const aggregateFilterOptions = (Object.keys(aggregateMeta) as DataAggregateKey[]).reduce(
    (options, key) => {
      const counts = dataAssets.reduce<Map<string, number>>((acc, item) => {
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

  const operations = createDataOperations({
    activeTab,
    aggregateFilters,
    aggregationOrder,
    dataAction,
    dataExportRun,
    isRelationView,
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
    toggleAggregateFilterValue,
    toggleFolder,
    visibleDataAssets,
    ...selectedAssetRelations,
    ...operations
  };
}

export type DataWorkspace = ReturnType<typeof useDataWorkspace>;
