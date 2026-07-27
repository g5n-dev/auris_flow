import {
  BookOpen,
  ChevronDown,
  Database,
  Folder,
  FolderOpen,
  GripVertical,
  Headphones,
  Play,
  Plus
} from "lucide-react";

import { aggregateMeta } from "../fixtures";
import { formatSessionConfidence } from "../dataTruthModel";
import type { DataAggregateKey } from "../types";
import type { DataWorkspace } from "../useDataWorkspace";

export function DataHierarchyView({
  workspace,
  openConnectorImport
}: {
  workspace: DataWorkspace;
  openConnectorImport: () => void;
}) {
  const {
    aggregateFilterInputs,
    aggregateFilters,
    aggregationOrder,
    clearAggregateFilterKey,
    clearPriorityDragState,
    closedFolders,
    dragOverAggregate,
    draggedAggregate,
    filterMenuKey,
    filteredAggregateOptions,
    formatAggregateFilterValues,
    groupItems,
    isRelationView,
    moveAggregate,
    openAggregateFilterMenu,
    openAssetsFromDataAsset,
    openListeningFromDataAsset,
    pendingDataAssetCount,
    selectedAsset,
    selectedAssetCatalog,
    selectedAssetId,
    setAggregateFilterInputs,
    setDragOverAggregate,
    setDraggedAggregate,
    setFilterMenuKey,
    setSelectedAssetId,
    toggleAggregateFilterValue,
    toggleFolder,
    truthMode,
    visibleDataAssets
  } = workspace;
  return (
      <section className="hierarchy-panel">
        <div className="hierarchy-toolbar">
          <div>
            <span>聚合优先级</span>
            <strong>{aggregationOrder.map((key) => aggregateMeta[key].label).join(" → ")}</strong>
          </div>
          <div className="priority-switcher">
            {aggregationOrder.map((key, index) => {
              const Icon = aggregateMeta[key].icon;
              const selectedFilterValues = aggregateFilters[key];
              const filterSummary = formatAggregateFilterValues(selectedFilterValues);
              const isFiltered = selectedFilterValues.length > 0;
              return (
                <div
                  key={key}
                  className={[
                    "priority-chip",
                    `p${index}`,
                    isFiltered ? "filtered" : "",
                    draggedAggregate === key ? "dragging" : "",
                    dragOverAggregate === key && draggedAggregate !== key ? "drop-target" : ""
                  ].join(" ")}
                  role="button"
                  tabIndex={0}
                  aria-expanded={filterMenuKey === key}
                  onClick={() => openAggregateFilterMenu(key)}
                  onKeyDown={(event) => {
                    if (event.target instanceof HTMLInputElement) return;
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      openAggregateFilterMenu(key);
                    }
                  }}
                  onDragOver={(event) => {
                    event.preventDefault();
                    event.dataTransfer.dropEffect = "move";
                    setDragOverAggregate(key);
                  }}
                  onDragLeave={(event) => {
                    const relatedTarget = event.relatedTarget as Node | null;
                    if (!relatedTarget || !event.currentTarget.contains(relatedTarget)) {
                      setDragOverAggregate((current) => (current === key ? null : current));
                    }
                  }}
                  onDrop={(event) => {
                    event.preventDefault();
                    const sourceKey = event.dataTransfer.getData("text/plain") as DataAggregateKey;
                    if (["space", "time", "event", "person"].includes(sourceKey)) {
                      moveAggregate(sourceKey, key);
                    }
                    clearPriorityDragState();
                  }}
                >
                  <b>{index + 1}</b>
                  <Icon size={14} />
                  <span>{aggregateMeta[key].label}</span>
                  <div className="priority-filter-field">
                    <input
                      value={filterMenuKey === key ? aggregateFilterInputs[key] : filterSummary}
                      placeholder={aggregateMeta[key].hint}
                      title={filterSummary || aggregateMeta[key].hint}
                      onFocus={(event) => {
                        const inputElement = event.currentTarget;
                        openAggregateFilterMenu(key);
                        window.requestAnimationFrame(() => inputElement.select());
                      }}
                      onChange={(event) => {
                        setAggregateFilterInputs((inputs) => ({ ...inputs, [key]: event.target.value }));
                        setFilterMenuKey(key);
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Escape") {
                          event.stopPropagation();
                          setFilterMenuKey(null);
                          setAggregateFilterInputs((inputs) => ({ ...inputs, [key]: filterSummary }));
                        }
                      }}
                    />
                    {filterMenuKey === key && (
                      <div className="priority-filter-menu" onClick={(event) => event.stopPropagation()}>
                        <div className="priority-filter-title">
                          <span>{aggregateMeta[key].label}筛选</span>
                          <button type="button" onClick={() => clearAggregateFilterKey(key)}>
                            清除
                          </button>
                        </div>
                        {filteredAggregateOptions(key).map((option) => {
                          const selected = selectedFilterValues.includes(option.value);
                          return (
                            <button
                              type="button"
                              key={option.value}
                              className={selected ? "selected" : ""}
                              aria-pressed={selected}
                              onClick={() => toggleAggregateFilterValue(key, option.value)}
                            >
                              <span>{option.value}</span>
                              <b>{option.count}</b>
                            </button>
                          );
                        })}
                        {filteredAggregateOptions(key).length === 0 && <div className="priority-filter-empty">无匹配项</div>}
                      </div>
                    )}
                  </div>
                  <div className="priority-actions">
                    <button
                      type="button"
                      className="priority-drag-handle"
                      draggable
                      onClick={(event) => event.stopPropagation()}
                      onMouseDown={(event) => event.stopPropagation()}
                      onDragStart={(event) => {
                        event.stopPropagation();
                        setFilterMenuKey(null);
                        setDraggedAggregate(key);
                        setDragOverAggregate(null);
                        event.dataTransfer.effectAllowed = "move";
                        event.dataTransfer.setData("text/plain", key);
                      }}
                      onDragEnd={clearPriorityDragState}
                      title="拖拽排序"
                      aria-label={`拖拽排序 ${aggregateMeta[key].label}`}
                    >
                      <GripVertical size={14} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="hierarchy-content">
          <div className="folder-tree">
            {isRelationView && (
              <div className="relation-path-overview" aria-label="当前关系路径摘要">
                <div>
                  <span>Root</span>
                  <strong>{aggregateMeta[aggregationOrder[0]].label}</strong>
                  <em>当前树的第一层聚合对象</em>
                </div>
                <div>
                  <span>Join Path</span>
                  <strong>{aggregationOrder.map((key) => aggregateMeta[key].label).join(" → ")}</strong>
                  <em>决定关系展开顺序和右侧对象上下文</em>
                </div>
                <div>
                  <span>Leaf Objects</span>
                  <strong>{visibleDataAssets.length}</strong>
                  <em>音频 / 事件 / 单据 / 人物可追踪叶子</em>
                </div>
                <div>
                  <span>Repair Queue</span>
                  <strong>{pendingDataAssetCount}</strong>
                  <em>低置信、断链或需人工确认</em>
                </div>
              </div>
            )}
            {visibleDataAssets.length === 0 ? (
              <div className="data-empty-import">
                <Database size={22} />
                <strong>当前筛选下没有数据资产</strong>
                <span>清除筛选，或打开任务配置接入数据源并运行一次导入任务。</span>
                <button type="button" onClick={openConnectorImport}>
                  <Plus size={14} />
                  新建音频导入配置
                </button>
              </div>
            ) : groupItems(visibleDataAssets, aggregationOrder[0]).map(([levelOne, levelOneItems]) => {
              const levelOneKey = `${aggregationOrder[0]}:${levelOne}`;
              const levelOneClosed = closedFolders.has(levelOneKey);
              return (
                <div className="folder-branch level-0" key={levelOne}>
                  <button
                    type="button"
                    className={levelOneClosed ? "folder-row primary-folder collapsed" : "folder-row primary-folder"}
                    aria-expanded={!levelOneClosed}
                    onClick={() => toggleFolder(levelOneKey)}
                  >
                    {levelOneClosed ? <Folder size={20} /> : <FolderOpen size={20} />}
                    <div>
                      <strong>{levelOne}</strong>
                      <span>{aggregateMeta[aggregationOrder[0]].label}优先 · {levelOneItems.length} 个对象</span>
                    </div>
                    <b>{levelOneItems.filter((item) => item.status !== "confirmed").length} 待处理</b>
                    <ChevronDown className="folder-toggle-icon" size={15} />
                  </button>
                  {!levelOneClosed &&
                    groupItems(levelOneItems, aggregationOrder[1]).map(([levelTwo, levelTwoItems]) => {
                      const levelTwoKey = `${levelOneKey}>${aggregationOrder[1]}:${levelTwo}`;
                      const levelTwoClosed = closedFolders.has(levelTwoKey);
                      return (
                        <div className="folder-branch level-1" key={levelTwo}>
                          <button
                            type="button"
                            className={levelTwoClosed ? "folder-row secondary-folder collapsed" : "folder-row secondary-folder"}
                            aria-expanded={!levelTwoClosed}
                            onClick={() => toggleFolder(levelTwoKey)}
                          >
                            {levelTwoClosed ? <Folder size={17} /> : <FolderOpen size={17} />}
                            <div>
                              <strong>{levelTwo}</strong>
                              <span>{aggregateMeta[aggregationOrder[1]].label}聚合 · {levelTwoItems.length} 条</span>
                            </div>
                            <ChevronDown className="folder-toggle-icon" size={14} />
                          </button>
                          {!levelTwoClosed &&
                            groupItems(levelTwoItems, aggregationOrder[2]).map(([levelThree, leafItems]) => {
                              const levelThreeKey = `${levelTwoKey}>${aggregationOrder[2]}:${levelThree}`;
                              const levelThreeClosed = closedFolders.has(levelThreeKey);
                              return (
                                <div className="folder-branch level-2" key={levelThree}>
                                  <button
                                    type="button"
                                    className={levelThreeClosed ? "folder-row tertiary-folder collapsed" : "folder-row tertiary-folder"}
                                    aria-expanded={!levelThreeClosed}
                                    onClick={() => toggleFolder(levelThreeKey)}
                                  >
                                    {levelThreeClosed ? <Folder size={15} /> : <FolderOpen size={15} />}
                                    <div>
                                      <strong>{levelThree}</strong>
                                      <span>{aggregateMeta[aggregationOrder[2]].label}聚合 · 叶子 {leafItems.length}</span>
                                    </div>
                                    <ChevronDown className="folder-toggle-icon" size={13} />
                                  </button>
                                  {!levelThreeClosed && (
                                    <div className="asset-leaf-list">
                                      {leafItems.map((item) => (
                                        <button
                                          key={item.id}
                                          className={selectedAssetId === item.id ? `asset-leaf audio-asset-leaf active ${item.status}` : `asset-leaf audio-asset-leaf ${item.status}`}
                                          onClick={() => setSelectedAssetId(item.id)}
                                        >
                                          <span className="leaf-play">
                                            <Play size={15} fill="currentColor" strokeWidth={0} />
                                          </span>
                                          <div>
                                            <strong>{item.audio}</strong>
                                            <span>{item.duration} · {item.person} · {item.event}</span>
                                            <small>{item.assetKey} · {item.assetCheck}</small>
                                          </div>
                                          <div className="leaf-tags">
                                            {truthMode
                                              ? item.processingProducts?.length
                                                ? item.processingProducts.map((product) => <em key={product}>{product}</em>)
                                                : <em>处理产物未提供</em>
                                              : <><em>VAD</em><em>ASR</em></>}
                                            <em>{item.status === "confirmed" ? "已入库" : item.status === "pending" ? "待复核" : "风险"}</em>
                                          </div>
                                          <b>{formatSessionConfidence(item.confidence)}</b>
                                        </button>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                        </div>
                      );
                    })}
                </div>
              );
            })}
          </div>

          <aside className="hierarchy-detail">
            <div className={`detail-status ${selectedAsset.status}`}>
              <span>当前音频资产</span>
              <strong>{selectedAsset.audio}</strong>
              <b>{formatSessionConfidence(selectedAsset.confidence)}</b>
            </div>
            <div className="detail-path">
              {aggregationOrder.map((key) => (
                <span key={key}>
                  <b>{aggregateMeta[key].label}</b>
                  {selectedAsset[key]}
                </span>
              ))}
            </div>
            <div className="detail-evidence">
              <strong>{truthMode ? "音频会话" : "录音切片"} / {selectedAsset.id}</strong>
              <p>{selectedAsset.duration} · {selectedAsset.event} · {selectedAsset.assetCheck}</p>
              <div>
                {truthMode
                  ? selectedAsset.processingProducts?.length
                    ? selectedAsset.processingProducts.map((product) => <em key={product}>{product}</em>)
                    : <em>处理产物未提供</em>
                  : <><em>raw wav</em><em>voice_segments</em><em>ASR transcript</em></>}
              </div>
            </div>
            <div className="detail-docs">
              <span>关联单据</span>
              {selectedAsset.docs.map((doc) => (
                <button key={doc}>{doc}</button>
              ))}
            </div>
            <div className="detail-dagster-card">
              <div className="detail-dagster-head">
                <span>关联数据资产</span>
                <strong>{selectedAssetCatalog.name}</strong>
                <b>{selectedAssetCatalog.quality ?? "未提供"}</b>
              </div>
              <div className="detail-dagster-grid">
                <span>
                  <b>资产 Key</b>
                  {selectedAsset.assetKey}
                </span>
                <span>
                  <b>分区</b>
                  {selectedAsset.partitionKey}
                </span>
                <span>
                  <b>生成记录</b>
                  {selectedAsset.materializationId}
                </span>
                <span>
                  <b>运行</b>
                  {selectedAsset.dagsterRunId}
                </span>
                <span>
                  <b>质量检查</b>
                  {selectedAsset.assetCheck}
                </span>
                <span>
                  <b>新鲜度</b>
                  {selectedAsset.freshness}
                </span>
              </div>
              <div className="detail-lineage-chips">
                <strong>上游</strong>
                {selectedAsset.upstreamAssets.map((asset) => (
                  <em key={asset}>{asset}</em>
                ))}
                <strong>下游</strong>
                {selectedAsset.downstreamAssets.map((asset) => (
                  <em key={asset}>{asset}</em>
                ))}
              </div>
              <code>{selectedAsset.bffEndpoint}</code>
            </div>
            <div className="detail-actions">
              <button onClick={() => openListeningFromDataAsset(selectedAsset)}>
                <Headphones size={14} />
                进入调听
              </button>
              <button onClick={() => openAssetsFromDataAsset(selectedAsset)}>
                <BookOpen size={14} />
                资产血缘
              </button>
            </div>
          </aside>
        </div>
      </section>
      );
}
