import { matrixTimes } from "../../fixtures/evidenceFixtures";
import { matrixStatusMeta } from "../../fixtures/matrixFixtures";
import type { MatrixDimension, MatrixFinding, MatrixModeProps, MatrixStatus } from "./matrixModeTypes";
import type { MatrixFilterState } from "./useMatrixFilterState";
import type { CSSProperties } from "react";

export function buildMatrixFilterModel(context: MatrixFilterState) {
  const { activeDimensionKey, findingByKey, matrixDimensions, matrixFindings, showFullMatrix, statusFilter } = context;
  const activeDimension = matrixDimensions.find((item) => item.key === activeDimensionKey) ?? matrixDimensions[1];

  const fallbackFinding = (rowId: string, col: number): MatrixFinding => ({
      key: `${rowId}-${col}`,
      rowId,
      col,
      status: col === 3 ? "watch" : "normal",
      label: col === 3 ? "关注" : "正常",
      score: col === 3 ? 0.66 : 0.22 + ((col * 7 + rowId.length) % 34) / 100,
      overlap: col === 3 ? 6 : 2 + ((col + rowId.length) % 4),
      records: col === 3 ? 2 : 1,
      reason: "该格未命中强异常，仅保留为同时间窗背景录音。",
      evidence: ["有录音", "无重复 ASR", "未关联单据异常"]
    });

  const getCell = (rowId: string, col: number) => findingByKey[`${rowId}-${col}`] ?? fallbackFinding(rowId, col);

  const getCellFromKey = (key: string) => {
      const separatorIndex = key.lastIndexOf("-");
      const rowId = separatorIndex > -1 ? key.slice(0, separatorIndex) : key;
      const colValue = separatorIndex > -1 ? key.slice(separatorIndex + 1) : "0";
      const col = Number(colValue);
      return findingByKey[key] ?? fallbackFinding(rowId, Number.isNaN(col) ? 0 : col);
    };

  const windowForColumn = (col: number) => {
      if (col === 3) return "12:23 - 12:33";
      if (col === 5) return "14:00 - 14:10";
      const start = matrixTimes[col] ?? "当前";
      const end = matrixTimes[col + 1] ?? `${start} 后`;
      return `${start} - ${end}`;
    };

  const findBestFindingKey = (dimension: MatrixDimension, filter: MatrixStatus | null = statusFilter) => {
      const candidates = matrixFindings.filter(
        (finding) =>
          dimension.rowIds.includes(finding.rowId) &&
          dimension.cols.includes(finding.col) &&
          (!filter || finding.status === filter)
      );
      return candidates.sort((a, b) => matrixStatusMeta[b.status].severity - matrixStatusMeta[a.status].severity || b.score - a.score)[0]?.key;
    };

  const visibleRowIds: readonly string[] = activeDimension.rowIds;

  const visibleCols = showFullMatrix ? matrixTimes.map((_, index) => index) : activeDimension.cols;

  const matrixGridStyle = { "--matrix-cols": visibleCols.length } as CSSProperties;

  return { ...context, activeDimension, fallbackFinding, getCell, getCellFromKey, windowForColumn, findBestFindingKey, visibleRowIds, visibleCols, matrixGridStyle };
}

export type MatrixFilterModel = ReturnType<typeof buildMatrixFilterModel>;
