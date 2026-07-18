import type { MatrixModel } from "./matrixModel";
import type { MatrixDimension, MatrixModeProps, MatrixStatus } from "./matrixModeTypes";
import { useState } from "react";

export function useMatrixFilterState(context: MatrixModel) {
  const [activeDimensionKey, setActiveDimensionKey] = useState<MatrixDimension["key"]>("space");

  const [showFullMatrix, setShowFullMatrix] = useState(false);

  const [statusFilter, setStatusFilter] = useState<MatrixStatus | null>(null);

  const [onlyAnomalies, setOnlyAnomalies] = useState(false);

  const [previewState, setPreviewState] = useState<"idle" | "playing">("idle");

  const [reviewReceipt, setReviewReceipt] = useState("已载入空间路径：按邻近设备定位主录音和串音候选。");

  return { ...context, activeDimensionKey, setActiveDimensionKey, showFullMatrix, setShowFullMatrix, statusFilter, setStatusFilter, onlyAnomalies, setOnlyAnomalies, previewState, setPreviewState, reviewReceipt, setReviewReceipt };
}

export type MatrixFilterState = ReturnType<typeof useMatrixFilterState>;
