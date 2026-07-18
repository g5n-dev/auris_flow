import { CanvasWorkspaceView } from "./components/CanvasWorkspaceView";
import { useCanvasController } from "./controller/useCanvasController";
import type { CanvasModuleProps } from "./types";

export function CanvasModule(props: CanvasModuleProps) {
  const controller = useCanvasController(props);
  return <CanvasWorkspaceView controller={controller} />;
}
