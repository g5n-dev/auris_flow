import { LabelsWorkspaceView } from "./components/LabelsWorkspaceView";
import { useLabelsController } from "./controller/useLabelsController";
import type { LabelsModuleProps } from "./types";

export function LabelsModule(props: LabelsModuleProps) {
  const controller = useLabelsController(props);
  return <LabelsWorkspaceView controller={controller} />;
}
