import { InsightsWorkspaceView } from "./components/InsightsWorkspaceView";
import { useInsightsController } from "./controller/useInsightsController";
import type { InsightsModuleProps } from "./types";

export function InsightsModule(props: InsightsModuleProps) {
  const controller = useInsightsController(props);

  return <InsightsWorkspaceView controller={controller} />;
}
