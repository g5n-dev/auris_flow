import { labelTrackMeta } from "../../../fixtures/evidenceFixtures";
import type { TrackRegion } from "../../../model/trackLayout";
import { percentToClock } from "../../../model/trackLayout";
import { createManualLabelDraftActions } from "./manualLabelDraftActions";
import { createManualLabelRebaseActions } from "./manualLabelRebaseActions";
import type { TrackRegionModalModel } from "./trackRegionModalModel";

export function createTrackRegionModalActions(context: TrackRegionModalModel) {
  const {
    allTracks,
    customLayers,
    dragState,
    endRegionDrag,
    hideTag,
    lastCreatedAnnotationId,
    modalRegion,
    moveRegionDrag,
    previewWindowForRegion,
    regionDragClickGuard,
    selectedRegion,
    setActiveTrack,
    setRegionEdits,
    setSelectedRegion,
    setTrackPreviewState,
    setTrackRegionModalId,
    startRegionDrag,
    trackLayouts,
    trackPreviewState
  } = context;
  const {
    loadManualLabelScope,
    saveModalAnnotationDraft,
    submitModalAnnotationDraft
  } = createManualLabelDraftActions(context);
  const {
    confirmModalAnnotationRebase,
    previewModalAnnotationRebase,
    setManualLabelSelection,
    setManualMappingBundleId,
    setManualRebaseConfirmed
  } = createManualLabelRebaseActions(context);

  const startTrackPreview = (region: TrackRegion, clip: "before" | "current" | "after") => {
    const windowText = previewWindowForRegion(region, clip);
    setTrackPreviewState((current) => {
      const sameClip = current?.regionId === region.id && current.clip === clip;
      return {
        regionId: region.id,
        clip,
        windowText,
        playing: sameClip ? !current.playing : true
      };
    });
  };

  const updateModalRegion = (patch: Partial<TrackRegion>) => {
    if (!modalRegion) return;
    setRegionEdits((current) => ({
      ...current,
      [modalRegion.id]: {
        ...modalRegion,
        ...(current[modalRegion.id] ?? {}),
        ...patch
      }
    }));
    setSelectedRegion(modalRegion.id);
  };

  const openTrackRegionModal = (track: (typeof allTracks)[number], region: TrackRegion) => {
    setSelectedRegion(region.id);
    setActiveTrack(track.key);
    setTrackRegionModalId(region.id);
    setTrackPreviewState(null);
    void loadManualLabelScope(track, region);
  };

  const trackLevelLabel = (track: { key: string }) => {
    const customLayer = customLayers.find((layer) => layer.key === track.key);
    if (customLayer) return `L${customLayer.level}+`;
    const baseIndex = labelTrackMeta.findIndex((item) => item.key === track.key);
    return `L${baseIndex + 1}`;
  };

  const renderTrackRegions = (layout: (typeof trackLayouts)[number]) => {
    const { track, regions, height } = layout;
    return (
      <div key={track.key} className="tr" style={{ height, flex: "0 0 auto" }}>
        <div className="gd" />
        {regions.map((region) => (
          <button
            key={region.id}
            className={[
              "rg",
              region.tone,
              region.laneCount > 1 ? "stk" : "",
              selectedRegion === region.id ? "sl" : "",
              trackPreviewState?.regionId === region.id && trackPreviewState.playing ? "previewing" : "",
              lastCreatedAnnotationId === region.id ? "just-created" : "",
              dragState?.id === region.id ? "dragging" : ""
            ].join(" ")}
            style={{
              left: `${region.left}%`,
              width: `${region.width}%`,
              top: region.laneCount > 1 ? `${4 + region.lane * 22}px` : undefined,
              bottom: region.laneCount > 1 ? "auto" : undefined,
              height: region.laneCount > 1 ? "18px" : undefined
            }}
            onPointerDown={() => setSelectedRegion(region.id)}
            onPointerMove={(event) => moveRegionDrag(event, region)}
            onPointerUp={endRegionDrag}
            onPointerCancel={endRegionDrag}
            onClick={() => {
              if (regionDragClickGuard.current) {
                regionDragClickGuard.current = false;
                return;
              }
              openTrackRegionModal(track, region);
            }}
            title={`${track.label} · ${region.label}`}
          >
            <span className="rg-h lh" onPointerDown={(event) => startRegionDrag(event, region, "left")} />
            <span className="gp" />
            <span className="lb">{region.label}</span>
            <span className="rg-time">
              {percentToClock(region.left)}-{percentToClock(region.left + region.width)}
            </span>
            <span className="rg-h rh" onPointerDown={(event) => startRegionDrag(event, region, "right")} />
            <span
              className="rg-x"
              role="button"
              tabIndex={0}
              onPointerDown={(event) => event.stopPropagation()}
              onClick={(event) => {
                event.stopPropagation();
                hideTag(track.key, region.label);
              }}
            >
              ×
            </span>
          </button>
        ))}
      </div>
    );
  };

  return {
    ...context,
    loadManualLabelScope,
    saveModalAnnotationDraft,
    submitModalAnnotationDraft,
    confirmModalAnnotationRebase,
    previewModalAnnotationRebase,
    setManualLabelSelection,
    setManualMappingBundleId,
    setManualRebaseConfirmed,
    startTrackPreview,
    updateModalRegion,
    openTrackRegionModal,
    trackLevelLabel,
    renderTrackRegions
  };
}

export type WaveformPanelController = ReturnType<typeof createTrackRegionModalActions>;
