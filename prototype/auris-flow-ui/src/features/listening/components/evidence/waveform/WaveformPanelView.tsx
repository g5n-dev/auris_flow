import { LazyBranchBoundary } from "../../../../../shared/ui/LazyBranchBoundary";
import { TrackEditor } from "./TrackEditor";
import { TrackRegionDialog } from "./TrackRegionDialog";
import type { WaveformPanelController } from "./trackRegionModalActions";
import { WaveformOverview } from "./WaveformOverview";

export function WaveformPanelView({ controller }: { controller: WaveformPanelController }) {
  const { modalRegion, modalTrack } = controller;
  return (
    (
        <>
          <WaveformOverview controller={controller} />

          <TrackEditor controller={controller} />

          {modalRegion && modalTrack && (
            <LazyBranchBoundary label="轨道标注编辑器" minHeight={520} resetKey={modalRegion.id} testId="listening-track-dialog">
              <TrackRegionDialog controller={controller} />
            </LazyBranchBoundary>
          )}
        </>
      )
  );
}
