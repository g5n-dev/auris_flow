import { matrixTimes } from "../../fixtures/evidenceFixtures";
import { matrixStatusMeta } from "../../fixtures/matrixFixtures";
import type { MarkState } from "../../types";
import type { MatrixDimension, MatrixModeProps, MatrixStatus } from "./matrixModeTypes";
import type { MatrixSelectionState } from "./useMatrixSelectionState";

export function createMatrixActions(context: MatrixSelectionState) {
  const { activeCell, activeDimension, findBestFindingKey, findingByKey, getCellFromKey, matrixFindings, matrixRows, onlyAnomalies, selectedWindow, setActiveCell, setActiveDimensionKey, setMarkState, setOnlyAnomalies, setPreviewState, setReviewReceipt, setSelectedWindow, setShowFullMatrix, setStatusFilter, showFullMatrix, statusFilter, visibleCols, visibleRowIds, windowForColumn } = context;
  const rowMatchesCurrentFilter = (rowId: string) => {
      if (!statusFilter && !onlyAnomalies) return true;
      return matrixFindings.some(
        (finding) =>
          finding.rowId === rowId &&
          visibleCols.includes(finding.col) &&
          (!statusFilter || finding.status === statusFilter)
      );
    };

  const visibleRows = (showFullMatrix ? matrixRows : matrixRows.filter((row) => visibleRowIds.includes(row.id))).filter((row) => rowMatchesCurrentFilter(row.id));

  const activeFinding = getCellFromKey(activeCell);

  const activeRow = matrixRows.find((row) => row.id === activeFinding.rowId) ?? matrixRows[2];

  const activeTime = matrixTimes[activeFinding.col];

  const statusCounts = matrixFindings.reduce<Record<string, number>>((acc, item) => {
      acc[item.status] = (acc[item.status] ?? 0) + 1;
      return acc;
    }, {});

  const statusFilters: Array<{ key: MatrixStatus; label: string }> = [
      { key: "main", label: "主录音" },
      { key: "danger", label: "串音候选" },
      { key: "low", label: "低置信" },
      { key: "missing", label: "缺失" }
    ];

  const pickCell = (rowId: string, col: number) => {
      const key = `${rowId}-${col}`;
      const finding = findingByKey[key];
      setActiveCell(finding ? key : `${rowId}-${col}`);
      setSelectedWindow(windowForColumn(col));
      setReviewReceipt(
        finding
          ? `已选中 ${matrixTimes[col]} / ${matrixRows.find((row) => row.id === rowId)?.label ?? rowId}：${finding.label}，等待人审决策。`
          : `已选中 ${matrixTimes[col]} / ${matrixRows.find((row) => row.id === rowId)?.label ?? rowId} 背景格，可作为异常对照。`
      );
    };

  const pickRow = (rowId: string) => {
      const best = matrixFindings
        .filter((finding) => finding.rowId === rowId && visibleCols.includes(finding.col) && (!statusFilter || finding.status === statusFilter))
        .sort((a, b) => matrixStatusMeta[b.status].severity - matrixStatusMeta[a.status].severity || b.score - a.score)[0];
      pickCell(rowId, best?.col ?? visibleCols[0] ?? 0);
    };

  const selectMatrixDimension = (dimension: MatrixDimension) => {
      setActiveDimensionKey(dimension.key);
      setShowFullMatrix(false);
      const bestCell = findBestFindingKey(dimension) ?? dimension.defaultCell;
      const bestFinding = getCellFromKey(bestCell);
      setActiveCell(bestCell);
      setSelectedWindow(windowForColumn(bestFinding.col));
      setReviewReceipt(`${dimension.label}路径已载入：${dimension.summary}，默认定位 ${bestFinding.label}。`);
    };

  const toggleStatusFilter = (status: MatrixStatus) => {
      const nextFilter = statusFilter === status ? null : status;
      setStatusFilter(nextFilter);
      setOnlyAnomalies(Boolean(nextFilter));
      const bestCell = findBestFindingKey(activeDimension, nextFilter) ?? activeDimension.defaultCell;
      const bestFinding = getCellFromKey(bestCell);
      setActiveCell(bestCell);
      setSelectedWindow(windowForColumn(bestFinding.col));
      setReviewReceipt(nextFilter ? `已筛选 ${matrixStatusMeta[nextFilter].label}，默认定位最高优先级异常。` : "已取消状态筛选，恢复当前审查路径。");
    };

  const toggleOnlyAnomalies = () => {
      const nextOnlyAnomalies = !onlyAnomalies;
      setOnlyAnomalies(nextOnlyAnomalies);
      setStatusFilter(null);
      const bestCell = nextOnlyAnomalies ? findBestFindingKey(activeDimension, null) ?? activeDimension.defaultCell : activeDimension.defaultCell;
      const bestFinding = getCellFromKey(bestCell);
      setActiveCell(bestCell);
      setSelectedWindow(windowForColumn(bestFinding.col));
      setReviewReceipt(nextOnlyAnomalies ? "已切换为只看异常，普通背景格降权展示。" : "已恢复背景录音对照，异常和正常格一起展示。");
    };

  const resetMatrixFilters = () => {
      const bestCell = findBestFindingKey(activeDimension, null) ?? activeDimension.defaultCell;
      const bestFinding = getCellFromKey(bestCell);
      setStatusFilter(null);
      setOnlyAnomalies(false);
      setShowFullMatrix(false);
      setActiveCell(bestCell);
      setSelectedWindow(windowForColumn(bestFinding.col));
      setReviewReceipt("已重置矩阵筛选，回到当前审查路径的推荐异常。");
    };

  const previewCurrentSegment = () => {
      setPreviewState("playing");
      setReviewReceipt(`独立试听 ${activeRow.label} / ${activeTime}，不切换主页面播放器状态。`);
      window.setTimeout(() => setPreviewState("idle"), 900);
    };

  const commitMatrixAction = (state: MarkState, label: string) => {
      setMarkState(state);
      setReviewReceipt(`${activeRow.label} / ${activeTime} 已${label}，回写到 ${selectedWindow} 复核窗口草稿。`);
    };

  const currentFilterLabel = statusFilter ? matrixStatusMeta[statusFilter].label : onlyAnomalies ? "只看异常" : "当前路径全部";

  return { ...context, rowMatchesCurrentFilter, visibleRows, activeFinding, activeRow, activeTime, statusCounts, statusFilters, pickCell, pickRow, selectMatrixDimension, toggleStatusFilter, toggleOnlyAnomalies, resetMatrixFilters, previewCurrentSegment, commitMatrixAction, currentFilterLabel };
}

export type MatrixModeController = ReturnType<typeof createMatrixActions>;
