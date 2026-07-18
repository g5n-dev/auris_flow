import { buildMatrixModel } from "./matrixModel";
import { useMatrixFilterState } from "./useMatrixFilterState";
import { buildMatrixFilterModel } from "./matrixFilterModel";
import { useMatrixSelectionState } from "./useMatrixSelectionState";
import { createMatrixActions } from "./matrixActions";
import type { MatrixModeProps } from "./matrixModeTypes";

export function useMatrixController(props: MatrixModeProps) {
  const step1 = buildMatrixModel(props);
  const step2 = useMatrixFilterState(step1);
  const step3 = buildMatrixFilterModel(step2);
  const step4 = useMatrixSelectionState(step3);
  return createMatrixActions(step4);
}

export type MatrixController = ReturnType<typeof useMatrixController>;
