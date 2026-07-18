import { InsightsEmptyProjection } from "./components/InsightsEmptyProjection";
import { InsightsModule } from "./InsightsModule";
import type { InsightsModuleProps } from "./types";

export type InsightsFeatureProps =
  | { mode: "empty"; activeTab: string }
  | ({ mode: "full" } & InsightsModuleProps);

export default function InsightsFeature(props: InsightsFeatureProps) {
  if (props.mode === "empty") {
    return <InsightsEmptyProjection activeTab={props.activeTab} />;
  }

  const { mode: _mode, ...moduleProps } = props;
  return <InsightsModule {...moduleProps} />;
}
