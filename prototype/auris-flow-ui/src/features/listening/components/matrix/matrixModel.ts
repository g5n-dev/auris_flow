import {
  matrixDimensions,
  matrixFindings,
  matrixRows
} from "../../fixtures/matrixFixtures";
import type { MatrixFinding, MatrixModeProps } from "./matrixModeTypes";

export function buildMatrixModel(props: MatrixModeProps) {
  const {
  setMode,
  selectedWindow,
  setSelectedWindow,
  setMarkState
} = props;
  const findingByKey = Object.fromEntries(matrixFindings.map((finding) => [finding.key, finding])) as Record<string, MatrixFinding>;
  return { setMode, selectedWindow, setSelectedWindow, setMarkState, matrixRows, matrixFindings, findingByKey, matrixDimensions };
}

export type MatrixModel = ReturnType<typeof buildMatrixModel>;
