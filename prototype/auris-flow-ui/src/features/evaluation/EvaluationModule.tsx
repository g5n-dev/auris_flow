import { EvaluationWorkspaceView } from "./components/EvaluationWorkspaceView";
import { useEvaluationController } from "./controller/useEvaluationController";
import type { EvaluationModuleProps } from "./types";

export default function EvaluationModule(props: EvaluationModuleProps) {
  const controller = useEvaluationController(props);

  return <EvaluationWorkspaceView controller={controller} />;
}
