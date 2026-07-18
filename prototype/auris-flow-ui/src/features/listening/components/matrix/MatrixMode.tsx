import type { MatrixModeProps } from "./matrixModeTypes";
import { MatrixModeView } from "./MatrixModeView";
import { useMatrixController } from "./useMatrixController";

export function MatrixMode(props: MatrixModeProps) {
  const controller = useMatrixController(props);
  return <MatrixModeView controller={controller} />;
}
