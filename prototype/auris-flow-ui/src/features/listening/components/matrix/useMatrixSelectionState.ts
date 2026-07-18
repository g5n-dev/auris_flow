import type { MatrixFilterModel } from "./matrixFilterModel";
import type { MatrixModeProps } from "./matrixModeTypes";
import { useState } from "react";

export function useMatrixSelectionState(context: MatrixFilterModel) {
  const [activeCell, setActiveCell] = useState("sales-b-3");

  return { ...context, activeCell, setActiveCell };
}

export type MatrixSelectionState = ReturnType<typeof useMatrixSelectionState>;
