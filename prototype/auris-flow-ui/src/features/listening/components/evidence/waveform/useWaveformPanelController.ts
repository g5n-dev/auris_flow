import { useWaveformPanelState } from "./useWaveformPanelState";
import { buildTrackEditorModel } from "./trackEditorModel";
import { createTrackEditorActions } from "./trackEditorActions";
import { createTrackRegionActions } from "./trackRegionActions";
import { buildTrackRegionModalModel } from "./trackRegionModalModel";
import { createTrackRegionModalActions } from "./trackRegionModalActions";
import type { WaveformPanelProps } from "./waveformPanelTypes";

export function useWaveformPanelController(props: WaveformPanelProps) {
  const step1 = useWaveformPanelState(props);
  const step2 = buildTrackEditorModel(step1);
  const step3 = createTrackEditorActions(step2);
  const step4 = createTrackRegionActions(step3);
  const step5 = buildTrackRegionModalModel(step4);
  return createTrackRegionModalActions(step5);
}

export type WaveformPanelController = ReturnType<typeof useWaveformPanelController>;
