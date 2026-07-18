import type { WaveformPanelProps } from "./waveformPanelTypes";
import { WaveformPanelView } from "./WaveformPanelView";
import { useWaveformPanelController } from "./useWaveformPanelController";

export function WaveformPanel(props: WaveformPanelProps) {
  const controller = useWaveformPanelController(props);
  return <WaveformPanelView controller={controller} />;
}
